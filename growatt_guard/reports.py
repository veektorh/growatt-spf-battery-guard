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
from growatt_guard.growatt_api import load_context
from growatt_guard.notifications import embed_summary, send_discord_embed
from growatt_guard.weather import choose_preserve_threshold


BASE_DIR = Path(__file__).resolve().parent.parent
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


def command_weekly_summary(config: Config) -> int:
    now = dt.datetime.now()
    since = now - dt.timedelta(days=7)
    previous_week_start = now - dt.timedelta(days=14)

    solar_this: dict = {}
    solar_last: dict = {}
    if config.pvoutput_enabled:
        from growatt_guard.pvoutput import fetch_pvoutput_daily_outputs

        solar_this = fetch_pvoutput_daily_outputs(config, since.date(), now.date())
        solar_last = fetch_pvoutput_daily_outputs(config, previous_week_start.date(), since.date())

    summary = build_weekly_summary(
        now=now,
        solar_this_week=solar_this or None,
        solar_last_week=solar_last or None,
        charge_rate_w=config.battery_charge_rate_w,
        low_battery_soc=config.low_battery_soc,
        battery_bms_cutoff_soc=config.battery_bms_cutoff_soc,
    )
    if config.discord_webhook_url:
        send_discord_embed(config, embed_summary("Weekly Summary", summary))
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
