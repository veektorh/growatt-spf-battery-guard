"""Tests for the Growatt login circuit breaker (account-lock 507 backoff)."""
import datetime as dt
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_guard import state as state_mod
from growatt_guard.growatt_api import (
    connect,
    login_response_is_locked,
    parse_lock_hours,
)
from growatt_guard.exceptions import GrowattGuardError


LOCK_RESPONSE = {
    "msg": "507",
    "lockDuration": "24",
    "success": False,
    "error": "Current account has been locked for 24 hours",
}


class _FakeApi:
    def __init__(self, login_result):
        self._login_result = login_result
        self.server_url = None
        self.login_calls = 0

    def login(self, username, password):
        self.login_calls += 1
        return self._login_result


class _FakeServer:
    def __init__(self, login_result):
        self.api = _FakeApi(login_result)
        self.constructed = 0

    def GrowattApi(self, **kwargs):
        self.constructed += 1
        return self.api


class ParsingTests(unittest.TestCase):
    def test_detects_507(self):
        self.assertTrue(login_response_is_locked(LOCK_RESPONSE))

    def test_detects_locked_text_without_507(self):
        self.assertTrue(login_response_is_locked({"success": False, "error": "Account LOCKED"}))

    def test_ignores_ordinary_failure(self):
        self.assertFalse(login_response_is_locked({"success": False, "msg": "501"}))

    def test_parse_lock_hours_defaults_on_garbage(self):
        self.assertEqual(parse_lock_hours("24"), 24.0)
        self.assertEqual(parse_lock_hours(None), 24.0)
        self.assertEqual(parse_lock_hours("nonsense"), 24.0)
        self.assertEqual(parse_lock_hours("0"), 24.0)


class CircuitBreakerTests(unittest.TestCase):
    def _patch_state(self, tmpdir):
        return patch.object(state_mod, "LOGIN_COOLDOWN_FILE", Path(tmpdir) / "cooldown.json")

    def test_lock_response_sets_cooldown(self):
        config = make_config()
        fake = _FakeServer(LOCK_RESPONSE)
        with TemporaryDirectory() as tmpdir, self._patch_state(tmpdir), \
             patch("growatt_guard.growatt_api.growattServer", fake), \
             patch("growatt_guard.growatt_api.require_dependencies"):
            with self.assertRaises(GrowattGuardError):
                connect(config)
            # A cooldown was written, dated ~24h out.
            until = state_mod.login_cooldown_until()
            self.assertIsNotNone(until)
            self.assertGreater(until, state_mod.utc_now() + dt.timedelta(hours=23))

    def test_active_cooldown_skips_login_entirely(self):
        config = make_config()
        fake = _FakeServer(LOCK_RESPONSE)
        with TemporaryDirectory() as tmpdir, self._patch_state(tmpdir), \
             patch("growatt_guard.growatt_api.growattServer", fake), \
             patch("growatt_guard.growatt_api.require_dependencies"):
            state_mod.write_login_cooldown_state(
                state_mod.utc_now() + dt.timedelta(hours=5), "test lock"
            )
            with self.assertRaises(GrowattGuardError) as ctx:
                connect(config)
            self.assertIn("login skipped", str(ctx.exception).lower())
            # The key guarantee: no API object built, no login attempted.
            self.assertEqual(fake.constructed, 0)
            self.assertEqual(fake.api.login_calls, 0)

    def test_expired_cooldown_allows_login_and_clears_on_success(self):
        config = make_config()
        fake = _FakeServer({"success": True, "userId": "u1"})
        with TemporaryDirectory() as tmpdir, self._patch_state(tmpdir), \
             patch("growatt_guard.growatt_api.growattServer", fake), \
             patch("growatt_guard.growatt_api.require_dependencies"):
            # Cooldown already in the past -> must not block, and a successful
            # login must clear any cooldown file.
            state_mod.write_login_cooldown_state(
                state_mod.utc_now() - dt.timedelta(minutes=1), "stale lock"
            )
            api, login_response = connect(config)
            self.assertEqual(fake.api.login_calls, 1)
            self.assertTrue(login_response["success"])
            self.assertIsNone(state_mod.login_cooldown_until())


class ClearCommandTests(unittest.TestCase):
    def test_clear_login_cooldown_command(self):
        from growatt_guard.pause import command_clear_login_cooldown

        config = make_config()
        with TemporaryDirectory() as tmpdir, \
             patch.object(state_mod, "LOGIN_COOLDOWN_FILE", Path(tmpdir) / "cooldown.json"):
            state_mod.write_login_cooldown_state(
                state_mod.utc_now() + dt.timedelta(hours=10), "test"
            )
            with redirect_stdout(StringIO()):
                rc = command_clear_login_cooldown(config)
            self.assertEqual(rc, 0)
            self.assertIsNone(state_mod.login_cooldown_until())

            # No cooldown -> still succeeds, reports nothing to clear.
            with redirect_stdout(StringIO()) as out:
                rc = command_clear_login_cooldown(config)
            self.assertEqual(rc, 0)
            self.assertIn("no active", out.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
