"""Hour-of-day load profile from the dashboard snapshot history (shadow only).

For each local hour, every day in the window contributes the mean of its
``load_w`` readings in that hour. The median across days is the expected load
and the 20th to 80th percentile the likely range. An hour needs readings on
``MIN_PROFILE_DAYS`` days before it counts.

This is a read-only comparison against the banded night planner in
``load_learning.py``, with one exception: ``pre_cut_load_basis`` lets the
morning preserve job reject a transient ``outPutPower`` spike before it sizes a
Utility hold from it (ROADMAP N26). Wiring the profile into the night planner
changes inverter mode timing and still needs the owner's go-ahead (ROADMAP N17).
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
from typing import Any

from growatt_guard.config import Config
from growatt_guard.dashboard_metrics import read_dashboard_metrics_history
from growatt_guard.load_learning import (
    EVENING_START_HOUR,
    OVERNIGHT_END_HOUR,
    OVERNIGHT_START_HOUR,
    _band_average,
    day_type,
    night_key,
    resolve_timezone,
    select_night_load_profile,
    select_overnight_load,
)
from growatt_guard.schedule import validate_schedule
from growatt_guard.state import read_discharge_rate_history

MIN_PROFILE_DAYS = 3
# Snapshot window for the hourly load profile. Two full weeks guarantees four
# weekend nights (buffer of one above MIN_PROFILE_DAYS) so both Saturday and
# Sunday qualify instead of Saturday falling back to the band planner under
# the 8-day dashboard chart window, while still clearing seasonal load shifts
# within two weeks.
LOAD_PROFILE_HISTORY_DAYS = 14
# Readings an hour of one day needs before its mean stands for that hour. The
# dashboard refreshes about four times an hour, so two readings is half an hour.
MIN_HOUR_READINGS = 2
LOW_PERCENTILE = 20
HIGH_PERCENTILE = 80
# How far above the current hour's 80th percentile a single reading may sit
# before the pre-cut projection stops trusting it. The spike that opened N26 was
# 2.7 times that percentile; a genuinely hard hour stays under this ratio.
PRE_CUT_SPIKE_RATIO = 1.5


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile of a non-empty list."""
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct / 100.0
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _local(row: dict[str, Any], tz: dt.tzinfo | None) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(row.get("timestamp")))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz) if tz is not None else parsed.astimezone()
    return parsed.astimezone(tz) if tz is not None else parsed.astimezone()


def hourly_load_profile(
    rows: list[dict[str, Any]],
    *,
    tz: dt.tzinfo | None = None,
    min_days: int = MIN_PROFILE_DAYS,
    night_type: str | None = None,
) -> dict[int, dict[str, Any]]:
    """Per local hour: median and likely-range load in W, and the days behind it.

    ``night_type`` keeps only readings that belong to a night of that day type.
    The night is the one a sample falls in, so after-midnight hours are judged
    by the evening that started the night, exactly as the band history does.
    """
    readings: dict[tuple[dt.date, int], list[float]] = {}
    for row in rows:
        load = row.get("load_w")
        if isinstance(load, bool) or not isinstance(load, (int, float)) or load < 0:
            continue
        local = _local(row, tz)
        if local is None:
            continue
        if night_type is not None and day_type(night_key(local, tz)) != night_type:
            continue
        readings.setdefault((local.date(), local.hour), []).append(float(load))

    per_hour: dict[int, list[float]] = {hour: [] for hour in range(24)}
    for (_, hour), values in readings.items():
        if len(values) >= MIN_HOUR_READINGS:
            per_hour[hour].append(statistics.fmean(values))

    profile: dict[int, dict[str, Any]] = {}
    for hour, values in per_hour.items():
        entry: dict[str, Any] = {"days": len(values), "ready": len(values) >= min_days}
        if values:
            entry["median_w"] = round(statistics.median(values))
            entry["low_w"] = round(percentile(values, LOW_PERCENTILE))
            entry["high_w"] = round(percentile(values, HIGH_PERCENTILE))
        profile[hour] = entry
    return profile


def project_load(
    profile: dict[int, dict[str, Any]],
    start: dt.datetime,
    hours: float,
) -> dict[str, Any] | None:
    """Energy over a horizon from the hourly profile, partial hours included.

    Returns None when any hour the horizon touches is not ready, so a projection
    is never quietly built from a gap.
    """
    if hours <= 0:
        return None
    end = start + dt.timedelta(hours=hours)
    totals = {"median_w": 0.0, "low_w": 0.0, "high_w": 0.0}
    cursor = start
    while cursor < end:
        boundary = cursor.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        segment_end = min(boundary, end)
        entry = profile.get(cursor.hour) or {}
        if not entry.get("ready"):
            return None
        span = (segment_end - cursor).total_seconds() / 3600.0
        for key in totals:
            totals[key] += float(entry[key]) * span
        cursor = segment_end
    return {
        "hours": round(hours, 2),
        "median_wh": round(totals["median_w"]),
        "low_wh": round(totals["low_w"]),
        "high_wh": round(totals["high_w"]),
        "rate_w": round(totals["median_w"] / hours),
    }


def pre_cut_load_basis(
    sample_w: float | None,
    rows: list[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    tz: dt.tzinfo | None = None,
) -> tuple[float | None, str | None]:
    """Load to project the outage-start SOC from, and why it was substituted.

    The projection drains one ``outPutPower`` reading across the whole time to
    the cut, so a transient spike decides the hold. When the sample is more than
    ``PRE_CUT_SPIKE_RATIO`` times the current hour's 80th percentile, the hour's
    profile median is used instead and a note explains the substitution for the
    audit row.

    Every other case keeps the sample: an hour without enough days, a missing
    band, a quiet reading, and a sample within the ratio. The guard must keep
    failing towards a hold, so the substitution is one-sided — it can only make
    the projection less pessimistic, and only for a reading no hour has
    supported. The band is built from every day, not one night's day type,
    because a plausibility limit wants the widest support.
    """
    if not isinstance(sample_w, (int, float)) or isinstance(sample_w, bool) or sample_w <= 0:
        return sample_w, None
    local_now = now or dt.datetime.now().astimezone()
    if local_now.tzinfo is None:
        local_now = local_now.astimezone()
    local_hour = local_now.astimezone(tz).hour if tz is not None else local_now.hour
    entry = hourly_load_profile(rows, tz=tz).get(local_hour) or {}
    if not entry.get("ready"):
        return sample_w, None
    high_w = entry.get("high_w")
    median_w = entry.get("median_w")
    if not isinstance(high_w, (int, float)) or not isinstance(median_w, (int, float)) or high_w <= 0:
        return sample_w, None
    if sample_w <= PRE_CUT_SPIKE_RATIO * high_w:
        return sample_w, None
    note = (
        f"projecting from the {local_hour:02d}:00 hourly profile median {median_w:.0f} W "
        f"because the live {sample_w:.0f} W reading is {sample_w / high_w:.1f}x the hour's "
        f"80th percentile {high_w:.0f} W"
    )
    return float(median_w), note


def planning_night_load(
    rows: list[dict[str, Any]],
    history: list[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    hours: float,
    tz: dt.tzinfo | None = None,
) -> tuple[float | None, str]:
    """Night load for the reserve plan.

    Prefers the hour-of-day profile of the night the horizon belongs to when
    every hour it touches has enough days, then the banded planner, then its
    flat fallback. The night is the one the horizon starts in, so a plan made at
    01:00 uses the evening that started the night rather than the new date.
    """
    now = now or dt.datetime.now().astimezone()
    if hours > 0:
        local = now.astimezone(tz) if tz is not None else now.astimezone()
        kind = day_type(night_key(local, tz))
        projection = project_load(hourly_load_profile(rows, tz=tz, night_type=kind), local, hours)
        if projection is not None:
            return float(projection["rate_w"]), f"hourly profile median ({kind}, {projection['hours']:g}h)"
    profile = select_night_load_profile(history, now=now, hours=hours, tz=tz)
    if profile["rate_w"] is not None:
        return float(profile["rate_w"]), str(profile["source"])
    flat = select_overnight_load(history, now=now)
    return (
        float(flat["rate_w"]) if flat["rate_w"] is not None else None,
        str(flat["source"]),
    )


def _night_start(now: dt.datetime) -> dt.datetime:
    """The next 18:00 start of the night bands, or tonight's if it is already night."""
    start = now.replace(hour=EVENING_START_HOUR, minute=0, second=0, microsecond=0)
    return start - dt.timedelta(days=1) if now.hour < OVERNIGHT_END_HOUR else start


def band_planner_night(history: list[dict[str, Any]], target_day_type: str) -> dict[str, Any] | None:
    """The banded planner's night energy for one day type, from its own learned rates."""
    matching = [row for row in history if row.get("day_type") == target_day_type]
    evening = _band_average(matching, "evening")
    overnight = _band_average(matching, "overnight")
    if evening is None or overnight is None:
        return None
    evening_hours = OVERNIGHT_START_HOUR - EVENING_START_HOUR
    overnight_hours = 24 - OVERNIGHT_START_HOUR + OVERNIGHT_END_HOUR
    energy = evening * evening_hours + overnight * overnight_hours
    return {
        "evening_w": round(evening),
        "overnight_w": round(overnight),
        "nights": len(matching),
        "energy_wh": round(energy),
        "rate_w": round(energy / (evening_hours + overnight_hours)),
    }


def build_load_profile_report(
    rows: list[dict[str, Any]],
    discharge_history: list[dict[str, Any]],
    *,
    now: dt.datetime,
    tz: dt.tzinfo | None = None,
) -> dict[str, Any]:
    local_now = now.astimezone(tz) if tz is not None else now.astimezone()
    profile = hourly_load_profile(rows, tz=tz)
    night_start = _night_start(local_now)
    night_hours = 24 - EVENING_START_HOUR + OVERNIGHT_END_HOUR
    target = day_type(night_start)
    typed = {
        kind: hourly_load_profile(rows, tz=tz, night_type=kind) for kind in ("weekday", "weekend")
    }
    return {
        "generated_at": local_now.isoformat(timespec="minutes"),
        "min_days": MIN_PROFILE_DAYS,
        "hours": {str(hour): entry for hour, entry in profile.items()},
        "night": {
            "start": night_start.isoformat(timespec="minutes"),
            "hours": night_hours,
            "day_type": target,
            "hourly": project_load(profile, night_start, night_hours),
            "bands": band_planner_night(discharge_history, target),
            "hourly_by_type": {
                kind: project_load(typed[kind], night_start, night_hours)
                for kind in typed
            },
            "bands_by_type": {
                kind: band_planner_night(discharge_history, kind) for kind in typed
            },
        },
    }


def format_load_profile_report(report: dict[str, Any]) -> str:
    lines = [
        "Hourly load profile (shadow only; the planner does not use it)",
        f"Median and {LOW_PERCENTILE}-{HIGH_PERCENTILE}th percentile of each day's mean load in that hour; "
        f"an hour needs {report['min_days']} days.",
    ]
    for hour in range(24):
        entry = report["hours"][str(hour)]
        if "median_w" not in entry:
            lines.append(f"  {hour:02d}:00  no data")
            continue
        flag = "" if entry["ready"] else "  (not enough days)"
        lines.append(
            f"  {hour:02d}:00  {entry['median_w']:>5} W  ({entry['low_w']}-{entry['high_w']} W, "
            f"{entry['days']} days){flag}"
        )
    night = report["night"]
    lines.append(
        f"Night from {night['start'][11:16]} for {night['hours']}h ({night['day_type']}):"
    )
    hourly = night["hourly"]
    if hourly:
        lines.append(
            f"  hourly profile: {hourly['median_wh'] / 1000:.1f} kWh (average {hourly['rate_w']} W), "
            f"likely {hourly['low_wh'] / 1000:.1f}-{hourly['high_wh'] / 1000:.1f} kWh"
        )
    else:
        lines.append("  hourly profile: not every night hour has enough days yet")
    bands = night["bands"]
    if bands:
        lines.append(
            f"  band planner:   {bands['energy_wh'] / 1000:.1f} kWh (evening {bands['evening_w']} W, "
            f"overnight {bands['overnight_w']} W, {bands['nights']} nights)"
        )
    else:
        lines.append("  band planner:   no banded history for this day type")
    typed = []
    for kind in ("weekday", "weekend"):
        hourly_typed = (night.get("hourly_by_type") or {}).get(kind)
        bands_typed = (night.get("bands_by_type") or {}).get(kind)
        hourly_text = f"{hourly_typed['median_wh'] / 1000:.1f}" if hourly_typed else "n/a"
        bands_text = f"{bands_typed['energy_wh'] / 1000:.1f}" if bands_typed else "n/a"
        typed.append(f"{kind} {hourly_text} / {bands_text} kWh")
    lines.append("  by day type (hourly / band): " + ", ".join(typed))
    return "\n".join(lines)


def command_load_profile(config: Config, json_output: bool = False) -> int:
    schedule = validate_schedule()
    tz = resolve_timezone(schedule.get("timezone")) or resolve_timezone(config.weather_timezone)
    now = dt.datetime.now().astimezone()
    report = build_load_profile_report(
        read_dashboard_metrics_history(now=now, days=LOAD_PROFILE_HISTORY_DAYS),
        read_discharge_rate_history(),
        now=now,
        tz=tz,
    )
    if json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_load_profile_report(report))
    return 0
