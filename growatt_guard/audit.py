from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from growatt_guard.growatt_api import (
    extract_soc,
    extract_spf_output_source,
    format_metric,
)
from growatt_guard.state import pause_message, read_pause_state


BASE_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "growatt_power_guard.log"
MODE_AUDIT_FILE = LOG_DIR / "mode_decisions.csv"

MODE_AUDIT_FIELDS = (
    "timestamp",
    "command",
    "soc",
    "threshold",
    "weather_category",
    "previous_mode",
    "action",
    "dry_run",
    "result",
    "note",
)


def summarize_today_log_counts() -> dict[str, int]:
    today = dt.datetime.now().strftime("%Y-%m-%d")
    counts = {
        "success": 0,
        "failure": 0,
        "watchdog_repairs": 0,
        "preserve_actions": 0,
        "return_sbu_actions": 0,
    }
    if not LOG_FILE.exists():
        return counts

    for line in LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(today):
            continue
        lower = line.lower()
        if "inv_set_success" in lower or "mode response" in lower:
            counts["success"] += 1
        if " error " in lower or " failed" in lower or "unhandled error" in lower:
            counts["failure"] += 1
        if "watchdog detected" in lower or "watchdog repaired" in lower:
            counts["watchdog_repairs"] += 1
        if "switching to utility" in lower:
            counts["preserve_actions"] += 1
        if "sbu mode response" in lower:
            counts["return_sbu_actions"] += 1
    return counts


def format_audit_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True)
    else:
        text = str(value)
    if len(text) > 500:
        return text[:497] + "..."
    return text


def append_mode_audit(
    config: Any,
    command: str,
    *,
    soc: float | None = None,
    threshold: float | None = None,
    weather_category: str = "",
    previous_mode: str = "",
    action: str = "",
    result: Any = None,
    note: str = "",
) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    write_header = not MODE_AUDIT_FILE.exists()
    row = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "command": command,
        "soc": format_audit_value(soc),
        "threshold": format_audit_value(threshold),
        "weather_category": weather_category,
        "previous_mode": previous_mode,
        "action": action,
        "dry_run": str(config.dry_run).lower(),
        "result": format_audit_value(result),
        "note": note,
    }
    with MODE_AUDIT_FILE.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MODE_AUDIT_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def prune_audit_rows(cutoff: dt.datetime) -> tuple[int, int]:
    """Remove rows older than cutoff from MODE_AUDIT_FILE. Returns (removed, kept)."""
    if not MODE_AUDIT_FILE.exists():
        return 0, 0
    with MODE_AUDIT_FILE.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or MODE_AUDIT_FIELDS)
        rows = list(reader)
    kept = []
    removed = 0
    for row in rows:
        ts = parse_audit_timestamp(row.get("timestamp", ""))
        if ts is None or ts >= cutoff:
            kept.append(row)
        else:
            removed += 1
    if removed > 0:
        # Atomic rewrite: never truncate the audit log in place, or a failed
        # write (disk full, killed process) would destroy all history.
        fd, tmp = tempfile.mkstemp(
            dir=MODE_AUDIT_FILE.parent, prefix=MODE_AUDIT_FILE.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(kept)
            os.replace(tmp, MODE_AUDIT_FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return removed, len(kept)


def parse_audit_timestamp(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def read_mode_audit_rows(
    *,
    since: dt.datetime | None = None,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[dict[str, str]]:
    if not MODE_AUDIT_FILE.exists():
        return []
    with MODE_AUDIT_FILE.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if since is not None:
        rows = [
            row
            for row in rows
            if (timestamp := parse_audit_timestamp(row.get("timestamp", ""))) is not None and timestamp >= since
        ]
    if newest_first:
        rows = list(reversed(rows))
    if limit is not None:
        rows = rows[:limit]
    return rows


def parse_audit_float(row: dict[str, str], key: str) -> float | None:
    value = row.get(key, "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def parse_topup_minutes(row: dict[str, str]) -> int | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*min", row.get("note", ""))
    if match is None:
        return None
    try:
        return round(float(match.group(1)))
    except ValueError:
        return None


def _compare_timestamp(ts: dt.datetime, now: dt.datetime) -> dt.datetime:
    if ts.tzinfo is not None and now.tzinfo is None:
        return ts.astimezone().replace(tzinfo=None)
    if ts.tzinfo is None and now.tzinfo is not None:
        return ts.replace(tzinfo=now.tzinfo)
    return ts


def find_overdue_unclosed_topup(
    now: dt.datetime | None = None,
    *,
    grace_minutes: float = 15.0,
    lookback_hours: float = 12.0,
) -> dict[str, Any] | None:
    """Return the latest overdue auto-topup audit row that has no later SBU return."""
    now = now or dt.datetime.now()
    cutoff = now - dt.timedelta(hours=lookback_hours)
    candidate: tuple[dict[str, str], dt.datetime] | None = None

    for row in read_mode_audit_rows():
        ts = parse_audit_timestamp(row.get("timestamp", ""))
        if ts is None:
            continue
        comparable_ts = _compare_timestamp(ts, now)
        if comparable_ts < cutoff:
            continue

        action = row.get("action", "")
        command = row.get("command", "")
        if action == "auto-topup-started":
            candidate = (row, comparable_ts)
            continue

        closes_topup = (
            candidate is not None
            and comparable_ts >= candidate[1]
            and (
                (command == "return-sbu" and action in {"switch-to-sbu", "no-change"})
                or action == "repair-sbu"
            )
        )
        if closes_topup:
            candidate = None

    if candidate is None:
        return None

    row, started_at = candidate
    minutes = parse_topup_minutes(row)
    if minutes is None:
        return None
    due_at = started_at + dt.timedelta(minutes=minutes + grace_minutes)
    if now < due_at:
        return None
    return {
        "row": row,
        "started_at": started_at,
        "due_at": due_at,
        "minutes": minutes,
    }


def build_chart_data(now: dt.datetime | None = None, days: int = 7) -> dict[str, Any]:
    now = now or dt.datetime.now()
    since = now - dt.timedelta(days=days)
    rows = read_mode_audit_rows(since=since)
    dates = [(now - dt.timedelta(days=i)).date() for i in range(days - 1, -1, -1)]
    labels = [d.strftime("%a %m-%d") for d in dates]
    preserve_by_date: dict[str, int] = {d.isoformat(): 0 for d in dates}
    utility_by_date: dict[str, int] = {d.isoformat(): 0 for d in dates}
    watchdog_by_date: dict[str, int] = {d.isoformat(): 0 for d in dates}
    for row in rows:
        ts = parse_audit_timestamp(row.get("timestamp", ""))
        if ts is None:
            continue
        date_key = ts.date().isoformat()
        if row.get("command") == "preserve-battery" and date_key in preserve_by_date:
            preserve_by_date[date_key] += 1
        if row.get("action") == "switch-to-utility" and date_key in utility_by_date:
            utility_by_date[date_key] += 1
        if row.get("action") == "repair-sbu" and date_key in watchdog_by_date:
            watchdog_by_date[date_key] += 1
    return {
        "labels": labels,
        "preserve_checks": [preserve_by_date[d.isoformat()] for d in dates],
        "utility_switches": [utility_by_date[d.isoformat()] for d in dates],
        "watchdog_repairs": [watchdog_by_date[d.isoformat()] for d in dates],
    }


def build_weekly_summary(
    now: dt.datetime | None = None,
    solar_this_week: dict[str, int] | None = None,
    solar_last_week: dict[str, int] | None = None,
    charge_rate_w: float = 0.0,
    low_battery_soc: float | None = None,
    battery_bms_cutoff_soc: float = 25.0,
) -> str:
    now = now or dt.datetime.now()
    since = now - dt.timedelta(days=7)
    rows = read_mode_audit_rows(since=since)
    preserve_rows = [row for row in rows if row.get("command") == "preserve-battery"]
    preserve_socs = [soc for row in preserve_rows if (soc := parse_audit_float(row, "soc")) is not None]
    utility_switches = [row for row in rows if row.get("action") == "switch-to-utility"]
    preserve_no_changes = [row for row in rows if row.get("command") == "preserve-battery" and row.get("action") == "no-change"]
    return_sbu = [row for row in rows if row.get("action") == "switch-to-sbu"]
    watchdog_repairs = [row for row in rows if row.get("action") == "repair-sbu"]
    failures = [row for row in rows if row.get("action", "").endswith("-failed") or row.get("result") == "error"]
    topup_rows = [row for row in rows if row.get("action") == "auto-topup-started"]
    last_row = rows[-1] if rows else None

    topup_total_min = 0
    for row in topup_rows:
        minutes = parse_topup_minutes(row)
        if minutes is not None:
            topup_total_min += minutes

    avg_soc = average(preserve_socs)
    tuning_lines = _weekly_tuning_lines(
        rows=rows,
        preserve_socs=preserve_socs,
        topup_rows=topup_rows,
        topup_total_min=topup_total_min,
        low_battery_soc=low_battery_soc,
        battery_bms_cutoff_soc=battery_bms_cutoff_soc,
    )
    lines = [
        f"Growatt weekly performance - {since.strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')}",
        f"Audit rows: {len(rows)}",
        f"Preserve-battery checks: {len(preserve_rows)}",
        f"Utility switches: {len(utility_switches)}",
        f"No-change preserve checks: {len(preserve_no_changes)}",
        f"Return-SBU switches: {len(return_sbu)}",
        f"Watchdog repairs: {len(watchdog_repairs)}",
        f"Failures: {len(failures)}",
    ]

    topup_line = f"Auto-topups: {len(topup_rows)}"
    if topup_rows and topup_total_min > 0:
        topup_line += f" ({topup_total_min} min grid charging"
        if charge_rate_w > 0:
            kwh = topup_total_min / 60.0 * charge_rate_w / 1000.0
            topup_line += f", ~{kwh:.1f} kWh"
        topup_line += ")"
    lines.append(topup_line)
    if avg_soc is not None:
        lines.append(f"Average preserve-check SOC: {avg_soc:g}%")
        lines.append(f"Lowest preserve-check SOC: {min(preserve_socs):g}%")
    else:
        lines.append("Average preserve-check SOC: not enough data")
    if tuning_lines:
        lines.append("")
        lines.append("Threshold tuning:")
        lines.extend(f"  - {line}" for line in tuning_lines)
    if last_row:
        lines.append(
            "Last action: "
            f"{last_row.get('timestamp', '')} {last_row.get('command', '')} "
            f"{last_row.get('action', '')} SOC={last_row.get('soc', '')}%"
        )

    solar_yield_change: float | None = None
    if solar_this_week:
        days = len(solar_this_week)
        total_wh = sum(solar_this_week.values())
        avg_wh = total_wh / days
        lines.append(
            f"Solar this week: {total_wh / 1000:.1f} kWh total, "
            f"{avg_wh / 1000:.2f} kWh/day avg ({days} day{'s' if days != 1 else ''} data)"
        )
        if solar_last_week:
            days_last = len(solar_last_week)
            total_last_wh = sum(solar_last_week.values())
            avg_last_wh = total_last_wh / days_last
            lines.append(
                f"Solar last week: {total_last_wh / 1000:.1f} kWh total, "
                f"{avg_last_wh / 1000:.2f} kWh/day avg"
            )
            if avg_last_wh > 0:
                solar_yield_change = (avg_wh - avg_last_wh) / avg_last_wh * 100
                direction = "▲" if solar_yield_change >= 0 else "▼"
                lines.append(f"Week-over-week yield: {direction} {abs(solar_yield_change):.0f}%")

    recommendations = _weekly_recommendations(
        preserve_rows=preserve_rows,
        utility_switches=utility_switches,
        watchdog_repairs=watchdog_repairs,
        failures=failures,
        avg_soc=avg_soc,
        solar_yield_change=solar_yield_change,
    )
    if recommendations:
        lines.append("")
        lines.append("Recommendations:")
        for tip in recommendations:
            lines.append(f"  - {tip}")

    return "\n".join(lines)


def _weekly_tuning_lines(
    *,
    rows: list[dict[str, str]],
    preserve_socs: list[float],
    topup_rows: list[dict[str, str]],
    topup_total_min: int,
    low_battery_soc: float | None,
    battery_bms_cutoff_soc: float,
) -> list[str]:
    socs = [soc for row in rows if (soc := parse_audit_float(row, "soc")) is not None]
    if not socs:
        return ["Need more SOC data before recommending a threshold change."]

    lowest_soc = min(socs)
    highest_soc = max(socs)
    topup_start_socs = [soc for row in topup_rows if (soc := parse_audit_float(row, "soc")) is not None]
    near_cutoff_limit = battery_bms_cutoff_soc + 5
    near_cutoff_count = len([soc for soc in socs if soc <= near_cutoff_limit])
    configured_threshold = low_battery_soc
    if configured_threshold is None:
        thresholds = [value for row in rows if (value := parse_audit_float(row, "threshold")) is not None]
        configured_threshold = max(thresholds) if thresholds else None

    lines = [
        f"Observed SOC range: {lowest_soc:g}% to {highest_soc:g}%",
    ]
    if battery_bms_cutoff_soc > 0:
        lines.append(f"Lowest margin above BMS cutoff: {lowest_soc - battery_bms_cutoff_soc:+g}%")
        lines.append(f"Near-cutoff readings (<= {near_cutoff_limit:g}%): {near_cutoff_count}")
    if topup_start_socs:
        avg_topup_soc = average(topup_start_socs)
        if avg_topup_soc is not None:
            lines.append(f"Avg auto-topup start SOC: {avg_topup_soc:g}%")

    hint = "Hold current threshold until another few nights of data confirm the pattern."
    if near_cutoff_count > 0:
        hint = "Do not lower yet; at least one reading was close to BMS cutoff."
    elif topup_rows and topup_total_min >= 180:
        hint = "Do not lower yet; grid topup time is still high this week."
    elif len(topup_rows) >= 4:
        hint = "Hold current threshold; frequent topups mean the battery is already being used hard overnight."
    elif configured_threshold is not None and preserve_socs:
        lowest_preserve = min(preserve_socs)
        comfortable_week = (
            len(rows) >= 12
            and not topup_rows
            and lowest_soc >= battery_bms_cutoff_soc + 12
            and lowest_preserve >= configured_threshold + 3
        )
        if comfortable_week:
            hint = "Could trial lowering LOW_BATTERY_SOC by 2-3% in similar weather."
        elif lowest_soc >= battery_bms_cutoff_soc + 7 and len(topup_rows) <= 2:
            hint = "Current threshold looks balanced; a tiny 1-2% lower trial is reasonable if you want less grid use."
        else:
            hint = "Current threshold looks balanced for the observed load and weather."

    lines.append(f"Tuning hint: {hint}")
    return lines


def _weekly_recommendations(
    *,
    preserve_rows: list[dict[str, str]],
    utility_switches: list[dict[str, str]],
    watchdog_repairs: list[dict[str, str]],
    failures: list[dict[str, str]],
    avg_soc: float | None,
    solar_yield_change: float | None = None,
) -> list[str]:
    tips: list[str] = []

    if len(utility_switches) >= 5:
        tips.append(
            f"Battery switched to utility {len(utility_switches)} times this week — "
            "consider raising LOW_BATTERY_SOC."
        )

    if len(watchdog_repairs) >= 3:
        tips.append(
            f"Watchdog repaired SBU mode {len(watchdog_repairs)} times — "
            "check inverter output-source setting or cron timing."
        )

    if len(failures) > 0:
        tips.append(
            f"{len(failures)} command failure(s) recorded — check logs/growatt_power_guard.log."
        )

    if avg_soc is not None:
        thresholds = {parse_audit_float(row, "threshold") for row in preserve_rows}
        thresholds.discard(None)
        if thresholds:
            representative_threshold = max(t for t in thresholds if t is not None)
            if avg_soc < representative_threshold - 5:
                tips.append(
                    f"Average SOC at preserve-battery time ({avg_soc:g}%) is well below "
                    f"threshold ({representative_threshold:g}%) — consider raising LOW_BATTERY_SOC."
                )

    if len(utility_switches) == 0 and len(preserve_rows) >= 5:
        tips.append(
            "Battery maintained SBU all week — conditions may allow a lower threshold "
            "if weather is reliably sunny."
        )

    if solar_yield_change is not None and solar_yield_change <= -20:
        tips.append(
            f"Solar yield dropped {abs(solar_yield_change):.0f}% week-over-week — "
            "consider checking panel cleanliness or shading."
        )

    return tips


def build_monthly_summary(
    now: dt.datetime | None = None,
    solar_this_month: dict[str, int] | None = None,
    solar_last_month: dict[str, int] | None = None,
) -> str:
    now = now or dt.datetime.now()
    since = now - dt.timedelta(days=30)
    rows = read_mode_audit_rows(since=since)
    preserve_rows = [row for row in rows if row.get("command") == "preserve-battery"]
    preserve_socs = [soc for row in preserve_rows if (soc := parse_audit_float(row, "soc")) is not None]
    utility_switches = [row for row in rows if row.get("action") == "switch-to-utility"]
    preserve_no_changes = [row for row in rows if row.get("command") == "preserve-battery" and row.get("action") == "no-change"]
    return_sbu = [row for row in rows if row.get("action") == "switch-to-sbu"]
    watchdog_repairs = [row for row in rows if row.get("action") == "repair-sbu"]
    failures = [row for row in rows if row.get("action", "").endswith("-failed") or row.get("result") == "error"]
    last_row = rows[-1] if rows else None

    avg_soc = average(preserve_socs)
    lines = [
        f"Growatt monthly performance - {since.strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')}",
        f"Audit rows: {len(rows)}",
        f"Preserve-battery checks: {len(preserve_rows)}",
        f"Utility switches: {len(utility_switches)}",
        f"No-change preserve checks: {len(preserve_no_changes)}",
        f"Return-SBU switches: {len(return_sbu)}",
        f"Watchdog repairs: {len(watchdog_repairs)}",
        f"Failures: {len(failures)}",
    ]
    if avg_soc is not None:
        lines.append(f"Average preserve-check SOC: {avg_soc:g}%")
        lines.append(f"Lowest preserve-check SOC: {min(preserve_socs):g}%")
    else:
        lines.append("Average preserve-check SOC: not enough data")
    if last_row:
        lines.append(
            "Last action: "
            f"{last_row.get('timestamp', '')} {last_row.get('command', '')} "
            f"{last_row.get('action', '')} SOC={last_row.get('soc', '')}%"
        )

    if solar_this_month:
        days = len(solar_this_month)
        total_wh = sum(solar_this_month.values())
        avg_wh = total_wh / days
        lines.append(
            f"Solar this month: {total_wh / 1000:.1f} kWh total, "
            f"{avg_wh / 1000:.2f} kWh/day avg ({days} day{'s' if days != 1 else ''} data)"
        )
        if solar_last_month:
            days_last = len(solar_last_month)
            total_last_wh = sum(solar_last_month.values())
            avg_last_wh = total_last_wh / days_last
            lines.append(
                f"Solar last month: {total_last_wh / 1000:.1f} kWh total, "
                f"{avg_last_wh / 1000:.2f} kWh/day avg"
            )
            if avg_last_wh > 0:
                change = (avg_wh - avg_last_wh) / avg_last_wh * 100
                direction = "▲" if change >= 0 else "▼"
                lines.append(f"Month-over-month yield: {direction} {abs(change):.0f}%")

    return "\n".join(lines)


def build_daily_summary(status: dict[str, Any], tomorrow_kwh_m2: float | None = None) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"Growatt daily summary - {now}"]

    state = read_pause_state()
    if state:
        lines.append(f"Automation pause: {pause_message(state)}")

    soc_result = extract_soc(status)
    if soc_result:
        soc, _ = soc_result
        lines.append(f"Battery SOC: {soc:g}%")

    output_source = extract_spf_output_source(status)
    if output_source:
        raw, label, _ = output_source
        lines.append(f"Output source: {label} [{raw}]")

    metric_specs = [
        ("PV power", ("ppvText", "ppv"), " W"),
        ("Grid voltage", ("vGridText", "vGrid"), " V"),
        ("Output power", ("outPutPowerText", "outPutPower", "activePower"), " W"),
        ("Battery charge power", ("pChargeText", "pCharge"), " W"),
        ("Battery discharge power", ("pDischargeText", "pDischarge"), " W"),
        ("Energy charged today", ("eChargeTodayText", "eChargeToday"), " kWh"),
        ("AC charge today", ("eacChargeToday", "eacChargeTodayText"), " kWh"),
        ("Energy discharged today", ("eDischargeTodayText", "eDischargeToday"), " kWh"),
    ]
    for label, keys, unit in metric_specs:
        formatted = format_metric(status, label, keys, unit)
        if formatted:
            lines.append(formatted)

    try:
        from growatt_guard.pvoutput import read_pvoutput_state
        pv_state = read_pvoutput_state()
        if pv_state:
            uploaded_at = pv_state.get("uploaded_at", "")
            today_str = dt.datetime.now().strftime("%Y-%m-%d")
            if uploaded_at.startswith(today_str):
                v1 = pv_state.get("fields", {}).get("v1")
                if v1 is not None:
                    lines.append(f"Solar today: {int(v1) / 1000:.2f} kWh")
    except Exception:
        pass

    if tomorrow_kwh_m2 is not None:
        lines.append(f"Tomorrow's solar forecast: {tomorrow_kwh_m2:.1f} kWh/m²")

    counts = summarize_today_log_counts()
    lines.extend(
        [
            "",
            "Automation today:",
            f"Successful mode responses: {counts['success']}",
            f"Failures/errors: {counts['failure']}",
            f"Preserve-battery actions: {counts['preserve_actions']}",
            f"Return-SBU actions: {counts['return_sbu_actions']}",
            f"Watchdog repairs: {counts['watchdog_repairs']}",
        ]
    )
    return "\n".join(lines)
