from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - handled at runtime for friendlier output
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    server_url: str
    plant_id: str | None
    device_sn: str | None
    low_battery_soc: float
    dry_run: bool
    mode_driver: str
    set_mode_path: str
    set_mode_method: str
    utility_mode_params: str
    sbu_mode_params: str
    discord_webhook_url: str
    discord_notify_success: bool
    discord_notify_skip: bool
    discord_notify_failure: bool
    log_retention_days: int
    emergency_soc: float = 30
    emergency_soc_recovery: float = 35
    cloud_failure_alert_threshold: int = 3
    dashboard_stale_minutes: float = 30
    weather_enabled: bool = False
    weather_lat: float | None = None
    weather_lon: float | None = None
    weather_timezone: str = "Africa/Lagos"
    weather_lookahead_hours: int = 4
    weather_cloudy_threshold: float = 70
    weather_sunny_threshold: float = 35
    weather_rain_threshold_mm: float = 1
    low_battery_soc_normal: float = 45
    low_battery_soc_sunny: float = 40
    season_profiles_enabled: bool = False
    pvoutput_enabled: bool = False
    pvoutput_api_key: str = ""
    pvoutput_system_id: str = ""
    discord_bot_token: str = ""
    discord_control_channel_id: str = ""
    discord_control_allowed_user_ids: tuple[str, ...] = ()
    discord_control_guild_id: str = ""
    discord_topup_max_minutes: int = 180
    battery_capacity_wh: float = 0.0
    battery_bms_cutoff_soc: float = 25.0
    battery_charge_rate_w: float = 0.0
    load_aware_threshold: bool = False
    battery_charge_target_soc: float = 0.0
    auto_topup_enabled: bool = False
    auto_topup_min_hours_to_sunrise: float = 4.0
    runtime_alert_minutes: float = 0.0
    runtime_alert_clear_minutes: float = 0.0


def app_module() -> Any:
    module = sys.modules.get("growatt_power_guard")
    if module is not None and hasattr(module, "GrowattGuardError"):
        return module

    main_module = sys.modules.get("__main__")
    if main_module is not None and hasattr(main_module, "GrowattGuardError"):
        return main_module

    import growatt_power_guard

    return growatt_power_guard


def config_error(message: str) -> Exception:
    return app_module().GrowattGuardError(message)


def str_to_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def optional_float(value: str) -> float | None:
    return float(value) if value else None


def csv_env(name: str) -> tuple[str, ...]:
    value = env(name)
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def validate_config(config: Config) -> list[str]:
    warnings: list[str] = []
    if config.load_aware_threshold and not config.weather_enabled:
        warnings.append(
            "LOAD_AWARE_THRESHOLD=true has no effect without WEATHER_ENABLED=true"
        )
    if config.battery_capacity_wh > 0 and config.battery_charge_rate_w <= 0:
        warnings.append(
            "BATTERY_CAPACITY_WH is set but BATTERY_CHARGE_RATE_W=0; "
            "/growatt_topup target_soc and topup-to-sunrise estimate will not work"
        )
    if config.weather_enabled and (config.weather_lat is None or config.weather_lon is None):
        warnings.append(
            "WEATHER_ENABLED=true but WEATHER_LAT and/or WEATHER_LON are not set"
        )
    if config.auto_topup_enabled and (config.battery_capacity_wh <= 0 or config.battery_charge_rate_w <= 0):
        warnings.append(
            "AUTO_TOPUP_ENABLED requires BATTERY_CAPACITY_WH and BATTERY_CHARGE_RATE_W to be configured"
        )
    return warnings


def load_config() -> Config:
    if load_dotenv is not None:
        load_dotenv(BASE_DIR / ".env")

    username = env("GROWATT_USERNAME")
    password = env("GROWATT_PASSWORD")
    if not username or not password:
        raise config_error(
            "Missing GROWATT_USERNAME or GROWATT_PASSWORD. Copy .env.example to .env and fill them in."
        )

    mode_driver = env("GROWATT_MODE_DRIVER", "spf5000").lower()
    utility_mode_params = env("GROWATT_UTILITY_MODE_PARAMS")
    sbu_mode_params = env("GROWATT_SBU_MODE_PARAMS")
    if mode_driver == "custom" and not utility_mode_params and not sbu_mode_params:
        mode_driver = "spf5000"

    return Config(
        username=username,
        password=password,
        server_url=env("GROWATT_SERVER_URL", "https://openapi.growatt.com/"),
        plant_id=env("GROWATT_PLANT_ID") or None,
        device_sn=env("GROWATT_DEVICE_SN") or None,
        low_battery_soc=float(env("LOW_BATTERY_SOC", "45")),
        dry_run=str_to_bool(env("DRY_RUN"), default=True),
        mode_driver=mode_driver,
        set_mode_path=env("GROWATT_SET_MODE_PATH", "tcpSet.do"),
        set_mode_method=env("GROWATT_SET_MODE_METHOD", "post").lower(),
        utility_mode_params=utility_mode_params,
        sbu_mode_params=sbu_mode_params,
        discord_webhook_url=env("DISCORD_WEBHOOK_URL"),
        discord_notify_success=str_to_bool(env("DISCORD_NOTIFY_SUCCESS"), default=True),
        discord_notify_skip=str_to_bool(env("DISCORD_NOTIFY_SKIP"), default=False),
        discord_notify_failure=str_to_bool(env("DISCORD_NOTIFY_FAILURE"), default=True),
        log_retention_days=int(env("LOG_RETENTION_DAYS", "30")),
        emergency_soc=float(env("EMERGENCY_SOC", "30")),
        emergency_soc_recovery=float(env("EMERGENCY_SOC_RECOVERY", "35")),
        cloud_failure_alert_threshold=int(env("GROWATT_CLOUD_FAILURE_ALERT_THRESHOLD", "3")),
        dashboard_stale_minutes=float(env("DASHBOARD_STALE_MINUTES", "30")),
        weather_enabled=str_to_bool(env("WEATHER_ENABLED"), default=False),
        weather_lat=optional_float(env("WEATHER_LAT")),
        weather_lon=optional_float(env("WEATHER_LON")),
        weather_timezone=env("WEATHER_TIMEZONE", "Africa/Lagos"),
        weather_lookahead_hours=int(env("WEATHER_LOOKAHEAD_HOURS", "4")),
        weather_cloudy_threshold=float(env("WEATHER_CLOUDY_THRESHOLD", "70")),
        weather_sunny_threshold=float(env("WEATHER_SUNNY_THRESHOLD", "35")),
        weather_rain_threshold_mm=float(env("WEATHER_RAIN_THRESHOLD_MM", "1")),
        low_battery_soc_normal=float(env("LOW_BATTERY_SOC_NORMAL", "45")),
        low_battery_soc_sunny=float(env("LOW_BATTERY_SOC_SUNNY", "40")),
        season_profiles_enabled=str_to_bool(env("SEASON_PROFILES_ENABLED"), default=False),
        pvoutput_enabled=str_to_bool(env("PVOUTPUT_ENABLED"), default=False),
        pvoutput_api_key=env("PVOUTPUT_API_KEY"),
        pvoutput_system_id=env("PVOUTPUT_SYSTEM_ID"),
        discord_bot_token=env("DISCORD_BOT_TOKEN"),
        discord_control_channel_id=env("DISCORD_CONTROL_CHANNEL_ID"),
        discord_control_allowed_user_ids=csv_env("DISCORD_CONTROL_ALLOWED_USER_IDS"),
        discord_control_guild_id=env("DISCORD_CONTROL_GUILD_ID"),
        discord_topup_max_minutes=int(env("DISCORD_TOPUP_MAX_MINUTES", "180")),
        battery_capacity_wh=float(env("BATTERY_CAPACITY_WH", "0")),
        battery_bms_cutoff_soc=float(env("BATTERY_BMS_CUTOFF_SOC", "25")),
        battery_charge_rate_w=float(env("BATTERY_CHARGE_RATE_W", "0")),
        load_aware_threshold=str_to_bool(env("LOAD_AWARE_THRESHOLD"), default=False),
        battery_charge_target_soc=float(env("BATTERY_CHARGE_TARGET_SOC", "0")),
        auto_topup_enabled=str_to_bool(env("AUTO_TOPUP_ENABLED"), default=False),
        auto_topup_min_hours_to_sunrise=float(env("AUTO_TOPUP_MIN_HOURS_TO_SUNRISE", "4")),
        runtime_alert_minutes=float(env("RUNTIME_ALERT_MINUTES", "0")),
        runtime_alert_clear_minutes=float(env("RUNTIME_ALERT_CLEAR_MINUTES", "0")),
    )
