from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from growatt_guard.audit import parse_audit_float, parse_audit_timestamp
from growatt_guard.weather import parse_forecast_time


_MIN_COMPARABLE_OUTCOMES = 3
_METRIC_ENDPOINT_TOLERANCE_MINUTES = 20.0
_METRIC_MAX_GAP_MINUTES = 20.0


@dataclass(frozen=True)
class SolarBridgeDecision:
    status: str
    reason: str
    forecast_pv_kwh: float | None = None
    forecast_load_kwh: float | None = None
    forecast_factor: float | None = None
    projected_min_soc: float | None = None
    projected_recovery_soc: float | None = None

    @property
    def eligible(self) -> bool:
        return self.status == "eligible"


@dataclass(frozen=True)
class PreservationOutcome:
    started_at: str
    returned_at: str | None
    recovery_at: str
    classification: str
    bridge_status: str
    start_soc: float | None
    target_soc: float | None
    counterfactual_min_soc: float | None
    counterfactual_recovery_soc: float | None
    grid_import_kwh: float | None
    metric_coverage_complete: bool


@dataclass(frozen=True)
class PreservationScorecard:
    outcomes: tuple[PreservationOutcome, ...]
    classification_counts: dict[str, int]
    comparable_count: int
    minimum_comparable_count: int
    recommendation_status: str
    recommendation: str
    measured_grid_import_kwh: float | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["outcomes"] = [asdict(item) for item in self.outcomes]
        return payload


def build_solar_bridge_decision(
    *,
    forecast: dict[str, Any] | None,
    now: dt.datetime,
    soc: float,
    target_soc: float,
    safety_floor_soc: float,
    start_hour: int,
    recovery_hour: int,
    load_w: float | None,
    load_factor: float,
    battery_capacity_wh: float,
    panel_kwp: float,
    performance_ratio: float,
    solar_factor: float = 1.0,
) -> SolarBridgeDecision:
    """Project whether forecast PV can safely bridge a morning SOC deficit.

    The calculation is deliberately conservative: current load is held flat and
    uplifted for every interval. Any missing prerequisite or incomplete hourly
    radiation coverage fails closed.
    """
    local_now = now.replace(tzinfo=None) if now.tzinfo is not None else now
    recovery_at = local_now.replace(hour=recovery_hour, minute=0, second=0, microsecond=0)
    if local_now.hour < start_hour or local_now >= recovery_at:
        return SolarBridgeDecision("ineligible", "outside the morning recovery window")
    if soc < safety_floor_soc:
        return SolarBridgeDecision(
            "ineligible",
            f"SOC {soc:g}% is below the {safety_floor_soc:g}% bridge safety floor",
        )
    if (
        not isinstance(forecast, dict)
        or load_w is None
        or load_w <= 0
        or battery_capacity_wh <= 0
        or panel_kwp <= 0
        or performance_ratio <= 0
    ):
        return SolarBridgeDecision("unavailable", "bridge inputs are incomplete")

    hourly = forecast.get("hourly") if isinstance(forecast.get("hourly"), dict) else {}
    times = hourly.get("time", [])
    radiation = hourly.get("shortwave_radiation", [])
    if not times or not radiation:
        return SolarBridgeDecision("unavailable", "hourly solar radiation is unavailable")

    expected_hours = (recovery_at - local_now).total_seconds() / 3600.0
    covered_hours = 0.0
    forecast_pv_kwh = 0.0
    forecast_load_kwh = 0.0
    current_soc = soc
    minimum_soc = soc
    conservative_load_w = load_w * max(1.0, load_factor)

    segments_by_start: dict[dt.datetime, float] = {}
    for index, raw_time in enumerate(times):
        if index >= len(radiation) or radiation[index] is None:
            continue
        try:
            starts_at = parse_forecast_time(str(raw_time))
            radiation_w_m2 = max(0.0, float(radiation[index]))
        except (TypeError, ValueError):
            continue
        segments_by_start[starts_at] = radiation_w_m2

    covered_until = local_now
    for starts_at, radiation_w_m2 in sorted(segments_by_start.items()):
        ends_at = starts_at + dt.timedelta(hours=1)
        overlap_start = max(local_now, starts_at)
        overlap_end = min(recovery_at, ends_at)
        if overlap_end <= overlap_start:
            continue
        if overlap_start > covered_until + dt.timedelta(minutes=1):
            return SolarBridgeDecision("unavailable", "hourly solar coverage is incomplete")
        overlap_start = max(overlap_start, covered_until)
        if overlap_end <= overlap_start:
            continue
        hours = (overlap_end - overlap_start).total_seconds() / 3600.0
        covered_hours += hours
        covered_until = overlap_end
        pv_kwh = (
            radiation_w_m2
            / 1000.0
            * panel_kwp
            * performance_ratio
            * min(1.0, max(0.0, solar_factor))
            * hours
        )
        load_kwh = conservative_load_w / 1000.0 * hours
        forecast_pv_kwh += pv_kwh
        forecast_load_kwh += load_kwh
        current_soc += (pv_kwh - load_kwh) * 1000.0 / battery_capacity_wh * 100.0
        minimum_soc = min(minimum_soc, current_soc)

    if covered_until < recovery_at or covered_hours + 0.05 < expected_hours:
        return SolarBridgeDecision("unavailable", "hourly solar coverage is incomplete")

    projected_recovery_soc = min(100.0, current_soc)
    rounded = {
        "forecast_pv_kwh": round(forecast_pv_kwh, 2),
        "forecast_load_kwh": round(forecast_load_kwh, 2),
        "forecast_factor": round(min(1.0, max(0.0, solar_factor)), 2),
        "projected_min_soc": round(minimum_soc, 1),
        "projected_recovery_soc": round(projected_recovery_soc, 1),
    }
    if minimum_soc < safety_floor_soc:
        return SolarBridgeDecision(
            "ineligible",
            f"projected minimum SOC {minimum_soc:.1f}% is below the {safety_floor_soc:g}% floor",
            **rounded,
        )
    if projected_recovery_soc < target_soc:
        return SolarBridgeDecision(
            "ineligible",
            f"projected recovery SOC {projected_recovery_soc:.1f}% is below the {target_soc:g}% target",
            **rounded,
        )
    return SolarBridgeDecision(
        "eligible",
        f"forecast reaches {projected_recovery_soc:.1f}% by {recovery_at:%H:%M} without crossing {minimum_soc:.1f}%",
        **rounded,
    )


def format_bridge_audit_note(decision: SolarBridgeDecision) -> str:
    parts = [f"bridge={decision.status}"]
    for key, value in (
        ("forecast_pv_kwh", decision.forecast_pv_kwh),
        ("forecast_load_kwh", decision.forecast_load_kwh),
        ("forecast_factor", decision.forecast_factor),
        ("projected_min_soc", decision.projected_min_soc),
        ("projected_recovery_soc", decision.projected_recovery_soc),
    ):
        if value is not None:
            parts.append(f"{key}={value:g}")
    return ", ".join(parts)


def _bridge_status(note: str) -> str:
    match = re.search(r"(?:^|[,;\s])bridge=([a-z-]+)", note, re.IGNORECASE)
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


def _interpolated_metric(
    samples: list[tuple[dt.datetime, dict[str, Any]]],
    at: dt.datetime,
    key: str,
) -> float | None:
    before = [item for item in samples if item[0] <= at and _metric_number(item[1], key) is not None]
    after = [item for item in samples if item[0] >= at and _metric_number(item[1], key) is not None]
    if not before or not after:
        return None
    left_at, left_row = before[-1]
    right_at, right_row = after[0]
    left = _metric_number(left_row, key)
    right = _metric_number(right_row, key)
    if left is None or right is None:
        return None
    if left_at == right_at:
        return left
    fraction = (at - left_at).total_seconds() / (right_at - left_at).total_seconds()
    return left + (right - left) * fraction


def _counterfactual_window(
    history: list[dict[str, Any]],
    *,
    started_at: dt.datetime,
    returned_at: dt.datetime,
    recovery_at: dt.datetime,
    start_soc: float,
    battery_capacity_wh: float,
) -> tuple[float | None, float | None, float | None, bool]:
    samples = sorted(
        (
            (timestamp, row)
            for row in history
            if (timestamp := _metric_timestamp(row)) is not None
            and started_at - dt.timedelta(minutes=_METRIC_ENDPOINT_TOLERANCE_MINUTES)
            <= timestamp
            <= recovery_at + dt.timedelta(minutes=_METRIC_ENDPOINT_TOLERANCE_MINUTES)
        ),
        key=lambda item: item[0],
    )
    if len(samples) < 2:
        return None, None, None, False

    before_start = [item for item in samples if item[0] <= started_at]
    after_recovery = [item for item in samples if item[0] >= recovery_at]
    if not before_start or not after_recovery:
        return None, None, None, False
    if (
        (started_at - before_start[-1][0]).total_seconds() / 60.0 > _METRIC_ENDPOINT_TOLERANCE_MINUTES
        or (after_recovery[0][0] - recovery_at).total_seconds() / 60.0 > _METRIC_ENDPOINT_TOLERANCE_MINUTES
    ):
        return None, None, None, False
    bounded = [item for item in samples if before_start[-1][0] <= item[0] <= after_recovery[0][0]]
    if any(
        (right[0] - left[0]).total_seconds() / 60.0 > _METRIC_MAX_GAP_MINUTES
        for left, right in zip(bounded, bounded[1:])
    ):
        return None, None, None, False

    points: list[tuple[dt.datetime, float, float]] = []
    for at in [started_at] + [item[0] for item in bounded if started_at < item[0] < recovery_at] + [recovery_at]:
        pv_w = _interpolated_metric(bounded, at, "pv_w")
        load_w = _interpolated_metric(bounded, at, "load_w")
        if pv_w is None or load_w is None:
            return None, None, None, False
        points.append((at, pv_w, load_w))

    current_soc = start_soc
    minimum_soc = start_soc
    for left, right in zip(points, points[1:]):
        hours = (right[0] - left[0]).total_seconds() / 3600.0
        average_net_w = ((left[1] - left[2]) + (right[1] - right[2])) / 2.0
        current_soc += average_net_w * hours / battery_capacity_wh * 100.0
        minimum_soc = min(minimum_soc, current_soc)

    start_grid = _interpolated_metric(bounded, started_at, "grid_today_kwh")
    return_grid = _interpolated_metric(bounded, returned_at, "grid_today_kwh")
    grid_import = (
        round(max(0.0, return_grid - start_grid), 3)
        if start_grid is not None and return_grid is not None
        else None
    )
    return round(minimum_soc, 1), round(current_soc, 1), grid_import, True


def _recommendation(outcomes: list[PreservationOutcome]) -> tuple[str, str]:
    comparable = [
        item
        for item in outcomes
        if item.metric_coverage_complete and item.bridge_status == "eligible"
    ]
    if len(comparable) < _MIN_COMPARABLE_OUTCOMES:
        remaining = _MIN_COMPARABLE_OUTCOMES - len(comparable)
        return (
            "hold",
            f"Need {remaining} more comparable morning preservation outcome"
            f"{'s' if remaining != 1 else ''} before enabling the solar bridge.",
        )
    recent = comparable[-_MIN_COMPARABLE_OUTCOMES:]
    counts = Counter(item.classification for item in recent)
    if counts["likely-avoidable"] == len(recent):
        return (
            "review-solar-bridge",
            "Repeated counterfactuals stayed above the safety floor and recovered the target; review an opt-in solar-bridge trial.",
        )
    if counts["safety-preserving"] > 0:
        return (
            "keep-current",
            "Repeated counterfactuals crossed the safety floor; keep morning Utility preservation unchanged.",
        )
    return ("hold", "Morning preservation outcomes are mixed; keep the solar bridge disabled.")


def build_preservation_scorecard(
    rows: list[dict[str, str]],
    history: list[dict[str, Any]],
    *,
    battery_capacity_wh: float,
    safety_floor_soc: float,
    start_hour: int,
    recovery_hour: int,
) -> PreservationScorecard:
    def audit_timestamp_key(row: dict[str, str]) -> dt.datetime:
        parsed = parse_audit_timestamp(row.get("timestamp", ""))
        return _naive_local(parsed) if parsed is not None else dt.datetime.min

    ordered = sorted(
        rows,
        key=audit_timestamp_key,
    )
    outcomes: list[PreservationOutcome] = []
    for index, row in enumerate(ordered):
        if row.get("command") != "preserve-battery" or row.get("action") != "switch-to-utility":
            continue
        started = parse_audit_timestamp(row.get("timestamp", ""))
        start_soc = parse_audit_float(row, "soc")
        target_soc = parse_audit_float(row, "threshold")
        bridge_status = _bridge_status(row.get("note", ""))
        if started is None:
            continue
        started = _naive_local(started)
        if started.hour < start_hour or started.hour >= recovery_hour:
            continue
        recovery_at = started.replace(hour=recovery_hour, minute=0, second=0, microsecond=0)
        returned: dt.datetime | None = None
        for candidate in ordered[index + 1:]:
            candidate_at = parse_audit_timestamp(candidate.get("timestamp", ""))
            if candidate_at is None:
                continue
            candidate_at = _naive_local(candidate_at)
            if candidate_at.date() != started.date() or candidate_at > recovery_at:
                break
            if candidate.get("action") == "switch-to-utility-unconfirmed":
                returned = None
                break
            if candidate.get("command") == "return-sbu" and candidate.get("action") == "switch-to-sbu":
                returned = candidate_at
                break

        minimum_soc = recovery_soc = grid_import = None
        complete = False
        if (
            returned is not None
            and start_soc is not None
            and target_soc is not None
            and battery_capacity_wh > 0
        ):
            minimum_soc, recovery_soc, grid_import, complete = _counterfactual_window(
                history,
                started_at=started,
                returned_at=returned,
                recovery_at=recovery_at,
                start_soc=start_soc,
                battery_capacity_wh=battery_capacity_wh,
            )
        classification = "unknown"
        if complete and minimum_soc is not None and recovery_soc is not None and target_soc is not None:
            if minimum_soc < safety_floor_soc:
                classification = "safety-preserving"
            elif recovery_soc >= target_soc:
                classification = "likely-avoidable"
            else:
                classification = "partial-benefit"
        outcomes.append(
            PreservationOutcome(
                started_at=started.isoformat(timespec="seconds"),
                returned_at=returned.isoformat(timespec="seconds") if returned else None,
                recovery_at=recovery_at.isoformat(timespec="seconds"),
                classification=classification,
                bridge_status=bridge_status,
                start_soc=start_soc,
                target_soc=target_soc,
                counterfactual_min_soc=minimum_soc,
                counterfactual_recovery_soc=recovery_soc,
                grid_import_kwh=grid_import,
                metric_coverage_complete=complete,
            )
        )

    recommendation_status, recommendation = _recommendation(outcomes)
    measured = [item.grid_import_kwh for item in outcomes if item.grid_import_kwh is not None]
    return PreservationScorecard(
        outcomes=tuple(outcomes),
        classification_counts=dict(Counter(item.classification for item in outcomes)),
        comparable_count=sum(
            1
            for item in outcomes
            if item.metric_coverage_complete and item.bridge_status == "eligible"
        ),
        minimum_comparable_count=_MIN_COMPARABLE_OUTCOMES,
        recommendation_status=recommendation_status,
        recommendation=recommendation,
        measured_grid_import_kwh=round(sum(measured), 3) if measured else None,
    )
