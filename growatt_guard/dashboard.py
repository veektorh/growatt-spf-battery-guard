from __future__ import annotations

import datetime as dt
import html
import http.server
import json
import logging
import socketserver
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

from growatt_guard.audit import build_chart_data, read_mode_audit_rows
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.notifications import notify_failure, send_discord_message
from growatt_guard.operational_status import build_sbu_guard_status
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
from growatt_guard.dashboard_metrics import (
    _history_with_live,
    _metric_date,
    _parse_metric_timestamp,
    _series_value,
    _fmt_g,
    _fmt_kwh,
    _fmt_pct,
    _fmt_volts,
    _fmt_w,
    append_dashboard_metric_snapshot,
    build_dashboard_history_payload,
    extract_dashboard_metric_sources,
    extract_dashboard_metrics,
    read_dashboard_metrics_history,
)
from growatt_guard.dashboard_insights import (
    build_dashboard_daily_insights,
    build_dashboard_data_quality,
    build_dashboard_energy_reconciliation,
    build_tonight_risk,
    compute_tonight_safe,
)
from growatt_guard.dashboard_planning import (
    _project_sunset_soc,
    _should_use_projected_sunset_start,
    build_dashboard_assistant_summary,
    build_dashboard_daily_mix,
    build_dashboard_energy_outlook,
    build_dashboard_home_status,
    _numeric_metric,
    build_dashboard_next_action,
    build_dashboard_recommendations,
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
from growatt_guard.dashboard_viewmodel import (
    _today_job_rows,
    _upcoming_override_rows,
    build_dashboard_data_payload,
    build_dashboard_schedule_timeline,
    dashboard_freshness,
)
from growatt_guard.dashboard_render_components import (
    _glance_card,
    _inline_badge,
    _status_badge_class,
    _metric_card,
    _pvoutput_card_html,
    _render_activity_items,
    _render_daily_mix,
    _render_energy_outlook,
    _render_insight_cards,
    _render_night_view,
    _render_status_rows,
    _render_timeline_items,
    _stat_block,
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
    min_sbu_return_soc: float = 30.0,
    dashboard_data: dict[str, Any] | None = None,
) -> str:
    if dashboard_data is None:
        now = dt.datetime.now()
        generated_at = now.astimezone()
        generated_at_iso = generated_at.isoformat(timespec="seconds")
        live_metrics = extract_dashboard_metrics(status, now=generated_at)
        metric_history = _history_with_live(metrics_history or [], live_metrics)
        metric_history_json = json.dumps(build_dashboard_history_payload(metric_history, now=now))
    else:
        generated_at_iso = str(dashboard_data["generated_at"])
        generated_at = dt.datetime.fromisoformat(generated_at_iso)
        now = generated_at.replace(tzinfo=None)
        live_metrics = dict(dashboard_data["live"])
        metric_history = []
        metric_history_json = json.dumps(dashboard_data["history"])
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
    if dashboard_data is None:
        sbu_guard = build_sbu_guard_status(
            min_sbu_return_soc,
            audit_rows=last_actions,
            utility_hold=utility_hold_state,
        )
    else:
        sbu_guard = dashboard_data["automation"]["sbu_return_guard"]
    next_runs = next_scheduled_runs(schedule, now=now, limit=8)
    next_action = (
        dashboard_data["schedule"]["next_action"]
        if dashboard_data is not None
        else build_dashboard_next_action(schedule, now=now)
    )
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
    if dashboard_data is not None:
        tonight_risk = dashboard_data["planner"]["tonight_risk"]
    else:
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
    if dashboard_data is None:
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
    else:
        metric_sources = dashboard_data["sources"]
        data_quality = dashboard_data["quality"]["data"]
        daily_mix = dashboard_data["insights"]["daily_mix"]
        daily_insights = dashboard_data["insights"]["daily"]
        energy_outlook = dashboard_data["planner"]["outlook"]
    threshold_display = _fmt_g(getattr(threshold_decision, "threshold", None), "%")
    threshold_reason = str(getattr(threshold_decision, "reason", "") or "Weather signal is unavailable.")
    if dashboard_data is None:
        home_status = build_dashboard_home_status(
            live_metrics, mode, battery_flow_dir, tonight_risk, next_action, now=now,
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
    else:
        home_status = dashboard_data["assistant"]["status"]
        recommendations = dashboard_data["assistant"]["recommendations"]
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
    _calibration = pv_forecast.get("calibration") if pv_forecast else None
    _calibration_value = "Learning"
    _calibration_detail = "Collecting five completed forecast days before suggesting changes."
    if isinstance(_calibration, dict):
        _calibration_samples = int(_calibration.get("sample_count") or 0)
        _calibration_error = _calibration.get("mean_absolute_error_kwh")
        _calibration_confidence = str(_calibration.get("confidence") or "learning").title()
        _calibration_value = f"{_calibration_confidence} ({_calibration_samples}d)"
        _calibration_detail = str(_calibration.get("recommendation") or _calibration_detail)
        if _calibration_samples > 0 and isinstance(_calibration_error, (int, float)):
            _tomorrow_pv_detail += (
                f" Calibration: {_calibration_samples} completed days, "
                f"{_fmt_kwh(float(_calibration_error))} mean absolute error."
            )
        else:
            _tomorrow_pv_detail += " Calibration is collecting completed forecast days."
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
            ("Forecast Calibration", _calibration_value, _calibration_detail),
            (
                "SBU Return Guard",
                f"{sbu_guard['minimum_soc']:g}% {sbu_guard['state']}",
                str(sbu_guard["detail"]),
            ),
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
                    ("Usable", usable_kwh_display),
                    ("Flow", f"{battery_power_label} {battery_flow_display}"),
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
                    ("Live load cover", pv_cover_display),
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
                    ("Bypass", bypass_badge_label),
                    ("Mode", mode),
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
    guard_badge_class = "badge-fail" if sbu_guard["state"] == "misconfigured" else (
        "badge-warn" if sbu_guard["state"] in {"disabled", "blocked_hold"} else "badge-ok"
    )
    system_status_rows = _render_status_rows(
        [
            ("Inverter Mode", mode, mode_badge_class),
            ("Grid Bypass", bypass_badge_label, bypass_badge_class),
            ("Dashboard", "OK", "badge-ok"),
            ("Data Quality", quality_display, quality_badge_class),
            ("Emergency Alert", alert, emergency_badge_class),
            ("Cloud Streak", str(cloud_streak), cloud_badge_class),
            ("SBU Guard", f"{sbu_guard['minimum_soc']:g}% {sbu_guard['state']}", guard_badge_class),
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

    <details class="detail-panel reserve-details" aria-label="Reserve details and supporting signals">
        <summary>
          <span>
            <span class="label">Reserve Details</span>
            <span class="summary-copy">Supporting values behind the first-glance battery and tonight risk cards.</span>
          </span>
          <span class="reserve-badges">
            <span class="badge {esc(soc_health_class)}">Battery: {esc(soc_health)}</span>
            <span class="badge badge-neutral">Floor: {esc(reserve_floor_display)}</span>
            <span class="badge badge-neutral">Charge: {esc(battery_charge_rate_display)}</span>
          </span>
        </summary>
        <div class="reserve-body">
        <div class="battery-stats">
          <div><span>Charge rate</span><strong>{esc(battery_charge_rate_display)}</strong><em>Configured grid charge</em></div>
          <div><span>Voltage</span><strong>{esc(vbat)}</strong><em>Battery bus reading</em></div>
          <div><span>Day throughput</span><strong>{esc(battery_throughput_display)}</strong><em>Charge plus discharge today</em></div>
          <div><span>Expected grid top-up</span><strong>{esc(_grid_forecast_str)}</strong><em>Duration {esc(_topup_duration_str)}</em></div>
          <div><span>Estimate basis</span><strong>{esc(sunrise_reserve_detail_display)}</strong><em>Projection input for tonight risk</em></div>
          <div><span>Weather context</span><strong>{esc(_weather_short_str)}</strong><em>{esc(_weather_reason_str)}</em></div>
        </div>
        </div>
    </details>

    <section class="flow-stage" id="flow" aria-label="Live energy flow">
        <div class="flow-head">
          <div>
            <h2>Live energy flow</h2>
            <div class="muted">{esc(bat_status)} &middot; Load: {esc(load_power_display)} at {esc(load_pct)} &middot; Battery {esc(battery_power_label.lower())}</div>
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
                <div class="flow-label">Battery Flow</div>
                <div class="flow-value">{esc(battery_flow_display)}</div>
              </div>
              <div class="flow-detail">{esc(battery_power_label)} - {esc(battery_context)}</div>
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
        <div class="label">Tonight Risk Basis</div>
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
