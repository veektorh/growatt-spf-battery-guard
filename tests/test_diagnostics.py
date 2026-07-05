import datetime as dt
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_guard.diagnostics import (
    DiagnosticItem,
    build_diagnostic_bundle,
    build_diagnostic_bundle_payload,
    build_pv_metric_probe_payload,
    build_service_status,
    build_service_status_payload,
    command_redact_probe,
    command_service_status,
    format_diagnostic_items,
    format_pv_metric_probe,
    redact_probe_fixture,
)


class DiagnosticsTests(unittest.TestCase):
    def test_format_diagnostic_items_reports_overall_status(self):
        text = format_diagnostic_items(
            "Service checks",
            [
                DiagnosticItem("Schedule", "OK", "15 jobs"),
                DiagnosticItem("Dashboard", "WARN", "stale"),
            ],
        )

        self.assertIn("Result: WARN", text)
        self.assertIn("[WARN] Dashboard: stale", text)

    def test_build_service_status_is_read_only_and_summarizes_local_state(self):
        config = make_config()
        schedule = {"timezone": "Africa/Lagos", "jobs": [{"id": "health", "cron": "10 6 * * *", "command": "health-check"}]}

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.diagnostics.validate_schedule", return_value=schedule), patch(
            "growatt_guard.diagnostics.check_cron_schedule",
            return_value=[DiagnosticItem("Cron jobs", "OK", "1 scheduled job installed.")],
        ), patch(
            "growatt_guard.diagnostics.dashboard_freshness",
            return_value={"stale": False, "reason": "dashboard file is fresh"},
        ), patch("growatt_guard.diagnostics.read_pause_state", return_value=None), patch(
            "growatt_guard.diagnostics.read_topup_state", return_value=None
        ), patch("growatt_guard.diagnostics.read_command_lock_state", return_value=None), patch(
            "growatt_guard.diagnostics.os.name", "nt"
        ):
            items = build_service_status(config)

        names = [item.name for item in items]
        self.assertIn("Schedule", names)
        self.assertIn("Dashboard freshness", names)
        self.assertIn("growatt-dashboard-refresh.service", names)

    def test_command_service_status_prints_report(self):
        config = make_config()
        with patch(
            "growatt_guard.diagnostics.build_service_status",
            return_value=[DiagnosticItem("Schedule", "OK", "15 jobs")],
        ), redirect_stdout(StringIO()) as stdout:
            result = command_service_status(config)

        self.assertEqual(result, 0)
        self.assertIn("Growatt service status", stdout.getvalue())

    def test_command_service_status_can_print_json(self):
        config = make_config()
        with patch(
            "growatt_guard.diagnostics.build_service_status",
            return_value=[DiagnosticItem("Schedule", "OK", "15 jobs")],
        ), redirect_stdout(StringIO()) as stdout:
            result = command_service_status(config, json_output=True)

        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["result"], "OK")
        self.assertEqual(payload["items"][0]["name"], "Schedule")

    def test_build_service_status_includes_pvoutput_freshness(self):
        config = make_config(pvoutput_enabled=True)
        schedule = {"timezone": "Africa/Lagos", "jobs": [{"id": "health", "cron": "10 6 * * *", "command": "health-check"}]}
        now = dt.datetime.now()

        with patch("growatt_guard.diagnostics.validate_schedule", return_value=schedule), patch(
            "growatt_guard.diagnostics.lint_schedule",
            return_value=[DiagnosticItem("Schedule lint", "OK", "fine")],
        ), patch(
            "growatt_guard.diagnostics.check_cron_schedule",
            return_value=[],
        ), patch(
            "growatt_guard.diagnostics.dashboard_freshness",
            return_value={"stale": False, "reason": "dashboard file is fresh"},
        ), patch(
            "growatt_guard.diagnostics.read_pvoutput_state",
            return_value={"uploaded_at": now.isoformat(timespec="seconds"), "fields": {"v1": 1000}},
        ), patch("growatt_guard.diagnostics.read_pause_state", return_value=None), patch(
            "growatt_guard.diagnostics.read_topup_state", return_value=None
        ), patch("growatt_guard.diagnostics.read_command_lock_state", return_value=None), patch(
            "growatt_guard.diagnostics.os.name", "nt"
        ):
            items = build_service_status(config)

        self.assertTrue(any(item.name == "PVOutput freshness" and item.status == "OK" for item in items))

    def test_build_service_status_payload_is_structured(self):
        config = make_config()
        with patch(
            "growatt_guard.diagnostics.build_service_status",
            return_value=[DiagnosticItem("Schedule", "OK", "15 jobs")],
        ):
            payload = build_service_status_payload(config)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["result"], "OK")
        self.assertEqual(payload["items"][0]["detail"], "15 jobs")

    def test_diagnostic_bundle_redacts_and_includes_sections(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.diagnostics.LOG_FILE", Path(tmpdir) / "growatt_power_guard.log"
        ), patch(
            "growatt_guard.diagnostics.build_service_status",
            return_value=[DiagnosticItem("Schedule", "OK", "15 jobs")],
        ), patch("growatt_guard.diagnostics.read_mode_audit_rows", return_value=[]):
            (Path(tmpdir) / "growatt_power_guard.log").write_text(
                "2026-06-26 ERROR DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/example\n",
                encoding="utf-8",
            )
            bundle = build_diagnostic_bundle(config)

        self.assertIn("Growatt diagnostic bundle", bundle)
        self.assertIn("DISCORD_WEBHOOK_CONFIGURED=True", bundle)
        self.assertNotIn("discord.com/api/webhooks/example", bundle)
        self.assertIn("This bundle is local/read-only", bundle)

    def test_diagnostic_bundle_payload_can_include_cloud_summary(self):
        config = make_config()
        status = {
            "device": {"capacity": "68%"},
            "storage_params": {"storageBean": {"outputConfig": "0"}},
        }
        with patch(
            "growatt_guard.diagnostics.build_service_status",
            return_value=[DiagnosticItem("Schedule", "OK", "15 jobs")],
        ), patch("growatt_guard.diagnostics.read_mode_audit_rows", return_value=[]), patch(
            "growatt_guard.diagnostics.load_context",
            return_value=(None, None, status),
        ):
            payload = build_diagnostic_bundle_payload(config, include_cloud=True)

        self.assertEqual(payload["cloud_summary"]["status"], "OK")
        self.assertEqual(payload["cloud_summary"]["soc"]["value"], 68.0)

    def test_pv_metric_probe_payload_redacts_to_metric_paths(self):
        status = {
            "storage_params": {
                "storageDetailBean": {
                    "ppv": 156,
                    "ppv2": 267,
                    "epvToday": 14.9,
                    "outputConfig": "0",
                }
            }
        }

        payload = build_pv_metric_probe_payload(status, now=dt.datetime(2026, 6, 26, 15, 0))
        text = format_pv_metric_probe(payload)

        self.assertEqual(payload["dashboard"]["pv_w"], 423)
        self.assertIn("storage_params.storageDetailBean.ppv2", text)
        self.assertIn("PV now: 423 W", text)

    def test_redact_probe_fixture_preserves_metrics_and_removes_identifiers(self):
        raw = {
            "plantId": "real-plant-id",
            "deviceSn": "real-device-sn",
            "dataloggerSn": "real-datalogger-sn",
            "storage_params": {"storageDetailBean": {"ppv": 156, "ppv2": 267}},
            "note": "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/example",
        }

        redacted = redact_probe_fixture(raw)

        self.assertEqual(redacted["plantId"], "[redacted]")
        self.assertEqual(redacted["deviceSn"], "[redacted]")
        self.assertEqual(redacted["dataloggerSn"], "[redacted]")
        self.assertEqual(redacted["storage_params"]["storageDetailBean"]["ppv"], 156)
        self.assertEqual(redacted["storage_params"]["storageDetailBean"]["ppv2"], 267)
        self.assertNotIn("discord.com/api/webhooks/example", redacted["note"])

    def test_command_redact_probe_writes_redacted_json(self):
        raw = {"plantName": "private plant", "storage_params": {"storageDetailBean": {"epvToday": 14.9}}}

        with TemporaryDirectory() as tmpdir, redirect_stdout(StringIO()) as stdout:
            input_path = Path(tmpdir) / "raw.json"
            output_path = Path(tmpdir) / "redacted.json"
            input_path.write_text(json.dumps(raw), encoding="utf-8")

            result = command_redact_probe(str(input_path), str(output_path))

            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(payload["plantName"], "[redacted]")
        self.assertEqual(payload["storage_params"]["storageDetailBean"]["epvToday"], 14.9)
        self.assertIn("Redacted probe written", stdout.getvalue())


class DeploymentPreflightTests(unittest.TestCase):

    def test_update_server_uses_preflight_without_completing_topup(self):
        script = (Path(__file__).resolve().parents[1] / "update_server.sh").read_text(encoding="utf-8")

        venv_pos = script.index("python3 -m venv")
        preflight_pos = script.index("deployment-preflight")
        pull_pos = script.index("git pull --ff-only")
        self.assertLess(venv_pos, preflight_pos)
        self.assertLess(preflight_pos, pull_pos)
        self.assertNotIn("topup-complete-check", script)

    def test_preflight_allows_clear_state(self):
        from growatt_guard.diagnostics import build_deployment_preflight_payload, command_deployment_preflight

        with patch("growatt_guard.diagnostics.read_topup_state", return_value=None), \
            patch("growatt_guard.diagnostics.read_utility_hold_state", return_value=None), \
            patch("growatt_guard.diagnostics.read_pause_state", return_value=None), \
            patch("growatt_guard.diagnostics.read_command_lock_state", return_value=None), \
            patch("growatt_guard.diagnostics.dashboard_freshness", return_value={"stale": False, "reason": "fresh"}) as freshness_mock, \
            patch("growatt_guard.diagnostics._run", return_value=None):
            payload = build_deployment_preflight_payload(make_config(dashboard_stale_minutes=45))

        self.assertEqual(payload["result"], "OK")
        self.assertFalse(payload["topup"]["present"])
        freshness_mock.assert_called_once()
        self.assertEqual(freshness_mock.call_args.args[1], 45)

        with patch("growatt_guard.diagnostics.build_deployment_preflight_payload", return_value=payload), redirect_stdout(StringIO()):
            self.assertEqual(command_deployment_preflight(make_config()), 0)

    def test_preflight_blocks_active_utility_hold(self):
        from growatt_guard.diagnostics import build_deployment_preflight_payload, command_deployment_preflight

        hold = {"ownership": "owned", "target_soc": 50, "max_expiry": "2026-07-05T07:00:00+00:00"}
        with patch("growatt_guard.diagnostics.read_topup_state", return_value=None), \
            patch("growatt_guard.diagnostics.read_utility_hold_state", return_value=hold), \
            patch("growatt_guard.diagnostics.read_pause_state", return_value=None), \
            patch("growatt_guard.diagnostics.read_command_lock_state", return_value=None), \
            patch("growatt_guard.diagnostics.dashboard_freshness", return_value={"stale": False, "reason": "fresh"}), \
            patch("growatt_guard.diagnostics._run", return_value=None):
            payload = build_deployment_preflight_payload()

        self.assertEqual(payload["result"], "BLOCKED")
        self.assertTrue(payload["utility_hold"]["present"])

        with patch("growatt_guard.diagnostics.build_deployment_preflight_payload", return_value=payload), redirect_stdout(StringIO()):
            self.assertEqual(command_deployment_preflight(make_config()), 1)
