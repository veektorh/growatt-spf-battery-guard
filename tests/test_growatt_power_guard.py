import datetime as dt
import subprocess
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from growatt_power_guard import (
    Config,
    DeviceRef,
    HealthCheckItem,
    ThresholdDecision,
    append_mode_audit,
    build_daily_summary,
    build_weekly_summary,
    acquire_command_lock,
    extract_soc,
    extract_spf_output_source,
    render_params,
    set_mode,
    build_parser,
    check_cron_schedule,
    command_battery_alert,
    command_dashboard,
    command_dashboard_refresh,
    command_health_check,
    command_run_scheduled,
    command_watchdog_sbu,
    ensure_not_paused,
    format_health_report,
    next_scheduled_runs,
    notify_failure,
    record_growatt_cloud_success,
    read_growatt_cloud_failure_state,
    read_pause_state,
    release_command_lock,
    run_with_command_lock,
    validate_schedule_overrides,
    validate_schedule,
    GrowattGuardError,
    write_pause_state,
    send_discord_message,
    truncate_discord_message,
    analyze_weather_window,
    choose_preserve_threshold,
)


def make_config(**overrides):
    values = {
        "username": "u",
        "password": "p",
        "server_url": "https://openapi.growatt.com/",
        "plant_id": "plant123",
        "device_sn": "SN123",
        "low_battery_soc": 50,
        "dry_run": True,
        "mode_driver": "spf5000",
        "set_mode_path": "tcpSet.do",
        "set_mode_method": "post",
        "utility_mode_params": "",
        "sbu_mode_params": "",
        "discord_webhook_url": "",
        "discord_notify_success": True,
        "discord_notify_skip": False,
        "discord_notify_failure": True,
        "log_retention_days": 30,
        "emergency_soc": 30,
        "emergency_soc_recovery": 35,
        "cloud_failure_alert_threshold": 3,
        "weather_enabled": False,
        "weather_lat": None,
        "weather_lon": None,
        "weather_timezone": "Africa/Lagos",
        "weather_lookahead_hours": 4,
        "weather_cloudy_threshold": 70,
        "weather_sunny_threshold": 35,
        "weather_rain_threshold_mm": 1,
        "low_battery_soc_normal": 45,
        "low_battery_soc_sunny": 40,
    }
    values.update(overrides)
    return Config(**values)


class GrowattPowerGuardTests(unittest.TestCase):
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

    def test_preserve_battery_command_is_available(self):
        args = build_parser().parse_args(["preserve-battery"])

        self.assertEqual(args.command, "preserve-battery")

    def test_test_discord_command_is_available(self):
        args = build_parser().parse_args(["test-discord"])

        self.assertEqual(args.command, "test-discord")

    def test_watchdog_sbu_command_is_available(self):
        args = build_parser().parse_args(["watchdog-sbu"])

        self.assertEqual(args.command, "watchdog-sbu")

    def test_daily_summary_command_is_available(self):
        args = build_parser().parse_args(["daily-summary"])

        self.assertEqual(args.command, "daily-summary")

    def test_weekly_summary_command_is_available(self):
        args = build_parser().parse_args(["weekly-summary"])

        self.assertEqual(args.command, "weekly-summary")

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

    def test_serve_dashboard_command_is_available(self):
        args = build_parser().parse_args(["serve-dashboard", "--host", "127.0.0.1", "--port", "8080"])

        self.assertEqual(args.command, "serve-dashboard")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8080)

    def test_truncate_discord_message_keeps_short_messages(self):
        self.assertEqual(truncate_discord_message("hello"), "hello")

    def test_truncate_discord_message_limits_long_messages(self):
        self.assertLessEqual(len(truncate_discord_message("x" * 2500)), 1904)

    def test_send_discord_message_posts_json_payload(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")

        class Response:
            status_code = 204
            text = ""

        with patch("growatt_power_guard.requests.post", return_value=Response()) as mocked:
            self.assertTrue(send_discord_message(config, "hello"))

        self.assertEqual(mocked.call_args.args[0], "https://discord.com/api/webhooks/example")
        self.assertEqual(mocked.call_args.kwargs["json"]["content"], "hello")
        self.assertIn("User-Agent", mocked.call_args.kwargs["headers"])

    def test_growatt_cloud_failures_alert_only_after_threshold(self):
        config = make_config(
            discord_webhook_url="https://discord.com/api/webhooks/example",
            cloud_failure_alert_threshold=3,
        )

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_power_guard.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch("growatt_power_guard.send_discord_message", return_value=True) as send_mock:
            notify_failure(config, "status", "Growatt login failed: temporary cloud error")
            notify_failure(config, "status", "Growatt login failed: temporary cloud error")
            self.assertEqual(send_mock.call_count, 0)

            notify_failure(config, "status", "Growatt login failed: temporary cloud error")
            state = read_growatt_cloud_failure_state()

        self.assertEqual(send_mock.call_count, 1)
        self.assertIsNotNone(state)
        self.assertEqual(state["count"], 3)
        self.assertTrue(state["alerted"])

    def test_growatt_cloud_success_clears_alerted_streak(self):
        config = make_config(
            discord_webhook_url="https://discord.com/api/webhooks/example",
            cloud_failure_alert_threshold=2,
        )

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_power_guard.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch("growatt_power_guard.send_discord_message", return_value=True) as send_mock:
            notify_failure(config, "status", "Growatt login failed: temporary cloud error")
            notify_failure(config, "status", "Growatt login failed: temporary cloud error")
            record_growatt_cloud_success(config)
            state = read_growatt_cloud_failure_state()

        self.assertIsNone(state)
        self.assertEqual(send_mock.call_count, 2)

    def test_non_cloud_failure_still_alerts_immediately(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_power_guard.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch("growatt_power_guard.send_discord_message", return_value=True) as send_mock:
            notify_failure(config, "validate-schedule", "schedule.json is invalid")

        self.assertEqual(send_mock.call_count, 1)

    def test_watchdog_sbu_does_nothing_when_already_sbu(self):
        config = make_config()

        with TemporaryDirectory() as tmpdir, patch("growatt_power_guard.LOG_DIR", Path(tmpdir)), patch(
            "growatt_power_guard.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"
        ), patch(
            "growatt_power_guard.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), {"storage_params": {"outputConfig": "0"}}),
        ), patch("growatt_power_guard.set_mode") as set_mode_mock, redirect_stdout(StringIO()):
            self.assertEqual(command_watchdog_sbu(config), 0)

        set_mode_mock.assert_not_called()

    def test_watchdog_sbu_retries_when_not_sbu(self):
        config = make_config()

        with TemporaryDirectory() as tmpdir, patch("growatt_power_guard.LOG_DIR", Path(tmpdir)), patch(
            "growatt_power_guard.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"
        ), patch(
            "growatt_power_guard.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), {"storage_params": {"outputConfig": "2"}}),
        ), patch("growatt_power_guard.set_mode", return_value={"success": True}) as set_mode_mock, patch(
            "growatt_power_guard.logging.warning"
        ), redirect_stdout(StringIO()):
            self.assertEqual(command_watchdog_sbu(config), 0)

        set_mode_mock.assert_called_once()

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

        with patch("growatt_power_guard.summarize_today_log_counts", return_value={
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

        with patch("growatt_power_guard.validate_schedule", return_value=schedule), patch(
            "growatt_power_guard.validate_schedule_overrides", return_value=overrides
        ), patch("growatt_power_guard.dispatch_command") as dispatch_mock, redirect_stdout(StringIO()) as stdout:
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

        with patch("growatt_power_guard.validate_schedule", return_value=schedule), patch(
            "growatt_power_guard.validate_schedule_overrides", return_value=overrides
        ), patch("growatt_power_guard.dispatch_command", return_value=0) as dispatch_mock:
            self.assertEqual(command_run_scheduled(config, "morning-preserve"), 0)

        dispatched_args = dispatch_mock.call_args.args[1]
        self.assertEqual(dispatched_args.command, "health-check")
        self.assertTrue(dispatched_args.notify)

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

        with patch("growatt_power_guard.os.name", "posix"), patch(
            "growatt_power_guard.subprocess.run",
            return_value=subprocess.CompletedProcess(["crontab", "-l"], 0, stdout=crontab, stderr=""),
        ):
            checks = check_cron_schedule(schedule)

        self.assertTrue(all(check.status == "OK" for check in checks))

    def test_command_health_check_reports_ok_when_everything_is_available(self):
        config = make_config(dry_run=False, discord_webhook_url="https://discord.com/api/webhooks/example")
        status = {"device": {"capacity": "50%"}, "storage_params": {"outputConfig": "0"}}
        schedule = {"timezone": "Africa/Lagos", "jobs": [{"cron": "30 6 * * *", "command": "preserve-battery"}]}

        with TemporaryDirectory() as tmpdir, patch("growatt_power_guard.PAUSE_FILE", Path(tmpdir) / "pause.json"), patch(
            "growatt_power_guard.COMMAND_LOCK_FILE", Path(tmpdir) / "mode_command.lock"
        ), patch(
            "growatt_power_guard.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch("growatt_power_guard.validate_schedule", return_value=schedule), patch(
            "growatt_power_guard.validate_schedule_overrides", return_value={"dates": {}}
        ), patch(
            "growatt_power_guard.check_cron_schedule",
            return_value=[HealthCheckItem("Cron jobs", "OK", "1 scheduled job installed.")],
        ), patch(
            "growatt_power_guard.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch(
            "growatt_power_guard.choose_preserve_threshold",
            return_value=ThresholdDecision(50, "weather disabled; using fixed threshold 50%"),
        ), patch("growatt_power_guard.read_pause_state", return_value=None), redirect_stdout(StringIO()) as stdout:
            exit_code = command_health_check(config)

        self.assertEqual(exit_code, 0)
        self.assertIn("Result: OK", stdout.getvalue())

    def test_battery_alert_sends_once_while_low(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")
        status = {"device": {"capacity": "29%"}, "storage_params": {"outputConfig": "0"}}

        with TemporaryDirectory() as tmpdir, patch("growatt_power_guard.STATE_DIR", Path(tmpdir)), patch(
            "growatt_power_guard.BATTERY_ALERT_FILE", Path(tmpdir) / "battery_alert.json"
        ), patch(
            "growatt_power_guard.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch("growatt_power_guard.send_discord_message", return_value=True) as send_mock, redirect_stdout(StringIO()):
            self.assertEqual(command_battery_alert(config), 0)
            self.assertEqual(command_battery_alert(config), 0)

        self.assertEqual(send_mock.call_count, 1)

    def test_append_mode_audit_writes_csv_row(self):
        config = make_config(dry_run=False)

        with TemporaryDirectory() as tmpdir, patch("growatt_power_guard.LOG_DIR", Path(tmpdir)), patch(
            "growatt_power_guard.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"
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

    def test_weekly_summary_uses_audit_rows(self):
        with TemporaryDirectory() as tmpdir, patch("growatt_power_guard.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"):
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

    def test_dashboard_writes_html(self):
        config = make_config()
        status = {"device": {"capacity": "50%"}, "storage_params": {"outputConfig": "0"}}
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [{"id": "morning-preserve", "name": "Preserve", "cron": "30 6 * * *", "command": "preserve-battery"}],
        }

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_power_guard.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch("growatt_power_guard.validate_schedule", return_value=schedule), patch(
            "growatt_power_guard.validate_schedule_overrides", return_value={"dates": {}}
        ), patch(
            "growatt_power_guard.choose_preserve_threshold",
            return_value=ThresholdDecision(50, "weather disabled; using fixed threshold 50%"),
        ), patch("growatt_power_guard.read_mode_audit_rows", return_value=[]), redirect_stdout(StringIO()):
            output = Path(tmpdir) / "dashboard.html"
            self.assertEqual(command_dashboard(config, str(output)), 0)
            html = output.read_text(encoding="utf-8")

        self.assertIn("Growatt Dashboard", html)
        self.assertIn("50%", html)
        self.assertIn("SBU priority", html)

    def test_dashboard_refresh_once_writes_and_exits(self):
        config = make_config()

        with patch("growatt_power_guard.write_dashboard", return_value=Path("dashboard.html")) as write_mock, redirect_stdout(
            StringIO()
        ) as stdout:
            self.assertEqual(command_dashboard_refresh(config, "dashboard.html", 1, once=True), 0)

        write_mock.assert_called_once_with(config, "dashboard.html")
        self.assertIn("Dashboard refreshed", stdout.getvalue())

    def test_dashboard_refresh_rejects_too_fast_loop(self):
        config = make_config()

        with self.assertRaises(GrowattGuardError):
            command_dashboard_refresh(config, "dashboard.html", 1, once=False)

    def test_command_lock_skips_when_busy(self):
        config = make_config(discord_notify_skip=False)
        with TemporaryDirectory() as tmpdir, patch("growatt_power_guard.STATE_DIR", Path(tmpdir)), patch(
            "growatt_power_guard.COMMAND_LOCK_FILE", Path(tmpdir) / "mode_command.lock"
        ), redirect_stdout(StringIO()) as stdout:
            token = acquire_command_lock("preserve-battery")
            self.assertIsNotNone(token)
            self.assertEqual(run_with_command_lock(config, "return-sbu", lambda: 99), 0)
            release_command_lock(token or "")

        self.assertIn("already running", stdout.getvalue())

    def test_choose_preserve_threshold_uses_fixed_when_weather_disabled(self):
        decision = choose_preserve_threshold(make_config(weather_enabled=False, low_battery_soc=50))

        self.assertEqual(decision.threshold, 50)
        self.assertEqual(decision.weather_category, "disabled")

    def test_analyze_weather_window_keeps_rainy_threshold_at_50(self):
        now = dt.datetime.now().replace(minute=0, second=0, microsecond=0)
        forecast = {
            "hourly": {
                "time": [(now + dt.timedelta(hours=i)).isoformat(timespec="minutes") for i in range(4)],
                "cloud_cover": [80, 75, 70, 60],
                "precipitation": [0, 0.2, 0.9, 0],
            }
        }

        decision = analyze_weather_window(make_config(), forecast)

        self.assertEqual(decision.threshold, 50)
        self.assertEqual(decision.weather_category, "rainy/cloudy")

    def test_analyze_weather_window_uses_normal_threshold(self):
        now = dt.datetime.now().replace(minute=0, second=0, microsecond=0)
        forecast = {
            "hourly": {
                "time": [(now + dt.timedelta(hours=i)).isoformat(timespec="minutes") for i in range(4)],
                "cloud_cover": [50, 55, 45, 40],
                "precipitation": [0, 0, 0, 0],
            }
        }

        decision = analyze_weather_window(make_config(), forecast)

        self.assertEqual(decision.threshold, 45)
        self.assertEqual(decision.weather_category, "normal")

    def test_analyze_weather_window_uses_sunny_threshold(self):
        now = dt.datetime.now().replace(minute=0, second=0, microsecond=0)
        forecast = {
            "hourly": {
                "time": [(now + dt.timedelta(hours=i)).isoformat(timespec="minutes") for i in range(4)],
                "cloud_cover": [10, 15, 20, 30],
                "precipitation": [0, 0, 0, 0],
            }
        }

        decision = analyze_weather_window(make_config(), forecast)

        self.assertEqual(decision.threshold, 40)
        self.assertEqual(decision.weather_category, "sunny")

    def test_write_and_read_pause_state(self):
        with TemporaryDirectory() as tmpdir, patch("growatt_power_guard.STATE_DIR", Path(tmpdir)), patch(
            "growatt_power_guard.PAUSE_FILE", Path(tmpdir) / "automation_pause.json"
        ):
            state = write_pause_state(1, "testing")
            read_back = read_pause_state()

        self.assertEqual(state["reason"], "testing")
        self.assertIsNotNone(read_back)
        self.assertEqual(read_back["reason"], "testing")

    def test_ensure_not_paused_returns_true_when_paused(self):
        config = make_config(discord_notify_skip=False)
        with TemporaryDirectory() as tmpdir, patch("growatt_power_guard.STATE_DIR", Path(tmpdir)), patch(
            "growatt_power_guard.PAUSE_FILE", Path(tmpdir) / "automation_pause.json"
        ), redirect_stdout(StringIO()):
            write_pause_state(1, "testing")
            self.assertTrue(ensure_not_paused(config, "watchdog-sbu"))


if __name__ == "__main__":
    unittest.main()
