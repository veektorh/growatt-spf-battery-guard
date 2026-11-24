import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from growatt_guard.forecast_calibration import (
    apply_weather_adjustment,
    summarize_forecast_calibration,
    summarize_pv_remaining_fraction,
    update_forecast_calibration,
)
from growatt_guard.state import write_forecast_calibration_history


class ForecastCalibrationTests(unittest.TestCase):
    def _metrics(self, day: dt.date, pv_kwh: float) -> list[dict]:
        return [
            {"timestamp": f"{day.isoformat()}T12:00:00+01:00", "pv_today_kwh": pv_kwh * 0.6},
            {"timestamp": f"{day.isoformat()}T23:55:00+01:00", "pv_today_kwh": pv_kwh},
        ]

    def test_records_tomorrow_forecast_and_finalizes_actual(self):
        issued_at = dt.datetime(2026, 7, 11, 20, 0, tzinfo=dt.timezone(dt.timedelta(hours=1)))
        finalized_at = issued_at + dt.timedelta(days=2)
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.FORECAST_CALIBRATION_FILE", Path(tmpdir) / "forecast.json"
        ):
            update_forecast_calibration(
                {"tomorrow_kwh": 10.0, "tomorrow_irradiance_kwh_m2": 5.2},
                [],
                current_performance_ratio=0.75,
                sunny_threshold_kwh_m2=4.0,
                now=issued_at,
            )
            summary = update_forecast_calibration(
                {"tomorrow_kwh": 12.0, "tomorrow_irradiance_kwh_m2": 5.8},
                self._metrics(issued_at.date() + dt.timedelta(days=1), 8.0),
                current_performance_ratio=0.75,
                sunny_threshold_kwh_m2=4.0,
                now=finalized_at,
            )

        self.assertEqual(summary["sample_count"], 1)
        self.assertEqual(summary["mean_absolute_error_kwh"], 2.0)
        self.assertEqual(summary["sunny_sample_count"], 1)
        self.assertEqual(summary["recent"][0]["actual_kwh"], 8.0)

    def test_learns_rainy_factor_only_after_five_comparable_days(self):
        now = dt.datetime(2026, 7, 12, 20, 0, tzinfo=dt.timezone.utc)
        metrics: list[dict] = []
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.FORECAST_CALIBRATION_FILE", Path(tmpdir) / "forecast.json"
        ):
            for days_ago in range(6, 0, -1):
                issued = now - dt.timedelta(days=days_ago + 1)
                update_forecast_calibration(
                    {
                        "tomorrow_kwh": 10.0,
                        "tomorrow_irradiance_kwh_m2": 5.0,
                        "tomorrow_weather_category": "rainy/cloudy",
                    },
                    metrics,
                    current_performance_ratio=0.75,
                    sunny_threshold_kwh_m2=4.0,
                    now=issued,
                )
                actual_day = issued.date() + dt.timedelta(days=1)
                metrics.extend(self._metrics(actual_day, 8.0))
            summary = update_forecast_calibration(
                {"tomorrow_kwh": 10.0},
                metrics,
                current_performance_ratio=0.75,
                sunny_threshold_kwh_m2=4.0,
                now=now,
            )

        self.assertGreaterEqual(summary["rainy_sample_count"], 5)
        self.assertEqual(summary["confidence"], "medium")
        self.assertEqual(summary["rainy_adjustment_factor"], 0.8)
        self.assertIn("80% of the Open-Meteo base", summary["recommendation"])
        self.assertNotIn("PANEL_PERFORMANCE_RATIO", summary["recommendation"])

    def test_rainy_factor_follows_the_recent_window_not_all_history(self):
        now = dt.datetime(2026, 11, 12, 20, 0, tzinfo=dt.timezone.utc)
        history = []
        for days_ago in range(20, 0, -1):
            forecast_date = (now - dt.timedelta(days=days_ago)).date()
            history.append(
                {
                    "forecast_date": forecast_date.isoformat(),
                    "predicted_kwh": 10.0,
                    "actual_kwh": 10.0 if days_ago > 8 else 5.0,
                    "weather_category": "rainy/cloudy",
                }
            )
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.FORECAST_CALIBRATION_FILE", Path(tmpdir) / "forecast.json"
        ):
            write_forecast_calibration_history(history)
            summary = summarize_forecast_calibration()

        self.assertEqual(summary["rainy_sample_count"], 14)
        self.assertEqual(summary["rainy_actual_to_forecast_ratio"], 0.8)
        self.assertEqual(summary["rainy_recent_actual_to_forecast_ratio"], 0.714)
        self.assertEqual(summary["rainy_adjustment_factor"], 0.71)
        self.assertIn("the last 14 comparable completed day(s)", summary["recommendation"])

    def test_applies_default_rainy_factor_to_headline_without_compounding(self):
        forecast = {
            "tomorrow_kwh": 23.3,
            "tomorrow_weather_category": "rainy/cloudy",
        }

        adjusted = apply_weather_adjustment(forecast, {})
        adjusted_again = apply_weather_adjustment(forecast, {})

        self.assertIs(adjusted, forecast)
        self.assertIs(adjusted_again, forecast)
        self.assertEqual(forecast["base_tomorrow_kwh"], 23.3)
        self.assertEqual(forecast["tomorrow_kwh"], 14.0)
        self.assertTrue(forecast["weather_adjusted"])
        self.assertEqual(forecast["weather_adjustment_factor"], 0.6)

    def test_does_not_adjust_non_rainy_forecast(self):
        forecast = {"tomorrow_kwh": 23.3, "tomorrow_weather_category": "normal"}

        apply_weather_adjustment(forecast, {"rainy_adjustment_factor": 0.4})

        self.assertEqual(forecast["tomorrow_kwh"], 23.3)
        self.assertFalse(forecast["weather_adjusted"])

    def test_ignores_current_day_partial_pv_total(self):
        now = dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.timezone.utc)
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.FORECAST_CALIBRATION_FILE", Path(tmpdir) / "forecast.json"
        ):
            summary = update_forecast_calibration(
                {"tomorrow_kwh": 10.0},
                self._metrics(now.date(), 3.0),
                current_performance_ratio=0.75,
                now=now,
            )

        self.assertEqual(summary["sample_count"], 0)


class PvRemainingFractionTests(unittest.TestCase):
    def _day(self, day: str, total: float, at_14: float) -> list[dict]:
        return [
            {"timestamp": f"{day}T09:00:00+01:00", "pv_today_kwh": total * 0.5},
            {"timestamp": f"{day}T14:00:00+01:00", "pv_today_kwh": at_14},
            {"timestamp": f"{day}T18:30:00+01:00", "pv_today_kwh": total},
            {"timestamp": f"{day}T23:50:00+01:00", "pv_today_kwh": total},
        ]

    def test_uses_the_observed_share_after_the_clock_time(self):
        rows: list[dict] = []
        for day in ("2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"):
            rows += self._day(day, 30.0, 24.0)

        result = summarize_pv_remaining_fraction(
            rows, now=dt.datetime(2026, 11, 21, 14, 10)
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["sample_days"], 4)
        self.assertEqual(result["remaining_fraction"], 0.2)

    def test_ignores_todays_partial_rows(self):
        rows = self._day("2026-11-17", 30.0, 24.0) + self._day("2026-11-18", 30.0, 24.0)
        rows += self._day("2026-11-19", 30.0, 24.0)
        rows.append({"timestamp": "2026-11-20T14:00:00+01:00", "pv_today_kwh": 24.2})

        result = summarize_pv_remaining_fraction(
            rows, now=dt.datetime(2026, 11, 20, 14, 10)
        )

        self.assertEqual(result["sample_days"], 3)

    def test_skips_days_with_a_gap_or_an_early_final_sample(self):
        rows = self._day("2026-11-17", 30.0, 24.0) + self._day("2026-11-18", 30.0, 24.0)
        rows += self._day("2026-11-19", 30.0, 24.0)
        rows += [
            {"timestamp": "2026-11-14T09:00:00+01:00", "pv_today_kwh": 10.0},
            {"timestamp": "2026-11-14T23:50:00+01:00", "pv_today_kwh": 30.0},
        ]
        rows += [
            {"timestamp": "2026-11-15T14:00:00+01:00", "pv_today_kwh": 20.0},
            {"timestamp": "2026-11-15T15:00:00+01:00", "pv_today_kwh": 21.0},
        ]

        result = summarize_pv_remaining_fraction(
            rows, now=dt.datetime(2026, 11, 20, 14, 10)
        )

        self.assertEqual(result["sample_days"], 3)

    def test_returns_none_below_the_minimum_day_count(self):
        rows = self._day("2026-11-18", 30.0, 24.0) + self._day("2026-11-19", 30.0, 24.0)

        result = summarize_pv_remaining_fraction(
            rows, now=dt.datetime(2026, 11, 20, 14, 10)
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
