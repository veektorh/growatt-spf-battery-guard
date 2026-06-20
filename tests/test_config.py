import unittest

from helpers import make_config
from growatt_guard.config import validate_config


class ValidateConfigTests(unittest.TestCase):
    def test_no_warnings_for_clean_config(self):
        config = make_config(
            weather_enabled=True,
            weather_lat=6.5,
            weather_lon=3.4,
            battery_capacity_wh=30_000,
            battery_charge_rate_w=3_000,
        )
        self.assertEqual(validate_config(config), [])

    def test_load_aware_without_weather_warns(self):
        config = make_config(
            load_aware_threshold=True,
            weather_enabled=False,
        )
        warnings = validate_config(config)
        self.assertEqual(len(warnings), 1)
        self.assertIn("LOAD_AWARE_THRESHOLD", warnings[0])
        self.assertIn("WEATHER_ENABLED", warnings[0])

    def test_load_aware_with_weather_no_warning(self):
        config = make_config(
            load_aware_threshold=True,
            weather_enabled=True,
            weather_lat=6.5,
            weather_lon=3.4,
        )
        warnings = validate_config(config)
        self.assertFalse(any("LOAD_AWARE_THRESHOLD" in w for w in warnings))

    def test_capacity_set_without_charge_rate_warns(self):
        config = make_config(battery_capacity_wh=30_000, battery_charge_rate_w=0)
        warnings = validate_config(config)
        self.assertEqual(len(warnings), 1)
        self.assertIn("BATTERY_CHARGE_RATE_W", warnings[0])

    def test_capacity_zero_no_charge_rate_warning(self):
        config = make_config(battery_capacity_wh=0, battery_charge_rate_w=0)
        warnings = validate_config(config)
        self.assertFalse(any("BATTERY_CHARGE_RATE_W" in w for w in warnings))

    def test_weather_enabled_without_lat_lon_warns(self):
        config = make_config(weather_enabled=True, weather_lat=None, weather_lon=None)
        warnings = validate_config(config)
        self.assertTrue(any("WEATHER_LAT" in w for w in warnings))

    def test_weather_enabled_with_only_lat_warns(self):
        config = make_config(weather_enabled=True, weather_lat=6.5, weather_lon=None)
        warnings = validate_config(config)
        self.assertTrue(any("WEATHER_LAT" in w for w in warnings))

    def test_multiple_warnings_returned(self):
        config = make_config(
            load_aware_threshold=True,
            weather_enabled=False,
            battery_capacity_wh=30_000,
            battery_charge_rate_w=0,
        )
        warnings = validate_config(config)
        self.assertEqual(len(warnings), 2)


if __name__ == "__main__":
    unittest.main()
