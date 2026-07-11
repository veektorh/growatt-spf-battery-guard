from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from growatt_guard.schedule import (
    BASE_DIR,
    schedule_error,
    schedule_job_id,
    schedule_job_tokens,
    validate_schedule,
    validate_schedule_overrides,
)

SCHEDULE_OVERRIDES_FILE = BASE_DIR / "schedule_overrides.json"

def _load_overrides_raw() -> dict[str, Any]:
    if not SCHEDULE_OVERRIDES_FILE.exists():
        return {"dates": {}}
    try:
        data = json.loads(SCHEDULE_OVERRIDES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"dates": {}}
    if not isinstance(data, dict) or not isinstance(data.get("dates"), dict):
        return {"dates": {}}
    return data


def _save_overrides(overrides: dict[str, Any], schedule: dict[str, Any]) -> None:
    tmp = SCHEDULE_OVERRIDES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(overrides, indent=2, sort_keys=True), encoding="utf-8")
    try:
        validate_schedule_overrides(schedule, tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(SCHEDULE_OVERRIDES_FILE)


def _parse_override_date(date_str: str) -> str:
    try:
        dt.date.fromisoformat(date_str)
    except ValueError:
        raise schedule_error(f"Invalid date: {date_str!r}. Use YYYY-MM-DD format.")
    return date_str


def _override_list(date_filter: str) -> int:
    overrides = _load_overrides_raw()
    dates = overrides.get("dates", {})
    if date_filter:
        _parse_override_date(date_filter)
        dates = {k: v for k, v in dates.items() if k == date_filter}

    if not dates:
        print(f"No overrides for {date_filter}." if date_filter else "No schedule overrides configured.")
        return 0

    for date_key in sorted(dates):
        override = dates[date_key]
        note = str(override.get("note", "")).strip()
        header = date_key + (f"  — {note}" if note else "")
        print(header)
        if override.get("skip_all"):
            print("  skip-all")
        for job_id in override.get("skip", []):
            print(f"  skip: {job_id}")
        for job_id, replacement in (override.get("replace") or {}).items():
            repl_str = " ".join(schedule_job_tokens(replacement, 0))
            print(f"  replace: {job_id} -> {repl_str}")
    return 0


def _override_add_skip(schedule: dict[str, Any], date_str: str, job_id: str, note: str) -> int:
    date_str = _parse_override_date(date_str)
    overrides = _load_overrides_raw()
    entry = overrides["dates"].setdefault(date_str, {})
    skip_list: list[str] = entry.setdefault("skip", [])
    if job_id in skip_list:
        print(f"Job {job_id!r} is already in the skip list for {date_str}.")
        return 0
    skip_list.append(job_id)
    if note and not entry.get("note"):
        entry["note"] = note
    _save_overrides(overrides, schedule)
    print(f"Added skip: {job_id!r} on {date_str}.")
    return 0


def _override_add_skip_all(schedule: dict[str, Any], date_str: str, note: str) -> int:
    date_str = _parse_override_date(date_str)
    overrides = _load_overrides_raw()
    entry = overrides["dates"].setdefault(date_str, {})
    entry["skip_all"] = True
    if note and not entry.get("note"):
        entry["note"] = note
    _save_overrides(overrides, schedule)
    print(f"Added skip-all on {date_str}.")
    return 0


def _override_add_replace(
    schedule: dict[str, Any],
    date_str: str,
    job_id: str,
    replacement_command: str,
    replacement_args: list[str],
    note: str,
) -> int:
    date_str = _parse_override_date(date_str)
    overrides = _load_overrides_raw()
    entry = overrides["dates"].setdefault(date_str, {})
    replace_map: dict[str, Any] = entry.setdefault("replace", {})
    replacement: dict[str, Any] = {"command": replacement_command}
    if replacement_args:
        replacement["args"] = list(replacement_args)
    replace_map[job_id] = replacement
    if note and not entry.get("note"):
        entry["note"] = note
    _save_overrides(overrides, schedule)
    repl_str = " ".join([replacement_command] + list(replacement_args))
    print(f"Added replace: {job_id!r} -> {repl_str!r} on {date_str}.")
    return 0


def _override_remove(schedule: dict[str, Any], date_str: str, job_id: str) -> int:
    date_str = _parse_override_date(date_str)
    overrides = _load_overrides_raw()
    dates = overrides.get("dates", {})

    if date_str not in dates:
        print(f"No overrides found for {date_str}.")
        return 0

    if not job_id:
        del dates[date_str]
        _save_overrides(overrides, schedule)
        print(f"Removed all overrides for {date_str}.")
        return 0

    entry = dates[date_str]
    removed = False
    skip_list = entry.get("skip", [])
    if job_id in skip_list:
        skip_list.remove(job_id)
        removed = True
    replace_map = entry.get("replace") or {}
    if job_id in replace_map:
        del replace_map[job_id]
        removed = True

    if not removed:
        print(f"Job {job_id!r} not found in overrides for {date_str}.")
        return 0

    if not skip_list and not replace_map and not entry.get("skip_all") and not entry.get("note"):
        del dates[date_str]

    _save_overrides(overrides, schedule)
    print(f"Removed {job_id!r} from overrides for {date_str}.")
    return 0


_MODE_CHANGING_COMMANDS = {"preserve-battery", "utility-check", "morning-check", "return-sbu", "watchdog-sbu"}

BUILTIN_OUTAGE_PROFILES: dict[str, str] = {
    "skip-all": "Skip all scheduled automation jobs.",
    "maintenance": "Alias for skip-all — use during planned maintenance windows.",
    "health-only": "Replace mode-changing jobs with health-check; monitoring still runs.",
}


def _outage_apply_profile(
    profile_name: str,
    dates: list[str],
    note: str,
) -> int:
    if profile_name not in BUILTIN_OUTAGE_PROFILES:
        names = ", ".join(BUILTIN_OUTAGE_PROFILES)
        raise schedule_error(f"Unknown profile: {profile_name!r}. Available: {names}.")

    for date_str in dates:
        _parse_override_date(date_str)

    schedule = validate_schedule()
    overrides = _load_overrides_raw()

    for date_str in dates:
        entry = overrides["dates"].setdefault(date_str, {})
        if note and not entry.get("note"):
            entry["note"] = note

        if profile_name in ("skip-all", "maintenance"):
            entry["skip_all"] = True
        elif profile_name == "health-only":
            replace_map: dict[str, Any] = entry.setdefault("replace", {})
            for index, job in enumerate(schedule["jobs"], start=1):
                job_id = schedule_job_id(job, index)
                if str(job.get("command", "")).strip() in _MODE_CHANGING_COMMANDS:
                    replace_map[job_id] = {"command": "health-check", "args": ["--notify"]}

    _save_overrides(overrides, schedule)
    date_list = ", ".join(dates)
    print(f"Applied profile {profile_name!r} to: {date_list}.")
    return 0


def command_outage_profile(config: Any, args: Any) -> int:
    subcommand = getattr(args, "outage_subcommand", None)

    if subcommand == "list":
        print("Available outage profiles:")
        for name, description in BUILTIN_OUTAGE_PROFILES.items():
            print(f"  {name:<16}  {description}")
        return 0

    if subcommand == "apply":
        return _outage_apply_profile(
            args.profile_name,
            list(args.dates),
            getattr(args, "note", "") or "",
        )

    raise schedule_error(f"Unknown outage-profile subcommand: {subcommand!r}")


def command_schedule_override(config: Any, args: Any) -> int:
    subcommand = getattr(args, "override_subcommand", None)
    if not subcommand:
        raise schedule_error("No schedule-override subcommand specified.")

    if subcommand == "list":
        return _override_list(getattr(args, "date", "") or "")

    schedule = validate_schedule()

    if subcommand == "add-skip":
        return _override_add_skip(schedule, args.date, args.job_id, getattr(args, "note", "") or "")
    if subcommand == "add-skip-all":
        return _override_add_skip_all(schedule, args.date, getattr(args, "note", "") or "")
    if subcommand == "add-replace":
        return _override_add_replace(
            schedule,
            args.date,
            args.job_id,
            args.replacement_command,
            getattr(args, "replacement_args", []) or [],
            getattr(args, "note", "") or "",
        )
    if subcommand == "remove":
        return _override_remove(schedule, args.date, getattr(args, "job_id", "") or "")

    raise schedule_error(f"Unknown schedule-override subcommand: {subcommand!r}")
