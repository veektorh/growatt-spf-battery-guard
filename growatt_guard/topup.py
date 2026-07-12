from __future__ import annotations

import datetime as dt
import logging

from growatt_guard.audit import append_mode_audit, find_overdue_unclosed_topup
from growatt_guard.config import Config
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import (
    describe_status_output_source,
    estimate_topup_for_sunrise,
    extract_first_metric,
    extract_soc,
    extract_spf_output_source,
    load_context,
    parse_number,
    set_mode,
)
from growatt_guard.modes import command_return_sbu
from growatt_guard.load_learning import select_overnight_load
from growatt_guard.notifications import (
    embed_auto_topup_started,
    embed_topup_below_target,
    embed_topup_complete_summary,
    embed_topup_failed_low,
    embed_topup_skipped_sunny,
    embed_topup_soc_complete,
    embed_topup_soc_started,
    send_discord_embed,
)
from growatt_guard.pause import command_pause, command_resume
from growatt_guard.state import (
    append_charge_rate_reading,
    append_discharge_rate_reading,
    clear_pause_state,
    clear_topup_state,
    clear_utility_hold_state,
    parse_utc_datetime,
    read_discharge_rate_history,
    read_pause_state,
    read_topup_state,
    read_utility_hold_state,
    topup_is_active,
    topup_skip_notification_due,
    utility_hold_ownership,
    utc_now,
    write_topup_skip_notification_state,
    write_utility_hold_state,
)
from growatt_guard.weather import hours_until_next_sunrise

_TOPUP_EXPIRY_BUFFER_FACTOR = 1.2
_TOPUP_EXPIRY_BUFFER_MIN_MINUTES = 15.0

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



def _sunrise_hours(config: Config) -> float | None:
    try:
        return hours_until_next_sunrise(config)
    except Exception:  # noqa: BLE001
        return None

def _persist_auto_topup_intent(
    config: Config,
    *,
    minutes: int,
    reason: str,
    paused_until: dt.datetime,
    start_soc: float,
    start_load_w: float,
    target_soc: float,
) -> None:
    """Persist recoverable ownership intent before changing physical inverter mode."""
    command_pause(config, minutes / 60.0, reason)
    try:
        write_utility_hold_state(
            ownership="owned",
            target_soc=target_soc,
            max_expiry=paused_until,
            start_soc=start_soc,
            completion_policy="soc",
            minutes=minutes,
            reason=reason,
            start_load_w=start_load_w,
        )
    except Exception:
        clear_utility_hold_state()
        clear_topup_state()
        clear_pause_state()
        raise


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

    append_discharge_rate_reading(load_w, aggregate_nightly=True)
    history = read_discharge_rate_history()
    learned_load = select_overnight_load(history)
    if learned_load["rate_w"] is not None:
        avg_load_w = float(learned_load["rate_w"])
        logging.info(
            "Using learned discharge rate %.0f W (%s) instead of live %.0f W",
            avg_load_w, learned_load["source"], load_w,
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
    _persist_auto_topup_intent(
        config,
        minutes=topup_min,
        reason=reason,
        paused_until=paused_until,
        start_soc=soc,
        start_load_w=load_w,
        target_soc=current_soc_target,
    )

    try:
        result = set_mode(api, config, device, "utility")
    except Exception as exc:  # noqa: BLE001
        append_mode_audit(
            config, "auto-topup-check", soc=soc, previous_mode=previous_mode,
            action="utility-failed", result="error", note=str(exc),
        )
        # The request may have reached the inverter even when the cloud call failed.
        # Keep ownership intent so the completion check can reconcile by returning SBU.
        raise

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


def _optional_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _read_topup_end_soc(config: Config) -> float | None:
    try:
        _, _, status = load_context(config)
        result = extract_soc(status)
        return result[0] if result else None
    except Exception as exc:  # noqa: BLE001 - completion must still restore SBU without telemetry
        logging.warning("topup-complete-check: could not read final SOC: %s", exc)
        return None


def _elapsed_minutes(started_at: object, default: float = 0.0) -> float:
    if not started_at:
        return default
    try:
        return max(1.0, (utc_now() - parse_utc_datetime(str(started_at))).total_seconds() / 60.0)
    except ValueError:
        return default


def _return_sbu_and_clear_topup(config: Config) -> int:
    try:
        result = command_return_sbu(config)
    except Exception:
        logging.warning("topup-complete-check: return-sbu failed; state preserved for retry")
        raise
    if result != 0:
        logging.warning("topup-complete-check: return-sbu was blocked; state preserved for retry")
        return result
    clear_utility_hold_state()
    clear_topup_state()
    return result


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
    if hold_state is not None and hold_state.get("completion_policy", "soc") == "soc":
        ownership = str(hold_state.get("ownership", "owned"))
        target_soc_raw = hold_state.get("target_soc")
        max_expiry_str = hold_state.get("max_expiry")
        start_soc_raw = hold_state.get("start_soc")
        started_at_str = hold_state.get("started_at")

        target_soc = _optional_float(target_soc_raw)
        start_soc = _optional_float(start_soc_raw)
        end_soc = _read_topup_end_soc(config)
        actual_min = _elapsed_minutes(started_at_str)

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
            return _return_sbu_and_clear_topup(config)

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
                return _return_sbu_and_clear_topup(config)

            if max_expiry is not None:
                remaining_min = max(0, int((max_expiry - utc_now()).total_seconds() // 60))
                soc_str = f"{end_soc:.0f}%" if end_soc is not None else "unknown"
                target_str = f"{target_soc:.0f}%" if target_soc is not None else "unknown"
                print(f"Topup active ({ownership}): SOC {soc_str} / {target_str} target, ~{remaining_min} min max remaining.")
                return 0

        # Hold exists but no max_expiry and target not reached — still active.
        print("Topup active; skipping.")
        return 0

    # ---- Time-based completion policy (including migrated legacy topup state) ----
    if topup_is_active():
        try:
            paused_until = parse_utc_datetime(str(topup_state["paused_until"]))
            remaining = max(0, int((paused_until - utc_now()).total_seconds() // 60))
            print(f"Topup still active (~{remaining} min remaining); skipping.")
        except (KeyError, ValueError):
            print("Topup still active; skipping.")
        return 0

    logging.info("Topup window expired; completing topup (legacy time-based).")

    end_soc_legacy = _read_topup_end_soc(config)

    command_resume(config)

    start_soc_legacy = _optional_float(topup_state.get("start_soc"))
    planned_min = _optional_float(topup_state.get("minutes")) or 0.0
    actual_min_legacy = _elapsed_minutes(topup_state.get("started_at"), planned_min)

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

    return _return_sbu_and_clear_topup(config)


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
