from __future__ import annotations
import datetime as dt
from typing import Any

from growatt_guard.growatt_api import format_duration_minutes
from growatt_guard.dashboard_metrics import _fmt_kwh, _fmt_pct, _fmt_w
from growatt_guard.schedule import next_scheduled_runs, schedule_job_tokens

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


