import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from helpers import make_config
from growatt_power_guard import (
    PVOUTPUT_STATE_FILE,
    PVOUTPUT_URL,
    extract_pvoutput_fields,
    upload_pvoutput_status,
    write_pvoutput_state,
    read_pvoutput_state,
)


# Minimal status dict matching the Growatt storage_params structure
def _make_status(**overrides):
    bean = {
        "ppv": 1200.0,
        "vGrid": 231.4,
        "outPutPower": 800.0,
        "pCharge": 400.0,
        "pDischarge": 0.0,
        "epvToday": 3.5,
    }
    bean.update(overrides.pop("bean", {}))
    status = {
        "plant_id": "plant1",
        "device_sn": "SN1",
        "device_type": "storage",
        "device": {"capacity": "65 %"},
        "storage_params": {"storageBean": bean},
    }
    status.update(overrides)
    return status


FIXED_NOW = dt.datetime(2026, 6, 20, 14, 30, 0)


class ExtractPvoutputFieldsTests(unittest.TestCase):
    def test_extracts_standard_fields(self):
        fields = extract_pvoutput_fields(_make_status(), now=FIXED_NOW)

        self.assertEqual(fields["d"], "20260620")
        self.assertEqual(fields["t"], "14:30")
        self.assertEqual(fields["v2"], 1200)  # ppv (W)
        self.assertEqual(fields["v1"], 3500)  # epvToday 3.5 kWh → 3500 Wh
        self.assertEqual(fields["v4"], 800)   # outPutPower (W)
        self.assertEqual(fields["v6"], 231.4) # vGrid (V)

    def test_extracts_extended_fields(self):
        fields = extract_pvoutput_fields(_make_status(), now=FIXED_NOW)

        self.assertEqual(fields["v7"], 65.0)  # battery SOC
        self.assertEqual(fields["v8"], 400)   # pCharge (W)
        self.assertEqual(fields["v9"], 0)     # pDischarge (W)

    def test_prefers_epv_today_over_epv_today_total(self):
        status = _make_status(bean={"ppv": 500.0, "epvToday": 2.1, "epvTodayTotal": 5.0})
        fields = extract_pvoutput_fields(status, now=FIXED_NOW)
        self.assertEqual(fields["v1"], 2100)  # epvToday wins over epvTodayTotal

    def test_missing_pv_power_omits_v2(self):
        status = {
            "device": {"capacity": "65 %"},
            "storage_params": {"storageBean": {"epvToday": 1.0}},
        }
        fields = extract_pvoutput_fields(status, now=FIXED_NOW)
        self.assertNotIn("v2", fields)

    def test_missing_energy_today_omits_v1(self):
        status = {
            "device": {"capacity": "65 %"},
            "storage_params": {"storageBean": {"ppv": 100.0}},
        }
        fields = extract_pvoutput_fields(status, now=FIXED_NOW)
        self.assertNotIn("v1", fields)

    def test_no_soc_omits_v7(self):
        status = _make_status()
        status["device"] = {}
        # Also clear SOC-like keys from bean
        status["storage_params"]["storageBean"].pop("pCharge", None)
        fields = extract_pvoutput_fields(status, now=FIXED_NOW)
        self.assertNotIn("v7", fields)

    def test_zero_pv_power_included(self):
        status = _make_status(bean={"ppv": 0.0, "epvToday": 0.0})
        fields = extract_pvoutput_fields(status, now=FIXED_NOW)
        self.assertEqual(fields["v2"], 0)
        self.assertEqual(fields["v1"], 0)


class UploadPvoutputStatusTests(unittest.TestCase):
    def _config(self, **kw):
        return make_config(
            dry_run=False,
            pvoutput_enabled=True,
            pvoutput_api_key="TESTKEY",
            pvoutput_system_id="12345",
            **kw,
        )

    def test_success_returns_true(self):
        fields = {"d": "20260620", "t": "14:30", "v2": "1200"}
        mock_response = MagicMock(status_code=200)
        with patch("growatt_guard.pvoutput.requests.post", return_value=mock_response) as mock_post:
            result = upload_pvoutput_status(self._config(), fields)

        self.assertTrue(result)
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        self.assertEqual(call_kwargs[0][0], PVOUTPUT_URL)
        self.assertEqual(call_kwargs[1]["headers"]["X-Pvoutput-Apikey"], "TESTKEY")
        self.assertEqual(call_kwargs[1]["headers"]["X-Pvoutput-SystemId"], "12345")

    def test_extended_data_rejected_retries_without_v7_v9(self):
        fields = {"d": "20260620", "t": "14:30", "v2": "1200", "v7": "65", "v8": "400", "v9": "0"}
        first_response = MagicMock(status_code=400, text="No extended data features enabled")
        retry_response = MagicMock(status_code=200, text="OK")
        with patch(
            "growatt_guard.pvoutput.requests.post", side_effect=[first_response, retry_response]
        ) as mock_post:
            result = upload_pvoutput_status(self._config(), fields)

        self.assertTrue(result)
        self.assertEqual(mock_post.call_count, 2)
        # Second call should not include v7-v9
        retry_data = mock_post.call_args_list[1][1]["data"]
        self.assertNotIn("v7", retry_data)
        self.assertNotIn("v8", retry_data)
        self.assertNotIn("v9", retry_data)
        self.assertIn("v2", retry_data)

    def test_non_extended_400_returns_false(self):
        fields = {"d": "20260620", "t": "14:30", "v2": "1200"}
        mock_response = MagicMock(status_code=400, text="Bad timestamp format")
        with patch("growatt_guard.pvoutput.requests.post", return_value=mock_response):
            result = upload_pvoutput_status(self._config(), fields)

        self.assertFalse(result)

    def test_network_error_returns_false(self):
        import requests as req_lib

        fields = {"d": "20260620", "t": "14:30", "v2": "1200"}
        with patch("growatt_guard.pvoutput.requests.post", side_effect=req_lib.ConnectionError("timeout")):
            result = upload_pvoutput_status(self._config(), fields)

        self.assertFalse(result)

    def test_missing_api_key_raises(self):
        from growatt_guard.exceptions import GrowattGuardError

        fields = {"d": "20260620", "t": "14:30", "v2": "1200"}
        config = make_config(pvoutput_enabled=True, pvoutput_api_key="", pvoutput_system_id="12345")
        with self.assertRaises(GrowattGuardError):
            upload_pvoutput_status(config, fields)

    def test_missing_system_id_raises(self):
        from growatt_guard.exceptions import GrowattGuardError

        fields = {"d": "20260620", "t": "14:30", "v2": "1200"}
        config = make_config(pvoutput_enabled=True, pvoutput_api_key="KEY", pvoutput_system_id="")
        with self.assertRaises(GrowattGuardError):
            upload_pvoutput_status(config, fields)


class CommandPvoutputUploadTests(unittest.TestCase):
    def test_skips_when_disabled(self):
        from growatt_guard.pvoutput import command_pvoutput_upload

        config = make_config(pvoutput_enabled=False)
        with patch("growatt_guard.pvoutput.load_context") as mock_ctx:
            result = command_pvoutput_upload(config)

        self.assertEqual(result, 0)
        mock_ctx.assert_not_called()

    def test_dry_run_prints_fields_without_posting(self):
        from growatt_guard.pvoutput import command_pvoutput_upload

        config = make_config(dry_run=True, pvoutput_enabled=True, pvoutput_api_key="K", pvoutput_system_id="1")
        status = _make_status()
        with patch("growatt_guard.pvoutput.load_context", return_value=(None, None, status)), \
             patch("growatt_guard.pvoutput.requests.post") as mock_post, \
             patch("builtins.print") as mock_print:
            result = command_pvoutput_upload(config)

        self.assertEqual(result, 0)
        mock_post.assert_not_called()
        printed = mock_print.call_args[0][0]
        self.assertIn("DRY_RUN", printed)
        self.assertIn("v2=", printed)

    def test_upload_success_prints_summary(self):
        from growatt_guard.pvoutput import command_pvoutput_upload

        config = make_config(dry_run=False, pvoutput_enabled=True, pvoutput_api_key="K", pvoutput_system_id="1")
        status = _make_status()
        mock_response = MagicMock(status_code=200)
        with patch("growatt_guard.pvoutput.load_context", return_value=(None, None, status)), \
             patch("growatt_guard.pvoutput.requests.post", return_value=mock_response), \
             patch("builtins.print") as mock_print:
            result = command_pvoutput_upload(config)

        self.assertEqual(result, 0)
        printed = mock_print.call_args[0][0]
        self.assertIn("PVOutput OK", printed)

    def test_upload_failure_raises(self):
        from growatt_guard.pvoutput import command_pvoutput_upload
        from growatt_guard.exceptions import GrowattGuardError

        config = make_config(dry_run=False, pvoutput_enabled=True, pvoutput_api_key="K", pvoutput_system_id="1")
        status = _make_status()
        mock_response = MagicMock(status_code=500, text="server error")
        with patch("growatt_guard.pvoutput.load_context", return_value=(None, None, status)), \
             patch("growatt_guard.pvoutput.requests.post", return_value=mock_response):
            with self.assertRaises(GrowattGuardError):
                command_pvoutput_upload(config)


class PvoutputStateTests(unittest.TestCase):
    def test_write_and_read_state(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        fields = {"v1": 17300, "v2": 694, "v4": 1017, "v6": 222.3, "v7": 68.0}
        now = dt.datetime(2026, 6, 20, 14, 30, 0)
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "pvoutput_last.json"
            with patch("growatt_guard.pvoutput.PVOUTPUT_STATE_FILE", state_path):
                write_pvoutput_state(fields, now=now)
                state = read_pvoutput_state()

        self.assertIsNotNone(state)
        self.assertEqual(state["uploaded_at"], "2026-06-20T14:30:00")
        self.assertEqual(state["fields"]["v1"], 17300)
        self.assertEqual(state["fields"]["v2"], 694)
        self.assertNotIn("d", state["fields"])
        self.assertNotIn("t", state["fields"])

    def test_read_state_returns_none_when_missing(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "pvoutput_last.json"
            with patch("growatt_guard.pvoutput.PVOUTPUT_STATE_FILE", state_path):
                result = read_pvoutput_state()
        self.assertIsNone(result)

    def test_upload_success_writes_state(self):
        from growatt_guard.pvoutput import command_pvoutput_upload
        from pathlib import Path
        from tempfile import TemporaryDirectory

        config = make_config(dry_run=False, pvoutput_enabled=True, pvoutput_api_key="K", pvoutput_system_id="1")
        status = _make_status()
        mock_response = MagicMock(status_code=200)
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "pvoutput_last.json"
            with patch("growatt_guard.pvoutput.load_context", return_value=(None, None, status)), \
                 patch("growatt_guard.pvoutput.requests.post", return_value=mock_response), \
                 patch("growatt_guard.pvoutput.PVOUTPUT_STATE_FILE", state_path), \
                 patch("builtins.print"):
                command_pvoutput_upload(config)
            state = read_pvoutput_state.__wrapped__(state_path) if hasattr(read_pvoutput_state, "__wrapped__") else None
            import json
            raw = json.loads(state_path.read_text()) if state_path.exists() else None
        self.assertIsNotNone(raw)
        self.assertIn("uploaded_at", raw)
        self.assertIn("v2", raw["fields"])


class PvoutputParserTests(unittest.TestCase):
    def test_pvoutput_upload_command_is_registered(self):
        from growatt_guard.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["pvoutput-upload"])
        self.assertEqual(args.command, "pvoutput-upload")


if __name__ == "__main__":
    unittest.main()
