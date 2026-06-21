from __future__ import annotations

import logging

from growatt_guard.config import Config
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.notifications import send_discord_message
from growatt_guard.state import (
    COMMAND_LOCK_FILE,
    COMMAND_LOCK_STALE_SECONDS,
    acquire_command_lock,
    clear_login_cooldown_state,
    clear_pause_state,
    command_lock_is_stale,
    format_local_time,
    login_cooldown_until,
    pause_message,
    read_command_lock_state,
    read_pause_state,
    release_command_lock,
    write_pause_state,
)


def ensure_not_paused(config: Config, command: str) -> bool:
    state = read_pause_state()
    if not state:
        return False

    message = f"Skipped `{command}` because {pause_message(state)}."
    logging.info(message)
    if config.discord_notify_skip:
        send_discord_message(config, message)
    print(message)
    return True


def run_with_command_lock(config: Config, command: str, action) -> int:
    token = acquire_command_lock(command)
    if token is None:
        state = read_command_lock_state() or {}
        locked_command = state.get("command", "another command")
        created_at = state.get("created_at", "unknown time")
        message = f"Skipped `{command}` because `{locked_command}` is already running since {created_at}."
        logging.warning(message)
        if config.discord_notify_skip:
            send_discord_message(config, message)
        print(message)
        return 0
    try:
        return action()
    finally:
        release_command_lock(token)


def command_pause(config: Config, hours: float, reason: str) -> int:
    if hours <= 0:
        raise GrowattGuardError("--hours must be greater than 0.")
    state = write_pause_state(hours, reason)
    message = f"Growatt automation paused until {format_local_time(state['paused_until_dt'])}."
    if reason:
        message += f"\nReason: {reason}"
    send_discord_message(config, message)
    print(message)
    return 0


def command_resume(config: Config) -> int:
    was_paused = read_pause_state() is not None
    clear_pause_state()
    message = "Growatt automation resumed." if was_paused else "Growatt automation was not paused."
    send_discord_message(config, message)
    print(message)
    return 0


def command_pause_status(config: Config) -> int:
    _ = config
    state = read_pause_state()
    if not state:
        print("Growatt automation is active.")
        return 0
    print(f"Growatt automation is paused: {pause_message(state)}.")
    return 0


def command_clear_stale_lock(config: Config) -> int:
    _ = config
    if not COMMAND_LOCK_FILE.exists():
        print("No lock file found.")
        return 0

    state = read_command_lock_state() or {}
    locked_command = state.get("command", "unknown command")
    created_at = state.get("created_at", "unknown time")

    if not command_lock_is_stale():
        age_limit = COMMAND_LOCK_STALE_SECONDS // 60
        print(
            f"Lock is active: `{locked_command}` running since {created_at}. "
            f"Not clearing (locks auto-clear after {age_limit} min)."
        )
        return 1

    try:
        COMMAND_LOCK_FILE.unlink()
    except OSError as exc:
        raise GrowattGuardError(f"Could not remove stale lock: {exc}") from exc

    logging.info("Cleared stale lock for %s (started %s).", locked_command, created_at)
    print(f"Cleared stale lock: `{locked_command}` started {created_at}.")
    return 0


def command_clear_login_cooldown(config: Config) -> int:
    _ = config
    until = login_cooldown_until()
    if until is None:
        print("No active Growatt login cooldown.")
        return 0
    clear_login_cooldown_state()
    logging.info("Cleared Growatt login cooldown (was active until %s).", until.isoformat())
    print(
        f"Cleared Growatt login cooldown (was active until {format_local_time(until)}). "
        "Next scheduled job will attempt a login again."
    )
    return 0
