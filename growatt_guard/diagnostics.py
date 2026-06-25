from __future__ import annotations

import datetime as dt
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from growatt_guard.audit import read_mode_audit_rows
from growatt_guard.dashboard import DASHBOARD_FILE, dashboard_freshness
from growatt_guard.schedule import check_cron_schedule, next_scheduled_runs, schedule_job_tokens, validate_schedule
from growatt_guard.state import (
    parse_utc_datetime,
    pause_message,
    read_command_lock_state,
    read_pause_state,
    read_topup_state,
)


SERVICE_UNITS = (
    "growatt-dashboard-refresh.service",
    "growatt-dashboard-server.service",
    "growatt-dashboard-stale-alert.timer",
    "growatt-discord-control.service",
)
BASE_DIR = Path(__file__).resolve().parents[1]
LOG_FILE = BASE_DIR / "logs" / "growatt_power_guard.log"


@dataclass(frozen=True)
class DiagnosticItem:
    name: str
    status: str
    detail: str


def _overall(items: list[DiagnosticItem]) -> str:
    statuses = {item.status for item in items}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "OK"


def _run(args: list[str], timeout: int = 8) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, check=False, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _systemd_unit_status(unit: str) -> DiagnosticItem:
    if os.name == "nt":
        return DiagnosticItem(unit, "SKIP", "systemd is not available on Windows.")
    result = _run(["systemctl", "is-active", unit])
    if result is None:
        return DiagnosticItem(unit, "SKIP", "systemctl is not available.")
    state = (result.stdout or result.stderr or "").strip() or f"exit {result.returncode}"
    if state == "active":
        return DiagnosticItem(unit, "OK", "active")
    if state in {"inactive", "unknown"}:
        return DiagnosticItem(unit, "WARN", state)
    return DiagnosticItem(unit, "FAIL", state)


def _state_items() -> list[DiagnosticItem]:
    items: list[DiagnosticItem] = []
    pause_state = read_pause_state()
    if pause_state:
        items.append(DiagnosticItem("Pause state", "WARN", pause_message(pause_state)))
    else:
        items.append(DiagnosticItem("Pause state", "OK", "automation is active."))

    topup_state = read_topup_state()
    if topup_state:
        try:
            paused_until = parse_utc_datetime(str(topup_state["paused_until"]))
            now = dt.datetime.now(dt.timezone.utc)
            if now < paused_until:
                remaining = int((paused_until - now).total_seconds() // 60)
                items.append(DiagnosticItem("Topup state", "WARN", f"active; about {remaining} min remaining."))
            else:
                items.append(DiagnosticItem("Topup state", "WARN", "state exists but topup window has expired."))
        except (KeyError, ValueError):
            items.append(DiagnosticItem("Topup state", "WARN", "state exists but could not be parsed."))
    else:
        items.append(DiagnosticItem("Topup state", "OK", "no active topup."))

    lock_state = read_command_lock_state()
    if lock_state:
        items.append(
            DiagnosticItem(
                "Command lock",
                "WARN",
                f"{lock_state.get('command', 'unknown command')} since {lock_state.get('created_at', 'unknown time')}.",
            )
        )
    else:
        items.append(DiagnosticItem("Command lock", "OK", "no active mode-command lock."))
    return items


def build_service_status(config: Any) -> list[DiagnosticItem]:
    items: list[DiagnosticItem] = []
    try:
        schedule = validate_schedule()
    except Exception as exc:  # noqa: BLE001 - diagnostic output should continue
        items.append(DiagnosticItem("Schedule", "FAIL", str(exc)))
        schedule = None
    else:
        jobs = schedule.get("jobs", [])
        items.append(DiagnosticItem("Schedule", "OK", f"{len(jobs)} job(s) in {schedule.get('timezone', 'unknown')}."))
        for check in check_cron_schedule(schedule):
            items.append(DiagnosticItem(check.name, check.status, check.detail))
        next_runs = next_scheduled_runs(schedule, limit=1)
        if next_runs:
            run_at, job = next_runs[0]
            items.append(
                DiagnosticItem(
                    "Next job",
                    "OK",
                    f"{run_at.strftime('%Y-%m-%d %H:%M')} - {' '.join(schedule_job_tokens(job))}",
                )
            )

    try:
        freshness = dashboard_freshness(DASHBOARD_FILE, config.dashboard_stale_minutes)
    except Exception as exc:  # noqa: BLE001
        items.append(DiagnosticItem("Dashboard freshness", "WARN", f"could not inspect dashboard.html: {exc}"))
    else:
        items.append(
            DiagnosticItem(
                "Dashboard freshness",
                "WARN" if freshness["stale"] else "OK",
                f"{freshness['reason']}; stale after {config.dashboard_stale_minutes:g} min.",
            )
        )

    items.extend(_state_items())
    items.extend(_systemd_unit_status(unit) for unit in SERVICE_UNITS)
    return items


def format_diagnostic_items(title: str, items: list[DiagnosticItem]) -> str:
    lines = [title, f"Result: {_overall(items)}", ""]
    for item in items:
        detail = " ".join(str(item.detail).split())
        lines.append(f"[{item.status}] {item.name}: {detail}")
    return "\n".join(lines)


def command_service_status(config: Any) -> int:
    items = build_service_status(config)
    print(format_diagnostic_items("Growatt service status", items))
    return 1 if _overall(items) == "FAIL" else 0


def _redacted_config_summary(config: Any) -> list[str]:
    return [
        f"DRY_RUN={config.dry_run}",
        f"LOW_BATTERY_SOC={config.low_battery_soc:g}",
        f"BATTERY_CAPACITY_WH={config.battery_capacity_wh:g}",
        f"BATTERY_CHARGE_RATE_W={config.battery_charge_rate_w:g}",
        f"AUTO_TOPUP_ENABLED={config.auto_topup_enabled}",
        f"DASHBOARD_STALE_MINUTES={config.dashboard_stale_minutes:g}",
        f"PVOUTPUT_ENABLED={getattr(config, 'pvoutput_enabled', False)}",
        f"DISCORD_WEBHOOK_CONFIGURED={bool(config.discord_webhook_url)}",
    ]


def _recent_error_lines(log_path: Path, limit: int = 8) -> list[str]:
    if not log_path.exists():
        return ["no log file found."]
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"could not read log file: {exc}"]
    matches = [line for line in lines if " ERROR " in line or " WARNING " in line]
    return matches[-limit:] if matches else ["no recent warnings or errors."]


def build_diagnostic_bundle(config: Any) -> str:
    generated_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    items = build_service_status(config)
    rows = read_mode_audit_rows(limit=8, newest_first=True)
    lines = [
        "Growatt diagnostic bundle",
        f"Generated: {generated_at}",
        "",
        "## Redacted Config",
        *_redacted_config_summary(config),
        "",
        "## Service Status",
        format_diagnostic_items("Service checks", items),
        "",
        "## Recent Mode Decisions",
    ]
    if rows:
        for row in rows:
            lines.append(
                f"{row.get('timestamp', '')} | {row.get('command', '')} | "
                f"{row.get('action', '')} | SOC={row.get('soc', '')} | {row.get('note', '')}"
            )
    else:
        lines.append("no audit rows found.")

    lines.extend(["", "## Recent Warnings And Errors"])
    lines.extend(_recent_error_lines(LOG_FILE))
    lines.extend([
        "",
        "## Notes",
        "This bundle is local/read-only and does not call Growatt.",
        "Run `python growatt_power_guard.py health-check` separately for live cloud connectivity.",
    ])
    return "\n".join(lines)


def command_diagnostic_bundle(config: Any) -> int:
    print(build_diagnostic_bundle(config))
    return 0
