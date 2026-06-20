from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import growatt_guard.state as state_files
from growatt_guard.audit import (
    MODE_AUDIT_FILE,
    append_mode_audit,
    build_daily_summary,
    build_weekly_summary,
    read_mode_audit_rows,
    summarize_today_log_counts,
)
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
from growatt_guard.cli import (
    build_parser,
    dispatch_command,
    main,
    parse_command_tokens,
)
from growatt_guard.config import (
    Config,
    env,
    load_config,
    optional_float,
    str_to_bool,
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

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "growatt_power_guard.log"

PAUSABLE_COMMANDS = {"preserve-battery", "utility-check", "morning-check", "return-sbu", "watchdog-sbu"}
LOCKED_COMMANDS = PAUSABLE_COMMANDS


class GrowattGuardError(RuntimeError):
    pass


@dataclass(frozen=True)
class HealthCheckItem:
    name: str
    status: str
    detail: str


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


if __name__ == "__main__":
    raise SystemExit(main())
