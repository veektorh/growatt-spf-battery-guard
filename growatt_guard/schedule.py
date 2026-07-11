from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from growatt_guard.exceptions import GrowattGuardError


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
    "ops-review",
    "rotate-logs",
    "prune-audit",
    "health-check",
    "battery-alert",
    "weekly-summary",
    "dashboard-stale-alert",
    "pvoutput-upload",
    "observability-refresh",
    "auto-topup-check",
    "topup-complete-check",
    "waste-alert-check",
    "runtime-alert",
    "waste-alert-check",
}
SCHEDULE_COMMAND_ARGS = {
    "health-check": {"--notify"},
    "ops-review": {"--notify"},
}
GROWATT_READ_COMMANDS = {
    "auto-topup-check",
    "battery-alert",
    "dashboard-stale-alert",
    "health-check",
    "observability-refresh",
    "pvoutput-upload",
    "runtime-alert",
    "topup-complete-check",
    "waste-alert-check",
}
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
    for item in lint_schedule(schedule):
        print(f"{item.name} {item.status}: {item.detail}")
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
        if command in GROWATT_READ_COMMANDS:
            read_jobs_by_cron.setdefault(cron, []).append(job_id)
            if interval is not None and interval < 5:
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
                    "no duplicate pollers, fast pollers, or risky mode-changing jobs found."
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
