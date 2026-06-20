from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import growatt_guard.state as state_files
from growatt_guard.dashboard import (
    DASHBOARD_FILE,
    MIN_DASHBOARD_REFRESH_MINUTES,
    command_dashboard,
    command_dashboard_refresh,
    command_dashboard_stale_alert,
    command_serve_dashboard,
    dashboard_freshness,
    read_dashboard_stale_alert_state,
)
from growatt_guard.growatt_api import (
    DeviceRef,
    describe_status_output_source,
    extract_soc,
    extract_spf_output_source,
    extract_status_soc,
    format_metric,
    load_context,
    render_params,
    set_mode,
    summarize_status,
    write_probe,
)
from growatt_guard.notifications import (
    notify_failure,
    read_growatt_cloud_failure_state,
    record_growatt_cloud_success,
    send_discord_message,
    truncate_discord_message,
)
from growatt_guard.schedule import (
    check_cron_schedule,
    command_validate_schedule,
    find_schedule_job,
    next_scheduled_runs,
    schedule_job_tokens,
    today_schedule_override,
    validate_schedule,
    validate_schedule_overrides,
)
from growatt_guard.state import (
    acquire_command_lock,
    clear_battery_alert_state,
    clear_pause_state,
    command_lock_is_stale,
    format_local_time,
    pause_message,
    read_battery_alert_state,
    read_command_lock_state,
    read_pause_state,
    release_command_lock,
    utc_now,
    write_battery_alert_state,
    write_pause_state,
)
from growatt_guard.weather import (
    ThresholdDecision,
    analyze_weather_window,
    choose_preserve_threshold,
    fetch_weather_forecast,
)

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - handled at runtime for friendlier output
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "growatt_power_guard.log"
MODE_AUDIT_FILE = LOG_DIR / "mode_decisions.csv"

PAUSABLE_COMMANDS = {"preserve-battery", "utility-check", "morning-check", "return-sbu", "watchdog-sbu"}
LOCKED_COMMANDS = PAUSABLE_COMMANDS


class GrowattGuardError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    server_url: str
    plant_id: str | None
    device_sn: str | None
    low_battery_soc: float
    dry_run: bool
    mode_driver: str
    set_mode_path: str
    set_mode_method: str
    utility_mode_params: str
    sbu_mode_params: str
    discord_webhook_url: str
    discord_notify_success: bool
    discord_notify_skip: bool
    discord_notify_failure: bool
    log_retention_days: int
    emergency_soc: float = 30
    emergency_soc_recovery: float = 35
    cloud_failure_alert_threshold: int = 3
    dashboard_stale_minutes: float = 30
    weather_enabled: bool = False
    weather_lat: float | None = None
    weather_lon: float | None = None
    weather_timezone: str = "Africa/Lagos"
    weather_lookahead_hours: int = 4
    weather_cloudy_threshold: float = 70
    weather_sunny_threshold: float = 35
    weather_rain_threshold_mm: float = 1
    low_battery_soc_normal: float = 45
    low_battery_soc_sunny: float = 40


@dataclass(frozen=True)
class HealthCheckItem:
    name: str
    status: str
    detail: str


def str_to_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def optional_float(value: str) -> float | None:
    return float(value) if value else None


def load_config() -> Config:
    if load_dotenv is not None:
        load_dotenv(BASE_DIR / ".env")

    username = env("GROWATT_USERNAME")
    password = env("GROWATT_PASSWORD")
    if not username or not password:
        raise GrowattGuardError(
            "Missing GROWATT_USERNAME or GROWATT_PASSWORD. Copy .env.example to .env and fill them in."
        )

    mode_driver = env("GROWATT_MODE_DRIVER", "spf5000").lower()
    utility_mode_params = env("GROWATT_UTILITY_MODE_PARAMS")
    sbu_mode_params = env("GROWATT_SBU_MODE_PARAMS")
    if mode_driver == "custom" and not utility_mode_params and not sbu_mode_params:
        mode_driver = "spf5000"

    return Config(
        username=username,
        password=password,
        server_url=env("GROWATT_SERVER_URL", "https://openapi.growatt.com/"),
        plant_id=env("GROWATT_PLANT_ID") or None,
        device_sn=env("GROWATT_DEVICE_SN") or None,
        low_battery_soc=float(env("LOW_BATTERY_SOC", "45")),
        dry_run=str_to_bool(env("DRY_RUN"), default=True),
        mode_driver=mode_driver,
        set_mode_path=env("GROWATT_SET_MODE_PATH", "tcpSet.do"),
        set_mode_method=env("GROWATT_SET_MODE_METHOD", "post").lower(),
        utility_mode_params=utility_mode_params,
        sbu_mode_params=sbu_mode_params,
        discord_webhook_url=env("DISCORD_WEBHOOK_URL"),
        discord_notify_success=str_to_bool(env("DISCORD_NOTIFY_SUCCESS"), default=True),
        discord_notify_skip=str_to_bool(env("DISCORD_NOTIFY_SKIP"), default=False),
        discord_notify_failure=str_to_bool(env("DISCORD_NOTIFY_FAILURE"), default=True),
        log_retention_days=int(env("LOG_RETENTION_DAYS", "30")),
        emergency_soc=float(env("EMERGENCY_SOC", "30")),
        emergency_soc_recovery=float(env("EMERGENCY_SOC_RECOVERY", "35")),
        cloud_failure_alert_threshold=int(env("GROWATT_CLOUD_FAILURE_ALERT_THRESHOLD", "3")),
        dashboard_stale_minutes=float(env("DASHBOARD_STALE_MINUTES", "30")),
        weather_enabled=str_to_bool(env("WEATHER_ENABLED"), default=False),
        weather_lat=optional_float(env("WEATHER_LAT")),
        weather_lon=optional_float(env("WEATHER_LON")),
        weather_timezone=env("WEATHER_TIMEZONE", "Africa/Lagos"),
        weather_lookahead_hours=int(env("WEATHER_LOOKAHEAD_HOURS", "4")),
        weather_cloudy_threshold=float(env("WEATHER_CLOUDY_THRESHOLD", "70")),
        weather_sunny_threshold=float(env("WEATHER_SUNNY_THRESHOLD", "35")),
        weather_rain_threshold_mm=float(env("WEATHER_RAIN_THRESHOLD_MM", "1")),
        low_battery_soc_normal=float(env("LOW_BATTERY_SOC_NORMAL", "45")),
        low_battery_soc_sunny=float(env("LOW_BATTERY_SOC_SUNNY", "40")),
    )


def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    console_handler.setLevel(level)
    root.addHandler(console_handler)


def ensure_not_paused(config: Config, command: str) -> bool:
    state = read_pause_state()
    if not state:
        return False

    message = f"Skipped `{command}` because {pause_message(state)}."
    logging.info(message)
    if config.discord_notify_skip:
        send_discord_message(config, message)
    print(message)
    return True


def run_with_command_lock(config: Config, command: str, action) -> int:
    token = acquire_command_lock(command)
    if token is None:
        state = read_command_lock_state() or {}
        locked_command = state.get("command", "another command")
        created_at = state.get("created_at", "unknown time")
        message = f"Skipped `{command}` because `{locked_command}` is already running since {created_at}."
        logging.warning(message)
        if config.discord_notify_skip:
            send_discord_message(config, message)
        print(message)
        return 0
    try:
        return action()
    finally:
        release_command_lock(token)


def summarize_today_log_counts() -> dict[str, int]:
    today = dt.datetime.now().strftime("%Y-%m-%d")
    counts = {
        "success": 0,
        "failure": 0,
        "watchdog_repairs": 0,
        "preserve_actions": 0,
        "return_sbu_actions": 0,
    }
    if not LOG_FILE.exists():
        return counts

    for line in LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(today):
            continue
        lower = line.lower()
        if "inv_set_success" in lower or "mode response" in lower:
            counts["success"] += 1
        if " error " in lower or " failed" in lower or "unhandled error" in lower:
            counts["failure"] += 1
        if "watchdog detected" in lower or "watchdog repaired" in lower:
            counts["watchdog_repairs"] += 1
        if "switching to utility" in lower:
            counts["preserve_actions"] += 1
        if "sbu mode response" in lower:
            counts["return_sbu_actions"] += 1
    return counts


MODE_AUDIT_FIELDS = (
    "timestamp",
    "command",
    "soc",
    "threshold",
    "weather_category",
    "previous_mode",
    "action",
    "dry_run",
    "result",
    "note",
)


def format_audit_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True)
    else:
        text = str(value)
    if len(text) > 500:
        return text[:497] + "..."
    return text


def append_mode_audit(
    config: Config,
    command: str,
    *,
    soc: float | None = None,
    threshold: float | None = None,
    weather_category: str = "",
    previous_mode: str = "",
    action: str = "",
    result: Any = None,
    note: str = "",
) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    write_header = not MODE_AUDIT_FILE.exists()
    row = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "command": command,
        "soc": format_audit_value(soc),
        "threshold": format_audit_value(threshold),
        "weather_category": weather_category,
        "previous_mode": previous_mode,
        "action": action,
        "dry_run": str(config.dry_run).lower(),
        "result": format_audit_value(result),
        "note": note,
    }
    with MODE_AUDIT_FILE.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MODE_AUDIT_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def parse_audit_timestamp(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def read_mode_audit_rows(
    *,
    since: dt.datetime | None = None,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[dict[str, str]]:
    if not MODE_AUDIT_FILE.exists():
        return []
    with MODE_AUDIT_FILE.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if since is not None:
        rows = [
            row
            for row in rows
            if (timestamp := parse_audit_timestamp(row.get("timestamp", ""))) is not None and timestamp >= since
        ]
    if newest_first:
        rows = list(reversed(rows))
    if limit is not None:
        rows = rows[:limit]
    return rows


def parse_audit_float(row: dict[str, str], key: str) -> float | None:
    value = row.get(key, "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_weekly_summary(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    since = now - dt.timedelta(days=7)
    rows = read_mode_audit_rows(since=since)
    preserve_rows = [row for row in rows if row.get("command") == "preserve-battery"]
    preserve_socs = [soc for row in preserve_rows if (soc := parse_audit_float(row, "soc")) is not None]
    utility_switches = [row for row in rows if row.get("action") == "switch-to-utility"]
    preserve_no_changes = [row for row in rows if row.get("command") == "preserve-battery" and row.get("action") == "no-change"]
    return_sbu = [row for row in rows if row.get("action") == "switch-to-sbu"]
    watchdog_repairs = [row for row in rows if row.get("action") == "repair-sbu"]
    failures = [row for row in rows if row.get("action", "").endswith("-failed") or row.get("result") == "error"]
    last_row = rows[-1] if rows else None

    avg_soc = average(preserve_socs)
    lines = [
        f"Growatt weekly performance - {since.strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')}",
        f"Audit rows: {len(rows)}",
        f"Preserve-battery checks: {len(preserve_rows)}",
        f"Utility switches: {len(utility_switches)}",
        f"No-change preserve checks: {len(preserve_no_changes)}",
        f"Return-SBU switches: {len(return_sbu)}",
        f"Watchdog repairs: {len(watchdog_repairs)}",
        f"Failures: {len(failures)}",
    ]
    if avg_soc is not None:
        lines.append(f"Average preserve-check SOC: {avg_soc:g}%")
        lines.append(f"Lowest preserve-check SOC: {min(preserve_socs):g}%")
    else:
        lines.append("Average preserve-check SOC: not enough data")
    if last_row:
        lines.append(
            "Last action: "
            f"{last_row.get('timestamp', '')} {last_row.get('command', '')} "
            f"{last_row.get('action', '')} SOC={last_row.get('soc', '')}%"
        )
    return "\n".join(lines)


def command_status(config: Config) -> int:
    _, _, status = load_context(config)
    print(summarize_status(status))
    return 0


def command_probe(config: Config) -> int:
    _, _, status = load_context(config)
    path = write_probe(status)
    print(summarize_status(status))
    print(f"Wrote redacted probe data to {path}")
    return 0


def command_preserve_battery(config: Config) -> int:
    if ensure_not_paused(config, "preserve-battery"):
        return 0

    api, device, status = load_context(config)
    soc_result = extract_soc(status)
    if not soc_result:
        raise GrowattGuardError("Could not find battery SOC in Growatt response. Run the probe command.")

    soc, path = soc_result
    previous_mode = describe_status_output_source(status)
    threshold_decision = choose_preserve_threshold(config)
    threshold = threshold_decision.threshold
    logging.info("Preserve-battery threshold: %.1f%% (%s)", threshold, threshold_decision.reason)

    if soc < threshold:
        logging.info("Battery SOC %.1f%% from %s is below %.1f%%; switching to Utility.", soc, path, threshold)
        try:
            result = set_mode(api, config, device, "utility")
        except Exception as exc:  # noqa: BLE001 - audit failed mode decisions before re-raising
            append_mode_audit(
                config,
                "preserve-battery",
                soc=soc,
                threshold=threshold,
                weather_category=threshold_decision.weather_category,
                previous_mode=previous_mode,
                action="switch-to-utility-failed",
                result="error",
                note=str(exc),
            )
            raise
        append_mode_audit(
            config,
            "preserve-battery",
            soc=soc,
            threshold=threshold,
            weather_category=threshold_decision.weather_category,
            previous_mode=previous_mode,
            action="switch-to-utility",
            result=result,
            note=f"SOC from {path}",
        )
        if config.discord_notify_success and not config.dry_run:
            send_discord_message(
                config,
                (
                    "Growatt preserve-battery action completed.\n"
                    f"SOC `{soc:g}%` is below threshold `{threshold:g}%`; "
                    "switched to `Utility first`.\n"
                    f"Reason: {threshold_decision.reason}."
                ),
            )
        print(f"SOC {soc:g}% < {threshold:g}%; Utility command result: {result}")
        print(f"Threshold reason: {threshold_decision.reason}")
    else:
        logging.info("Battery SOC %.1f%% is not below %.1f%%; leaving SBU as-is.", soc, threshold)
        append_mode_audit(
            config,
            "preserve-battery",
            soc=soc,
            threshold=threshold,
            weather_category=threshold_decision.weather_category,
            previous_mode=previous_mode,
            action="no-change",
            result="skipped",
            note=f"SOC from {path}",
        )
        if config.discord_notify_skip:
            send_discord_message(
                config,
                (
                    "Growatt preserve-battery check skipped.\n"
                    f"SOC `{soc:g}%` is at or above threshold `{threshold:g}%`; no switch needed.\n"
                    f"Reason: {threshold_decision.reason}."
                ),
            )
        print(f"SOC {soc:g}% >= {threshold:g}%; no switch needed.")
        print(f"Threshold reason: {threshold_decision.reason}")
    return 0


def command_utility_check(config: Config) -> int:
    return command_preserve_battery(config)


def command_morning_check(config: Config) -> int:
    return command_preserve_battery(config)


def command_return_sbu(config: Config) -> int:
    if ensure_not_paused(config, "return-sbu"):
        return 0

    api, device, status = load_context(config)
    soc = extract_status_soc(status)
    previous_mode = describe_status_output_source(status)
    try:
        result = set_mode(api, config, device, "sbu")
    except Exception as exc:  # noqa: BLE001 - audit failed mode decisions before re-raising
        append_mode_audit(
            config,
            "return-sbu",
            soc=soc,
            previous_mode=previous_mode,
            action="switch-to-sbu-failed",
            result="error",
            note=str(exc),
        )
        raise
    append_mode_audit(
        config,
        "return-sbu",
        soc=soc,
        previous_mode=previous_mode,
        action="switch-to-sbu",
        result=result,
    )
    if config.discord_notify_success and not config.dry_run:
        send_discord_message(config, "Growatt return-sbu action completed.\nSwitched to `SBU priority`.")
    print(f"SBU command result: {result}")
    return 0


def command_watchdog_sbu(config: Config) -> int:
    if ensure_not_paused(config, "watchdog-sbu"):
        return 0

    api, device, status = load_context(config)
    output_source = extract_spf_output_source(status)
    soc = extract_status_soc(status)
    previous_mode = describe_status_output_source(status)
    if not output_source:
        message = "Could not read current SPF output source; cannot verify SBU mode."
        append_mode_audit(
            config,
            "watchdog-sbu",
            soc=soc,
            previous_mode=previous_mode,
            action="verify-sbu-failed",
            result="error",
            note=message,
        )
        if config.discord_notify_failure:
            send_discord_message(config, f"Growatt SBU watchdog could not verify mode.\n{message}")
        raise GrowattGuardError(message)

    raw, label, path = output_source
    if raw == "0":
        logging.info("SBU watchdog OK: output=%s [%s] from %s", label, raw, path)
        append_mode_audit(
            config,
            "watchdog-sbu",
            soc=soc,
            previous_mode=previous_mode,
            action="verified-sbu",
            result="ok",
            note=f"output from {path}",
        )
        print(f"SBU watchdog OK: output={label} [{raw}]")
        return 0

    logging.warning("SBU watchdog detected output=%s [%s] from %s; retrying SBU.", label, raw, path)
    try:
        result = set_mode(api, config, device, "sbu")
    except Exception as exc:  # noqa: BLE001 - audit failed mode decisions before re-raising
        append_mode_audit(
            config,
            "watchdog-sbu",
            soc=soc,
            previous_mode=previous_mode,
            action="repair-sbu-failed",
            result="error",
            note=str(exc),
        )
        raise
    append_mode_audit(
        config,
        "watchdog-sbu",
        soc=soc,
        previous_mode=previous_mode,
        action="repair-sbu",
        result=result,
        note=f"output from {path}",
    )
    message = (
        "Growatt SBU watchdog repaired output source.\n"
        f"Detected `{label}` [{raw}] from `{path}`; retried `SBU priority`.\n"
        f"Growatt response: `{result}`"
    )
    if config.discord_notify_failure and not config.dry_run:
        send_discord_message(config, message)
    print(message)
    return 0


def build_daily_summary(status: dict[str, Any]) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"Growatt daily summary - {now}"]

    state = read_pause_state()
    if state:
        lines.append(f"Automation pause: {pause_message(state)}")

    soc_result = extract_soc(status)
    if soc_result:
        soc, _ = soc_result
        lines.append(f"Battery SOC: {soc:g}%")

    output_source = extract_spf_output_source(status)
    if output_source:
        raw, label, _ = output_source
        lines.append(f"Output source: {label} [{raw}]")

    metric_specs = [
        ("PV power", ("ppvText", "ppv"), " W"),
        ("Grid voltage", ("vGridText", "vGrid"), " V"),
        ("Output power", ("outPutPowerText", "outPutPower", "activePower"), " W"),
        ("Battery charge power", ("pChargeText", "pCharge"), " W"),
        ("Battery discharge power", ("pDischargeText", "pDischarge"), " W"),
        ("Energy charged today", ("eChargeTodayText", "eChargeToday"), " kWh"),
        ("AC charge today", ("eacChargeToday", "eacChargeTodayText"), " kWh"),
        ("Energy discharged today", ("eDischargeTodayText", "eDischargeToday"), " kWh"),
    ]
    for label, keys, unit in metric_specs:
        formatted = format_metric(status, label, keys, unit)
        if formatted:
            lines.append(formatted)

    counts = summarize_today_log_counts()
    lines.extend(
        [
            "",
            "Automation today:",
            f"Successful mode responses: {counts['success']}",
            f"Failures/errors: {counts['failure']}",
            f"Preserve-battery actions: {counts['preserve_actions']}",
            f"Return-SBU actions: {counts['return_sbu_actions']}",
            f"Watchdog repairs: {counts['watchdog_repairs']}",
        ]
    )
    return "\n".join(lines)


def command_daily_summary(config: Config) -> int:
    _, _, status = load_context(config)
    summary = build_daily_summary(status)
    if config.discord_webhook_url:
        send_discord_message(config, summary)
    print(summary)
    return 0


def command_weekly_summary(config: Config) -> int:
    summary = build_weekly_summary()
    if config.discord_webhook_url:
        send_discord_message(config, summary)
    print(summary)
    return 0


def command_rotate_logs(config: Config) -> int:
    cutoff = dt.datetime.now() - dt.timedelta(days=config.log_retention_days)
    removed = 0
    LOG_DIR.mkdir(exist_ok=True)
    for path in LOG_DIR.iterdir():
        if not path.is_file():
            continue
        if path.name in {"growatt_power_guard.log", "cron.log"}:
            continue
        if path.stat().st_mtime < cutoff.timestamp():
            path.unlink()
            removed += 1
    print(f"Removed {removed} old log/probe files older than {config.log_retention_days} days.")
    return 0


def command_weather_threshold(config: Config) -> int:
    decision = choose_preserve_threshold(config)
    print(f"Threshold: {decision.threshold:g}%")
    print(f"Category: {decision.weather_category}")
    print(f"Reason: {decision.reason}")
    return 0


def command_battery_alert(config: Config) -> int:
    _, _, status = load_context(config)
    soc_result = extract_soc(status)
    if not soc_result:
        raise GrowattGuardError("Could not find battery SOC in Growatt response. Run the probe command.")

    soc, path = soc_result
    previous_mode = describe_status_output_source(status) or "unknown"
    state = read_battery_alert_state()
    recovery_soc = max(config.emergency_soc_recovery, config.emergency_soc)

    if soc < config.emergency_soc:
        if state and state.get("active"):
            print(
                f"Emergency battery alert already active: SOC {soc:g}% < "
                f"{config.emergency_soc:g}% ({previous_mode})."
            )
            return 0
        if not config.discord_webhook_url:
            raise GrowattGuardError("DISCORD_WEBHOOK_URL must be configured for emergency battery alerts.")

        message = (
            "Growatt emergency battery alert.\n"
            f"SOC `{soc:g}%` is below emergency threshold `{config.emergency_soc:g}%`.\n"
            f"Current output source: `{previous_mode}`.\n"
            f"SOC source: `{path}`."
        )
        if not send_discord_message(config, message):
            raise GrowattGuardError("Emergency battery alert could not be sent to Discord.")
        write_battery_alert_state(soc)
        print(f"Emergency battery alert sent: SOC {soc:g}% < {config.emergency_soc:g}%.")
        return 0

    if state and state.get("active") and soc >= recovery_soc:
        clear_battery_alert_state()
        message = (
            "Growatt battery alert recovered.\n"
            f"SOC `{soc:g}%` is now at or above recovery threshold `{recovery_soc:g}%`.\n"
            f"Current output source: `{previous_mode}`."
        )
        if config.discord_webhook_url:
            send_discord_message(config, message)
        print(f"Emergency battery alert cleared: SOC {soc:g}% >= {recovery_soc:g}%.")
        return 0

    print(f"Battery alert OK: SOC {soc:g}% >= {config.emergency_soc:g}% ({previous_mode}).")
    return 0


def command_pause(config: Config, hours: float, reason: str) -> int:
    if hours <= 0:
        raise GrowattGuardError("--hours must be greater than 0.")
    state = write_pause_state(hours, reason)
    message = f"Growatt automation paused until {format_local_time(state['paused_until_dt'])}."
    if reason:
        message += f"\nReason: {reason}"
    send_discord_message(config, message)
    print(message)
    return 0


def command_resume(config: Config) -> int:
    was_paused = read_pause_state() is not None
    clear_pause_state()
    message = "Growatt automation resumed." if was_paused else "Growatt automation was not paused."
    send_discord_message(config, message)
    print(message)
    return 0


def command_pause_status(config: Config) -> int:
    _ = config
    state = read_pause_state()
    if not state:
        print("Growatt automation is active.")
        return 0
    print(f"Growatt automation is paused: {pause_message(state)}.")
    return 0


def health_result(checks: list[HealthCheckItem]) -> str:
    statuses = {check.status for check in checks}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "OK"


def format_health_report(checks: list[HealthCheckItem]) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    result = health_result(checks)
    lines = [f"Growatt health check - {now}", f"Result: {result}", ""]
    for check in checks:
        detail = " ".join(str(check.detail).split())
        lines.append(f"[{check.status}] {check.name}: {detail}")
    return "\n".join(lines)


def command_health_check(config: Config, notify: bool = False) -> int:
    checks: list[HealthCheckItem] = [
        HealthCheckItem("Config", "OK", ".env loaded and required Growatt credentials are present."),
        HealthCheckItem(
            "Dry run",
            "WARN" if config.dry_run else "OK",
            "DRY_RUN=true; mode-changing commands will only simulate." if config.dry_run else "DRY_RUN=false.",
        ),
    ]

    if config.emergency_soc_recovery <= config.emergency_soc:
        checks.append(
            HealthCheckItem(
                "Emergency alert",
                "WARN",
                (
                    f"alerts below {config.emergency_soc:g}%, but recovery "
                    f"{config.emergency_soc_recovery:g}% is not above the alert threshold."
                ),
            )
        )
    elif not config.discord_webhook_url:
        checks.append(
            HealthCheckItem(
                "Emergency alert",
                "WARN",
                f"alerts below {config.emergency_soc:g}%, but DISCORD_WEBHOOK_URL is not configured.",
            )
        )
    else:
        checks.append(
            HealthCheckItem(
                "Emergency alert",
                "OK",
                f"alerts below {config.emergency_soc:g}% and clears at {config.emergency_soc_recovery:g}%.",
            )
        )

    cloud_state = read_growatt_cloud_failure_state()
    if cloud_state:
        count = int(cloud_state.get("count", 0))
        threshold = int(cloud_state.get("threshold", config.cloud_failure_alert_threshold))
        status = "WARN" if cloud_state.get("alerted") else "OK"
        checks.append(
            HealthCheckItem(
                "Growatt cloud streak",
                status,
                f"{count}/{threshold} consecutive failure(s); last command {cloud_state.get('last_command', 'unknown')}.",
            )
        )
    else:
        checks.append(
            HealthCheckItem(
                "Growatt cloud streak",
                "OK",
                f"no active failure streak; alert threshold is {config.cloud_failure_alert_threshold}.",
            )
        )

    try:
        freshness = dashboard_freshness(DASHBOARD_FILE, config.dashboard_stale_minutes)
    except OSError as exc:
        checks.append(HealthCheckItem("Dashboard freshness", "WARN", f"could not inspect dashboard.html: {exc}"))
    else:
        status = "WARN" if freshness["stale"] else "OK"
        checks.append(
            HealthCheckItem(
                "Dashboard freshness",
                status,
                f"{freshness['reason']}; stale threshold is {config.dashboard_stale_minutes:g} minutes.",
            )
        )

    if config.mode_driver not in {"spf5000", "spf", "custom"}:
        checks.append(
            HealthCheckItem(
                "Mode driver",
                "FAIL",
                f"GROWATT_MODE_DRIVER={config.mode_driver!r} is unsupported; mode changes will fail.",
            )
        )
    elif config.mode_driver == "custom":
        if not config.utility_mode_params:
            checks.append(HealthCheckItem("Utility command", "FAIL", "custom driver missing GROWATT_UTILITY_MODE_PARAMS."))
        if not config.sbu_mode_params:
            checks.append(HealthCheckItem("SBU command", "FAIL", "custom driver missing GROWATT_SBU_MODE_PARAMS."))
        if config.utility_mode_params and config.sbu_mode_params:
            checks.append(HealthCheckItem("Mode driver", "OK", "custom mode driver parameters are configured."))
    else:
        checks.append(HealthCheckItem("Mode driver", "OK", "SPF output-source command driver is configured."))

    schedule: dict[str, Any] | None = None
    try:
        schedule = validate_schedule()
        checks.append(HealthCheckItem("Schedule", "OK", f"{len(schedule['jobs'])} jobs in {schedule['timezone']}."))
    except GrowattGuardError as exc:
        checks.append(HealthCheckItem("Schedule", "FAIL", str(exc)))

    if schedule is not None:
        try:
            overrides = validate_schedule_overrides(schedule)
        except GrowattGuardError as exc:
            checks.append(HealthCheckItem("Schedule overrides", "FAIL", str(exc)))
        else:
            count = len(overrides.get("dates", {}))
            detail = f"{count} date override(s) configured." if count else "no local date overrides configured."
            checks.append(HealthCheckItem("Schedule overrides", "OK", detail))
        checks.extend(check_cron_schedule(schedule))

    try:
        _, device, status = load_context(config)
    except Exception as exc:  # noqa: BLE001 - health check should continue reporting other checks
        checks.append(HealthCheckItem("Growatt cloud", "FAIL", str(exc)))
    else:
        checks.append(
            HealthCheckItem(
                "Growatt cloud",
                "OK",
                f"login ok; plant={device.plant_id}, device={device.device_sn}, type={device.device_type or 'unknown'}.",
            )
        )

        soc_result = extract_soc(status)
        if soc_result:
            soc, path = soc_result
            checks.append(HealthCheckItem("Battery SOC", "OK", f"{soc:g}% from {path}."))
        else:
            checks.append(HealthCheckItem("Battery SOC", "FAIL", "SOC was not found in the Growatt status response."))

        output_source = extract_spf_output_source(status)
        if output_source:
            raw, label, path = output_source
            checks.append(HealthCheckItem("Output source", "OK", f"{label} [{raw}] from {path}."))
        else:
            checks.append(
                HealthCheckItem("Output source", "FAIL", "SPF output source was not found in the Growatt status response.")
            )

    threshold_decision = choose_preserve_threshold(config)
    threshold_status = "WARN" if threshold_decision.weather_category == "unavailable" else "OK"
    checks.append(
        HealthCheckItem(
            "Preserve threshold",
            threshold_status,
            f"{threshold_decision.threshold:g}% ({threshold_decision.reason}).",
        )
    )

    pause_state = read_pause_state()
    if pause_state:
        checks.append(HealthCheckItem("Pause state", "WARN", pause_message(pause_state)))
    elif state_files.PAUSE_FILE.exists():
        checks.append(HealthCheckItem("Pause state", "WARN", "pause file exists but could not be read; automation is active."))
    else:
        checks.append(HealthCheckItem("Pause state", "OK", "automation is active."))

    lock_state = read_command_lock_state()
    if lock_state and command_lock_is_stale():
        checks.append(HealthCheckItem("Command lock", "WARN", "stale mode-command lock file is present."))
    elif lock_state:
        checks.append(
            HealthCheckItem(
                "Command lock",
                "WARN",
                f"{lock_state.get('command', 'unknown command')} has held the mode lock since {lock_state.get('created_at')}.",
            )
        )
    else:
        checks.append(HealthCheckItem("Command lock", "OK", "no active mode-command lock."))

    if notify:
        if not config.discord_webhook_url:
            checks.append(HealthCheckItem("Discord report", "FAIL", "DISCORD_WEBHOOK_URL is not configured."))
        elif send_discord_message(config, format_health_report(checks)):
            checks.append(HealthCheckItem("Discord report", "OK", "health report sent."))
        else:
            checks.append(HealthCheckItem("Discord report", "FAIL", "Discord webhook rejected the health report."))

    print(format_health_report(checks))
    return 1 if health_result(checks) == "FAIL" else 0


def command_run_scheduled(config: Config, job_id: str) -> int:
    schedule = validate_schedule()
    job, index = find_schedule_job(schedule, job_id)
    overrides = validate_schedule_overrides(schedule)
    override = today_schedule_override(overrides)
    today = dt.date.today().isoformat()
    note = str(override.get("note", "")).strip()

    if override.get("skip_all") or job_id in override.get("skip", []):
        message = f"Skipped scheduled job `{job_id}` for {today} due to schedule override."
        if note:
            message += f" Note: {note}"
        logging.info(message)
        if config.discord_notify_skip:
            send_discord_message(config, message)
        print(message)
        return 0

    replacement = override.get("replace", {}).get(job_id) if isinstance(override.get("replace", {}), dict) else None
    if replacement:
        tokens = schedule_job_tokens(replacement, 0)
        logging.info("Running schedule override for %s: %s", job_id, " ".join(tokens))
    else:
        tokens = schedule_job_tokens(job, index)

    args = parse_command_tokens(tokens)
    return dispatch_command(config, args)


def command_test_discord(config: Config) -> int:
    if not config.discord_webhook_url:
        raise GrowattGuardError("DISCORD_WEBHOOK_URL is not configured in .env.")
    ok = send_discord_message(config, "Growatt Guard Discord test message.")
    if not ok:
        raise GrowattGuardError("Discord test message failed. Check the webhook URL and network access.")
    print("Discord test message sent.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Growatt SPF battery-preservation automation.")
    parser.add_argument("--verbose", action="store_true", help="Log extra details.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Log in, select plant/device, and print battery SOC.")
    subparsers.add_parser("probe", help="Write redacted raw Growatt responses to logs/ for setup.")
    subparsers.add_parser("preserve-battery", help="Switch to Utility if battery SOC is below LOW_BATTERY_SOC.")
    subparsers.add_parser("utility-check", help="Alias for preserve-battery.")
    subparsers.add_parser("morning-check", help="Alias for preserve-battery.")
    subparsers.add_parser("return-sbu", help="Switch back to SBU.")
    subparsers.add_parser("watchdog-sbu", help="Verify output source is SBU; retry SBU once if needed.")
    subparsers.add_parser("daily-summary", help="Post/print a daily Growatt and automation summary.")
    subparsers.add_parser("weekly-summary", help="Post/print a weekly automation performance summary.")
    subparsers.add_parser("rotate-logs", help="Delete old generated probe/log files according to LOG_RETENTION_DAYS.")
    subparsers.add_parser("weather-threshold", help="Print the current weather-aware preserve-battery threshold.")
    subparsers.add_parser("battery-alert", help="Send a Discord alert if battery SOC is below EMERGENCY_SOC.")
    subparsers.add_parser("validate-schedule", help="Validate schedule.json.")
    subparsers.add_parser("test-discord", help="Send a test Discord webhook message.")
    run_parser = subparsers.add_parser("run-scheduled", help="Run a schedule job by id, applying date overrides first.")
    run_parser.add_argument("job_id", help="Schedule job id from schedule.json.")
    health_parser = subparsers.add_parser("health-check", help="Run read-only configuration and connectivity checks.")
    health_parser.add_argument("--notify", action="store_true", help="Post the health report to Discord.")
    dashboard_parser = subparsers.add_parser("dashboard", help="Generate a small local HTML dashboard.")
    dashboard_parser.add_argument("--output", default=str(DASHBOARD_FILE), help="Dashboard HTML output path.")
    refresh_parser = subparsers.add_parser("dashboard-refresh", help="Regenerate dashboard.html on a safe interval.")
    refresh_parser.add_argument("--output", default=str(DASHBOARD_FILE), help="Dashboard HTML output path.")
    refresh_parser.add_argument(
        "--interval-minutes",
        type=float,
        default=10,
        help=f"Refresh interval. Minimum {MIN_DASHBOARD_REFRESH_MINUTES} minutes unless --once is used.",
    )
    refresh_parser.add_argument("--once", action="store_true", help="Refresh once, then exit.")
    stale_parser = subparsers.add_parser("dashboard-stale-alert", help="Alert if dashboard.html has not refreshed recently.")
    stale_parser.add_argument("--output", default=str(DASHBOARD_FILE), help="Dashboard HTML file to check.")
    stale_parser.add_argument(
        "--max-age-minutes",
        type=float,
        default=None,
        help="Override DASHBOARD_STALE_MINUTES for this check.",
    )
    serve_parser = subparsers.add_parser("serve-dashboard", help="Serve dashboard.html without calling Growatt.")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind host. Use 127.0.0.1 for SSH tunnel access.")
    serve_parser.add_argument("--port", type=int, default=8080, help="Bind port.")
    serve_parser.add_argument("--output", default=str(DASHBOARD_FILE), help="Dashboard HTML file to serve.")
    pause_parser = subparsers.add_parser("pause", help="Pause scheduled mode-changing automation.")
    pause_parser.add_argument("--hours", type=float, required=True, help="How long to pause automation for.")
    pause_parser.add_argument("--reason", default="", help="Optional reason stored in pause state and Discord alert.")
    subparsers.add_parser("resume", help="Resume scheduled mode-changing automation.")
    subparsers.add_parser("pause-status", help="Show whether automation is currently paused.")
    return parser


def parse_command_tokens(tokens: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(tokens)


def dispatch_command(config: Config, args: argparse.Namespace) -> int:
    command = args.command

    def action() -> int:
        if command == "status":
            return command_status(config)
        if command == "probe":
            return command_probe(config)
        if command == "preserve-battery":
            return command_preserve_battery(config)
        if command == "utility-check":
            return command_utility_check(config)
        if command == "morning-check":
            return command_morning_check(config)
        if command == "return-sbu":
            return command_return_sbu(config)
        if command == "watchdog-sbu":
            return command_watchdog_sbu(config)
        if command == "daily-summary":
            return command_daily_summary(config)
        if command == "weekly-summary":
            return command_weekly_summary(config)
        if command == "rotate-logs":
            return command_rotate_logs(config)
        if command == "weather-threshold":
            return command_weather_threshold(config)
        if command == "battery-alert":
            return command_battery_alert(config)
        if command == "test-discord":
            return command_test_discord(config)
        if command == "health-check":
            return command_health_check(config, args.notify)
        if command == "dashboard":
            return command_dashboard(config, args.output)
        if command == "dashboard-refresh":
            return command_dashboard_refresh(config, args.output, args.interval_minutes, args.once)
        if command == "dashboard-stale-alert":
            return command_dashboard_stale_alert(config, args.output, args.max_age_minutes)
        if command == "serve-dashboard":
            return command_serve_dashboard(config, args.host, args.port, args.output)
        if command == "run-scheduled":
            return command_run_scheduled(config, args.job_id)
        if command == "pause":
            return command_pause(config, args.hours, args.reason)
        if command == "resume":
            return command_resume(config)
        if command == "pause-status":
            return command_pause_status(config)
        raise GrowattGuardError(f"Unknown command: {command}")

    if command in LOCKED_COMMANDS:
        return run_with_command_lock(config, command, action)
    return action()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    config: Config | None = None

    try:
        if args.command == "validate-schedule":
            return command_validate_schedule()
        config = load_config()
        logging.info("Command=%s dry_run=%s low_soc=%s", args.command, config.dry_run, config.low_battery_soc)
        return dispatch_command(config, args)
    except GrowattGuardError as exc:
        logging.error("%s", exc)
        notify_failure(config, args.command, str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - logs traceback for unattended scheduler runs
        logging.exception("Unhandled error")
        notify_failure(config, args.command, str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
