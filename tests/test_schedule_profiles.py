import datetime as dt
import http.client
import http.server
import json
import subprocess
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
import growatt_power_guard  # noqa: F401 — ensures app_module() resolves GrowattGuardError
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.schedule import (
    check_cron_schedule,
    format_outage_windows,
    load_schedule_document,
    outages_for_date,
    validate_schedule,
)
from growatt_guard.schedule_profiles import command_profiles, command_set_profile
from growatt_guard.schedule_views import build_schedule_preview_payload
from growatt_guard.dashboard_service import make_dashboard_handler
from growatt_guard.dashboard import build_dashboard_html
from growatt_guard.weather import ThresholdDecision


PROFILE_SCHEDULE = {
    "timezone": "Africa/Lagos",
    "active_profile": "current",
    "jobs": [
        {"id": "daily-summary", "cron": "0 21 * * *", "command": "daily-summary"},
        {"id": "battery-alert", "cron": "*/30 * * * *", "command": "battery-alert"},
    ],
    "profiles": {
        "current": {
            "label": "Current plan",
            "description": "weekday cuts",
            "outages": [
                {"days": "weekdays", "start": "08:00", "end": "10:30"},
                {"days": "weekdays", "start": "15:30", "end": "18:00"},
            ],
            "jobs": [
                {"id": "afternoon-return-sbu", "cron": "25 15 * * *", "command": "return-sbu"},
            ],
        },
        "reduced": {
            "label": "Reduced plan",
            "description": "16h weekday, 18h weekend",
            "outages": [
                {"days": "weekdays", "start": "08:00", "end": "12:00"},
                {"days": "weekdays", "start": "14:00", "end": "18:00"},
                {"days": "weekends", "start": "08:00", "end": "11:00"},
                {"days": "weekends", "start": "15:00", "end": "18:00"},
            ],
            "jobs": [
                {"id": "afternoon-return-sbu", "cron": "55 13 * * 1-5", "command": "return-sbu"},
                {"id": "weekend-return-sbu", "cron": "55 14 * * 0,6", "command": "return-sbu"},
            ],
        },
    },
}


def _write_schedule(tmpdir: str) -> Path:
    path = Path(tmpdir) / "schedule.json"
    path.write_text(json.dumps(PROFILE_SCHEDULE), encoding="utf-8")
    return path


class ProfileValidationTests(unittest.TestCase):
    def test_active_profile_merges_shared_and_profile_jobs(self):
        with TemporaryDirectory() as tmpdir:
            path = _write_schedule(tmpdir)
            with patch("growatt_guard.schedule.read_schedule_profile_state", return_value=None):
                schedule = validate_schedule(path)

        job_ids = [job["id"] for job in schedule["jobs"]]
        self.assertEqual(job_ids, ["daily-summary", "battery-alert", "afternoon-return-sbu"])
        self.assertEqual(schedule["active_profile"], "current")
        self.assertEqual(len(schedule["profiles"]), 2)

    def test_switched_state_overrides_json_default(self):
        with TemporaryDirectory() as tmpdir:
            path = _write_schedule(tmpdir)
            with patch(
                "growatt_guard.schedule.read_schedule_profile_state",
                return_value={"name": "reduced"},
            ):
                schedule = validate_schedule(path)

        job_ids = [job["id"] for job in schedule["jobs"]]
        self.assertEqual(schedule["active_profile"], "reduced")
        self.assertIn("weekend-return-sbu", job_ids)

    def test_unknown_switched_state_falls_back_to_default(self):
        with TemporaryDirectory() as tmpdir:
            path = _write_schedule(tmpdir)
            with patch(
                "growatt_guard.schedule.read_schedule_profile_state",
                return_value={"name": "missing"},
            ):
                schedule = validate_schedule(path)

        self.assertEqual(schedule["active_profile"], "current")

    def test_explicit_profile_selects_that_profile(self):
        with TemporaryDirectory() as tmpdir:
            path = _write_schedule(tmpdir)
            schedule = validate_schedule(path, profile="reduced")

        self.assertEqual(schedule["active_profile"], "reduced")
        self.assertEqual(schedule["jobs"][-1]["cron"], "55 14 * * 0,6")

    def test_explicit_unknown_profile_is_rejected(self):
        with TemporaryDirectory() as tmpdir:
            path = _write_schedule(tmpdir)

            with self.assertRaises(GrowattGuardError):
                validate_schedule(path, profile="missing")

    def test_profile_cannot_reuse_a_shared_job_id(self):
        raw = json.loads(json.dumps(PROFILE_SCHEDULE))
        raw["profiles"]["current"]["jobs"].append(
            {"id": "daily-summary", "cron": "0 20 * * *", "command": "daily-summary"}
        )
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "schedule.json"
            path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaises(GrowattGuardError):
                load_schedule_document(path)

    def test_overlapping_outage_windows_are_rejected(self):
        raw = json.loads(json.dumps(PROFILE_SCHEDULE))
        raw["profiles"]["current"]["outages"].append(
            {"days": "daily", "start": "10:00", "end": "11:00"}
        )
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "schedule.json"
            path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaises(GrowattGuardError):
                load_schedule_document(path)

    def test_outages_are_filtered_by_day_type(self):
        windows = PROFILE_SCHEDULE["profiles"]["reduced"]["outages"]

        weekday = [w["start"] for w in outages_for_date(windows, dt.date(2026, 11, 9))]
        weekend = [w["start"] for w in outages_for_date(windows, dt.date(2026, 11, 14))]

        self.assertEqual(weekday, ["08:00", "14:00"])
        self.assertEqual(weekend, ["08:00", "15:00"])
        self.assertEqual(
            format_outage_windows(windows),
            "weekdays 08:00-12:00, 14:00-18:00; weekends 08:00-11:00, 15:00-18:00",
        )


class ProfileCommandTests(unittest.TestCase):
    def test_profiles_lists_active_and_available(self):
        payload = {
            "active_profile": "current",
            "label": "Current plan",
            "description": "weekday cuts",
            "outages": [],
            "outages_text": "weekdays 08:00-10:30",
            "profiles": [
                {
                    "name": "current",
                    "label": "Current plan",
                    "description": "weekday cuts",
                    "outages": [],
                    "outages_text": "weekdays 08:00-10:30",
                    "active": True,
                },
                {
                    "name": "reduced",
                    "label": "Reduced plan",
                    "description": "16h weekday, 18h weekend",
                    "outages": [],
                    "outages_text": "weekdays 08:00-12:00",
                    "active": False,
                },
            ],
        }
        with patch(
            "growatt_guard.schedule_profiles.build_profiles_payload", return_value=payload
        ), redirect_stdout(StringIO()) as stdout:
            result = command_profiles(make_config())

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("Active profile: Current plan (current)", output)
        self.assertIn("* current", output)
        self.assertIn("Reduced plan", output)

    def test_set_profile_writes_state_and_reports_outages(self):
        config = make_config()
        with TemporaryDirectory() as tmpdir:
            document = load_schedule_document(_write_schedule(tmpdir))
            with patch(
                "growatt_guard.schedule_profiles.load_schedule_document", return_value=document
            ), patch(
                "growatt_guard.schedule_profiles.write_schedule_profile_state"
            ) as write_state, patch(
                "growatt_guard.schedule_profiles.send_discord_message"
            ) as send_message, patch(
                "growatt_guard.schedule_profiles.outage_mode", return_value="all"
            ), redirect_stdout(StringIO()) as stdout:
                result = command_set_profile(config, "reduced", reason="estate notice")

        self.assertEqual(result, 0)
        write_state.assert_called_once_with("reduced", "estate notice")
        self.assertIn("weekends 08:00-11:00", stdout.getvalue())
        send_message.assert_called_once()

    def test_set_profile_warns_when_weekday_only_mode_would_skip_weekend_jobs(self):
        config = make_config()
        with TemporaryDirectory() as tmpdir:
            document = load_schedule_document(_write_schedule(tmpdir))
            with patch(
                "growatt_guard.schedule_profiles.load_schedule_document", return_value=document
            ), patch("growatt_guard.schedule_profiles.write_schedule_profile_state"), patch(
                "growatt_guard.schedule_profiles.send_discord_message"
            ), patch(
                "growatt_guard.schedule_profiles.outage_mode", return_value="weekdays"
            ), redirect_stdout(StringIO()) as stdout:
                command_set_profile(config, "reduced")

        self.assertIn("set-outage-days all", stdout.getvalue())

    def test_set_profile_rejects_unknown_name(self):
        config = make_config()
        with TemporaryDirectory() as tmpdir:
            document = load_schedule_document(_write_schedule(tmpdir))
            with patch(
                "growatt_guard.schedule_profiles.load_schedule_document", return_value=document
            ):
                with self.assertRaises(GrowattGuardError):
                    command_set_profile(config, "missing")


class ProfileScheduledRunTests(unittest.TestCase):
    def test_inactive_profile_job_is_skipped(self):
        from growatt_power_guard import command_run_scheduled

        config = make_config()
        schedule = {
            "timezone": "Africa/Lagos",
            "active_profile": "current",
            "jobs": [{"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"}],
            "profiles": {},
        }
        with patch("growatt_guard.modes.validate_schedule", return_value=schedule), redirect_stdout(
            StringIO()
        ) as stdout:
            result = command_run_scheduled(
                config, "morning-preserve", dry_plan=True, profile="reduced"
            )

        self.assertEqual(result, 0)
        self.assertIn("is not active", stdout.getvalue())


class ProfileCronTests(unittest.TestCase):
    def _cron_text(self, schedule: dict) -> str:
        lines = ["CRON_TZ=Africa/Lagos"]
        for job in schedule["shared_jobs"]:
            lines.append(
                f"{job['cron']} cd /x && python run-scheduled {job['id']} "
                ">> logs/cron.log 2>&1 # growatt-power-guard"
            )
        for name, body in schedule["profiles"].items():
            for job in body["jobs"]:
                lines.append(
                    f"{job['cron']} cd /x && python run-scheduled --profile {name} "
                    f"{job['id']} >> logs/cron.log 2>&1 # growatt-power-guard"
                )
        return "\n".join(lines)

    def test_cron_check_requires_every_profile_variant(self):
        with TemporaryDirectory() as tmpdir:
            schedule = validate_schedule(_write_schedule(tmpdir), profile="current")
        with patch("growatt_guard.schedule.os.name", "posix"), patch(
            "growatt_guard.schedule.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["crontab", "-l"], 0, stdout=self._cron_text(schedule), stderr=""
            ),
        ):
            checks = check_cron_schedule(schedule)

        self.assertTrue(all(check.status == "OK" for check in checks), checks)

    def test_cron_check_reports_missing_inactive_profile_job(self):
        with TemporaryDirectory() as tmpdir:
            schedule = validate_schedule(_write_schedule(tmpdir), profile="current")
        cron_text = self._cron_text(schedule)
        cron_text = "\n".join(
            line for line in cron_text.splitlines() if "weekend-return-sbu" not in line
        )
        with patch("growatt_guard.schedule.os.name", "posix"), patch(
            "growatt_guard.schedule.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["crontab", "-l"], 0, stdout=cron_text, stderr=""
            ),
        ):
            checks = check_cron_schedule(schedule)

        cron_check = next(check for check in checks if check.name == "Cron jobs")
        self.assertEqual(cron_check.status, "FAIL")
        self.assertIn("weekend-return-sbu", cron_check.detail)


class ProfilePreviewTests(unittest.TestCase):
    def test_preview_payload_includes_profile_and_day_outages(self):
        with TemporaryDirectory() as tmpdir:
            schedule = validate_schedule(_write_schedule(tmpdir), profile="reduced")
        payload = build_schedule_preview_payload(
            schedule, {"dates": {}}, days=7, today=dt.date(2026, 11, 9)
        )

        self.assertEqual(payload["active_profile"], "reduced")
        weekday = next(day for day in payload["dates"] if day["date"] == "2026-11-09")
        saturday = next(day for day in payload["dates"] if day["date"] == "2026-11-14")
        self.assertEqual(weekday["outages"], ["08:00-12:00", "14:00-18:00"])
        self.assertEqual(saturday["outages"], ["08:00-11:00", "15:00-18:00"])


class DashboardProfileToggleTests(unittest.TestCase):
    def _serve(self, tmpdir: str):
        output = Path(tmpdir) / "dashboard.html"
        output.write_text("<html>dashboard</html>", encoding="utf-8")
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), make_dashboard_handler(output)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def _request(self, server, method: str, path: str, body: str = "", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_post_switches_active_profile_and_redirects(self):
        document = {"profiles": {"current": {}, "reduced": {}}}
        with TemporaryDirectory() as tmpdir:
            server = self._serve(tmpdir)
            with patch(
                "growatt_guard.dashboard_service.load_schedule_document",
                return_value=document,
            ), patch(
                "growatt_guard.dashboard_service.write_schedule_profile_state"
            ) as write_state:
                status, _ = self._request(
                    server,
                    "POST",
                    "/profile",
                    body="name=reduced",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

        self.assertEqual(status, 303)
        write_state.assert_called_once_with("reduced", "dashboard toggle")

    def test_post_rejects_unknown_profile(self):
        document = {"profiles": {"current": {}}}
        with TemporaryDirectory() as tmpdir:
            server = self._serve(tmpdir)
            with patch(
                "growatt_guard.dashboard_service.load_schedule_document",
                return_value=document,
            ), patch(
                "growatt_guard.dashboard_service.write_schedule_profile_state"
            ) as write_state:
                status, _ = self._request(server, "POST", "/profile", body="name=missing")

        self.assertEqual(status, 400)
        write_state.assert_not_called()

    def test_post_rejects_cross_origin_switch(self):
        with TemporaryDirectory() as tmpdir:
            server = self._serve(tmpdir)
            status, _ = self._request(
                server,
                "POST",
                "/profile",
                body="name=reduced",
                headers={"Origin": "https://evil.example"},
            )

        self.assertEqual(status, 403)

    def test_api_profile_returns_active_profile_payload(self):
        payload = {
            "active_profile": "reduced",
            "label": "Reduced plan",
            "description": "16h weekday",
            "outages": [],
            "outages_text": "none",
            "profiles": [],
        }
        with TemporaryDirectory() as tmpdir:
            server = self._serve(tmpdir)
            with patch(
                "growatt_guard.dashboard_service.build_profiles_payload",
                return_value=payload,
            ):
                status, body = self._request(server, "GET", "/api/profile")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), payload)

    def test_dashboard_renders_profile_card_and_toggle_forms(self):
        with TemporaryDirectory() as tmpdir:
            schedule = validate_schedule(_write_schedule(tmpdir), profile="current")
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
        }

        html = build_dashboard_html(
            status, schedule, {"dates": {}}, ThresholdDecision(50, "test")
        )

        self.assertIn('id="estate-profile"', html)
        self.assertIn('action="/profile"', html)
        self.assertIn("Current plan", html)
        self.assertIn("Reduced plan", html)
        self.assertIn("profile-button", html)


if __name__ == "__main__":
    unittest.main()
