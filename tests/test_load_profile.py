import datetime as dt
import unittest

from growatt_guard.cli import build_parser
from growatt_guard.load_profile import (
    band_planner_night,
    build_load_profile_report,
    format_load_profile_report,
    hourly_load_profile,
    percentile,
    planning_night_load,
    project_load,
)

WAT = dt.timezone(dt.timedelta(hours=1))


def _readings(day: dt.date, hour: int, watts: float, count: int = 4):
    return [
        {
            "timestamp": dt.datetime.combine(day, dt.time(hour, 5 + 14 * index), tzinfo=WAT).isoformat(),
            "load_w": watts,
        }
        for index in range(count)
    ]


def _days(count: int, hour_watts: dict[int, list[float]], first=dt.date(2026, 11, 9)):
    rows = []
    for offset in range(count):
        day = first + dt.timedelta(days=offset)
        for hour, watts in hour_watts.items():
            rows.extend(_readings(day, hour, watts[offset % len(watts)]))
    return rows


class PercentileTests(unittest.TestCase):
    def test_linear_interpolation(self):
        self.assertEqual(percentile([1000.0], 20), 1000.0)
        self.assertEqual(percentile([1000.0, 2000.0], 50), 1500.0)
        self.assertAlmostEqual(percentile([1000.0, 1100.0, 1200.0, 1300.0, 2000.0], 80), 1440.0)


class HourlyProfileTests(unittest.TestCase):
    def test_median_range_and_readiness(self):
        rows = _days(5, {20: [2000, 2200, 2100, 1900, 3000], 3: [1600]})
        rows += _days(2, {12: [1200]})
        profile = hourly_load_profile(rows, tz=WAT)
        self.assertEqual(profile[20]["median_w"], 2100)
        self.assertEqual(profile[20]["days"], 5)
        self.assertTrue(profile[20]["ready"])
        self.assertEqual((profile[20]["low_w"], profile[20]["high_w"]), (1980, 2360))
        self.assertFalse(profile[12]["ready"])
        self.assertEqual(profile[5], {"days": 0, "ready": False})

    def test_hour_with_a_single_reading_does_not_count(self):
        rows = _days(3, {20: [2000]})
        rows += [{"timestamp": "2026-11-12T20:30:00+01:00", "load_w": 9000}]
        self.assertEqual(hourly_load_profile(rows, tz=WAT)[20]["days"], 3)

    def test_night_type_keeps_the_night_a_sample_belongs_to(self):
        friday, saturday, sunday = dt.date(2026, 11, 13), dt.date(2026, 11, 14), dt.date(2026, 11, 15)
        rows = _readings(friday, 23, 1500) + _readings(saturday, 0, 700)
        rows += _readings(saturday, 23, 2100) + _readings(sunday, 0, 1900)
        weekend = hourly_load_profile(rows, tz=WAT, night_type="weekend")
        self.assertEqual((weekend[23]["median_w"], weekend[23]["days"]), (2100, 1))
        self.assertEqual((weekend[0]["median_w"], weekend[0]["days"]), (1900, 1))
        weekday = hourly_load_profile(rows, tz=WAT, night_type="weekday")
        self.assertEqual((weekday[23]["median_w"], weekday[23]["days"]), (1500, 1))
        self.assertEqual((weekday[0]["median_w"], weekday[0]["days"]), (700, 1))

    def test_skips_missing_and_bad_values(self):
        rows = _days(3, {20: [2000]})
        rows += [
            {"timestamp": "2026-11-09T20:40:00+01:00", "load_w": None},
            {"timestamp": "2026-11-09T20:41:00+01:00", "load_w": True},
            {"timestamp": "bad", "load_w": 5000},
        ]
        self.assertEqual(hourly_load_profile(rows, tz=WAT)[20]["median_w"], 2000)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            hour: {"ready": True, "median_w": 1000 + hour, "low_w": 900, "high_w": 1200, "days": 5}
            for hour in range(24)
        }

    def test_partial_hours_at_both_ends(self):
        start = dt.datetime(2026, 11, 18, 20, 30, tzinfo=WAT)
        projection = project_load(self.profile, start, 1.5)
        # Half of 20:00 (1020 W) and all of 21:00 (1021 W).
        self.assertEqual(projection["median_wh"], round(1020 * 0.5 + 1021))
        self.assertEqual(projection["low_wh"], 1350)
        self.assertEqual(projection["rate_w"], round((1020 * 0.5 + 1021) / 1.5))

    def test_gap_returns_none(self):
        self.profile[2]["ready"] = False
        start = dt.datetime(2026, 11, 18, 18, 0, tzinfo=WAT)
        self.assertIsNone(project_load(self.profile, start, 12))
        self.assertIsNotNone(project_load(self.profile, start, 4))
        self.assertIsNone(project_load(self.profile, start, 0))


class PlanningNightLoadTests(unittest.TestCase):
    """The reserve plan prefers the hour-of-day profile, then the bands."""

    def _nights(self, days: int, evening: float, overnight: float, first=dt.date(2026, 11, 9)):
        rows = []
        for offset in range(days):
            evening_day = first + dt.timedelta(days=offset)
            morning = evening_day + dt.timedelta(days=1)
            for hour in range(19, 24):
                rows.extend(_readings(evening_day, hour, evening))
            for hour in range(0, 7):
                rows.extend(_readings(morning, hour, overnight))
        return rows

    def test_uses_the_hourly_profile_when_every_hour_is_ready(self):
        rows = self._nights(4, 1800, 1500)
        history = [
            {"day_type": "weekday", "rate_w": 2000, "bands": {
                "evening": {"rate_w": 2100, "sample_count": 10},
                "overnight": {"rate_w": 1600, "sample_count": 20},
            }},
        ]
        rate, source = planning_night_load(
            rows, history, now=dt.datetime(2026, 11, 12, 19, 20, tzinfo=WAT), hours=11.0, tz=WAT
        )
        self.assertEqual(rate, 1627.0)
        self.assertIn("hourly profile median (weekday", source)

    def test_falls_back_to_the_bands_when_hours_are_short(self):
        rows = self._nights(2, 1800, 1500)
        history = [
            {"day_type": "weekday", "rate_w": 2000, "bands": {
                "evening": {"rate_w": 2100, "sample_count": 10},
                "overnight": {"rate_w": 1600, "sample_count": 20},
            }}
            for _ in range(4)
        ]
        rate, source = planning_night_load(
            rows, history, now=dt.datetime(2026, 11, 12, 19, 20, tzinfo=WAT), hours=11.0, tz=WAT
        )
        self.assertTrue(1600.0 < rate < 2100.0)
        self.assertNotIn("hourly", source)

    def test_falls_back_to_the_flat_night_without_bands(self):
        history = [{"day_type": "weekday", "rate_w": 1400} for _ in range(4)]
        rate, source = planning_night_load(
            [], history, now=dt.datetime(2026, 11, 12, 19, 20, tzinfo=WAT), hours=11.0, tz=WAT
        )
        self.assertEqual(rate, 1400.0)
        self.assertNotIn("hourly", source)


class ReportTests(unittest.TestCase):
    def test_compares_night_with_band_planner(self):
        night = {hour: [2000] for hour in range(18, 22)}
        night.update({hour: [1500] for hour in (22, 23, 0, 1, 2, 3, 4, 5)})
        rows = _days(4, night)
        history = [
            {"day_type": "weekday", "rate_w": 1700, "bands": {
                "evening": {"rate_w": 2100, "sample_count": 10},
                "overnight": {"rate_w": 1600, "sample_count": 20},
            }},
            {"day_type": "weekend", "rate_w": 900, "bands": {
                "evening": {"rate_w": 900, "sample_count": 10},
                "overnight": {"rate_w": 900, "sample_count": 20},
            }},
        ]
        report = build_load_profile_report(
            rows, history, now=dt.datetime(2026, 11, 18, 21, 0, tzinfo=WAT), tz=WAT
        )
        self.assertEqual(report["night"]["start"], "2026-11-18T18:00+01:00")
        self.assertEqual(report["night"]["day_type"], "weekday")
        self.assertEqual(report["night"]["hourly"]["median_wh"], 4 * 2000 + 8 * 1500)
        self.assertEqual(report["night"]["bands"]["energy_wh"], 4 * 2100 + 8 * 1600)
        text = format_load_profile_report(report)
        self.assertIn("hourly profile: 20.0 kWh (average 1667 W)", text)
        self.assertIn("band planner:   21.2 kWh (evening 2100 W, overnight 1600 W, 1 nights)", text)
        self.assertIsNotNone(report["night"]["hourly_by_type"]["weekday"])
        self.assertIsNone(report["night"]["hourly_by_type"]["weekend"])
        self.assertIn(
            "by day type (hourly / band): weekday 20.0 / 21.2 kWh, weekend n/a / 10.8 kWh", text
        )
        self.assertIn("  12:00  no data", text)

    def test_after_midnight_uses_the_night_that_started_yesterday(self):
        report = build_load_profile_report([], [], now=dt.datetime(2026, 11, 19, 2, 0, tzinfo=WAT), tz=WAT)
        self.assertEqual(report["night"]["start"], "2026-11-18T18:00+01:00")
        self.assertIsNone(report["night"]["hourly"])
        self.assertIsNone(report["night"]["bands"])
        text = format_load_profile_report(report)
        self.assertIn("not every night hour has enough days yet", text)
        self.assertIn("no banded history for this day type", text)

    def test_band_planner_needs_both_bands(self):
        history = [{"day_type": "weekday", "bands": {"evening": {"rate_w": 2000, "sample_count": 3}}}]
        self.assertIsNone(band_planner_night(history, "weekday"))

    def test_cli_parses_load_profile(self):
        args = build_parser().parse_args(["load-profile", "--json"])
        self.assertEqual((args.command, args.json), ("load-profile", True))


if __name__ == "__main__":
    unittest.main()
