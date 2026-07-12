from __future__ import annotations

import datetime as dt
import json
import math
from statistics import mean
from typing import Any

from growatt_guard.config import Config
from growatt_guard.growatt_api import (
    extract_first_metric,
    extract_spf_output_source,
    extract_status_soc,
    load_context,
    parse_number,
)
from growatt_guard.state import (
    parse_utc_datetime,
    read_charge_rate_history,
    read_utility_hold_state,
    utc_now,
)

TOPUP_STALL_MINUTES = 20


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def learned_charge_rate() -> tuple[float | None, int]:
    rates = [
        float(row["rate_w"])
        for row in read_charge_rate_history()
        if isinstance(row.get("rate_w"), (int, float)) and float(row["rate_w"]) > 0
    ]
    if len(rates) < 2:
        return None, len(rates)
    return mean(rates), len(rates)


def build_topup_status_payload(
    hold: dict[str, Any] | None,
    current_soc: float | None,
    config: Config,
    *,
    charge_w: float | None = None,
    output_mode_raw: str = "",
    learned_rate_w: float | None = None,
    learned_rate_samples: int = 0,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    if hold is None:
        return {
            "active": False,
            "current_soc": current_soc,
            "configured_charge_rate_w": config.battery_charge_rate_w,
            "learned_charge_rate_w": round(learned_rate_w) if learned_rate_w else None,
            "learned_charge_rate_samples": learned_rate_samples,
            "warnings": [],
        }
    try:
        started_at = parse_utc_datetime(str(hold["started_at"]))
        max_expiry = parse_utc_datetime(str(hold["max_expiry"]))
    except (KeyError, ValueError) as exc:
        return {
            "active": True,
            "valid": False,
            "error": str(exc),
            "current_soc": current_soc,
            "warnings": ["Top-up state timestamps are invalid."],
        }

    elapsed_minutes = max(0, math.floor((now - started_at).total_seconds() / 60))
    remaining_to_expiry = max(0, math.ceil((max_expiry - now).total_seconds() / 60))
    target_soc = _number(hold.get("target_soc"))
    start_soc = _number(hold.get("start_soc"))
    completion_policy = str(hold.get("completion_policy") or "soc")
    soc_gain = current_soc - start_soc if current_soc is not None and start_soc is not None else None
    observed_rate_w = None
    if soc_gain is not None and soc_gain > 0 and elapsed_minutes >= 10 and config.battery_capacity_wh > 0:
        observed_rate_w = (soc_gain / 100.0 * config.battery_capacity_wh) / (elapsed_minutes / 60.0)

    projection_rate_w = None
    projection_rate_source = "maximum expiry"
    if observed_rate_w is not None:
        projection_rate_w = observed_rate_w
        projection_rate_source = "observed SOC gain"
    elif learned_rate_w is not None and learned_rate_samples >= 2:
        projection_rate_w = learned_rate_w
        projection_rate_source = f"learned rate ({learned_rate_samples} samples)"
    elif config.battery_charge_rate_w > 0:
        projection_rate_w = config.battery_charge_rate_w
        projection_rate_source = "configured charge rate"

    projected_at = max_expiry
    projected_basis = "maximum expiry"
    projected_minutes = remaining_to_expiry
    if completion_policy == "soc" and target_soc is not None and current_soc is not None:
        if current_soc >= target_soc:
            projected_at = now
            projected_minutes = 0
            projected_basis = "target already reached; next completion check"
        elif projection_rate_w and config.battery_capacity_wh > 0:
            needed_wh = (target_soc - current_soc) / 100.0 * config.battery_capacity_wh
            estimate_minutes = max(1, math.ceil(needed_wh / projection_rate_w * 60))
            estimate_at = now + dt.timedelta(minutes=estimate_minutes)
            if estimate_at < max_expiry:
                projected_at = estimate_at
                projected_minutes = estimate_minutes
                projected_basis = projection_rate_source

    warnings: list[str] = []
    if output_mode_raw and output_mode_raw != "2":
        warnings.append(f"Utility hold is active but inverter output mode is [{output_mode_raw}], not Utility first [2].")
    if elapsed_minutes >= TOPUP_STALL_MINUTES:
        if charge_w is not None and charge_w < 100:
            warnings.append("Top-up appears stalled: reported battery charge power is below 100 W.")
        elif soc_gain is not None and soc_gain <= 0 and charge_w is None:
            warnings.append("Top-up may be stalled: SOC has not increased and charge power is unavailable.")
    if (
        observed_rate_w is not None
        and config.battery_charge_rate_w > 0
        and observed_rate_w < config.battery_charge_rate_w * 0.8
    ):
        warnings.append(
            f"Charging is slower than configured: observed {observed_rate_w:.0f} W versus "
            f"{config.battery_charge_rate_w:.0f} W configured."
        )

    return {
        "active": True,
        "valid": True,
        "current_soc": current_soc,
        "start_soc": start_soc,
        "soc_gain": round(soc_gain, 1) if soc_gain is not None else None,
        "target_soc": target_soc,
        "ownership": str(hold.get("ownership") or "unknown"),
        "completion_policy": completion_policy,
        "elapsed_minutes": elapsed_minutes,
        "started_at": started_at.isoformat(),
        "max_expiry": max_expiry.isoformat(),
        "remaining_to_expiry_minutes": remaining_to_expiry,
        "projected_completion": projected_at.isoformat(),
        "projected_completion_minutes": projected_minutes,
        "projected_basis": projected_basis,
        "configured_charge_rate_w": config.battery_charge_rate_w,
        "learned_charge_rate_w": round(learned_rate_w) if learned_rate_w else None,
        "learned_charge_rate_samples": learned_rate_samples,
        "observed_charge_rate_w": round(observed_rate_w) if observed_rate_w else None,
        "projection_charge_rate_w": round(projection_rate_w) if projection_rate_w else None,
        "charge_w": round(charge_w) if charge_w is not None else None,
        "output_mode_raw": output_mode_raw,
        "reason": str(hold.get("reason") or ""),
        "warnings": warnings,
    }


def collect_topup_status(
    config: Config,
    *,
    status: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    hold = read_utility_hold_state()
    learned_rate_w, learned_samples = learned_charge_rate()
    if hold is None:
        return build_topup_status_payload(
            None,
            None,
            config,
            learned_rate_w=learned_rate_w,
            learned_rate_samples=learned_samples,
            now=now,
        )
    if status is None:
        _, _, status = load_context(config)
    current_soc = extract_status_soc(status)
    output_source = extract_spf_output_source(status)
    charge_metric = extract_first_metric(status, ("pCharge", "pChargeText", "chargePower"))
    charge_w = parse_number(charge_metric[0]) if charge_metric else None
    return build_topup_status_payload(
        hold,
        current_soc,
        config,
        charge_w=charge_w,
        output_mode_raw=output_source[0] if output_source else "",
        learned_rate_w=learned_rate_w,
        learned_rate_samples=learned_samples,
        now=now,
    )


def format_topup_status(payload: dict[str, Any]) -> str:
    if not payload.get("active"):
        learned = payload.get("learned_charge_rate_w")
        samples = payload.get("learned_charge_rate_samples", 0)
        suffix = f" Learned charge rate: {learned:g} W ({samples} samples)." if learned else ""
        return "No active Guard-owned top-up." + suffix
    if not payload.get("valid"):
        return f"Active top-up state is invalid: {payload.get('error', 'unknown error')}"
    soc_text = f"{payload['current_soc']:g}%" if isinstance(payload.get("current_soc"), (int, float)) else "unavailable"
    lines = [
        "Growatt top-up status",
        f"SOC: {soc_text}",
        f"Target: {payload.get('target_soc') if payload.get('target_soc') is not None else 'time-based'}",
        f"Ownership: {payload['ownership']} ({payload['completion_policy']})",
        f"Elapsed: {payload['elapsed_minutes']} min",
        f"Maximum expiry: {payload['max_expiry']}",
        f"Projected completion: {payload['projected_completion']} ({payload['projected_basis']})",
        (
            "Charge rates: "
            f"configured={payload.get('configured_charge_rate_w') or 'unavailable'} W, "
            f"learned={payload.get('learned_charge_rate_w') or 'unavailable'} W, "
            f"observed={payload.get('observed_charge_rate_w') or 'unavailable'} W"
        ),
    ]
    if payload.get("soc_gain") is not None:
        lines.append(f"SOC gain: {payload['soc_gain']:+g}%")
    lines.extend(f"WARNING: {warning}" for warning in payload.get("warnings", []))
    return "\n".join(lines)


def command_topup_status(config: Config, json_output: bool = False) -> int:
    payload = collect_topup_status(config)
    print(json.dumps(payload, indent=2, sort_keys=True) if json_output else format_topup_status(payload))
    return 0
