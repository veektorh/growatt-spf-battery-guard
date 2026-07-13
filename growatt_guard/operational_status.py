from __future__ import annotations

import datetime as dt
from typing import Any

from growatt_guard.audit import parse_audit_timestamp, read_mode_audit_rows
from growatt_guard.forecast_calibration import (
    FORECAST_CALIBRATION_MIN_SAMPLES,
    summarize_forecast_calibration,
)
from growatt_guard.state import read_utility_hold_state

_GUARD_ACTIONS = {"low-soc-guard-blocked", "low-soc-guard-bypassed"}


def build_sbu_guard_status(
    minimum_soc: float,
    *,
    audit_rows: list[dict[str, str]] | None = None,
    utility_hold: dict[str, Any] | None = None,
) -> dict[str, Any]:
    threshold = float(minimum_soc)
    rows = audit_rows if audit_rows is not None else read_mode_audit_rows(limit=100, newest_first=True)
    hold = utility_hold if utility_hold is not None else read_utility_hold_state()
    last = next((row for row in rows if row.get("action") in _GUARD_ACTIONS), None)

    hold_blocked = False
    if hold and last and last.get("action") == "low-soc-guard-blocked":
        event_at = parse_audit_timestamp(last.get("timestamp", ""))
        try:
            hold_started = dt.datetime.fromisoformat(str(hold.get("started_at", "")))
        except ValueError:
            hold_started = None
        if event_at is not None and hold_started is not None:
            if event_at.tzinfo is None and hold_started.tzinfo is not None:
                event_at = event_at.replace(tzinfo=hold_started.tzinfo)
            elif event_at.tzinfo is not None and hold_started.tzinfo is None:
                hold_started = hold_started.replace(tzinfo=event_at.tzinfo)
            hold_blocked = event_at >= hold_started
        else:
            hold_blocked = True

    if threshold < 0 or threshold > 100:
        state = "misconfigured"
        detail = f"MIN_SBU_RETURN_SOC={threshold:g}% is outside 0-100%."
    elif threshold == 0:
        state = "disabled"
        detail = "MIN_SBU_RETURN_SOC=0; automatic Utility-to-SBU returns are not SOC-guarded."
    elif hold_blocked:
        state = "blocked_hold"
        detail = f"Guard enabled at {threshold:g}%; an active Utility hold remains after a blocked SBU return."
    else:
        state = "enabled"
        detail = f"Guard enabled at {threshold:g}%; unreadable SOC also blocks automatic SBU returns."

    last_event = None
    if last:
        last_event = {
            key: last.get(key, "")
            for key in ("timestamp", "command", "soc", "threshold", "action", "result", "note")
        }
        detail += f" Last event: {last.get('action')} at {last.get('timestamp', 'unknown time')}."

    return {
        "state": state,
        "enabled": 0 < threshold <= 100,
        "minimum_soc": threshold,
        "hold_blocked": hold_blocked,
        "detail": detail,
        "last_event": last_event,
    }


def build_forecast_calibration_status(config: Any) -> dict[str, Any]:
    configured = bool(config.panel_kwp > 0 and config.weather_lat is not None and config.weather_lon is not None)
    summary = summarize_forecast_calibration(
        current_performance_ratio=config.panel_performance_ratio,
        sunny_threshold_kwh_m2=config.auto_topup_solar_skip_kwh_m2,
    )
    samples = int(summary.get("rainy_sample_count") or 0)
    ready = samples >= FORECAST_CALIBRATION_MIN_SAMPLES
    if not configured:
        detail = "not configured; set PANEL_KWP and weather coordinates to collect day-ahead PV evidence."
    elif ready:
        detail = f"ready with {samples} completed rainy/cloudy day(s); {summary.get('recommendation', '')}"
    else:
        detail = f"learning with {samples}/{FORECAST_CALIBRATION_MIN_SAMPLES} completed rainy/cloudy day(s)."
    return {
        "configured": configured,
        "ready": ready,
        "sample_count": samples,
        "minimum_samples": FORECAST_CALIBRATION_MIN_SAMPLES,
        "detail": detail,
        "summary": summary,
    }
