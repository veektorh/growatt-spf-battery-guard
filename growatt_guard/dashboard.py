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
from growatt_guard.pvoutput import publish_pvoutput_status_from_status, read_pvoutput_state
from growatt_guard.growatt_api import (
    PV_POWER_CHANNELS,
    PV_TODAY_CHANNELS,
    estimate_charge_time,
    estimate_runtime,
    estimate_topup_for_sunrise,
    deep_values,
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
from growatt_guard.weather import choose_preserve_threshold, hours_until_next_sunrise


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

    delta_text = ""
    if delta_pct is not None:
        delta_text = f", {delta_pct:+.0f}%"
    detail = (
        f"{_fmt_insight_value(float(current), unit)} vs "
        f"{_fmt_insight_value(baseline, unit)} same-time average "
        f"({sample_count} days{delta_text})."
    )
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
        _build_pace_item(live_metrics, baseline_rows, "pv_today_kwh", "PV pace", "kWh"),
        _build_pace_item(live_metrics, baseline_rows, "load_today_kwh", "Load pace", "kWh", lower_is_better=True),
        _build_pace_item(live_metrics, baseline_rows, "grid_today_kwh", "Grid pace", "kWh", lower_is_better=True),
        _build_pace_item(live_metrics, baseline_rows, "soc", "SOC position", "%"),
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

    soc_drop = (load_w * hours_to_sunrise / battery_capacity_wh) * 100.0
    projected_soc = max(0.0, soc - soc_drop)
    target_soc = max(battery_bms_cutoff_soc, auto_topup_target_soc)
    margin = projected_soc - target_soc
    margin_hours = hours_to_sunrise + max(0.0, auto_topup_solar_skip_min_margin_minutes) / 60.0
    topup_minutes = None
    if battery_charge_rate_w > 0:
        topup_minutes = estimate_topup_for_sunrise(
            soc,
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
        "load_w": round(load_w, 1),
        "load_source": source,
        "topup_minutes": topup_minutes,
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
    if not items:
        items.append("All key dashboard metrics are present.")

    return {
        "level": level,
        "title": title,
        "score": score,
        "missing": missing,
        "items": items,
    }


def build_dashboard_energy_balance(live_metrics: dict[str, Any]) -> dict[str, Any]:
    """Compare daily energy supply and demand when Growatt exposes enough fields."""
    supply_parts = [
        ("PV today", "pv_today_kwh"),
        ("grid import today", "grid_today_kwh"),
        ("battery discharge today", "discharge_today_kwh"),
    ]
    demand_parts = [
        ("load today", "load_today_kwh"),
        ("battery charge today", "charge_today_kwh"),
    ]
    required_parts = supply_parts + demand_parts
    missing = [
        label
        for label, key in required_parts
        if not isinstance(live_metrics.get(key), (int, float))
    ]
    if missing:
        return {
            "level": "unknown",
            "title": "Incomplete",
            "detail": "Missing: " + ", ".join(missing) + ".",
            "missing": missing,
            "supply_kwh": None,
            "demand_kwh": None,
            "difference_kwh": None,
            "difference_pct": None,
        }

    supply_kwh = sum(float(live_metrics[key]) for _, key in supply_parts)
    demand_kwh = sum(float(live_metrics[key]) for _, key in demand_parts)
    if supply_kwh <= 0 or demand_kwh <= 0:
        return {
            "level": "unknown",
            "title": "Incomplete",
            "detail": "Supply or demand is zero, so balance cannot be calculated.",
            "missing": [],
            "supply_kwh": round(supply_kwh, 2),
            "demand_kwh": round(demand_kwh, 2),
            "difference_kwh": round(supply_kwh - demand_kwh, 2),
            "difference_pct": None,
        }

    difference_kwh = supply_kwh - demand_kwh
    difference_pct = abs(difference_kwh) / max(supply_kwh, demand_kwh) * 100.0
    if difference_pct <= 15:
        level = "good"
        title = "Balanced"
    elif difference_pct <= 30:
        level = "watch"
        title = "Watch"
    else:
        level = "high"
        title = "Mismatch"

    return {
        "level": level,
        "title": title,
        "detail": (
            f"Supply {_fmt_kwh(supply_kwh)} vs demand {_fmt_kwh(demand_kwh)} "
            f"(gap {_fmt_kwh(abs(difference_kwh))}, {difference_pct:.0f}%)."
        ),
        "missing": [],
        "supply_kwh": round(supply_kwh, 2),
        "demand_kwh": round(demand_kwh, 2),
        "difference_kwh": round(difference_kwh, 2),
        "difference_pct": round(difference_pct, 1),
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
    energy_balance = build_dashboard_energy_balance(live_metrics)
    daily_mix = build_dashboard_daily_mix(live_metrics)
    next_action = build_dashboard_next_action(schedule, now=now)
    daily_insights = build_dashboard_daily_insights(live_metrics, metric_history, now=now)
    risk = build_tonight_risk(
        live_metrics,
        battery_capacity_wh,
        battery_bms_cutoff_soc,
        hours_to_sunrise,
        battery_charge_rate_w,
        auto_topup_target_soc,
        auto_topup_solar_skip_min_margin_minutes,
    )

    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "freshness": {"stale_after_minutes": stale_after_minutes},
        "live": live_metrics,
        "sources": sources,
        "quality": {"data": data_quality, "energy_balance": energy_balance},
        "insights": {"daily": daily_insights, "daily_mix": daily_mix},
        "planner": {"tonight_risk": risk},
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
    bat_status = extract_battery_status(status) or "—"
    _load = extract_first_metric(status, ("loadPercent", "loadPercent1"))
    _n = parse_number(_load[0]) if _load else None
    load_pct = f"{_n:.0f}%" if _n is not None else "—"
    _pd = extract_first_metric(status, ("pDischarge", "pDischarge1"))
    _pc = extract_first_metric(status, ("pCharge", "pCharge1"))
    _pdv = parse_number(_pd[0]) if _pd else None
    _pcv = parse_number(_pc[0]) if _pc else None
    est_runtime = "—"
    if _pdv is not None or _pcv is not None:
        _bw = (_pdv or 0.0) - (_pcv or 0.0)
        if battery_capacity_wh > 0 and soc_result:
            if _bw > 0:
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
    last_actions = read_mode_audit_rows(limit=8, newest_first=True)
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
    tonight_risk = build_tonight_risk(
        live_metrics,
        battery_capacity_wh,
        battery_bms_cutoff_soc,
        hours_to_sunrise,
        battery_charge_rate_w,
        auto_topup_target_soc,
        auto_topup_solar_skip_min_margin_minutes,
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
    grid_now_detail = "estimated from load + charge - PV" if grid_source == "estimated" else (grid_detail or "reported by Growatt")
    metric_sources = extract_dashboard_metric_sources(status)
    data_quality = build_dashboard_data_quality(live_metrics, metric_sources)
    energy_balance = build_dashboard_energy_balance(live_metrics)
    daily_mix = build_dashboard_daily_mix(live_metrics)
    daily_insights = build_dashboard_daily_insights(live_metrics, metric_history, now=now)
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
    balance_badge_class = _status_badge_class(str(energy_balance.get("level", "unknown")))
    balance_title = str(energy_balance.get("title", "Unknown"))
    balance_detail = str(energy_balance.get("detail", "Energy balance could not be calculated."))

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
    daily_mix_html = f"""
    <section class="daily-mix card" aria-label="Today energy mix">
      <div class="mix-header">
        <div>
          <div class="label">Today Mix</div>
          <div class="muted small">Where energy came from, where it went, and the battery net position.</div>
        </div>
        <span class="badge {esc(balance_badge_class)}">{esc(balance_title)}</span>
      </div>
      <div class="mix-grid">
        <div class="mix-panel">
          <div class="mix-row-head"><strong>Supply</strong><span>{esc(supply_total_display)}</span></div>
          <div class="mix-bar" aria-label="PV and grid supply mix">
            <span class="mix-segment primary" style="width:{esc(_mix_width('pv_supply_pct'))}%"></span>
            <span class="mix-segment neutral" style="width:{esc(_mix_width('grid_supply_pct'))}%"></span>
          </div>
          <div class="mix-legend">
            <div><span>PV</span><strong>{esc(pv_today_display)} - {esc(_mix_pct_display('pv_supply_pct'))}</strong></div>
            <div><span>Grid</span><strong>{esc(grid_today_display)} - {esc(_mix_pct_display('grid_supply_pct'))}</strong></div>
          </div>
        </div>
        <div class="mix-panel">
          <div class="mix-row-head"><strong>Demand</strong><span>{esc(demand_total_display)}</span></div>
          <div class="mix-bar" aria-label="Load and battery charging demand mix">
            <span class="mix-segment primary" style="width:{esc(_mix_width('load_demand_pct'))}%"></span>
            <span class="mix-segment neutral" style="width:{esc(_mix_width('charge_demand_pct'))}%"></span>
          </div>
          <div class="mix-legend">
            <div><span>House load</span><strong>{esc(load_today_display)} - {esc(_mix_pct_display('load_demand_pct'))}</strong></div>
            <div><span>Stored</span><strong>{esc(charge_today_display)} - {esc(_mix_pct_display('charge_demand_pct'))}</strong></div>
          </div>
        </div>
        <div class="mix-panel">
          <div class="mix-row-head"><strong>Battery</strong><span>{esc(battery_activity_display)}</span></div>
          <div class="mix-bar" aria-label="Battery charge and discharge mix">
            <span class="mix-segment primary" style="width:{esc(_mix_width('charge_battery_pct'))}%"></span>
            <span class="mix-segment neutral" style="width:{esc(_mix_width('discharge_battery_pct'))}%"></span>
          </div>
          <div class="mix-legend">
            <div><span>{esc(battery_net_title)}</span><strong>{esc(battery_net_display)}</strong></div>
            <div><span>Discharged</span><strong>{esc(discharge_today_display)} - {esc(_mix_pct_display('discharge_battery_pct'))}</strong></div>
          </div>
        </div>
      </div>
    </section>
"""
    next_action_relative = str(next_action.get("relative") or "none")
    next_action_title = str(next_action.get("title") or "No upcoming jobs")
    next_action_detail = str(next_action.get("detail") or "No scheduled jobs found.")
    overview_cards = "\n".join(
        [
            (
                f'<article class="card kpi-card"><div><div class="label">Battery SOC</div>'
                f'<div class="value">{esc(soc)}</div></div>'
                f'<div class="muted small">{esc(soc_health)} - {esc(mode)}</div></article>'
            ),
            (
                f'<article class="card kpi-card"><div><div class="label">PV Now</div>'
                f'<div class="value">{esc(pv_power_display)}</div></div>'
                f'<div class="muted small">{esc(pv_today_display)} generated today</div></article>'
            ),
            (
                f'<article class="card kpi-card"><div><div class="label">Load Now</div>'
                f'<div class="value">{esc(load_power_display)}</div></div>'
                f'<div class="muted small">{esc(load_today_display)} consumed today</div></article>'
            ),
            (
                f'<article class="card kpi-card"><div><div class="label">Grid Import Now</div>'
                f'<div class="value">{esc(grid_power_display)}</div></div>'
                f'<div class="muted small">{esc(grid_now_detail)}</div></article>'
            ),
            (
                f'<article class="card kpi-card"><div><div class="label">Next Automation</div>'
                f'<div class="value">{esc(next_action_relative)}</div></div>'
                f'<div class="muted small">{esc(next_action_title)}</div></article>'
            ),
        ]
    )
    insight_cards = "\n".join(
        (
            '<article class="card insight-card">'
            f'<div class="label">{esc(str(item.get("label", "")))}</div>'
            f'<div class="value"><span class="badge {esc(_status_badge_class(str(item.get("level", "unknown"))))}">'
            f'{esc(str(item.get("title", "Unknown")))}</span></div>'
            f'<div class="muted small">{esc(str(item.get("detail", "")))}</div>'
            "</article>"
        )
        for item in daily_insights.get("items", [])
        if isinstance(item, dict)
    )

    energy_cards = "\n".join(
        [
            (
                f'<article class="card metric-card accent-pv"><div class="metric-head">'
                f'<div><div class="label">PV Today</div><div class="value">{esc(pv_today_display)}</div></div></div>'
                f'<div class="metric-meter"><span style="width:{solar_share_width:.0f}%"></span></div>'
                f'<div class="muted small">Solar share of load: {esc(solar_share_display)}</div></article>'
            ),
            (
                f'<article class="card metric-card accent-grid"><div class="metric-head">'
                f'<div><div class="label">Grid Import Today</div><div class="value">{esc(grid_today_display)}</div></div></div>'
                f'<div class="metric-meter grid-meter"><span style="width:{grid_reliance_width:.0f}%"></span></div>'
                f'<div class="muted small">Grid reliance vs load: {esc(grid_reliance_display)}</div></article>'
            ),
            (
                f'<article class="card metric-card accent-load"><div class="metric-head">'
                f'<div><div class="label">Load Today</div><div class="value">{esc(load_today_display)}</div></div></div>'
                f'<div class="metric-meter load-meter"><span style="width:100%"></span></div>'
                f'<div class="muted small">Total house consumption</div></article>'
            ),
            (
                f'<article class="card metric-card accent-battery"><div class="metric-head">'
                f'<div><div class="label">Battery Charge Today</div><div class="value">{esc(charge_today_display)}</div></div></div>'
                f'<div class="metric-meter battery-meter"><span style="width:{battery_charge_share_width:.0f}%"></span></div>'
                f'<div class="muted small">Stored energy vs load: {esc(battery_charge_share_display)}</div></article>'
            ),
            (
                f'<article class="card metric-card accent-battery"><div class="metric-head">'
                f'<div><div class="label">Battery Discharge Today</div><div class="value">{esc(discharge_today_display)}</div></div></div>'
                f'<div class="metric-meter battery-meter"><span style="width:100%"></span></div>'
                f'<div class="muted small">Battery output to inverter</div></article>'
            ),
        ]
    )
    if pv_total_text:
        energy_cards += (
            f'\n<article class="card metric-card"><div class="metric-head"><div><div class="label">PV Lifetime</div>'
            f'<div class="value">{esc(pv_total_text)}</div></div></div>'
            f'<div class="muted small">Total production reported by Growatt</div></article>'
        )

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
    system_status_rows = "\n".join(
        (
            '<div class="status-row">'
            f'<span>{esc(label)}</span>'
            f'<span class="badge {esc(badge_class)}">{esc(value)}</span>'
            "</div>"
        )
        for label, value, badge_class in [
            ("Inverter Mode", mode, mode_badge_class),
            ("Dashboard", "OK", "badge-ok"),
            ("Data Quality", quality_display, quality_badge_class),
            ("Energy Balance", balance_title, balance_badge_class),
            ("Emergency Alert", alert, emergency_badge_class),
            ("Cloud Streak", str(cloud_streak), cloud_badge_class),
        ]
    )
    activity_items = "\n".join(
        (
            '<li class="activity-item">'
            '<div>'
            f'<strong>{esc(row.get("action", "") or row.get("command", "") or "mode decision")}</strong>'
            f'<span>{esc(row.get("timestamp", ""))}</span>'
            '</div>'
            f'<span class="summary-meta">SOC {esc(row.get("soc", "") or "--")}</span>'
            '</li>'
        )
        for row in last_actions[:5]
    )
    if not activity_items:
        activity_items = '<li class="activity-item muted">No recent mode decisions recorded.</li>'
    timeline_badges = {
        "next": "badge-ok",
        "monitoring": "badge-ok",
        "upcoming": "badge-warn",
        "passed": "badge-warn",
        "skipped": "badge-warn",
        "replaced": "badge-warn",
    }
    timeline_items = "\n".join(
        (
            f'<li class="timeline-item timeline-{esc(str(item.get("state", "unknown")))}">'
            '<div class="timeline-marker" aria-hidden="true"></div>'
            '<div class="timeline-main">'
            f'<strong>{esc(str(item.get("time", "--")))} - {esc(str(item.get("name", "")))}</strong>'
            f'<span>{esc(str(item.get("detail", "")))}</span>'
            '</div>'
            f'<span class="badge {esc(timeline_badges.get(str(item.get("state", "")), "badge-warn"))}">'
            f'{esc(str(item.get("status", "Unknown")))}</span>'
            '</li>'
        )
        for item in schedule_timeline[:8]
    )
    if not timeline_items:
        timeline_items = '<li class="timeline-item muted">No automation jobs scheduled today.</li>'
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

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>Growatt Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      --ink: #111827;
      --muted: #6b7280;
      --soft: #9ca3af;
      --surface: #f8fafc;
      --panel: #ffffff;
      --line: #e5e7eb;
      --line-strong: #d1d5db;
      --accent: #2563eb;
      --accent-soft: #eff6ff;
      --radius: 10px;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      background: var(--surface);
      color: var(--ink);
      font-size: 14px;
      line-height: 1.45;
    }}
    .app-shell {{
      display: grid;
      grid-template-columns: 248px minmax(0, 1fr);
      min-height: 100vh;
    }}
    .sidebar {{
      position: sticky;
      top: 0;
      height: 100vh;
      display: flex;
      flex-direction: column;
      padding: 24px 18px;
      background: var(--panel);
      border-right: 1px solid var(--line);
    }}
    .sidebar-brand {{ display: flex; align-items: center; gap: 12px; margin-bottom: 36px; }}
    .sidebar-title {{ font-weight: 760; font-size: 16px; }}
    .sidebar-nav {{ display: grid; gap: 4px; }}
    .sidebar-nav a {{
      display: flex;
      align-items: center;
      min-height: 38px;
      padding: 8px 10px;
      border-radius: 8px;
      color: #374151;
      text-decoration: none;
      font-weight: 620;
      font-size: 14px;
    }}
    .sidebar-nav a:hover, .sidebar-nav a.active {{ background: var(--accent-soft); color: #1d4ed8; }}
    .sidebar-status {{
      margin-top: auto;
      display: grid;
      gap: 12px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface);
    }}
    main {{ max-width: 1360px; width: 100%; margin: 0 auto; padding: 32px 32px 48px; }}
    h1 {{ font-size: clamp(28px, 4vw, 40px); line-height: 1.05; margin: 0; letter-spacing: 0; font-weight: 760; }}
    h2 {{ font-size: 18px; line-height: 1.3; margin: 40px 0 0; letter-spacing: 0; font-weight: 720; }}
    code {{ color: #374151; font-size: 12px; white-space: normal; overflow-wrap: anywhere; }}
    .muted {{ color: var(--muted); font-size: 14px; }}
    .small {{ font-size: 13px; margin-top: 8px; }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      margin-bottom: 32px;
    }}
    .brand {{ display: flex; align-items: center; gap: 12px; min-width: 0; }}
    .brand-mark {{
      width: 32px;
      height: 32px;
      border-radius: 8px;
      border: 1px solid var(--line-strong);
      background: var(--panel);
      position: relative;
      flex: 0 0 auto;
    }}
    .brand-mark::after {{
      content: "";
      position: absolute;
      inset: 10px;
      border-radius: 999px;
      background: var(--accent);
    }}
    .brand-title {{ font-weight: 720; font-size: 16px; }}
    .top-actions {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 32px;
      padding: 6px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel);
      color: #374151;
      font-size: 13px;
      font-weight: 620;
      white-space: nowrap;
    }}
    .hero-grid {{
      display: grid;
      grid-template-columns: minmax(340px, 0.9fr) minmax(520px, 1.35fr);
      gap: 16px;
      align-items: stretch;
    }}
    .hero-panel, .flow-stage, .card, table, .detail-panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
    }}
    .hero-panel {{
      padding: 24px;
      min-height: 360px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }}
    .hero-copy {{ display: grid; gap: 8px; }}
    .hero-kicker {{ color: var(--accent); font-size: 12px; font-weight: 720; text-transform: uppercase; letter-spacing: 0; }}
    .hero-subtitle {{ max-width: 620px; font-size: 15px; color: var(--muted); line-height: 1.55; }}
    .soc-command {{
      display: grid;
      grid-template-columns: 176px minmax(0, 1fr);
      gap: 24px;
      align-items: center;
      margin-top: 24px;
    }}
    .soc-ring {{
      width: min(176px, 52vw);
      aspect-ratio: 1;
      border-radius: 12px;
      display: grid;
      place-items: center;
      background: var(--surface);
      border: 1px solid var(--line);
    }}
    .soc-core {{ text-align: center; }}
    .soc-core strong {{ display: block; font-size: clamp(40px, 6vw, 56px); line-height: 0.95; letter-spacing: 0; font-weight: 760; }}
    .soc-core span {{ color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: 680; }}
    .mode-stack {{ display: grid; gap: 12px; min-width: 0; }}
    .mode-line {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }}
    .mode-value {{ font-size: 24px; line-height: 1.15; font-weight: 720; overflow-wrap: anywhere; }}
    .quick-stats {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 24px; }}
    .quick-stat {{
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 12px;
      background: var(--surface);
      min-width: 0;
    }}
    .quick-stat b {{ display: block; font-size: 20px; line-height: 1.1; overflow-wrap: anywhere; font-weight: 720; }}
    .quick-stat span {{ display: block; margin-top: 4px; color: var(--muted); font-size: 12px; }}
    .flow-stage {{ padding: 24px; min-height: 360px; }}
    .section-head, .flow-head {{ display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; margin: 40px 0 16px; }}
    .section-head h2, .flow-head h2 {{ margin: 0; }}
    .flow-map {{
      display: grid;
      grid-template-columns: repeat(5, minmax(112px, 1fr));
      gap: 12px;
      align-items: center;
      min-height: 260px;
    }}
    .flow-tile {{
      min-height: 144px;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 16px;
      background: var(--panel);
      display: grid;
      align-content: space-between;
      position: relative;
    }}
    .flow-tile::before {{ content: ""; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--accent); border-radius: 10px 0 0 10px; }}
    .flow-tile.solar, .flow-tile.grid-source, .flow-tile.inverter, .flow-tile.battery, .flow-tile.load {{ grid-column: auto; grid-row: auto; }}
    .flow-label {{ color: var(--muted); font-size: 12px; font-weight: 680; text-transform: uppercase; letter-spacing: 0; }}
    .flow-value {{ font-size: 26px; font-weight: 740; line-height: 1.05; margin-top: 8px; overflow-wrap: anywhere; }}
    .flow-detail {{ color: var(--muted); font-size: 13px; margin-top: 8px; }}
    .connector {{ display: none; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-top: 16px; }}
    .overview-grid {{ display: grid; grid-template-columns: repeat(5, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }}
    .daily-grid {{ grid-template-columns: repeat(auto-fit, minmax(224px, 1fr)); }}
    .daily-mix {{ display: grid; gap: 16px; margin-top: 16px; }}
    .mix-header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }}
    .mix-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
    .mix-panel {{
      min-width: 0;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--surface);
    }}
    .mix-row-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }}
    .mix-row-head strong {{ font-size: 15px; font-weight: 720; }}
    .mix-row-head span {{ color: var(--muted); font-size: 13px; font-weight: 640; white-space: nowrap; }}
    .mix-bar {{ display: flex; height: 10px; margin: 14px 0 12px; overflow: hidden; border-radius: 999px; background: #eef2f7; }}
    .mix-segment {{ display: block; height: 100%; }}
    .mix-segment.primary {{ background: var(--accent); }}
    .mix-segment.neutral {{ background: #cbd5e1; }}
    .mix-legend {{ display: grid; gap: 8px; }}
    .mix-legend div {{ display: flex; justify-content: space-between; gap: 12px; color: var(--muted); font-size: 12px; }}
    .mix-legend strong {{ color: var(--ink); font-weight: 680; text-align: right; }}
    .ops-grid {{ grid-template-columns: repeat(auto-fit, minmax(216px, 1fr)); }}
    .insight-grid {{ grid-template-columns: repeat(auto-fit, minmax(232px, 1fr)); }}
    .status-activity-grid {{ display: grid; grid-template-columns: minmax(280px, 0.9fr) minmax(320px, 1.1fr); gap: 12px; margin-top: 12px; }}
    .card {{ padding: 16px; }}
    .kpi-card {{ min-height: 132px; display: grid; align-content: space-between; gap: 10px; }}
    .metric-card {{ min-height: 148px; display: grid; align-content: space-between; gap: 12px; }}
    .insight-card {{ min-height: 132px; display: grid; align-content: space-between; gap: 8px; }}
    .metric-head {{ display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }}
    .metric-meter {{ height: 6px; border-radius: 999px; background: #eef2f7; overflow: hidden; }}
    .metric-meter span {{ display: block; height: 100%; max-width: 100%; background: var(--accent); border-radius: inherit; }}
    .grid-meter span, .load-meter span, .battery-meter span {{ background: var(--accent); }}
    .accent-pv, .accent-grid, .accent-load, .accent-battery {{ border-top: 1px solid var(--line); }}
    .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0; font-weight: 680; }}
    .value {{ font-size: 24px; font-weight: 740; margin-top: 8px; line-height: 1.08; overflow-wrap: anywhere; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 5px 9px;
      font-size: 12px;
      font-weight: 680;
      line-height: 1;
      border: 1px solid transparent;
    }}
    .badge-ok {{ background: var(--accent-soft); color: #1d4ed8; border-color: #bfdbfe; }}
    .badge-warn {{ background: #f9fafb; color: #374151; border-color: var(--line-strong); }}
    .badge-fail {{ background: #111827; color: #ffffff; border-color: #111827; }}
    .planner-grid {{ display: grid; grid-template-columns: minmax(260px, 0.9fr) repeat(3, minmax(160px, 1fr)); gap: 12px; margin-top: 16px; }}
    .planner-card {{ padding: 16px; background: #fff; border: 1px solid var(--line); border-radius: var(--radius); }}
    .planner-card.primary {{ background: var(--panel); color: var(--ink); border-color: var(--line-strong); }}
    .planner-card.primary .muted, .planner-card.primary .label {{ color: var(--muted); }}
    .banner-warn {{ background: #fff; color: #374151; border: 1px solid var(--line-strong); border-radius: var(--radius); padding: 12px 16px; margin: 16px 0 24px; font-weight: 620; }}
    .chart-grid {{ display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(320px, .9fr); gap: 12px; }}
    .chart-card canvas {{ width: 100%; height: 240px; display: block; }}
    .chart-card.compact canvas {{ height: 200px; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; color: var(--muted); font-size: 13px; }}
    .legend span::before {{ content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; background: var(--c); }}
    .table-wrap {{ overflow-x: auto; border-radius: var(--radius); border: 1px solid var(--line); background: #fff; margin-top: 12px; }}
    table {{ width: 100%; border-collapse: collapse; box-shadow: none; border: 0; min-width: 640px; }}
    th, td {{ padding: 12px 14px; border-bottom: 1px solid var(--line); text-align: left; font-size: 14px; vertical-align: top; }}
    th {{ background: #f9fafb; color: #4b5563; font-size: 12px; text-transform: uppercase; letter-spacing: 0; font-weight: 680; }}
    tr:last-child td {{ border-bottom: 0; }}
    .status-ok {{ color: #111827; font-weight: 680; }}
    .status-skip {{ color: #111827; font-weight: 680; }}
    .status-replace {{ color: #111827; font-weight: 680; }}
    .details-stack {{ display: grid; gap: 10px; margin-top: 16px; }}
    .detail-panel {{ padding: 0; overflow: hidden; }}
    .detail-panel summary {{
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 16px;
      font-weight: 680;
      color: #374151;
      list-style: none;
    }}
    .detail-panel summary::-webkit-details-marker {{ display: none; }}
    .detail-panel summary::after {{ content: "+"; color: var(--soft); font-weight: 720; }}
    .detail-panel[open] summary {{ border-bottom: 1px solid var(--line); }}
    .detail-panel[open] summary::after {{ content: "-"; }}
    .detail-panel .table-wrap {{ border: 0; border-radius: 0; margin-top: 0; }}
    .detail-panel .card {{ border: 0; border-radius: 0; }}
    .summary-meta {{ color: var(--muted); font-size: 12px; font-weight: 560; white-space: nowrap; }}
    .status-list {{ display: grid; gap: 10px; margin-top: 14px; }}
    .status-row {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 10px 0; border-bottom: 1px solid var(--line); }}
    .status-row:last-child {{ border-bottom: 0; padding-bottom: 0; }}
    .activity-list {{ list-style: none; padding: 0; margin: 14px 0 0; display: grid; gap: 10px; }}
    .activity-item {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 10px 0; border-bottom: 1px solid var(--line); }}
    .activity-item:last-child {{ border-bottom: 0; padding-bottom: 0; }}
    .activity-item strong {{ display: block; font-size: 14px; font-weight: 680; }}
    .activity-item span {{ display: block; margin-top: 3px; color: var(--muted); font-size: 12px; }}
    .timeline-card {{ margin-top: 12px; }}
    .timeline-list {{ list-style: none; padding: 0; margin: 14px 0 0; display: grid; gap: 0; }}
    .timeline-item {{
      position: relative;
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr) auto;
      gap: 12px;
      align-items: start;
      padding: 0 0 16px;
    }}
    .timeline-item::before {{
      content: "";
      position: absolute;
      left: 5px;
      top: 16px;
      bottom: 0;
      width: 1px;
      background: var(--line);
    }}
    .timeline-item:last-child {{ padding-bottom: 0; }}
    .timeline-item:last-child::before {{ display: none; }}
    .timeline-marker {{ width: 11px; height: 11px; margin-top: 4px; border-radius: 999px; border: 2px solid var(--accent); background: var(--panel); }}
    .timeline-passed .timeline-marker, .timeline-skipped .timeline-marker, .timeline-replaced .timeline-marker {{ border-color: var(--line-strong); }}
    .timeline-main {{ min-width: 0; }}
    .timeline-main strong {{ display: block; font-size: 14px; font-weight: 700; overflow-wrap: anywhere; }}
    .timeline-main span {{ display: block; margin-top: 3px; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }}
    @media (max-width: 1040px) {{
      .app-shell {{ grid-template-columns: 1fr; }}
      .sidebar {{
        position: static;
        height: auto;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }}
      .sidebar-brand {{ margin-bottom: 18px; }}
      .sidebar-nav {{ grid-template-columns: repeat(auto-fit, minmax(128px, 1fr)); }}
      .sidebar-status {{ margin-top: 18px; }}
      .overview-grid {{ grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }}
      .hero-grid, .chart-grid, .planner-grid, .flow-map, .status-activity-grid, .mix-grid {{ grid-template-columns: 1fr; }}
      .hero-panel, .flow-stage {{ min-height: auto; }}
    }}
    @media (max-width: 720px) {{
      .sidebar {{ padding: 18px 14px; }}
      main {{ padding: 20px 14px 36px; }}
      .topbar, .section-head, .flow-head {{ align-items: flex-start; flex-direction: column; }}
      .top-actions {{ justify-content: flex-start; }}
      .hero-panel, .flow-stage {{ padding: 16px; }}
      .soc-command {{ grid-template-columns: 1fr; gap: 16px; margin-top: 18px; }}
      .soc-ring {{ width: 100%; max-width: none; height: auto; min-height: 136px; aspect-ratio: auto; }}
      .mode-stack {{ gap: 9px; }}
      .mode-value {{ font-size: 20px; }}
      .quick-stats {{ grid-template-columns: 1fr; gap: 8px; margin-top: 16px; }}
      .quick-stat {{ padding: 10px; }}
      .quick-stat b {{ font-size: 18px; }}
      .quick-stat span {{ font-size: 11px; }}
      .flow-map {{ grid-template-rows: none; min-height: auto; }}
      table {{ min-width: 560px; }}
    }}
  </style>
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
      <a href="#daily">Daily Energy</a>
      <a href="#planner">Tonight Planner</a>
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
        <div class="hero-kicker">Local solar dashboard</div>
        <h1>Overview</h1>
        <div class="muted">Generated {esc(generated_at.strftime('%Y-%m-%d %H:%M:%S %Z'))}</div>
      </div>
      <div class="top-actions">
        <span class="pill">Mode: {esc(mode)}</span>
        <span class="pill">SOC: {esc(soc)}</span>
        <span class="pill">Refresh: 5min</span>
      </div>
    </header>
    {skip_all_banner}

    <section class="overview-grid" aria-label="Overview metrics">
      {overview_cards}
    </section>

    <section class="hero-grid" id="flow">
      <section class="hero-panel" aria-label="Battery command status">
        <div class="hero-copy">
          <div class="hero-kicker">Command status</div>
          <h2>Battery and mode</h2>
          <div class="hero-subtitle">SOC, inverter mode, load pressure, and sunrise survival in one glance.</div>
        </div>
        <div class="soc-command">
          <div class="soc-ring">
            <div class="soc-core">
              <strong>{esc(soc)}</strong>
              <span>Battery SOC</span>
            </div>
          </div>
          <div class="mode-stack">
            <div class="mode-line">
              <span class="badge {esc(soc_health_class)}">{esc(soc_health)}</span>
              <span class="badge {esc(mode_badge_class)}">{esc(mode)}</span>
            </div>
            <div class="mode-value">{esc(bat_status)}</div>
            <div class="muted">Battery flow is {esc(battery_flow_display)} {esc(battery_flow_dir)}. Load is {esc(load_power_display)} at {esc(load_pct)} inverter load.</div>
          </div>
        </div>
        <div class="quick-stats">
          <div class="quick-stat"><b>{esc(pv_cover_display)}</b><span>solar covering live load</span></div>
          <div class="quick-stat"><b>{esc(grid_reliance_display)}</b><span>grid reliance today</span></div>
          <div class="quick-stat"><b>{esc(tonight_projection_display)}</b><span>projected sunrise SOC</span></div>
        </div>
      </section>

      <section class="flow-stage" aria-label="Live energy flow">
        <div class="flow-head">
          <div>
            <h2>Live energy flow</h2>
            <div class="muted">Solar, grid, inverter, battery, and load right now.</div>
          </div>
          <span class="badge {esc(tonight_badge_class)}">Tonight: {esc(tonight_title)}</span>
        </div>
        <div class="flow-map">
          <div class="flow-tile solar">
            <div>
              <div class="flow-label">Solar Now</div>
              <div class="flow-value">{esc(pv_power_display)}</div>
            </div>
            <div class="flow-detail">{esc(pv_today_display)} generated today</div>
          </div>
          <div class="connector pv" aria-hidden="true"></div>
          <div class="flow-tile inverter">
            <div>
              <div class="flow-label">Inverter</div>
              <div class="flow-value">{esc(mode)}</div>
            </div>
            <div class="flow-detail">{esc(bat_status)}</div>
          </div>
          <div class="connector battery" aria-hidden="true"></div>
          <div class="flow-tile battery">
            <div>
              <div class="flow-label">Battery</div>
              <div class="flow-value">{esc(soc)}</div>
            </div>
            <div class="flow-detail">{esc(battery_flow_display)} {esc(battery_flow_dir)}</div>
          </div>
          <div class="flow-tile grid-source">
            <div>
              <div class="flow-label">Grid Import Now</div>
              <div class="flow-value">{esc(grid_power_display)}</div>
            </div>
            <div class="flow-detail">{esc(grid_now_detail)}</div>
          </div>
          <div class="connector grid" aria-hidden="true"></div>
          <div class="connector load" aria-hidden="true"></div>
          <div class="flow-tile load">
            <div>
              <div class="flow-label">Load Now</div>
              <div class="flow-value">{esc(load_power_display)}</div>
            </div>
            <div class="flow-detail">{esc(load_today_display)} consumed today</div>
          </div>
        </div>
      </section>
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

    <div class="section-head">
      <div>
        <h2>Energy Insights</h2>
        <div class="muted">Same-time comparison against recent local history, without extra Growatt calls.</div>
      </div>
      <span class="badge {esc(_status_badge_class(str(daily_insights.get("status", "unknown"))))}">{esc(str(daily_insights.get("title", "Learning")))}</span>
    </div>
    <section class="grid insight-grid">
      {insight_cards}
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
        <div class="value">{esc(f'{threshold_decision.threshold:g}%')}</div>
        <div class="muted small">{esc(threshold_decision.reason)}</div>
      </div>
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
        <div class="label">Energy Balance</div>
        <div class="value"><span class="badge {esc(balance_badge_class)}">{esc(balance_title)}</span></div>
        <div class="muted small">{esc(balance_detail)}</div>
      </div>
      <div class="card">
        <div class="label">Projected Sunrise SOC</div>
        <div class="value">{esc(tonight_projection_display)}</div>
        <div class="muted small">Topup estimate: {esc(tonight_topup_display)}</div>
      </div>
      <div class="card"><div class="label">Battery Voltage</div><div class="value">{esc(vbat)}</div></div>
      <div class="card"><div class="label">Est. Runtime</div><div class="value">{esc(est_runtime)}</div></div>
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

    <h2 id="trends">Energy Trends</h2>
    <section class="chart-grid">
      <div class="card chart-card">
        <div class="label">Power Today</div>
        <canvas id="power-trend-chart"></canvas>
        <div class="legend">
          <span style="--c:#2563eb">PV</span>
          <span style="--c:#6b7280">Load</span>
          <span style="--c:#111827">Grid</span>
        </div>
      </div>
      <div class="card chart-card compact">
        <div class="label">Battery SOC</div>
        <canvas id="soc-trend-chart"></canvas>
        <div class="legend"><span style="--c:#2563eb">SOC</span></div>
      </div>
      <div class="card chart-card compact">
        <div class="label">7-Day Battery Energy</div>
        <canvas id="battery-energy-chart"></canvas>
        <div class="legend">
          <span style="--c:#2563eb">Charge</span>
          <span style="--c:#6b7280">Discharge</span>
        </div>
      </div>
      <div class="card chart-card compact">
        <div class="label">7-Day Supply Mix</div>
        <canvas id="supply-energy-chart"></canvas>
        <div class="legend">
          <span style="--c:#2563eb">PV</span>
          <span style="--c:#111827">Grid</span>
          <span style="--c:#6b7280">Load</span>
        </div>
      </div>
      <div class="card chart-card compact">
        <div class="label">7-Day History</div>
        <canvas id="history-chart"></canvas>
        <div class="legend">
          <span style="--c:#2563eb">Preserve</span>
          <span style="--c:#6b7280">Utility</span>
          <span style="--c:#111827">Watchdog</span>
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
          <div>Threshold: {esc(threshold_decision.reason)}</div>
          <div>Skipped today: {esc(skipped or 'none')}</div>
        </div>
      </details>
    </section>
  </main>
  </div>
  <script>
    (function () {{
      const canvas = document.getElementById("history-chart");
      const dataEl = document.getElementById("chart-data");
      if (canvas && dataEl) {{
        try {{
          const data = JSON.parse(dataEl.textContent);
          const ctx = canvas.getContext("2d");
          const dpr = window.devicePixelRatio || 1;
          const rect = canvas.getBoundingClientRect();
          canvas.width = rect.width * dpr || 600 * dpr;
          canvas.height = 160 * dpr;
          ctx.scale(dpr, dpr);
          const W = canvas.width / dpr, H = 160;
          const PAD = {{ top: 12, right: 12, bottom: 28, left: 32 }};
          const chartW = W - PAD.left - PAD.right;
          const chartH = H - PAD.top - PAD.bottom;
          const n = data.labels.length;
          const maxVal = Math.max(1, ...data.preserve_checks, ...data.utility_switches, ...data.watchdog_repairs);
          const yStep = Math.ceil(maxVal / 4);
          ctx.font = "11px system-ui, sans-serif";
          ctx.fillStyle = "#64727d";
          for (let y = 0; y <= maxVal; y += yStep) {{
            const px = PAD.top + chartH - (y / maxVal) * chartH;
            ctx.fillText(y, 0, px + 4);
            ctx.strokeStyle = "#e8eef2"; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(PAD.left, px); ctx.lineTo(PAD.left + chartW, px); ctx.stroke();
          }}
          const groupW = chartW / n;
          const barW = Math.max(4, groupW / 4 - 2);
          const COLORS = ["#2563eb", "#6b7280", "#111827"];
          const SERIES = ["preserve_checks", "utility_switches", "watchdog_repairs"];
          SERIES.forEach(function (key, si) {{
            ctx.fillStyle = COLORS[si];
            data[key].forEach(function (val, i) {{
              const x = PAD.left + i * groupW + si * (barW + 2) + (groupW - SERIES.length * (barW + 2)) / 2;
              const barH = (val / maxVal) * chartH;
              ctx.fillRect(x, PAD.top + chartH - barH, barW, barH || 1);
            }});
          }});
          data.labels.forEach(function (label, i) {{
            ctx.fillStyle = "#64727d";
            const x = PAD.left + i * groupW + groupW / 2;
            ctx.textAlign = "center";
            ctx.fillText(label, x, H - 6);
          }});
          ctx.textAlign = "left";
          const legendY = PAD.top; const legendX = PAD.left + chartW - 200;
          [["Preserve checks", "#2563eb"], ["Utility switches", "#6b7280"], ["Watchdog repairs", "#111827"]].forEach(function (item, i) {{
            ctx.fillStyle = item[1];
            ctx.fillRect(legendX + i * 70, legendY, 8, 8);
            ctx.fillStyle = "#64727d";
            ctx.fillText(item[0].split(" ")[0], legendX + i * 70 + 11, legendY + 8);
          }});
        }} catch (e) {{ /* chart render failed */ }}
      }}
    }})();
    (function () {{
      const dataEl = document.getElementById("metric-history-data");
      if (!dataEl) return;

      function clean(values) {{
        return values.map(function (v) {{ return typeof v === "number" && isFinite(v) ? v : null; }});
      }}

      function setupCanvas(id) {{
        const canvas = document.getElementById(id);
        if (!canvas) return null;
        const ctx = canvas.getContext("2d");
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.getBoundingClientRect();
        const width = rect.width || 600;
        const height = rect.height || 220;
        canvas.width = width * dpr;
        canvas.height = height * dpr;
        ctx.scale(dpr, dpr);
        return {{ canvas, ctx, width, height }};
      }}

      function noData(ctx, width, height) {{
        ctx.fillStyle = "#64727d";
        ctx.font = "13px system-ui, sans-serif";
        ctx.fillText("No local history yet", 18, height / 2);
      }}

      function drawGrid(ctx, width, height, pad, maxVal, suffix) {{
        ctx.font = "11px system-ui, sans-serif";
        ctx.fillStyle = "#64727d";
        ctx.strokeStyle = "#e8eef2";
        ctx.lineWidth = 1;
        for (let i = 0; i <= 4; i++) {{
          const y = pad.top + ((height - pad.top - pad.bottom) / 4) * i;
          const val = maxVal - (maxVal / 4) * i;
          ctx.beginPath();
          ctx.moveTo(pad.left, y);
          ctx.lineTo(width - pad.right, y);
          ctx.stroke();
          ctx.fillText(Math.round(val) + suffix, 6, y + 4);
        }}
      }}

      function drawLineChart(id, labels, series, options) {{
        const setup = setupCanvas(id);
        if (!setup) return;
        const {{ ctx, width, height }} = setup;
        const pad = {{ top: 14, right: 16, bottom: 28, left: 48 }};
        const values = series.flatMap(function (s) {{ return clean(s.values).filter(function (v) {{ return v !== null; }}); }});
        if (labels.length < 2 || values.length === 0) {{
          noData(ctx, width, height);
          return;
        }}
        const maxVal = Math.max(options.minMax || 1, ...values);
        drawGrid(ctx, width, height, pad, maxVal, options.suffix || "");
        const chartW = width - pad.left - pad.right;
        const chartH = height - pad.top - pad.bottom;
        series.forEach(function (s) {{
          const vals = clean(s.values);
          ctx.strokeStyle = s.color;
          ctx.lineWidth = 2;
          ctx.beginPath();
          let started = false;
          vals.forEach(function (value, index) {{
            if (value === null) return;
            const x = pad.left + (chartW * index) / Math.max(1, labels.length - 1);
            const y = pad.top + chartH - (value / maxVal) * chartH;
            if (!started) {{
              ctx.moveTo(x, y);
              started = true;
            }} else {{
              ctx.lineTo(x, y);
            }}
          }});
          ctx.stroke();
        }});
        ctx.fillStyle = "#64727d";
        ctx.font = "11px system-ui, sans-serif";
        ctx.textAlign = "left";
        ctx.fillText(labels[0] || "", pad.left, height - 8);
        ctx.textAlign = "right";
        ctx.fillText(labels[labels.length - 1] || "", width - pad.right, height - 8);
        ctx.textAlign = "left";
      }}

      function drawBarChart(id, labels, series, suffix) {{
        const setup = setupCanvas(id);
        if (!setup) return;
        const {{ ctx, width, height }} = setup;
        const pad = {{ top: 14, right: 16, bottom: 34, left: 44 }};
        const values = series.flatMap(function (s) {{ return clean(s.values).filter(function (v) {{ return v !== null; }}); }});
        if (labels.length === 0 || values.length === 0) {{
          noData(ctx, width, height);
          return;
        }}
        const maxVal = Math.max(1, ...values);
        drawGrid(ctx, width, height, pad, maxVal, suffix || "");
        const chartW = width - pad.left - pad.right;
        const chartH = height - pad.top - pad.bottom;
        const groupW = chartW / labels.length;
        const barW = Math.max(5, groupW / (series.length + 1) - 4);
        series.forEach(function (s, si) {{
          ctx.fillStyle = s.color;
          clean(s.values).forEach(function (value, i) {{
            if (value === null) return;
            const x = pad.left + i * groupW + si * (barW + 4) + (groupW - series.length * (barW + 4)) / 2;
            const barH = (value / maxVal) * chartH;
            ctx.fillRect(x, pad.top + chartH - barH, barW, Math.max(1, barH));
          }});
        }});
        ctx.fillStyle = "#64727d";
        ctx.font = "11px system-ui, sans-serif";
        ctx.textAlign = "center";
        labels.forEach(function (label, i) {{
          ctx.fillText(label, pad.left + i * groupW + groupW / 2, height - 10);
        }});
        ctx.textAlign = "left";
      }}

      try {{
        const data = JSON.parse(dataEl.textContent);
        drawLineChart("power-trend-chart", data.power.labels || [], [
          {{ color: "#2563eb", values: data.power.pv_w || [] }},
          {{ color: "#6b7280", values: data.power.load_w || [] }},
          {{ color: "#111827", values: data.power.grid_w || [] }}
        ], {{ suffix: "W", minMax: 1000 }});
        drawLineChart("soc-trend-chart", data.soc.labels || [], [
          {{ color: "#2563eb", values: data.soc.soc || [] }}
        ], {{ suffix: "%", minMax: 100 }});
        drawBarChart("battery-energy-chart", data.daily.labels || [], [
          {{ color: "#2563eb", values: data.daily.charge_kwh || [] }},
          {{ color: "#6b7280", values: data.daily.discharge_kwh || [] }}
        ], "kWh");
        drawBarChart("supply-energy-chart", data.daily.labels || [], [
          {{ color: "#2563eb", values: data.daily.pv_kwh || [] }},
          {{ color: "#111827", values: data.daily.grid_kwh || [] }},
          {{ color: "#6b7280", values: data.daily.load_kwh || [] }}
        ], "kWh");
      }} catch (e) {{ /* metric chart render failed */ }}
    }})();
    (function () {{
      const badge = document.querySelector("[data-refresh-badge]");
      const ageNodes = Array.from(document.querySelectorAll("[data-refresh-age]"));
      if (!badge || ageNodes.length === 0) return;

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
          ageNodes.forEach(function (node) {{ node.textContent = "Generated time could not be read."; }});
          return;
        }}
        const ageMs = Date.now() - generatedAt.getTime();
        const stale = ageMs > staleMinutes * 60 * 1000;
        badge.textContent = stale ? "STALE" : "OK";
        badge.className = "badge " + (stale ? "badge-warn" : "badge-ok");
        ageNodes.forEach(function (node) {{
          node.textContent = "Generated " + formatAge(ageMs) + " ago; stale after " + staleMinutes + " minutes.";
        }});
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
    json_payload = build_dashboard_data_payload(
        status, schedule, overrides, threshold_decision, config.dashboard_stale_minutes,
        config.battery_capacity_wh, config.battery_bms_cutoff_soc,
        hrs_to_sunrise, config.battery_charge_rate_w,
        config.auto_topup_target_soc,
        config.auto_topup_solar_skip_min_margin_minutes,
        metrics_history,
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
