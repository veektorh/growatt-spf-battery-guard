import datetime as dt
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from helpers import make_config
from growatt_guard.cli import parse_command_tokens
from growatt_guard.dashboard_render_components import _render_utility_units_panel
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.utility_units import (
    build_utility_report,
    command_utility_units,
    delete_event,
    format_utility_events,
    format_utility_report,
    normalize_event,
    read_utility_units,
    record_reading,
    record_topup,
    write_utility_units,
)

WAT = dt.timezone(dt.timedelta(hours=1))
NOW = dt.datetime(2026, 11, 22, 12, 0, tzinfo=WAT)


def _events(rows):
    return [normalize_event(row) for row in rows]


def _month_events():
    return _events(
        [
            {"kind": "reading", "at": "2026-11-01T07:00:00+01:00", "balance": 500},
            {"kind": "topup", "at": "2026-11-10T18:00:00+01:00", "units": 100},
            {"kind": "reading", "at": "2026-11-22T08:00:00+01:00", "balance": 280},
        ]
    )


def _daily_rows(days=20, wh=10000):
    start = dt.date(2026, 11, 2)
    return [
        {"date": (start + dt.timedelta(days=offset)).isoformat(), "utility_wh": wh}
        for offset in range(days)
    ]


class ReportTests(unittest.TestCase):
    def test_month_consumption_splits_inverter_and_outside_use(self):
        report = build_utility_report(
            _month_events(),
            now=NOW,
            tz=WAT,
            month="2026-11",
            daily_rows=_daily_rows(),
        )

        self.assertEqual(report["current"]["balance"], 280)
        self.assertEqual(report["period"]["purchased"], 100)
        self.assertEqual(report["period"]["consumed"], 320)
        self.assertTrue(report["period"]["complete"])
        # 20 whole days between the readings, 10 kWh of grid each.
        self.assertEqual(report["period"]["inverter_units"], 200)
        self.assertEqual(report["period"]["outside_units"], 120)
        self.assertEqual(report["period"]["inverter_days"], {"covered": 20, "expected": 20})
        self.assertEqual(report["burn"]["units_per_day"], 15.2)
        self.assertEqual(report["cover"]["days"], 18.4)
        self.assertEqual(report["cover"]["runs_out"], "2026-12-10")
        self.assertEqual(report["level"], "ok")
        self.assertEqual(report["ledger"], {
            "events": 3,
            "readings": 2,
            "topups": 1,
            "first": "2026-11-01T07:00+01:00",
            "last": "2026-11-22T08:00+01:00",
        })

    def test_boundary_days_and_today_are_not_counted_as_whole_days(self):
        rows = _daily_rows()
        rows.append({"date": "2026-11-01", "utility_wh": 99000})
        rows.append({"date": "2026-11-22", "utility_wh": 99000})
        rows.append({"date": "2026-11-23", "utility_wh": 99000})
        report = build_utility_report(
            _month_events(), now=NOW, tz=WAT, month="2026-11", daily_rows=rows
        )
        self.assertEqual(report["period"]["inverter_units"], 200)
        self.assertEqual(report["period"]["inverter_days"]["expected"], 20)

    def test_partial_inverter_coverage_is_reported_not_hidden(self):
        report = build_utility_report(
            _month_events(),
            now=NOW,
            tz=WAT,
            month="2026-11",
            daily_rows=_daily_rows(days=10),
        )
        self.assertEqual(report["period"]["inverter_days"], {"covered": 10, "expected": 20})
        self.assertEqual(report["period"]["outside_units"], 220)
        self.assertTrue(any("10 of 20 whole days" in note for note in report["notes"]))

    def test_same_days_last_month_needs_its_own_readings(self):
        events = _events(
            [
                {"kind": "reading", "at": "2026-10-01T06:00:00+01:00", "balance": 400},
                {"kind": "reading", "at": "2026-10-22T06:00:00+01:00", "balance": 250},
                {"kind": "reading", "at": "2026-11-01T07:00:00+01:00", "balance": 500},
                {"kind": "reading", "at": "2026-11-22T08:00:00+01:00", "balance": 280},
            ]
        )
        report = build_utility_report(events, now=NOW, tz=WAT, month="2026-11")
        self.assertEqual(report["previous"]["consumed"], 150)
        self.assertEqual(report["previous"]["from"], "2026-10-01")
        self.assertEqual(report["previous"]["to"], "2026-10-22")

    def test_purchases_alone_cannot_measure_consumption(self):
        events = _events([{"kind": "topup", "at": "2026-11-05T09:00:00+01:00", "units": 100}])
        report = build_utility_report(events, now=NOW, tz=WAT, month="2026-11")
        self.assertIsNone(report["current"])
        self.assertIsNone(report["period"]["consumed"])
        self.assertEqual(report["ledger"]["topups"], 1)
        self.assertTrue(any("utility-units reading" in note for note in report["notes"]))

    def test_single_reading_has_no_burn_rate(self):
        events = _events([{"kind": "reading", "at": "2026-11-22T08:00:00+01:00", "balance": 280}])
        report = build_utility_report(events, now=NOW, tz=WAT, month="2026-11")
        self.assertIsNone(report["burn"])
        self.assertIsNone(report["cover"])
        self.assertEqual(report["level"], "unknown")
        self.assertTrue(any("second reading" in note for note in report["notes"]))

    def test_opening_reading_before_the_range_is_labelled(self):
        events = _events(
            [
                {"kind": "reading", "at": "2026-10-28T07:00:00+01:00", "balance": 500},
                {"kind": "reading", "at": "2026-11-22T08:00:00+01:00", "balance": 280},
            ]
        )
        report = build_utility_report(events, now=NOW, tz=WAT, month="2026-11")
        self.assertFalse(report["period"]["complete"])
        self.assertEqual(report["period"]["consumed"], 220)
        self.assertTrue(any("2026-10-28" in note for note in report["notes"]))

    def test_missing_topup_shows_as_a_negative_balance_move(self):
        events = _events(
            [
                {"kind": "reading", "at": "2026-11-01T07:00:00+01:00", "balance": 500},
                {"kind": "reading", "at": "2026-11-22T08:00:00+01:00", "balance": 700},
            ]
        )
        report = build_utility_report(events, now=NOW, tz=WAT, month="2026-11")
        self.assertEqual(report["period"]["consumed"], -200)
        self.assertIsNone(report["burn"])
        self.assertTrue(any("purchase may be missing" in note for note in report["notes"]))

    def test_same_minute_topup_is_counted_before_the_reading(self):
        events = _events(
            [
                {"kind": "reading", "at": "2026-11-01T07:00:00+01:00", "balance": 200},
                {"kind": "reading", "at": "2026-11-22T08:00:00+01:00", "balance": 300},
                {"kind": "topup", "at": "2026-11-22T08:00:00+01:00", "units": 100},
            ]
        )
        report = build_utility_report(events, now=NOW, tz=WAT, month="2026-11")
        self.assertEqual(report["period"]["consumed"], 0)

    def test_future_and_reversed_ranges_are_rejected(self):
        with self.assertRaises(GrowattGuardError):
            build_utility_report([], now=NOW, tz=WAT, month="2026-12")
        with self.assertRaises(GrowattGuardError):
            build_utility_report([], now=NOW, tz=WAT, from_date="2026-11-20", to_date="2026-11-10")

    def test_cli_and_dashboard_formatters(self):
        report = build_utility_report(
            _month_events(), now=NOW, tz=WAT, month="2026-11", daily_rows=_daily_rows()
        )
        text = format_utility_report(report)
        self.assertIn("Balance   280 units", text)
        self.assertIn("Burn      15.2 units/day", text)
        self.assertIn("inverter 200 units · 20 of 20 whole days", text)
        self.assertIn("outside 120 units", text)

        html = _render_utility_units_panel(report)
        self.assertIn('id="utility-units"', html)
        self.assertIn("280 units", html)
        self.assertIn("Outside inverter", html)
        self.assertIn("120 units", html)
        self.assertIn("18.4 days cover", html)

        empty = _render_utility_units_panel(None)
        self.assertIn("No readings", empty)
        self.assertIn("utility-units reading", empty)


class DashboardViewTests(unittest.TestCase):
    def test_view_is_built_from_the_ledger_and_daily_rows(self):
        from growatt_guard import dashboard_viewmodel as viewmodel

        with patch.object(viewmodel, "read_utility_units", return_value=_month_events()), patch.object(
            viewmodel, "read_daily_generation", return_value=_daily_rows()
        ):
            view = viewmodel._utility_units_view(NOW, WAT)
        self.assertEqual(view["current"]["balance"], 280)
        self.assertEqual(view["period"]["consumed"], 320)
        self.assertEqual(view["period"]["outside_units"], 120)

    def test_view_failure_does_not_break_the_dashboard(self):
        from growatt_guard import dashboard_viewmodel as viewmodel

        with patch.object(viewmodel, "read_utility_units", side_effect=GrowattGuardError("bad ledger")):
            self.assertIsNone(viewmodel._utility_units_view(NOW, WAT))


class LedgerTests(unittest.TestCase):
    def test_append_read_and_normalise(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "utility_units.jsonl"
            record_reading(500, at="2026-11-01T07:00:00+01:00", path=ledger)
            record_topup(100, cost=15000, note="vending", at="2026-11-10T18:00:00+01:00", path=ledger)
            record_reading(280, at="2026-11-22T08:00:00+01:00", path=ledger)
            events = read_utility_units(ledger)
            raw = ledger.read_text(encoding="utf-8").splitlines()

        self.assertEqual([event["kind"] for event in events], ["reading", "topup", "reading"])
        self.assertEqual(events[1]["cost"], 15000)
        self.assertEqual(events[1]["note"], "vending")
        self.assertEqual(len(raw), 3)

    def test_validation_rejects_bad_rows(self):
        for row in (
            {"kind": "reading", "at": "2026-11-22T08:00:00+01:00"},
            {"kind": "topup", "at": "2026-11-22T08:00:00+01:00", "units": 0},
            {"kind": "topup", "at": "2026-11-22T08:00:00+01:00", "units": -5},
            {"kind": "reading", "at": "2026-11-22T08:00:00+01:00", "balance": -1},
            {"kind": "reading", "at": "not-a-date", "balance": 10},
            {"kind": "meter", "at": "2026-11-22T08:00:00+01:00", "balance": 10},
            {"kind": "reading", "at": "2026-11-22T08:00:00+01:00", "balance": True},
            "not an object",
        ):
            with self.subTest(row=row), self.assertRaises(GrowattGuardError):
                normalize_event(row)

    def test_delete_uses_the_listed_index_and_rewrites_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "utility_units.jsonl"
            record_reading(500, at="2026-11-01T07:00:00+01:00", path=ledger)
            record_topup(100, at="2026-11-10T18:00:00+01:00", path=ledger)
            record_reading(280, at="2026-11-22T08:00:00+01:00", path=ledger)
            shown = format_utility_events(read_utility_units(ledger))
            removed = delete_event(2, ledger)
            remaining = read_utility_units(ledger)
            with self.assertRaises(GrowattGuardError):
                delete_event(9, ledger)

        self.assertIn(" 2  10 Nov 18:00", shown)
        self.assertEqual(removed["kind"], "topup")
        self.assertEqual([event["kind"] for event in remaining], ["reading", "reading"])

    def test_write_utility_units_sorts_and_validates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "utility_units.jsonl"
            write_utility_units(
                [
                    {"kind": "reading", "at": "2026-11-22T08:00:00+01:00", "balance": 280},
                    {"kind": "topup", "at": "2026-11-10T18:00:00+01:00", "units": 100},
                ],
                ledger,
            )
            events = read_utility_units(ledger)
            first_line = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual([event["kind"] for event in events], ["topup", "reading"])
        self.assertEqual(first_line["units"], 100)


class CommandTests(unittest.TestCase):
    def test_parser_accepts_every_subcommand(self):
        topup = parse_command_tokens(["utility-units", "topup", "100", "--cost", "15000"])
        reading = parse_command_tokens(["utility-units", "reading", "280"])
        report = parse_command_tokens(
            ["utility-units", "report", "--month", "2026-11", "--json"]
        )
        listed = parse_command_tokens(["utility-units", "list"])
        deleted = parse_command_tokens(["utility-units", "delete", "3"])

        self.assertEqual((topup.utility_action, topup.units, topup.cost), ("topup", 100.0, 15000.0))
        self.assertEqual((reading.utility_action, reading.balance), ("reading", 280.0))
        self.assertEqual((report.utility_action, report.month, report.json), ("report", "2026-11", True))
        self.assertEqual(listed.utility_action, "list")
        self.assertEqual(deleted.index, 3)

    def test_commands_write_print_and_report_from_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "utility_units.jsonl"
            config = make_config(weather_timezone="Africa/Lagos")
            with patch("growatt_guard.utility_units.UTILITY_UNITS_FILE", ledger), patch(
                "growatt_guard.utility_units._read_daily_rows", return_value=_daily_rows()
            ):
                out = StringIO()
                with redirect_stdout(out):
                    command_utility_units(
                        config,
                        parse_command_tokens(
                            ["utility-units", "reading", "500", "--at", "2026-11-01T07:00:00+01:00"]
                        ),
                    )
                    command_utility_units(
                        config,
                        parse_command_tokens(
                            ["utility-units", "topup", "100", "--at", "2026-11-10T18:00:00+01:00"]
                        ),
                    )
                    command_utility_units(
                        config,
                        parse_command_tokens(
                            ["utility-units", "reading", "280", "--at", "2026-11-22T08:00:00+01:00"]
                        ),
                    )
                    command_utility_units(
                        config,
                        parse_command_tokens(
                            ["utility-units", "report", "--month", "2026-11", "--json"]
                        ),
                        now=NOW,
                    )
                printed = out.getvalue()
                stored = read_utility_units(ledger)

        self.assertIn("Recorded reading: 500 units", printed)
        self.assertIn("Recorded top-up: 100 units", printed)
        report = json.loads(printed[printed.index("{") :])
        self.assertEqual(report["period"]["consumed"], 320)
        self.assertEqual(len(stored), 3)


if __name__ == "__main__":
    unittest.main()
