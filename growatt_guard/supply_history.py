"""Observed estate supply, read from the grid voltage in each dashboard snapshot.

The schedule's outage windows say when the estate *should* cut supply. This
module reports when it actually did: a snapshot whose grid voltage is at or
above ``SUPPLY_ON_MIN_V`` counts as supply on, anything lower as supply off.
Snapshots are minutes apart, so each edge is reported as the pair of readings
that brackets it (last reading on the old side, first on the new side) rather
than as an invented instant.

Read-only: it uses the bounded snapshot history the dashboard already writes,
adds no Growatt API calls, and changes no inverter mode.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from growatt_guard.config import Config
from growatt_guard.dashboard_metrics import read_dashboard_metrics_history
from growatt_guard.load_learning import resolve_timezone
from growatt_guard.schedule import outages_for_date, validate_schedule

# The SPF reads 0 V on its AC input during a cut and about 215-235 V on supply.
# Anything from a deep brownout upwards still counts as supply on.
SUPPLY_ON_MIN_V = 100.0
DEFAULT_SUPPLY_HISTORY_DAYS = 7


def supply_on(row: dict[str, Any]) -> bool | None:
    """Supply state for one snapshot, or None when it carries no grid voltage."""
    value = row.get("grid_v")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) >= SUPPLY_ON_MIN_V


def _local_timestamp(row: dict[str, Any], tz: dt.tzinfo | None) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(row.get("timestamp")))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz) if tz is not None else parsed.astimezone()
    return parsed.astimezone(tz) if tz is not None else parsed.astimezone()


def _soc(row: dict[str, Any]) -> float | None:
    value = row.get("soc")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _minutes(start: dt.datetime | None, end: dt.datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() / 60.0, 1)


def observed_cuts(rows: list[dict[str, Any]], tz: dt.tzinfo | None = None) -> list[dict[str, Any]]:
    """Every run of supply-off snapshots, bracketed by the readings around it.

    ``start_after`` is the last supply-on reading before the cut (None when the
    history starts inside it) and ``start_by`` the first supply-off reading;
    ``end_after`` is the last supply-off reading and ``end_by`` the first
    supply-on reading after it (None while the cut is still running).
    """
    samples: list[tuple[dt.datetime, bool, float | None]] = []
    for row in rows:
        state = supply_on(row)
        timestamp = _local_timestamp(row, tz)
        if state is None or timestamp is None:
            continue
        samples.append((timestamp, state, _soc(row)))
    samples.sort(key=lambda item: item[0])

    cuts: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    previous: tuple[dt.datetime, bool, float | None] | None = None
    for timestamp, state, soc in samples:
        if not state:
            if current is None:
                on_before = previous is not None and previous[1]
                current = {
                    "start_after": previous[0] if on_before and previous else None,
                    "start_by": timestamp,
                    "soc_before": previous[2] if on_before and previous else None,
                    "soc_at_start": soc,
                    "samples": 0,
                }
            current["end_after"] = timestamp
            current["samples"] += 1
        elif current is not None:
            current["end_by"] = timestamp
            cuts.append(current)
            current = None
        previous = (timestamp, state, soc)
    if current is not None:
        current["end_by"] = None
        cuts.append(current)
    return cuts


def _window_bounds(day: dt.date, window: dict[str, str], tz: dt.tzinfo | None) -> tuple[dt.datetime, dt.datetime]:
    def at(text: str) -> dt.datetime:
        hour, minute = (int(part) for part in text.split(":", 1))
        naive = dt.datetime.combine(day, dt.time(hour, minute))
        return naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()

    return at(window["start"]), at(window["end"])


def _overlaps(cut: dict[str, Any], start: dt.datetime, end: dt.datetime) -> bool:
    """Whether any reading that was actually off falls inside the window.

    The bracketing on readings are not used: a gap between readings would
    otherwise pull an evening cut into the afternoon window before it.
    """
    return cut["start_by"] < end and cut["end_after"] >= start


def build_supply_report(
    rows: list[dict[str, Any]],
    outage_windows: list[dict[str, str]],
    *,
    now: dt.datetime,
    tz: dt.tzinfo | None = None,
    days: int = DEFAULT_SUPPLY_HISTORY_DAYS,
) -> dict[str, Any]:
    """Observed cuts set against the scheduled outage windows, oldest first."""
    local_now = now.astimezone(tz) if tz is not None else now.astimezone()
    first_day = local_now.date() - dt.timedelta(days=max(days, 1) - 1)
    observed = [
        row for row in rows
        if supply_on(row) is not None
        and (timestamp := _local_timestamp(row, tz)) is not None
        and timestamp.date() >= first_day
    ]
    observed_times = sorted(t for row in observed if (t := _local_timestamp(row, tz)) is not None)
    cuts = observed_cuts(observed, tz)
    matched: set[int] = set()
    events: list[dict[str, Any]] = []

    if observed_times:
        day = max(first_day, observed_times[0].date())
        while day <= local_now.date():
            for window in outages_for_date(outage_windows, day):
                start, end = _window_bounds(day, window, tz)
                if start > local_now:
                    continue
                label = f"{window['start']}-{window['end']}"
                hits = [index for index, cut in enumerate(cuts) if _overlaps(cut, start, end)]
                if hits:
                    for index in hits:
                        if index not in matched:
                            matched.add(index)
                            events.append(_cut_event(cuts[index], scheduled=label, scheduled_start=start))
                    continue
                inside = [t for t in observed_times if start <= t < min(end, local_now)]
                events.append({
                    "date": day.isoformat(),
                    "kind": "no-cut" if inside else "no-data",
                    "scheduled": label,
                    "scheduled_start": _clock(start),
                    "readings": len(inside),
                })
            day += dt.timedelta(days=1)

    for index, cut in enumerate(cuts):
        if index not in matched:
            events.append(_cut_event(cut, scheduled=None, scheduled_start=None))
    events.sort(key=lambda event: (event["date"], event.get("start_by") or event.get("scheduled_start") or ""))

    return {
        "since": observed_times[0].isoformat(timespec="minutes") if observed_times else None,
        "readings": len(observed_times),
        "supply_on_min_v": SUPPLY_ON_MIN_V,
        "events": events,
    }


def _clock(value: dt.datetime | None) -> str | None:
    return value.isoformat(timespec="minutes") if value is not None else None


def _cut_event(
    cut: dict[str, Any],
    *,
    scheduled: str | None,
    scheduled_start: dt.datetime | None,
) -> dict[str, Any]:
    start_after, start_by = cut["start_after"], cut["start_by"]
    end_after, end_by = cut["end_after"], cut["end_by"]
    event: dict[str, Any] = {
        "date": start_by.date().isoformat(),
        "kind": "cut" if scheduled else "unscheduled-cut",
        "scheduled": scheduled,
        "start_after": _clock(start_after),
        "start_by": _clock(start_by),
        "end_after": _clock(end_after),
        "end_by": _clock(end_by),
        "ongoing": end_by is None,
        "readings": cut["samples"],
        "soc_before": cut["soc_before"],
        "soc_at_start": cut["soc_at_start"],
        "duration_min": [_minutes(start_by, end_after), _minutes(start_after, end_by)],
    }
    if scheduled_start is not None:
        event["start_offset_min"] = [_minutes(scheduled_start, start_after), _minutes(scheduled_start, start_by)]
    return event


def _hhmm(value: str | None) -> str:
    return value[11:16] if value else "?"


def _pct(value: Any) -> str:
    return f"{value:g}%" if isinstance(value, (int, float)) else "?"


def format_supply_report(report: dict[str, Any], profile: str | None) -> str:
    lines = [f"Observed supply (grid voltage >= {report['supply_on_min_v']:g} V counts as on)"]
    if profile:
        lines.append(f"Scheduled windows from the active profile: {profile}")
    if not report["since"]:
        lines.append("No snapshot carries a grid voltage yet; the dashboard records one from this release on.")
        return "\n".join(lines)
    lines.append(f"Readings since {report['since']}: {report['readings']}")
    for event in report["events"]:
        date = event["date"]
        kind = event["kind"]
        if kind in ("no-cut", "no-data"):
            detail = (
                f"supply stayed on ({event['readings']} readings)"
                if kind == "no-cut"
                else "no readings inside the window"
            )
            lines.append(f"{date}  scheduled {event['scheduled']}: {detail}")
            continue
        start = f"{_hhmm(event['start_after'])}-{_hhmm(event['start_by'])}"
        end = "still off" if event["ongoing"] else f"{_hhmm(event['end_after'])}-{_hhmm(event['end_by'])}"
        label = f"scheduled {event['scheduled']}" if event["scheduled"] else "unscheduled"
        lines.append(
            f"{date}  {label}: off between {start}, back between {end}; "
            f"SOC {_pct(event['soc_before'])} before, {_pct(event['soc_at_start'])} at first off reading"
        )
    return "\n".join(lines)


def command_supply_history(config: Config, days: int = DEFAULT_SUPPLY_HISTORY_DAYS, json_output: bool = False) -> int:
    schedule = validate_schedule()
    tz = resolve_timezone(schedule.get("timezone")) or resolve_timezone(config.weather_timezone)
    now = dt.datetime.now().astimezone()
    report = build_supply_report(
        read_dashboard_metrics_history(now=now, days=max(days, 1) + 1),
        schedule.get("outage_windows") or [],
        now=now,
        tz=tz,
        days=days,
    )
    report["profile"] = schedule.get("active_profile")
    if json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_supply_report(report, schedule.get("active_profile")))
    return 0
