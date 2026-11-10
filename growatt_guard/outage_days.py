from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from growatt_guard.config import Config
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.notifications import send_discord_message
from growatt_guard.schedule import outages_for_date, validate_schedule
from growatt_guard.state import (
    OUTAGE_MODES,
    outage_days_message,
    outage_mode,
    read_outage_days_state,
    write_outage_days_state,
)

WEEKEND_WEEKDAYS = (5, 6)
STATUS_DAYS = 7


def _profile_windows(schedule: dict[str, Any] | None = None) -> tuple[str | None, list[dict[str, str]]]:
    """Active profile name and its outage windows, when the schedule defines a profile."""
    schedule = schedule if schedule is not None else validate_schedule()
    profile = str(schedule.get("active_profile") or "").strip()
    windows = schedule.get("outage_windows") or []
    if not profile or not windows:
        return None, []
    return profile, list(windows)


def today_is_outage_day(
    mode: str | None = None,
    now: dt.datetime | None = None,
    schedule: dict[str, Any] | None = None,
) -> bool:
    if mode is None:
        mode = outage_mode()
    if mode not in OUTAGE_MODES:
        mode = "all"
    local_now = now or dt.datetime.now()
    if mode == "weekdays":
        return local_now.weekday() not in WEEKEND_WEEKDAYS
    if mode == "auto":
        profile, windows = _profile_windows(schedule)
        if profile is None:
            # No profile (legacy schedule) or no windows declared: `all` semantics.
            return True
        return bool(outages_for_date(windows, local_now.date()))
    return True


def outage_gate_source(
    mode: str | None = None,
    schedule: dict[str, Any] | None = None,
) -> str:
    mode = mode if mode is not None else outage_mode()
    if mode == "auto":
        profile, _ = _profile_windows(schedule)
        return f"profile:{profile}" if profile else "fallback:all"
    return f"manual:{mode}"


def outage_mode_label(
    mode: str | None = None,
    now: dt.datetime | None = None,
    schedule: dict[str, Any] | None = None,
) -> str:
    mode = mode if mode is not None else outage_mode()
    source = outage_gate_source(mode, schedule)
    today = "outage day" if today_is_outage_day(mode, now, schedule) else "no outage"
    return f"{mode} ({source}; today: {today})"


def ensure_outage_day(config: Config, command: str) -> bool:
    """Skip a grid-importing job on a day the estate has no scheduled cuts.

    Only `preserve-battery` consults this gate: returning to SBU and verifying SBU
    are safe on any day, and gating them would strand Utility across a skipped day.
    """
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


def command_outage_status(config: Config, days: int = STATUS_DAYS) -> int:
    _ = config
    mode = outage_mode()
    state = read_outage_days_state()
    source = outage_gate_source(mode)
    today = today_is_outage_day(mode)
    print(f"Outage days mode: {mode} ({source}) — {outage_days_message(state)}.")
    print(f"Today: {'outage day' if today else 'no outage day'}.")
    print("Gate: only preserve-battery is skipped on non-outage days; return-sbu and watchdog-sbu always run.")
    now = dt.datetime.now()
    print(f"Next {days} day(s):")
    for offset in range(max(1, days)):
        day = (now + dt.timedelta(days=offset)).replace(hour=12, minute=0, second=0, microsecond=0)
        resolved = today_is_outage_day(mode, now=day)
        print(f"  {day.date().isoformat()} {day.strftime('%a')}  {'outage' if resolved else 'no outage'}")
    return 0
