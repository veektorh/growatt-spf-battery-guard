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
# Growatt reports pv_today in 0.1 kWh steps, so a drop larger than rounding means the
# counter reset and every earlier sample in the day was a stale carry-over.
STALE_RESET_MIN_DROP_KWH = 0.2
# Daily energy totals kept beside generation so the dashboard can show Utility and
# load by day, week and month. Growatt exposes only today's counters, so a day that
# is not captured here can never be recovered.
DAILY_ENERGY_FIELDS: tuple[tuple[str, str], ...] = (
    ("load_wh", "load_today_kwh"),
    ("utility_wh", "grid_today_kwh"),
    ("charge_wh", "charge_today_kwh"),
    ("discharge_wh", "discharge_today_kwh"),
)


def _parse_timestamp(value: Any, tz: dt.tzinfo | None = None) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz) if tz is not None else parsed
    return parsed.astimezone(tz) if tz is not None else parsed


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
        for field, _ in DAILY_ENERGY_FIELDS:
            if field not in row or row[field] is None:
                continue
            try:
                value = int(row[field])
            except (TypeError, ValueError) as exc:
                raise GrowattGuardError(
                    f"Invalid daily generation ledger row {line_number}: {field} must be an integer."
                ) from exc
            if value < 0:
                raise GrowattGuardError(
                    f"Invalid daily generation ledger row {line_number}: {field} cannot be negative."
                )
            normalized[field] = value
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


def _energy_row(value: int | dict[str, int]) -> dict[str, int]:
    if isinstance(value, dict):
        return {field: int(amount) for field, amount in value.items()}
    return {"generated_wh": int(value)}


def merge_daily_generation(
    records: dict[dt.date, int | dict[str, int]],
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
    for day, raw in sorted(records.items()):
        energy = _energy_row(raw)
        generated_wh = energy.get("generated_wh", 0)
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
        for field, amount in energy.items():
            if field != "generated_wh":
                row[field] = int(amount)
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


def _final_observation(samples: list[tuple[dt.datetime, float]]) -> tuple[dt.datetime, float]:
    """Return the day's closing sample, ignoring a stale carry-over from the previous day.

    Growatt can keep serving the previous day's ``pv_today`` total after midnight and
    then drop it when the counter resets, so every sample before the last drop belongs
    to the previous day. Taking ``max`` over the day records that stale peak as the
    day's generation, which inflates any day that out-generated the day before it.
    """
    ordered = sorted(samples)
    segment_peak = ordered[0][1]
    for _, value in ordered[1:]:
        if value < segment_peak - STALE_RESET_MIN_DROP_KWH:
            segment_peak = value
            continue
        segment_peak = max(segment_peak, value)
    return ordered[-1]


def finalize_completed_daily_generation(
    metric_rows: list[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    path: Path | None = None,
    tz: dt.tzinfo | None = None,
) -> tuple[int, tuple[dict[str, Any], ...]]:
    local_now = now or dt.datetime.now()
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=tz) if tz is not None else local_now.astimezone()
    elif tz is not None:
        local_now = local_now.astimezone(tz)
    existing = read_daily_generation(path)
    latest_date = (
        dt.date.fromisoformat(str(existing[-1]["date"]))
        if existing
        else local_now.date() - dt.timedelta(days=1)
    )
    values: dict[dt.date, list[tuple[dt.datetime, float]]] = {}
    counters: dict[dt.date, dict[dt.datetime, dict[str, int]]] = {}
    for row in metric_rows:
        timestamp = _parse_timestamp(row.get("timestamp"), tz)
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
        day_counters = counters.setdefault(timestamp.date(), {})
        snapshot = day_counters.setdefault(timestamp, {})
        for field, source_key in DAILY_ENERGY_FIELDS:
            try:
                amount_kwh = float(row[source_key])
            except (KeyError, TypeError, ValueError):
                continue
            if amount_kwh < 0:
                continue
            snapshot[field] = round(amount_kwh * 1000)

    records: dict[dt.date, dict[str, int]] = {}
    observed_through: dict[dt.date, str] = {}
    previous_wh = int(existing[-1]["generated_wh"]) if existing else None
    for day in sorted(values):
        samples = values[day]
        last_timestamp, generated_kwh = _final_observation(samples)
        if last_timestamp.hour < FINAL_OBSERVATION_HOUR:
            continue
        generated_wh = round(generated_kwh * 1000)
        peak = max(value for _, value in samples)
        trough = min(value for _, value in samples)
        counter_flat = (peak - trough) <= STALE_RESET_MIN_DROP_KWH
        if (
            previous_wh is not None
            and previous_wh > 0
            and generated_wh == previous_wh
            and counter_flat
        ):
            logging.warning(
                "Daily generation for %s repeats %sWh with a flat pv_today counter; "
                "treating it as an all-day stale carry-over and leaving the day unrecorded.",
                day.isoformat(),
                generated_wh,
            )
            continue
        record = {"generated_wh": generated_wh}
        record.update(counters.get(day, {}).get(last_timestamp, {}))
        records[day] = record
        observed_through[day] = last_timestamp.isoformat()
        previous_wh = generated_wh

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


def _kwh(value: int | None) -> float | None:
    return None if value is None else round(value / 1000, 2)


def _sum_or_none(values: list[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def build_daily_energy_view(
    rows: list[dict[str, Any]],
    *,
    today: dt.date | None = None,
    months: int = 12,
) -> dict[str, Any]:
    """Month totals plus the current month's per-day solar, load and Utility energy.

    Load and Utility only exist from the day the ledger started recording them, so a
    month reports ``None`` for a metric whose days carry no reading instead of zero.
    """
    local_today = today or dt.date.today()
    current_month = local_today.isoformat()[:7]
    by_month: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        month = str(row["date"])[:7]
        by_month.setdefault(month, []).append(row)

    month_rows: list[dict[str, Any]] = []
    for month in sorted(by_month)[-months:]:
        days = by_month[month]
        month_rows.append(
            {
                "month": month,
                "solar_kwh": _kwh(_sum_or_none([int(day["generated_wh"]) for day in days])),
                "load_kwh": _kwh(_sum_or_none([day.get("load_wh") for day in days])),
                "utility_kwh": _kwh(_sum_or_none([day.get("utility_wh") for day in days])),
                "days": len(days),
                "complete": month != current_month,
            }
        )

    month_days = [
        {
            "date": str(row["date"]),
            "day": int(str(row["date"])[8:10]),
            "solar_kwh": _kwh(int(row["generated_wh"])),
            "load_kwh": _kwh(row.get("load_wh")),
            "utility_kwh": _kwh(row.get("utility_wh")),
        }
        for row in by_month.get(current_month, [])
    ]
    return {"month": current_month, "months": month_rows, "month_days": month_days}
