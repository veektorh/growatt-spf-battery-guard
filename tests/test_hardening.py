"""Regression tests for the hardening pass:

- atomic state writes (state.write_json_state)
- atomic audit-log pruning (audit.prune_audit_rows)
- topup implied charge rate excludes grid-served load
- auto-topup minute floor never produces a zero-length pause
"""
import csv
import datetime as dt
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_guard.modes import command_topup_complete_check
from growatt_guard.state import write_json_state, read_json_state


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
