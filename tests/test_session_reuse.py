"""Tests for Growatt session reuse (skip the rate-limited login endpoint)."""
import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from helpers import make_config
from growatt_guard import state as state_mod
from growatt_guard.growatt_api import connect, load_context
from growatt_guard.exceptions import GrowattGuardError


class _FakeApi:
    def __init__(self, login_result):
        self._login_result = login_result
        self.server_url = None
        self.login_calls = 0
        self.session = requests.Session()

    def login(self, username, password):
        self.login_calls += 1
        self.session.cookies.set("JSESSIONID", "tok123")
        return self._login_result


class _FakeServer:
    def __init__(self, login_result):
        self.api = _FakeApi(login_result)

    def GrowattApi(self, **kwargs):
        return self.api


SUCCESS = {"success": True, "userId": "u1", "userLevel": 1,
           "user": {"id": "u1", "password": "SECRET_HASH", "token": "SECRET_TOKEN"}}


class SessionReuseTests(unittest.TestCase):
    def _ctx(self, tmpdir, fake):
        return (
            patch.object(state_mod, "SESSION_CACHE_FILE", Path(tmpdir) / "session.json"),
            patch.object(state_mod, "LOGIN_COOLDOWN_FILE", Path(tmpdir) / "cooldown.json"),
            patch("growatt_guard.growatt_api.growattServer", fake),
            patch("growatt_guard.growatt_api.require_dependencies"),
        )

    def test_fresh_login_caches_session_without_secrets(self):
        config = make_config(growatt_session_ttl_minutes=60)
        fake = _FakeServer(SUCCESS)
        with TemporaryDirectory() as tmpdir:
            patches = self._ctx(tmpdir, fake)
            for p in patches:
                p.start()
            try:
                connect(config)
                self.assertEqual(fake.api.login_calls, 1)
                cache = state_mod.read_session_cache()
                self.assertEqual(cache["cookies"].get("JSESSIONID"), "tok123")
                # Minimal login_response only — no credentials persisted to disk.
                serialized = repr(cache)
                self.assertNotIn("SECRET_HASH", serialized)
                self.assertNotIn("SECRET_TOKEN", serialized)
                self.assertEqual(cache["login_response"]["userId"], "u1")
            finally:
                for p in patches:
                    p.stop()

    def test_fresh_cache_skips_login(self):
        config = make_config(growatt_session_ttl_minutes=60)
        fake = _FakeServer(SUCCESS)
        with TemporaryDirectory() as tmpdir:
            patches = self._ctx(tmpdir, fake)
            for p in patches:
                p.start()
            try:
                state_mod.write_session_cache({"JSESSIONID": "cached"}, {"success": True, "userId": "u1"})
                api, login_response = connect(config)
                # Login endpoint not touched; cached cookie restored.
                self.assertEqual(fake.api.login_calls, 0)
                self.assertEqual(login_response["userId"], "u1")
                self.assertEqual(api.session.cookies.get("JSESSIONID"), "cached")
            finally:
                for p in patches:
                    p.stop()

    def test_session_beyond_safety_ceiling_triggers_fresh_login(self):
        # Sessions older than the 23h safety ceiling are proactively refreshed.
        # Sessions younger than that are reused and rely on the server to reject
        # stale cookies (HTTPError → load_context retry path).
        config = make_config(growatt_session_ttl_minutes=60)
        fake = _FakeServer(SUCCESS)
        with TemporaryDirectory() as tmpdir:
            patches = self._ctx(tmpdir, fake)
            for p in patches:
                p.start()
            try:
                old = (state_mod.utc_now() - dt.timedelta(hours=24)).isoformat()
                state_mod.write_json_state(
                    state_mod.SESSION_CACHE_FILE,
                    {"cookies": {"JSESSIONID": "old"}, "login_response": {"success": True}, "saved_at": old},
                )
                connect(config)
                self.assertEqual(fake.api.login_calls, 1)
            finally:
                for p in patches:
                    p.stop()

    def test_ttl_zero_disables_reuse(self):
        config = make_config(growatt_session_ttl_minutes=0)
        fake = _FakeServer(SUCCESS)
        with TemporaryDirectory() as tmpdir:
            patches = self._ctx(tmpdir, fake)
            for p in patches:
                p.start()
            try:
                state_mod.write_session_cache({"JSESSIONID": "cached"}, {"success": True, "userId": "u1"})
                connect(config)
                # Reuse disabled -> always logs in, and does not write a cache.
                self.assertEqual(fake.api.login_calls, 1)
            finally:
                for p in patches:
                    p.stop()

    def test_load_context_clears_cache_on_failure(self):
        config = make_config(growatt_session_ttl_minutes=60)
        with TemporaryDirectory() as tmpdir, \
             patch.object(state_mod, "SESSION_CACHE_FILE", Path(tmpdir) / "session.json"), \
             patch("growatt_guard.growatt_api.connect", side_effect=GrowattGuardError("dead session")):
            state_mod.write_session_cache({"JSESSIONID": "stale"}, {"success": True})
            with self.assertRaises(GrowattGuardError):
                load_context(config)
            self.assertIsNone(state_mod.read_session_cache())


if __name__ == "__main__":
    unittest.main()
