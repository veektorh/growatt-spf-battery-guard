from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - handled at runtime for friendlier output
    load_dotenv = None

try:
    import growattServer
except ImportError:  # pragma: no cover - handled at runtime for friendlier output
    growattServer = None


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "growatt_power_guard.log"

SOC_KEYS = (
    "SOC",
    "soc",
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


class GrowattGuardError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    server_url: str
    plant_id: str | None
    device_sn: str | None
    low_battery_soc: float
    dry_run: bool
    mode_driver: str
    set_mode_path: str
    set_mode_method: str
    utility_mode_params: str
    sbu_mode_params: str
    discord_webhook_url: str
    discord_notify_success: bool
    discord_notify_skip: bool
    discord_notify_failure: bool


@dataclass(frozen=True)
class DeviceRef:
    plant_id: str
    device_sn: str
    device_type: str
    raw: dict[str, Any]


def str_to_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def load_config() -> Config:
    if load_dotenv is not None:
        load_dotenv(BASE_DIR / ".env")

    username = env("GROWATT_USERNAME")
    password = env("GROWATT_PASSWORD")
    if not username or not password:
        raise GrowattGuardError(
            "Missing GROWATT_USERNAME or GROWATT_PASSWORD. Copy .env.example to .env and fill them in."
        )

    mode_driver = env("GROWATT_MODE_DRIVER", "spf5000").lower()
    utility_mode_params = env("GROWATT_UTILITY_MODE_PARAMS")
    sbu_mode_params = env("GROWATT_SBU_MODE_PARAMS")
    if mode_driver == "custom" and not utility_mode_params and not sbu_mode_params:
        mode_driver = "spf5000"

    return Config(
        username=username,
        password=password,
        server_url=env("GROWATT_SERVER_URL", "https://openapi.growatt.com/"),
        plant_id=env("GROWATT_PLANT_ID") or None,
        device_sn=env("GROWATT_DEVICE_SN") or None,
        low_battery_soc=float(env("LOW_BATTERY_SOC", "45")),
        dry_run=str_to_bool(env("DRY_RUN"), default=True),
        mode_driver=mode_driver,
        set_mode_path=env("GROWATT_SET_MODE_PATH", "tcpSet.do"),
        set_mode_method=env("GROWATT_SET_MODE_METHOD", "post").lower(),
        utility_mode_params=utility_mode_params,
        sbu_mode_params=sbu_mode_params,
        discord_webhook_url=env("DISCORD_WEBHOOK_URL"),
        discord_notify_success=str_to_bool(env("DISCORD_NOTIFY_SUCCESS"), default=True),
        discord_notify_skip=str_to_bool(env("DISCORD_NOTIFY_SKIP"), default=False),
        discord_notify_failure=str_to_bool(env("DISCORD_NOTIFY_FAILURE"), default=True),
    )


def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    console_handler.setLevel(level)
    root.addHandler(console_handler)


def truncate_discord_message(message: str) -> str:
    if len(message) <= 1900:
        return message
    return message[:1890] + "...[truncated]"


def send_discord_message(config: Config, message: str) -> bool:
    if not config.discord_webhook_url:
        return False

    payload = {
        "username": "Growatt Guard",
        "content": truncate_discord_message(message),
    }
    headers = {
        "User-Agent": "growatt-spf-battery-guard/1.0",
    }

    try:
        response = requests.post(config.discord_webhook_url, json=payload, headers=headers, timeout=10)
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        body = f": {response.text[:500]}" if response is not None and response.text else ""
        logging.warning("Discord notification failed: %s%s", exc, body)
        return False
    if response.status_code >= 300:
        logging.warning("Discord webhook returned HTTP %s: %s", response.status_code, response.text[:500])
        return False
    return True


def notify_failure(config: Config | None, command: str, message: str) -> None:
    if config is None or not config.discord_notify_failure or command == "test-discord":
        return
    send_discord_message(config, f"Growatt automation failed during `{command}`.\n{message}")


def require_dependencies() -> None:
    missing = []
    if load_dotenv is None:
        missing.append("python-dotenv")
    if growattServer is None:
        missing.append("growattServer")
    if missing:
        raise GrowattGuardError(
            "Missing dependencies: "
            + ", ".join(missing)
            + ". Install them with: python -m pip install -r requirements.txt"
        )


def connect(config: Config):
    require_dependencies()
    api = growattServer.GrowattApi(add_random_user_id=True, agent_identifier=config.username)
    api.server_url = config.server_url

    logging.info("Logging into Growatt server %s", config.server_url)
    login_response = api.login(config.username, config.password)
    if not isinstance(login_response, dict) or not login_response.get("success"):
        raise GrowattGuardError(f"Growatt login failed: {login_response}")
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


def choose_plant(api, login_response: dict[str, Any], config: Config) -> str:
    if config.plant_id:
        return config.plant_id

    user = login_response.get("user", {})
    user_id = login_response.get("userId") or user.get("id")
    if not user_id:
        raise GrowattGuardError("Login succeeded but no user id was returned by Growatt.")

    plants = normalize_list_response(api.plant_list(user_id))
    if not plants:
        plants = normalize_list_response(login_response)
    if not plants:
        raise GrowattGuardError("No Growatt plants found for this account.")

    plant = plants[0]
    plant_id = get_key(plant, "plantId", "id")
    if not plant_id:
        raise GrowattGuardError(f"Could not determine plant id from: {plant}")
    logging.info("Using plant %s (%s)", plant_id, get_key(plant, "plantName", "name") or "unnamed")
    return str(plant_id)


def normalize_device(device: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(device)
    normalized["deviceSn"] = str(get_key(device, "deviceSn", "device_sn", "sn", "serialNum") or "")
    normalized["deviceType"] = str(get_key(device, "deviceType", "type", "device_type") or "").lower()
    return normalized


def choose_device(api, plant_id: str, config: Config) -> DeviceRef:
    devices = [normalize_device(device) for device in normalize_list_response(api.device_list(plant_id))]
    if not devices:
        raise GrowattGuardError(f"No devices found for plant {plant_id}.")

    if config.device_sn:
        for device in devices:
            if device["deviceSn"] == config.device_sn:
                return DeviceRef(plant_id, device["deviceSn"], device["deviceType"], device)
        raise GrowattGuardError(f"Device {config.device_sn} was not found in plant {plant_id}.")

    for wanted_type in DEVICE_TYPE_PRIORITY:
        for device in devices:
            if device["deviceType"] == wanted_type and device["deviceSn"]:
                logging.info("Using %s device %s", device["deviceType"], device["deviceSn"])
                return DeviceRef(plant_id, device["deviceSn"], device["deviceType"], device)

    first = devices[0]
    if not first["deviceSn"]:
        raise GrowattGuardError(f"Could not determine device serial from: {first}")
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
            if parsed is not None and 0 <= parsed <= 100:
                return parsed, path
    return None


def extract_spf_output_source(data: dict[str, Any]) -> tuple[str, str, str] | None:
    for path, value in deep_values(data):
        if path.split(".")[-1] == "outputConfig":
            raw = str(value)
            return raw, SPF_OUTPUT_SOURCE.get(raw, f"Unknown ({raw})"), path
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


def summarize_status(status: dict[str, Any]) -> str:
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
    return ", ".join(parts)


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
        raise GrowattGuardError(
            f"No custom params configured for {mode}. Set GROWATT_{mode.upper()}_MODE_PARAMS in .env."
        )
    rendered = (
        template.replace("{plant_id}", device.plant_id)
        .replace("{device_sn}", device.device_sn)
        .replace("{serial}", device.device_sn)
        .replace("{mode}", mode)
    )
    try:
        params = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise GrowattGuardError(f"Invalid JSON for {mode} params: {exc}") from exc
    if not isinstance(params, dict):
        raise GrowattGuardError(f"{mode} params must be a JSON object.")
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
            raise GrowattGuardError(f"Unsupported request method: {method}")
    except Exception as exc:  # noqa: BLE001 - preserve Growatt response text from request hooks
        body = response_error_text(exc)
        if body:
            raise GrowattGuardError(f"Growatt request failed via {method}: {exc}; body={body}") from exc
        raise GrowattGuardError(f"Growatt request failed via {method}: {exc}") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise GrowattGuardError(f"Growatt returned non-JSON response via {method}: {response.text}") from exc


def send_spf5000_output_source(api, path: str, params: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    for method in ("post_params", "post_data"):
        try:
            return request_json_with_error_detail(api, method, path, params)
        except GrowattGuardError as exc:
            failures.append(str(exc))
            logging.warning("%s", exc)

    raise GrowattGuardError("Growatt SPF output-source command failed. " + " | ".join(failures))


def ensure_growatt_success(result: dict[str, Any], action: str) -> None:
    if result.get("success") is False:
        raise GrowattGuardError(f"Growatt {action} failed: {result}")


def set_mode(api, config: Config, device: DeviceRef, mode: str) -> dict[str, Any]:
    if mode not in {"utility", "sbu"}:
        raise GrowattGuardError(f"Unsupported mode: {mode}")

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
        raise GrowattGuardError(
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
    if method == "post":
        response = api.session.post(url, params=params)
    elif method == "get":
        response = api.session.get(url, params=params)
    else:
        raise GrowattGuardError("GROWATT_SET_MODE_METHOD must be 'post' or 'get'.")
    result = response.json()
    ensure_growatt_success(result, f"{mode} mode command")
    logging.info("Growatt %s mode response: %s", mode, result)
    return result


def load_context(config: Config):
    api, login_response = connect(config)
    plant_id = choose_plant(api, login_response, config)
    device = choose_device(api, plant_id, config)
    status = read_device_status(api, device)
    logging.info("Current status: %s", summarize_status(status))
    return api, device, status


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
    api, device, status = load_context(config)
    soc_result = extract_soc(status)
    if not soc_result:
        raise GrowattGuardError("Could not find battery SOC in Growatt response. Run the probe command.")

    soc, path = soc_result
    if soc < config.low_battery_soc:
        logging.info("Battery SOC %.1f%% from %s is below %.1f%%; switching to Utility.", soc, path, config.low_battery_soc)
        result = set_mode(api, config, device, "utility")
        if config.discord_notify_success and not config.dry_run:
            send_discord_message(
                config,
                (
                    "Growatt preserve-battery action completed.\n"
                    f"SOC `{soc:g}%` is below threshold `{config.low_battery_soc:g}%`; "
                    "switched to `Utility first`."
                ),
            )
        print(f"SOC {soc:g}% < {config.low_battery_soc:g}%; Utility command result: {result}")
    else:
        logging.info("Battery SOC %.1f%% is not below %.1f%%; leaving SBU as-is.", soc, config.low_battery_soc)
        if config.discord_notify_skip:
            send_discord_message(
                config,
                (
                    "Growatt preserve-battery check skipped.\n"
                    f"SOC `{soc:g}%` is at or above threshold `{config.low_battery_soc:g}%`; no switch needed."
                ),
            )
        print(f"SOC {soc:g}% >= {config.low_battery_soc:g}%; no switch needed.")
    return 0


def command_utility_check(config: Config) -> int:
    return command_preserve_battery(config)


def command_morning_check(config: Config) -> int:
    return command_preserve_battery(config)


def command_return_sbu(config: Config) -> int:
    api, device, _ = load_context(config)
    result = set_mode(api, config, device, "sbu")
    if config.discord_notify_success and not config.dry_run:
        send_discord_message(config, "Growatt return-sbu action completed.\nSwitched to `SBU priority`.")
    print(f"SBU command result: {result}")
    return 0


def command_test_discord(config: Config) -> int:
    if not config.discord_webhook_url:
        raise GrowattGuardError("DISCORD_WEBHOOK_URL is not configured in .env.")
    ok = send_discord_message(config, "Growatt Guard Discord test message.")
    if not ok:
        raise GrowattGuardError("Discord test message failed. Check the webhook URL and network access.")
    print("Discord test message sent.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Growatt SPF battery-preservation automation.")
    parser.add_argument("--verbose", action="store_true", help="Log extra details.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Log in, select plant/device, and print battery SOC.")
    subparsers.add_parser("probe", help="Write redacted raw Growatt responses to logs/ for setup.")
    subparsers.add_parser("preserve-battery", help="Switch to Utility if battery SOC is below LOW_BATTERY_SOC.")
    subparsers.add_parser("utility-check", help="Alias for preserve-battery.")
    subparsers.add_parser("morning-check", help="Alias for preserve-battery.")
    subparsers.add_parser("return-sbu", help="Switch back to SBU.")
    subparsers.add_parser("test-discord", help="Send a test Discord webhook message.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    config: Config | None = None

    try:
        config = load_config()
        logging.info("Command=%s dry_run=%s low_soc=%s", args.command, config.dry_run, config.low_battery_soc)
        if args.command == "status":
            return command_status(config)
        if args.command == "probe":
            return command_probe(config)
        if args.command == "preserve-battery":
            return command_preserve_battery(config)
        if args.command == "utility-check":
            return command_utility_check(config)
        if args.command == "morning-check":
            return command_morning_check(config)
        if args.command == "return-sbu":
            return command_return_sbu(config)
        if args.command == "test-discord":
            return command_test_discord(config)
        parser.error(f"Unknown command: {args.command}")
        return 2
    except GrowattGuardError as exc:
        logging.error("%s", exc)
        notify_failure(config, args.command, str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - logs traceback for unattended scheduler runs
        logging.exception("Unhandled error")
        notify_failure(config, args.command, str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
