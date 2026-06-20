from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from growatt_guard.audit import (
    append_mode_audit,
    build_daily_summary,
    build_monthly_summary,
    build_weekly_summary,
)
from growatt_guard.cli import dispatch_command, parse_command_tokens
from growatt_guard.config import Config
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import (
    describe_status_output_source,
    extract_soc,
    extract_spf_output_source,
    extract_status_soc,
    load_context,
    set_mode,
    summarize_status,
    verify_mode_switch,
    write_probe,
)
from growatt_guard.notifications import (
    embed_battery_alert,
    embed_battery_cleared,
    embed_mode_not_confirmed,
    embed_mode_switch_sbu,
    embed_mode_switch_utility,
    embed_preserve_skipped,
    embed_summary,
    embed_watchdog_failed,
    embed_watchdog_repaired,
    send_discord_embed,
    send_discord_message,
)
from growatt_guard.pause import ensure_not_paused
from growatt_guard.schedule import (
    find_schedule_job,
    schedule_job_tokens,
    today_schedule_override,
    validate_schedule,
    validate_schedule_overrides,
)
from growatt_guard.state import (
    clear_battery_alert_state,
    pause_message,
    read_battery_alert_state,
    read_pause_state,
    write_battery_alert_state,
)
from growatt_guard.weather import choose_preserve_threshold

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"

_MODE_CHANGING_COMMANDS = {
    "preserve-battery",
    "utility-check",
    "morning-check",
    "return-sbu",
    "watchdog-sbu",
    "force-utility",
}


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
        current_source = extract_spf_output_source(status)
        if current_source and current_source[0] == "2":
            logging.info("Battery SOC %.1f%% is below %.1f%% but already in Utility; skipping switch.", soc, threshold)
            append_mode_audit(
                config,
                "preserve-battery",
                soc=soc,
                threshold=threshold,
                weather_category=threshold_decision.weather_category,
                previous_mode=previous_mode,
                action="no-change",
                result="skipped",
                note="already in Utility mode",
            )
            print(f"SOC {soc:g}% < {threshold:g}%; already in Utility mode, no switch needed.")
            return 0
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
            send_discord_embed(config, embed_mode_switch_utility(
                soc, previous_mode, threshold, threshold_decision.weather_category, threshold_decision.reason,
            ))
        print(f"SOC {soc:g}% < {threshold:g}%; Utility command result: {result}")
        print(f"Threshold reason: {threshold_decision.reason}")
        if not config.dry_run:
            confirmed = verify_mode_switch(api, device, "utility")
            if confirmed is False:
                logging.warning("preserve-battery: Utility switch not confirmed by re-read.")
                if config.discord_notify_failure:
                    send_discord_embed(config, embed_mode_not_confirmed("preserve-battery", "Utility first"))
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
            send_discord_embed(config, embed_preserve_skipped(
                soc, threshold, threshold_decision.weather_category, threshold_decision.reason,
            ))
        print(f"SOC {soc:g}% >= {threshold:g}%; no switch needed.")
        print(f"Threshold reason: {threshold_decision.reason}")
    return 0


def command_utility_check(config: Config) -> int:
    return command_preserve_battery(config)


def command_morning_check(config: Config) -> int:
    return command_preserve_battery(config)


def command_force_utility(config: Config, reason: str = "") -> int:
    api, device, status = load_context(config)
    soc = extract_status_soc(status)
    previous_mode = describe_status_output_source(status)

    current_source = extract_spf_output_source(status)
    if current_source and current_source[0] == "2":
        logging.info("Already in Utility first mode; skipping force-utility switch.")
        append_mode_audit(
            config,
            "force-utility",
            soc=soc,
            previous_mode=previous_mode,
            action="no-change",
            result="skipped",
            note="already in Utility mode" + (f"; {reason}" if reason else ""),
        )
        print("Already in Utility first mode; no switch needed.")
        return 0

    try:
        result = set_mode(api, config, device, "utility")
    except Exception as exc:  # noqa: BLE001 - audit failed mode decisions before re-raising
        append_mode_audit(
            config,
            "force-utility",
            soc=soc,
            previous_mode=previous_mode,
            action="switch-to-utility-failed",
            result="error",
            note=str(exc),
        )
        raise
    append_mode_audit(
        config,
        "force-utility",
        soc=soc,
        previous_mode=previous_mode,
        action="switch-to-utility",
        result=result,
        note=reason,
    )
    if config.discord_notify_success and not config.dry_run:
        send_discord_embed(config, embed_mode_switch_utility(soc, previous_mode, reason=reason))
    print(f"Utility command result: {result}")
    if not config.dry_run:
        confirmed = verify_mode_switch(api, device, "utility")
        if confirmed is False:
            logging.warning("force-utility: Utility switch not confirmed by re-read.")
            if config.discord_notify_failure:
                send_discord_embed(config, embed_mode_not_confirmed("force-utility", "Utility first"))
    return 0


def command_return_sbu(config: Config) -> int:
    if ensure_not_paused(config, "return-sbu"):
        return 0

    api, device, status = load_context(config)
    soc = extract_status_soc(status)
    previous_mode = describe_status_output_source(status)

    current_source = extract_spf_output_source(status)
    if current_source and current_source[0] == "0":
        logging.info("Already in SBU priority mode; skipping return-sbu switch.")
        append_mode_audit(
            config,
            "return-sbu",
            soc=soc,
            previous_mode=previous_mode,
            action="no-change",
            result="skipped",
            note="already in SBU mode",
        )
        print("Already in SBU priority mode; no switch needed.")
        return 0

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
        send_discord_embed(config, embed_mode_switch_sbu(soc, previous_mode))
    print(f"SBU command result: {result}")
    if not config.dry_run:
        confirmed = verify_mode_switch(api, device, "sbu")
        if confirmed is False:
            logging.warning("return-sbu: SBU switch not confirmed by re-read.")
            if config.discord_notify_failure:
                send_discord_embed(config, embed_mode_not_confirmed("return-sbu", "SBU priority"))
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
            send_discord_embed(config, embed_watchdog_failed(message))
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
        send_discord_embed(config, embed_watchdog_repaired(soc, previous_mode))
    print(message)
    return 0


def command_daily_summary(config: Config) -> int:
    _, _, status = load_context(config)
    summary = build_daily_summary(status)
    if config.discord_webhook_url:
        send_discord_embed(config, embed_summary("Daily Summary", summary))
    print(summary)
    return 0


def command_weekly_summary(config: Config) -> int:
    summary = build_weekly_summary()
    if config.discord_webhook_url:
        send_discord_embed(config, embed_summary("Weekly Summary", summary))
    print(summary)
    return 0


def command_monthly_summary(config: Config) -> int:
    summary = build_monthly_summary()
    if config.discord_webhook_url:
        send_discord_embed(config, embed_summary("Monthly Summary", summary))
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
        if not send_discord_embed(config, embed_battery_alert(soc, config.emergency_soc, previous_mode)):
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
            send_discord_embed(config, embed_battery_cleared(soc, recovery_soc, previous_mode))
        print(f"Emergency battery alert cleared: SOC {soc:g}% >= {recovery_soc:g}%.")
        return 0

    print(f"Battery alert OK: SOC {soc:g}% >= {config.emergency_soc:g}% ({previous_mode}).")
    return 0


def _run_scheduled_dry_plan(
    config: Config,
    job_id: str,
    job: dict,
    index: int,
    override: dict,
    today: str,
    note: str,
) -> int:
    skip_all = bool(override.get("skip_all", False))
    skip_ids = override.get("skip", [])
    replace_map = override.get("replace", {}) if isinstance(override.get("replace", {}), dict) else {}

    scheduled_tokens = schedule_job_tokens(job, index)
    scheduled_cmd = " ".join(scheduled_tokens)

    lines = [f"Dry plan: run-scheduled {job_id} ({today})"]
    lines.append(f"  Scheduled command:  {scheduled_cmd}")

    if skip_all:
        override_label = "SKIP-ALL"
        if note:
            override_label += f" — {note}"
        lines.append(f"  Override today:     {override_label}")
        lines.append(f"  Outcome:            would skip  (schedule override: skip-all)")
        print("\n".join(lines))
        return 0

    if job_id in (skip_ids or []):
        override_label = "SKIP"
        if note:
            override_label += f" — {note}"
        lines.append(f"  Override today:     {override_label}")
        lines.append(f"  Outcome:            would skip  (schedule override)")
        print("\n".join(lines))
        return 0

    replacement = replace_map.get(job_id)
    if replacement:
        repl_tokens = schedule_job_tokens(replacement, 0)
        repl_cmd = " ".join(repl_tokens)
        override_label = f"replace -> {repl_cmd}"
        if note:
            override_label += f" — {note}"
        lines.append(f"  Override today:     {override_label}")
        effective_tokens = repl_tokens
    else:
        override_label = "none" + (f" — {note}" if note else "")
        lines.append(f"  Override today:     {override_label}")
        effective_tokens = scheduled_tokens

    effective_cmd = " ".join(effective_tokens)
    effective_command = effective_tokens[0] if effective_tokens else ""
    is_mode_changing = effective_command in _MODE_CHANGING_COMMANDS

    lines.append(f"  Effective command:  {effective_cmd}")
    lines.append(f"  Mode-changing:      {'yes' if is_mode_changing else 'no'}")

    pause_state = read_pause_state()
    if pause_state:
        lines.append(f"  Paused:             yes — {pause_message(pause_state)}")
    else:
        lines.append(f"  Paused:             no")

    lines.append(f"  DRY_RUN:            {'true' if config.dry_run else 'false'}")

    if is_mode_changing and pause_state:
        lines.append(f"  Outcome:            would skip  (paused)")
    else:
        lines.append(f"  Outcome:            would run   {effective_cmd}")

    print("\n".join(lines))
    return 0


def command_run_scheduled(config: Config, job_id: str, dry_plan: bool = False) -> int:
    schedule = validate_schedule()
    job, index = find_schedule_job(schedule, job_id)
    overrides = validate_schedule_overrides(schedule)
    override = today_schedule_override(overrides)
    today = dt.date.today().isoformat()
    note = str(override.get("note", "")).strip()

    if dry_plan:
        return _run_scheduled_dry_plan(config, job_id, job, index, override, today, note)

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
