from __future__ import annotations

import logging

from growatt_guard.config import Config
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import (
    PV_POWER_CHANNELS,
    describe_status_output_source,
    detect_grid_bypass,
    detect_unexpected_grid_bypass,
    estimate_runtime,
    extract_channel_metric_sum,
    extract_first_metric,
    extract_soc,
    extract_spf_output_source,
    load_context,
    parse_number,
)
from growatt_guard.notifications import (
    embed_battery_alert,
    embed_battery_cleared,
    embed_bypass_alert,
    embed_bypass_cleared,
    embed_runtime_alert,
    embed_runtime_alert_cleared,
    embed_utility_unavailable_alert,
    embed_waste_alert,
    send_discord_embed,
)
from growatt_guard.state import (
    battery_alert_is_muted,
    clear_battery_alert_mute,
    clear_battery_alert_state,
    clear_bypass_alert_state,
    clear_runtime_alert_state,
    clear_waste_alert_mute,
    clear_waste_alert_state,
    read_battery_alert_state,
    read_bypass_alert_state,
    read_runtime_alert_state,
    topup_is_active,
    utility_hold_ownership,
    waste_alert_is_due,
    waste_alert_is_muted,
    waste_alert_is_snoozed,
    write_battery_alert_mute,
    write_battery_alert_state,
    write_bypass_alert_state,
    write_runtime_alert_state,
    write_waste_alert_last_sent,
    write_waste_alert_mute,
)

_BYPASS_ALERT_MAX_SENDS = 3

def _low_soc_utility_missing_reason(status: dict, soc: float, threshold: float) -> str | None:
    if soc > threshold:
        return None

    bypass = detect_grid_bypass(status)
    if bypass.get("detected"):
        return None

    evidence = ["no grid bypass or AC charge detected"]
    for label, key in (
        ("charge_w", "charge_w"),
        ("grid_w", "grid_w"),
        ("discharge_w", "discharge_w"),
    ):
        value = bypass.get(key)
        if isinstance(value, (int, float)):
            evidence.append(f"{label}={value:g}")
    battery_status = str(bypass.get("battery_status") or "").strip()
    if battery_status:
        evidence.append(f"status={battery_status}")
    return "; ".join(evidence)


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
    utility_expected_soc = config.battery_bms_cutoff_soc if config.battery_bms_cutoff_soc > 0 else config.emergency_soc
    utility_missing_reason = _low_soc_utility_missing_reason(status, soc, utility_expected_soc)
    utility_missing = utility_missing_reason is not None

    if soc < config.emergency_soc:
        if state and state.get("active"):
            if utility_missing and not state.get("utility_unavailable"):
                if not config.discord_webhook_url:
                    raise GrowattGuardError("DISCORD_WEBHOOK_URL must be configured for emergency battery alerts.")
                if not send_discord_embed(
                    config,
                    embed_utility_unavailable_alert(soc, utility_expected_soc, previous_mode, utility_missing_reason),
                ):
                    raise GrowattGuardError("Low battery utility-missing alert could not be sent to Discord.")
                write_battery_alert_state(soc, utility_unavailable=True)
                print(
                    f"Low battery utility-missing alert sent: SOC {soc:g}% <= "
                    f"{utility_expected_soc:g}% ({utility_missing_reason})."
                )
                return 0
            print(
                f"Emergency battery alert already active: SOC {soc:g}% < "
                f"{config.emergency_soc:g}% ({previous_mode})."
            )
            return 0
        if not config.discord_webhook_url:
            raise GrowattGuardError("DISCORD_WEBHOOK_URL must be configured for emergency battery alerts.")

        embed = (
            embed_utility_unavailable_alert(soc, utility_expected_soc, previous_mode, utility_missing_reason)
            if utility_missing
            else embed_battery_alert(soc, config.emergency_soc, previous_mode)
        )
        if not send_discord_embed(config, embed):
            raise GrowattGuardError("Emergency battery alert could not be sent to Discord.")
        write_battery_alert_state(soc, utility_unavailable=utility_missing)
        suffix = f" Utility/charging not detected: {utility_missing_reason}." if utility_missing else ""
        print(f"Emergency battery alert sent: SOC {soc:g}% < {config.emergency_soc:g}%.{suffix}")
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
