from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.paths import DATA_HOME


DAILY_GENERATION_FILE = DATA_HOME / "history" / "daily_generation.jsonl"
FINAL_OBSERVATION_HOUR = 20


def _parse_timestamp(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def read_daily_generation(path: Path | None = None) -> list[dict[str, Any]]:
    source = path or DAILY_GENERATION_FILE
    if not source.exists():
        return []
    rows: list[dict[str, Any]] = []
    dates: set[str] = set()
    try:
        lines = source.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise GrowattGuardError(f"Could not read daily generation ledger: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GrowattGuardError(
                f"Invalid daily generation ledger row {line_number}: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise GrowattGuardError(
                f"Invalid daily generation ledger row {line_number}: expected an object."
            )
        try:
            dt.date.fromisoformat(str(row["date"]))
            generated_wh = int(row["generated_wh"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GrowattGuardError(
                f"Invalid daily generation ledger row {line_number}: date and generated_wh are required."
            ) from exc
        if generated_wh < 0:
            raise GrowattGuardError(
                f"Invalid daily generation ledger row {line_number}: generated_wh cannot be negative."
            )
        normalized = dict(row)
        normalized["date"] = dt.date.fromisoformat(str(row["date"])).isoformat()
        if normalized["date"] in dates:
            raise GrowattGuardError(
                f"Invalid daily generation ledger row {line_number}: duplicate date {normalized['date']}."
            )
        dates.add(normalized["date"])
        normalized["generated_wh"] = generated_wh
        rows.append(normalized)
    rows.sort(key=lambda row: str(row["date"]))
    return rows


def _write_daily_generation(rows: list[dict[str, Any]], path: Path | None = None) -> None:
    target = path or DAILY_GENERATION_FILE
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


def merge_daily_generation(
    records: dict[dt.date, int],
    *,
    source: str,
    finalized_at: dt.datetime | None = None,
    observed_through: dict[dt.date, str] | None = None,
    strict: bool = False,
    path: Path | None = None,
) -> tuple[int, tuple[dict[str, Any], ...]]:
    existing_rows = read_daily_generation(path)
    by_date = {str(row["date"]): row for row in existing_rows}
    conflicts: list[dict[str, Any]] = []
    added = 0
    stamp = (finalized_at or dt.datetime.now(dt.timezone.utc)).isoformat()
    for day, generated_wh in sorted(records.items()):
        if generated_wh < 0:
            raise GrowattGuardError(f"Daily generation cannot be negative for {day.isoformat()}.")
        key = day.isoformat()
        existing = by_date.get(key)
        if existing is not None:
            if int(existing["generated_wh"]) != int(generated_wh):
                conflicts.append(
                    {
                        "date": key,
                        "existing_wh": int(existing["generated_wh"]),
                        "candidate_wh": int(generated_wh),
                        "existing_source": str(existing.get("source", "unknown")),
                        "candidate_source": source,
                    }
                )
            continue
        row: dict[str, Any] = {
            "date": key,
            "generated_wh": int(generated_wh),
            "source": source,
            "finalized_at": stamp,
        }
        if observed_through and day in observed_through:
            row["observed_through"] = observed_through[day]
        by_date[key] = row
        added += 1
    if conflicts and strict:
        dates = ", ".join(item["date"] for item in conflicts[:5])
        raise GrowattGuardError(f"Daily generation ledger conflicts on: {dates}.")
    if added:
        _write_daily_generation([by_date[key] for key in sorted(by_date)], path)
    return added, tuple(conflicts)


def finalize_completed_daily_generation(
    metric_rows: list[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    path: Path | None = None,
) -> tuple[int, tuple[dict[str, Any], ...]]:
    local_now = now or dt.datetime.now()
    if local_now.tzinfo is None:
        local_now = local_now.astimezone()
    existing = read_daily_generation(path)
    latest_date = (
        dt.date.fromisoformat(str(existing[-1]["date"]))
        if existing
        else local_now.date() - dt.timedelta(days=1)
    )
    values: dict[dt.date, list[tuple[dt.datetime, float]]] = {}
    for row in metric_rows:
        timestamp = _parse_timestamp(row.get("timestamp"))
        if timestamp is None or timestamp.date() >= local_now.date():
            continue
        if existing and timestamp.date() <= latest_date:
            continue
        if not existing and timestamp.date() != latest_date:
            continue
        try:
            generated_kwh = float(row["pv_today_kwh"])
        except (KeyError, TypeError, ValueError):
            continue
        if generated_kwh < 0:
            continue
        values.setdefault(timestamp.date(), []).append((timestamp, generated_kwh))

    records: dict[dt.date, int] = {}
    observed_through: dict[dt.date, str] = {}
    for day, samples in values.items():
        last_timestamp = max(timestamp for timestamp, _ in samples)
        if last_timestamp.hour < FINAL_OBSERVATION_HOUR:
            continue
        records[day] = round(max(value for _, value in samples) * 1000)
        observed_through[day] = last_timestamp.isoformat()

    added, conflicts = merge_daily_generation(
        records,
        source="growatt-observability",
        finalized_at=local_now,
        observed_through=observed_through,
        strict=False,
        path=path,
    )
    for conflict in conflicts:
        logging.error(
            "Daily generation ledger conflict for %s: existing=%sWh candidate=%sWh.",
            conflict["date"],
            conflict["existing_wh"],
            conflict["candidate_wh"],
        )
    return added, conflicts
