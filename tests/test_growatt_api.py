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
    verify_mode_switch,
    command_watchdog_sbu,
)


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
        ), patch(
            "growatt_guard.modes.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), {"storage_params": {"outputConfig": "0"}}),
        ), patch("growatt_guard.modes.set_mode") as set_mode_mock, redirect_stdout(StringIO()):
            self.assertEqual(command_watchdog_sbu(config), 0)

        set_mode_mock.assert_not_called()

    def test_watchdog_sbu_retries_when_not_sbu(self):
        config = make_config()

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.audit.LOG_DIR", Path(tmpdir)), patch(
            "growatt_guard.audit.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"
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


if __name__ == "__main__":
    unittest.main()
