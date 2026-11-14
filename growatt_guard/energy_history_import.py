"""Fill past days' load and Utility energy in the daily ledger from a Growatt chart export.

The ledger records load and Utility only from the day it started capturing them, and
the Growatt app API has no per-day history for storage plants. The Growatt website's
Energy Trend chart does: its month view (``/panel/storage/getStorage6000EnergyMonthChart``)
returns one value per day for PV (``ppv``), load (``elocalLoad``) and grid import
(``pacToUser``). Save those responses as ``{"YYYY-MM": charts, ...}`` and import them here.

The import only fills ``load_wh`` and ``utility_wh`` where a ledger day has none. It never
changes a value the ledger already recorded, reports days where the export's solar
disagrees with the ledger, and backs the ledger up before writing. ``generated_wh``
changes only with the explicit ``correct_solar`` option, and then only on the reported
days, keeping the old value in ``previous_wh`` as ``solar-reconcile`` does.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from growatt_guard.daily_generation import (
    DAILY_GENERATION_FILE,
    _write_daily_generation,
    read_daily_generation,
)
from growatt_guard.exceptions import GrowattGuardError

IMPORT_SOURCE = "growatt-web-chart"
# Export and ledger both round to 0.1 kWh; anything beyond a step is a real disagreement.
SOLAR_MISMATCH_WH = 100


@dataclass(frozen=True)
class ExportDay:
    solar_wh: int
    load_wh: int
    utility_wh: int


@dataclass
class ImportPlan:
    fills: dict[str, dict[str, int]] = field(default_factory=dict)
    solar_mismatches: list[tuple[str, int, int]] = field(default_factory=list)
    kept: list[tuple[str, str, int, int]] = field(default_factory=list)
    not_in_ledger: list[str] = field(default_factory=list)
    no_data: list[str] = field(default_factory=list)


def _wh(value: Any, where: str) -> int:
    try:
        kwh = float(value)
    except (TypeError, ValueError) as exc:
        raise GrowattGuardError(f"{where}: expected a number, got {value!r}.") from exc
    if kwh < 0:
        raise GrowattGuardError(f"{where}: energy cannot be negative.")
    return round(kwh * 1000)


def parse_chart_export(payload: Any) -> dict[str, ExportDay]:
    """Turn ``{"YYYY-MM": {"ppv": [...], "elocalLoad": [...], "pacToUser": [...]}}`` into days."""
    if not isinstance(payload, dict) or not payload:
        raise GrowattGuardError("Energy history export must be a non-empty object keyed by YYYY-MM.")
    days: dict[str, ExportDay] = {}
    for month, charts in payload.items():
        try:
            first = dt.date.fromisoformat(f"{month}-01")
        except ValueError as exc:
            raise GrowattGuardError(f"Energy history export: {month!r} is not a YYYY-MM month.") from exc
        if not isinstance(charts, dict):
            raise GrowattGuardError(f"Energy history export {month}: expected the chart object.")
        series = {}
        for key in ("ppv", "elocalLoad", "pacToUser"):
            values = charts.get(key)
            if not isinstance(values, list):
                raise GrowattGuardError(f"Energy history export {month}: missing the {key} series.")
            series[key] = values
        lengths = {len(values) for values in series.values()}
        if len(lengths) != 1:
            raise GrowattGuardError(f"Energy history export {month}: series lengths differ.")
        for index in range(lengths.pop()):
            try:
                day = first.replace(day=index + 1)
            except ValueError as exc:
                raise GrowattGuardError(f"Energy history export {month}: more values than days.") from exc
            where = f"Energy history export {day.isoformat()}"
            days[day.isoformat()] = ExportDay(
                solar_wh=_wh(series["ppv"][index], where),
                load_wh=_wh(series["elocalLoad"][index], where),
                utility_wh=_wh(series["pacToUser"][index], where),
            )
    return days


def build_import_plan(
    rows: list[dict[str, Any]],
    export: dict[str, ExportDay],
    *,
    today: dt.date,
) -> ImportPlan:
    plan = ImportPlan()
    ledger = {str(row["date"]): row for row in rows}
    for date, day in sorted(export.items()):
        if date >= today.isoformat():
            continue
        row = ledger.get(date)
        if row is None:
            if day.solar_wh or day.load_wh or day.utility_wh:
                plan.not_in_ledger.append(date)
            continue
        # A house always draws something, so zero load means the export has no data that day.
        if day.load_wh == 0:
            plan.no_data.append(date)
            continue
        if abs(int(row["generated_wh"]) - day.solar_wh) > SOLAR_MISMATCH_WH:
            plan.solar_mismatches.append((date, int(row["generated_wh"]), day.solar_wh))
        fill: dict[str, int] = {}
        for ledger_field, value in (("load_wh", day.load_wh), ("utility_wh", day.utility_wh)):
            existing = row.get(ledger_field)
            if existing is None:
                fill[ledger_field] = value
            elif abs(int(existing) - value) > SOLAR_MISMATCH_WH:
                plan.kept.append((date, ledger_field, int(existing), value))
        if fill:
            plan.fills[date] = fill
    return plan


def apply_import_plan(
    plan: ImportPlan,
    *,
    correct_solar: bool = False,
    path: Path | None = None,
    now: dt.datetime | None = None,
) -> Path | None:
    """Write the fills (and, if asked, the solar corrections) after copying the ledger aside.

    Returns the backup path, or None when there is nothing to write.
    """
    corrections = {date: export_wh for date, _, export_wh in plan.solar_mismatches} if correct_solar else {}
    if not plan.fills and not corrections:
        return None
    moment = now or dt.datetime.now(dt.timezone.utc)
    target = path or DAILY_GENERATION_FILE
    rows = read_daily_generation(target)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    backup = target.with_name(f"{target.name}.bak-{stamp}")
    shutil.copy2(target, backup)
    for row in rows:
        date = str(row["date"])
        fill = plan.fills.get(date)
        if fill:
            for ledger_field, value in fill.items():
                if row.get(ledger_field) is None:
                    row[ledger_field] = value
            row["energy_import_source"] = IMPORT_SOURCE
        if date in corrections:
            row["previous_wh"] = int(row["generated_wh"])
            row["generated_wh"] = corrections[date]
            row["source"] = IMPORT_SOURCE
            row["corrected_at"] = moment.isoformat()
    _write_daily_generation(rows, target)
    return backup


def _kwh(wh: int) -> str:
    return f"{wh / 1000:.1f} kWh"


def command_import_energy_history(
    input_path: str,
    apply: bool = False,
    correct_solar: bool = False,
    *,
    today: dt.date | None = None,
    ledger_path: Path | None = None,
) -> int:
    source = Path(input_path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GrowattGuardError(f"Could not read energy history export {source}: {exc}") from exc
    export = parse_chart_export(payload)
    rows = read_daily_generation(ledger_path)
    plan = build_import_plan(rows, export, today=today or dt.date.today())

    print(f"Export: {len(export)} days. Ledger: {len(rows)} days.")
    print(f"Days to fill with load and Utility: {len(plan.fills)}")
    if plan.fills:
        first, last = min(plan.fills), max(plan.fills)
        load = sum(fill.get("load_wh", 0) for fill in plan.fills.values())
        utility = sum(fill.get("utility_wh", 0) for fill in plan.fills.values())
        print(f"  {first} to {last}: load {_kwh(load)}, Utility {_kwh(utility)}")
    outcome = "corrected to the export" if correct_solar else "ledger kept; --correct-solar replaces it"
    for date, ledger_wh, export_wh in plan.solar_mismatches:
        print(f"Solar differs on {date}: ledger {_kwh(ledger_wh)}, export {_kwh(export_wh)} ({outcome})")
    for date, ledger_field, existing, value in plan.kept:
        print(f"Recorded {ledger_field} kept on {date}: ledger {_kwh(existing)}, export {_kwh(value)}")
    if plan.not_in_ledger:
        print(f"Export days missing from the ledger (not added): {', '.join(plan.not_in_ledger)}")
    if plan.no_data:
        print(f"Export days without load data (skipped): {', '.join(plan.no_data)}")

    if not apply:
        print("Preview only; re-run with --apply to write.")
        return 0
    backup = apply_import_plan(plan, correct_solar=correct_solar, path=ledger_path)
    if backup is None:
        print("Nothing to write.")
        return 0
    corrected = len(plan.solar_mismatches) if correct_solar else 0
    print(f"Filled {len(plan.fills)} days, corrected solar on {corrected}. Ledger backup: {backup}")
    return 0
