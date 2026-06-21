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
    estimate_runtime,
    estimate_topup_for_sunrise,
    extract_first_metric,
    extract_soc,
    extract_spf_output_source,
    extract_status_soc,
    load_context,
    parse_number,
    set_mode,
    summarize_status,
    verify_mode_switch,
    write_probe,
)
from growatt_guard.notifications import (
    embed_auto_topup_started,
    embed_topup_complete_summary,
    embed_topup_skipped_sunny,
    embed_battery_alert,
    embed_battery_cleared,
    embed_mode_not_confirmed,
    embed_mode_switch_sbu,
    embed_mode_switch_utility,
    embed_preserve_skipped,
    embed_runtime_alert,
    embed_runtime_alert_cleared,
    embed_summary,
    embed_watchdog_failed,
    embed_watchdog_repaired,
    send_discord_embed,
    send_discord_message,
)
from growatt_guard.pause import command_pause, command_resume, ensure_not_paused
from growatt_guard.schedule import (
    find_schedule_job,
    schedule_job_tokens,
    today_schedule_override,
    validate_schedule,
    validate_schedule_overrides,
)
from growatt_guard.state import (
    append_charge_rate_reading,
    clear_battery_alert_state,
    clear_runtime_alert_state,
    clear_topup_state,
    parse_utc_datetime,
    pause_message,
    read_battery_alert_state,
    read_pause_state,
    read_runtime_alert_state,
    read_topup_state,
    topup_is_active,
    utc_now,
    write_battery_alert_state,
    write_runtime_alert_state,
    write_topup_state,
)
from growatt_guard.weather import apply_load_adjustment, choose_preserve_threshold, hours_until_next_sunrise

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


def command_estimate_charge_rate(config: Config, wait_seconds: int = 900) -> int:
    import time as _time

    if config.battery_capacity_wh <= 0:
        raise GrowattGuardError(
            "BATTERY_CAPACITY_WH must be set to estimate charge rate. "
            "Set it to your total battery capacity in Wh (e.g. 30000 for 2x15kWh)."
        )

    _, _, status1 = load_context(config)
    soc1_result = extract_soc(status1)
    if not soc1_result:
        raise GrowattGuardError("Could not read SOC from Growatt.")
    soc1, _ = soc1_result

    _pc = extract_first_metric(status1, ("pCharge", "pCharge1"))
    pcv = parse_number(_pc[0]) if _pc else None
    output_source = extract_spf_output_source(status1)
    on_utility = bool(output_source and output_source[0] == "2")
    if (pcv is None or pcv <= 0) and not on_utility:
        raise GrowattGuardError(
            f"Battery does not appear to be charging (pCharge={pcv}). "
            "Switch to Utility/mains charging first, then run this command."
        )

    if pcv is not None and pcv > 0:
        print(f"Initial SOC : {soc1:.0f}%  |  API charge reading: {pcv:g} W")
    else:
        print(
            f"Initial SOC : {soc1:.0f}%  |  API charge reading: {pcv or 0:g} W "
            "(continuing because output source is Utility first)"
        )
    print(f"Waiting {wait_seconds}s ({wait_seconds // 60}m {wait_seconds % 60:02d}s) — do not change modes...")

    _time.sleep(wait_seconds)

    _, _, status2 = load_context(config)
    soc2_result = extract_soc(status2)
    if not soc2_result:
        raise GrowattGuardError("Could not read SOC after wait.")
    soc2, _ = soc2_result

    delta_soc = soc2 - soc1
    print(f"Final SOC   : {soc2:.0f}%  |  Delta: {delta_soc:+.0f}%")

    if delta_soc <= 0:
        print(
            f"No SOC increase detected. The wait may be too short for the API's 1% resolution. "
            f"Try: estimate-charge-rate --wait-seconds {wait_seconds * 2}"
        )
        return 1

    delta_wh = delta_soc / 100.0 * config.battery_capacity_wh
    rate_w = delta_wh / (wait_seconds / 3600.0)
    print(f"Delta energy: {delta_wh:g} Wh")
    print(f"Estimated charge rate: {rate_w:.0f} W")
    print(f"\nAdd to .env:  BATTERY_CHARGE_RATE_W={rate_w:.0f}")
    return 0


def _sunrise_hours(config: Config) -> float | None:
    try:
        return hours_until_next_sunrise(config)
    except Exception:  # noqa: BLE001
        return None


def command_status(config: Config) -> int:
    _, _, status = load_context(config)
    print(summarize_status(
        status,
        config.battery_capacity_wh,
        config.battery_bms_cutoff_soc,
        config.battery_charge_rate_w,
        _sunrise_hours(config),
    ))
    return 0


def command_probe(config: Config) -> int:
    _, _, status = load_context(config)
    path = write_probe(status)
    print(summarize_status(
        status,
        config.battery_capacity_wh,
        config.battery_bms_cutoff_soc,
        config.battery_charge_rate_w,
        _sunrise_hours(config),
    ))
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
    if config.load_aware_threshold:
        _load = extract_first_metric(status, ("loadPercent", "loadPercent1"))
        _load_pct = parse_number(_load[0]) if _load else None
        threshold_decision = apply_load_adjustment(threshold_decision, _load_pct)
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

    # Charge ceiling: if we're on Utility and haven't reached the target SOC yet, hold off
    if config.battery_charge_target_soc > 0 and soc is not None and soc < config.battery_charge_target_soc:
        logging.info(
            "Charge ceiling: SOC %.1f%% < target %.1f%%; staying on Utility.", soc, config.battery_charge_target_soc
        )
        append_mode_audit(
            config,
            "watchdog-sbu",
            soc=soc,
            previous_mode=previous_mode,
            action="ceiling-hold",
            result="ok",
            note=f"SOC {soc:.0f}% below ceiling {config.battery_charge_target_soc:g}%",
        )
        print(f"Charge ceiling hold: SOC {soc:.0f}% < target {config.battery_charge_target_soc:g}%; staying on Utility.")
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
    now = dt.datetime.now()
    since = now - dt.timedelta(days=7)
    prev_week_start = now - dt.timedelta(days=14)

    solar_this: dict = {}
    solar_last: dict = {}
    if config.pvoutput_enabled:
        from growatt_guard.pvoutput import fetch_pvoutput_daily_outputs
        solar_this = fetch_pvoutput_daily_outputs(config, since.date(), now.date())
        solar_last = fetch_pvoutput_daily_outputs(config, prev_week_start.date(), since.date())

    summary = build_weekly_summary(
        now=now,
        solar_this_week=solar_this or None,
        solar_last_week=solar_last or None,
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


def command_auto_topup_check(config: Config) -> int:
    if not config.auto_topup_enabled:
        print("Auto-topup disabled (AUTO_TOPUP_ENABLED=false).")
        return 0

    if read_pause_state() or topup_is_active():
        print("Automation already paused or topup active; skipping auto-topup check.")
        return 0

    hrs = _sunrise_hours(config)
    if hrs is None or hrs <= 0:
        print("Sunrise unavailable or already past; skipping auto-topup.")
        return 0
    if config.auto_topup_min_hours_to_sunrise > 0 and hrs < config.auto_topup_min_hours_to_sunrise:
        print(
            f"Too close to sunrise ({hrs:.1f}h < {config.auto_topup_min_hours_to_sunrise:g}h cutoff); skipping auto-topup."
        )
        return 0

    api, device, status = load_context(config)
    soc_result = extract_soc(status)
    if not soc_result:
        raise GrowattGuardError("Could not read SOC for auto-topup check.")
    soc, _ = soc_result
    previous_mode = describe_status_output_source(status)

    _pd = extract_first_metric(status, ("pDischarge", "pDischarge1"))
    load_w = parse_number(_pd[0]) if _pd else None
    if not load_w or load_w <= 0:
        print(f"Battery not discharging; no auto-topup needed.")
        return 0

    topup_min_f = estimate_topup_for_sunrise(
        soc, load_w, config.battery_capacity_wh, config.battery_bms_cutoff_soc,
        config.battery_charge_rate_w, hrs,
    )
    if topup_min_f is None or topup_min_f <= 0:
        print(f"Battery sufficient to reach sunrise (SOC={soc:.0f}%, {hrs:.1f}h remaining).")
        return 0

    topup_min = round(topup_min_f)
    if config.auto_topup_min_minutes > 0 and topup_min < config.auto_topup_min_minutes:
        logging.info("Topup floor applied: calculated %d min < min %g min; using %g min.", topup_min, config.auto_topup_min_minutes, config.auto_topup_min_minutes)
        topup_min = round(config.auto_topup_min_minutes)
    topup_min = min(topup_min, config.discord_topup_max_minutes)

    if config.auto_topup_solar_skip_kwh_m2 > 0:
        from growatt_guard.weather import get_tomorrow_solar_kwh_m2
        tomorrow_kwh = get_tomorrow_solar_kwh_m2(config)
        if tomorrow_kwh is not None and tomorrow_kwh >= config.auto_topup_solar_skip_kwh_m2:
            msg = (
                f"Solar forecast {tomorrow_kwh:.1f} kWh/m² ≥ {config.auto_topup_solar_skip_kwh_m2:g} kWh/m²"
                f" — skipping {topup_min}min topup (sunny tomorrow)."
            )
            logging.info(msg)
            print(msg)
            append_mode_audit(
                config, "auto-topup-check", soc=soc, previous_mode=previous_mode,
                action="topup-skipped-sunny", result="ok",
                note=f"solar {tomorrow_kwh:.1f} kWh/m², threshold {config.auto_topup_solar_skip_kwh_m2:g}",
            )
            if config.discord_notify_success and not config.dry_run:
                send_discord_embed(config, embed_topup_skipped_sunny(soc, topup_min, tomorrow_kwh, config.auto_topup_solar_skip_kwh_m2))
            return 0

    reason = f"Auto-topup: {topup_min}min needed for {hrs:.1f}h until sunrise"
    paused_until = utc_now() + dt.timedelta(minutes=topup_min)

    command_pause(config, topup_min / 60.0, reason)

    try:
        result = set_mode(api, config, device, "utility")
    except Exception as exc:  # noqa: BLE001
        append_mode_audit(
            config, "auto-topup-check", soc=soc, previous_mode=previous_mode,
            action="utility-failed", result="error", note=str(exc),
        )
        from growatt_guard.state import clear_pause_state
        clear_pause_state()
        raise

    write_topup_state(topup_min, reason, paused_until, start_soc=soc, start_load_w=load_w)
    append_mode_audit(
        config, "auto-topup-check", soc=soc, previous_mode=previous_mode,
        action="auto-topup-started", result=result,
        note=f"{topup_min}min, {hrs:.1f}h to sunrise",
    )
    if config.discord_notify_success and not config.dry_run:
        send_discord_embed(config, embed_auto_topup_started(soc, topup_min, hrs, load_w))

    print(f"Auto-topup started: {topup_min}min on Utility (SOC={soc:.0f}%, {hrs:.1f}h to sunrise).")
    return 0


def command_topup_complete_check(config: Config) -> int:
    state = read_topup_state()
    if state is None:
        print("No active topup.")
        return 0
    if topup_is_active():
        try:
            paused_until = parse_utc_datetime(str(state["paused_until"]))
            remaining = max(0, int((paused_until - utc_now()).total_seconds() // 60))
            print(f"Topup still active (~{remaining} min remaining); skipping.")
        except (KeyError, ValueError):
            print("Topup still active; skipping.")
        return 0

    logging.info("Topup window expired; completing topup.")

    end_soc: float | None = None
    try:
        _, _, end_status = load_context(config)
        end_soc_result = extract_soc(end_status)
        if end_soc_result:
            end_soc, _ = end_soc_result
    except Exception:  # noqa: BLE001
        pass

    command_resume(config)

    def _f(v: object) -> float | None:
        try:
            return float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    start_soc = _f(state.get("start_soc"))
    start_load_w = _f(state.get("start_load_w"))
    started_at_str = state.get("started_at")
    planned_min = _f(state.get("minutes")) or 0.0

    actual_min = planned_min
    if started_at_str:
        try:
            actual_min = max(1.0, (utc_now() - parse_utc_datetime(str(started_at_str))).total_seconds() / 60.0)
        except ValueError:
            pass

    if end_soc is not None and start_soc is not None and start_load_w is not None and config.battery_capacity_wh > 0:
        soc_gain = end_soc - start_soc
        if soc_gain > 0:
            energy_wh = soc_gain / 100.0 * config.battery_capacity_wh + start_load_w * (actual_min / 60.0)
            implied_rate_w = energy_wh / (actual_min / 60.0)

            history = append_charge_rate_reading(implied_rate_w)
            rates = [r["rate_w"] for r in history if isinstance(r.get("rate_w"), (int, float))]
            avg_rate_w = sum(rates) / len(rates) if len(rates) >= 2 else None

            print(
                f"Topup complete: {actual_min:.0f}min, {start_soc:.0f}% → {end_soc:.0f}% (+{soc_gain:.0f}%)\n"
                f"Implied charge rate: {implied_rate_w:.0f} W (configured: {config.battery_charge_rate_w:g} W)"
            )
            if avg_rate_w is not None:
                print(f"  Avg charge rate ({len(rates)} readings): {avg_rate_w:.0f} W")
                ref_rate = avg_rate_w
            else:
                ref_rate = implied_rate_w
            if config.battery_charge_rate_w > 0:
                diff_pct = abs(ref_rate - config.battery_charge_rate_w) / config.battery_charge_rate_w * 100
                if diff_pct >= 10:
                    print(f"  Tip: consider updating BATTERY_CHARGE_RATE_W={ref_rate:.0f}")
            if config.discord_notify_success and not config.dry_run:
                send_discord_embed(config, embed_topup_complete_summary(
                    start_soc, end_soc, actual_min, implied_rate_w, config.battery_charge_rate_w,
                    avg_rate_w=avg_rate_w, reading_count=len(rates),
                ))
        else:
            print(f"Topup complete: {start_soc:.0f}% → {end_soc:.0f}% (no SOC gain detected).")
    else:
        print("Topup complete.")

    # Clear state only after a successful return to SBU so the next cron run
    # retries the switch if the Growatt API was temporarily unavailable.
    try:
        rc = command_return_sbu(config)
    except Exception:
        logging.warning("topup-complete-check: return-sbu failed; topup state preserved for retry on next cron run")
        raise
    clear_topup_state()
    return rc


def command_runtime_alert(config: Config) -> int:
    if config.runtime_alert_minutes <= 0:
        print("Runtime alert disabled (RUNTIME_ALERT_MINUTES not set).")
        return 0
    if config.battery_capacity_wh <= 0:
        raise GrowattGuardError("BATTERY_CAPACITY_WH must be set for runtime alerts.")

    _, _, status = load_context(config)
    soc_result = extract_soc(status)
    if not soc_result:
        raise GrowattGuardError("Could not read SOC.")
    soc, _ = soc_result
    previous_mode = describe_status_output_source(status) or "unknown"

    _pd = extract_first_metric(status, ("pDischarge", "pDischarge1"))
    load_w = parse_number(_pd[0]) if _pd else None
    rt: float | None = None
    if load_w and load_w > 0:
        rt = estimate_runtime(soc, load_w, config.battery_capacity_wh, config.battery_bms_cutoff_soc)

    state = read_runtime_alert_state()
    clear_minutes = config.runtime_alert_clear_minutes or config.runtime_alert_minutes * 1.5

    if rt is not None and rt < config.runtime_alert_minutes:
        if state and state.get("active"):
            print(f"Runtime alert already active ({rt:.0f} min remaining); no repeat.")
            return 0
        if not config.discord_webhook_url:
            raise GrowattGuardError("DISCORD_WEBHOOK_URL must be set for runtime alerts.")
        send_discord_embed(config, embed_runtime_alert(rt, load_w or 0.0, soc))
        write_runtime_alert_state(rt)
        print(f"Runtime alert sent: {rt:.0f} min remaining at {load_w:g} W.")
        return 0

    if state and state.get("active") and (rt is None or rt >= clear_minutes):
        clear_runtime_alert_state()
        if config.discord_webhook_url:
            send_discord_embed(config, embed_runtime_alert_cleared(rt, soc))
        print("Runtime alert cleared.")
        return 0

    rt_str = f"{rt:.0f} min" if rt is not None else "unknown (not discharging)"
    print(f"Runtime OK: {rt_str}.")
    return 0
