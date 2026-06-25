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
    build_service_status,
    command_service_status,
    format_diagnostic_items,
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

    def test_diagnostic_bundle_redacts_and_includes_sections(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.diagnostics.LOG_FILE", Path(tmpdir) / "growatt_power_guard.log"
        ), patch(
            "growatt_guard.diagnostics.build_service_status",
            return_value=[DiagnosticItem("Schedule", "OK", "15 jobs")],
        ), patch("growatt_guard.diagnostics.read_mode_audit_rows", return_value=[]):
            bundle = build_diagnostic_bundle(config)

        self.assertIn("Growatt diagnostic bundle", bundle)
        self.assertIn("DISCORD_WEBHOOK_CONFIGURED=True", bundle)
        self.assertNotIn("discord.com/api/webhooks", bundle)
        self.assertIn("This bundle is local/read-only", bundle)
