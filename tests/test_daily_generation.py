import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from growatt_guard.daily_generation import (
    build_daily_energy_view,
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

    def test_ignores_stale_carry_over_from_the_previous_day(self):
        rows = [
            {"timestamp": "2026-10-04T23:45:00+01:00", "pv_today_kwh": 16.2},
            # Growatt kept serving the 10-04 total after midnight, then reset.
            {"timestamp": "2026-10-05T00:15:00+01:00", "pv_today_kwh": 16.2},
            {"timestamp": "2026-10-05T00:30:00+01:00", "pv_today_kwh": 0.1},
            {"timestamp": "2026-10-05T23:45:00+01:00", "pv_today_kwh": 11.2},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "daily_generation.jsonl"
            merge_daily_generation(
                {dt.date(2026, 10, 4): 16200}, source="growatt-history", path=ledger
            )
            added, _ = finalize_completed_daily_generation(
                rows,
                now=dt.datetime(2026, 10, 6, 0, 5, tzinfo=dt.timezone(dt.timedelta(hours=1))),
                path=ledger,
            )
            saved = read_daily_generation(ledger)

        self.assertEqual(added, 1)
        self.assertEqual(saved[-1]["date"], "2026-10-05")
        self.assertEqual(saved[-1]["generated_wh"], 11200)

    def test_all_day_stale_counter_is_not_recorded(self):
        rows = [
            {"timestamp": "2026-10-05T00:15:00+01:00", "pv_today_kwh": 16.2},
            {"timestamp": "2026-10-05T12:00:00+01:00", "pv_today_kwh": 16.2},
            {"timestamp": "2026-10-05T23:45:00+01:00", "pv_today_kwh": 16.2},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "daily_generation.jsonl"
            merge_daily_generation(
                {dt.date(2026, 10, 4): 16200}, source="growatt-history", path=ledger
            )
            added, _ = finalize_completed_daily_generation(
                rows,
                now=dt.datetime(2026, 10, 6, 0, 5, tzinfo=dt.timezone(dt.timedelta(hours=1))),
                path=ledger,
            )
            saved = read_daily_generation(ledger)

        self.assertEqual(added, 0)
        self.assertEqual([row["date"] for row in saved], ["2026-10-04"])

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


class DailyEnergyViewTests(unittest.TestCase):
    def test_records_load_and_utility_counters_beside_generation(self):
        rows = [
            {
                "timestamp": "2026-08-11T20:15:00+01:00",
                "pv_today_kwh": 18.4,
                "load_today_kwh": 12.5,
                "grid_today_kwh": 3.1,
                "charge_today_kwh": 6.2,
                "discharge_today_kwh": 4.4,
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "daily_generation.jsonl"
            finalize_completed_daily_generation(
                rows,
                now=dt.datetime(2026, 8, 12, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=1))),
                path=ledger,
            )
            saved = read_daily_generation(ledger)

        self.assertEqual(saved[0]["generated_wh"], 18400)
        self.assertEqual(saved[0]["load_wh"], 12500)
        self.assertEqual(saved[0]["utility_wh"], 3100)
        self.assertEqual(saved[0]["charge_wh"], 6200)
        self.assertEqual(saved[0]["discharge_wh"], 4400)

    def test_month_totals_report_missing_metrics_as_none(self):
        rows = [
            {"date": "2026-09-30", "generated_wh": 28000},
            {"date": "2026-10-01", "generated_wh": 31000},
            {"date": "2026-10-02", "generated_wh": 12000, "load_wh": 21000, "utility_wh": 4000},
            {"date": "2026-11-01", "generated_wh": 22000, "load_wh": 18000, "utility_wh": 0},
        ]

        view = build_daily_energy_view(rows, today=dt.date(2026, 11, 12))

        by_month = {row["month"]: row for row in view["months"]}
        self.assertEqual(by_month["2026-09"]["solar_kwh"], 28.0)
        self.assertIsNone(by_month["2026-09"]["load_kwh"])
        self.assertIsNone(by_month["2026-09"]["utility_kwh"])
        self.assertEqual(by_month["2026-10"]["solar_kwh"], 43.0)
        self.assertEqual(by_month["2026-10"]["load_kwh"], 21.0)
        self.assertEqual(by_month["2026-10"]["utility_kwh"], 4.0)
        self.assertTrue(by_month["2026-10"]["complete"])
        self.assertFalse(by_month["2026-11"]["complete"])
        self.assertEqual(by_month["2026-11"]["utility_kwh"], 0.0)
        self.assertEqual([row["date"] for row in view["days"]], [row["date"] for row in rows])
        self.assertEqual(
            view["days"][-1],
            {"date": "2026-11-01", "solar_kwh": 22.0, "load_kwh": 18.0, "utility_kwh": 0.0},
        )
        self.assertIsNone(view["days"][0]["load_kwh"])
