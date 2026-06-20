import datetime as dt
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_power_guard import (
    ThresholdDecision,
    analyze_weather_window,
    choose_preserve_threshold,
)


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


if __name__ == "__main__":
    unittest.main()
