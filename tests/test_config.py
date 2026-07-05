import unittest
from unittest.mock import patch

from helpers import make_config
from growatt_guard.config import load_config, validate_config


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

    def test_auto_topup_without_battery_specs_warns(self):
        config = make_config(auto_topup_enabled=True, battery_capacity_wh=0, battery_charge_rate_w=0)

        warnings = validate_config(config)

        self.assertTrue(any("AUTO_TOPUP_ENABLED" in w for w in warnings))

    def test_auto_topup_with_battery_specs_no_warning(self):
        config = make_config(auto_topup_enabled=True, battery_capacity_wh=30_000, battery_charge_rate_w=3_000)

        warnings = validate_config(config)

        self.assertFalse(any("AUTO_TOPUP_ENABLED" in w for w in warnings))


class LoadConfigTests(unittest.TestCase):
    def _load_with_env(self, env):
        with patch("growatt_guard.config.load_dotenv", return_value=None), patch.dict("os.environ", env, clear=True):
            return load_config()

    def test_load_config_defaults_to_safe_dry_run(self):
        config = self._load_with_env({"GROWATT_USERNAME": "user", "GROWATT_PASSWORD": "pass"})

        self.assertTrue(config.dry_run)
        self.assertEqual(config.server_url, "https://openapi.growatt.com/")
        self.assertEqual(config.mode_driver, "spf5000")
        self.assertEqual(config.discord_control_allowed_user_ids, ())
        self.assertEqual(config.panel_performance_ratio, 0.75)

    def test_load_config_parses_typed_values(self):
        config = self._load_with_env(
            {
                "GROWATT_USERNAME": "user",
                "GROWATT_PASSWORD": "pass",
                "DRY_RUN": "false",
                "LOW_BATTERY_SOC": "51",
                "WEATHER_ENABLED": "true",
                "WEATHER_LAT": "6.5",
                "WEATHER_LON": "3.4",
                "DISCORD_CONTROL_ALLOWED_USER_IDS": "111, 222 ,,333",
                "AUTO_TOPUP_ENABLED": "yes",
                "BATTERY_CAPACITY_WH": "30000",
                "BATTERY_CHARGE_RATE_W": "2500",
                "GROWATT_SESSION_TTL_MINUTES": "60",
                "PANEL_KWP": "8.2",
                "PANEL_PERFORMANCE_RATIO": "0.7",
            }
        )

        self.assertFalse(config.dry_run)
        self.assertEqual(config.low_battery_soc, 51)
        self.assertTrue(config.weather_enabled)
        self.assertEqual(config.weather_lat, 6.5)
        self.assertEqual(config.weather_lon, 3.4)
        self.assertEqual(config.discord_control_allowed_user_ids, ("111", "222", "333"))
        self.assertTrue(config.auto_topup_enabled)
        self.assertEqual(config.battery_capacity_wh, 30000)
        self.assertEqual(config.battery_charge_rate_w, 2500)
        self.assertEqual(config.growatt_session_ttl_minutes, 60)
        self.assertEqual(config.panel_kwp, 8.2)
        self.assertEqual(config.panel_performance_ratio, 0.7)

    def test_load_config_custom_driver_without_params_falls_back_to_spf5000(self):
        config = self._load_with_env(
            {
                "GROWATT_USERNAME": "user",
                "GROWATT_PASSWORD": "pass",
                "GROWATT_MODE_DRIVER": "custom",
            }
        )

        self.assertEqual(config.mode_driver, "spf5000")

    def test_load_config_custom_driver_keeps_params(self):
        config = self._load_with_env(
            {
                "GROWATT_USERNAME": "user",
                "GROWATT_PASSWORD": "pass",
                "GROWATT_MODE_DRIVER": "custom",
                "GROWATT_UTILITY_MODE_PARAMS": "{\"mode\":\"utility\"}",
            }
        )

        self.assertEqual(config.mode_driver, "custom")
        self.assertIn("utility", config.utility_mode_params)

    def test_load_config_missing_credentials_raises(self):
        with patch("growatt_guard.config.load_dotenv", return_value=None), patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(Exception) as ctx:
                load_config()

        self.assertIn("Missing GROWATT_USERNAME", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
