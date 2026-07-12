from __future__ import annotations
import datetime as dt
from pathlib import Path
from typing import Any

from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.pvoutput import read_pvoutput_state
from growatt_guard.operational_status import build_sbu_guard_status
from growatt_guard.growatt_api import format_duration_minutes
from growatt_guard.dashboard_metrics import (
    _fmt_w, _history_with_live, build_dashboard_history_payload,
    extract_dashboard_metric_sources, extract_dashboard_metrics,
)
from growatt_guard.dashboard_insights import (
    build_dashboard_daily_insights, build_dashboard_data_quality, build_tonight_risk,
)
from growatt_guard.dashboard_planning import (
    _numeric_metric, _project_sunset_soc, _should_use_projected_sunset_start,
    build_dashboard_assistant_summary, build_dashboard_daily_mix,
    build_dashboard_energy_outlook, build_dashboard_home_status,
    build_dashboard_next_action, build_dashboard_recommendations,
)
from growatt_guard.state import (
    pause_message, read_battery_alert_state, read_growatt_cloud_failure_state,
    read_pause_state, utc_now,
)
from growatt_guard.schedule import (
    cron_matches, next_scheduled_runs, schedule_job_id, schedule_job_tokens,
    today_schedule_override,
)

from growatt_guard.dashboard_render_components import format_duration, _status_badge_class

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
    min_sbu_return_soc: float = 30.0,
    topup_status: dict[str, Any] | None = None,
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
    sbu_guard = build_sbu_guard_status(min_sbu_return_soc)
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
        now=now,
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
        "planner": {
            "tonight_risk": risk,
            "outlook": energy_outlook,
            "forecast_calibration": energy_outlook.get("forecast_calibration"),
        },
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
            "sbu_return_guard": sbu_guard,
            "topup_status": topup_status or {"active": False, "warnings": []},
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
        raise GrowattGuardError("Dashboard stale threshold must be greater than 0 minutes.")

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
