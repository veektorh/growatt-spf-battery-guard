from __future__ import annotations

import datetime as dt
import logging
import time

from growatt_guard.audit import (
    append_mode_audit,
    find_overdue_unclosed_topup,
)
from growatt_guard.cli import dispatch_command, parse_command_tokens
from growatt_guard.config import Config
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import (
    describe_status_output_source,
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
    embed_mode_not_confirmed,
    embed_mode_switch_sbu,
    embed_mode_switch_utility,
    embed_preserve_skipped,
    embed_sbu_return_blocked,
    embed_watchdog_failed,
    embed_watchdog_repaired,
    send_discord_embed,
    send_discord_message,
)
from growatt_guard.pause import ensure_not_paused
from growatt_guard.schedule import (
    find_schedule_job,
    next_scheduled_runs,
    resolve_effective_schedule_job,
    schedule_job_tokens,
    schedule_job_id,
    today_schedule_override,
    validate_schedule,
    validate_schedule_overrides,
)
from growatt_guard.state import (
    clear_utility_hold_state,
    parse_utc_datetime,
    pause_message,
    read_pause_state,
    read_utility_hold_state,
    topup_is_active,
    utility_hold_ownership,
    utc_now,
    write_utility_hold_state,
    write_waste_alert_snooze,
)
from growatt_guard.weather import apply_load_adjustment, choose_preserve_threshold, hours_until_next_sunrise

_PRESERVE_HOLD_FALLBACK_MINUTES = 90.0
_PRESERVE_HOLD_MAX_SCHEDULE_MINUTES = 180.0


def _sbu_return_guard_blocks(
    config: Config,
    command: str,
    soc: float | None,
    previous_mode: str,
    *,
    allow_low_soc: bool = False,
    reason: str = "",
) -> bool:
    threshold = max(0.0, float(config.min_sbu_return_soc))
    if threshold <= 0 or (soc is not None and soc >= threshold):
        return False
    if allow_low_soc:
        if not reason.strip():
            raise GrowattGuardError("--allow-low-soc requires --reason so the safety override is auditable.")
        append_mode_audit(
            config,
            command,
            soc=soc,
            threshold=threshold,
            previous_mode=previous_mode,
            action="low-soc-guard-bypassed",
            result="override",
            note=reason.strip(),
        )
        logging.warning(
            "%s: low-SOC SBU guard explicitly bypassed (SOC=%s, minimum=%.1f%%): %s",
            command,
            "unavailable" if soc is None else f"{soc:.1f}%",
            threshold,
            reason.strip(),
        )
        return False

    detail = (
        f"SOC {soc:.1f}% is below minimum {threshold:.1f}%"
        if soc is not None
        else f"SOC is unavailable while minimum return SOC is {threshold:.1f}%"
    )
    append_mode_audit(
        config,
        command,
        soc=soc,
        threshold=threshold,
        previous_mode=previous_mode,
        action="low-soc-guard-blocked",
        result="blocked",
        note=detail,
    )
    logging.warning("%s: %s; staying on Utility.", command, detail)
    if config.discord_notify_failure and not config.dry_run:
        send_discord_embed(config, embed_sbu_return_blocked(command, soc, threshold, previous_mode))
    print(f"SBU return blocked: {detail}; staying on Utility.")
    return True


def _preserve_hold_expiry(now: dt.datetime | None = None) -> dt.datetime:
    """Return a conservative Utility-hold expiry for preserve-battery.

    Prefer the next effective scheduled return-sbu, capped so a manual preserve
    cannot suppress waste alerts for most of a day.
    """
    local_now = now or dt.datetime.now().astimezone()
    fallback = local_now + dt.timedelta(minutes=_PRESERVE_HOLD_FALLBACK_MINUTES)
    try:
        schedule = validate_schedule()
        overrides = validate_schedule_overrides(schedule)
    except Exception as exc:  # noqa: BLE001 - preserve mode should not fail because schedule metadata is unavailable
        logging.warning("Could not read schedule for preserve Utility hold expiry: %s", exc)
        return fallback.astimezone(dt.timezone.utc)

    max_delta = dt.timedelta(minutes=_PRESERVE_HOLD_MAX_SCHEDULE_MINUTES)
    for run_at, job in next_scheduled_runs(schedule, now=local_now, limit=256):
        try:
            index = next(i for i, candidate in enumerate(schedule["jobs"], start=1) if candidate is job)
            override = today_schedule_override(overrides, run_at.date())
            command = resolve_effective_schedule_job(job, index, override).effective_command
        except Exception:  # noqa: BLE001
            command = str(job.get("command", "")).strip()
        if command != "return-sbu":
            continue
        if run_at - local_now <= max_delta:
            return run_at.astimezone(dt.timezone.utc)
        break
    return fallback.astimezone(dt.timezone.utc)


def _record_preserve_utility_hold(config: Config, soc: float, threshold: float) -> None:
    if config.dry_run:
        return
    max_expiry = _preserve_hold_expiry()
    write_utility_hold_state("owned", threshold, max_expiry, start_soc=soc)
    logging.info(
        "Preserve-battery Utility hold recorded until %s with target SOC %.1f%%.",
        max_expiry.isoformat(),
        threshold,
    )


def _set_preserve_utility_mode(api, config: Config, device) -> tuple[dict, int]:
    max_attempts = max(1, int(config.preserve_utility_max_attempts))
    retry_delay = max(0.0, float(config.preserve_utility_retry_delay_seconds))

    for attempt in range(1, max_attempts + 1):
        try:
            return set_mode(api, config, device, "utility"), attempt
        except Exception as exc:  # noqa: BLE001 - retry transient Growatt mode-write failures
            if attempt >= max_attempts:
                raise
            logging.warning(
                "preserve-battery: Utility switch attempt %s/%s failed: %s; retrying in %.0fs.",
                attempt,
                max_attempts,
                exc,
                retry_delay,
            )
            if retry_delay > 0:
                time.sleep(retry_delay)

    raise GrowattGuardError("Preserve-battery Utility switch retry loop exited unexpectedly.")


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
            _record_preserve_utility_hold(config, soc, threshold)
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
        # Record ownership before the cloud write so an ambiguous response can be reconciled safely.
        _record_preserve_utility_hold(config, soc, threshold)
        try:
            result, attempts_used = _set_preserve_utility_mode(api, config, device)
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
                note=f"{str(exc)}; attempts={max(1, int(getattr(config, 'preserve_utility_max_attempts', 2)))}",
            )
            raise
        retry_note = f"; attempts={attempts_used}" if attempts_used > 1 else ""
        append_mode_audit(
            config,
            "preserve-battery",
            soc=soc,
            threshold=threshold,
            weather_category=threshold_decision.weather_category,
            previous_mode=previous_mode,
            action="switch-to-utility",
            result=result,
            note=f"SOC from {path}{retry_note}",
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
                clear_utility_hold_state()
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


def command_return_sbu(
    config: Config,
    allow_low_soc: bool = False,
    reason: str = "",
) -> int:
    if ensure_not_paused(config, "return-sbu"):
        return 0

    api, device, status = load_context(config)
    soc = extract_status_soc(status)
    previous_mode = describe_status_output_source(status)

    current_source = extract_spf_output_source(status)
    if current_source and current_source[0] == "0":
        logging.info("Already in SBU priority mode; skipping return-sbu switch.")
        if not config.dry_run:
            clear_utility_hold_state()
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

    if _sbu_return_guard_blocks(
        config,
        "return-sbu",
        soc,
        previous_mode,
        allow_low_soc=allow_low_soc,
        reason=reason,
    ):
        return 2

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
        else:
            clear_utility_hold_state()
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

    # On Utility — check ownership before deciding whether to repair.
    hold_state = read_utility_hold_state()
    ownership = hold_state.get("ownership") if hold_state else None

    if ownership not in ("owned", "adopted"):
        # Observed state: Guard did not create this Utility hold; never auto-return.
        logging.info(
            "watchdog-sbu: Utility detected with no Guard ownership (observed); skipping repair."
        )
        append_mode_audit(
            config,
            "watchdog-sbu",
            soc=soc,
            previous_mode=previous_mode,
            action="observed-utility",
            result="skipped",
            note="no Guard ownership state — hard rule: never auto-return from observed Utility",
        )
        print("On Utility with no Guard ownership; watchdog skipping repair (observed state).")
        return 0

    # Owned or adopted: check if topup target/ceiling still active.
    hold_target_soc = hold_state.get("target_soc") if hold_state else 0
    hold_expiry_str = hold_state.get("max_expiry") if hold_state else None

    # Use hold target_soc as ceiling (preferred over legacy config var).
    ceiling_soc = float(hold_target_soc) if hold_target_soc else config.battery_charge_target_soc
    if ceiling_soc > 0 and soc is not None and soc < ceiling_soc:
        logging.info(
            "Charge ceiling: SOC %.1f%% < target %.1f%%; staying on Utility.", soc, ceiling_soc
        )
        append_mode_audit(
            config,
            "watchdog-sbu",
            soc=soc,
            previous_mode=previous_mode,
            action="ceiling-hold",
            result="ok",
            note=f"SOC {soc:.0f}% below ceiling {ceiling_soc:g}% ({ownership})",
        )
        print(f"Charge ceiling hold: SOC {soc:.0f}% < target {ceiling_soc:g}% ({ownership}); staying on Utility.")
        return 0

    # If max_expiry has not passed yet, hold off — topup-complete-check will finalize.
    if hold_expiry_str:
        try:
            hold_expiry = parse_utc_datetime(str(hold_expiry_str))
            if utc_now() < hold_expiry:
                remaining_min = (hold_expiry - utc_now()).total_seconds() / 60
                logging.info(
                    "watchdog-sbu: owned/adopted hold still within expiry (%.0f min left); skipping repair.",
                    remaining_min,
                )
                print(f"Hold active ({ownership}): {remaining_min:.0f} min until max expiry; watchdog holding.")
                return 0
        except ValueError:
            pass

    if _sbu_return_guard_blocks(config, "watchdog-sbu", soc, previous_mode):
        return 2

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


def _run_scheduled_dry_plan(
    config: Config,
    job_id: str,
    job: dict,
    index: int,
    override: dict,
    today: str,
    note: str,
) -> int:
    effective_job = resolve_effective_schedule_job(job, index, override)
    scheduled_tokens = effective_job.original_tokens
    scheduled_cmd = " ".join(scheduled_tokens)

    lines = [f"Dry plan: run-scheduled {job_id} ({today})"]
    lines.append(f"  Scheduled command:  {scheduled_cmd}")

    if effective_job.status == "skip" and override.get("skip_all"):
        override_label = "SKIP-ALL"
        if note:
            override_label += f" — {note}"
        lines.append(f"  Override today:     {override_label}")
        lines.append(f"  Outcome:            would skip  (schedule override: skip-all)")
        print("\n".join(lines))
        return 0

    if effective_job.status == "skip":
        override_label = "SKIP"
        if note:
            override_label += f" — {note}"
        lines.append(f"  Override today:     {override_label}")
        lines.append(f"  Outcome:            would skip  (schedule override)")
        print("\n".join(lines))
        return 0

    if effective_job.status == "replace":
        repl_tokens = effective_job.effective_tokens
        repl_cmd = " ".join(repl_tokens)
        override_label = f"replace -> {repl_cmd}"
        if note:
            override_label += f" — {note}"
        lines.append(f"  Override today:     {override_label}")
        effective_tokens = repl_tokens
    else:
        override_label = "none" + (f" — {note}" if note else "")
        lines.append(f"  Override today:     {override_label}")
        effective_tokens = effective_job.effective_tokens

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
    effective_job = resolve_effective_schedule_job(job, index, override)
    today = dt.date.today().isoformat()
    note = str(override.get("note", "")).strip()

    if dry_plan:
        return _run_scheduled_dry_plan(config, job_id, job, index, override, today, note)

    if effective_job.status == "skip":
        message = f"Skipped scheduled job `{job_id}` for {today} due to schedule override."
        if note:
            message += f" Note: {note}"
        logging.info(message)
        if config.discord_notify_skip:
            send_discord_message(config, message)
        print(message)
        return 0

    if effective_job.status == "replace":
        tokens = effective_job.effective_tokens
        logging.info("Running schedule override for %s: %s", job_id, " ".join(tokens))
    else:
        tokens = effective_job.effective_tokens

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


def command_snooze_waste(config: Config, duration: str) -> int:
    """Snooze waste-alert-check notifications for a duration ('2h', '30m', 'today')."""
    now = utc_now()
    dur_lower = duration.strip().lower()
    if dur_lower == "today":
        local_midnight = (now.astimezone() + dt.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        snooze_until = local_midnight.astimezone(dt.timezone.utc)
    elif dur_lower.endswith("h"):
        hours = float(dur_lower[:-1])
        snooze_until = now + dt.timedelta(hours=hours)
    elif dur_lower.endswith("m"):
        minutes = float(dur_lower[:-1])
        snooze_until = now + dt.timedelta(minutes=minutes)
    else:
        raise GrowattGuardError(
            f"Unrecognised duration '{duration}'. Use e.g. '2h', '30m', or 'today'."
        )
    write_waste_alert_snooze(snooze_until)
    local_str = snooze_until.astimezone().strftime("%H:%M %Z")
    print(f"Waste alerts snoozed until {local_str}.")
    return 0
