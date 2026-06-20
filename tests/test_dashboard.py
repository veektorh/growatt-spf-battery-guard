import datetime as dt
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
from growatt_guard.dashboard import _today_job_rows, _upcoming_override_rows


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

    def test_dashboard_html_includes_todays_schedule_section(self):
        config = make_config()
        status = {"device": {"capacity": "60%"}, "storage_params": {"outputConfig": "0"}}
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [{"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"}],
        }

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.dashboard.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch("growatt_guard.dashboard.validate_schedule", return_value=schedule), patch(
            "growatt_guard.dashboard.validate_schedule_overrides", return_value={"dates": {}}
        ), patch(
            "growatt_guard.dashboard.choose_preserve_threshold",
            return_value=ThresholdDecision(50, "fixed threshold"),
        ), patch(
            "growatt_guard.state.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch("growatt_guard.dashboard.read_mode_audit_rows", return_value=[]), redirect_stdout(StringIO()):
            output = Path(tmpdir) / "dashboard.html"
            command_dashboard(config, str(output))
            html = output.read_text(encoding="utf-8")

        self.assertIn("Today&#8217;s Schedule", html)
        self.assertIn("morning-preserve", html)


class TodayJobRowsTests(unittest.TestCase):
    SCHEDULE = {
        "jobs": [
            {"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"},
            {"id": "morning-health", "cron": "10 6 * * *", "command": "health-check", "args": ["--notify"]},
            {"id": "watchdog", "cron": "*/30 * * * *", "command": "watchdog-sbu"},
        ],
    }

    def test_all_ok_with_no_overrides(self):
        today = dt.date(2026, 6, 20)
        rows = _today_job_rows(self.SCHEDULE, {}, today)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(st == "OK" for _, _, _, st in rows))

    def test_skip_all_marks_jobs_as_skip(self):
        today = dt.date(2026, 6, 20)
        rows = _today_job_rows(self.SCHEDULE, {"skip_all": True}, today)
        self.assertTrue(all(st == "SKIP" for _, _, _, st in rows))

    def test_individual_skip_marks_one_job(self):
        today = dt.date(2026, 6, 20)
        rows = _today_job_rows(self.SCHEDULE, {"skip": ["morning-preserve"]}, today)
        statuses = {jid: st for _, jid, _, st in rows}
        self.assertEqual(statuses["morning-preserve"], "SKIP")
        self.assertEqual(statuses["morning-health"], "OK")

    def test_replace_shows_replacement_command(self):
        today = dt.date(2026, 6, 20)
        override = {"replace": {"morning-preserve": {"command": "health-check", "args": ["--notify"]}}}
        rows = _today_job_rows(self.SCHEDULE, override, today)
        statuses = {jid: st for _, jid, _, st in rows}
        self.assertIn("health-check", statuses["morning-preserve"])

    def test_interval_job_shows_every_n_min_label(self):
        today = dt.date(2026, 6, 20)
        rows = _today_job_rows(self.SCHEDULE, {}, today)
        time_strs = {jid: t for t, jid, _, _ in rows}
        self.assertIn("every", time_strs["watchdog"])


class UpcomingOverrideRowsTests(unittest.TestCase):
    def test_empty_when_no_overrides(self):
        today = dt.date(2026, 6, 20)
        rows = _upcoming_override_rows({}, today)
        self.assertEqual(rows, [])

    def test_excludes_today_and_past(self):
        today = dt.date(2026, 6, 20)
        overrides = {
            "dates": {
                "2026-06-19": {"skip_all": True},
                "2026-06-20": {"skip_all": True},
                "2026-06-21": {"skip_all": True},
            }
        }
        rows = _upcoming_override_rows(overrides, today)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "2026-06-21")

    def test_skip_all_shows_correct_action(self):
        today = dt.date(2026, 6, 20)
        overrides = {"dates": {"2026-06-21": {"skip_all": True, "note": "Holiday"}}}
        rows = _upcoming_override_rows(overrides, today)
        date_str, note, action = rows[0]
        self.assertEqual(action, "skip-all")
        self.assertEqual(note, "Holiday")


if __name__ == "__main__":
    unittest.main()
