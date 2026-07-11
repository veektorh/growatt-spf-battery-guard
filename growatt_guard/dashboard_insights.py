from __future__ import annotations
import datetime as dt
from typing import Any

from growatt_guard.growatt_api import estimate_topup_for_sunrise, format_duration_minutes
from growatt_guard.dashboard_metrics import _fmt_kwh, _fmt_pct, _fmt_w, _parse_metric_timestamp
from growatt_guard.state import read_discharge_rate_history

from growatt_guard.dashboard_planning import _positive_metric
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


