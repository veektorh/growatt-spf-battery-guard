from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]


def _default_state_dir() -> Path:
    env_dir = os.environ.get("GROWATT_GUARD_STATE_DIR")
    if env_dir:
        return Path(env_dir)
    if "unittest" in sys.modules:
        return Path(tempfile.gettempdir()) / f"growatt_guard_test_state_{os.getpid()}"
    return BASE_DIR / "state"


def configure_state_dir(path: str | os.PathLike[str]) -> Path:
    global STATE_DIR
    global PAUSE_FILE, BATTERY_ALERT_FILE, BATTERY_ALERT_MUTED_FILE, BYPASS_ALERT_FILE
    global COMMAND_LOCK_FILE, DASHBOARD_STALE_ALERT_FILE, GROWATT_CLOUD_FAILURE_FILE
    global LOGIN_COOLDOWN_FILE, SESSION_CACHE_FILE, SESSION_REFRESH_LOCK_FILE
    global TOPUP_STATE_FILE, TOPUP_SKIP_NOTIFICATION_FILE
    global CHARGE_RATE_HISTORY_FILE, DISCHARGE_RATE_HISTORY_FILE, RUNTIME_ALERT_FILE
    global UTILITY_HOLD_FILE, WASTE_ALERT_FILE

    STATE_DIR = Path(path)
    PAUSE_FILE = STATE_DIR / "automation_pause.json"
    BATTERY_ALERT_FILE = STATE_DIR / "battery_alert.json"
    BATTERY_ALERT_MUTED_FILE = STATE_DIR / "battery_alert_muted.json"
    BYPASS_ALERT_FILE = STATE_DIR / "bypass_alert.json"
    COMMAND_LOCK_FILE = STATE_DIR / "mode_command.lock"
    DASHBOARD_STALE_ALERT_FILE = STATE_DIR / "dashboard_stale_alert.json"
    GROWATT_CLOUD_FAILURE_FILE = STATE_DIR / "growatt_cloud_failures.json"
    LOGIN_COOLDOWN_FILE = STATE_DIR / "growatt_login_cooldown.json"
    SESSION_CACHE_FILE = STATE_DIR / "growatt_session.json"
    SESSION_REFRESH_LOCK_FILE = STATE_DIR / "growatt_session_refresh.lock"
    TOPUP_STATE_FILE = STATE_DIR / "topup_active.json"
    TOPUP_SKIP_NOTIFICATION_FILE = STATE_DIR / "topup_skip_notification.json"
    CHARGE_RATE_HISTORY_FILE = STATE_DIR / "charge_rate_history.json"
    DISCHARGE_RATE_HISTORY_FILE = STATE_DIR / "discharge_rate_history.json"
    RUNTIME_ALERT_FILE = STATE_DIR / "runtime_alert.json"
    UTILITY_HOLD_FILE = STATE_DIR / "utility_hold.json"
    WASTE_ALERT_FILE = STATE_DIR / "waste_alert.json"
    return STATE_DIR


STATE_DIR = configure_state_dir(_default_state_dir())
COMMAND_LOCK_STALE_SECONDS = 45 * 60
SESSION_REFRESH_LOCK_STALE_SECONDS = 2 * 60
STATE_SCHEMA_VERSION = 1
_STATE_METADATA_KEYS = ("_schema_version", "_updated_at")


@dataclass(frozen=True)
class UtilityHold:
    ownership: str
    completion_policy: str
    max_expiry: dt.datetime
    started_at: dt.datetime
    target_soc: float | None = None
    start_soc: float | None = None
    minutes: int | None = None
    reason: str = ""
    start_load_w: float | None = None

    def to_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "ownership": self.ownership,
            "completion_policy": self.completion_policy,
            "max_expiry": self.max_expiry.isoformat(),
            "started_at": self.started_at.isoformat(),
        }
        for key, value in (
            ("target_soc", self.target_soc),
            ("start_soc", self.start_soc),
            ("minutes", self.minutes),
            ("reason", self.reason or None),
            ("start_load_w", self.start_load_w),
        ):
            if value is not None:
                state[key] = value
        return state


    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "UtilityHold":
        def optional_float(key: str) -> float | None:
            value = state.get(key)
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        max_expiry = parse_utc_datetime(str(state["max_expiry"]))
        started_at = parse_utc_datetime(str(state.get("started_at") or state["max_expiry"]))
        minutes_value = optional_float("minutes")
        return cls(
            ownership=str(state.get("ownership") or "owned"),
            completion_policy=str(state.get("completion_policy") or "soc"),
            max_expiry=max_expiry,
            started_at=started_at,
            target_soc=optional_float("target_soc"),
            start_soc=optional_float("start_soc"),
            minutes=int(minutes_value) if minutes_value is not None else None,
            reason=str(state.get("reason") or ""),
            start_load_w=optional_float("start_load_w"),
        )


    @classmethod
    def from_legacy_topup(cls, state: dict[str, Any]) -> "UtilityHold":
        normalized = dict(state)
        normalized["ownership"] = "owned"
        normalized["completion_policy"] = "time"
        normalized["max_expiry"] = state["paused_until"]
        return cls.from_state(normalized)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_utc_datetime(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _strip_state_metadata(state: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if key not in _STATE_METADATA_KEYS}


def _with_state_metadata(state: dict[str, Any]) -> dict[str, Any]:
    payload = dict(state)
    payload["_schema_version"] = STATE_SCHEMA_VERSION
    payload["_updated_at"] = utc_now().isoformat()
    return payload


def read_json_state(path: Path, description: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Ignoring invalid %s state: %s", description, exc)
        return None
    if not isinstance(state, dict):
        logging.warning("Ignoring invalid %s state: expected a JSON object", description)
        return None
    return _strip_state_metadata(state)


def write_json_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_with_state_metadata(state), indent=2, sort_keys=True)
    # Atomic write: a crash mid-write must never leave a half-written state
    # file (these control inverter/pause behaviour for scheduled jobs).
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear_state_file(path: Path) -> None:
    if path.exists():
        path.unlink()


def read_pause_state(now: dt.datetime | None = None) -> dict[str, Any] | None:
    if not PAUSE_FILE.exists():
        return None
    now = now or utc_now()
    state = read_json_state(PAUSE_FILE, "pause")
    if state is None:
        return None
    try:
        until = parse_utc_datetime(str(state["paused_until"]))
    except (KeyError, ValueError) as exc:
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
    payload = _with_state_metadata({
        "token": token,
        "pid": os.getpid(),
        "command": command,
        "created_at": utc_now().isoformat(),
    })

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


def write_battery_alert_state(soc: float, utility_unavailable: bool = False) -> None:
    state = {
        "active": True,
        "last_soc": soc,
        "last_alert_at": utc_now().isoformat(),
    }
    if utility_unavailable:
        state["utility_unavailable"] = True
    write_json_state(BATTERY_ALERT_FILE, state)


def clear_battery_alert_state() -> None:
    clear_state_file(BATTERY_ALERT_FILE)


def read_bypass_alert_state() -> dict[str, Any] | None:
    return read_json_state(BYPASS_ALERT_FILE, "bypass alert")


def write_bypass_alert_state(soc: float, reason: str, sent_count: int = 1) -> None:
    write_json_state(BYPASS_ALERT_FILE, {
        "active": True,
        "last_soc": soc,
        "reason": reason,
        "sent_count": sent_count,
        "last_alert_at": utc_now().isoformat(),
    })


def clear_bypass_alert_state() -> None:
    clear_state_file(BYPASS_ALERT_FILE)


def battery_alert_is_muted() -> bool:
    return bool(read_json_state(BATTERY_ALERT_MUTED_FILE, "battery alert mute"))


def write_battery_alert_mute() -> None:
    write_json_state(BATTERY_ALERT_MUTED_FILE, {"muted": True, "muted_at": utc_now().isoformat()})


def clear_battery_alert_mute() -> None:
    clear_state_file(BATTERY_ALERT_MUTED_FILE)


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


def read_login_cooldown_state() -> dict[str, Any] | None:
    return read_json_state(LOGIN_COOLDOWN_FILE, "Growatt login cooldown")


def write_login_cooldown_state(retry_after: dt.datetime, reason: str) -> None:
    write_json_state(
        LOGIN_COOLDOWN_FILE,
        {
            "retry_after": retry_after.isoformat(),
            "reason": reason,
            "created_at": utc_now().isoformat(),
        },
    )


def clear_login_cooldown_state() -> None:
    clear_state_file(LOGIN_COOLDOWN_FILE)


def login_cooldown_until(now: dt.datetime | None = None) -> dt.datetime | None:
    """Return the time login attempts may resume, or None if no cooldown is active.

    Auto-clears the cooldown file once it has expired so a stale file never
    blocks logins forever.
    """
    state = read_login_cooldown_state()
    if not state:
        return None
    now = now or utc_now()
    try:
        retry_after = parse_utc_datetime(str(state["retry_after"]))
    except (KeyError, ValueError):
        clear_login_cooldown_state()
        return None
    if retry_after <= now:
        clear_login_cooldown_state()
        return None
    return retry_after



def try_acquire_session_refresh_lock(owner: str = "", stale_seconds: int = SESSION_REFRESH_LOCK_STALE_SECONDS) -> bool:
    SESSION_REFRESH_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    payload = json.dumps(
        _with_state_metadata({
            "created_at": now.isoformat(),
            "owner": owner,
            "pid": os.getpid(),
        }),
        indent=2,
        sort_keys=True,
    )
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(SESSION_REFRESH_LOCK_FILE, flags)
    except FileExistsError:
        state = read_json_state(SESSION_REFRESH_LOCK_FILE, "Growatt session refresh lock")
        created_at = None
        if state:
            try:
                created_at = parse_utc_datetime(str(state.get("created_at", "")))
            except ValueError:
                created_at = None
        if created_at is not None and (now - created_at).total_seconds() < stale_seconds:
            return False
        try:
            SESSION_REFRESH_LOCK_FILE.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logging.warning("Could not clear stale Growatt session refresh lock: %s", exc)
            return False
        try:
            fd = os.open(SESSION_REFRESH_LOCK_FILE, flags)
        except FileExistsError:
            return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return True


def release_session_refresh_lock() -> None:
    clear_state_file(SESSION_REFRESH_LOCK_FILE)


def read_session_cache() -> dict[str, Any] | None:
    return read_json_state(SESSION_CACHE_FILE, "Growatt session cache")


def write_session_cache(cookies: dict[str, Any], login_response: dict[str, Any]) -> None:
    write_json_state(
        SESSION_CACHE_FILE,
        {
            "cookies": cookies,
            "login_response": login_response,
            "saved_at": utc_now().isoformat(),
        },
    )


def clear_session_cache() -> None:
    clear_state_file(SESSION_CACHE_FILE)


def session_cache_age_minutes(state: dict[str, Any], now: dt.datetime | None = None) -> float | None:
    """Age of a session cache entry in minutes, or None if it can't be parsed."""
    saved_at = state.get("saved_at")
    if not saved_at:
        return None
    try:
        saved = parse_utc_datetime(str(saved_at))
    except ValueError:
        return None
    now = now or utc_now()
    return (now - saved).total_seconds() / 60.0


def _read_legacy_topup_state() -> dict[str, Any] | None:
    return read_json_state(TOPUP_STATE_FILE, "topup")


def read_topup_state() -> dict[str, Any] | None:
    legacy = _read_legacy_topup_state()
    if legacy is not None:
        return legacy
    hold = read_utility_hold_state()
    if hold is None or hold.get("minutes") is None:
        return None
    return {
        "started_at": hold.get("started_at"),
        "minutes": hold.get("minutes"),
        "paused_until": hold.get("max_expiry"),
        "reason": hold.get("reason", ""),
        "start_soc": hold.get("start_soc"),
        "start_load_w": hold.get("start_load_w"),
    }


def write_topup_state(
    minutes: int,
    reason: str,
    paused_until: dt.datetime,
    start_soc: float | None = None,
    start_load_w: float | None = None,
) -> None:
    state: dict[str, Any] = {
        "started_at": utc_now().isoformat(),
        "minutes": minutes,
        "paused_until": paused_until.isoformat(),
        "reason": reason,
    }
    if start_soc is not None:
        state["start_soc"] = start_soc
    if start_load_w is not None:
        state["start_load_w"] = start_load_w
    write_json_state(TOPUP_STATE_FILE, state)


def clear_topup_state() -> None:
    clear_state_file(TOPUP_STATE_FILE)


def read_topup_skip_notification_state() -> dict[str, Any] | None:
    return read_json_state(TOPUP_SKIP_NOTIFICATION_FILE, "topup skip notification")


def topup_skip_notification_due(
    key: str,
    cooldown_minutes: float = 180.0,
    now: dt.datetime | None = None,
) -> bool:
    state = read_topup_skip_notification_state()
    if not state:
        return True
    if state.get("key") != key:
        return True
    try:
        last_notified_at = parse_utc_datetime(str(state["last_notified_at"]))
    except (KeyError, ValueError):
        return True
    now = now or utc_now()
    return (now - last_notified_at).total_seconds() >= cooldown_minutes * 60


def write_topup_skip_notification_state(key: str, detail: dict[str, Any] | None = None) -> None:
    state: dict[str, Any] = {
        "key": key,
        "last_notified_at": utc_now().isoformat(),
    }
    if detail:
        state["detail"] = detail
    write_json_state(TOPUP_SKIP_NOTIFICATION_FILE, state)


def topup_is_active(now: dt.datetime | None = None) -> bool:
    now = now or utc_now()
    state = read_topup_state()
    if state is not None:
        try:
            paused_until = parse_utc_datetime(str(state["paused_until"]))
            if now < paused_until:
                return True
        except (KeyError, ValueError):
            pass
    # Also active if a utility hold (owned/adopted) with unexpired max_expiry exists.
    # Checked via utility_hold_is_active() but we avoid a forward reference by inlining.
    hold = read_utility_hold_state()
    if hold is not None:
        max_expiry_str = hold.get("max_expiry")
        if not max_expiry_str:
            return True
        try:
            max_expiry = parse_utc_datetime(str(max_expiry_str))
            if now < max_expiry:
                return True
        except ValueError:
            pass
    return False


_CHARGE_RATE_MAX_READINGS = 10


def read_charge_rate_history() -> list[dict]:
    state = read_json_state(CHARGE_RATE_HISTORY_FILE, "charge rate history")
    if not state:
        return []
    readings = state.get("readings")
    if not isinstance(readings, list):
        return []
    return readings


def append_charge_rate_reading(rate_w: float) -> list[dict]:
    readings = read_charge_rate_history()
    readings.append({"rate_w": round(rate_w), "recorded_at": utc_now().isoformat()})
    readings = readings[-_CHARGE_RATE_MAX_READINGS:]
    write_json_state(CHARGE_RATE_HISTORY_FILE, {"readings": readings})
    return readings


_DISCHARGE_RATE_MAX_READINGS = 10


def read_discharge_rate_history() -> list[dict]:
    state = read_json_state(DISCHARGE_RATE_HISTORY_FILE, "discharge rate history")
    if not state:
        return []
    readings = state.get("readings")
    if not isinstance(readings, list):
        return []
    return readings


def append_discharge_rate_reading(rate_w: float) -> list[dict]:
    readings = read_discharge_rate_history()
    readings.append({"rate_w": round(rate_w), "recorded_at": utc_now().isoformat()})
    readings = readings[-_DISCHARGE_RATE_MAX_READINGS:]
    write_json_state(DISCHARGE_RATE_HISTORY_FILE, {"readings": readings})
    return readings


def read_runtime_alert_state() -> dict[str, Any] | None:
    return read_json_state(RUNTIME_ALERT_FILE, "runtime alert")


def write_runtime_alert_state(runtime_min: float) -> None:
    write_json_state(RUNTIME_ALERT_FILE, {
        "active": True,
        "runtime_min": runtime_min,
        "last_alert_at": utc_now().isoformat(),
    })


def clear_runtime_alert_state() -> None:
    clear_state_file(RUNTIME_ALERT_FILE)


# ---------------------------------------------------------------------------
# Utility hold — tracks owned/adopted Utility state for auto-return logic
# ---------------------------------------------------------------------------

def read_utility_hold() -> UtilityHold | None:
    state = read_json_state(UTILITY_HOLD_FILE, "utility hold")
    try:
        if state is not None:
            return UtilityHold.from_state(state)
        legacy = _read_legacy_topup_state()
        return UtilityHold.from_legacy_topup(legacy) if legacy is not None else None
    except (KeyError, TypeError, ValueError) as exc:
        logging.warning("Ignoring invalid utility hold state: %s", exc)
        return None


def read_utility_hold_state() -> dict[str, Any] | None:
    hold = read_utility_hold()
    return hold.to_state() if hold is not None else None


def write_utility_hold_state(
    ownership: str,
    target_soc: float | None,
    max_expiry: dt.datetime,
    start_soc: float | None = None,
    *,
    completion_policy: str = "soc",
    minutes: int | None = None,
    reason: str = "",
    start_load_w: float | None = None,
) -> None:
    if completion_policy not in {"soc", "time"}:
        raise ValueError(f"Unsupported utility hold completion policy: {completion_policy}")
    hold = UtilityHold(
        ownership=ownership,
        completion_policy=completion_policy,
        max_expiry=max_expiry,
        started_at=utc_now(),
        target_soc=target_soc,
        start_soc=start_soc,
        minutes=minutes,
        reason=reason,
        start_load_w=start_load_w,
    )
    write_json_state(UTILITY_HOLD_FILE, hold.to_state())


def clear_utility_hold_state() -> None:
    clear_state_file(UTILITY_HOLD_FILE)


def utility_hold_ownership(now: dt.datetime | None = None) -> str | None:
    """Return "owned", "adopted", or None if no active hold."""
    state = read_utility_hold_state()
    if state is None:
        return None
    max_expiry_str = state.get("max_expiry")
    if max_expiry_str:
        try:
            max_expiry = parse_utc_datetime(str(max_expiry_str))
        except ValueError:
            return None
        if (now or utc_now()) >= max_expiry:
            return None
    return str(state.get("ownership")) if state.get("ownership") else None


def utility_hold_is_active(now: dt.datetime | None = None) -> bool:
    state = read_utility_hold_state()
    if state is None:
        return False
    max_expiry_str = state.get("max_expiry")
    if not max_expiry_str:
        return True  # no expiry set means indefinitely active
    try:
        max_expiry = parse_utc_datetime(str(max_expiry_str))
        return (now or utc_now()) < max_expiry
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Waste alert — throttles avoidable-waste notifications + snooze
# ---------------------------------------------------------------------------

def read_waste_alert_state() -> dict[str, Any] | None:
    return read_json_state(WASTE_ALERT_FILE, "waste alert")


def write_waste_alert_last_sent() -> None:
    state = read_waste_alert_state() or {}
    state["last_sent_at"] = utc_now().isoformat()
    write_json_state(WASTE_ALERT_FILE, state)


def write_waste_alert_snooze(until: dt.datetime) -> None:
    state = read_waste_alert_state() or {}
    state["snooze_until"] = until.isoformat()
    write_json_state(WASTE_ALERT_FILE, state)


def waste_alert_is_snoozed(now: dt.datetime | None = None) -> bool:
    state = read_waste_alert_state()
    if not state:
        return False
    snooze_str = state.get("snooze_until")
    if not snooze_str:
        return False
    try:
        snooze_until = parse_utc_datetime(str(snooze_str))
    except ValueError:
        return False
    return (now or utc_now()) < snooze_until


def waste_alert_is_due(cooldown_minutes: float = 30.0, now: dt.datetime | None = None) -> bool:
    """Return True if enough time has passed since the last waste alert."""
    state = read_waste_alert_state()
    if not state:
        return True
    last_sent_str = state.get("last_sent_at")
    if not last_sent_str:
        return True
    try:
        last_sent = parse_utc_datetime(str(last_sent_str))
    except ValueError:
        return True
    return ((now or utc_now()) - last_sent).total_seconds() >= cooldown_minutes * 60


def clear_waste_alert_state() -> None:
    clear_state_file(WASTE_ALERT_FILE)


def waste_alert_is_muted() -> bool:
    state = read_waste_alert_state()
    return bool(state and state.get("muted"))


def write_waste_alert_mute() -> None:
    state = read_waste_alert_state() or {}
    state["muted"] = True
    state["muted_at"] = utc_now().isoformat()
    write_json_state(WASTE_ALERT_FILE, state)


def clear_waste_alert_mute() -> None:
    state = read_waste_alert_state() or {}
    state.pop("muted", None)
    state.pop("muted_at", None)
    if state:
        write_json_state(WASTE_ALERT_FILE, state)
    else:
        clear_state_file(WASTE_ALERT_FILE)


