from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from growatt_guard.state import STATE_DIR

WEATHER_CACHE_FILE = STATE_DIR / "weather_cache.json"
WEATHER_CACHE_TTL_SECONDS = 15 * 60

RAINY_SEASON_MONTHS: frozenset[int] = frozenset(range(4, 11))  # April–October (Lagos)

DRY_SEASON_THRESHOLDS: dict[str, float] = {
    "rainy/cloudy": 45.0,
    "normal": 40.0,
    "sunny": 35.0,
    "disabled": 45.0,
    "unavailable": 45.0,
}


@dataclass(frozen=True)
class ThresholdDecision:
    threshold: float
    reason: str
    weather_category: str = "disabled"
    cloud_cover: float | None = None
    precipitation_mm: float | None = None


def app_module() -> Any:
    module = sys.modules.get("growatt_power_guard")
    if module is not None and hasattr(module, "GrowattGuardError"):
        return module

    main_module = sys.modules.get("__main__")
    if main_module is not None and hasattr(main_module, "GrowattGuardError"):
        return main_module

    import growatt_power_guard

    return growatt_power_guard


def current_season(date: dt.date | None = None) -> str:
    d = date or dt.date.today()
    return "rainy" if d.month in RAINY_SEASON_MONTHS else "dry"


def apply_season_adjustment(decision: ThresholdDecision, season: str) -> ThresholdDecision:
    if season != "dry":
        return decision
    dry_threshold = DRY_SEASON_THRESHOLDS.get(decision.weather_category)
    if dry_threshold is None:
        return decision
    return ThresholdDecision(
        threshold=dry_threshold,
        reason=decision.reason + f"; dry season (Lagos): lowered to {dry_threshold:g}%",
        weather_category=decision.weather_category,
        cloud_cover=decision.cloud_cover,
        precipitation_mm=decision.precipitation_mm,
    )


def weather_error(message: str) -> Exception:
    return app_module().GrowattGuardError(message)


def parse_forecast_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _read_weather_cache() -> dict[str, Any] | None:
    if not WEATHER_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(WEATHER_CACHE_FILE.read_text(encoding="utf-8"))
        fetched_at = dt.datetime.fromisoformat(str(data["fetched_at"]))
        age = (dt.datetime.now() - fetched_at).total_seconds()
        if age > WEATHER_CACHE_TTL_SECONDS:
            return None
        return data.get("forecast")
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def _write_weather_cache(forecast: dict[str, Any]) -> None:
    try:
        WEATHER_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"fetched_at": dt.datetime.now().isoformat(), "forecast": forecast}
        WEATHER_CACHE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        logging.warning("Could not write weather cache: %s", exc)


def fetch_weather_forecast(config: Any) -> dict[str, Any]:
    if config.weather_lat is None or config.weather_lon is None:
        raise weather_error("WEATHER_LAT and WEATHER_LON must be set when WEATHER_ENABLED=true.")

    cached = _read_weather_cache()
    if cached is not None:
        logging.debug("Using cached weather forecast (age < %d min).", WEATHER_CACHE_TTL_SECONDS // 60)
        return cached

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
    forecast = response.json()
    _write_weather_cache(forecast)
    return forecast


def analyze_weather_window(config: Any, forecast: dict[str, Any]) -> ThresholdDecision:
    hourly = forecast.get("hourly", {})
    times = hourly.get("time", [])
    cloud_cover = hourly.get("cloud_cover", [])
    precipitation = hourly.get("precipitation", [])
    if not times or not cloud_cover or not precipitation:
        raise weather_error("Weather response did not include hourly time, cloud_cover and precipitation.")

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


def choose_preserve_threshold(config: Any, today: dt.date | None = None) -> ThresholdDecision:
    if not config.weather_enabled:
        decision = ThresholdDecision(
            threshold=config.low_battery_soc,
            reason=f"weather disabled; using fixed threshold {config.low_battery_soc:g}%",
        )
    else:
        try:
            decision = analyze_weather_window(config, fetch_weather_forecast(config))
        except Exception as exc:  # noqa: BLE001 - preserve automation if weather is unavailable
            logging.warning("Weather threshold unavailable, using fixed threshold: %s", exc)
            decision = ThresholdDecision(
                threshold=config.low_battery_soc,
                reason=f"weather unavailable; using fixed threshold {config.low_battery_soc:g}%",
                weather_category="unavailable",
            )

    if getattr(config, "season_profiles_enabled", False):
        season = current_season(today)
        decision = apply_season_adjustment(decision, season)
        logging.debug("Season: %s; threshold after season adjustment: %g%%", season, decision.threshold)

    return decision
