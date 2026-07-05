"""Regression tests for the hardening pass:

- atomic state writes (state.write_json_state)
- atomic audit-log pruning (audit.prune_audit_rows)
- topup implied charge rate excludes grid-served load
- auto-topup minute floor never produces a zero-length pause
"""
import csv
import datetime as dt
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_guard.modes import command_topup_complete_check
from growatt_guard.state import (
    STATE_SCHEMA_VERSION,
    acquire_command_lock,
    read_command_lock_state,
    read_json_state,
    release_command_lock,
    topup_skip_notification_due,
    write_json_state,
    write_topup_skip_notification_state,
)


class AtomicStateWriteTests(unittest.TestCase):
    def test_roundtrip_and_no_temp_leftovers(self):
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "sub" / "state.json"
            write_json_state(target, {"b": 2, "a": 1})

            self.assertEqual(read_json_state(target, "test"), {"a": 1, "b": 2})
            # The atomic write must not leave any temp files behind.
            leftovers = [p.name for p in target.parent.iterdir() if p.name != "state.json"]
            self.assertEqual(leftovers, [])

    def test_overwrite_replaces_previous_content(self):
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "state.json"
            write_json_state(target, {"v": 1})
            write_json_state(target, {"v": 2})
            self.assertEqual(read_json_state(target, "test"), {"v": 2})

    def test_state_files_include_version_metadata_on_disk(self):
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "state.json"
            write_json_state(target, {"value": 42})

            raw = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(raw["_schema_version"], STATE_SCHEMA_VERSION)
        self.assertIn("_updated_at", raw)
        self.assertEqual(raw["value"], 42)

    def test_read_json_state_strips_metadata_and_accepts_legacy_files(self):
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "state.json"
            target.write_text(
                json.dumps({"_schema_version": 1, "_updated_at": "2026-07-05T00:00:00+00:00", "value": 42}),
                encoding="utf-8",
            )
            self.assertEqual(read_json_state(target, "test"), {"value": 42})

            legacy = Path(tmpdir) / "legacy.json"
            legacy.write_text(json.dumps({"value": 7}), encoding="utf-8")
            self.assertEqual(read_json_state(legacy, "legacy"), {"value": 7})

    def test_command_lock_files_include_version_metadata(self):
        from growatt_guard import state as state_mod

        original = state_mod.COMMAND_LOCK_FILE
        with TemporaryDirectory() as tmpdir:
            try:
                state_mod.COMMAND_LOCK_FILE = Path(tmpdir) / "mode_command.lock"
                token = acquire_command_lock("test-command")
                self.assertIsNotNone(token)

                raw = json.loads(state_mod.COMMAND_LOCK_FILE.read_text(encoding="utf-8"))

                self.assertEqual(raw["_schema_version"], STATE_SCHEMA_VERSION)
                self.assertEqual(read_command_lock_state()["command"], "test-command")
                release_command_lock(str(token))
                self.assertFalse(state_mod.COMMAND_LOCK_FILE.exists())
            finally:
                state_mod.COMMAND_LOCK_FILE = original


class StateDirectoryIsolationTests(unittest.TestCase):
    def test_unittest_default_state_dir_is_not_live_repo_state(self):
        from growatt_guard import state as state_mod

        live_state_dir = state_mod.BASE_DIR / "state"
        self.assertNotEqual(state_mod.STATE_DIR.resolve(), live_state_dir.resolve())
        self.assertTrue(str(state_mod.STATE_DIR).startswith("/tmp/"), state_mod.STATE_DIR)

    def test_configure_state_dir_updates_all_state_paths(self):
        from growatt_guard import state as state_mod

        original = state_mod.STATE_DIR
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "state"
            try:
                state_mod.configure_state_dir(target)
                self.assertEqual(state_mod.STATE_DIR, target)
                for name in (
                    "PAUSE_FILE",
                    "BATTERY_ALERT_FILE",
                    "BATTERY_ALERT_MUTED_FILE",
                    "BYPASS_ALERT_FILE",
                    "COMMAND_LOCK_FILE",
                    "DASHBOARD_STALE_ALERT_FILE",
                    "GROWATT_CLOUD_FAILURE_FILE",
                    "LOGIN_COOLDOWN_FILE",
                    "SESSION_CACHE_FILE",
                    "SESSION_REFRESH_LOCK_FILE",
                    "TOPUP_STATE_FILE",
                    "TOPUP_SKIP_NOTIFICATION_FILE",
                    "CHARGE_RATE_HISTORY_FILE",
                    "DISCHARGE_RATE_HISTORY_FILE",
                    "RUNTIME_ALERT_FILE",
                    "UTILITY_HOLD_FILE",
                    "WASTE_ALERT_FILE",
                ):
                    self.assertEqual(getattr(state_mod, name).parent, target, name)
            finally:
                state_mod.configure_state_dir(original)


class TopupSkipNotificationThrottleTests(unittest.TestCase):
    def test_repeated_same_key_is_throttled(self):
        from growatt_guard import state as state_mod

        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "TOPUP_SKIP_NOTIFICATION_FILE", Path(tmpdir) / "skip.json"):
                self.assertTrue(topup_skip_notification_due("sunny"))
                write_topup_skip_notification_state("sunny", {"reason": "forecast"})
                self.assertFalse(topup_skip_notification_due("sunny", cooldown_minutes=180))
                self.assertTrue(topup_skip_notification_due("different", cooldown_minutes=180))


class PruneAuditRowsTests(unittest.TestCase):
    def test_keeps_header_and_recent_rows_drops_old(self):
        from growatt_guard import audit

        with TemporaryDirectory() as tmpdir:
            audit_file = Path(tmpdir) / "mode_decisions.csv"
            old = "2020-01-01T00:00:00+00:00"
            recent = "2026-06-20T00:00:00+00:00"
            with audit_file.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=audit.MODE_AUDIT_FIELDS)
                writer.writeheader()
                writer.writerow({f: "" for f in audit.MODE_AUDIT_FIELDS} | {"timestamp": old})
                writer.writerow({f: "" for f in audit.MODE_AUDIT_FIELDS} | {"timestamp": recent})

            cutoff = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_file):
                removed, kept = audit.prune_audit_rows(cutoff)

            self.assertEqual((removed, kept), (1, 1))
            with audit_file.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(list(reader.fieldnames), list(audit.MODE_AUDIT_FIELDS))
                rows = list(reader)
            self.assertEqual([r["timestamp"] for r in rows], [recent])
            leftovers = [p.name for p in audit_file.parent.iterdir() if p.name != "mode_decisions.csv"]
            self.assertEqual(leftovers, [])


class TopupImpliedRateTests(unittest.TestCase):
    def _run(self, config, state, end_capacity, recorded_rate):
        end_status = {"device": {"capacity": end_capacity}}
        with patch("growatt_guard.modes.read_topup_state", return_value=state), \
             patch("growatt_guard.modes.topup_is_active", return_value=False), \
             patch("growatt_guard.modes.load_context",
                   return_value=(None, None, end_status)), \
             patch("growatt_guard.modes.command_resume"), \
             patch("growatt_guard.modes.command_return_sbu", return_value=0), \
             patch("growatt_guard.modes.append_charge_rate_reading",
                   return_value=[{"rate_w": recorded_rate}]), \
             patch("growatt_guard.modes.append_mode_audit"), \
             redirect_stdout(StringIO()) as out:
            rc = command_topup_complete_check(config)
        return rc, out.getvalue()

    def test_implied_rate_excludes_load(self):
        # 50% -> 60% over 60 min on a 30kWh battery = 3000 Wh gained = 3000 W.
        # The old code added start_load_w, inflating the rate; it must not.
        config = make_config(battery_capacity_wh=30_000, battery_charge_rate_w=0.0)
        state = {
            "start_soc": 50,
            "minutes": 60,
            "start_load_w": 800,  # present but must be ignored now
        }
        rc, output = self._run(config, state, "60 %", recorded_rate=3000)
        self.assertEqual(rc, 0)
        self.assertIn("Implied charge rate: 3000 W", output)


if __name__ == "__main__":
    unittest.main()
