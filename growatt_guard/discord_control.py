from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from growatt_guard.config import Config
from growatt_guard.exceptions import GrowattGuardError


BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = BASE_DIR / "growatt_power_guard.py"
MAX_DISCORD_MESSAGE = 1800


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
        stderr=asyncio.subprocess.STDOUT,
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
        await _guarded(config, interaction, lambda: run_and_send(interaction, "status", ["status"]))

    @tree.command(name="growatt_health", description="Run health-check.", **command_scope)
    async def growatt_health(interaction: discord.Interaction) -> None:
        await _guarded(config, interaction, lambda: run_and_send(interaction, "health-check", ["health-check"]))

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
    @app_commands.describe(minutes="Top-up duration in minutes")
    async def growatt_topup(interaction: discord.Interaction, minutes: int) -> None:
        async def action() -> None:
            if minutes <= 0 or minutes > config.discord_topup_max_minutes:
                await interaction.response.send_message(
                    f"Minutes must be between 1 and {config.discord_topup_max_minutes}.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                f"Starting top-up for {minutes} minute(s). I will pause automation, switch to Utility, then return to SBU.",
            )
            pause_rc, pause_out = await run_guard_command(
                ["pause", "--hours", f"{minutes / 60:.4f}", "--reason", f"Discord top-up for {minutes} minute(s)"]
            )
            if pause_rc != 0:
                await interaction.channel.send(command_result_text("topup pause", pause_rc, pause_out))
                return

            utility_rc, utility_out = await run_guard_command(
                ["force-utility", "--reason", f"Discord top-up for {minutes} minute(s)"]
            )
            await interaction.channel.send(command_result_text("topup utility", utility_rc, utility_out))
            if utility_rc != 0:
                resume_rc, resume_out = await run_guard_command(["resume"])
                await interaction.channel.send(command_result_text("topup resume after failure", resume_rc, resume_out))
                return

            await asyncio.sleep(minutes * 60)
            resume_rc, resume_out = await run_guard_command(["resume"])
            await interaction.channel.send(command_result_text("topup resume", resume_rc, resume_out))
            sbu_rc, sbu_out = await run_guard_command(["return-sbu"])
            await interaction.channel.send(command_result_text("topup return-sbu", sbu_rc, sbu_out))

        await _guarded(config, interaction, action)

    client.run(config.discord_bot_token)
    return 0
