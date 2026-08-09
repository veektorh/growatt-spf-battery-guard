from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from growatt_guard.audit import parse_audit_float, parse_audit_timestamp, parse_topup_minutes


_COMPLETION_ACTIONS = {
    "topup-target-reached",
    "topup-expired",
}
_NEAR_TARGET_TOLERANCE_SOC = 2.0
_LOAD_OVERRUN_FACTOR = 1.35
_LOW_CHARGE_RATE_FACTOR = 0.55
_MIN_COMPARABLE_OUTCOMES = 3
_METRIC_ENDPOINT_TOLERANCE_MINUTES = 15.0
_METRIC_MAX_GAP_MINUTES = 20.0


@dataclass(frozen=True)
class TopupOutcome:
    started_at: str
    completed_at: str
    closure: str
    classification: str
    grid_use_class: str
    plan_kind: str
    planned_minutes: float | None
    actual_minutes: float | None
    start_soc: float | None
    end_soc: float | None
    target_soc: float | None
    soc_gain: float | None
    planned_load_w: float | None
    average_load_w: float | None
    planned_charge_rate_w: float | None
    implied_charge_rate_w: float | None
    grid_import_kwh: float | None
    metric_coverage_complete: bool


@dataclass(frozen=True)
class TopupScorecard:
    outcomes: tuple[TopupOutcome, ...]
    closure_counts: dict[str, int]
    classification_counts: dict[str, int]
    grid_use_counts: dict[str, int]
    comparable_count: int
    minimum_comparable_count: int
    recommendation_status: str
    recommendation: str
    average_planned_minutes: float | None
    average_actual_minutes: float | None
    average_soc_gain: float | None
    measured_grid_import_kwh: float | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["outcomes"] = [asdict(item) for item in self.outcomes]
        return payload


def _number_from_note(note: str, key: str) -> float | None:
    match = re.search(rf"(?:^|[,;\s]){re.escape(key)}=([-+]?\d+(?:\.\d+)?)", note)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _text_from_note(note: str, key: str) -> str:
    match = re.search(rf"(?:^|[,;\s]){re.escape(key)}=([a-z0-9-]+)", note, re.IGNORECASE)
    return match.group(1).lower() if match else "unknown"


def _naive_local(value: dt.datetime) -> dt.datetime:
    return value.astimezone().replace(tzinfo=None) if value.tzinfo is not None else value


def _metric_timestamp(row: dict[str, Any]) -> dt.datetime | None:
    try:
        return _naive_local(dt.datetime.fromisoformat(str(row.get("timestamp", ""))))
    except ValueError:
        return None


def _metric_number(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _window_metrics(
    history: list[dict[str, Any]],
    started_at: dt.datetime,
    completed_at: dt.datetime,
) -> tuple[float | None, float | None, bool]:
    samples = [
        (timestamp, row)
        for row in history
        if (timestamp := _metric_timestamp(row)) is not None
        and started_at - dt.timedelta(minutes=_METRIC_ENDPOINT_TOLERANCE_MINUTES)
        <= timestamp
        <= completed_at + dt.timedelta(minutes=_METRIC_ENDPOINT_TOLERANCE_MINUTES)
    ]
    samples.sort(key=lambda item: item[0])
    if len(samples) < 2:
        return None, None, False

    before_start = [item for item in samples if item[0] <= started_at]
    after_end = [item for item in samples if item[0] >= completed_at]
    first = before_start[-1] if before_start else samples[0]
    last = after_end[0] if after_end else samples[-1]
    bounded = [item for item in samples if first[0] <= item[0] <= last[0]]
    first_at = first[0]
    last_at = last[0]
    endpoint_covered = (
        abs((first_at - started_at).total_seconds()) / 60.0
        <= _METRIC_ENDPOINT_TOLERANCE_MINUTES
        and abs((last_at - completed_at).total_seconds()) / 60.0
        <= _METRIC_ENDPOINT_TOLERANCE_MINUTES
    )
    gaps_covered = all(
        (right[0] - left[0]).total_seconds() / 60.0 <= _METRIC_MAX_GAP_MINUTES
        for left, right in zip(bounded, bounded[1:])
    )
    complete = endpoint_covered and gaps_covered

    in_window = [
        row for timestamp, row in bounded if started_at <= timestamp <= completed_at
    ]
    loads = [
        value
        for row in in_window
        if (value := _metric_number(row, "load_w")) is not None
    ]
    average_load = sum(loads) / len(loads) if loads else None

    counters = [
        (timestamp, value)
        for timestamp, row in bounded
        if (value := _metric_number(row, "grid_today_kwh")) is not None
    ]
    grid_import = None
    if complete and len(counters) >= 2:
        total = 0.0
        for (left_at, left), (right_at, right) in zip(counters, counters[1:]):
            total += max(0.0, right if right_at.date() != left_at.date() else right - left)
        grid_import = round(total, 3)
    return average_load, grid_import, complete


def _classify(
    closure: str,
    *,
    start_soc: float | None,
    end_soc: float | None,
    target_soc: float | None,
    planned_load_w: float | None,
    average_load_w: float | None,
    planned_charge_rate_w: float | None,
    implied_charge_rate_w: float | None,
) -> str:
    if closure == "topup-target-reached":
        return "target-reached"
    if closure != "topup-expired" or end_soc is None or target_soc is None:
        return "unknown"
    if end_soc >= target_soc - _NEAR_TARGET_TOLERANCE_SOC:
        return "near-target-expiry"
    if (
        planned_load_w is not None
        and planned_load_w > 0
        and average_load_w is not None
        and average_load_w >= planned_load_w * _LOAD_OVERRUN_FACTOR
    ):
        return "load-overrun"
    soc_gain = end_soc - start_soc if start_soc is not None else None
    if soc_gain is not None and soc_gain <= 1.0:
        return "insufficient-utility-charge"
    if (
        planned_charge_rate_w is not None
        and planned_charge_rate_w > 0
        and implied_charge_rate_w is not None
        and implied_charge_rate_w < planned_charge_rate_w * _LOW_CHARGE_RATE_FACTOR
    ):
        return "insufficient-utility-charge"
    return "unknown"


def _grid_use_class(classification: str, plan_kind: str) -> str:
    if classification in {"load-overrun", "insufficient-utility-charge"}:
        return "inefficient"
    if plan_kind == "adopted":
        return "adopted-hold"
    if plan_kind == "late-safety":
        return "expected-safety-hold"
    if classification in {"target-reached", "near-target-expiry"}:
        return "planned-reserve"
    return "unknown"


def _comparable_cohort(outcomes: list[TopupOutcome]) -> list[TopupOutcome]:
    candidates = [
        item
        for item in outcomes
        if item.classification != "unknown"
        and item.target_soc is not None
        and item.planned_load_w is not None
        and item.planned_load_w > 0
    ]
    best: list[TopupOutcome] = []
    for anchor in candidates:
        cohort = [
            item
            for item in candidates
            if item.plan_kind == anchor.plan_kind
            and abs(float(item.target_soc) - float(anchor.target_soc)) <= 2.0
            and 0.75
            <= float(item.planned_load_w) / float(anchor.planned_load_w)
            <= 1.25
        ]
        if len(cohort) > len(best):
            best = cohort
    return best


def _recommendation(cohort: list[TopupOutcome]) -> tuple[str, str]:
    if len(cohort) < _MIN_COMPARABLE_OUTCOMES:
        remaining = _MIN_COMPARABLE_OUTCOMES - len(cohort)
        return (
            "hold",
            f"Need {remaining} more comparable completed top-up"
            f"{'s' if remaining != 1 else ''} before changing targets, charge rate, reserve margin, or thresholds.",
        )
    counts = Counter(item.classification for item in cohort)
    if counts["load-overrun"] >= 2:
        return (
            "review-load",
            "Comparable outcomes show repeated load overruns; review the learned overnight load and reserve margin before changing the SOC target.",
        )
    if counts["insufficient-utility-charge"] >= 2:
        return (
            "review-charge",
            "Comparable outcomes show insufficient charging; verify Utility availability and the learned charge rate before changing reserve thresholds.",
        )
    successful = counts["target-reached"] + counts["near-target-expiry"]
    if successful == len(cohort):
        return (
            "keep-current",
            "Comparable outcomes support the current top-up target, charge-rate estimate, reserve margin, and thresholds; keep them unchanged.",
        )
    return (
        "hold",
        "Comparable outcomes are mixed; hold current settings until one pattern repeats clearly.",
    )


def build_topup_scorecard(
    rows: list[dict[str, str]],
    history: list[dict[str, Any]],
    *,
    configured_charge_rate_w: float,
) -> TopupScorecard:
    def timestamp_key(row: dict[str, str]) -> dt.datetime:
        parsed = parse_audit_timestamp(row.get("timestamp", ""))
        return _naive_local(parsed) if parsed is not None else dt.datetime.min

    ordered = sorted(rows, key=timestamp_key)
    pending_start: dict[str, str] | None = None
    outcomes: list[TopupOutcome] = []
    for row in ordered:
        action = row.get("action", "")
        if action == "auto-topup-started":
            pending_start = row
            continue
        if action not in _COMPLETION_ACTIONS:
            continue

        start_note = pending_start.get("note", "") if pending_start else ""
        completion_note = row.get("note", "")
        completion_planned_minutes = _number_from_note(
            completion_note, "planned_min"
        )
        if pending_start is None and completion_planned_minutes is None:
            # Preserve-battery Utility holds share the completion command but
            # are not auto-topups. Older ambiguous closures fail closed here.
            continue
        started = (
            parse_audit_timestamp(pending_start.get("timestamp", ""))
            if pending_start
            else None
        )
        completed = parse_audit_timestamp(row.get("timestamp", ""))
        actual_minutes = _number_from_note(completion_note, "actual_min")
        if started is None and completed is not None and actual_minutes is not None:
            started = completed - dt.timedelta(minutes=actual_minutes)
        if started is None or completed is None:
            pending_start = None
            continue
        started = _naive_local(started)
        completed = _naive_local(completed)
        if actual_minutes is None:
            actual_minutes = max(0.0, (completed - started).total_seconds() / 60.0)

        planned_minutes = _number_from_note(start_note, "planned_min")
        if planned_minutes is None and pending_start is not None:
            parsed_minutes = parse_topup_minutes(pending_start)
            planned_minutes = float(parsed_minutes) if parsed_minutes is not None else None
        if planned_minutes is None:
            planned_minutes = completion_planned_minutes
        start_soc = _number_from_note(completion_note, "start_soc")
        if start_soc is None and pending_start is not None:
            start_soc = parse_audit_float(pending_start, "soc")
        end_soc = _number_from_note(completion_note, "end_soc")
        if end_soc is None:
            end_soc = parse_audit_float(row, "soc")
        target_soc = _number_from_note(completion_note, "target_soc")
        if target_soc is None:
            target_soc = _number_from_note(start_note, "target_soc")
        planned_load_w = _number_from_note(start_note, "planned_load_w")
        if planned_load_w is None:
            planned_load_w = _number_from_note(completion_note, "planned_load_w")
        planned_charge_rate_w = _number_from_note(start_note, "planned_charge_rate_w")
        if planned_charge_rate_w is None:
            planned_charge_rate_w = _number_from_note(completion_note, "planned_charge_rate_w")
        if planned_charge_rate_w is None and configured_charge_rate_w > 0:
            planned_charge_rate_w = configured_charge_rate_w
        implied_charge_rate_w = _number_from_note(completion_note, "implied_rate_w")
        plan_kind = _text_from_note(start_note, "plan_kind")
        if plan_kind == "unknown":
            plan_kind = _text_from_note(completion_note, "plan_kind")
        average_load_w, grid_import_kwh, metric_complete = _window_metrics(
            history, started, completed
        )
        classification = _classify(
            action,
            start_soc=start_soc,
            end_soc=end_soc,
            target_soc=target_soc,
            planned_load_w=planned_load_w,
            average_load_w=average_load_w,
            planned_charge_rate_w=planned_charge_rate_w,
            implied_charge_rate_w=implied_charge_rate_w,
        )
        outcomes.append(
            TopupOutcome(
                started_at=started.isoformat(timespec="seconds"),
                completed_at=completed.isoformat(timespec="seconds"),
                closure=action,
                classification=classification,
                grid_use_class=_grid_use_class(classification, plan_kind),
                plan_kind=plan_kind,
                planned_minutes=planned_minutes,
                actual_minutes=actual_minutes,
                start_soc=start_soc,
                end_soc=end_soc,
                target_soc=target_soc,
                soc_gain=(end_soc - start_soc) if end_soc is not None and start_soc is not None else None,
                planned_load_w=planned_load_w,
                average_load_w=average_load_w,
                planned_charge_rate_w=planned_charge_rate_w,
                implied_charge_rate_w=implied_charge_rate_w,
                grid_import_kwh=grid_import_kwh,
                metric_coverage_complete=metric_complete,
            )
        )
        pending_start = None

    cohort = _comparable_cohort(outcomes)
    recommendation_status, recommendation = _recommendation(cohort)
    planned = [item.planned_minutes for item in outcomes if item.planned_minutes is not None]
    actual = [item.actual_minutes for item in outcomes if item.actual_minutes is not None]
    gains = [item.soc_gain for item in outcomes if item.soc_gain is not None]
    grid = [item.grid_import_kwh for item in outcomes if item.grid_import_kwh is not None]
    return TopupScorecard(
        outcomes=tuple(outcomes),
        closure_counts=dict(Counter(item.closure for item in outcomes)),
        classification_counts=dict(Counter(item.classification for item in outcomes)),
        grid_use_counts=dict(Counter(item.grid_use_class for item in outcomes)),
        comparable_count=len(cohort),
        minimum_comparable_count=_MIN_COMPARABLE_OUTCOMES,
        recommendation_status=recommendation_status,
        recommendation=recommendation,
        average_planned_minutes=sum(planned) / len(planned) if planned else None,
        average_actual_minutes=sum(actual) / len(actual) if actual else None,
        average_soc_gain=sum(gains) / len(gains) if gains else None,
        measured_grid_import_kwh=round(sum(grid), 3) if grid else None,
    )
