from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from growatt_guard.audit import (
    average,
    parse_audit_float,
    parse_audit_timestamp,
    parse_topup_minutes,
    read_mode_audit_rows,
    real_audit_rows,
)
from growatt_guard.config import Config
from growatt_guard.dashboard import DASHBOARD_JSON_FILE
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.notifications import send_discord_embed
from growatt_guard.state import (
    read_battery_alert_state,
    read_bypass_alert_state,
    read_command_lock_state,
    read_growatt_cloud_failure_state,
    read_pause_state,
    read_topup_state,
    topup_is_active,
    utc_now,
)


_COLOR_OK = 0x57F287
_COLOR_WARN = 0xFEE75C
_COLOR_FAIL = 0xED4245


@dataclass(frozen=True)
class OpsReview:
    text: str
    recommendations: list[str]
    severity: str
    metrics: dict[str, Any]


def _load_dashboard_payload(path: Path = DASHBOARD_JSON_FILE) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_dashboard_timestamp(payload: dict[str, Any]) -> dt.datetime | None:
    value = payload.get("generated_at")
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _fmt_value(value: Any, suffix: str = "") -> str:
    if isinstance(value, float):
        return f"{value:g}{suffix}"
    if isinstance(value, int):
        return f"{value}{suffix}"
    if value is None or value == "":
        return "--"
    return f"{value}{suffix}"


def _fmt_kwh(value: Any) -> str:
    return f"{value:.1f} kWh" if isinstance(value, (int, float)) else "--"


def _fmt_w(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "--"
    if abs(float(value)) >= 1000:
        return f"{float(value) / 1000:.1f} kW"
    return f"{float(value):.0f} W"


def _fmt_age(minutes: float | None) -> str:
    if minutes is None:
        return "age unknown"
    if minutes < 90:
        return f"{minutes:.0f} min ago"
    hours = minutes / 60.0
    if hours < 48:
        return f"{hours:.1f} h ago"
    return f"{hours / 24.0:.1f} d ago"


def _age_minutes(timestamp: dt.datetime | None, now: dt.datetime) -> float | None:
    if timestamp is None:
        return None
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone().replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    return max(0.0, (now - timestamp).total_seconds() / 60.0)


def _parse_topup_completion_note(note: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for key, pattern in {
        "start_soc": r"start_soc=([-+]?\d+(?:\.\d+)?)",
        "end_soc": r"end_soc=([-+]?\d+(?:\.\d+)?)",
        "actual_min": r"actual_min=([-+]?\d+(?:\.\d+)?)",
        "implied_rate_w": r"implied_rate_w=([-+]?\d+(?:\.\d+)?)",
    }.items():
        match = re.search(pattern, note)
        if match:
            try:
                values[key] = float(match.group(1))
            except ValueError:
                pass
    return values


def _last_mode_change(rows: list[dict[str, str]]) -> dict[str, str] | None:
    mode_change_actions = {
        "switch-to-utility",
        "switch-to-sbu",
        "repair-sbu",
        "auto-topup-started",
    }
    for row in reversed(rows):
        if row.get("action") in mode_change_actions and row.get("result") != "error":
            return row
    return None


def _audit_stats(rows: list[dict[str, str]]) -> dict[str, Any]:
    preserve_rows = [row for row in rows if row.get("command") == "preserve-battery"]
    preserve_no_changes = [
        row for row in preserve_rows if row.get("action") == "no-change"
    ]
    utility_switches = [row for row in rows if row.get("action") == "switch-to-utility"]
    return_sbu = [row for row in rows if row.get("action") == "switch-to-sbu"]
    watchdog_repairs = [row for row in rows if row.get("action") == "repair-sbu"]
    failures = [
        row for row in rows
        if row.get("action", "").endswith("-failed") or row.get("result") == "error"
    ]
    topups = [row for row in rows if row.get("action") == "auto-topup-started"]
    socs = [soc for row in rows if (soc := parse_audit_float(row, "soc")) is not None]
    preserve_socs = [
        soc for row in preserve_rows if (soc := parse_audit_float(row, "soc")) is not None
    ]
    topup_socs = [
        soc for row in topups if (soc := parse_audit_float(row, "soc")) is not None
    ]
    topup_minutes = [
        minutes for row in topups if (minutes := parse_topup_minutes(row)) is not None
    ]
    completed_topups = [
        row for row in rows
        if row.get("command") == "topup-complete-check"
        and row.get("action") == "topup-target-reached"
    ]
    expired_topups = [
        row for row in rows
        if row.get("command") == "topup-complete-check"
        and row.get("action") == "topup-expired"
    ]
    completion_notes = [
        _parse_topup_completion_note(row.get("note", ""))
        for row in completed_topups
    ]
    soc_gains = [
        values["end_soc"] - values["start_soc"]
        for values in completion_notes
        if "start_soc" in values and "end_soc" in values
    ]
    implied_rates = [
        values["implied_rate_w"] for values in completion_notes if "implied_rate_w" in values
    ]
    unclosed_topups = max(0, len(topups) - len(completed_topups) - len(expired_topups))
    return {
        "rows": len(rows),
        "preserve_checks": len(preserve_rows),
        "preserve_no_changes": len(preserve_no_changes),
        "utility_switches": len(utility_switches),
        "return_sbu": len(return_sbu),
        "watchdog_repairs": len(watchdog_repairs),
        "failures": len(failures),
        "topups": len(topups),
        "topup_minutes": sum(topup_minutes),
        "avg_topup_soc": average(topup_socs),
        "completed_topups": len(completed_topups),
        "expired_topups": len(expired_topups),
        "unclosed_topups": unclosed_topups,
        "avg_topup_soc_gain": average(soc_gains),
        "avg_implied_charge_rate_w": average(implied_rates),
        "lowest_soc": min(socs) if socs else None,
        "avg_preserve_soc": average(preserve_socs),
        "last_row": rows[-1] if rows else None,
        "last_mode_change": _last_mode_change(rows),
    }


def _state_summary(now: dt.datetime | None = None) -> dict[str, Any]:
    now_utc = utc_now()
    pause = read_pause_state(now=now_utc)
    topup = read_topup_state()
    bypass = read_bypass_alert_state()
    battery = read_battery_alert_state()
    cloud = read_growatt_cloud_failure_state()
    lock = read_command_lock_state()
    return {
        "pause": "paused" if pause else "active",
        "topup_active": topup_is_active(now=now_utc),
        "topup": topup,
        "bypass_alert": bypass,
        "battery_alert": battery,
        "cloud_failure_count": int(cloud.get("count", 0)) if cloud else 0,
        "command_lock": lock,
    }


def _recommendations(
    *,
    live: dict[str, Any],
    quality: dict[str, Any],
    planner: dict[str, Any],
    stats: dict[str, Any],
    state: dict[str, Any],
    dashboard_age_min: float | None,
    config: Config,
) -> tuple[list[str], str]:
    tips: list[str] = []
    severity = "ok"

    if dashboard_age_min is None:
        tips.append("Dashboard JSON is missing; run observability-refresh before relying on the review.")
        severity = "warn"
    elif dashboard_age_min > config.dashboard_stale_minutes:
        tips.append(f"Dashboard data is stale ({dashboard_age_min:.0f} min old); check observability/dashboard service.")
        severity = "warn"

    if live.get("bypass_detected"):
        soc = live.get("soc")
        tips.append(f"Unexpected grid bypass is currently detected at SOC {_fmt_value(soc, '%')}; inspect inverter state.")
        severity = "fail"

    quality_level = str(quality.get("level", "")).lower()
    if quality_level in {"warn", "poor", "fail"}:
        tips.append(f"Dashboard data quality is {quality.get('title') or quality_level}; fix missing metrics before tuning.")
        severity = "warn" if severity == "ok" else severity

    projected = planner.get("projected_sunrise_soc")
    reserve = planner.get("reserve_target_soc") or config.auto_topup_sunrise_floor_soc
    topup_minutes = planner.get("topup_minutes")
    if isinstance(projected, (int, float)) and isinstance(reserve, (int, float)) and projected < reserve:
        tips.append(f"Projected sunrise SOC is {projected:.0f}% below the {reserve:.0f}% reserve; keep auto-topup enabled.")
        severity = "warn" if severity == "ok" else severity
    elif topup_minutes == 0:
        tips.append("Tonight projection says no top-up is needed; current preserve/top-up logic looks aligned.")

    if stats["failures"] > 0:
        tips.append(f"{stats['failures']} automation failure(s) recorded in the review window; inspect logs before tuning.")
        severity = "warn" if severity == "ok" else severity
    if stats["watchdog_repairs"] >= 2:
        tips.append(f"Watchdog repaired SBU {stats['watchdog_repairs']} times; keep the 08:00 return and watchdog checks.")
        severity = "warn" if severity == "ok" else severity
    if stats["topups"] >= 4:
        tips.append(f"Auto-topup ran {stats['topups']} times; do not reduce reserve thresholds yet.")
    elif stats["topups"] == 0 and isinstance(stats["lowest_soc"], (int, float)) and stats["lowest_soc"] >= config.battery_bms_cutoff_soc + 12:
        tips.append("No topups and SOC stayed comfortably above the floor; settings look conservative for this window.")

    if state["cloud_failure_count"] > 0:
        tips.append(f"Growatt cloud failure streak is {state['cloud_failure_count']}; watch for API instability.")
        severity = "warn" if severity == "ok" else severity
    if state["topup_active"]:
        tips.append("A top-up or utility hold is currently active; avoid manual mode changes until it completes.")
    if state["pause"] == "paused":
        tips.append("Scheduled mode-changing automation is paused.")
        severity = "warn" if severity == "ok" else severity

    if not tips:
        tips.append("No action needed from the available local data.")
    return tips, severity


def build_ops_review(
    config: Config,
    *,
    days: int = 7,
    now: dt.datetime | None = None,
    dashboard_path: Path = DASHBOARD_JSON_FILE,
) -> OpsReview:
    days = max(1, min(int(days), 31))
    now = now or dt.datetime.now()
    since = now - dt.timedelta(days=days)
    audit_rows = real_audit_rows(read_mode_audit_rows(since=since))
    stats = _audit_stats(audit_rows)
    if config.battery_charge_rate_w > 0:
        stats["topup_estimated_grid_kwh"] = (
            stats["topup_minutes"] / 60.0 * config.battery_charge_rate_w / 1000.0
        )
    else:
        stats["topup_estimated_grid_kwh"] = None
    dashboard = _load_dashboard_payload(dashboard_path)
    generated_at = _parse_dashboard_timestamp(dashboard)
    dashboard_age_min = _age_minutes(generated_at, now)
    live = dashboard.get("live") if isinstance(dashboard.get("live"), dict) else {}
    quality_wrapper = dashboard.get("quality") if isinstance(dashboard.get("quality"), dict) else {}
    quality = quality_wrapper.get("data") if isinstance(quality_wrapper.get("data"), dict) else {}
    planner_wrapper = dashboard.get("planner") if isinstance(dashboard.get("planner"), dict) else {}
    planner = planner_wrapper.get("outlook") if isinstance(planner_wrapper.get("outlook"), dict) else {}
    automation = dashboard.get("automation") if isinstance(dashboard.get("automation"), dict) else {}
    state = _state_summary(now=now)

    recommendations, severity = _recommendations(
        live=live,
        quality=quality,
        planner=planner,
        stats=stats,
        state=state,
        dashboard_age_min=dashboard_age_min,
        config=config,
    )

    lines = [
        f"Growatt ops review - last {days} day{'s' if days != 1 else ''}",
        "",
        "Current snapshot:",
        f"  Dashboard age: {dashboard_age_min:.0f} min" if dashboard_age_min is not None else "  Dashboard age: unavailable",
        f"  SOC/mode: {_fmt_value(live.get('soc'), '%')} / {_fmt_value(live.get('mode'))}",
        f"  Battery: {_fmt_value(live.get('battery_status'))}; bypass: {'detected' if live.get('bypass_detected') else 'clear'}",
        f"  Power now: PV {_fmt_w(live.get('pv_w'))}, load {_fmt_w(live.get('load_w'))}, grid {_fmt_w(live.get('grid_w'))}, battery net {_fmt_w(live.get('battery_net_w'))}",
        f"  Today: PV {_fmt_kwh(live.get('pv_today_kwh'))}, load {_fmt_kwh(live.get('load_today_kwh'))}, grid {_fmt_kwh(live.get('grid_today_kwh'))}, charge {_fmt_kwh(live.get('charge_today_kwh'))}, discharge {_fmt_kwh(live.get('discharge_today_kwh'))}",
        "",
        "Sunrise plan:",
        f"  Sunset SOC: {_fmt_value(planner.get('projected_sunset_soc'), '%')}",
        f"  Sunrise SOC: {_fmt_value(planner.get('projected_sunrise_soc'), '%')} against reserve {_fmt_value(planner.get('reserve_target_soc'), '%')}",
        f"  Top-up: {_fmt_value(planner.get('topup_minutes'), ' min')} ({_fmt_kwh(planner.get('expected_grid_kwh'))} expected grid)",
        f"  Weather/quality: {_fmt_value(planner.get('weather'))}; {_fmt_value(quality.get('title') or quality.get('level'))}",
        "",
        "Automation audit:",
        f"  Rows: {stats['rows']} real rows",
        f"  Preserve checks: {stats['preserve_checks']} ({stats['preserve_no_changes']} no-change)",
        f"  Utility switches: {stats['utility_switches']}; return-SBU: {stats['return_sbu']}; watchdog repairs: {stats['watchdog_repairs']}",
        (
            f"  Auto-topups: {stats['topups']} "
            f"({stats['topup_minutes']} min total, "
            f"{_fmt_kwh(stats.get('topup_estimated_grid_kwh'))} est. grid)"
        ),
        (
            f"  Topup closures: {stats['completed_topups']} target reached, "
            f"{stats['expired_topups']} expired, {stats['unclosed_topups']} unclosed/legacy; "
            f"avg SOC gain {_fmt_value(stats['avg_topup_soc_gain'], '%')}; "
            f"avg implied charge {_fmt_w(stats['avg_implied_charge_rate_w'])}"
        ),
        f"  Failures: {stats['failures']}",
        f"  SOC range: lowest {_fmt_value(stats['lowest_soc'], '%')}; avg preserve {_fmt_value(stats['avg_preserve_soc'], '%')}; avg topup start {_fmt_value(stats['avg_topup_soc'], '%')}",
        "",
        "State:",
        f"  Scheduled automation: {state['pause']}; active top-up/hold: {'yes' if state['topup_active'] else 'no'}",
        f"  Battery alert: {'active' if state['battery_alert'] and state['battery_alert'].get('active') else 'clear'}; bypass alert: {'active' if state['bypass_alert'] and state['bypass_alert'].get('active') else 'clear'}",
        f"  Cloud failure streak: {state['cloud_failure_count']}; command lock: {'present' if state['command_lock'] else 'clear'}",
    ]
    if automation:
        lines.append(f"  Dashboard scheduled automation: {automation.get('pause', 'unknown')}; emergency alert {automation.get('emergency_alert', 'unknown')}")
    if stats["last_mode_change"]:
        row = stats["last_mode_change"]
        ts = parse_audit_timestamp(row.get("timestamp", ""))
        lines.extend([
            "",
            "Last mode change:",
            (
                f"  {row.get('timestamp', '')} ({_fmt_age(_age_minutes(ts, now))}) "
                f"{row.get('command', '')} {row.get('action', '')} "
                f"SOC={row.get('soc', '')}%"
            ),
        ])
    if stats["last_row"]:
        row = stats["last_row"]
        ts = parse_audit_timestamp(row.get("timestamp", ""))
        lines.extend([
            "",
            "Last audit action:",
            (
                f"  {row.get('timestamp', '')} ({_fmt_age(_age_minutes(ts, now))}) "
                f"{row.get('command', '')} {row.get('action', '')} "
                f"SOC={row.get('soc', '')}%"
            ),
        ])

    lines.extend(["", "Recommendations:"])
    lines.extend(f"  - {tip}" for tip in recommendations)

    metrics = {
        "days": days,
        "dashboard_age_min": dashboard_age_min,
        "soc": live.get("soc"),
        "mode": live.get("mode"),
        "bypass_detected": bool(live.get("bypass_detected")),
        "projected_sunrise_soc": planner.get("projected_sunrise_soc"),
        "topup_minutes": planner.get("topup_minutes"),
        **stats,
    }
    return OpsReview("\n".join(lines), recommendations, severity, metrics)


def build_ops_review_embed(review: OpsReview) -> dict[str, Any]:
    color = _COLOR_FAIL if review.severity == "fail" else (_COLOR_WARN if review.severity == "warn" else _COLOR_OK)
    metrics = review.metrics
    fields = [
        {"name": "SOC / Mode", "value": f"{_fmt_value(metrics.get('soc'), '%')} / {_fmt_value(metrics.get('mode'))}", "inline": True},
        {"name": "Bypass", "value": "detected" if metrics.get("bypass_detected") else "clear", "inline": True},
        {"name": "Sunrise", "value": _fmt_value(metrics.get("projected_sunrise_soc"), "%"), "inline": True},
        {"name": "Top-ups", "value": f"{metrics.get('topups', 0)} ({metrics.get('topup_minutes', 0)} min)", "inline": True},
        {"name": "Failures", "value": str(metrics.get("failures", 0)), "inline": True},
        {"name": "Watchdog repairs", "value": str(metrics.get("watchdog_repairs", 0)), "inline": True},
        {"name": "Recommendations", "value": "\n".join(f"- {tip}" for tip in review.recommendations[:4])[:1024], "inline": False},
    ]
    return {
        "title": f"Growatt ops review ({metrics.get('days', 7)}d)",
        "color": color,
        "fields": fields,
        "timestamp": utc_now().isoformat(),
    }


def command_ops_review(config: Config, days: int = 7, notify: bool = False) -> int:
    review = build_ops_review(config, days=days)
    if notify:
        if not config.discord_webhook_url:
            raise GrowattGuardError("DISCORD_WEBHOOK_URL must be configured for ops-review --notify.")
        if not send_discord_embed(config, build_ops_review_embed(review)):
            raise GrowattGuardError("Ops review could not be sent to Discord.")
    print(review.text)
    return 0
