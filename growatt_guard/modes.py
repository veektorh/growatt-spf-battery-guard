from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path

from growatt_guard.audit import (
    append_mode_audit,
    build_daily_summary,
    build_monthly_summary,
    build_weekly_summary,
    find_overdue_unclosed_topup,
)
from growatt_guard.cli import dispatch_command, parse_command_tokens
from growatt_guard.config import Config
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import (
    describe_status_output_source,
    detect_unexpected_grid_bypass,
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
    embed_topup_soc_started,
    embed_topup_soc_complete,
    embed_topup_below_target,
    embed_topup_failed_low,
    embed_waste_alert,
    embed_battery_alert,
    embed_battery_cleared,
    embed_bypass_alert,
    embed_bypass_cleared,
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
    next_scheduled_runs,
    schedule_job_tokens,
    schedule_job_id,
    today_schedule_override,
    validate_schedule,
    validate_schedule_overrides,
)
from growatt_guard.state import (
    append_charge_rate_reading,
    append_discharge_rate_reading,
    battery_alert_is_muted,
    clear_battery_alert_mute,
    clear_battery_alert_state,
    clear_bypass_alert_state,
    clear_runtime_alert_state,
    clear_topup_state,
    clear_utility_hold_state,
    clear_waste_alert_mute,
    clear_waste_alert_state,
    parse_utc_datetime,
    pause_message,
    read_battery_alert_state,
    read_bypass_alert_state,
    read_discharge_rate_history,
    read_pause_state,
    read_runtime_alert_state,
    read_topup_state,
    read_utility_hold_state,
    topup_skip_notification_due,
    topup_is_active,
    utility_hold_ownership,
    utc_now,
    waste_alert_is_due,
    waste_alert_is_muted,
    waste_alert_is_snoozed,
    write_battery_alert_mute,
    write_battery_alert_state,
    write_bypass_alert_state,
    write_runtime_alert_state,
    write_topup_skip_notification_state,
    write_topup_state,
    write_utility_hold_state,
    write_waste_alert_last_sent,
    write_waste_alert_mute,
    write_waste_alert_snooze,
)
from growatt_guard.weather import apply_load_adjustment, choose_preserve_threshold, hours_until_next_sunrise

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

_TOPUP_EXPIRY_BUFFER_FACTOR = 1.2  # max_expiry = eta * 1.2
_TOPUP_EXPIRY_BUFFER_MIN_MINUTES = 15.0  # minimum buffer added to ETA
_PRESERVE_HOLD_FALLBACK_MINUTES = 90.0
_BYPASS_ALERT_MAX_SENDS = 3
_PRESERVE_HOLD_MAX_SCHEDULE_MINUTES = 180.0


def _projected_sunrise_soc(
    soc: float,
    load_w: float,
    capacity_wh: float,
    bms_cutoff_soc: float,
    hours: float,
) -> float | None:
    """Estimated battery SOC at next sunrise given current discharge rate."""
    if capacity_wh <= 0 or hours <= 0:
        return None
    drain_soc_pct = load_w * hours / capacity_wh * 100.0
    return max(bms_cutoff_soc, soc - drain_soc_pct)


def _eta_minutes(
    current_soc: float,
    target_soc: float,
    capacity_wh: float,
    charge_rate_w: float,
) -> float | None:
    """Minutes to charge from current_soc to target_soc at charge_rate_w."""
    if capacity_wh <= 0 or charge_rate_w <= 0 or target_soc <= current_soc:
        return None
    soc_gain = target_soc - current_soc
    wh_needed = soc_gain / 100.0 * capacity_wh
    return wh_needed / charge_rate_w * 60.0


def _topup_max_expiry(eta_min: float) -> tuple[float, dt.datetime]:
    """Return (max_minutes, max_expiry_utc) given an ETA in minutes."""
    buffer = max(_TOPUP_EXPIRY_BUFFER_MIN_MINUTES, eta_min * (_TOPUP_EXPIRY_BUFFER_FACTOR - 1))
    max_min = eta_min + buffer
    return max_min, utc_now() + dt.timedelta(minutes=max_min)


def _topup_completion_target_soc(
    reserve_soc: float,
    load_w: float,
    capacity_wh: float,
    hours_to_horizon: float,
    topup_minutes: float,
) -> float:
    """SOC needed when Utility topup ends to preserve reserve_soc at horizon."""
    if capacity_wh <= 0:
        return reserve_soc
    remaining_hours = max(0.0, hours_to_horizon - topup_minutes / 60.0)
    remaining_drain_soc_pct = load_w * remaining_hours / capacity_wh * 100.0
    return min(100.0, reserve_soc + remaining_drain_soc_pct)


def _effective_scheduled_command(job: dict, index: int, override: dict) -> str | None:
    if override.get("skip_all"):
        return None
    job_id = schedule_job_id(job, index)
    if job_id in override.get("skip", []):
        return None
    replacement = override.get("replace", {}).get(job_id) if isinstance(override.get("replace", {}), dict) else None
    if isinstance(replacement, dict):
        return str(replacement.get("command", "")).strip() or None
    return str(job.get("command", "")).strip() or None


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
            command = _effective_scheduled_command(job, index, override)
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
    max_attempts = max(1, int(getattr(config, "preserve_utility_max_attempts", 2)))
    retry_delay = max(0.0, float(getattr(config, "preserve_utility_retry_delay_seconds", 30.0)))

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
                if config.discord_notify_failure:
                    send_discord_embed(config, embed_mode_not_confirmed("preserve-battery", "Utility first"))
            else:
                _record_preserve_utility_hold(config, soc, threshold)
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
    tomorrow_kwh_m2: float | None = None
    if getattr(config, "weather_lat", None) and getattr(config, "weather_lon", None):
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
    from growatt_guard.audit import prune_audit_rows
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


def command_battery_alert(config: Config) -> int:
    if battery_alert_is_muted():
        print("Battery alert is muted.")
        return 0
    _, _, status = load_context(config)
    soc_result = extract_soc(status)
    if not soc_result:
        raise GrowattGuardError("Could not find battery SOC in Growatt response. Run the probe command.")

    soc, path = soc_result
    previous_mode = describe_status_output_source(status) or "unknown"
    bypass_threshold = config.bypass_alert_soc
    bypass = detect_unexpected_grid_bypass(status, recovery_soc=bypass_threshold)
    bypass_state = read_bypass_alert_state()
    utility_ownership = utility_hold_ownership()
    intentional_utility_hold = utility_ownership in {"owned", "adopted"} or topup_is_active()
    if bypass_threshold > 0 and bypass["detected"] and soc > bypass_threshold and intentional_utility_hold:
        reason = str(bypass.get("reason") or "bypass detected")
        print(
            f"Grid bypass observed during intentional Utility/topup hold; suppressing bypass alert "
            f"(SOC {soc:g}% > {bypass_threshold:g}%; {previous_mode}; {reason})."
        )
    elif bypass_threshold > 0 and bypass["detected"] and soc > bypass_threshold:
        reason = str(bypass.get("reason") or "bypass detected")
        sent_count = int((bypass_state or {}).get("sent_count", 1 if bypass_state else 0) or 0)
        if bypass_state and bypass_state.get("active") and sent_count >= _BYPASS_ALERT_MAX_SENDS:
            print(
                f"Grid bypass alert already sent {sent_count} times; suppressing further alerts "
                f"until bypass clears (SOC {soc:g}% > {bypass_threshold:g}%; {previous_mode}; {reason})."
            )
        else:
            if not config.discord_webhook_url:
                raise GrowattGuardError("DISCORD_WEBHOOK_URL must be configured for grid bypass alerts.")
            if not send_discord_embed(config, embed_bypass_alert(soc, bypass_threshold, previous_mode, reason)):
                raise GrowattGuardError("Grid bypass alert could not be sent to Discord.")
            sent_count += 1
            write_bypass_alert_state(soc, reason, sent_count=sent_count)
            print(
                f"Grid bypass alert sent ({sent_count}/{_BYPASS_ALERT_MAX_SENDS}): "
                f"SOC {soc:g}% > {bypass_threshold:g}% ({reason})."
            )
    elif bypass_state and bypass_state.get("active"):
        clear_bypass_alert_state()
        if config.discord_webhook_url:
            send_discord_embed(config, embed_bypass_cleared(soc, bypass_threshold, previous_mode))
        print(f"Grid bypass alert cleared: SOC {soc:g}% / bypass={bool(bypass['detected'])}.")

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

    bypass_note = " bypass=detected" if bypass["detected"] else ""
    print(f"Battery alert OK: SOC {soc:g}% >= {config.emergency_soc:g}% ({previous_mode}).{bypass_note}")
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

    append_discharge_rate_reading(load_w)
    history = read_discharge_rate_history()
    rates = [r["rate_w"] for r in history if isinstance(r.get("rate_w"), (int, float))]
    if len(rates) >= 2:
        avg_load_w = sum(rates) / len(rates)
        logging.info(
            "Using avg discharge rate %.0f W (%d readings) instead of live %.0f W",
            avg_load_w, len(rates), load_w,
        )
        load_w = avg_load_w

    survival_topup_min_f = estimate_topup_for_sunrise(
        soc, load_w, config.battery_capacity_wh, config.battery_bms_cutoff_soc,
        config.battery_charge_rate_w, hrs,
    )
    margin_minutes = max(0.0, config.auto_topup_solar_skip_min_margin_minutes)
    margin_topup_min_f = estimate_topup_for_sunrise(
        soc, load_w, config.battery_capacity_wh, config.battery_bms_cutoff_soc,
        config.battery_charge_rate_w, hrs + margin_minutes / 60.0,
    ) if margin_minutes > 0 else survival_topup_min_f
    effective_target_soc = max(config.battery_bms_cutoff_soc, config.auto_topup_target_soc)
    target_topup_min_f = estimate_topup_for_sunrise(
        soc, load_w, config.battery_capacity_wh, effective_target_soc,
        config.battery_charge_rate_w, hrs,
    )
    topup_candidates = [
        value for value in (target_topup_min_f, margin_topup_min_f)
        if value is not None
    ]
    topup_min_f = max(topup_candidates) if topup_candidates else None
    if topup_min_f is None or topup_min_f <= 0:
        margin_text = f" plus {margin_minutes:.0f}min margin" if margin_minutes > 0 else ""
        print(f"Battery sufficient to reach sunrise{margin_text} (SOC={soc:.0f}%, {hrs:.1f}h remaining).")
        return 0

    # Floor at 1 min: topup_min_f can be e.g. 0.3 (passes the >0 check above) yet
    # round to 0, which would make command_pause raise on a 0-hour pause.
    topup_min = max(1, round(topup_min_f))
    if config.auto_topup_min_minutes > 0 and topup_min_f < config.auto_topup_min_minutes:
        calculated_text = f"{topup_min_f:.1f}min"
        msg = (
            f"Calculated topup {calculated_text} is below AUTO_TOPUP_MIN_MINUTES="
            f"{config.auto_topup_min_minutes:g}; skipping Utility switch."
        )
        logging.info(msg)
        print(msg)
        append_mode_audit(
            config,
            "auto-topup-check",
            soc=soc,
            previous_mode=previous_mode,
            action="topup-skipped-short",
            result="ok",
            note=f"calculated {calculated_text} < minimum {config.auto_topup_min_minutes:g}min",
        )
        return 0
    topup_min = min(topup_min, config.discord_topup_max_minutes)

    if config.auto_topup_solar_skip_kwh_m2 > 0:
        from growatt_guard.weather import get_tomorrow_solar_kwh_m2
        tomorrow_kwh = get_tomorrow_solar_kwh_m2(config)
        if tomorrow_kwh is not None and tomorrow_kwh >= config.auto_topup_solar_skip_kwh_m2:
            survival_safe = survival_topup_min_f is not None and survival_topup_min_f <= 0
            margin_safe = margin_topup_min_f is not None and margin_topup_min_f <= 0
            if not (survival_safe and margin_safe):
                logging.info(
                    "Sunny forecast %.1f kWh/m2 ignored: survival_topup=%s, margin_topup=%s.",
                    tomorrow_kwh,
                    "unknown" if survival_topup_min_f is None else f"{survival_topup_min_f:.0f}min",
                    "unknown" if margin_topup_min_f is None else f"{margin_topup_min_f:.0f}min",
                )
                tomorrow_kwh = None
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
                notify_key = "sunny-topup-skip"
                if topup_skip_notification_due(notify_key):
                    if send_discord_embed(
                        config,
                        embed_topup_skipped_sunny(
                            soc,
                            topup_min,
                            tomorrow_kwh,
                            config.auto_topup_solar_skip_kwh_m2,
                        ),
                    ):
                        write_topup_skip_notification_state(
                            notify_key,
                            {
                                "soc": soc,
                                "topup_min": topup_min,
                                "forecast_kwh_m2": tomorrow_kwh,
                                "threshold_kwh_m2": config.auto_topup_solar_skip_kwh_m2,
                                "margin_minutes": margin_minutes,
                            },
                        )
            return 0

    reason = f"Auto-topup: {topup_min}min needed for {hrs:.1f}h until sunrise"
    if margin_minutes > 0:
        reason += f" (+{margin_minutes:.0f}min margin)"
    if effective_target_soc > config.battery_bms_cutoff_soc:
        reason += f" (target {effective_target_soc:g}% SOC at sunrise)"
    paused_until = utc_now() + dt.timedelta(minutes=topup_min)

    # Completion target is the SOC needed when the planned Utility window ends,
    # not the SOC needed right now. The topup period itself avoids discharge.
    current_soc_target = _topup_completion_target_soc(
        effective_target_soc,
        load_w,
        config.battery_capacity_wh,
        hrs,
        topup_min,
    )
    if margin_minutes > 0:
        margin_target = _topup_completion_target_soc(
            config.battery_bms_cutoff_soc,
            load_w,
            config.battery_capacity_wh,
            hrs + margin_minutes / 60.0,
            topup_min,
        )
        current_soc_target = max(current_soc_target, margin_target)

    reason += f" (topup target {current_soc_target:.0f}% SOC)"
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
    write_utility_hold_state(
        ownership="owned",
        target_soc=current_soc_target,
        max_expiry=paused_until,
        start_soc=soc,
    )
    append_mode_audit(
        config, "auto-topup-check", soc=soc, previous_mode=previous_mode,
        action="auto-topup-started", result=result,
        note=f"{topup_min}min, {hrs:.1f}h to sunrise",
    )
    target_soc_arg = effective_target_soc if effective_target_soc > config.battery_bms_cutoff_soc else None
    if config.discord_notify_success and not config.dry_run:
        send_discord_embed(config, embed_auto_topup_started(
            soc,
            topup_min,
            hrs,
            load_w,
            target_soc=target_soc_arg,
            completion_target_soc=current_soc_target,
        ))

    target_note = f", target {effective_target_soc:g}% SOC" if target_soc_arg else ""
    print(f"Auto-topup started: {topup_min}min on Utility (SOC={soc:.0f}%, {hrs:.1f}h to sunrise{target_note}).")
    return 0


def _topup_record_charge_rate(
    config: Config,
    start_soc: float | None,
    end_soc: float | None,
    actual_min: float,
) -> float | None:
    """Record implied charge rate from a completed topup if there was SOC gain."""
    if end_soc is None or start_soc is None or config.battery_capacity_wh <= 0:
        return None
    soc_gain = end_soc - start_soc
    if soc_gain <= 0:
        return None
    energy_wh = soc_gain / 100.0 * config.battery_capacity_wh
    implied_rate_w = energy_wh / (max(1.0, actual_min) / 60.0)
    append_charge_rate_reading(implied_rate_w)
    return implied_rate_w


def _topup_completion_note(
    *,
    start_soc: float | None,
    end_soc: float | None,
    target_soc: float | None,
    actual_min: float,
    implied_rate_w: float | None,
    ownership: str,
) -> str:
    parts = [f"actual_min={actual_min:.0f}", f"ownership={ownership}"]
    if start_soc is not None:
        parts.append(f"start_soc={start_soc:g}")
    if end_soc is not None:
        parts.append(f"end_soc={end_soc:g}")
    if target_soc is not None:
        parts.append(f"target_soc={target_soc:g}")
    if implied_rate_w is not None:
        parts.append(f"implied_rate_w={implied_rate_w:.0f}")
    return ", ".join(parts)


def command_topup_complete_check(config: Config) -> int:
    hold_state = read_utility_hold_state()
    topup_state = read_topup_state()

    # Nothing active — check audit for overdue unclosed topup.
    if hold_state is None and topup_state is None:
        overdue = find_overdue_unclosed_topup()
        if overdue is not None:
            row = overdue["row"]
            message = (
                "No active topup state, but the audit log shows an overdue "
                f"auto-topup from {row.get('timestamp', 'unknown time')} with no later SBU return; repairing."
            )
            logging.warning(message)
            print(message)
            return command_return_sbu(config)
        print("No active topup.")
        return 0

    # ---- SOC-based completion path (owned/adopted utility hold) ----
    if hold_state is not None:
        ownership = str(hold_state.get("ownership", "owned"))
        target_soc_raw = hold_state.get("target_soc")
        max_expiry_str = hold_state.get("max_expiry")
        start_soc_raw = hold_state.get("start_soc")
        started_at_str = hold_state.get("started_at")

        def _flt(v: object) -> float | None:
            try:
                return float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        target_soc = _flt(target_soc_raw)
        start_soc = _flt(start_soc_raw)

        # Read current SOC.
        end_soc: float | None = None
        try:
            _, _, end_status = load_context(config)
            end_soc_result = extract_soc(end_status)
            if end_soc_result:
                end_soc, _ = end_soc_result
        except Exception:  # noqa: BLE001
            pass

        # Compute actual duration.
        actual_min = 0.0
        if started_at_str:
            try:
                actual_min = max(1.0, (utc_now() - parse_utc_datetime(str(started_at_str))).total_seconds() / 60.0)
            except ValueError:
                pass

        # Check if SOC target reached (early completion).
        if target_soc is not None and end_soc is not None and end_soc >= target_soc:
            logging.info("Topup SOC target reached: %.0f%% >= %.0f%%", end_soc, target_soc)
            implied_rate_w = _topup_record_charge_rate(config, start_soc, end_soc, actual_min)
            append_mode_audit(
                config,
                "topup-complete-check",
                soc=end_soc,
                action="topup-target-reached",
                result="ok",
                note=_topup_completion_note(
                    start_soc=start_soc,
                    end_soc=end_soc,
                    target_soc=target_soc,
                    actual_min=actual_min,
                    implied_rate_w=implied_rate_w,
                    ownership=ownership,
                ),
            )
            if config.discord_notify_success and not config.dry_run:
                send_discord_embed(config, embed_topup_soc_complete(
                    start_soc or end_soc, end_soc, target_soc, actual_min, ownership=ownership,
                ))
            print(f"Topup complete: reached {end_soc:.0f}% (target {target_soc:.0f}%), returning to SBU.")
            command_resume(config)
            try:
                rc = command_return_sbu(config)
            except Exception:
                logging.warning("topup-complete-check: return-sbu failed; state preserved for retry")
                raise
            clear_utility_hold_state()
            clear_topup_state()
            return rc

        # Check max expiry.
        if max_expiry_str:
            try:
                max_expiry = parse_utc_datetime(str(max_expiry_str))
            except ValueError:
                max_expiry = None

            if max_expiry is not None and utc_now() >= max_expiry:
                # Expiry reached — determine outcome based on final SOC.
                floor_soc = config.auto_topup_sunrise_floor_soc
                logging.warning(
                    "Topup max expiry reached: end_soc=%s target=%s", end_soc, target_soc
                )
                implied_rate_w = _topup_record_charge_rate(config, start_soc, end_soc, actual_min)
                append_mode_audit(
                    config,
                    "topup-complete-check",
                    soc=end_soc,
                    action="topup-expired",
                    result="ok",
                    note=_topup_completion_note(
                        start_soc=start_soc,
                        end_soc=end_soc,
                        target_soc=target_soc,
                        actual_min=actual_min,
                        implied_rate_w=implied_rate_w,
                        ownership=ownership,
                    ),
                )
                command_resume(config)
                if end_soc is not None and end_soc <= floor_soc:
                    # At or below safety floor — urgent alert.
                    if config.discord_notify_failure and not config.dry_run:
                        send_discord_embed(config, embed_topup_failed_low(
                            end_soc, target_soc or 0, ownership=ownership,
                        ))
                    print(
                        f"Topup expired at {end_soc:.0f}% — at or below {floor_soc:.0f}% floor. "
                        "Returning to SBU; investigate manually."
                    )
                else:
                    # Above floor but below target — soft completion.
                    if config.discord_notify_success and not config.dry_run:
                        send_discord_embed(config, embed_topup_below_target(
                            start_soc or (end_soc or 0), end_soc or 0,
                            target_soc or 0, ownership=ownership,
                        ))
                    soc_str = f"{end_soc:.0f}%" if end_soc is not None else "unknown"
                    print(f"Topup expired below target: {soc_str} (target {target_soc:.0f}%); returning to SBU.")
                try:
                    rc = command_return_sbu(config)
                except Exception:
                    logging.warning("topup-complete-check: return-sbu failed; state preserved for retry")
                    raise
                clear_utility_hold_state()
                clear_topup_state()
                return rc

            if max_expiry is not None:
                remaining_min = max(0, int((max_expiry - utc_now()).total_seconds() // 60))
                soc_str = f"{end_soc:.0f}%" if end_soc is not None else "unknown"
                target_str = f"{target_soc:.0f}%" if target_soc is not None else "unknown"
                print(f"Topup active ({ownership}): SOC {soc_str} / {target_str} target, ~{remaining_min} min max remaining.")
                return 0

        # Hold exists but no max_expiry and target not reached — still active.
        print("Topup active; skipping.")
        return 0

    # ---- Legacy time-based completion path (topup_active.json only, no hold) ----
    if topup_is_active():
        try:
            paused_until = parse_utc_datetime(str(topup_state["paused_until"]))
            remaining = max(0, int((paused_until - utc_now()).total_seconds() // 60))
            print(f"Topup still active (~{remaining} min remaining); skipping.")
        except (KeyError, ValueError):
            print("Topup still active; skipping.")
        return 0

    logging.info("Topup window expired; completing topup (legacy time-based).")

    end_soc_legacy: float | None = None
    try:
        _, _, end_status = load_context(config)
        end_soc_result = extract_soc(end_status)
        if end_soc_result:
            end_soc_legacy, _ = end_soc_result
    except Exception:  # noqa: BLE001
        pass

    command_resume(config)

    def _f(v: object) -> float | None:
        try:
            return float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    start_soc_legacy = _f(topup_state.get("start_soc"))
    started_at_str_legacy = topup_state.get("started_at")
    planned_min = _f(topup_state.get("minutes")) or 0.0

    actual_min_legacy = planned_min
    if started_at_str_legacy:
        try:
            actual_min_legacy = max(
                1.0, (utc_now() - parse_utc_datetime(str(started_at_str_legacy))).total_seconds() / 60.0
            )
        except ValueError:
            pass

    if end_soc_legacy is not None and start_soc_legacy is not None and config.battery_capacity_wh > 0:
        soc_gain = end_soc_legacy - start_soc_legacy
        if soc_gain > 0:
            energy_wh = soc_gain / 100.0 * config.battery_capacity_wh
            implied_rate_w = energy_wh / (actual_min_legacy / 60.0)

            history = append_charge_rate_reading(implied_rate_w)
            rates = [r["rate_w"] for r in history if isinstance(r.get("rate_w"), (int, float))]
            avg_rate_w = sum(rates) / len(rates) if len(rates) >= 2 else None

            print(
                f"Topup complete: {actual_min_legacy:.0f}min, "
                f"{start_soc_legacy:.0f}% → {end_soc_legacy:.0f}% (+{soc_gain:.0f}%)\n"
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
                    start_soc_legacy, end_soc_legacy, actual_min_legacy,
                    implied_rate_w, config.battery_charge_rate_w,
                    avg_rate_w=avg_rate_w, reading_count=len(rates),
                ))
        else:
            print(f"Topup complete: {start_soc_legacy:.0f}% → {end_soc_legacy:.0f}% (no SOC gain detected).")
    else:
        print("Topup complete.")

    try:
        rc = command_return_sbu(config)
    except Exception:
        logging.warning("topup-complete-check: return-sbu failed; topup state preserved for retry on next cron run")
        raise
    clear_topup_state()
    return rc


def command_adopt_utility(config: Config, target_soc: float) -> int:
    """Adopt the current Utility state and schedule auto-return at target_soc%."""
    if config.battery_capacity_wh <= 0 or config.battery_charge_rate_w <= 0:
        raise GrowattGuardError(
            "BATTERY_CAPACITY_WH and BATTERY_CHARGE_RATE_W must be configured for adopt-utility."
        )

    api, device, status = load_context(config)
    soc_result = extract_soc(status)
    if not soc_result:
        raise GrowattGuardError("Could not read SOC from Growatt.")
    soc, _ = soc_result
    previous_mode = describe_status_output_source(status)
    current_source = extract_spf_output_source(status)

    if not (current_source and current_source[0] == "2"):
        raise GrowattGuardError(
            f"Inverter is not currently on Utility (mode: {previous_mode}). "
            "adopt-utility is only valid when already on Utility."
        )

    existing = utility_hold_ownership()
    if existing in ("owned", "adopted"):
        print(f"Hold already {existing}; current SOC {soc:.0f}%.")
        return 0

    eta_min = _eta_minutes(soc, target_soc, config.battery_capacity_wh, config.battery_charge_rate_w)
    if eta_min is None:
        raise GrowattGuardError("Cannot compute ETA: check BATTERY_CAPACITY_WH and BATTERY_CHARGE_RATE_W.")

    max_min, max_expiry = _topup_max_expiry(eta_min)

    append_mode_audit(
        config, "adopt-utility", soc=soc, previous_mode=previous_mode,
        action="adopted", result="ok",
        note=f"target {target_soc:.0f}%, eta {eta_min:.0f}min",
    )
    if not config.dry_run:
        write_utility_hold_state(ownership="adopted", target_soc=target_soc, max_expiry=max_expiry, start_soc=soc)
        command_pause(config, max_min / 60.0, f"adopted Utility to {target_soc:.0f}%")

    if config.discord_notify_success and not config.dry_run:
        send_discord_embed(config, embed_topup_soc_started(soc, target_soc, eta_min, max_min, ownership="adopted"))

    from growatt_guard.growatt_api import format_duration_minutes
    print(
        f"Adopted Utility: {soc:.0f}% -> {target_soc:.0f}%, "
        f"ETA {format_duration_minutes(eta_min)}, max {format_duration_minutes(max_min)}, "
        "will return to SBU."
    )
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


def command_mute_battery_alert(config: Config) -> int:
    """Permanently mute battery-alert notifications until unmuted."""
    write_battery_alert_mute()
    print("Battery alert muted. Re-enable with: unmute-battery-alert")
    return 0


def command_unmute_battery_alert(config: Config) -> int:
    """Re-enable battery-alert notifications."""
    clear_battery_alert_mute()
    print("Battery alert re-enabled.")
    return 0


def command_mute_waste_alert(config: Config) -> int:
    """Permanently mute waste-alert-check notifications until unmuted."""
    write_waste_alert_mute()
    print("Waste alert muted. Re-enable with: unmute-waste-alert")
    return 0


def command_unmute_waste_alert(config: Config) -> int:
    """Re-enable waste-alert-check notifications."""
    clear_waste_alert_mute()
    print("Waste alert re-enabled.")
    return 0


def _pv_can_cover_load(status: dict) -> tuple[float, float, bool]:
    """Return (pv_w, load_w, can_cover) by reading PV and load power from status."""
    from growatt_guard.growatt_api import extract_channel_metric_sum, parse_number, extract_first_metric
    from growatt_guard.growatt_api import PV_POWER_CHANNELS

    pv_result = extract_channel_metric_sum(status, PV_POWER_CHANNELS)
    pv_w = pv_result[0] if pv_result is not None else None
    if pv_w is None:
        pv_w = 0.0

    load_keys = ("outPutPower", "outPutPower1", "activePower", "outPower", "pLoad", "pLoadText")
    load_raw = extract_first_metric(status, load_keys)
    load_w_val = parse_number(load_raw[0]) if load_raw else None
    load_w = load_w_val if load_w_val is not None else 0.0

    return pv_w, load_w, (pv_w > 0 and pv_w >= load_w)


def command_waste_alert_check(config: Config) -> int:
    """Notify if Utility is on during daylight, PV can cover load, and no Guard hold is active."""
    current_source_result = None
    soc: float | None = None
    try:
        _, _, status = load_context(config)
        soc_result = extract_soc(status)
        if soc_result:
            soc, _ = soc_result
        current_source_result = extract_spf_output_source(status)
    except Exception as exc:  # noqa: BLE001
        logging.warning("waste-alert-check: could not load status: %s", exc)
        return 0

    # Must be on Utility to be wasteful.
    if not (current_source_result and current_source_result[0] == "2"):
        clear_waste_alert_state()
        return 0

    # If Guard owns/adopted this state, it's intentional — not waste.
    ownership = utility_hold_ownership()
    if ownership in ("owned", "adopted"):
        clear_waste_alert_state()
        return 0

    pv_w, load_w, can_cover = _pv_can_cover_load(status)
    if not can_cover:
        # PV can't cover load — not waste (or nighttime).
        clear_waste_alert_state()
        return 0

    # Muted (permanent until unmuted)?
    if waste_alert_is_muted():
        print("Waste alert is muted.")
        return 0

    # Snoozed?
    if waste_alert_is_snoozed():
        print("Waste condition detected but alerts are snoozed.")
        return 0

    # Throttle to once per 30 min.
    if not waste_alert_is_due(cooldown_minutes=30.0):
        print(f"Waste condition detected (PV {pv_w:g} W covers load {load_w:g} W); alert already sent recently.")
        return 0

    logging.warning(
        "Utility on during daylight: PV %.0f W covers load %.0f W; no Guard ownership.", pv_w, load_w
    )
    if config.discord_notify_failure and not config.dry_run:
        send_discord_embed(config, embed_waste_alert(soc, pv_w, load_w))
    write_waste_alert_last_sent()
    print(
        f"Waste alert sent: Utility on, PV {pv_w:g} W can cover load {load_w:g} W, "
        "no Guard ownership — no auto-return made."
    )
    return 0


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
