from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from growatt_guard.exceptions import GrowattGuardError

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - handled at runtime for friendlier output
    load_dotenv = None


from growatt_guard.paths import DATA_HOME


BASE_DIR = DATA_HOME


@dataclass(frozen=True)
class AppHealthTarget:
    name: str
    url: str
    container: str
    host_header: str = ""


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
    audit_retention_days: int = 90
    emergency_soc: float = 30
    emergency_soc_recovery: float = 35
    bypass_alert_soc: float = 40
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
    growatt_api_token: str = ""
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
    preserve_utility_max_attempts: int = 2
    preserve_utility_retry_delay_seconds: float = 30.0
    morning_solar_bridge_enabled: bool = False
    morning_solar_bridge_safety_floor_soc: float = 35.0
    morning_solar_bridge_start_hour: int = 6
    morning_solar_bridge_recovery_hour: int = 10
    morning_solar_bridge_load_factor: float = 1.25
    auto_topup_enabled: bool = False
    auto_topup_min_hours_to_sunrise: float = 4.0
    auto_topup_min_minutes: float = 0.0
    auto_topup_target_soc: float = 0.0
    auto_topup_solar_skip_kwh_m2: float = 0.0
    auto_topup_solar_skip_min_margin_minutes: float = 60.0
    runtime_alert_minutes: float = 0.0
    runtime_alert_clear_minutes: float = 0.0
    growatt_session_ttl_minutes: float = 0.0
    betterstack_heartbeat_url: str = ""
    auto_topup_sunrise_floor_soc: float = 35.0
    auto_topup_sunrise_buffer_soc: float = 38.0
    auto_topup_unusual_soc_threshold: float = 70.0
    panel_kwp: float = 0.0
    panel_performance_ratio: float = 0.75
    min_sbu_return_soc: float = 30.0
    app_health_targets: tuple[AppHealthTarget, ...] = ()
    app_health_failure_threshold: int = 3
    app_health_timeout_seconds: float = 5.0
    app_health_recovery_enabled: bool = False
    app_health_recovery_cooldown_minutes: float = 60.0
    app_health_recovery_wait_seconds: float = 10.0


def config_error(message: str) -> GrowattGuardError:
    return GrowattGuardError(message)


def str_to_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "y", "on"}


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def optional_float(value: str) -> float | None:
    return float(value) if value else None


def csv_env(name: str) -> tuple[str, ...]:
    value = env(name)
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def parse_app_health_targets(value: str) -> tuple[AppHealthTarget, ...]:
    if not value.strip():
        return ()

    targets: list[AppHealthTarget] = []
    names: set[str] = set()
    for raw_target in value.split(","):
        parts = tuple(part.strip() for part in raw_target.split("|"))
        if len(parts) not in {3, 4} or not all(parts):
            raise config_error(
                "APP_HEALTH_TARGETS entries must use "
                "name|http://127.0.0.1:port/health|container[|host-header]."
            )
        name, url, container = parts[:3]
        host_header = parts[3] if len(parts) == 4 else ""
        normalized_name = name.lower()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", name):
            raise config_error(f"Invalid app health target name: {name!r}.")
        if normalized_name in names:
            raise config_error(f"Duplicate app health target name: {name!r}.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
            raise config_error(f"Invalid app health container name for {name!r}.")
        if host_header:
            labels = host_header.split(".")
            if len(host_header) > 253 or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in labels
            ):
                raise config_error(f"Invalid app health Host header for {name!r}.")

        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise config_error(f"App health target {name!r} must use a loopback HTTP URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise config_error(f"App health target {name!r} URL must not contain credentials, query, or fragment.")
        try:
            port = parsed.port
        except ValueError as exc:
            raise config_error(f"Invalid app health target port for {name!r}.") from exc
        if port is None or not parsed.path.startswith("/"):
            raise config_error(f"App health target {name!r} requires an explicit port and path.")

        names.add(normalized_name)
        targets.append(
            AppHealthTarget(
                name=name,
                url=url,
                container=container,
                host_header=host_header,
            )
        )
    return tuple(targets)


def validate_config(config: Config) -> list[str]:
    warnings: list[str] = []
    if not 0 <= config.min_sbu_return_soc <= 100:
        warnings.append("MIN_SBU_RETURN_SOC must be between 0 and 100")
    if config.load_aware_threshold and not config.weather_enabled:
        warnings.append(
            "LOAD_AWARE_THRESHOLD=true has no effect without WEATHER_ENABLED=true"
        )
    if config.morning_solar_bridge_enabled:
        if not config.weather_enabled:
            warnings.append("MORNING_SOLAR_BRIDGE_ENABLED requires WEATHER_ENABLED=true")
        if config.battery_capacity_wh <= 0 or config.panel_kwp <= 0:
            warnings.append(
                "MORNING_SOLAR_BRIDGE_ENABLED requires BATTERY_CAPACITY_WH and PANEL_KWP"
            )
    if not 0 <= config.morning_solar_bridge_safety_floor_soc <= 100:
        warnings.append("MORNING_SOLAR_BRIDGE_SAFETY_FLOOR_SOC must be between 0 and 100")
    if not 0 <= config.morning_solar_bridge_start_hour <= 22:
        warnings.append("MORNING_SOLAR_BRIDGE_START_HOUR must be between 0 and 22")
    if not 1 <= config.morning_solar_bridge_recovery_hour <= 23:
        warnings.append("MORNING_SOLAR_BRIDGE_RECOVERY_HOUR must be between 1 and 23")
    if config.morning_solar_bridge_start_hour >= config.morning_solar_bridge_recovery_hour:
        warnings.append("MORNING_SOLAR_BRIDGE_START_HOUR must be before MORNING_SOLAR_BRIDGE_RECOVERY_HOUR")
    if config.morning_solar_bridge_load_factor < 1:
        warnings.append("MORNING_SOLAR_BRIDGE_LOAD_FACTOR must be at least 1")
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
    if config.app_health_failure_threshold < 1:
        warnings.append("APP_HEALTH_FAILURE_THRESHOLD must be at least 1")
    if config.app_health_timeout_seconds <= 0:
        warnings.append("APP_HEALTH_TIMEOUT_SECONDS must be greater than 0")
    if config.app_health_recovery_cooldown_minutes < 1:
        warnings.append("APP_HEALTH_RECOVERY_COOLDOWN_MINUTES must be at least 1")
    if config.app_health_recovery_wait_seconds < 0:
        warnings.append("APP_HEALTH_RECOVERY_WAIT_SECONDS must not be negative")
    if config.app_health_recovery_enabled and not config.app_health_targets:
        warnings.append("APP_HEALTH_RECOVERY_ENABLED=true has no effect without APP_HEALTH_TARGETS")
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
        audit_retention_days=int(env("AUDIT_RETENTION_DAYS", "90")),
        emergency_soc=float(env("EMERGENCY_SOC", "30")),
        emergency_soc_recovery=float(env("EMERGENCY_SOC_RECOVERY", "35")),
        bypass_alert_soc=float(env("BYPASS_ALERT_SOC", "40")),
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
        growatt_api_token=env("GROWATT_API_TOKEN"),
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
        preserve_utility_max_attempts=int(env("PRESERVE_UTILITY_MAX_ATTEMPTS", "2")),
        preserve_utility_retry_delay_seconds=float(env("PRESERVE_UTILITY_RETRY_DELAY_SECONDS", "30")),
        morning_solar_bridge_enabled=str_to_bool(
            env("MORNING_SOLAR_BRIDGE_ENABLED"), default=False
        ),
        morning_solar_bridge_safety_floor_soc=float(
            env("MORNING_SOLAR_BRIDGE_SAFETY_FLOOR_SOC", "35")
        ),
        morning_solar_bridge_start_hour=int(
            env("MORNING_SOLAR_BRIDGE_START_HOUR", "6")
        ),
        morning_solar_bridge_recovery_hour=int(
            env("MORNING_SOLAR_BRIDGE_RECOVERY_HOUR", "10")
        ),
        morning_solar_bridge_load_factor=float(
            env("MORNING_SOLAR_BRIDGE_LOAD_FACTOR", "1.25")
        ),
        auto_topup_enabled=str_to_bool(env("AUTO_TOPUP_ENABLED"), default=False),
        auto_topup_min_hours_to_sunrise=float(env("AUTO_TOPUP_MIN_HOURS_TO_SUNRISE", "4")),
        auto_topup_min_minutes=float(env("AUTO_TOPUP_MIN_MINUTES", "0")),
        auto_topup_target_soc=float(env("AUTO_TOPUP_TARGET_SOC", "0")),
        auto_topup_solar_skip_kwh_m2=float(env("AUTO_TOPUP_SOLAR_SKIP_KWH_M2", "0")),
        auto_topup_solar_skip_min_margin_minutes=float(env("AUTO_TOPUP_SOLAR_SKIP_MIN_MARGIN_MINUTES", "60")),
        runtime_alert_minutes=float(env("RUNTIME_ALERT_MINUTES", "0")),
        runtime_alert_clear_minutes=float(env("RUNTIME_ALERT_CLEAR_MINUTES", "0")),
        growatt_session_ttl_minutes=float(env("GROWATT_SESSION_TTL_MINUTES", "0")),
        betterstack_heartbeat_url=env("BETTERSTACK_HEARTBEAT_URL"),
        auto_topup_sunrise_floor_soc=float(env("AUTO_TOPUP_SUNRISE_FLOOR_SOC", "35")),
        auto_topup_sunrise_buffer_soc=float(env("AUTO_TOPUP_SUNRISE_BUFFER_SOC", "38")),
        auto_topup_unusual_soc_threshold=float(env("AUTO_TOPUP_UNUSUAL_SOC_THRESHOLD", "70")),
        panel_kwp=float(env("PANEL_KWP", "0")),
        panel_performance_ratio=float(env("PANEL_PERFORMANCE_RATIO", "0.75")),
        min_sbu_return_soc=float(env("MIN_SBU_RETURN_SOC", "30")),
        app_health_targets=parse_app_health_targets(env("APP_HEALTH_TARGETS")),
        app_health_failure_threshold=int(env("APP_HEALTH_FAILURE_THRESHOLD", "3")),
        app_health_timeout_seconds=float(env("APP_HEALTH_TIMEOUT_SECONDS", "5")),
        app_health_recovery_enabled=str_to_bool(env("APP_HEALTH_RECOVERY_ENABLED"), default=False),
        app_health_recovery_cooldown_minutes=float(env("APP_HEALTH_RECOVERY_COOLDOWN_MINUTES", "60")),
        app_health_recovery_wait_seconds=float(env("APP_HEALTH_RECOVERY_WAIT_SECONDS", "10")),
    )
