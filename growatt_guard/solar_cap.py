"""Full-battery solar cap: when the array is throttled to the house load.

The SPF has no export path, so once the battery is full the inverter pins PV
to the load. A day spent there records low generation that is not weak sun.
The dashboard's live "Solar surplus" row and the day's durable record share
this one predicate so they never disagree about what counts as capped.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

CAPPED_FULL_SOC = 95.0
CAPPED_PIN_W = 100.0
# Inverter losses grow with throughput, so at a 3.7 kW load a pinned array
# reads ~170 W above the load (2026-11-20 13:37, battery at 99%, not charging).
CAPPED_PIN_SHARE = 0.05
# Consecutive capped reads count the time between them only when the gap is no
# longer than two normal refreshes; a longer hole is unobserved, not capped.
CAPPED_MAX_GAP_MINUTES = 30.0


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def is_capped_reading(row: dict[str, Any]) -> bool:
    soc = _number(row.get("soc"))
    pv_w = _number(row.get("pv_w"))
    load_w = _number(row.get("load_w"))
    return (
        soc is not None
        and pv_w is not None
        and load_w is not None
        and soc >= CAPPED_FULL_SOC
        and pv_w > 0
        and abs(pv_w - load_w) <= max(CAPPED_PIN_W, CAPPED_PIN_SHARE * load_w)
    )


def summarize_capped_day(
    readings: list[tuple[dt.datetime, dict[str, Any]]],
) -> dict[str, int] | None:
    """Capped minutes and peak PV over one day's metric readings.

    Time counts only between two consecutive capped reads, so a single reading
    at the top (a passing cloud) adds nothing, matching the live row's two-read
    rule. Returns None when the day has no PV readings at all.
    """
    ordered = sorted(readings, key=lambda item: item[0])
    pv_values = [value for _, row in ordered if (value := _number(row.get("pv_w"))) is not None]
    if not pv_values:
        return None
    capped_minutes = 0.0
    for (start, start_row), (end, end_row) in zip(ordered, ordered[1:]):
        gap = (end - start).total_seconds() / 60
        if 0 < gap <= CAPPED_MAX_GAP_MINUTES and is_capped_reading(start_row) and is_capped_reading(end_row):
            capped_minutes += gap
    return {"capped_min": round(capped_minutes), "peak_pv_w": round(max(0.0, max(pv_values)))}
