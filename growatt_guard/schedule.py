from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.paths import APP_HOME, DATA_HOME
from growatt_guard.state import read_schedule_profile_state


BASE_DIR = APP_HOME
SCHEDULE_FILE = BASE_DIR / "schedule.json"
SCHEDULE_OVERRIDES_FILE = DATA_HOME / "schedule_overrides.json"

SCHEDULE_COMMANDS = {
    "preserve-battery",
    "utility-check",
    "morning-check",
    "return-sbu",
    "watchdog-sbu",
    "daily-summary",
    "monthly-summary",
    "ops-review",
    "rotate-logs",
    "prune-audit",
    "health-check",
    "battery-alert",
    "weekly-summary",
    "dashboard-stale-alert",
    "pvoutput-upload",
    "observability-refresh",
    "solar-reconcile",
    "auto-topup-check",
    "topup-complete-check",
    "waste-alert-check",
    "runtime-alert",
    "waste-alert-check",
}
SCHEDULE_COMMAND_ARGS = {
    "health-check": {"--notify"},
    "ops-review": {"--notify"},
    "solar-reconcile": {"--notify"},
}
GROWATT_READ_COMMANDS = {
    "auto-topup-check",
    "battery-alert",
    "dashboard-stale-alert",
    "health-check",
    "observability-refresh",
    "pvoutput-upload",
    "runtime-alert",
    "waste-alert-check",
}
CONDITIONAL_GROWATT_READ_COMMANDS = {"topup-complete-check"}
# Jobs that each post their own Discord summary. Two of them in the same minute bury
# one report under the other, so collisions are a schedule defect, not just noise.
REPORT_COMMANDS = {"daily-summary", "weekly-summary", "monthly-summary", "ops-review"}
REPORT_COLLISION_WINDOW_DAYS = 400
MODE_CHANGING_COMMANDS = {"preserve-battery", "utility-check", "morning-check", "return-sbu", "watchdog-sbu"}
GROWATT_API_COMMANDS = GROWATT_READ_COMMANDS | MODE_CHANGING_COMMANDS
GROWATT_API_DAILY_WARNING_THRESHOLD = 200


@dataclass(frozen=True)
class HealthCheckItem:
    name: str
    status: str
    detail: str


def schedule_error(message: str) -> GrowattGuardError:
    return GrowattGuardError(message)


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

    profiles = schedule.get("profiles") or {}
    expected: list[tuple[str, str, str]] = []
    if profiles:
        for index, job in enumerate(schedule.get("shared_jobs") or [], start=1):
            expected.append(
                (
                    str(job["cron"]).strip(),
                    f"run-scheduled {schedule_job_id(job, index)}",
                    "growatt_power_guard.py " + " ".join(schedule_job_tokens(job, index)),
                )
            )
        for name, body in profiles.items():
            for index, job in enumerate(body.get("jobs", []), start=1):
                expected.append(
                    (
                        str(job["cron"]).strip(),
                        f"run-scheduled --profile {name} {schedule_job_id(job, index)}",
                        "growatt_power_guard.py " + " ".join(schedule_job_tokens(job, index)),
                    )
                )
    else:
        for index, job in enumerate(schedule["jobs"], start=1):
            expected.append(
                (
                    str(job["cron"]).strip(),
                    f"run-scheduled {schedule_job_id(job, index)}",
                    "growatt_power_guard.py " + " ".join(schedule_job_tokens(job, index)),
                )
            )

    missing: list[str] = []
    for cron, run_tokens, direct_fragment in expected:
        found = any(
            line.startswith(f"{cron} ")
            and f" {run_tokens} " in f" {line} "
            and "# growatt-power-guard" in line
            for line in cron_lines
        )
        if not found:
            found = any(
                line.startswith(f"{cron} ")
                and direct_fragment in line
                and "# growatt-power-guard" in line
                for line in cron_lines
            )
        if not found:
            missing.append(f"{cron} {run_tokens}")

    checks: list[HealthCheckItem] = []
    installed_count = sum(1 for line in cron_lines if "# growatt-power-guard" in line)
    if missing:
        checks.append(
            HealthCheckItem(
                "Cron jobs",
                "FAIL",
                (
                    f"{installed_count}/{len(expected)} growatt jobs found; "
                    f"missing: {', '.join(missing)}"
                ),
            )
        )
    else:
        detail = f"{len(expected)} scheduled jobs installed."
        if profiles:
            detail = f"{len(expected)} jobs across {len(profiles)} profile(s) installed."
        checks.append(HealthCheckItem("Cron jobs", "OK", detail))

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


@dataclass(frozen=True)
class EffectiveScheduleJob:
    job_id: str
    index: int
    original_job: dict[str, Any]
    effective_job: dict[str, Any] | None
    status: str
    note: str = ""
    fires: tuple[dt.datetime, ...] = ()

    @property
    def original_tokens(self) -> list[str]:
        return schedule_job_tokens(self.original_job, self.index)

    @property
    def effective_tokens(self) -> list[str]:
        if self.effective_job is None:
            return []
        index = 0 if self.status == "replace" else self.index
        return schedule_job_tokens(self.effective_job, index)

    @property
    def effective_command(self) -> str | None:
        return self.effective_tokens[0] if self.effective_tokens else None


def resolve_effective_schedule_job(
    job: dict[str, Any],
    index: int,
    override: dict[str, Any],
    *,
    fires: tuple[dt.datetime, ...] = (),
) -> EffectiveScheduleJob:
    job_id = schedule_job_id(job, index)
    note = str(override.get("note", "")).strip()
    skip_ids = override.get("skip", [])
    replace_map = override.get("replace", {}) if isinstance(override.get("replace", {}), dict) else {}
    if override.get("skip_all") or job_id in skip_ids:
        return EffectiveScheduleJob(job_id, index, job, None, "skip", note, fires)
    replacement = replace_map.get(job_id)
    if isinstance(replacement, dict):
        return EffectiveScheduleJob(job_id, index, job, replacement, "replace", note, fires)
    return EffectiveScheduleJob(job_id, index, job, job, "scheduled", note, fires)


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

    job_ids = schedule_job_ids(schedule)
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


PROFILE_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
OUTAGE_DAY_TYPES = ("daily", "weekdays", "weekends")


def _validate_job_list(jobs: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(jobs, list) or not jobs:
        raise schedule_error(f"{label} must contain at least one job.")

    job_ids: set[str] = set()
    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise schedule_error(f"{label} job {index} must be an object.")
        job_id = schedule_job_id(job, index)
        if job_id in job_ids:
            raise schedule_error(f"{label} job {index} has duplicate id: {job_id!r}")
        job_ids.add(job_id)
        cron = str(job.get("cron", "")).strip()
        command = str(job.get("command", "")).strip()
        if len(cron.split()) != 5:
            raise schedule_error(f"{label} job {index} has invalid cron expression: {cron!r}")
        if command not in SCHEDULE_COMMANDS:
            raise schedule_error(f"{label} job {index} has unsupported command: {command!r}")
        schedule_job_args(job, command, index)
    return jobs


def _parse_hhmm(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise schedule_error(f"{label} must be HH:MM, got {value!r}.")
    return text


def _time_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _validate_outage_windows(windows: Any, label: str) -> list[dict[str, str]]:
    if windows in (None, ""):
        return []
    if not isinstance(windows, list):
        raise schedule_error(f"{label} outages must be a list.")

    validated: list[dict[str, str]] = []
    for index, window in enumerate(windows, start=1):
        if not isinstance(window, dict):
            raise schedule_error(f"{label} outage {index} must be an object.")
        days = str(window.get("days", "daily")).strip()
        if days not in OUTAGE_DAY_TYPES:
            raise schedule_error(
                f"{label} outage {index} days must be one of: {', '.join(OUTAGE_DAY_TYPES)}."
            )
        start = _parse_hhmm(window.get("start"), f"{label} outage {index} start")
        end = _parse_hhmm(window.get("end"), f"{label} outage {index} end")
        if _time_minutes(end) <= _time_minutes(start):
            raise schedule_error(f"{label} outage {index} end must be after start.")

        for other_index, other in enumerate(validated, start=1):
            days_overlap = days == "daily" or other["days"] == "daily" or days == other["days"]
            times_overlap = _time_minutes(start) < _time_minutes(other["end"]) and _time_minutes(
                other["start"]
            ) < _time_minutes(end)
            if days_overlap and times_overlap:
                raise schedule_error(f"{label} outages {other_index} and {index} overlap.")
        validated.append({"days": days, "start": start, "end": end})
    return validated


def load_schedule_document(path: Path = SCHEDULE_FILE) -> dict[str, Any]:
    """Load and validate schedule.json, including every named profile.

    The returned document uses ``jobs`` for jobs shared by all profiles and
    ``profiles`` for the per-estate-regime job lists. A legacy file with only a
    top-level ``jobs`` list is accepted and returned with no profiles.
    """
    if not path.exists():
        raise schedule_error(f"Schedule file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise schedule_error(f"Invalid schedule JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise schedule_error("schedule.json must contain a JSON object.")

    timezone = raw.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip():
        raise schedule_error("schedule.json must contain a non-empty timezone.")

    profiles_raw = raw.get("profiles")
    if profiles_raw is None:
        jobs = _validate_job_list(raw.get("jobs"), "schedule.json")
        return {"timezone": timezone.strip(), "active_profile": None, "profiles": {}, "jobs": jobs}

    if not isinstance(profiles_raw, dict) or not profiles_raw:
        raise schedule_error("schedule.json profiles must be a non-empty object.")

    shared_jobs_raw = raw.get("jobs", [])
    shared_jobs = _validate_job_list(shared_jobs_raw, "schedule.json shared") if shared_jobs_raw else []
    shared_ids = {schedule_job_id(job, index) for index, job in enumerate(shared_jobs, start=1)}

    profiles: dict[str, dict[str, Any]] = {}
    for raw_name, body in profiles_raw.items():
        name = str(raw_name).strip()
        if not PROFILE_NAME_RE.fullmatch(name):
            raise schedule_error(f"Invalid schedule profile name: {raw_name!r}")
        if name in profiles:
            raise schedule_error(f"Duplicate schedule profile name: {name!r}")
        if not isinstance(body, dict):
            raise schedule_error(f"Schedule profile {name!r} must be an object.")

        description = body.get("description", "")
        if not isinstance(description, str):
            raise schedule_error(f"Schedule profile {name!r} description must be a string.")
        label = body.get("label", name)
        if not isinstance(label, str) or not label.strip():
            raise schedule_error(f"Schedule profile {name!r} label must be a non-empty string.")
        outages = _validate_outage_windows(body.get("outages", []), f"Schedule profile {name!r}")

        profile_jobs_raw = body.get("jobs")
        profile_jobs = (
            _validate_job_list(profile_jobs_raw, f"Schedule profile {name!r}")
            if profile_jobs_raw
            else []
        )
        if not shared_jobs and not profile_jobs:
            raise schedule_error(f"Schedule profile {name!r} has no jobs.")
        profile_ids = {
            schedule_job_id(job, index) for index, job in enumerate(profile_jobs, start=1)
        }
        duplicate_ids = sorted(shared_ids & profile_ids)
        if duplicate_ids:
            raise schedule_error(
                f"Schedule profile {name!r} reuses shared job ids: {', '.join(duplicate_ids)}"
            )
        profiles[name] = {
            "label": label.strip(),
            "description": description.strip(),
            "outages": outages,
            "jobs": profile_jobs,
        }

    active_profile = raw.get("active_profile")
    if active_profile is None:
        active_profile = next(iter(profiles))
    elif not isinstance(active_profile, str) or active_profile.strip() not in profiles:
        raise schedule_error(f"schedule.json active_profile is not a known profile: {active_profile!r}")
    else:
        active_profile = active_profile.strip()

    return {
        "timezone": timezone.strip(),
        "active_profile": active_profile,
        "profiles": profiles,
        "jobs": shared_jobs,
    }


def active_profile_name(
    document: dict[str, Any],
    requested: str | None = None,
) -> str | None:
    """Resolve the active profile: explicit request, local switch state, then schedule default."""
    profiles = document.get("profiles") or {}
    if not profiles:
        if requested is not None:
            raise schedule_error("schedule.json has no profiles; --profile is not supported.")
        return None

    if requested is not None:
        name = requested.strip()
        if name not in profiles:
            available = ", ".join(profiles)
            raise schedule_error(f"Unknown schedule profile: {requested!r}. Available: {available}.")
        return name

    state = read_schedule_profile_state()
    state_name = str((state or {}).get("name", "")).strip()
    if state_name and state_name not in profiles:
        logging.warning(
            "Ignoring switched schedule profile %r: it is not defined in schedule.json.", state_name
        )
        state_name = ""

    configured = str(document.get("active_profile") or "").strip()
    if state_name:
        return state_name
    if configured in profiles:
        return configured
    return next(iter(profiles))


def schedule_job_ids(schedule: dict[str, Any]) -> set[str]:
    """Every job id a date override may reference, across shared and profile jobs."""
    job_ids = {str(job.get("id", "")) for job in schedule.get("jobs", []) if isinstance(job, dict)}
    for body in (schedule.get("profiles") or {}).values():
        for job in body.get("jobs", []):
            if isinstance(job, dict):
                job_ids.add(str(job.get("id", "")))
    job_ids.discard("")
    return job_ids


def outages_for_date(windows: list[dict[str, str]], date: dt.date) -> list[dict[str, str]]:
    day_type = "weekends" if date.weekday() >= 5 else "weekdays"
    return [
        window
        for window in windows
        if str(window.get("days", "daily")) in ("daily", day_type)
    ]


def format_outage_windows(windows: list[dict[str, str]]) -> str:
    if not windows:
        return "none"
    grouped: dict[str, list[str]] = {}
    for window in windows:
        grouped.setdefault(str(window.get("days", "daily")), []).append(
            f"{window['start']}-{window['end']}"
        )
    return "; ".join(f"{days} {', '.join(times)}" for days, times in grouped.items())


def format_cron_summary(cron: str) -> str:
    """Return a human schedule summary for simple cron expressions."""
    text = str(cron).strip()
    parts = text.split()
    if len(parts) != 5:
        return text
    minute, hour, day, month, day_of_week = parts
    if (
        minute.startswith("*/")
        and hour == "*"
        and day == "*"
        and month == "*"
        and day_of_week == "*"
    ):
        return f"every {minute[2:]} min"
    if not (minute.isdigit() and hour.isdigit()):
        return text
    at = f"{int(hour):02d}:{int(minute):02d}"
    if day != "*" or month != "*":
        return f"{text} ({at})"
    if day_of_week == "*":
        return f"daily {at}"
    if day_of_week == "1-5":
        return f"weekdays {at}"
    if day_of_week in {"0,6", "6,0", "0,7", "7,0"}:
        return f"weekends {at}"
    return f"{text} ({at})"


def _profile_job_summaries(profile: dict[str, Any]) -> list[dict[str, str]]:
    jobs: list[dict[str, str]] = []
    for job in profile.get("jobs", []):
        command = str(job.get("command", "")).strip()
        jobs.append(
            {
                "id": str(job.get("id", "")),
                "name": str(job.get("name") or command),
                "command": command,
                "when": format_cron_summary(str(job.get("cron", ""))),
            }
        )
    return jobs


def build_profiles_payload(document: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the active and available profile metadata for CLI, Discord, and the dashboard."""
    document = document or load_schedule_document()
    profiles = document.get("profiles") or {}
    if not profiles:
        return {
            "active_profile": None,
            "label": "",
            "description": "",
            "outages": [],
            "outages_text": "none",
            "profiles": [],
        }

    active = active_profile_name(document)
    body = profiles[active]
    return {
        "active_profile": active,
        "label": body["label"],
        "description": body["description"],
        "outages": body["outages"],
        "outages_text": format_outage_windows(body["outages"]),
        "jobs": _profile_job_summaries(body),
        "profiles": [
            {
                "name": name,
                "label": profile["label"],
                "description": profile["description"],
                "outages": profile["outages"],
                "outages_text": format_outage_windows(profile["outages"]),
                "jobs": _profile_job_summaries(profile),
                "active": name == active,
            }
            for name, profile in profiles.items()
        ],
    }


def validate_schedule(path: Path = SCHEDULE_FILE, profile: str | None = None) -> dict[str, Any]:
    """Return the active profile's effective schedule.

    ``jobs`` is always the effective list (shared jobs plus the selected
    profile's jobs), so callers that only understand the legacy shape keep
    working. ``profiles`` carries per-profile metadata (including each
    profile's own jobs) for cron installation and profile switching.
    """
    document = load_schedule_document(path)
    name = active_profile_name(document, profile)
    shared_jobs = document["jobs"]
    profiles = document["profiles"]

    if name is None:
        return {
            "timezone": document["timezone"],
            "jobs": list(shared_jobs),
            "active_profile": None,
            "profile_description": "",
            "outage_windows": [],
            "profiles": {},
            "shared_jobs": list(shared_jobs),
        }

    body = profiles[name]
    return {
        "timezone": document["timezone"],
        "jobs": [*shared_jobs, *body["jobs"]],
        "active_profile": name,
        "profile_label": body["label"],
        "profile_description": body["description"],
        "outage_windows": list(body["outages"]),
        "profiles": profiles,
        "shared_jobs": list(shared_jobs),
    }


def command_validate_schedule(config: Any | None = None) -> int:
    _ = config
    schedule = validate_schedule()
    profile = schedule.get("active_profile")
    if profile:
        shared_count = len(schedule.get("shared_jobs") or [])
        print(
            f"Schedule OK: {len(schedule['jobs'])} active jobs "
            f"({shared_count} shared + {len(schedule['jobs']) - shared_count} {profile}), "
            f"{len(schedule.get('profiles') or {})} profile(s) in {schedule['timezone']}."
        )
        print(f"Active profile: {profile} — {format_outage_windows(schedule.get('outage_windows') or [])}")
    else:
        print(f"Schedule OK: {len(schedule['jobs'])} jobs in {schedule['timezone']}.")
    overrides = validate_schedule_overrides(schedule)
    if overrides.get("dates"):
        print(f"Schedule overrides OK: {len(overrides['dates'])} date override(s).")
    for item in lint_schedule(schedule):
        print(f"{item.name} {item.status}: {item.detail}")
    for name, body in (schedule.get("profiles") or {}).items():
        if name == profile:
            continue
        view = {
            "timezone": schedule["timezone"],
            "jobs": [*(schedule.get("shared_jobs") or []), *body["jobs"]],
        }
        for item in lint_schedule(view):
            if item.status != "OK":
                print(f"Profile {name} — {item.name} {item.status}: {item.detail}")
    return 0


def _cron_interval_minutes(cron: str) -> int | None:
    parts = cron.strip().split()
    if len(parts) != 5:
        return None
    minute_field = parts[0]
    if not minute_field.startswith("*/"):
        return None
    try:
        value = int(minute_field[2:])
    except ValueError:
        return None
    return value if value > 0 else None


def estimate_growatt_api_runs_per_day(
    schedule: dict[str, Any],
    *,
    today: dt.date | None = None,
    days: int = 14,
) -> tuple[int, dt.date, dict[str, int]]:
    today = today or dt.date.today()
    max_total = 0
    max_date = today
    max_by_job: dict[str, int] = {}
    jobs = schedule.get("jobs", [])
    for day_offset in range(max(1, days)):
        date = today + dt.timedelta(days=day_offset)
        start = dt.datetime.combine(date, dt.time(0, 0))
        cursor = start
        by_job: dict[str, int] = {}
        while cursor < start + dt.timedelta(days=1):
            for index, job in enumerate(jobs, start=1):
                if not isinstance(job, dict):
                    continue
                command = str(job.get("command", "")).strip()
                if command not in GROWATT_API_COMMANDS:
                    continue
                if cron_matches(str(job.get("cron", "")), cursor):
                    job_id = schedule_job_id(job, index)
                    by_job[job_id] = by_job.get(job_id, 0) + 1
            cursor += dt.timedelta(minutes=1)
        total = sum(by_job.values())
        if total > max_total:
            max_total = total
            max_date = date
            max_by_job = by_job
    return max_total, max_date, max_by_job


def _job_minutes_of_day(cron: str) -> set[int]:
    minute, hour, *_ = cron.split()
    return {
        hour_value * 60 + minute_value
        for hour_value in range(24)
        for minute_value in range(60)
        if cron_part_matches(minute_value, minute, 0, 59)
        and cron_part_matches(hour_value, hour, 0, 23)
    }


def _job_matches_date(cron: str, day: dt.date) -> bool:
    _, _, day_field, month_field, day_of_week = cron.split()
    cron_dow = (day.weekday() + 1) % 7
    return (
        cron_part_matches(day.day, day_field, 1, 31)
        and cron_part_matches(day.month, month_field, 1, 12)
        and (
            cron_part_matches(cron_dow, day_of_week, 0, 7)
            or (cron_dow == 0 and cron_part_matches(7, day_of_week, 0, 7))
        )
    )


def _first_report_collision(
    first: tuple[str, str],
    second: tuple[str, str],
    *,
    start: dt.date,
    days: int = REPORT_COLLISION_WINDOW_DAYS,
) -> tuple[dt.date, int] | None:
    first_id, first_cron = first
    second_id, second_cron = second
    shared = _job_minutes_of_day(first_cron) & _job_minutes_of_day(second_cron)
    if not shared:
        return None
    shared_minute = min(shared)
    for offset in range(days):
        day = start + dt.timedelta(days=offset)
        if _job_matches_date(first_cron, day) and _job_matches_date(second_cron, day):
            return day, shared_minute
    return None


def lint_schedule(schedule: dict[str, Any]) -> list[HealthCheckItem]:
    items: list[HealthCheckItem] = []
    jobs = schedule.get("jobs", [])
    commands = [str(job.get("command", "")).strip() for job in jobs if isinstance(job, dict)]

    if "observability-refresh" in commands and "pvoutput-upload" in commands:
        items.append(
            HealthCheckItem(
                "Schedule lint",
                "WARN",
                "observability-refresh and pvoutput-upload are both scheduled; observability-refresh already uploads PVOutput.",
            )
        )

    read_jobs_by_cron: dict[str, list[str]] = {}
    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            continue
        job_id = schedule_job_id(job, index)
        command = str(job.get("command", "")).strip()
        cron = str(job.get("cron", "")).strip()
        interval = _cron_interval_minutes(cron)
        if command in GROWATT_READ_COMMANDS | CONDITIONAL_GROWATT_READ_COMMANDS:
            read_jobs_by_cron.setdefault(cron, []).append(job_id)
            # Conditional readers (topup-complete-check) exit before touching Growatt
            # unless a hold is active, and a hold deadline has to be honoured within a
            # minute; only unconditional readers carry a standing API cost.
            if command in GROWATT_READ_COMMANDS and interval is not None and interval < 5:
                items.append(
                    HealthCheckItem(
                        "Schedule lint",
                        "WARN",
                        f"{job_id} polls Growatt every {interval} min; keep read loops at 5+ min.",
                    )
                )
        if command in MODE_CHANGING_COMMANDS and interval is not None:
            items.append(
                HealthCheckItem(
                    "Schedule lint",
                    "WARN",
                    f"{job_id} is a mode-changing interval job ({cron}); use fixed windows to avoid repeated writes.",
                )
            )

    for cron, job_ids in sorted(read_jobs_by_cron.items()):
        if len(job_ids) <= 1:
            continue
        items.append(
            HealthCheckItem(
                "Schedule lint",
                "WARN",
                f"Growatt read jobs share cron {cron!r}: {', '.join(job_ids)}; stagger or combine them to reduce bursts.",
            )
        )

    report_jobs = [
        (schedule_job_id(job, index), str(job.get("cron", "")).strip())
        for index, job in enumerate(jobs, start=1)
        if isinstance(job, dict) and str(job.get("command", "")).strip() in REPORT_COMMANDS
    ]
    today = dt.date.today()
    for position, first in enumerate(report_jobs):
        for second in report_jobs[position + 1 :]:
            collision = _first_report_collision(first, second, start=today)
            if collision is None:
                continue
            day, minute_of_day = collision
            items.append(
                HealthCheckItem(
                    "Schedule lint",
                    "WARN",
                    (
                        f"{first[0]} and {second[0]} both post a report at "
                        f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d} "
                        f"(first on {day.isoformat()}); stagger one so neither report is buried."
                    ),
                )
            )

    runs = next_scheduled_runs(schedule, now=dt.datetime.now().replace(second=0, microsecond=0), limit=64)
    previous_mode_run: tuple[dt.datetime, str] | None = None
    for run_at, job in runs:
        command = str(job.get("command", "")).strip()
        if command not in MODE_CHANGING_COMMANDS:
            continue
        job_id = str(job.get("id", command))
        if previous_mode_run is not None:
            previous_at, previous_id = previous_mode_run
            gap_min = (run_at - previous_at).total_seconds() / 60.0
            if 0 <= gap_min < 5:
                items.append(
                    HealthCheckItem(
                        "Schedule lint",
                        "WARN",
                        f"{previous_id} and {job_id} are only {gap_min:g} min apart.",
                    )
                )
                break
        previous_mode_run = (run_at, job_id)

    api_runs, api_run_date, api_runs_by_job = estimate_growatt_api_runs_per_day(schedule)
    if api_runs > GROWATT_API_DAILY_WARNING_THRESHOLD:
        top_jobs = sorted(api_runs_by_job.items(), key=lambda item: item[1], reverse=True)[:3]
        top_summary = ", ".join(f"{job_id} x{count}" for job_id, count in top_jobs)
        items.append(
            HealthCheckItem(
                "Schedule lint",
                "WARN",
                (
                    f"estimated Growatt API runs peak at {api_runs}/day on {api_run_date.isoformat()} "
                    f"(threshold {GROWATT_API_DAILY_WARNING_THRESHOLD}); busiest jobs: {top_summary}."
                ),
            )
        )

    if not items:
        items.append(
            HealthCheckItem(
                "Schedule lint",
                "OK",
                (
                    f"estimated Growatt API runs peak at {api_runs}/day; "
                    "no duplicate pollers, fast pollers, report collisions, or risky mode-changing jobs found."
                ),
            )
        )
    return items


def _job_fires_on_date(schedule: dict[str, Any], date: dt.date) -> dict[str, list[dt.datetime]]:
    job_fires: dict[str, list[dt.datetime]] = {}
    start = dt.datetime.combine(date, dt.time(0, 0))
    cursor = start
    while cursor < start + dt.timedelta(days=1):
        for job in schedule["jobs"]:
            if cron_matches(str(job["cron"]), cursor):
                job_id = str(job.get("id", ""))
                job_fires.setdefault(job_id, []).append(cursor)
        cursor += dt.timedelta(minutes=1)
    return job_fires


def effective_schedule_jobs_on_date(
    schedule: dict[str, Any],
    overrides: dict[str, Any],
    date: dt.date,
) -> list[EffectiveScheduleJob]:
    override = today_schedule_override(overrides, date)
    fires_by_job = _job_fires_on_date(schedule, date)
    effective: list[EffectiveScheduleJob] = []
    for index, job in enumerate(schedule["jobs"], start=1):
        job_id = schedule_job_id(job, index)
        fires = tuple(fires_by_job.get(job_id, ()))
        if fires:
            effective.append(resolve_effective_schedule_job(job, index, override, fires=fires))
    return effective
