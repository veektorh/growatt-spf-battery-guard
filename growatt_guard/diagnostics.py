from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from growatt_guard.audit import read_mode_audit_rows
from growatt_guard.dashboard import (
    DASHBOARD_FILE,
    dashboard_freshness,
    extract_dashboard_metric_sources,
    extract_dashboard_metrics,
)
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import deep_values, extract_soc, extract_spf_output_source, load_context
from growatt_guard.pvoutput import extract_pvoutput_fields, read_pvoutput_state
from growatt_guard.schedule import (
    check_cron_schedule,
    lint_schedule,
    next_scheduled_runs,
    schedule_job_tokens,
    validate_schedule,
)
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
PV_METRIC_KEYS = {
    "ppv",
    "ppvText",
    "pPv",
    "pPv1",
    "pPv2",
    "ppv1",
    "ppv2",
    "pvPower",
    "pv1Power",
    "pv2Power",
    "epvToday",
    "ePvToday",
    "epvTodayTotal",
    "epv1Today",
    "epv2Today",
    "ePv1Today",
    "ePv2Today",
}
REDACTION_PATTERNS = (
    (re.compile(r"https://discord\.com/api/webhooks/\S+"), "https://discord.com/api/webhooks/[redacted]"),
    (re.compile(r"(GROWATT_USERNAME=)\S+"), r"\1[redacted]"),
    (re.compile(r"(GROWATT_PASSWORD=)\S+"), r"\1[redacted]"),
    (re.compile(r"(DISCORD_WEBHOOK_URL=)\S+"), r"\1[redacted]"),
    (re.compile(r"(DISCORD_BOT_TOKEN=)\S+"), r"\1[redacted]"),
)
SENSITIVE_PROBE_KEY_PARTS = (
    "apikey",
    "apiuser",
    "coord",
    "datalog",
    "deviceid",
    "devicesn",
    "email",
    "ipaddress",
    "latitude",
    "longitude",
    "macaddress",
    "password",
    "plantid",
    "plantname",
    "secret",
    "serial",
    "token",
    "username",
    "webhook",
)
SENSITIVE_PROBE_KEYS = {"ip", "mac", "serverurl", "sn"}


@dataclass(frozen=True)
class DiagnosticItem:
    name: str
    status: str
    detail: str


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def diagnostic_item_dict(item: DiagnosticItem) -> dict[str, str]:
    return {"name": item.name, "status": item.status, "detail": " ".join(str(item.detail).split())}


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _redact_text(value: Any) -> str:
    text = str(value)
    for pattern, replacement in REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _probe_key_is_sensitive(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized in SENSITIVE_PROBE_KEYS or any(part in normalized for part in SENSITIVE_PROBE_KEY_PARTS)


def redact_probe_fixture(data: Any) -> Any:
    """Redact identifiers/secrets from raw probe-like JSON while preserving metrics."""
    if isinstance(data, dict):
        return {
            key: "[redacted]" if _probe_key_is_sensitive(key) else redact_probe_fixture(value)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact_probe_fixture(value) for value in data]
    if isinstance(data, str):
        return _redact_text(data)
    return data


def command_redact_probe(input_path: str, output_path: str = "") -> int:
    source = Path(input_path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GrowattGuardError(f"Could not read JSON probe {input_path}: {exc}") from exc

    text = json.dumps(redact_probe_fixture(data), indent=2, sort_keys=True) + "\n"
    if output_path:
        destination = Path(output_path)
        destination.write_text(text, encoding="utf-8")
        print(f"Redacted probe written to {destination}")
    else:
        print(text, end="")
    return 0


def _fmt_probe_number(value: Any) -> str:
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else f"{value:g}"
    return str(value)


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
        items.extend(DiagnosticItem(check.name, check.status, check.detail) for check in lint_schedule(schedule))
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

    if getattr(config, "pvoutput_enabled", False):
        pvo_state = read_pvoutput_state()
        if pvo_state is None:
            items.append(DiagnosticItem("PVOutput freshness", "WARN", "enabled but no successful upload state found."))
        else:
            try:
                uploaded_at = dt.datetime.fromisoformat(str(pvo_state.get("uploaded_at", "")))
                age_seconds = max(0.0, (dt.datetime.now() - uploaded_at).total_seconds())
            except (TypeError, ValueError):
                items.append(DiagnosticItem("PVOutput freshness", "WARN", "upload state could not be parsed."))
            else:
                age_min = int(age_seconds // 60)
                status = "WARN" if age_seconds > 30 * 60 else "OK"
                items.append(DiagnosticItem("PVOutput freshness", status, f"last upload {age_min} min ago."))
    else:
        items.append(DiagnosticItem("PVOutput freshness", "SKIP", "PVOUTPUT_ENABLED=false."))

    items.extend(_state_items())
    items.extend(_systemd_unit_status(unit) for unit in SERVICE_UNITS)
    return items


def format_diagnostic_items(title: str, items: list[DiagnosticItem]) -> str:
    lines = [title, f"Result: {_overall(items)}", ""]
    for item in items:
        detail = " ".join(str(item.detail).split())
        lines.append(f"[{item.status}] {item.name}: {detail}")
    return "\n".join(lines)


def build_service_status_payload(config: Any) -> dict[str, Any]:
    items = build_service_status(config)
    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "result": _overall(items),
        "items": [diagnostic_item_dict(item) for item in items],
    }


def command_service_status(config: Any, json_output: bool = False) -> int:
    payload = build_service_status_payload(config)
    if json_output:
        _print_json(payload)
        return 1 if payload["result"] == "FAIL" else 0
    items = [DiagnosticItem(item["name"], item["status"], item["detail"]) for item in payload["items"]]
    print(format_diagnostic_items("Growatt service status", items))
    return 1 if payload["result"] == "FAIL" else 0


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
    return [_redact_text(line) for line in matches[-limit:]] if matches else ["no recent warnings or errors."]


def _cloud_summary(config: Any) -> dict[str, Any]:
    try:
        _, _, status = load_context(config)
    except Exception as exc:  # noqa: BLE001 - diagnostics should summarize the failure
        return {"status": "FAIL", "error": _redact_text(exc)}

    soc_result = extract_soc(status)
    output_source = extract_spf_output_source(status)
    summary: dict[str, Any] = {"status": "OK"}
    if soc_result:
        summary["soc"] = {"value": soc_result[0], "source": soc_result[1]}
    if output_source:
        raw, label, path = output_source
        summary["output_source"] = {"raw": raw, "label": label, "source": path}
    return summary


def build_pv_metric_probe_payload(status: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
    generated_at = now or dt.datetime.now().astimezone()
    metrics = extract_dashboard_metrics(status, now=generated_at)
    sources = extract_dashboard_metric_sources(status)
    pvoutput_fields = extract_pvoutput_fields(status, now=generated_at)
    raw_metrics = [
        {"path": path, "key": path.split(".")[-1], "value": value}
        for path, value in deep_values(status)
        if path.split(".")[-1] in PV_METRIC_KEYS
    ]
    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "dashboard": {
            "pv_w": metrics.get("pv_w"),
            "pv_today_kwh": metrics.get("pv_today_kwh"),
            "pv_source": sources.get("pv_w", ""),
            "pv_today_source": sources.get("pv_today_kwh", ""),
        },
        "pvoutput_fields": {k: v for k, v in pvoutput_fields.items() if not k.startswith("_")},
        "raw_metrics": raw_metrics,
    }


def format_pv_metric_probe(payload: dict[str, Any]) -> str:
    dashboard = payload.get("dashboard", {})
    lines = [
        "Growatt PV metric probe",
        f"Generated: {payload.get('generated_at', '')}",
        "",
        "## Dashboard Interpretation",
        f"PV now: {_fmt_probe_number(dashboard.get('pv_w', ''))} W ({dashboard.get('pv_source', '')})",
        f"PV today: {_fmt_probe_number(dashboard.get('pv_today_kwh', ''))} kWh ({dashboard.get('pv_today_source', '')})",
        "",
        "## PVOutput Fields",
    ]
    pvoutput_fields = payload.get("pvoutput_fields", {})
    if pvoutput_fields:
        for key, value in sorted(pvoutput_fields.items()):
            lines.append(f"{key}={value}")
    else:
        lines.append("none")

    lines.extend(["", "## Raw PV Metric Paths"])
    raw_metrics = payload.get("raw_metrics", [])
    if raw_metrics:
        for item in raw_metrics:
            lines.append(f"{item.get('path', '')}={item.get('value', '')}")
    else:
        lines.append("none")
    return "\n".join(lines)


def command_pv_metric_probe(config: Any, json_output: bool = False) -> int:
    _, _, status = load_context(config)
    payload = build_pv_metric_probe_payload(status)
    if json_output:
        _print_json(payload)
    else:
        print(format_pv_metric_probe(payload))
    return 0


def build_diagnostic_bundle_payload(config: Any, include_cloud: bool = False) -> dict[str, Any]:
    items = build_service_status(config)
    rows = read_mode_audit_rows(limit=8, newest_first=True)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "redacted_config": _redacted_config_summary(config),
        "service_status": {
            "result": _overall(items),
            "items": [diagnostic_item_dict(item) for item in items],
        },
        "recent_mode_decisions": rows,
        "recent_warnings_and_errors": _recent_error_lines(LOG_FILE),
        "notes": [
            "This bundle is local/read-only unless include_cloud is true.",
            "Use health-check for the full live readiness report.",
        ],
    }
    if include_cloud:
        payload["cloud_summary"] = _cloud_summary(config)
    return payload


def build_diagnostic_bundle(config: Any, include_cloud: bool = False) -> str:
    payload = build_diagnostic_bundle_payload(config, include_cloud=include_cloud)
    items = build_service_status(config)
    lines = [
        "Growatt diagnostic bundle",
        f"Generated: {payload['generated_at']}",
        "",
        "## Redacted Config",
        *payload["redacted_config"],
        "",
        "## Service Status",
        format_diagnostic_items("Service checks", items),
        "",
        "## Recent Mode Decisions",
    ]
    rows = payload["recent_mode_decisions"]
    if rows:
        for row in rows:
            lines.append(
                f"{row.get('timestamp', '')} | {row.get('command', '')} | "
                f"{row.get('action', '')} | SOC={row.get('soc', '')} | {row.get('note', '')}"
            )
    else:
        lines.append("no audit rows found.")

    lines.extend(["", "## Recent Warnings And Errors"])
    lines.extend(payload["recent_warnings_and_errors"])
    if include_cloud:
        lines.extend(["", "## Live Cloud Summary"])
        cloud = payload.get("cloud_summary", {})
        if cloud.get("status") == "OK":
            soc = cloud.get("soc", {})
            output = cloud.get("output_source", {})
            if soc:
                lines.append(f"SOC={soc.get('value')} from {soc.get('source')}")
            if output:
                lines.append(f"Output={output.get('label')} [{output.get('raw')}] from {output.get('source')}")
        else:
            lines.append(f"FAIL: {cloud.get('error', 'unknown error')}")
    lines.extend([
        "",
        "## Notes",
        "This bundle is local/read-only unless --include-cloud is used.",
        "Run `python growatt_power_guard.py health-check` for the full live readiness report.",
    ])
    return "\n".join(lines)


def command_diagnostic_bundle(config: Any, json_output: bool = False, include_cloud: bool = False) -> int:
    if json_output:
        _print_json(build_diagnostic_bundle_payload(config, include_cloud=include_cloud))
    else:
        print(build_diagnostic_bundle(config, include_cloud=include_cloud))
    return 0
