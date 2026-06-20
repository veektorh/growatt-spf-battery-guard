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
    build_daily_summary,
    extract_soc,
    extract_spf_output_source,
    render_params,
    set_mode,
    build_parser,
    check_cron_schedule,
    command_health_check,
    command_watchdog_sbu,
    ensure_not_paused,
    format_health_report,
    read_pause_state,
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

    def test_rotate_logs_command_is_available(self):
        args = build_parser().parse_args(["rotate-logs"])

        self.assertEqual(args.command, "rotate-logs")

    def test_validate_schedule_command_is_available(self):
        args = build_parser().parse_args(["validate-schedule"])

        self.assertEqual(args.command, "validate-schedule")

    def test_weather_threshold_command_is_available(self):
        args = build_parser().parse_args(["weather-threshold"])

        self.assertEqual(args.command, "weather-threshold")

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

    def test_watchdog_sbu_does_nothing_when_already_sbu(self):
        config = make_config()

        with patch(
            "growatt_power_guard.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), {"storage_params": {"outputConfig": "0"}}),
        ), patch("growatt_power_guard.set_mode") as set_mode_mock, redirect_stdout(StringIO()):
            self.assertEqual(command_watchdog_sbu(config), 0)

        set_mode_mock.assert_not_called()

    def test_watchdog_sbu_retries_when_not_sbu(self):
        config = make_config()

        with patch(
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
                '{"timezone":"Africa/Lagos","jobs":[{"cron":"0 1 * * *","command":"bad-command"}]}',
                encoding="utf-8",
            )

            with self.assertRaises(GrowattGuardError):
                validate_schedule(path)

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
                {"cron": "30 6 * * *", "command": "preserve-battery"},
                {"cron": "55 7 * * *", "command": "return-sbu"},
            ],
        }
        crontab = "\n".join(
            [
                "CRON_TZ=Africa/Lagos",
                (
                    "30 6 * * * cd /home/ubuntu/automation && .venv/bin/python "
                    "growatt_power_guard.py preserve-battery >> logs/cron.log 2>&1 # growatt-power-guard"
                ),
                (
                    "55 7 * * * cd /home/ubuntu/automation && .venv/bin/python "
                    "growatt_power_guard.py return-sbu >> logs/cron.log 2>&1 # growatt-power-guard"
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
        config = make_config(dry_run=False)
        status = {"device": {"capacity": "50%"}, "storage_params": {"outputConfig": "0"}}
        schedule = {"timezone": "Africa/Lagos", "jobs": [{"cron": "30 6 * * *", "command": "preserve-battery"}]}

        with TemporaryDirectory() as tmpdir, patch("growatt_power_guard.PAUSE_FILE", Path(tmpdir) / "pause.json"), patch(
            "growatt_power_guard.validate_schedule", return_value=schedule
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
