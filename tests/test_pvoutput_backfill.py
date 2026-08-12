import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from helpers import make_config
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.pvoutput_backfill import (
    BackfillRecord,
    build_backfill_plan,
    command_pvoutput_backfill,
    fetch_existing_pvoutput_outputs,
    fetch_growatt_daily_generation,
    parse_growatt_energy_history,
    read_growatt_csv,
)


class GrowattHistoryParsingTests(unittest.TestCase):
    def test_parses_official_energy_history(self):
        payload = {
            "count": 2,
            "energys": [
                {"date": "2026-03-14", "energy": "1.25"},
                {"date": "2026-03-15", "energy": 2.5},
            ],
        }

        result = parse_growatt_energy_history(payload)

        self.assertEqual(result[dt.date(2026, 3, 14)], 1250)
        self.assertEqual(result[dt.date(2026, 3, 15)], 2500)

    def test_unknown_history_shape_fails_closed(self):
        with self.assertRaisesRegex(GrowattGuardError, "did not contain recognized"):
            parse_growatt_energy_history({"unexpected": {"ppv": [1, 2]}})

    def test_fetches_official_history_in_seven_day_windows(self):
        api = MagicMock()
        api.plant_energy_history.side_effect = [
            {"energys": [{"date": "2026-03-14", "energy": "1.0"}]},
            {"energys": [{"date": "2026-03-21", "energy": "2.0"}]},
        ]

        result = fetch_growatt_daily_generation(
            api, "123", dt.date(2026, 3, 14), dt.date(2026, 3, 21)
        )

        self.assertEqual(result, {dt.date(2026, 3, 14): 1000, dt.date(2026, 3, 21): 2000})
        self.assertEqual(api.plant_energy_history.call_count, 2)
        first = api.plant_energy_history.call_args_list[0]
        self.assertEqual(first.kwargs["end_date"], dt.date(2026, 3, 20))


class GrowattCsvTests(unittest.TestCase):
    def test_reads_semicolon_csv_and_kwh_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "growatt.csv"
            path.write_text(
                "Date;PV Generation (kWh)\n2026-03-14;1.25\n2026-03-15;2.5\n",
                encoding="utf-8",
            )

            result = read_growatt_csv(path)

        self.assertEqual(result[dt.date(2026, 3, 14)], 1250)
        self.assertEqual(result[dt.date(2026, 3, 15)], 2500)

    def test_reads_wh_without_multiplying(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "growatt.csv"
            path.write_text("Date,Generated Energy (Wh)\n2026-03-14,1250\n", encoding="utf-8")

            result = read_growatt_csv(path)

        self.assertEqual(result[dt.date(2026, 3, 14)], 1250)

    def test_rejects_unknown_headers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "growatt.csv"
            path.write_text("Timestamp;Battery SOC\n2026-03-14;60\n", encoding="utf-8")

            with self.assertRaisesRegex(GrowattGuardError, "date column"):
                read_growatt_csv(path)


class BackfillPlanningTests(unittest.TestCase):
    def test_plan_only_uploads_missing_and_reports_conflicts(self):
        growatt = {
            dt.date(2026, 3, 14): 1000,
            dt.date(2026, 3, 15): 2000,
            dt.date(2026, 3, 16): 3000,
        }
        pvoutput = {
            dt.date(2026, 3, 14): 1000,
            dt.date(2026, 3, 15): 2500,
        }

        plan = build_backfill_plan(
            growatt, pvoutput, dt.date(2026, 3, 14), dt.date(2026, 3, 16), 6000
        )

        self.assertEqual(plan.matching_days, 1)
        self.assertEqual(plan.missing_days, (BackfillRecord("2026-03-16", 3000),))
        self.assertEqual(plan.conflicts[0]["delta_wh"], -500)
        self.assertEqual(plan.reconciliation_delta_wh, 0)

    def test_fetch_existing_uses_31_day_windows(self):
        first = MagicMock(status_code=200, text="20260301,1000,0,;")
        second = MagicMock(status_code=200, text="20260401,2000,0,;")
        with patch("growatt_guard.pvoutput_backfill.requests.get", side_effect=[first, second]) as get:
            result = fetch_existing_pvoutput_outputs(
                make_config(pvoutput_enabled=True, pvoutput_api_key="K", pvoutput_system_id="1"),
                dt.date(2026, 3, 1),
                dt.date(2026, 4, 2),
            )

        self.assertEqual(len(result), 2)
        self.assertEqual(get.call_count, 2)

    def test_fetch_existing_accepts_empty_range_response(self):
        response = MagicMock(status_code=400, text="Bad request 400: No system or data found")
        with patch("growatt_guard.pvoutput_backfill.requests.get", return_value=response):
            result = fetch_existing_pvoutput_outputs(
                make_config(pvoutput_enabled=True, pvoutput_api_key="K", pvoutput_system_id="1"),
                dt.date(2026, 3, 1),
                dt.date(2026, 3, 31),
            )

        self.assertEqual(result, {})


class BackfillCommandTests(unittest.TestCase):
    def _csv(self, directory: str) -> Path:
        path = Path(directory) / "growatt.csv"
        path.write_text("Date,Generated Energy (kWh)\n2026-03-14,1.0\n2026-03-15,2.0\n", encoding="utf-8")
        return path

    def test_preview_from_csv_writes_report_without_uploading(self):
        config = make_config(pvoutput_enabled=True, pvoutput_api_key="K", pvoutput_system_id="1")
        with tempfile.TemporaryDirectory() as tmpdir:
            source = self._csv(tmpdir)
            report = Path(tmpdir) / "report.json"
            with patch(
                "growatt_guard.pvoutput_backfill.fetch_existing_pvoutput_outputs",
                return_value={dt.date(2026, 3, 14): 1000},
            ), patch("growatt_guard.pvoutput_backfill.requests.post") as post:
                result = command_pvoutput_backfill(
                    config, "", "2026-03-15", False, False, str(report), str(source)
                )
            payload = json.loads(report.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(payload["missing_days"], [{"date": "2026-03-15", "generated_wh": 2000}])
        post.assert_not_called()

    def test_apply_is_blocked_by_global_dry_run(self):
        config = make_config(pvoutput_enabled=True, pvoutput_api_key="K", pvoutput_system_id="1", dry_run=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            source = self._csv(tmpdir)
            with patch("growatt_guard.pvoutput_backfill.fetch_existing_pvoutput_outputs", return_value={}):
                command_pvoutput_backfill(
                    config, "", "2026-03-15", False, False, str(Path(tmpdir) / "report.json"), str(source)
                )
                with self.assertRaisesRegex(GrowattGuardError, "DRY_RUN=true"):
                    command_pvoutput_backfill(
                        config, "", "2026-03-15", True, False, str(Path(tmpdir) / "report.json"), str(source)
                    )

    def test_apply_posts_missing_days_only(self):
        config = make_config(pvoutput_enabled=True, pvoutput_api_key="K", pvoutput_system_id="1", dry_run=False)
        response = MagicMock(status_code=200, text="OK", headers={})
        with tempfile.TemporaryDirectory() as tmpdir:
            source = self._csv(tmpdir)
            with patch(
                "growatt_guard.pvoutput_backfill.fetch_existing_pvoutput_outputs",
                return_value={dt.date(2026, 3, 14): 1000},
            ), patch("growatt_guard.pvoutput_backfill.requests.post", return_value=response) as post:
                command_pvoutput_backfill(
                    config, "", "2026-03-15", False, False, str(Path(tmpdir) / "report.json"), str(source)
                )
                result = command_pvoutput_backfill(
                    config, "", "2026-03-15", True, False, str(Path(tmpdir) / "report.json"), str(source)
                )

        self.assertEqual(result, 0)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["data"], {"data": "20260315,2000"})

    def test_apply_batches_multiple_days(self):
        config = make_config(pvoutput_enabled=True, pvoutput_api_key="K", pvoutput_system_id="1", dry_run=False)
        response = MagicMock(status_code=200, text="OK", headers={})
        with tempfile.TemporaryDirectory() as tmpdir:
            source = self._csv(tmpdir)
            with patch("growatt_guard.pvoutput_backfill.fetch_existing_pvoutput_outputs", return_value={}), patch(
                "growatt_guard.pvoutput_backfill.requests.post", return_value=response
            ) as post:
                command_pvoutput_backfill(
                    config, "", "2026-03-15", False, False, str(Path(tmpdir) / "report.json"), str(source)
                )
                command_pvoutput_backfill(
                    config, "", "2026-03-15", True, False, str(Path(tmpdir) / "report.json"), str(source)
                )

        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["data"], {"data": "20260314,1000;20260315,2000"})


if __name__ == "__main__":
    unittest.main()
