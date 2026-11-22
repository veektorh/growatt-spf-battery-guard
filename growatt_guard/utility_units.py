"""Prepaid utility-meter units ledger.

The inverter knows only the grid energy it draws itself. A prepaid meter also
counts loads wired straight to the grid, so the estate's real consumption can
only come from the meter. This ledger keeps the two numbers the meter cannot
tell the app on its own:

* ``topup`` — units bought, with an optional cost;
* ``reading`` — the meter's remaining balance at a known moment.

Consumption between two readings is ``opening + top-ups - closing``. One unit is
one kWh, which is what a prepaid meter sells. The daily ledger's ``utility_wh``
is subtracted from that total to show how much of the estate's grid use the
inverter saw; the remainder is consumption outside the inverter.

Read-only with respect to the inverter: no Growatt API call and no mode change.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.paths import DATA_HOME

UTILITY_UNITS_FILE = DATA_HOME / "history" / "utility_units.jsonl"
UTILITY_TARIFF_FILE = DATA_HOME / "state" / "utility_tariff.json"
UTILITY_CURRENCY = "NGN"
CURRENCY_SYMBOL = "₦"
TARIFF_SCHEMA_VERSION = 1
METER_KINDS = ("topup", "reading")
DEFAULT_BURN_WINDOW_DAYS = 14.0
LOW_COVER_DAYS = 7.0
MAX_NOTE_LENGTH = 200


def _at(event: dict[str, Any]) -> dt.datetime:
    return dt.datetime.fromisoformat(str(event["at"]))


def _local_now(now: dt.datetime | None, tz: dt.tzinfo | None) -> dt.datetime:
    if now is None:
        now = dt.datetime.now()
    if now.tzinfo is None:
        now = now.astimezone()
    return now.astimezone(tz) if tz is not None else now.astimezone()


def _parse_time(value: Any, where: str) -> dt.datetime:
    if value in (None, ""):
        return dt.datetime.now().astimezone()
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise GrowattGuardError(f"{where} must be an ISO 8601 date or timestamp.") from exc
    # A date or a timestamp without an offset is wall-clock time on this host.
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def _number(value: Any, where: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GrowattGuardError(f"{where} must be a number.")
    number = float(value)
    if not math.isfinite(number):
        raise GrowattGuardError(f"{where} must be finite.")
    if minimum is not None and number < minimum:
        raise GrowattGuardError(f"{where} must be at least {minimum:g}.")
    return number


def normalize_event(raw: Any, index: int | None = None) -> dict[str, Any]:
    """Validate one ledger row and return its canonical form."""
    where = f"Utility units event {index}" if index is not None else "Utility units event"
    if not isinstance(raw, dict):
        raise GrowattGuardError(f"{where} must be an object.")
    kind = str(raw.get("kind") or "").strip()
    if kind not in METER_KINDS:
        raise GrowattGuardError(f"{where} kind must be one of {', '.join(METER_KINDS)}.")
    event: dict[str, Any] = {
        "kind": kind,
        "at": _parse_time(raw.get("at"), f"{where} time").isoformat(timespec="minutes"),
    }
    if kind == "topup":
        units = _number(raw.get("units"), f"{where} units", minimum=0.0)
        if units <= 0:
            raise GrowattGuardError(f"{where} units must be greater than zero.")
        event["units"] = round(units, 1)
        if raw.get("cost") is not None:
            event["cost"] = round(_number(raw.get("cost"), f"{where} cost", minimum=0.0), 2)
    else:
        event["balance"] = round(_number(raw.get("balance"), f"{where} balance", minimum=0.0), 1)
    note = str(raw.get("note") or "").strip()
    if note:
        event["note"] = note[:MAX_NOTE_LENGTH]
    return event


def read_utility_units(path: Path | None = None) -> list[dict[str, Any]]:
    """Every ledger event, oldest first; a missing file is an empty ledger."""
    source = path or UTILITY_UNITS_FILE
    if not source.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GrowattGuardError(f"Could not read the utility units ledger: {exc}") from exc
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GrowattGuardError(f"Invalid utility units row {index}: {exc}") from exc
        events.append(normalize_event(raw, index))
    # A top-up recorded in the same minute as a reading happened before it, so
    # the reading includes the purchase. Sort by time, then top-ups first.
    events.sort(key=lambda event: (_at(event), 0 if event["kind"] == "topup" else 1))
    return events


def _write_events(events: list[dict[str, Any]], path: Path | None = None) -> None:
    target = path or UTILITY_UNITS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n" for event in events
    )
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append(event: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    target = path or UTILITY_UNITS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    return event


def record_topup(
    units: float,
    *,
    cost: float | None = None,
    at: str | None = None,
    note: str = "",
    path: Path | None = None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {"kind": "topup", "units": units, "at": at, "note": note}
    if cost is not None:
        raw["cost"] = cost
    return _append(normalize_event(raw), path)


def record_reading(
    balance: float,
    *,
    at: str | None = None,
    note: str = "",
    path: Path | None = None,
) -> dict[str, Any]:
    raw = {"kind": "reading", "balance": balance, "at": at, "note": note}
    return _append(normalize_event(raw), path)


def delete_event(index: int, path: Path | None = None) -> dict[str, Any]:
    """Remove the 1-based event index shown by ``utility-units list``."""
    events = read_utility_units(path)
    if not 1 <= index <= len(events):
        raise GrowattGuardError(
            f"Utility units event {index} does not exist; the ledger has {len(events)} events."
        )
    removed = events.pop(index - 1)
    _write_events(events, path)
    return removed


def write_utility_units(events: list[dict[str, Any]], path: Path | None = None) -> None:
    """Replace the ledger with validated, time-ordered events."""
    normalized = [normalize_event(event) for event in events]
    normalized.sort(key=lambda event: (_at(event), 0 if event["kind"] == "topup" else 1))
    _write_events(normalized, path)


def normalize_tariff_row(raw: Any, index: int | None = None) -> dict[str, Any]:
    """Validate one effective-dated tariff and return its canonical form."""
    where = f"Utility tariff row {index}" if index is not None else "Utility tariff row"
    if not isinstance(raw, dict):
        raise GrowattGuardError(f"{where} must be an object.")
    try:
        effective = dt.date.fromisoformat(str(raw.get("from"))).isoformat()
    except ValueError as exc:
        raise GrowattGuardError(f"{where} from must be a date like YYYY-MM-DD.") from exc
    row: dict[str, Any] = {
        "from": effective,
        "rate": round(_number(raw.get("rate"), f"{where} rate", minimum=0.0), 4),
        "fixed": round(_number(raw.get("fixed"), f"{where} fixed charge", minimum=0.0), 2),
    }
    if row["rate"] <= 0:
        raise GrowattGuardError(f"{where} rate must be greater than zero.")
    note = str(raw.get("note") or "").strip()
    if note:
        row["note"] = note[:MAX_NOTE_LENGTH]
    return row


def read_utility_tariff(path: Path | None = None) -> list[dict[str, Any]]:
    """Effective-dated tariffs, oldest first; a missing file has no tariff."""
    source = path or UTILITY_TARIFF_FILE
    if not source.exists():
        return []
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GrowattGuardError(f"Could not read the utility tariff: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != TARIFF_SCHEMA_VERSION:
        raise GrowattGuardError(f"Utility tariff schema_version must be {TARIFF_SCHEMA_VERSION}.")
    rows = payload.get("rates")
    if not isinstance(rows, list):
        raise GrowattGuardError("Utility tariff rates must be a list.")
    normalized = [normalize_tariff_row(row, index) for index, row in enumerate(rows, start=1)]
    normalized.sort(key=lambda row: row["from"])
    return normalized


def write_utility_tariff(rates: list[dict[str, Any]], path: Path | None = None) -> None:
    """Replace the tariff schedule with validated, date-ordered rows."""
    target = path or UTILITY_TARIFF_FILE
    normalized = [normalize_tariff_row(row) for row in rates]
    normalized.sort(key=lambda row: row["from"])
    target.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        {"schema_version": TARIFF_SCHEMA_VERSION, "rates": normalized},
        indent=2,
        sort_keys=True,
    ) + "\n"
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_utility_tariff(
    rate: float,
    fixed: float,
    *,
    from_date: str | None = None,
    note: str = "",
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Add a tariff effective from ``from_date``, replacing a row on that date."""
    effective = from_date or dt.date.today().isoformat()
    rows = [row for row in read_utility_tariff(path) if row["from"] != effective]
    rows.append(
        normalize_tariff_row(
            {"from": effective, "rate": rate, "fixed": fixed, "note": note}
        )
    )
    write_utility_tariff(rows, path)
    return read_utility_tariff(path)


def tariff_at(rates: list[dict[str, Any]], day: dt.date) -> dict[str, Any] | None:
    """The tariff effective on ``day``, or None before the first row."""
    found = None
    for row in rates:
        if dt.date.fromisoformat(row["from"]) <= day:
            found = row
    return found


def _day_end(day: dt.date, tz: dt.tzinfo | None) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.max, tzinfo=tz) if tz is not None else (
        dt.datetime.combine(day, dt.time.max).astimezone()
    )


def _range_dates(
    now: dt.datetime,
    *,
    month: str | None,
    from_date: str | None,
    to_date: str | None,
) -> tuple[dt.date, dt.date]:
    today = now.date()
    if month:
        try:
            start = dt.date.fromisoformat(f"{month}-01")
        except ValueError as exc:
            raise GrowattGuardError("--month must look like YYYY-MM.") from exc
        next_month = (
            dt.date(start.year + 1, 1, 1)
            if start.month == 12
            else dt.date(start.year, start.month + 1, 1)
        )
        if start > today:
            raise GrowattGuardError(f"{month} is in the future.")
        end = next_month - dt.timedelta(days=1) if next_month <= today else today
    else:
        try:
            end = dt.date.fromisoformat(to_date) if to_date else today
            start = dt.date.fromisoformat(from_date) if from_date else end.replace(day=1)
        except ValueError as exc:
            raise GrowattGuardError("--from and --to must look like YYYY-MM-DD.") from exc
        if end > today:
            raise GrowattGuardError("--to cannot be in the future.")
    if start > end:
        raise GrowattGuardError("The period start must be on or before its end.")
    return start, end


def _latest_reading(readings: list[dict[str, Any]], moment: dt.datetime) -> dict[str, Any] | None:
    found = None
    for reading in readings:
        if _at(reading) <= moment:
            found = reading
    return found


def _meter_period(
    events: list[dict[str, Any]],
    start: dt.date,
    end: dt.date,
    tz: dt.tzinfo | None,
) -> dict[str, Any]:
    """Consumption between the readings that bracket ``start`` and ``end``."""
    readings = [event for event in events if event["kind"] == "reading"]
    opening = _latest_reading(readings, _day_end(start, tz))
    closing = _latest_reading(readings, _day_end(end, tz))
    period: dict[str, Any] = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "opening": None,
        "closing": None,
        "opening_at": None,
        "closing_at": None,
        "purchased": None,
        "consumed": None,
        "topups": 0,
        "inverter_units": None,
        "outside_units": None,
        "inverter_days": None,
        "complete": False,
        "notes": [],
    }
    if opening is None:
        if closing is not None:
            period["closing"] = closing["balance"]
            period["closing_at"] = closing["at"]
        period["notes"].append(
            f"No meter reading on or before {start.isoformat()}: record a balance to size this period."
        )
        return period

    period["opening"] = opening["balance"]
    period["opening_at"] = opening["at"]
    if opening["at"][:10] != start.isoformat():
        period["notes"].append(
            f"The opening reading is {opening['at'][:10]}, not {start.isoformat()}; "
            "the total starts there."
        )
    if closing is None or _at(closing) <= _at(opening):
        period["notes"].append("A later reading is needed to close this period.")
        return period

    period["closing"] = closing["balance"]
    period["closing_at"] = closing["at"]
    if closing["at"][:10] < end.isoformat():
        period["notes"].append(
            f"The closing reading is {closing['at'][:10]}, the latest before {end.isoformat()}."
        )
    topups = [
        event
        for event in events
        if event["kind"] == "topup" and _at(opening) < _at(event) <= _at(closing)
    ]
    purchased = round(sum(event["units"] for event in topups), 1)
    consumed = round(opening["balance"] + purchased - closing["balance"], 1)
    period["purchased"] = purchased
    period["consumed"] = consumed
    period["topups"] = len(topups)
    period["complete"] = (
        opening["at"][:10] == start.isoformat() and closing["at"][:10] == end.isoformat()
    )
    if consumed < 0:
        period["notes"].append(
            "Logged top-ups do not explain the balance drop; a purchase may be missing."
        )
    return period


def _inverter_split(
    period: dict[str, Any],
    daily_rows: list[dict[str, Any]] | None,
) -> None:
    """Inverter grid energy over the whole days between the two readings."""
    if daily_rows is None or period["opening_at"] is None or period["closing_at"] is None:
        return
    opening_day = dt.date.fromisoformat(str(period["opening_at"])[:10])
    closing_day = dt.date.fromisoformat(str(period["closing_at"])[:10])
    expected = max(0, (closing_day - opening_day).days - 1)
    if expected == 0:
        return
    energy_by_day: dict[dt.date, float] = {}
    for row in daily_rows:
        try:
            day = dt.date.fromisoformat(str(row.get("date")))
        except ValueError:
            continue
        if not opening_day < day < closing_day:
            continue
        value = row.get("utility_wh")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        energy_by_day[day] = float(value)
    covered = len(energy_by_day)
    period["inverter_days"] = {"covered": covered, "expected": expected}
    if covered == 0:
        period["notes"].append("The daily ledger has no inverter grid energy for this period.")
        return
    inverter_units = round(sum(energy_by_day.values()) / 1000.0, 1)
    period["inverter_units"] = inverter_units
    if period["consumed"] is not None:
        period["outside_units"] = round(period["consumed"] - inverter_units, 1)
    if covered < expected:
        period["notes"].append(
            f"Inverter grid energy covers {covered} of {expected} whole days between the readings."
        )


def _months_touched(start: dt.date, end: dt.date) -> int:
    """Calendar months a period touches, since the fixed charge bills monthly."""
    count = 0
    cursor = start.replace(day=1)
    while cursor <= end:
        count += 1
        cursor = (
            dt.date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else dt.date(cursor.year, cursor.month + 1, 1)
        )
    return count


def _money_view(
    period: dict[str, Any],
    events: list[dict[str, Any]],
    tariff_rates: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Recorded cash out and the tariff cost of the period's consumption."""
    money: dict[str, Any] = {
        "paid": None,
        "paid_units": None,
        "paid_rate": None,
        "tariff": None,
        "units_cost": None,
        "fixed_months": None,
        "fixed_cost": None,
        "total": None,
        "cost_per_unit": None,
    }
    notes: list[str] = []
    if period["opening_at"] is None or period["closing_at"] is None:
        return money, notes
    opening_day = dt.date.fromisoformat(str(period["opening_at"])[:10])
    closing_day = dt.date.fromisoformat(str(period["closing_at"])[:10])
    opening_at = dt.datetime.fromisoformat(str(period["opening_at"]))
    closing_at = dt.datetime.fromisoformat(str(period["closing_at"]))

    paid_topups = [
        event
        for event in events
        if event["kind"] == "topup"
        and "cost" in event
        and opening_at < _at(event) <= closing_at
    ]
    if paid_topups:
        paid = sum(event["cost"] for event in paid_topups)
        paid_units = sum(event["units"] for event in paid_topups)
        money["paid"] = round(paid, 2)
        money["paid_units"] = round(paid_units, 1)
        if paid_units > 0:
            money["paid_rate"] = round(paid / paid_units, 2)

    tariff = tariff_at(tariff_rates, closing_day)
    if tariff is None:
        return money, notes
    money["tariff"] = {key: tariff[key] for key in ("from", "rate", "fixed")}
    if period["consumed"] is None:
        return money, notes

    units_cost = period["consumed"] * tariff["rate"]
    fixed_months = _months_touched(opening_day, closing_day)
    fixed_cost = fixed_months * tariff["fixed"]
    total = units_cost + fixed_cost
    money["units_cost"] = round(units_cost, 2)
    money["fixed_months"] = fixed_months
    money["fixed_cost"] = round(fixed_cost, 2)
    money["total"] = round(total, 2)
    if period["consumed"] > 0:
        money["cost_per_unit"] = round(total / period["consumed"], 2)

    for row in tariff_rates:
        changed = dt.date.fromisoformat(row["from"])
        if opening_day < changed <= closing_day:
            notes.append(
                f"The tariff changed on {row['from']}; the whole period is costed at the "
                f"rate effective on the closing reading."
            )
            break
    return money, notes


def _burn_view(
    events: list[dict[str, Any]],
    latest: dict[str, Any] | None,
    window_days: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Units per day between the latest reading and the last one before the window."""
    if latest is None:
        return None, None
    readings = [event for event in events if event["kind"] == "reading"]
    if len(readings) < 2:
        return None, "A second reading gives the burn rate and days of cover."
    target = _at(latest) - dt.timedelta(days=window_days)
    earlier = [reading for reading in readings if _at(reading) <= target]
    start = earlier[-1] if earlier else readings[0]
    if start is latest or _at(latest) <= _at(start):
        return None, "A second reading on another day is needed to measure the burn rate."
    days = (_at(latest) - _at(start)).total_seconds() / 86400.0
    if days < 0.5:
        return None, "The two readings are too close together to measure a daily burn rate."
    topups = [
        event
        for event in events
        if event["kind"] == "topup" and _at(start) < _at(event) <= _at(latest)
    ]
    consumed = start["balance"] + sum(event["units"] for event in topups) - latest["balance"]
    if consumed < 0:
        return None, "Logged top-ups do not explain the balance drop; a purchase may be missing."
    return (
        {
            "units_per_day": round(consumed / days, 1),
            "consumed": round(consumed, 1),
            "days": round(days, 1),
            "from": start["at"],
            "to": latest["at"],
        },
        None,
    )


def build_utility_report(
    events: list[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    tz: dt.tzinfo | None = None,
    month: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    daily_rows: list[dict[str, Any]] | None = None,
    tariff_rates: list[dict[str, Any]] | None = None,
    burn_window_days: float = DEFAULT_BURN_WINDOW_DAYS,
) -> dict[str, Any]:
    """Balance, burn rate, cover and the requested period's units and cost."""
    events = sorted(
        events, key=lambda event: (_at(event), 0 if event["kind"] == "topup" else 1)
    )
    local_now = _local_now(now, tz)
    start, end = _range_dates(local_now, month=month, from_date=from_date, to_date=to_date)
    readings = [event for event in events if event["kind"] == "reading"]
    latest = readings[-1] if readings else None
    period = _meter_period(events, start, end, tz)
    _inverter_split(period, daily_rows)
    money, money_notes = _money_view(period, events, tariff_rates or [])
    current_tariff = tariff_at(tariff_rates or [], local_now.date())
    next_tariff = next(
        (
            row
            for row in tariff_rates or []
            if dt.date.fromisoformat(row["from"]) > local_now.date()
        ),
        None,
    )

    burn, burn_note = _burn_view(events, latest, burn_window_days)
    notes: list[str] = list(period["notes"]) + money_notes
    if burn_note and burn_note not in notes:
        notes.append(burn_note)
    if not readings:
        notes.append("Record the meter balance with: utility-units reading <balance>.")

    current = None
    cover = None
    level = "unknown"
    if latest is not None:
        age_hours = max(0.0, (local_now - _at(latest)).total_seconds() / 3600.0)
        current = {
            "balance": latest["balance"],
            "at": latest["at"],
            "age_hours": round(age_hours, 1),
        }
        if burn is not None and burn["units_per_day"] > 0:
            cover_days = latest["balance"] / burn["units_per_day"]
            cover = {
                "days": round(cover_days, 1),
                "runs_out": (_at(latest) + dt.timedelta(days=cover_days)).date().isoformat(),
            }
            level = "watch" if cover_days < LOW_COVER_DAYS else "ok"
        else:
            level = "unknown"

    previous = None
    if month is not None or (from_date is None and to_date is None):
        previous_end = start - dt.timedelta(days=1)
        previous_start = previous_end.replace(day=1)
        same_day = previous_start.replace(day=min(end.day, previous_end.day))
        previous_period = _meter_period(events, previous_start, same_day, tz)
        if previous_period["consumed"] is not None:
            previous = {
                "from": previous_start.isoformat(),
                "to": same_day.isoformat(),
                "consumed": previous_period["consumed"],
                "purchased": previous_period["purchased"],
            }

    return {
        "generated_at": local_now.isoformat(timespec="seconds"),
        "ledger": {
            "events": len(events),
            "readings": len(readings),
            "topups": len(events) - len(readings),
            "first": events[0]["at"] if events else None,
            "last": events[-1]["at"] if events else None,
        },
        "current": current,
        "burn": burn,
        "cover": cover,
        "level": level,
        "range": {"from": start.isoformat(), "to": end.isoformat()},
        "period": period,
        "money": money,
        "tariff": current_tariff,
        "next_tariff": next_tariff,
        "previous": previous,
        "notes": notes,
    }


def _short_moment(value: Any) -> str:
    try:
        return dt.datetime.fromisoformat(str(value)).strftime("%d %b %H:%M")
    except (TypeError, ValueError):
        return "?"


def format_money(value: Any) -> str:
    """Naira amount without false precision; ``--`` when unknown."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "--"
    if float(value).is_integer():
        return f"{CURRENCY_SYMBOL}{value:,.0f}"
    return f"{CURRENCY_SYMBOL}{value:,.2f}".rstrip("0").rstrip(".")


def format_energy_rate(value: Any) -> str:
    return f"{format_money(value)}/kWh"


def _short_date(value: Any) -> str:
    try:
        return dt.date.fromisoformat(str(value)).strftime("%d %b")
    except (TypeError, ValueError):
        return "?"


def _units(value: Any, suffix: str = "units") -> str:
    if not isinstance(value, (int, float)):
        return "--"
    return f"{value:g} {suffix}".strip()


def format_utility_report(report: dict[str, Any]) -> str:
    """Terminal summary of ``build_utility_report``."""
    lines: list[str] = []
    current = report.get("current")
    if not current:
        lines.append("Utility units: no meter readings yet.")
        lines.append("Record the meter balance with: utility-units reading <balance>")
        return "\n".join(lines)

    lines.append(
        f"Balance   {_units(current['balance'])} · {_short_moment(current['at'])} "
        f"({current['age_hours']:g}h ago)"
    )
    burn = report.get("burn")
    if burn:
        lines.append(
            f"Burn      {_units(burn['units_per_day'], 'units/day')} "
            f"· {burn['consumed']:g} units over {burn['days']:g} days"
        )
    else:
        lines.append("Burn      not enough readings yet")
    cover = report.get("cover")
    if cover:
        lines.append(f"Cover     {cover['days']:g} days · runs out {_short_date(cover['runs_out'])}")
    else:
        lines.append("Cover     unknown without a measured burn rate")

    period = report["period"]
    if period["consumed"] is not None:
        header = f"Period    {period['from']} to {period['to']}: {_units(period['consumed'])} used"
        lines.append(header + f" · bought {_units(period['purchased'])}")
        split = []
        if period["inverter_units"] is not None:
            split.append(f"inverter {_units(period['inverter_units'])}")
            days = period["inverter_days"] or {}
            split.append(
                f"{days.get('covered', 0)} of {days.get('expected', 0)} whole days"
            )
        if period["outside_units"] is not None:
            split.append(f"outside {_units(period['outside_units'])}")
        if split:
            lines.append("          " + " · ".join(split))
        if period["opening_at"]:
            lines.append(
                f"          Opening {_units(period['opening'])} on {_short_moment(period['opening_at'])}"
                f" · closing {_units(period['closing'])} on {_short_moment(period['closing_at'])}"
            )
        previous = report.get("previous")
        if previous:
            lines.append(
                f"          Same days last month: {_units(previous['consumed'])} used"
            )
    else:
        lines.append(
            f"Period    {period['from']} to {period['to']}: not measurable yet"
        )
    money = report.get("money") or {}
    if money.get("paid") is not None:
        paid = (
            f"Paid      {format_money(money['paid'])} for {_units(money['paid_units'])}"
        )
        if money.get("paid_rate") is not None:
            paid += f" · {format_energy_rate(money['paid_rate'])}"
        lines.append(paid)
    if money.get("total") is not None:
        rate = money["tariff"]["rate"]
        lines.append(
            f"Cost      {_units(period['consumed'])} at {format_energy_rate(rate)} = "
            f"{format_money(money['units_cost'])}"
        )
        months = money["fixed_months"]
        lines.append(
            f"          fixed {format_money(money['fixed_cost'])} "
            f"({months} month{'s' if months != 1 else ''}) · "
            f"total {format_money(money['total'])}"
            + (
                f" ({format_energy_rate(money['cost_per_unit'])} used)"
                if money.get("cost_per_unit") is not None
                else ""
            )
        )
    for note in report.get("notes") or []:
        lines.append(f"Note      {note}")
    ledger = report["ledger"]
    lines.append(
        f"Ledger    {ledger['events']} events · {ledger['readings']} readings · "
        f"{ledger['topups']} top-up{'s' if ledger['topups'] != 1 else ''}"
    )
    return "\n".join(lines)


def format_utility_events(events: list[dict[str, Any]]) -> str:
    """One line per ledger event, with the index ``delete`` takes."""
    if not events:
        return "Utility units ledger is empty."
    rows = [f" {'#':>2}  {'WHEN':<16} {'KIND':<8} {'AMOUNT':>9}  NOTE"]
    for index, event in enumerate(events, start=1):
        amount = event.get("balance") if event["kind"] == "reading" else event.get("units")
        label = "balance" if event["kind"] == "reading" else "bought"
        rows.append(
            f" {index:>2}  {_short_moment(event['at']):<16} {event['kind']:<8} "
            f"{amount:>7g} {label:<2}  {event.get('note', '')}".rstrip()
        )
    return "\n".join(rows)


def format_utility_tariff(rates: list[dict[str, Any]]) -> str:
    if not rates:
        return "No tariff configured. Set one with: utility-units tariff --rate 228 --fixed 120000"
    rows = [f" {'FROM':<12} {'RATE':>10}  {'FIXED/MONTH':>12}  NOTE"]
    for row in rates:
        rows.append(
            f" {row['from']:<12} {format_energy_rate(row['rate']):>10}  "
            f"{format_money(row['fixed']):>12}  {row.get('note', '')}".rstrip()
        )
    return "\n".join(rows)


def command_utility_units(config: Any, args: Any, now: dt.datetime | None = None) -> int:
    """CLI entry point for ``utility-units``."""
    from growatt_guard.load_learning import resolve_timezone

    action = str(getattr(args, "utility_action", ""))
    if action == "tariff":
        if getattr(args, "list", False):
            rates = read_utility_tariff()
            if args.json:
                print(json.dumps(
                    {"schema_version": TARIFF_SCHEMA_VERSION, "rates": rates},
                    indent=2,
                    sort_keys=True,
                ))
            else:
                print(format_utility_tariff(rates))
            return 0
        if args.rate is None or args.fixed is None:
            raise GrowattGuardError("Provide both --rate and --fixed to record a tariff.")
        effective = args.from_date or dt.date.today().isoformat()
        record_utility_tariff(
            args.rate,
            args.fixed,
            from_date=effective,
            note=args.note,
        )
        print(
            f"Recorded tariff from {effective}: {format_energy_rate(args.rate)} · "
            f"fixed {format_money(args.fixed)}/month."
        )
        return 0
    if action == "topup":
        event = record_topup(
            args.units,
            cost=args.cost,
            at=args.at,
            note=args.note,
        )
        suffix = f" (cost {event['cost']:g})" if "cost" in event else ""
        print(f"Recorded top-up: {event['units']:g} units on {_short_moment(event['at'])}{suffix}.")
        return 0
    if action == "reading":
        event = record_reading(args.balance, at=args.at, note=args.note)
        print(f"Recorded reading: {event['balance']:g} units on {_short_moment(event['at'])}.")
        return 0
    if action == "delete":
        removed = delete_event(args.index)
        amount = removed.get("balance") if removed["kind"] == "reading" else removed.get("units")
        print(f"Removed {removed['kind']} of {amount:g} units on {_short_moment(removed['at'])}.")
        return 0

    events = read_utility_units()
    if action == "list":
        if args.json:
            print(json.dumps(events, indent=2, sort_keys=True))
        else:
            print(format_utility_events(events))
        return 0

    tz = resolve_timezone(getattr(config, "weather_timezone", ""))
    report = build_utility_report(
        events,
        now=now,
        tz=tz,
        month=args.month,
        from_date=args.from_date,
        to_date=args.to_date,
        daily_rows=_read_daily_rows(),
        tariff_rates=read_utility_tariff(),
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_utility_report(report))
    return 0


def _read_daily_rows() -> list[dict[str, Any]]:
    from growatt_guard.daily_generation import read_daily_generation

    return read_daily_generation()
