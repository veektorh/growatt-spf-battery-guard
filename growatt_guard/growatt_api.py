from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from growatt_guard.notifications import record_growatt_cloud_success

try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:  # pragma: no cover - handled at runtime for friendlier output
    _load_dotenv = None

try:
    import growattServer
except ImportError:  # pragma: no cover - handled at runtime for friendlier output
    growattServer = None


BASE_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = BASE_DIR / "logs"

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

DEVICE_TYPE_PRIORITY = ("storage", "mix", "sph", "tlx", "inverter")


@dataclass(frozen=True)
class DeviceRef:
    plant_id: str
    device_sn: str
    device_type: str
    raw: dict[str, Any]


def app_module() -> Any:
    module = sys.modules.get("growatt_power_guard")
    if module is not None and hasattr(module, "GrowattGuardError"):
        return module

    main_module = sys.modules.get("__main__")
    if main_module is not None and hasattr(main_module, "GrowattGuardError"):
        return main_module

    import growatt_power_guard

    return growatt_power_guard


def api_error(message: str) -> Exception:
    return app_module().GrowattGuardError(message)


def require_dependencies() -> None:
    missing = []
    if _load_dotenv is None:
        missing.append("python-dotenv")
    if growattServer is None:
        missing.append("growattServer")
    if missing:
        raise api_error(
            "Missing dependencies: "
            + ", ".join(missing)
            + ". Install them with: python -m pip install -r requirements.txt"
        )


def connect(config: Any):
    require_dependencies()
    api = growattServer.GrowattApi(add_random_user_id=True, agent_identifier=config.username)
    api.server_url = config.server_url

    logging.info("Logging into Growatt server %s", config.server_url)
    login_response = api.login(config.username, config.password)
    if not isinstance(login_response, dict) or not login_response.get("success"):
        raise api_error(f"Growatt login failed: {login_response}")
    return api, login_response


def normalize_list_response(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("data", "back", "deviceList", "devices", "PlantList"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
            if isinstance(nested, dict):
                return normalize_list_response(nested)
    return []


def get_key(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def choose_plant(api, login_response: dict[str, Any], config: Any) -> str:
    if config.plant_id:
        return config.plant_id

    user = login_response.get("user", {})
    user_id = login_response.get("userId") or user.get("id")
    if not user_id:
        raise api_error("Login succeeded but no user id was returned by Growatt.")

    plants = normalize_list_response(api.plant_list(user_id))
    if not plants:
        plants = normalize_list_response(login_response)
    if not plants:
        raise api_error("No Growatt plants found for this account.")

    plant = plants[0]
    plant_id = get_key(plant, "plantId", "id")
    if not plant_id:
        raise api_error(f"Could not determine plant id from: {plant}")
    logging.info("Using plant %s (%s)", plant_id, get_key(plant, "plantName", "name") or "unnamed")
    return str(plant_id)


def normalize_device(device: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(device)
    normalized["deviceSn"] = str(get_key(device, "deviceSn", "device_sn", "sn", "serialNum") or "")
    normalized["deviceType"] = str(get_key(device, "deviceType", "type", "device_type") or "").lower()
    return normalized


def choose_device(api, plant_id: str, config: Any) -> DeviceRef:
    devices = [normalize_device(device) for device in normalize_list_response(api.device_list(plant_id))]
    if not devices:
        raise api_error(f"No devices found for plant {plant_id}.")

    if config.device_sn:
        for device in devices:
            if device["deviceSn"] == config.device_sn:
                return DeviceRef(plant_id, device["deviceSn"], device["deviceType"], device)
        raise api_error(f"Device {config.device_sn} was not found in plant {plant_id}.")

    for wanted_type in DEVICE_TYPE_PRIORITY:
        for device in devices:
            if device["deviceType"] == wanted_type and device["deviceSn"]:
                logging.info("Using %s device %s", device["deviceType"], device["deviceSn"])
                return DeviceRef(plant_id, device["deviceSn"], device["deviceType"], device)

    first = devices[0]
    if not first["deviceSn"]:
        raise api_error(f"Could not determine device serial from: {first}")
    logging.info("Using first device %s (%s)", first["deviceSn"], first["deviceType"] or "unknown type")
    return DeviceRef(plant_id, first["deviceSn"], first["deviceType"], first)


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
    for wanted_key in keys:
        for path, value in deep_values(data):
            if path.split(".")[-1] == wanted_key and value not in (None, ""):
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


def read_device_status(api, device: DeviceRef) -> dict[str, Any]:
    status: dict[str, Any] = {
        "plant_id": device.plant_id,
        "device_sn": device.device_sn,
        "device_type": device.device_type,
        "device": device.raw,
    }

    attempts: list[tuple[str, Any]] = []
    if device.device_type == "storage":
        attempts.extend(
            [
                ("storage_params", lambda: api.storage_params(device.device_sn)),
                ("storage_detail", lambda: api.storage_detail(device.device_sn)),
                (
                    "storage_energy_overview",
                    lambda: api.storage_energy_overview(device.plant_id, device.device_sn),
                ),
            ]
        )
    elif device.device_type == "mix":
        attempts.extend(
            [
                ("mix_info", lambda: api.mix_info(device.device_sn, device.plant_id)),
                ("mix_system_status", lambda: api.mix_system_status(device.device_sn, device.plant_id)),
                ("mix_detail", lambda: api.mix_detail(device.device_sn, device.plant_id)),
            ]
        )
    elif device.device_type == "tlx":
        attempts.extend(
            [
                ("tlx_detail", lambda: api.tlx_detail(device.device_sn)),
                ("tlx_params", lambda: api.tlx_params(device.device_sn)),
            ]
        )
    elif device.device_type == "inverter":
        attempts.append(("inverter_detail", lambda: api.inverter_detail(device.device_sn)))

    attempts.extend(
        [
            ("storage_params_fallback", lambda: api.storage_params(device.device_sn)),
            ("storage_detail_fallback", lambda: api.storage_detail(device.device_sn)),
            ("inverter_detail_fallback", lambda: api.inverter_detail(device.device_sn)),
        ]
    )

    errors: dict[str, str] = {}
    for name, func in attempts:
        if name in status:
            continue
        try:
            value = func()
        except Exception as exc:  # noqa: BLE001 - probing heterogeneous Growatt endpoints
            errors[name] = str(exc)
        else:
            if value:
                status[name] = value

    if errors:
        status["_probe_errors"] = errors
    return status


def summarize_status(
    status: dict[str, Any],
    battery_capacity_wh: float = 0.0,
    bms_cutoff_soc: float = 25.0,
    charge_rate_w: float = 0.0,
    hours_to_sunrise: float | None = None,
) -> str:
    soc_result = extract_soc(status)
    parts = [
        f"plant={status.get('plant_id')}",
        f"device={status.get('device_sn')}",
        f"type={status.get('device_type') or 'unknown'}",
    ]
    if soc_result:
        soc, path = soc_result
        parts.append(f"soc={soc:g}% ({path})")
    else:
        parts.append("soc=not found")
    output_source = extract_spf_output_source(status)
    if output_source:
        raw, label, path = output_source
        parts.append(f"output={label} [{raw}] ({path})")
    bat_status = extract_battery_status(status)
    if bat_status:
        parts.append(f"bat_status={bat_status}")
    _out_w = extract_first_metric(status, ("outPutPower", "outPutPower1", "activePower"))
    if _out_w:
        n = parse_number(_out_w[0])
        if n is not None:
            parts.append(f"out_w={n:g}")
    _load = extract_first_metric(status, ("loadPercent", "loadPercent1"))
    if _load:
        n = parse_number(_load[0])
        if n is not None:
            parts.append(f"load_pct={n:.0f}")
    _pd = extract_first_metric(status, ("pDischarge", "pDischarge1"))
    _pc = extract_first_metric(status, ("pCharge", "pCharge1"))
    pdv = parse_number(_pd[0]) if _pd else None
    pcv = parse_number(_pc[0]) if _pc else None
    bat_w_val = None
    if pdv is not None or pcv is not None:
        bat_w_val = (pdv or 0.0) - (pcv or 0.0)
        parts.append(f"bat_w={bat_w_val:g}")
    if battery_capacity_wh > 0 and soc_result is not None and bat_w_val is not None:
        soc_val = soc_result[0]
        if bat_w_val > 0:
            rt = estimate_runtime(soc_val, bat_w_val, battery_capacity_wh, bms_cutoff_soc)
            if rt is not None:
                parts.append(f"runtime_min={rt:.0f}")
        elif bat_w_val < 0:
            ct = estimate_charge_time(soc_val, abs(bat_w_val), battery_capacity_wh)
            if ct is not None:
                parts.append(f"charge_min={ct:.0f}")
    _vbat = extract_first_metric(status, ("vBat", "vBat1", "vbat"))
    if _vbat:
        n = parse_number(_vbat[0])
        if n is not None:
            parts.append(f"vbat={n:g}")
    if hours_to_sunrise is not None and hours_to_sunrise > 0:
        parts.append(f"sunrise_h={hours_to_sunrise:.2f}")
        if charge_rate_w > 0 and soc_result is not None and bat_w_val is not None and bat_w_val > 0:
            topup = estimate_topup_for_sunrise(
                soc_result[0], bat_w_val, battery_capacity_wh, bms_cutoff_soc, charge_rate_w, hours_to_sunrise
            )
            if topup is not None:
                parts.append(f"topup_sunrise_min={topup:.0f}")
    return ", ".join(parts)


def extract_status_soc(status: dict[str, Any]) -> float | None:
    soc_result = extract_soc(status)
    if not soc_result:
        return None
    soc, _ = soc_result
    return soc


def describe_status_output_source(status: dict[str, Any]) -> str:
    output_source = extract_spf_output_source(status)
    if not output_source:
        return ""
    raw, label, _ = output_source
    return f"{label} [{raw}]"


def redact(data: Any) -> Any:
    secret_words = ("password", "token", "secret", "auth", "session", "cookie")
    if isinstance(data, dict):
        redacted = {}
        for key, value in data.items():
            if any(word in str(key).lower() for word in secret_words):
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = redact(value)
        return redacted
    if isinstance(data, list):
        return [redact(item) for item in data]
    return data


def write_probe(status: dict[str, Any]) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = LOG_DIR / f"growatt-probe-{timestamp}.json"
    path.write_text(json.dumps(redact(status), indent=2, sort_keys=True), encoding="utf-8")
    return path


def render_params(template: str, device: DeviceRef, mode: str) -> dict[str, Any]:
    if not template:
        raise api_error(f"No custom params configured for {mode}. Set GROWATT_{mode.upper()}_MODE_PARAMS in .env.")
    rendered = (
        template.replace("{plant_id}", device.plant_id)
        .replace("{device_sn}", device.device_sn)
        .replace("{serial}", device.device_sn)
        .replace("{mode}", mode)
    )
    try:
        params = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise api_error(f"Invalid JSON for {mode} params: {exc}") from exc
    if not isinstance(params, dict):
        raise api_error(f"{mode} params must be a JSON object.")
    return params


def response_error_text(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    text = getattr(response, "text", "")
    if len(text) > 1000:
        text = text[:1000] + "...[truncated]"
    return text


def request_json_with_error_detail(api, method: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    url = api.get_url(path)
    try:
        if method == "post_params":
            response = api.session.post(url, params=params, timeout=35)
        elif method == "post_data":
            response = api.session.post(url, data=params, timeout=35)
        elif method == "get":
            response = api.session.get(url, params=params, timeout=35)
        else:
            raise api_error(f"Unsupported request method: {method}")
    except Exception as exc:  # noqa: BLE001 - preserve Growatt response text from request hooks
        body = response_error_text(exc)
        if body:
            raise api_error(f"Growatt request failed via {method}: {exc}; body={body}") from exc
        raise api_error(f"Growatt request failed via {method}: {exc}") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise api_error(f"Growatt returned non-JSON response via {method}: {response.text}") from exc


def send_spf5000_output_source(api, path: str, params: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    for method in ("post_params", "post_data"):
        try:
            return request_json_with_error_detail(api, method, path, params)
        except app_module().GrowattGuardError as exc:
            failures.append(str(exc))
            logging.warning("%s", exc)

    raise api_error("Growatt SPF output-source command failed. " + " | ".join(failures))


def ensure_growatt_success(result: dict[str, Any], action: str) -> None:
    if result.get("success") is False:
        raise api_error(f"Growatt {action} failed: {result}")


def set_mode(api, config: Any, device: DeviceRef, mode: str) -> dict[str, Any]:
    if mode not in {"utility", "sbu"}:
        raise api_error(f"Unsupported mode: {mode}")

    if config.mode_driver in {"spf5000", "spf"}:
        value = "2" if mode == "utility" else "0"
        params = {
            "action": "storageSPF5000Set",
            "serialNum": device.device_sn,
            "type": "storage_spf5000_ac_output_source",
            "param1": value,
            "param2": "",
            "param3": "",
            "param4": "",
        }
        path = "tcpSet.do"
        method = "post_params"
        logging.info("Prepared SPF output-source command for %s: %s", mode, params)
        if config.dry_run:
            logging.info("DRY_RUN=true, not sending SPF output-source command.")
            return {"dry_run": True, "mode": mode, "path": path, "method": method, "params": params}
        result = send_spf5000_output_source(api, path, params)
        ensure_growatt_success(result, f"{mode} mode command")
        logging.info("Growatt SPF %s mode response: %s", mode, result)
        return result

    if config.mode_driver != "custom":
        raise api_error(
            "Unsupported GROWATT_MODE_DRIVER="
            f"{config.mode_driver!r}. Supported values: 'spf5000' and 'custom'."
        )

    template = config.utility_mode_params if mode == "utility" else config.sbu_mode_params
    params = render_params(template, device, mode)

    logging.info("Prepared %s mode command: path=%s params=%s", mode, config.set_mode_path, params)
    if config.dry_run:
        logging.info("DRY_RUN=true, not sending mode command.")
        return {"dry_run": True, "mode": mode, "path": config.set_mode_path, "params": params}

    url = api.get_url(config.set_mode_path)
    method = config.set_mode_method
    if method not in {"post", "get"}:
        raise api_error("GROWATT_SET_MODE_METHOD must be 'post' or 'get'.")
    try:
        if method == "post":
            response = api.session.post(url, params=params, timeout=35)
        else:
            response = api.session.get(url, params=params, timeout=35)
    except Exception as exc:  # noqa: BLE001 - preserve Growatt response text from request hooks
        body = response_error_text(exc)
        if body:
            raise api_error(f"Growatt {mode} mode request failed: {exc}; body={body}") from exc
        raise api_error(f"Growatt {mode} mode request failed: {exc}") from exc
    try:
        result = response.json()
    except ValueError as exc:
        raise api_error(f"Growatt returned non-JSON response for {mode} mode: {response.text}") from exc
    ensure_growatt_success(result, f"{mode} mode command")
    logging.info("Growatt %s mode response: %s", mode, result)
    return result


def load_context(config: Any, max_attempts: int = 3):
    from growatt_guard.exceptions import GrowattGuardError as _GrowattGuardError

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            api, login_response = connect(config)
            plant_id = choose_plant(api, login_response, config)
            device = choose_device(api, plant_id, config)
            status = read_device_status(api, device)
            logging.info("Current status: %s", summarize_status(status))
            record_growatt_cloud_success(config)
            return api, device, status
        except _GrowattGuardError:
            raise  # auth failures, bad config, missing device — permanent, don't retry
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < max_attempts - 1:
                delay = (5.0, 10.0)[min(attempt, 1)]
                logging.warning(
                    "Growatt API call failed (attempt %d/%d): %s. Retrying in %.0fs.",
                    attempt + 1,
                    max_attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)

    raise api_error(f"Growatt API failed after {max_attempts} attempts: {last_exc}") from last_exc


SPF_EXPECTED_OUTPUT_CONFIG: dict[str, str] = {"utility": "2", "sbu": "0"}


def verify_mode_switch(
    api: Any,
    device: DeviceRef,
    mode: str,
    delay_seconds: float = 3.0,
) -> bool | None:
    """Re-read outputConfig after a mode switch to confirm the inverter responded.

    Waits delay_seconds, then reads device status and checks outputConfig.
    Returns True if confirmed, False if the wrong value is found, None if
    the status cannot be read or the mode is not recognised.
    """
    expected = SPF_EXPECTED_OUTPUT_CONFIG.get(mode)
    if expected is None:
        return None
    time.sleep(delay_seconds)
    try:
        fresh_status = read_device_status(api, device)
    except Exception:  # noqa: BLE001
        logging.warning("verify_mode_switch: could not re-read device status after %s switch.", mode)
        return None
    result = extract_spf_output_source(fresh_status)
    if result is None:
        logging.warning("verify_mode_switch: outputConfig not found after %s switch.", mode)
        return None
    raw, label, path = result
    if raw == expected:
        logging.info("Mode switch verified: outputConfig=%s (%s) from %s.", raw, label, path)
        return True
    logging.warning(
        "Mode switch NOT confirmed: expected outputConfig=%s, got %s (%s) from %s.",
        expected,
        raw,
        label,
        path,
    )
    return False
