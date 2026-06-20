from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import growatt_guard.state as state_module
from growatt_guard.config import Config
from growatt_guard.dashboard import DASHBOARD_FILE, dashboard_freshness
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import extract_soc, extract_spf_output_source, load_context
from growatt_guard.notifications import read_growatt_cloud_failure_state, send_discord_message
from growatt_guard.pvoutput import read_pvoutput_state
from growatt_guard.schedule import (
    check_cron_schedule,
    next_scheduled_runs,
    schedule_job_id,
    validate_schedule,
    validate_schedule_overrides,
)
from growatt_guard.state import (
    command_lock_is_stale,
    parse_utc_datetime,
    pause_message,
    read_command_lock_state,
    read_pause_state,
    read_topup_state,
    topup_is_active,
)
from growatt_guard.weather import choose_preserve_threshold


@dataclass(frozen=True)
class HealthCheckItem:
    name: str
    status: str
    detail: str


def health_result(checks: list[HealthCheckItem]) -> str:
    statuses = {check.status for check in checks}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "OK"


def format_health_report(checks: list[HealthCheckItem]) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    result = health_result(checks)
    lines = [f"Growatt health check - {now}", f"Result: {result}", ""]
    for check in checks:
        detail = " ".join(str(check.detail).split())
        lines.append(f"[{check.status}] {check.name}: {detail}")
    return "\n".join(lines)


def command_health_check(config: Config, notify: bool = False) -> int:
    checks: list[HealthCheckItem] = [
        HealthCheckItem("Config", "OK", ".env loaded and required Growatt credentials are present."),
        HealthCheckItem(
            "Dry run",
            "WARN" if config.dry_run else "OK",
            "DRY_RUN=true; mode-changing commands will only simulate." if config.dry_run else "DRY_RUN=false.",
        ),
    ]

    if config.emergency_soc_recovery <= config.emergency_soc:
        checks.append(
            HealthCheckItem(
                "Emergency alert",
                "WARN",
                (
                    f"alerts below {config.emergency_soc:g}%, but recovery "
                    f"{config.emergency_soc_recovery:g}% is not above the alert threshold."
                ),
            )
        )
    elif not config.discord_webhook_url:
        checks.append(
            HealthCheckItem(
                "Emergency alert",
                "WARN",
                f"alerts below {config.emergency_soc:g}%, but DISCORD_WEBHOOK_URL is not configured.",
            )
        )
    else:
        checks.append(
            HealthCheckItem(
                "Emergency alert",
                "OK",
                f"alerts below {config.emergency_soc:g}% and clears at {config.emergency_soc_recovery:g}%.",
            )
        )

    cloud_state = read_growatt_cloud_failure_state()
    if cloud_state:
        count = int(cloud_state.get("count", 0))
        threshold = int(cloud_state.get("threshold", config.cloud_failure_alert_threshold))
        status = "WARN" if cloud_state.get("alerted") else "OK"
        checks.append(
            HealthCheckItem(
                "Growatt cloud streak",
                status,
                f"{count}/{threshold} consecutive failure(s); last command {cloud_state.get('last_command', 'unknown')}.",
            )
        )
    else:
        checks.append(
            HealthCheckItem(
                "Growatt cloud streak",
                "OK",
                f"no active failure streak; alert threshold is {config.cloud_failure_alert_threshold}.",
            )
        )

    try:
        freshness = dashboard_freshness(DASHBOARD_FILE, config.dashboard_stale_minutes)
    except OSError as exc:
        checks.append(HealthCheckItem("Dashboard freshness", "WARN", f"could not inspect dashboard.html: {exc}"))
    else:
        status = "WARN" if freshness["stale"] else "OK"
        checks.append(
            HealthCheckItem(
                "Dashboard freshness",
                status,
                f"{freshness['reason']}; stale threshold is {config.dashboard_stale_minutes:g} minutes.",
            )
        )

    if config.mode_driver not in {"spf5000", "spf", "custom"}:
        checks.append(
            HealthCheckItem(
                "Mode driver",
                "FAIL",
                f"GROWATT_MODE_DRIVER={config.mode_driver!r} is unsupported; mode changes will fail.",
            )
        )
    elif config.mode_driver == "custom":
        if not config.utility_mode_params:
            checks.append(HealthCheckItem("Utility command", "FAIL", "custom driver missing GROWATT_UTILITY_MODE_PARAMS."))
        if not config.sbu_mode_params:
            checks.append(HealthCheckItem("SBU command", "FAIL", "custom driver missing GROWATT_SBU_MODE_PARAMS."))
        if config.utility_mode_params and config.sbu_mode_params:
            checks.append(HealthCheckItem("Mode driver", "OK", "custom mode driver parameters are configured."))
    else:
        checks.append(HealthCheckItem("Mode driver", "OK", "SPF output-source command driver is configured."))

    schedule: dict[str, Any] | None = None
    try:
        schedule = validate_schedule()
        checks.append(HealthCheckItem("Schedule", "OK", f"{len(schedule['jobs'])} jobs in {schedule['timezone']}."))
    except GrowattGuardError as exc:
        checks.append(HealthCheckItem("Schedule", "FAIL", str(exc)))

    if schedule is not None:
        try:
            overrides = validate_schedule_overrides(schedule)
        except GrowattGuardError as exc:
            checks.append(HealthCheckItem("Schedule overrides", "FAIL", str(exc)))
        else:
            count = len(overrides.get("dates", {}))
            detail = f"{count} date override(s) configured." if count else "no local date overrides configured."
            checks.append(HealthCheckItem("Schedule overrides", "OK", detail))
        checks.extend(check_cron_schedule(schedule))

        now = dt.datetime.now()
        next_runs = next_scheduled_runs(schedule, now=now, limit=1)
        if next_runs:
            run_at, job = next_runs[0]
            job_id = str(job.get("id", "?"))
            minutes_away = int((run_at - now).total_seconds() // 60)
            checks.append(
                HealthCheckItem(
                    "Next job",
                    "OK",
                    f"{job_id} at {run_at.strftime('%H:%M')} (in {minutes_away} min).",
                )
            )

    try:
        _, device, status = load_context(config)
    except Exception as exc:  # noqa: BLE001 - health check continues reporting other checks on cloud failure
        checks.append(HealthCheckItem("Growatt cloud", "FAIL", str(exc)))
    else:
        checks.append(
            HealthCheckItem(
                "Growatt cloud",
                "OK",
                f"login ok; plant={device.plant_id}, device={device.device_sn}, type={device.device_type or 'unknown'}.",
            )
        )

        soc_result = extract_soc(status)
        if soc_result:
            soc, path = soc_result
            checks.append(HealthCheckItem("Battery SOC", "OK", f"{soc:g}% from {path}."))
        else:
            checks.append(HealthCheckItem("Battery SOC", "FAIL", "SOC was not found in the Growatt status response."))

        output_source = extract_spf_output_source(status)
        if output_source:
            raw, label, path = output_source
            checks.append(HealthCheckItem("Output source", "OK", f"{label} [{raw}] from {path}."))
        else:
            checks.append(
                HealthCheckItem("Output source", "FAIL", "SPF output source was not found in the Growatt status response.")
            )

    threshold_decision = choose_preserve_threshold(config)
    threshold_status = "WARN" if threshold_decision.weather_category == "unavailable" else "OK"
    checks.append(
        HealthCheckItem(
            "Preserve threshold",
            threshold_status,
            f"{threshold_decision.threshold:g}% ({threshold_decision.reason}).",
        )
    )

    if getattr(config, "pvoutput_enabled", False):
        pvo_state = read_pvoutput_state()
        if pvo_state is None:
            checks.append(HealthCheckItem("PVOutput", "WARN", "enabled but no successful uploads recorded yet."))
        else:
            try:
                uploaded_at = dt.datetime.fromisoformat(str(pvo_state.get("uploaded_at", "")))
                age_seconds = max(0.0, (dt.datetime.now() - uploaded_at).total_seconds())
                age_min = int(age_seconds // 60)
                status_str = "WARN" if age_seconds > 30 * 60 else "OK"
                checks.append(HealthCheckItem("PVOutput", status_str, f"last upload {age_min} min ago."))
            except (ValueError, TypeError):
                checks.append(HealthCheckItem("PVOutput", "WARN", "upload state file could not be parsed."))

    topup_state = read_topup_state()
    if topup_state:
        try:
            paused_until = parse_utc_datetime(str(topup_state["paused_until"]))
            now_utc = dt.datetime.now(dt.timezone.utc)
            reason = topup_state.get("reason", "Discord top-up")
            if now_utc < paused_until:
                remaining = int((paused_until - now_utc).total_seconds() // 60)
                checks.append(HealthCheckItem("Topup", "WARN", f"active: {reason}; ~{remaining} min remaining."))
            else:
                checks.append(HealthCheckItem("Topup", "WARN", f"state file present but pause has expired — may be interrupted: {reason}."))
        except (KeyError, ValueError):
            checks.append(HealthCheckItem("Topup", "WARN", "topup state file present but could not be parsed."))

    pause_state = read_pause_state()
    if pause_state:
        checks.append(HealthCheckItem("Pause state", "WARN", pause_message(pause_state)))
    elif state_module.PAUSE_FILE.exists():
        checks.append(HealthCheckItem("Pause state", "WARN", "pause file exists but could not be read; automation is active."))
    else:
        checks.append(HealthCheckItem("Pause state", "OK", "automation is active."))

    lock_state = read_command_lock_state()
    if lock_state and command_lock_is_stale():
        checks.append(HealthCheckItem("Command lock", "WARN", "stale mode-command lock file is present."))
    elif lock_state:
        checks.append(
            HealthCheckItem(
                "Command lock",
                "WARN",
                f"{lock_state.get('command', 'unknown command')} has held the mode lock since {lock_state.get('created_at')}.",
            )
        )
    else:
        checks.append(HealthCheckItem("Command lock", "OK", "no active mode-command lock."))

    if notify:
        if not config.discord_webhook_url:
            checks.append(HealthCheckItem("Discord report", "FAIL", "DISCORD_WEBHOOK_URL is not configured."))
        elif send_discord_message(config, format_health_report(checks)):
            checks.append(HealthCheckItem("Discord report", "OK", "health report sent."))
        else:
            checks.append(HealthCheckItem("Discord report", "FAIL", "Discord webhook rejected the health report."))

    print(format_health_report(checks))
    return 1 if health_result(checks) == "FAIL" else 0
