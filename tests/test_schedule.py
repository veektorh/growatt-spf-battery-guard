import datetime as dt
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_guard.schedule import command_schedule_preview
from growatt_guard.schedule import lint_schedule
from growatt_power_guard import (
    GrowattGuardError,
    build_parser,
    command_run_scheduled,
    next_scheduled_runs,
    validate_schedule,
    validate_schedule_overrides,
)


class ScheduleTests(unittest.TestCase):
    def test_validate_schedule_accepts_current_file(self):
        schedule = validate_schedule()

        self.assertEqual(schedule["timezone"], "Africa/Lagos")
        self.assertGreaterEqual(len(schedule["jobs"]), 1)

    def test_validate_schedule_rejects_unknown_command(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "schedule.json"
            path.write_text(
                '{"timezone":"Africa/Lagos","jobs":[{"id":"bad","cron":"0 1 * * *","command":"bad-command"}]}',
                encoding="utf-8",
            )

            with self.assertRaises(GrowattGuardError):
                validate_schedule(path)

    def test_validate_schedule_accepts_supported_command_args(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "schedule.json"
            path.write_text(
                (
                    '{"timezone":"Africa/Lagos","jobs":[{"id":"health","cron":"10 6 * * *",'
                    '"command":"health-check","args":["--notify"]}]}'
                ),
                encoding="utf-8",
            )

            schedule = validate_schedule(path)

        self.assertEqual(schedule["jobs"][0]["args"], ["--notify"])

    def test_validate_schedule_accepts_observability_refresh(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "schedule.json"
            path.write_text(
                (
                    '{"timezone":"Africa/Lagos","jobs":[{"id":"observability",'
                    '"cron":"*/10 * * * *","command":"observability-refresh"}]}'
                ),
                encoding="utf-8",
            )

            schedule = validate_schedule(path)

        self.assertEqual(schedule["jobs"][0]["command"], "observability-refresh")

    def test_validate_schedule_rejects_unsupported_command_args(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "schedule.json"
            path.write_text(
                (
                    '{"timezone":"Africa/Lagos","jobs":[{"id":"preserve","cron":"30 6 * * *",'
                    '"command":"preserve-battery","args":["--notify"]}]}'
                ),
                encoding="utf-8",
            )

            with self.assertRaises(GrowattGuardError):
                validate_schedule(path)

    def test_validate_schedule_overrides_accepts_skip_and_replace(self):
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [
                {"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"},
                {"id": "morning-health", "cron": "10 6 * * *", "command": "health-check", "args": ["--notify"]},
            ],
        }
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "schedule_overrides.json"
            path.write_text(
                (
                    '{"dates":{"2026-06-26":{"note":"skip",'
                    '"skip":["morning-preserve"],'
                    '"replace":{"morning-health":{"command":"health-check","args":["--notify"]}}}}}'
                ),
                encoding="utf-8",
            )

            overrides = validate_schedule_overrides(schedule, path)

        self.assertIn("2026-06-26", overrides["dates"])

    def test_run_scheduled_skips_date_override(self):
        config = make_config(discord_notify_skip=False)
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [{"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"}],
        }
        overrides = {"dates": {dt.date.today().isoformat(): {"skip": ["morning-preserve"], "note": "test skip"}}}

        with patch("growatt_guard.modes.validate_schedule", return_value=schedule), patch(
            "growatt_guard.modes.validate_schedule_overrides", return_value=overrides
        ), patch("growatt_guard.modes.dispatch_command") as dispatch_mock, redirect_stdout(StringIO()) as stdout:
            self.assertEqual(command_run_scheduled(config, "morning-preserve"), 0)

        dispatch_mock.assert_not_called()
        self.assertIn("Skipped scheduled job", stdout.getvalue())

    def test_run_scheduled_dispatches_replacement(self):
        config = make_config()
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [{"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"}],
        }
        overrides = {
            "dates": {
                dt.date.today().isoformat(): {
                    "replace": {"morning-preserve": {"command": "health-check", "args": ["--notify"]}}
                }
            }
        }

        with patch("growatt_guard.modes.validate_schedule", return_value=schedule), patch(
            "growatt_guard.modes.validate_schedule_overrides", return_value=overrides
        ), patch("growatt_guard.modes.dispatch_command", return_value=0) as dispatch_mock:
            self.assertEqual(command_run_scheduled(config, "morning-preserve"), 0)

        dispatched_args = dispatch_mock.call_args.args[1]
        self.assertEqual(dispatched_args.command, "health-check")
        self.assertTrue(dispatched_args.notify)

    def test_next_scheduled_runs_orders_jobs(self):
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [
                {"id": "morning-preserve", "name": "Preserve", "cron": "30 6 * * *", "command": "preserve-battery"},
                {"id": "daily-summary", "name": "Summary", "cron": "0 21 * * *", "command": "daily-summary"},
            ],
        }

        runs = next_scheduled_runs(schedule, now=dt.datetime(2026, 6, 20, 6, 29), limit=2)

        self.assertEqual(runs[0][0], dt.datetime(2026, 6, 20, 6, 30))
        self.assertEqual(runs[0][1]["id"], "morning-preserve")

    def test_schedule_preview_command_is_available(self):
        args = build_parser().parse_args(["schedule-preview", "--days", "3", "--json"])

        self.assertEqual(args.command, "schedule-preview")
        self.assertEqual(args.days, 3)
        self.assertTrue(args.json)

    def test_schedule_preview_shows_fixed_and_interval_jobs(self):
        config = make_config()
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [
                {"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"},
                {"id": "battery-check", "cron": "*/30 * * * *", "command": "battery-alert"},
            ],
        }
        overrides = {"dates": {}}

        with patch("growatt_guard.schedule.validate_schedule", return_value=schedule), patch(
            "growatt_guard.schedule.validate_schedule_overrides", return_value=overrides
        ), redirect_stdout(StringIO()) as stdout:
            result = command_schedule_preview(config, days=1, today=dt.date(2026, 6, 20))

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("2026-06-20", output)
        self.assertIn("06:30", output)
        self.assertIn("preserve-battery", output)
        self.assertIn("every 30 min", output)
        self.assertIn("battery-alert", output)
        self.assertIn("x48/day", output)

    def test_schedule_preview_json_output(self):
        config = make_config()
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [{"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"}],
        }

        with patch("growatt_guard.schedule.validate_schedule", return_value=schedule), patch(
            "growatt_guard.schedule.validate_schedule_overrides", return_value={"dates": {}}
        ), redirect_stdout(StringIO()) as stdout:
            result = command_schedule_preview(config, days=1, today=dt.date(2026, 6, 20), json_output=True)

        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["dates"][0]["jobs"][0]["job_id"], "morning-preserve")

    def test_schedule_lint_warns_about_duplicate_pvoutput_poller(self):
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [
                {"id": "observability", "cron": "*/10 * * * *", "command": "observability-refresh"},
                {"id": "pvoutput", "cron": "*/10 * * * *", "command": "pvoutput-upload"},
            ],
        }

        items = lint_schedule(schedule)

        self.assertTrue(any(item.status == "WARN" and "both scheduled" in item.detail for item in items))

    def test_schedule_lint_warns_about_fast_polling(self):
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [{"id": "observability", "cron": "*/3 * * * *", "command": "observability-refresh"}],
        }

        items = lint_schedule(schedule)

        self.assertTrue(any(item.status == "WARN" and "every 3 min" in item.detail for item in items))

    def test_schedule_preview_marks_skipped_and_replaced_jobs(self):
        config = make_config()
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [
                {"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"},
                {"id": "morning-health", "cron": "10 6 * * *", "command": "health-check", "args": ["--notify"]},
            ],
        }
        overrides = {
            "dates": {
                "2026-06-20": {
                    "note": "adjusted day",
                    "skip": ["morning-preserve"],
                    "replace": {"morning-health": {"command": "health-check", "args": ["--notify"]}},
                }
            }
        }

        with patch("growatt_guard.schedule.validate_schedule", return_value=schedule), patch(
            "growatt_guard.schedule.validate_schedule_overrides", return_value=overrides
        ), redirect_stdout(StringIO()) as stdout:
            result = command_schedule_preview(config, days=1, today=dt.date(2026, 6, 20))

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("adjusted day", output)
        self.assertIn("[SKIP]", output)
        self.assertIn("[-> health-check --notify]", output)

    def test_run_scheduled_dry_plan_flag_is_available(self):
        args = build_parser().parse_args(["run-scheduled", "morning-preserve", "--dry-plan"])

        self.assertEqual(args.command, "run-scheduled")
        self.assertEqual(args.job_id, "morning-preserve")
        self.assertTrue(args.dry_plan)

    def test_run_scheduled_dry_plan_no_override(self):
        config = make_config()
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [{"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"}],
        }
        with patch("growatt_guard.modes.validate_schedule", return_value=schedule), patch(
            "growatt_guard.modes.validate_schedule_overrides", return_value={"dates": {}}
        ), patch("growatt_guard.modes.today_schedule_override", return_value={}), patch(
            "growatt_guard.modes.read_pause_state", return_value=None
        ), redirect_stdout(StringIO()) as stdout:
            result = command_run_scheduled(config, "morning-preserve", dry_plan=True)

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("Dry plan: run-scheduled morning-preserve", output)
        self.assertIn("Scheduled command:  preserve-battery", output)
        self.assertIn("Override today:     none", output)
        self.assertIn("Mode-changing:      yes", output)
        self.assertIn("Paused:             no", output)
        self.assertIn("would run", output)

    def test_run_scheduled_dry_plan_skip_override(self):
        config = make_config()
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [{"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"}],
        }
        with patch("growatt_guard.modes.validate_schedule", return_value=schedule), patch(
            "growatt_guard.modes.validate_schedule_overrides", return_value={"dates": {}}
        ), patch(
            "growatt_guard.modes.today_schedule_override",
            return_value={"skip": ["morning-preserve"], "note": "grid work"},
        ), redirect_stdout(StringIO()) as stdout:
            result = command_run_scheduled(config, "morning-preserve", dry_plan=True)

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("Override today:     SKIP", output)
        self.assertIn("grid work", output)
        self.assertIn("would skip", output)

    def test_run_scheduled_dry_plan_replace_override(self):
        config = make_config()
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [{"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"}],
        }
        with patch("growatt_guard.modes.validate_schedule", return_value=schedule), patch(
            "growatt_guard.modes.validate_schedule_overrides", return_value={"dates": {}}
        ), patch(
            "growatt_guard.modes.today_schedule_override",
            return_value={"replace": {"morning-preserve": {"command": "health-check"}}},
        ), patch("growatt_guard.modes.read_pause_state", return_value=None), redirect_stdout(
            StringIO()
        ) as stdout:
            result = command_run_scheduled(config, "morning-preserve", dry_plan=True)

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("replace -> health-check", output)
        self.assertIn("Effective command:  health-check", output)
        self.assertIn("Mode-changing:      no", output)
        self.assertIn("would run", output)

    def test_run_scheduled_dry_plan_paused_mode_changing(self):
        config = make_config()
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [{"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"}],
        }
        pause_state = {"until": "2099-01-01T00:00:00", "reason": "grid work"}
        with patch("growatt_guard.modes.validate_schedule", return_value=schedule), patch(
            "growatt_guard.modes.validate_schedule_overrides", return_value={"dates": {}}
        ), patch("growatt_guard.modes.today_schedule_override", return_value={}), patch(
            "growatt_guard.modes.read_pause_state", return_value=pause_state
        ), patch("growatt_guard.modes.pause_message", return_value="paused until 2099-01-01"), redirect_stdout(
            StringIO()
        ) as stdout:
            result = command_run_scheduled(config, "morning-preserve", dry_plan=True)

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("Paused:             yes", output)
        self.assertIn("paused until 2099-01-01", output)
        self.assertIn("would skip  (paused)", output)


if __name__ == "__main__":
    unittest.main()
