import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_power_guard import (
    DeviceRef,
    GrowattGuardError,
    ThresholdDecision,
    command_dashboard,
    command_dashboard_refresh,
    command_dashboard_stale_alert,
    read_dashboard_stale_alert_state,
)


class DashboardTests(unittest.TestCase):
    def test_dashboard_writes_html(self):
        config = make_config()
        status = {"device": {"capacity": "50%"}, "storage_params": {"outputConfig": "0"}}
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [{"id": "morning-preserve", "name": "Preserve", "cron": "30 6 * * *", "command": "preserve-battery"}],
        }

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.dashboard.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch("growatt_guard.dashboard.validate_schedule", return_value=schedule), patch(
            "growatt_guard.dashboard.validate_schedule_overrides", return_value={"dates": {}}
        ), patch(
            "growatt_guard.dashboard.choose_preserve_threshold",
            return_value=ThresholdDecision(50, "weather disabled; using fixed threshold 50%"),
        ), patch(
            "growatt_guard.state.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch("growatt_guard.dashboard.read_mode_audit_rows", return_value=[]), redirect_stdout(StringIO()):
            output = Path(tmpdir) / "dashboard.html"
            self.assertEqual(command_dashboard(config, str(output)), 0)
            html = output.read_text(encoding="utf-8")

        self.assertIn("Growatt Dashboard", html)
        self.assertIn("Dashboard Health", html)
        self.assertIn("data-refresh-badge", html)
        self.assertIn("Cloud Streak", html)
        self.assertIn("50%", html)
        self.assertIn("SBU priority", html)

    def test_dashboard_refresh_once_writes_and_exits(self):
        config = make_config()

        with patch("growatt_guard.dashboard.write_dashboard", return_value=Path("dashboard.html")) as write_mock, redirect_stdout(
            StringIO()
        ) as stdout:
            self.assertEqual(command_dashboard_refresh(config, "dashboard.html", 1, once=True), 0)

        write_mock.assert_called_once_with(config, "dashboard.html")
        self.assertIn("Dashboard refreshed", stdout.getvalue())

    def test_dashboard_stale_alert_sends_once_when_file_is_missing(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.DASHBOARD_STALE_ALERT_FILE", Path(tmpdir) / "dashboard_stale_alert.json"
        ), patch("growatt_power_guard.send_discord_message", return_value=True) as send_mock, redirect_stdout(StringIO()):
            output = Path(tmpdir) / "dashboard.html"
            self.assertEqual(command_dashboard_stale_alert(config, str(output), 30), 0)
            self.assertEqual(command_dashboard_stale_alert(config, str(output), 30), 0)
            state = read_dashboard_stale_alert_state()

        self.assertEqual(send_mock.call_count, 1)
        self.assertIsNotNone(state)
        self.assertTrue(state["active"])

    def test_dashboard_stale_alert_clears_after_fresh_file(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.DASHBOARD_STALE_ALERT_FILE", Path(tmpdir) / "dashboard_stale_alert.json"
        ), patch("growatt_power_guard.send_discord_message", return_value=True) as send_mock, redirect_stdout(StringIO()):
            output = Path(tmpdir) / "dashboard.html"
            self.assertEqual(command_dashboard_stale_alert(config, str(output), 30), 0)
            output.write_text("<html></html>", encoding="utf-8")
            self.assertEqual(command_dashboard_stale_alert(config, str(output), 30), 0)
            state = read_dashboard_stale_alert_state()

        self.assertEqual(send_mock.call_count, 2)
        self.assertIsNone(state)

    def test_dashboard_refresh_rejects_too_fast_loop(self):
        config = make_config()

        with self.assertRaises(GrowattGuardError):
            command_dashboard_refresh(config, "dashboard.html", 1, once=False)


if __name__ == "__main__":
    unittest.main()
