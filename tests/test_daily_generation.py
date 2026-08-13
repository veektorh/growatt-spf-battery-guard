import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from growatt_guard.daily_generation import (
    finalize_completed_daily_generation,
    merge_daily_generation,
    read_daily_generation,
)
from growatt_guard.exceptions import GrowattGuardError


class DailyGenerationTests(unittest.TestCase):
    def test_finalizes_only_completed_days_with_a_late_sample(self):
        rows = [
            {"timestamp": "2026-08-11T19:45:00+01:00", "pv_today_kwh": 18.1},
            {"timestamp": "2026-08-11T20:15:00+01:00", "pv_today_kwh": 18.4},
            {"timestamp": "2026-08-12T20:15:00+01:00", "pv_today_kwh": 19.2},
            {"timestamp": "2026-08-13T20:15:00+01:00", "pv_today_kwh": 5.0},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "daily_generation.jsonl"
            added, conflicts = finalize_completed_daily_generation(
                rows,
                now=dt.datetime(2026, 8, 13, 21, 0, tzinfo=dt.timezone(dt.timedelta(hours=1))),
                path=ledger,
            )
            saved = read_daily_generation(ledger)

        self.assertEqual(added, 1)
        self.assertEqual(conflicts, ())
        self.assertEqual(saved[0]["date"], "2026-08-12")
        self.assertEqual(saved[0]["generated_wh"], 19200)
        self.assertEqual(saved[0]["source"], "growatt-observability")

    def test_seeded_ledger_advances_forward_through_available_completed_days(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "daily_generation.jsonl"
            merge_daily_generation(
                {dt.date(2026, 8, 10): 17000}, source="growatt-history", path=ledger
            )
            added, _ = finalize_completed_daily_generation(
                [
                    {"timestamp": "2026-08-11T20:15:00+01:00", "pv_today_kwh": 18.4},
                    {"timestamp": "2026-08-12T20:15:00+01:00", "pv_today_kwh": 19.2},
                ],
                now=dt.datetime(2026, 8, 13, 21, 0, tzinfo=dt.timezone(dt.timedelta(hours=1))),
                path=ledger,
            )
            saved = read_daily_generation(ledger)

        self.assertEqual(added, 2)
        self.assertEqual([row["date"] for row in saved], ["2026-08-10", "2026-08-11", "2026-08-12"])

    def test_merge_is_idempotent_and_strict_conflicts_fail_closed(self):
        day = dt.date(2026, 8, 11)
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "daily_generation.jsonl"
            self.assertEqual(
                merge_daily_generation({day: 18400}, source="growatt-history", path=ledger)[0],
                1,
            )
            self.assertEqual(
                merge_daily_generation({day: 18400}, source="growatt-history", path=ledger)[0],
                0,
            )
            with self.assertRaisesRegex(GrowattGuardError, "conflicts"):
                merge_daily_generation(
                    {day: 18500}, source="growatt-history", strict=True, path=ledger
                )

    def test_malformed_existing_ledger_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "daily_generation.jsonl"
            ledger.write_text('{"date":"2026-08-11"}\n', encoding="utf-8")
            with self.assertRaisesRegex(GrowattGuardError, "generated_wh"):
                merge_daily_generation(
                    {dt.date(2026, 8, 12): 1000}, source="growatt-history", path=ledger
                )
            self.assertEqual(json.loads(ledger.read_text(encoding="utf-8"))["date"], "2026-08-11")


if __name__ == "__main__":
    unittest.main()
