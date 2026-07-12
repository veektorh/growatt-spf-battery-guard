from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import growatt_guard.state as state_module

from growatt_guard.config import Config
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.state import (
    battery_alert_is_muted,
    clear_battery_alert_mute,
    clear_topup_state,
    clear_utility_hold_state,
    clear_waste_alert_mute,
    parse_utc_datetime,
    read_topup_state,
    topup_is_active,
    utc_now,
    utility_hold_ownership,
    waste_alert_is_muted,
    write_battery_alert_mute,
    write_utility_hold_state,
    write_waste_alert_mute,
)
from growatt_guard.topup_status import build_topup_status_payload


BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = BASE_DIR / "growatt_power_guard.py"
MAX_DISCORD_MESSAGE = 1800

_COLOR_OK = 0x57F287
_COLOR_WARN = 0xFEE75C
_COLOR_FAIL = 0xED4245
_STATUS_ICON = {"OK": "✅", "WARN": "⚠️", "FAIL": "❌"}
_CHECK_RE = re.compile(r"^\[(OK|WARN|FAIL)\]\s+([^:]+):\s+(.+)$")
_HEALTH_EMBED_MAX_PROBLEM_FIELDS = 6
_HEALTH_EMBED_FIELD_LIMIT = 360


def finalize_topup_state_after_sbu(resume_rc: int, sbu_rc: int) -> bool:
    """Clear residual top-up state only after canonical SBU cleanup succeeded.

    command_return_sbu owns Utility-hold cleanup after verification. A remaining
    raw hold file means the command skipped, failed verification, or was blocked;
    in all of those cases Discord must preserve recovery intent.
    """
    if resume_rc != 0 or sbu_rc != 0 or state_module.UTILITY_HOLD_FILE.exists():
        return False
    clear_topup_state()
    return True


def build_topup_status_embed(discord_module: Any, payload: dict[str, Any]) -> Any:
    if not payload.get("active"):
        embed = discord_module.Embed(title="Growatt Top-up Status", color=_COLOR_OK)
        embed.description = "No active Guard-owned top-up."
        embed.timestamp = dt.datetime.now(dt.timezone.utc)
        return embed
    if not payload.get("valid"):
        embed = discord_module.Embed(title="Growatt Top-up Status", color=_COLOR_FAIL)
        embed.description = f"Active top-up state is invalid: {payload.get('error', 'unknown error')}"
        embed.timestamp = dt.datetime.now(dt.timezone.utc)
        return embed

    soc = payload.get("current_soc")
    target = payload.get("target_soc")
    projected_at = parse_utc_datetime(str(payload["projected_completion"]))
    expiry = parse_utc_datetime(str(payload["max_expiry"]))
    color = _COLOR_FAIL if payload.get("warnings") else _COLOR_WARN
    embed = discord_module.Embed(title="Growatt Top-up Status", color=color)
    embed.add_field(name="Battery SOC", value=f"{soc:g}%" if soc is not None else "unavailable", inline=True)
    embed.add_field(name="Target", value=f"{target:g}%" if target is not None else "time-based", inline=True)
    embed.add_field(name="Ownership", value=payload["ownership"], inline=True)
    embed.add_field(name="Policy", value=payload["completion_policy"], inline=True)
    embed.add_field(name="Elapsed", value=_fmt_duration(payload["elapsed_minutes"]), inline=True)
    gain = payload.get("soc_gain")
    embed.add_field(name="SOC Gain", value=f"{gain:+g}%" if gain is not None else "unavailable", inline=True)
    embed.add_field(
        name="Maximum expiry",
        value=f"<t:{int(expiry.timestamp())}:f> (<t:{int(expiry.timestamp())}:R>)",
        inline=False,
    )
    embed.add_field(
        name="Projected completion",
        value=(
            f"<t:{int(projected_at.timestamp())}:f> (<t:{int(projected_at.timestamp())}:R>)\n"
            f"Basis: {payload['projected_basis']}"
        ),
        inline=False,
    )
    if payload.get("reason"):
        embed.add_field(name="Reason", value=payload["reason"], inline=False)
    rates = (
        f"Configured: {payload.get('configured_charge_rate_w') or 'unavailable'} W\n"
        f"Learned: {payload.get('learned_charge_rate_w') or 'unavailable'} W "
        f"({payload.get('learned_charge_rate_samples', 0)} samples)\n"
        f"Observed: {payload.get('observed_charge_rate_w') or 'unavailable'} W"
    )
    embed.add_field(name="Charge Rates", value=rates, inline=False)
    if payload.get("warnings"):
        embed.add_field(name="Warnings", value="\n".join(payload["warnings"]), inline=False)
    embed.timestamp = dt.datetime.now(dt.timezone.utc)
    return embed


def _read_state_file(relative_path: str) -> dict[str, Any] | None:
    path = BASE_DIR / relative_path
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _result_color(status: str) -> int:
    return {"OK": _COLOR_OK, "WARN": _COLOR_WARN}.get(status, _COLOR_FAIL)


def build_health_embed(discord_module: Any, output: str, return_code: int) -> Any:
    lines = output.strip().splitlines()
    title_line = lines[0] if lines else "Growatt health check"
    overall = "FAIL"
    parsed_checks: list[tuple[str, str, str]] = []
    for line in lines:
        if line.startswith("Result:"):
            overall = line.split(":", 1)[1].strip()
            continue
        m = _CHECK_RE.match(line)
        if m:
            parsed_checks.append((m.group(1), m.group(2).strip(), m.group(3).strip()))
    if return_code != 0 and overall == "OK":
        overall = "FAIL"

    counts = {status: 0 for status in ("OK", "WARN", "FAIL")}
    for status, _, _ in parsed_checks:
        counts[status] = counts.get(status, 0) + 1
    description = f"{counts['OK']} OK, {counts['WARN']} WARN, {counts['FAIL']} FAIL."
    if counts["WARN"] or counts["FAIL"]:
        description += " Showing only checks that need attention."
    else:
        description += " All checks passed."

    embed = discord_module.Embed(
        title=title_line,
        description=description,
        color=_result_color(overall),
    )
    problem_checks = [check for check in parsed_checks if check[0] != "OK"]
    for status, name, detail in problem_checks[:_HEALTH_EMBED_MAX_PROBLEM_FIELDS]:
        icon = _STATUS_ICON.get(status, "•")
        embed.add_field(name=f"{icon} {name}", value=detail[:_HEALTH_EMBED_FIELD_LIMIT], inline=False)
    overflow = len(problem_checks) - _HEALTH_EMBED_MAX_PROBLEM_FIELDS
    if overflow > 0:
        embed.add_field(name="More checks", value=f"{overflow} additional WARN/FAIL check(s) not shown.", inline=False)
    embed.set_footer(text=f"Overall: {overall}")
    embed.timestamp = dt.datetime.now(dt.timezone.utc)
    return embed


def build_status_embed(discord_module: Any, output: str, return_code: int) -> Any:
    def _extract(pattern: str) -> str:
        m = re.search(pattern, output)
        return m.group(1).strip() if m else "unknown"

    soc_raw = _extract(r"soc=([^,]+)")
    output_raw = _extract(r"output=([^,]+)")
    plant = _extract(r"plant=([^,]+)")
    device = _extract(r"device=([^,]+)")

    soc_clean = re.sub(r"\s*\([^)]+\)", "", soc_raw).strip()
    output_clean = re.sub(r"\s*\([^)]+\)", "", output_raw).strip()

    color = _COLOR_FAIL
    if return_code == 0:
        try:
            soc_val = float(soc_clean.rstrip("%"))
            color = _COLOR_OK if soc_val >= 60 else (_COLOR_WARN if soc_val >= 30 else _COLOR_FAIL)
        except ValueError:
            color = _COLOR_WARN

    embed = discord_module.Embed(title="Growatt Status", color=color)
    embed.add_field(name="Battery SOC", value=soc_clean or "unknown", inline=True)
    embed.add_field(name="Output Mode", value=output_clean or "unknown", inline=True)
    embed.add_field(name="​", value="​", inline=True)
    embed.add_field(name="Plant", value=plant, inline=True)
    embed.add_field(name="Device", value=device, inline=True)
    embed.timestamp = dt.datetime.now(dt.timezone.utc)
    return embed


def _fmt_duration(minutes: int) -> str:
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60:02d}m"
    return f"{minutes}min"


def build_dashboard_embed(discord_module: Any, status_output: str, return_code: int) -> Any:
    def _extract(pattern: str) -> str:
        m = re.search(pattern, status_output)
        return m.group(1).strip() if m else "unknown"

    soc_raw = _extract(r"soc=([^,]+)")
    output_raw = _extract(r"output=([^,]+)")
    soc_clean = re.sub(r"\s*\([^)]+\)", "", soc_raw).strip()
    output_clean = re.sub(r"\s*\([^)]+\)", "", output_raw).strip()
    bat_status_raw = _extract(r"bat_status=([^,]+)")
    out_w_raw = _extract(r"out_w=([^,]+)")
    load_pct_raw = _extract(r"load_pct=([^,]+)")
    bat_w_raw = _extract(r"bat_w=(-?[^,]+)")
    runtime_min_raw = _extract(r"runtime_min=(\d+)")
    charge_min_raw = _extract(r"charge_min=(\d+)")
    vbat_raw = _extract(r"vbat=([^,]+)")
    sunrise_h_raw = _extract(r"sunrise_h=([^,]+)")
    topup_sunrise_raw = _extract(r"topup_sunrise_min=(\d+)")

    color = _COLOR_FAIL
    if return_code == 0:
        try:
            soc_val = float(soc_clean.rstrip("%"))
            color = _COLOR_OK if soc_val >= 60 else (_COLOR_WARN if soc_val >= 30 else _COLOR_FAIL)
        except ValueError:
            color = _COLOR_WARN

    soc_display = soc_clean or "unknown"
    if vbat_raw != "unknown":
        soc_display += f" · {vbat_raw}V"

    embed = discord_module.Embed(title="Growatt Dashboard", color=color)
    embed.add_field(name="Battery SOC", value=soc_display, inline=True)
    embed.add_field(name="Output Mode", value=output_clean or "unknown", inline=True)
    embed.add_field(name="​", value="​", inline=True)

    # Live metrics row: battery (status + power + runtime), output power, load
    bat_value = bat_status_raw if bat_status_raw != "unknown" else ""
    if bat_w_raw != "unknown":
        try:
            bw = float(bat_w_raw)
            if bw != 0:
                bat_value += f" · {abs(bw):g} W" if bat_value else f"{abs(bw):g} W"
        except ValueError:
            pass
    if runtime_min_raw != "unknown":
        try:
            dur = _fmt_duration(int(runtime_min_raw))
            bat_value += f" · {dur} left"
        except ValueError:
            pass
    elif charge_min_raw != "unknown":
        try:
            dur = _fmt_duration(int(charge_min_raw))
            bat_value += f" · {dur} to full"
        except ValueError:
            pass
    embed.add_field(name="Battery", value=bat_value or "—", inline=True)
    embed.add_field(name="Output", value=f"{out_w_raw} W" if out_w_raw != "unknown" else "—", inline=True)
    embed.add_field(name="Load", value=f"{load_pct_raw}%" if load_pct_raw != "unknown" else "—", inline=True)

    # Sunrise / topup-to-sunrise row
    if sunrise_h_raw != "unknown":
        try:
            hrs = float(sunrise_h_raw)
            sunrise_text = _fmt_duration(round(hrs * 60))
            embed.add_field(name="Sunrise in", value=sunrise_text, inline=True)
        except ValueError:
            pass
    if topup_sunrise_raw != "unknown":
        try:
            t = int(topup_sunrise_raw)
            topup_text = "not needed" if t == 0 else _fmt_duration(t)
            embed.add_field(name="Topup to sunrise", value=topup_text, inline=True)
        except ValueError:
            pass

    # PVOutput — from state file, no API call needed
    pvo = _read_state_file("state/pvoutput_last.json")
    if pvo:
        try:
            uploaded_at = dt.datetime.fromisoformat(str(pvo.get("uploaded_at", "")))
            age_min = int((dt.datetime.now() - uploaded_at).total_seconds() // 60)
            fields = pvo.get("fields", {})
            v1 = fields.get("v1")
            v2 = fields.get("v2")
            parts = []
            if v1 is not None:
                parts.append(f"{int(v1) / 1000:.1f} kWh")
            if v2 is not None:
                parts.append(f"{v2} W PV")
            pvo_text = (", ".join(parts) + f" · {age_min} min ago") if parts else f"{age_min} min ago"
            pvo_label = "PVOutput ⚠️" if age_min > 20 else "PVOutput"
            embed.add_field(name=pvo_label, value=pvo_text, inline=True)
        except (ValueError, TypeError):
            embed.add_field(name="PVOutput", value="state unreadable", inline=True)
    else:
        embed.add_field(name="PVOutput", value="no uploads yet", inline=True)

    # Automation pause — from state file
    pause = _read_state_file("state/automation_pause.json")
    automation_label = "Automation"
    automation_value = "active"
    if pause:
        try:
            paused_until = parse_utc_datetime(str(pause["paused_until"]))
            now_utc = dt.datetime.now(dt.timezone.utc)
            if now_utc < paused_until:
                remaining = int((paused_until - now_utc).total_seconds() // 60)
                reason = pause.get("reason", "")
                automation_value = f"paused ~{remaining} min" + (f" · {reason}" if reason else "")
                automation_label = "Automation ⚠️"
        except (KeyError, ValueError):
            pass
    embed.add_field(name=automation_label, value=automation_value, inline=True)

    # Topup — from state file
    topup = read_topup_state()
    if topup:
        if topup_is_active():
            try:
                paused_until = parse_utc_datetime(str(topup["paused_until"]))
                remaining = max(0, int((paused_until - dt.datetime.now(dt.timezone.utc)).total_seconds() // 60))
                embed.add_field(
                    name="Topup ⚡",
                    value=f"{topup.get('minutes', '?')} min total · ~{remaining} min remaining",
                    inline=True,
                )
            except (KeyError, ValueError):
                embed.add_field(name="Topup ⚡", value="active", inline=True)
        else:
            reason = topup.get("reason", "topup")
            embed.add_field(name="Topup ⚡", value=f"interrupted · {reason}", inline=True)

    embed.timestamp = dt.datetime.now(dt.timezone.utc)
    return embed


def trim_output(text: str, limit: int = MAX_DISCORD_MESSAGE) -> str:
    text = text.strip()
    if not text:
        return "(no output)"
    if len(text) <= limit:
        return text
    return text[-limit:]


def validate_control_config(config: Config) -> None:
    if not config.discord_bot_token:
        raise GrowattGuardError("DISCORD_BOT_TOKEN is not configured in .env.")
    if not config.discord_control_channel_id:
        raise GrowattGuardError("DISCORD_CONTROL_CHANNEL_ID is not configured in .env.")
    if not config.discord_control_allowed_user_ids:
        raise GrowattGuardError("DISCORD_CONTROL_ALLOWED_USER_IDS is not configured in .env.")


def _id_matches(value: Any, allowed: str | tuple[str, ...]) -> bool:
    text = str(value)
    if isinstance(allowed, str):
        return text == allowed
    return text in allowed


def is_authorized_interaction(config: Config, interaction: Any) -> bool:
    user_id = getattr(getattr(interaction, "user", None), "id", "")
    channel_id = getattr(getattr(interaction, "channel", None), "id", "")
    return _id_matches(user_id, config.discord_control_allowed_user_ids) and _id_matches(
        channel_id, config.discord_control_channel_id
    )


async def run_guard_command(tokens: list[str], timeout_seconds: int = 1800) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SCRIPT_PATH),
        *tokens,
        cwd=str(BASE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"Command timed out after {timeout_seconds} seconds."

    text = output.decode("utf-8", errors="replace") if output else ""
    return int(proc.returncode or 0), trim_output(text)


def command_result_text(label: str, return_code: int, output: str) -> str:
    status = "OK" if return_code == 0 else f"FAILED ({return_code})"
    return f"{label}: {status}\n```text\n{trim_output(output, 1600)}\n```"


async def run_and_send(interaction: Any, label: str, tokens: list[str], timeout_seconds: int = 1800) -> None:
    await interaction.response.defer(thinking=True)
    return_code, output = await run_guard_command(tokens, timeout_seconds=timeout_seconds)
    await interaction.followup.send(command_result_text(label, return_code, output))


async def _reject(interaction: Any) -> None:
    if interaction.response.is_done():
        await interaction.followup.send("Not authorized.", ephemeral=True)
    else:
        await interaction.response.send_message("Not authorized.", ephemeral=True)


async def _guarded(config: Config, interaction: Any, callback: Any) -> None:
    if not is_authorized_interaction(config, interaction):
        await _reject(interaction)
        return
    await callback()


def command_serve_discord_bot(config: Config) -> int:
    validate_control_config(config)
    try:
        import discord
        from discord import app_commands
    except ImportError as exc:  # pragma: no cover - runtime dependency hint
        raise GrowattGuardError(
            "discord.py is not installed. Run: .venv/bin/python -m pip install -r requirements.txt"
        ) from exc

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    guild = discord.Object(id=int(config.discord_control_guild_id)) if config.discord_control_guild_id else None
    command_scope = {"guild": guild} if guild is not None else {}

    @client.event
    async def on_ready() -> None:
        if guild is not None:
            await tree.sync(guild=guild)
        else:
            await tree.sync()
        print(f"Discord control bot ready as {client.user}.", flush=True)

    @tree.command(name="growatt_status", description="Read Growatt status.", **command_scope)
    async def growatt_status(interaction: discord.Interaction) -> None:
        async def action() -> None:
            await interaction.response.defer(thinking=True)
            return_code, output = await run_guard_command(["status"], timeout_seconds=60)
            try:
                embed = build_status_embed(discord, output, return_code)
                await interaction.followup.send(embed=embed)
            except Exception:
                await interaction.followup.send(command_result_text("status", return_code, output))
        await _guarded(config, interaction, action)

    @tree.command(name="growatt_health", description="Run health-check.", **command_scope)
    async def growatt_health(interaction: discord.Interaction) -> None:
        async def action() -> None:
            await interaction.response.defer(thinking=True)
            return_code, output = await run_guard_command(["health-check"], timeout_seconds=90)
            try:
                embed = build_health_embed(discord, output, return_code)
                await interaction.followup.send(embed=embed)
            except Exception:
                await interaction.followup.send(command_result_text("health-check", return_code, output))
        await _guarded(config, interaction, action)

    @tree.command(name="growatt_dashboard", description="Show key metrics at a glance.", **command_scope)
    async def growatt_dashboard(interaction: discord.Interaction) -> None:
        async def action() -> None:
            await interaction.response.defer(thinking=True)
            return_code, output = await run_guard_command(["status"], timeout_seconds=60)
            try:
                embed = build_dashboard_embed(discord, output, return_code)
                await interaction.followup.send(embed=embed)
            except Exception:
                await interaction.followup.send(command_result_text("dashboard", return_code, output))
        await _guarded(config, interaction, action)

    @tree.command(name="growatt_refresh", description="Refresh dashboard and PVOutput.", **command_scope)
    async def growatt_refresh(interaction: discord.Interaction) -> None:
        await _guarded(
            config,
            interaction,
            lambda: run_and_send(interaction, "observability-refresh", ["observability-refresh"]),
        )

    @tree.command(name="growatt_pause", description="Pause scheduled mode-changing automation.", **command_scope)
    @app_commands.describe(hours="Pause duration in hours", reason="Optional reason")
    async def growatt_pause(interaction: discord.Interaction, hours: float, reason: str = "Discord control") -> None:
        async def action() -> None:
            if hours <= 0 or hours > 24:
                await interaction.response.send_message("Hours must be greater than 0 and no more than 24.", ephemeral=True)
                return
            await run_and_send(interaction, "pause", ["pause", "--hours", str(hours), "--reason", reason])

        await _guarded(config, interaction, action)

    @tree.command(name="growatt_resume", description="Resume scheduled mode-changing automation.", **command_scope)
    async def growatt_resume(interaction: discord.Interaction) -> None:
        await _guarded(config, interaction, lambda: run_and_send(interaction, "resume", ["resume"]))

    @tree.command(name="growatt_sbu", description="Switch back to SBU priority.", **command_scope)
    async def growatt_sbu(interaction: discord.Interaction) -> None:
        await _guarded(config, interaction, lambda: run_and_send(interaction, "return-sbu", ["return-sbu"]))

    @tree.command(name="growatt_utility", description="Switch to Utility first intentionally.", **command_scope)
    @app_commands.describe(reason="Optional reason for the audit log")
    async def growatt_utility(interaction: discord.Interaction, reason: str = "Discord control") -> None:
        await _guarded(
            config,
            interaction,
            lambda: run_and_send(interaction, "force-utility", ["force-utility", "--reason", reason]),
        )

    @tree.command(name="growatt_preserve", description="Run preserve-battery threshold logic.", **command_scope)
    async def growatt_preserve(interaction: discord.Interaction) -> None:
        await _guarded(
            config,
            interaction,
            lambda: run_and_send(interaction, "preserve-battery", ["preserve-battery"]),
        )

    @tree.command(name="growatt_topup", description="Top up on Utility, then return to SBU.", **command_scope)
    @app_commands.describe(
        minutes="Top-up duration in minutes (use this OR target_soc, not both).",
        target_soc="Target SOC % to reach (requires BATTERY_CAPACITY_WH and BATTERY_CHARGE_RATE_W configured).",
    )
    async def growatt_topup(
        interaction: discord.Interaction,
        minutes: int = 0,
        target_soc: int = 0,
    ) -> None:
        async def action() -> None:
            effective_minutes = minutes
            if target_soc > 0:
                if config.battery_capacity_wh <= 0 or config.battery_charge_rate_w <= 0:
                    await interaction.response.send_message(
                        "target_soc requires BATTERY_CAPACITY_WH and BATTERY_CHARGE_RATE_W to be configured.",
                        ephemeral=True,
                    )
                    return
                rc, out = await run_guard_command(["status"])
                m = re.search(r"soc=(\d+(?:\.\d+)?)", out)
                if not m:
                    await interaction.response.send_message(
                        "Could not read current SOC from status. Try again.",
                        ephemeral=True,
                    )
                    return
                current_soc = float(m.group(1))
                if target_soc <= current_soc:
                    await interaction.response.send_message(
                        f"Battery is already at {current_soc:.0f}% — target {target_soc}% is already met.",
                        ephemeral=True,
                    )
                    return
                needed_wh = (target_soc - current_soc) / 100.0 * config.battery_capacity_wh
                effective_minutes = max(1, round(needed_wh / config.battery_charge_rate_w * 60))
                if effective_minutes > config.discord_topup_max_minutes:
                    effective_minutes = config.discord_topup_max_minutes
                    await interaction.channel.send(
                        f"⚠️ Computed duration exceeds max ({config.discord_topup_max_minutes} min); capping."
                    )
            elif minutes <= 0:
                await interaction.response.send_message(
                    "Provide either minutes (1–{}) or target_soc (> current SOC).".format(
                        config.discord_topup_max_minutes
                    ),
                    ephemeral=True,
                )
                return
            elif minutes > config.discord_topup_max_minutes:
                await interaction.response.send_message(
                    f"Minutes must be between 1 and {config.discord_topup_max_minutes}.",
                    ephemeral=True,
                )
                return

            if topup_is_active() or utility_hold_ownership() in ("owned", "adopted"):
                active = read_topup_state()
                active_reason = active.get("reason", "unknown") if active else "active Guard hold"
                await interaction.response.send_message(
                    f"A top-up is already in progress: {active_reason}. Use /growatt_topup_cancel to cancel it.",
                    ephemeral=True,
                )
                return

            reason = f"Discord top-up for {effective_minutes} minute(s)"
            completion_policy = "soc" if target_soc > 0 else "time"
            max_expiry = (
                utc_now() + dt.timedelta(minutes=effective_minutes * 1.2 + 15)
                if completion_policy == "soc"
                else utc_now() + dt.timedelta(minutes=effective_minutes)
            )
            pause_minutes = max(1, math.ceil((max_expiry - utc_now()).total_seconds() / 60))
            write_utility_hold_state(
                ownership="owned",
                target_soc=float(target_soc) if target_soc > 0 else None,
                max_expiry=max_expiry,
                start_soc=current_soc if target_soc > 0 else None,
                completion_policy=completion_policy,
                minutes=effective_minutes,
                reason=reason,
            )

            if target_soc > 0:
                rate_kw = config.battery_charge_rate_w / 1000.0
                hint = f"~{effective_minutes} min ({current_soc:.0f}% → {target_soc}% at {rate_kw:.1f} kW)"
            else:
                hint = f"{effective_minutes} min"
            await interaction.response.send_message(
                f"Starting top-up for {hint}. Completion is persisted and monitored every 10 minutes; "
                "the Discord bot does not need to stay attached.",
            )

            pause_rc, pause_out = await run_guard_command(
                ["pause", "--hours", f"{pause_minutes / 60:.4f}", "--reason", reason]
            )
            if pause_rc != 0:
                clear_utility_hold_state()
                clear_topup_state()
                await interaction.channel.send(command_result_text("topup pause", pause_rc, pause_out))
                return

            utility_rc, utility_out = await run_guard_command(
                ["force-utility", "--reason", reason]
            )
            await interaction.channel.send(command_result_text("topup utility", utility_rc, utility_out))
            if utility_rc != 0:
                resume_rc, resume_out = await run_guard_command(["resume"])
                await interaction.channel.send(command_result_text("topup resume after failure", resume_rc, resume_out))
                await interaction.channel.send(
                    "Top-up ownership state was preserved because the Utility command failed; "
                    "check inverter mode before retrying or cancelling."
                )
                return

            await interaction.channel.send(
                "Top-up started. Use /growatt_topup_status for progress or /growatt_topup_cancel to stop it."
            )

        await _guarded(config, interaction, action)

    @tree.command(name="growatt_topup_status", description="Show active top-up progress and projected completion.", **command_scope)
    async def growatt_topup_status(interaction: discord.Interaction) -> None:
        async def action() -> None:
            await interaction.response.defer(thinking=True, ephemeral=True)
            rc, out = await run_guard_command(["topup-status", "--json"], timeout_seconds=60)
            try:
                payload = json.loads(out) if rc == 0 else {
                    "active": True, "valid": False, "error": out or f"command failed ({rc})"
                }
            except json.JSONDecodeError:
                payload = {"active": True, "valid": False, "error": "topup-status returned invalid JSON"}
            await interaction.followup.send(
                embed=build_topup_status_embed(discord, payload),
                ephemeral=True,
            )

        await _guarded(config, interaction, action)

    @tree.command(name="growatt_topup_cancel", description="Cancel an active top-up and return to SBU.", **command_scope)
    async def growatt_topup_cancel(interaction: discord.Interaction) -> None:
        async def action() -> None:
            if not topup_is_active() and utility_hold_ownership() not in ("owned", "adopted"):
                await interaction.response.send_message("No active top-up to cancel.", ephemeral=True)
                return
            await interaction.response.send_message("Cancelling top-up — resuming automation and returning to SBU.")
            resume_rc, resume_out = await run_guard_command(["resume"])
            await interaction.channel.send(command_result_text("topup cancel resume", resume_rc, resume_out))
            sbu_rc, sbu_out = await run_guard_command(["return-sbu"])
            await interaction.channel.send(command_result_text("topup cancel return-sbu", sbu_rc, sbu_out))
            if not finalize_topup_state_after_sbu(resume_rc, sbu_rc):
                await interaction.channel.send(
                    "Cancellation state preserved: SBU cleanup did not clear Utility ownership. "
                    "Review the command result before further action."
                )

        await _guarded(config, interaction, action)

    @tree.command(name="growatt_snooze_waste", description="Snooze waste-alert notifications for a duration.", **command_scope)
    @app_commands.describe(duration="How long to snooze: '2h', '30m', or 'today'.")
    async def growatt_snooze_waste(interaction: discord.Interaction, duration: str = "today") -> None:
        async def action() -> None:
            rc, out = await run_guard_command(["snooze-waste", duration])
            msg = out.strip() or ("Done." if rc == 0 else "Failed.")
            if rc == 0:
                await interaction.response.send_message(f"✅ {msg}")
            else:
                await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

        await _guarded(config, interaction, action)

    @tree.command(name="growatt_adopt_utility", description="Claim Guard ownership of the current Utility state — auto-returns to SBU at target SOC.", **command_scope)
    @app_commands.describe(target_soc="Target battery SOC % to reach before returning to SBU.")
    async def growatt_adopt_utility(interaction: discord.Interaction, target_soc: int) -> None:
        async def action() -> None:
            rc, out = await run_guard_command(["adopt-utility", str(target_soc)])
            msg = out.strip() or ("Done." if rc == 0 else "Failed.")
            if rc == 0:
                await interaction.response.send_message(f"✅ {msg}")
            else:
                await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

        await _guarded(config, interaction, action)

    @tree.command(name="growatt_mute_alert", description="Permanently mute battery or waste alert notifications.", **command_scope)
    @app_commands.describe(target="Which alert to mute: battery, waste, or both.")
    @app_commands.choices(target=[
        app_commands.Choice(name="Battery alert", value="battery"),
        app_commands.Choice(name="Waste alert", value="waste"),
        app_commands.Choice(name="Both", value="both"),
    ])
    async def growatt_mute_alert(interaction: discord.Interaction, target: str) -> None:
        async def action() -> None:
            muted = []
            if target in ("battery", "both"):
                write_battery_alert_mute()
                muted.append("battery-alert")
            if target in ("waste", "both"):
                write_waste_alert_mute()
                muted.append("waste-alert")
            if not muted:
                await interaction.response.send_message("Unknown target. Choose battery, waste, or both.", ephemeral=True)
                return
            names = " and ".join(muted)
            await interaction.response.send_message(f"🔕 {names} muted. Use /growatt_unmute_alert to re-enable.", ephemeral=True)

        await _guarded(config, interaction, action)

    @tree.command(name="growatt_unmute_alert", description="Re-enable battery or waste alert notifications.", **command_scope)
    @app_commands.describe(target="Which alert to re-enable: battery, waste, or both.")
    @app_commands.choices(target=[
        app_commands.Choice(name="Battery alert", value="battery"),
        app_commands.Choice(name="Waste alert", value="waste"),
        app_commands.Choice(name="Both", value="both"),
    ])
    async def growatt_unmute_alert(interaction: discord.Interaction, target: str) -> None:
        async def action() -> None:
            unmuted = []
            if target in ("battery", "both"):
                clear_battery_alert_mute()
                unmuted.append("battery-alert")
            if target in ("waste", "both"):
                clear_waste_alert_mute()
                unmuted.append("waste-alert")
            if not unmuted:
                await interaction.response.send_message("Unknown target. Choose battery, waste, or both.", ephemeral=True)
                return
            names = " and ".join(unmuted)
            await interaction.response.send_message(f"🔔 {names} re-enabled.", ephemeral=True)

        await _guarded(config, interaction, action)

    client.run(config.discord_bot_token)
    return 0
