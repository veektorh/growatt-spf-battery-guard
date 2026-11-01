from __future__ import annotations

import datetime as dt
from typing import Any

from growatt_guard.daily_generation import (
    _write_daily_generation,
    read_daily_generation,
)
from growatt_guard.notifications import embed_summary, send_discord_embed


RECONCILE_TOLERANCE_KWH = 0.2
RECONCILE_TOLERANCE_RATIO = 0.02


def build_reconciliation(
    ledger: dict[str, int],
    pvoutput: dict[str, int],
    *,
    tolerance_kwh: float = RECONCILE_TOLERANCE_KWH,
    tolerance_ratio: float = RECONCILE_TOLERANCE_RATIO,
) -> dict[str, Any]:
    """Compare finalized daily generation with the PVOutput record for the same days.

    PVOutput rounds to whole watt-hours, so a small absolute tolerance keeps rounding
    noise out; anything larger than ``tolerance_ratio`` of the ledger value is drift.
    Days missing from the ledger are reported as gaps rather than conflicts.
    """
    tolerance_wh = max(tolerance_kwh * 1000.0, 0.0)
    missing: list[tuple[str, int]] = []
    conflicts: list[tuple[str, int, int]] = []
    matching = 0
    for day in sorted(pvoutput):
        reference = int(pvoutput[day])
        current = ledger.get(day)
        if current is None:
            missing.append((day, reference))
            continue
        allowed = max(tolerance_wh, abs(current) * tolerance_ratio)
        if abs(current - reference) <= allowed:
            matching += 1
        else:
            conflicts.append((day, int(current), reference))
    return {
        "days_compared": len(pvoutput),
        "matching": matching,
        "missing": tuple(missing),
        "conflicts": tuple(conflicts),
        "ledger_delta_wh": sum(
            reference - ledger[day] if day in ledger else reference
            for day, reference in pvoutput.items()
        ),
    }


def _apply_reconciliation(
    missing: tuple[tuple[str, int], ...],
    conflicts: tuple[tuple[str, int, int], ...],
) -> int:
    if not missing and not conflicts:
        return 0
    rows = read_daily_generation()
    by_date = {str(row["date"]): row for row in rows}
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    changed = 0
    for day, reference in missing:
        by_date[day] = {
            "date": day,
            "generated_wh": int(reference),
            "source": "pvoutput",
            "finalized_at": stamp,
        }
        changed += 1
    for day, current, reference in conflicts:
        row = by_date.get(day)
        if row is None:
            continue
        row["previous_wh"] = int(current)
        row["generated_wh"] = int(reference)
        row["source"] = "pvoutput"
        row["corrected_at"] = stamp
        changed += 1
    if changed:
        _write_daily_generation([by_date[key] for key in sorted(by_date)])
    return changed


def command_solar_reconcile(
    config: Any,
    *,
    days: int = 7,
    apply: bool = True,
    notify: bool = False,
) -> int:
    """Reconcile the finalized solar ledger with PVOutput and repair any drift."""
    if days < 1:
        days = 1
    if not config.pvoutput_enabled:
        print("PVOutput is not enabled; nothing to reconcile.")
        return 0
    from growatt_guard.pvoutput import fetch_pvoutput_daily_outputs

    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days - 1)
    reference = fetch_pvoutput_daily_outputs(config, start, end)
    if not reference:
        print(f"PVOutput returned no daily outputs for {start.isoformat()}..{end.isoformat()}; skipping.")
        return 0

    ledger = {str(row["date"]): int(row["generated_wh"]) for row in read_daily_generation()}
    plan = build_reconciliation(ledger, reference)
    missing = plan["missing"]
    conflicts = plan["conflicts"]
    if not missing and not conflicts:
        print(
            f"Solar ledger matches PVOutput for {plan['matching']} day(s) "
            f"({start.isoformat()}..{end.isoformat()})."
        )
        return 0

    lines = [f"Window: {start.isoformat()} to {end.isoformat()}"]
    for day, reference_wh in missing:
        lines.append(f"missing {day}: PVOutput {reference_wh / 1000:.1f} kWh")
    for day, current_wh, reference_wh in conflicts:
        lines.append(
            f"drift {day}: ledger {current_wh / 1000:.1f} kWh vs PVOutput {reference_wh / 1000:.1f} kWh"
        )
    if apply and not getattr(config, "dry_run", False):
        changed = _apply_reconciliation(missing, conflicts)
        lines.append(f"Repaired {changed} ledger row(s).")
    elif apply:
        lines.append("DRY_RUN is enabled; nothing written.")
    else:
        lines.append("Preview only; nothing written.")
    summary = "Solar ledger reconciliation\n\n" + "\n".join(lines)
    if notify and config.discord_webhook_url:
        send_discord_embed(config, embed_summary("Solar Ledger Reconciliation", summary))
    print(summary)
    return 0
