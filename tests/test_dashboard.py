import datetime as dt
import json
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
    build_dashboard_daily_insights,
    build_dashboard_daily_mix,
    build_dashboard_data_payload,
    build_dashboard_data_quality,
    build_dashboard_energy_reconciliation,
    build_dashboard_history_payload,
    build_dashboard_html,
    build_dashboard_next_action,
    build_tonight_risk,
    build_dashboard_schedule_timeline,
    extract_dashboard_metric_sources,
    extract_dashboard_metrics,
    read_dashboard_metrics_history,
    _today_job_rows,
    _upcoming_override_rows,
)
from growatt_guard.dashboard_service import dashboard_asset_for_path



class DashboardTests(unittest.TestCase):
    def test_dashboard_writes_html(self):
        config = make_config()
        status = {
            "device": {"capacity": "50%"},
            "storage_params": {
                "outputConfig": "0",
                "storageBean": {
                    "pGrid": 0,
                    "eToUserToday": 0,
                    "epvTotal": 0,
                    "eChargeToday": 0,
                    "useEnergyToday": 0,
                },
            },
            "storage_energy_overview": {
                "eToUserToday": "13.7",
                "epvTotal": "2864.1",
                "eChargeToday": "10.5",
                "useEnergyToday": "12.5",
            },
        }
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [{"id": "morning-preserve", "name": "Preserve", "cron": "30 6 * * *", "command": "preserve-battery"}],
        }

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.dashboard_service.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch("growatt_guard.dashboard_service.validate_schedule", return_value=schedule), patch(
            "growatt_guard.dashboard_service.validate_schedule_overrides", return_value={"dates": {}}
        ), patch(
            "growatt_guard.dashboard_service.choose_preserve_threshold",
            return_value=ThresholdDecision(50, "weather disabled; using fixed threshold 50%"),
        ), patch(
            "growatt_guard.state.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch(
            "growatt_guard.dashboard_metrics.DASHBOARD_METRICS_FILE", Path(tmpdir) / "dashboard_metrics.jsonl"
        ), patch("growatt_guard.dashboard.read_mode_audit_rows", return_value=[]), redirect_stdout(StringIO()):
            output = Path(tmpdir) / "dashboard.html"
            self.assertEqual(command_dashboard(config, str(output)), 0)
            html = output.read_text(encoding="utf-8")
            self.assertIn("function toggleDashTheme()", html)
            self.assertIn("drawLineChart(\"power-trend-chart\"", html)
            self.assertNotIn("'''", html)
            dashboard_json = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
            html_asset = dashboard_asset_for_path(output, "/dashboard.html?cache=1")
            json_asset = dashboard_asset_for_path(output, "/dashboard.json")

        self.assertIn("Growatt Dashboard", html)
        self.assertIn('rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials"', html)
        self.assertIn('rel="icon" href="/dashboard-icon.svg" type="image/svg+xml"', html)
        self.assertIn('rel="apple-touch-icon" href="/dashboard-icon-180.png"', html)
        self.assertIn('<meta name="theme-color" content="#0f1318">', html)
        self.assertIn("Solar Inverter", html)
        self.assertIn("app-shell", html)
        self.assertIn("sidebar-nav", html)
        self.assertIn("flow-stage", html)
        self.assertIn("Dashboard Health", html)
        self.assertIn("Tonight Risk", html)
        self.assertIn("Live energy flow", html)
        self.assertIn("Grid Import Now", html)
        self.assertIn("Grid Import Today", html)
        self.assertIn("Grid Bypass", html)
        self.assertIn("Bypass: Clear", html)
        self.assertIn("Daily Energy", html)
        self.assertIn("Today Mix", html)
        self.assertIn('class="mix-grid"', html)
        self.assertIn('class="mix-grid-source"', html)
        self.assertNotIn(".mix-grid { background:", html)
        self.assertIn("Where energy came from", html)
        self.assertIn("Key details at a glance", html)
        self.assertIn("glance-grid", html)
        self.assertIn("glance-battery", html)
        self.assertIn("glance-solar", html)
        self.assertIn("Live load cover", html)
        self.assertIn("glance-utility", html)
        self.assertIn("glance-risk", html)
        self.assertIn("Battery", html)
        self.assertIn("Solar", html)
        self.assertIn("Utility", html)
        self.assertIn("layout-toggle-btn", html)
        self.assertIn("New design", html)
        self.assertIn("dashboard-night", html)
        self.assertIn("night-console", html)
        self.assertIn("design-dashboard", html)
        self.assertIn("design-primary-grid", html)
        self.assertIn("7-day solar outlook", html)
        self.assertIn("Automation &amp; operations", html)
        self.assertIn("localStorage.getItem('dash-view') === 'operations' ? 'current' : 'night'", html)
        self.assertNotIn("night-header", html)
        self.assertEqual(html.count("<h1>"), 2)
        self.assertEqual(html.count('<div class="hero-kicker">'), 1)
        self.assertIn("Solar Detail", html)
        self.assertIn("PV Lifetime", html)
        self.assertIn("7-day battery", html)
        self.assertIn("Today mix", html)
        self.assertIn("setDashLayout", html)
        self.assertIn("Reserve Details", html)
        self.assertIn("Supporting values behind the first-glance battery", html)
        self.assertIn("Battery:", html)
        self.assertIn("Floor:", html)
        self.assertIn("Battery Reserve", html)
        self.assertIn("Estimate basis", html)
        self.assertIn("Top-up needed", html)
        self.assertIn("Reserve target", html)
        self.assertIn("Charge rate", html)
        self.assertIn("Tomorrow PV", html)
        self.assertIn("Weather context", html)
        self.assertIn("Energy Outlook", html)
        self.assertIn("SBU Return Guard", html)
        self.assertIn("Estimate basis:", html)
        self.assertIn("Top-up duration:", html)
        self.assertIn("flow-chain", html)
        self.assertIn("flow-main-row", html)
        self.assertIn("flow-support-row", html)
        self.assertNotIn("Home Status", html)
        self.assertNotIn("hero-next", html)
        self.assertNotIn("hero-subtitle", html)
        self.assertIn("Ranked assistant suggestions", html)
        self.assertIn("Live energy flow", html)
        self.assertIn("Tonight Planner", html)
        self.assertIn("Metric source paths", html)
        self.assertIn("Projected Sunrise SOC", html)
        self.assertIn("Data Quality", html)
        self.assertNotIn("Energy Balance", html)
        self.assertIn("Next Automation", html)
        self.assertIn("Energy Insights", html)
        self.assertIn("System & Automation", html)
        self.assertIn("Today Automation", html)
        self.assertIn("timeline-list", html)
        self.assertIn("Current and upcoming jobs", html)
        self.assertIn("System Status", html)
        self.assertIn("Recent Activity", html)
        self.assertIn("status-activity-grid", html)
        self.assertIn("No recent mode decisions recorded.", html)
        self.assertIn("Operations Details", html)
        self.assertIn("detail-panel", html)
        self.assertIn("13.7 kWh", html)
        self.assertIn("2.86 MWh", html)
        self.assertIn("10.5 kWh", html)
        self.assertIn("12.5 kWh", html)
        self.assertNotIn('<div class="label">PV Power</div>', html)
        self.assertNotIn('<div class="label">Load Power</div>', html)
        self.assertNotIn('<div class="label">Output Source</div>', html)
        self.assertNotIn('<div class="label">Output Power</div>', html)
        self.assertIn("Energy Trends", html)
        self.assertIn("Automation History", html)
        self.assertLess(html.index('<h2>Daily Energy</h2>'), html.index('<h2 id="trends">Energy Trends</h2>'))
        self.assertLess(html.index('<h2 id="trends">Energy Trends</h2>'), html.index('<h2 id="planner">Tonight Planner</h2>'))
        self.assertIn("drawHistoryTip", html)
        self.assertIn('setupBarTooltip("battery-energy-chart"', html)
        self.assertIn('setupBarTooltip("supply-energy-chart"', html)
        self.assertIn("data-refresh-badge", html)
        self.assertIn("Cloud Streak", html)
        self.assertIn("50%", html)
        self.assertIn("SBU priority", html)
        self.assertEqual(dashboard_json["schema_version"], 1)
        self.assertEqual(
            dashboard_json["freshness"]["last_successful_growatt_read_at"],
            dashboard_json["generated_at"],
        )
        self.assertIn("last_successful_pvoutput_upload_at", dashboard_json["freshness"])
        self.assertEqual(dashboard_json["live"]["grid_today_kwh"], 13.7)
        self.assertEqual(dashboard_json["sources"]["load_today_kwh"], "storage_energy_overview.useEnergyToday")
        self.assertEqual(dashboard_json["quality"]["data"]["level"], "poor")
        self.assertNotIn("energy_balance", dashboard_json["quality"])
        self.assertIn("daily", dashboard_json["insights"])
        self.assertEqual(dashboard_json["insights"]["daily_mix"]["battery_net_title"], "Battery net unknown")
        self.assertEqual(dashboard_json["insights"]["daily_mix"]["supply_total_kwh"], 13.7)
        self.assertEqual(dashboard_json["schedule"]["next_action"]["job_id"], "morning-preserve")
        self.assertEqual(dashboard_json["schedule"]["timeline"][0]["job_id"], "morning-preserve")
        self.assertIn("tonight_risk", dashboard_json["planner"])
        self.assertIn("outlook", dashboard_json["planner"])
        self.assertIn("sunrise_basis", dashboard_json["planner"]["outlook"])
        self.assertIn("sunrise_note", dashboard_json["planner"]["outlook"])
        self.assertIn("topup_minutes", dashboard_json["planner"]["outlook"])
        self.assertIn("assistant", dashboard_json)
        self.assertIn("now_label", dashboard_json["assistant"]["status"])
        self.assertIn("tonight_level", dashboard_json["assistant"]["status"])
        self.assertIn("summary", dashboard_json["assistant"])
        self.assertIn("recommendations", dashboard_json["assistant"])
        self.assertIn("topup_status", dashboard_json["automation"])

        self.assertIsNotNone(html_asset)
        self.assertIsNotNone(json_asset)
        self.assertEqual(html_asset[0], 200)
        self.assertEqual(html_asset[1], "text/html; charset=utf-8")
        self.assertIn(b"Growatt Dashboard", html_asset[2])
        self.assertEqual(json_asset[0], 200)
        self.assertEqual(json_asset[1], "application/json; charset=utf-8")
        self.assertEqual(json.loads(json_asset[2])["schema_version"], 1)

    def test_dashboard_html_tolerates_missing_optional_forecast_values(self):
        class PartialThreshold:
            threshold = None
            reason = ""
            weather_category = ""

        status = {
            "device": {"capacity": "50%"},
            "storage_params": {
                "storageBean": {"outputConfig": "0", "pPv1": 700, "pPv2": 500, "outPutPower": 900},
                "storageDetailBean": {"bmsSoc": 50},
            },
        }
        schedule = {"timezone": "Africa/Lagos", "jobs": []}
        pv_forecast = {"tomorrow_kwh": None, "today_remaining_kwh": None, "panel_kwp": None}

        with patch("growatt_guard.dashboard_viewmodel.read_pause_state", return_value=None), patch(
            "growatt_guard.dashboard_viewmodel.read_battery_alert_state", return_value=None
        ), patch("growatt_guard.dashboard_viewmodel.read_growatt_cloud_failure_state", return_value=None), patch(
            "growatt_guard.dashboard_viewmodel.read_pvoutput_state", return_value=None
        ), patch("growatt_guard.dashboard.read_mode_audit_rows", return_value=[]):
            html = build_dashboard_html(
                status,
                schedule,
                {"dates": {}},
                PartialThreshold(),
                metrics_history=[],
                pv_forecast=pv_forecast,
            )

        self.assertIn("Preserve Threshold", html)
        self.assertIn("Weather signal is unavailable.", html)
        self.assertIn("Set PANEL_KWP", html)

    def test_dashboard_next_action_finds_upcoming_job(self):
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [
                {
                    "id": "morning-preserve",
                    "name": "Morning Preserve",
                    "cron": "30 6 * * *",
                    "command": "preserve-battery",
                }
            ],
        }

        action = build_dashboard_next_action(schedule, now=dt.datetime(2026, 6, 25, 6, 0))

        self.assertEqual(action["status"], "scheduled")
        self.assertEqual(action["job_id"], "morning-preserve")
        self.assertEqual(action["minutes_until"], 30)
        self.assertEqual(action["relative"], "in 30min")
        self.assertIn("preserve-battery", action["detail"])

    def test_dashboard_schedule_timeline_groups_recurring_jobs(self):
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [
                {
                    "id": "morning-preserve",
                    "name": "Morning Preserve",
                    "cron": "30 6 * * *",
                    "command": "preserve-battery",
                },
                {
                    "id": "morning-return",
                    "name": "Morning Return",
                    "cron": "0 8 * * *",
                    "command": "return-sbu",
                },
                {
                    "id": "battery-alert",
                    "name": "Battery Alert",
                    "cron": "*/30 * * * *",
                    "command": "battery-alert",
                },
                {
                    "id": "auto-topup-check",
                    "name": "Auto Topup",
                    "cron": "*/20 22-23,0-2 * * *",
                    "command": "auto-topup-check",
                },
            ],
        }

        timeline = build_dashboard_schedule_timeline(
            schedule,
            {},
            now=dt.datetime(2026, 6, 25, 7, 0),
        )
        by_id = {item["job_id"]: item for item in timeline}

        self.assertEqual(by_id["morning-preserve"]["status"], "Passed")
        self.assertEqual(by_id["morning-return"]["status"], "Next")
        self.assertEqual(by_id["battery-alert"]["status"], "Monitoring")
        self.assertEqual(by_id["battery-alert"]["time"], "00:00-23:30")
        self.assertEqual(by_id["battery-alert"]["detail"], "battery-alert - every 30min")
        self.assertEqual(by_id["auto-topup-check"]["status"], "Upcoming")
        self.assertEqual(by_id["auto-topup-check"]["time"], "00:00-02:40, 22:00-23:40")

    def test_dashboard_daily_insights_compare_same_time_history(self):
        now = dt.datetime(2026, 6, 25, 9, 0)
        live = {
            "timestamp": now.isoformat(),
            "pv_today_kwh": 2.0,
            "load_today_kwh": 3.0,
            "grid_today_kwh": 0.8,
            "soc": 56,
        }
        history = [
            {"timestamp": "2026-06-22T08:45:00", "pv_today_kwh": 1.0, "load_today_kwh": 4.0, "grid_today_kwh": 2.0, "soc": 45},
            {"timestamp": "2026-06-22T09:15:00", "pv_today_kwh": 1.8, "load_today_kwh": 5.0, "grid_today_kwh": 3.0, "soc": 46},
            {"timestamp": "2026-06-23T08:55:00", "pv_today_kwh": 1.2, "load_today_kwh": 4.2, "grid_today_kwh": 2.2, "soc": 47},
            {"timestamp": "2026-06-24T08:50:00", "pv_today_kwh": 1.1, "load_today_kwh": 4.1, "grid_today_kwh": 2.1, "soc": 46},
        ]

        insights = build_dashboard_daily_insights(live, history, now=now)
        items = {item["key"]: item for item in insights["items"]}

        self.assertEqual(insights["status"], "good")
        self.assertEqual(insights["sample_days"], 3)
        self.assertEqual(items["pv_today_kwh"]["level"], "good")
        self.assertEqual(items["load_today_kwh"]["level"], "good")
        self.assertEqual(items["grid_today_kwh"]["level"], "good")
        self.assertEqual(items["soc"]["level"], "good")
        self.assertEqual(items["pv_today_kwh"]["baseline"], 1.1)
        self.assertIn("above your", items["pv_today_kwh"]["detail"])

    def test_dashboard_daily_insights_handles_zero_baseline(self):
        now = dt.datetime(2026, 6, 25, 9, 0)
        live = {
            "timestamp": now.isoformat(),
            "pv_today_kwh": 1.2,
        }
        history = [
            {"timestamp": "2026-06-23T08:55:00", "pv_today_kwh": 0.0},
            {"timestamp": "2026-06-24T08:50:00", "pv_today_kwh": 0.0},
        ]

        insights = build_dashboard_daily_insights(live, history, now=now)
        pv_item = next(item for item in insights["items"] if item["key"] == "pv_today_kwh")

        self.assertEqual(pv_item["level"], "good")
        self.assertEqual(pv_item["baseline"], 0.0)
        self.assertIsNone(pv_item["delta_pct"])
        self.assertIn("zero average", pv_item["detail"])

    def test_tonight_risk_can_start_from_projected_sunset_soc(self):
        live = {"soc": 90.0, "battery_net_w": -2500.0, "load_w": 1200.0}

        with patch("growatt_guard.dashboard_insights.read_discharge_rate_history", return_value=[{"rate_w": 1700}, {"rate_w": 1700}]):
            risk = build_tonight_risk(
                live,
                battery_capacity_wh=30000,
                battery_bms_cutoff_soc=25,
                hours_to_sunrise=16,
                battery_charge_rate_w=2400,
                projection_start_soc=100,
                projection_hours=12,
                projection_basis="projected sunset SOC",
            )

        self.assertEqual(risk["level"], "watch")
        self.assertEqual(risk["projected_sunrise_soc"], 32.0)
        self.assertEqual(risk["topup_minutes"], 0.0)
        self.assertEqual(risk["projection_basis"], "projected sunset SOC")


    def test_dashboard_payload_morning_projection_does_not_start_from_sunset(self):
        status = {
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {
                    "bmsSoc": 44,
                    "pDischarge": 1300,
                    "pCharge": 0,
                    "ppv": 225,
                    "outPutPower": 1400,
                },
            }
        }
        schedule = {"timezone": "Africa/Lagos", "jobs": []}

        with patch("growatt_guard.dashboard_insights.read_discharge_rate_history", return_value=[{"rate_w": 400}, {"rate_w": 400}]), \
            patch("growatt_guard.dashboard_viewmodel.read_pause_state", return_value=None), \
            patch("growatt_guard.dashboard_viewmodel.read_battery_alert_state", return_value=None), \
            patch("growatt_guard.dashboard_viewmodel.read_growatt_cloud_failure_state", return_value=None), \
            patch("growatt_guard.dashboard_viewmodel.read_pvoutput_state", return_value=None):
            payload = build_dashboard_data_payload(
                status,
                schedule,
                {"dates": {}},
                ThresholdDecision(50, "fixed threshold"),
                battery_capacity_wh=30000,
                battery_bms_cutoff_soc=25,
                hours_to_sunrise=22,
                battery_charge_rate_w=2100,
                hours_to_sunset=10,
                now=dt.datetime(2026, 7, 5, 8, 0),
            )

        outlook = payload["planner"]["outlook"]
        self.assertGreater(outlook["projected_sunrise_soc"], 10)
        self.assertNotIn("projected sunset SOC", outlook["sunrise_basis"])

    def test_dashboard_payload_includes_last_successful_pvoutput_upload(self):
        status = {
            "device": {"capacity": "50%"},
            "storage_params": {
                "storageBean": {"outputConfig": "0", "ppv": 0, "outPutPower": 500},
                "storageDetailBean": {"bmsSoc": 50, "pDischarge": 200, "pCharge": 0},
            },
        }
        schedule = {"timezone": "Africa/Lagos", "jobs": []}
        now = dt.datetime(2026, 6, 25, 8, 0)

        with patch("growatt_guard.dashboard_insights.read_discharge_rate_history", return_value=[]), patch(
            "growatt_guard.dashboard_viewmodel.read_pause_state", return_value=None
        ), patch("growatt_guard.dashboard_viewmodel.read_battery_alert_state", return_value=None), patch(
            "growatt_guard.dashboard_viewmodel.read_growatt_cloud_failure_state", return_value={}
        ), patch(
            "growatt_guard.dashboard_viewmodel.read_pvoutput_state",
            return_value={"uploaded_at": "2026-06-25T07:55:00", "fields": {"v1": 1200}},
        ), patch("growatt_guard.dashboard.read_mode_audit_rows", return_value=[]), patch(
            "growatt_guard.dashboard.build_chart_data", return_value={"labels": [], "soc": []}
        ):
            payload = build_dashboard_data_payload(
                status, schedule, {"dates": {}}, ThresholdDecision(50, "test"), now=now
            )

        self.assertEqual(payload["freshness"]["last_successful_growatt_read_at"], "2026-06-25T08:00:00")
        self.assertEqual(payload["freshness"]["last_successful_pvoutput_upload_at"], "2026-06-25T07:55:00")

    def test_dashboard_topup_estimate_shows_short_topup_skip(self):
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
        ), patch("growatt_guard.dashboard_viewmodel.read_pause_state", return_value=None), patch(
            "growatt_guard.dashboard_viewmodel.read_battery_alert_state", return_value=None
        ), patch(
            "growatt_guard.dashboard_viewmodel.read_growatt_cloud_failure_state", return_value={}
        ), patch(
            "growatt_guard.dashboard.read_mode_audit_rows", return_value=[]
        ), patch(
            "growatt_guard.dashboard_viewmodel.read_pvoutput_state", return_value=None
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
        self.assertIn("skip (&lt;20min)", html)

    def test_dashboard_recent_activity_hides_dry_run_rows(self):
        status = {"device": {"capacity": "90%"}, "storage_params": {"storageBean": {"outputConfig": "0"}}}
        schedule = {"timezone": "Africa/Lagos", "jobs": []}
        audit_rows = [
            {
                "timestamp": "2026-07-01T08:00:26",
                "command": "auto-topup-check",
                "action": "auto-topup-started",
                "soc": "40",
                "previous_mode": "SBU priority [0]",
                "dry_run": "true",
            },
            {
                "timestamp": "2026-07-01T08:01:04",
                "command": "watchdog-sbu",
                "action": "verified-sbu",
                "soc": "89",
                "previous_mode": "SBU priority [0]",
                "dry_run": "false",
            },
        ]

        with patch("growatt_guard.dashboard_viewmodel.read_pause_state", return_value=None), patch(
            "growatt_guard.dashboard_viewmodel.read_battery_alert_state", return_value=None
        ), patch(
            "growatt_guard.dashboard_viewmodel.read_growatt_cloud_failure_state", return_value={}
        ), patch(
            "growatt_guard.dashboard.read_mode_audit_rows", return_value=audit_rows
        ), patch(
            "growatt_guard.dashboard_viewmodel.read_pvoutput_state", return_value=None
        ), patch(
            "growatt_guard.dashboard.build_chart_data",
            return_value={"labels": [], "preserve_checks": [], "utility_switches": [], "watchdog_repairs": []},
        ):
            html = build_dashboard_html(status, schedule, {"dates": {}}, ThresholdDecision(50, "fixed threshold"))

        self.assertIn("verified-sbu", html)
        self.assertNotIn("auto-topup-started", html)

    def test_dashboard_pv_forecast_explains_missing_panel_size(self):
        status = {
            "device": {"capacity": "85%"},
            "storage_params": {
                "outputConfig": "0",
                "storageBean": {
                    "ppv": 2900,
                    "epvToday": 25.1,
                },
            },
        }
        schedule = {"timezone": "Africa/Lagos", "jobs": []}

        with patch("growatt_guard.dashboard_viewmodel.read_pause_state", return_value=None), patch(
            "growatt_guard.dashboard_viewmodel.read_battery_alert_state", return_value=None
        ), patch(
            "growatt_guard.dashboard_viewmodel.read_growatt_cloud_failure_state", return_value={}
        ), patch(
            "growatt_guard.dashboard.read_mode_audit_rows", return_value=[]
        ), patch(
            "growatt_guard.dashboard_viewmodel.read_pvoutput_state", return_value=None
        ), patch(
            "growatt_guard.dashboard.build_chart_data",
            return_value={"labels": [], "preserve_checks": [], "utility_switches": [], "watchdog_repairs": []},
        ):
            html = build_dashboard_html(
                status,
                schedule,
                {"dates": {}},
                ThresholdDecision(
                    50,
                    "rainy/cloudy forecast: max cloud 90%, rain 2mm",
                    weather_category="rainy/cloudy",
                    cloud_cover=90,
                    precipitation_mm=2,
                ),
                battery_capacity_wh=30000,
                battery_bms_cutoff_soc=25,
                hours_to_sunrise=8,
                battery_charge_rate_w=2400,
                auto_topup_solar_skip_min_margin_minutes=60,
                auto_topup_min_minutes=20,
            )

        self.assertIn("Needs PANEL_KWP", html)
        self.assertIn("Set PANEL_KWP to convert Open-Meteo irradiance into PV kWh.", html)

    def test_dashboard_runtime_labels_current_load_energy_to_floor(self):
        status = {
            "device": {"capacity": "70%"},
            "storage_params": {
                "outputConfig": "0",
                "storageBean": {
                    "pDischarge": 1000,
                    "pCharge": 0,
                    "ppv": 0,
                    "outPutPower": 1000,
                },
            },
        }
        schedule = {"timezone": "Africa/Lagos", "jobs": []}

        with patch("growatt_guard.dashboard_viewmodel.read_pause_state", return_value=None), patch(
            "growatt_guard.dashboard_viewmodel.read_battery_alert_state", return_value=None
        ), patch(
            "growatt_guard.dashboard_viewmodel.read_growatt_cloud_failure_state", return_value={}
        ), patch(
            "growatt_guard.dashboard.read_mode_audit_rows", return_value=[]
        ), patch(
            "growatt_guard.dashboard_viewmodel.read_pvoutput_state", return_value=None
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
            )

        self.assertIn("Reserve Details", html)
        self.assertIn("Supporting values behind the first-glance battery", html)
        self.assertIn("Current Load Runtime", html)
        self.assertIn("13h 30m remaining", html)
        self.assertIn("Usable to 25% floor: 13.5 kWh", html)
        self.assertNotIn("Capacity 30.0 kWh", html)

    def test_dashboard_runtime_handles_near_zero_battery_draw(self):
        status = {
            "device": {"capacity": "98%"},
            "storage_params": {
                "outputConfig": "0",
                "storageBean": {
                    "pDischarge": 53,
                    "pCharge": 0,
                    "ppv": 1134,
                    "outPutPower": 1088,
                },
            },
        }
        schedule = {"timezone": "Africa/Lagos", "jobs": []}

        with patch("growatt_guard.dashboard_viewmodel.read_pause_state", return_value=None), patch(
            "growatt_guard.dashboard_viewmodel.read_battery_alert_state", return_value=None
        ), patch(
            "growatt_guard.dashboard_viewmodel.read_growatt_cloud_failure_state", return_value={}
        ), patch(
            "growatt_guard.dashboard.read_mode_audit_rows", return_value=[]
        ), patch(
            "growatt_guard.dashboard_viewmodel.read_pvoutput_state", return_value=None
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
            )

        self.assertIn("PV covering load", html)
        self.assertIn("Live battery draw only 53 W", html)
        self.assertNotIn("413h", html)

    def test_dashboard_pv_forecast_shows_rain_adjusted_headline(self):
        status = {
            "device": {"capacity": "85%"},
            "storage_params": {
                "outputConfig": "0",
                "storageBean": {
                    "ppv": 2900,
                    "epvToday": 25.1,
                },
            },
        }
        schedule = {"timezone": "Africa/Lagos", "jobs": []}

        with patch("growatt_guard.dashboard_viewmodel.read_pause_state", return_value=None), patch(
            "growatt_guard.dashboard_viewmodel.read_battery_alert_state", return_value=None
        ), patch(
            "growatt_guard.dashboard_viewmodel.read_growatt_cloud_failure_state", return_value={}
        ), patch(
            "growatt_guard.dashboard.read_mode_audit_rows", return_value=[]
        ), patch(
            "growatt_guard.dashboard_viewmodel.read_pvoutput_state", return_value=None
        ), patch(
            "growatt_guard.dashboard.build_chart_data",
            return_value={"labels": [], "preserve_checks": [], "utility_switches": [], "watchdog_repairs": []},
        ):
            html = build_dashboard_html(
                status,
                schedule,
                {"dates": {}},
                ThresholdDecision(
                    50,
                    "rainy/cloudy forecast: max cloud 90%, rain 2mm",
                    weather_category="rainy/cloudy",
                    cloud_cover=90,
                    precipitation_mm=2,
                ),
                pv_forecast={
                    "tomorrow_kwh": 18.3,
                    "base_tomorrow_kwh": 30.5,
                    "today_remaining_kwh": 8.2,
                    "panel_kwp": 6.0,
                    "weather_adjusted": True,
                    "weather_adjustment_factor": 0.6,
                    "weather_adjustment_source": "conservative default",
                    "tomorrow_cloud_cover": 90,
                    "tomorrow_precipitation_mm": 2,
                },
            )

        self.assertIn("Tomorrow PV", html)
        self.assertIn("18.3 kWh", html)
        self.assertIn("Rain-adjusted", html)
        self.assertIn("Rain-adjusted from 30.5 kWh", html)
        self.assertIn("60% conservative default factor", html)

    def test_dashboard_asset_for_path_handles_missing_json(self):
        with TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "dashboard.html"
            output.write_text("<html>ok</html>", encoding="utf-8")

            asset = dashboard_asset_for_path(output, "/dashboard.json")

        self.assertIsNotNone(asset)
        self.assertEqual(asset[0], 503)
        self.assertEqual(asset[1], "application/json; charset=utf-8")
        self.assertEqual(json.loads(asset[2])["error"], "dashboard_json_not_generated")
        self.assertIsNone(dashboard_asset_for_path(output, "/not-found"))

    def test_dashboard_asset_for_path_serves_install_assets(self):
        with TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "dashboard.html"
            manifest_asset = dashboard_asset_for_path(output, "/manifest.webmanifest?version=1")
            svg_asset = dashboard_asset_for_path(output, "/dashboard-icon.svg")
            icon_assets = {
                size: dashboard_asset_for_path(output, f"/dashboard-icon-{size}.png")
                for size in (180, 192, 512)
            }
            maskable_asset = dashboard_asset_for_path(output, "/dashboard-icon-maskable-512.png")

        self.assertEqual(manifest_asset[0:2], (200, "application/manifest+json; charset=utf-8"))
        manifest = json.loads(manifest_asset[2])
        self.assertEqual(manifest["id"], "/dashboard.html")
        self.assertEqual(manifest["start_url"], "/dashboard.html")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual({icon["sizes"] for icon in manifest["icons"]}, {"192x192", "512x512"})
        self.assertTrue(any(icon["purpose"] == "maskable" for icon in manifest["icons"]))
        self.assertEqual(svg_asset[0:2], (200, "image/svg+xml; charset=utf-8"))
        self.assertIn(b"Growatt energy dashboard", svg_asset[2])
        for size, asset in icon_assets.items():
            self.assertEqual(asset[0:2], (200, "image/png"))
            self.assertEqual(int.from_bytes(asset[2][16:20]), size)
            self.assertEqual(int.from_bytes(asset[2][20:24]), size)
        self.assertEqual(maskable_asset[0:2], (200, "image/png"))
        self.assertEqual(int.from_bytes(maskable_asset[2][16:20]), 512)

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
