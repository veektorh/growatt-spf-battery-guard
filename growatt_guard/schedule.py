from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
SCHEDULE_FILE = BASE_DIR / "schedule.json"
SCHEDULE_OVERRIDES_FILE = BASE_DIR / "schedule_overrides.json"

SCHEDULE_COMMANDS = {
    "preserve-battery",
    "utility-check",
    "morning-check",
    "return-sbu",
    "watchdog-sbu",
    "daily-summary",
    "monthly-summary",
    "rotate-logs",
    "health-check",
    "battery-alert",
    "weekly-summary",
    "dashboard-stale-alert",
    "pvoutput-upload",
}
SCHEDULE_COMMAND_ARGS = {
    "health-check": {"--notify"},
}


@dataclass(frozen=True)
class HealthCheckItem:
    name: str
    status: str
    detail: str


def app_module() -> Any:
    module = sys.modules.get("growatt_power_guard")
    if module is not None and hasattr(module, "GrowattGuardError"):
        return module

    main_module = sys.modules.get("__main__")
    if main_module is not None and hasattr(main_module, "GrowattGuardError"):
        return main_module

    import growatt_power_guard

    return growatt_power_guard


def schedule_error(message: str) -> Exception:
    return app_module().GrowattGuardError(message)


def cron_part_matches(value: int, field: str, minimum: int, maximum: int) -> bool:
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            return True
        if part.startswith("*/"):
            step = int(part[2:])
            return step > 0 and value % step == 0
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start <= value <= end:
                return True
            continue
        try:
            wanted = int(part)
        except ValueError:
            continue
        if minimum <= wanted <= maximum and value == wanted:
            return True
    return False


def cron_matches(cron: str, when: dt.datetime) -> bool:
    minute, hour, day, month, day_of_week = cron.split()
    cron_dow = (when.weekday() + 1) % 7
    return (
        cron_part_matches(when.minute, minute, 0, 59)
        and cron_part_matches(when.hour, hour, 0, 23)
        and cron_part_matches(when.day, day, 1, 31)
        and cron_part_matches(when.month, month, 1, 12)
        and (cron_part_matches(cron_dow, day_of_week, 0, 7) or (cron_dow == 0 and cron_part_matches(7, day_of_week, 0, 7)))
    )


def next_scheduled_runs(
    schedule: dict[str, Any],
    *,
    now: dt.datetime | None = None,
    limit: int = 8,
) -> list[tuple[dt.datetime, dict[str, Any]]]:
    now = now or dt.datetime.now()
    cursor = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    end = cursor + dt.timedelta(days=14)
    matches: list[tuple[dt.datetime, dict[str, Any]]] = []
    while cursor <= end and len(matches) < limit:
        for job in schedule["jobs"]:
            if cron_matches(str(job["cron"]), cursor):
                matches.append((cursor, job))
                if len(matches) >= limit:
                    break
        cursor += dt.timedelta(minutes=1)
    return matches


def check_cron_schedule(schedule: dict[str, Any]) -> list[HealthCheckItem]:
    if os.name == "nt":
        return [
            HealthCheckItem(
                "Cron",
                "WARN",
                "cron check skipped on Windows; verify Task Scheduler locally or run this on the VPS.",
            )
        ]

    try:
        completed = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return [HealthCheckItem("Cron", "WARN", "crontab command not found; cron check skipped.")]
    except subprocess.TimeoutExpired:
        return [HealthCheckItem("Cron", "FAIL", "crontab -l timed out after 10 seconds.")]

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "no crontab installed").strip()
        return [HealthCheckItem("Cron", "FAIL", f"crontab -l failed: {message}")]

    cron_text = completed.stdout
    cron_lines = [line.strip() for line in cron_text.splitlines()]
    expected_jobs = schedule["jobs"]
    missing: list[str] = []
    for index, job in enumerate(expected_jobs, start=1):
        cron = str(job["cron"]).strip()
        job_id = schedule_job_id(job, index)
        tokens = schedule_job_tokens(job, index)
        wrapper_fragment = f"growatt_power_guard.py run-scheduled {job_id}"
        direct_fragment = "growatt_power_guard.py " + " ".join(tokens)
        found = any(
            line.startswith(f"{cron} ")
            and (wrapper_fragment in line or direct_fragment in line)
            and "# growatt-power-guard" in line
            for line in cron_lines
        )
        if not found:
            missing.append(f"{cron} run-scheduled {job_id}")

    checks: list[HealthCheckItem] = []
    installed_count = sum(1 for line in cron_lines if "# growatt-power-guard" in line)
    if missing:
        checks.append(
            HealthCheckItem(
                "Cron jobs",
                "FAIL",
                (
                    f"{installed_count}/{len(expected_jobs)} growatt jobs found; "
                    f"missing: {', '.join(missing)}"
                ),
            )
        )
    else:
        checks.append(HealthCheckItem("Cron jobs", "OK", f"{len(expected_jobs)} scheduled jobs installed."))

    timezone = str(schedule.get("timezone", "")).strip()
    if timezone and f"CRON_TZ={timezone}" not in cron_text:
        checks.append(HealthCheckItem("Cron timezone", "WARN", f"CRON_TZ={timezone} not found in crontab."))
    elif timezone:
        checks.append(HealthCheckItem("Cron timezone", "OK", f"CRON_TZ={timezone} is installed."))

    return checks


def schedule_job_id(job: dict[str, Any], index: int) -> str:
    job_id = str(job.get("id", "")).strip()
    if not job_id:
        raise schedule_error(f"Schedule job {index} must contain a non-empty id.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
        raise schedule_error(f"Schedule job {index} has invalid id: {job_id!r}")
    return job_id


def schedule_job_args(job: dict[str, Any], command: str, index: int) -> list[str]:
    raw_args = job.get("args", [])
    if raw_args in (None, ""):
        return []
    if not isinstance(raw_args, list):
        raise schedule_error(f"Schedule job {index} args must be a list of strings.")

    args: list[str] = []
    for arg_index, raw_arg in enumerate(raw_args, start=1):
        if not isinstance(raw_arg, str) or not raw_arg.strip():
            raise schedule_error(f"Schedule job {index} arg {arg_index} must be a non-empty string.")
        arg = raw_arg.strip()
        if "\n" in arg or "\r" in arg:
            raise schedule_error(f"Schedule job {index} arg {arg_index} cannot contain newlines.")
        args.append(arg)

    allowed_args = SCHEDULE_COMMAND_ARGS.get(command, set())
    if args and not allowed_args:
        raise schedule_error(f"Schedule job {index} command {command!r} does not support args.")
    unsupported = [arg for arg in args if arg not in allowed_args]
    if unsupported:
        raise schedule_error(f"Schedule job {index} has unsupported args for {command!r}: {unsupported}")
    return args


def schedule_job_tokens(job: dict[str, Any], index: int = 0) -> list[str]:
    command = str(job.get("command", "")).strip()
    return [command, *schedule_job_args(job, command, index)]


def validate_schedule_overrides(schedule: dict[str, Any], path: Path = SCHEDULE_OVERRIDES_FILE) -> dict[str, Any]:
    if not path.exists():
        return {"dates": {}}
    try:
        overrides = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise schedule_error(f"Invalid schedule overrides JSON: {exc}") from exc
    if not isinstance(overrides, dict):
        raise schedule_error("schedule_overrides.json must contain a JSON object.")

    dates = overrides.get("dates", {})
    if not isinstance(dates, dict):
        raise schedule_error("schedule_overrides.json dates must be an object.")

    job_ids = {schedule_job_id(job, index) for index, job in enumerate(schedule["jobs"], start=1)}
    for date_key, override in dates.items():
        try:
            dt.date.fromisoformat(str(date_key))
        except ValueError as exc:
            raise schedule_error(f"Invalid override date: {date_key!r}") from exc
        if not isinstance(override, dict):
            raise schedule_error(f"Override for {date_key} must be an object.")

        skip = override.get("skip", [])
        if skip in (None, ""):
            skip = []
        if not isinstance(skip, list) or not all(isinstance(item, str) and item in job_ids for item in skip):
            raise schedule_error(f"Override skip list for {date_key} must contain known schedule job ids.")

        skip_all = override.get("skip_all", False)
        if not isinstance(skip_all, bool):
            raise schedule_error(f"Override skip_all for {date_key} must be true or false.")

        replace = override.get("replace", {})
        if replace in (None, ""):
            replace = {}
        if not isinstance(replace, dict):
            raise schedule_error(f"Override replace for {date_key} must be an object.")
        for job_id, replacement in replace.items():
            if job_id not in job_ids:
                raise schedule_error(f"Override replace for {date_key} references unknown job id {job_id!r}.")
            if not isinstance(replacement, dict):
                raise schedule_error(f"Override replacement for {date_key}/{job_id} must be an object.")
            command = str(replacement.get("command", "")).strip()
            if command not in SCHEDULE_COMMANDS:
                raise schedule_error(
                    f"Override replacement for {date_key}/{job_id} has unsupported command: {command!r}"
                )
            schedule_job_args(replacement, command, 0)

    return {"dates": dates}


def find_schedule_job(schedule: dict[str, Any], job_id: str) -> tuple[dict[str, Any], int]:
    for index, job in enumerate(schedule["jobs"], start=1):
        if schedule_job_id(job, index) == job_id:
            return job, index
    raise schedule_error(f"Schedule job id not found: {job_id}")


def today_schedule_override(overrides: dict[str, Any], today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    value = overrides.get("dates", {}).get(today.isoformat(), {})
    return value if isinstance(value, dict) else {}


def validate_schedule(path: Path = SCHEDULE_FILE) -> dict[str, Any]:
    if not path.exists():
        raise schedule_error(f"Schedule file not found: {path}")
    try:
        schedule = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise schedule_error(f"Invalid schedule JSON: {exc}") from exc

    timezone = schedule.get("timezone")
    jobs = schedule.get("jobs")
    if not isinstance(timezone, str) or not timezone.strip():
        raise schedule_error("schedule.json must contain a non-empty timezone.")
    if not isinstance(jobs, list) or not jobs:
        raise schedule_error("schedule.json must contain at least one job.")

    job_ids: set[str] = set()
    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise schedule_error(f"Schedule job {index} must be an object.")
        job_id = schedule_job_id(job, index)
        if job_id in job_ids:
            raise schedule_error(f"Schedule job {index} has duplicate id: {job_id!r}")
        job_ids.add(job_id)
        cron = str(job.get("cron", "")).strip()
        command = str(job.get("command", "")).strip()
        if len(cron.split()) != 5:
            raise schedule_error(f"Schedule job {index} has invalid cron expression: {cron!r}")
        if command not in SCHEDULE_COMMANDS:
            raise schedule_error(f"Schedule job {index} has unsupported command: {command!r}")
        schedule_job_args(job, command, index)
    return schedule


def command_validate_schedule(config: Any | None = None) -> int:
    _ = config
    schedule = validate_schedule()
    print(f"Schedule OK: {len(schedule['jobs'])} jobs in {schedule['timezone']}.")
    overrides = validate_schedule_overrides(schedule)
    if overrides.get("dates"):
        print(f"Schedule overrides OK: {len(overrides['dates'])} date override(s).")
    return 0


def _cron_interval_label(cron: str) -> str | None:
    """Return 'every N min' if the cron fires on a repeating sub-hourly interval, else None."""
    parts = cron.strip().split()
    if len(parts) != 5:
        return None
    minute_field, hour_field = parts[0], parts[1]
    if minute_field.startswith("*/") and hour_field == "*":
        try:
            return f"every {int(minute_field[2:])} min"
        except ValueError:
            return None
    return None


def command_schedule_preview(config: Any, days: int = 7, today: dt.date | None = None) -> int:
    _ = config
    schedule = validate_schedule()
    overrides = validate_schedule_overrides(schedule)

    today = today or dt.date.today()
    timezone = schedule.get("timezone", "")
    print(f"Schedule preview — {days} day(s) from {today} [{timezone}]")

    for day_offset in range(days):
        date = today + dt.timedelta(days=day_offset)
        day_override = overrides.get("dates", {}).get(date.isoformat(), {})
        skip_all = bool(day_override.get("skip_all", False))
        skip_ids = set(day_override.get("skip", []))
        replace_map = day_override.get("replace", {}) if isinstance(day_override.get("replace"), dict) else {}
        note = str(day_override.get("note", "")).strip()

        # Collect firing times per job across this calendar day
        job_fires: dict[str, list[dt.datetime]] = {}
        start = dt.datetime.combine(date, dt.time(0, 0))
        cursor = start
        while cursor < start + dt.timedelta(days=1):
            for job in schedule["jobs"]:
                if cron_matches(str(job["cron"]), cursor):
                    job_id = job.get("id", "")
                    job_fires.setdefault(job_id, []).append(cursor)
            cursor += dt.timedelta(minutes=1)

        jobs_on_day = [job for job in schedule["jobs"] if job.get("id", "") in job_fires]
        if not jobs_on_day:
            continue

        header = f"\n{date.strftime('%a %Y-%m-%d')}"
        if skip_all:
            header += "  [skip-all]"
        if note:
            header += f"  — {note}"
        print(header)

        for job in jobs_on_day:
            job_id = job.get("id", "")
            fires = job_fires[job_id]
            command_str = " ".join(schedule_job_tokens(job, 0))

            interval_label = _cron_interval_label(str(job["cron"]))
            if interval_label:
                time_str = interval_label
                count_suffix = f"  x{len(fires)}/day"
            else:
                time_str = fires[0].strftime("%H:%M")
                count_suffix = ""

            if skip_all or job_id in skip_ids:
                status_suffix = "  [SKIP]"
            elif job_id in replace_map:
                repl_str = " ".join(schedule_job_tokens(replace_map[job_id], 0))
                status_suffix = f"  [-> {repl_str}]"
            else:
                status_suffix = ""

            print(f"  {time_str:<16}  {command_str:<32}  ({job_id}){count_suffix}{status_suffix}")

    return 0


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
