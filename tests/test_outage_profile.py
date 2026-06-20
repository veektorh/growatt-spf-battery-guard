import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import growatt_power_guard  # noqa: F401 — ensures app_module() resolves GrowattGuardError
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.schedule import command_outage_profile


FAKE_SCHEDULE = {
    "timezone": "Africa/Lagos",
    "jobs": [
        {"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"},
        {"id": "morning-health", "cron": "10 6 * * *", "command": "health-check", "args": ["--notify"]},
        {"id": "morning-return-sbu", "cron": "55 7 * * *", "command": "return-sbu"},
        {"id": "watchdog", "cron": "*/30 * * * *", "command": "watchdog-sbu"},
    ],
}


def _apply_args(profile_name: str, dates: list[str], note: str = "") -> SimpleNamespace:
    return SimpleNamespace(outage_subcommand="apply", profile_name=profile_name, dates=dates, note=note)


class OutageProfileListTests(unittest.TestCase):
    def test_list_shows_all_builtin_profiles(self):
        buf = StringIO()
        with redirect_stdout(buf):
            rc = command_outage_profile(None, SimpleNamespace(outage_subcommand="list"))
        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("skip-all", output)
        self.assertIn("maintenance", output)
        self.assertIn("health-only", output)


class OutageProfileApplyTests(unittest.TestCase):
    def _patch(self, tmpdir: str):
        override_file = Path(tmpdir) / "schedule_overrides.json"
        return (
            patch("growatt_guard.schedule.SCHEDULE_OVERRIDES_FILE", override_file),
            patch("growatt_guard.schedule.validate_schedule", return_value=FAKE_SCHEDULE),
            override_file,
        )

    def test_apply_skip_all_sets_flag(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, override_file = self._patch(tmpdir)
            with p1, p2:
                rc = command_outage_profile(None, _apply_args("skip-all", ["2026-07-01"]))
            self.assertEqual(rc, 0)
            data = json.loads(override_file.read_text(encoding="utf-8"))
            self.assertTrue(data["dates"]["2026-07-01"]["skip_all"])

    def test_apply_maintenance_is_alias_for_skip_all(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, override_file = self._patch(tmpdir)
            with p1, p2:
                rc = command_outage_profile(None, _apply_args("maintenance", ["2026-07-04"], note="Public holiday"))
            self.assertEqual(rc, 0)
            data = json.loads(override_file.read_text(encoding="utf-8"))
            entry = data["dates"]["2026-07-04"]
            self.assertTrue(entry["skip_all"])
            self.assertEqual(entry["note"], "Public holiday")

    def test_apply_health_only_replaces_mode_changing_jobs(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, override_file = self._patch(tmpdir)
            with p1, p2:
                rc = command_outage_profile(None, _apply_args("health-only", ["2026-07-02"]))
            self.assertEqual(rc, 0)
            data = json.loads(override_file.read_text(encoding="utf-8"))
            replace_map = data["dates"]["2026-07-02"]["replace"]
            # Mode-changing jobs should be replaced
            self.assertIn("morning-preserve", replace_map)
            self.assertIn("morning-return-sbu", replace_map)
            self.assertIn("watchdog", replace_map)
            # health-check job should NOT be replaced
            self.assertNotIn("morning-health", replace_map)
            self.assertEqual(replace_map["morning-preserve"]["command"], "health-check")

    def test_apply_profile_to_multiple_dates(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, override_file = self._patch(tmpdir)
            with p1, p2:
                rc = command_outage_profile(None, _apply_args("skip-all", ["2026-07-01", "2026-07-02", "2026-07-03"]))
            self.assertEqual(rc, 0)
            data = json.loads(override_file.read_text(encoding="utf-8"))
            for date_str in ["2026-07-01", "2026-07-02", "2026-07-03"]:
                self.assertTrue(data["dates"][date_str]["skip_all"])

    def test_apply_unknown_profile_raises_error(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, _ = self._patch(tmpdir)
            with p1, p2, self.assertRaises(GrowattGuardError):
                command_outage_profile(None, _apply_args("nonexistent", ["2026-07-01"]))

    def test_apply_invalid_date_raises_error(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, _ = self._patch(tmpdir)
            with p1, p2, self.assertRaises(GrowattGuardError):
                command_outage_profile(None, _apply_args("skip-all", ["not-a-date"]))


if __name__ == "__main__":
    unittest.main()
