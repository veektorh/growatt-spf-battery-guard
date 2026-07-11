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
    def test_dashboard_data_quality_explains_missing_and_estimated_values(self):
        quality = build_dashboard_data_quality(
            {
                "soc": 47,
                "mode": "SBU priority",
                "pv_w": 1029,
                "load_w": None,
                "battery_net_w": 374,
                "pv_today_kwh": 1.2,
                "load_today_kwh": None,
                "grid_today_kwh": 13.7,
                "charge_today_kwh": 10.5,
                "grid_source": "estimated",
            },
            {"pv_w": "channel-sum:pPv1,pPv2"},
        )

        self.assertEqual(quality["level"], "watch")
        self.assertEqual(quality["score"], 78)
        self.assertIn("load now", quality["missing"])
        self.assertTrue(any("estimated" in item for item in quality["items"]))

    def test_dashboard_energy_reconciliation_balances_daily_counters(self):
        reconciliation = build_dashboard_energy_reconciliation(
            {
                "pv_today_kwh": 1.2,
                "grid_today_kwh": 13.7,
                "load_today_kwh": 12.5,
                "charge_today_kwh": 10.5,
                "discharge_today_kwh": 8.1,
            }
        )

        self.assertEqual(reconciliation["status"], "ok")
        self.assertEqual(reconciliation["supply_total_kwh"], 23.0)
        self.assertEqual(reconciliation["demand_total_kwh"], 23.0)
        self.assertEqual(reconciliation["delta_kwh"], 0.0)

    def test_dashboard_data_quality_warns_on_daily_counter_mismatch(self):
        quality = build_dashboard_data_quality(
            {
                "soc": 70,
                "mode": "SBU priority",
                "pv_w": 500,
                "load_w": 700,
                "battery_net_w": 100,
                "pv_today_kwh": 1.0,
                "grid_today_kwh": 1.0,
                "load_today_kwh": 10.0,
                "charge_today_kwh": 2.0,
                "discharge_today_kwh": 0.0,
            },
            {},
        )

        self.assertEqual(quality["level"], "watch")
        self.assertEqual(quality["score"], 100)
        self.assertEqual(quality["reconciliation"]["status"], "watch")
        self.assertEqual(quality["reconciliation"]["delta_kwh"], -10.0)
        self.assertTrue(any("do not reconcile" in item for item in quality["items"]))

    def test_dashboard_energy_reconciliation_reports_missing_counters(self):
        reconciliation = build_dashboard_energy_reconciliation(
            {
                "pv_today_kwh": 1.2,
                "grid_today_kwh": 13.7,
                "load_today_kwh": 12.5,
                "charge_today_kwh": 10.5,
            }
        )

        self.assertEqual(reconciliation["status"], "unavailable")
        self.assertEqual(reconciliation["missing"], ["discharge_today_kwh"])

    def test_dashboard_daily_mix_summarizes_energy_context(self):
        mix = build_dashboard_daily_mix(
            {
                "pv_today_kwh": 1.2,
                "grid_today_kwh": 13.7,
                "load_today_kwh": 12.5,
                "charge_today_kwh": 10.5,
                "discharge_today_kwh": 8.1,
            }
        )

        self.assertEqual(mix["supply_total_kwh"], 14.9)
        self.assertEqual(mix["demand_total_kwh"], 23.0)
        self.assertEqual(mix["battery_activity_total_kwh"], 18.6)
        self.assertEqual(mix["battery_net_kwh"], 2.4)
        self.assertEqual(mix["battery_net_title"], "Net stored")
        self.assertEqual(mix["pv_supply_pct"], 8.1)
        self.assertEqual(mix["grid_supply_pct"], 91.9)
        self.assertEqual(mix["load_demand_pct"], 54.3)
        self.assertEqual(mix["charge_demand_pct"], 45.7)

    def test_dashboard_metrics_extracts_live_energy_values(self):
        now = dt.datetime(2026, 6, 25, 8, 30)
        status = {
            "device": {"capacity": "47%"},
            "storage_params": {
                "storageBean": {
                    "outputConfig": "0",
                    "ppv": 906,
                    "epvToday": 1.2,
                    "pGrid": 0,
                    "eToUserToday": 0,
                    "eChargeToday": 0,
                    "useEnergyToday": 0,
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
            "storage_energy_overview": {
                "eToUserToday": "13.7",
                "eChargeToday": "10.5",
                "useEnergyToday": "12.5",
            },
        }

        metrics = extract_dashboard_metrics(status, now=now)

        self.assertEqual(metrics["soc"], 47)
        self.assertEqual(metrics["pv_w"], 906)
        self.assertEqual(metrics["pv_today_kwh"], 1.2)
        self.assertEqual(metrics["grid_w"], 0)
        self.assertEqual(metrics["grid_today_kwh"], 13.7)
        self.assertEqual(metrics["charge_today_kwh"], 10.5)
        self.assertEqual(metrics["load_today_kwh"], 12.5)
        self.assertEqual(metrics["load_w"], 1145)
        self.assertEqual(metrics["discharge_w"], 374)
        self.assertEqual(metrics["battery_net_w"], 374)

    def test_dashboard_metrics_include_bypass_detection(self):
        now = dt.datetime(2026, 6, 25, 23, 30)
        status = {
            "device": {"capacity": "55%"},
            "storage_params": {
                "storageBean": {"outputConfig": "0", "pGrid": 1800, "outPutPower": 700, "pCharge": 0},
                "storageDetailBean": {"bmsSoc": 55, "pCharge": 1200, "pDischarge": 0, "statusText": "AC charge and Bypass"},
            },
        }

        metrics = extract_dashboard_metrics(status, now=now)

        self.assertTrue(metrics["bypass_detected"])
        self.assertIn("AC charge and Bypass", metrics["bypass_reason"])
        self.assertEqual(metrics["charge_w"], 1200)
        self.assertEqual(metrics["battery_net_w"], -1200)

    def test_dashboard_html_displays_bypass_detected_badge(self):
        status = {
            "device": {"capacity": "55%"},
            "storage_params": {
                "storageBean": {"outputConfig": "0", "pGrid": 1800, "outPutPower": 700},
                "storageDetailBean": {"bmsSoc": 55, "pCharge": 1200, "pDischarge": 0, "statusText": "AC charge and Bypass"},
            },
        }
        schedule = {"timezone": "Africa/Lagos", "jobs": []}

        html = build_dashboard_html(status, schedule, {"dates": {}}, ThresholdDecision(50, "test threshold"))

        self.assertIn("Grid Bypass", html)
        self.assertIn("Bypass: Detected", html)
        self.assertIn("AC charge and Bypass", html)

    def test_dashboard_html_treats_utility_bypass_as_clear(self):
        status = {
            "device": {"capacity": "34%"},
            "storage_params": {
                "storageBean": {"outputConfig": "2", "pAcInPut": 899, "outPutPower": 883},
                "storageDetailBean": {"bmsSoc": 34, "pCharge": 131, "pDischarge": 0, "statusText": "Combine charge and Bypass"},
            },
        }
        schedule = {"timezone": "Africa/Lagos", "jobs": []}

        html = build_dashboard_html(status, schedule, {"dates": {}}, ThresholdDecision(50, "test threshold"))
        metrics = extract_dashboard_metrics(status)

        self.assertFalse(metrics["bypass_detected"])
        self.assertEqual(metrics["bypass_reason"], "")
        self.assertIn("Bypass: Clear", html)
        self.assertNotIn("Bypass: Detected", html)

    def test_dashboard_metrics_sums_pv_input_channels_when_ppv_is_one_channel(self):
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
                    "epvTotal": 0,
                },
                "storageDetailBean": {"bmsSoc": 47, "epvTotal": 2864.1},
            },
        }

        metrics = extract_dashboard_metrics(status, now=now)
        sources = extract_dashboard_metric_sources(status)

        self.assertEqual(metrics["pv_w"], 1029)
        self.assertEqual(
            sources["pv_w"],
            "channel-sum:storage_params.storageBean.pPv1,storage_params.storageBean.pPv2",
        )
        self.assertEqual(metrics["pv_today_kwh"], 1.2)
        self.assertEqual(
            sources["pv_today_kwh"],
            "channel-sum:storage_params.storageBean.epv1Today,storage_params.storageBean.epv2Today",
        )
        self.assertEqual(metrics["pv_total"], "2.86 MWh")

    def test_dashboard_metrics_does_not_double_count_duplicate_pv_channel_aliases(self):
        now = dt.datetime(2026, 6, 25, 8, 30)
        status = {
            "device": {"capacity": "47%"},
            "storage_params": {
                "storageBean": {
                    "outputConfig": "0",
                    "ppv": 300,
                    "pPv1": 300,
                    "pPv2": 500,
                    "pv1Power": 300,
                    "pv2Power": 500,
                },
                "storageDetailBean": {"bmsSoc": 47},
            },
        }

        metrics = extract_dashboard_metrics(status, now=now)
        sources = extract_dashboard_metric_sources(status)

        self.assertEqual(metrics["pv_w"], 800)
        self.assertEqual(
            sources["pv_w"],
            "channel-sum:storage_params.storageBean.pPv1,storage_params.storageBean.pPv2",
        )

    def test_dashboard_metrics_sums_ppv_and_ppv2_live_spf_shape(self):
        now = dt.datetime(2026, 6, 25, 8, 30)
        status = {
            "device": {"capacity": "47%"},
            "storage_params": {
                "storageDetailBean": {
                    "bmsSoc": 47,
                    "outputConfig": "0",
                    "ppv": 156,
                    "ppvText": "156.0 W",
                    "ppv2": 267,
                },
            },
        }

        metrics = extract_dashboard_metrics(status, now=now)
        sources = extract_dashboard_metric_sources(status)

        self.assertEqual(metrics["pv_w"], 423)
        self.assertEqual(
            sources["pv_w"],
            "channel-sum:storage_params.storageDetailBean.ppv,storage_params.storageDetailBean.ppv2",
        )

    def test_dashboard_metrics_sums_mixed_pv_channel_aliases(self):
        now = dt.datetime(2026, 6, 25, 8, 30)
        status = {
            "device": {"capacity": "47%"},
            "storage_params": {
                "storageBean": {
                    "outputConfig": "0",
                    "ppv": 156,
                    "pPv2": 267,
                },
                "storageDetailBean": {"bmsSoc": 47},
            },
        }

        metrics = extract_dashboard_metrics(status, now=now)
        sources = extract_dashboard_metric_sources(status)

        self.assertEqual(metrics["pv_w"], 423)
        self.assertEqual(
            sources["pv_w"],
            "channel-sum:storage_params.storageBean.ppv,storage_params.storageBean.pPv2",
        )

    def test_dashboard_metrics_falls_back_to_pv_channel_power_without_total(self):
        now = dt.datetime(2026, 6, 25, 8, 30)
        status = {
            "device": {"capacity": "47%"},
            "storage_params": {
                "storageBean": {
                    "outputConfig": "0",
                    "pPv1": 300,
                    "pPv2": 200,
                },
                "storageDetailBean": {"bmsSoc": 47},
            },
        }

        metrics = extract_dashboard_metrics(status, now=now)
        sources = extract_dashboard_metric_sources(status)

        self.assertEqual(metrics["pv_w"], 500)
        self.assertEqual(
            sources["pv_w"],
            "channel-sum:storage_params.storageBean.pPv1,storage_params.storageBean.pPv2",
        )

    def test_dashboard_metrics_history_roundtrip_and_payload(self):
        status = {
            "device": {"capacity": "50%"},
            "storage_params": {
                "storageBean": {"outputConfig": "0", "ppv": 500, "outPutPower": 800, "epvToday": 2.5},
                "storageDetailBean": {"bmsSoc": 50, "pDischarge": 300, "pCharge": 0},
            },
        }

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.dashboard_metrics.DASHBOARD_METRICS_FILE", Path(tmpdir) / "dashboard_metrics.jsonl"
        ):
            append_dashboard_metric_snapshot(status, now=dt.datetime(2026, 6, 25, 8, 0))
            append_dashboard_metric_snapshot(status, now=dt.datetime(2026, 6, 25, 8, 10))
            history = read_dashboard_metrics_history(now=dt.datetime(2026, 6, 25, 8, 15))
            payload = build_dashboard_history_payload(history, now=dt.datetime(2026, 6, 25, 8, 15))

        self.assertEqual(len(history), 2)
        self.assertEqual(payload["power"]["pv_w"], [500.0, 500.0])
        self.assertEqual(payload["soc"]["soc"], [50.0, 50.0])
        self.assertEqual(payload["daily"]["pv_kwh"][-1], 2.5)

    def test_payload_is_json_serializable_when_paused(self):
        # read_pause_state injects a raw datetime under paused_until_dt; it must
        # not leak into the JSON payload (it crashed observability-refresh during
        # overnight topup pauses). Assert json.dumps succeeds WITHOUT a default=
        # fallback, proving no raw datetime survives into the payload.
        status = {
            "device": {"capacity": "50%"},
            "storage_params": {
                "storageBean": {"outputConfig": "0"},
                "storageDetailBean": {"bmsSoc": 50},
            },
        }
        schedule = {"timezone": "Africa/Lagos", "jobs": []}
        paused = {
            "paused_until": "2026-06-26T06:00:00+00:00",
            "reason": "auto-topup",
            "paused_until_dt": dt.datetime(2026, 6, 26, 6, 0, tzinfo=dt.timezone.utc),
        }

        with patch("growatt_guard.dashboard_viewmodel.read_pause_state", return_value=paused), patch(
            "growatt_guard.dashboard_viewmodel.read_battery_alert_state", return_value=None
        ), patch(
            "growatt_guard.dashboard_viewmodel.read_growatt_cloud_failure_state", return_value=None
        ), patch("growatt_guard.dashboard_viewmodel.read_pvoutput_state", return_value=None):
            payload = build_dashboard_data_payload(
                status, schedule, {"dates": {}}, None,
                now=dt.datetime(2026, 6, 26, 2, 0).astimezone(),
            )

        # Strict serialization: no default= encoder. Raises if a datetime leaks.
        json.dumps(payload)
        self.assertNotIn("paused_until_dt", payload["automation"]["pause_state"])
        self.assertEqual(payload["automation"]["pause_state"]["paused_until"], "2026-06-26T06:00:00+00:00")

    def test_dashboard_refresh_once_writes_and_exits(self):
        config = make_config()

        with patch("growatt_guard.dashboard_service.write_dashboard", return_value=Path("dashboard.html")) as write_mock, redirect_stdout(
            StringIO()
        ) as stdout:
            self.assertEqual(command_dashboard_refresh(config, "dashboard.html", 1, once=True), 0)

        write_mock.assert_called_once_with(config, "dashboard.html")
        self.assertIn("Dashboard refreshed", stdout.getvalue())

    def test_dashboard_stale_alert_sends_once_when_file_is_missing(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.DASHBOARD_STALE_ALERT_FILE", Path(tmpdir) / "dashboard_stale_alert.json"
        ), patch("growatt_guard.dashboard_service.send_discord_message", return_value=True) as send_mock, redirect_stdout(StringIO()):
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
        ), patch("growatt_guard.dashboard_service.send_discord_message", return_value=True) as send_mock, redirect_stdout(StringIO()):
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
            "growatt_guard.dashboard_service.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ) as load_mock, patch("growatt_guard.dashboard_service.validate_schedule", return_value=schedule), patch(
            "growatt_guard.dashboard_service.validate_schedule_overrides", return_value={"dates": {}}
        ), patch(
            "growatt_guard.dashboard_service.choose_preserve_threshold",
            return_value=ThresholdDecision(50, "fixed threshold"),
        ), patch(
            "growatt_guard.dashboard_metrics.DASHBOARD_METRICS_FILE", Path(tmpdir) / "dashboard_metrics.jsonl"
        ), patch(
            "growatt_guard.dashboard_service.publish_pvoutput_status_from_status",
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
            "growatt_guard.dashboard_service.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch("growatt_guard.dashboard_service.validate_schedule", return_value=schedule), patch(
            "growatt_guard.dashboard_service.validate_schedule_overrides", return_value={"dates": {}}
        ), patch(
            "growatt_guard.dashboard_service.choose_preserve_threshold",
            return_value=ThresholdDecision(50, "fixed threshold"),
        ), patch(
            "growatt_guard.state.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch(
            "growatt_guard.dashboard_metrics.DASHBOARD_METRICS_FILE", Path(tmpdir) / "dashboard_metrics.jsonl"
        ), patch("growatt_guard.dashboard.read_mode_audit_rows", return_value=[]), redirect_stdout(StringIO()):
            output = Path(tmpdir) / "dashboard.html"
            command_dashboard(config, str(output))
            html = output.read_text(encoding="utf-8")

        self.assertIn("Today&#8217;s Schedule", html)
        self.assertIn("morning-preserve", html)


