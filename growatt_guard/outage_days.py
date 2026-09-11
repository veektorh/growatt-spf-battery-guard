from __future__ import annotations

import datetime as dt
import logging

from growatt_guard.config import Config
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.notifications import send_discord_message
from growatt_guard.state import (
    OUTAGE_MODES,
    outage_days_message,
    outage_mode,
    read_outage_days_state,
    write_outage_days_state,
)

WEEKEND_WEEKDAYS = (5, 6)


def today_is_outage_day(mode: str | None = None, now: dt.datetime | None = None) -> bool:
    if mode is None:
        mode = outage_mode()
    if mode not in OUTAGE_MODES:
        mode = "all"
    if mode != "weekdays":
        return True
    local_now = now or dt.datetime.now()
    return local_now.weekday() not in WEEKEND_WEEKDAYS


def ensure_outage_day(config: Config, command: str) -> bool:
    if today_is_outage_day():
        return False

    message = f"Skipped `{command}` because today is not an outage day ({outage_days_message(read_outage_days_state())})."
    logging.info(message)
    if config.discord_notify_skip:
        send_discord_message(config, message)
    print(message)
    return True


def command_set_outage_days(config: Config, mode: str, reason: str = "") -> int:
    if mode not in OUTAGE_MODES:
        raise GrowattGuardError(f"Outage days mode must be one of: {', '.join(OUTAGE_MODES)}.")
    write_outage_days_state(mode, reason)
    message = f"Growatt outage days set to `{mode}`: {outage_days_message(read_outage_days_state())}."
    if reason:
        message += f"\nReason: {reason}"
    send_discord_message(config, message)
    print(message)
    return 0


def command_outage_status(config: Config) -> int:
    _ = config
    print(f"Outage days mode: {outage_mode()} — {outage_days_message(read_outage_days_state())}.")
    return 0
