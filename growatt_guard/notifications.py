from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import requests


BASE_DIR = Path(__file__).resolve().parents[1]
STATE_DIR = BASE_DIR / "state"
GROWATT_CLOUD_FAILURE_FILE = STATE_DIR / "growatt_cloud_failures.json"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def truncate_discord_message(message: str) -> str:
    if len(message) <= 1900:
        return message
    return message[:1890] + "...[truncated]"


def send_discord_message(config: Any, message: str) -> bool:
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


GROWATT_CLOUD_FAILURE_PATTERNS = (
    "growatt login failed",
    "login succeeded but no user id",
    "no growatt plants found",
    "no devices found",
    "was not found in plant",
    "could not determine plant id",
    "could not determine device serial",
    "could not find battery soc",
    "soc was not found",
    "spF output source was not found".lower(),
    "could not read current spf output source",
    "connectionerror",
    "connecttimeout",
    "readtimeout",
    "read timed out",
    "max retries exceeded",
    "name or service not known",
    "temporary failure in name resolution",
    "failed to establish a new connection",
)


def is_growatt_cloud_failure(message: str) -> bool:
    lower = message.lower()
    return any(pattern in lower for pattern in GROWATT_CLOUD_FAILURE_PATTERNS)


def read_growatt_cloud_failure_state() -> dict[str, Any] | None:
    if not GROWATT_CLOUD_FAILURE_FILE.exists():
        return None
    try:
        return json.loads(GROWATT_CLOUD_FAILURE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Ignoring invalid Growatt cloud failure state: %s", exc)
        return None


def write_growatt_cloud_failure_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    GROWATT_CLOUD_FAILURE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def clear_growatt_cloud_failure_state() -> None:
    if GROWATT_CLOUD_FAILURE_FILE.exists():
        GROWATT_CLOUD_FAILURE_FILE.unlink()


def record_growatt_cloud_failure(config: Any, command: str, message: str) -> None:
    state = read_growatt_cloud_failure_state() or {}
    count = int(state.get("count", 0)) + 1
    threshold = max(1, config.cloud_failure_alert_threshold)
    alerted = bool(state.get("alerted"))
    state.update(
        {
            "count": count,
            "alerted": alerted,
            "first_failure_at": state.get("first_failure_at") or utc_now().isoformat(),
            "last_failure_at": utc_now().isoformat(),
            "last_command": command,
            "last_message": message,
            "threshold": threshold,
        }
    )

    if count >= threshold and not alerted:
        alert = (
            "Growatt cloud appears flaky.\n"
            f"`{command}` has failed `{count}` consecutive time(s); alert threshold is `{threshold}`.\n"
            f"Latest error: {message}"
        )
        if send_discord_message(config, alert):
            state["alerted"] = True

    write_growatt_cloud_failure_state(state)


def record_growatt_cloud_success(config: Any) -> None:
    state = read_growatt_cloud_failure_state()
    if not state:
        return
    count = int(state.get("count", 0))
    was_alerted = bool(state.get("alerted"))
    clear_growatt_cloud_failure_state()
    if was_alerted and config.discord_notify_failure:
        send_discord_message(
            config,
            f"Growatt cloud recovered after `{count}` consecutive failure(s). Automation reads are working again.",
        )


def notify_failure(config: Any | None, command: str, message: str) -> None:
    if config is None or not config.discord_notify_failure or command == "test-discord":
        return
    if is_growatt_cloud_failure(message):
        record_growatt_cloud_failure(config, command, message)
        return
    send_discord_message(config, f"Growatt automation failed during `{command}`.\n{message}")

