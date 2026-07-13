from __future__ import annotations

import datetime as dt
from typing import Any

from growatt_guard.state import (
    read_forecast_calibration_history,
    write_forecast_calibration_history,
)

FORECAST_CALIBRATION_RETENTION_DAYS = 45
FORECAST_CALIBRATION_MIN_SAMPLES = 5
RAINY_FORECAST_DEFAULT_FACTOR = 0.60
RAINY_FORECAST_MIN_FACTOR = 0.40
RAINY_FORECAST_MAX_FACTOR = 1.00


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _actual_pv_by_date(
    metrics_history: list[dict[str, Any]],
    today: dt.date,
) -> dict[str, float]:
    actual: dict[str, float] = {}
    for row in metrics_history:
        timestamp = row.get("timestamp")
        value = _number(row.get("pv_today_kwh"))
        if not timestamp or value is None or value < 0:
            continue
        try:
            row_date = dt.datetime.fromisoformat(str(timestamp)).date()
        except ValueError:
            continue
        if row_date >= today:
            continue
        key = row_date.isoformat()
        actual[key] = max(actual.get(key, 0.0), value)
    return actual


def _summarize(
    rows: list[dict[str, Any]],
    sunny_threshold_kwh_m2: float,
) -> dict[str, Any]:
    completed = [
        row for row in rows
        if _number(row.get("predicted_kwh")) is not None and _number(row.get("actual_kwh")) is not None
    ]
    errors = [float(row["actual_kwh"]) - float(row["predicted_kwh"]) for row in completed]
    percentage_errors = [
        abs(float(row["actual_kwh"]) - float(row["predicted_kwh"])) / float(row["actual_kwh"]) * 100
        for row in completed
        if float(row["actual_kwh"]) >= 0.5
    ]
    predicted_total = sum(float(row["predicted_kwh"]) for row in completed)
    actual_total = sum(float(row["actual_kwh"]) for row in completed)
    realization_ratio = actual_total / predicted_total if predicted_total > 0 else None
    sample_count = len(completed)

    rainy_rows = [
        row for row in completed
        if str(row.get("weather_category", "")).strip().lower() == "rainy/cloudy"
    ]
    rainy_predicted = sum(float(row["predicted_kwh"]) for row in rainy_rows)
    rainy_actual = sum(float(row["actual_kwh"]) for row in rainy_rows)
    rainy_ratio = rainy_actual / rainy_predicted if rainy_predicted > 0 else None
    rainy_sample_count = len(rainy_rows)
    rainy_errors = [
        float(row["actual_kwh"]) - float(row["predicted_kwh"])
        for row in rainy_rows
    ]
    confidence = (
        "high" if rainy_sample_count >= 14
        else ("medium" if rainy_sample_count >= FORECAST_CALIBRATION_MIN_SAMPLES else "learning")
    )
    rainy_factor = RAINY_FORECAST_DEFAULT_FACTOR
    rainy_factor_source = "conservative default"
    if rainy_sample_count >= FORECAST_CALIBRATION_MIN_SAMPLES and rainy_ratio is not None:
        rainy_factor = round(
            min(RAINY_FORECAST_MAX_FACTOR, max(RAINY_FORECAST_MIN_FACTOR, rainy_ratio)),
            2,
        )
        rainy_factor_source = "learned from rainy/cloudy days"
        recommendation = (
            f"Rainy/cloudy forecasts use {rainy_factor * 100:.0f}% of the Open-Meteo base, "
            f"learned from {rainy_sample_count} comparable completed day(s)."
        )
    else:
        remaining = FORECAST_CALIBRATION_MIN_SAMPLES - rainy_sample_count
        recommendation = (
            f"Using a conservative {RAINY_FORECAST_DEFAULT_FACTOR * 100:.0f}% rainy/cloudy factor; "
            f"collect {remaining} more comparable completed day(s) before learning it."
        )

    sunny_rows = [
        row for row in completed
        if sunny_threshold_kwh_m2 > 0
        and (_number(row.get("irradiance_kwh_m2")) or 0) >= sunny_threshold_kwh_m2
        and str(row.get("weather_category", "")).strip().lower() != "rainy/cloudy"
    ]
    sunny_predicted = sum(float(row["predicted_kwh"]) for row in sunny_rows)
    sunny_actual = sum(float(row["actual_kwh"]) for row in sunny_rows)

    return {
        "sample_count": sample_count,
        "confidence": confidence,
        "mean_absolute_error_kwh": round(sum(abs(error) for error in errors) / sample_count, 2) if errors else None,
        "mean_absolute_percentage_error": round(sum(percentage_errors) / len(percentage_errors), 1) if percentage_errors else None,
        "mean_bias_kwh": round(sum(errors) / sample_count, 2) if errors else None,
        "actual_to_forecast_ratio": round(realization_ratio, 3) if realization_ratio is not None else None,
        "recommendation": recommendation,
        "rainy_sample_count": rainy_sample_count,
        "rainy_actual_to_forecast_ratio": round(rainy_ratio, 3) if rainy_ratio is not None else None,
        "rainy_adjustment_factor": rainy_factor,
        "rainy_mean_absolute_error_kwh": (
            round(sum(abs(error) for error in rainy_errors) / rainy_sample_count, 2) if rainy_errors else None
        ),
        "rainy_adjustment_source": rainy_factor_source,
        "sunny_sample_count": len(sunny_rows),
        "sunny_actual_to_forecast_ratio": round(sunny_actual / sunny_predicted, 3) if sunny_predicted > 0 else None,
        "recent": completed[-7:],
    }


def summarize_forecast_calibration(
    *,
    current_performance_ratio: float | None = None,
    sunny_threshold_kwh_m2: float = 0.0,
) -> dict[str, Any]:
    # Retained for caller compatibility; weather calibration must not tune it.
    del current_performance_ratio
    return _summarize(
        read_forecast_calibration_history(),
        sunny_threshold_kwh_m2,
    )


def update_forecast_calibration(
    pv_forecast: dict[str, Any] | None,
    metrics_history: list[dict[str, Any]],
    *,
    current_performance_ratio: float | None = None,
    sunny_threshold_kwh_m2: float = 0.0,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    # Retained for caller compatibility; weather calibration must not tune it.
    del current_performance_ratio
    local_now = now or dt.datetime.now().astimezone()
    today = local_now.date()
    cutoff = today - dt.timedelta(days=FORECAST_CALIBRATION_RETENTION_DAYS)
    rows = []
    for row in read_forecast_calibration_history():
        try:
            forecast_date = dt.date.fromisoformat(str(row.get("forecast_date")))
        except ValueError:
            continue
        if forecast_date >= cutoff:
            rows.append(dict(row))

    actual_by_date = _actual_pv_by_date(metrics_history, today)
    for row in rows:
        actual = actual_by_date.get(str(row.get("forecast_date")))
        if actual is not None:
            row["actual_kwh"] = round(actual, 2)

    predicted = None
    if pv_forecast:
        predicted = _number(pv_forecast.get("base_tomorrow_kwh"))
        if predicted is None:
            predicted = _number(pv_forecast.get("tomorrow_kwh"))
    if predicted is not None:
        target_date = (today + dt.timedelta(days=1)).isoformat()
        rows = [row for row in rows if row.get("forecast_date") != target_date]
        forecast_row: dict[str, Any] = {
            "forecast_date": target_date,
            "issued_at": local_now.isoformat(timespec="seconds"),
            "predicted_kwh": round(predicted, 2),
        }
        irradiance = _number(pv_forecast.get("tomorrow_irradiance_kwh_m2"))
        if irradiance is not None:
            forecast_row["irradiance_kwh_m2"] = round(irradiance, 2)
        weather_category = str(pv_forecast.get("tomorrow_weather_category") or "").strip()
        if weather_category:
            forecast_row["weather_category"] = weather_category
        cloud_cover = _number(pv_forecast.get("tomorrow_cloud_cover"))
        if cloud_cover is not None:
            forecast_row["cloud_cover"] = round(cloud_cover, 1)
        precipitation = _number(pv_forecast.get("tomorrow_precipitation_mm"))
        if precipitation is not None:
            forecast_row["precipitation_mm"] = round(precipitation, 1)
        rows.append(forecast_row)

    rows.sort(key=lambda row: str(row.get("forecast_date", "")))
    write_forecast_calibration_history(rows)
    return _summarize(rows, sunny_threshold_kwh_m2)


def apply_weather_adjustment(
    pv_forecast: dict[str, Any] | None,
    calibration: dict[str, Any],
) -> dict[str, Any] | None:
    """Apply a non-compounding rainy/cloudy factor to the headline PV forecast."""
    if pv_forecast is None:
        return None

    base_kwh = _number(pv_forecast.get("base_tomorrow_kwh"))
    if base_kwh is None:
        base_kwh = _number(pv_forecast.get("tomorrow_kwh"))
    if base_kwh is None:
        return pv_forecast

    pv_forecast["base_tomorrow_kwh"] = round(base_kwh, 1)
    category = str(pv_forecast.get("tomorrow_weather_category") or "").strip().lower()
    if category != "rainy/cloudy":
        pv_forecast["weather_adjusted"] = False
        return pv_forecast

    factor = _number(calibration.get("rainy_adjustment_factor"))
    if factor is None:
        factor = RAINY_FORECAST_DEFAULT_FACTOR
    factor = min(RAINY_FORECAST_MAX_FACTOR, max(RAINY_FORECAST_MIN_FACTOR, factor))
    pv_forecast["tomorrow_kwh"] = round(base_kwh * factor, 1)
    pv_forecast["weather_adjusted"] = True
    pv_forecast["weather_adjustment_factor"] = round(factor, 2)
    pv_forecast["weather_adjustment_source"] = str(
        calibration.get("rainy_adjustment_source") or "conservative default"
    )
    return pv_forecast
