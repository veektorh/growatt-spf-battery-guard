from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import http.server
import json
import logging
import os
import re
import socketserver
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - handled at runtime for friendlier output
    load_dotenv = None

try:
    import growattServer
except ImportError:  # pragma: no cover - handled at runtime for friendlier output
    growattServer = None


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "growatt_power_guard.log"
MODE_AUDIT_FILE = LOG_DIR / "mode_decisions.csv"
DASHBOARD_FILE = BASE_DIR / "dashboard.html"
MIN_DASHBOARD_REFRESH_MINUTES = 5
SCHEDULE_FILE = BASE_DIR / "schedule.json"
SCHEDULE_OVERRIDES_FILE = BASE_DIR / "schedule_overrides.json"
STATE_DIR = BASE_DIR / "state"
PAUSE_FILE = STATE_DIR / "automation_pause.json"
BATTERY_ALERT_FILE = STATE_DIR / "battery_alert.json"
DASHBOARD_STALE_ALERT_FILE = STATE_DIR / "dashboard_stale_alert.json"
COMMAND_LOCK_FILE = STATE_DIR / "mode_command.lock"
GROWATT_CLOUD_FAILURE_FILE = STATE_DIR / "growatt_cloud_failures.json"
COMMAND_LOCK_STALE_SECONDS = 45 * 60

SOC_KEYS = (
    "SOC",
    "soc",
    "capacity",
    "batteryCapacity",
    "batterySoc",
    "batCapacity",
    "batteryPercent",
    "battery_percentage",
    "eCapacity",
)

SPF_OUTPUT_SOURCE = {
    "0": "SBU priority",
    "1": "Solar first",
    "2": "Utility first",
    "3": "SUB priority",
}

DEVICE_TYPE_PRIORITY = ("storage", "mix", "sph", "tlx", "inverter")
SCHEDULE_COMMANDS = {
    "preserve-battery",
    "utility-check",
    "morning-check",
    "return-sbu",
    "watchdog-sbu",
    "daily-summary",
    "rotate-logs",
    "health-check",
    "battery-alert",
    "weekly-summary",
    "dashboard-stale-alert",
}
SCHEDULE_COMMAND_ARGS = {
    "health-check": {"--notify"},
}
PAUSABLE_COMMANDS = {"preserve-battery", "utility-check", "morning-check", "return-sbu", "watchdog-sbu"}
LOCKED_COMMANDS = PAUSABLE_COMMANDS


class GrowattGuardError(RuntimeError):
    pass


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


@dataclass(frozen=True)
class ThresholdDecision:
    threshold: float
    reason: str
    weather_category: str = "disabled"
    cloud_cover: float | None = None
    precipitation_mm: float | None = None


@dataclass(frozen=True)
class HealthCheckItem:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class DeviceRef:
    plant_id: str
    device_sn: str
    device_type: str
    raw: dict[str, Any]


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


def load_config() -> Config:
    if load_dotenv is not None:
        load_dotenv(BASE_DIR / ".env")

    username = env("GROWATT_USERNAME")
    password = env("GROWATT_PASSWORD")
    if not username or not password:
        raise GrowattGuardError(
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
    )


def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    console_handler.setLevel(level)
    root.addHandler(console_handler)


def truncate_discord_message(message: str) -> str:
    if len(message) <= 1900:
        return message
    return message[:1890] + "...[truncated]"


def send_discord_message(config: Config, message: str) -> bool:
    if not config.discord_webhook_url:
        return False

    payload = {
        "username": "Growatt Guard",
        "content": truncate_discord_message(message),
    }
    headers = {
        "User-Agent": "growatt-spf-battery-guard/1.0",
    }

    try:
        response = requests.post(config.discord_webhook_url, json=payload, headers=headers, timeout=10)
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        body = f": {response.text[:500]}" if response is not None and response.text else ""
        logging.warning("Discord notification failed: %s%s", exc, body)
        return False
    if response.status_code >= 300:
        logging.warning("Discord webhook returned HTTP %s: %s", response.status_code, response.text[:500])
        return False
    return True


GROWATT_CLOUD_FAILURE_PATTERNS = (
    "growatt login failed",
    "login succeeded but no user id",
    "no growatt plants found",
    "no devices found",
    "was not found in plant",
    "could not determine plant id",
    "could not determine device serial",
    "could not find battery soc",
    "soc was not found",
    "spF output source was not found".lower(),
    "could not read current spf output source",
    "connectionerror",
    "connecttimeout",
    "readtimeout",
    "read timed out",
    "max retries exceeded",
    "name or service not known",
    "temporary failure in name resolution",
    "failed to establish a new connection",
)


def is_growatt_cloud_failure(message: str) -> bool:
    lower = message.lower()
    return any(pattern in lower for pattern in GROWATT_CLOUD_FAILURE_PATTERNS)


def read_growatt_cloud_failure_state() -> dict[str, Any] | None:
    if not GROWATT_CLOUD_FAILURE_FILE.exists():
        return None
    try:
        return json.loads(GROWATT_CLOUD_FAILURE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Ignoring invalid Growatt cloud failure state: %s", exc)
        return None


def write_growatt_cloud_failure_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    GROWATT_CLOUD_FAILURE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def clear_growatt_cloud_failure_state() -> None:
    if GROWATT_CLOUD_FAILURE_FILE.exists():
        GROWATT_CLOUD_FAILURE_FILE.unlink()


def record_growatt_cloud_failure(config: Config, command: str, message: str) -> None:
    state = read_growatt_cloud_failure_state() or {}
    count = int(state.get("count", 0)) + 1
    threshold = max(1, config.cloud_failure_alert_threshold)
    alerted = bool(state.get("alerted"))
    state.update(
        {
            "count": count,
            "alerted": alerted,
            "first_failure_at": state.get("first_failure_at") or utc_now().isoformat(),
            "last_failure_at": utc_now().isoformat(),
            "last_command": command,
            "last_message": message,
            "threshold": threshold,
        }
    )

    if count >= threshold and not alerted:
        alert = (
            "Growatt cloud appears flaky.\n"
            f"`{command}` has failed `{count}` consecutive time(s); alert threshold is `{threshold}`.\n"
            f"Latest error: {message}"
        )
        if send_discord_message(config, alert):
            state["alerted"] = True

    write_growatt_cloud_failure_state(state)


def record_growatt_cloud_success(config: Config) -> None:
    state = read_growatt_cloud_failure_state()
    if not state:
        return
    count = int(state.get("count", 0))
    was_alerted = bool(state.get("alerted"))
    clear_growatt_cloud_failure_state()
    if was_alerted and config.discord_notify_failure:
        send_discord_message(
            config,
            f"Growatt cloud recovered after `{count}` consecutive failure(s). Automation reads are working again.",
        )


def notify_failure(config: Config | None, command: str, message: str) -> None:
    if config is None or not config.discord_notify_failure or command == "test-discord":
        return
    if is_growatt_cloud_failure(message):
        record_growatt_cloud_failure(config, command, message)
        return
    send_discord_message(config, f"Growatt automation failed during `{command}`.\n{message}")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_utc_datetime(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def read_pause_state(now: dt.datetime | None = None) -> dict[str, Any] | None:
    if not PAUSE_FILE.exists():
        return None
    now = now or utc_now()
    try:
        state = json.loads(PAUSE_FILE.read_text(encoding="utf-8"))
        until = parse_utc_datetime(str(state["paused_until"]))
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        logging.warning("Ignoring invalid pause state: %s", exc)
        return None
    if until <= now:
        try:
            PAUSE_FILE.unlink()
        except OSError:
            pass
        return None
    state["paused_until_dt"] = until
    return state


def format_local_time(value: dt.datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def pause_message(state: dict[str, Any]) -> str:
    until = state["paused_until_dt"]
    reason = state.get("reason") or "no reason provided"
    return f"automation paused until {format_local_time(until)} ({reason})"


def ensure_not_paused(config: Config, command: str) -> bool:
    state = read_pause_state()
    if not state:
        return False

    message = f"Skipped `{command}` because {pause_message(state)}."
    logging.info(message)
    if config.discord_notify_skip:
        send_discord_message(config, message)
    print(message)
    return True


def write_pause_state(hours: float, reason: str) -> dict[str, Any]:
    if hours <= 0:
        raise GrowattGuardError("--hours must be greater than 0.")
    until = utc_now() + dt.timedelta(hours=hours)
    state = {
        "paused_until": until.isoformat(),
        "reason": reason,
        "created_at": utc_now().isoformat(),
    }
    STATE_DIR.mkdir(exist_ok=True)
    PAUSE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    state["paused_until_dt"] = until
    return state


def read_command_lock_state() -> dict[str, Any] | None:
    if not COMMAND_LOCK_FILE.exists():
        return None
    try:
        return json.loads(COMMAND_LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Ignoring invalid command lock state: %s", exc)
        return None


def command_lock_is_stale() -> bool:
    if not COMMAND_LOCK_FILE.exists():
        return False
    try:
        age_seconds = dt.datetime.now().timestamp() - COMMAND_LOCK_FILE.stat().st_mtime
    except OSError:
        return False
    return age_seconds > COMMAND_LOCK_STALE_SECONDS


def acquire_command_lock(command: str) -> str | None:
    STATE_DIR.mkdir(exist_ok=True)
    token = f"{os.getpid()}-{utc_now().timestamp()}"
    payload = {
        "token": token,
        "pid": os.getpid(),
        "command": command,
        "created_at": utc_now().isoformat(),
    }

    for _ in range(2):
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            fd = os.open(str(COMMAND_LOCK_FILE), flags)
        except FileExistsError:
            if command_lock_is_stale():
                try:
                    COMMAND_LOCK_FILE.unlink()
                except OSError:
                    pass
                continue
            return None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        return token
    return None


def release_command_lock(token: str) -> None:
    state = read_command_lock_state()
    if state and state.get("token") != token:
        return
    try:
        COMMAND_LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def run_with_command_lock(config: Config, command: str, action) -> int:
    token = acquire_command_lock(command)
    if token is None:
        state = read_command_lock_state() or {}
        locked_command = state.get("command", "another command")
        created_at = state.get("created_at", "unknown time")
        message = f"Skipped `{command}` because `{locked_command}` is already running since {created_at}."
        logging.warning(message)
        if config.discord_notify_skip:
            send_discord_message(config, message)
        print(message)
        return 0
    try:
        return action()
    finally:
        release_command_lock(token)


def read_battery_alert_state() -> dict[str, Any] | None:
    if not BATTERY_ALERT_FILE.exists():
        return None
    try:
        return json.loads(BATTERY_ALERT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Ignoring invalid battery alert state: %s", exc)
        return None


def write_battery_alert_state(soc: float) -> None:
    state = {
        "active": True,
        "last_soc": soc,
        "last_alert_at": utc_now().isoformat(),
    }
    STATE_DIR.mkdir(exist_ok=True)
    BATTERY_ALERT_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def clear_battery_alert_state() -> None:
    if BATTERY_ALERT_FILE.exists():
        BATTERY_ALERT_FILE.unlink()


def read_dashboard_stale_alert_state() -> dict[str, Any] | None:
    if not DASHBOARD_STALE_ALERT_FILE.exists():
        return None
    try:
        return json.loads(DASHBOARD_STALE_ALERT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Ignoring invalid dashboard stale alert state: %s", exc)
        return None


def write_dashboard_stale_alert_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    DASHBOARD_STALE_ALERT_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def clear_dashboard_stale_alert_state() -> None:
    if DASHBOARD_STALE_ALERT_FILE.exists():
        DASHBOARD_STALE_ALERT_FILE.unlink()


def parse_forecast_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def fetch_weather_forecast(config: Config) -> dict[str, Any]:
    if config.weather_lat is None or config.weather_lon is None:
        raise GrowattGuardError("WEATHER_LAT and WEATHER_LON must be set when WEATHER_ENABLED=true.")

    response = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": config.weather_lat,
            "longitude": config.weather_lon,
            "hourly": "precipitation,cloud_cover",
            "forecast_days": 2,
            "timezone": config.weather_timezone,
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def analyze_weather_window(config: Config, forecast: dict[str, Any]) -> ThresholdDecision:
    hourly = forecast.get("hourly", {})
    times = hourly.get("time", [])
    cloud_cover = hourly.get("cloud_cover", [])
    precipitation = hourly.get("precipitation", [])
    if not times or not cloud_cover or not precipitation:
        raise GrowattGuardError("Weather response did not include hourly time, cloud_cover and precipitation.")

    now = dt.datetime.now()
    window_end = now + dt.timedelta(hours=config.weather_lookahead_hours)
    clouds: list[float] = []
    rain: list[float] = []

    for index, time_value in enumerate(times):
        forecast_time = parse_forecast_time(str(time_value))
        if now <= forecast_time <= window_end:
            if index < len(cloud_cover) and cloud_cover[index] is not None:
                clouds.append(float(cloud_cover[index]))
            if index < len(precipitation) and precipitation[index] is not None:
                rain.append(float(precipitation[index]))

    if not clouds and not rain:
        # If the forecast starts at the next hour boundary, still use the first few available points.
        lookahead = max(1, config.weather_lookahead_hours)
        clouds = [float(value) for value in cloud_cover[:lookahead] if value is not None]
        rain = [float(value) for value in precipitation[:lookahead] if value is not None]

    max_cloud = max(clouds) if clouds else 0.0
    total_rain = sum(rain)

    if total_rain >= config.weather_rain_threshold_mm or max_cloud >= config.weather_cloudy_threshold:
        return ThresholdDecision(
            threshold=config.low_battery_soc,
            reason=(
                f"rainy/cloudy forecast: max cloud {max_cloud:g}%, "
                f"rain {total_rain:g}mm; using rainy-season threshold {config.low_battery_soc:g}%"
            ),
            weather_category="rainy/cloudy",
            cloud_cover=max_cloud,
            precipitation_mm=total_rain,
        )

    if total_rain == 0 and max_cloud <= config.weather_sunny_threshold:
        return ThresholdDecision(
            threshold=config.low_battery_soc_sunny,
            reason=(
                f"sunny forecast: max cloud {max_cloud:g}%, "
                f"rain {total_rain:g}mm; using sunny threshold {config.low_battery_soc_sunny:g}%"
            ),
            weather_category="sunny",
            cloud_cover=max_cloud,
            precipitation_mm=total_rain,
        )

    return ThresholdDecision(
        threshold=config.low_battery_soc_normal,
        reason=(
            f"normal forecast: max cloud {max_cloud:g}%, "
            f"rain {total_rain:g}mm; using normal threshold {config.low_battery_soc_normal:g}%"
        ),
        weather_category="normal",
        cloud_cover=max_cloud,
        precipitation_mm=total_rain,
    )


def choose_preserve_threshold(config: Config) -> ThresholdDecision:
    if not config.weather_enabled:
        return ThresholdDecision(
            threshold=config.low_battery_soc,
            reason=f"weather disabled; using fixed threshold {config.low_battery_soc:g}%",
        )

    try:
        return analyze_weather_window(config, fetch_weather_forecast(config))
    except Exception as exc:  # noqa: BLE001 - preserve automation if weather is unavailable
        logging.warning("Weather threshold unavailable, using fixed threshold: %s", exc)
        return ThresholdDecision(
            threshold=config.low_battery_soc,
            reason=f"weather unavailable; using fixed threshold {config.low_battery_soc:g}%",
            weather_category="unavailable",
        )


def require_dependencies() -> None:
    missing = []
    if load_dotenv is None:
        missing.append("python-dotenv")
    if growattServer is None:
        missing.append("growattServer")
    if missing:
        raise GrowattGuardError(
            "Missing dependencies: "
            + ", ".join(missing)
            + ". Install them with: python -m pip install -r requirements.txt"
        )


def connect(config: Config):
    require_dependencies()
    api = growattServer.GrowattApi(add_random_user_id=True, agent_identifier=config.username)
    api.server_url = config.server_url

    logging.info("Logging into Growatt server %s", config.server_url)
    login_response = api.login(config.username, config.password)
    if not isinstance(login_response, dict) or not login_response.get("success"):
        raise GrowattGuardError(f"Growatt login failed: {login_response}")
    return api, login_response


def normalize_list_response(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("data", "back", "deviceList", "devices", "PlantList"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
            if isinstance(nested, dict):
                return normalize_list_response(nested)
    return []


def get_key(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def choose_plant(api, login_response: dict[str, Any], config: Config) -> str:
    if config.plant_id:
        return config.plant_id

    user = login_response.get("user", {})
    user_id = login_response.get("userId") or user.get("id")
    if not user_id:
        raise GrowattGuardError("Login succeeded but no user id was returned by Growatt.")

    plants = normalize_list_response(api.plant_list(user_id))
    if not plants:
        plants = normalize_list_response(login_response)
    if not plants:
        raise GrowattGuardError("No Growatt plants found for this account.")

    plant = plants[0]
    plant_id = get_key(plant, "plantId", "id")
    if not plant_id:
        raise GrowattGuardError(f"Could not determine plant id from: {plant}")
    logging.info("Using plant %s (%s)", plant_id, get_key(plant, "plantName", "name") or "unnamed")
    return str(plant_id)


def normalize_device(device: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(device)
    normalized["deviceSn"] = str(get_key(device, "deviceSn", "device_sn", "sn", "serialNum") or "")
    normalized["deviceType"] = str(get_key(device, "deviceType", "type", "device_type") or "").lower()
    return normalized


def choose_device(api, plant_id: str, config: Config) -> DeviceRef:
    devices = [normalize_device(device) for device in normalize_list_response(api.device_list(plant_id))]
    if not devices:
        raise GrowattGuardError(f"No devices found for plant {plant_id}.")

    if config.device_sn:
        for device in devices:
            if device["deviceSn"] == config.device_sn:
                return DeviceRef(plant_id, device["deviceSn"], device["deviceType"], device)
        raise GrowattGuardError(f"Device {config.device_sn} was not found in plant {plant_id}.")

    for wanted_type in DEVICE_TYPE_PRIORITY:
        for device in devices:
            if device["deviceType"] == wanted_type and device["deviceSn"]:
                logging.info("Using %s device %s", device["deviceType"], device["deviceSn"])
                return DeviceRef(plant_id, device["deviceSn"], device["deviceType"], device)

    first = devices[0]
    if not first["deviceSn"]:
        raise GrowattGuardError(f"Could not determine device serial from: {first}")
    logging.info("Using first device %s (%s)", first["deviceSn"], first["deviceType"] or "unknown type")
    return DeviceRef(plant_id, first["deviceSn"], first["deviceType"], first)


def deep_values(data: Any, path: str = "") -> list[tuple[str, Any]]:
    values: list[tuple[str, Any]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            next_path = f"{path}.{key}" if path else str(key)
            values.extend(deep_values(value, next_path))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            values.extend(deep_values(value, f"{path}[{index}]"))
    else:
        values.append((path, data))
    return values


def parse_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        if match:
            return float(match.group(0))
    return None


def extract_soc(data: dict[str, Any]) -> tuple[float, str] | None:
    flat = deep_values(data)
    for wanted_key in SOC_KEYS:
        for path, value in flat:
            if path.split(".")[-1] == wanted_key:
                parsed = parse_number(value)
                if parsed is not None and 0 <= parsed <= 100:
                    return parsed, path
    for path, value in flat:
        if "soc" in path.lower() or "capacity" in path.lower():
            parsed = parse_number(value)
            if parsed is not None and 0 <= parsed <= 100:
                return parsed, path
    return None


def extract_spf_output_source(data: dict[str, Any]) -> tuple[str, str, str] | None:
    for path, value in deep_values(data):
        if path.split(".")[-1] == "outputConfig":
            raw = str(value)
            return raw, SPF_OUTPUT_SOURCE.get(raw, f"Unknown ({raw})"), path
    return None


def output_source_label(raw: str) -> str:
    return SPF_OUTPUT_SOURCE.get(raw, f"Unknown ({raw})")


def extract_first_metric(data: dict[str, Any], keys: tuple[str, ...]) -> tuple[Any, str] | None:
    for wanted_key in keys:
        for path, value in deep_values(data):
            if path.split(".")[-1] == wanted_key and value not in (None, ""):
                return value, path
    return None


def format_metric(data: dict[str, Any], label: str, keys: tuple[str, ...], unit: str = "") -> str | None:
    result = extract_first_metric(data, keys)
    if not result:
        return None
    value, _ = result
    if isinstance(value, str) and re.search(r"[a-zA-Z%]", value):
        return f"{label}: {value}"
    return f"{label}: {value}{unit}"


def summarize_today_log_counts() -> dict[str, int]:
    today = dt.datetime.now().strftime("%Y-%m-%d")
    counts = {
        "success": 0,
        "failure": 0,
        "watchdog_repairs": 0,
        "preserve_actions": 0,
        "return_sbu_actions": 0,
    }
    if not LOG_FILE.exists():
        return counts

    for line in LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(today):
            continue
        lower = line.lower()
        if "inv_set_success" in lower or "mode response" in lower:
            counts["success"] += 1
        if " error " in lower or " failed" in lower or "unhandled error" in lower:
            counts["failure"] += 1
        if "watchdog detected" in lower or "watchdog repaired" in lower:
            counts["watchdog_repairs"] += 1
        if "switching to utility" in lower:
            counts["preserve_actions"] += 1
        if "sbu mode response" in lower:
            counts["return_sbu_actions"] += 1
    return counts


def read_device_status(api, device: DeviceRef) -> dict[str, Any]:
    status: dict[str, Any] = {
        "plant_id": device.plant_id,
        "device_sn": device.device_sn,
        "device_type": device.device_type,
        "device": device.raw,
    }

    attempts: list[tuple[str, Any]] = []
    if device.device_type == "storage":
        attempts.extend(
            [
                ("storage_params", lambda: api.storage_params(device.device_sn)),
                ("storage_detail", lambda: api.storage_detail(device.device_sn)),
                (
                    "storage_energy_overview",
                    lambda: api.storage_energy_overview(device.plant_id, device.device_sn),
                ),
            ]
        )
    elif device.device_type == "mix":
        attempts.extend(
            [
                ("mix_info", lambda: api.mix_info(device.device_sn, device.plant_id)),
                ("mix_system_status", lambda: api.mix_system_status(device.device_sn, device.plant_id)),
                ("mix_detail", lambda: api.mix_detail(device.device_sn, device.plant_id)),
            ]
        )
    elif device.device_type == "tlx":
        attempts.extend(
            [
                ("tlx_detail", lambda: api.tlx_detail(device.device_sn)),
                ("tlx_params", lambda: api.tlx_params(device.device_sn)),
            ]
        )
    elif device.device_type == "inverter":
        attempts.append(("inverter_detail", lambda: api.inverter_detail(device.device_sn)))

    attempts.extend(
        [
            ("storage_params_fallback", lambda: api.storage_params(device.device_sn)),
            ("storage_detail_fallback", lambda: api.storage_detail(device.device_sn)),
            ("inverter_detail_fallback", lambda: api.inverter_detail(device.device_sn)),
        ]
    )

    errors: dict[str, str] = {}
    for name, func in attempts:
        if name in status:
            continue
        try:
            value = func()
        except Exception as exc:  # noqa: BLE001 - probing heterogeneous Growatt endpoints
            errors[name] = str(exc)
        else:
            if value:
                status[name] = value

    if errors:
        status["_probe_errors"] = errors
    return status


def summarize_status(status: dict[str, Any]) -> str:
    soc_result = extract_soc(status)
    parts = [
        f"plant={status.get('plant_id')}",
        f"device={status.get('device_sn')}",
        f"type={status.get('device_type') or 'unknown'}",
    ]
    if soc_result:
        soc, path = soc_result
        parts.append(f"soc={soc:g}% ({path})")
    else:
        parts.append("soc=not found")
    output_source = extract_spf_output_source(status)
    if output_source:
        raw, label, path = output_source
        parts.append(f"output={label} [{raw}] ({path})")
    return ", ".join(parts)


def extract_status_soc(status: dict[str, Any]) -> float | None:
    soc_result = extract_soc(status)
    if not soc_result:
        return None
    soc, _ = soc_result
    return soc


def describe_status_output_source(status: dict[str, Any]) -> str:
    output_source = extract_spf_output_source(status)
    if not output_source:
        return ""
    raw, label, _ = output_source
    return f"{label} [{raw}]"


MODE_AUDIT_FIELDS = (
    "timestamp",
    "command",
    "soc",
    "threshold",
    "weather_category",
    "previous_mode",
    "action",
    "dry_run",
    "result",
    "note",
)


def format_audit_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True)
    else:
        text = str(value)
    if len(text) > 500:
        return text[:497] + "..."
    return text


def append_mode_audit(
    config: Config,
    command: str,
    *,
    soc: float | None = None,
    threshold: float | None = None,
    weather_category: str = "",
    previous_mode: str = "",
    action: str = "",
    result: Any = None,
    note: str = "",
) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    write_header = not MODE_AUDIT_FILE.exists()
    row = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "command": command,
        "soc": format_audit_value(soc),
        "threshold": format_audit_value(threshold),
        "weather_category": weather_category,
        "previous_mode": previous_mode,
        "action": action,
        "dry_run": str(config.dry_run).lower(),
        "result": format_audit_value(result),
        "note": note,
    }
    with MODE_AUDIT_FILE.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MODE_AUDIT_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def parse_audit_timestamp(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def read_mode_audit_rows(
    *,
    since: dt.datetime | None = None,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[dict[str, str]]:
    if not MODE_AUDIT_FILE.exists():
        return []
    with MODE_AUDIT_FILE.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if since is not None:
        rows = [
            row
            for row in rows
            if (timestamp := parse_audit_timestamp(row.get("timestamp", ""))) is not None and timestamp >= since
        ]
    if newest_first:
        rows = list(reversed(rows))
    if limit is not None:
        rows = rows[:limit]
    return rows


def parse_audit_float(row: dict[str, str], key: str) -> float | None:
    value = row.get(key, "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_weekly_summary(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    since = now - dt.timedelta(days=7)
    rows = read_mode_audit_rows(since=since)
    preserve_rows = [row for row in rows if row.get("command") == "preserve-battery"]
    preserve_socs = [soc for row in preserve_rows if (soc := parse_audit_float(row, "soc")) is not None]
    utility_switches = [row for row in rows if row.get("action") == "switch-to-utility"]
    preserve_no_changes = [row for row in rows if row.get("command") == "preserve-battery" and row.get("action") == "no-change"]
    return_sbu = [row for row in rows if row.get("action") == "switch-to-sbu"]
    watchdog_repairs = [row for row in rows if row.get("action") == "repair-sbu"]
    failures = [row for row in rows if row.get("action", "").endswith("-failed") or row.get("result") == "error"]
    last_row = rows[-1] if rows else None

    avg_soc = average(preserve_socs)
    lines = [
        f"Growatt weekly performance - {since.strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')}",
        f"Audit rows: {len(rows)}",
        f"Preserve-battery checks: {len(preserve_rows)}",
        f"Utility switches: {len(utility_switches)}",
        f"No-change preserve checks: {len(preserve_no_changes)}",
        f"Return-SBU switches: {len(return_sbu)}",
        f"Watchdog repairs: {len(watchdog_repairs)}",
        f"Failures: {len(failures)}",
    ]
    if avg_soc is not None:
        lines.append(f"Average preserve-check SOC: {avg_soc:g}%")
        lines.append(f"Lowest preserve-check SOC: {min(preserve_socs):g}%")
    else:
        lines.append("Average preserve-check SOC: not enough data")
    if last_row:
        lines.append(
            "Last action: "
            f"{last_row.get('timestamp', '')} {last_row.get('command', '')} "
            f"{last_row.get('action', '')} SOC={last_row.get('soc', '')}%"
        )
    return "\n".join(lines)


def redact(data: Any) -> Any:
    secret_words = ("password", "token", "secret", "auth", "session", "cookie")
    if isinstance(data, dict):
        redacted = {}
        for key, value in data.items():
            if any(word in str(key).lower() for word in secret_words):
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = redact(value)
        return redacted
    if isinstance(data, list):
        return [redact(item) for item in data]
    return data


def write_probe(status: dict[str, Any]) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = LOG_DIR / f"growatt-probe-{timestamp}.json"
    path.write_text(json.dumps(redact(status), indent=2, sort_keys=True), encoding="utf-8")
    return path


def render_params(template: str, device: DeviceRef, mode: str) -> dict[str, Any]:
    if not template:
        raise GrowattGuardError(
            f"No custom params configured for {mode}. Set GROWATT_{mode.upper()}_MODE_PARAMS in .env."
        )
    rendered = (
        template.replace("{plant_id}", device.plant_id)
        .replace("{device_sn}", device.device_sn)
        .replace("{serial}", device.device_sn)
        .replace("{mode}", mode)
    )
    try:
        params = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise GrowattGuardError(f"Invalid JSON for {mode} params: {exc}") from exc
    if not isinstance(params, dict):
        raise GrowattGuardError(f"{mode} params must be a JSON object.")
    return params


def response_error_text(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    text = getattr(response, "text", "")
    if len(text) > 1000:
        text = text[:1000] + "...[truncated]"
    return text


def request_json_with_error_detail(api, method: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    url = api.get_url(path)
    try:
        if method == "post_params":
            response = api.session.post(url, params=params, timeout=35)
        elif method == "post_data":
            response = api.session.post(url, data=params, timeout=35)
        elif method == "get":
            response = api.session.get(url, params=params, timeout=35)
        else:
            raise GrowattGuardError(f"Unsupported request method: {method}")
    except Exception as exc:  # noqa: BLE001 - preserve Growatt response text from request hooks
        body = response_error_text(exc)
        if body:
            raise GrowattGuardError(f"Growatt request failed via {method}: {exc}; body={body}") from exc
        raise GrowattGuardError(f"Growatt request failed via {method}: {exc}") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise GrowattGuardError(f"Growatt returned non-JSON response via {method}: {response.text}") from exc


def send_spf5000_output_source(api, path: str, params: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    for method in ("post_params", "post_data"):
        try:
            return request_json_with_error_detail(api, method, path, params)
        except GrowattGuardError as exc:
            failures.append(str(exc))
            logging.warning("%s", exc)

    raise GrowattGuardError("Growatt SPF output-source command failed. " + " | ".join(failures))


def ensure_growatt_success(result: dict[str, Any], action: str) -> None:
    if result.get("success") is False:
        raise GrowattGuardError(f"Growatt {action} failed: {result}")


def set_mode(api, config: Config, device: DeviceRef, mode: str) -> dict[str, Any]:
    if mode not in {"utility", "sbu"}:
        raise GrowattGuardError(f"Unsupported mode: {mode}")

    if config.mode_driver in {"spf5000", "spf"}:
        value = "2" if mode == "utility" else "0"
        params = {
            "action": "storageSPF5000Set",
            "serialNum": device.device_sn,
            "type": "storage_spf5000_ac_output_source",
            "param1": value,
            "param2": "",
            "param3": "",
            "param4": "",
        }
        path = "tcpSet.do"
        method = "post_params"
        logging.info("Prepared SPF output-source command for %s: %s", mode, params)
        if config.dry_run:
            logging.info("DRY_RUN=true, not sending SPF output-source command.")
            return {"dry_run": True, "mode": mode, "path": path, "method": method, "params": params}
        result = send_spf5000_output_source(api, path, params)
        ensure_growatt_success(result, f"{mode} mode command")
        logging.info("Growatt SPF %s mode response: %s", mode, result)
        return result

    if config.mode_driver != "custom":
        raise GrowattGuardError(
            "Unsupported GROWATT_MODE_DRIVER="
            f"{config.mode_driver!r}. Supported values: 'spf5000' and 'custom'."
        )

    template = config.utility_mode_params if mode == "utility" else config.sbu_mode_params
    params = render_params(template, device, mode)

    logging.info("Prepared %s mode command: path=%s params=%s", mode, config.set_mode_path, params)
    if config.dry_run:
        logging.info("DRY_RUN=true, not sending mode command.")
        return {"dry_run": True, "mode": mode, "path": config.set_mode_path, "params": params}

    url = api.get_url(config.set_mode_path)
    method = config.set_mode_method
    if method == "post":
        response = api.session.post(url, params=params)
    elif method == "get":
        response = api.session.get(url, params=params)
    else:
        raise GrowattGuardError("GROWATT_SET_MODE_METHOD must be 'post' or 'get'.")
    result = response.json()
    ensure_growatt_success(result, f"{mode} mode command")
    logging.info("Growatt %s mode response: %s", mode, result)
    return result


def load_context(config: Config):
    api, login_response = connect(config)
    plant_id = choose_plant(api, login_response, config)
    device = choose_device(api, plant_id, config)
    status = read_device_status(api, device)
    logging.info("Current status: %s", summarize_status(status))
    record_growatt_cloud_success(config)
    return api, device, status


def command_status(config: Config) -> int:
    _, _, status = load_context(config)
    print(summarize_status(status))
    return 0


def command_probe(config: Config) -> int:
    _, _, status = load_context(config)
    path = write_probe(status)
    print(summarize_status(status))
    print(f"Wrote redacted probe data to {path}")
    return 0


def command_preserve_battery(config: Config) -> int:
    if ensure_not_paused(config, "preserve-battery"):
        return 0

    api, device, status = load_context(config)
    soc_result = extract_soc(status)
    if not soc_result:
        raise GrowattGuardError("Could not find battery SOC in Growatt response. Run the probe command.")

    soc, path = soc_result
    previous_mode = describe_status_output_source(status)
    threshold_decision = choose_preserve_threshold(config)
    threshold = threshold_decision.threshold
    logging.info("Preserve-battery threshold: %.1f%% (%s)", threshold, threshold_decision.reason)

    if soc < threshold:
        logging.info("Battery SOC %.1f%% from %s is below %.1f%%; switching to Utility.", soc, path, threshold)
        try:
            result = set_mode(api, config, device, "utility")
        except Exception as exc:  # noqa: BLE001 - audit failed mode decisions before re-raising
            append_mode_audit(
                config,
                "preserve-battery",
                soc=soc,
                threshold=threshold,
                weather_category=threshold_decision.weather_category,
                previous_mode=previous_mode,
                action="switch-to-utility-failed",
                result="error",
                note=str(exc),
            )
            raise
        append_mode_audit(
            config,
            "preserve-battery",
            soc=soc,
            threshold=threshold,
            weather_category=threshold_decision.weather_category,
            previous_mode=previous_mode,
            action="switch-to-utility",
            result=result,
            note=f"SOC from {path}",
        )
        if config.discord_notify_success and not config.dry_run:
            send_discord_message(
                config,
                (
                    "Growatt preserve-battery action completed.\n"
                    f"SOC `{soc:g}%` is below threshold `{threshold:g}%`; "
                    "switched to `Utility first`.\n"
                    f"Reason: {threshold_decision.reason}."
                ),
            )
        print(f"SOC {soc:g}% < {threshold:g}%; Utility command result: {result}")
        print(f"Threshold reason: {threshold_decision.reason}")
    else:
        logging.info("Battery SOC %.1f%% is not below %.1f%%; leaving SBU as-is.", soc, threshold)
        append_mode_audit(
            config,
            "preserve-battery",
            soc=soc,
            threshold=threshold,
            weather_category=threshold_decision.weather_category,
            previous_mode=previous_mode,
            action="no-change",
            result="skipped",
            note=f"SOC from {path}",
        )
        if config.discord_notify_skip:
            send_discord_message(
                config,
                (
                    "Growatt preserve-battery check skipped.\n"
                    f"SOC `{soc:g}%` is at or above threshold `{threshold:g}%`; no switch needed.\n"
                    f"Reason: {threshold_decision.reason}."
                ),
            )
        print(f"SOC {soc:g}% >= {threshold:g}%; no switch needed.")
        print(f"Threshold reason: {threshold_decision.reason}")
    return 0


def command_utility_check(config: Config) -> int:
    return command_preserve_battery(config)


def command_morning_check(config: Config) -> int:
    return command_preserve_battery(config)


def command_return_sbu(config: Config) -> int:
    if ensure_not_paused(config, "return-sbu"):
        return 0

    api, device, status = load_context(config)
    soc = extract_status_soc(status)
    previous_mode = describe_status_output_source(status)
    try:
        result = set_mode(api, config, device, "sbu")
    except Exception as exc:  # noqa: BLE001 - audit failed mode decisions before re-raising
        append_mode_audit(
            config,
            "return-sbu",
            soc=soc,
            previous_mode=previous_mode,
            action="switch-to-sbu-failed",
            result="error",
            note=str(exc),
        )
        raise
    append_mode_audit(
        config,
        "return-sbu",
        soc=soc,
        previous_mode=previous_mode,
        action="switch-to-sbu",
        result=result,
    )
    if config.discord_notify_success and not config.dry_run:
        send_discord_message(config, "Growatt return-sbu action completed.\nSwitched to `SBU priority`.")
    print(f"SBU command result: {result}")
    return 0


def command_watchdog_sbu(config: Config) -> int:
    if ensure_not_paused(config, "watchdog-sbu"):
        return 0

    api, device, status = load_context(config)
    output_source = extract_spf_output_source(status)
    soc = extract_status_soc(status)
    previous_mode = describe_status_output_source(status)
    if not output_source:
        message = "Could not read current SPF output source; cannot verify SBU mode."
        append_mode_audit(
            config,
            "watchdog-sbu",
            soc=soc,
            previous_mode=previous_mode,
            action="verify-sbu-failed",
            result="error",
            note=message,
        )
        if config.discord_notify_failure:
            send_discord_message(config, f"Growatt SBU watchdog could not verify mode.\n{message}")
        raise GrowattGuardError(message)

    raw, label, path = output_source
    if raw == "0":
        logging.info("SBU watchdog OK: output=%s [%s] from %s", label, raw, path)
        append_mode_audit(
            config,
            "watchdog-sbu",
            soc=soc,
            previous_mode=previous_mode,
            action="verified-sbu",
            result="ok",
            note=f"output from {path}",
        )
        print(f"SBU watchdog OK: output={label} [{raw}]")
        return 0

    logging.warning("SBU watchdog detected output=%s [%s] from %s; retrying SBU.", label, raw, path)
    try:
        result = set_mode(api, config, device, "sbu")
    except Exception as exc:  # noqa: BLE001 - audit failed mode decisions before re-raising
        append_mode_audit(
            config,
            "watchdog-sbu",
            soc=soc,
            previous_mode=previous_mode,
            action="repair-sbu-failed",
            result="error",
            note=str(exc),
        )
        raise
    append_mode_audit(
        config,
        "watchdog-sbu",
        soc=soc,
        previous_mode=previous_mode,
        action="repair-sbu",
        result=result,
        note=f"output from {path}",
    )
    message = (
        "Growatt SBU watchdog repaired output source.\n"
        f"Detected `{label}` [{raw}] from `{path}`; retried `SBU priority`.\n"
        f"Growatt response: `{result}`"
    )
    if config.discord_notify_failure and not config.dry_run:
        send_discord_message(config, message)
    print(message)
    return 0


def build_daily_summary(status: dict[str, Any]) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"Growatt daily summary - {now}"]

    state = read_pause_state()
    if state:
        lines.append(f"Automation pause: {pause_message(state)}")

    soc_result = extract_soc(status)
    if soc_result:
        soc, _ = soc_result
        lines.append(f"Battery SOC: {soc:g}%")

    output_source = extract_spf_output_source(status)
    if output_source:
        raw, label, _ = output_source
        lines.append(f"Output source: {label} [{raw}]")

    metric_specs = [
        ("PV power", ("ppvText", "ppv"), " W"),
        ("Grid voltage", ("vGridText", "vGrid"), " V"),
        ("Output power", ("outPutPowerText", "outPutPower", "activePower"), " W"),
        ("Battery charge power", ("pChargeText", "pCharge"), " W"),
        ("Battery discharge power", ("pDischargeText", "pDischarge"), " W"),
        ("Energy charged today", ("eChargeTodayText", "eChargeToday"), " kWh"),
        ("AC charge today", ("eacChargeToday", "eacChargeTodayText"), " kWh"),
        ("Energy discharged today", ("eDischargeTodayText", "eDischargeToday"), " kWh"),
    ]
    for label, keys, unit in metric_specs:
        formatted = format_metric(status, label, keys, unit)
        if formatted:
            lines.append(formatted)

    counts = summarize_today_log_counts()
    lines.extend(
        [
            "",
            "Automation today:",
            f"Successful mode responses: {counts['success']}",
            f"Failures/errors: {counts['failure']}",
            f"Preserve-battery actions: {counts['preserve_actions']}",
            f"Return-SBU actions: {counts['return_sbu_actions']}",
            f"Watchdog repairs: {counts['watchdog_repairs']}",
        ]
    )
    return "\n".join(lines)


def command_daily_summary(config: Config) -> int:
    _, _, status = load_context(config)
    summary = build_daily_summary(status)
    if config.discord_webhook_url:
        send_discord_message(config, summary)
    print(summary)
    return 0


def command_weekly_summary(config: Config) -> int:
    summary = build_weekly_summary()
    if config.discord_webhook_url:
        send_discord_message(config, summary)
    print(summary)
    return 0


def cron_part_matches(value: int, field: str, minimum: int, maximum: int) -> bool:
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            return True
        if part.startswith("*/"):
            step = int(part[2:])
            return step > 0 and value % step == 0
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start <= value <= end:
                return True
            continue
        try:
            wanted = int(part)
        except ValueError:
            continue
        if minimum <= wanted <= maximum and value == wanted:
            return True
    return False


def cron_matches(cron: str, when: dt.datetime) -> bool:
    minute, hour, day, month, day_of_week = cron.split()
    cron_dow = (when.weekday() + 1) % 7
    return (
        cron_part_matches(when.minute, minute, 0, 59)
        and cron_part_matches(when.hour, hour, 0, 23)
        and cron_part_matches(when.day, day, 1, 31)
        and cron_part_matches(when.month, month, 1, 12)
        and (cron_part_matches(cron_dow, day_of_week, 0, 7) or (cron_dow == 0 and cron_part_matches(7, day_of_week, 0, 7)))
    )


def next_scheduled_runs(
    schedule: dict[str, Any],
    *,
    now: dt.datetime | None = None,
    limit: int = 8,
) -> list[tuple[dt.datetime, dict[str, Any]]]:
    now = now or dt.datetime.now()
    cursor = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    end = cursor + dt.timedelta(days=14)
    matches: list[tuple[dt.datetime, dict[str, Any]]] = []
    while cursor <= end and len(matches) < limit:
        for job in schedule["jobs"]:
            if cron_matches(str(job["cron"]), cursor):
                matches.append((cursor, job))
                if len(matches) >= limit:
                    break
        cursor += dt.timedelta(minutes=1)
    return matches


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 60:
        unit = "second" if seconds == 1 else "seconds"
        return f"{seconds} {unit}"
    minutes = seconds // 60
    if minutes < 60:
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    unit = "hour" if hours == 1 else "hours"
    if remaining_minutes == 0:
        return f"{hours} {unit}"
    return f"{hours} {unit} {remaining_minutes} minutes"


def dashboard_freshness(
    output_path: Path,
    stale_minutes: float,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    if stale_minutes <= 0:
        raise GrowattGuardError("Dashboard stale threshold must be greater than 0 minutes.")

    now = now or utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    else:
        now = now.astimezone(dt.timezone.utc)

    if not output_path.exists():
        return {
            "path": str(output_path),
            "exists": False,
            "stale": True,
            "age_seconds": None,
            "modified_at": None,
            "stale_minutes": stale_minutes,
            "reason": "dashboard file does not exist",
        }

    modified_at = dt.datetime.fromtimestamp(output_path.stat().st_mtime, tz=dt.timezone.utc)
    age_seconds = max(0.0, (now - modified_at).total_seconds())
    stale = age_seconds > stale_minutes * 60
    age_text = format_duration(age_seconds)
    return {
        "path": str(output_path),
        "exists": True,
        "stale": stale,
        "age_seconds": age_seconds,
        "modified_at": modified_at.isoformat(),
        "stale_minutes": stale_minutes,
        "reason": (
            f"dashboard file is {age_text} old"
            if stale
            else f"dashboard file is fresh at {age_text} old"
        ),
    }


def build_dashboard_html(
    status: dict[str, Any],
    schedule: dict[str, Any],
    overrides: dict[str, Any],
    threshold_decision: ThresholdDecision,
    stale_after_minutes: float = 30,
) -> str:
    now = dt.datetime.now()
    generated_at = now.astimezone()
    generated_at_iso = generated_at.isoformat(timespec="seconds")
    soc_result = extract_soc(status)
    soc = f"{soc_result[0]:g}%" if soc_result else "Not found"
    output_source = extract_spf_output_source(status)
    mode = f"{output_source[1]} [{output_source[0]}]" if output_source else "Not found"
    pause_state = read_pause_state()
    pause = pause_message(pause_state) if pause_state else "active"
    alert_state = read_battery_alert_state()
    alert = "active" if alert_state and alert_state.get("active") else "clear"
    cloud_state = read_growatt_cloud_failure_state()
    cloud_streak = int(cloud_state.get("count", 0)) if cloud_state else 0
    today_override = today_schedule_override(overrides, now.date())
    override_note = str(today_override.get("note", "")).strip() or "none"
    skipped = ", ".join(today_override.get("skip", [])) if isinstance(today_override.get("skip", []), list) else ""
    last_actions = read_mode_audit_rows(limit=8, newest_first=True)
    next_runs = next_scheduled_runs(schedule, now=now, limit=8)
    stale_minutes_text = f"{stale_after_minutes:g}"

    next_rows = "\n".join(
        "<tr>"
        f"<td>{esc(run_at.strftime('%Y-%m-%d %H:%M'))}</td>"
        f"<td>{esc(job.get('id', ''))}</td>"
        f"<td>{esc(job.get('name', ''))}</td>"
        f"<td>{esc(' '.join(schedule_job_tokens(job)))}</td>"
        "</tr>"
        for run_at, job in next_runs
    )
    action_rows = "\n".join(
        "<tr>"
        f"<td>{esc(row.get('timestamp', ''))}</td>"
        f"<td>{esc(row.get('command', ''))}</td>"
        f"<td>{esc(row.get('action', ''))}</td>"
        f"<td>{esc(row.get('soc', ''))}</td>"
        f"<td>{esc(row.get('previous_mode', ''))}</td>"
        "</tr>"
        for row in last_actions
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>Growatt Dashboard</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; }}
    body {{ margin: 0; background: #f5f7f8; color: #172026; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 24px; }}
    h1 {{ font-size: 28px; margin: 0 0 4px; }}
    h2 {{ font-size: 18px; margin: 28px 0 12px; }}
    .muted {{ color: #64727d; font-size: 14px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-top: 20px; }}
    .card {{ background: #fff; border: 1px solid #dce3e8; border-radius: 8px; padding: 14px; }}
    .label {{ color: #64727d; font-size: 12px; text-transform: uppercase; letter-spacing: 0; }}
    .value {{ font-size: 22px; font-weight: 700; margin-top: 8px; }}
    .small {{ font-size: 13px; margin-top: 8px; }}
    .badge {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 9px; font-size: 14px; font-weight: 800; }}
    .badge-ok {{ background: #dff6e8; color: #155f34; }}
    .badge-warn {{ background: #fff2cc; color: #775800; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #dce3e8; }}
    th, td {{ padding: 10px; border-bottom: 1px solid #e8eef2; text-align: left; font-size: 14px; }}
    th {{ background: #eef3f5; color: #34444f; }}
    tr:last-child td {{ border-bottom: 0; }}
  </style>
</head>
<body>
  <main>
    <h1>Growatt Dashboard</h1>
    <div class="muted">Generated {esc(generated_at.strftime('%Y-%m-%d %H:%M:%S %Z'))}</div>
    <section class="grid">
      <div class="card">
        <div class="label">Dashboard Health</div>
        <div class="value">
          <span class="badge badge-ok" data-refresh-badge data-generated-at="{esc(generated_at_iso)}" data-stale-minutes="{esc(stale_minutes_text)}">OK</span>
        </div>
        <div class="muted small" data-refresh-age>Generated just now; stale after {esc(stale_minutes_text)} minutes.</div>
      </div>
      <div class="card"><div class="label">Battery SOC</div><div class="value">{esc(soc)}</div></div>
      <div class="card"><div class="label">Output Source</div><div class="value">{esc(mode)}</div></div>
      <div class="card"><div class="label">Preserve Threshold</div><div class="value">{esc(f'{threshold_decision.threshold:g}%')}</div></div>
      <div class="card"><div class="label">Pause State</div><div class="value">{esc(pause)}</div></div>
      <div class="card"><div class="label">Emergency Alert</div><div class="value">{esc(alert)}</div></div>
      <div class="card"><div class="label">Cloud Streak</div><div class="value">{esc(cloud_streak)}</div></div>
      <div class="card"><div class="label">Today Override</div><div class="value">{esc(override_note)}</div></div>
    </section>
    <h2>Next Scheduled Jobs</h2>
    <table><thead><tr><th>Time</th><th>ID</th><th>Name</th><th>Command</th></tr></thead><tbody>{next_rows}</tbody></table>
    <h2>Recent Mode Decisions</h2>
    <table><thead><tr><th>Time</th><th>Command</th><th>Action</th><th>SOC</th><th>Previous Mode</th></tr></thead><tbody>{action_rows}</tbody></table>
    <h2>Automation Notes</h2>
    <div class="card">
      <div>Threshold: {esc(threshold_decision.reason)}</div>
      <div>Skipped today: {esc(skipped or 'none')}</div>
    </div>
  </main>
  <script>
    (function () {{
      const badge = document.querySelector("[data-refresh-badge]");
      const ageNode = document.querySelector("[data-refresh-age]");
      if (!badge || !ageNode) return;

      const generatedAt = new Date(badge.dataset.generatedAt);
      const staleMinutes = Number(badge.dataset.staleMinutes || "30");

      function plural(value, unit) {{
        return value + " " + unit + (value === 1 ? "" : "s");
      }}

      function formatAge(milliseconds) {{
        const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
        if (totalSeconds < 60) return plural(totalSeconds, "second");
        const totalMinutes = Math.floor(totalSeconds / 60);
        if (totalMinutes < 60) return plural(totalMinutes, "minute");
        const hours = Math.floor(totalMinutes / 60);
        const minutes = totalMinutes % 60;
        return minutes ? plural(hours, "hour") + " " + plural(minutes, "minute") : plural(hours, "hour");
      }}

      function updateRefreshHealth() {{
        if (Number.isNaN(generatedAt.getTime())) {{
          badge.textContent = "UNKNOWN";
          badge.className = "badge badge-warn";
          ageNode.textContent = "Generated time could not be read.";
          return;
        }}
        const ageMs = Date.now() - generatedAt.getTime();
        const stale = ageMs > staleMinutes * 60 * 1000;
        badge.textContent = stale ? "STALE" : "OK";
        badge.className = "badge " + (stale ? "badge-warn" : "badge-ok");
        ageNode.textContent = "Generated " + formatAge(ageMs) + " ago; stale after " + staleMinutes + " minutes.";
      }}

      updateRefreshHealth();
      window.setInterval(updateRefreshHealth, 30000);
    }})();
  </script>
</body>
</html>
"""


def resolve_dashboard_output(output: str) -> Path:
    output_path = Path(output)
    if not output_path.is_absolute():
        output_path = BASE_DIR / output_path
    return output_path


def write_dashboard(config: Config, output: str) -> Path:
    _, _, status = load_context(config)
    schedule = validate_schedule()
    overrides = validate_schedule_overrides(schedule)
    threshold_decision = choose_preserve_threshold(config)
    output_path = resolve_dashboard_output(output)
    output_path.write_text(
        build_dashboard_html(status, schedule, overrides, threshold_decision, config.dashboard_stale_minutes),
        encoding="utf-8",
    )
    return output_path


def command_dashboard(config: Config, output: str) -> int:
    output_path = write_dashboard(config, output)
    print(f"Wrote dashboard to {output_path}")
    return 0


def command_dashboard_refresh(config: Config, output: str, interval_minutes: float, once: bool = False) -> int:
    if not once and interval_minutes < MIN_DASHBOARD_REFRESH_MINUTES:
        raise GrowattGuardError(
            f"--interval-minutes must be at least {MIN_DASHBOARD_REFRESH_MINUTES} to avoid Growatt API overuse."
        )

    while True:
        try:
            output_path = write_dashboard(config, output)
        except Exception as exc:  # noqa: BLE001 - keep refresh service alive after transient failures
            logging.exception("Dashboard refresh failed")
            if once:
                raise
            notify_failure(config, "dashboard-refresh", str(exc))
        else:
            message = f"Dashboard refreshed: {output_path}"
            logging.info(message)
            print(message, flush=True)
            if once:
                return 0
        time.sleep(interval_minutes * 60)


def command_dashboard_stale_alert(config: Config, output: str, max_age_minutes: float | None = None) -> int:
    stale_minutes = max_age_minutes if max_age_minutes is not None else config.dashboard_stale_minutes
    output_path = resolve_dashboard_output(output)
    freshness = dashboard_freshness(output_path, stale_minutes)
    state = read_dashboard_stale_alert_state()

    if freshness["stale"]:
        message = (
            "Growatt dashboard refresh is stale.\n"
            f"Dashboard file: `{freshness['path']}`.\n"
            f"Reason: {freshness['reason']}.\n"
            f"Stale threshold: `{stale_minutes:g}` minutes."
        )
        if state and state.get("active"):
            if not state.get("notified") and config.discord_webhook_url and config.discord_notify_failure:
                if not send_discord_message(config, message):
                    raise GrowattGuardError("Dashboard stale alert could not be sent to Discord.")
                state["notified"] = True
                state["last_alert_at"] = utc_now().isoformat()
                write_dashboard_stale_alert_state(state)
            print(f"Dashboard stale alert already active: {freshness['reason']}.")
            return 0

        notified = False
        if config.discord_webhook_url and config.discord_notify_failure:
            if not send_discord_message(config, message):
                raise GrowattGuardError("Dashboard stale alert could not be sent to Discord.")
            notified = True

        write_dashboard_stale_alert_state(
            {
                "active": True,
                "notified": notified,
                "first_detected_at": utc_now().isoformat(),
                "last_alert_at": utc_now().isoformat() if notified else "",
                "path": freshness["path"],
                "reason": freshness["reason"],
                "stale_minutes": stale_minutes,
            }
        )
        print(f"Dashboard stale alert {'sent' if notified else 'recorded'}: {freshness['reason']}.")
        return 0

    if state and state.get("active"):
        clear_dashboard_stale_alert_state()
        message = (
            "Growatt dashboard refresh recovered.\n"
            f"Dashboard file is fresh again: {freshness['reason']}."
        )
        if state.get("notified") and config.discord_webhook_url and config.discord_notify_failure:
            send_discord_message(config, message)
        print(f"Dashboard stale alert cleared: {freshness['reason']}.")
        return 0

    print(f"Dashboard freshness OK: {freshness['reason']}.")
    return 0


def make_dashboard_handler(output_path: Path):
    class DashboardHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path not in {"/", "/dashboard.html"}:
                self.send_error(404)
                return
            if not output_path.exists():
                body = (
                    "<!doctype html><html><body><h1>Growatt Dashboard</h1>"
                    "<p>Dashboard has not been generated yet.</p></body></html>"
                ).encode("utf-8")
                self.send_response(503)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = output_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler API
            logging.info("Dashboard server: " + format, *args)

    return DashboardHandler


def command_serve_dashboard(config: Config, host: str, port: int, output: str) -> int:
    _ = config
    output_path = resolve_dashboard_output(output)
    handler = make_dashboard_handler(output_path)

    class ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    with ReusableThreadingTCPServer((host, port), handler) as server:
        print(f"Serving {output_path} at http://{host}:{port}/dashboard.html", flush=True)
        server.serve_forever()
    return 0


def command_rotate_logs(config: Config) -> int:
    cutoff = dt.datetime.now() - dt.timedelta(days=config.log_retention_days)
    removed = 0
    LOG_DIR.mkdir(exist_ok=True)
    for path in LOG_DIR.iterdir():
        if not path.is_file():
            continue
        if path.name in {"growatt_power_guard.log", "cron.log"}:
            continue
        if path.stat().st_mtime < cutoff.timestamp():
            path.unlink()
            removed += 1
    print(f"Removed {removed} old log/probe files older than {config.log_retention_days} days.")
    return 0


def command_weather_threshold(config: Config) -> int:
    decision = choose_preserve_threshold(config)
    print(f"Threshold: {decision.threshold:g}%")
    print(f"Category: {decision.weather_category}")
    print(f"Reason: {decision.reason}")
    return 0


def command_battery_alert(config: Config) -> int:
    _, _, status = load_context(config)
    soc_result = extract_soc(status)
    if not soc_result:
        raise GrowattGuardError("Could not find battery SOC in Growatt response. Run the probe command.")

    soc, path = soc_result
    previous_mode = describe_status_output_source(status) or "unknown"
    state = read_battery_alert_state()
    recovery_soc = max(config.emergency_soc_recovery, config.emergency_soc)

    if soc < config.emergency_soc:
        if state and state.get("active"):
            print(
                f"Emergency battery alert already active: SOC {soc:g}% < "
                f"{config.emergency_soc:g}% ({previous_mode})."
            )
            return 0
        if not config.discord_webhook_url:
            raise GrowattGuardError("DISCORD_WEBHOOK_URL must be configured for emergency battery alerts.")

        message = (
            "Growatt emergency battery alert.\n"
            f"SOC `{soc:g}%` is below emergency threshold `{config.emergency_soc:g}%`.\n"
            f"Current output source: `{previous_mode}`.\n"
            f"SOC source: `{path}`."
        )
        if not send_discord_message(config, message):
            raise GrowattGuardError("Emergency battery alert could not be sent to Discord.")
        write_battery_alert_state(soc)
        print(f"Emergency battery alert sent: SOC {soc:g}% < {config.emergency_soc:g}%.")
        return 0

    if state and state.get("active") and soc >= recovery_soc:
        clear_battery_alert_state()
        message = (
            "Growatt battery alert recovered.\n"
            f"SOC `{soc:g}%` is now at or above recovery threshold `{recovery_soc:g}%`.\n"
            f"Current output source: `{previous_mode}`."
        )
        if config.discord_webhook_url:
            send_discord_message(config, message)
        print(f"Emergency battery alert cleared: SOC {soc:g}% >= {recovery_soc:g}%.")
        return 0

    print(f"Battery alert OK: SOC {soc:g}% >= {config.emergency_soc:g}% ({previous_mode}).")
    return 0


def command_pause(config: Config, hours: float, reason: str) -> int:
    state = write_pause_state(hours, reason)
    message = f"Growatt automation paused until {format_local_time(state['paused_until_dt'])}."
    if reason:
        message += f"\nReason: {reason}"
    send_discord_message(config, message)
    print(message)
    return 0


def command_resume(config: Config) -> int:
    was_paused = read_pause_state() is not None
    if PAUSE_FILE.exists():
        PAUSE_FILE.unlink()
    message = "Growatt automation resumed." if was_paused else "Growatt automation was not paused."
    send_discord_message(config, message)
    print(message)
    return 0


def command_pause_status(config: Config) -> int:
    _ = config
    state = read_pause_state()
    if not state:
        print("Growatt automation is active.")
        return 0
    print(f"Growatt automation is paused: {pause_message(state)}.")
    return 0


def health_result(checks: list[HealthCheckItem]) -> str:
    statuses = {check.status for check in checks}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "OK"


def format_health_report(checks: list[HealthCheckItem]) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    result = health_result(checks)
    lines = [f"Growatt health check - {now}", f"Result: {result}", ""]
    for check in checks:
        detail = " ".join(str(check.detail).split())
        lines.append(f"[{check.status}] {check.name}: {detail}")
    return "\n".join(lines)


def check_cron_schedule(schedule: dict[str, Any]) -> list[HealthCheckItem]:
    if os.name == "nt":
        return [
            HealthCheckItem(
                "Cron",
                "WARN",
                "cron check skipped on Windows; verify Task Scheduler locally or run this on the VPS.",
            )
        ]

    try:
        completed = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return [HealthCheckItem("Cron", "WARN", "crontab command not found; cron check skipped.")]
    except subprocess.TimeoutExpired:
        return [HealthCheckItem("Cron", "FAIL", "crontab -l timed out after 10 seconds.")]

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "no crontab installed").strip()
        return [HealthCheckItem("Cron", "FAIL", f"crontab -l failed: {message}")]

    cron_text = completed.stdout
    cron_lines = [line.strip() for line in cron_text.splitlines()]
    expected_jobs = schedule["jobs"]
    missing: list[str] = []
    for index, job in enumerate(expected_jobs, start=1):
        cron = str(job["cron"]).strip()
        job_id = schedule_job_id(job, index)
        tokens = schedule_job_tokens(job, index)
        wrapper_fragment = f"growatt_power_guard.py run-scheduled {job_id}"
        direct_fragment = "growatt_power_guard.py " + " ".join(tokens)
        found = any(
            line.startswith(f"{cron} ")
            and (wrapper_fragment in line or direct_fragment in line)
            and "# growatt-power-guard" in line
            for line in cron_lines
        )
        if not found:
            missing.append(f"{cron} run-scheduled {job_id}")

    checks: list[HealthCheckItem] = []
    installed_count = sum(1 for line in cron_lines if "# growatt-power-guard" in line)
    if missing:
        checks.append(
            HealthCheckItem(
                "Cron jobs",
                "FAIL",
                (
                    f"{installed_count}/{len(expected_jobs)} growatt jobs found; "
                    f"missing: {', '.join(missing)}"
                ),
            )
        )
    else:
        checks.append(HealthCheckItem("Cron jobs", "OK", f"{len(expected_jobs)} scheduled jobs installed."))

    timezone = str(schedule.get("timezone", "")).strip()
    if timezone and f"CRON_TZ={timezone}" not in cron_text:
        checks.append(HealthCheckItem("Cron timezone", "WARN", f"CRON_TZ={timezone} not found in crontab."))
    elif timezone:
        checks.append(HealthCheckItem("Cron timezone", "OK", f"CRON_TZ={timezone} is installed."))

    return checks


def command_health_check(config: Config, notify: bool = False) -> int:
    checks: list[HealthCheckItem] = [
        HealthCheckItem("Config", "OK", ".env loaded and required Growatt credentials are present."),
        HealthCheckItem(
            "Dry run",
            "WARN" if config.dry_run else "OK",
            "DRY_RUN=true; mode-changing commands will only simulate." if config.dry_run else "DRY_RUN=false.",
        ),
    ]

    if config.emergency_soc_recovery <= config.emergency_soc:
        checks.append(
            HealthCheckItem(
                "Emergency alert",
                "WARN",
                (
                    f"alerts below {config.emergency_soc:g}%, but recovery "
                    f"{config.emergency_soc_recovery:g}% is not above the alert threshold."
                ),
            )
        )
    elif not config.discord_webhook_url:
        checks.append(
            HealthCheckItem(
                "Emergency alert",
                "WARN",
                f"alerts below {config.emergency_soc:g}%, but DISCORD_WEBHOOK_URL is not configured.",
            )
        )
    else:
        checks.append(
            HealthCheckItem(
                "Emergency alert",
                "OK",
                f"alerts below {config.emergency_soc:g}% and clears at {config.emergency_soc_recovery:g}%.",
            )
        )

    cloud_state = read_growatt_cloud_failure_state()
    if cloud_state:
        count = int(cloud_state.get("count", 0))
        threshold = int(cloud_state.get("threshold", config.cloud_failure_alert_threshold))
        status = "WARN" if cloud_state.get("alerted") else "OK"
        checks.append(
            HealthCheckItem(
                "Growatt cloud streak",
                status,
                f"{count}/{threshold} consecutive failure(s); last command {cloud_state.get('last_command', 'unknown')}.",
            )
        )
    else:
        checks.append(
            HealthCheckItem(
                "Growatt cloud streak",
                "OK",
                f"no active failure streak; alert threshold is {config.cloud_failure_alert_threshold}.",
            )
        )

    try:
        freshness = dashboard_freshness(DASHBOARD_FILE, config.dashboard_stale_minutes)
    except OSError as exc:
        checks.append(HealthCheckItem("Dashboard freshness", "WARN", f"could not inspect dashboard.html: {exc}"))
    else:
        status = "WARN" if freshness["stale"] else "OK"
        checks.append(
            HealthCheckItem(
                "Dashboard freshness",
                status,
                f"{freshness['reason']}; stale threshold is {config.dashboard_stale_minutes:g} minutes.",
            )
        )

    if config.mode_driver not in {"spf5000", "spf", "custom"}:
        checks.append(
            HealthCheckItem(
                "Mode driver",
                "FAIL",
                f"GROWATT_MODE_DRIVER={config.mode_driver!r} is unsupported; mode changes will fail.",
            )
        )
    elif config.mode_driver == "custom":
        if not config.utility_mode_params:
            checks.append(HealthCheckItem("Utility command", "FAIL", "custom driver missing GROWATT_UTILITY_MODE_PARAMS."))
        if not config.sbu_mode_params:
            checks.append(HealthCheckItem("SBU command", "FAIL", "custom driver missing GROWATT_SBU_MODE_PARAMS."))
        if config.utility_mode_params and config.sbu_mode_params:
            checks.append(HealthCheckItem("Mode driver", "OK", "custom mode driver parameters are configured."))
    else:
        checks.append(HealthCheckItem("Mode driver", "OK", "SPF output-source command driver is configured."))

    schedule: dict[str, Any] | None = None
    try:
        schedule = validate_schedule()
        checks.append(HealthCheckItem("Schedule", "OK", f"{len(schedule['jobs'])} jobs in {schedule['timezone']}."))
    except GrowattGuardError as exc:
        checks.append(HealthCheckItem("Schedule", "FAIL", str(exc)))

    if schedule is not None:
        try:
            overrides = validate_schedule_overrides(schedule)
        except GrowattGuardError as exc:
            checks.append(HealthCheckItem("Schedule overrides", "FAIL", str(exc)))
        else:
            count = len(overrides.get("dates", {}))
            detail = f"{count} date override(s) configured." if count else "no local date overrides configured."
            checks.append(HealthCheckItem("Schedule overrides", "OK", detail))
        checks.extend(check_cron_schedule(schedule))

    try:
        _, device, status = load_context(config)
    except Exception as exc:  # noqa: BLE001 - health check should continue reporting other checks
        checks.append(HealthCheckItem("Growatt cloud", "FAIL", str(exc)))
    else:
        checks.append(
            HealthCheckItem(
                "Growatt cloud",
                "OK",
                f"login ok; plant={device.plant_id}, device={device.device_sn}, type={device.device_type or 'unknown'}.",
            )
        )

        soc_result = extract_soc(status)
        if soc_result:
            soc, path = soc_result
            checks.append(HealthCheckItem("Battery SOC", "OK", f"{soc:g}% from {path}."))
        else:
            checks.append(HealthCheckItem("Battery SOC", "FAIL", "SOC was not found in the Growatt status response."))

        output_source = extract_spf_output_source(status)
        if output_source:
            raw, label, path = output_source
            checks.append(HealthCheckItem("Output source", "OK", f"{label} [{raw}] from {path}."))
        else:
            checks.append(
                HealthCheckItem("Output source", "FAIL", "SPF output source was not found in the Growatt status response.")
            )

    threshold_decision = choose_preserve_threshold(config)
    threshold_status = "WARN" if threshold_decision.weather_category == "unavailable" else "OK"
    checks.append(
        HealthCheckItem(
            "Preserve threshold",
            threshold_status,
            f"{threshold_decision.threshold:g}% ({threshold_decision.reason}).",
        )
    )

    pause_state = read_pause_state()
    if pause_state:
        checks.append(HealthCheckItem("Pause state", "WARN", pause_message(pause_state)))
    elif PAUSE_FILE.exists():
        checks.append(HealthCheckItem("Pause state", "WARN", "pause file exists but could not be read; automation is active."))
    else:
        checks.append(HealthCheckItem("Pause state", "OK", "automation is active."))

    lock_state = read_command_lock_state()
    if lock_state and command_lock_is_stale():
        checks.append(HealthCheckItem("Command lock", "WARN", "stale mode-command lock file is present."))
    elif lock_state:
        checks.append(
            HealthCheckItem(
                "Command lock",
                "WARN",
                f"{lock_state.get('command', 'unknown command')} has held the mode lock since {lock_state.get('created_at')}.",
            )
        )
    else:
        checks.append(HealthCheckItem("Command lock", "OK", "no active mode-command lock."))

    if notify:
        if not config.discord_webhook_url:
            checks.append(HealthCheckItem("Discord report", "FAIL", "DISCORD_WEBHOOK_URL is not configured."))
        elif send_discord_message(config, format_health_report(checks)):
            checks.append(HealthCheckItem("Discord report", "OK", "health report sent."))
        else:
            checks.append(HealthCheckItem("Discord report", "FAIL", "Discord webhook rejected the health report."))

    print(format_health_report(checks))
    return 1 if health_result(checks) == "FAIL" else 0


def schedule_job_id(job: dict[str, Any], index: int) -> str:
    job_id = str(job.get("id", "")).strip()
    if not job_id:
        raise GrowattGuardError(f"Schedule job {index} must contain a non-empty id.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
        raise GrowattGuardError(f"Schedule job {index} has invalid id: {job_id!r}")
    return job_id


def schedule_job_args(job: dict[str, Any], command: str, index: int) -> list[str]:
    raw_args = job.get("args", [])
    if raw_args in (None, ""):
        return []
    if not isinstance(raw_args, list):
        raise GrowattGuardError(f"Schedule job {index} args must be a list of strings.")

    args: list[str] = []
    for arg_index, raw_arg in enumerate(raw_args, start=1):
        if not isinstance(raw_arg, str) or not raw_arg.strip():
            raise GrowattGuardError(f"Schedule job {index} arg {arg_index} must be a non-empty string.")
        arg = raw_arg.strip()
        if "\n" in arg or "\r" in arg:
            raise GrowattGuardError(f"Schedule job {index} arg {arg_index} cannot contain newlines.")
        args.append(arg)

    allowed_args = SCHEDULE_COMMAND_ARGS.get(command, set())
    if args and not allowed_args:
        raise GrowattGuardError(f"Schedule job {index} command {command!r} does not support args.")
    unsupported = [arg for arg in args if arg not in allowed_args]
    if unsupported:
        raise GrowattGuardError(f"Schedule job {index} has unsupported args for {command!r}: {unsupported}")
    return args


def schedule_job_tokens(job: dict[str, Any], index: int = 0) -> list[str]:
    command = str(job.get("command", "")).strip()
    return [command, *schedule_job_args(job, command, index)]


def validate_schedule_overrides(schedule: dict[str, Any], path: Path = SCHEDULE_OVERRIDES_FILE) -> dict[str, Any]:
    if not path.exists():
        return {"dates": {}}
    try:
        overrides = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GrowattGuardError(f"Invalid schedule overrides JSON: {exc}") from exc
    if not isinstance(overrides, dict):
        raise GrowattGuardError("schedule_overrides.json must contain a JSON object.")

    dates = overrides.get("dates", {})
    if not isinstance(dates, dict):
        raise GrowattGuardError("schedule_overrides.json dates must be an object.")

    job_ids = {schedule_job_id(job, index) for index, job in enumerate(schedule["jobs"], start=1)}
    for date_key, override in dates.items():
        try:
            dt.date.fromisoformat(str(date_key))
        except ValueError as exc:
            raise GrowattGuardError(f"Invalid override date: {date_key!r}") from exc
        if not isinstance(override, dict):
            raise GrowattGuardError(f"Override for {date_key} must be an object.")

        skip = override.get("skip", [])
        if skip in (None, ""):
            skip = []
        if not isinstance(skip, list) or not all(isinstance(item, str) and item in job_ids for item in skip):
            raise GrowattGuardError(f"Override skip list for {date_key} must contain known schedule job ids.")

        skip_all = override.get("skip_all", False)
        if not isinstance(skip_all, bool):
            raise GrowattGuardError(f"Override skip_all for {date_key} must be true or false.")

        replace = override.get("replace", {})
        if replace in (None, ""):
            replace = {}
        if not isinstance(replace, dict):
            raise GrowattGuardError(f"Override replace for {date_key} must be an object.")
        for job_id, replacement in replace.items():
            if job_id not in job_ids:
                raise GrowattGuardError(f"Override replace for {date_key} references unknown job id {job_id!r}.")
            if not isinstance(replacement, dict):
                raise GrowattGuardError(f"Override replacement for {date_key}/{job_id} must be an object.")
            command = str(replacement.get("command", "")).strip()
            if command not in SCHEDULE_COMMANDS:
                raise GrowattGuardError(
                    f"Override replacement for {date_key}/{job_id} has unsupported command: {command!r}"
                )
            schedule_job_args(replacement, command, 0)

    return {"dates": dates}


def find_schedule_job(schedule: dict[str, Any], job_id: str) -> tuple[dict[str, Any], int]:
    for index, job in enumerate(schedule["jobs"], start=1):
        if schedule_job_id(job, index) == job_id:
            return job, index
    raise GrowattGuardError(f"Schedule job id not found: {job_id}")


def today_schedule_override(overrides: dict[str, Any], today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    value = overrides.get("dates", {}).get(today.isoformat(), {})
    return value if isinstance(value, dict) else {}


def command_run_scheduled(config: Config, job_id: str) -> int:
    schedule = validate_schedule()
    job, index = find_schedule_job(schedule, job_id)
    overrides = validate_schedule_overrides(schedule)
    override = today_schedule_override(overrides)
    today = dt.date.today().isoformat()
    note = str(override.get("note", "")).strip()

    if override.get("skip_all") or job_id in override.get("skip", []):
        message = f"Skipped scheduled job `{job_id}` for {today} due to schedule override."
        if note:
            message += f" Note: {note}"
        logging.info(message)
        if config.discord_notify_skip:
            send_discord_message(config, message)
        print(message)
        return 0

    replacement = override.get("replace", {}).get(job_id) if isinstance(override.get("replace", {}), dict) else None
    if replacement:
        tokens = schedule_job_tokens(replacement, 0)
        logging.info("Running schedule override for %s: %s", job_id, " ".join(tokens))
    else:
        tokens = schedule_job_tokens(job, index)

    args = parse_command_tokens(tokens)
    return dispatch_command(config, args)


def validate_schedule(path: Path = SCHEDULE_FILE) -> dict[str, Any]:
    if not path.exists():
        raise GrowattGuardError(f"Schedule file not found: {path}")
    try:
        schedule = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GrowattGuardError(f"Invalid schedule JSON: {exc}") from exc

    timezone = schedule.get("timezone")
    jobs = schedule.get("jobs")
    if not isinstance(timezone, str) or not timezone.strip():
        raise GrowattGuardError("schedule.json must contain a non-empty timezone.")
    if not isinstance(jobs, list) or not jobs:
        raise GrowattGuardError("schedule.json must contain at least one job.")

    job_ids: set[str] = set()
    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise GrowattGuardError(f"Schedule job {index} must be an object.")
        job_id = schedule_job_id(job, index)
        if job_id in job_ids:
            raise GrowattGuardError(f"Schedule job {index} has duplicate id: {job_id!r}")
        job_ids.add(job_id)
        cron = str(job.get("cron", "")).strip()
        command = str(job.get("command", "")).strip()
        if len(cron.split()) != 5:
            raise GrowattGuardError(f"Schedule job {index} has invalid cron expression: {cron!r}")
        if command not in SCHEDULE_COMMANDS:
            raise GrowattGuardError(f"Schedule job {index} has unsupported command: {command!r}")
        schedule_job_args(job, command, index)
    return schedule


def command_validate_schedule(config: Config | None = None) -> int:
    _ = config
    schedule = validate_schedule()
    print(f"Schedule OK: {len(schedule['jobs'])} jobs in {schedule['timezone']}.")
    overrides = validate_schedule_overrides(schedule)
    if overrides.get("dates"):
        print(f"Schedule overrides OK: {len(overrides['dates'])} date override(s).")
    return 0


def command_test_discord(config: Config) -> int:
    if not config.discord_webhook_url:
        raise GrowattGuardError("DISCORD_WEBHOOK_URL is not configured in .env.")
    ok = send_discord_message(config, "Growatt Guard Discord test message.")
    if not ok:
        raise GrowattGuardError("Discord test message failed. Check the webhook URL and network access.")
    print("Discord test message sent.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Growatt SPF battery-preservation automation.")
    parser.add_argument("--verbose", action="store_true", help="Log extra details.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Log in, select plant/device, and print battery SOC.")
    subparsers.add_parser("probe", help="Write redacted raw Growatt responses to logs/ for setup.")
    subparsers.add_parser("preserve-battery", help="Switch to Utility if battery SOC is below LOW_BATTERY_SOC.")
    subparsers.add_parser("utility-check", help="Alias for preserve-battery.")
    subparsers.add_parser("morning-check", help="Alias for preserve-battery.")
    subparsers.add_parser("return-sbu", help="Switch back to SBU.")
    subparsers.add_parser("watchdog-sbu", help="Verify output source is SBU; retry SBU once if needed.")
    subparsers.add_parser("daily-summary", help="Post/print a daily Growatt and automation summary.")
    subparsers.add_parser("weekly-summary", help="Post/print a weekly automation performance summary.")
    subparsers.add_parser("rotate-logs", help="Delete old generated probe/log files according to LOG_RETENTION_DAYS.")
    subparsers.add_parser("weather-threshold", help="Print the current weather-aware preserve-battery threshold.")
    subparsers.add_parser("battery-alert", help="Send a Discord alert if battery SOC is below EMERGENCY_SOC.")
    subparsers.add_parser("validate-schedule", help="Validate schedule.json.")
    subparsers.add_parser("test-discord", help="Send a test Discord webhook message.")
    run_parser = subparsers.add_parser("run-scheduled", help="Run a schedule job by id, applying date overrides first.")
    run_parser.add_argument("job_id", help="Schedule job id from schedule.json.")
    health_parser = subparsers.add_parser("health-check", help="Run read-only configuration and connectivity checks.")
    health_parser.add_argument("--notify", action="store_true", help="Post the health report to Discord.")
    dashboard_parser = subparsers.add_parser("dashboard", help="Generate a small local HTML dashboard.")
    dashboard_parser.add_argument("--output", default=str(DASHBOARD_FILE), help="Dashboard HTML output path.")
    refresh_parser = subparsers.add_parser("dashboard-refresh", help="Regenerate dashboard.html on a safe interval.")
    refresh_parser.add_argument("--output", default=str(DASHBOARD_FILE), help="Dashboard HTML output path.")
    refresh_parser.add_argument(
        "--interval-minutes",
        type=float,
        default=10,
        help=f"Refresh interval. Minimum {MIN_DASHBOARD_REFRESH_MINUTES} minutes unless --once is used.",
    )
    refresh_parser.add_argument("--once", action="store_true", help="Refresh once, then exit.")
    stale_parser = subparsers.add_parser("dashboard-stale-alert", help="Alert if dashboard.html has not refreshed recently.")
    stale_parser.add_argument("--output", default=str(DASHBOARD_FILE), help="Dashboard HTML file to check.")
    stale_parser.add_argument(
        "--max-age-minutes",
        type=float,
        default=None,
        help="Override DASHBOARD_STALE_MINUTES for this check.",
    )
    serve_parser = subparsers.add_parser("serve-dashboard", help="Serve dashboard.html without calling Growatt.")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind host. Use 127.0.0.1 for SSH tunnel access.")
    serve_parser.add_argument("--port", type=int, default=8080, help="Bind port.")
    serve_parser.add_argument("--output", default=str(DASHBOARD_FILE), help="Dashboard HTML file to serve.")
    pause_parser = subparsers.add_parser("pause", help="Pause scheduled mode-changing automation.")
    pause_parser.add_argument("--hours", type=float, required=True, help="How long to pause automation for.")
    pause_parser.add_argument("--reason", default="", help="Optional reason stored in pause state and Discord alert.")
    subparsers.add_parser("resume", help="Resume scheduled mode-changing automation.")
    subparsers.add_parser("pause-status", help="Show whether automation is currently paused.")
    return parser


def parse_command_tokens(tokens: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(tokens)


def dispatch_command(config: Config, args: argparse.Namespace) -> int:
    command = args.command

    def action() -> int:
        if command == "status":
            return command_status(config)
        if command == "probe":
            return command_probe(config)
        if command == "preserve-battery":
            return command_preserve_battery(config)
        if command == "utility-check":
            return command_utility_check(config)
        if command == "morning-check":
            return command_morning_check(config)
        if command == "return-sbu":
            return command_return_sbu(config)
        if command == "watchdog-sbu":
            return command_watchdog_sbu(config)
        if command == "daily-summary":
            return command_daily_summary(config)
        if command == "weekly-summary":
            return command_weekly_summary(config)
        if command == "rotate-logs":
            return command_rotate_logs(config)
        if command == "weather-threshold":
            return command_weather_threshold(config)
        if command == "battery-alert":
            return command_battery_alert(config)
        if command == "test-discord":
            return command_test_discord(config)
        if command == "health-check":
            return command_health_check(config, args.notify)
        if command == "dashboard":
            return command_dashboard(config, args.output)
        if command == "dashboard-refresh":
            return command_dashboard_refresh(config, args.output, args.interval_minutes, args.once)
        if command == "dashboard-stale-alert":
            return command_dashboard_stale_alert(config, args.output, args.max_age_minutes)
        if command == "serve-dashboard":
            return command_serve_dashboard(config, args.host, args.port, args.output)
        if command == "run-scheduled":
            return command_run_scheduled(config, args.job_id)
        if command == "pause":
            return command_pause(config, args.hours, args.reason)
        if command == "resume":
            return command_resume(config)
        if command == "pause-status":
            return command_pause_status(config)
        raise GrowattGuardError(f"Unknown command: {command}")

    if command in LOCKED_COMMANDS:
        return run_with_command_lock(config, command, action)
    return action()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    config: Config | None = None

    try:
        if args.command == "validate-schedule":
            return command_validate_schedule()
        config = load_config()
        logging.info("Command=%s dry_run=%s low_soc=%s", args.command, config.dry_run, config.low_battery_soc)
        return dispatch_command(config, args)
    except GrowattGuardError as exc:
        logging.error("%s", exc)
        notify_failure(config, args.command, str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - logs traceback for unattended scheduler runs
        logging.exception("Unhandled error")
        notify_failure(config, args.command, str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
