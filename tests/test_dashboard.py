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
    command_observability_refresh,
    read_dashboard_stale_alert_state,
)
from growatt_guard.audit import build_chart_data
from growatt_guard.dashboard import (
    append_dashboard_metric_snapshot,
    build_dashboard_history_payload,
    build_dashboard_html,
    extract_dashboard_metrics,
    read_dashboard_metrics_history,
    _today_job_rows,
    _upcoming_override_rows,
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
        ), patch(
            "growatt_guard.dashboard.DASHBOARD_METRICS_FILE", Path(tmpdir) / "dashboard_metrics.jsonl"
        ), patch("growatt_guard.dashboard.read_mode_audit_rows", return_value=[]), redirect_stdout(StringIO()):
            output = Path(tmpdir) / "dashboard.html"
            self.assertEqual(command_dashboard(config, str(output)), 0)
            html = output.read_text(encoding="utf-8")

        self.assertIn("Growatt Dashboard", html)
        self.assertIn("Dashboard Health", html)
        self.assertIn("Live energy flow", html)
        self.assertIn("Energy Trends", html)
        self.assertIn("data-refresh-badge", html)
        self.assertIn("Cloud Streak", html)
        self.assertIn("50%", html)
        self.assertIn("SBU priority", html)

    def test_dashboard_topup_estimate_matches_auto_topup_floor(self):
        status = {
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {
                    "bmsSoc": 66,
                    "pDischarge": 2402,
                    "pCharge": 0,
                },
            }
        }
        schedule = {"timezone": "Africa/Lagos", "jobs": []}

        with patch(
            "growatt_guard.dashboard.read_discharge_rate_history",
            return_value=[{"rate_w": 1507}, {"rate_w": 1507}],
        ), patch("growatt_guard.dashboard.read_pause_state", return_value=None), patch(
            "growatt_guard.dashboard.read_battery_alert_state", return_value=None
        ), patch(
            "growatt_guard.dashboard.read_growatt_cloud_failure_state", return_value={}
        ), patch(
            "growatt_guard.dashboard.read_mode_audit_rows", return_value=[]
        ), patch(
            "growatt_guard.dashboard.read_pvoutput_state", return_value=None
        ), patch(
            "growatt_guard.dashboard.build_chart_data",
            return_value={"labels": [], "preserve_checks": [], "utility_switches": [], "watchdog_repairs": []},
        ):
            html = build_dashboard_html(
                status,
                schedule,
                {"dates": {}},
                ThresholdDecision(50, "fixed threshold"),
                battery_capacity_wh=30000,
                battery_bms_cutoff_soc=25,
                hours_to_sunrise=8,
                battery_charge_rate_w=2400,
                auto_topup_solar_skip_min_margin_minutes=60,
                auto_topup_min_minutes=20,
            )

        self.assertIn("Topup to Sunrise", html)
        self.assertIn("20min", html)

    def test_dashboard_metrics_extracts_live_energy_values(self):
        now = dt.datetime(2026, 6, 25, 8, 30)
        status = {
            "device": {"capacity": "47%"},
            "storage_params": {
                "storageBean": {
                    "outputConfig": "0",
                    "ppv": 906,
                    "epvToday": 1.2,
                    "outPutPower": 1145,
                    "eLoadToday": 11.8,
                },
                "storageDetailBean": {
                    "bmsSoc": 47,
                    "pDischarge": 374,
                    "pCharge": 0,
                    "vBat": 52.1,
                    "statusText": "Discharging",
                },
            },
        }

        metrics = extract_dashboard_metrics(status, now=now)

        self.assertEqual(metrics["soc"], 47)
        self.assertEqual(metrics["pv_w"], 906)
        self.assertEqual(metrics["pv_today_kwh"], 1.2)
        self.assertEqual(metrics["load_w"], 1145)
        self.assertEqual(metrics["discharge_w"], 374)
        self.assertEqual(metrics["battery_net_w"], 374)

    def test_dashboard_metrics_sums_pv_channels_when_total_is_lower(self):
        now = dt.datetime(2026, 6, 25, 8, 30)
        status = {
            "device": {"capacity": "47%"},
            "storage_params": {
                "storageBean": {
                    "outputConfig": "0",
                    "ppv": 337,
                    "pPv1": 337,
                    "pPv2": 692,
                    "epv1Today": 0.4,
                    "epv2Today": 0.8,
                    "epvTotal": 2.9,
                },
                "storageDetailBean": {"bmsSoc": 47},
            },
        }

        metrics = extract_dashboard_metrics(status, now=now)

        self.assertEqual(metrics["pv_w"], 1029)
        self.assertEqual(metrics["pv_today_kwh"], 1.2)
        self.assertEqual(metrics["pv_total"], "2.9 MWh")

    def test_dashboard_metrics_history_roundtrip_and_payload(self):
        status = {
            "device": {"capacity": "50%"},
            "storage_params": {
                "storageBean": {"outputConfig": "0", "ppv": 500, "outPutPower": 800, "epvToday": 2.5},
                "storageDetailBean": {"bmsSoc": 50, "pDischarge": 300, "pCharge": 0},
            },
        }

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.dashboard.DASHBOARD_METRICS_FILE", Path(tmpdir) / "dashboard_metrics.jsonl"
        ):
            append_dashboard_metric_snapshot(status, now=dt.datetime(2026, 6, 25, 8, 0))
            append_dashboard_metric_snapshot(status, now=dt.datetime(2026, 6, 25, 8, 10))
            history = read_dashboard_metrics_history(now=dt.datetime(2026, 6, 25, 8, 15))
            payload = build_dashboard_history_payload(history, now=dt.datetime(2026, 6, 25, 8, 15))

        self.assertEqual(len(history), 2)
        self.assertEqual(payload["power"]["pv_w"], [500.0, 500.0])
        self.assertEqual(payload["soc"]["soc"], [50.0, 50.0])
        self.assertEqual(payload["daily"]["pv_kwh"][-1], 2.5)

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

    def test_observability_refresh_once_reuses_status_for_dashboard_and_pvoutput(self):
        config = make_config(pvoutput_enabled=True, dry_run=True)
        status = {
            "device": {"capacity": "60%"},
            "storage_params": {"outputConfig": "0", "storageBean": {"ppv": 1200.0, "epvToday": 3.5}},
        }
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [{"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"}],
        }

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.dashboard.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ) as load_mock, patch("growatt_guard.dashboard.validate_schedule", return_value=schedule), patch(
            "growatt_guard.dashboard.validate_schedule_overrides", return_value={"dates": {}}
        ), patch(
            "growatt_guard.dashboard.choose_preserve_threshold",
            return_value=ThresholdDecision(50, "fixed threshold"),
        ), patch(
            "growatt_guard.dashboard.DASHBOARD_METRICS_FILE", Path(tmpdir) / "dashboard_metrics.jsonl"
        ), patch(
            "growatt_guard.dashboard.publish_pvoutput_status_from_status",
            return_value=(True, "PVOutput OK: v2=1200"),
        ) as pvoutput_mock, patch(
            "growatt_guard.dashboard.read_mode_audit_rows", return_value=[]
        ), redirect_stdout(StringIO()) as stdout:
            output = Path(tmpdir) / "dashboard.html"
            result = command_observability_refresh(config, str(output), 1, loop=False)
            output_exists = output.exists()

        self.assertEqual(result, 0)
        load_mock.assert_called_once_with(config)
        pvoutput_mock.assert_called_once_with(config, status)
        self.assertTrue(output_exists)
        self.assertIn("Observability refreshed", stdout.getvalue())
        self.assertIn("PVOutput OK", stdout.getvalue())

    def test_observability_refresh_rejects_too_fast_loop(self):
        config = make_config()

        with self.assertRaises(GrowattGuardError):
            command_observability_refresh(config, "dashboard.html", 1, loop=True)

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
        ), patch(
            "growatt_guard.dashboard.DASHBOARD_METRICS_FILE", Path(tmpdir) / "dashboard_metrics.jsonl"
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


class ChartDataTests(unittest.TestCase):
    def test_chart_data_has_correct_keys(self):
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.audit.MODE_AUDIT_FILE", Path(tmpdir) / "m.csv"):
            data = build_chart_data(dt.datetime(2026, 6, 20, 12, 0), days=7)
        self.assertIn("labels", data)
        self.assertIn("preserve_checks", data)
        self.assertIn("utility_switches", data)
        self.assertIn("watchdog_repairs", data)
        self.assertEqual(len(data["labels"]), 7)

    def test_chart_data_counts_correct_events(self):
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "m.csv"
            audit_path.write_text(
                "\n".join([
                    "timestamp,command,soc,threshold,weather_category,previous_mode,action,dry_run,result,note",
                    "2026-06-19T06:30:00,preserve-battery,47,50,normal,SBU priority [0],switch-to-utility,false,ok,",
                    "2026-06-19T06:30:00,preserve-battery,55,50,normal,SBU priority [0],no-change,false,skipped,",
                    "2026-06-20T08:00:00,watchdog-sbu,54,,normal,Utility first [2],repair-sbu,false,ok,",
                ]),
                encoding="utf-8",
            )
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                data = build_chart_data(dt.datetime(2026, 6, 20, 12, 0), days=7)

        labels = data["labels"]
        label_19 = dt.date(2026, 6, 19).strftime("%a %m-%d")
        label_20 = dt.date(2026, 6, 20).strftime("%a %m-%d")
        idx_19 = labels.index(label_19)
        idx_20 = labels.index(label_20)
        self.assertEqual(data["preserve_checks"][idx_19], 2)
        self.assertEqual(data["utility_switches"][idx_19], 1)
        self.assertEqual(data["watchdog_repairs"][idx_20], 1)


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
