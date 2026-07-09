from __future__ import annotations

import datetime as dt
import html
import http.server
import json
import logging
import socketserver
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

from growatt_guard.audit import build_chart_data, read_mode_audit_rows
from growatt_guard.dashboard_assets import DASHBOARD_CSS, DASHBOARD_JS
from growatt_guard.pvoutput import publish_pvoutput_status_from_status, read_pvoutput_state
from growatt_guard.growatt_api import (
    PV_POWER_CHANNELS,
    PV_TODAY_CHANNELS,
    estimate_charge_time,
    estimate_runtime,
    estimate_topup_for_sunrise,
    deep_values,
    detect_unexpected_grid_bypass,
    extract_battery_status,
    extract_channel_metric_sum,
    extract_first_metric,
    extract_soc,
    extract_spf_output_source,
    format_duration_minutes,
    load_context,
    parse_number,
)
from growatt_guard.state import (
    clear_dashboard_stale_alert_state,
    pause_message,
    read_battery_alert_state,
    read_dashboard_stale_alert_state,
    read_discharge_rate_history,
    read_growatt_cloud_failure_state,
    read_pause_state,
    utc_now,
    write_dashboard_stale_alert_state,
)
from growatt_guard.schedule import (
    cron_matches,
    next_scheduled_runs,
    schedule_job_id,
    schedule_job_tokens,
    today_schedule_override,
    validate_schedule,
    validate_schedule_overrides,
)
from growatt_guard.weather import choose_preserve_threshold, get_pv_forecast, hours_until_next_sunrise


BASE_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = BASE_DIR / "logs"
DASHBOARD_FILE = BASE_DIR / "dashboard.html"
DASHBOARD_JSON_FILE = BASE_DIR / "dashboard.json"
DASHBOARD_METRICS_FILE = LOG_DIR / "dashboard_metrics.jsonl"
DASHBOARD_METRICS_RETENTION_DAYS = 8
MIN_DASHBOARD_REFRESH_MINUTES = 5

PV_POWER_KEYS = ("ppv", "ppvText", "pPv", "pvPower")
PV_TODAY_KEYS = ("epvToday", "ePvToday", "epvTodayTotal")
PV_TOTAL_KEYS = ("epvTotalText", "ePvTotalText", "eTotalText", "epvTotal", "ePvTotal", "eTotal")
LOAD_POWER_KEYS = ("outPutPower", "outPutPower1", "activePower", "outPower")
LOAD_TODAY_KEYS = (
    "eLoadToday", "eLoadTodayText", "eloadToday", "eConsumptionToday",
    "consumptionToday", "useEnergyToday", "useEnergyTodayText",
    "eopDischrToday", "eopDischrTodayText",
)
GRID_POWER_KEYS = (
    "pGrid", "pGridText", "gridPower", "pImport", "pImportText",
    "pAcInput", "pAcInPut", "pacToUser", "pToUser",
)
GRID_TODAY_KEYS = (
    "eGridToday", "eGridTodayText", "eToUserToday", "eToUserTodayText",
    "eImportToday", "eImportTodayText", "eAcChargeToday", "eacChargeToday",
    "eGridChargeToday", "eGridChargeTodayText", "eGridImportToday",
    "eGridImportTodayText", "eBuyToday", "eBuyTodayText",
)
CHARGE_POWER_KEYS = ("pCharge", "pChargeText", "chargePower")
DISCHARGE_POWER_KEYS = ("pDischarge", "pDischargeText", "dischargePower")
CHARGE_TODAY_KEYS = ("eChargeToday", "eChargeTodayText", "eacChargeToday", "eAcChargeToday")
DISCHARGE_TODAY_KEYS = ("eDischargeToday", "eDischargeTodayText")
LOAD_PERCENT_KEYS = ("loadPercent", "loadPercent1")
BATTERY_VOLTAGE_KEYS = ("vBat", "vBat1", "vbat")


def app_module() -> Any:
    module = sys.modules.get("growatt_power_guard")
    if module is not None and hasattr(module, "GrowattGuardError"):
        return module

    main_module = sys.modules.get("__main__")
    if main_module is not None and hasattr(main_module, "GrowattGuardError"):
        return main_module

    import growatt_power_guard

    return growatt_power_guard


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


def _metric_number(status: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    result = extract_first_metric(status, keys)
    if result is None:
        return None
    return parse_number(result[0])


def _metric_max(status: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    values: list[float] = []
    wanted = set(keys)
    for path, value in deep_values(status):
        if path.split(".")[-1] not in wanted:
            continue
        parsed = parse_number(value)
        if parsed is not None:
            values.append(parsed)
    return max(values) if values else None


def _metric_max_source(status: dict[str, Any], keys: tuple[str, ...]) -> str:
    best_value: float | None = None
    best_path = ""
    wanted = set(keys)
    for path, value in deep_values(status):
        if path.split(".")[-1] not in wanted:
            continue
        parsed = parse_number(value)
        if parsed is None:
            continue
        if best_value is None or parsed > best_value:
            best_value = parsed
            best_path = path
    return best_path


def _metric_energy_kwh_max(status: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    values: list[float] = []
    wanted = set(keys)
    for path, value in deep_values(status):
        if path.split(".")[-1] not in wanted:
            continue
        parsed = parse_number(value)
        if parsed is None:
            continue
        if isinstance(value, str) and "mwh" in value.lower():
            parsed *= 1000
        values.append(parsed)
    return max(values) if values else None


def _metric_energy_kwh_max_source(status: dict[str, Any], keys: tuple[str, ...]) -> str:
    best_value: float | None = None
    best_path = ""
    wanted = set(keys)
    for path, value in deep_values(status):
        if path.split(".")[-1] not in wanted:
            continue
        parsed = parse_number(value)
        if parsed is None:
            continue
        if isinstance(value, str) and "mwh" in value.lower():
            parsed *= 1000
        if best_value is None or parsed > best_value:
            best_value = parsed
            best_path = path
    return best_path


def _metric_number_or_channel_sum(
    status: dict[str, Any],
    total_keys: tuple[str, ...],
    channels: tuple[tuple[str, ...], ...],
) -> float | None:
    total = _metric_number(status, total_keys)
    channel_result = extract_channel_metric_sum(status, channels)
    channel_total = channel_result[0] if channel_result is not None else None
    if channel_total is not None and (total is None or channel_total > total):
        return channel_total
    return total


def _format_lifetime_kwh(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value / 1000:.2f} MWh"
    return f"{value:g} kWh"


def _metric_lifetime_text(status: dict[str, Any]) -> str:
    value_kwh = _metric_energy_kwh_max(status, PV_TOTAL_KEYS)
    if value_kwh is None:
        return ""
    return _format_lifetime_kwh(value_kwh)


def extract_dashboard_metric_sources(status: dict[str, Any]) -> dict[str, str]:
    soc_result = extract_soc(status)
    output_source = extract_spf_output_source(status)

    def first_path(keys: tuple[str, ...]) -> str:
        result = extract_first_metric(status, keys)
        return result[1] if result else ""

    pv_total = _metric_number(status, PV_POWER_KEYS)
    pv_channel_result = extract_channel_metric_sum(status, PV_POWER_CHANNELS)
    pv_source = first_path(PV_POWER_KEYS)
    if pv_channel_result is not None and (pv_total is None or pv_channel_result[0] > pv_total):
        pv_source = pv_channel_result[1]

    pv_today_total = _metric_number(status, PV_TODAY_KEYS)
    pv_today_channel_result = extract_channel_metric_sum(status, PV_TODAY_CHANNELS)
    pv_today_channel_total = pv_today_channel_result[0] if pv_today_channel_result is not None else None
    pv_today_source = first_path(PV_TODAY_KEYS)
    if pv_today_channel_total is not None and (pv_today_total is None or pv_today_channel_total > pv_today_total):
        pv_today_source = pv_today_channel_result[1] if pv_today_channel_result else pv_today_source

    return {
        "soc": soc_result[1] if soc_result else "",
        "mode": output_source[2] if output_source else "",
        "pv_w": pv_source,
        "pv_today_kwh": pv_today_source,
        "pv_total": _metric_energy_kwh_max_source(status, PV_TOTAL_KEYS),
        "load_w": first_path(LOAD_POWER_KEYS),
        "load_pct": first_path(LOAD_PERCENT_KEYS),
        "load_today_kwh": _metric_max_source(status, LOAD_TODAY_KEYS),
        "grid_w": first_path(GRID_POWER_KEYS),
        "grid_today_kwh": _metric_max_source(status, GRID_TODAY_KEYS),
        "charge_w": first_path(CHARGE_POWER_KEYS),
        "charge_today_kwh": _metric_max_source(status, CHARGE_TODAY_KEYS),
        "discharge_w": first_path(DISCHARGE_POWER_KEYS),
        "discharge_today_kwh": _metric_max_source(status, DISCHARGE_TODAY_KEYS),
        "vbat": first_path(BATTERY_VOLTAGE_KEYS),
    }


def _rounded(value: float | None, digits: int = 1) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def _parse_metric_timestamp(row: dict[str, Any]) -> dt.datetime | None:
    value = row.get("timestamp")
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone().replace(tzinfo=None)


def _metric_date(row: dict[str, Any]) -> dt.date | None:
    ts = _parse_metric_timestamp(row)
    return ts.date() if ts else None


def extract_dashboard_metrics(status: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now().astimezone()
    soc_result = extract_soc(status)
    output_source = extract_spf_output_source(status)
    bypass = detect_unexpected_grid_bypass(status)
    pv_w = _metric_number_or_channel_sum(status, PV_POWER_KEYS, PV_POWER_CHANNELS)
    load_w = _metric_number(status, LOAD_POWER_KEYS)
    charge_w = _metric_number(status, CHARGE_POWER_KEYS)
    discharge_w = _metric_number(status, DISCHARGE_POWER_KEYS)
    grid_w = _metric_number(status, GRID_POWER_KEYS)
    grid_source = "api" if grid_w is not None else ""
    if grid_w is None and any(value is not None for value in (load_w, charge_w, pv_w)):
        grid_w = max(0.0, (load_w or 0.0) + (charge_w or 0.0) - (pv_w or 0.0))
        grid_source = "estimated"

    battery_net_w: float | None = None
    if charge_w is not None or discharge_w is not None:
        battery_net_w = (discharge_w or 0.0) - (charge_w or 0.0)

    return {
        "timestamp": now.isoformat(timespec="seconds"),
        "soc": _rounded(soc_result[0] if soc_result else None),
        "soc_source": soc_result[1] if soc_result else "",
        "mode_raw": output_source[0] if output_source else "",
        "mode": output_source[1] if output_source else "",
        "mode_source": output_source[2] if output_source else "",
        "battery_status": extract_battery_status(status) or "",
        "bypass_detected": bool(bypass["detected"]),
        "bypass_reason": bypass.get("reason", ""),
        "pv_w": _rounded(pv_w, 0),
        "pv_today_kwh": _rounded(_metric_number_or_channel_sum(status, PV_TODAY_KEYS, PV_TODAY_CHANNELS), 2),
        "pv_total": _metric_lifetime_text(status),
        "load_w": _rounded(load_w, 0),
        "load_pct": _rounded(_metric_number(status, LOAD_PERCENT_KEYS), 0),
        "load_today_kwh": _rounded(_metric_max(status, LOAD_TODAY_KEYS), 2),
        "grid_w": _rounded(grid_w, 0),
        "grid_source": grid_source,
        "grid_today_kwh": _rounded(_metric_max(status, GRID_TODAY_KEYS), 2),
        "charge_w": _rounded(charge_w, 0),
        "charge_today_kwh": _rounded(_metric_max(status, CHARGE_TODAY_KEYS), 2),
        "discharge_w": _rounded(discharge_w, 0),
        "discharge_today_kwh": _rounded(_metric_max(status, DISCHARGE_TODAY_KEYS), 2),
        "battery_net_w": _rounded(battery_net_w, 0),
        "vbat": _rounded(_metric_number(status, BATTERY_VOLTAGE_KEYS), 2),
    }


def read_dashboard_metrics_history(
    now: dt.datetime | None = None,
    days: int = DASHBOARD_METRICS_RETENTION_DAYS,
) -> list[dict[str, Any]]:
    if not DASHBOARD_METRICS_FILE.exists():
        return []
    now = now or dt.datetime.now()
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    cutoff = now - dt.timedelta(days=days)
    rows: list[dict[str, Any]] = []
    try:
        lines = DASHBOARD_METRICS_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_metric_timestamp(row)
        if ts is None or ts < cutoff:
            continue
        rows.append(row)
    rows.sort(key=lambda row: str(row.get("timestamp", "")))
    return rows


def _write_dashboard_metrics_history(rows: list[dict[str, Any]]) -> None:
    DASHBOARD_METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=DASHBOARD_METRICS_FILE.parent,
        prefix=".dashboard_metrics_", suffix=".jsonl", delete=False,
    )
    try:
        for row in rows:
            tmp.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        tmp.flush()
        tmp.close()
        Path(tmp.name).replace(DASHBOARD_METRICS_FILE)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def append_dashboard_metric_snapshot(
    status: dict[str, Any],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now().astimezone()
    metric = extract_dashboard_metrics(status, now=now)
    rows = read_dashboard_metrics_history(now=now.replace(tzinfo=None))
    rows.append(metric)
    cutoff = now.replace(tzinfo=None) - dt.timedelta(days=DASHBOARD_METRICS_RETENTION_DAYS)
    rows = [row for row in rows if (ts := _parse_metric_timestamp(row)) is not None and ts >= cutoff]
    _write_dashboard_metrics_history(rows)
    return metric


def _fmt_w(value: float | None) -> str:
    if value is None:
        return "--"
    if abs(value) >= 1000:
        return f"{value / 1000:.1f} kW"
    return f"{value:.0f} W"


def _fmt_kwh(value: float | None) -> str:
    if value is None:
        return "--"
    if abs(value) >= 1000:
        return f"{value / 1000:.1f} MWh"
    return f"{value:.1f} kWh"


def _fmt_pct(value: float | None) -> str:
    return "--" if value is None else f"{value:.0f}%"


def _fmt_g(value: Any, suffix: str = "") -> str:
    if not isinstance(value, (int, float)):
        return "--"
    return f"{value:g}{suffix}"


def _fmt_volts(value: float | None) -> str:
    return "--" if value is None else f"{value:g} V"


def _history_with_live(history: list[dict[str, Any]], live: dict[str, Any]) -> list[dict[str, Any]]:
    if not history:
        return [live]
    if history[-1].get("timestamp") == live.get("timestamp"):
        return history
    return history + [live]


def _series_value(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def build_dashboard_history_payload(
    history: list[dict[str, Any]],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now()
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    cutoff = now - dt.timedelta(hours=24)
    recent = [
        row for row in history
        if (ts := _parse_metric_timestamp(row)) is not None and ts >= cutoff
    ]
    if len(recent) > 144:
        step = max(1, len(recent) // 144)
        recent = recent[::step]

    def label(row: dict[str, Any]) -> str:
        ts = _parse_metric_timestamp(row)
        return ts.strftime("%H:%M") if ts else ""

    dates = [(now.date() - dt.timedelta(days=i)) for i in range(6, -1, -1)]
    latest_by_date: dict[str, dict[str, Any]] = {}
    for row in history:
        row_date = _metric_date(row)
        if row_date is not None:
            latest_by_date[row_date.isoformat()] = row

    return {
        "power": {
            "labels": [label(row) for row in recent],
            "pv_w": [_series_value(row, "pv_w") for row in recent],
            "load_w": [_series_value(row, "load_w") for row in recent],
            "grid_w": [_series_value(row, "grid_w") for row in recent],
            "battery_net_w": [_series_value(row, "battery_net_w") for row in recent],
            "mode": [str(row.get("mode", "")) for row in recent],
        },
        "soc": {
            "labels": [label(row) for row in recent],
            "soc": [_series_value(row, "soc") for row in recent],
        },
        "daily": {
            "labels": [day.strftime("%m-%d") for day in dates],
            "pv_kwh": [_series_value(latest_by_date.get(day.isoformat(), {}), "pv_today_kwh") for day in dates],
            "charge_kwh": [_series_value(latest_by_date.get(day.isoformat(), {}), "charge_today_kwh") for day in dates],
            "discharge_kwh": [_series_value(latest_by_date.get(day.isoformat(), {}), "discharge_today_kwh") for day in dates],
            "load_kwh": [_series_value(latest_by_date.get(day.isoformat(), {}), "load_today_kwh") for day in dates],
            "grid_kwh": [_series_value(latest_by_date.get(day.isoformat(), {}), "grid_today_kwh") for day in dates],
        },
    }


def _minutes_since_midnight(value: dt.datetime) -> int:
    return value.hour * 60 + value.minute


def _same_time_baseline_rows(
    history: list[dict[str, Any]],
    now: dt.datetime,
    days: int = 7,
) -> list[dict[str, Any]]:
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)

    target_minute = _minutes_since_midnight(now)
    cutoff = now.date() - dt.timedelta(days=days)
    latest_by_day: dict[dt.date, tuple[dt.datetime, dict[str, Any]]] = {}
    for row in history:
        ts = _parse_metric_timestamp(row)
        if ts is None:
            continue
        row_date = ts.date()
        if row_date >= now.date() or row_date < cutoff:
            continue
        if _minutes_since_midnight(ts) > target_minute:
            continue
        current = latest_by_day.get(row_date)
        if current is None or ts > current[0]:
            latest_by_day[row_date] = (ts, row)

    return [row for _, row in sorted(latest_by_day.values(), key=lambda item: item[0])]


def _average_numeric(rows: list[dict[str, Any]], key: str) -> tuple[float | None, int]:
    values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
    if not values:
        return None, 0
    return sum(values) / len(values), len(values)


def _fmt_insight_value(value: float | None, unit: str) -> str:
    if value is None:
        return "--"
    if unit == "%":
        return _fmt_pct(value)
    return _fmt_kwh(value)


def _build_pace_item(
    live_metrics: dict[str, Any],
    baseline_rows: list[dict[str, Any]],
    key: str,
    label: str,
    unit: str,
    lower_is_better: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    current = live_metrics.get(key)
    if not isinstance(current, (int, float)):
        return {
            "key": key,
            "label": label,
            "level": "unknown",
            "title": "Not reported",
            "detail": f"{label} is not available in the current Growatt payload.",
            "current": None,
            "baseline": None,
            "delta": None,
            "delta_pct": None,
            "sample_count": 0,
        }

    baseline, sample_count = _average_numeric(baseline_rows, key)
    if baseline is None or sample_count < 2:
        return {
            "key": key,
            "label": label,
            "level": "unknown",
            "title": "Learning",
            "detail": f"Need at least two recent same-time snapshots for {label.lower()}.",
            "current": round(float(current), 2),
            "baseline": None,
            "delta": None,
            "delta_pct": None,
            "sample_count": sample_count,
        }

    delta = float(current) - baseline
    delta_pct = (delta / baseline * 100.0) if baseline else None
    if baseline == 0:
        if current == 0:
            level = "ok"
            title = "Normal"
        elif lower_is_better:
            level = "watch"
            title = "Above usual"
        else:
            level = "good"
            title = "Ahead"
    elif lower_is_better:
        ratio = float(current) / baseline
        if ratio <= 0.9:
            level = "good"
            title = "Lower than usual"
        elif ratio >= 1.15:
            level = "watch"
            title = "Above usual"
        else:
            level = "ok"
            title = "Normal"
    else:
        ratio = float(current) / baseline
        if ratio >= 1.1:
            level = "good"
            title = "Ahead"
        elif ratio <= 0.8:
            level = "watch"
            title = "Behind"
        else:
            level = "ok"
            title = "Normal"

    _now = now or dt.datetime.now()
    abs_pct = abs(delta_pct) if delta_pct is not None else None

    # Linear projection to end-of-day (6am–6pm window; skip if unit != kWh or >95% through day)
    _day_fraction = max(0.08, min(1.0, (_now.hour + _now.minute / 60 - 6.0) / 12.0))
    _can_project = unit == "kWh" and _day_fraction < 0.95 and float(current) >= 0

    def _proj() -> str:
        proj = float(current) / _day_fraction
        return f"~{proj:.1f} kWh"

    n = sample_count
    above_average = (
        f"{abs_pct:.0f}% above your {n}-day average"
        if abs_pct is not None
        else f"above your {n}-day zero average"
    )
    below_average = (
        f"{abs_pct:.0f}% below your {n}-day average"
        if abs_pct is not None
        else f"below your {n}-day zero average"
    )

    if key == "pv_today_kwh":
        if level == "good":
            detail = f"↑ {above_average}."
            if _can_project:
                detail += f" On track for {_proj()} today."
        elif level == "watch":
            detail = f"↓ {below_average}."
            if _can_project:
                detail += f" Could finish around {_proj()}."
        else:
            detail = f"Tracking your {n}-day average."
            if _can_project:
                detail += f" {_proj()} expected today."

    elif key == "load_today_kwh":
        if level == "watch":
            load_change = (
                f"{abs_pct:.0f}% above your typical load"
                if abs_pct is not None
                else "above your usual zero load baseline"
            )
            detail = f"↑ {load_change} — consumption running high."
        elif level == "good":
            load_change = (
                f"{abs_pct:.0f}% below your typical load"
                if abs_pct is not None
                else "below your usual zero load baseline"
            )
            detail = f"↓ {load_change} — efficient day so far."
        else:
            detail = f"Load tracking your {n}-day average."

    elif key == "grid_today_kwh":
        if float(current) < 0.05:
            detail = "Zero grid import — running entirely on solar."
        elif level == "watch":
            grid_change = (
                f"{abs_pct:.0f}% more grid than usual"
                if abs_pct is not None
                else "more grid than your usual zero baseline"
            )
            detail = f"↑ {grid_change} — solar may not be covering full load."
        elif level == "good":
            grid_change = (
                f"{abs_pct:.0f}% less grid than usual"
                if abs_pct is not None
                else "less grid than your usual zero baseline"
            )
            detail = f"↓ {grid_change} — solar covering well."
        else:
            detail = f"Grid usage tracking your {n}-day average."

    elif key == "soc":
        delta_abs = abs(round(float(current) - baseline))
        if level == "good":
            detail = f"Battery {delta_abs}% ahead of your {n}-day average for this time."
        elif level == "watch":
            detail = f"Battery {delta_abs}% below your typical position — worth watching tonight."
        else:
            detail = f"Battery tracking your {n}-day position."

    else:
        # Fallback for any future metrics
        if delta_pct is not None:
            arrow = "↑" if delta > 0 else "↓"
            detail = f"{arrow} {abs_pct:.0f}% vs your {n}-day average."
        else:
            detail = f"Changed from your {n}-day zero average."
    return {
        "key": key,
        "label": label,
        "level": level,
        "title": title,
        "detail": detail,
        "current": round(float(current), 2),
        "baseline": round(baseline, 2),
        "delta": round(delta, 2),
        "delta_pct": round(delta_pct, 1) if delta_pct is not None else None,
        "sample_count": sample_count,
    }


def build_dashboard_daily_insights(
    live_metrics: dict[str, Any],
    history: list[dict[str, Any]],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now()
    baseline_rows = _same_time_baseline_rows(history, now)
    items = [
        _build_pace_item(live_metrics, baseline_rows, "pv_today_kwh", "PV pace", "kWh", now=now),
        _build_pace_item(live_metrics, baseline_rows, "load_today_kwh", "Load pace", "kWh", lower_is_better=True, now=now),
        _build_pace_item(live_metrics, baseline_rows, "grid_today_kwh", "Grid pace", "kWh", lower_is_better=True, now=now),
        _build_pace_item(live_metrics, baseline_rows, "soc", "SOC position", "%", now=now),
    ]
    levels = {str(item.get("level")) for item in items}
    if "watch" in levels:
        status = "watch"
        title = "Watch today"
    elif levels == {"unknown"}:
        status = "unknown"
        title = "Learning"
    elif "good" in levels and "unknown" not in levels:
        status = "good"
        title = "Better than usual"
    else:
        status = "ok"
        title = "Normal day"

    return {
        "status": status,
        "title": title,
        "sample_days": len(baseline_rows),
        "items": items,
    }


def _average_recent_discharge_w() -> float | None:
    history = read_discharge_rate_history()
    rates = [float(r["rate_w"]) for r in history if isinstance(r.get("rate_w"), (int, float))]
    if len(rates) < 2:
        return None
    return sum(rates) / len(rates)


def compute_tonight_safe(
    projected_sunrise_soc: float | None,
    hours_to_sunset: float | None,
    floor_soc: float = 35.0,
    comfortable_soc: float = 45.0,
    cutoff_offset_minutes: float = 30.0,
) -> dict[str, Any]:
    """Return tonight-safe headline data.

    Show only after the evening cutoff (sunset minus cutoff_offset_minutes).
    A negative hours_to_sunset means sunset already passed.
    """
    cutoff_hours = cutoff_offset_minutes / 60.0
    past_cutoff = hours_to_sunset is None or hours_to_sunset <= cutoff_hours

    if not past_cutoff:
        return {"show": False}
    if projected_sunrise_soc is None:
        return {"show": False}

    if projected_sunrise_soc < floor_soc:
        return {
            "show": True,
            "headline": "Topup needed tonight",
            "subtext": "Battery may not last until morning.",
            "reason": f"Projected sunrise: {projected_sunrise_soc:.0f}%, below {floor_soc:.0f}% floor.",
            "score": None,
            "level": "danger",
        }
    if projected_sunrise_soc >= comfortable_soc:
        return {
            "show": True,
            "headline": "Tonight safe: 100%",
            "subtext": "",
            "score": 100,
            "level": "ok",
        }
    score = int(projected_sunrise_soc)
    return {
        "show": True,
        "headline": f"Tonight safe: {score}%",
        "subtext": "",
        "score": score,
        "level": "watch",
    }


def build_tonight_risk(
    live_metrics: dict[str, Any],
    battery_capacity_wh: float,
    battery_bms_cutoff_soc: float,
    hours_to_sunrise: float | None,
    battery_charge_rate_w: float,
    auto_topup_target_soc: float = 0.0,
    auto_topup_solar_skip_min_margin_minutes: float = 0.0,
    projection_start_soc: float | None = None,
    projection_hours: float | None = None,
    projection_basis: str = "",
) -> dict[str, Any]:
    soc = live_metrics.get("soc")
    if not isinstance(soc, (int, float)):
        return {
            "level": "unknown",
            "title": "Unknown",
            "detail": "SOC is unavailable.",
            "projected_sunrise_soc": None,
            "load_w": None,
            "topup_minutes": None,
        }
    if not hours_to_sunrise or hours_to_sunrise <= 0:
        return {
            "level": "unknown",
            "title": "Unknown",
            "detail": "Sunrise estimate is unavailable.",
            "projected_sunrise_soc": None,
            "load_w": None,
            "topup_minutes": None,
        }
    if battery_capacity_wh <= 0:
        return {
            "level": "unknown",
            "title": "Unknown",
            "detail": "BATTERY_CAPACITY_WH is not configured.",
            "projected_sunrise_soc": None,
            "load_w": None,
            "topup_minutes": None,
        }

    load_w = _average_recent_discharge_w()
    source = "recent average"
    if load_w is None:
        battery_net_w = live_metrics.get("battery_net_w")
        if isinstance(battery_net_w, (int, float)) and battery_net_w > 0:
            load_w = float(battery_net_w)
            source = "live discharge"
        elif isinstance(live_metrics.get("load_w"), (int, float)):
            load_w = float(live_metrics["load_w"])
            source = "live load"

    if not load_w or load_w <= 0:
        return {
            "level": "unknown",
            "title": "Unknown",
            "detail": "Battery is not currently discharging and no recent load average is available.",
            "projected_sunrise_soc": None,
            "load_w": None,
            "topup_minutes": None,
        }

    start_soc = soc
    duration_hours = hours_to_sunrise
    basis = projection_basis
    if (
        isinstance(projection_start_soc, (int, float))
        and isinstance(projection_hours, (int, float))
        and projection_hours > 0
    ):
        start_soc = float(projection_start_soc)
        duration_hours = float(projection_hours)
        basis = basis or "projected sunset SOC"

    soc_drop = (load_w * duration_hours / battery_capacity_wh) * 100.0
    projected_soc = max(0.0, start_soc - soc_drop)
    target_soc = max(battery_bms_cutoff_soc, auto_topup_target_soc)
    margin = projected_soc - target_soc
    margin_hours = duration_hours + max(0.0, auto_topup_solar_skip_min_margin_minutes) / 60.0
    topup_minutes = None
    if battery_charge_rate_w > 0:
        topup_minutes = estimate_topup_for_sunrise(
            start_soc,
            load_w,
            battery_capacity_wh,
            target_soc,
            battery_charge_rate_w,
            margin_hours,
        )
        if topup_minutes is not None:
            topup_minutes = max(0.0, round(topup_minutes, 1))

    if margin < 0:
        level = "high"
        title = "High risk"
    elif margin < 8:
        level = "watch"
        title = "Watch"
    else:
        level = "comfortable"
        title = "Comfortable"

    detail_parts = [
        f"Projected sunrise SOC {projected_soc:.0f}%",
        f"target {target_soc:g}%",
        f"load {_fmt_w(load_w)} ({source})",
    ]
    if basis:
        detail_parts.append(f"start {start_soc:.0f}% ({basis})")
    if topup_minutes and topup_minutes > 0:
        detail_parts.append(f"topup {format_duration_minutes(topup_minutes)}")
    elif topup_minutes == 0:
        detail_parts.append("topup not needed")

    return {
        "level": level,
        "title": title,
        "detail": "; ".join(detail_parts),
        "projected_sunrise_soc": round(projected_soc, 1),
        "target_soc": round(target_soc, 1),
        "margin_soc": round(margin, 1),
        "hours_to_sunrise": round(hours_to_sunrise, 2),
        "projection_hours": round(duration_hours, 2),
        "projection_start_soc": round(start_soc, 1),
        "projection_basis": basis,
        "load_w": round(load_w, 1),
        "load_source": source,
        "topup_minutes": topup_minutes,
    }


def build_dashboard_energy_reconciliation(live_metrics: dict[str, Any]) -> dict[str, Any]:
    """Compare daily source counters against demand counters when available."""
    pv_kwh = _positive_metric(live_metrics.get("pv_today_kwh"))
    grid_kwh = _positive_metric(live_metrics.get("grid_today_kwh"))
    load_kwh = _positive_metric(live_metrics.get("load_today_kwh"))
    charge_kwh = _positive_metric(live_metrics.get("charge_today_kwh"))
    discharge_kwh = _positive_metric(live_metrics.get("discharge_today_kwh"))
    required = {
        "pv_today_kwh": pv_kwh,
        "grid_today_kwh": grid_kwh,
        "load_today_kwh": load_kwh,
        "charge_today_kwh": charge_kwh,
        "discharge_today_kwh": discharge_kwh,
    }
    missing = [key for key, value in required.items() if value is None]
    if missing:
        return {"status": "unavailable", "missing": missing}

    supply_total = (pv_kwh or 0.0) + (grid_kwh or 0.0) + (discharge_kwh or 0.0)
    demand_total = (load_kwh or 0.0) + (charge_kwh or 0.0)
    delta = supply_total - demand_total
    baseline = max(supply_total, demand_total, 0.0)
    delta_pct = abs(delta) / baseline * 100.0 if baseline > 0 else 0.0
    tolerance_kwh = max(1.0, baseline * 0.25)
    status = "ok" if abs(delta) <= tolerance_kwh else "watch"
    return {
        "status": status,
        "supply_total_kwh": round(supply_total, 2),
        "demand_total_kwh": round(demand_total, 2),
        "delta_kwh": round(delta, 2),
        "delta_pct": round(delta_pct, 1),
        "tolerance_kwh": round(tolerance_kwh, 2),
        "missing": [],
    }


def build_dashboard_data_quality(
    live_metrics: dict[str, Any],
    sources: dict[str, str],
) -> dict[str, Any]:
    required_metrics = [
        ("SOC", "soc"),
        ("mode", "mode"),
        ("PV now", "pv_w"),
        ("load now", "load_w"),
        ("battery flow", "battery_net_w"),
        ("PV today", "pv_today_kwh"),
        ("load today", "load_today_kwh"),
        ("grid import today", "grid_today_kwh"),
        ("battery charge today", "charge_today_kwh"),
    ]
    missing = [
        label
        for label, key in required_metrics
        if live_metrics.get(key) is None or live_metrics.get(key) == ""
    ]
    score = round(((len(required_metrics) - len(missing)) / len(required_metrics)) * 100)
    if score >= 90:
        level = "good"
        title = "Good"
    elif score >= 65:
        level = "watch"
        title = "Watch"
    else:
        level = "poor"
        title = "Poor"

    items: list[str] = []
    if missing:
        items.append("Missing: " + ", ".join(missing) + ".")
    if live_metrics.get("grid_source") == "estimated":
        items.append("Live grid power is estimated from load + charge - PV.")
    if str(sources.get("pv_w", "")).startswith("channel-sum:"):
        items.append("PV power is using summed PV channel values.")
    if str(sources.get("pv_today_kwh", "")).startswith("channel-sum:"):
        items.append("PV energy today is using summed PV channel values.")
    reconciliation = build_dashboard_energy_reconciliation(live_metrics)
    if reconciliation.get("status") == "watch":
        if level == "good":
            level = "watch"
            title = "Watch"
        items.append(
            "Daily energy counters do not reconcile: "
            f"supply {reconciliation['supply_total_kwh']:g} kWh vs "
            f"demand {reconciliation['demand_total_kwh']:g} kWh "
            f"(delta {reconciliation['delta_kwh']:+g} kWh)."
        )

    if not items:
        items.append("All key dashboard metrics are present.")

    return {
        "level": level,
        "title": title,
        "score": score,
        "missing": missing,
        "items": items,
        "reconciliation": reconciliation,
    }


def _positive_metric(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    return None


def _metric_share(value: float | None, total: float | None) -> float | None:
    if value is None or total is None or total <= 0:
        return None
    return max(0.0, min(100.0, value / total * 100.0))


def _round_optional(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def build_dashboard_daily_mix(live_metrics: dict[str, Any]) -> dict[str, Any]:
    """Summarize today's energy into source, demand, and battery mix values."""
    pv_kwh = _positive_metric(live_metrics.get("pv_today_kwh"))
    grid_kwh = _positive_metric(live_metrics.get("grid_today_kwh"))
    load_kwh = _positive_metric(live_metrics.get("load_today_kwh"))
    charge_kwh = _positive_metric(live_metrics.get("charge_today_kwh"))
    discharge_kwh = _positive_metric(live_metrics.get("discharge_today_kwh"))

    supply_parts = [value for value in (pv_kwh, grid_kwh) if value is not None]
    demand_parts = [value for value in (load_kwh, charge_kwh) if value is not None]
    battery_parts = [value for value in (charge_kwh, discharge_kwh) if value is not None]

    supply_total = sum(supply_parts) if supply_parts else None
    demand_total = sum(demand_parts) if demand_parts else None
    battery_activity_total = sum(battery_parts) if battery_parts else None
    battery_net = charge_kwh - discharge_kwh if charge_kwh is not None and discharge_kwh is not None else None
    if battery_net is None:
        battery_net_title = "Battery net unknown"
    elif battery_net > 0.05:
        battery_net_title = "Net stored"
    elif battery_net < -0.05:
        battery_net_title = "Net supplied"
    else:
        battery_net_title = "Battery balanced"

    return {
        "supply_total_kwh": _round_optional(supply_total),
        "demand_total_kwh": _round_optional(demand_total),
        "battery_activity_total_kwh": _round_optional(battery_activity_total),
        "battery_net_kwh": _round_optional(battery_net),
        "battery_net_title": battery_net_title,
        "pv_supply_pct": _round_optional(_metric_share(pv_kwh, supply_total), 1),
        "grid_supply_pct": _round_optional(_metric_share(grid_kwh, supply_total), 1),
        "load_demand_pct": _round_optional(_metric_share(load_kwh, demand_total), 1),
        "charge_demand_pct": _round_optional(_metric_share(charge_kwh, demand_total), 1),
        "charge_battery_pct": _round_optional(_metric_share(charge_kwh, battery_activity_total), 1),
        "discharge_battery_pct": _round_optional(_metric_share(discharge_kwh, battery_activity_total), 1),
    }


def _numeric_metric(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _clamp_pct(value: float) -> float:
    return max(0.0, min(100.0, value))


def _project_sunset_soc(
    live_metrics: dict[str, Any],
    battery_capacity_wh: float,
    hours_to_sunset: float | None,
) -> float | None:
    soc = _numeric_metric(live_metrics.get("soc"))
    battery_net_w = _numeric_metric(live_metrics.get("battery_net_w"))
    if (
        soc is None
        or battery_net_w is None
        or battery_capacity_wh <= 0
        or hours_to_sunset is None
        or hours_to_sunset <= 0
    ):
        return None
    return _clamp_pct(soc - (battery_net_w * hours_to_sunset / battery_capacity_wh) * 100.0)


def _should_use_projected_sunset_start(
    now: dt.datetime,
    hours_to_sunset: float | None,
    hours_to_sunrise: float | None,
) -> bool:
    if hours_to_sunset is None or hours_to_sunrise is None:
        return False
    if hours_to_sunset <= 0 or hours_to_sunrise <= hours_to_sunset:
        return False
    return now.hour >= 12 or hours_to_sunset <= 6


def _dashboard_greeting(now: dt.datetime) -> str:
    if now.hour < 12:
        return "Good morning"
    if now.hour < 17:
        return "Good afternoon"
    return "Good evening"


def _active_grid_draw(grid_w: float | None) -> bool:
    return grid_w is not None and grid_w >= 20


def build_dashboard_home_status(
    live_metrics: dict[str, Any],
    mode: str,
    battery_flow_dir: str,
    tonight_risk: dict[str, Any],
    next_action: dict[str, Any],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Build the first-screen household status sentence."""
    now = now or dt.datetime.now()
    pv_w = _numeric_metric(live_metrics.get("pv_w"))
    load_w = _numeric_metric(live_metrics.get("load_w"))
    grid_w = _numeric_metric(live_metrics.get("grid_w"))
    soc = _numeric_metric(live_metrics.get("soc"))
    tonight_level = str(tonight_risk.get("level", "")).lower()
    grid_active = _active_grid_draw(grid_w)
    solar_covering = pv_w is not None and load_w is not None and pv_w >= load_w and not grid_active
    has_solar = pv_w is not None and pv_w > 0

    if solar_covering:
        headline = "Solar is covering the house"
        if battery_flow_dir == "charging":
            headline += " and charging the battery"
    elif has_solar and load_w is not None and grid_active:
        headline = "Solar is helping, but the grid is assisting"
    elif grid_active:
        headline = "Grid power is supporting the house"
    elif battery_flow_dir == "discharging":
        headline = "Battery is carrying the house"
    elif "utility" in mode.lower():
        headline = "Utility mode is active"
    else:
        headline = "Home energy is stable"

    if soc is None and pv_w is None and load_w is None and grid_w is None:
        level = "unknown"
    elif isinstance(soc, (int, float)) and soc < 25 and battery_flow_dir == "discharging" and not has_solar:
        level = "high"
    elif grid_active or "utility" in mode.lower() or (
        isinstance(soc, (int, float)) and soc < 50 and battery_flow_dir == "discharging"
    ):
        level = "watch"
    else:
        level = "comfortable"

    battery_text = _fmt_pct(soc)
    if battery_flow_dir == "charging":
        battery_context = f"Battery {battery_text} and charging"
    elif battery_flow_dir == "discharging":
        battery_context = f"Battery {battery_text} and discharging"
    else:
        battery_context = f"Battery {battery_text}"

    next_relative = str(next_action.get("relative") or "none")
    next_title = str(next_action.get("title") or "No upcoming automation")
    tonight_title = str(tonight_risk.get("title") or "Unknown")
    power_bits = []
    if pv_w is not None:
        power_bits.append(f"PV {_fmt_w(pv_w)}")
    if load_w is not None:
        power_bits.append(f"house {_fmt_w(load_w)}")
    if grid_w is not None:
        power_bits.append(f"grid {_fmt_w(grid_w)}")
    power_context = ", ".join(power_bits) if power_bits else "Live power values are still loading"
    detail = f"{battery_context}. {power_context}."
    now_labels = {
        "comfortable": "Healthy",
        "watch": "Watch",
        "high": "Urgent",
        "unknown": "Learning",
    }

    return {
        "greeting": _dashboard_greeting(now),
        "headline": headline,
        "detail": detail,
        "level": level,
        "now_label": now_labels.get(level, "Learning"),
        "tonight_level": tonight_level or "unknown",
        "tonight_title": tonight_title,
        "next_action": f"{next_relative} - {next_title}",
    }


def build_dashboard_energy_outlook(
    live_metrics: dict[str, Any],
    tonight_risk: dict[str, Any],
    pv_forecast: dict[str, Any] | None,
    threshold_decision: Any,
    hours_to_sunset: float | None,
    hours_to_sunrise: float | None,
    battery_capacity_wh: float,
    battery_charge_rate_w: float,
) -> dict[str, Any]:
    """Summarize predictive energy outcomes using available local signals."""
    projected_sunset_soc = _project_sunset_soc(live_metrics, battery_capacity_wh, hours_to_sunset)

    projected_sunrise_soc = tonight_risk.get("projected_sunrise_soc")
    if not isinstance(projected_sunrise_soc, (int, float)):
        projected_sunrise_soc = None

    topup_minutes = tonight_risk.get("topup_minutes")
    expected_grid_kwh: float | None = None
    if isinstance(topup_minutes, (int, float)) and topup_minutes > 0 and battery_charge_rate_w > 0:
        expected_grid_kwh = round((float(topup_minutes) / 60.0) * battery_charge_rate_w / 1000.0, 1)
    elif topup_minutes == 0:
        expected_grid_kwh = 0.0
    load_w = tonight_risk.get("load_w")
    load_source = str(tonight_risk.get("load_source") or "").strip()
    projection_hours = tonight_risk.get("projection_hours")
    duration_for_basis = projection_hours if isinstance(projection_hours, (int, float)) and projection_hours > 0 else hours_to_sunrise
    sunrise_duration = format_duration_minutes(duration_for_basis * 60) if duration_for_basis and duration_for_basis > 0 else ""
    projection_basis = str(tonight_risk.get("projection_basis") or "").strip()
    projection_start_soc = tonight_risk.get("projection_start_soc")
    if isinstance(load_w, (int, float)) and load_w > 0:
        basis_source = f" ({load_source})" if load_source else ""
        duration_context = f" for {sunrise_duration}" if sunrise_duration else ""
        start_context = ""
        if projection_basis and isinstance(projection_start_soc, (int, float)):
            start_context = f" from {_fmt_pct(projection_start_soc)} {projection_basis}"
        sunrise_basis = f"{_fmt_w(load_w)} overnight load{basis_source}{duration_context}{start_context}"
    elif hours_to_sunrise is None or hours_to_sunrise <= 0:
        sunrise_basis = "Sunrise time unavailable"
    else:
        sunrise_basis = "Waiting for discharge or load history"
    target_soc = tonight_risk.get("target_soc")
    if not isinstance(target_soc, (int, float)):
        target_soc = None
    if projected_sunrise_soc is None:
        sunrise_note = "Projection will appear after SOC, sunrise, capacity, and load signals are available."
    elif projected_sunrise_soc <= 1:
        sunrise_note = "Stress estimate from carrying that load until sunrise."
    elif target_soc is not None:
        margin = float(projected_sunrise_soc) - float(target_soc)
        if margin < 0:
            sunrise_note = f"Below the {_fmt_pct(target_soc)} reserve target."
        else:
            sunrise_note = f"{_fmt_pct(margin)} above the reserve target."
    else:
        sunrise_note = "Projection improves as overnight load history accumulates."

    tomorrow_kwh = pv_forecast.get("tomorrow_kwh") if pv_forecast else None
    today_remaining_kwh = pv_forecast.get("today_remaining_kwh") if pv_forecast else None
    weather_category = str(getattr(threshold_decision, "weather_category", "") or "not configured")
    cloud = getattr(threshold_decision, "cloud_cover", None)
    rain = getattr(threshold_decision, "precipitation_mm", None)
    weather_detail = weather_category
    detail_bits: list[str] = []
    if isinstance(cloud, (int, float)):
        detail_bits.append(f"cloud {cloud:g}%")
    if isinstance(rain, (int, float)):
        detail_bits.append(f"rain {rain:g}mm")
    if detail_bits:
        weather_detail += " (" + ", ".join(detail_bits) + ")"

    signals = [
        pv_forecast is not None,
        projected_sunrise_soc is not None,
        projected_sunset_soc is not None,
        hours_to_sunrise is not None,
    ]
    signal_count = sum(1 for value in signals if value)
    if signal_count >= 3:
        confidence = "High"
    elif signal_count >= 2:
        confidence = "Medium"
    else:
        confidence = "Learning"

    return {
        "today_remaining_kwh": today_remaining_kwh if isinstance(today_remaining_kwh, (int, float)) else None,
        "tomorrow_kwh": tomorrow_kwh if isinstance(tomorrow_kwh, (int, float)) else None,
        "projected_sunset_soc": round(projected_sunset_soc, 1) if projected_sunset_soc is not None else None,
        "projected_sunrise_soc": round(float(projected_sunrise_soc), 1) if projected_sunrise_soc is not None else None,
        "sunrise_basis": sunrise_basis,
        "sunrise_note": sunrise_note,
        "reserve_target_soc": round(float(target_soc), 1) if target_soc is not None else None,
        "topup_minutes": round(float(topup_minutes), 1) if isinstance(topup_minutes, (int, float)) else None,
        "expected_grid_kwh": expected_grid_kwh,
        "weather": weather_detail,
        "confidence": confidence,
    }


def build_dashboard_assistant_summary(
    home_status: dict[str, Any],
    daily_insights: dict[str, Any],
    energy_outlook: dict[str, Any],
    recommendations: list[dict[str, Any]],
    data_quality: dict[str, Any],
) -> dict[str, str]:
    """Generate a concise natural-language dashboard summary."""
    quality_level = str(data_quality.get("level", "")).lower()
    if quality_level == "poor":
        title = "Telemetry needs attention"
        lead = "Some key Growatt values are missing, so this view is lower confidence."
    else:
        title = str(home_status.get("headline") or "Home energy is stable")
        lead = str(home_status.get("detail") or "Live power values are stable.")

    sunrise_soc = energy_outlook.get("projected_sunrise_soc")
    topup_minutes = energy_outlook.get("topup_minutes")
    if isinstance(sunrise_soc, (int, float)):
        if isinstance(topup_minutes, (int, float)) and topup_minutes > 0:
            outlook_text = (
                f"Tonight needs a {format_duration_minutes(topup_minutes)} top-up to protect the sunrise reserve."
            )
        else:
            outlook_text = f"Tonight is on track: projected sunrise reserve is {sunrise_soc:.0f}%."
    else:
        outlook_text = "Tonight projection is still learning because one or more reserve signals are missing."

    lead_text = lead.strip().rstrip(".")

    return {
        "title": title,
        "text": f"{lead_text}. {outlook_text}" if lead_text else outlook_text,
    }


def _build_dashboard_recommendations_legacy(
    live_metrics: dict,
    soc_health: str,
    battery_flow_dir: str,
    tonight_risk: dict,
    daily_insights: dict,
    pv_power_display: str,
    grid_status_text: str,
) -> list[dict]:
    """Generate rule-based recommendations for the dashboard."""
    recs: list[dict] = []
    soc = live_metrics.get("soc_pct") or 0
    grid_w = live_metrics.get("grid_w") or 0
    pv_w = live_metrics.get("pv_w") or 0
    load_w = live_metrics.get("load_w") or 0
    tonight_level = str(tonight_risk.get("level", "")).lower()

    if soc >= 95 and pv_w > 0:
        recs.append({"icon": "✓", "text": f"Battery full — all {pv_power_display} solar going directly to load."})
    elif battery_flow_dir == "charging" and pv_w > load_w:
        surplus = _fmt_w(pv_w - load_w)
        recs.append({"icon": "↑", "text": f"PV surplus of {surplus} charging battery while covering load."})

    if grid_w < 20 and pv_w > 0:
        recs.append({"icon": "✓", "text": "Running entirely on solar right now. Zero grid draw."})
    elif grid_w > 500:
        recs.append({"icon": "↗", "text": f"Drawing {_fmt_w(grid_w)} from grid — consider shifting heavy loads to solar peak hours."})

    if tonight_level == "ok":
        recs.append({"icon": "✓", "text": "Battery on track for sunrise. No top-up needed tonight."})
    elif tonight_level in ("warn", "high"):
        topup = tonight_risk.get("topup_window_display") or tonight_risk.get("detail", "")
        recs.append({"icon": "⚠", "text": f"Tonight risk elevated — {str(topup)[:80] if topup else 'consider scheduling a top-up.'}"})

    pv_pace = next(
        (i for i in (daily_insights.get("items") or []) if isinstance(i, dict) and "PV" in str(i.get("label", "")).upper()),
        None,
    )
    if pv_pace:
        level = str(pv_pace.get("level", "")).lower()
        detail = str(pv_pace.get("detail", ""))
        if level == "ok" and detail:
            recs.append({"icon": "☀", "text": detail})
        elif level == "warn" and detail:
            recs.append({"icon": "↓", "text": detail})

    if not recs:
        recs.append({"icon": "✓", "text": "System operating normally. No actions required."})

    return recs[:5]


def build_dashboard_recommendations(
    live_metrics: dict,
    soc_health: str,
    battery_flow_dir: str,
    tonight_risk: dict,
    daily_insights: dict,
    pv_power_display: str,
    grid_status_text: str,
    energy_outlook: dict[str, Any] | None = None,
    threshold_decision: Any | None = None,
) -> list[dict]:
    """Generate ranked recommendations with reason and impact context."""
    _ = soc_health
    recs: list[dict[str, Any]] = []
    soc = _numeric_metric(live_metrics.get("soc")) or 0
    grid_w = _numeric_metric(live_metrics.get("grid_w")) or 0
    pv_w = _numeric_metric(live_metrics.get("pv_w")) or 0
    load_w = _numeric_metric(live_metrics.get("load_w")) or 0
    tonight_level = str(tonight_risk.get("level", "")).lower()
    energy_outlook = energy_outlook or {}

    if soc >= 95 and pv_w > 0:
        recs.append({
            "icon": "OK",
            "level": "good",
            "title": "Use solar while it is abundant",
            "text": f"Battery is full, so the current {pv_power_display} solar is best used by flexible house loads.",
            "meta": "Best window: now",
        })
    elif battery_flow_dir == "charging" and pv_w > load_w:
        surplus = _fmt_w(pv_w - load_w)
        recs.append({
            "icon": "PV",
            "level": "good",
            "title": "Solar surplus available",
            "text": f"PV surplus of {surplus} is charging the battery while covering load.",
            "meta": "Good time for shiftable loads",
        })

    if grid_w < 20 and pv_w > 0:
        recs.append({
            "icon": "OK",
            "level": "good",
            "title": "No grid action needed",
            "text": "Running entirely on solar right now with zero meaningful grid draw.",
            "meta": grid_status_text,
        })
    elif grid_w > 500:
        recs.append({
            "icon": "LOAD",
            "level": "watch",
            "title": "Shift heavy loads if possible",
            "text": f"Drawing {_fmt_w(grid_w)} from grid; move laundry, pumping, or charging to the next strong solar window.",
            "meta": "Impact: lower grid import",
        })

    if tonight_level in {"comfortable", "ok"}:
        recs.append({
            "icon": "OK",
            "level": "good",
            "title": "Skip overnight top-up",
            "text": "Battery is on track for sunrise, so no grid top-up is needed tonight.",
            "meta": str(tonight_risk.get("detail", "")),
        })
    elif tonight_level in {"watch", "warn", "high"}:
        topup_minutes = tonight_risk.get("topup_minutes")
        projected_soc = tonight_risk.get("projected_sunrise_soc")
        target_soc = tonight_risk.get("target_soc")
        sunrise_basis = str(energy_outlook.get("sunrise_basis") or tonight_risk.get("detail") or "").strip()
        topup_display = (
            format_duration_minutes(float(topup_minutes))
            if isinstance(topup_minutes, (int, float)) and topup_minutes > 0
            else ""
        )
        if topup_display:
            title = f"Schedule {topup_display} top-up tonight"
            meta = "Top-up window: tonight"
        else:
            title = "Review overnight reserve tonight"
            meta = "Reserve review needed"
        projection_bits: list[str] = []
        if isinstance(projected_soc, (int, float)):
            projection_bits.append(f"Sunrise reserve stress estimate is {projected_soc:.0f}%")
        if isinstance(target_soc, (int, float)):
            projection_bits.append(f"target is {target_soc:g}%")
        if sunrise_basis:
            projection_bits.append(sunrise_basis)
        recs.append({
            "icon": "RISK",
            "level": "high" if tonight_level == "high" else "watch",
            "title": title,
            "text": "; ".join(projection_bits) if projection_bits else "Tonight risk is elevated; protect the sunrise reserve.",
            "meta": meta,
        })

    tomorrow_kwh = energy_outlook.get("tomorrow_kwh")
    if isinstance(tomorrow_kwh, (int, float)):
        if tomorrow_kwh >= 25:
            recs.append({
                "icon": "SUN",
                "level": "good",
                "title": "Tomorrow can run leaner",
                "text": f"Tomorrow is forecast at {_fmt_kwh(tomorrow_kwh)}; reserve can stay conservative only if outages are expected.",
                "meta": "Forecast-informed reserve",
            })
        elif tomorrow_kwh <= 12:
            recs.append({
                "icon": "WX",
                "level": "watch",
                "title": "Raise tomorrow's reserve",
                "text": f"Tomorrow's PV forecast is only {_fmt_kwh(tomorrow_kwh)}; consider a higher reserve before long outages.",
                "meta": "Cloudy-day protection",
            })

    if threshold_decision is not None and str(getattr(threshold_decision, "weather_category", "")) in {"rainy/cloudy", "unavailable"}:
        recs.append({
            "icon": "WX",
            "level": "watch",
            "title": "Weather is driving reserve decisions",
            "text": str(getattr(threshold_decision, "reason", "Weather is increasing reserve caution."))[:120],
            "meta": "Automation threshold context",
        })

    pv_pace = next(
        (i for i in (daily_insights.get("items") or []) if isinstance(i, dict) and "PV" in str(i.get("label", "")).upper()),
        None,
    )
    if pv_pace:
        level = str(pv_pace.get("level", "")).lower()
        detail = str(pv_pace.get("detail", ""))
        if level == "ok" and detail:
            recs.append({
                "icon": "PV",
                "level": "good",
                "title": "Solar pace is normal",
                "text": detail,
                "meta": "Same-time comparison",
            })
        elif level == "warn" and detail:
            recs.append({
                "icon": "PV",
                "level": "watch",
                "title": "Solar pace is behind",
                "text": detail,
                "meta": "Same-time comparison",
            })

    if not recs:
        recs.append({
            "icon": "OK",
            "level": "good",
            "title": "No action required",
            "text": "System is operating normally. Keep the current automation plan.",
            "meta": "Assistant check",
        })

    priority = {"high": 0, "watch": 1, "good": 2}
    recs.sort(key=lambda item: priority.get(str(item.get("level", "good")), 2))
    return recs[:6]


def build_dashboard_next_action(
    schedule: dict[str, Any],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now()
    if now.tzinfo is not None:
        now_naive = now.astimezone().replace(tzinfo=None)
    else:
        now_naive = now

    runs = next_scheduled_runs(schedule, now=now_naive, limit=1)
    if not runs:
        return {
            "status": "none",
            "title": "No upcoming jobs",
            "detail": "No scheduled jobs found.",
            "run_at": None,
            "minutes_until": None,
            "job_id": "",
            "name": "",
            "command": "",
            "relative": "none",
        }

    run_at, job = runs[0]
    minutes_until = max(0, int(round((run_at - now_naive).total_seconds() / 60)))
    relative = "now" if minutes_until <= 0 else f"in {format_duration_minutes(minutes_until)}"
    command = " ".join(schedule_job_tokens(job))
    job_id = str(job.get("id", ""))
    name = str(job.get("name", "")).strip() or job_id or command
    return {
        "status": "scheduled",
        "title": name,
        "detail": f"{command} at {run_at.strftime('%Y-%m-%d %H:%M')} ({relative}).",
        "run_at": run_at.isoformat(timespec="minutes"),
        "minutes_until": minutes_until,
        "job_id": job_id,
        "name": name,
        "command": command,
        "relative": relative,
    }


def _status_badge_class(level: str) -> str:
    if level in {"comfortable", "good", "ok"}:
        return "badge-ok"
    if level in {"watch", "unknown"}:
        return "badge-warn"
    return "badge-fail"


def build_dashboard_data_payload(
    status: dict[str, Any],
    schedule: dict[str, Any],
    overrides: dict[str, Any],
    threshold_decision: Any,
    stale_after_minutes: float = 30,
    battery_capacity_wh: float = 0.0,
    battery_bms_cutoff_soc: float = 25.0,
    hours_to_sunrise: float | None = None,
    battery_charge_rate_w: float = 0.0,
    auto_topup_target_soc: float = 0.0,
    auto_topup_solar_skip_min_margin_minutes: float = 0.0,
    metrics_history: list[dict[str, Any]] | None = None,
    hours_to_sunset: float | None = None,
    pv_forecast: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now().astimezone()
    live_metrics = extract_dashboard_metrics(status, now=now)
    metric_history = _history_with_live(metrics_history or [], live_metrics)
    today_override = today_schedule_override(overrides, now.date())
    today_jobs = _today_job_rows(schedule, today_override, now.date())
    schedule_timeline = build_dashboard_schedule_timeline(schedule, today_override, now=now)
    next_runs = next_scheduled_runs(schedule, now=now.replace(tzinfo=None), limit=8)
    pause_state = read_pause_state()
    alert_state = read_battery_alert_state()
    cloud_state = read_growatt_cloud_failure_state()
    pvoutput_state = read_pvoutput_state()
    sources = extract_dashboard_metric_sources(status)
    data_quality = build_dashboard_data_quality(live_metrics, sources)
    daily_mix = build_dashboard_daily_mix(live_metrics)
    next_action = build_dashboard_next_action(schedule, now=now)
    daily_insights = build_dashboard_daily_insights(live_metrics, metric_history, now=now)
    projected_sunset_soc = _project_sunset_soc(live_metrics, battery_capacity_wh, hours_to_sunset)
    overnight_hours = None
    risk_start_soc = None
    risk_basis = ""
    if projected_sunset_soc is not None and _should_use_projected_sunset_start(now, hours_to_sunset, hours_to_sunrise):
        overnight_hours = float(hours_to_sunrise or 0) - float(hours_to_sunset or 0)
        risk_start_soc = projected_sunset_soc
        risk_basis = "projected sunset SOC"
    risk = build_tonight_risk(
        live_metrics,
        battery_capacity_wh,
        battery_bms_cutoff_soc,
        hours_to_sunrise,
        battery_charge_rate_w,
        auto_topup_target_soc,
        auto_topup_solar_skip_min_margin_minutes,
        projection_start_soc=risk_start_soc,
        projection_hours=overnight_hours,
        projection_basis=risk_basis,
    )
    battery_net_w = _numeric_metric(live_metrics.get("battery_net_w")) or 0.0
    battery_flow_dir = "discharging" if battery_net_w > 0 else ("charging" if battery_net_w < 0 else "standby")
    grid_w = _numeric_metric(live_metrics.get("grid_w")) or 0.0
    if grid_w < 20:
        grid_status_text = "Solar covering entire load" if (_numeric_metric(live_metrics.get("pv_w")) or 0) > 0 else "No meaningful grid draw"
    elif grid_w > 0:
        grid_status_text = f"Drawing {_fmt_w(grid_w)} from grid"
    else:
        grid_status_text = f"Exporting {_fmt_w(abs(grid_w))} to grid"
    energy_outlook = build_dashboard_energy_outlook(
        live_metrics,
        risk,
        pv_forecast,
        threshold_decision,
        hours_to_sunset,
        hours_to_sunrise,
        battery_capacity_wh,
        battery_charge_rate_w,
    )
    home_status = build_dashboard_home_status(
        live_metrics,
        str(live_metrics.get("mode") or ""),
        battery_flow_dir,
        risk,
        next_action,
        now=now,
    )
    recommendations = build_dashboard_recommendations(
        live_metrics,
        "",
        battery_flow_dir,
        risk,
        daily_insights,
        _fmt_w(live_metrics.get("pv_w")),
        grid_status_text,
        energy_outlook=energy_outlook,
        threshold_decision=threshold_decision,
    )
    assistant_summary = build_dashboard_assistant_summary(
        home_status,
        daily_insights,
        energy_outlook,
        recommendations,
        data_quality,
    )

    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "freshness": {
            "stale_after_minutes": stale_after_minutes,
            "last_successful_growatt_read_at": now.isoformat(timespec="seconds"),
            "last_successful_pvoutput_upload_at": (
                str(pvoutput_state.get("uploaded_at"))
                if isinstance(pvoutput_state, dict) and pvoutput_state.get("uploaded_at")
                else None
            ),
        },
        "live": live_metrics,
        "sources": sources,
        "quality": {"data": data_quality},
        "insights": {"daily": daily_insights, "daily_mix": daily_mix},
        "planner": {"tonight_risk": risk, "outlook": energy_outlook},
        "assistant": {
            "status": home_status,
            "summary": assistant_summary,
            "recommendations": recommendations,
        },
        "threshold": {
            "value": getattr(threshold_decision, "threshold", None),
            "reason": getattr(threshold_decision, "reason", ""),
            "weather_category": getattr(threshold_decision, "weather_category", ""),
        },
        "automation": {
            "pause": pause_message(pause_state) if pause_state else "active",
            # Strip the internal paused_until_dt datetime helper; paused_until
            # (ISO string) is already present and JSON-serializable.
            "pause_state": {k: v for k, v in pause_state.items() if k != "paused_until_dt"} if pause_state else None,
            "emergency_alert": "active" if alert_state and alert_state.get("active") else "clear",
            "cloud_failure_streak": int(cloud_state.get("count", 0)) if cloud_state else 0,
            "today_override_note": str(today_override.get("note", "")).strip() or "none",
            "today_skipped_jobs": today_override.get("skip", []) if isinstance(today_override.get("skip", []), list) else [],
        },
        "schedule": {
            "timezone": schedule.get("timezone", ""),
            "next_action": next_action,
            "today": [
                {"time": t, "job_id": jid, "command": cmd, "status": st}
                for t, jid, cmd, st in today_jobs
            ],
            "timeline": schedule_timeline,
            "next_runs": [
                {
                    "time": run_at.isoformat(timespec="minutes"),
                    "job_id": str(job.get("id", "")),
                    "name": str(job.get("name", "")),
                    "command": " ".join(schedule_job_tokens(job)),
                }
                for run_at, job in next_runs
            ],
        },
        "pvoutput": pvoutput_state or {"status": "not_configured_or_no_uploads"},
        "history": build_dashboard_history_payload(metric_history, now=now),
    }


def dashboard_freshness(
    output_path: Path,
    stale_minutes: float,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    if stale_minutes <= 0:
        raise app_module().GrowattGuardError("Dashboard stale threshold must be greater than 0 minutes.")

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


def _job_fires_between(job: dict[str, Any], start: dt.datetime, end: dt.datetime) -> list[dt.datetime]:
    cron_expr = str(job.get("cron", ""))
    fires: list[dt.datetime] = []
    cursor = start
    while cursor < end:
        if cron_matches(cron_expr, cursor):
            fires.append(cursor)
        cursor += dt.timedelta(minutes=1)
    return fires


def _fire_window_groups(fires: list[dt.datetime]) -> tuple[list[list[dt.datetime]], int]:
    if not fires:
        return [], 0
    intervals = [
        int((later - earlier).total_seconds() / 60)
        for earlier, later in zip(fires, fires[1:])
        if later > earlier
    ]
    cadence = min(intervals) if intervals else 0
    groups: list[list[dt.datetime]] = [[fires[0]]]
    for fire in fires[1:]:
        previous = groups[-1][-1]
        gap_minutes = int((fire - previous).total_seconds() / 60)
        if cadence and gap_minutes <= cadence + 1:
            groups[-1].append(fire)
        else:
            groups.append([fire])
    return groups, cadence


def _format_fire_windows(fires: list[dt.datetime]) -> tuple[str, str]:
    if not fires:
        return "--", ""
    if len(fires) == 1:
        return fires[0].strftime("%H:%M"), "once"

    groups, cadence = _fire_window_groups(fires)

    windows = []
    for group in groups[:2]:
        start = group[0].strftime("%H:%M")
        end = group[-1].strftime("%H:%M")
        windows.append(start if start == end else f"{start}-{end}")
    if len(groups) > 2:
        windows.append(f"+{len(groups) - 2} windows")

    cadence_text = f"every {format_duration_minutes(cadence)}" if cadence else "repeating"
    return ", ".join(windows), cadence_text


def build_dashboard_schedule_timeline(
    schedule: dict[str, Any],
    today_override: dict[str, Any],
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or dt.datetime.now()
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    now = now.replace(second=0, microsecond=0)
    start = dt.datetime.combine(now.date(), dt.time(0, 0))
    end = start + dt.timedelta(days=1)
    skip_all = bool(today_override.get("skip_all", False))
    skip_ids = set(today_override.get("skip", []))
    replace_map = today_override.get("replace") or {}

    entries: list[dict[str, Any]] = []
    for index, job in enumerate(schedule.get("jobs", []), start=1):
        fires = _job_fires_between(job, start, end)
        if not fires:
            continue
        job_id = schedule_job_id(job, index)
        command = " ".join(schedule_job_tokens(job, index))
        name = str(job.get("name", "")).strip() or job_id
        time_label, cadence = _format_fire_windows(fires)
        next_fire = next((fire for fire in fires if fire >= now), None)
        first_fire = fires[0]
        last_fire = fires[-1]
        recurring = len(fires) > 1
        fire_groups, _ = _fire_window_groups(fires)
        active_window = any(group[0] <= now <= group[-1] for group in fire_groups)
        detail_parts = [command]
        if recurring and cadence:
            detail_parts.append(cadence)

        if skip_all or job_id in skip_ids:
            state = "skipped"
            status = "Skipped"
            detail_parts.append("skipped by override")
        elif job_id in replace_map:
            state = "replaced"
            status = "Replaced"
            replacement = " ".join(schedule_job_tokens(replace_map[job_id], 0))
            detail_parts = [f"replacement: {replacement}"]
        elif recurring and active_window:
            state = "monitoring"
            status = "Monitoring"
        elif next_fire is not None:
            state = "upcoming"
            status = "Upcoming"
        else:
            state = "passed"
            status = "Passed"

        entries.append(
            {
                "time": time_label,
                "first_fire": first_fire.isoformat(timespec="minutes"),
                "last_fire": last_fire.isoformat(timespec="minutes"),
                "next_fire": next_fire.isoformat(timespec="minutes") if next_fire else None,
                "job_id": job_id,
                "name": name,
                "command": command,
                "status": status,
                "state": state,
                "detail": " - ".join(detail_parts),
                "recurring": recurring,
            }
        )

    def _entry_sort_key(entry: dict[str, Any]) -> tuple[int, str]:
        next_fire = str(entry.get("next_fire") or "")
        if entry.get("state") in {"monitoring", "upcoming"} and next_fire:
            return (0, next_fire)
        return (1, str(entry.get("first_fire") or ""))

    entries.sort(key=_entry_sort_key)
    for entry in entries:
        if entry.get("state") == "upcoming":
            entry["status"] = "Next"
            entry["state"] = "next"
            break
    return entries


def _today_job_rows(
    schedule: dict[str, Any],
    today_override: dict[str, Any],
    today: dt.date,
) -> list[tuple[str, str, str, str]]:
    skip_all = bool(today_override.get("skip_all", False))
    skip_ids = set(today_override.get("skip", []))
    replace_map = today_override.get("replace") or {}
    start = dt.datetime.combine(today, dt.time(0, 0))
    end = start + dt.timedelta(days=1)
    rows: list[tuple[str, str, str, str]] = []
    for index, job in enumerate(schedule.get("jobs", []), start=1):
        job_id = schedule_job_id(job, index)
        fires = _job_fires_between(job, start, end)
        if not fires:
            continue
        cmd = " ".join(schedule_job_tokens(job, index))
        time_str, cadence = _format_fire_windows(fires)
        if cadence.startswith("every ") and len(fires) > 8:
            time_str = cadence

        if skip_all or job_id in skip_ids:
            status_str = "SKIP"
        elif job_id in replace_map:
            repl_cmd = " ".join(schedule_job_tokens(replace_map[job_id], 0))
            status_str = f"→ {repl_cmd}"
        else:
            status_str = "OK"
        rows.append((time_str, job_id, cmd, status_str))
    return rows


def _upcoming_override_rows(overrides: dict[str, Any], today: dt.date, days: int = 14) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    cutoff = (today + dt.timedelta(days=days)).isoformat()
    today_iso = today.isoformat()
    for date_str in sorted(overrides.get("dates", {})):
        if date_str <= today_iso or date_str > cutoff:
            continue
        override = overrides["dates"][date_str]
        note = str(override.get("note", "")).strip()
        if override.get("skip_all"):
            action = "skip-all"
        else:
            parts: list[str] = []
            skip_ids = override.get("skip", [])
            if skip_ids:
                parts.append(f"skip: {', '.join(skip_ids)}")
            replace_map = override.get("replace") or {}
            if replace_map:
                parts.append(f"replace: {', '.join(replace_map)}")
            action = "; ".join(parts) if parts else "none"
        rows.append((date_str, note, action))
    return rows


def _pvoutput_card_html(state: dict[str, Any] | None, now: dt.datetime) -> str:
    if state is None:
        return (
            '<div class="card"><div class="label">PVOutput</div>'
            '<div class="value muted" style="font-size:16px">—</div>'
            '<div class="muted small">no uploads recorded</div></div>'
        )
    try:
        uploaded_at = dt.datetime.fromisoformat(str(state.get("uploaded_at", "")))
        age_seconds = max(0.0, (now - uploaded_at).total_seconds())
        time_str = uploaded_at.strftime("%H:%M")
        stale = age_seconds > 20 * 60
    except (ValueError, TypeError):
        return (
            '<div class="card"><div class="label">PVOutput</div>'
            '<div class="value muted" style="font-size:16px">—</div>'
            '<div class="muted small">invalid state</div></div>'
        )
    fields = state.get("fields", {})
    parts: list[str] = []
    v1 = fields.get("v1")
    v2 = fields.get("v2")
    if v1 is not None:
        parts.append(f"{int(v1) / 1000:.1f} kWh")
    if v2 is not None:
        parts.append(f"{v2} W PV")
    age_text = format_duration(age_seconds)
    detail = (", ".join(parts) + f" · {age_text} ago") if parts else f"{age_text} ago"
    badge_cls = "badge-warn" if stale else "badge-ok"
    badge_txt = "STALE" if stale else "OK"
    return (
        '<div class="card"><div class="label">PVOutput</div>'
        f'<div class="value"><span class="badge {badge_cls}">{badge_txt}</span>'
        f' <span style="font-size:16px">{esc(time_str)}</span></div>'
        f'<div class="muted small">{esc(detail)}</div></div>'
    )



def _inline_badge(text: Any, class_name: str) -> str:
    return f'<span class="badge {esc(class_name)}">{esc(text)}</span>'


def _stat_block(label: Any, value: Any, detail: Any = "", class_name: str = "") -> str:
    class_attr = f' class="{esc(class_name)}"' if class_name else ""
    detail_html = f"<em>{esc(detail)}</em>" if detail else ""
    return (
        f"<div{class_attr}>"
        f"<span>{esc(label)}</span>"
        f"<strong>{esc(value)}</strong>"
        f"{detail_html}"
        "</div>"
    )


def _glance_card(
    class_name: str,
    title: Any,
    value: Any,
    badge_text: Any,
    badge_class: str,
    rows: list[tuple[Any, Any]],
    detail: Any = "",
) -> str:
    row_html = "".join(
        f"<div><span>{esc(label)}</span><strong>{esc(row_value)}</strong></div>"
        for label, row_value in rows
    )
    detail_html = f'<p class="glance-detail">{esc(detail)}</p>' if detail else ""
    return (
        f'<article class="glance-card {esc(class_name)}">'
        '<div class="glance-head">'
        f'<div><div class="label">{esc(title)}</div><div class="glance-value">{esc(value)}</div></div>'
        f'{_inline_badge(badge_text, badge_class)}'
        '</div>'
        f'{detail_html}'
        f'<div class="glance-stats">{row_html}</div>'
        '</article>'
    )



def _render_insight_cards(items: Any) -> str:
    return "\n".join(
        (
            '<article class="card insight-card">'
            f'<div class="label">{esc(str(item.get("label", "")))}</div>'
            f'<div class="value">{_inline_badge(str(item.get("title", "Unknown")), _status_badge_class(str(item.get("level", "unknown"))))}</div>'
            f'<div class="muted small">{esc(str(item.get("detail", "")))}</div>'
            "</article>"
        )
        for item in items
        if isinstance(item, dict)
    )


def _metric_card(label: Any, value: Any, detail: Any, accent: str = "", meter_width: float | None = None) -> str:
    accent_class = f" accent-{accent}" if accent else ""
    meter_class = f" {accent}-meter" if accent else ""
    meter_html = ""
    if meter_width is not None:
        width = max(0.0, min(100.0, float(meter_width)))
        meter_html = f'<div class="metric-meter{meter_class}"><span style="width:{width:.0f}%"></span></div>'
    return (
        f'<article class="card metric-card{accent_class}">'
        '<div class="metric-head"><div>'
        f'<div class="label">{esc(label)}</div><div class="value">{esc(value)}</div>'
        '</div></div>'
        f'{meter_html}'
        f'<div class="muted small">{esc(detail)}</div>'
        '</article>'
    )


def _render_status_rows(rows: list[tuple[Any, Any, str]]) -> str:
    return "\n".join(
        (
            '<div class="status-row">'
            f'<span>{esc(label)}</span>'
            f'{_inline_badge(value, badge_class)}'
            "</div>"
        )
        for label, value, badge_class in rows
    )


def _render_activity_items(rows: list[dict[str, Any]]) -> str:
    items = "\n".join(
        (
            '<li class="activity-item">'
            '<div>'
            f'<strong>{esc(row.get("action", "") or row.get("command", "") or "mode decision")}</strong>'
            f'<span>{esc(row.get("timestamp", ""))}</span>'
            '</div>'
            f'<span class="summary-meta">SOC {esc(row.get("soc", "") or "--")}</span>'
            '</li>'
        )
        for row in rows[:5]
    )
    return items or '<li class="activity-item muted">No recent mode decisions recorded.</li>'


def _render_timeline_items(items: list[dict[str, Any]]) -> str:
    timeline_badges = {
        "next": "badge-ok",
        "monitoring": "badge-ok",
        "upcoming": "badge-warn",
        "passed": "badge-warn",
        "skipped": "badge-warn",
        "replaced": "badge-warn",
    }
    rendered = "\n".join(
        (
            f'<li class="timeline-item timeline-{esc(str(item.get("state", "unknown")))}">'
            '<div class="timeline-marker" aria-hidden="true"></div>'
            '<div class="timeline-main">'
            f'<strong>{esc(str(item.get("time", "--")))} - {esc(str(item.get("name", "")))}</strong>'
            f'<span>{esc(str(item.get("detail", "")))}</span>'
            '</div>'
            f'{_inline_badge(str(item.get("status", "Unknown")), timeline_badges.get(str(item.get("state", "")), "badge-warn"))}'
            '</li>'
        )
        for item in items[:8]
    )
    return rendered or '<li class="timeline-item muted">No automation jobs scheduled today.</li>'



def _render_energy_outlook(view: dict[str, Any]) -> str:
    cards = "\n".join(
        '<div class="card">'
        f'<div class="label">{esc(label)}</div>'
        f'<div class="value">{esc(value)}</div>'
        f'<div class="muted small">{esc(detail)}</div>'
        '</div>'
        for label, value, detail in view["cards"]
    )
    return f"""
    <div class="section-head" id="forecast">
      <div>
        <h2>Energy Outlook</h2>
        <div class="muted">Predictive view of generation, reserve, grid use, and weather impact.</div>
      </div>
      {_inline_badge('Confidence: ' + str(view['confidence']), 'badge-neutral')}
    </div>
    <section class="grid ops-grid" aria-label="Energy outlook">
      {cards}
    </section>"""


def _render_daily_mix(view: dict[str, Any]) -> str:
    def _bar(primary_width: str, neutral_width: str, label: str) -> str:
        return (
            f'<div class="mix-bar" aria-label="{esc(label)}">'
            f'<span class="mix-segment primary" style="width:{esc(primary_width)}%"></span>'
            f'<span class="mix-segment neutral" style="width:{esc(neutral_width)}%"></span>'
            '</div>'
        )

    def _panel(title: str, total: str, bar_html: str, rows: list[tuple[str, str]]) -> str:
        legend = "".join(f'<div><span>{esc(label)}</span><strong>{esc(value)}</strong></div>' for label, value in rows)
        return f"""
        <div class="mix-panel">
          <div class="mix-row-head"><strong>{esc(title)}</strong><span>{esc(total)}</span></div>
          {bar_html}
          <div class="mix-legend">{legend}</div>
        </div>"""

    panels = "\n".join(
        [
            _panel(
                "Supply",
                view["supply_total_display"],
                _bar(view["pv_supply_width"], view["grid_supply_width"], "PV and grid supply mix"),
                [("PV", view["pv_supply_label"]), ("Grid", view["grid_supply_label"])],
            ),
            _panel(
                "Demand",
                view["demand_total_display"],
                _bar(view["load_demand_width"], view["charge_demand_width"], "Load and battery charging demand mix"),
                [("House load", view["load_demand_label"]), ("Stored", view["charge_demand_label"])],
            ),
            _panel(
                "Battery",
                view["battery_activity_display"],
                _bar(view["charge_battery_width"], view["discharge_battery_width"], "Battery charge and discharge mix"),
                [(view["battery_net_title"], view["battery_net_display"]), ("Discharged", view["discharge_battery_label"])],
            ),
        ]
    )
    return f"""
    <section class="daily-mix card" aria-label="Today energy mix">
      <div class="mix-header">
        <div>
          <div class="label">Today Mix</div>
          <div class="muted small">Where energy came from, where it went, and the battery net position.</div>
        </div>
        {_inline_badge('Reported counters', view['quality_badge_class'])}
      </div>
      <div class="mix-grid">
        {panels}
      </div>
    </section>
"""


def _render_night_view(view: dict[str, Any]) -> str:
    solar_now_level = "night" if (view.get("pv_w") or 0) < 20 else "active"
    solar_detail = (
        "PV is offline now; tomorrow forecast remains the next solar signal."
        if solar_now_level == "night"
        else f"PV is covering {view['pv_cover_display']} of live house load."
    )
    day_total_items = "\n".join(
        _stat_block(label, value, detail, "night-total-item")
        for label, value, detail in [
            ("PV Today", view["pv_today_display"], f"Lifetime {view['pv_lifetime']}"),
            ("Tomorrow PV", view["tomorrow_pv"], view["forecast_short"]),
            ("Grid Today", view["grid_today_display"], view["grid_status_text"]),
            ("Battery Charge", view["charge_today_display"], "stored today"),
            ("Battery Discharge", view["discharge_today_display"], "used today"),
            ("Battery Throughput", view["battery_throughput_display"], "charge + discharge"),
        ]
    )
    battery_stats = "\n".join(
        [
            _stat_block("Current power", view["battery_flow_display"], view["battery_context"]),
            _stat_block("Usable reserve", view["usable_kwh_display"], f"Floor {view['reserve_floor_display']}"),
            _stat_block("Voltage", view["vbat"], "Battery bus reading"),
        ]
    )
    battery_subgrid = "\n".join(
        [
            _stat_block("Charge today", view["charge_today_display"]),
            _stat_block("Discharge today", view["discharge_today_display"]),
            _stat_block("Throughput", view["battery_throughput_display"]),
            _stat_block("Runtime", view["est_runtime"]),
        ]
    )
    solar_stats = "\n".join(
        [
            _stat_block("PV Today", view["pv_today_display"], solar_detail, "night-primary-stat"),
            _stat_block("PV Lifetime", view["pv_lifetime"], "Total Growatt production"),
            _stat_block("Tomorrow PV", view["tomorrow_pv"], view["forecast_short"]),
            _stat_block("Weather", view["weather_short"], view["weather_detail"]),
        ]
    )
    risk_scores = "\n".join(
        [
            _stat_block("Projected sunrise", view["tonight_projection_display"]),
            _stat_block("Reserve target", view["reserve_target_display"]),
            _stat_block("Top-up needed", view["topup_needed_display"]),
        ]
    )
    return f"""
    <section class="night-console" aria-label="Night operations solar and battery view">
      <div class="night-context-strip">
        {_inline_badge('Data: ' + str(view['quality_display']), view['quality_badge_class'])}
        {_inline_badge('Next: ' + str(view['next_action_relative']) + ' - ' + str(view['next_action_title']), 'badge-neutral')}
      </div>
      <div class="night-hero-grid">
        <article class="night-panel night-battery">
          <div class="night-panel-head">
            <div><div class="label">Battery Reserve</div><div class="night-panel-title">{esc(view['soc'])}</div></div>
            {_inline_badge(view['soc_health'], view['soc_health_class'])}
          </div>
          <div class="night-battery-main">
            <div class="soc-ring night-soc-ring" style="--soc:{float(view['soc_gauge_value']):.0f}%">
              <div class="soc-core"><strong>{esc(view['soc'])}</strong><span>{esc(view['battery_power_label'])}</span></div>
            </div>
            <div class="night-metric-stack">{battery_stats}</div>
          </div>
          <div class="night-subgrid">{battery_subgrid}</div>
        </article>
        <article class="night-panel night-solar">
          <div class="night-panel-head">
            <div><div class="label">Solar Detail</div><div class="night-panel-title">{esc(view['pv_power_display'])}</div></div>
            {_inline_badge(solar_now_level.capitalize(), 'badge-neutral')}
          </div>
          <div class="night-solar-grid">{solar_stats}</div>
          <div class="night-spark" aria-hidden="true">
            <span style="height:18%"></span><span style="height:32%"></span><span style="height:48%"></span>
            <span style="height:72%"></span><span style="height:88%"></span><span style="height:64%"></span>
            <span style="height:42%"></span><span style="height:12%"></span>
          </div>
        </article>
        <article class="night-panel night-risk">
          <div class="night-panel-head">
            <div><div class="label">Tonight Risk</div><div class="night-panel-title">{_inline_badge(view['tonight_title'], view['night_topup_class'])}</div></div>
            {_inline_badge(view['next_action_relative'], 'badge-neutral')}
          </div>
          <div class="night-risk-score">{risk_scores}</div>
          <div class="night-risk-note">{esc(view['tonight_detail'])}</div>
          <div class="night-next">
            <span>Next automation</span><strong>{esc(view['next_action_title'])}</strong><em>{esc(view['next_action_detail'])}</em>
          </div>
        </article>
      </div>
      <section class="night-flow" aria-label="Night live power flow">
        <div class="night-flow-node solar"><span>Solar Now</span><strong>{esc(view['pv_power_display'])}</strong><em>{esc(view['pv_today_display'])} today</em></div>
        <div class="night-flow-arrow">-&gt;</div>
        <div class="night-flow-node inverter"><span>Inverter</span><strong>{esc(view['mode'])}</strong><em>{esc(view['bat_status'])}</em></div>
        <div class="night-flow-arrow">-&gt;</div>
        <div class="night-flow-node load"><span>Load Now</span><strong>{esc(view['load_power_display'])}</strong><em>{esc(view['load_today_display'])} today</em></div>
        <div class="night-flow-node battery"><span>Battery</span><strong>{esc(view['soc'])}</strong><em>{esc(view['battery_context'])} - {esc(view['battery_flow_display'])}</em></div>
        <div class="night-flow-node grid-source"><span>Grid Now</span><strong>{esc(view['grid_power_display'])}</strong><em>{esc(view['grid_now_detail'])}</em></div>
      </section>
      <section class="night-totals" aria-label="Solar and battery day totals">{day_total_items}</section>
    </section>"""


def build_dashboard_html(
    status: dict[str, Any],
    schedule: dict[str, Any],
    overrides: dict[str, Any],
    threshold_decision: Any,
    stale_after_minutes: float = 30,
    battery_capacity_wh: float = 0.0,
    battery_bms_cutoff_soc: float = 25.0,
    hours_to_sunrise: float | None = None,
    battery_charge_rate_w: float = 0.0,
    auto_topup_target_soc: float = 0.0,
    auto_topup_solar_skip_min_margin_minutes: float = 60.0,
    auto_topup_min_minutes: float = 0.0,
    discord_topup_max_minutes: float = 0.0,
    metrics_history: list[dict[str, Any]] | None = None,
    hours_to_sunset: float | None = None,
    tonight_floor_soc: float = 35.0,
    tonight_comfortable_soc: float = 45.0,
    utility_hold_state: dict[str, Any] | None = None,
    pv_forecast: dict[str, Any] | None = None,
) -> str:
    now = dt.datetime.now()
    generated_at = now.astimezone()
    generated_at_iso = generated_at.isoformat(timespec="seconds")
    live_metrics = extract_dashboard_metrics(status, now=generated_at)
    metric_history = _history_with_live(metrics_history or [], live_metrics)
    metric_history_json = json.dumps(build_dashboard_history_payload(metric_history, now=now))
    soc_result = extract_soc(status)
    soc = f"{soc_result[0]:g}%" if soc_result else "Not found"
    output_source = extract_spf_output_source(status)
    mode = f"{output_source[1]} [{output_source[0]}]" if output_source else "Not found"
    bypass = detect_unexpected_grid_bypass(status)
    bypass_detected = bool(bypass["detected"])
    bypass_reason = str(bypass.get("reason") or "")
    bat_status = extract_battery_status(status) or "—"
    _load = extract_first_metric(status, ("loadPercent", "loadPercent1"))
    _n = parse_number(_load[0]) if _load else None
    load_pct = f"{_n:.0f}%" if _n is not None else "—"
    _pd = extract_first_metric(status, ("pDischarge", "pDischarge1"))
    _pc = extract_first_metric(status, ("pCharge", "pCharge1"))
    _pdv = parse_number(_pd[0]) if _pd else None
    _pcv = parse_number(_pc[0]) if _pc else None
    est_runtime = "—"
    runtime_note = "Usable energy unavailable"
    if battery_capacity_wh > 0 and soc_result:
        _usable_runtime_kwh = max(0.0, (soc_result[0] - battery_bms_cutoff_soc) / 100.0 * battery_capacity_wh / 1000.0)
        runtime_note = f"Usable to {_fmt_pct(battery_bms_cutoff_soc)} floor: {_fmt_kwh(_usable_runtime_kwh)}"
    elif battery_capacity_wh > 0:
        runtime_note = "Capacity " + _fmt_kwh(battery_capacity_wh / 1000.0)
    if _pdv is not None or _pcv is not None:
        _bw = (_pdv or 0.0) - (_pcv or 0.0)
        if battery_capacity_wh > 0 and soc_result:
            if _bw > 0:
                if _bw < 200:
                    est_runtime = "PV covering load"
                    runtime_note = f"Live battery draw only {_fmt_w(_bw)}"
                else:
                    _rt = estimate_runtime(soc_result[0], _bw, battery_capacity_wh, battery_bms_cutoff_soc)
                    if _rt is not None:
                        est_runtime = format_duration_minutes(_rt) + " remaining"
            elif _bw < 0:
                _ct = estimate_charge_time(soc_result[0], abs(_bw), battery_capacity_wh)
                if _ct is not None:
                    est_runtime = format_duration_minutes(_ct) + " to full"
    _vbat = extract_first_metric(status, ("vBat", "vBat1", "vbat"))
    _vbat_n = parse_number(_vbat[0]) if _vbat else None
    vbat = f"{_vbat_n:g} V" if _vbat_n is not None else "—"
    sunrise_display = "—"
    topup_sunrise_display = "—"
    if hours_to_sunrise is not None and hours_to_sunrise > 0:
        sunrise_display = format_duration_minutes(hours_to_sunrise * 60)
        if battery_charge_rate_w > 0 and soc_result and _pdv is not None and _pdv > 0:
            topup_load_w = _pdv
            history = read_discharge_rate_history()
            rates = [r["rate_w"] for r in history if isinstance(r.get("rate_w"), (int, float))]
            if len(rates) >= 2:
                topup_load_w = sum(rates) / len(rates)

            margin_minutes = max(0.0, auto_topup_solar_skip_min_margin_minutes)
            margin_hours = hours_to_sunrise + margin_minutes / 60.0
            effective_target_soc = max(battery_bms_cutoff_soc, auto_topup_target_soc)
            estimates = [
                estimate_topup_for_sunrise(
                    soc_result[0], topup_load_w, battery_capacity_wh, battery_bms_cutoff_soc,
                    battery_charge_rate_w, margin_hours,
                ),
                estimate_topup_for_sunrise(
                    soc_result[0], topup_load_w, battery_capacity_wh, effective_target_soc,
                    battery_charge_rate_w, hours_to_sunrise,
                ),
            ]
            valid_estimates = [value for value in estimates if value is not None]
            _ts = max(valid_estimates) if valid_estimates else None
            if _ts is not None:
                if _ts <= 0:
                    topup_sunrise_display = "not needed"
                else:
                    topup_min = max(1, round(_ts))
                    if auto_topup_min_minutes > 0 and _ts < auto_topup_min_minutes:
                        minimum = format_duration_minutes(auto_topup_min_minutes)
                        topup_sunrise_display = f"skip (<{minimum})"
                    else:
                        if discord_topup_max_minutes > 0 and topup_min > discord_topup_max_minutes:
                            topup_min = round(discord_topup_max_minutes)
                        topup_sunrise_display = format_duration_minutes(topup_min)
    pause_state = read_pause_state()
    pause = pause_message(pause_state) if pause_state else "active"
    alert_state = read_battery_alert_state()
    alert = "active" if alert_state and alert_state.get("active") else "clear"
    cloud_state = read_growatt_cloud_failure_state()
    cloud_streak = int(cloud_state.get("count", 0)) if cloud_state else 0
    today_override = today_schedule_override(overrides, now.date())
    override_note = str(today_override.get("note", "")).strip() or "none"
    skipped = ", ".join(today_override.get("skip", [])) if isinstance(today_override.get("skip", []), list) else ""
    last_actions = [
        row
        for row in read_mode_audit_rows(limit=40, newest_first=True)
        if str(row.get("dry_run", "")).strip().lower() != "true"
    ][:8]
    next_runs = next_scheduled_runs(schedule, now=now, limit=8)
    next_action = build_dashboard_next_action(schedule, now=now)
    stale_minutes_text = f"{stale_after_minutes:g}"

    today_jobs = _today_job_rows(schedule, today_override, now.date())
    schedule_timeline = build_dashboard_schedule_timeline(schedule, today_override, now=now)
    upcoming_overrides = _upcoming_override_rows(overrides, now.date())
    chart_data_json = json.dumps(build_chart_data(now=now))
    pvoutput_card = _pvoutput_card_html(read_pvoutput_state(), now)
    pv_power_display = _fmt_w(live_metrics.get("pv_w"))
    grid_power_display = _fmt_w(live_metrics.get("grid_w"))
    grid_source = str(live_metrics.get("grid_source") or "")
    load_power_display = _fmt_w(live_metrics.get("load_w"))
    battery_flow_display = _fmt_w(abs(live_metrics["battery_net_w"])) if live_metrics.get("battery_net_w") is not None else "--"
    battery_flow_dir = (
        "discharging"
        if (live_metrics.get("battery_net_w") or 0) > 0
        else ("charging" if (live_metrics.get("battery_net_w") or 0) < 0 else "standby")
    )
    pv_today_display = _fmt_kwh(live_metrics.get("pv_today_kwh"))
    charge_today_display = _fmt_kwh(live_metrics.get("charge_today_kwh"))
    discharge_today_display = _fmt_kwh(live_metrics.get("discharge_today_kwh"))
    load_today_display = _fmt_kwh(live_metrics.get("load_today_kwh"))
    grid_today_display = _fmt_kwh(live_metrics.get("grid_today_kwh"))
    grid_detail = f"source: {grid_source}" if grid_source else "not reported by API"
    pv_total_text = str(live_metrics.get("pv_total") or "").strip()
    projected_sunset_soc = _project_sunset_soc(live_metrics, battery_capacity_wh, hours_to_sunset)
    overnight_hours = None
    risk_start_soc = None
    risk_basis = ""
    if projected_sunset_soc is not None and _should_use_projected_sunset_start(now, hours_to_sunset, hours_to_sunrise):
        overnight_hours = float(hours_to_sunrise or 0) - float(hours_to_sunset or 0)
        risk_start_soc = projected_sunset_soc
        risk_basis = "projected sunset SOC"
    tonight_risk = build_tonight_risk(
        live_metrics,
        battery_capacity_wh,
        battery_bms_cutoff_soc,
        hours_to_sunrise,
        battery_charge_rate_w,
        auto_topup_target_soc,
        auto_topup_solar_skip_min_margin_minutes,
        projection_start_soc=risk_start_soc,
        projection_hours=overnight_hours,
        projection_basis=risk_basis,
    )
    tonight_badge_class = _status_badge_class(str(tonight_risk.get("level", "unknown")))
    tonight_title = str(tonight_risk.get("title", "Unknown"))
    tonight_detail = str(tonight_risk.get("detail", ""))
    tonight_projection = tonight_risk.get("projected_sunrise_soc")
    tonight_projection_display = _fmt_pct(tonight_projection if isinstance(tonight_projection, (int, float)) else None)
    tonight_topup = tonight_risk.get("topup_minutes")
    tonight_topup_display = (
        format_duration_minutes(float(tonight_topup))
        if isinstance(tonight_topup, (int, float)) and tonight_topup > 0
        else ("not needed" if tonight_topup == 0 else "--")
    )

    # Tonight Safe headline (only shown after evening cutoff).
    _tonight_proj = tonight_risk.get("projected_sunrise_soc")
    tonight_safe = compute_tonight_safe(
        projected_sunrise_soc=_tonight_proj if isinstance(_tonight_proj, (int, float)) else None,
        hours_to_sunset=hours_to_sunset,
        floor_soc=tonight_floor_soc,
        comfortable_soc=tonight_comfortable_soc,
    )
    tonight_safe_html = ""
    if tonight_safe.get("show"):
        _ts_level = tonight_safe.get("level", "watch")
        _ts_badge = "badge-fail" if _ts_level == "danger" else ("badge-ok" if _ts_level == "ok" else "badge-warn")
        _ts_headline = esc(str(tonight_safe.get("headline", "")))
        _ts_subtext = esc(str(tonight_safe.get("subtext", "")))
        _ts_reason = esc(str(tonight_safe.get("reason", "")))
        tonight_safe_html = f"""
      <div class="planner-card primary">
        <div class="label">Tonight Safe</div>
        <div class="value"><span class="badge {_ts_badge}">{_ts_headline}</span></div>
        {f'<div class="muted small">{_ts_subtext}</div>' if _ts_subtext else ''}
        {f'<div class="muted small">{_ts_reason}</div>' if _ts_reason else ''}
      </div>"""

    # Utility hold status (shown when owned/adopted hold is active).
    from growatt_guard.state import read_utility_hold_state as _read_hold
    _hold_state = utility_hold_state if utility_hold_state is not None else _read_hold()
    utility_hold_html = ""
    if _hold_state and _hold_state.get("ownership") in ("owned", "adopted"):
        _own = str(_hold_state.get("ownership", "owned")).capitalize()
        _target = _hold_state.get("target_soc")
        _expiry_str = _hold_state.get("max_expiry", "")
        _eta_str = ""
        if _expiry_str:
            try:
                import datetime as _dt2
                from growatt_guard.state import parse_utc_datetime as _putc, utc_now as _unow
                _exp = _putc(str(_expiry_str))
                _rem_min = int(max(0, (_exp - _unow()).total_seconds() // 60))
                _eta_str = f" · ETA {_rem_min}m"
            except Exception:  # noqa: BLE001
                pass
        _target_str = f"{_target:.0f}%" if isinstance(_target, (int, float)) else "?"
        utility_hold_html = f"""
      <div class="planner-card">
        <div class="label">Utility Hold</div>
        <div class="value"><span class="badge badge-warn">{esc(_own)}</span></div>
        <div class="muted small">Returning to SBU at {esc(_target_str)}{esc(_eta_str)}</div>
      </div>"""

    soc_value = soc_result[0] if soc_result else None
    soc_gauge_value = max(0.0, min(100.0, float(soc_value))) if isinstance(soc_value, (int, float)) else 0.0
    if isinstance(soc_value, (int, float)) and soc_value < battery_bms_cutoff_soc + 5:
        soc_health = "Critical"
        soc_health_class = "badge-fail"
    elif isinstance(soc_value, (int, float)) and soc_value < 50:
        soc_health = "Watch"
        soc_health_class = "badge-warn"
    elif isinstance(soc_value, (int, float)):
        soc_health = "Ready"
        soc_health_class = "badge-ok"
    else:
        soc_health = "Unknown"
        soc_health_class = "badge-warn"

    def _ratio(numerator: Any, denominator: Any) -> float | None:
        if not isinstance(numerator, (int, float)) or not isinstance(denominator, (int, float)):
            return None
        if denominator <= 0:
            return None
        return max(0.0, numerator / denominator * 100.0)

    pv_cover = _ratio(live_metrics.get("pv_w"), live_metrics.get("load_w"))
    pv_cover_display = f"{pv_cover:.0f}%" if pv_cover is not None else "--"
    solar_share = _ratio(live_metrics.get("pv_today_kwh"), live_metrics.get("load_today_kwh"))
    solar_share_display = f"{solar_share:.0f}%" if solar_share is not None else "--"
    solar_share_width = min(100.0, solar_share) if solar_share is not None else 0.0
    grid_reliance = _ratio(live_metrics.get("grid_today_kwh"), live_metrics.get("load_today_kwh"))
    grid_reliance_display = f"{grid_reliance:.0f}%" if grid_reliance is not None else "--"
    grid_reliance_width = min(100.0, grid_reliance) if grid_reliance is not None else 0.0
    battery_charge_share = _ratio(live_metrics.get("charge_today_kwh"), live_metrics.get("load_today_kwh"))
    battery_charge_share_display = f"{battery_charge_share:.0f}%" if battery_charge_share is not None else "--"
    battery_charge_share_width = min(100.0, battery_charge_share) if battery_charge_share is not None else 0.0
    mode_badge_class = "badge-warn" if "utility" in mode.lower() else ("badge-ok" if "sbu" in mode.lower() else "badge-warn")
    bypass_badge_class = "badge-fail" if bypass_detected else "badge-ok"
    bypass_badge_label = "Detected" if bypass_detected else "Clear"
    bypass_status_detail = bypass_reason or "No grid bypass detected"
    grid_now_detail = "estimated from load + charge - PV" if grid_source == "estimated" else (grid_detail or "reported by Growatt")
    _grid_w = live_metrics.get("grid_w") or 0
    if _grid_w < 20:
        grid_status_text = "Solar covering entire load"
    elif _grid_w > 0:
        grid_status_text = f"Drawing {grid_power_display} from grid"
    else:
        grid_status_text = f"Exporting {_fmt_w(abs(int(_grid_w)))} to grid"

    if battery_flow_dir == "charging":
        battery_context = f"Charging · {soc_health}"
    elif battery_flow_dir == "discharging":
        battery_context = f"Discharging · {soc_health}"
    else:
        battery_context = f"Idle · {soc_health}"
    metric_sources = extract_dashboard_metric_sources(status)
    data_quality = build_dashboard_data_quality(live_metrics, metric_sources)
    daily_mix = build_dashboard_daily_mix(live_metrics)
    daily_insights = build_dashboard_daily_insights(live_metrics, metric_history, now=now)
    energy_outlook = build_dashboard_energy_outlook(
        live_metrics,
        tonight_risk,
        pv_forecast,
        threshold_decision,
        hours_to_sunset,
        hours_to_sunrise,
        battery_capacity_wh,
        battery_charge_rate_w,
    )
    threshold_display = _fmt_g(getattr(threshold_decision, "threshold", None), "%")
    threshold_reason = str(getattr(threshold_decision, "reason", "") or "Weather signal is unavailable.")
    home_status = build_dashboard_home_status(
        live_metrics,
        mode,
        battery_flow_dir,
        tonight_risk,
        next_action,
        now=now,
    )
    recommendations = build_dashboard_recommendations(
        live_metrics=live_metrics,
        soc_health=soc_health,
        battery_flow_dir=battery_flow_dir,
        tonight_risk=tonight_risk,
        daily_insights=daily_insights,
        pv_power_display=pv_power_display,
        grid_status_text=grid_status_text,
        energy_outlook=energy_outlook,
        threshold_decision=threshold_decision,
    )
    recommendations_html = "\n".join(
        (
            f'<div class="rec-item rec-{esc(str(r.get("level", "good")))}">'
            f'<span class="rec-icon">{esc(str(r.get("icon", "OK")))}</span>'
            '<span>'
            f'<strong>{esc(str(r.get("title", "Recommendation")))}</strong>'
            f'{esc(str(r.get("text", "")))}'
            f'<em>{esc(str(r.get("meta", "")))}</em>'
            '</span></div>'
        )
        for r in recommendations
    )

    _tmr_value = energy_outlook.get("tomorrow_kwh")
    _tmr_str = _fmt_kwh(_tmr_value)
    _rem_str = _fmt_kwh(energy_outlook.get("today_remaining_kwh"))
    _sunset_str = _fmt_pct(energy_outlook.get("projected_sunset_soc"))
    _sunrise_str = _fmt_pct(energy_outlook.get("projected_sunrise_soc"))
    _grid_forecast_str = _fmt_kwh(energy_outlook.get("expected_grid_kwh"))
    _sunrise_basis_str = str(energy_outlook.get("sunrise_basis") or "Waiting for load history")
    _sunrise_note_str = str(energy_outlook.get("sunrise_note") or "Estimate improves with more history.")
    _topup_minutes = energy_outlook.get("topup_minutes")
    _topup_duration_str = (
        format_duration_minutes(float(_topup_minutes))
        if isinstance(_topup_minutes, (int, float)) and _topup_minutes > 0
        else ("not needed" if _topup_minutes == 0 else "--")
    )
    _weather_str = str(energy_outlook.get("weather", "not configured"))
    _weather_short_str = _weather_str.split(" (", 1)[0]
    _weather_reason_str = threshold_reason
    _tomorrow_pv_detail = "Open-Meteo estimate; actual output can be lower in local rain/cloud."
    _weather_sensitive_pv = False
    _rain = getattr(threshold_decision, "precipitation_mm", None)
    _cloud = getattr(threshold_decision, "cloud_cover", None)
    if isinstance(_tmr_value, (int, float)) and (
        (isinstance(_rain, (int, float)) and _rain >= 1)
        or (isinstance(_cloud, (int, float)) and _cloud >= 70)
    ):
        _low_tmr = _fmt_kwh(float(_tmr_value) * 0.6)
        _tomorrow_pv_detail = f"Weather-sensitive estimate; plan around {_low_tmr}-{_tmr_str}."
        _weather_sensitive_pv = True
    _kwp = pv_forecast.get("panel_kwp", 0) if pv_forecast else 0
    _weather_category = str(getattr(threshold_decision, "weather_category", "") or "").strip().lower()
    _has_weather_signal = _weather_category not in {"", "disabled", "unavailable", "not configured", "unknown"}
    if isinstance(_kwp, (int, float)) and _kwp > 0:
        _forecast_source = _tomorrow_pv_detail + f" {_fmt_g(float(_kwp))} kWp system."
        _forecast_short_str = "Weather-sensitive" if _weather_sensitive_pv else "Open-Meteo estimate"
    elif _has_weather_signal:
        _forecast_source = "Set PANEL_KWP to convert Open-Meteo irradiance into PV kWh."
        _forecast_short_str = "Needs PANEL_KWP"
    else:
        _forecast_source = "Set PANEL_KWP plus WEATHER_LAT/WEATHER_LON to enable PV kWh forecasts."
        _forecast_short_str = "Needs forecast setup"
    energy_outlook_view = {
        "confidence": str(energy_outlook.get("confidence", "Learning")),
        "cards": [
            ("Tomorrow PV", _tmr_str, _forecast_source),
            ("Today Remaining", _rem_str, "Expected generation from now until sunset."),
            ("Battery at Sunset", _sunset_str, "Current-flow estimate; improves with more history."),
            ("Battery at Sunrise", _sunrise_str, f"Estimate basis: {_sunrise_basis_str}. {_sunrise_note_str}"),
            ("Expected Grid Top-up", _grid_forecast_str, f"Top-up duration: {_topup_duration_str} from charge-rate config."),
            ("Weather Impact", _weather_str, _weather_reason_str),
        ],
    }
    pv_forecast_html = _render_energy_outlook(energy_outlook_view)
    quality_badge_class = _status_badge_class(str(data_quality.get("level", "unknown")))
    quality_title = str(data_quality.get("title", "Unknown"))
    quality_score = data_quality.get("score")
    quality_items = data_quality.get("items", [])
    quality_detail = (
        str(quality_items[0])
        if isinstance(quality_items, list) and quality_items
        else "Data quality could not be calculated."
    )
    quality_display = (
        f"{quality_title} {quality_score}%"
        if isinstance(quality_score, (int, float))
        else quality_title
    )
    home_badge_class = _status_badge_class(str(home_status.get("level", "unknown")))
    home_badge_label = str(home_status.get("now_label") or "Learning")
    home_tonight_level = str(home_status.get("tonight_level") or tonight_risk.get("level") or "unknown")
    home_tonight_title = str(home_status.get("tonight_title") or tonight_title)
    home_tonight_badge_class = _status_badge_class(home_tonight_level)
    sunrise_basis_display = str(energy_outlook.get("sunrise_basis") or "Waiting for load history")
    sunrise_reserve_detail_display = sunrise_basis_display
    if isinstance(tonight_projection, (int, float)) and tonight_projection <= 1:
        sunrise_reserve_detail_display = "Stress estimate"
    reserve_target_display = _fmt_pct(energy_outlook.get("reserve_target_soc"))
    topup_needed_display = tonight_topup_display
    battery_charge_rate_display = _fmt_w(battery_charge_rate_w) if battery_charge_rate_w > 0 else "--"
    usable_kwh = None
    if isinstance(soc_value, (int, float)) and battery_capacity_wh > 0:
        usable_kwh = max(0.0, (float(soc_value) - battery_bms_cutoff_soc) / 100.0 * battery_capacity_wh / 1000.0)
    usable_kwh_display = _fmt_kwh(usable_kwh)
    battery_capacity_display = _fmt_kwh(battery_capacity_wh / 1000.0) if battery_capacity_wh > 0 else "--"
    reserve_floor_display = _fmt_pct(battery_bms_cutoff_soc)
    battery_power_label = battery_flow_dir.capitalize()
    battery_throughput = None
    if isinstance(live_metrics.get("charge_today_kwh"), (int, float)) and isinstance(live_metrics.get("discharge_today_kwh"), (int, float)):
        battery_throughput = float(live_metrics["charge_today_kwh"]) + float(live_metrics["discharge_today_kwh"])
    battery_throughput_display = _fmt_kwh(battery_throughput)
    utility_badge_class = bypass_badge_class if bypass_detected else mode_badge_class
    utility_badge_label = "Bypass" if bypass_detected else ("Utility" if "utility" in mode.lower() else "SBU")
    glance_cards = "\n".join(
        [
            _glance_card(
                "glance-battery",
                "Battery",
                soc,
                soc_health,
                soc_health_class,
                [
                    ("Flow", f"{battery_power_label} {battery_flow_display}"),
                    ("Usable", usable_kwh_display),
                    ("Runtime", est_runtime),
                ],
                battery_context,
            ),
            _glance_card(
                "glance-solar",
                "Solar",
                pv_power_display,
                "Active" if (live_metrics.get("pv_w") or 0) >= 20 else "Low",
                "badge-ok" if (live_metrics.get("pv_w") or 0) >= 20 else "badge-warn",
                [
                    ("Today", pv_today_display),
                    ("Cover", pv_cover_display),
                    ("Tomorrow", _tmr_str),
                ],
                _forecast_short_str,
            ),
            _glance_card(
                "glance-utility",
                "Utility",
                grid_power_display,
                utility_badge_label,
                utility_badge_class,
                [
                    ("Today", grid_today_display),
                    ("Mode", mode),
                    ("Bypass", bypass_badge_label),
                ],
                grid_status_text,
            ),
            _glance_card(
                "glance-risk",
                "Tonight Risk",
                tonight_title,
                home_tonight_title,
                home_tonight_badge_class,
                [
                    ("Sunrise", tonight_projection_display),
                    ("Top-up", topup_needed_display),
                    ("Target", reserve_target_display),
                ],
                tonight_detail,
            ),
        ]
    )
    flow_solar_class = "active" if (live_metrics.get("pv_w") or 0) > 0 else ""
    flow_load_class = "active" if (live_metrics.get("load_w") or 0) > 0 else ""

    def _mix_number(key: str) -> float | None:
        value = daily_mix.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    def _mix_width(key: str) -> str:
        value = _mix_number(key)
        return f"{max(0.0, min(100.0, value)):.0f}" if value is not None else "0"

    def _mix_pct_display(key: str) -> str:
        return _fmt_pct(_mix_number(key))

    supply_total_display = _fmt_kwh(_mix_number("supply_total_kwh"))
    demand_total_display = _fmt_kwh(_mix_number("demand_total_kwh"))
    battery_activity_display = _fmt_kwh(_mix_number("battery_activity_total_kwh"))
    battery_net_value = _mix_number("battery_net_kwh")
    battery_net_display = _fmt_kwh(abs(battery_net_value)) if battery_net_value is not None else "--"
    battery_net_title = str(daily_mix.get("battery_net_title", "Battery net unknown"))
    daily_mix_view = {
        "quality_badge_class": quality_badge_class,
        "supply_total_display": supply_total_display,
        "demand_total_display": demand_total_display,
        "battery_activity_display": battery_activity_display,
        "battery_net_title": battery_net_title,
        "battery_net_display": battery_net_display,
        "pv_supply_width": _mix_width("pv_supply_pct"),
        "grid_supply_width": _mix_width("grid_supply_pct"),
        "load_demand_width": _mix_width("load_demand_pct"),
        "charge_demand_width": _mix_width("charge_demand_pct"),
        "charge_battery_width": _mix_width("charge_battery_pct"),
        "discharge_battery_width": _mix_width("discharge_battery_pct"),
        "pv_supply_label": f"{pv_today_display} - {_mix_pct_display('pv_supply_pct')}",
        "grid_supply_label": f"{grid_today_display} - {_mix_pct_display('grid_supply_pct')}",
        "load_demand_label": f"{load_today_display} - {_mix_pct_display('load_demand_pct')}",
        "charge_demand_label": f"{charge_today_display} - {_mix_pct_display('charge_demand_pct')}",
        "discharge_battery_label": f"{discharge_today_display} - {_mix_pct_display('discharge_battery_pct')}",
    }
    daily_mix_html = _render_daily_mix(daily_mix_view)
    next_action_relative = str(next_action.get("relative") or "none")
    next_action_title = str(next_action.get("title") or "No upcoming jobs")
    next_action_detail = str(next_action.get("detail") or "No scheduled jobs found.")
    insight_cards = _render_insight_cards(daily_insights.get("items", []))

    energy_cards = "\n".join(
        [
            _metric_card("PV Today", pv_today_display, f"Solar share of load: {solar_share_display}", "pv", solar_share_width),
            _metric_card("Grid Import Today", grid_today_display, f"Grid reliance vs load: {grid_reliance_display}", "grid", grid_reliance_width),
            _metric_card("Load Today", load_today_display, "Total house consumption", "load", 100),
            _metric_card("Battery Charge Today", charge_today_display, f"Stored energy vs load: {battery_charge_share_display}", "battery", battery_charge_share_width),
            _metric_card("Battery Discharge Today", discharge_today_display, "Battery output to inverter", "battery", 100),
        ]
    )
    if pv_total_text:
        energy_cards += "\n" + _metric_card("PV Lifetime", pv_total_text, "Total production reported by Growatt")

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
    emergency_badge_class = "badge-fail" if alert == "active" else "badge-ok"
    cloud_badge_class = "badge-warn" if cloud_streak else "badge-ok"
    system_status_rows = _render_status_rows(
        [
            ("Inverter Mode", mode, mode_badge_class),
            ("Grid Bypass", bypass_badge_label, bypass_badge_class),
            ("Dashboard", "OK", "badge-ok"),
            ("Data Quality", quality_display, quality_badge_class),
            ("Emergency Alert", alert, emergency_badge_class),
            ("Cloud Streak", str(cloud_streak), cloud_badge_class),
        ]
    )
    activity_items = _render_activity_items(last_actions)
    timeline_items = _render_timeline_items(schedule_timeline)
    today_job_rows_html = "\n".join(
        "<tr>"
        f"<td>{esc(t)}</td>"
        f"<td>{esc(jid)}</td>"
        f"<td>{esc(cmd)}</td>"
        f'<td class="status-{"skip" if st == "SKIP" else ("replace" if st.startswith("→") else "ok")}">{esc(st)}</td>'
        "</tr>"
        for t, jid, cmd, st in today_jobs
    )
    upcoming_override_rows_html = "\n".join(
        "<tr>"
        f"<td>{esc(d)}</td>"
        f"<td>{esc(n) if n else '<span class=\"muted\">—</span>'}</td>"
        f"<td>{esc(a)}</td>"
        "</tr>"
        for d, n, a in upcoming_overrides
    )
    source_rows_html = "\n".join(
        "<tr>"
        f"<td>{esc(label)}</td>"
        f"<td><code>{esc(path or 'not reported')}</code></td>"
        "</tr>"
        for label, path in [
            ("SOC", live_metrics.get("soc_source", "")),
            ("Mode", live_metrics.get("mode_source", "")),
            ("PV power", metric_sources.get("pv_w", "")),
            ("PV today", metric_sources.get("pv_today_kwh", "")),
            ("Load today", metric_sources.get("load_today_kwh", "")),
            ("Grid today", metric_sources.get("grid_today_kwh", "")),
            ("Battery charge today", metric_sources.get("charge_today_kwh", "")),
        ]
    )

    skip_all_banner = (
        '<div class="banner-warn">⚠ All automation jobs are skipped today'
        + (f" — {esc(override_note)}" if override_note != "none" else "")
        + "</div>"
        if today_override.get("skip_all")
        else ""
    )
    upcoming_override_section = (
        f'<details class="detail-panel"><summary><span>Upcoming Overrides</span>'
        f'<span class="summary-meta">{len(upcoming_overrides)} active</span></summary>'
        f'<div class="table-wrap"><table><thead><tr><th>Date</th><th>Note</th><th>Actions</th></tr></thead><tbody>{upcoming_override_rows_html}</tbody></table></div></details>'
        if upcoming_overrides
        else ""
    )
    night_view_html = _render_night_view(
        {
            "bat_status": bat_status,
            "battery_context": battery_context,
            "battery_flow_display": battery_flow_display,
            "battery_power_label": battery_power_label,
            "battery_throughput_display": battery_throughput_display,
            "charge_today_display": charge_today_display,
            "discharge_today_display": discharge_today_display,
            "est_runtime": est_runtime,
            "forecast_short": _forecast_short_str,
            "grid_now_detail": grid_now_detail,
            "grid_power_display": grid_power_display,
            "grid_status_text": grid_status_text,
            "grid_today_display": grid_today_display,
            "load_power_display": load_power_display,
            "load_today_display": load_today_display,
            "mode": mode,
            "mode_badge_class": mode_badge_class,
            "next_action_detail": next_action_detail,
            "next_action_relative": next_action_relative,
            "next_action_title": next_action_title,
            "night_topup_class": "badge-fail" if tonight_badge_class == "badge-fail" else tonight_badge_class,
            "pv_cover_display": pv_cover_display,
            "pv_lifetime": pv_total_text or "--",
            "pv_power_display": pv_power_display,
            "pv_today_display": pv_today_display,
            "pv_w": live_metrics.get("pv_w") or 0,
            "quality_badge_class": quality_badge_class,
            "quality_display": quality_display,
            "reserve_floor_display": reserve_floor_display,
            "reserve_target_display": reserve_target_display,
            "soc": soc,
            "soc_gauge_value": soc_gauge_value,
            "soc_health": soc_health,
            "soc_health_class": soc_health_class,
            "tomorrow_pv": _tmr_str,
            "tonight_detail": tonight_detail,
            "tonight_projection_display": tonight_projection_display,
            "tonight_title": tonight_title,
            "topup_needed_display": topup_needed_display,
            "usable_kwh_display": usable_kwh_display,
            "vbat": vbat,
            "weather_detail": _weather_reason_str or _weather_str,
            "weather_short": _weather_short_str,
        }
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>Growatt Dashboard</title>
  <style>{DASHBOARD_CSS}</style>
</head>
<body>
  <div class="app-shell">
  <aside class="sidebar" aria-label="Dashboard sections">
    <div class="sidebar-brand">
      <div class="brand-mark" aria-hidden="true"></div>
      <div>
        <div class="sidebar-title">Solar Inverter</div>
        <div class="muted">Growatt Dashboard</div>
      </div>
    </div>
    <nav class="sidebar-nav">
      <a class="active" href="#overview">Overview</a>
      <a href="#flow">Power Flow</a>
      <a href="#insights">Energy Insights</a>
      <a href="#daily">Daily Energy</a>
      <a href="#planner">Tonight Planner</a>
      <a href="#forecast">Outlook</a>
      <a href="#recommendations">Recommendations</a>
      <a href="#automation">Automation</a>
      <a href="#trends">Trends</a>
      <a href="#operations">Operations</a>
    </nav>
    <div class="sidebar-status">
      <div>
        <div class="label">System Status</div>
        <div class="value"><span class="badge {esc(soc_health_class)}">{esc(soc_health)}</span></div>
      </div>
      <div>
        <div class="label">Last Updated</div>
        <div class="muted" data-refresh-age>Generated just now</div>
      </div>
    </div>
  </aside>
  <main>
    <header class="topbar" id="overview">
      <div>
        <div class="hero-kicker">{esc(str(home_status.get("greeting", "Hello")))}</div>
        <h1>{esc(str(home_status.get("headline", "Home energy is stable")))}</h1>
        <div class="muted">Generated {esc(generated_at.strftime('%Y-%m-%d %H:%M:%S %Z'))}</div>
      </div>
      <div class="top-actions">
        <span class="pill">Mode: {esc(mode)}</span>
        <span class="pill">SOC: {esc(soc)}</span>
        <span class="pill">Refresh: 5min</span>
        <button class="theme-toggle" id="layout-toggle-btn" onclick="toggleDashLayout()">Night ops</button>
        <button class="theme-toggle" id="theme-toggle-btn" onclick="toggleDashTheme()">Light</button>
      </div>
    </header>
    {skip_all_banner}
    <div class="dashboard-view dashboard-night">
      {night_view_html}
    </div>
    <div class="dashboard-view dashboard-current">

    <section class="glance-grid" aria-label="Key details at a glance">
      {glance_cards}
    </section>

    <section class="battery-overview card" aria-label="Battery reserve and overnight plan">
        <div class="battery-panel-head">
          <div>
            <div class="label">Battery Reserve</div>
            <div class="mode-value">{esc(soc)}</div>
          </div>
          <div class="reserve-badges">
            <span class="badge {esc(home_badge_class)}">Now: {esc(home_badge_label)}</span>
            <span class="badge {esc(home_tonight_badge_class)}">Tonight: {esc(home_tonight_title)}</span>
            <span class="badge {esc(soc_health_class)}">{esc(soc_health)}</span>
          </div>
        </div>
        <div class="soc-command battery-command">
          <div class="soc-ring" style="--soc:{soc_gauge_value:.0f}%">
            <div class="soc-core">
              <strong>{esc(soc)}</strong>
              <span>{esc(battery_power_label)}</span>
            </div>
          </div>
          <div class="battery-stats">
            <div><span>Current power</span><strong>{esc(battery_flow_display)}</strong><em>{esc(battery_context)}</em></div>
            <div><span>Usable reserve</span><strong>{esc(usable_kwh_display)}</strong><em>Floor {esc(reserve_floor_display)}</em></div>
            <div><span>Sunrise reserve</span><strong>{esc(tonight_projection_display)}</strong><em>{esc(sunrise_reserve_detail_display)}</em></div>
            <div><span>Top-up needed</span><strong>{esc(topup_needed_display)}</strong><em>Expected grid {esc(_grid_forecast_str)}</em></div>
            <div><span>Reserve target</span><strong>{esc(reserve_target_display)}</strong><em>{esc(sunrise_basis_display)}</em></div>
            <div><span>Current-load runtime</span><strong>{esc(est_runtime)}</strong><em>{esc(runtime_note)}</em></div>
            <div><span>Charge rate</span><strong>{esc(battery_charge_rate_display)}</strong><em>Configured grid charge</em></div>
            <div><span>Voltage</span><strong>{esc(vbat)}</strong><em>Battery bus reading</em></div>
            <div><span>Day throughput</span><strong>{esc(battery_throughput_display)}</strong><em>Charge plus discharge today</em></div>
          </div>
        </div>
        <div class="battery-outlook" aria-label="Tomorrow battery context">
          <div><span>Tomorrow PV</span><strong>{esc(_tmr_str)}</strong><em>{esc(_forecast_short_str)}</em></div>
          <div><span>Weather context</span><strong>{esc(_weather_short_str)}</strong><em>{esc(_weather_reason_str)}</em></div>
        </div>
    </section>

    <section class="flow-stage" id="flow" aria-label="Live energy flow">
        <div class="flow-head">
          <div>
            <h2>Live energy flow</h2>
            <div class="muted">{esc(bat_status)} &middot; {esc(battery_context)} &middot; Load: {esc(load_power_display)} at {esc(load_pct)}</div>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
            <a href="#insights" class="badge {esc(_status_badge_class(str(daily_insights.get("status", "unknown"))))}" style="text-decoration:none">Today: {esc(str(daily_insights.get("title", "Learning")))}</a>
            <span class="badge {esc(tonight_badge_class)}">Tonight: {esc(tonight_title)}</span>
            <span class="badge {esc(bypass_badge_class)}" title="{esc(bypass_status_detail)}">Bypass: {esc(bypass_badge_label)}</span>
            <span class="badge badge-neutral" title="{esc(next_action_detail)}">Next: {esc(next_action_relative)} · {esc(next_action_title)}</span>
          </div>
        </div>
        <div class="flow-map flow-chain" aria-label="Live energy flow chain">
          <div class="flow-main-row">
            <div class="flow-tile solar">
              <div>
                <div class="flow-label">Solar Now</div>
                <div class="flow-value">{esc(pv_power_display)}</div>
              </div>
              <div class="flow-detail">{esc(pv_today_display)} generated today</div>
            </div>
            <div class="connector pv {esc(flow_solar_class)}" aria-hidden="true"></div>
            <div class="flow-tile inverter">
              <div>
                <div class="flow-label">Inverter</div>
                <div class="flow-value">{esc(mode)}</div>
              </div>
              <div class="flow-detail">{esc(bat_status)}{esc(' · ' + bypass_status_detail if bypass_detected else '')}</div>
            </div>
            <div class="connector load {esc(flow_load_class)}" aria-hidden="true"></div>
            <div class="flow-tile load">
              <div>
                <div class="flow-label">Load Now</div>
                <div class="flow-value">{esc(load_power_display)}</div>
              </div>
              <div class="flow-detail">{esc(load_today_display)} consumed today</div>
            </div>
          </div>
          <div class="flow-support-row">
            <div class="flow-tile flow-chip battery">
              <div>
                <div class="flow-label">Battery</div>
                <div class="flow-value">{esc(soc)}</div>
              </div>
              <div class="flow-detail">{esc(battery_context)} - {esc(battery_flow_display)}</div>
            </div>
            <div class="flow-tile flow-chip grid-source">
              <div>
                <div class="flow-label">Grid Import Now</div>
                <div class="flow-value">{esc(grid_power_display)}</div>
              </div>
              <div class="flow-detail">{esc(grid_status_text)}</div>
            </div>
          </div>
        </div>
    </section>

    <section class="chart-grid today-charts" aria-label="Today trends">
      <div class="card chart-card">
        <div class="label">Power Today</div>
        <canvas id="power-trend-chart"></canvas>
        <div class="legend">
          <span style="--c:#F5A82A">PV</span>
          <span style="--c:#EF6F6F">Load</span>
          <span style="--c:#5B8DEF">Grid</span>
        </div>
      </div>
      <div class="card chart-card compact">
        <div class="label">Battery SOC Today</div>
        <canvas id="soc-trend-chart"></canvas>
        <div class="legend"><span style="--c:#35C4A0">SOC</span></div>
      </div>
    </section>

    <div class="section-head" id="insights">
      <div>
        <h2>Energy Insights</h2>
        <div class="muted">Same-time comparison against recent local history, without extra Growatt calls.</div>
      </div>
      <span class="badge {esc(_status_badge_class(str(daily_insights.get("status", "unknown"))))}">{esc(str(daily_insights.get("title", "Learning")))}</span>
    </div>
    <section class="grid insight-grid">
      {insight_cards}
    </section>

    <div class="section-head" id="daily">
      <div>
        <h2>Daily Energy</h2>
        <div class="muted">Production, consumption, grid import, and battery movement for today.</div>
      </div>
    </div>
    {daily_mix_html}
    <section class="grid daily-grid">
      {energy_cards}
    </section>

    <h2 id="trends">Energy Trends</h2>
    <section class="chart-grid">
      <div class="card chart-card compact">
        <div class="label">7-Day Battery Energy</div>
        <canvas id="battery-energy-chart"></canvas>
        <div class="legend">
          <span style="--c:#35C4A0">Charge</span>
          <span style="--c:#6A7A99">Discharge</span>
        </div>
      </div>
      <div class="card chart-card compact">
        <div class="label">7-Day Supply Mix</div>
        <canvas id="supply-energy-chart"></canvas>
        <div class="legend">
          <span style="--c:#F5A82A">PV</span>
          <span style="--c:#5B8DEF">Grid</span>
          <span style="--c:#EF6F6F">Load</span>
        </div>
      </div>
    </section>

    <h2 id="planner">Tonight Planner</h2>
    <section class="planner-grid">
      {tonight_safe_html}
      {utility_hold_html}
      <div class="planner-card primary">
        <div class="label">Tonight Risk</div>
        <div class="value"><span class="badge {esc(tonight_badge_class)}">{esc(tonight_title)}</span></div>
        <div class="muted small">{esc(tonight_detail)}</div>
      </div>
      <div class="planner-card">
        <div class="label">Sunrise In</div>
        <div class="value">{esc(sunrise_display)}</div>
        <div class="muted small">includes configured location</div>
      </div>
      <div class="planner-card">
        <div class="label">Topup to Sunrise</div>
        <div class="value">{esc(topup_sunrise_display)}</div>
        <div class="muted small">recommended grid charge window</div>
      </div>
      <div class="planner-card">
        <div class="label">Preserve Threshold</div>
        <div class="value">{esc(threshold_display)}</div>
        <div class="muted small">{esc(threshold_reason)}</div>
      </div>
    </section>

    {pv_forecast_html}

    <div class="section-head" id="recommendations">
      <div>
        <h2>Recommendations</h2>
        <div class="muted">Ranked assistant suggestions with reason and expected impact.</div>
      </div>
    </div>
    <section class="card rec-section" aria-label="Recommendations">
      {recommendations_html}
    </section>

    <div class="section-head" id="automation">
      <div>
        <h2>System & Automation</h2>
        <div class="muted">Operational state, dashboard freshness, alerts, and integration health.</div>
      </div>
    </div>
    <section class="grid ops-grid">
      <div class="card">
        <div class="label">Dashboard Health</div>
        <div class="value">
          <span class="badge badge-ok" data-refresh-badge data-generated-at="{esc(generated_at_iso)}" data-stale-minutes="{esc(stale_minutes_text)}">OK</span>
        </div>
        <div class="muted small" data-refresh-age>Generated just now; stale after {esc(stale_minutes_text)} minutes.</div>
      </div>
      <div class="card">
        <div class="label">Next Automation</div>
        <div class="value">{esc(next_action_relative)}</div>
        <div class="muted small">{esc(next_action_title)} - {esc(next_action_detail)}</div>
      </div>
      <div class="card">
        <div class="label">Data Quality</div>
        <div class="value"><span class="badge {esc(quality_badge_class)}">{esc(quality_display)}</span></div>
        <div class="muted small">{esc(quality_detail)}</div>
      </div>
      <div class="card">
        <div class="label">Projected Sunrise SOC</div>
        <div class="value">{esc(tonight_projection_display)}</div>
        <div class="muted small">Topup estimate: {esc(tonight_topup_display)}</div>
      </div>
      <div class="card"><div class="label">Battery Voltage</div><div class="value">{esc(vbat)}</div></div>
      <div class="card"><div class="label">Current Load Runtime</div><div class="value">{esc(est_runtime)}</div><div class="muted small">{esc(runtime_note)}</div></div>
      <div class="card"><div class="label">Pause State</div><div class="value">{esc(pause)}</div></div>
      <div class="card"><div class="label">Emergency Alert</div><div class="value">{esc(alert)}</div></div>
      <div class="card"><div class="label">Cloud Streak</div><div class="value">{esc(cloud_streak)}</div></div>
      <div class="card"><div class="label">Today Override</div><div class="value">{esc(override_note)}</div></div>
      {pvoutput_card}
    </section>
    <section class="card timeline-card" aria-label="Today automation timeline">
      <div class="mix-header">
        <div>
          <div class="label">Today Automation</div>
          <div class="muted small">Current and upcoming jobs from the local schedule.</div>
        </div>
        <span class="pill">{len(schedule_timeline)} jobs</span>
      </div>
      <ol class="timeline-list">
        {timeline_items}
      </ol>
    </section>
    <section class="status-activity-grid" aria-label="System status and recent activity">
      <article class="card">
        <div class="label">System Status</div>
        <div class="muted small">Current operating signals and automation health.</div>
        <div class="status-list">
          {system_status_rows}
        </div>
      </article>
      <article class="card">
        <div class="label">Recent Activity</div>
        <div class="muted small">Latest mode decisions from the local audit trail.</div>
        <ul class="activity-list">
          {activity_items}
        </ul>
      </article>
    </section>
    <details class="detail-panel source-drawer">
      <summary><span>Metric source paths</span><span class="summary-meta">debug</span></summary>
      <div class="table-wrap"><table><thead><tr><th>Metric</th><th>Source</th></tr></thead><tbody>{source_rows_html}</tbody></table></div>
    </details>

    <h2 id="automation-history">Automation History</h2>
    <section class="chart-grid">
      <div class="card chart-card compact">
        <div class="label">7-Day History</div>
        <canvas id="history-chart"></canvas>
        <div class="legend">
          <span style="--c:#3AC87A">Preserve</span>
          <span style="--c:#F5A82A">Utility</span>
          <span style="--c:#5B8DEF">Watchdog</span>
        </div>
      </div>
    </section>
    <script id="chart-data" type="application/json">{chart_data_json}</script>
    <script id="metric-history-data" type="application/json">{metric_history_json}</script>
    <div class="section-head" id="operations">
      <div>
        <h2>Operations Details</h2>
        <div class="muted">Schedules, upcoming runs, audit rows, and low-level notes when you need to inspect them.</div>
      </div>
    </div>
    <section class="details-stack">
      <details class="detail-panel" open>
        <summary><span>Today&#8217;s Schedule - {esc(now.strftime('%A, %Y-%m-%d'))}</span><span class="summary-meta">{len(today_jobs)} jobs</span></summary>
        <div class="table-wrap"><table><thead><tr><th>Time</th><th>Job ID</th><th>Command</th><th>Status</th></tr></thead><tbody>{today_job_rows_html}</tbody></table></div>
      </details>
      {upcoming_override_section}
      <details class="detail-panel">
        <summary><span>Next Scheduled Jobs</span><span class="summary-meta">{len(next_runs)} queued</span></summary>
        <div class="table-wrap"><table><thead><tr><th>Time</th><th>ID</th><th>Name</th><th>Command</th></tr></thead><tbody>{next_rows}</tbody></table></div>
      </details>
      <details class="detail-panel">
        <summary><span>Recent Mode Decisions</span><span class="summary-meta">{len(last_actions)} rows</span></summary>
        <div class="table-wrap"><table><thead><tr><th>Time</th><th>Command</th><th>Action</th><th>SOC</th><th>Previous Mode</th></tr></thead><tbody>{action_rows}</tbody></table></div>
      </details>
      <details class="detail-panel">
        <summary><span>Automation Notes</span><span class="summary-meta">context</span></summary>
        <div class="card">
          <div>Threshold: {esc(threshold_reason)}</div>
          <div>Skipped today: {esc(skipped or 'none')}</div>
        </div>
      </details>
    </section>
    </div>
  </main>
  </div>
  <script>{DASHBOARD_JS}</script>
</body>
</html>
"""


def resolve_dashboard_output(output: str) -> Path:
    output_path = Path(output)
    if not output_path.is_absolute():
        output_path = BASE_DIR / output_path
    return output_path


def resolve_dashboard_json_output(output_path: Path) -> Path:
    return output_path.with_suffix(".json")


def _write_json_atomic(output_path: Path, payload: dict[str, Any]) -> None:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output_path.parent,
        prefix=".dash_tmp_", suffix=".json", delete=False,
    )
    try:
        json.dump(payload, tmp, indent=2, sort_keys=True, default=lambda o: o.isoformat() if isinstance(o, (dt.datetime, dt.date)) else str(o))
        tmp.write("\n")
        tmp.flush()
        tmp.close()
        Path(tmp.name).replace(output_path)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def write_dashboard_from_status(config: Any, status: dict[str, Any], output: str) -> Path:
    from growatt_guard.weather import hours_until_next_sunset
    from growatt_guard.state import read_utility_hold_state
    schedule = validate_schedule()
    overrides = validate_schedule_overrides(schedule)
    threshold_decision = choose_preserve_threshold(config)
    hrs_to_sunrise: float | None = None
    hrs_to_sunset: float | None = None
    try:
        hrs_to_sunrise = hours_until_next_sunrise(config)
    except Exception:  # noqa: BLE001
        pass
    try:
        hrs_to_sunset = hours_until_next_sunset(config)
    except Exception:  # noqa: BLE001
        pass
    output_path = resolve_dashboard_output(output)
    append_dashboard_metric_snapshot(status, now=dt.datetime.now().astimezone())
    metrics_history = read_dashboard_metrics_history()
    pv_forecast = get_pv_forecast(config)
    json_payload = build_dashboard_data_payload(
        status, schedule, overrides, threshold_decision, config.dashboard_stale_minutes,
        config.battery_capacity_wh, config.battery_bms_cutoff_soc,
        hrs_to_sunrise, config.battery_charge_rate_w,
        config.auto_topup_target_soc,
        config.auto_topup_solar_skip_min_margin_minutes,
        metrics_history,
        hours_to_sunset=hrs_to_sunset,
        pv_forecast=pv_forecast,
    )
    html_content = build_dashboard_html(
        status, schedule, overrides, threshold_decision, config.dashboard_stale_minutes,
        config.battery_capacity_wh, config.battery_bms_cutoff_soc,
        hrs_to_sunrise, config.battery_charge_rate_w,
        config.auto_topup_target_soc,
        config.auto_topup_solar_skip_min_margin_minutes,
        config.auto_topup_min_minutes,
        config.discord_topup_max_minutes,
        metrics_history,
        hours_to_sunset=hrs_to_sunset,
        tonight_floor_soc=getattr(config, "auto_topup_sunrise_floor_soc", 35.0),
        tonight_comfortable_soc=45.0,
        utility_hold_state=read_utility_hold_state(),
        pv_forecast=pv_forecast,
    )
    # Atomic write: temp file in same directory then rename to avoid serving
    # a partially written file when the browser auto-refreshes mid-write.
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output_path.parent,
        prefix=".dash_tmp_", suffix=".html", delete=False,
    )
    try:
        tmp.write(html_content)
        tmp.flush()
        tmp.close()
        Path(tmp.name).replace(output_path)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise
    _write_json_atomic(resolve_dashboard_json_output(output_path), json_payload)
    return output_path


def write_dashboard(config: Any, output: str) -> Path:
    _, _, status = load_context(config)
    return write_dashboard_from_status(config, status, output)


def command_dashboard(config: Any, output: str) -> int:
    output_path = write_dashboard(config, output)
    print(f"Wrote dashboard to {output_path}")
    return 0


def command_dashboard_refresh(config: Any, output: str, interval_minutes: float, once: bool = False) -> int:
    if not once and interval_minutes < MIN_DASHBOARD_REFRESH_MINUTES:
        raise app_module().GrowattGuardError(
            f"--interval-minutes must be at least {MIN_DASHBOARD_REFRESH_MINUTES} to avoid Growatt API overuse."
        )

    while True:
        try:
            output_path = write_dashboard(config, output)
        except Exception as exc:  # noqa: BLE001 - keep refresh service alive after transient failures
            logging.exception("Dashboard refresh failed")
            if once:
                raise
            app_module().notify_failure(config, "dashboard-refresh", str(exc))
        else:
            message = f"Dashboard refreshed: {output_path}"
            logging.info(message)
            print(message, flush=True)
            if once:
                return 0
        time.sleep(interval_minutes * 60)


def refresh_observability_once(config: Any, output: str) -> dict[str, Any]:
    _, _, status = load_context(config)
    output_path = write_dashboard_from_status(config, status, output)
    try:
        pvoutput_ok, pvoutput_message = publish_pvoutput_status_from_status(config, status)
    except Exception as exc:  # noqa: BLE001 - dashboard refresh should survive PVOutput issues
        logging.exception("PVOutput step failed during observability refresh")
        pvoutput_ok = False
        pvoutput_message = f"PVOutput failed: {exc}"
    return {
        "dashboard_path": output_path,
        "pvoutput_ok": pvoutput_ok,
        "pvoutput_message": pvoutput_message,
    }


def command_observability_refresh(config: Any, output: str, interval_minutes: float, loop: bool = False) -> int:
    if loop and interval_minutes < MIN_DASHBOARD_REFRESH_MINUTES:
        raise app_module().GrowattGuardError(
            f"--interval-minutes must be at least {MIN_DASHBOARD_REFRESH_MINUTES} to avoid Growatt API overuse."
        )

    while True:
        try:
            result = refresh_observability_once(config, output)
        except Exception as exc:  # noqa: BLE001 - keep loop service alive after transient failures
            logging.exception("Observability refresh failed")
            if not loop:
                raise
            app_module().notify_failure(config, "observability-refresh", str(exc))
        else:
            message = (
                f"Observability refreshed: dashboard={result['dashboard_path']}; "
                f"{result['pvoutput_message']}"
            )
            logging.info(message)
            print(message, flush=True)
            if not result["pvoutput_ok"]:
                logging.error("%s", result["pvoutput_message"])
                if not loop:
                    raise app_module().GrowattGuardError(str(result["pvoutput_message"]))
                app_module().notify_failure(config, "observability-refresh", str(result["pvoutput_message"]))
            if not loop:
                return 0
        time.sleep(interval_minutes * 60)


def command_dashboard_stale_alert(config: Any, output: str, max_age_minutes: float | None = None) -> int:
    app = app_module()
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
                if not app.send_discord_message(config, message):
                    raise app.GrowattGuardError("Dashboard stale alert could not be sent to Discord.")
                state["notified"] = True
                state["last_alert_at"] = utc_now().isoformat()
                write_dashboard_stale_alert_state(state)
            print(f"Dashboard stale alert already active: {freshness['reason']}.")
            return 0

        notified = False
        if config.discord_webhook_url and config.discord_notify_failure:
            if not app.send_discord_message(config, message):
                raise app.GrowattGuardError("Dashboard stale alert could not be sent to Discord.")
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
            app.send_discord_message(config, message)
        print(f"Dashboard stale alert cleared: {freshness['reason']}.")
        return 0

    print(f"Dashboard freshness OK: {freshness['reason']}.")
    return 0


def dashboard_asset_for_path(output_path: Path, request_path: str) -> tuple[int, str, bytes] | None:
    parsed_path = urllib.parse.urlsplit(request_path).path
    if parsed_path in {"/", "/dashboard.html"}:
        if not output_path.exists():
            body = (
                "<!doctype html><html><body><h1>Growatt Dashboard</h1>"
                "<p>Dashboard has not been generated yet.</p></body></html>"
            ).encode("utf-8")
            return 503, "text/html; charset=utf-8", body
        return 200, "text/html; charset=utf-8", output_path.read_bytes()

    if parsed_path == "/dashboard.json":
        json_path = resolve_dashboard_json_output(output_path)
        if not json_path.exists():
            body = json.dumps(
                {
                    "error": "dashboard_json_not_generated",
                    "message": "dashboard.json has not been generated yet.",
                },
                separators=(",", ":"),
            ).encode("utf-8")
            return 503, "application/json; charset=utf-8", body
        return 200, "application/json; charset=utf-8", json_path.read_bytes()

    return None


def make_dashboard_handler(output_path: Path):
    class DashboardHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            asset = dashboard_asset_for_path(output_path, self.path)
            if asset is None:
                self.send_error(404)
                return
            status_code, content_type, body = asset
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler API
            logging.info("Dashboard server: " + format, *args)

    return DashboardHandler


def command_serve_dashboard(config: Any, host: str, port: int, output: str) -> int:
    _ = config
    output_path = resolve_dashboard_output(output)
    handler = make_dashboard_handler(output_path)

    class ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    with ReusableThreadingTCPServer((host, port), handler) as server:
        print(f"Serving {output_path} at http://{host}:{port}/dashboard.html", flush=True)
        server.serve_forever()
    return 0
