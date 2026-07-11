from __future__ import annotations
import datetime as dt
import json
import tempfile
from pathlib import Path
from typing import Any, TypedDict

from growatt_guard.growatt_api import (
    PV_POWER_CHANNELS, PV_TODAY_CHANNELS, deep_values,
    detect_unexpected_grid_bypass, extract_battery_status,
    extract_channel_metric_sum, extract_first_metric, extract_soc,
    extract_spf_output_source, parse_number,
)

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

class DashboardMetrics(TypedDict, total=False):
    """Normalized telemetry contract shared by dashboard policy and rendering."""

    timestamp: str
    soc: float | None
    mode_raw: str
    mode: str
    battery_status: str
    bypass_detected: bool
    pv_w: float | None
    pv_today_kwh: float | None
    load_w: float | None
    load_today_kwh: float | None
    grid_w: float | None
    grid_today_kwh: float | None
    charge_w: float | None
    charge_today_kwh: float | None
    discharge_w: float | None
    discharge_today_kwh: float | None
    battery_net_w: float | None
    vbat: float | None



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


def extract_dashboard_metrics(status: dict[str, Any], now: dt.datetime | None = None) -> DashboardMetrics:
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


