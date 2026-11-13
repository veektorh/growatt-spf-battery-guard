from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MIN_MATCHING_NIGHTS = 3


def day_type(value: dt.datetime) -> str:
    local = value.astimezone() if value.tzinfo is not None else value
    return "weekday" if local.weekday() < 5 else "weekend"


def _row_day_type(row: dict[str, Any]) -> str | None:
    stored = row.get("day_type")
    if stored in {"weekday", "weekend"}:
        return str(stored)
    timestamp = row.get("recorded_at")
    if not isinstance(timestamp, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return day_type(parsed)


def select_overnight_load(
    history: list[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    min_matching_nights: int = MIN_MATCHING_NIGHTS,
) -> dict[str, Any]:
    """Choose a learned overnight load without over-trusting sparse evidence."""
    now = now or dt.datetime.now().astimezone()
    target = day_type(now)
    valid = [row for row in history if isinstance(row.get("rate_w"), (int, float)) and row["rate_w"] > 0]
    matching = [row for row in valid if _row_day_type(row) == target]
    if len(matching) >= min_matching_nights:
        return {
            "rate_w": sum(float(row["rate_w"]) for row in matching) / len(matching),
            "source": f"{target} history ({len(matching)} nights)",
            "day_type": target,
            "matching_nights": len(matching),
            "total_nights": len(valid),
            "ready": True,
        }
    if len(valid) >= 2:
        return {
            "rate_w": sum(float(row["rate_w"]) for row in valid) / len(valid),
            "source": f"recent average ({len(valid)} nights; {len(matching)}/{min_matching_nights} matching {target})",
            "day_type": target,
            "matching_nights": len(matching),
            "total_nights": len(valid),
            "ready": False,
        }
    return {
        "rate_w": None,
        "source": "insufficient history",
        "day_type": target,
        "matching_nights": len(matching),
        "total_nights": len(valid),
        "ready": False,
    }


EVENING_START_HOUR = 18
OVERNIGHT_START_HOUR = 22
OVERNIGHT_END_HOUR = 6
NIGHT_BANDS = ("evening", "overnight")


def resolve_timezone(name: Any) -> dt.tzinfo | None:
    """Site timezone for night boundaries, or None to use the host's local time."""
    text = str(name or "").strip()
    if not text:
        return None
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _local(value: dt.datetime, tz: dt.tzinfo | None = None) -> dt.datetime:
    if tz is not None:
        return value.astimezone(tz) if value.tzinfo is not None else value.replace(tzinfo=tz)
    return value.astimezone() if value.tzinfo is not None else value


def night_band(value: dt.datetime, tz: dt.tzinfo | None = None) -> str | None:
    """Which band a discharge sample belongs to, or None for daytime samples."""
    hour = _local(value, tz).hour
    if EVENING_START_HOUR <= hour < OVERNIGHT_START_HOUR:
        return "evening"
    if hour >= OVERNIGHT_START_HOUR or hour < OVERNIGHT_END_HOUR:
        return "overnight"
    return None


def night_key(value: dt.datetime, tz: dt.tzinfo | None = None) -> dt.date:
    """The night a sample belongs to.

    Evening hours keep their date and after-midnight hours belong to the previous
    date, so one night's evening and overnight samples share a row and the night
    keeps the day type of the evening that started it.
    """
    return (_local(value, tz) - dt.timedelta(hours=OVERNIGHT_END_HOUR)).date()


def band_hours(start: dt.datetime, hours: float, band: str, tz: dt.tzinfo | None = None) -> float:
    """Hours of a remaining horizon that fall inside one band.

    Bands change on the hour, so splitting the horizon at every hour boundary and
    testing each segment's midpoint gives an exact split, including partial hours
    at either end of the horizon.
    """
    if hours <= 0:
        return 0.0
    end = start + dt.timedelta(hours=hours)
    edges = {start, end}
    cursor = start.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    while cursor < end:
        edges.add(cursor)
        cursor += dt.timedelta(hours=1)
    points = sorted(edges)
    total = 0.0
    for lower, upper in zip(points, points[1:]):
        if night_band(lower + (upper - lower) / 2, tz) == band:
            total += (upper - lower).total_seconds() / 3600.0
    return total


def _band_average(rows: list[dict[str, Any]], band: str) -> float | None:
    weighted = 0.0
    samples = 0
    for row in rows:
        bands = row.get("bands")
        if not isinstance(bands, dict):
            continue
        entry = bands.get(band)
        if not isinstance(entry, dict):
            continue
        rate = entry.get("rate_w")
        if not isinstance(rate, (int, float)) or rate <= 0:
            continue
        count = max(1, int(entry.get("sample_count", 1) or 1))
        weighted += float(rate) * count
        samples += count
    return weighted / samples if samples else None


def select_night_load_profile(
    history: list[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    hours: float,
    tz: dt.tzinfo | None = None,
    min_matching_nights: int = MIN_MATCHING_NIGHTS,
) -> dict[str, Any]:
    """Learn the evening and overnight loads separately and weight them by the horizon.

    ``rate_w`` is the horizon-weighted average, which is energy-exact: the deficit
    over the remaining hours is the integral of the banded rates. When a band that
    the horizon still needs has no history, the profile is not ready and ``rate_w``
    is None so the caller keeps using the flat nightly average.
    """
    local_now = _local(now or dt.datetime.now(), tz)
    target = day_type(local_now)
    matching = [
        row
        for row in history
        if isinstance(row.get("rate_w"), (int, float)) and _row_day_type(row) == target
    ]
    banded_nights = {
        band: sum(
            1
            for row in matching
            if isinstance(row.get("bands"), dict)
            and isinstance((row["bands"].get(band) or {}).get("rate_w"), (int, float))
        )
        for band in NIGHT_BANDS
    }
    band_rates = {band: _band_average(matching, band) for band in NIGHT_BANDS}
    band_spans = {band: band_hours(local_now, hours, band, tz) for band in NIGHT_BANDS}
    # A night horizon can start before 18:00 or end after 06:00. Those hours are
    # outside both bands and are planned at the overnight rate, which is the rate
    # either side of them.
    unattributed = max(0.0, hours - sum(band_spans.values()))
    band_spans["overnight"] += unattributed
    needed = [band for band, span in band_spans.items() if span > 0]
    profile: dict[str, Any] = {
        "rate_w": None,
        "source": "insufficient banded history",
        "ready": False,
        "day_type": target,
        "matching_nights": len(matching),
        "banded_nights": banded_nights,
        "bands": {band: {"rate_w": band_rates[band], "hours": round(band_spans[band], 2)} for band in NIGHT_BANDS},
    }
    if not needed or any(band_rates[band] is None for band in needed):
        return profile
    # A single banded night is not evidence: each band the horizon needs must
    # have its own matching nights before the flat average is replaced.
    if any(banded_nights[band] < min_matching_nights for band in needed):
        return profile
    span = sum(band_spans[band] for band in needed)
    if span <= 0:
        return profile
    profile["rate_w"] = sum(band_rates[band] * band_spans[band] for band in needed) / span
    profile["ready"] = True
    profile["source"] = (
        f"{target} bands ("
        + ", ".join(
            f"{band} {band_rates[band]:.0f} W for {band_spans[band]:.1f}h over {banded_nights[band]} nights"
            for band in needed
        )
        + ")"
    )
    return profile
