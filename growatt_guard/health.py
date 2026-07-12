from __future__ import annotations

import datetime as dt
import shutil
from dataclasses import dataclass
from typing import Any

import requests

import growatt_guard.state as state_module
from growatt_guard.config import Config
from growatt_guard.dashboard import DASHBOARD_FILE, dashboard_freshness
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import extract_soc, extract_spf_output_source, load_context
from growatt_guard.notifications import read_growatt_cloud_failure_state, send_discord_embed, send_discord_message
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
    suggestion: str = ""


_HEALTH_EMBED_MAX_PROBLEM_FIELDS = 6


def disk_usage_check() -> HealthCheckItem:
    """Report free space on the filesystem containing the automation checkout."""
    try:
        usage = shutil.disk_usage(__file__)
    except OSError as exc:
        return HealthCheckItem("Disk space", "WARN", f"could not inspect filesystem: {exc}")

    free_percent = (usage.free / usage.total * 100.0) if usage.total else 0.0
    free_gib = usage.free / (1024 ** 3)
    if free_percent < 5:
        status = "FAIL"
    elif free_percent < 10:
        status = "WARN"
    else:
        status = "OK"
    return HealthCheckItem(
        "Disk space",
        status,
        f"{free_gib:.1f} GiB free ({free_percent:.1f}%); thresholds WARN <10%, FAIL <5%.",
    )


_HEALTH_EMBED_FIELD_LIMIT = 360


def health_result(checks: list[HealthCheckItem]) -> str:
    statuses = {check.status for check in checks}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "OK"


def default_health_suggestion(check: HealthCheckItem) -> str:
    if check.status == "OK":
        return ""
    name = check.name.lower()
    detail = str(check.detail).lower()
    if "dry run" in name:
        return "Set DRY_RUN=false only after status, probe, and schedule checks are healthy."
    if "emergency alert" in name:
        return "Configure DISCORD_WEBHOOK_URL and keep recovery SOC above the alert SOC."
    if "cloud streak" in name or "growatt cloud" in name:
        return "Check logs and avoid repeated manual logins while cooldown/session reuse is protecting the account."
    if "login cooldown" in name:
        return "Wait for cooldown unless you are certain the Growatt lock has cleared."
    if "dashboard freshness" in name:
        return "Run dashboard-refresh --once and check growatt-dashboard-refresh.service."
    if "mode driver" in name:
        return "Use GROWATT_MODE_DRIVER=spf5000 for SPF models unless custom params are intentional."
    if "utility command" in name or "sbu command" in name:
        return "Set the missing custom params or switch back to the SPF driver."
    if "schedule override" in name:
        return "Inspect schedule_overrides.json or remove the bad date override."
    if "disk space" in name:
        return "Free space is low; rotate/prune logs and remove unneeded files before restarting services."
    if "schedule" in name or "cron" in name:
        return "Run validate-schedule, then reinstall cron with install_cloud_cron.sh if needed."
    if "battery soc" in name:
        return "Run probe and add the SOC path to extraction tests."
    if "output source" in name:
        return "Run probe and inspect outputConfig paths."
    if "pvoutput" in name:
        return "Run observability-refresh once and check PVOUTPUT_API_KEY/PVOUTPUT_SYSTEM_ID."
    if "topup" in name and ("expired" in detail or "parse" in detail):
        return "Run topup-complete-check to repair state and return to SBU if needed."
    if "pause state" in name:
        return "Run resume when scheduled mode changes should be active again."
    if "command lock" in name:
        return "Run clear-stale-lock only if the command is no longer active."
    if "discord report" in name:
        return "Set or rotate DISCORD_WEBHOOK_URL, then run test-discord."
    return ""


def format_health_report(checks: list[HealthCheckItem]) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    result = health_result(checks)
    lines = [f"Growatt health check - {now}", f"Result: {result}", ""]
    for check in checks:
        detail = " ".join(str(check.detail).split())
        line = f"[{check.status}] {check.name}: {detail}"
        suggestion = getattr(check, "suggestion", "") or default_health_suggestion(check)
        if suggestion:
            line += f" Next: {' '.join(str(suggestion).split())}"
        lines.append(line)
    return "\n".join(lines)


def health_embed_description(checks: list[HealthCheckItem]) -> str:
    counts = {status: 0 for status in ("OK", "WARN", "FAIL")}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    parts = [f"{counts['OK']} OK", f"{counts['WARN']} WARN", f"{counts['FAIL']} FAIL"]
    if counts["WARN"] or counts["FAIL"]:
        return ", ".join(parts) + ". Showing only checks that need attention."
    return ", ".join(parts) + ". All checks passed."


def health_embed_fields(checks: list[HealthCheckItem]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    problem_checks = [check for check in checks if check.status != "OK"]
    for check in problem_checks[:_HEALTH_EMBED_MAX_PROBLEM_FIELDS]:
        value = " ".join(str(check.detail).split()) or "-"
        suggestion = getattr(check, "suggestion", "") or default_health_suggestion(check)
        if suggestion:
            value = f"{value}\nNext: {' '.join(str(suggestion).split())}"
        fields.append({"name": f"[{check.status}] {check.name}", "value": value[:_HEALTH_EMBED_FIELD_LIMIT], "inline": False})
    overflow = len(problem_checks) - _HEALTH_EMBED_MAX_PROBLEM_FIELDS
    if overflow > 0:
        fields.append({"name": "More checks", "value": f"{overflow} additional WARN/FAIL check(s) not shown.", "inline": False})
    return fields


def command_health_check(config: Config, notify: bool = False) -> int:
    checks: list[HealthCheckItem] = [
        HealthCheckItem("Config", "OK", ".env loaded and required Growatt credentials are present."),
        HealthCheckItem(
            "Dry run",
            "WARN" if config.dry_run else "OK",
            "DRY_RUN=true; mode-changing commands will only simulate." if config.dry_run else "DRY_RUN=false.",
        ),
    ]
    checks.append(disk_usage_check())

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

    cooldown_until = state_module.login_cooldown_until()
    if cooldown_until is not None:
        checks.append(
            HealthCheckItem(
                "Growatt login cooldown",
                "WARN",
                f"account was locked; backing off all logins until {state_module.format_local_time(cooldown_until)}. "
                "Run clear-login-cooldown to override.",
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
        next_runs = next_scheduled_runs(schedule, now=now, limit=3)
        if next_runs:
            run_details: list[str] = []
            for run_at, job in next_runs:
                job_id = str(job.get("id", "?"))
                minutes_away = max(0, int((run_at - now).total_seconds() // 60))
                run_details.append(f"{job_id} at {run_at.strftime('%a %H:%M')} (in {minutes_away} min)")
            checks.append(
                HealthCheckItem(
                    "Next jobs",
                    "OK",
                    "; ".join(run_details) + ".",
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
        else:
            result = health_result(checks)
            color = 0x57F287 if result == "OK" else (0xFEE75C if result == "WARN" else 0xED4245)
            embed = {
                "title": f"Growatt health - {result}",
                "color": color,
                "description": health_embed_description(checks),
                "fields": health_embed_fields(checks),
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            if send_discord_embed(config, embed):
                checks.append(HealthCheckItem("Discord report", "OK", "health report sent."))
            else:
                checks.append(HealthCheckItem("Discord report", "FAIL", "Discord webhook rejected the health report."))

    print(format_health_report(checks))

    if config.betterstack_heartbeat_url:
        try:
            requests.get(config.betterstack_heartbeat_url, timeout=10)
        except Exception:  # noqa: BLE001 - heartbeat ping is best-effort
            pass

    return 1 if health_result(checks) == "FAIL" else 0
