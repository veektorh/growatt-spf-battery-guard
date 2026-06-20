from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
STATE_DIR = BASE_DIR / "state"
PAUSE_FILE = STATE_DIR / "automation_pause.json"
BATTERY_ALERT_FILE = STATE_DIR / "battery_alert.json"
COMMAND_LOCK_FILE = STATE_DIR / "mode_command.lock"
DASHBOARD_STALE_ALERT_FILE = STATE_DIR / "dashboard_stale_alert.json"
GROWATT_CLOUD_FAILURE_FILE = STATE_DIR / "growatt_cloud_failures.json"
COMMAND_LOCK_STALE_SECONDS = 45 * 60


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_utc_datetime(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def read_json_state(path: Path, description: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Ignoring invalid %s state: %s", description, exc)
        return None


def write_json_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def clear_state_file(path: Path) -> None:
    if path.exists():
        path.unlink()


def read_pause_state(now: dt.datetime | None = None) -> dict[str, Any] | None:
    if not PAUSE_FILE.exists():
        return None
    now = now or utc_now()
    try:
        state = json.loads(PAUSE_FILE.read_text(encoding="utf-8"))
        until = parse_utc_datetime(str(state["paused_until"]))
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        logging.warning("Ignoring invalid pause state: %s", exc)
        return None
    if until <= now:
        try:
            PAUSE_FILE.unlink()
        except OSError:
            pass
        return None
    state["paused_until_dt"] = until
    return state


def format_local_time(value: dt.datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def pause_message(state: dict[str, Any]) -> str:
    until = state["paused_until_dt"]
    reason = state.get("reason") or "no reason provided"
    return f"automation paused until {format_local_time(until)} ({reason})"


def write_pause_state(hours: float, reason: str) -> dict[str, Any]:
    if hours <= 0:
        raise ValueError("--hours must be greater than 0.")
    until = utc_now() + dt.timedelta(hours=hours)
    state = {
        "paused_until": until.isoformat(),
        "reason": reason,
        "created_at": utc_now().isoformat(),
    }
    write_json_state(PAUSE_FILE, state)
    state["paused_until_dt"] = until
    return state


def clear_pause_state() -> None:
    clear_state_file(PAUSE_FILE)


def read_command_lock_state() -> dict[str, Any] | None:
    return read_json_state(COMMAND_LOCK_FILE, "command lock")


def command_lock_is_stale() -> bool:
    if not COMMAND_LOCK_FILE.exists():
        return False
    try:
        age_seconds = dt.datetime.now().timestamp() - COMMAND_LOCK_FILE.stat().st_mtime
    except OSError:
        return False
    return age_seconds > COMMAND_LOCK_STALE_SECONDS


def acquire_command_lock(command: str) -> str | None:
    COMMAND_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}-{utc_now().timestamp()}"
    payload = {
        "token": token,
        "pid": os.getpid(),
        "command": command,
        "created_at": utc_now().isoformat(),
    }

    for _ in range(2):
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            fd = os.open(str(COMMAND_LOCK_FILE), flags)
        except FileExistsError:
            if command_lock_is_stale():
                try:
                    COMMAND_LOCK_FILE.unlink()
                except OSError:
                    pass
                continue
            return None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        return token
    return None


def release_command_lock(token: str) -> None:
    state = read_command_lock_state()
    if state and state.get("token") != token:
        return
    try:
        COMMAND_LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def read_battery_alert_state() -> dict[str, Any] | None:
    return read_json_state(BATTERY_ALERT_FILE, "battery alert")


def write_battery_alert_state(soc: float) -> None:
    state = {
        "active": True,
        "last_soc": soc,
        "last_alert_at": utc_now().isoformat(),
    }
    write_json_state(BATTERY_ALERT_FILE, state)


def clear_battery_alert_state() -> None:
    clear_state_file(BATTERY_ALERT_FILE)


def read_dashboard_stale_alert_state() -> dict[str, Any] | None:
    return read_json_state(DASHBOARD_STALE_ALERT_FILE, "dashboard stale alert")


def write_dashboard_stale_alert_state(state: dict[str, Any]) -> None:
    write_json_state(DASHBOARD_STALE_ALERT_FILE, state)


def clear_dashboard_stale_alert_state() -> None:
    clear_state_file(DASHBOARD_STALE_ALERT_FILE)


def read_growatt_cloud_failure_state() -> dict[str, Any] | None:
    return read_json_state(GROWATT_CLOUD_FAILURE_FILE, "Growatt cloud failure")


def write_growatt_cloud_failure_state(state: dict[str, Any]) -> None:
    write_json_state(GROWATT_CLOUD_FAILURE_FILE, state)


def clear_growatt_cloud_failure_state() -> None:
    clear_state_file(GROWATT_CLOUD_FAILURE_FILE)
