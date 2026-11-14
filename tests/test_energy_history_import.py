import datetime as dt
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from growatt_guard.daily_generation import read_daily_generation
from growatt_guard.energy_history_import import (
    build_import_plan,
    command_import_energy_history,
    parse_chart_export,
)
from growatt_guard.exceptions import GrowattGuardError


def _charts(ppv, load, grid):
    return {"ppv": ppv, "elocalLoad": load, "pacToUser": grid, "pacToGrid": [0.0] * len(ppv)}


class EnergyHistoryImportTests(unittest.TestCase):
    def _ledger(self, tmp: Path, rows: list[dict]) -> Path:
        path = tmp / "daily_generation.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def test_parses_one_value_per_day_into_wh(self):
        days = parse_chart_export({"2026-11": _charts([32.5, 27.000000000000364], [42.7, 24.9], [12.7, 8.400000000000091])})

        self.assertEqual(days["2026-11-01"].solar_wh, 32500)
        self.assertEqual(days["2026-11-02"].solar_wh, 27000)
        self.assertEqual(days["2026-11-02"].utility_wh, 8400)

    def test_rejects_malformed_exports(self):
        for payload in ({}, {"Nov": _charts([1.0], [1.0], [1.0])}, {"2026-11": {"ppv": [1.0]}},
                        {"2026-11": _charts([1.0, 2.0], [1.0], [1.0])}, {"2026-02": _charts([1.0] * 29, [1.0] * 29, [1.0] * 29)},
                        {"2026-11": _charts([-1.0], [1.0], [1.0])}):
            with self.subTest(payload=str(payload)[:40]):
                with self.assertRaises(GrowattGuardError):
                    parse_chart_export(payload)

    def test_fills_only_missing_fields_and_never_touches_solar(self):
        rows = [
            {"date": "2026-08-27", "generated_wh": 9000},
            {"date": "2026-08-28", "generated_wh": 10100},
            {"date": "2026-11-12", "generated_wh": 27400, "load_wh": 39500, "utility_wh": 21000},
            {"date": "2026-11-13", "generated_wh": 34200},
        ]
        export = parse_chart_export({
            "2026-08": {**_charts([0.0] * 31, [0.0] * 31, [0.0] * 31)},
            "2026-11": _charts([0.0] * 11 + [27.4, 34.2, 1.0], [0.0] * 11 + [45.0, 42.8, 5.0], [0.0] * 11 + [21.4, 9.9, 1.0]),
        })
        export["2026-08-28"] = export["2026-08-28"].__class__(solar_wh=16500, load_wh=30000, utility_wh=4000)

        plan = build_import_plan(rows, export, today=dt.date(2026, 11, 14))

        self.assertEqual(plan.fills["2026-08-28"], {"load_wh": 30000, "utility_wh": 4000})
        self.assertEqual(plan.fills["2026-11-13"], {"load_wh": 42800, "utility_wh": 9900})
        self.assertNotIn("2026-11-12", plan.fills)
        self.assertEqual(plan.solar_mismatches, [("2026-08-28", 10100, 16500)])
        # 11-12 already has both: the export's differing values are reported, not written.
        self.assertEqual(
            plan.kept,
            [("2026-11-12", "load_wh", 39500, 45000), ("2026-11-12", "utility_wh", 21000, 21400)],
        )
        # Today is still in progress, and a ledger day the export has no load for is skipped.
        self.assertNotIn("2026-11-14", plan.fills)
        self.assertEqual(plan.no_data, ["2026-08-27"])
        self.assertNotIn("2026-08-01", plan.not_in_ledger)

    def test_export_days_missing_from_the_ledger_are_reported_not_added(self):
        export = parse_chart_export({"2026-11": _charts([5.0, 6.0], [10.0, 11.0], [1.0, 2.0])})

        plan = build_import_plan([{"date": "2026-11-01", "generated_wh": 5000}], export, today=dt.date(2026, 11, 14))

        self.assertEqual(plan.not_in_ledger, ["2026-11-02"])
        self.assertEqual(list(plan.fills), ["2026-11-01"])

    def test_preview_writes_nothing_and_apply_backs_up_then_fills(self):
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ledger = self._ledger(tmp, [{"date": "2026-11-01", "generated_wh": 32500, "source": "growatt-history"}])
            export = tmp / "export.json"
            export.write_text(json.dumps({"2026-11": _charts([32.5], [42.7], [12.7])}), encoding="utf-8")
            before = ledger.read_text(encoding="utf-8")

            with redirect_stdout(StringIO()) as out:
                command_import_energy_history(str(export), today=dt.date(2026, 11, 14), ledger_path=ledger)
            self.assertIn("Days to fill with load and Utility: 1", out.getvalue())
            self.assertEqual(ledger.read_text(encoding="utf-8"), before)

            with redirect_stdout(StringIO()):
                command_import_energy_history(str(export), apply=True, today=dt.date(2026, 11, 14), ledger_path=ledger)
            row = read_daily_generation(ledger)[0]
            backups = list(tmp.glob("daily_generation.jsonl.bak-*"))

        self.assertEqual(row["load_wh"], 42700)
        self.assertEqual(row["utility_wh"], 12700)
        self.assertEqual(row["generated_wh"], 32500)
        self.assertEqual(row["source"], "growatt-history")
        self.assertEqual(row["energy_import_source"], "growatt-web-chart")
        self.assertEqual(len(backups), 1)


if __name__ == "__main__":
    unittest.main()
