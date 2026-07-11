from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from growatt_guard.schedule import (
    MODE_CHANGING_COMMANDS,
    effective_schedule_jobs_on_date,
    today_schedule_override,
    validate_schedule,
    validate_schedule_overrides,
)

def _ical_escape(value: Any) -> str:
    text = str(value)
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ical_datetime(value: dt.datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S")


def _fold_ical_line(line: str) -> list[str]:
    # RFC 5545 line folding is byte-based; this ASCII-safe approximation keeps
    # generated files compatible with common calendar clients.
    if len(line) <= 75:
        return [line]
    lines: list[str] = []
    current = line
    while len(current) > 75:
        lines.append(current[:75])
        current = " " + current[75:]
    lines.append(current)
    return lines


def _schedule_calendar_events(
    schedule: dict[str, Any],
    overrides: dict[str, Any],
    days: int,
    today: dt.date,
    include_all: bool = False,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for day_offset in range(days):
        date = today + dt.timedelta(days=day_offset)
        for effective in effective_schedule_jobs_on_date(schedule, overrides, date):
            command = effective.effective_command
            if effective.status == "skip" or command is None:
                continue
            if not include_all and command not in MODE_CHANGING_COMMANDS:
                continue
            command_str = " ".join(effective.effective_tokens)
            for fire_at in effective.fires:
                events.append({
                    "job_id": effective.job_id,
                    "command": command,
                    "command_str": command_str,
                    "status": effective.status,
                    "start": fire_at,
                    "end": fire_at + dt.timedelta(minutes=5),
                    "note": effective.note,
                })
    events.sort(key=lambda item: (item["start"], item["job_id"]))
    return events


def build_schedule_calendar_ics(
    schedule: dict[str, Any],
    overrides: dict[str, Any],
    days: int,
    today: dt.date,
    include_all: bool = False,
    generated_at: dt.datetime | None = None,
) -> str:
    timezone = str(schedule.get("timezone", "")).strip() or "UTC"
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=dt.timezone.utc)
    dtstamp = generated_at.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    events = _schedule_calendar_events(schedule, overrides, days, today, include_all=include_all)

    raw_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Growatt Guard//Schedule Export//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ical_escape('Growatt Guard schedule')}",
        f"X-WR-TIMEZONE:{_ical_escape(timezone)}",
    ]
    for event in events:
        start = event["start"]
        end = event["end"]
        job_id = str(event["job_id"])
        command_str = str(event["command_str"])
        status = str(event["status"])
        note = str(event.get("note") or "")
        description_parts = [f"job_id={job_id}", f"command={command_str}", f"status={status}"]
        if note:
            description_parts.append(f"note={note}")
        raw_lines.extend([
            "BEGIN:VEVENT",
            f"UID:growatt-{start.strftime('%Y%m%dT%H%M')}-{_ical_escape(job_id)}@growatt-guard",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;TZID={_ical_escape(timezone)}:{_ical_datetime(start)}",
            f"DTEND;TZID={_ical_escape(timezone)}:{_ical_datetime(end)}",
            f"SUMMARY:{_ical_escape('Growatt: ' + command_str)}",
            f"DESCRIPTION:{_ical_escape('; '.join(description_parts))}",
            "END:VEVENT",
        ])
    raw_lines.append("END:VCALENDAR")

    folded: list[str] = []
    for line in raw_lines:
        folded.extend(_fold_ical_line(line))
    return "\r\n".join(folded) + "\r\n"


def command_schedule_calendar(
    config: Any,
    days: int = 14,
    output: str = "",
    include_all: bool = False,
    today: dt.date | None = None,
) -> int:
    _ = config
    schedule = validate_schedule()
    overrides = validate_schedule_overrides(schedule)
    today = today or dt.date.today()
    content = build_schedule_calendar_ics(schedule, overrides, days, today, include_all=include_all)
    if output:
        Path(output).write_text(content, encoding="utf-8", newline="")
        print(f"Wrote schedule calendar to {output}")
    else:
        print(content, end="")
    return 0


def _cron_interval_label(cron: str) -> str | None:
    """Return an interval label for repeating sub-hourly cron entries."""
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


def build_schedule_preview_payload(
    schedule: dict[str, Any],
    overrides: dict[str, Any],
    days: int,
    today: dt.date,
) -> dict[str, Any]:
    dates: list[dict[str, Any]] = []
    for day_offset in range(days):
        date = today + dt.timedelta(days=day_offset)
        override = today_schedule_override(overrides, date)
        jobs: list[dict[str, Any]] = []
        for effective in effective_schedule_jobs_on_date(schedule, overrides, date):
            interval_label = _cron_interval_label(str(effective.original_job["cron"]))
            replacement = (
                " ".join(effective.effective_tokens)
                if effective.status == "replace"
                else ""
            )
            jobs.append({
                "time": interval_label or effective.fires[0].strftime("%H:%M"),
                "job_id": effective.job_id,
                "command": " ".join(effective.original_tokens),
                "status": effective.status,
                "count": len(effective.fires) if interval_label else 1,
                "replacement": replacement,
            })
        if jobs:
            dates.append({
                "date": date.isoformat(),
                "weekday": date.strftime("%a"),
                "skip_all": bool(override.get("skip_all", False)),
                "note": str(override.get("note", "")).strip(),
                "jobs": jobs,
            })
    return {
        "schema_version": 1,
        "timezone": schedule.get("timezone", ""),
        "start_date": today.isoformat(),
        "days": days,
        "dates": dates,
    }


def command_schedule_preview(
    config: Any,
    days: int = 7,
    today: dt.date | None = None,
    json_output: bool = False,
) -> int:
    _ = config
    schedule = validate_schedule()
    overrides = validate_schedule_overrides(schedule)
    today = today or dt.date.today()
    payload = build_schedule_preview_payload(schedule, overrides, days, today)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Schedule preview — {days} day(s) from {today} [{payload['timezone']}]")
    for day in payload["dates"]:
        date = dt.date.fromisoformat(str(day["date"]))
        header = f"\n{date.strftime('%a %Y-%m-%d')}"
        if day["skip_all"]:
            header += "  [skip-all]"
        if day["note"]:
            header += f"  — {day['note']}"
        print(header)
        for job in day["jobs"]:
            count_suffix = f"  x{job['count']}/day" if job["count"] > 1 else ""
            if job["status"] == "skip":
                status_suffix = "  [SKIP]"
            elif job["status"] == "replace":
                status_suffix = f"  [-> {job['replacement']}]"
            else:
                status_suffix = ""
            print(
                f"  {job['time']:<16}  {job['command']:<32}  "
                f"({job['job_id']}){count_suffix}{status_suffix}"
            )
    return 0
