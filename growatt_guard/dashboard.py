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

from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.outage_days import outage_mode_label
from growatt_guard.notifications import notify_failure, send_discord_message
from growatt_guard.dashboard_assets import DASHBOARD_CSS, DASHBOARD_JS
from growatt_guard.growatt_api import (
    PV_POWER_CHANNELS,
    PV_TODAY_CHANNELS,
    estimate_topup_for_sunrise,
    deep_values,
    detect_unexpected_grid_bypass,
    extract_battery_status,
    extract_channel_metric_sum,
    extract_first_metric,
    extract_soc,
    resolve_soc,
    extract_spf_output_source,
    format_battery_temperature,
    format_duration_minutes,
    load_context,
    parse_number,
)
from growatt_guard.dashboard_metrics import (
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
    build_dashboard_daily_mix,
    build_dashboard_next_action,
)
from growatt_guard.state import (
    clear_dashboard_stale_alert_state,
    read_dashboard_stale_alert_state,
    write_dashboard_stale_alert_state,
)
from growatt_guard.schedule import (
    cron_matches,
    format_outage_windows,
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
    _inline_badge,
    _kv_rows,
    _live_cell,
    _meter,
    _pvoutput_status,
    _render_alerts,
    _render_balance_bar,
    _render_health_rows,
    _render_insight_rows,
    _render_recommendations,
    _render_timeline_rows,
    _status_badge_class,
    _tile,
)
from growatt_guard.weather import choose_preserve_threshold, get_pv_forecast, hours_until_next_sunrise
from growatt_guard.paths import DATA_HOME


BASE_DIR = DATA_HOME
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


def _month_label(month: str) -> str:
    try:
        parsed = dt.date.fromisoformat(f"{month}-01")
    except ValueError:
        return str(month)
    return parsed.strftime("%b %Y")


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
    auto_topup_headroom_soc: float = 0.0,
    auto_topup_min_minutes: float = 0.0,
    auto_topup_max_minutes: float = 0.0,
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
        # The renderer reads nothing itself: every state file and audit read
        # belongs to the view-model payload.
        dashboard_data = build_dashboard_data_payload(
            status,
            schedule,
            overrides,
            threshold_decision,
            stale_after_minutes=stale_after_minutes,
            battery_capacity_wh=battery_capacity_wh,
            battery_bms_cutoff_soc=battery_bms_cutoff_soc,
            hours_to_sunrise=hours_to_sunrise,
            battery_charge_rate_w=battery_charge_rate_w,
            auto_topup_target_soc=auto_topup_target_soc,
            auto_topup_solar_skip_min_margin_minutes=auto_topup_solar_skip_min_margin_minutes,
            auto_topup_headroom_soc=auto_topup_headroom_soc,
            metrics_history=metrics_history,
            hours_to_sunset=hours_to_sunset,
            pv_forecast=pv_forecast,
            min_sbu_return_soc=min_sbu_return_soc,
        )
    generated_at_iso = str(dashboard_data["generated_at"])
    generated_at = dt.datetime.fromisoformat(generated_at_iso)
    now = generated_at.replace(tzinfo=None)
    live_metrics = dict(dashboard_data["live"])
    history_payload = dashboard_data["history"]
    metric_history_json = json.dumps(history_payload)
    resolved = resolve_soc(status)
    soc_result = resolved.as_tuple()
    # Keep the primary SOC label short so glance/ring cards stay compact.
    # Surface BMS disagreement in muted context, not the hero value.
    soc_note = ""
    if soc_result:
        soc = f"{soc_result[0]:g}%"
        if resolved.diverged and resolved.bms is not None:
            soc_note = f"BMS {resolved.bms:g}%"
    else:
        soc = "Not found"
    output_source = extract_spf_output_source(status)
    mode = f"{output_source[1]} [{output_source[0]}]" if output_source else "Not found"
    bypass = detect_unexpected_grid_bypass(status)
    bypass_detected = bool(bypass["detected"])
    bypass_reason = str(bypass.get("reason") or "")
    bat_status = extract_battery_status(status) or "—"
    _load = extract_first_metric(status, ("loadPercent", "loadPercent1"))
    _n = parse_number(_load[0]) if _load else None
    load_pct = f"{_n:.0f}%" if _n is not None else "—"
    runtime = dashboard_data["runtime"]
    est_runtime = runtime["display"]
    runtime_note = runtime["note"]
    _vbat = extract_first_metric(status, ("vBat", "vBat1", "vbat"))
    _vbat_n = parse_number(_vbat[0]) if _vbat else None
    vbat = f"{_vbat_n:g} V" if _vbat_n is not None else "—"
    battery_temp = format_battery_temperature(status)
    sunrise_display = "—"
    if hours_to_sunrise is not None and hours_to_sunrise > 0:
        sunrise_display = format_duration_minutes(hours_to_sunrise * 60)
    pause_state = dashboard_data["automation"].get("pause_state")
    pause = str(dashboard_data["automation"]["pause"])
    outage = outage_mode_label(schedule=schedule)
    profile = str(schedule.get("active_profile") or "").strip()
    profile_label = str(schedule.get("profile_label") or profile or "single schedule")
    profile_description = str(schedule.get("profile_description") or "").strip()
    profile_outages = format_outage_windows(schedule.get("outage_windows") or [])
    profile_buttons_html = ""
    schedule_profiles = schedule.get("profiles") or {}
    if schedule_profiles:
        buttons: list[str] = []
        for profile_name, body in schedule_profiles.items():
            label = str(body.get("label") or profile_name)
            is_active = profile_name == profile
            suffix = " · active" if is_active else ""
            disabled = " disabled" if is_active else ""
            buttons.append(
                f'<button type="button" class="pill profile-button" '
                f'data-profile="{esc(profile_name)}"{disabled}>'
                f"{esc(label + suffix)}</button>"
            )
        profile_buttons_html = "".join(buttons)
    profile_line = f"Outage days: {outage}" + (f" · Profile: {profile}" if profile else "")
    alert = str(dashboard_data["automation"]["emergency_alert"])
    cloud_streak = int(dashboard_data["automation"].get("cloud_failure_streak") or 0)
    today_override = today_schedule_override(overrides, now.date())
    override_note = str(today_override.get("note", "")).strip() or "none"
    skipped = ", ".join(today_override.get("skip", [])) if isinstance(today_override.get("skip", []), list) else ""
    last_actions = dashboard_data["automation"]["recent_decisions"]
    sbu_guard = dashboard_data["automation"]["sbu_return_guard"]
    topup_status = dashboard_data["automation"].get("topup_status") or {"active": False}
    next_runs = next_scheduled_runs(schedule, now=now, limit=8)
    next_action = dashboard_data["schedule"]["next_action"]
    stale_minutes_text = f"{stale_after_minutes:g}"

    today_jobs = _today_job_rows(schedule, today_override, now.date())
    schedule_timeline = build_dashboard_schedule_timeline(schedule, today_override, now=now)
    upcoming_overrides = dashboard_data["schedule"].get("upcoming_overrides") or []
    chart_data_json = json.dumps(dashboard_data["automation"]["chart_data"])
    energy_history = dashboard_data.get("energy_history") or {}
    energy_history_json = json.dumps(energy_history)
    month_rows_html = "\n".join(
        "<tr>"
        + f'<td class="t">{esc(_month_label(row["month"]))}</td>'
        + f'<td class="r">{esc(_fmt_kwh(row["solar_kwh"]))}</td>'
        + f'<td class="r">{esc(_fmt_kwh(row["load_kwh"]))}</td>'
        + f'<td class="r">{esc(_fmt_kwh(row["utility_kwh"]))}</td>'
        + f'<td class="r">{row["days"]}{"" if row["complete"] else "*"}</td>'
        + "</tr>"
        for row in reversed(energy_history.get("months") or [])
    ) or '<tr><td colspan="5" class="muted">No completed days recorded yet.</td></tr>'
    energy_metrics_note = (
        "Load and Utility totals start on the day the ledger began recording them, "
        "so earlier months show a dash."
        if any(row.get("load_kwh") is None for row in (energy_history.get("months") or []))
        else "Every month here carries solar, load and Utility totals."
    )
    pv_power_display = _fmt_w(live_metrics.get("pv_w"))
    grid_power_display = _fmt_w(live_metrics.get("grid_w"))
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
    pv_total_text = str(live_metrics.get("pv_total") or "").strip()
    tonight_risk = dashboard_data["planner"]["tonight_risk"]
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
    # One top-up estimate for the whole page: the planner's, not a second one
    # computed here from a different load and horizon.
    topup_sunrise_display = tonight_topup_display
    if (
        isinstance(tonight_topup, (int, float))
        and 0 < tonight_topup < auto_topup_min_minutes
    ):
        topup_sunrise_display = (
            f"{format_duration_minutes(tonight_topup)}"
            f" · skip (<{format_duration_minutes(auto_topup_min_minutes)})"
        )

    # Tonight Safe headline (only shown after evening cutoff).
    _tonight_proj = tonight_risk.get("projected_sunrise_soc")
    tonight_safe = compute_tonight_safe(
        projected_sunrise_soc=_tonight_proj if isinstance(_tonight_proj, (int, float)) else None,
        hours_to_sunset=hours_to_sunset,
        floor_soc=tonight_floor_soc,
        comfortable_soc=tonight_comfortable_soc,
    )
    tonight_safe_alert: tuple[str, str, str] | None = None
    if tonight_safe.get("show"):
        _ts_level = tonight_safe.get("level", "watch")
        _ts_badge = "badge-fail" if _ts_level == "danger" else ("badge-ok" if _ts_level == "ok" else "badge-warn")
        tonight_safe_alert = (
            _ts_badge,
            str(tonight_safe.get("headline", "")) or "Tonight",
            " ".join(
                part
                for part in (str(tonight_safe.get("subtext", "")), str(tonight_safe.get("reason", "")))
                if part
            ),
        )

    # Utility hold status (shown when owned/adopted hold is active).
    utility_hold_detail = str(dashboard_data["automation"].get("utility_hold_detail") or "")

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
    mode_badge_class = "badge-warn" if "utility" in mode.lower() else ("badge-ok" if "sbu" in mode.lower() else "badge-warn")
    bypass_badge_class = "badge-fail" if bypass_detected else "badge-ok"
    bypass_badge_label = "Detected" if bypass_detected else "Clear"
    bypass_status_detail = bypass_reason or "No grid bypass detected"
    grid_status_text = str(dashboard_data["grid_status_text"])

    if battery_flow_dir == "charging":
        battery_context = f"Charging · {soc_health}"
    elif battery_flow_dir == "discharging":
        battery_context = f"Discharging · {soc_health}"
    else:
        battery_context = f"Idle · {soc_health}"
    if soc_note:
        battery_context = f"{battery_context} · {soc_note}"
    metric_sources = dashboard_data["sources"]
    data_quality = dashboard_data["quality"]["data"]
    daily_insights = dashboard_data["insights"]["daily"]
    energy_outlook = dashboard_data["planner"]["outlook"]
    threshold_display = _fmt_g(getattr(threshold_decision, "threshold", None), "%")
    threshold_reason = str(getattr(threshold_decision, "reason", "") or "Weather signal is unavailable.")
    home_status = dashboard_data["assistant"]["status"]
    recommendations = dashboard_data["assistant"]["recommendations"]

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
    _calibration_detail = "Collecting five completed rainy/cloudy forecast days."
    if isinstance(_calibration, dict):
        _calibration_samples = int(_calibration.get("rainy_sample_count") or 0)
        _calibration_error = _calibration.get("rainy_mean_absolute_error_kwh")
        _calibration_confidence = str(_calibration.get("confidence") or "learning").title()
        _calibration_value = f"{_calibration_confidence} ({_calibration_samples}d)"
        _calibration_detail = str(_calibration.get("recommendation") or _calibration_detail)
        if _calibration_samples > 0 and isinstance(_calibration_error, (int, float)):
            _tomorrow_pv_detail += (
                f" Rain calibration: {_calibration_samples} comparable completed days, "
                f"{_fmt_kwh(float(_calibration_error))} mean absolute error."
            )
        else:
            _tomorrow_pv_detail += " Rain calibration is collecting comparable completed days."
    _weather_sensitive_pv = False
    if pv_forecast and pv_forecast.get("weather_adjusted"):
        _base_tmr = pv_forecast.get("base_tomorrow_kwh")
        _factor = pv_forecast.get("weather_adjustment_factor")
        _factor_pct = f"{float(_factor) * 100:.0f}%" if isinstance(_factor, (int, float)) else "conservative"
        _factor_source = str(pv_forecast.get("weather_adjustment_source") or "rainy/cloudy")
        _condition_bits: list[str] = []
        _tomorrow_cloud = pv_forecast.get("tomorrow_cloud_cover")
        _tomorrow_rain = pv_forecast.get("tomorrow_precipitation_mm")
        if isinstance(_tomorrow_cloud, (int, float)):
            _condition_bits.append(f"{float(_tomorrow_cloud):.0f}% cloud")
        if isinstance(_tomorrow_rain, (int, float)):
            _condition_bits.append(f"{float(_tomorrow_rain):g} mm rain")
        _conditions = f" Forecast: {', '.join(_condition_bits)}." if _condition_bits else ""
        _tomorrow_pv_detail = (
            f"Rain-adjusted from {_fmt_kwh(_base_tmr)} using a {_factor_pct} {_factor_source} factor."
            f"{_conditions}"
        )
        _weather_sensitive_pv = True
    _kwp = pv_forecast.get("panel_kwp", 0) if pv_forecast else 0
    _weather_category = str(getattr(threshold_decision, "weather_category", "") or "").strip().lower()
    _has_weather_signal = _weather_category not in {"", "disabled", "unavailable", "not configured", "unknown"}
    if isinstance(_kwp, (int, float)) and _kwp > 0:
        _forecast_source = _tomorrow_pv_detail + f" {_fmt_g(float(_kwp))} kWp system."
        _forecast_short_str = "Rain-adjusted" if _weather_sensitive_pv else "Open-Meteo estimate"
    elif _has_weather_signal:
        _forecast_source = "Set PANEL_KWP to convert Open-Meteo irradiance into PV kWh."
        _forecast_short_str = "Needs PANEL_KWP"
    else:
        _forecast_source = "Set PANEL_KWP plus WEATHER_LAT/WEATHER_LON to enable PV kWh forecasts."
        _forecast_short_str = "Needs forecast setup"
    active_topup_detail = (
        f"{topup_status.get('ownership', 'unknown')} ownership; "
        f"elapsed {topup_status.get('elapsed_minutes', '--')} min; "
        f"projected {topup_status.get('projected_completion', topup_status.get('max_expiry', '--'))}. "
        + (" ".join(topup_status.get("warnings", [])) or "Completion monitoring healthy.")
        if topup_status.get("active") else "No persisted Utility hold."
    )
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
            (
                "Active Top-up",
                (
                    f"{topup_status.get('current_soc', '--')}% → {topup_status.get('target_soc', 'time')}"
                    if topup_status.get("active") else "None"
                ),
                active_topup_detail,
            ),
        ],
    }
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
    reserve_target_display = _fmt_pct(energy_outlook.get("reserve_target_soc"))
    battery_charge_rate_display = _fmt_w(battery_charge_rate_w) if battery_charge_rate_w > 0 else "--"
    reserve_floor_display = _fmt_pct(battery_bms_cutoff_soc)
    battery_power_label = battery_flow_dir.capitalize()
    next_action_relative = str(next_action.get("relative") or "none")
    next_action_title = str(next_action.get("title") or "No upcoming jobs")
    next_action_detail = str(next_action.get("detail") or "No scheduled jobs found.")

    def _json_for_script(payload: Any) -> str:
        return json.dumps(payload).replace("</", "<\\/")

    # ---- Now tab: attention rows, shown only when they apply -------------
    emergency_badge_class = "badge-fail" if alert == "active" else "badge-ok"
    cloud_badge_class = "badge-warn" if cloud_streak else "badge-ok"
    guard_badge_class = "badge-fail" if sbu_guard["state"] == "misconfigured" else (
        "badge-warn" if sbu_guard["state"] in {"disabled", "blocked_hold"} else "badge-ok"
    )
    topup_badge_class = "badge-fail" if topup_status.get("warnings") else (
        "badge-warn" if topup_status.get("active") else "badge-ok"
    )
    dashboard_health_ok = alert == "clear" and cloud_streak == 0 and sbu_guard["state"] != "misconfigured"
    alerts: list[tuple[str, str, str]] = []
    if today_override.get("skip_all"):
        alerts.append((
            "badge-warn",
            "Automation skipped",
            "All automation jobs are skipped today" + (f" — {override_note}" if override_note != "none" else ""),
        ))
    if bypass_detected:
        alerts.append(("badge-fail", "Grid bypass", bypass_status_detail))
    if alert == "active":
        alerts.append(("badge-fail", "Emergency battery alert", "Battery SOC is at or below the emergency threshold."))
    if sbu_guard["state"] == "misconfigured":
        alerts.append(("badge-fail", "SBU return guard", str(sbu_guard["detail"])))
    if pause_state:
        alerts.append(("badge-warn", "Automation paused", pause))
    if cloud_streak:
        alerts.append(("badge-warn", "Growatt cloud", f"{cloud_streak} consecutive failed Growatt reads."))
    if utility_hold_detail or topup_status.get("active"):
        alerts.append((topup_badge_class, "Top-up running", utility_hold_detail or active_topup_detail))
    if tonight_safe_alert is not None:
        alerts.append(tonight_safe_alert)
    alerts_html = _render_alerts(alerts)

    tonight_needs_topup = isinstance(tonight_topup, (int, float)) and tonight_topup > 0
    attention_html = (
        (_inline_badge("Top-up needed tonight", tonight_badge_class) if tonight_needs_topup else "")
        + _inline_badge("Systems OK" if dashboard_health_ok else "Needs attention", "badge-ok" if dashboard_health_ok else "badge-warn")
    )

    # ---- Now tab: live band and answer tiles ------------------------------
    live_band_html = "\n".join(
        [
            _live_cell("solar", "Solar Now", pv_power_display, f"Live load cover {pv_cover_display}"),
            _live_cell("load", "Load Now", load_power_display, f"{load_pct} of inverter capacity"),
            _live_cell("grid", "Grid Import Now", grid_power_display, f"{grid_status_text} · Bypass: {bypass_badge_label}"),
            _live_cell(
                "battery",
                "Battery Now",
                battery_flow_display,
                battery_power_label + (f" · {soc_note}" if soc_note else ""),
            ),
        ]
    )

    def _num(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) else None

    pv_insight = next(
        (
            item
            for item in daily_insights.get("items", [])
            if isinstance(item, dict) and item.get("key") == "pv_today_kwh"
        ),
        None,
    )
    pv_today_value = _num(live_metrics.get("pv_today_kwh"))
    pv_baseline = _num(pv_insight.get("baseline")) if pv_insight else None
    solar_meter_html = ""
    if pv_today_value is not None and pv_baseline is not None and pv_baseline > 0:
        _solar_scale = max(pv_today_value, pv_baseline) * 1.15
        solar_meter_html = _meter(
            pv_today_value / _solar_scale * 100,
            "solar",
            pv_baseline / _solar_scale * 100,
            f"usual by now {_fmt_kwh(pv_baseline)}",
        )
    if pv_insight:
        solar_badge = (str(pv_insight.get("title", "Learning")), _status_badge_class(str(pv_insight.get("level", "unknown"))))
    else:
        solar_badge = ("Active", "badge-ok") if (live_metrics.get("pv_w") or 0) >= 20 else ("Low", "badge-neutral")
    solar_tile = _tile(
        "tile-solar",
        "Solar Today",
        pv_today_display,
        "generated so far",
        solar_badge[0],
        solar_badge[1],
        solar_meter_html,
        [
            ("Today Remaining", _rem_str),
            ("Tomorrow PV", _tmr_str),
            ("Forecast", _forecast_short_str),
        ],
    )
    battery_tile = _tile(
        "tile-battery",
        "Battery",
        soc,
        est_runtime if est_runtime != "—" else battery_power_label,
        soc_health,
        soc_health_class,
        _meter(
            soc_gauge_value if soc_result else None,
            "battery",
            battery_bms_cutoff_soc,
            f"floor {reserve_floor_display}",
        ),
        [
            ("Charged / discharged today", f"{charge_today_display} / {discharge_today_display}"),
            ("Battery Voltage", vbat),
            ("Battery Temperature", battery_temp),
        ],
        note=runtime_note,
    )
    reserve_target_value = _num(energy_outlook.get("reserve_target_soc"))
    topup_tile = _tile(
        "tile-topup",
        "Top-up Tonight",
        tonight_topup_display,
        "of grid charging needed" if tonight_needs_topup else "projected from tonight's load",
        tonight_title,
        tonight_badge_class,
        _meter(
            _num(tonight_projection),
            "ok" if tonight_badge_class == "badge-ok" else ("warn" if tonight_badge_class == "badge-warn" else "fail"),
            reserve_target_value,
            f"target {reserve_target_display} at sunrise",
        ),
        [
            ("Without a top-up", f"{_sunset_str} at sunset → {tonight_projection_display} at sunrise"),
            ("Topup to Sunrise", topup_sunrise_display),
            ("Sunrise In", sunrise_display),
        ],
    )

    # ---- Energy tab ----------------------------------------------------------
    balance_html = _render_balance_bar(
        "Where energy came from",
        [
            ("solar", "PV Today", _num(live_metrics.get("pv_today_kwh"))),
            ("grid", "Grid Import Today", _num(live_metrics.get("grid_today_kwh"))),
            ("neutral", "Battery Discharge Today", _num(live_metrics.get("discharge_today_kwh"))),
        ],
    ) + _render_balance_bar(
        "Where energy went",
        [
            ("load", "Load Today", _num(live_metrics.get("load_today_kwh"))),
            ("battery", "Battery Charge Today", _num(live_metrics.get("charge_today_kwh"))),
        ],
    )
    insight_rows_html = _render_insight_rows(daily_insights.get("items", []))
    outlook_rows = list(energy_outlook_view["cards"]) + [
        ("Tonight Risk Basis", tonight_title, tonight_detail),
        ("Preserve Threshold", threshold_display, threshold_reason),
    ]
    if pv_total_text:
        outlook_rows.append(("PV Lifetime", pv_total_text, "Total production reported by Growatt."))
    outlook_rows_html = "\n".join(
        f'<tr><td class="health-label">{esc(label)}</td><td class="v">{esc(value)}</td><td class="muted">{esc(detail)}</td></tr>'
        for label, value, detail in outlook_rows
    )
    forecast_recent = _calibration.get("recent") if isinstance(_calibration, dict) else None
    forecast_history_json = _json_for_script(forecast_recent if isinstance(forecast_recent, list) else [])

    # ---- Automation tab ------------------------------------------------------
    recommendations_html = _render_recommendations(recommendations)
    timed_jobs = sorted(
        (item for item in schedule_timeline if not item.get("recurring")),
        key=lambda item: str(item.get("time", "")),
    )
    monitor_jobs = [item for item in schedule_timeline if item.get("recurring")]
    # The strip on the Now tab names the next timed job; recurring monitors fire too often to be useful there.
    next_timed_job = next((item for item in timed_jobs if item.get("state") in {"next", "upcoming"}), None)
    if next_timed_job is not None:
        next_job_when = str(next_timed_job.get("time", "--"))
        next_job_name = str(next_timed_job.get("name", ""))
    else:
        next_job_when, next_job_name = next_action_relative, next_action_title
    timed_rows_html = _render_timeline_rows(timed_jobs) or (
        '<tr><td colspan="3" class="muted">No timed jobs scheduled today.</td></tr>'
    )
    monitor_rows_html = _render_timeline_rows(monitor_jobs) or (
        '<tr><td colspan="3" class="muted">No background monitors scheduled today.</td></tr>'
    )
    action_rows = "\n".join(
        "<tr>"
        f'<td class="t">{esc(str(row.get("timestamp", "")).replace("T", " "))}</td>'
        f"<td><code>{esc(row.get('command', ''))}</code></td>"
        f"<td>{esc(row.get('action', ''))}</td>"
        f'<td class="r">{esc(row.get("soc", ""))}</td>'
        f'<td class="r">{esc(row.get("previous_mode", ""))}</td>'
        "</tr>"
        for row in last_actions
    ) or '<tr><td colspan="5" class="muted">No recent mode decisions recorded.</td></tr>'
    upcoming_override_rows_html = "\n".join(
        "<tr>"
        + f'<td class="t">{esc(d)}</td>'
        + "<td>{}</td>".format(esc(n) if n else '<span class="muted">—</span>')
        + f"<td>{esc(a)}</td>"
        + "</tr>"
        for d, n, a in (
            (row["date"], row["note"], row["action"]) for row in upcoming_overrides
        )
    )
    upcoming_overrides_html = (
        '<div class="table-wrap"><table><thead><tr><th>Date</th><th>Note</th><th>Actions</th></tr></thead>'
        f"<tbody>{upcoming_override_rows_html}</tbody></table></div>"
        if upcoming_overrides
        else '<p class="muted">No overrides are scheduled. An override for today also shows as a banner on the Now tab.</p>'
    )
    guard_rows_html = _kv_rows(
        [
            ("Preserve Threshold", threshold_display),
            ("SBU Return Guard", f"{sbu_guard['minimum_soc']:g}% {sbu_guard['state']}"),
            ("Configured charge rate", battery_charge_rate_display),
            ("Pause State", pause),
            ("Today Override", override_note),
            ("Skipped today", skipped or "none"),
        ]
    )

    # ---- System tab ----------------------------------------------------------
    pvoutput_badge, pvoutput_badge_class, pvoutput_detail = _pvoutput_status(
        dashboard_data["pvoutput"], now,
    )
    health_rows_html = _render_health_rows(
        [
            ("Inverter Mode", bat_status, mode, mode_badge_class),
            ("Grid Bypass", bypass_status_detail, bypass_badge_label, bypass_badge_class),
            ("Data Quality", quality_detail, quality_display, quality_badge_class),
            ("Emergency Alert", "Battery SOC emergency threshold.", alert, emergency_badge_class),
            ("Cloud Streak", "Consecutive failed Growatt cloud reads.", str(cloud_streak), cloud_badge_class),
            ("SBU Guard", str(sbu_guard["detail"]), str(sbu_guard["state"]), guard_badge_class),
            (
                "Top-up",
                " ".join(topup_status.get("warnings", [])) or "Completion monitoring healthy.",
                "Active" if topup_status.get("active") else "Clear",
                topup_badge_class,
            ),
            ("PVOutput", pvoutput_detail, pvoutput_badge, pvoutput_badge_class),
        ]
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
    insight_badge = _inline_badge(
        str(daily_insights.get("title", "Learning")),
        _status_badge_class(str(daily_insights.get("status", "unknown"))),
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#0f1318">
  <meta name="color-scheme" content="dark light">
  <meta name="application-name" content="Growatt Energy Dashboard">
  <meta name="apple-mobile-web-app-title" content="Growatt">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta http-equiv="refresh" content="300">
  <link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">
  <link rel="icon" href="/dashboard-icon.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" href="/dashboard-icon-180.png" sizes="180x180">
  <title>Growatt Dashboard</title>
  <style>{DASHBOARD_CSS}</style>
</head>
<body data-generated-at="{esc(generated_at_iso)}">
  <div class="app">
  <header class="topbar">
    <div class="brand"><span class="brand-mark" aria-hidden="true"></span>Solar Inverter <span class="muted">· Growatt</span></div>
    <nav class="tabs" aria-label="Dashboard views">
      <a href="#now" data-tab="now">Now</a>
      <a href="#energy" data-tab="energy">Energy</a>
      <a href="#automation" data-tab="automation">Automation</a>
      <a href="#system" data-tab="system">System</a>
    </nav>
    <div class="top-actions">
      {attention_html}
      <span class="pill">Mode: {esc(mode)}</span>
      {f'<span class="pill" title="App SOC matches Growatt; {esc(soc_note)} differs">{esc(soc_note)}</span>' if soc_note else ""}
      <span class="muted small" data-refresh-age="short">Updated {esc(generated_at.strftime('%H:%M'))}</span>
      <button class="theme-toggle" id="theme-toggle-btn" type="button" onclick="toggleDashTheme()">Light</button>
    </div>
  </header>
  <main>

    <section class="tab-panel" id="tab-now" data-tab-panel="now" aria-label="Now">
      <div class="lead">
        <h1>{esc(str(home_status.get("headline", "Home energy is stable")))}</h1>
        <p class="muted">{esc(generated_at.strftime('%a %d %b'))} · {esc(_weather_str)}</p>
      </div>
      <div class="alerts">
        <div class="alert alert-warn" data-stale-alert hidden>
          <span class="badge badge-warn">Data is stale</span>
          <span data-refresh-age>The dashboard has not refreshed recently.</span>
        </div>
        {alerts_html}
      </div>
      <div class="live-band" aria-label="Live power right now">
        {live_band_html}
      </div>
      <div class="tiles" aria-label="Key details at a glance">
        {solar_tile}
        {battery_tile}
        {topup_tile}
      </div>
      <div class="charts">
        <article class="panel">
          <div class="eyebrow">
            <h2>Power, Last 24 Hours</h2>
            <div class="legend">
              <span><i class="line seg-solar"></i>Solar</span>
              <span><i class="line seg-load"></i>Load</span>
              <span><i class="line seg-grid"></i>Grid</span>
            </div>
          </div>
          <div class="chart" id="power-trend-chart"></div>
        </article>
        <article class="panel">
          <div class="eyebrow"><h2>Battery SOC, Last 24 Hours</h2></div>
          <div class="chart" id="soc-trend-chart" data-reference="{esc(f'{reserve_target_value:g}' if reserve_target_value is not None else '')}"></div>
        </article>
        <article class="panel">
          <div class="eyebrow">
            <h2>7-Day Supply Mix</h2>
            <div class="legend">
              <span><i class="seg-solar"></i>Solar</span>
              <span><i class="seg-grid"></i>Grid</span>
              <span><i class="line seg-ink"></i>Load</span>
            </div>
          </div>
          <div class="chart" id="supply-energy-chart"></div>
        </article>
      </div>
      <div class="strip">
        <div title="{esc(next_action_detail)}"><span>Next Automation</span> <b>{esc(next_job_when)}</b> {esc(next_job_name)}</div>
        <div><span>Outages</span> <b>{esc(profile_outages)}</b></div>
        <div><span>Profile</span> <b>{esc(profile_label)}</b></div>
      </div>
    </section>

    <section class="tab-panel" id="tab-energy" data-tab-panel="energy" aria-label="Energy">
      <div class="two">
        <article class="panel">
          <div class="eyebrow"><h2>Today's Balance</h2>{_inline_badge('Data ' + quality_display, quality_badge_class)}</div>
          {balance_html}
        </article>
        <article class="panel">
          <div class="eyebrow"><h2>Energy Insights</h2>{insight_badge}</div>
          <div class="table-wrap"><table>
            <thead><tr><th>Measure</th><th class="r">Today</th><th class="r">Usual</th><th class="r">Status</th></tr></thead>
            <tbody>{insight_rows_html}</tbody>
          </table></div>
          <p class="muted small">Same-time comparison against recent local history, without extra Growatt calls.</p>
        </article>
      </div>
      <div class="two">
        <article class="panel">
          <div class="eyebrow">
            <h2>7-Day Battery Energy</h2>
            <div class="legend"><span><i class="seg-battery"></i>Charge</span><span><i class="seg-neutral"></i>Discharge</span></div>
          </div>
          <div class="chart" id="battery-energy-chart"></div>
        </article>
        <article class="panel">
          <div class="eyebrow">
            <h2>Solar Forecast Against Actual</h2>
            <div class="legend"><span><i class="seg-neutral"></i>Forecast</span><span><i class="seg-solar"></i>Actual</span></div>
          </div>
          <div class="chart" id="forecast-accuracy-chart"></div>
        </article>
      </div>
      <article class="panel">
        <div class="eyebrow">
          <h2>This Month by Day</h2>
          <div class="legend">
            <span><i class="seg-solar"></i>Solar</span>
            <span><i class="seg-grid"></i>Utility</span>
            <span><i class="line seg-ink"></i>Load</span>
          </div>
        </div>
        <div class="chart" id="month-energy-chart"></div>
      </article>
      <article class="panel">
        <div class="eyebrow"><h2>Month by Month</h2><span class="muted small">* month in progress</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th>Month</th><th class="r">Solar</th><th class="r">Load</th><th class="r">Utility</th><th class="r">Days</th></tr></thead>
          <tbody>{month_rows_html}</tbody>
        </table></div>
        <p class="muted small">{esc(energy_metrics_note)}</p>
      </article>
      <article class="panel">
        <div class="eyebrow"><h2>Energy Outlook</h2>{_inline_badge('Confidence: ' + str(energy_outlook_view['confidence']), 'badge-neutral')}</div>
        <div class="table-wrap"><table><tbody>{outlook_rows_html}</tbody></table></div>
      </article>
    </section>

    <section class="tab-panel" id="tab-automation" data-tab-panel="automation" aria-label="Automation">
      <div class="two">
        <article class="panel profile-card" id="estate-profile" aria-label="Estate schedule profile">
          <div class="eyebrow"><h2>Estate Profile</h2><span class="pill" id="profile-label">{esc(profile_label)}</span></div>
          <p id="profile-description">{esc(profile_description)}</p>
          <p class="muted small" id="profile-outages">Outages: {esc(profile_outages)}</p>
          <p class="muted small">{esc(profile_line)}</p>
          <div id="profile-buttons" class="profile-actions">{profile_buttons_html}</div>
        </article>
        <article class="panel">
          <div class="eyebrow"><h2>Recommendations</h2></div>
          <div class="recs">{recommendations_html}</div>
        </article>
      </div>
      <dialog id="profile-confirm" class="profile-dialog" aria-labelledby="profile-confirm-title">
        <form method="post" action="/profile" id="profile-confirm-form">
          <input type="hidden" name="name" id="profile-confirm-name">
          <div class="label" id="profile-confirm-title">Switch estate profile</div>
          <div class="muted small" id="profile-confirm-desc"></div>
          <div class="profile-detail">
            <strong>Outages</strong>
            <div class="muted small" id="profile-confirm-outages"></div>
          </div>
          <div class="profile-detail">
            <strong>Preserve / mode jobs</strong>
            <ul class="profile-job-list" id="profile-confirm-jobs"></ul>
          </div>
          <div class="profile-actions">
            <button type="button" class="pill profile-button" id="profile-confirm-cancel">Cancel</button>
            <button type="submit" class="pill profile-button profile-confirm">Confirm switch</button>
          </div>
        </form>
      </dialog>
      <div class="two">
        <article class="panel">
          <div class="eyebrow">
            <h2>Automation History</h2>
            <div class="legend">
              <span><i class="seg-battery"></i>Preserve</span>
              <span><i class="seg-grid"></i>Utility</span>
              <span><i class="seg-solar"></i>Watchdog</span>
            </div>
          </div>
          <div class="chart" id="history-chart"></div>
        </article>
        <article class="panel">
          <div class="eyebrow"><h2>Upcoming Overrides</h2><span class="muted small">{len(upcoming_overrides)} active</span></div>
          {upcoming_overrides_html}
        </article>
      </div>
      <article class="panel">
        <div class="eyebrow"><h2>Recent Mode Decisions</h2><span class="muted small">Latest mode decisions from the local audit trail.</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th>Time</th><th>Command</th><th>Action</th><th class="r">SOC</th><th class="r">Previous Mode</th></tr></thead>
          <tbody>{action_rows}</tbody>
        </table></div>
      </article>
      <div class="two">
        <article class="panel">
          <div class="eyebrow"><h2>Today&#8217;s Schedule</h2><span class="muted small">{len(timed_jobs)} jobs · {esc(now.strftime('%A, %Y-%m-%d'))}</span></div>
          <div class="table-wrap"><table>
            <thead><tr><th>Time</th><th>Job</th><th class="r">Status</th></tr></thead>
            <tbody>{timed_rows_html}</tbody>
          </table></div>
        </article>
        <div class="stack">
          <article class="panel">
            <div class="eyebrow"><h2>Background Monitors</h2><span class="muted small">{len(monitor_jobs)} recurring</span></div>
            <div class="table-wrap"><table><tbody>{monitor_rows_html}</tbody></table></div>
          </article>
          <article class="panel">
            <div class="eyebrow"><h2>Thresholds and Guards</h2></div>
            <dl class="kv">{guard_rows_html}</dl>
            <p class="muted small">{esc(threshold_reason)}</p>
          </article>
        </div>
      </div>
    </section>

    <section class="tab-panel" id="tab-system" data-tab-panel="system" aria-label="System">
      <article class="panel">
        <div class="eyebrow">
          <h2>System Status</h2>
          {_inline_badge("All systems OK" if dashboard_health_ok else "Needs attention", "badge-ok" if dashboard_health_ok else "badge-warn")}
        </div>
        <div class="table-wrap"><table><tbody>
          <tr>
            <td class="health-label">Dashboard Health</td>
            <td data-refresh-age>Generated just now; stale after {esc(stale_minutes_text)} minutes.</td>
            <td class="r"><span class="badge badge-ok" data-refresh-badge data-generated-at="{esc(generated_at_iso)}" data-stale-minutes="{esc(stale_minutes_text)}">OK</span></td>
          </tr>
          {health_rows_html}
        </tbody></table></div>
        <p class="muted small">Generated {esc(generated_at.strftime('%Y-%m-%d %H:%M:%S %Z'))} · page refreshes every 5 minutes.</p>
      </article>
      <details class="panel source-drawer">
        <summary>Metric source paths</summary>
        <div class="table-wrap"><table><thead><tr><th>Metric</th><th>Source</th></tr></thead><tbody>{source_rows_html}</tbody></table></div>
      </details>
    </section>

    <script id="chart-data" type="application/json">{chart_data_json}</script>
    <script id="energy-history-data" type="application/json">{energy_history_json}</script>
    <script id="metric-history-data" type="application/json">{metric_history_json}</script>
    <script id="forecast-history-data" type="application/json">{forecast_history_json}</script>
  </main>
  </div>
  <script>{DASHBOARD_JS}</script>
</body>
</html>
"""
