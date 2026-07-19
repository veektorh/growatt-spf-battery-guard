from __future__ import annotations

import datetime as dt
from pathlib import Path

from growatt_guard.audit import (
    build_daily_summary,
    build_monthly_summary,
    build_weekly_summary,
    prune_audit_rows,
)
from growatt_guard.config import Config
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import load_context
from growatt_guard.notifications import embed_summary, send_discord_embed
from growatt_guard.dashboard_metrics import read_dashboard_metrics_history
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


def command_daily_summary(config: Config) -> int:
    _, _, status = load_context(config)
    tomorrow_kwh_m2: float | None = None
    if config.weather_lat is not None and config.weather_lon is not None:
        from growatt_guard.weather import get_tomorrow_solar_kwh_m2

        tomorrow_kwh_m2 = get_tomorrow_solar_kwh_m2(config)
    summary = build_daily_summary(status, tomorrow_kwh_m2=tomorrow_kwh_m2)
    if config.discord_webhook_url:
        send_discord_embed(config, embed_summary("Daily Summary", summary))
    print(summary)
    return 0


def _local_solar_daily_outputs(
    now: dt.datetime,
    *,
    days: int = 7,
) -> dict[str, int]:
    """Return the latest local Growatt PV total for each calendar day, in Wh."""
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
            parsed = parsed.astimezone().replace(tzinfo=None)
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


def command_weekly_summary(config: Config) -> int:
    now = dt.datetime.now()
    days = 7
    local_this = _local_solar_daily_outputs(now, days=days)
    local_this_complete = _complete_solar_window(local_this, now, days=days)
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
    if config.discord_webhook_url:
        if not send_discord_embed(config, embed_summary("Weekly Summary", summary)):
            raise GrowattGuardError("Weekly summary could not be sent to Discord.")
    print(summary)
    return 0


def command_monthly_summary(config: Config) -> int:
    now = dt.datetime.now()
    this_month_start = now - dt.timedelta(days=30)
    last_month_start = now - dt.timedelta(days=60)

    solar_this: dict = {}
    solar_last: dict = {}
    if config.pvoutput_enabled:
        from growatt_guard.pvoutput import fetch_pvoutput_daily_outputs

        solar_this = fetch_pvoutput_daily_outputs(config, this_month_start.date(), now.date())
        solar_last = fetch_pvoutput_daily_outputs(config, last_month_start.date(), this_month_start.date())

    summary = build_monthly_summary(
        now=now,
        solar_this_month=solar_this or None,
        solar_last_month=solar_last or None,
    )
    if config.discord_webhook_url:
        send_discord_embed(config, embed_summary("Monthly Summary", summary))
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
    print(f"Removed {removed} old log/probe files older than {config.log_retention_days} days.")
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
