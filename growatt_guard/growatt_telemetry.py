from __future__ import annotations

import re
from typing import Any


SOC_KEYS = (
    "SOC",
    "soc",
    "bmsSoc",
    "capacity",
    "batteryCapacity",
    "batterySoc",
    "batCapacity",
    "batteryPercent",
    "battery_percentage",
    "eCapacity",
)

SPF_OUTPUT_SOURCE = {
    "0": "SBU priority",
    "1": "Solar first",
    "2": "Utility first",
    "3": "SUB priority",
}

PV_POWER_CHANNELS = (
    ("pPv1", "ppv1", "pv1Power", "ppv", "ppvText"),
    ("pPv2", "ppv2", "pv2Power"),
)
PV_TODAY_CHANNELS = (
    ("epv1Today", "ePv1Today"),
    ("epv2Today", "ePv2Today"),
)
DETAIL_PREFERRED_METRIC_KEYS = {
    "pCharge",
    "pCharge1",
    "pChargeText",
    "chargePower",
    "pDischarge",
    "pDischarge1",
    "pDischargeText",
    "dischargePower",
}


def deep_values(data: Any, path: str = "") -> list[tuple[str, Any]]:
    values: list[tuple[str, Any]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            next_path = f"{path}.{key}" if path else str(key)
            values.extend(deep_values(value, next_path))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            values.extend(deep_values(value, f"{path}[{index}]"))
    else:
        values.append((path, data))
    return values


def parse_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        if match:
            return float(match.group(0))
    return None


def extract_channel_metric_sum(
    data: Any,
    channels: tuple[tuple[str, ...], ...],
) -> tuple[float, str] | None:
    """Return a summed metric from one alias per physical channel.

    Growatt sometimes reports PV1 as a total-looking key (`ppv`) and PV2 as
    `ppv2`/`pPv2`. Keep this logic shared so dashboard and PVOutput interpret
    those shapes the same way.
    """
    partial: tuple[float, str, int] | None = None

    def remember_partial(total: float, source: str, count: int) -> None:
        nonlocal partial
        if partial is None or count > partial[2] or (count == partial[2] and total > partial[0]):
            partial = (total, source, count)

    def walk(node: Any, path: str = "") -> tuple[float, str] | None:
        if isinstance(node, dict):
            total = 0.0
            paths: list[str] = []
            for aliases in channels:
                for key in aliases:
                    if key not in node:
                        continue
                    parsed = parse_number(node[key])
                    if parsed is None:
                        continue
                    total += parsed
                    paths.append(f"{path}.{key}" if path else key)
                    break
            if paths:
                source = "channel-sum:" + ",".join(paths)
                if len(paths) == len(channels):
                    return total, source
                remember_partial(total, source, len(paths))
            for key, value in node.items():
                result = walk(value, f"{path}.{key}" if path else str(key))
                if result is not None:
                    return result
        elif isinstance(node, list):
            for index, value in enumerate(node):
                result = walk(value, f"{path}[{index}]")
                if result is not None:
                    return result
        return None

    result = walk(data)
    if result is not None:
        return result
    if partial is not None:
        return partial[0], partial[1]
    return None


def extract_soc(data: dict[str, Any]) -> tuple[float, str] | None:
    flat = deep_values(data)
    for wanted_key in SOC_KEYS:
        for path, value in flat:
            if path.split(".")[-1] == wanted_key:
                parsed = parse_number(value)
                if parsed is not None and 0 <= parsed <= 100:
                    return parsed, path
    for path, value in flat:
        if "soc" in path.lower() or "capacity" in path.lower():
            parsed = parse_number(value)
            if parsed is not None and 0 < parsed <= 100:
                return parsed, path
    return None


def extract_spf_output_source(data: dict[str, Any]) -> tuple[str, str, str] | None:
    # Prefer paths through a *Bean object — avoids shadowing from top-level or device keys
    fallback = None
    for path, value in deep_values(data):
        if path.split(".")[-1] == "outputConfig":
            raw = str(value)
            if "Bean" in path:
                return raw, SPF_OUTPUT_SOURCE.get(raw, f"Unknown ({raw})"), path
            if fallback is None:
                fallback = (raw, SPF_OUTPUT_SOURCE.get(raw, f"Unknown ({raw})"), path)
    return fallback


def output_source_label(raw: str) -> str:
    return SPF_OUTPUT_SOURCE.get(raw, f"Unknown ({raw})")


def extract_first_metric(data: dict[str, Any], keys: tuple[str, ...]) -> tuple[Any, str] | None:
    flat = deep_values(data)
    for wanted_key in keys:
        matches = [
            (path, value)
            for path, value in flat
            if path.split(".")[-1] == wanted_key and value not in (None, "")
        ]
        if not matches:
            continue
        if wanted_key in DETAIL_PREFERRED_METRIC_KEYS:
            matches.sort(key=lambda item: 0 if "Detail" in item[0] else 1)
        path, value = matches[0]
        return value, path
    return None


def format_metric(data: dict[str, Any], label: str, keys: tuple[str, ...], unit: str = "") -> str | None:
    result = extract_first_metric(data, keys)
    if not result:
        return None
    value, _ = result
    if isinstance(value, str) and re.search(r"[a-zA-Z%]", value):
        return f"{label}: {value}"
    return f"{label}: {value}{unit}"


def estimate_runtime(
    soc: float,
    p_discharge_w: float,
    battery_capacity_wh: float,
    bms_cutoff_soc: float = 25.0,
) -> float | None:
    """Return estimated runtime in minutes down to BMS cutoff, or None if inputs are invalid."""
    if battery_capacity_wh <= 0 or p_discharge_w <= 0:
        return None
    if soc <= bms_cutoff_soc:
        return 0.0
    usable_wh = battery_capacity_wh * (soc - bms_cutoff_soc) / 100.0
    return usable_wh / p_discharge_w * 60.0


def estimate_charge_time(
    soc: float,
    p_charge_w: float,
    battery_capacity_wh: float,
) -> float | None:
    """Return estimated time to full charge in minutes, or None if inputs are invalid."""
    if battery_capacity_wh <= 0 or p_charge_w <= 0 or soc >= 100.0:
        return None
    remaining_wh = battery_capacity_wh * (100.0 - soc) / 100.0
    return remaining_wh / p_charge_w * 60.0


def estimate_topup_for_sunrise(
    soc: float,
    load_w: float,
    battery_capacity_wh: float,
    bms_cutoff_soc: float,
    charge_rate_w: float,
    hours_to_sunrise: float,
) -> float | None:
    """Return topup minutes needed to survive until sunrise at current load rate.

    Returns 0.0 if battery is already sufficient, None if inputs are insufficient.
    During topup each minute saves load_w Wh of discharge AND adds charge_rate_w Wh,
    so the effective rate is (charge_rate_w + load_w) Wh per 60 minutes.
    """
    if battery_capacity_wh <= 0 or charge_rate_w <= 0 or hours_to_sunrise <= 0 or load_w <= 0:
        return None
    usable_wh = max(0.0, (soc - bms_cutoff_soc) / 100.0 * battery_capacity_wh)
    needed_wh = load_w * hours_to_sunrise
    if usable_wh >= needed_wh:
        return 0.0
    return (needed_wh - usable_wh) / (charge_rate_w + load_w) * 60.0


def format_duration_minutes(minutes: float) -> str:
    m = round(minutes)
    if m >= 60:
        return f"{m // 60}h {m % 60:02d}m"
    return f"{m}min"


def extract_battery_status(data: dict[str, Any]) -> str | None:
    # DetailBean contains human-readable values like "Discharge", "Charging", "Standby"
    for path, value in deep_values(data):
        if path.split(".")[-1] == "statusText" and "Detail" in path:
            s = str(value).strip()
            if s and "." not in s:
                return s
    # Fallback: any statusText that isn't a dot-notation key like "storage.status.discharge"
    for path, value in deep_values(data):
        if path.split(".")[-1] == "statusText":
            s = str(value).strip()
            if s and "." not in s:
                return s
    return None


def _first_metric_number(data: dict[str, Any], keys: tuple[str, ...]) -> tuple[float | None, str]:
    result = extract_first_metric(data, keys)
    if not result:
        return None, ""
    parsed = parse_number(result[0])
    return parsed, result[1]


def detect_grid_bypass(data: dict[str, Any], min_power_w: float = 100.0) -> dict[str, Any]:
    """Detect actual grid bypass/AC charging from live status metrics.

    outputConfig is only the configured priority. SPF payloads can still report
    the real power path through statusText and charge/grid power readings.
    """
    battery_status = extract_battery_status(data) or ""
    output_source = extract_spf_output_source(data)
    charge_w, charge_source = _first_metric_number(data, ("pCharge", "pCharge1", "pChargeText", "chargePower"))
    discharge_w, discharge_source = _first_metric_number(data, ("pDischarge", "pDischarge1", "pDischargeText", "dischargePower"))
    grid_w, grid_source = _first_metric_number(
        data,
        ("pGrid", "pGridText", "gridPower", "pImport", "pImportText", "pAcInput", "pAcInPut", "pacToUser", "pToUser"),
    )
    text_bypass = "bypass" in battery_status.lower()
    grid_charging = (charge_w or 0.0) > min_power_w and (grid_w or 0.0) > min_power_w
    detected = text_bypass or grid_charging
    reasons: list[str] = []
    if text_bypass:
        reasons.append(f"statusText={battery_status}")
    if grid_charging:
        reasons.append(f"grid_w={grid_w:g}, charge_w={charge_w:g}")
    return {
        "detected": detected,
        "reason": "; ".join(reasons),
        "battery_status": battery_status,
        "output_raw": output_source[0] if output_source else "",
        "output_label": output_source[1] if output_source else "",
        "charge_w": charge_w,
        "charge_source": charge_source,
        "discharge_w": discharge_w,
        "discharge_source": discharge_source,
        "grid_w": grid_w,
        "grid_source": grid_source,
    }


def detect_unexpected_grid_bypass(
    data: dict[str, Any],
    min_power_w: float = 100.0,
    recovery_soc: float = 40.0,
) -> dict[str, Any]:
    """Detect grid bypass that conflicts with SBU priority.

    Growatt reports normal Utility operation as "Bypass" because the grid is
    feeding the load. For alerts/dashboard warnings, only treat it as bypass
    when the configured output source is SBU and the live power path still
    shows grid bypass or grid charging above the low-SOC recovery band.
    """
    result = detect_grid_bypass(data, min_power_w=min_power_w)
    output_label = str(result.get("output_label") or "").lower()
    output_raw = str(result.get("output_raw") or "")
    sbu_configured = "sbu" in output_label or output_raw == "0"
    soc_result = extract_soc(data)
    soc = soc_result[0] if soc_result else None
    low_soc_recovery = isinstance(soc, (int, float)) and recovery_soc > 0 and soc <= recovery_soc
    raw_detected = bool(result.get("detected"))
    result["detected"] = raw_detected and sbu_configured and not low_soc_recovery
    result["expected_utility"] = raw_detected and not sbu_configured
    result["expected_recovery"] = raw_detected and sbu_configured and low_soc_recovery
    result["soc"] = soc
    result["recovery_soc"] = recovery_soc
    if (result["expected_utility"] or result["expected_recovery"]) and not result["detected"]:
        result["reason"] = ""
    return result


