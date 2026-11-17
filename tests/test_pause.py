import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_guard.audit import read_mode_audit_rows
from growatt_guard.discord_control import MAX_PAUSE_HOURS
from growatt_power_guard import (
    GrowattGuardError,
    LOCK_SKIP_ACTION,
    LOCKED_COMMANDS,
    acquire_command_lock,
    command_clear_stale_lock,
    ensure_not_paused,
    read_pause_state,
    release_command_lock,
    run_with_command_lock,
    write_pause_state,
)


class PauseTests(unittest.TestCase):
    def test_topup_completion_owns_its_conditional_lock(self):
        self.assertNotIn("topup-complete-check", LOCKED_COMMANDS)

    def test_discord_max_pause_is_accepted_by_pause_state(self):
        # The Discord layer caps duration on its own; the cap is only meaningful
        # if the underlying pause state actually accepts it.
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
            "growatt_guard.state.PAUSE_FILE", Path(tmpdir) / "automation_pause.json"
        ):
            state = write_pause_state(MAX_PAUSE_HOURS, "away")
            read_back = read_pause_state()

        self.assertEqual(MAX_PAUSE_HOURS, 168.0)
        self.assertIsNotNone(read_back)
        self.assertEqual(read_back["reason"], "away")
        self.assertEqual(state["reason"], "away")

    def test_write_and_read_pause_state(self):
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
            "growatt_guard.state.PAUSE_FILE", Path(tmpdir) / "automation_pause.json"
        ):
            state = write_pause_state(1, "testing")
            read_back = read_pause_state()

        self.assertEqual(state["reason"], "testing")
        self.assertIsNotNone(read_back)
        self.assertEqual(read_back["reason"], "testing")

    def test_ensure_not_paused_returns_true_when_paused(self):
        config = make_config(discord_notify_skip=False)
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
            "growatt_guard.state.PAUSE_FILE", Path(tmpdir) / "automation_pause.json"
        ), redirect_stdout(StringIO()):
            write_pause_state(1, "testing")
            self.assertTrue(ensure_not_paused(config, "watchdog-sbu"))

    def test_command_lock_skips_when_busy(self):
        config = make_config(discord_notify_skip=False)
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
            "growatt_guard.state.COMMAND_LOCK_FILE", Path(tmpdir) / "mode_command.lock"
        ), patch("growatt_guard.audit.LOG_DIR", Path(tmpdir)), patch(
            "growatt_guard.audit.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"
        ), redirect_stdout(StringIO()) as stdout:
            token = acquire_command_lock("preserve-battery")
            self.assertIsNotNone(token)
            self.assertEqual(run_with_command_lock(config, "return-sbu", lambda: 99), 0)
            release_command_lock(token or "")
            rows = read_mode_audit_rows()

        self.assertIn("already running", stdout.getvalue())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["command"], "return-sbu")
        self.assertEqual(rows[0]["action"], LOCK_SKIP_ACTION)
        self.assertEqual(rows[0]["result"], "skipped")
        self.assertIn("held_by=preserve-battery", rows[0]["note"])
        self.assertIn("age_min=", rows[0]["note"])
        self.assertIn("held_since=", rows[0]["note"])

    def test_clear_stale_lock_no_lock_file(self):
        config = make_config()
        lock_path = Path("nonexistent_lock_file_xyz.lock")
        with patch("growatt_guard.pause.COMMAND_LOCK_FILE", lock_path), redirect_stdout(StringIO()) as stdout:
            result = command_clear_stale_lock(config)
        self.assertEqual(result, 0)
        self.assertIn("No lock file found", stdout.getvalue())

    def test_clear_stale_lock_removes_stale_lock(self):
        config = make_config()
        lock_state = {"command": "preserve-battery", "created_at": "2026-01-01T00:00:00"}
        with TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "mode_command.lock"
            lock_path.write_text('{"command": "preserve-battery", "created_at": "2026-01-01T00:00:00"}')
            with patch("growatt_guard.pause.COMMAND_LOCK_FILE", lock_path), patch(
                "growatt_guard.pause.command_lock_is_stale", return_value=True
            ), patch("growatt_guard.pause.read_command_lock_state", return_value=lock_state), redirect_stdout(
                StringIO()
            ) as stdout:
                result = command_clear_stale_lock(config)
            self.assertEqual(result, 0)
            self.assertFalse(lock_path.exists())
            self.assertIn("Cleared stale lock", stdout.getvalue())
            self.assertIn("preserve-battery", stdout.getvalue())

    def test_clear_stale_lock_refuses_active_lock(self):
        config = make_config()
        lock_state = {"command": "return-sbu", "created_at": "2026-06-20T10:00:00"}
        with TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "mode_command.lock"
            lock_path.write_text('{"command": "return-sbu", "created_at": "2026-06-20T10:00:00"}')
            with patch("growatt_guard.pause.COMMAND_LOCK_FILE", lock_path), patch(
                "growatt_guard.pause.command_lock_is_stale", return_value=False
            ), patch("growatt_guard.pause.read_command_lock_state", return_value=lock_state), redirect_stdout(
                StringIO()
            ) as stdout:
                result = command_clear_stale_lock(config)
            self.assertEqual(result, 1)
            self.assertTrue(lock_path.exists())
            self.assertIn("active", stdout.getvalue())
            self.assertIn("return-sbu", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()


class CommandLockAtomicityTests(unittest.TestCase):
    """N15: a concurrent reader must never see an empty lock file."""

    def _paths(self, tmpdir):
        return patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
            "growatt_guard.state.COMMAND_LOCK_FILE", Path(tmpdir) / "mode_command.lock"
        )

    def test_lock_holds_its_payload_the_moment_it_appears(self):
        import json
        import os
        from growatt_guard import state

        real_link = os.link
        seen = []

        def link_then_read(src, dst):
            real_link(src, dst)
            # Exactly what a concurrent topup-complete-check would read now.
            seen.append(json.loads(Path(dst).read_text(encoding="utf-8")))

        with TemporaryDirectory() as tmpdir:
            state_patch, lock_patch = self._paths(tmpdir)
            with state_patch, lock_patch, patch("growatt_guard.state.os.link", side_effect=link_then_read):
                token = acquire_command_lock("preserve-battery")
                self.assertEqual(state.read_command_lock_state()["command"], "preserve-battery")
                release_command_lock(token or "")
            leftovers = list(Path(tmpdir).iterdir())

        self.assertIsNotNone(token)
        self.assertEqual(seen[0]["command"], "preserve-battery")
        self.assertEqual(seen[0]["token"], token)
        self.assertEqual(leftovers, [])

    def test_second_acquire_fails_and_leaves_the_holder_intact(self):
        from growatt_guard import state

        with TemporaryDirectory() as tmpdir:
            state_patch, lock_patch = self._paths(tmpdir)
            with state_patch, lock_patch:
                first = acquire_command_lock("preserve-battery")
                second = acquire_command_lock("topup-complete-check")
                holder = state.read_command_lock_state()
                names = sorted(path.name for path in Path(tmpdir).iterdir())
                release_command_lock(first or "")

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(holder["command"], "preserve-battery")
        self.assertEqual(names, ["mode_command.lock"])

    def test_falls_back_to_exclusive_create_without_hard_links(self):
        from growatt_guard import state

        with TemporaryDirectory() as tmpdir:
            state_patch, lock_patch = self._paths(tmpdir)
            with state_patch, lock_patch, patch("growatt_guard.state.os.link", side_effect=PermissionError("no links")):
                token = acquire_command_lock("return-sbu")
                holder = state.read_command_lock_state()
                again = acquire_command_lock("watchdog-sbu")
                names = sorted(path.name for path in Path(tmpdir).iterdir())
                release_command_lock(token or "")

        self.assertIsNotNone(token)
        self.assertEqual(holder["command"], "return-sbu")
        self.assertIsNone(again)
        self.assertEqual(names, ["mode_command.lock"])

    def test_lock_released_before_read_is_named(self):
        config = make_config(discord_notify_skip=False)
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
            "growatt_guard.state.COMMAND_LOCK_FILE", Path(tmpdir) / "mode_command.lock"
        ), patch("growatt_guard.audit.LOG_DIR", Path(tmpdir)), patch(
            "growatt_guard.audit.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"
        ), patch("growatt_guard.pause.acquire_command_lock", return_value=None), redirect_stdout(StringIO()):
            # The holder finished between the failed acquire and the state read.
            self.assertEqual(run_with_command_lock(config, "topup-complete-check", lambda: 99), 0)
            rows = read_mode_audit_rows()

        self.assertEqual(rows[0]["action"], LOCK_SKIP_ACTION)
        self.assertEqual(rows[0]["note"], "held_by=released-before-read")
