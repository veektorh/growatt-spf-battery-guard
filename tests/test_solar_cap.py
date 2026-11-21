import datetime as dt
import unittest
from unittest.mock import patch

from growatt_guard.reports import _solar_cap_section
from growatt_guard.solar_cap import is_capped_reading, summarize_capped_day


def _reading(stamp: str, *, soc: float, pv_w: float, load_w: float, pv_today: float = 0.0):
    return {
        "timestamp": stamp,
        "soc": soc,
        "pv_w": pv_w,
        "load_w": load_w,
        "pv_today_kwh": pv_today,
    }


def _pairs(rows):
    return [(dt.datetime.fromisoformat(row["timestamp"]), row) for row in rows]


class SolarCapTests(unittest.TestCase):
    def test_capped_reading_needs_full_battery_and_pinned_pv(self):
        self.assertTrue(is_capped_reading({"soc": 96, "pv_w": 1300, "load_w": 1250}))
        self.assertFalse(is_capped_reading({"soc": 94, "pv_w": 1300, "load_w": 1250}))
        self.assertFalse(is_capped_reading({"soc": 99, "pv_w": 3000, "load_w": 1250}))
        # Inverter losses scale with load: 170 W over a 3.7 kW load is still pinned.
        self.assertTrue(is_capped_reading({"soc": 99, "pv_w": 3845, "load_w": 3675}))
        self.assertFalse(is_capped_reading({"soc": 99, "pv_w": 1116, "load_w": 3420}))
        self.assertFalse(is_capped_reading({"soc": 99, "pv_w": 0, "load_w": 50}))
        self.assertFalse(is_capped_reading({"soc": 99, "pv_w": None, "load_w": 1250}))

    def test_counts_time_only_between_consecutive_capped_reads(self):
        rows = [
            _reading("2026-11-20T12:00:00", soc=90, pv_w=4000, load_w=1500),
            _reading("2026-11-20T12:15:00", soc=97, pv_w=2413, load_w=2322),
            _reading("2026-11-20T12:30:00", soc=98, pv_w=1268, load_w=1202),
            _reading("2026-11-20T12:45:00", soc=98, pv_w=1360, load_w=1284),
            _reading("2026-11-20T13:00:00", soc=98, pv_w=2600, load_w=1200),
            _reading("2026-11-20T13:15:00", soc=98, pv_w=1300, load_w=1250),
        ]
        self.assertEqual(
            summarize_capped_day(_pairs(rows)),
            {"capped_min": 30, "peak_pv_w": 4000},
        )

    def test_a_gap_longer_than_two_refreshes_is_not_counted(self):
        rows = [
            _reading("2026-11-20T12:00:00", soc=98, pv_w=1300, load_w=1250),
            _reading("2026-11-20T13:00:00", soc=98, pv_w=1300, load_w=1250),
        ]
        self.assertEqual(summarize_capped_day(_pairs(rows))["capped_min"], 0)

    def test_day_without_pv_readings_has_no_summary(self):
        self.assertIsNone(summarize_capped_day([]))
        self.assertIsNone(
            summarize_capped_day([(dt.datetime(2026, 11, 20, 12), {"pv_today_kwh": 3.0})])
        )

    def test_summary_names_a_throttled_day(self):
        rows = [
            _reading(f"2026-11-20T{hour:02d}:{minute:02d}:00+01:00", soc=98, pv_w=1300, load_w=1250, pv_today=20.0)
            for hour in (13, 14)
            for minute in (0, 15, 30, 45)
        ]
        rows.insert(0, _reading("2026-11-20T11:00:00+01:00", soc=80, pv_w=5185, load_w=1500, pv_today=12.0))
        rows.append(_reading("2026-11-19T13:00:00+01:00", soc=98, pv_w=9000, load_w=8950, pv_today=40.0))
        forecast = [{"forecast_date": "2026-11-20", "predicted_kwh": 38.2}]
        with patch("growatt_guard.reports.read_dashboard_metrics_history", return_value=rows), \
             patch("growatt_guard.reports.read_forecast_calibration_history", return_value=forecast):
            section = _solar_cap_section(
                dt.datetime(2026, 11, 20, 21, 0, tzinfo=dt.timezone(dt.timedelta(hours=1)))
            )
        self.assertIn("peak PV 5.2 kW", section)
        self.assertIn("generated 20.0 kWh against a 38.2 kWh day-ahead forecast", section)
        self.assertIn("capped 1.8 h", section)
        self.assertIn("Generation understates the array", section)

    def test_summary_names_a_cloudy_day_without_a_cap(self):
        rows = [
            _reading("2026-11-18T12:00:00", soc=70, pv_w=1800, load_w=1300, pv_today=9.0),
            _reading("2026-11-18T12:15:00", soc=71, pv_w=2100, load_w=1300, pv_today=12.0),
        ]
        forecast = [{"forecast_date": "2026-11-18", "predicted_kwh": 30.0}]
        with patch("growatt_guard.reports.read_dashboard_metrics_history", return_value=rows), \
             patch("growatt_guard.reports.read_forecast_calibration_history", return_value=forecast):
            section = _solar_cap_section(dt.datetime(2026, 11, 18, 21, 0))
        self.assertIn("not capped", section)
        self.assertIn("Below forecast without a cap", section)

    def test_summary_without_readings(self):
        with patch("growatt_guard.reports.read_dashboard_metrics_history", return_value=[]), \
             patch("growatt_guard.reports.read_forecast_calibration_history", return_value=[]):
            self.assertEqual(
                _solar_cap_section(dt.datetime(2026, 11, 18, 21, 0)),
                "Solar cap: no PV readings today.",
            )


if __name__ == "__main__":
    unittest.main()
