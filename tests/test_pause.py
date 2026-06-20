import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_power_guard import (
    GrowattGuardError,
    acquire_command_lock,
    command_clear_stale_lock,
    ensure_not_paused,
    read_pause_state,
    release_command_lock,
    run_with_command_lock,
    write_pause_state,
)


class PauseTests(unittest.TestCase):
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
        ), redirect_stdout(StringIO()) as stdout:
            token = acquire_command_lock("preserve-battery")
            self.assertIsNotNone(token)
            self.assertEqual(run_with_command_lock(config, "return-sbu", lambda: 99), 0)
            release_command_lock(token or "")

        self.assertIn("already running", stdout.getvalue())

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
