import datetime as dt
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_guard.alerts import command_battery_alert
from growatt_guard.dashboard_service import refresh_observability_once
from growatt_guard.growatt_api import extract_device_lost, extract_last_seen_text
from growatt_guard.growatt_api import DeviceRef
from growatt_guard.health import HealthCheckItem, command_health_check
from growatt_guard.modes import ensure_device_reporting
from growatt_guard.weather import ThresholdDecision

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def state_patches(tmpdir):
    root = Path(tmpdir)
    return (
        patch("growatt_guard.state.STATE_DIR", root),
        patch("growatt_guard.state.DEVICE_LOST_FILE", root / "device_lost.json"),
    )


class DeviceLostExtractionTests(unittest.TestCase):
    def test_lost_device_is_detected_from_real_payload(self):
        status = load_fixture("spf_lost_device.json")
        self.assertTrue(extract_device_lost(status))
        self.assertEqual(extract_last_seen_text(status), "2026-08-28 13:55:51")

    def test_reporting_device_is_not_flagged(self):
        status = load_fixture("spf_sbu_discharging.json")
        self.assertFalse(extract_device_lost(status))

    def test_lost_accepts_string_booleans(self):
        self.assertTrue(extract_device_lost({"device": {"lost": "true"}}))
        self.assertFalse(extract_device_lost({"device": {"lost": "false"}}))


class DeviceLostGuardTests(unittest.TestCase):
    def test_guard_blocks_mode_change_when_device_is_lost(self):
        config = make_config(discord_webhook_url="")
        status = load_fixture("spf_lost_device.json")
        with TemporaryDirectory() as tmpdir:
            p1, p2 = state_patches(tmpdir)
            with p1, p2, redirect_stdout(StringIO()) as out, patch(
                "growatt_guard.modes.append_mode_audit"
            ):
                blocked = ensure_device_reporting(config, "preserve-battery", status)
        self.assertTrue(blocked)
        self.assertIn("not reporting", out.getvalue())

    def test_guard_allows_mode_change_when_device_reports(self):
        config = make_config()
        status = load_fixture("spf_sbu_discharging.json")
        with TemporaryDirectory() as tmpdir:
            p1, p2 = state_patches(tmpdir)
            with p1, p2, redirect_stdout(StringIO()):
                self.assertFalse(ensure_device_reporting(config, "preserve-battery", status))

    def test_lost_device_alerts_only_once(self):
        config = make_config(discord_webhook_url="https://example.invalid/hook")
        status = load_fixture("spf_lost_device.json")
        with TemporaryDirectory() as tmpdir:
            p1, p2 = state_patches(tmpdir)
            with p1, p2, redirect_stdout(StringIO()), patch(
                "growatt_guard.modes.append_mode_audit"
            ), patch("growatt_guard.notifications.send_discord_embed", return_value=True) as send:
                for _ in range(5):
                    ensure_device_reporting(config, "auto-topup-check", status)
        self.assertEqual(send.call_count, 1)

    def test_recovery_notifies_once_after_an_alert(self):
        config = make_config(discord_webhook_url="https://example.invalid/hook")
        lost = load_fixture("spf_lost_device.json")
        healthy = load_fixture("spf_sbu_discharging.json")
        with TemporaryDirectory() as tmpdir:
            p1, p2 = state_patches(tmpdir)
            with p1, p2, redirect_stdout(StringIO()), patch(
                "growatt_guard.modes.append_mode_audit"
            ), patch("growatt_guard.notifications.send_discord_embed", return_value=True) as send:
                ensure_device_reporting(config, "preserve-battery", lost)
                ensure_device_reporting(config, "preserve-battery", healthy)
                ensure_device_reporting(config, "preserve-battery", healthy)
        titles = [call.args[1]["title"] for call in send.call_args_list]
        self.assertEqual(len(titles), 2)
        self.assertIn("not reporting", titles[0])
        self.assertIn("reporting again", titles[1])

    def test_lost_device_alert_reports_last_known_soc_as_stale(self):
        config = make_config(discord_webhook_url="https://example.invalid/hook")
        status = load_fixture("spf_lost_device.json")
        with TemporaryDirectory() as tmpdir:
            p1, p2 = state_patches(tmpdir)
            with p1, p2, redirect_stdout(StringIO()), patch(
                "growatt_guard.modes.append_mode_audit"
            ), patch("growatt_guard.notifications.send_discord_embed", return_value=True) as send:
                ensure_device_reporting(config, "auto-topup-check", status)
        fields = {field["name"]: field["value"] for field in send.call_args.args[1]["fields"]}
        self.assertEqual(fields["Last known SOC"], "87% (stale)")

    def test_lost_device_realerts_after_the_re_alert_interval(self):
        config = make_config(discord_webhook_url="https://example.invalid/hook")
        status = load_fixture("spf_lost_device.json")
        base = dt.datetime(2026, 10, 7, 5, 0, 0, tzinfo=dt.timezone.utc)
        clock = {"now": base}

        def fake_now():
            return clock["now"]

        with TemporaryDirectory() as tmpdir:
            p1, p2 = state_patches(tmpdir)
            with p1, p2, redirect_stdout(StringIO()), patch(
                "growatt_guard.modes.append_mode_audit"
            ), patch(
                "growatt_guard.notifications.utc_now", side_effect=fake_now
            ), patch("growatt_guard.notifications.send_discord_embed", return_value=True) as send:
                ensure_device_reporting(config, "auto-topup-check", status)
                ensure_device_reporting(config, "auto-topup-check", status)
                self.assertEqual(send.call_count, 1)
                clock["now"] = base + dt.timedelta(minutes=61)
                ensure_device_reporting(config, "auto-topup-check", status)

        self.assertEqual(send.call_count, 2)
        titles = [call.args[1]["title"] for call in send.call_args_list]
        self.assertNotIn("still", titles[0])
        self.assertIn("still not reporting", titles[1])


class ObservabilityDeviceTrackingTests(unittest.TestCase):
    def test_refresh_observability_tracks_device_reporting(self):
        config = make_config()
        status = load_fixture("spf_lost_device.json")
        with TemporaryDirectory() as tmpdir:
            with patch(
                "growatt_guard.dashboard_service.load_context",
                return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
            ), patch(
                "growatt_guard.dashboard_service.write_dashboard_from_status",
                return_value=Path(tmpdir) / "dashboard.html",
            ), patch(
                "growatt_guard.dashboard_service.publish_pvoutput_status_from_status",
                return_value=(True, "PVOutput OK"),
            ), patch("growatt_guard.dashboard_service.track_device_reporting") as track:
                refresh_observability_once(config, str(Path(tmpdir) / "dashboard.html"))

        track.assert_called_once_with(config, "observability-refresh", status)


class BatteryAlertStaleTests(unittest.TestCase):
    def test_battery_alert_skips_stale_soc(self):
        config = make_config(discord_webhook_url="https://example.invalid/hook")
        status = load_fixture("spf_lost_device.json")
        with TemporaryDirectory() as tmpdir:
            p1, p2 = state_patches(tmpdir)
            with p1, p2, patch(
                "growatt_guard.alerts.load_context", return_value=(None, None, status)
            ), patch("growatt_guard.alerts.battery_alert_is_muted", return_value=False), patch(
                "growatt_guard.notifications.send_discord_embed", return_value=True
            ) as send, redirect_stdout(StringIO()) as out:
                rc = command_battery_alert(config)
        self.assertEqual(rc, 0)
        self.assertIn("skipping battery alert", out.getvalue())
        titles = [call.args[1]["title"] for call in send.call_args_list]
        self.assertTrue(all("Battery" not in t for t in titles), titles)


class HealthCheckStaleTests(unittest.TestCase):
    def test_health_check_warns_when_inverter_is_not_reporting(self):
        config = make_config(dry_run=False, discord_webhook_url="")
        status = load_fixture("spf_lost_device.json")
        schedule = {
            "timezone": "Africa/Lagos",
            "jobs": [{"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"}],
        }
        next_runs = [(dt.datetime(2026, 8, 31, 6, 30), schedule["jobs"][0])]

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.PAUSE_FILE", Path(tmpdir) / "pause.json"
        ), patch(
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
        ), patch("growatt_guard.health.next_scheduled_runs", return_value=next_runs), patch(
            "growatt_guard.health.load_context",
            return_value=(None, DeviceRef("plant123", "SN123", "storage", {}), status),
        ), patch(
            "growatt_guard.health.choose_preserve_threshold",
            return_value=ThresholdDecision(50, "weather disabled; using fixed threshold 50%"),
        ), patch("growatt_guard.health.read_pause_state", return_value=None), redirect_stdout(
            StringIO()
        ) as stdout:
            (Path(tmpdir) / "dashboard.html").write_text("<html></html>", encoding="utf-8")
            command_health_check(config)

        output = stdout.getvalue()
        # WARN rather than FAIL so a real outage cannot block deployments.
        self.assertIn("Result: WARN", output)
        self.assertIn("[WARN] Inverter reporting:", output)
        self.assertIn("2026-08-28 13:55:51", output)
        # The frozen numbers must not be presented as healthy readings.
        self.assertIn("[WARN] Battery SOC:", output)
        self.assertIn("[WARN] Output source:", output)


if __name__ == "__main__":
    unittest.main()
