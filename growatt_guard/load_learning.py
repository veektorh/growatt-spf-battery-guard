from __future__ import annotations

import datetime as dt
from typing import Any


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
