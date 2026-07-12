from __future__ import annotations

import datetime as dt
from typing import Any

from growatt_guard.state import (
    read_forecast_calibration_history,
    write_forecast_calibration_history,
)

FORECAST_CALIBRATION_RETENTION_DAYS = 45
FORECAST_CALIBRATION_MIN_SAMPLES = 5


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
    current_performance_ratio: float,
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
    confidence = "high" if sample_count >= 14 else ("medium" if sample_count >= FORECAST_CALIBRATION_MIN_SAMPLES else "learning")

    recommendation = "Collect at least 5 completed forecast days before tuning PANEL_PERFORMANCE_RATIO."
    suggested_ratio: float | None = None
    if sample_count >= FORECAST_CALIBRATION_MIN_SAMPLES and realization_ratio is not None:
        raw_suggestion = current_performance_ratio * realization_ratio
        suggested_ratio = round(min(1.0, max(0.4, raw_suggestion)), 2)
        if abs(realization_ratio - 1.0) < 0.1:
            recommendation = "Forecasts are tracking actual PV closely; keep PANEL_PERFORMANCE_RATIO unchanged."
        else:
            direction = "low" if realization_ratio > 1 else "high"
            recommendation = (
                f"Forecasts are running {direction}; consider PANEL_PERFORMANCE_RATIO={suggested_ratio:.2f} "
                "after reviewing shading, clipping, and missing telemetry."
            )

    sunny_rows = [
        row for row in completed
        if sunny_threshold_kwh_m2 > 0
        and (_number(row.get("irradiance_kwh_m2")) or 0) >= sunny_threshold_kwh_m2
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
        "suggested_performance_ratio": suggested_ratio,
        "recommendation": recommendation,
        "sunny_sample_count": len(sunny_rows),
        "sunny_actual_to_forecast_ratio": round(sunny_actual / sunny_predicted, 3) if sunny_predicted > 0 else None,
        "recent": completed[-7:],
    }


def summarize_forecast_calibration(
    *,
    current_performance_ratio: float,
    sunny_threshold_kwh_m2: float = 0.0,
) -> dict[str, Any]:
    return _summarize(
        read_forecast_calibration_history(),
        current_performance_ratio,
        sunny_threshold_kwh_m2,
    )


def update_forecast_calibration(
    pv_forecast: dict[str, Any] | None,
    metrics_history: list[dict[str, Any]],
    *,
    current_performance_ratio: float,
    sunny_threshold_kwh_m2: float = 0.0,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
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

    predicted = _number(pv_forecast.get("tomorrow_kwh")) if pv_forecast else None
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
        rows.append(forecast_row)

    rows.sort(key=lambda row: str(row.get("forecast_date", "")))
    write_forecast_calibration_history(rows)
    return _summarize(rows, current_performance_ratio, sunny_threshold_kwh_m2)
