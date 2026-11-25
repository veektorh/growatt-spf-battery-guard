import datetime as dt
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_power_guard import (
    DRY_SEASON_THRESHOLDS,
    RAINY_SEASON_MONTHS,
    ThresholdDecision,
    analyze_weather_window,
    apply_season_adjustment,
    choose_preserve_threshold,
    current_season,
    GrowattGuardError,
)
from growatt_guard.weather import apply_load_adjustment


class WeatherTests(unittest.TestCase):
    def test_choose_preserve_threshold_uses_fixed_when_weather_disabled(self):
        decision = choose_preserve_threshold(make_config(weather_enabled=False, low_battery_soc=50))

        self.assertEqual(decision.threshold, 50)
        self.assertEqual(decision.weather_category, "disabled")

    def test_analyze_weather_window_keeps_rainy_threshold_at_50(self):
        now = dt.datetime.now().replace(minute=0, second=0, microsecond=0)
        forecast = {
            "hourly": {
                "time": [(now + dt.timedelta(hours=i)).isoformat(timespec="minutes") for i in range(4)],
                "cloud_cover": [80, 75, 70, 60],
                "precipitation": [0, 0.2, 0.9, 0],
            }
        }

        decision = analyze_weather_window(make_config(), forecast)

        self.assertEqual(decision.threshold, 50)
        self.assertEqual(decision.weather_category, "rainy/cloudy")

    def test_analyze_weather_window_uses_normal_threshold(self):
        now = dt.datetime.now().replace(minute=0, second=0, microsecond=0)
        forecast = {
            "hourly": {
                "time": [(now + dt.timedelta(hours=i)).isoformat(timespec="minutes") for i in range(4)],
                "cloud_cover": [50, 55, 45, 40],
                "precipitation": [0, 0, 0, 0],
            }
        }

        decision = analyze_weather_window(make_config(), forecast)

        self.assertEqual(decision.threshold, 45)
        self.assertEqual(decision.weather_category, "normal")

    def test_analyze_weather_window_uses_sunny_threshold(self):
        now = dt.datetime.now().replace(minute=0, second=0, microsecond=0)
        forecast = {
            "hourly": {
                "time": [(now + dt.timedelta(hours=i)).isoformat(timespec="minutes") for i in range(4)],
                "cloud_cover": [10, 15, 20, 30],
                "precipitation": [0, 0, 0, 0],
            }
        }

        decision = analyze_weather_window(make_config(), forecast)

        self.assertEqual(decision.threshold, 40)
        self.assertEqual(decision.weather_category, "sunny")

    def test_fetch_weather_forecast_uses_cache_when_fresh(self):
        from growatt_guard.weather import fetch_weather_forecast
        forecast = {"hourly": {"time": [], "cloud_cover": [], "precipitation": []}}
        payload = {"fetched_at": dt.datetime.now().isoformat(), "forecast": forecast}
        with TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "weather_cache.json"
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("growatt_guard.weather.WEATHER_CACHE_FILE", cache_path), patch(
                "growatt_guard.weather.requests"
            ) as mock_requests:
                result = fetch_weather_forecast(make_config(weather_lat=6.5, weather_lon=3.4))
        self.assertEqual(result, forecast)
        mock_requests.get.assert_not_called()

    def test_fetch_weather_forecast_fetches_and_caches_when_stale(self):
        import unittest.mock
        from growatt_guard.weather import fetch_weather_forecast
        forecast = {"hourly": {"time": [], "cloud_cover": [], "precipitation": []}}
        stale_payload = {
            "fetched_at": (dt.datetime.now() - dt.timedelta(hours=1)).isoformat(),
            "forecast": {"old": True},
        }
        mock_response = unittest.mock.MagicMock()
        mock_response.json.return_value = forecast
        with TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "weather_cache.json"
            cache_path.write_text(json.dumps(stale_payload), encoding="utf-8")
            with patch("growatt_guard.weather.WEATHER_CACHE_FILE", cache_path), patch(
                "growatt_guard.weather.requests"
            ) as mock_requests:
                mock_requests.get.return_value = mock_response
                result = fetch_weather_forecast(make_config(weather_lat=6.5, weather_lon=3.4))
            self.assertEqual(result, forecast)
            mock_requests.get.assert_called_once()
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(cached["forecast"], forecast)


    def test_fetch_weather_forecast_uses_stale_cache_on_request_failure(self):
        import requests
        from growatt_guard.weather import fetch_weather_forecast

        stale_forecast = {"hourly": {"time": [], "cloud_cover": [], "precipitation": []}}
        stale_payload = {
            "fetched_at": (dt.datetime.now() - dt.timedelta(hours=1)).isoformat(),
            "forecast": stale_forecast,
        }
        with TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "weather_cache.json"
            cache_path.write_text(json.dumps(stale_payload), encoding="utf-8")
            with patch(
                "growatt_guard.weather.WEATHER_CACHE_FILE",
                cache_path,
            ), patch(
                "growatt_guard.weather.requests.get",
                side_effect=requests.exceptions.RequestException(
                    "https://api.open-meteo.com/v1/forecast?latitude=6.5&longitude=3.4"
                ),
            ) as mock_requests, patch("growatt_guard.weather.logging.warning") as mock_warning:
                result = fetch_weather_forecast(make_config(weather_lat=6.5, weather_lon=3.4))

        self.assertEqual(result, stale_forecast)
        mock_requests.assert_called_once()
        self.assertEqual(
            mock_warning.call_args.args[0],
            "Open-Meteo forecast unavailable; using stale weather cache (age %d min).",
        )
        self.assertIsInstance(mock_warning.call_args.args[1], int)

    def test_fetch_weather_forecast_raises_when_no_stale_cache_exists(self):
        from requests.exceptions import RequestException
        from growatt_guard.weather import fetch_weather_forecast

        with TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "weather_cache.json"
            with patch("growatt_guard.weather.WEATHER_CACHE_FILE", cache_path), patch(
                "growatt_guard.weather.requests.get",
                side_effect=RequestException(
                    "https://api.open-meteo.com/v1/forecast?latitude=6.5&longitude=3.4"
                ),
            ):
                with self.assertRaises(GrowattGuardError) as context:
                    fetch_weather_forecast(make_config(weather_lat=6.5, weather_lon=3.4))

        message = str(context.exception)
        self.assertEqual(message, "Open-Meteo forecast unavailable and no cached weather data is available.")
        self.assertNotIn("api.open-meteo.com", message.lower())
        self.assertNotIn("latitude", message.lower())
        self.assertNotIn("longitude", message.lower())


class SeasonProfileTests(unittest.TestCase):
    def test_current_season_rainy_months(self):
        for month in RAINY_SEASON_MONTHS:
            date = dt.date(2026, month, 15)
            self.assertEqual(current_season(date), "rainy", f"Expected rainy for month {month}")

    def test_current_season_dry_months(self):
        for month in [1, 2, 3, 11, 12]:
            date = dt.date(2026, month, 15)
            self.assertEqual(current_season(date), "dry", f"Expected dry for month {month}")

    def test_apply_season_adjustment_lowers_threshold_in_dry_season(self):
        decision = ThresholdDecision(threshold=50.0, reason="rainy/cloudy", weather_category="rainy/cloudy")
        adjusted = apply_season_adjustment(decision, "dry")
        self.assertEqual(adjusted.threshold, DRY_SEASON_THRESHOLDS["rainy/cloudy"])
        self.assertIn("dry season", adjusted.reason)

    def test_apply_season_adjustment_no_change_in_rainy_season(self):
        decision = ThresholdDecision(threshold=50.0, reason="rainy/cloudy", weather_category="rainy/cloudy")
        adjusted = apply_season_adjustment(decision, "rainy")
        self.assertEqual(adjusted.threshold, 50.0)
        self.assertEqual(adjusted.reason, decision.reason)

    def test_dry_season_thresholds_values(self):
        self.assertEqual(DRY_SEASON_THRESHOLDS["rainy/cloudy"], 45.0)
        self.assertEqual(DRY_SEASON_THRESHOLDS["normal"], 40.0)
        self.assertEqual(DRY_SEASON_THRESHOLDS["sunny"], 35.0)

    def test_choose_preserve_threshold_applies_dry_season_when_enabled(self):
        config = make_config(weather_enabled=False, low_battery_soc=50, season_profiles_enabled=True)
        dry_date = dt.date(2026, 1, 15)  # January = dry season
        decision = choose_preserve_threshold(config, today=dry_date)
        self.assertEqual(decision.threshold, DRY_SEASON_THRESHOLDS["disabled"])
        self.assertIn("dry season", decision.reason)

    def test_choose_preserve_threshold_no_adjustment_in_rainy_season(self):
        config = make_config(weather_enabled=False, low_battery_soc=50, season_profiles_enabled=True)
        rainy_date = dt.date(2026, 6, 20)  # June = rainy season
        decision = choose_preserve_threshold(config, today=rainy_date)
        self.assertEqual(decision.threshold, 50.0)

    def test_choose_preserve_threshold_no_adjustment_when_disabled(self):
        config = make_config(weather_enabled=False, low_battery_soc=50, season_profiles_enabled=False)
        dry_date = dt.date(2026, 1, 15)
        decision = choose_preserve_threshold(config, today=dry_date)
        self.assertEqual(decision.threshold, 50.0)


class LoadAdjustmentTests(unittest.TestCase):
    BASE = ThresholdDecision(threshold=45.0, reason="test base", weather_category="normal")

    def test_high_load_raises_threshold(self):
        result = apply_load_adjustment(self.BASE, 65.0)
        self.assertEqual(result.threshold, 50.0)
        self.assertIn("high", result.reason)
        self.assertIn("+5", result.reason)

    def test_low_load_lowers_threshold(self):
        result = apply_load_adjustment(self.BASE, 15.0)
        self.assertEqual(result.threshold, 40.0)
        self.assertIn("low", result.reason)
        self.assertIn("-5", result.reason)

    def test_normal_load_unchanged(self):
        result = apply_load_adjustment(self.BASE, 40.0)
        self.assertEqual(result.threshold, 45.0)
        self.assertEqual(result.reason, self.BASE.reason)

    def test_none_load_unchanged(self):
        result = apply_load_adjustment(self.BASE, None)
        self.assertEqual(result.threshold, 45.0)

    def test_threshold_floor_at_zero(self):
        low_base = ThresholdDecision(threshold=3.0, reason="base", weather_category="sunny")
        result = apply_load_adjustment(low_base, 10.0)
        self.assertEqual(result.threshold, 0.0)

    def test_boundary_exactly_at_high_threshold_not_adjusted(self):
        result = apply_load_adjustment(self.BASE, 60.0)
        self.assertEqual(result.threshold, 45.0)

    def test_boundary_exactly_at_low_threshold_not_adjusted(self):
        result = apply_load_adjustment(self.BASE, 20.0)
        self.assertEqual(result.threshold, 45.0)

    def test_weather_category_preserved_after_adjustment(self):
        result = apply_load_adjustment(self.BASE, 70.0)
        self.assertEqual(result.weather_category, "normal")
        self.assertIsNone(result.cloud_cover)


class GetTodayRemainingSolarKwhM2Tests(unittest.TestCase):
    def _forecast(self, day: dt.date, radiation_values: list) -> dict:
        return {
            "hourly": {
                "time": [f"{day.isoformat()}T{h:02d}:00" for h in range(len(radiation_values))],
                "shortwave_radiation": radiation_values,
            }
        }

    def test_prorates_the_current_hour(self):
        from growatt_guard.weather import get_today_remaining_solar_kwh_m2
        cfg = make_config(weather_lat=6.5, weather_lon=3.4)
        day = dt.date(2026, 6, 20)
        forecast = self._forecast(day, [1000.0] * 24)
        with patch("growatt_guard.weather.fetch_weather_forecast", return_value=forecast):
            # Half of the 13:00 bucket plus the ten hours from 14:00 on.
            result = get_today_remaining_solar_kwh_m2(
                cfg, now=dt.datetime(2026, 6, 20, 13, 30)
            )
        self.assertAlmostEqual(result, 10.5)

    def test_returns_zero_after_sunset(self):
        from growatt_guard.weather import get_today_remaining_solar_kwh_m2
        cfg = make_config(weather_lat=6.5, weather_lon=3.4)
        day = dt.date(2026, 6, 20)
        forecast = self._forecast(day, [1000.0] * 12 + [0.0] * 12)
        with patch("growatt_guard.weather.fetch_weather_forecast", return_value=forecast):
            result = get_today_remaining_solar_kwh_m2(
                cfg, now=dt.datetime(2026, 6, 20, 20, 30)
            )
        self.assertEqual(result, 0.0)

    def test_empirical_fraction_drives_today_remaining(self):
        from growatt_guard.weather import get_pv_forecast
        cfg = make_config(
            weather_lat=6.5,
            weather_lon=3.4,
            panel_kwp=8.0,
            panel_performance_ratio=0.75,
        )
        today = dt.date(2026, 6, 20)
        tomorrow = dt.date(2026, 6, 21)
        radiation = [250.0] * 24
        forecast = {
            "hourly": {
                "time": (
                    [f"{today.isoformat()}T{h:02d}:00" for h in range(24)]
                    + [f"{tomorrow.isoformat()}T{h:02d}:00" for h in range(24)]
                ),
                "shortwave_radiation": radiation + radiation,
            }
        }
        with patch("growatt_guard.weather.fetch_weather_forecast", return_value=forecast):
            corrected = get_pv_forecast(
                cfg, now=dt.datetime(2026, 6, 20, 13, 30), remaining_fraction=0.2
            )
            linear = get_pv_forecast(cfg, now=dt.datetime(2026, 6, 20, 13, 30))

        # 6.0 kWh/m² × 8 kWp × 0.75 = 36 kWh today; 20% left is 7.2 kWh.
        self.assertEqual(corrected["today_kwh"], 36.0)
        self.assertEqual(corrected["today_remaining_kwh"], 7.2)
        self.assertEqual(corrected["today_remaining_fraction"], 0.2)
        self.assertGreater(linear["today_remaining_kwh"], 14.0)


class GetTomorrowSolarKwhM2Tests(unittest.TestCase):
    def _make_forecast(self, tomorrow_date: dt.date, radiation_values: list) -> dict:
        """Build a minimal Open-Meteo-style forecast with shortwave_radiation for tomorrow."""
        times = [
            f"{tomorrow_date.isoformat()}T{h:02d}:00" for h in range(len(radiation_values))
        ]
        return {"hourly": {"time": times, "shortwave_radiation": radiation_values}}

    def test_sums_tomorrow_radiation_to_kwh(self):
        from growatt_guard.weather import get_tomorrow_solar_kwh_m2
        cfg = make_config(weather_lat=6.5, weather_lon=3.4)
        now = dt.datetime(2026, 6, 20, 22, 0)
        tomorrow = dt.date(2026, 6, 21)
        # 24 hours × 250 W/m² = 6000 Wh/m² = 6.0 kWh/m²
        forecast = self._make_forecast(tomorrow, [250.0] * 24)
        with patch("growatt_guard.weather.fetch_weather_forecast", return_value=forecast):
            result = get_tomorrow_solar_kwh_m2(cfg, now=now)
        self.assertAlmostEqual(result, 6.0)

    def test_only_counts_tomorrows_date(self):
        from growatt_guard.weather import get_tomorrow_solar_kwh_m2
        cfg = make_config(weather_lat=6.5, weather_lon=3.4)
        now = dt.datetime(2026, 6, 20, 22, 0)
        today = dt.date(2026, 6, 20)
        tomorrow = dt.date(2026, 6, 21)
        # Mix today and tomorrow values in the forecast
        times = (
            [f"{today.isoformat()}T{h:02d}:00" for h in range(12)]
            + [f"{tomorrow.isoformat()}T{h:02d}:00" for h in range(12)]
        )
        radiation = [1000.0] * 12 + [200.0] * 12
        forecast = {"hourly": {"time": times, "shortwave_radiation": radiation}}
        with patch("growatt_guard.weather.fetch_weather_forecast", return_value=forecast):
            result = get_tomorrow_solar_kwh_m2(cfg, now=now)
        self.assertAlmostEqual(result, 200.0 * 12 / 1000)

    def test_returns_none_when_shortwave_radiation_absent(self):
        from growatt_guard.weather import get_tomorrow_solar_kwh_m2
        cfg = make_config(weather_lat=6.5, weather_lon=3.4)
        forecast = {"hourly": {"time": ["2026-06-21T00:00"], "cloud_cover": [50]}}
        with patch("growatt_guard.weather.fetch_weather_forecast", return_value=forecast):
            result = get_tomorrow_solar_kwh_m2(cfg, now=dt.datetime(2026, 6, 20, 22, 0))
        self.assertIsNone(result)

    def test_returns_none_when_no_coords(self):
        from growatt_guard.weather import get_tomorrow_solar_kwh_m2
        cfg = make_config()  # no weather_lat / weather_lon
        result = get_tomorrow_solar_kwh_m2(cfg, now=dt.datetime(2026, 6, 20, 22, 0))
        self.assertIsNone(result)

    def test_returns_none_on_api_failure(self):
        from growatt_guard.weather import get_tomorrow_solar_kwh_m2
        cfg = make_config(weather_lat=6.5, weather_lon=3.4)
        with patch("growatt_guard.weather.fetch_weather_forecast", side_effect=RuntimeError("timeout")):
            result = get_tomorrow_solar_kwh_m2(cfg, now=dt.datetime(2026, 6, 20, 22, 0))
        self.assertIsNone(result)

    def test_summarizes_tomorrow_as_rainy_cloudy(self):
        from growatt_guard.weather import summarize_forecast_day_weather
        tomorrow = dt.date(2026, 6, 21)
        forecast = {
            "hourly": {
                "time": [f"{tomorrow.isoformat()}T{hour:02d}:00" for hour in range(24)],
                "cloud_cover": [90.0] * 24,
                "precipitation": [0.5] * 24,
            }
        }

        result = summarize_forecast_day_weather(forecast, tomorrow)

        self.assertEqual(result["weather_category"], "rainy/cloudy")
        self.assertEqual(result["cloud_cover"], 90.0)
        self.assertEqual(result["precipitation_mm"], 12.0)

    def test_pv_forecast_includes_tomorrow_weather_regime(self):
        from growatt_guard.weather import get_pv_forecast
        cfg = make_config(
            weather_lat=6.5,
            weather_lon=3.4,
            panel_kwp=8.0,
            panel_performance_ratio=0.75,
        )
        now = dt.datetime(2026, 6, 20, 22, 0)
        tomorrow = dt.date(2026, 6, 21)
        forecast = {
            "hourly": {
                "time": [f"{tomorrow.isoformat()}T{hour:02d}:00" for hour in range(24)],
                "shortwave_radiation": [250.0] * 24,
                "cloud_cover": [80.0] * 24,
                "precipitation": [0.25] * 24,
            }
        }

        with patch("growatt_guard.weather.fetch_weather_forecast", return_value=forecast):
            result = get_pv_forecast(cfg, now=now)

        self.assertIsNotNone(result)
        self.assertEqual(result["tomorrow_kwh"], 36.0)
        self.assertEqual(result["tomorrow_weather_category"], "rainy/cloudy")
        self.assertEqual(result["tomorrow_cloud_cover"], 80.0)
        self.assertEqual(result["tomorrow_precipitation_mm"], 6.0)


class WeatherWindowStartTests(unittest.TestCase):
    @staticmethod
    def _two_day_forecast(evening_cloud: float, morning_cloud: float, morning_rain: float = 0.0) -> dict:
        times: list[str] = []
        clouds: list[float] = []
        rain: list[float] = []
        base = dt.datetime(2026, 11, 24, 0, 0)
        for hour_offset in range(48):
            ts = base + dt.timedelta(hours=hour_offset)
            times.append(ts.isoformat(timespec="minutes"))
            if ts.date() == dt.date(2026, 11, 24) and 18 <= ts.hour <= 22:
                clouds.append(evening_cloud)
                rain.append(0.0)
            elif ts.date() == dt.date(2026, 11, 25) and 6 <= ts.hour <= 11:
                clouds.append(morning_cloud)
                rain.append(morning_rain if ts.hour == 8 else 0.0)
            else:
                clouds.append(10.0)
                rain.append(0.0)
        return {"hourly": {"time": times, "cloud_cover": clouds, "precipitation": rain}}

    def test_clear_evening_normal_morning_uses_morning_window(self):
        forecast = self._two_day_forecast(evening_cloud=5.0, morning_cloud=45.0)
        cfg = make_config(weather_enabled=True, season_profiles_enabled=True, weather_timezone="Africa/Lagos")
        morning = dt.datetime(2026, 11, 25, 6, 38, tzinfo=dt.timezone(dt.timedelta(hours=1)))

        with patch("growatt_guard.weather.fetch_weather_forecast", return_value=forecast):
            decision = choose_preserve_threshold(cfg, today=morning.date(), window_start=morning)

        self.assertEqual(decision.weather_category, "normal")
        self.assertEqual(decision.threshold, 40.0)

    def test_clear_evening_rainy_morning_uses_morning_window(self):
        forecast = self._two_day_forecast(evening_cloud=0.0, morning_cloud=85.0, morning_rain=1.5)
        cfg = make_config(weather_enabled=True, season_profiles_enabled=True, weather_timezone="Africa/Lagos")
        morning = dt.datetime(2026, 11, 25, 6, 38, tzinfo=dt.timezone(dt.timedelta(hours=1)))

        with patch("growatt_guard.weather.fetch_weather_forecast", return_value=forecast):
            decision = choose_preserve_threshold(cfg, today=morning.date(), window_start=morning)

        self.assertEqual(decision.weather_category, "rainy/cloudy")
        self.assertEqual(decision.threshold, 45.0)

    def test_cloudy_evening_sunny_morning_uses_morning_window(self):
        forecast = self._two_day_forecast(evening_cloud=95.0, morning_cloud=15.0)
        cfg = make_config(weather_enabled=True, season_profiles_enabled=True, weather_timezone="Africa/Lagos")
        morning = dt.datetime(2026, 11, 25, 6, 38, tzinfo=dt.timezone(dt.timedelta(hours=1)))

        with patch("growatt_guard.weather.fetch_weather_forecast", return_value=forecast):
            decision = choose_preserve_threshold(cfg, today=morning.date(), window_start=morning)

        self.assertEqual(decision.weather_category, "sunny")
        self.assertEqual(decision.threshold, 35.0)

    def test_utc_aware_window_start_converts_to_weather_timezone(self):
        # 06:00 Lagos is 05:00 UTC. Put clouds ONLY at 10:00 Lagos (09:00 UTC) so
        # a UTC-unadjusted 05:38..09:38 window misses it, while Lagos 06:38..10:38 includes 10:00.
        base = dt.datetime(2026, 11, 25, 0, 0)
        times = [(base + dt.timedelta(hours=h)).isoformat(timespec="minutes") for h in range(24)]
        clouds = [50.0 if h == 10 else 5.0 for h in range(24)]
        rain = [0.0] * 24
        forecast = {"hourly": {"time": times, "cloud_cover": clouds, "precipitation": rain}}
        cfg = make_config(weather_enabled=True, weather_lookahead_hours=4, weather_timezone="Africa/Lagos")
        utc_start = dt.datetime(2026, 11, 25, 5, 38, tzinfo=dt.timezone.utc)

        decision = analyze_weather_window(cfg, forecast, window_start=utc_start)

        self.assertEqual(decision.weather_category, "normal")
        self.assertEqual(decision.cloud_cover, 50.0)

    def test_explicit_window_outside_forecast_fails_closed(self):
        forecast = self._two_day_forecast(evening_cloud=0.0, morning_cloud=0.0)
        cfg = make_config(weather_enabled=True, season_profiles_enabled=True, weather_timezone="Africa/Lagos")
        far_future = dt.datetime(2026, 11, 28, 6, 38, tzinfo=dt.timezone(dt.timedelta(hours=1)))

        with self.assertRaises(GrowattGuardError):
            analyze_weather_window(cfg, forecast, window_start=far_future)

        with patch("growatt_guard.weather.fetch_weather_forecast", return_value=forecast):
            decision = choose_preserve_threshold(cfg, window_start=far_future)

        self.assertEqual(decision.weather_category, "unavailable")
        self.assertEqual(decision.threshold, 45.0)


if __name__ == "__main__":
    unittest.main()
