from __future__ import annotations

import logging
from typing import Any

import requests

from growatt_guard.state import (
    clear_growatt_cloud_failure_state,
    read_growatt_cloud_failure_state,
    utc_now,
    write_growatt_cloud_failure_state,
)

_COLOR_OK = 0x57F287
_COLOR_WARN = 0xFEE75C
_COLOR_FAIL = 0xED4245


def _f(name: str, value: str, inline: bool = True) -> dict:
    return {"name": name, "value": str(value) or "—", "inline": inline}


def _embed(title: str, color: int, fields: list[dict], description: str = "") -> dict:
    result: dict[str, Any] = {
        "title": title,
        "color": color,
        "fields": fields,
        "timestamp": utc_now().isoformat(),
    }
    if description:
        result["description"] = description
    return result


def truncate_discord_message(message: str) -> str:
    if len(message) <= 1900:
        return message
    return message[:1890] + "...[truncated]"


def _post_webhook(config: Any, payload: dict) -> bool:
    headers = {"User-Agent": "growatt-spf-battery-guard/1.0"}
    try:
        response = requests.post(config.discord_webhook_url, json=payload, headers=headers, timeout=10)
    except requests.RequestException as exc:
        body = ""
        resp = getattr(exc, "response", None)
        if resp is not None and resp.text:
            body = f": {resp.text[:500]}"
        logging.warning("Discord notification failed: %s%s", exc, body)
        return False
    if response.status_code >= 300:
        logging.warning("Discord webhook returned HTTP %s: %s", response.status_code, response.text[:500])
        return False
    return True


def send_discord_message(config: Any, message: str) -> bool:
    if not config.discord_webhook_url:
        return False
    return _post_webhook(config, {"username": "Growatt Guard", "content": truncate_discord_message(message)})


def send_discord_embed(config: Any, embed: dict) -> bool:
    if not config.discord_webhook_url:
        return False
    return _post_webhook(config, {"username": "Growatt Guard", "embeds": [embed]})


# --- Pre-built embed builders ---

def embed_mode_switch_utility(
    soc: float | None,
    previous_mode: str,
    threshold: float | None = None,
    weather_category: str = "",
    reason: str = "",
) -> dict:
    fields = []
    if soc is not None:
        fields.append(_f("Battery SOC", f"{soc:g}%"))
    if threshold is not None:
        thr = f"{threshold:g}%" + (f" ({weather_category})" if weather_category else "")
        fields.append(_f("Threshold", thr))
    fields.append(_f("Mode", f"{previous_mode} → Utility first" if previous_mode else "→ Utility first"))
    if reason:
        fields.append(_f("Reason", reason, inline=False))
    return _embed("Switched to Utility first", _COLOR_WARN, fields)


def embed_mode_switch_sbu(soc: float | None, previous_mode: str) -> dict:
    fields = []
    if soc is not None:
        fields.append(_f("Battery SOC", f"{soc:g}%"))
    fields.append(_f("Mode", f"{previous_mode} → SBU priority" if previous_mode else "→ SBU priority"))
    return _embed("Returned to SBU priority", _COLOR_OK, fields)


def embed_mode_not_confirmed(command: str, expected_mode: str) -> dict:
    fields = [
        _f("Command", command),
        _f("Expected", expected_mode),
        _f("Detail", "Switch command accepted but outputConfig did not update on re-read — check the inverter.", inline=False),
    ]
    return _embed("⚠️ Switch not confirmed", _COLOR_FAIL, fields)


def embed_preserve_skipped(soc: float, threshold: float, weather_category: str, reason: str) -> dict:
    thr = f"{threshold:g}%" + (f" ({weather_category})" if weather_category else "")
    fields = [
        _f("Battery SOC", f"{soc:g}%"),
        _f("Threshold", thr),
        _f("Reason", reason, inline=False),
    ]
    return _embed("SOC above threshold — no switch", _COLOR_OK, fields)


def embed_watchdog_repaired(soc: float | None, previous_mode: str) -> dict:
    fields = []
    if soc is not None:
        fields.append(_f("Battery SOC", f"{soc:g}%"))
    fields.append(_f("Was", previous_mode or "unknown"))
    fields.append(_f("Repaired to", "SBU priority"))
    return _embed("⚠️ SBU watchdog repaired", _COLOR_WARN, fields)


def embed_watchdog_failed(detail: str) -> dict:
    return _embed("❌ SBU watchdog failed", _COLOR_FAIL, [_f("Detail", detail, inline=False)])


def embed_battery_alert(soc: float, threshold: float, output_mode: str) -> dict:
    fields = [
        _f("Battery SOC", f"{soc:g}%"),
        _f("Threshold", f"{threshold:g}%"),
        _f("Output mode", output_mode),
    ]
    return _embed("🔋 Emergency: low battery", _COLOR_FAIL, fields)


def embed_battery_cleared(soc: float, recovery_soc: float, output_mode: str) -> dict:
    fields = [
        _f("Battery SOC", f"{soc:g}%"),
        _f("Recovery threshold", f"{recovery_soc:g}%"),
        _f("Output mode", output_mode),
    ]
    return _embed("✅ Battery alert cleared", _COLOR_OK, fields)


def embed_cloud_failure(command: str, count: int, threshold: int, message: str) -> dict:
    fields = [
        _f("Command", command),
        _f("Failures", f"{count}/{threshold}"),
        _f("Latest error", message[:500], inline=False),
    ]
    return _embed("⚠️ Growatt cloud failures", _COLOR_FAIL, fields)


def embed_cloud_recovered(count: int) -> dict:
    return _embed("✅ Growatt cloud recovered", _COLOR_OK, [_f("Consecutive failures", str(count))])


def embed_automation_failure(command: str, message: str) -> dict:
    fields = [
        _f("Command", command),
        _f("Error", message[:1024], inline=False),
    ]
    return _embed(f"❌ Automation error: {command}", _COLOR_FAIL, fields)


def embed_summary(title: str, text: str) -> dict:
    return _embed(title, _COLOR_OK, [], description=text[:4096])


GROWATT_CLOUD_FAILURE_PATTERNS = (
    "growatt login failed",
    "login succeeded but no user id",
    "no growatt plants found",
    "no devices found",
    "was not found in plant",
    "could not determine plant id",
    "could not determine device serial",
    "could not find battery soc",
    "soc was not found",
    "spF output source was not found".lower(),
    "could not read current spf output source",
    "connectionerror",
    "connecttimeout",
    "readtimeout",
    "read timed out",
    "max retries exceeded",
    "name or service not known",
    "temporary failure in name resolution",
    "failed to establish a new connection",
)


def is_growatt_cloud_failure(message: str) -> bool:
    lower = message.lower()
    return any(pattern in lower for pattern in GROWATT_CLOUD_FAILURE_PATTERNS)


def record_growatt_cloud_failure(config: Any, command: str, message: str) -> None:
    state = read_growatt_cloud_failure_state() or {}
    count = int(state.get("count", 0)) + 1
    threshold = max(1, config.cloud_failure_alert_threshold)
    alerted = bool(state.get("alerted"))
    state.update(
        {
            "count": count,
            "alerted": alerted,
            "first_failure_at": state.get("first_failure_at") or utc_now().isoformat(),
            "last_failure_at": utc_now().isoformat(),
            "last_command": command,
            "last_message": message,
            "threshold": threshold,
        }
    )

    if count >= threshold and not alerted:
        if send_discord_embed(config, embed_cloud_failure(command, count, threshold, message)):
            state["alerted"] = True

    write_growatt_cloud_failure_state(state)


def record_growatt_cloud_success(config: Any) -> None:
    state = read_growatt_cloud_failure_state()
    if not state:
        return
    count = int(state.get("count", 0))
    was_alerted = bool(state.get("alerted"))
    clear_growatt_cloud_failure_state()
    if was_alerted and config.discord_notify_failure:
        send_discord_embed(config, embed_cloud_recovered(count))


def notify_failure(config: Any | None, command: str, message: str) -> None:
    if config is None or not config.discord_notify_failure or command == "test-discord":
        return
    if is_growatt_cloud_failure(message):
        record_growatt_cloud_failure(config, command, message)
        return
    send_discord_embed(config, embed_automation_failure(command, message))


def embed_runtime_alert(runtime_min: float, load_w: float, soc: float) -> dict:
    from growatt_guard.growatt_api import format_duration_minutes
    fields = [
        _f("Battery SOC", f"{soc:g}%"),
        _f("Est. Runtime", format_duration_minutes(runtime_min)),
        _f("Load", f"{load_w:g} W"),
    ]
    return _embed("⚠️ Low battery runtime", _COLOR_WARN, fields)


def embed_runtime_alert_cleared(runtime_min: float | None, soc: float) -> dict:
    from growatt_guard.growatt_api import format_duration_minutes
    rt_str = format_duration_minutes(runtime_min) if runtime_min is not None else "unknown"
    fields = [
        _f("Battery SOC", f"{soc:g}%"),
        _f("Est. Runtime", rt_str),
    ]
    return _embed("✅ Runtime alert cleared", _COLOR_OK, fields)


def embed_topup_skipped_sunny(
    soc: float, skipped_min: int, forecast_kwh_m2: float, threshold_kwh_m2: float
) -> dict:
    from growatt_guard.growatt_api import format_duration_minutes
    fields = [
        _f("Battery SOC", f"{soc:g}%"),
        _f("Skipped topup", format_duration_minutes(skipped_min)),
        _f("Solar forecast", f"{forecast_kwh_m2:.1f} kWh/m²"),
        _f("Threshold", f"{threshold_kwh_m2:g} kWh/m²"),
    ]
    return _embed("☀️ Topup skipped — sunny forecast", _COLOR_OK, fields)


def embed_auto_topup_started(soc: float, topup_min: int, hours_to_sunrise: float, load_w: float) -> dict:
    from growatt_guard.growatt_api import format_duration_minutes
    fields = [
        _f("Battery SOC", f"{soc:g}%"),
        _f("Topup duration", format_duration_minutes(topup_min)),
        _f("Sunrise in", format_duration_minutes(hours_to_sunrise * 60)),
        _f("Load", f"{load_w:g} W"),
    ]
    return _embed("⚡ Auto-topup started", _COLOR_WARN, fields)


def embed_topup_complete_summary(
    start_soc: float,
    end_soc: float,
    actual_min: float,
    implied_rate_w: float,
    config_rate_w: float,
    avg_rate_w: float | None = None,
    reading_count: int = 1,
) -> dict:
    from growatt_guard.growatt_api import format_duration_minutes
    rate_str = f"{implied_rate_w:.0f} W"
    fields = [
        _f("SOC", f"{start_soc:.0f}% → {end_soc:.0f}% (+{end_soc - start_soc:.0f}%)"),
        _f("Duration", format_duration_minutes(actual_min)),
        _f("Implied charge rate", rate_str),
    ]
    if avg_rate_w is not None and reading_count >= 2:
        tip = f"{avg_rate_w:.0f} W ({reading_count} readings)"
        if config_rate_w > 0:
            diff_pct = (avg_rate_w - config_rate_w) / config_rate_w * 100
            if abs(diff_pct) >= 10:
                tip += f" — consider updating BATTERY_CHARGE_RATE_W={avg_rate_w:.0f}"
        fields.append(_f("Avg charge rate", tip, inline=False))
    elif config_rate_w > 0:
        diff_pct = (implied_rate_w - config_rate_w) / config_rate_w * 100
        if abs(diff_pct) >= 10:
            fields.append(_f("Tip", f"Consider BATTERY_CHARGE_RATE_W={implied_rate_w:.0f} (configured {config_rate_w:g} W)", inline=False))
    return _embed("✅ Auto-topup complete", _COLOR_OK, fields)
