from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from growatt_guard.dashboard_metrics import summarize_bypass_episodes
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.paths import DATA_HOME


BYPASS_DAILY_FILE = DATA_HOME / "history" / "bypass_daily.jsonl"
BYPASS_ROLLUP_FIELDS = (
    "date",
    "episodes",
    "samples",
    "duration_minutes",
    "full_minutes",
    "full_idle_minutes",
    "full_grid_kwh",
)


def _parse_timestamp(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def read_bypass_daily(path: Path | None = None) -> list[dict[str, Any]]:
    source = path or BYPASS_DAILY_FILE
    if not source.exists():
        return []
    rows: list[dict[str, Any]] = []
    dates: set[str] = set()
    try:
        lines = source.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise GrowattGuardError(f"Could not read bypass rollup ledger: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GrowattGuardError(f"Invalid bypass rollup row {line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise GrowattGuardError(
                f"Invalid bypass rollup row {line_number}: expected an object."
            )
        try:
            day = dt.date.fromisoformat(str(row["date"])).isoformat()
            episodes = int(row["episodes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GrowattGuardError(
                f"Invalid bypass rollup row {line_number}: date and episodes are required."
            ) from exc
        if episodes < 0:
            raise GrowattGuardError(
                f"Invalid bypass rollup row {line_number}: episodes cannot be negative."
            )
        if day in dates:
            raise GrowattGuardError(
                f"Invalid bypass rollup row {line_number}: duplicate date {day}."
            )
        normalized = dict(row)
        normalized["date"] = day
        normalized["episodes"] = episodes
        dates.add(day)
        rows.append(normalized)
    rows.sort(key=lambda row: str(row["date"]))
    return rows


def _write_bypass_daily(rows: list[dict[str, Any]], path: Path | None = None) -> None:
    target = path or BYPASS_DAILY_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _day_rollup(day: dt.date, day_rows: list[dict[str, Any]]) -> dict[str, Any]:
    end_of_day = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min)
    report = summarize_bypass_episodes(day_rows, now=end_of_day, days=1)
    return {
        "date": day.isoformat(),
        "episodes": int(report["count"]),
        "samples": int(sum(episode["samples"] for episode in report["episodes"])),
        "duration_minutes": float(report["total_minutes"]),
        "full_minutes": float(report["total_full_minutes"]),
        "full_idle_minutes": float(report["total_full_idle_minutes"]),
        "full_grid_kwh": float(report["total_full_grid_kwh"]),
        "finalized_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def finalize_completed_bypass_days(
    metric_rows: list[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    path: Path | None = None,
) -> int:
    """Append a durable per-day bypass rollup for every completed observed day.

    The metric history only retains eight days, so month-scale bypass reporting needs
    its own ledger. Days already recorded are left untouched, and the whole retained
    history is rescanned so the rollup seeds itself on first run.
    """
    local_now = now or dt.datetime.now()
    if local_now.tzinfo is None:
        local_now = local_now.astimezone()
    existing = read_bypass_daily(path)
    known = {str(row["date"]) for row in existing}
    by_day: dict[dt.date, list[dict[str, Any]]] = {}
    for row in metric_rows:
        timestamp = _parse_timestamp(row.get("timestamp"))
        if timestamp is None or timestamp.date() >= local_now.date():
            continue
        if timestamp.date().isoformat() in known:
            continue
        by_day.setdefault(timestamp.date(), []).append(row)
    if not by_day:
        return 0

    added = 0
    updated = list(existing)
    for day in sorted(by_day):
        updated.append(_day_rollup(day, by_day[day]))
        added += 1
    updated.sort(key=lambda row: str(row["date"]))
    _write_bypass_daily(updated, path)
    return added
