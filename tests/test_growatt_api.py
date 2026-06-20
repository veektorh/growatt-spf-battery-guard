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
from growatt_guard.growatt_api import extract_battery_status, estimate_runtime, estimate_charge_time, estimate_topup_for_sunrise, format_duration_minutes


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
        config = make_config()

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.audit.LOG_DIR", Path(tmpdir)), patch(
            "growatt_guard.audit.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"
        ), patch("growatt_guard.state.PAUSE_FILE", Path(tmpdir) / "pause.json"), patch(
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
                patch("growatt_guard.modes.read_topup_state", return_value=state),
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
                patch("growatt_guard.modes.read_topup_state", return_value=state),
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
                patch("growatt_guard.modes.read_topup_state", return_value=state),
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


if __name__ == "__main__":
    unittest.main()
