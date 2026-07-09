import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_power_guard import (
    DeviceRef,
    SPF_EXPECTED_OUTPUT_CONFIG,
    extract_soc,
    extract_spf_output_source,
    render_params,
    set_mode,
    summarize_status,
    verify_mode_switch,
    command_watchdog_sbu,
)
from growatt_guard.growatt_api import detect_grid_bypass, detect_unexpected_grid_bypass, extract_battery_status, estimate_runtime, estimate_charge_time, estimate_topup_for_sunrise, format_duration_minutes


class GrowattApiTests(unittest.TestCase):
    def test_render_params_replaces_placeholders_inside_json(self):
        device = DeviceRef("plant123", "SN123", "storage", {})
        template = (
            '{"op":"storageSet","serialNum":"{device_sn}",'
            '"plant":"{plant_id}","mode":"{mode}","param1":"2"}'
        )

        self.assertEqual(
            render_params(template, device, "sbu"),
            {
                "op": "storageSet",
                "serialNum": "SN123",
                "plant": "plant123",
                "mode": "sbu",
                "param1": "2",
            },
        )

    def test_extract_soc_finds_nested_percentage(self):
        status = {"storage_params": {"storageDetailBean": {"capacity": "44%"}}}

        self.assertEqual(extract_soc(status), (44.0, "storage_params.storageDetailBean.capacity"))

    def test_extract_spf_output_source(self):
        status = {"storage_params": {"storageDetailBean": {"outputConfig": 2}}}

        self.assertEqual(
            extract_spf_output_source(status),
            ("2", "Utility first", "storage_params.storageDetailBean.outputConfig"),
        )

    def test_spf5000_driver_prepares_expected_dry_run_params(self):
        config = make_config()
        device = DeviceRef("plant123", "SN123", "storage", {})

        self.assertEqual(
            set_mode(None, config, device, "utility"),
            {
                "dry_run": True,
                "mode": "utility",
                "path": "tcpSet.do",
                "method": "post_params",
                "params": {
                    "action": "storageSPF5000Set",
                    "serialNum": "SN123",
                    "type": "storage_spf5000_ac_output_source",
                    "param1": "2",
                    "param2": "",
                    "param3": "",
                    "param4": "",
                },
            },
        )

    def test_watchdog_sbu_does_nothing_when_already_sbu(self):
        config = make_config()

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.audit.LOG_DIR", Path(tmpdir)), patch(
            "growatt_guard.audit.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"
        ), patch("growatt_guard.state.PAUSE_FILE", Path(tmpdir) / "pause.json"), patch(
            "growatt_guard.modes.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), {"storage_params": {"outputConfig": "0"}}),
        ), patch("growatt_guard.modes.set_mode") as set_mode_mock, redirect_stdout(StringIO()):
            self.assertEqual(command_watchdog_sbu(config), 0)

        set_mode_mock.assert_not_called()

    def test_watchdog_sbu_retries_when_not_sbu(self):
        import datetime as _dt, json as _json
        config = make_config()

        with TemporaryDirectory() as tmpdir:
            hold_file = Path(tmpdir) / "utility_hold.json"
            # Use expired max_expiry so watchdog proceeds to repair (not ceiling-hold).
            expiry = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)).isoformat()
            hold_file.write_text(
                _json.dumps({"ownership": "owned", "target_soc": 40.0, "max_expiry": expiry, "started_at": expiry}),
                encoding="utf-8",
            )
            with patch("growatt_guard.audit.LOG_DIR", Path(tmpdir)), patch(
                "growatt_guard.audit.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"
            ), patch("growatt_guard.state.PAUSE_FILE", Path(tmpdir) / "pause.json"), patch(
                "growatt_guard.state.UTILITY_HOLD_FILE", hold_file
            ), patch(
                "growatt_guard.modes.load_context",
                return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), {"storage_params": {"outputConfig": "2"}}),
            ), patch("growatt_guard.modes.set_mode", return_value={"success": True}) as set_mode_mock, patch(
                "logging.warning"
            ), redirect_stdout(StringIO()):
                self.assertEqual(command_watchdog_sbu(config), 0)

        set_mode_mock.assert_called_once()


class IdempotencyTests(unittest.TestCase):
    def _audit_patch(self, tmpdir):
        from pathlib import Path
        return [
            patch("growatt_guard.audit.LOG_DIR", Path(tmpdir)),
            patch("growatt_guard.audit.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"),
        ]

    def test_preserve_battery_skips_when_already_utility(self):
        from growatt_guard.modes import command_preserve_battery
        from contextlib import redirect_stdout
        from io import StringIO
        from tempfile import TemporaryDirectory

        config = make_config(low_battery_soc=50)
        status = {
            "device": {"capacity": "40 %"},
            "storage_params": {"storageBean": {"outputConfig": "2"}},
        }
        with TemporaryDirectory() as tmpdir:
            with self._audit_patch(tmpdir)[0], self._audit_patch(tmpdir)[1], \
                 patch("growatt_guard.modes.load_context", return_value=(None, DeviceRef("p", "s", "storage", {}), status)), \
                 patch("growatt_guard.modes.set_mode") as mock_set, \
                 patch("growatt_guard.modes.ensure_not_paused", return_value=False), \
                 redirect_stdout(StringIO()):
                result = command_preserve_battery(config)
        self.assertEqual(result, 0)
        mock_set.assert_not_called()

    def test_preserve_battery_retries_failed_utility_switch(self):
        from growatt_guard.exceptions import GrowattGuardError
        from growatt_guard.modes import command_preserve_battery
        from growatt_guard.weather import ThresholdDecision
        from contextlib import redirect_stdout
        from io import StringIO
        from tempfile import TemporaryDirectory

        config = make_config(
            dry_run=False,
            preserve_utility_max_attempts=2,
            preserve_utility_retry_delay_seconds=0,
        )
        status = {
            "device": {"capacity": "31%"},
            "storage_params": {"storageBean": {"outputConfig": "0"}},
        }
        with TemporaryDirectory() as tmpdir:
            with (
                self._audit_patch(tmpdir)[0],
                self._audit_patch(tmpdir)[1],
                patch("growatt_guard.modes.load_context", return_value=(None, DeviceRef("p", "s", "storage", {}), status)),
                patch("growatt_guard.modes.choose_preserve_threshold", return_value=ThresholdDecision(45, "rainy/cloudy", "rainy/cloudy")),
                patch("growatt_guard.modes.set_mode", side_effect=[GrowattGuardError("temporary failure"), {"success": True}]) as mock_set,
                patch("growatt_guard.modes.verify_mode_switch", return_value=True),
                patch("growatt_guard.modes.write_utility_hold_state"),
                redirect_stdout(StringIO()),
            ):
                result = command_preserve_battery(config)
            content = (Path(tmpdir) / "mode_decisions.csv").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(mock_set.call_count, 2)
        self.assertIn("attempts=2", content)

    def test_return_sbu_skips_when_already_sbu(self):
        from growatt_guard.modes import command_return_sbu
        from contextlib import redirect_stdout
        from io import StringIO
        from tempfile import TemporaryDirectory

        config = make_config()
        status = {
            "device": {"capacity": "70 %"},
            "storage_params": {"storageBean": {"outputConfig": "0"}},
        }
        with TemporaryDirectory() as tmpdir:
            with self._audit_patch(tmpdir)[0], self._audit_patch(tmpdir)[1], \
                 patch("growatt_guard.modes.load_context", return_value=(None, DeviceRef("p", "s", "storage", {}), status)), \
                 patch("growatt_guard.modes.set_mode") as mock_set, \
                 patch("growatt_guard.modes.ensure_not_paused", return_value=False), \
                 redirect_stdout(StringIO()):
                result = command_return_sbu(config)
        self.assertEqual(result, 0)
        mock_set.assert_not_called()

    def test_return_sbu_clears_existing_utility_hold_when_already_sbu(self):
        from growatt_guard.modes import command_return_sbu
        from growatt_guard.state import read_utility_hold_state, write_utility_hold_state
        from contextlib import redirect_stdout
        from io import StringIO
        from tempfile import TemporaryDirectory
        import datetime as dt

        config = make_config(dry_run=False)
        status = {
            "device": {"capacity": "70 %"},
            "storage_params": {"storageBean": {"outputConfig": "0"}},
        }
        with TemporaryDirectory() as tmpdir:
            hold_file = Path(tmpdir) / "utility_hold.json"
            with self._audit_patch(tmpdir)[0], self._audit_patch(tmpdir)[1], \
                 patch("growatt_guard.state.UTILITY_HOLD_FILE", hold_file):
                write_utility_hold_state(
                    "owned",
                    50.0,
                    dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
                    start_soc=40.0,
                )
                with patch("growatt_guard.modes.load_context", return_value=(None, DeviceRef("p", "s", "storage", {}), status)), \
                     patch("growatt_guard.modes.set_mode") as mock_set, \
                     patch("growatt_guard.modes.ensure_not_paused", return_value=False), \
                     redirect_stdout(StringIO()):
                    result = command_return_sbu(config)
                state = read_utility_hold_state()
        self.assertEqual(result, 0)
        self.assertIsNone(state)
        mock_set.assert_not_called()

    def test_preserve_battery_switches_when_not_already_utility(self):
        from growatt_guard.modes import command_preserve_battery
        from contextlib import redirect_stdout
        from io import StringIO
        from tempfile import TemporaryDirectory

        config = make_config(low_battery_soc=50)
        status = {
            "device": {"capacity": "40 %"},
            "storage_params": {"storageBean": {"outputConfig": "0"}},
        }
        with TemporaryDirectory() as tmpdir:
            with self._audit_patch(tmpdir)[0], self._audit_patch(tmpdir)[1], \
                 patch("growatt_guard.modes.load_context", return_value=(None, DeviceRef("p", "s", "storage", {}), status)), \
                 patch("growatt_guard.modes.set_mode", return_value={"dry_run": True}) as mock_set, \
                 patch("growatt_guard.modes.ensure_not_paused", return_value=False), \
                 patch("growatt_guard.modes.verify_mode_switch", return_value=None), \
                 redirect_stdout(StringIO()):
                result = command_preserve_battery(config)
        self.assertEqual(result, 0)
        mock_set.assert_called_once()

    def test_preserve_battery_records_utility_hold_after_verified_switch(self):
        from growatt_guard.modes import command_preserve_battery
        from growatt_guard.state import read_utility_hold_state
        from contextlib import redirect_stdout
        from io import StringIO
        from tempfile import TemporaryDirectory
        import datetime as dt

        config = make_config(low_battery_soc=50, dry_run=False, discord_notify_success=False)
        status = {
            "device": {"capacity": "40 %"},
            "storage_params": {"storageBean": {"outputConfig": "0"}},
        }
        expiry = dt.datetime(2026, 6, 29, 14, 55, tzinfo=dt.timezone.utc)
        with TemporaryDirectory() as tmpdir:
            hold_file = Path(tmpdir) / "utility_hold.json"
            with self._audit_patch(tmpdir)[0], self._audit_patch(tmpdir)[1], \
                 patch("growatt_guard.state.UTILITY_HOLD_FILE", hold_file), \
                 patch("growatt_guard.modes.load_context", return_value=(object(), DeviceRef("p", "s", "storage", {}), status)), \
                 patch("growatt_guard.modes.set_mode", return_value={"success": True}), \
                 patch("growatt_guard.modes.ensure_not_paused", return_value=False), \
                 patch("growatt_guard.modes.verify_mode_switch", return_value=True), \
                 patch("growatt_guard.modes._preserve_hold_expiry", return_value=expiry), \
                 redirect_stdout(StringIO()):
                result = command_preserve_battery(config)
                state = read_utility_hold_state()
        self.assertEqual(result, 0)
        self.assertIsNotNone(state)
        self.assertEqual(state["ownership"], "owned")
        self.assertEqual(state["target_soc"], 50)
        self.assertEqual(state["start_soc"], 40.0)

    def test_preserve_battery_does_not_record_hold_when_verify_fails(self):
        from growatt_guard.modes import command_preserve_battery
        from growatt_guard.state import read_utility_hold_state
        from contextlib import redirect_stdout
        from io import StringIO
        from tempfile import TemporaryDirectory

        config = make_config(low_battery_soc=50, dry_run=False, discord_notify_success=False)
        status = {
            "device": {"capacity": "40 %"},
            "storage_params": {"storageBean": {"outputConfig": "0"}},
        }
        with TemporaryDirectory() as tmpdir:
            hold_file = Path(tmpdir) / "utility_hold.json"
            with self._audit_patch(tmpdir)[0], self._audit_patch(tmpdir)[1], \
                 patch("growatt_guard.state.UTILITY_HOLD_FILE", hold_file), \
                 patch("growatt_guard.modes.load_context", return_value=(object(), DeviceRef("p", "s", "storage", {}), status)), \
                 patch("growatt_guard.modes.set_mode", return_value={"success": True}), \
                 patch("growatt_guard.modes.ensure_not_paused", return_value=False), \
                 patch("growatt_guard.modes.verify_mode_switch", return_value=False), \
                 redirect_stdout(StringIO()):
                result = command_preserve_battery(config)
                state = read_utility_hold_state()
        self.assertEqual(result, 0)
        self.assertIsNone(state)

    def test_force_utility_skips_when_already_utility(self):
        from growatt_guard.modes import command_force_utility
        from contextlib import redirect_stdout
        from io import StringIO
        from tempfile import TemporaryDirectory

        config = make_config()
        status = {
            "device": {"capacity": "70 %"},
            "storage_params": {"storageBean": {"outputConfig": "2"}},
        }
        with TemporaryDirectory() as tmpdir:
            with self._audit_patch(tmpdir)[0], self._audit_patch(tmpdir)[1], \
                 patch("growatt_guard.modes.load_context", return_value=(None, DeviceRef("p", "s", "storage", {}), status)), \
                 patch("growatt_guard.modes.set_mode") as mock_set, \
                 redirect_stdout(StringIO()):
                result = command_force_utility(config, "test")
        self.assertEqual(result, 0)
        mock_set.assert_not_called()

    def test_force_utility_switches_when_not_already_utility(self):
        from growatt_guard.modes import command_force_utility
        from contextlib import redirect_stdout
        from io import StringIO
        from tempfile import TemporaryDirectory

        config = make_config()
        status = {
            "device": {"capacity": "70 %"},
            "storage_params": {"storageBean": {"outputConfig": "0"}},
        }
        with TemporaryDirectory() as tmpdir:
            with self._audit_patch(tmpdir)[0], self._audit_patch(tmpdir)[1], \
                 patch("growatt_guard.modes.load_context", return_value=(object(), DeviceRef("p", "s", "storage", {}), status)), \
                 patch("growatt_guard.modes.set_mode", return_value={"dry_run": True}) as mock_set, \
                 patch("growatt_guard.modes.verify_mode_switch", return_value=None), \
                 redirect_stdout(StringIO()):
                result = command_force_utility(config, "test")
        self.assertEqual(result, 0)
        mock_set.assert_called_once()


class LoadContextRetryTests(unittest.TestCase):
    def _make_context(self):
        device = DeviceRef("plant1", "SN1", "storage", {})
        status = {"storage_params": {"storageBean": {"outputConfig": "0"}}}
        return (object(), device, status)

    def test_succeeds_on_first_attempt(self):
        from growatt_guard.growatt_api import load_context
        ctx = self._make_context()
        with patch("growatt_guard.growatt_api.connect", return_value=(object(), {})), \
             patch("growatt_guard.growatt_api.choose_plant", return_value="p1"), \
             patch("growatt_guard.growatt_api.choose_device", return_value=ctx[1]), \
             patch("growatt_guard.growatt_api.read_device_status", return_value=ctx[2]), \
             patch("growatt_guard.growatt_api.record_growatt_cloud_success"), \
             patch("growatt_guard.growatt_api.summarize_status", return_value="ok"):
            result = load_context(make_config())
        self.assertIsNotNone(result)

    def test_retries_on_transient_error_then_succeeds(self):
        from growatt_guard.growatt_api import load_context
        ctx = self._make_context()
        call_count = {"n": 0}

        def flaky_connect(_config):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise ConnectionError("timeout")
            return (object(), {})

        with patch("growatt_guard.growatt_api.connect", side_effect=flaky_connect), \
             patch("growatt_guard.growatt_api.choose_plant", return_value="p1"), \
             patch("growatt_guard.growatt_api.choose_device", return_value=ctx[1]), \
             patch("growatt_guard.growatt_api.read_device_status", return_value=ctx[2]), \
             patch("growatt_guard.growatt_api.record_growatt_cloud_success"), \
             patch("growatt_guard.growatt_api.summarize_status", return_value="ok"), \
             patch("growatt_guard.growatt_api.time.sleep"):
            result = load_context(make_config())
        self.assertEqual(call_count["n"], 2)
        self.assertIsNotNone(result)

    def test_raises_after_max_attempts(self):
        from growatt_guard.growatt_api import load_context
        from growatt_guard.exceptions import GrowattGuardError
        with patch("growatt_guard.growatt_api.connect", side_effect=ConnectionError("timeout")), \
             patch("growatt_guard.growatt_api.time.sleep"):
            with self.assertRaises(GrowattGuardError):
                load_context(make_config(), max_attempts=2)

    def test_does_not_retry_on_growatt_guard_error(self):
        from growatt_guard.growatt_api import load_context
        from growatt_guard.exceptions import GrowattGuardError
        call_count = {"n": 0}

        def failing_connect(_config):
            call_count["n"] += 1
            raise GrowattGuardError("auth failed")

        with patch("growatt_guard.growatt_api.connect", side_effect=failing_connect):
            with self.assertRaises(GrowattGuardError):
                load_context(make_config())
        self.assertEqual(call_count["n"], 1)


class VerifyModeSwitchTests(unittest.TestCase):
    def _device(self):
        return DeviceRef("plant1", "SN1", "storage", {})

    def _status(self, output_config: str) -> dict:
        return {"storage_params": {"storageDetailBean": {"outputConfig": output_config}}}

    def test_returns_true_when_config_matches_utility(self):
        api = object()
        with patch("growatt_guard.growatt_api.read_device_status", return_value=self._status("2")), \
             patch("growatt_guard.growatt_api.time.sleep"):
            result = verify_mode_switch(api, self._device(), "utility", delay_seconds=0)
        self.assertTrue(result)

    def test_returns_true_when_config_matches_sbu(self):
        api = object()
        with patch("growatt_guard.growatt_api.read_device_status", return_value=self._status("0")), \
             patch("growatt_guard.growatt_api.time.sleep"):
            result = verify_mode_switch(api, self._device(), "sbu", delay_seconds=0)
        self.assertTrue(result)

    def test_returns_false_when_config_does_not_match(self):
        api = object()
        with patch("growatt_guard.growatt_api.read_device_status", return_value=self._status("0")), \
             patch("growatt_guard.growatt_api.time.sleep"):
            result = verify_mode_switch(api, self._device(), "utility", delay_seconds=0)
        self.assertFalse(result)

    def test_returns_none_when_status_read_fails(self):
        api = object()
        with patch("growatt_guard.growatt_api.read_device_status", side_effect=Exception("network")), \
             patch("growatt_guard.growatt_api.time.sleep"):
            result = verify_mode_switch(api, self._device(), "utility", delay_seconds=0)
        self.assertIsNone(result)

    def test_returns_none_for_unknown_mode(self):
        result = verify_mode_switch(None, self._device(), "unknown_mode", delay_seconds=0)
        self.assertIsNone(result)

    def test_expected_configs(self):
        self.assertEqual(SPF_EXPECTED_OUTPUT_CONFIG["utility"], "2")
        self.assertEqual(SPF_EXPECTED_OUTPUT_CONFIG["sbu"], "0")

    def test_sleeps_before_reading(self):
        api = object()
        with patch("growatt_guard.growatt_api.read_device_status", return_value=self._status("2")), \
             patch("growatt_guard.growatt_api.time.sleep") as mock_sleep:
            verify_mode_switch(api, self._device(), "utility", delay_seconds=3)
        mock_sleep.assert_called_once_with(3)


class RuntimeEstimationTests(unittest.TestCase):
    # 2x SunmateMS 15kWh = 30,000 Wh total, 25% BMS cutoff
    CAPACITY = 30_000.0
    CUTOFF = 25.0

    def test_estimate_runtime_basic(self):
        # (62-25)/100 * 30000 / 1736 * 60 = 11100/1736*60 ≈ 383.87 min
        result = estimate_runtime(62.0, 1736.0, self.CAPACITY, self.CUTOFF)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 383.87, delta=0.5)

    def test_estimate_runtime_at_cutoff_returns_zero(self):
        result = estimate_runtime(25.0, 1736.0, self.CAPACITY, self.CUTOFF)
        self.assertEqual(result, 0.0)

    def test_estimate_runtime_below_cutoff_returns_zero(self):
        result = estimate_runtime(20.0, 1736.0, self.CAPACITY, self.CUTOFF)
        self.assertEqual(result, 0.0)

    def test_estimate_runtime_no_discharge_returns_none(self):
        self.assertIsNone(estimate_runtime(62.0, 0.0, self.CAPACITY, self.CUTOFF))

    def test_estimate_runtime_no_capacity_returns_none(self):
        self.assertIsNone(estimate_runtime(62.0, 1736.0, 0.0, self.CUTOFF))

    def test_estimate_charge_time_basic(self):
        # (100-62)/100 * 30000 / 1500 * 60 = 11400/1500*60 = 456 min
        result = estimate_charge_time(62.0, 1500.0, self.CAPACITY)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 456.0, delta=0.5)

    def test_estimate_charge_time_full_battery_returns_none(self):
        self.assertIsNone(estimate_charge_time(100.0, 1500.0, self.CAPACITY))

    def test_estimate_charge_time_no_charge_returns_none(self):
        self.assertIsNone(estimate_charge_time(62.0, 0.0, self.CAPACITY))

    def test_format_duration_minutes_under_an_hour(self):
        self.assertEqual(format_duration_minutes(45.0), "45min")

    def test_format_duration_minutes_exactly_one_hour(self):
        self.assertEqual(format_duration_minutes(60.0), "1h 00m")

    def test_format_duration_minutes_hours_and_minutes(self):
        self.assertEqual(format_duration_minutes(383.87), "6h 24m")

    def test_summarize_status_includes_runtime_when_capacity_set(self):
        status = {
            "plant_id": "p1", "device_sn": "SN1", "device_type": "storage",
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {
                    "bmsSoc": 62, "statusText": "Discharge",
                    "outPutPower": 1554, "loadPercent": 28.3,
                    "pDischarge": 1736, "pCharge": 0,
                },
            },
        }
        result = summarize_status(status, battery_capacity_wh=30_000.0, bms_cutoff_soc=25.0)
        self.assertIn("runtime_min=", result)
        m = __import__("re").search(r"runtime_min=(\d+)", result)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(int(m.group(1)), 384, delta=2)

    def test_summarize_status_no_runtime_when_capacity_zero(self):
        status = {
            "plant_id": "p1", "device_sn": "SN1", "device_type": "storage",
            "storage_params": {"storageDetailBean": {"bmsSoc": 62, "pDischarge": 1736, "pCharge": 0}},
        }
        result = summarize_status(status)  # no capacity passed
        self.assertNotIn("runtime_min=", result)
        self.assertNotIn("charge_min=", result)


class ExtractMetricsTests(unittest.TestCase):
    def test_extract_spf_output_source_prefers_bean_path(self):
        # device.outputConfig should lose to storageBean.outputConfig
        status = {
            "device": {"outputConfig": "3"},
            "storage_params": {"storageBean": {"outputConfig": "0"}},
        }
        raw, label, path = extract_spf_output_source(status)
        self.assertEqual(raw, "0")
        self.assertEqual(label, "SBU priority")
        self.assertIn("Bean", path)

    def test_extract_spf_output_source_falls_back_when_no_bean_path(self):
        status = {"device": {"outputConfig": "2"}}
        raw, label, path = extract_spf_output_source(status)
        self.assertEqual(raw, "2")
        self.assertEqual(label, "Utility first")

    def test_extract_battery_status_prefers_detail_bean(self):
        status = {
            "storage_params": {
                "storageBean": {"statusText": "storage.status.discharge"},
                "storageDetailBean": {"statusText": "Discharge"},
            }
        }
        self.assertEqual(extract_battery_status(status), "Discharge")

    def test_extract_battery_status_skips_dotted_values(self):
        # Internal key format like "storage.status.discharge" should not be returned
        status = {"storage_params": {"storageBean": {"statusText": "storage.status.discharge"}}}
        self.assertIsNone(extract_battery_status(status))

    def test_extract_battery_status_returns_none_when_absent(self):
        status = {"storage_params": {"storageDetailBean": {"bmsSoc": 62}}}
        self.assertIsNone(extract_battery_status(status))

    def test_detect_grid_bypass_from_status_text(self):
        status = {
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {"statusText": "AC charge and Bypass", "pCharge": 0},
            }
        }
        result = detect_grid_bypass(status)
        self.assertTrue(result["detected"])
        self.assertIn("AC charge and Bypass", result["reason"])
        self.assertEqual(result["output_raw"], "0")

    def test_detect_unexpected_grid_bypass_ignores_expected_utility_mode(self):
        status = {
            "storage_params": {
                "storageBean": {"outputConfig": "2", "pAcInPut": 900},
                "storageDetailBean": {"statusText": "Combine charge and Bypass", "pCharge": 131, "pDischarge": 0},
            }
        }

        result = detect_unexpected_grid_bypass(status)

        self.assertFalse(result["detected"])
        self.assertTrue(result["expected_utility"])
        self.assertEqual(result["reason"], "")

    def test_detect_unexpected_grid_bypass_ignores_low_soc_sbu_recovery(self):
        status = {
            "storage_params": {
                "storageBean": {"outputConfig": "0", "pAcInPut": 900},
                "storageDetailBean": {"bmsSoc": 35, "statusText": "AC charge and Bypass", "pCharge": 131, "pDischarge": 0},
            }
        }

        result = detect_unexpected_grid_bypass(status, recovery_soc=40)

        self.assertFalse(result["detected"])
        self.assertTrue(result["expected_recovery"])
        self.assertEqual(result["reason"], "")

    def test_detect_unexpected_grid_bypass_flags_sbu_bypass(self):
        status = {
            "storage_params": {
                "storageBean": {"outputConfig": "0", "pAcInPut": 900},
                "storageDetailBean": {"statusText": "AC charge and Bypass", "pCharge": 131, "pDischarge": 0},
            }
        }

        result = detect_unexpected_grid_bypass(status)

        self.assertTrue(result["detected"])
        self.assertFalse(result["expected_utility"])
        self.assertIn("AC charge and Bypass", result["reason"])

    def test_detect_grid_bypass_from_grid_charging_power(self):
        status = {
            "storage_params": {
                "storageBean": {"outputConfig": "0", "pGrid": 1800},
                "storageDetailBean": {"statusText": "Charging", "pCharge": 1200, "pDischarge": 0},
            }
        }
        result = detect_grid_bypass(status)
        self.assertTrue(result["detected"])
        self.assertEqual(result["grid_w"], 1800)
        self.assertEqual(result["charge_w"], 1200)

    def test_summarize_status_includes_live_metrics(self):
        status = {
            "plant_id": "p1",
            "device_sn": "SN1",
            "device_type": "storage",
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {
                    "bmsSoc": 62,
                    "statusText": "Discharge",
                    "outPutPower": 1554,
                    "loadPercent": 28.3,
                    "pDischarge": 1736,
                    "pCharge": 0,
                },
            },
        }
        result = summarize_status(status)
        self.assertIn("bat_status=Discharge", result)
        self.assertIn("out_w=1554", result)
        self.assertIn("load_pct=28", result)
        self.assertIn("bat_w=1736", result)

    def test_summarize_status_prefers_detail_charge_power_over_storage_bean_zero(self):
        status = {
            "plant_id": "p1",
            "device_sn": "SN1",
            "device_type": "storage",
            "storage_params": {
                "storageBean": {"outputConfig": "0", "pCharge": 0, "pDischarge": 0},
                "storageDetailBean": {
                    "bmsSoc": 99,
                    "statusText": "AC charge and Bypass",
                    "pCharge": 2026,
                    "pDischarge": 0,
                },
            },
        }

        result = summarize_status(status)

        self.assertIn("grid_bypass=detected", result)
        self.assertIn("bat_w=-2026", result)

    def test_summarize_status_includes_vbat(self):
        status = {
            "plant_id": "p1",
            "device_sn": "SN1",
            "device_type": "storage",
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {
                    "bmsSoc": 62,
                    "vBat": 52.3,
                    "pDischarge": 0,
                    "pCharge": 0,
                },
            },
        }
        result = summarize_status(status)
        self.assertIn("vbat=52.3", result)

    def test_summarize_status_includes_sunrise_fields(self):
        status = {
            "plant_id": "p1",
            "device_sn": "SN1",
            "device_type": "storage",
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {"bmsSoc": 62, "pDischarge": 1736, "pCharge": 0},
            },
        }
        result = summarize_status(
            status,
            battery_capacity_wh=30_000,
            bms_cutoff_soc=25,
            charge_rate_w=3000,
            hours_to_sunrise=6.0,
        )
        self.assertIn("sunrise_h=6.00", result)
        self.assertIn("topup_sunrise_min=", result)

    def test_summarize_status_no_sunrise_when_none(self):
        status = {
            "plant_id": "p1", "device_sn": "SN1", "device_type": "storage",
            "storage_params": {"storageDetailBean": {"bmsSoc": 62}},
        }
        result = summarize_status(status)
        self.assertNotIn("sunrise_h=", result)
        self.assertNotIn("topup_sunrise_min=", result)

    def test_summarize_status_no_vbat_when_absent(self):
        status = {
            "plant_id": "p1",
            "device_sn": "SN1",
            "device_type": "storage",
            "storage_params": {
                "storageDetailBean": {"bmsSoc": 62},
            },
        }
        result = summarize_status(status)
        self.assertNotIn("vbat=", result)


class TopupForSunriseTests(unittest.TestCase):
    # 30kWh capacity, 25% BMS cutoff, 3kW charge rate
    CAPACITY = 30_000.0
    CUTOFF = 25.0
    CHARGE_RATE = 3_000.0

    def test_no_topup_needed_when_sufficient(self):
        # SOC=80%, load=1000W, 5h to sunrise: usable=16500Wh, need=5000Wh → 0
        result = estimate_topup_for_sunrise(80.0, 1000.0, self.CAPACITY, self.CUTOFF, self.CHARGE_RATE, 5.0)
        self.assertEqual(result, 0.0)

    def test_topup_needed_accounts_for_charging_benefit(self):
        # SOC=30%, load=1500W, 6h to sunrise:
        # usable=(30-25)/100*30000=1500Wh, need=9000Wh, deficit=7500Wh
        # topup_min = 7500/(3000+1500)*60 = 100 min
        result = estimate_topup_for_sunrise(30.0, 1500.0, self.CAPACITY, self.CUTOFF, self.CHARGE_RATE, 6.0)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 100.0, delta=1.0)

    def test_returns_none_when_no_capacity(self):
        self.assertIsNone(estimate_topup_for_sunrise(62.0, 1000.0, 0.0, self.CUTOFF, self.CHARGE_RATE, 5.0))

    def test_returns_none_when_no_charge_rate(self):
        self.assertIsNone(estimate_topup_for_sunrise(62.0, 1000.0, self.CAPACITY, self.CUTOFF, 0.0, 5.0))

    def test_returns_none_when_no_hours(self):
        self.assertIsNone(estimate_topup_for_sunrise(62.0, 1000.0, self.CAPACITY, self.CUTOFF, self.CHARGE_RATE, 0.0))

    def test_returns_none_when_no_load(self):
        self.assertIsNone(estimate_topup_for_sunrise(62.0, 0.0, self.CAPACITY, self.CUTOFF, self.CHARGE_RATE, 5.0))

    def test_at_bms_cutoff_still_computes(self):
        # usable=0, any load → some topup needed
        result = estimate_topup_for_sunrise(25.0, 1000.0, self.CAPACITY, self.CUTOFF, self.CHARGE_RATE, 2.0)
        self.assertIsNotNone(result)
        self.assertGreater(result, 0.0)


class AutoTopupTargetSocTests(unittest.TestCase):
    """Tests for AUTO_TOPUP_TARGET_SOC sunrise buffer in command_auto_topup_check."""

    CAPACITY = 30_000.0
    CUTOFF = 25.0
    CHARGE_RATE = 3_000.0

    def test_higher_target_soc_produces_longer_topup(self):
        # Same conditions, higher target → more charging needed
        result_cutoff = estimate_topup_for_sunrise(40.0, 1500.0, self.CAPACITY, self.CUTOFF, self.CHARGE_RATE, 5.0)
        result_target = estimate_topup_for_sunrise(40.0, 1500.0, self.CAPACITY, 35.0, self.CHARGE_RATE, 5.0)
        self.assertGreater(result_target, result_cutoff)

    def test_target_soc_equal_to_cutoff_same_result(self):
        r1 = estimate_topup_for_sunrise(40.0, 1500.0, self.CAPACITY, self.CUTOFF, self.CHARGE_RATE, 5.0)
        r2 = estimate_topup_for_sunrise(40.0, 1500.0, self.CAPACITY, self.CUTOFF, self.CHARGE_RATE, 5.0)
        self.assertAlmostEqual(r1, r2)

    def test_command_uses_effective_target_soc(self):
        from growatt_guard.modes import command_auto_topup_check
        from growatt_guard import state as state_mod
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory

        status = {
            "datalogSn": "SN1", "deviceSn": "SN1",
            "data": {"soc": 40.0, "pDischarge": 1500.0, "outputConfig": "0"},
        }

        written_cutoff: dict = {}
        written_target: dict = {}

        def make_fake_write(store):
            def _fake(minutes, reason, paused_until, start_soc=None, start_load_w=None):
                store["minutes"] = minutes
            return _fake

        common_patches = lambda store: [
            patch("growatt_guard.modes.read_pause_state", return_value=None),
            patch("growatt_guard.modes.topup_is_active", return_value=False),
            patch("growatt_guard.modes.hours_until_next_sunrise", return_value=5.0),
            patch("growatt_guard.modes.load_context", return_value=(None, None, status)),
            patch("growatt_guard.modes.set_mode", return_value="ok"),
            patch("growatt_guard.modes.command_pause", return_value=0),
            patch("growatt_guard.modes.write_topup_state", side_effect=make_fake_write(store)),
            patch("growatt_guard.modes.write_utility_hold_state"),
            patch("growatt_guard.modes.append_mode_audit"),
        ]

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            cfg_cutoff = make_config(
                auto_topup_enabled=True, battery_capacity_wh=30000.0,
                battery_charge_rate_w=3000.0, battery_bms_cutoff_soc=25.0,
                auto_topup_target_soc=0.0, dry_run=True,
            )
            ps = common_patches(written_cutoff)
            with (patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "t1.json"),
                  patch.object(state_mod, "PAUSE_FILE", tmp / "p1.json"),
                  patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr1.json"),
                  ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7],
                  ps[8], redirect_stdout(StringIO())):
                command_auto_topup_check(cfg_cutoff)

            cfg_target = make_config(
                auto_topup_enabled=True, battery_capacity_wh=30000.0,
                battery_charge_rate_w=3000.0, battery_bms_cutoff_soc=25.0,
                auto_topup_target_soc=35.0, dry_run=True,
            )
            ps2 = common_patches(written_target)
            with (patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "t2.json"),
                  patch.object(state_mod, "PAUSE_FILE", tmp / "p2.json"),
                  patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr2.json"),
                  ps2[0], ps2[1], ps2[2], ps2[3], ps2[4], ps2[5], ps2[6], ps2[7],
                  ps2[8], redirect_stdout(StringIO())):
                command_auto_topup_check(cfg_target)

        self.assertIn("minutes", written_cutoff)
        self.assertIn("minutes", written_target)
        self.assertGreater(written_target["minutes"], written_cutoff["minutes"])

    def test_command_stores_completion_target_after_topup_window(self):
        from growatt_guard.modes import command_auto_topup_check
        from growatt_guard import state as state_mod
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory

        status = {
            "datalogSn": "SN1", "deviceSn": "SN1",
            "data": {"soc": 33.0, "pDischarge": 1707.1, "outputConfig": "0"},
        }
        cfg = make_config(
            auto_topup_enabled=True,
            auto_topup_min_hours_to_sunrise=5.0,
            auto_topup_solar_skip_min_margin_minutes=0.0,
            battery_capacity_wh=30000.0,
            battery_charge_rate_w=2100.0,
            battery_bms_cutoff_soc=25.0,
            discord_topup_max_minutes=210,
            dry_run=True,
        )

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            with (patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "t.json"),
                  patch.object(state_mod, "PAUSE_FILE", tmp / "p.json"),
                  patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr.json"),
                  patch("growatt_guard.modes.read_pause_state", return_value=None),
                  patch("growatt_guard.modes.topup_is_active", return_value=False),
                  patch("growatt_guard.modes.hours_until_next_sunrise", return_value=8.6),
                  patch("growatt_guard.modes.load_context", return_value=(None, None, status)),
                  patch("growatt_guard.modes.set_mode", return_value="ok"),
                  patch("growatt_guard.modes.command_pause", return_value=0),
                  patch("growatt_guard.modes.write_topup_state"),
                  patch("growatt_guard.modes.write_utility_hold_state") as hold_write,
                  patch("growatt_guard.modes.append_mode_audit"),
                  redirect_stdout(StringIO())):
                command_auto_topup_check(cfg)

        hold_write.assert_called_once()
        target_soc = hold_write.call_args.kwargs["target_soc"]
        self.assertAlmostEqual(target_soc, 55.5, delta=0.6)
        self.assertLess(target_soc, 60.0)

    def test_target_soc_below_cutoff_uses_cutoff(self):
        # target_soc=10 < bms_cutoff=25 → effective target = 25 (cutoff wins via max())
        result_low_target = estimate_topup_for_sunrise(40.0, 1500.0, self.CAPACITY, max(self.CUTOFF, 10.0), self.CHARGE_RATE, 5.0)
        result_cutoff = estimate_topup_for_sunrise(40.0, 1500.0, self.CAPACITY, self.CUTOFF, self.CHARGE_RATE, 5.0)
        self.assertAlmostEqual(result_low_target, result_cutoff)

    def test_target_note_appears_in_output(self):
        from growatt_guard.modes import command_auto_topup_check
        from growatt_guard import state as state_mod
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory

        status = {
            "datalogSn": "SN1", "deviceSn": "SN1",
            "data": {"soc": 40.0, "pDischarge": 1500.0, "outputConfig": "0"},
        }
        cfg = make_config(
            auto_topup_enabled=True, battery_capacity_wh=30000.0,
            battery_charge_rate_w=3000.0, battery_bms_cutoff_soc=25.0,
            auto_topup_target_soc=35.0, dry_run=True,
        )
        buf = StringIO()
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            with (patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "t.json"),
                  patch.object(state_mod, "PAUSE_FILE", tmp / "p.json"),
                  patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr.json"),
                  patch("growatt_guard.modes.read_pause_state", return_value=None),
                  patch("growatt_guard.modes.topup_is_active", return_value=False),
                  patch("growatt_guard.modes.hours_until_next_sunrise", return_value=5.0),
                  patch("growatt_guard.modes.load_context", return_value=(None, None, status)),
                  patch("growatt_guard.modes.set_mode", return_value="ok"),
                  patch("growatt_guard.modes.command_pause", return_value=0),
                  patch("growatt_guard.modes.write_topup_state"),
                  patch("growatt_guard.modes.write_utility_hold_state"),
                  patch("growatt_guard.modes.append_mode_audit"),
                  redirect_stdout(buf)):
                command_auto_topup_check(cfg)
        self.assertIn("target 35%", buf.getvalue())

    def test_pause_reason_and_embed_include_topup_target(self):
        from growatt_guard.modes import command_auto_topup_check
        from growatt_guard import state as state_mod
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory

        status = {
            "datalogSn": "SN1", "deviceSn": "SN1",
            "data": {"soc": 26.0, "pDischarge": 1682.4, "outputConfig": "0"},
        }
        cfg = make_config(
            auto_topup_enabled=True,
            battery_capacity_wh=30000.0,
            battery_charge_rate_w=3000.0,
            battery_bms_cutoff_soc=25.0,
            auto_topup_solar_skip_min_margin_minutes=60.0,
            discord_notify_success=True,
            dry_run=False,
        )

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            with (patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "t.json"),
                  patch.object(state_mod, "PAUSE_FILE", tmp / "p.json"),
                  patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr.json"),
                  patch("growatt_guard.modes.read_pause_state", return_value=None),
                  patch("growatt_guard.modes.topup_is_active", return_value=False),
                  patch("growatt_guard.modes.hours_until_next_sunrise", return_value=8.6),
                  patch("growatt_guard.modes.load_context", return_value=(None, None, status)),
                  patch("growatt_guard.modes.set_mode", return_value={"success": True}),
                  patch("growatt_guard.modes.command_pause", return_value=0) as pause_mock,
                  patch("growatt_guard.modes.write_topup_state"),
                  patch("growatt_guard.modes.write_utility_hold_state"),
                  patch("growatt_guard.modes.append_mode_audit"),
                  patch("growatt_guard.modes.send_discord_embed", return_value=True) as send_mock,
                  redirect_stdout(StringIO())):
                command_auto_topup_check(cfg)

        self.assertIn("topup target", pause_mock.call_args.args[2])
        embed = send_mock.call_args.args[1]
        field_names = [field["name"] for field in embed["fields"]]
        self.assertIn("Topup target", field_names)


class AutoTopupMinMinutesTests(unittest.TestCase):
    """Tests for AUTO_TOPUP_MIN_MINUTES skip threshold in command_auto_topup_check."""

    def _make_status(self, soc: float, discharge_w: float) -> dict:
        return {
            "datalogSn": "SN1",
            "deviceSn": "SN1",
            "data": {
                "soc": soc,
                "pDischarge": discharge_w,
                "outputConfig": "0",
            },
        }

    def test_skips_when_calculated_topup_below_minimum(self):
        from growatt_guard.modes import command_auto_topup_check
        from growatt_guard import state as state_mod
        from contextlib import redirect_stdout
        from io import StringIO

        # SOC=60%, load=2021W, 5.5h to sunrise → ~9 min calculated
        status = self._make_status(soc=60.0, discharge_w=2021.0)
        cfg = make_config(
            auto_topup_enabled=True,
            auto_topup_min_hours_to_sunrise=4.0,
            auto_topup_min_minutes=20.0,
            auto_topup_solar_skip_min_margin_minutes=0.0,
            battery_capacity_wh=30000.0,
            battery_charge_rate_w=3000.0,
            battery_bms_cutoff_soc=25.0,
            dry_run=True,
        )

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            written = {}

            def fake_write_topup_state(minutes, reason, paused_until, start_soc=None, start_load_w=None):
                written["minutes"] = minutes

            buf = StringIO()
            with (
                patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup_active.json"),
                patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
                patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr.json"),
                patch("growatt_guard.modes.read_pause_state", return_value=None),
                patch("growatt_guard.modes.topup_is_active", return_value=False),
                patch("growatt_guard.modes.hours_until_next_sunrise", return_value=5.5),
                patch("growatt_guard.modes.load_context", return_value=(None, None, status)),
                patch("growatt_guard.modes.set_mode", return_value="ok") as set_mode_mock,
                patch("growatt_guard.modes.command_pause", return_value=0) as pause_mock,
                patch("growatt_guard.modes.write_topup_state", side_effect=fake_write_topup_state),
                patch("growatt_guard.modes.append_mode_audit") as audit_mock,
                redirect_stdout(buf),
            ):
                command_auto_topup_check(cfg)

        self.assertNotIn("minutes", written)
        set_mode_mock.assert_not_called()
        pause_mock.assert_not_called()
        audit_mock.assert_called_once()
        self.assertEqual(audit_mock.call_args.kwargs["action"], "topup-skipped-short")
        self.assertIn("below AUTO_TOPUP_MIN_MINUTES=20", buf.getvalue())

    def test_minimum_does_not_skip_when_calculated_topup_exceeds_minimum(self):
        from growatt_guard.modes import command_auto_topup_check
        from growatt_guard import state as state_mod
        from contextlib import redirect_stdout
        from io import StringIO

        # SOC=30%, load=1500W, 6h to sunrise → ~100 min calculated
        status = self._make_status(soc=30.0, discharge_w=1500.0)
        cfg = make_config(
            auto_topup_enabled=True,
            auto_topup_min_hours_to_sunrise=4.0,
            auto_topup_min_minutes=20.0,
            battery_capacity_wh=30000.0,
            battery_charge_rate_w=3000.0,
            battery_bms_cutoff_soc=25.0,
            dry_run=True,
        )

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            written = {}

            def fake_write_topup_state(minutes, reason, paused_until, start_soc=None, start_load_w=None):
                written["minutes"] = minutes

            buf = StringIO()
            with (
                patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup_active.json"),
                patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
                patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr.json"),
                patch("growatt_guard.modes.read_pause_state", return_value=None),
                patch("growatt_guard.modes.topup_is_active", return_value=False),
                patch("growatt_guard.modes.hours_until_next_sunrise", return_value=6.0),
                patch("growatt_guard.modes.load_context", return_value=(None, None, status)),
                patch("growatt_guard.modes.set_mode", return_value="ok"),
                patch("growatt_guard.modes.command_pause", return_value=0),
                patch("growatt_guard.modes.write_topup_state", side_effect=fake_write_topup_state),
                patch("growatt_guard.modes.write_utility_hold_state"),
                patch("growatt_guard.modes.append_mode_audit"),
                redirect_stdout(buf),
            ):
                command_auto_topup_check(cfg)

        self.assertIn("minutes", written)
        self.assertGreater(written["minutes"], 20)


class ChargeRateHistoryTests(unittest.TestCase):
    def test_append_and_read_single_reading(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "cr.json"):
                readings = state_mod.append_charge_rate_reading(2800.0)
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0]["rate_w"], 2800)

    def test_trims_to_max_readings(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "cr.json"):
                for i in range(12):
                    readings = state_mod.append_charge_rate_reading(float(2000 + i * 100))
        self.assertEqual(len(readings), state_mod._CHARGE_RATE_MAX_READINGS)
        self.assertEqual(readings[-1]["rate_w"], 3100)

    def test_read_returns_empty_list_when_missing(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "cr.json"):
                result = state_mod.read_charge_rate_history()
        self.assertEqual(result, [])

    def test_accumulates_across_calls(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "cr.json"):
                state_mod.append_charge_rate_reading(2800.0)
                state_mod.append_charge_rate_reading(3000.0)
                readings = state_mod.read_charge_rate_history()
        self.assertEqual(len(readings), 2)
        self.assertEqual(readings[0]["rate_w"], 2800)
        self.assertEqual(readings[1]["rate_w"], 3000)


class TopupCompleteFeedbackTests(unittest.TestCase):
    """Tests for command_topup_complete_check charge-rate feedback."""

    def _write_audit(self, path: Path, rows: list[dict]) -> None:
        import csv as csv_mod
        from growatt_guard.audit import MODE_AUDIT_FIELDS
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=MODE_AUDIT_FIELDS)
            writer.writeheader()
            for row in rows:
                full = {k: "" for k in MODE_AUDIT_FIELDS}
                full.update(row)
                writer.writerow(full)

    def _make_status(self, soc: float) -> dict:
        return {
            "datalogSn": "SN1",
            "deviceSn": "SN1",
            "data": {
                "soc": soc,
                "pDischarge": 1000.0,
                "outputConfig": "1",
            },
        }

    def _make_topup_state(self, started_minutes_ago: float, start_soc: float, load_w: float, minutes: int) -> dict:
        import datetime as dt
        started_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=started_minutes_ago)).isoformat()
        paused_until = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()
        return {
            "started_at": started_at,
            "minutes": minutes,
            "paused_until": paused_until,
            "reason": "Auto-topup: test",
            "start_soc": start_soc,
            "start_load_w": load_w,
        }

    def test_topup_complete_prints_avg_rate_after_multiple_topups(self):
        from growatt_guard.modes import command_topup_complete_check
        from growatt_guard import state as state_mod

        state = self._make_topup_state(started_minutes_ago=100, start_soc=48.0, load_w=1000.0, minutes=100)
        end_status = self._make_status(soc=74.0)

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = make_config(
                battery_capacity_wh=30000.0,
                battery_charge_rate_w=3000.0,
                dry_run=True,
            )
            # Seed one prior reading so avg kicks in after this topup
            with patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", tmp / "cr.json"):
                state_mod.append_charge_rate_reading(2900.0)

            buf = StringIO()
            with (
                patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup_active.json"),
                patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
                patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", tmp / "cr.json"),
                patch("growatt_guard.audit.MODE_AUDIT_FILE", tmp / "mode_decisions.csv"),
                patch("growatt_guard.modes.read_topup_state", return_value=state),
                patch("growatt_guard.modes.read_utility_hold_state", return_value=None),
                patch("growatt_guard.modes.topup_is_active", return_value=False),
                patch("growatt_guard.modes.load_context", return_value=(None, None, end_status)),
                patch("growatt_guard.modes.command_resume", return_value=0),
                patch("growatt_guard.modes.clear_topup_state"),
                patch("growatt_guard.modes.command_return_sbu", return_value=0),
                redirect_stdout(buf),
            ):
                command_topup_complete_check(cfg)

        output = buf.getvalue()
        self.assertIn("Avg charge rate", output)
        self.assertIn("2 readings", output)

    def test_topup_complete_prints_implied_rate(self):
        from growatt_guard.modes import command_topup_complete_check
        from growatt_guard import state as state_mod

        state = self._make_topup_state(started_minutes_ago=100, start_soc=48.0, load_w=1000.0, minutes=100)
        end_status = self._make_status(soc=74.0)

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = make_config(
                battery_capacity_wh=30000.0,
                battery_charge_rate_w=3000.0,
                dry_run=True,
            )
            buf = StringIO()
            with (
                patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup_active.json"),
                patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
                patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", tmp / "cr.json"),
                patch("growatt_guard.audit.MODE_AUDIT_FILE", tmp / "mode_decisions.csv"),
                patch("growatt_guard.modes.read_topup_state", return_value=state),
                patch("growatt_guard.modes.read_utility_hold_state", return_value=None),
                patch("growatt_guard.modes.topup_is_active", return_value=False),
                patch("growatt_guard.modes.load_context", return_value=(None, None, end_status)),
                patch("growatt_guard.modes.command_resume", return_value=0),
                patch("growatt_guard.modes.clear_topup_state"),
                patch("growatt_guard.modes.command_return_sbu", return_value=0),
                redirect_stdout(buf),
            ):
                command_topup_complete_check(cfg)

        output = buf.getvalue()
        self.assertIn("48%", output)
        self.assertIn("74%", output)
        self.assertIn("Implied charge rate", output)

    def test_topup_complete_prints_fallback_when_no_soc_data(self):
        from growatt_guard.modes import command_topup_complete_check
        from growatt_guard import state as state_mod

        state = self._make_topup_state(started_minutes_ago=100, start_soc=48.0, load_w=1000.0, minutes=100)
        state.pop("start_soc")

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = make_config(
                battery_capacity_wh=30000.0,
                battery_charge_rate_w=3000.0,
                dry_run=True,
            )
            buf = StringIO()
            with (
                patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup_active.json"),
                patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
                patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", tmp / "cr.json"),
                patch("growatt_guard.audit.MODE_AUDIT_FILE", tmp / "mode_decisions.csv"),
                patch("growatt_guard.modes.read_topup_state", return_value=state),
                patch("growatt_guard.modes.read_utility_hold_state", return_value=None),
                patch("growatt_guard.modes.topup_is_active", return_value=False),
                patch("growatt_guard.modes.load_context", return_value=(None, None, self._make_status(74.0))),
                patch("growatt_guard.modes.command_resume", return_value=0),
                patch("growatt_guard.modes.clear_topup_state"),
                patch("growatt_guard.modes.command_return_sbu", return_value=0),
                redirect_stdout(buf),
            ):
                command_topup_complete_check(cfg)

        output = buf.getvalue()
        self.assertIn("Topup complete", output)
        self.assertNotIn("Implied charge rate", output)

    def test_topup_complete_skips_when_still_active(self):
        from growatt_guard.modes import command_topup_complete_check

        import datetime as dt
        paused_until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat()
        state = {
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "minutes": 60,
            "paused_until": paused_until,
            "reason": "test",
        }

        buf = StringIO()
        with (
            patch("growatt_guard.modes.read_topup_state", return_value=state),
            patch("growatt_guard.modes.topup_is_active", return_value=True),
            redirect_stdout(buf),
        ):
            rc = command_topup_complete_check(make_config())

        self.assertEqual(rc, 0)
        self.assertIn("remaining", buf.getvalue())

    def test_topup_complete_repairs_overdue_unclosed_topup_when_state_is_missing(self):
        from growatt_guard.modes import command_topup_complete_check

        import datetime as dt
        started = (dt.datetime.now() - dt.timedelta(minutes=60)).isoformat(timespec="seconds")
        rows = [
            {
                "timestamp": started,
                "command": "auto-topup-check",
                "soc": "66",
                "previous_mode": "SBU priority [0]",
                "action": "auto-topup-started",
                "result": "ok",
                "note": "20min, 8.2h to sunrise",
            }
        ]

        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            buf = StringIO()
            with (
                patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path),
                patch("growatt_guard.modes.read_topup_state", return_value=None),
                patch("growatt_guard.modes.read_utility_hold_state", return_value=None),
                patch("growatt_guard.modes.command_return_sbu", return_value=0) as return_mock,
                redirect_stdout(buf),
            ):
                rc = command_topup_complete_check(make_config())

        self.assertEqual(rc, 0)
        return_mock.assert_called_once()
        self.assertIn("overdue auto-topup", buf.getvalue())

    def test_topup_complete_does_not_repair_when_a_later_sbu_return_exists(self):
        from growatt_guard.modes import command_topup_complete_check

        import datetime as dt
        started = (dt.datetime.now() - dt.timedelta(minutes=60)).isoformat(timespec="seconds")
        returned = (dt.datetime.now() - dt.timedelta(minutes=30)).isoformat(timespec="seconds")
        rows = [
            {
                "timestamp": started,
                "command": "auto-topup-check",
                "action": "auto-topup-started",
                "note": "20min, 8.2h to sunrise",
            },
            {
                "timestamp": returned,
                "command": "return-sbu",
                "action": "switch-to-sbu",
            },
        ]

        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            buf = StringIO()
            with (
                patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path),
                patch("growatt_guard.modes.read_topup_state", return_value=None),
                patch("growatt_guard.modes.read_utility_hold_state", return_value=None),
                patch("growatt_guard.modes.command_return_sbu", return_value=0) as return_mock,
                redirect_stdout(buf),
            ):
                rc = command_topup_complete_check(make_config())

        self.assertEqual(rc, 0)
        return_mock.assert_not_called()
        self.assertIn("No active topup", buf.getvalue())


class SolarSkipTopupTests(unittest.TestCase):
    """Tests for AUTO_TOPUP_SOLAR_SKIP_KWH_M2 solar-forecast skip in command_auto_topup_check."""

    def _make_status(self, soc: float = 35.0, discharge_w: float = 1800.0) -> dict:
        return {
            "datalogSn": "SN1",
            "deviceSn": "SN1",
            "data": {"soc": soc, "pDischarge": discharge_w, "outputConfig": "0"},
        }

    def _base_cfg(self, **overrides) -> "Config":
        return make_config(
            auto_topup_enabled=True,
            auto_topup_min_hours_to_sunrise=4.0,
            battery_capacity_wh=30000.0,
            battery_charge_rate_w=3000.0,
            battery_bms_cutoff_soc=25.0,
            dry_run=True,
            **overrides,
        )

    def _run(self, cfg, status, tomorrow_kwh=None):
        from growatt_guard.modes import command_auto_topup_check
        from growatt_guard import state as state_mod
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory

        written = {}

        def fake_write_topup_state(minutes, reason, paused_until, start_soc=None, start_load_w=None):
            written["minutes"] = minutes

        buf = StringIO()
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            patches = [
                patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup_active.json"),
                patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
                patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr.json"),
                patch("growatt_guard.modes.read_pause_state", return_value=None),
                patch("growatt_guard.modes.topup_is_active", return_value=False),
                patch("growatt_guard.modes.hours_until_next_sunrise", return_value=5.0),
                patch("growatt_guard.modes.load_context", return_value=(None, None, status)),
                patch("growatt_guard.modes.set_mode", return_value="ok"),
                patch("growatt_guard.modes.command_pause", return_value=0),
                patch("growatt_guard.modes.write_topup_state", side_effect=fake_write_topup_state),
                patch("growatt_guard.modes.write_utility_hold_state"),
                patch("growatt_guard.modes.append_mode_audit"),
                patch("growatt_guard.weather.get_tomorrow_solar_kwh_m2", return_value=tomorrow_kwh),
            ]
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                 patches[5], patches[6], patches[7], patches[8], patches[9], \
                 patches[10], patches[11], patches[12], redirect_stdout(buf):
                rc = command_auto_topup_check(cfg)

        return rc, buf.getvalue(), written

    def test_topup_skipped_when_solar_forecast_above_threshold(self):
        cfg = self._base_cfg(auto_topup_solar_skip_kwh_m2=4.0, auto_topup_target_soc=50.0)
        rc, out, written = self._run(cfg, self._make_status(soc=55.0, discharge_w=1000.0), tomorrow_kwh=5.2)
        self.assertEqual(rc, 0)
        self.assertIn("skipping", out)
        self.assertIn("5.2 kWh/m²", out)
        self.assertNotIn("minutes", written)

    def test_topup_proceeds_when_solar_forecast_below_threshold(self):
        cfg = self._base_cfg(auto_topup_solar_skip_kwh_m2=4.0)
        rc, out, written = self._run(cfg, self._make_status(), tomorrow_kwh=2.8)
        self.assertIn("minutes", written)

    def test_topup_proceeds_when_sunny_but_survival_margin_needs_topup(self):
        cfg = self._base_cfg(auto_topup_solar_skip_kwh_m2=4.0)
        rc, out, written = self._run(cfg, self._make_status(soc=35.0, discharge_w=1800.0), tomorrow_kwh=5.2)
        self.assertIn("minutes", written)
        self.assertIn("Auto-topup started", out)

    def test_topup_proceeds_when_feature_disabled(self):
        cfg = self._base_cfg(auto_topup_solar_skip_kwh_m2=0.0)
        rc, out, written = self._run(cfg, self._make_status(), tomorrow_kwh=9.9)
        self.assertIn("minutes", written)

    def test_topup_proceeds_when_solar_forecast_unavailable(self):
        cfg = self._base_cfg(auto_topup_solar_skip_kwh_m2=4.0)
        rc, out, written = self._run(cfg, self._make_status(), tomorrow_kwh=None)
        self.assertIn("minutes", written)

    def test_topup_skipped_exactly_at_threshold(self):
        cfg = self._base_cfg(auto_topup_solar_skip_kwh_m2=4.0, auto_topup_target_soc=50.0)
        rc, out, written = self._run(cfg, self._make_status(soc=55.0, discharge_w=1000.0), tomorrow_kwh=4.0)
        self.assertNotIn("minutes", written)
        self.assertIn("skipping", out)


class TopupStatsWeeklySummaryTests(unittest.TestCase):
    """Tests for auto-topup stats section in build_weekly_summary."""

    def _write_audit(self, path: Path, rows: list[dict]) -> None:
        import csv as csv_mod
        from growatt_guard.audit import MODE_AUDIT_FIELDS
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=MODE_AUDIT_FIELDS)
            writer.writeheader()
            for row in rows:
                full = {k: "" for k in MODE_AUDIT_FIELDS}
                full.update(row)
                writer.writerow(full)

    def test_topup_count_and_minutes_in_summary(self):
        import datetime as dt
        from growatt_guard.audit import build_weekly_summary, MODE_AUDIT_FILE
        now = dt.datetime(2026, 6, 21, 21, 0)
        rows = [
            {"timestamp": "2026-06-19T01:00:00", "action": "auto-topup-started", "note": "45min, 5.0h to sunrise"},
            {"timestamp": "2026-06-20T02:00:00", "action": "auto-topup-started", "note": "30min, 4.5h to sunrise"},
        ]
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                result = build_weekly_summary(now=now)
        self.assertIn("Auto-topups: 2", result)
        self.assertIn("75 min", result)

    def test_kwh_estimate_shown_when_charge_rate_set(self):
        import datetime as dt
        from growatt_guard.audit import build_weekly_summary
        now = dt.datetime(2026, 6, 21, 21, 0)
        rows = [
            {"timestamp": "2026-06-19T01:00:00", "action": "auto-topup-started", "note": "60min, 5.0h to sunrise"},
        ]
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                # 60 min * 3000 W / 60 / 1000 = 3.0 kWh
                result = build_weekly_summary(now=now, charge_rate_w=3000.0)
        self.assertIn("3.0 kWh", result)

    def test_no_topups_shows_zero(self):
        import datetime as dt
        from growatt_guard.audit import build_weekly_summary
        now = dt.datetime(2026, 6, 21, 21, 0)
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, [])
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                result = build_weekly_summary(now=now)
        self.assertIn("Auto-topups: 0", result)

    def test_threshold_tuning_warns_when_soc_near_cutoff(self):
        import datetime as dt
        from growatt_guard.audit import build_weekly_summary
        now = dt.datetime(2026, 6, 21, 21, 0)
        rows = [
            {
                "timestamp": "2026-06-20T01:00:00",
                "command": "auto-topup-check",
                "soc": "29",
                "action": "auto-topup-started",
                "note": "20min, 5.0h to sunrise",
            },
            {
                "timestamp": "2026-06-20T06:30:00",
                "command": "preserve-battery",
                "soc": "35",
                "threshold": "50",
                "action": "switch-to-utility",
            },
        ]
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                result = build_weekly_summary(
                    now=now,
                    low_battery_soc=50.0,
                    battery_bms_cutoff_soc=25.0,
                )

        self.assertIn("Threshold tuning:", result)
        self.assertIn("Near-cutoff readings (<= 30%): 1", result)
        self.assertIn("Do not lower yet", result)

    def test_threshold_tuning_allows_small_lower_trial_when_week_is_comfortable(self):
        import datetime as dt
        from growatt_guard.audit import build_weekly_summary
        now = dt.datetime(2026, 6, 21, 21, 0)
        rows = [
            {
                "timestamp": f"2026-06-{15 + (i // 2):02d}T{6 + (i % 2):02d}:30:00",
                "command": "preserve-battery",
                "soc": "56",
                "threshold": "50",
                "action": "no-change",
            }
            for i in range(12)
        ]
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                result = build_weekly_summary(
                    now=now,
                    low_battery_soc=50.0,
                    battery_bms_cutoff_soc=25.0,
                )

        self.assertIn("Observed SOC range: 56% to 56%", result)
        self.assertIn("Could trial lowering LOW_BATTERY_SOC by 2-3%", result)


class DailyTomorrowForecastTests(unittest.TestCase):
    """Tests for tomorrow's solar forecast line in build_daily_summary."""

    def test_forecast_line_present_when_provided(self):
        from growatt_guard.audit import build_daily_summary
        with patch("growatt_guard.audit.summarize_today_log_counts", return_value={
            "success": 0, "failure": 0, "watchdog_repairs": 0,
            "preserve_actions": 0, "return_sbu_actions": 0,
        }):
            result = build_daily_summary({}, tomorrow_kwh_m2=5.3)
        self.assertIn("Tomorrow's solar forecast: 5.3 kWh/m²", result)

    def test_forecast_line_absent_when_none(self):
        from growatt_guard.audit import build_daily_summary
        with patch("growatt_guard.audit.summarize_today_log_counts", return_value={
            "success": 0, "failure": 0, "watchdog_repairs": 0,
            "preserve_actions": 0, "return_sbu_actions": 0,
        }):
            result = build_daily_summary({}, tomorrow_kwh_m2=None)
        self.assertNotIn("Tomorrow's solar forecast", result)


class PruneAuditTests(unittest.TestCase):
    """Tests for prune_audit_rows."""

    def _write_audit(self, path: Path, rows: list[dict]) -> None:
        import csv as csv_mod
        from growatt_guard.audit import MODE_AUDIT_FIELDS
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=MODE_AUDIT_FIELDS)
            writer.writeheader()
            for row in rows:
                full = {k: "" for k in MODE_AUDIT_FIELDS}
                full.update(row)
                writer.writerow(full)

    def test_removes_old_rows_keeps_recent(self):
        import datetime as dt
        from growatt_guard.audit import prune_audit_rows, MODE_AUDIT_FILE
        cutoff = dt.datetime(2026, 6, 1)
        rows = [
            {"timestamp": "2026-05-01T10:00:00", "action": "switch-to-utility"},  # old
            {"timestamp": "2026-06-15T10:00:00", "action": "switch-to-sbu"},       # recent
        ]
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                removed, kept = prune_audit_rows(cutoff)
        self.assertEqual(removed, 1)
        self.assertEqual(kept, 1)

    def test_returns_zero_when_nothing_to_prune(self):
        import datetime as dt
        from growatt_guard.audit import prune_audit_rows, MODE_AUDIT_FILE
        cutoff = dt.datetime(2026, 1, 1)
        rows = [
            {"timestamp": "2026-06-15T10:00:00", "action": "switch-to-sbu"},
        ]
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                removed, kept = prune_audit_rows(cutoff)
        self.assertEqual(removed, 0)
        self.assertEqual(kept, 1)

    def test_returns_zero_zero_when_file_missing(self):
        import datetime as dt
        from growatt_guard.audit import prune_audit_rows, MODE_AUDIT_FILE
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "missing.csv"
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                removed, kept = prune_audit_rows(dt.datetime(2026, 6, 1))
        self.assertEqual((removed, kept), (0, 0))

    def test_command_prune_audit_prints_result(self):
        import datetime as dt
        from growatt_guard.modes import command_prune_audit
        from growatt_guard.audit import MODE_AUDIT_FILE
        rows = [
            {"timestamp": "2026-03-01T10:00:00", "action": "switch-to-utility"},
            {"timestamp": "2026-06-15T10:00:00", "action": "switch-to-sbu"},
        ]
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            cfg = make_config(audit_retention_days=90)
            buf = StringIO()
            with (patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path),
                  redirect_stdout(buf)):
                command_prune_audit(cfg)
        self.assertIn("pruned", buf.getvalue())


class DischargeRateHistoryTests(unittest.TestCase):
    """Tests for discharge rate rolling average in state.py."""

    def test_appends_reading_and_returns_list(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "dr.json"):
                readings = state_mod.append_discharge_rate_reading(1500.0)
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0]["rate_w"], 1500)

    def test_trims_to_max_readings(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "dr.json"):
                for i in range(12):
                    readings = state_mod.append_discharge_rate_reading(float(1000 + i * 100))
        self.assertEqual(len(readings), state_mod._DISCHARGE_RATE_MAX_READINGS)
        self.assertEqual(readings[-1]["rate_w"], 2100)

    def test_read_returns_empty_list_when_missing(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "dr.json"):
                result = state_mod.read_discharge_rate_history()
        self.assertEqual(result, [])

    def test_accumulates_across_calls(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "dr.json"):
                state_mod.append_discharge_rate_reading(1200.0)
                state_mod.append_discharge_rate_reading(1800.0)
                readings = state_mod.read_discharge_rate_history()
        self.assertEqual(len(readings), 2)
        self.assertEqual(readings[0]["rate_w"], 1200)
        self.assertEqual(readings[1]["rate_w"], 1800)


class DischargeRateAverageTests(unittest.TestCase):
    """Tests that command_auto_topup_check uses the rolling discharge average."""

    def _run_check(self, cfg, tmpdir, discharge_w: float, prior_readings: list[float]) -> dict:
        from growatt_guard.modes import command_auto_topup_check
        from growatt_guard import state as state_mod

        status = {
            "datalogSn": "SN1", "deviceSn": "SN1",
            "data": {"soc": 40.0, "pDischarge": discharge_w, "outputConfig": "0"},
        }
        captured: dict = {}

        def fake_write(minutes, reason, paused_until, start_soc=None, start_load_w=None):
            captured["minutes"] = minutes
            captured["start_load_w"] = start_load_w

        tmp = Path(tmpdir)
        dr_file = tmp / "dr.json"
        with patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", dr_file):
            for r in prior_readings:
                state_mod.append_discharge_rate_reading(r)

        with (patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", dr_file),
              patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup.json"),
              patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
              patch("growatt_guard.modes.read_pause_state", return_value=None),
              patch("growatt_guard.modes.topup_is_active", return_value=False),
              patch("growatt_guard.modes.hours_until_next_sunrise", return_value=5.0),
              patch("growatt_guard.modes.load_context", return_value=(None, None, status)),
              patch("growatt_guard.modes.set_mode", return_value="ok"),
              patch("growatt_guard.modes.command_pause", return_value=0),
              patch("growatt_guard.modes.write_topup_state", side_effect=fake_write),
              patch("growatt_guard.modes.write_utility_hold_state"),
              patch("growatt_guard.modes.append_mode_audit"),
              redirect_stdout(StringIO())):
            command_auto_topup_check(cfg)

        return captured

    def _make_cfg(self):
        return make_config(
            auto_topup_enabled=True,
            battery_capacity_wh=30000.0,
            battery_charge_rate_w=3000.0,
            battery_bms_cutoff_soc=25.0,
            dry_run=True,
        )

    def test_uses_live_reading_when_history_has_only_one_entry(self):
        cfg = self._make_cfg()
        with TemporaryDirectory() as tmpdir:
            result = self._run_check(cfg, tmpdir, discharge_w=1500.0, prior_readings=[])
        # After appending live (1500), history has 1 entry → uses live
        self.assertAlmostEqual(result["start_load_w"], 1500.0, delta=1.0)

    def test_uses_average_when_history_has_prior_readings(self):
        cfg = self._make_cfg()
        with TemporaryDirectory() as tmpdir:
            # Seed one prior reading at 600 W; live spike = 3000 W
            # After appending live: history = [600, 3000] → avg = 1800 W
            result = self._run_check(cfg, tmpdir, discharge_w=3000.0, prior_readings=[600.0])
        self.assertAlmostEqual(result["start_load_w"], 1800.0, delta=1.0)

    def test_average_produces_different_topup_than_spike(self):
        cfg = self._make_cfg()
        with TemporaryDirectory() as tmpdir:
            spike_result = self._run_check(cfg, tmpdir, discharge_w=3000.0, prior_readings=[])
        with TemporaryDirectory() as tmpdir:
            avg_result = self._run_check(cfg, tmpdir, discharge_w=3000.0, prior_readings=[600.0])
        # Spike (3000 W) should demand more topup minutes than the smoothed avg (1800 W)
        self.assertGreater(spike_result.get("minutes", 0), avg_result.get("minutes", 0))


if __name__ == "__main__":
    unittest.main()
