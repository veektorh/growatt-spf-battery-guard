from __future__ import annotations

import datetime as dt
import logging
import sys
from dataclasses import dataclass
from typing import Any

import requests


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


def weather_error(message: str) -> Exception:
    return app_module().GrowattGuardError(message)


def parse_forecast_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def fetch_weather_forecast(config: Any) -> dict[str, Any]:
    if config.weather_lat is None or config.weather_lon is None:
        raise weather_error("WEATHER_LAT and WEATHER_LON must be set when WEATHER_ENABLED=true.")

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


def choose_preserve_threshold(config: Any) -> ThresholdDecision:
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
