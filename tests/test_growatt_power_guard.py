import datetime as dt
import json
import logging.handlers
import os
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
    GrowattGuardError,
    HealthCheckItem,
    ThresholdDecision,
    append_mode_audit,
    build_daily_summary,
    build_weekly_summary,
    build_parser,
    check_cron_schedule,
    command_battery_alert,
    command_estimate_charge_rate,
    command_health_check,
    command_public_hygiene,
    command_rotate_logs,
    format_health_report,
    main,
    setup_logging,
)
from growatt_guard.schedule import HealthCheckItem as ScheduleHealthCheckItem
from growatt_guard.health import health_embed_description, health_embed_fields


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

    def test_ops_review_command_is_available(self):
        args = build_parser().parse_args(["ops-review", "--days", "3", "--notify", "--json"])

        self.assertEqual(args.command, "ops-review")
        self.assertEqual(args.days, 3)
        self.assertTrue(args.notify)
        self.assertTrue(args.json)

    def test_deployment_preflight_command_is_available(self):
        args = build_parser().parse_args(["deployment-preflight", "--json"])

        self.assertEqual(args.command, "deployment-preflight")
        self.assertTrue(args.json)

    def test_rotate_logs_command_is_available(self):
        args = build_parser().parse_args(["rotate-logs"])

        self.assertEqual(args.command, "rotate-logs")

    def test_rotate_logs_removes_generated_files_but_preserves_stateful_logs(self):
        config = make_config(log_retention_days=30)

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.reports.LOG_DIR", Path(tmpdir)):
            old_probe = Path(tmpdir) / "growatt-probe-20200101-000000.json"
            old_probe.write_text("{}", encoding="utf-8")
            active_log = Path(tmpdir) / "growatt_power_guard.log"
            cron_log = Path(tmpdir) / "cron.log"
            audit_log = Path(tmpdir) / "mode_decisions.csv"
            metrics_log = Path(tmpdir) / "dashboard_metrics.jsonl"
            unrelated_old_file = Path(tmpdir) / "notes.txt"
            for file_path in (active_log, cron_log, audit_log, metrics_log, unrelated_old_file):
                file_path.write_text("keep", encoding="utf-8")

            old_timestamp = (dt.datetime.now() - dt.timedelta(days=45)).timestamp()
            for file_path in (old_probe, active_log, cron_log, audit_log, metrics_log, unrelated_old_file):
                os.utime(file_path, (old_timestamp, old_timestamp))

            output = StringIO()
            with redirect_stdout(output):
                command_rotate_logs(config)

            self.assertFalse(old_probe.exists())
            self.assertTrue(active_log.exists())
            self.assertTrue(cron_log.exists())
            self.assertTrue(audit_log.exists())
            self.assertTrue(metrics_log.exists())
            self.assertTrue(unrelated_old_file.exists())
            self.assertIn("Removed 1 old log/probe files", output.getvalue())

    def test_setup_logging_uses_rotating_file_handler(self):
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.cli.LOG_DIR", Path(tmpdir)), patch(
            "growatt_guard.cli.LOG_FILE", Path(tmpdir) / "growatt_power_guard.log"
        ):
            setup_logging(verbose=False)
            handlers = logging.getLogger().handlers
            try:
                file_handlers = [handler for handler in handlers if isinstance(handler, logging.handlers.RotatingFileHandler)]
                self.assertEqual(len(file_handlers), 1)
                self.assertGreater(file_handlers[0].maxBytes, 0)
                self.assertGreater(file_handlers[0].backupCount, 0)
            finally:
                for handler in list(logging.getLogger().handlers):
                    handler.close()
                logging.getLogger().handlers.clear()

    def test_setup_logging_replaces_existing_handlers(self):
        class DummyHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.closed_by_setup = False

            def close(self):
                self.closed_by_setup = True
                super().close()

        dummy = DummyHandler()
        root = logging.getLogger()
        root.addHandler(dummy)
        try:
            with TemporaryDirectory() as tmpdir, patch("growatt_guard.cli.LOG_DIR", Path(tmpdir)), patch(
                "growatt_guard.cli.LOG_FILE", Path(tmpdir) / "growatt_power_guard.log"
            ):
                setup_logging(verbose=False)
                self.assertTrue(dummy.closed_by_setup)
                self.assertNotIn(dummy, logging.getLogger().handlers)
                self.assertEqual(len(logging.getLogger().handlers), 2)
        finally:
            for handler in list(logging.getLogger().handlers):
                handler.close()
            logging.getLogger().handlers.clear()

    def test_validate_schedule_command_is_available(self):
        args = build_parser().parse_args(["validate-schedule"])

        self.assertEqual(args.command, "validate-schedule")

    def test_public_hygiene_command_is_available(self):
        args = build_parser().parse_args(["public-hygiene"])

        self.assertEqual(args.command, "public-hygiene")

    def test_public_hygiene_command_uses_shared_checker(self):
        with patch("scripts.check_public_hygiene.main", return_value=0) as checker:
            self.assertEqual(command_public_hygiene(), 0)

        checker.assert_called_once_with()

    def test_public_hygiene_main_does_not_load_config(self):
        with patch("growatt_guard.cli.setup_logging"), patch(
            "growatt_guard.cli.load_config", side_effect=AssertionError("config should not load")
        ), patch("scripts.check_public_hygiene.main", return_value=0) as checker:
            self.assertEqual(main(["public-hygiene"]), 0)

        checker.assert_called_once_with()

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

    def test_diagnostic_commands_are_available(self):
        service_args = build_parser().parse_args(["service-status", "--json"])
        bundle_args = build_parser().parse_args(["diagnostic-bundle", "--json", "--include-cloud"])
        pv_probe_args = build_parser().parse_args(["pv-metric-probe", "--json"])
        redact_probe_args = build_parser().parse_args(["redact-probe", "raw.json", "--output", "safe.json"])

        self.assertEqual(service_args.command, "service-status")
        self.assertTrue(service_args.json)
        self.assertEqual(bundle_args.command, "diagnostic-bundle")
        self.assertTrue(bundle_args.json)
        self.assertTrue(bundle_args.include_cloud)
        self.assertEqual(pv_probe_args.command, "pv-metric-probe")
        self.assertTrue(pv_probe_args.json)
        self.assertEqual(redact_probe_args.command, "redact-probe")
        self.assertEqual(redact_probe_args.input, "raw.json")
        self.assertEqual(redact_probe_args.output, "safe.json")

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

    def test_schedule_calendar_command_is_available(self):
        args = build_parser().parse_args(["schedule-calendar", "--days", "21", "--output", "schedule.ics", "--all"])

        self.assertEqual(args.command, "schedule-calendar")
        self.assertEqual(args.days, 21)
        self.assertEqual(args.output, "schedule.ics")
        self.assertTrue(args.all)

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
        self.assertIn("Next:", report)

    def test_health_report_accepts_schedule_health_items(self):
        report = format_health_report(
            [
                ScheduleHealthCheckItem("Cron jobs", "FAIL", "missing"),
            ]
        )

        self.assertIn("[FAIL] Cron jobs: missing", report)
        self.assertIn("Next:", report)

    def test_health_embed_keeps_discord_alert_compact(self):
        checks = [
            HealthCheckItem("Config", "OK", "loaded"),
            HealthCheckItem("Dashboard freshness", "WARN", "stale"),
            HealthCheckItem("Growatt cloud", "FAIL", "login failed"),
        ]

        fields = health_embed_fields(checks)

        self.assertEqual(health_embed_description(checks), "1 OK, 1 WARN, 1 FAIL. Showing only checks that need attention.")
        self.assertEqual([field["name"] for field in fields], ["[WARN] Dashboard freshness", "[FAIL] Growatt cloud"])
        self.assertNotIn("Config", "\n".join(field["name"] for field in fields))

    def test_health_embed_caps_problem_fields(self):
        checks = [HealthCheckItem(f"Check {i}", "WARN", "detail") for i in range(8)]

        fields = health_embed_fields(checks)

        self.assertEqual(len(fields), 7)
        self.assertEqual(fields[-1]["name"], "More checks")
        self.assertIn("2 additional", fields[-1]["value"])

    def test_check_cron_schedule_accepts_installed_jobs(self):
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [
                {"id": "morning-health", "cron": "10 6 * * *", "command": "health-check", "args": ["--notify"]},
                {"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"},
                {"id": "morning-return-sbu", "cron": "0 8 * * *", "command": "return-sbu"},
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
                    "0 8 * * * cd /home/ubuntu/automation && .venv/bin/python "
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
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [
                {"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"},
                {"id": "morning-return", "cron": "55 7 * * *", "command": "return-sbu"},
                {"id": "morning-watchdog", "cron": "1 8 * * *", "command": "watchdog-sbu"},
            ],
        }
        next_runs = [
            (dt.datetime(2026, 7, 5, 6, 30), schedule["jobs"][0]),
            (dt.datetime(2026, 7, 5, 7, 55), schedule["jobs"][1]),
            (dt.datetime(2026, 7, 5, 8, 1), schedule["jobs"][2]),
        ]

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.PAUSE_FILE", Path(tmpdir) / "pause.json"), patch(
            "growatt_guard.state.COMMAND_LOCK_FILE", Path(tmpdir) / "mode_command.lock"
        ), patch(
            "growatt_guard.state.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch(
            "growatt_guard.state.TOPUP_STATE_FILE", Path(tmpdir) / "topup_active.json"
        ), patch(
            "growatt_guard.state.LOGIN_COOLDOWN_FILE", Path(tmpdir) / "growatt_login_cooldown.json"
        ), patch(
            "growatt_guard.health.DASHBOARD_FILE", Path(tmpdir) / "dashboard.html"
        ), patch("growatt_guard.health.validate_schedule", return_value=schedule), patch(
            "growatt_guard.health.validate_schedule_overrides", return_value={"dates": {}}
        ), patch(
            "growatt_guard.health.check_cron_schedule",
            return_value=[HealthCheckItem("Cron jobs", "OK", "3 scheduled jobs installed.")],
        ), patch("growatt_guard.health.next_scheduled_runs", return_value=next_runs), patch(
            "growatt_guard.health.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch(
            "growatt_guard.health.choose_preserve_threshold",
            return_value=ThresholdDecision(50, "weather disabled; using fixed threshold 50%"),
        ), patch("growatt_guard.health.read_pause_state", return_value=None), redirect_stdout(StringIO()) as stdout:
            (Path(tmpdir) / "dashboard.html").write_text("<html></html>", encoding="utf-8")
            exit_code = command_health_check(config)

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("Result: OK", output)
        self.assertIn("[OK] Next jobs:", output)
        self.assertIn("morning-preserve", output)
        self.assertIn("morning-return", output)
        self.assertIn("morning-watchdog", output)

    def test_health_check_pings_betterstack_heartbeat_when_configured(self):
        config = make_config(
            dry_run=False,
            discord_webhook_url="https://discord.com/api/webhooks/example",
            betterstack_heartbeat_url="https://uptime.betterstack.com/api/v1/heartbeat/test",
        )
        status = {"device": {"capacity": "50%"}, "storage_params": {"outputConfig": "0"}}
        schedule = {"timezone": "Africa/Lagos", "jobs": [{"cron": "30 6 * * *", "command": "preserve-battery"}]}

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.PAUSE_FILE", Path(tmpdir) / "pause.json"), patch(
            "growatt_guard.state.COMMAND_LOCK_FILE", Path(tmpdir) / "mode_command.lock"
        ), patch(
            "growatt_guard.state.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch(
            "growatt_guard.state.TOPUP_STATE_FILE", Path(tmpdir) / "topup_active.json"
        ), patch(
            "growatt_guard.state.LOGIN_COOLDOWN_FILE", Path(tmpdir) / "growatt_login_cooldown.json"
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
        ), patch("growatt_guard.health.read_pause_state", return_value=None), patch(
            "growatt_guard.health.requests.get"
        ) as heartbeat_mock, redirect_stdout(StringIO()):
            (Path(tmpdir) / "dashboard.html").write_text("<html></html>", encoding="utf-8")
            command_health_check(config)

        heartbeat_mock.assert_called_once_with(
            "https://uptime.betterstack.com/api/v1/heartbeat/test", timeout=10
        )

    def test_estimate_charge_rate_continues_on_utility_when_pcharge_is_zero(self):
        config = make_config(battery_capacity_wh=30_000)
        status1 = {
            "storage_params": {
                "storageBean": {"outputConfig": "2"},
                "storageDetailBean": {"bmsSoc": 53, "pCharge": 0, "statusText": "AC charge and Bypass"},
            }
        }
        status2 = {
            "storage_params": {
                "storageBean": {"outputConfig": "2"},
                "storageDetailBean": {"bmsSoc": 54, "pCharge": 0, "statusText": "AC charge and Bypass"},
            }
        }

        with patch("growatt_guard.modes.load_context", side_effect=[(None, None, status1), (None, None, status2)]), patch(
            "time.sleep"
        ) as sleep_mock, redirect_stdout(StringIO()) as stdout:
            result = command_estimate_charge_rate(config, wait_seconds=900)

        self.assertEqual(result, 0)
        sleep_mock.assert_called_once_with(900)
        output = stdout.getvalue()
        self.assertIn("continuing because output source is Utility first", output)
        self.assertIn("Estimated charge rate: 1200 W", output)

    def test_estimate_charge_rate_rejects_zero_pcharge_when_not_on_utility(self):
        config = make_config(battery_capacity_wh=30_000)
        status = {
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {"bmsSoc": 53, "pCharge": 0},
            }
        }

        with patch("growatt_guard.modes.load_context", return_value=(None, None, status)):
            with self.assertRaises(GrowattGuardError):
                command_estimate_charge_rate(config, wait_seconds=900)

    def test_battery_alert_sends_once_while_low(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")
        status = {"device": {"capacity": "29%"}, "storage_params": {"outputConfig": "0"}}

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
            "growatt_guard.state.BATTERY_ALERT_FILE", Path(tmpdir) / "battery_alert.json"
        ), patch(
            "growatt_guard.state.BYPASS_ALERT_FILE", Path(tmpdir) / "bypass_alert.json"
        ), patch(
            "growatt_guard.alerts.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch("growatt_guard.alerts.send_discord_embed", return_value=True) as send_mock, redirect_stdout(StringIO()):
            self.assertEqual(command_battery_alert(config), 0)
            self.assertEqual(command_battery_alert(config), 0)

        self.assertEqual(send_mock.call_count, 1)

    def test_battery_alert_does_not_call_utility_missing_above_cutoff(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example", battery_bms_cutoff_soc=25)
        status = {
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {"bmsSoc": 29, "pCharge": 0, "pDischarge": 700, "statusText": "Discharging"},
            }
        }

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.BATTERY_ALERT_FILE", Path(tmpdir) / "battery_alert.json"
        ), patch(
            "growatt_guard.state.BYPASS_ALERT_FILE", Path(tmpdir) / "bypass_alert.json"
        ), patch(
            "growatt_guard.alerts.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch("growatt_guard.alerts.send_discord_embed", return_value=True) as send_mock, redirect_stdout(StringIO()):
            self.assertEqual(command_battery_alert(config), 0)
            state = json.loads((Path(tmpdir) / "battery_alert.json").read_text(encoding="utf-8"))

        self.assertEqual(send_mock.call_count, 1)
        self.assertEqual(send_mock.call_args.args[1]["title"], "🔋 Emergency: low battery")
        self.assertNotIn("utility_unavailable", state)

    def test_battery_alert_calls_out_missing_utility_when_low(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")
        status = {
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {"bmsSoc": 24, "pCharge": 0, "pDischarge": 700, "statusText": "Discharging"},
            }
        }

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.BATTERY_ALERT_FILE", Path(tmpdir) / "battery_alert.json"
        ), patch(
            "growatt_guard.state.BYPASS_ALERT_FILE", Path(tmpdir) / "bypass_alert.json"
        ), patch(
            "growatt_guard.alerts.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch("growatt_guard.alerts.send_discord_embed", return_value=True) as send_mock, redirect_stdout(StringIO()) as stdout:
            self.assertEqual(command_battery_alert(config), 0)
            self.assertEqual(command_battery_alert(config), 0)
            state = json.loads((Path(tmpdir) / "battery_alert.json").read_text(encoding="utf-8"))

        self.assertEqual(send_mock.call_count, 1)
        embed = send_mock.call_args.args[1]
        self.assertEqual(embed["title"], "❌ Low battery and utility not detected")
        self.assertTrue(state["utility_unavailable"])
        self.assertIn("Utility/charging not detected", stdout.getvalue())

    def test_battery_alert_escalates_active_low_alert_when_utility_missing(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")
        status = {
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {"bmsSoc": 24, "pCharge": 0, "pDischarge": 700},
            }
        }

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.BATTERY_ALERT_FILE", Path(tmpdir) / "battery_alert.json"
        ), patch(
            "growatt_guard.state.BYPASS_ALERT_FILE", Path(tmpdir) / "bypass_alert.json"
        ), patch(
            "growatt_guard.alerts.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch("growatt_guard.alerts.send_discord_embed", return_value=True) as send_mock, redirect_stdout(StringIO()):
            (Path(tmpdir) / "battery_alert.json").write_text(
                json.dumps({"active": True, "last_soc": 29, "last_alert_at": "2026-07-10T12:00:00+00:00"}),
                encoding="utf-8",
            )

            self.assertEqual(command_battery_alert(config), 0)
            self.assertEqual(command_battery_alert(config), 0)

        self.assertEqual(send_mock.call_count, 1)
        self.assertEqual(send_mock.call_args.args[1]["title"], "❌ Low battery and utility not detected")

    def test_battery_alert_suppresses_bypass_during_owned_topup(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example", bypass_alert_soc=40)
        status = {
            "storage_params": {
                "storageBean": {"outputConfig": "2"},
                "storageDetailBean": {"bmsSoc": 55, "pCharge": 1200, "statusText": "AC charge and Bypass"},
            }
        }

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.BATTERY_ALERT_FILE", Path(tmpdir) / "battery_alert.json"
        ), patch(
            "growatt_guard.state.BYPASS_ALERT_FILE", Path(tmpdir) / "bypass_alert.json"
        ), patch(
            "growatt_guard.alerts.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch("growatt_guard.alerts.utility_hold_ownership", return_value="owned"), patch(
            "growatt_guard.alerts.send_discord_embed", return_value=True
        ) as send_mock, redirect_stdout(StringIO()) as stdout:
            self.assertEqual(command_battery_alert(config), 0)

        send_mock.assert_not_called()
        self.assertIn("Battery alert OK", stdout.getvalue())

    def test_battery_alert_sends_at_most_three_times_for_high_soc_bypass(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example", bypass_alert_soc=40)
        status = {
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {"bmsSoc": 55, "pCharge": 0, "statusText": "AC charge and Bypass"},
            }
        }

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.BATTERY_ALERT_FILE", Path(tmpdir) / "battery_alert.json"
        ), patch(
            "growatt_guard.state.BYPASS_ALERT_FILE", Path(tmpdir) / "bypass_alert.json"
        ), patch(
            "growatt_guard.alerts.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch("growatt_guard.alerts.send_discord_embed", return_value=True) as send_mock, redirect_stdout(StringIO()) as stdout:
            self.assertEqual(command_battery_alert(config), 0)
            self.assertEqual(command_battery_alert(config), 0)
            self.assertEqual(command_battery_alert(config), 0)
            self.assertEqual(command_battery_alert(config), 0)

        self.assertEqual(send_mock.call_count, 3)
        self.assertIn("Grid bypass alert sent (3/3)", stdout.getvalue())
        self.assertIn("suppressing further alerts", stdout.getvalue())

    def test_battery_alert_clears_bypass_alert_when_bypass_stops(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example", bypass_alert_soc=40)
        bypass_status = {
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {"bmsSoc": 55, "statusText": "AC charge and Bypass"},
            }
        }
        normal_status = {
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {"bmsSoc": 55, "statusText": "Discharging", "pCharge": 0, "pDischarge": 600},
            }
        }

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.BATTERY_ALERT_FILE", Path(tmpdir) / "battery_alert.json"
        ), patch(
            "growatt_guard.state.BYPASS_ALERT_FILE", Path(tmpdir) / "bypass_alert.json"
        ), patch(
            "growatt_guard.alerts.load_context",
            side_effect=[
                (None, DeviceRef("plant123", "SN123", "storage", {}), bypass_status),
                (None, DeviceRef("plant123", "SN123", "storage", {}), normal_status),
            ],
        ), patch("growatt_guard.alerts.send_discord_embed", return_value=True) as send_mock, redirect_stdout(StringIO()) as stdout:
            self.assertEqual(command_battery_alert(config), 0)
            self.assertEqual(command_battery_alert(config), 0)

        self.assertEqual(send_mock.call_count, 2)
        self.assertIn("Grid bypass alert cleared", stdout.getvalue())

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

    def test_weekly_summary_ignores_dry_run_topups(self):
        from growatt_power_guard import build_weekly_summary

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.audit.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"):
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            audit_path.write_text(
                "\n".join(
                    [
                        "timestamp,command,soc,threshold,weather_category,previous_mode,action,dry_run,result,note",
                        "2026-06-19T00:00:00,auto-topup-check,40,,,SBU priority [0],auto-topup-started,true,ok,80min test",
                        "2026-06-19T01:00:00,auto-topup-check,38,,,SBU priority [0],auto-topup-started,false,ok,20min test",
                    ]
                ),
                encoding="utf-8",
            )

            summary = build_weekly_summary(dt.datetime(2026, 6, 20, 12, 0), charge_rate_w=3000)

        self.assertIn("Audit rows: 1", summary)
        self.assertIn("Auto-topups: 1 (20 min grid charging", summary)
        self.assertNotIn("100 min", summary)

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
