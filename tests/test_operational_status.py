import unittest
from unittest.mock import patch

from helpers import make_config
from growatt_guard.operational_status import build_forecast_calibration_status, build_sbu_guard_status


class OperationalStatusTests(unittest.TestCase):
    def test_guard_reports_active_hold_after_block(self):
        rows = [{
            "timestamp": "2026-07-12T12:05:00+00:00",
            "command": "return-sbu",
            "action": "low-soc-guard-blocked",
            "soc": "24",
        }]
        hold = {"started_at": "2026-07-12T12:00:00+00:00"}

        result = build_sbu_guard_status(30, audit_rows=rows, utility_hold=hold)

        self.assertEqual(result["state"], "blocked_hold")
        self.assertTrue(result["hold_blocked"])
        self.assertEqual(result["last_event"]["soc"], "24")

    def test_guard_reports_disabled_and_misconfigured(self):
        self.assertEqual(build_sbu_guard_status(0, audit_rows=[], utility_hold={})["state"], "disabled")
        self.assertEqual(build_sbu_guard_status(101, audit_rows=[], utility_hold={})["state"], "misconfigured")

    def test_forecast_status_reports_readiness(self):
        summary = {"sample_count": 5, "recommendation": "keep current setting"}
        with patch("growatt_guard.operational_status.summarize_forecast_calibration", return_value=summary):
            result = build_forecast_calibration_status(
                make_config(panel_kwp=6.0, weather_lat=1.0, weather_lon=2.0)
            )

        self.assertTrue(result["configured"])
        self.assertTrue(result["ready"])
        self.assertIn("5 completed", result["detail"])


if __name__ == "__main__":
    unittest.main()
