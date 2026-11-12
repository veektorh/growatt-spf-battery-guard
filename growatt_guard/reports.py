from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from growatt_guard.audit import (
    build_daily_summary,
    build_monthly_summary,
    build_weekly_summary,
    month_window,
    prune_audit_rows,
)
from growatt_guard.config import Config
from growatt_guard.daily_generation import read_daily_generation
from growatt_guard.load_learning import resolve_timezone
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import load_context
from growatt_guard.notifications import embed_summary, send_discord_embed
from growatt_guard.dashboard_metrics import (
    BYPASS_FULL_SOC,
    read_dashboard_metrics_history,
    summarize_bypass_episodes,
)
from growatt_guard.weather import choose_preserve_threshold
from growatt_guard.paths import DATA_HOME


BASE_DIR = DATA_HOME
LOG_DIR = BASE_DIR / "logs"
ROTATE_LOG_PROTECTED_FILES = {
    "cron.log",
    "dashboard_metrics.jsonl",
    "growatt_power_guard.log",
    "mode_decisions.csv",
}
ROTATE_LOG_GENERATED_PATTERNS = (
    "growatt-probe-*.json",
    ".dashboard_metrics_*.jsonl",
    ".dash_tmp_*.json",
    ".dash_tmp_*.html",
)
ENV_BACKUP_PATTERNS = (".env.pre-*", ".env.before-*", ".env.bak-*")
# A bypass sample this soon after a return-SBU is the inverter draining the tail of
# its grid-charge state, not an automation fault.
BYPASS_RETURN_CONTEXT_MINUTES = 30


def prune_env_backups(cutoff: dt.datetime) -> int:
    """Delete stale .env backup snapshots from the data home. Returns the count removed."""
    removed = 0
    for pattern in ENV_BACKUP_PATTERNS:
        for path in BASE_DIR.glob(pattern):
            if not path.is_file():
                continue
            if path.stat().st_mtime < cutoff.timestamp():
                path.unlink()
                removed += 1
    return removed


def command_daily_summary(config: Config) -> int:
    _, _, status = load_context(config)
    tomorrow_kwh_m2: float | None = None
    if config.weather_lat is not None and config.weather_lon is not None:
        from growatt_guard.weather import get_tomorrow_solar_kwh_m2

        tomorrow_kwh_m2 = get_tomorrow_solar_kwh_m2(config)
    summary = build_daily_summary(status, tomorrow_kwh_m2=tomorrow_kwh_m2)
    summary = f"{summary}\n\n{_bypass_episodes_section(dt.datetime.now(), days=1)}"
    if config.discord_webhook_url:
        send_discord_embed(config, embed_summary("Daily Summary", summary))
    print(summary)
    return 0


def _local_solar_daily_outputs(
    now: dt.datetime,
    *,
    days: int = 7,
    tz: dt.tzinfo | None = None,
) -> dict[str, int]:
    """Return the latest Growatt PV total for each calendar day in Wh.

    Days are bucketed in ``tz``, the estate's timezone, so a host whose zone
    differs from the site cannot move a day's total across midnight.
    """
    if tz is not None:
        now = now.astimezone(tz) if now.tzinfo is not None else now.replace(tzinfo=tz)
    expected_dates = [now.date() - dt.timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    wanted = {day.isoformat() for day in expected_dates}
    latest_by_date: dict[str, tuple[dt.datetime, float]] = {}
    for row in read_dashboard_metrics_history(now=now, days=days + 1):
        timestamp = row.get("timestamp")
        value = row.get("pv_today_kwh")
        if not isinstance(timestamp, str) or not isinstance(value, (int, float)):
            continue
        try:
            parsed = dt.datetime.fromisoformat(timestamp)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = (parsed.astimezone(tz) if tz is not None else parsed.astimezone()).replace(tzinfo=None)
        date_key = parsed.date().isoformat()
        if date_key not in wanted:
            continue
        existing = latest_by_date.get(date_key)
        if existing is None or parsed > existing[0]:
            latest_by_date[date_key] = (parsed, float(value))
    return {
        date_key: round(value_kwh * 1000)
        for date_key, (_, value_kwh) in latest_by_date.items()
    }


def _complete_solar_window(values: dict[str, int], now: dt.datetime, *, days: int = 7) -> bool:
    expected = {
        (now.date() - dt.timedelta(days=offset)).isoformat()
        for offset in range(days - 1, -1, -1)
    }
    return expected.issubset(values)


def _fmt_minutes(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f} min"
    return f"{minutes / 60.0:.1f} h"


def _fmt_soc_range(soc_min: float | None, soc_max: float | None) -> str:
    if soc_min is None or soc_max is None:
        return "SOC ?"
    if soc_min == soc_max:
        return f"{soc_min:g}%"
    return f"{soc_min:g}-{soc_max:g}%"


def _metric_sampling_minutes(
    history: list[dict[str, Any]],
    *,
    now: dt.datetime,
    days: int,
) -> float | None:
    """Median spacing between metric-history samples inside the window, in minutes."""
    cutoff = now - dt.timedelta(days=days)
    stamps: list[dt.datetime] = []
    for row in history:
        raw = row.get("timestamp")
        if not isinstance(raw, str):
            continue
        try:
            parsed = dt.datetime.fromisoformat(raw)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        if parsed < cutoff or parsed > now:
            continue
        stamps.append(parsed)
    stamps.sort()
    gaps = sorted(
        (later - earlier).total_seconds() / 60.0
        for earlier, later in zip(stamps, stamps[1:])
    )
    if not gaps:
        return None
    return gaps[len(gaps) // 2]


def _bypass_contexts(
    now: dt.datetime,
    *,
    days: int,
) -> tuple[list[dt.datetime], list[tuple[dt.datetime, dt.datetime]]]:
    """Return return-SBU times and top-up intervals from the mode audit trail."""
    from growatt_guard.audit import parse_audit_timestamp, read_mode_audit_rows, real_audit_rows

    since = now - dt.timedelta(days=days)
    returns: list[dt.datetime] = []
    topups: list[tuple[dt.datetime, dt.datetime]] = []
    open_start: dt.datetime | None = None
    for row in real_audit_rows(read_mode_audit_rows(since=since)):
        timestamp = parse_audit_timestamp(row.get("timestamp", ""))
        if timestamp is None:
            continue
        action = row.get("action", "")
        if action == "auto-topup-started":
            if open_start is not None:
                topups.append((open_start, timestamp))
            open_start = timestamp
        elif action in {"topup-target-reached", "topup-expired"} and open_start is not None:
            topups.append((open_start, timestamp))
            open_start = None
        elif action == "switch-to-sbu":
            returns.append(timestamp)
            if open_start is not None:
                topups.append((open_start, timestamp))
                open_start = None
    if open_start is not None:
        topups.append((open_start, now))
    return returns, topups


def _episode_context(
    started: dt.datetime,
    returns: list[dt.datetime],
    topups: list[tuple[dt.datetime, dt.datetime]],
) -> str:
    window = dt.timedelta(minutes=BYPASS_RETURN_CONTEXT_MINUTES)
    for returned in returns:
        if dt.timedelta(0) <= started - returned <= window:
            return "post-return-SBU"
    for start, end in topups:
        if start <= started <= end:
            return "during auto-topup"
    return ""


def _bypass_month_section(month_start: dt.date, month_end: dt.date) -> str:
    """Month-scale bypass totals from the durable daily rollup."""
    from growatt_guard.bypass_history import read_bypass_daily

    rows = [
        row
        for row in read_bypass_daily()
        if month_start.isoformat() <= str(row["date"]) <= month_end.isoformat()
    ]
    header = "Grid bypass (month rollup):"
    expected_days = (month_end - month_start).days + 1
    if not rows:
        return f"{header} no rollup coverage for this month."
    episodes = sum(int(row["episodes"]) for row in rows)
    samples = sum(int(row.get("samples", 0)) for row in rows)
    minutes = sum(float(row.get("duration_minutes", 0)) for row in rows)
    full_minutes = sum(float(row.get("full_minutes", 0)) for row in rows)
    grid_kwh = sum(float(row.get("full_grid_kwh", 0)) for row in rows)
    detail = f"  Coverage {len(rows)}/{expected_days} days; {episodes} episode(s), {samples} sample(s)"
    if minutes > 0:
        detail += f", ~{_fmt_minutes(minutes)} bypassed"
    elif episodes:
        detail += ", each shorter than one poll interval"
    lines = [header, detail]
    if full_minutes > 0:
        lines.append(
            f"  at>={BYPASS_FULL_SOC:g}%: {_fmt_minutes(full_minutes)}, {grid_kwh:.1f} kWh grid"
        )
    return "\n".join(lines)


def _bypass_episodes_section(now: dt.datetime, *, days: int) -> str:
    """Read-only summary of grid-bypass episodes from the local metric history."""
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    history = read_dashboard_metrics_history(now=now, days=days)
    report = summarize_bypass_episodes(history, now=now, days=days)
    interval = _metric_sampling_minutes(history, now=now, days=days)
    header = f"Grid bypass episodes (last {days}d, metric-history resolution"
    header += f" ~{interval:.0f} min):" if interval else "):"
    if not report["episodes"]:
        return f"{header} none observed."
    lines = [header]
    returns, topups = _bypass_contexts(now, days=days)
    for episode in report["episodes"]:
        start_dt = dt.datetime.fromisoformat(episode["start"])
        started = start_dt.strftime("%m-%d %H:%M")
        soc = _fmt_soc_range(episode["soc_min"], episode["soc_max"])
        if episode["samples"] <= 1:
            duration = f"1 sample, <{interval:.0f} min" if interval else "1 sample"
        else:
            duration = f"~{_fmt_minutes(episode['duration_minutes'])}"
        detail = f"  {started}  {duration}  {soc}"
        context = _episode_context(start_dt, returns, topups)
        if context:
            detail += f"  ({context})"
        if episode["full_minutes"] > 0:
            detail += (
                f"  at>={BYPASS_FULL_SOC:g}%: {_fmt_minutes(episode['full_minutes'])}"
                f", {episode['full_grid_kwh']:.1f} kWh grid"
            )
            if episode["full_idle_minutes"] > 0:
                detail += f" ({_fmt_minutes(episode['full_idle_minutes'])} not charging)"
        lines.append(detail)
    measured = [episode for episode in report["episodes"] if episode["samples"] > 1]
    single = [episode for episode in report["episodes"] if episode["samples"] <= 1]
    if measured:
        detail = (
            f"~{_fmt_minutes(sum(episode['duration_minutes'] for episode in measured))} bypassed"
            f" across {len(measured)} measured episode(s)"
        )
        if single:
            detail += f"; {len(single)} single-sample detection(s) shorter than the poll interval"
    else:
        detail = f"{len(single)} single-sample detection(s) shorter than the poll interval"
    total = f"  Total: {report['count']} episode(s); {detail}"
    if report["total_full_minutes"] > 0:
        total += (
            f"; at>={BYPASS_FULL_SOC:g}%: {_fmt_minutes(report['total_full_minutes'])},"
            f" {report['total_full_grid_kwh']:.1f} kWh grid"
            f" ({_fmt_minutes(report['total_full_idle_minutes'])} not charging)"
        )
    lines.append(total + ".")
    return "\n".join(lines)


def command_weekly_summary(config: Config) -> int:
    now = dt.datetime.now()
    days = 7
    site_tz = resolve_timezone(getattr(config, "weather_timezone", ""))
    site_now = now.astimezone(site_tz) if site_tz is not None else now
    local_this = _local_solar_daily_outputs(site_now, days=days, tz=site_tz)
    local_this_complete = _complete_solar_window(local_this, site_now, days=days)
    solar_this: dict[str, int] = local_this
    solar_this_source = (
        "local Growatt dashboard history"
        if local_this_complete
        else "local Growatt dashboard history (incomplete)"
    )
    solar_notes: list[str] = []
    solar_last: dict[str, int] = {}
    solar_last_source = ""
    if config.pvoutput_enabled:
        from growatt_guard.pvoutput import fetch_pvoutput_daily_outputs

        pvoutput_this = fetch_pvoutput_daily_outputs(
            config, now.date() - dt.timedelta(days=days - 1), now.date()
        )
        previous_end = now.date() - dt.timedelta(days=days)
        pvoutput_last = fetch_pvoutput_daily_outputs(
            config, previous_end - dt.timedelta(days=days - 1), previous_end
        )
        pvoutput_this_complete = _complete_solar_window(pvoutput_this, now, days=days)
        if not local_this_complete:
            if _complete_solar_window(pvoutput_this, now, days=days):
                solar_this = pvoutput_this
                solar_this_source = "PVOutput"
            elif pvoutput_this:
                if len(pvoutput_this) > len(local_this):
                    solar_this = pvoutput_this
                    solar_this_source = "PVOutput (incomplete)"
                else:
                    solar_this_source = "local Growatt dashboard history (incomplete)"
            else:
                solar_this_source = "local Growatt dashboard history (incomplete)"
        if local_this and not local_this_complete:
            solar_notes.append(
                f"Local Growatt history has {len(local_this)}/{days} days."
            )
        if pvoutput_this and not pvoutput_this_complete:
            solar_notes.append(
                f"PVOutput returned {len(pvoutput_this)}/{days} days; it was not treated as a complete weekly total."
            )
        if pvoutput_last:
            solar_last = pvoutput_last
            solar_last_source = "PVOutput"

    summary = build_weekly_summary(
        now=now,
        solar_this_week=solar_this or None,
        solar_last_week=solar_last or None,
        solar_this_week_source=solar_this_source if solar_this else "",
        solar_last_week_source=solar_last_source,
        solar_this_week_expected_days=days,
        solar_last_week_expected_days=days,
        solar_data_notes=solar_notes,
        charge_rate_w=config.battery_charge_rate_w,
        low_battery_soc=config.low_battery_soc,
        battery_bms_cutoff_soc=config.battery_bms_cutoff_soc,
    )
    summary = f"{summary}\n\n{_bypass_episodes_section(now, days=days)}"
    if config.discord_webhook_url:
        if not send_discord_embed(config, embed_summary("Weekly Summary", summary)):
            raise GrowattGuardError("Weekly summary could not be sent to Discord.")
    print(summary)
    return 0


def _ledger_window(ledger: dict[str, int], start: dt.date, end: dt.date) -> dict[str, int]:
    window: dict[str, int] = {}
    day = start
    while day <= end:
        key = day.isoformat()
        if key in ledger:
            window[key] = ledger[key]
        day += dt.timedelta(days=1)
    return window


def command_monthly_summary(config: Config) -> int:
    now = dt.datetime.now()
    month_start, month_end = month_window(now.date())
    previous_start, previous_end = month_window(month_start - dt.timedelta(days=1))
    ledger = {str(row["date"]): int(row["generated_wh"]) for row in read_daily_generation()}
    solar_this = _ledger_window(ledger, month_start, month_end)
    solar_last = _ledger_window(ledger, previous_start, previous_end)
    ledger_this = len(solar_this)
    ledger_last = len(solar_last)
    notes: list[str] = []
    if config.pvoutput_enabled:
        from growatt_guard.pvoutput import fetch_pvoutput_daily_outputs

        reference_this = fetch_pvoutput_daily_outputs(config, month_start, month_end)
        reference_last = fetch_pvoutput_daily_outputs(config, previous_start, previous_end)
        filled = 0
        for reference, target, window_start, window_end in (
            (reference_this, solar_this, month_start, month_end),
            (reference_last, solar_last, previous_start, previous_end),
        ):
            for day, generated_wh in reference.items():
                try:
                    parsed = dt.date.fromisoformat(day)
                except ValueError:
                    continue
                if not window_start <= parsed <= window_end or day in target:
                    continue
                target[day] = int(generated_wh)
                filled += 1
        drift = [
            day
            for day, generated_wh in reference_this.items()
            if month_start.isoformat() <= day <= month_end.isoformat()
            and day in ledger
            and abs(int(generated_wh) - ledger[day]) > max(200.0, abs(ledger[day]) * 0.02)
        ]
        if filled:
            notes.append(f"Filled {filled} missing day(s) from PVOutput.")
        if drift:
            notes.append(
                f"PVOutput differs from the ledger on {len(drift)} day(s); "
                "run solar-reconcile to review."
            )
    this_source = (
        "daily generation ledger"
        if len(solar_this) == ledger_this
        else ("PVOutput" if ledger_this == 0 else "daily generation ledger + PVOutput")
    )
    last_source = (
        "daily generation ledger"
        if len(solar_last) == ledger_last
        else ("PVOutput" if ledger_last == 0 else "daily generation ledger + PVOutput")
    )
    summary = build_monthly_summary(
        now=now,
        solar_this_month=solar_this or None,
        solar_last_month=solar_last or None,
        solar_this_month_source=this_source if solar_this else "",
        solar_last_month_source=last_source if solar_last else "",
        solar_this_month_expected_days=(month_end - month_start).days + 1 if solar_this else 0,
        solar_last_month_expected_days=(previous_end - previous_start).days + 1 if solar_last else 0,
        solar_data_notes=notes,
    )
    summary = f"{summary}\n\n{_bypass_month_section(month_start, month_end)}"
    if config.discord_webhook_url:
        if not send_discord_embed(config, embed_summary("Monthly Summary", summary)):
            raise GrowattGuardError("Monthly summary could not be sent to Discord.")
    print(summary)
    return 0


def command_rotate_logs(config: Config) -> int:
    cutoff = dt.datetime.now() - dt.timedelta(days=config.log_retention_days)
    removed = 0
    LOG_DIR.mkdir(exist_ok=True)
    candidates: set[Path] = set()
    for pattern in ROTATE_LOG_GENERATED_PATTERNS:
        candidates.update(LOG_DIR.glob(pattern))
    for path in candidates:
        if not path.is_file() or path.name in ROTATE_LOG_PROTECTED_FILES:
            continue
        if path.stat().st_mtime < cutoff.timestamp():
            path.unlink()
            removed += 1
    env_removed = prune_env_backups(cutoff)
    print(f"Removed {removed} old log/probe files older than {config.log_retention_days} days.")
    if env_removed:
        print(
            f"Removed {env_removed} old .env backup file(s) older than "
            f"{config.log_retention_days} days."
        )
    return 0


def command_prune_audit(config: Config) -> int:
    cutoff = dt.datetime.now() - dt.timedelta(days=config.audit_retention_days)
    removed, kept = prune_audit_rows(cutoff)
    if removed == 0:
        print(f"Audit log: {kept} rows, nothing to prune (retention: {config.audit_retention_days} days).")
    else:
        print(f"Audit log pruned: {removed} rows removed, {kept} remaining (retention: {config.audit_retention_days} days).")
    return 0


def command_weather_threshold(config: Config) -> int:
    decision = choose_preserve_threshold(config)
    print(f"Threshold: {decision.threshold:g}%")
    print(f"Category: {decision.weather_category}")
    print(f"Reason: {decision.reason}")
    return 0
