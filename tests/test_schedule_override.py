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
from growatt_guard.schedule_overrides import command_schedule_override


FAKE_SCHEDULE = {
    "timezone": "Africa/Lagos",
    "jobs": [
        {"id": "morning-preserve", "cron": "30 6 * * *", "command": "preserve-battery"},
        {"id": "morning-health", "cron": "10 6 * * *", "command": "health-check", "args": ["--notify"]},
    ],
}


def _args(subcommand: str, **kwargs) -> SimpleNamespace:
    return SimpleNamespace(override_subcommand=subcommand, **kwargs)


def _patch_files(tmpdir: str):
    override_file = Path(tmpdir) / "schedule_overrides.json"
    return (
        patch("growatt_guard.schedule_overrides.SCHEDULE_OVERRIDES_FILE", override_file),
        patch("growatt_guard.schedule_overrides.validate_schedule", return_value=FAKE_SCHEDULE),
        override_file,
    )


class ScheduleOverrideListTests(unittest.TestCase):
    def test_list_empty_when_no_file(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, _ = _patch_files(tmpdir)
            buf = StringIO()
            with p1, p2, redirect_stdout(buf):
                rc = command_schedule_override(None, _args("list", date=""))
            self.assertEqual(rc, 0)
            self.assertIn("No schedule overrides", buf.getvalue())

    def test_list_shows_existing_entries(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, override_file = _patch_files(tmpdir)
            override_file.write_text(
                json.dumps({"dates": {"2026-07-01": {"skip": ["morning-preserve"], "note": "Maintenance"}}}),
                encoding="utf-8",
            )
            buf = StringIO()
            with p1, p2, redirect_stdout(buf):
                rc = command_schedule_override(None, _args("list", date=""))
            self.assertEqual(rc, 0)
            output = buf.getvalue()
            self.assertIn("2026-07-01", output)
            self.assertIn("Maintenance", output)
            self.assertIn("morning-preserve", output)

    def test_list_filters_by_date(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, override_file = _patch_files(tmpdir)
            override_file.write_text(
                json.dumps({
                    "dates": {
                        "2026-07-01": {"skip": ["morning-preserve"]},
                        "2026-07-02": {"skip_all": True},
                    }
                }),
                encoding="utf-8",
            )
            buf = StringIO()
            with p1, p2, redirect_stdout(buf):
                rc = command_schedule_override(None, _args("list", date="2026-07-01"))
            self.assertEqual(rc, 0)
            output = buf.getvalue()
            self.assertIn("2026-07-01", output)
            self.assertNotIn("2026-07-02", output)


class ScheduleOverrideAddTests(unittest.TestCase):
    def test_add_skip_creates_file_with_entry(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, override_file = _patch_files(tmpdir)
            with p1, p2:
                rc = command_schedule_override(
                    None, _args("add-skip", date="2026-07-01", job_id="morning-preserve", note="Maintenance")
                )
            self.assertEqual(rc, 0)
            data = json.loads(override_file.read_text(encoding="utf-8"))
            self.assertIn("morning-preserve", data["dates"]["2026-07-01"]["skip"])
            self.assertEqual(data["dates"]["2026-07-01"]["note"], "Maintenance")

    def test_add_skip_all_sets_flag(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, override_file = _patch_files(tmpdir)
            with p1, p2:
                rc = command_schedule_override(
                    None, _args("add-skip-all", date="2026-07-04", note="Holiday")
                )
            self.assertEqual(rc, 0)
            data = json.loads(override_file.read_text(encoding="utf-8"))
            self.assertTrue(data["dates"]["2026-07-04"]["skip_all"])
            self.assertEqual(data["dates"]["2026-07-04"]["note"], "Holiday")

    def test_add_replace_writes_replacement(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, override_file = _patch_files(tmpdir)
            with p1, p2:
                rc = command_schedule_override(
                    None,
                    _args(
                        "add-replace",
                        date="2026-07-02",
                        job_id="morning-preserve",
                        replacement_command="health-check",
                        replacement_args=["--notify"],
                        note="",
                    ),
                )
            self.assertEqual(rc, 0)
            data = json.loads(override_file.read_text(encoding="utf-8"))
            repl = data["dates"]["2026-07-02"]["replace"]["morning-preserve"]
            self.assertEqual(repl["command"], "health-check")
            self.assertEqual(repl["args"], ["--notify"])

    def test_add_skip_duplicate_is_idempotent(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, override_file = _patch_files(tmpdir)
            with p1, p2:
                command_schedule_override(None, _args("add-skip", date="2026-07-01", job_id="morning-preserve", note=""))
                rc = command_schedule_override(None, _args("add-skip", date="2026-07-01", job_id="morning-preserve", note=""))
            self.assertEqual(rc, 0)
            data = json.loads(override_file.read_text(encoding="utf-8"))
            self.assertEqual(data["dates"]["2026-07-01"]["skip"].count("morning-preserve"), 1)

    def test_add_skip_invalid_job_fails_validation(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, _ = _patch_files(tmpdir)
            with p1, p2, self.assertRaises(GrowattGuardError):
                command_schedule_override(
                    None, _args("add-skip", date="2026-07-01", job_id="nonexistent-job", note="")
                )


class ScheduleOverrideRemoveTests(unittest.TestCase):
    def test_remove_specific_job_from_skip(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, override_file = _patch_files(tmpdir)
            override_file.write_text(
                json.dumps({
                    "dates": {"2026-07-01": {"skip": ["morning-preserve", "morning-health"], "note": "test"}}
                }),
                encoding="utf-8",
            )
            with p1, p2:
                rc = command_schedule_override(
                    None, _args("remove", date="2026-07-01", job_id="morning-preserve")
                )
            self.assertEqual(rc, 0)
            data = json.loads(override_file.read_text(encoding="utf-8"))
            self.assertNotIn("morning-preserve", data["dates"]["2026-07-01"]["skip"])
            self.assertIn("morning-health", data["dates"]["2026-07-01"]["skip"])

    def test_remove_entire_date(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, override_file = _patch_files(tmpdir)
            override_file.write_text(
                json.dumps({"dates": {"2026-07-01": {"skip_all": True, "note": "Holiday"}}}),
                encoding="utf-8",
            )
            with p1, p2:
                rc = command_schedule_override(None, _args("remove", date="2026-07-01", job_id=""))
            self.assertEqual(rc, 0)
            data = json.loads(override_file.read_text(encoding="utf-8"))
            self.assertNotIn("2026-07-01", data["dates"])

    def test_remove_last_job_cleans_empty_entry(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, override_file = _patch_files(tmpdir)
            override_file.write_text(
                json.dumps({"dates": {"2026-07-01": {"skip": ["morning-preserve"]}}}),
                encoding="utf-8",
            )
            with p1, p2:
                command_schedule_override(None, _args("remove", date="2026-07-01", job_id="morning-preserve"))
            data = json.loads(override_file.read_text(encoding="utf-8"))
            self.assertNotIn("2026-07-01", data["dates"])

    def test_remove_nonexistent_date_prints_message(self):
        with TemporaryDirectory() as tmpdir:
            p1, p2, _ = _patch_files(tmpdir)
            buf = StringIO()
            with p1, p2, redirect_stdout(buf):
                rc = command_schedule_override(None, _args("remove", date="2026-07-01", job_id=""))
            self.assertEqual(rc, 0)
            self.assertIn("No overrides found", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
