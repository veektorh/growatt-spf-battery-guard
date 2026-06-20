from __future__ import annotations

import csv
import datetime as dt
import json
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


def build_weekly_summary(now: dt.datetime | None = None) -> str:
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
    last_row = rows[-1] if rows else None

    avg_soc = average(preserve_socs)
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

    recommendations = _weekly_recommendations(
        preserve_rows=preserve_rows,
        utility_switches=utility_switches,
        watchdog_repairs=watchdog_repairs,
        failures=failures,
        avg_soc=avg_soc,
    )
    if recommendations:
        lines.append("")
        lines.append("Recommendations:")
        for tip in recommendations:
            lines.append(f"  - {tip}")

    return "\n".join(lines)


def _weekly_recommendations(
    *,
    preserve_rows: list[dict[str, str]],
    utility_switches: list[dict[str, str]],
    watchdog_repairs: list[dict[str, str]],
    failures: list[dict[str, str]],
    avg_soc: float | None,
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

    return tips


def build_monthly_summary(now: dt.datetime | None = None) -> str:
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
    return "\n".join(lines)


def build_daily_summary(status: dict[str, Any]) -> str:
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
