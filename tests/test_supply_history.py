import datetime as dt
import unittest

from growatt_guard.cli import build_parser
from growatt_guard.dashboard_metrics import extract_dashboard_metrics
from growatt_guard.supply_history import (
    build_supply_report,
    format_supply_report,
    observed_cuts,
    supply_on,
)

WAT = dt.timezone(dt.timedelta(hours=1))
WINDOWS = [
    {"days": "weekdays", "start": "08:00", "end": "10:30"},
    {"days": "weekdays", "start": "15:30", "end": "18:00"},
]


def _row(clock: str, volts, soc=50.0, day="2026-11-18"):
    return {"timestamp": f"{day}T{clock}:00+01:00", "grid_v": volts, "soc": soc}


class SupplyStateTests(unittest.TestCase):
    def test_voltage_threshold_and_missing_values(self):
        self.assertTrue(supply_on({"grid_v": 228.4}))
        self.assertTrue(supply_on({"grid_v": 150.0}))
        self.assertFalse(supply_on({"grid_v": 0.0}))
        self.assertIsNone(supply_on({"grid_v": None}))
        self.assertIsNone(supply_on({}))
        self.assertIsNone(supply_on({"grid_v": True}))

    def test_snapshot_records_grid_voltage(self):
        status = {"storage_params": {"storageDetailBean": {"vGrid": 225.30000305, "vac": 0}}}
        self.assertEqual(extract_dashboard_metrics(status)["grid_v"], 225.3)
        self.assertIsNone(extract_dashboard_metrics({})["grid_v"])


class ObservedCutTests(unittest.TestCase):
    def test_cut_is_bracketed_by_the_readings_around_it(self):
        rows = [
            _row("07:50", 229, soc=44),
            _row("08:04", 0, soc=43),
            _row("09:00", 0, soc=35),
            _row("10:28", 0, soc=27),
            _row("10:41", 231, soc=30),
        ]
        [cut] = observed_cuts(rows, WAT)
        self.assertEqual(cut["start_after"].strftime("%H:%M"), "07:50")
        self.assertEqual(cut["start_by"].strftime("%H:%M"), "08:04")
        self.assertEqual(cut["end_after"].strftime("%H:%M"), "10:28")
        self.assertEqual(cut["end_by"].strftime("%H:%M"), "10:41")
        self.assertEqual((cut["soc_before"], cut["soc_at_start"], cut["samples"]), (44, 43, 3))

    def test_history_starting_inside_a_cut_and_ongoing_cut(self):
        rows = [_row("08:10", 0), _row("08:30", 230), _row("15:40", 0)]
        first, second = observed_cuts(rows, WAT)
        self.assertIsNone(first["start_after"])
        self.assertIsNone(first["soc_before"])
        self.assertIsNone(second["end_by"])

    def test_rows_without_voltage_are_ignored(self):
        rows = [_row("07:50", 229), _row("08:00", None), _row("08:10", 228)]
        self.assertEqual(observed_cuts(rows, WAT), [])


class SupplyReportTests(unittest.TestCase):
    def test_matches_scheduled_windows_and_flags_the_rest(self):
        rows = [
            _row("07:50", 229, soc=44),
            _row("08:04", 0, soc=43),
            _row("10:41", 231, soc=30),
            # Supply stays on through the afternoon window.
            _row("16:00", 230),
            _row("17:30", 229),
            # An unscheduled cut in the evening.
            _row("20:10", 0, soc=70),
            _row("20:25", 228, soc=69),
        ]
        report = build_supply_report(
            rows, WINDOWS, now=dt.datetime(2026, 11, 18, 21, 0, tzinfo=WAT), tz=WAT, days=1
        )
        kinds = [(event["kind"], event["scheduled"]) for event in report["events"]]
        self.assertEqual(
            kinds,
            [("cut", "08:00-10:30"), ("no-cut", "15:30-18:00"), ("unscheduled-cut", None)],
        )
        morning = report["events"][0]
        self.assertEqual(morning["start_offset_min"], [-10.0, 4.0])
        self.assertEqual(morning["duration_min"], [0.0, 171.0])
        self.assertEqual(report["events"][1]["readings"], 2)

        text = format_supply_report(report, "estate-19h")
        self.assertIn("scheduled 08:00-10:30: off between 07:50-08:04, back between 08:04-10:41", text)
        self.assertIn("SOC 44% before, 43% at first off reading", text)
        self.assertIn("scheduled 15:30-18:00: supply stayed on (2 readings)", text)
        self.assertIn("unscheduled: off between 17:30-20:10, back between 20:10-20:25", text)

    def test_window_without_readings_and_future_windows(self):
        rows = [_row("07:00", 229), _row("11:00", 230)]
        report = build_supply_report(
            rows, WINDOWS, now=dt.datetime(2026, 11, 18, 12, 0, tzinfo=WAT), tz=WAT, days=1
        )
        self.assertEqual(
            [(event["kind"], event["scheduled"]) for event in report["events"]],
            [("no-data", "08:00-10:30")],
        )

    def test_weekend_skips_weekday_windows_and_empty_history(self):
        saturday = [_row("09:00", 230, day="2026-11-21")]
        report = build_supply_report(
            saturday, WINDOWS, now=dt.datetime(2026, 11, 21, 12, 0, tzinfo=WAT), tz=WAT, days=1
        )
        self.assertEqual(report["events"], [])

        empty = build_supply_report([], WINDOWS, now=dt.datetime(2026, 11, 21, 12, 0, tzinfo=WAT), tz=WAT)
        self.assertIsNone(empty["since"])
        self.assertIn("No snapshot carries a grid voltage yet", format_supply_report(empty, None))

    def test_cli_parses_supply_history(self):
        args = build_parser().parse_args(["supply-history", "--days", "3", "--json"])
        self.assertEqual((args.command, args.days, args.json), ("supply-history", 3, True))


if __name__ == "__main__":
    unittest.main()
