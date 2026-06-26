from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path
from typing import Any

import requests

from growatt_guard.growatt_api import extract_first_metric, extract_soc, load_context, parse_number
from growatt_guard.state import read_json_state, write_json_state

PVOUTPUT_URL = "https://pvoutput.org/service/r2/addstatus.jsp"
PVOUTPUT_GETOUTPUT_URL = "https://pvoutput.org/service/r2/getoutput.jsp"
PVOUTPUT_STATE_FILE = Path(__file__).resolve().parents[1] / "state" / "pvoutput_last.json"

# Keys tried in order; first non-empty value wins.
# v1 must be PV generation energy — charge-energy fields (eacChargeToday, eChargeToday)
# are intentionally excluded because they include grid charging and would underreport PV.
_V1_KEYS = ("epvToday", "ePvToday", "epvTodayTotal", "epv1Today", "epv2Today")
_V2_KEYS = ("ppv", "ppvText", "pPv1", "pPv2")
_V2_CHANNELS = (("pPv1", "ppv1", "pv1Power", "ppv", "ppvText"), ("pPv2", "ppv2", "pv2Power"))
_V4_KEYS = ("outPutPower", "outPutPowerText", "activePower", "outPower")
_V6_KEYS = ("vGrid", "vGridText", "vAc1", "vac1")
_V8_KEYS = ("pCharge", "pChargeText", "chargePower")
_V9_KEYS = ("pDischarge", "pDischargeText", "dischargePower")


def app_module() -> Any:
    module = sys.modules.get("growatt_power_guard")
    if module is not None and hasattr(module, "GrowattGuardError"):
        return module
    main_module = sys.modules.get("__main__")
    if main_module is not None and hasattr(main_module, "GrowattGuardError"):
        return main_module
    import growatt_power_guard

    return growatt_power_guard


def _pvoutput_error(message: str) -> Exception:
    return app_module().GrowattGuardError(message)


def _extract_float_with_key(
    status: dict[str, Any], keys: tuple[str, ...]
) -> tuple[float, str] | None:
    result = extract_first_metric(status, keys)
    if result is None:
        return None
    value = parse_number(result[0])
    if value is None:
        return None
    return value, result[1].split(".")[-1]


def _extract_float(status: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    result = _extract_float_with_key(status, keys)
    return result[0] if result is not None else None


def _extract_channel_sum(data: Any, channels: tuple[tuple[str, ...], ...], path: str = "") -> tuple[float, str] | None:
    if isinstance(data, dict):
        total = 0.0
        paths: list[str] = []
        for aliases in channels:
            for key in aliases:
                if key not in data:
                    continue
                parsed = parse_number(data[key])
                if parsed is None:
                    continue
                total += parsed
                paths.append(f"{path}.{key}" if path else key)
                break
        if len(paths) == len(channels):
            return total, "channel-sum:" + ",".join(paths)
        for key, value in data.items():
            result = _extract_channel_sum(value, channels, f"{path}.{key}" if path else str(key))
            if result is not None:
                return result
    elif isinstance(data, list):
        for index, value in enumerate(data):
            result = _extract_channel_sum(value, channels, f"{path}[{index}]")
            if result is not None:
                return result
    return None


def extract_pvoutput_fields(
    status: dict[str, Any], now: dt.datetime | None = None
) -> dict[str, Any]:
    """Extract PVOutput-compatible fields from a Growatt status dict.

    Standard fields: v1 (Wh generated today), v2 (W PV power), v4 (W output power),
    v6 (V grid voltage). Extended fields (v7-v12, donation feature): v7 (% SOC),
    v8 (W charge power), v9 (W discharge power).
    """
    if now is None:
        now = dt.datetime.now()
    fields: dict[str, Any] = {
        "d": now.strftime("%Y%m%d"),
        "t": now.strftime("%H:%M"),
    }

    # v2: current PV generation power (W)
    pv_total = _extract_float(status, _V2_KEYS)
    pv_channel_result = _extract_channel_sum(status, _V2_CHANNELS)
    pv_power = pv_total
    if pv_channel_result is not None and (pv_total is None or pv_channel_result[0] > pv_total):
        pv_power = pv_channel_result[0]
        fields["_v2_key"] = pv_channel_result[1]
    if pv_power is not None and pv_power >= 0:
        fields["v2"] = int(pv_power)

    # v1: energy generated today (Wh) — Growatt stores kWh, convert to Wh
    v1_result = _extract_float_with_key(status, _V1_KEYS)
    if v1_result is not None:
        kwh, v1_key = v1_result
        if kwh >= 0:
            fields["v1"] = int(kwh * 1000)
            fields["_v1_key"] = v1_key

    # v4: current output / consumption power (W)
    output_power = _extract_float(status, _V4_KEYS)
    if output_power is not None and output_power >= 0:
        fields["v4"] = int(output_power)

    # v6: grid voltage (V)
    voltage = _extract_float(status, _V6_KEYS)
    if voltage is not None and voltage > 0:
        fields["v6"] = round(voltage, 1)

    # v7 (extended): battery state of charge (%)
    soc_result = extract_soc(status)
    if soc_result is not None:
        soc, _ = soc_result
        fields["v7"] = round(soc, 1)

    # v8 (extended): battery charge power (W)
    charge_power = _extract_float(status, _V8_KEYS)
    if charge_power is not None and charge_power >= 0:
        fields["v8"] = int(charge_power)

    # v9 (extended): battery discharge power (W)
    discharge_power = _extract_float(status, _V9_KEYS)
    if discharge_power is not None and discharge_power >= 0:
        fields["v9"] = int(discharge_power)

    return fields


def _strip_extended(params: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in params.items() if not (k.startswith("v") and k[1:].isdigit() and int(k[1:]) >= 7)}


def _do_post(config: Any, params: dict[str, str]) -> requests.Response:
    return requests.post(
        PVOUTPUT_URL,
        data=params,
        headers={
            "X-Pvoutput-Apikey": config.pvoutput_api_key,
            "X-Pvoutput-SystemId": str(config.pvoutput_system_id),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=15,
    )


def upload_pvoutput_status(config: Any, fields: dict[str, Any]) -> bool | None:
    """POST a status entry to PVOutput. Returns True on success, False on failure,
    or None when PVOutput benignly rejects the upload (e.g. "Moon Powered" at night).

    If the account does not have extended data enabled, automatically retries
    without v7-v12 so basic generation data is always recorded.
    """
    if not config.pvoutput_api_key or not config.pvoutput_system_id:
        raise _pvoutput_error("PVOUTPUT_API_KEY and PVOUTPUT_SYSTEM_ID must be set in .env.")

    params = {k: str(v) for k, v in fields.items() if not k.startswith("_")}

    try:
        response = _do_post(config, params)
    except requests.RequestException as exc:
        logging.error("PVOutput upload failed (network error): %s", exc)
        return False

    if response.status_code == 200:
        return True

    # "Moon Powered" is PVOutput rejecting a zero-generation status at night.
    # This is expected after dark, not a fault — skip quietly so it neither logs
    # an error nor triggers a Discord failure alert every overnight cycle.
    if response.status_code == 400 and "moon powered" in response.text.lower():
        logging.info("PVOutput upload skipped: no generation at night (Moon Powered).")
        return None

    # Extended data (v7-v12) requires a PVOutput donation feature. Retry without
    # extended fields so standard generation data is still recorded.
    if response.status_code == 400 and "extend" in response.text.lower():
        logging.warning("PVOutput extended data rejected; retrying without v7-v12.")
        try:
            retry = _do_post(config, _strip_extended(params))
        except requests.RequestException as exc:
            logging.error("PVOutput retry (no extended) failed: %s", exc)
            return False
        if retry.status_code == 200:
            return True
        logging.error(
            "PVOutput upload failed after extended-data retry: %s %s",
            retry.status_code,
            retry.text[:200],
        )
        return False

    logging.error("PVOutput upload failed: %s %s", response.status_code, response.text[:200])
    return False


def _pvoutput_summary(fields: dict[str, Any]) -> tuple[str, str]:
    skip = {"d", "t"}
    api_fields = {k: v for k, v in fields.items() if not k.startswith("_") and k not in skip}
    debug_fields = {k: v for k, v in fields.items() if k.startswith("_")}
    summary = ", ".join(f"{k}={v}" for k, v in sorted(api_fields.items()))
    debug = ", ".join(f"{k}={v}" for k, v in sorted(debug_fields.items()))
    return summary, debug


def write_pvoutput_state(fields: dict[str, Any], now: dt.datetime | None = None) -> None:
    if now is None:
        now = dt.datetime.now()
    write_json_state(PVOUTPUT_STATE_FILE, {
        "uploaded_at": now.isoformat(timespec="seconds"),
        "fields": {k: v for k, v in fields.items() if k not in ("d", "t")},
    })


def read_pvoutput_state() -> dict[str, Any] | None:
    return read_json_state(PVOUTPUT_STATE_FILE, "pvoutput")


def publish_pvoutput_status_from_status(
    config: Any,
    status: dict[str, Any],
    now: dt.datetime | None = None,
) -> tuple[bool, str]:
    if not getattr(config, "pvoutput_enabled", False):
        return True, "PVOutput upload skipped: set PVOUTPUT_ENABLED=true in .env to enable."

    fields = extract_pvoutput_fields(status)

    if "v1" not in fields and "v2" not in fields:
        raise _pvoutput_error(
            "Could not extract PV power or energy from Growatt status. "
            "Run 'probe' to inspect available fields."
        )

    summary, debug = _pvoutput_summary(fields)
    suffix = f" ({debug})" if debug else ""

    if config.dry_run:
        return True, f"DRY_RUN: would upload to PVOutput: {summary}{suffix}"

    if now is None:
        now = dt.datetime.now()
    result = upload_pvoutput_status(config, fields)
    if result is None:
        # Benign nighttime rejection (Moon Powered). Don't write state (keeps the
        # last real daytime values) and report success so no failure alert fires.
        return True, "PVOutput skipped: no generation to report (night)."
    if result:
        write_pvoutput_state(fields, now=now)
        return True, f"PVOutput OK: {summary}{suffix}"

    return False, "PVOutput upload failed; check logs for details."


def fetch_pvoutput_daily_outputs(
    config: Any,
    start_date: dt.date,
    end_date: dt.date,
) -> dict[str, int]:
    """Fetch daily PV energy generation (Wh) from PVOutput for a date range.

    Returns a dict mapping ISO date string (YYYY-MM-DD) to energy generated in Wh.
    Returns empty dict if PVOutput is not enabled, on network error, or non-200 response.
    """
    if not getattr(config, "pvoutput_enabled", False):
        return {}
    if not config.pvoutput_api_key or not config.pvoutput_system_id:
        return {}
    try:
        response = requests.get(
            PVOUTPUT_GETOUTPUT_URL,
            params={
                "df": start_date.strftime("%Y%m%d"),
                "dt": end_date.strftime("%Y%m%d"),
            },
            headers={
                "X-Pvoutput-Apikey": config.pvoutput_api_key,
                "X-Pvoutput-SystemId": str(config.pvoutput_system_id),
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        logging.warning("PVOutput getoutput failed: %s", exc)
        return {}
    if response.status_code != 200:
        logging.warning("PVOutput getoutput returned HTTP %s: %s", response.status_code, response.text[:200])
        return {}
    result: dict[str, int] = {}
    for line in response.text.strip().splitlines():
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            date_str = dt.datetime.strptime(parts[0].strip(), "%Y%m%d").date().isoformat()
            energy_wh = int(parts[1].strip())
            result[date_str] = energy_wh
        except (ValueError, IndexError):
            continue
    return result


def command_pvoutput_upload(config: Any) -> int:
    if not getattr(config, "pvoutput_enabled", False):
        ok, message = publish_pvoutput_status_from_status(config, {})
        print(message)
        return 0 if ok else 1

    _, _, status = load_context(config)
    ok, message = publish_pvoutput_status_from_status(config, status)
    print(message)
    if ok:
        return 0

    raise _pvoutput_error(message)
