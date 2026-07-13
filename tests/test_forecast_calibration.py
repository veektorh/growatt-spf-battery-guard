import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from growatt_guard.forecast_calibration import (
    apply_weather_adjustment,
    update_forecast_calibration,
)


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


if __name__ == "__main__":
    unittest.main()
