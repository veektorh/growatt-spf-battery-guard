import datetime as dt
import json
import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from contextlib import redirect_stdout

from helpers import make_config
from growatt_guard.ops_review import build_ops_review, build_ops_review_embed, command_ops_review


class OpsReviewTests(unittest.TestCase):
    def dashboard_payload(self, generated_at: str) -> dict:
        return {
            "generated_at": generated_at,
            "live": {
                "soc": 63,
                "mode": "SBU priority",
                "battery_status": "Discharging",
                "bypass_detected": False,
                "pv_w": 1200,
                "load_w": 900,
                "grid_w": 0,
                "battery_net_w": 300,
                "pv_today_kwh": 12.4,
                "load_today_kwh": 9.1,
                "grid_today_kwh": 0.2,
                "charge_today_kwh": 6.5,
                "discharge_today_kwh": 4.4,
            },
            "quality": {"data": {"level": "good", "title": "Good"}},
            "planner": {
                "outlook": {
                    "projected_sunset_soc": 93,
                    "projected_sunrise_soc": 44,
                    "reserve_target_soc": 35,
                    "topup_minutes": 0,
                    "expected_grid_kwh": 0,
                    "weather": "sunny",
                }
            },
            "automation": {"pause": "active", "emergency_alert": "clear"},
        }

    def test_build_ops_review_uses_local_dashboard_and_real_audit_rows(self):
        now = dt.datetime(2026, 7, 4, 12, 0, 0)
        rows = [
            {
                "timestamp": "2026-07-04T07:55:00",
                "command": "preserve-battery",
                "soc": "64",
                "action": "no-change",
                "dry_run": "false",
                "result": "ok",
                "note": "",
            },
            {
                "timestamp": "2026-07-04T08:00:00",
                "command": "auto-topup-check",
                "soc": "40",
                "action": "auto-topup-started",
                "dry_run": "true",
                "result": "ok",
                "note": "60 min",
            },
        ]
        with TemporaryDirectory() as tmpdir:
            dashboard_path = Path(tmpdir) / "dashboard.json"
            dashboard_path.write_text(json.dumps(self.dashboard_payload("2026-07-04T11:50:00")), encoding="utf-8")
            with patch("growatt_guard.ops_review.read_mode_audit_rows", return_value=rows), \
                patch("growatt_guard.ops_review.read_pause_state", return_value=None), \
                patch("growatt_guard.ops_review.read_topup_state", return_value=None), \
                patch("growatt_guard.ops_review.read_bypass_alert_state", return_value=None), \
                patch("growatt_guard.ops_review.read_battery_alert_state", return_value=None), \
                patch("growatt_guard.ops_review.read_growatt_cloud_failure_state", return_value=None), \
                patch("growatt_guard.ops_review.read_command_lock_state", return_value=None), \
                patch("growatt_guard.ops_review.topup_is_active", return_value=False):
                review = build_ops_review(make_config(), days=7, now=now, dashboard_path=dashboard_path)

        self.assertIn("Growatt ops review - last 7 days", review.text)
        self.assertIn("Rows: 1 real rows", review.text)
        self.assertIn("Auto-topups: 0", review.text)
        self.assertIn("Tonight projection says no top-up is needed", review.text)

    def test_build_ops_review_flags_current_bypass(self):
        now = dt.datetime(2026, 7, 4, 12, 0, 0)
        payload = self.dashboard_payload("2026-07-04T11:55:00")
        payload["live"]["bypass_detected"] = True
        with TemporaryDirectory() as tmpdir:
            dashboard_path = Path(tmpdir) / "dashboard.json"
            dashboard_path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("growatt_guard.ops_review.read_mode_audit_rows", return_value=[]), \
                patch("growatt_guard.ops_review.read_pause_state", return_value=None), \
                patch("growatt_guard.ops_review.read_topup_state", return_value=None), \
                patch("growatt_guard.ops_review.read_bypass_alert_state", return_value=None), \
                patch("growatt_guard.ops_review.read_battery_alert_state", return_value=None), \
                patch("growatt_guard.ops_review.read_growatt_cloud_failure_state", return_value=None), \
                patch("growatt_guard.ops_review.read_command_lock_state", return_value=None), \
                patch("growatt_guard.ops_review.topup_is_active", return_value=False):
                review = build_ops_review(make_config(), days=3, now=now, dashboard_path=dashboard_path)

        self.assertEqual(review.severity, "fail")
        self.assertIn("Unexpected grid bypass is currently detected", review.text)
        embed = build_ops_review_embed(review)
        self.assertEqual(embed["color"], 0xED4245)

    def test_command_ops_review_prints_and_can_notify(self):
        review = type("Review", (), {
            "text": "ops text",
            "recommendations": ["check"],
            "severity": "ok",
            "metrics": {"days": 7},
        })()
        output = StringIO()
        with patch("growatt_guard.ops_review.build_ops_review", return_value=review), \
            patch("growatt_guard.ops_review.send_discord_embed", return_value=True) as send, \
            redirect_stdout(output):
            result = command_ops_review(make_config(discord_webhook_url="https://example.invalid/hook"), days=7, notify=True)

        self.assertEqual(result, 0)
        self.assertIn("ops text", output.getvalue())
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
