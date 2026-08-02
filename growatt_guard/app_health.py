from __future__ import annotations

import datetime as dt
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import requests

from growatt_guard.config import AppHealthTarget
from growatt_guard.notifications import (
    embed_app_health_auto_recovered,
    embed_app_health_failed,
    embed_app_health_recovered,
    send_discord_embed,
)
from growatt_guard.state import (
    parse_utc_datetime,
    read_app_health_monitor_state,
    utc_now,
    write_app_health_monitor_state,
)


@dataclass(frozen=True)
class AppHealthResult:
    healthy: bool
    detail: str
    status_code: int | None = None


def _clean_detail(value: Any, limit: int = 300) -> str:
    return " ".join(str(value).split())[:limit]


def _entry(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _failure_count(entry: dict[str, Any]) -> int:
    try:
        return max(0, int(entry.get("consecutive_failures", 0)))
    except (TypeError, ValueError):
        return 0


def probe_app_health(target: AppHealthTarget, timeout_seconds: float) -> AppHealthResult:
    try:
        response = requests.get(target.url, timeout=timeout_seconds)
    except requests.RequestException as exc:
        return AppHealthResult(False, _clean_detail(exc))
    if 200 <= response.status_code < 300:
        return AppHealthResult(True, f"HTTP {response.status_code}", response.status_code)
    return AppHealthResult(
        False,
        f"HTTP {response.status_code}: {_clean_detail(response.text, 200) or 'unhealthy response'}",
        response.status_code,
    )


def restart_app_container(target: AppHealthTarget) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["docker", "restart", "--time", "10", target.container],
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, _clean_detail(exc)
    if result.returncode == 0:
        return True, "container restart completed"
    detail = _clean_detail(result.stderr or result.stdout or f"exit code {result.returncode}")
    return False, detail


def _recovery_allowed(entry: dict[str, Any], now: dt.datetime, cooldown_minutes: float) -> bool:
    if entry.get("recovery_attempted"):
        return False
    last_attempt = str(entry.get("last_recovery_attempt_at") or "")
    if not last_attempt:
        return True
    try:
        elapsed = now - parse_utc_datetime(last_attempt)
    except ValueError:
        return True
    return elapsed.total_seconds() >= cooldown_minutes * 60


def _healthy_entry(entry: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    return {
        "active": False,
        "alerted": False,
        "consecutive_failures": 0,
        "first_failure_at": "",
        "last_checked_at": now.isoformat(),
        "last_error": "",
        "last_recovery_attempt_at": entry.get("last_recovery_attempt_at", ""),
        "recovery_attempted": False,
    }


def command_app_health_monitor(config: Any) -> int:
    targets = tuple(config.app_health_targets)
    if not targets:
        print("App health monitor disabled: APP_HEALTH_TARGETS is empty.")
        return 0

    now = utc_now()
    threshold = max(1, int(config.app_health_failure_threshold))
    timeout_seconds = max(0.1, float(config.app_health_timeout_seconds))
    cooldown_minutes = max(1.0, float(config.app_health_recovery_cooldown_minutes))
    recovery_wait_seconds = max(0.0, float(config.app_health_recovery_wait_seconds))
    state = read_app_health_monitor_state() or {}
    previous_apps = state.get("apps") if isinstance(state.get("apps"), dict) else {}
    apps: dict[str, dict[str, Any]] = {
        target.name.lower(): _entry(previous_apps.get(target.name.lower()))
        for target in targets
    }
    unresolved = False

    for target in targets:
        key = target.name.lower()
        entry = _entry(previous_apps.get(key))
        result = probe_app_health(target, timeout_seconds)

        if result.healthy:
            prior_failures = _failure_count(entry)
            if entry.get("alerted") and config.discord_notify_success:
                send_discord_embed(config, embed_app_health_recovered(target.name, prior_failures))
            apps[key] = _healthy_entry(entry, now)
            print(f"[OK] {target.name}: {result.detail}.")
            continue

        failures = _failure_count(entry) + 1
        entry.update(
            {
                "active": True,
                "consecutive_failures": failures,
                "first_failure_at": entry.get("first_failure_at") or now.isoformat(),
                "last_checked_at": now.isoformat(),
                "last_error": result.detail,
            }
        )
        apps[key] = entry

        if failures < threshold:
            print(f"[WARN] {target.name}: failure {failures}/{threshold}: {result.detail}.")
            continue

        recovery_detail = "automatic recovery disabled"
        auto_recovered = False
        if config.app_health_recovery_enabled and _recovery_allowed(
            entry, now, cooldown_minutes
        ):
            entry["recovery_attempted"] = True
            entry["last_recovery_attempt_at"] = now.isoformat()
            write_app_health_monitor_state({"apps": apps})
            restarted, restart_detail = restart_app_container(target)
            recovery_detail = restart_detail
            if restarted:
                if recovery_wait_seconds > 0:
                    time.sleep(recovery_wait_seconds)
                recovery_probe = probe_app_health(target, timeout_seconds)
                recovery_detail = f"{restart_detail}; {recovery_probe.detail}"
                if recovery_probe.healthy:
                    auto_recovered = True
                    if config.discord_notify_success:
                        send_discord_embed(
                            config,
                            embed_app_health_auto_recovered(target.name, failures, recovery_detail),
                        )
                    apps[key] = _healthy_entry(entry, utc_now())
                    print(f"[RECOVERED] {target.name}: {recovery_detail}.")
                    continue
        elif config.app_health_recovery_enabled:
            recovery_detail = "recovery already attempted for this incident or still in cooldown"

        if (
            not auto_recovered
            and not entry.get("alerted")
            and config.discord_notify_failure
        ):
            if send_discord_embed(
                config,
                embed_app_health_failed(target.name, failures, threshold, result.detail, recovery_detail),
            ):
                entry["alerted"] = True
        unresolved = True
        logging.error(
            "App health failure: app=%s failures=%s threshold=%s detail=%s recovery=%s",
            target.name,
            failures,
            threshold,
            result.detail,
            recovery_detail,
        )
        print(f"[FAIL] {target.name}: {failures} consecutive failures; {recovery_detail}.")

    write_app_health_monitor_state({"apps": apps})
    return 1 if unresolved else 0
