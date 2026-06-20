import datetime as dt
import subprocess
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_power_guard import (
    DeviceRef,
    HealthCheckItem,
    ThresholdDecision,
    append_mode_audit,
    build_daily_summary,
    build_weekly_summary,
    build_parser,
    check_cron_schedule,
    command_battery_alert,
    command_health_check,
    format_health_report,
)


class GrowattPowerGuardTests(unittest.TestCase):
    def test_preserve_battery_command_is_available(self):
        args = build_parser().parse_args(["preserve-battery"])

        self.assertEqual(args.command, "preserve-battery")

    def test_test_discord_command_is_available(self):
        args = build_parser().parse_args(["test-discord"])

        self.assertEqual(args.command, "test-discord")

    def test_watchdog_sbu_command_is_available(self):
        args = build_parser().parse_args(["watchdog-sbu"])

        self.assertEqual(args.command, "watchdog-sbu")

    def test_force_utility_command_is_available(self):
        args = build_parser().parse_args(["force-utility", "--reason", "manual top-up"])

        self.assertEqual(args.command, "force-utility")
        self.assertEqual(args.reason, "manual top-up")

    def test_discord_bot_command_is_available(self):
        args = build_parser().parse_args(["serve-discord-bot"])

        self.assertEqual(args.command, "serve-discord-bot")

    def test_daily_summary_command_is_available(self):
        args = build_parser().parse_args(["daily-summary"])

        self.assertEqual(args.command, "daily-summary")

    def test_weekly_summary_command_is_available(self):
        args = build_parser().parse_args(["weekly-summary"])

        self.assertEqual(args.command, "weekly-summary")

    def test_monthly_summary_command_is_available(self):
        args = build_parser().parse_args(["monthly-summary"])

        self.assertEqual(args.command, "monthly-summary")

    def test_rotate_logs_command_is_available(self):
        args = build_parser().parse_args(["rotate-logs"])

        self.assertEqual(args.command, "rotate-logs")

    def test_validate_schedule_command_is_available(self):
        args = build_parser().parse_args(["validate-schedule"])

        self.assertEqual(args.command, "validate-schedule")

    def test_weather_threshold_command_is_available(self):
        args = build_parser().parse_args(["weather-threshold"])

        self.assertEqual(args.command, "weather-threshold")

    def test_battery_alert_command_is_available(self):
        args = build_parser().parse_args(["battery-alert"])

        self.assertEqual(args.command, "battery-alert")

    def test_pause_commands_are_available(self):
        pause_args = build_parser().parse_args(["pause", "--hours", "2", "--reason", "maintenance"])
        resume_args = build_parser().parse_args(["resume"])
        status_args = build_parser().parse_args(["pause-status"])

        self.assertEqual(pause_args.command, "pause")
        self.assertEqual(pause_args.hours, 2)
        self.assertEqual(pause_args.reason, "maintenance")
        self.assertEqual(resume_args.command, "resume")
        self.assertEqual(status_args.command, "pause-status")

    def test_health_check_command_is_available(self):
        args = build_parser().parse_args(["health-check", "--notify"])

        self.assertEqual(args.command, "health-check")
        self.assertTrue(args.notify)

    def test_run_scheduled_command_is_available(self):
        args = build_parser().parse_args(["run-scheduled", "morning-preserve"])

        self.assertEqual(args.command, "run-scheduled")
        self.assertEqual(args.job_id, "morning-preserve")

    def test_dashboard_command_is_available(self):
        args = build_parser().parse_args(["dashboard", "--output", "dash.html"])

        self.assertEqual(args.command, "dashboard")
        self.assertEqual(args.output, "dash.html")

    def test_dashboard_refresh_command_is_available(self):
        args = build_parser().parse_args(["dashboard-refresh", "--interval-minutes", "10", "--once"])

        self.assertEqual(args.command, "dashboard-refresh")
        self.assertEqual(args.interval_minutes, 10)
        self.assertTrue(args.once)

    def test_dashboard_stale_alert_command_is_available(self):
        args = build_parser().parse_args(["dashboard-stale-alert", "--max-age-minutes", "20"])

        self.assertEqual(args.command, "dashboard-stale-alert")
        self.assertEqual(args.max_age_minutes, 20)

    def test_serve_dashboard_command_is_available(self):
        args = build_parser().parse_args(["serve-dashboard", "--host", "127.0.0.1", "--port", "8080"])

        self.assertEqual(args.command, "serve-dashboard")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8080)

    def test_clear_stale_lock_command_is_available(self):
        args = build_parser().parse_args(["clear-stale-lock"])

        self.assertEqual(args.command, "clear-stale-lock")

    def test_schedule_override_list_is_available(self):
        args = build_parser().parse_args(["schedule-override", "list"])

        self.assertEqual(args.command, "schedule-override")
        self.assertEqual(args.override_subcommand, "list")

    def test_schedule_override_add_skip_is_available(self):
        args = build_parser().parse_args(
            ["schedule-override", "add-skip", "2026-07-01", "morning-preserve", "--note", "Maintenance"]
        )

        self.assertEqual(args.command, "schedule-override")
        self.assertEqual(args.override_subcommand, "add-skip")
        self.assertEqual(args.date, "2026-07-01")
        self.assertEqual(args.job_id, "morning-preserve")
        self.assertEqual(args.note, "Maintenance")

    def test_schedule_override_add_replace_is_available(self):
        args = build_parser().parse_args(
            ["schedule-override", "add-replace", "2026-07-01", "morning-preserve", "health-check", "--notify"]
        )

        self.assertEqual(args.override_subcommand, "add-replace")
        self.assertEqual(args.replacement_command, "health-check")
        self.assertEqual(args.replacement_args, ["--notify"])

    def test_schedule_override_remove_is_available(self):
        args = build_parser().parse_args(["schedule-override", "remove", "2026-07-01"])

        self.assertEqual(args.override_subcommand, "remove")
        self.assertEqual(args.date, "2026-07-01")
        self.assertEqual(args.job_id, "")

    def test_outage_profile_apply_is_available(self):
        args = build_parser().parse_args(
            ["outage-profile", "apply", "skip-all", "2026-07-01", "2026-07-02", "--note", "Holiday"]
        )

        self.assertEqual(args.command, "outage-profile")
        self.assertEqual(args.outage_subcommand, "apply")
        self.assertEqual(args.profile_name, "skip-all")
        self.assertEqual(args.dates, ["2026-07-01", "2026-07-02"])
        self.assertEqual(args.note, "Holiday")

    def test_build_daily_summary_includes_key_metrics(self):
        status = {
            "device": {"capacity": "50 %"},
            "storage_params": {
                "storageBean": {
                    "outputConfig": "0",
                    "ppvText": "1234.0 W",
                    "vGrid": 230.5,
                    "outPutPower": 900,
                    "eChargeTodayText": "4.2 kWh",
                }
            },
        }

        with patch("growatt_guard.audit.summarize_today_log_counts", return_value={
            "success": 2,
            "failure": 0,
            "watchdog_repairs": 1,
            "preserve_actions": 1,
            "return_sbu_actions": 1,
        }):
            summary = build_daily_summary(status)

        self.assertIn("Battery SOC: 50%", summary)
        self.assertIn("Output source: SBU priority [0]", summary)
        self.assertIn("PV power: 1234.0 W", summary)
        self.assertIn("Successful mode responses: 2", summary)

    def test_format_health_report_summarizes_failures(self):
        report = format_health_report(
            [
                HealthCheckItem("Config", "OK", "loaded"),
                HealthCheckItem("Cron jobs", "FAIL", "missing"),
            ]
        )

        self.assertIn("Result: FAIL", report)
        self.assertIn("[FAIL] Cron jobs: missing", report)

    def test_check_cron_schedule_accepts_installed_jobs(self):
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [
                {"id": "morning-health", "cron": "10 6 * * *", "command": "health-check", "args": ["--notify"]},
                {"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"},
                {"id": "morning-return-sbu", "cron": "55 7 * * *", "command": "return-sbu"},
            ],
        }
        crontab = "\n".join(
            [
                "CRON_TZ=Africa/Lagos",
                (
                    "10 6 * * * cd /home/ubuntu/automation && .venv/bin/python "
                    "growatt_power_guard.py run-scheduled morning-health >> logs/cron.log 2>&1 # growatt-power-guard"
                ),
                (
                    "30 6 * * * cd /home/ubuntu/automation && .venv/bin/python "
                    "growatt_power_guard.py run-scheduled morning-preserve >> logs/cron.log 2>&1 # growatt-power-guard"
                ),
                (
                    "55 7 * * * cd /home/ubuntu/automation && .venv/bin/python "
                    "growatt_power_guard.py run-scheduled morning-return-sbu >> logs/cron.log 2>&1 # growatt-power-guard"
                ),
            ]
        )

        with patch("growatt_guard.schedule.os.name", "posix"), patch(
            "growatt_guard.schedule.subprocess.run",
            return_value=subprocess.CompletedProcess(["crontab", "-l"], 0, stdout=crontab, stderr=""),
        ):
            checks = check_cron_schedule(schedule)

        self.assertTrue(all(check.status == "OK" for check in checks))

    def test_command_health_check_reports_ok_when_everything_is_available(self):
        config = make_config(dry_run=False, discord_webhook_url="https://discord.com/api/webhooks/example")
        status = {"device": {"capacity": "50%"}, "storage_params": {"outputConfig": "0"}}
        schedule = {"timezone": "Africa/Lagos", "jobs": [{"cron": "30 6 * * *", "command": "preserve-battery"}]}

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.PAUSE_FILE", Path(tmpdir) / "pause.json"), patch(
            "growatt_guard.state.COMMAND_LOCK_FILE", Path(tmpdir) / "mode_command.lock"
        ), patch(
            "growatt_guard.state.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch(
            "growatt_guard.state.TOPUP_STATE_FILE", Path(tmpdir) / "topup_active.json"
        ), patch(
            "growatt_guard.health.DASHBOARD_FILE", Path(tmpdir) / "dashboard.html"
        ), patch("growatt_guard.health.validate_schedule", return_value=schedule), patch(
            "growatt_guard.health.validate_schedule_overrides", return_value={"dates": {}}
        ), patch(
            "growatt_guard.health.check_cron_schedule",
            return_value=[HealthCheckItem("Cron jobs", "OK", "1 scheduled job installed.")],
        ), patch(
            "growatt_guard.health.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch(
            "growatt_guard.health.choose_preserve_threshold",
            return_value=ThresholdDecision(50, "weather disabled; using fixed threshold 50%"),
        ), patch("growatt_guard.health.read_pause_state", return_value=None), redirect_stdout(StringIO()) as stdout:
            (Path(tmpdir) / "dashboard.html").write_text("<html></html>", encoding="utf-8")
            exit_code = command_health_check(config)

        self.assertEqual(exit_code, 0)
        self.assertIn("Result: OK", stdout.getvalue())

    def test_battery_alert_sends_once_while_low(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")
        status = {"device": {"capacity": "29%"}, "storage_params": {"outputConfig": "0"}}

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
            "growatt_guard.state.BATTERY_ALERT_FILE", Path(tmpdir) / "battery_alert.json"
        ), patch(
            "growatt_guard.modes.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch("growatt_guard.modes.send_discord_embed", return_value=True) as send_mock, redirect_stdout(StringIO()):
            self.assertEqual(command_battery_alert(config), 0)
            self.assertEqual(command_battery_alert(config), 0)

        self.assertEqual(send_mock.call_count, 1)

    def test_append_mode_audit_writes_csv_row(self):
        config = make_config(dry_run=False)

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.audit.LOG_DIR", Path(tmpdir)), patch(
            "growatt_guard.audit.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"
        ):
            append_mode_audit(
                config,
                "preserve-battery",
                soc=49,
                threshold=50,
                weather_category="rainy/cloudy",
                previous_mode="SBU priority [0]",
                action="switch-to-utility",
                result={"success": True},
            )
            content = (Path(tmpdir) / "mode_decisions.csv").read_text(encoding="utf-8")

        self.assertIn("timestamp,command,soc,threshold,weather_category", content)
        self.assertIn("preserve-battery,49,50,rainy/cloudy", content)
        self.assertIn("switch-to-utility", content)

    def test_monthly_summary_uses_audit_rows(self):
        from growatt_power_guard import build_monthly_summary
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.audit.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"):
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            audit_path.write_text(
                "\n".join(
                    [
                        "timestamp,command,soc,threshold,weather_category,previous_mode,action,dry_run,result,note",
                        "2026-06-01T06:30:00,preserve-battery,47,50,rainy/cloudy,SBU priority [0],switch-to-utility,false,ok,",
                        "2026-06-15T06:30:00,preserve-battery,55,50,normal,SBU priority [0],no-change,false,skipped,",
                    ]
                ),
                encoding="utf-8",
            )

            summary = build_monthly_summary(dt.datetime(2026, 6, 20, 12, 0))

        self.assertIn("monthly performance", summary)
        self.assertIn("Utility switches: 1", summary)
        self.assertIn("Average preserve-check SOC: 51%", summary)

    def test_weekly_summary_includes_recommendations(self):
        from growatt_power_guard import build_weekly_summary
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.audit.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"):
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            rows = ["timestamp,command,soc,threshold,weather_category,previous_mode,action,dry_run,result,note"]
            for i in range(6):
                rows.append(f"2026-06-{14+i:02d}T06:30:00,preserve-battery,45,50,rainy/cloudy,SBU priority [0],switch-to-utility,false,ok,")
            audit_path.write_text("\n".join(rows), encoding="utf-8")

            summary = build_weekly_summary(dt.datetime(2026, 6, 20, 12, 0))

        self.assertIn("Recommendations:", summary)
        self.assertIn("LOW_BATTERY_SOC", summary)

    def test_weekly_summary_uses_audit_rows(self):
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.audit.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"):
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            audit_path.write_text(
                "\n".join(
                    [
                        "timestamp,command,soc,threshold,weather_category,previous_mode,action,dry_run,result,note",
                        "2026-06-18T06:30:00,preserve-battery,49,50,rainy/cloudy,SBU priority [0],switch-to-utility,false,ok,",
                        "2026-06-19T06:30:00,preserve-battery,55,50,normal,SBU priority [0],no-change,false,skipped,",
                        "2026-06-19T08:01:00,watchdog-sbu,54,,normal,Utility first [2],repair-sbu,false,ok,",
                    ]
                ),
                encoding="utf-8",
            )

            summary = build_weekly_summary(dt.datetime(2026, 6, 20, 12, 0))

        self.assertIn("Utility switches: 1", summary)
        self.assertIn("Watchdog repairs: 1", summary)
        self.assertIn("Average preserve-check SOC: 52%", summary)


if __name__ == "__main__":
    unittest.main()
