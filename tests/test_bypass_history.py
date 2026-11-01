import datetime as dt
import tempfile
import unittest
from pathlib import Path

from growatt_guard.bypass_history import (
    finalize_completed_bypass_days,
    read_bypass_daily,
)


def _row(ts: str, *, bypass: bool = False, soc: float = 50, grid: float = 0, charge: float = 0):
    return {
        "timestamp": ts,
        "bypass_detected": bypass,
        "soc": soc,
        "grid_w": grid,
        "charge_w": charge,
    }


class BypassRollupTests(unittest.TestCase):
    def test_seeds_every_completed_day_once(self):
        rows = [
            _row("2026-10-30T03:15:00"),
            _row("2026-10-30T03:30:00", bypass=True, soc=49, grid=3880, charge=2165),
            _row("2026-10-30T23:45:00"),
            _row("2026-10-31T23:45:00"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "bypass_daily.jsonl"
            added = finalize_completed_bypass_days(
                rows,
                now=dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=1))),
                path=ledger,
            )
            stored = read_bypass_daily(ledger)
            again = finalize_completed_bypass_days(
                rows,
                now=dt.datetime(2026, 11, 1, 13, 0, tzinfo=dt.timezone(dt.timedelta(hours=1))),
                path=ledger,
            )

        self.assertEqual(added, 2)
        self.assertEqual(again, 0)
        self.assertEqual([row["date"] for row in stored], ["2026-10-30", "2026-10-31"])
        self.assertEqual(stored[0]["episodes"], 1)
        self.assertEqual(stored[0]["samples"], 1)
        self.assertEqual(stored[1]["episodes"], 0)
        self.assertEqual(stored[1]["samples"], 0)

    def test_current_day_is_left_for_tomorrow(self):
        rows = [_row("2026-11-01T12:00:00", bypass=True, soc=49, grid=3000, charge=2000)]
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "bypass_daily.jsonl"
            added = finalize_completed_bypass_days(
                rows,
                now=dt.datetime(2026, 11, 1, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=1))),
                path=ledger,
            )

        self.assertEqual(added, 0)
        self.assertFalse(ledger.exists())


if __name__ == "__main__":
    unittest.main()
