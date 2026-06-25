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
from pathlib import Path
from typing import Any

from growatt_guard.audit import build_chart_data, read_mode_audit_rows
from growatt_guard.pvoutput import publish_pvoutput_status_from_status, read_pvoutput_state
from growatt_guard.growatt_api import (
    estimate_charge_time,
    estimate_runtime,
    estimate_topup_for_sunrise,
    deep_values,
    extract_battery_status,
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
DASHBOARD_METRICS_FILE = LOG_DIR / "dashboard_metrics.jsonl"
DASHBOARD_METRICS_RETENTION_DAYS = 8
MIN_DASHBOARD_REFRESH_MINUTES = 5

PV_POWER_KEYS = ("ppv", "ppvText", "pPv", "pvPower")
PV_POWER_CHANNEL_KEYS = ("pPv1", "pPv2", "ppv1", "ppv2", "pv1Power", "pv2Power")
PV_TODAY_KEYS = ("epvToday", "ePvToday", "epvTodayTotal")
PV_TODAY_CHANNEL_KEYS = ("epv1Today", "epv2Today", "ePv1Today", "ePv2Today")
PV_TOTAL_KEYS = ("epvTotalText", "ePvTotalText", "eTotalText", "epvTotal", "ePvTotal", "eTotal")
LOAD_POWER_KEYS = ("outPutPower", "outPutPower1", "activePower", "outPower")
LOAD_TODAY_KEYS = ("eLoadToday", "eLoadTodayText", "eloadToday", "eConsumptionToday", "consumptionToday")
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


def _metric_sum(status: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    total = 0.0
    found = False
    wanted = set(keys)
    for path, value in deep_values(status):
        if path.split(".")[-1] not in wanted:
            continue
        parsed = parse_number(value)
        if parsed is None:
            continue
        total += parsed
        found = True
    return total if found else None


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


def _metric_number_or_channel_sum(
    status: dict[str, Any],
    total_keys: tuple[str, ...],
    channel_keys: tuple[str, ...],
) -> float | None:
    total = _metric_number(status, total_keys)
    channel_total = _metric_sum(status, channel_keys)
    if channel_total is not None and (total is None or channel_total > total):
        return channel_total
    return total


def _metric_lifetime_text(status: dict[str, Any]) -> str:
    result = extract_first_metric(status, PV_TOTAL_KEYS)
    if result is None:
        return ""
    value, _ = result
    if isinstance(value, str) and any(ch.isalpha() for ch in value):
        return value
    parsed = parse_number(value)
    if parsed is None:
        return str(value)
    return f"{parsed:g} MWh"


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
    pv_w = _metric_number_or_channel_sum(status, PV_POWER_KEYS, PV_POWER_CHANNEL_KEYS)
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
        "pv_today_kwh": _rounded(_metric_number_or_channel_sum(status, PV_TODAY_KEYS, PV_TODAY_CHANNEL_KEYS), 2),
        "pv_total": _metric_lifetime_text(status),
        "load_w": _rounded(load_w, 0),
        "load_pct": _rounded(_metric_number(status, LOAD_PERCENT_KEYS), 0),
        "load_today_kwh": _rounded(_metric_number(status, LOAD_TODAY_KEYS), 2),
        "grid_w": _rounded(grid_w, 0),
        "grid_source": grid_source,
        "grid_today_kwh": _rounded(_metric_max(status, GRID_TODAY_KEYS), 2),
        "charge_w": _rounded(charge_w, 0),
        "charge_today_kwh": _rounded(_metric_number(status, CHARGE_TODAY_KEYS), 2),
        "discharge_w": _rounded(discharge_w, 0),
        "discharge_today_kwh": _rounded(_metric_number(status, DISCHARGE_TODAY_KEYS), 2),
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
        cron_expr = str(job.get("cron", ""))
        fires: list[dt.datetime] = []
        cursor = start
        while cursor < end:
            if cron_matches(cron_expr, cursor):
                fires.append(cursor)
            cursor += dt.timedelta(minutes=1)
        if not fires:
            continue
        cmd = " ".join(schedule_job_tokens(job, index))
        # Show interval label for sub-hourly repeating jobs
        parts = cron_expr.strip().split()
        if len(parts) == 5 and parts[0].startswith("*/") and parts[1] == "*":
            try:
                interval = int(parts[0][2:])
                time_str = f"every {interval} min"
            except ValueError:
                time_str = fires[0].strftime("%H:%M")
        else:
            time_str = fires[0].strftime("%H:%M")

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
    _out_w = extract_first_metric(status, ("outPutPower", "outPutPower1", "activePower"))
    _n = parse_number(_out_w[0]) if _out_w else None
    out_w = f"{_n:g} W" if _n is not None else "—"
    _load = extract_first_metric(status, ("loadPercent", "loadPercent1"))
    _n = parse_number(_load[0]) if _load else None
    load_pct = f"{_n:.0f}%" if _n is not None else "—"
    _pd = extract_first_metric(status, ("pDischarge", "pDischarge1"))
    _pc = extract_first_metric(status, ("pCharge", "pCharge1"))
    _pdv = parse_number(_pd[0]) if _pd else None
    _pcv = parse_number(_pc[0]) if _pc else None
    bat_w = "—"
    est_runtime = "—"
    if _pdv is not None or _pcv is not None:
        _bw = (_pdv or 0.0) - (_pcv or 0.0)
        _dir = "discharge" if _bw > 0 else ("charge" if _bw < 0 else "standby")
        bat_w = f"{abs(_bw):g} W {_dir}"
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
                    if auto_topup_min_minutes > 0 and topup_min < auto_topup_min_minutes:
                        topup_min = round(auto_topup_min_minutes)
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
    stale_minutes_text = f"{stale_after_minutes:g}"

    today_jobs = _today_job_rows(schedule, today_override, now.date())
    upcoming_overrides = _upcoming_override_rows(overrides, now.date())
    chart_data_json = json.dumps(build_chart_data(now=now))
    pvoutput_card = _pvoutput_card_html(read_pvoutput_state(), now)
    pv_power_display = _fmt_w(live_metrics.get("pv_w"))
    grid_power_display = _fmt_w(live_metrics.get("grid_w"))
    grid_source = str(live_metrics.get("grid_source") or "")
    load_power_display = _fmt_w(live_metrics.get("load_w"))
    charge_power_display = _fmt_w(live_metrics.get("charge_w"))
    discharge_power_display = _fmt_w(live_metrics.get("discharge_w"))
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
    grid_detail_parts = [f"today {grid_today_display}"]
    if grid_source:
        grid_detail_parts.append(f"source: {grid_source}")
    grid_detail = " / ".join(grid_detail_parts)
    pv_total_text = str(live_metrics.get("pv_total") or "").strip()

    parity_cards = "\n".join(
        [
            f'<div class="card accent-pv"><div class="label">PV Power</div><div class="value">{esc(pv_power_display)}</div><div class="muted small">today {esc(pv_today_display)}</div></div>',
            f'<div class="card accent-grid"><div class="label">Grid Import Now</div><div class="value">{esc(grid_power_display)}</div><div class="muted small">{esc(grid_detail)}</div></div>',
            f'<div class="card accent-load"><div class="label">Load Power</div><div class="value">{esc(load_power_display)}</div><div class="muted small">today {esc(load_today_display)}</div></div>',
            f'<div class="card accent-battery"><div class="label">Battery Charge</div><div class="value">{esc(charge_power_display)}</div><div class="muted small">today {esc(charge_today_display)}</div></div>',
            f'<div class="card accent-battery"><div class="label">Battery Discharge</div><div class="value">{esc(discharge_power_display)}</div><div class="muted small">today {esc(discharge_today_display)}</div></div>',
            f'<div class="card"><div class="label">Grid Import Today</div><div class="value">{esc(grid_today_display)}</div><div class="muted small">cumulative energy</div></div>',
        ]
    )
    if pv_total_text:
        parity_cards += (
            f'\n<div class="card"><div class="label">PV Lifetime</div>'
            f'<div class="value">{esc(pv_total_text)}</div><div class="muted small">from Growatt</div></div>'
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

    skip_all_banner = (
        '<div class="banner-warn">⚠ All automation jobs are skipped today'
        + (f" — {esc(override_note)}" if override_note != "none" else "")
        + "</div>"
        if today_override.get("skip_all")
        else ""
    )
    upcoming_override_section = (
        f"<h2>Upcoming Overrides</h2>"
        f'<table><thead><tr><th>Date</th><th>Note</th><th>Actions</th></tr></thead><tbody>{upcoming_override_rows_html}</tbody></table>'
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
    :root {{ color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; }}
    body {{ margin: 0; background: #f5f7f8; color: #172026; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 24px; }}
    h1 {{ font-size: 28px; margin: 0 0 4px; }}
    h2 {{ font-size: 18px; margin: 28px 0 12px; }}
    .muted {{ color: #64727d; font-size: 14px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-top: 20px; }}
    .flow-grid {{ display: grid; grid-template-columns: repeat(5, minmax(130px, 1fr)); gap: 12px; margin-top: 20px; align-items: stretch; }}
    .flow-node {{ background: #fff; border: 1px solid #dce3e8; border-radius: 8px; padding: 14px; min-height: 116px; position: relative; }}
    .flow-node::after {{ content: ""; position: absolute; top: 50%; right: -10px; width: 8px; height: 8px; border-top: 2px solid #8ea0ad; border-right: 2px solid #8ea0ad; transform: translateY(-50%) rotate(45deg); background: #f5f7f8; }}
    .flow-node:last-child::after {{ display: none; }}
    .flow-kicker {{ color: #64727d; font-size: 12px; text-transform: uppercase; letter-spacing: 0; }}
    .flow-value {{ font-size: 24px; font-weight: 800; margin-top: 10px; line-height: 1.12; overflow-wrap: anywhere; }}
    .flow-detail {{ color: #64727d; font-size: 13px; margin-top: 8px; }}
    .chart-grid {{ display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(300px, .9fr); gap: 12px; }}
    .chart-card canvas {{ width: 100%; height: 220px; display: block; }}
    .chart-card.compact canvas {{ height: 190px; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 10px; color: #64727d; font-size: 13px; }}
    .legend span::before {{ content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; background: var(--c); }}
    .card {{ background: #fff; border: 1px solid #dce3e8; border-radius: 8px; padding: 14px; }}
    .accent-pv {{ border-left: 4px solid #25b8c7; }}
    .accent-grid {{ border-left: 4px solid #f0b429; }}
    .accent-load {{ border-left: 4px solid #f97373; }}
    .accent-battery {{ border-left: 4px solid #4ade80; }}
    .label {{ color: #64727d; font-size: 12px; text-transform: uppercase; letter-spacing: 0; }}
    .value {{ font-size: 22px; font-weight: 700; margin-top: 8px; line-height: 1.15; overflow-wrap: anywhere; }}
    .small {{ font-size: 13px; margin-top: 8px; }}
    .badge {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 9px; font-size: 14px; font-weight: 800; }}
    .badge-ok {{ background: #dff6e8; color: #155f34; }}
    .badge-warn {{ background: #fff2cc; color: #775800; }}
    .banner-warn {{ background: #fff2cc; color: #775800; border-radius: 8px; padding: 10px 16px; margin: 20px 0 0; font-weight: 600; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #dce3e8; }}
    th, td {{ padding: 10px; border-bottom: 1px solid #e8eef2; text-align: left; font-size: 14px; }}
    th {{ background: #eef3f5; color: #34444f; }}
    tr:last-child td {{ border-bottom: 0; }}
    .status-ok {{ color: #155f34; font-weight: 600; }}
    .status-skip {{ color: #9a3526; font-weight: 600; }}
    .status-replace {{ color: #775800; font-weight: 600; }}
    @media (max-width: 880px) {{
      main {{ padding: 16px; }}
      .flow-grid, .chart-grid {{ grid-template-columns: 1fr; }}
      .flow-node::after {{ display: none; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>Growatt Dashboard</h1>
    <div class="muted">Generated {esc(generated_at.strftime('%Y-%m-%d %H:%M:%S %Z'))}</div>
    {skip_all_banner}
    <section class="flow-grid" aria-label="Live energy flow">
      <div class="flow-node accent-pv">
        <div class="flow-kicker">Solar</div>
        <div class="flow-value">{esc(pv_power_display)}</div>
        <div class="flow-detail">PV today {esc(pv_today_display)}</div>
      </div>
      <div class="flow-node">
        <div class="flow-kicker">Inverter</div>
        <div class="flow-value">{esc(mode)}</div>
        <div class="flow-detail">{esc(bat_status)}</div>
      </div>
      <div class="flow-node accent-load">
        <div class="flow-kicker">Load</div>
        <div class="flow-value">{esc(load_power_display)}</div>
        <div class="flow-detail">{esc(load_pct)} load</div>
      </div>
      <div class="flow-node accent-battery">
        <div class="flow-kicker">Battery</div>
        <div class="flow-value">{esc(soc)}</div>
        <div class="flow-detail">{esc(battery_flow_display)} {esc(battery_flow_dir)}</div>
      </div>
      <div class="flow-node accent-grid">
        <div class="flow-kicker">Grid</div>
        <div class="flow-value">{esc(grid_power_display)}</div>
        <div class="flow-detail">{esc(grid_source or 'not reported')}</div>
      </div>
    </section>
    <section class="grid">
      {parity_cards}
    </section>
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
      <div class="card"><div class="label">Battery Status</div><div class="value">{esc(bat_status)}</div></div>
      <div class="card"><div class="label">Battery Power</div><div class="value">{esc(bat_w)}</div></div>
      <div class="card"><div class="label">Battery Voltage</div><div class="value">{esc(vbat)}</div></div>
      <div class="card"><div class="label">Est. Runtime</div><div class="value">{esc(est_runtime)}</div></div>
      <div class="card"><div class="label">Sunrise In</div><div class="value">{esc(sunrise_display)}</div></div>
      <div class="card"><div class="label">Topup to Sunrise</div><div class="value">{esc(topup_sunrise_display)}</div></div>
      <div class="card"><div class="label">Output Power</div><div class="value">{esc(out_w)}</div></div>
      <div class="card"><div class="label">Load</div><div class="value">{esc(load_pct)}</div></div>
      <div class="card"><div class="label">Preserve Threshold</div><div class="value">{esc(f'{threshold_decision.threshold:g}%')}</div></div>
      <div class="card"><div class="label">Pause State</div><div class="value">{esc(pause)}</div></div>
      <div class="card"><div class="label">Emergency Alert</div><div class="value">{esc(alert)}</div></div>
      <div class="card"><div class="label">Cloud Streak</div><div class="value">{esc(cloud_streak)}</div></div>
      <div class="card"><div class="label">Today Override</div><div class="value">{esc(override_note)}</div></div>
      {pvoutput_card}
    </section>
    <h2>Today&#8217;s Schedule — {esc(now.strftime('%A, %Y-%m-%d'))}</h2>
    <table><thead><tr><th>Time</th><th>Job ID</th><th>Command</th><th>Status</th></tr></thead><tbody>{today_job_rows_html}</tbody></table>
    {upcoming_override_section}
    <h2>7-Day History</h2>
    <div class="card" style="padding:16px 20px;">
      <canvas id="history-chart" style="width:100%;height:160px;display:block;"></canvas>
    </div>
    <script id="chart-data" type="application/json">{chart_data_json}</script>
    <h2>Energy Trends</h2>
    <section class="chart-grid">
      <div class="card chart-card">
        <div class="label">Power Today</div>
        <canvas id="power-trend-chart"></canvas>
        <div class="legend">
          <span style="--c:#25b8c7">PV</span>
          <span style="--c:#f97373">Load</span>
          <span style="--c:#6366f1">Grid</span>
        </div>
      </div>
      <div class="card chart-card compact">
        <div class="label">Battery SOC</div>
        <canvas id="soc-trend-chart"></canvas>
        <div class="legend"><span style="--c:#4ade80">SOC</span></div>
      </div>
      <div class="card chart-card compact">
        <div class="label">7-Day Battery Energy</div>
        <canvas id="battery-energy-chart"></canvas>
        <div class="legend">
          <span style="--c:#4ade80">Charge</span>
          <span style="--c:#a58b27">Discharge</span>
        </div>
      </div>
      <div class="card chart-card compact">
        <div class="label">7-Day Supply Mix</div>
        <canvas id="supply-energy-chart"></canvas>
        <div class="legend">
          <span style="--c:#25b8c7">PV</span>
          <span style="--c:#f0b429">Grid</span>
          <span style="--c:#f97373">Load</span>
        </div>
      </div>
    </section>
    <script id="metric-history-data" type="application/json">{metric_history_json}</script>
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
          const COLORS = ["#3b82f6", "#f59e0b", "#ef4444"];
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
          [["Preserve checks", "#3b82f6"], ["Utility switches", "#f59e0b"], ["Watchdog repairs", "#ef4444"]].forEach(function (item, i) {{
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
          {{ color: "#25b8c7", values: data.power.pv_w || [] }},
          {{ color: "#f97373", values: data.power.load_w || [] }},
          {{ color: "#6366f1", values: data.power.grid_w || [] }}
        ], {{ suffix: "W", minMax: 1000 }});
        drawLineChart("soc-trend-chart", data.soc.labels || [], [
          {{ color: "#4ade80", values: data.soc.soc || [] }}
        ], {{ suffix: "%", minMax: 100 }});
        drawBarChart("battery-energy-chart", data.daily.labels || [], [
          {{ color: "#4ade80", values: data.daily.charge_kwh || [] }},
          {{ color: "#a58b27", values: data.daily.discharge_kwh || [] }}
        ], "kWh");
        drawBarChart("supply-energy-chart", data.daily.labels || [], [
          {{ color: "#25b8c7", values: data.daily.pv_kwh || [] }},
          {{ color: "#f0b429", values: data.daily.grid_kwh || [] }},
          {{ color: "#f97373", values: data.daily.load_kwh || [] }}
        ], "kWh");
      }} catch (e) {{ /* metric chart render failed */ }}
    }})();
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


def write_dashboard_from_status(config: Any, status: dict[str, Any], output: str) -> Path:
    schedule = validate_schedule()
    overrides = validate_schedule_overrides(schedule)
    threshold_decision = choose_preserve_threshold(config)
    hrs_to_sunrise: float | None = None
    try:
        hrs_to_sunrise = hours_until_next_sunrise(config)
    except Exception:  # noqa: BLE001
        pass
    output_path = resolve_dashboard_output(output)
    append_dashboard_metric_snapshot(status, now=dt.datetime.now().astimezone())
    metrics_history = read_dashboard_metrics_history()
    html_content = build_dashboard_html(
        status, schedule, overrides, threshold_decision, config.dashboard_stale_minutes,
        config.battery_capacity_wh, config.battery_bms_cutoff_soc,
        hrs_to_sunrise, config.battery_charge_rate_w,
        config.auto_topup_target_soc,
        config.auto_topup_solar_skip_min_margin_minutes,
        config.auto_topup_min_minutes,
        config.discord_topup_max_minutes,
        metrics_history,
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
