import datetime as dt
import json
import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from contextlib import redirect_stdout

from helpers import make_config
from growatt_guard.exceptions import GrowattGuardError
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
            dashboard_path.write_text(
                json.dumps(self.dashboard_payload("2026-07-04T11:50:00")),
                encoding="utf-8",
            )
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
        self.assertIn("Scheduled automation: active; active top-up/hold: no", review.text)
        self.assertIn("Dashboard scheduled automation: active; emergency alert clear", review.text)
        self.assertIn("Tonight projection says no top-up is needed", review.text)

    def test_build_ops_review_summarizes_completed_topup_efficiency(self):
        now = dt.datetime(2026, 7, 4, 12, 0, 0)
        rows = [
            {
                "timestamp": "2026-07-04T01:00:00",
                "command": "auto-topup-check",
                "soc": "40",
                "action": "auto-topup-started",
                "dry_run": "false",
                "result": "ok",
                "note": "60min, 5.0h to sunrise",
            },
            {
                "timestamp": "2026-07-04T02:00:00",
                "command": "topup-complete-check",
                "soc": "48",
                "action": "topup-target-reached",
                "dry_run": "false",
                "result": "ok",
                "note": (
                    "actual_min=60, ownership=owned, start_soc=40, "
                    "end_soc=48, target_soc=48, implied_rate_w=2400"
                ),
            },
            {
                "timestamp": "2026-07-04T02:01:00",
                "command": "return-sbu",
                "soc": "48",
                "action": "switch-to-sbu",
                "dry_run": "false",
                "result": "ok",
                "note": "",
            },
        ]
        with TemporaryDirectory() as tmpdir:
            dashboard_path = Path(tmpdir) / "dashboard.json"
            dashboard_path.write_text(
                json.dumps(self.dashboard_payload("2026-07-04T11:50:00")),
                encoding="utf-8",
            )
            with patch("growatt_guard.ops_review.read_mode_audit_rows", return_value=rows), \
                patch("growatt_guard.ops_review.read_pause_state", return_value=None), \
                patch("growatt_guard.ops_review.read_topup_state", return_value=None), \
                patch("growatt_guard.ops_review.read_bypass_alert_state", return_value=None), \
                patch("growatt_guard.ops_review.read_battery_alert_state", return_value=None), \
                patch("growatt_guard.ops_review.read_growatt_cloud_failure_state", return_value=None), \
                patch("growatt_guard.ops_review.read_command_lock_state", return_value=None), \
                patch("growatt_guard.ops_review.topup_is_active", return_value=False):
                review = build_ops_review(
                    make_config(battery_charge_rate_w=3000.0),
                    days=7,
                    now=now,
                    dashboard_path=dashboard_path,
                )

        self.assertIn("Auto-topups: 1 (60 min total, 3.0 kWh est. grid)", review.text)
        self.assertIn(
            "Topup closures: 1 target reached, 0 expired, 0 legacy, 0 unclosed; avg SOC gain 8%; avg implied charge 2.4 kW",
            review.text,
        )
        self.assertIn("Last mode change:", review.text)
        self.assertIn("return-sbu switch-to-sbu SOC=48%", review.text)
        self.assertIn("Last audit action:", review.text)
        self.assertIn("10.0 h ago", review.text)

    def test_build_ops_review_counts_expired_topups_separately(self):
        now = dt.datetime(2026, 7, 4, 12, 0, 0)
        rows = [
            {
                "timestamp": "2026-07-04T01:00:00",
                "command": "topup-complete-check",
                "soc": "43",
                "action": "topup-expired",
                "dry_run": "false",
                "result": "ok",
                "note": (
                    "actual_min=80, ownership=owned, start_soc=40, "
                    "end_soc=43, target_soc=48, implied_rate_w=900"
                ),
            },
        ]
        with TemporaryDirectory() as tmpdir:
            dashboard_path = Path(tmpdir) / "dashboard.json"
            dashboard_path.write_text(
                json.dumps(self.dashboard_payload("2026-07-04T11:50:00")),
                encoding="utf-8",
            )
            with patch("growatt_guard.ops_review.read_mode_audit_rows", return_value=rows), \
                patch("growatt_guard.ops_review.read_pause_state", return_value=None), \
                patch("growatt_guard.ops_review.read_topup_state", return_value=None), \
                patch("growatt_guard.ops_review.read_bypass_alert_state", return_value=None), \
                patch("growatt_guard.ops_review.read_battery_alert_state", return_value=None), \
                patch("growatt_guard.ops_review.read_growatt_cloud_failure_state", return_value=None), \
                patch("growatt_guard.ops_review.read_command_lock_state", return_value=None), \
                patch("growatt_guard.ops_review.topup_is_active", return_value=False):
                review = build_ops_review(
                    make_config(battery_charge_rate_w=3000.0),
                    days=7,
                    now=now,
                    dashboard_path=dashboard_path,
                )

        self.assertIn("Topup closures: 0 target reached, 1 expired, 0 legacy, 0 unclosed", review.text)
        self.assertIn("avg SOC gain --", review.text)

    def test_build_ops_review_counts_unclosed_topups(self):
        now = dt.datetime(2026, 7, 4, 12, 0, 0)
        rows = [
            {
                "timestamp": "2026-07-04T01:00:00",
                "command": "auto-topup-check",
                "soc": "40",
                "action": "auto-topup-started",
                "dry_run": "false",
                "result": "ok",
                "note": "60min, 5.0h to sunrise",
            },
        ]
        with TemporaryDirectory() as tmpdir:
            dashboard_path = Path(tmpdir) / "dashboard.json"
            dashboard_path.write_text(
                json.dumps(self.dashboard_payload("2026-07-04T11:50:00")),
                encoding="utf-8",
            )
            with patch("growatt_guard.ops_review.read_mode_audit_rows", return_value=rows), \
                patch("growatt_guard.ops_review.read_pause_state", return_value=None), \
                patch("growatt_guard.ops_review.read_topup_state", return_value=None), \
                patch("growatt_guard.ops_review.read_bypass_alert_state", return_value=None), \
                patch("growatt_guard.ops_review.read_battery_alert_state", return_value=None), \
                patch("growatt_guard.ops_review.read_growatt_cloud_failure_state", return_value=None), \
                patch("growatt_guard.ops_review.read_command_lock_state", return_value=None), \
                patch("growatt_guard.ops_review.topup_is_active", return_value=False):
                review = build_ops_review(make_config(), days=7, now=now, dashboard_path=dashboard_path)

        self.assertIn("Topup closures: 0 target reached, 0 expired, 0 legacy, 1 unclosed", review.text)

    def test_build_ops_review_counts_return_sbu_as_legacy_topup_closure(self):
        now = dt.datetime(2026, 7, 4, 12, 0, 0)
        rows = [
            {
                "timestamp": "2026-07-04T01:00:00",
                "command": "auto-topup-check",
                "soc": "40",
                "action": "auto-topup-started",
                "dry_run": "false",
                "result": "ok",
                "note": "60min, 5.0h to sunrise",
            },
            {
                "timestamp": "2026-07-04T02:00:00",
                "command": "return-sbu",
                "soc": "46",
                "action": "switch-to-sbu",
                "dry_run": "false",
                "result": "ok",
                "note": "",
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

        self.assertIn("Topup closures: 0 target reached, 0 expired, 1 legacy, 0 unclosed", review.text)

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

    def test_command_ops_review_can_print_json(self):
        review = type("Review", (), {
            "text": "ops text",
            "recommendations": ["check"],
            "severity": "ok",
            "metrics": {"days": 7},
        })()
        output = StringIO()
        with patch("growatt_guard.ops_review.build_ops_review", return_value=review), redirect_stdout(output):
            result = command_ops_review(make_config(), days=7, json_output=True)

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["text"], "ops text")
        self.assertEqual(payload["metrics"]["days"], 7)

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

    def test_command_ops_review_notify_requires_webhook(self):
        review = type("Review", (), {
            "text": "ops text",
            "recommendations": ["check"],
            "severity": "ok",
            "metrics": {"days": 7},
        })()
        with patch("growatt_guard.ops_review.build_ops_review", return_value=review), \
            patch("growatt_guard.ops_review.send_discord_embed") as send:
            with self.assertRaisesRegex(GrowattGuardError, "DISCORD_WEBHOOK_URL"):
                command_ops_review(make_config(discord_webhook_url=""), days=7, notify=True)

        send.assert_not_called()

    def test_command_ops_review_notify_fails_when_webhook_rejects(self):
        review = type("Review", (), {
            "text": "ops text",
            "recommendations": ["check"],
            "severity": "ok",
            "metrics": {"days": 7},
        })()
        with patch("growatt_guard.ops_review.build_ops_review", return_value=review), \
            patch("growatt_guard.ops_review.send_discord_embed", return_value=False):
            with self.assertRaisesRegex(GrowattGuardError, "could not be sent"):
                command_ops_review(make_config(discord_webhook_url="https://example.invalid/hook"), days=7, notify=True)


if __name__ == "__main__":
    unittest.main()
