import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from helpers import make_config
from growatt_guard.discord_control import (
    build_dashboard_embed,
    build_health_embed,
    command_result_text,
    is_authorized_interaction,
    trim_output,
    validate_control_config,
)
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import summarize_status


class FakeEmbed:
    def __init__(self, title=None, description=None, color=None):
        self.title = title
        self.description = description
        self.color = color
        self.fields = []
        self.footer = None
        self.timestamp = None

    def add_field(self, name, value, inline=False):
        self.fields.append({"name": name, "value": value, "inline": inline})

    def set_footer(self, text):
        self.footer = text


class FakeDiscord:
    Embed = FakeEmbed


class DiscordControlTests(unittest.TestCase):
    def test_validate_control_config_requires_bot_token(self):
        config = make_config(
            discord_control_channel_id="123",
            discord_control_allowed_user_ids=("456",),
        )

        with self.assertRaises(GrowattGuardError):
            validate_control_config(config)

    def test_authorized_interaction_matches_user_and_channel(self):
        config = make_config(
            discord_bot_token="token",
            discord_control_channel_id="123",
            discord_control_allowed_user_ids=("456",),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=456),
            channel=SimpleNamespace(id=123),
        )

        self.assertTrue(is_authorized_interaction(config, interaction))

    def test_authorized_interaction_rejects_wrong_user(self):
        config = make_config(
            discord_bot_token="token",
            discord_control_channel_id="123",
            discord_control_allowed_user_ids=("456",),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=999),
            channel=SimpleNamespace(id=123),
        )

        self.assertFalse(is_authorized_interaction(config, interaction))

    def test_trim_output_keeps_tail(self):
        text = "a" * 10 + "THE_END"

        self.assertEqual(trim_output(text, limit=7), "THE_END")

    def test_command_result_text_formats_code_block(self):
        text = command_result_text("status", 0, "ok")

        self.assertIn("status: OK", text)
        self.assertIn("```text", text)

    def test_build_dashboard_embed_parses_public_fixture_summary(self):
        status = json.loads(
            (Path(__file__).resolve().parent / "fixtures" / "spf_missing_grid_live_power.json").read_text(
                encoding="utf-8"
            )
        )
        output = summarize_status(status, battery_capacity_wh=5120, charge_rate_w=2400)

        embed = build_dashboard_embed(FakeDiscord, output, return_code=0)
        fields = {field["name"]: field["value"] for field in embed.fields}

        self.assertEqual(embed.title, "Growatt Dashboard")
        self.assertEqual(fields["Battery SOC"], "62% · 53.2V")
        self.assertEqual(fields["Output Mode"], "SBU priority [0]")
        self.assertEqual(fields["Battery"], "Charging · 500 W · 3h 53m to full")
        self.assertEqual(fields["Output"], "900 W")

    def test_build_dashboard_embed_degrades_when_summary_lacks_soc_and_mode(self):
        status = json.loads(
            (Path(__file__).resolve().parent / "fixtures" / "spf_missing_soc_output.json").read_text(
                encoding="utf-8"
            )
        )
        output = summarize_status(status, battery_capacity_wh=5120, charge_rate_w=2400, hours_to_sunrise=4)

        embed = build_dashboard_embed(FakeDiscord, output, return_code=0)
        fields = {field["name"]: field["value"] for field in embed.fields}

        self.assertEqual(fields["Battery SOC"], "not found")
        self.assertEqual(fields["Output Mode"], "unknown")
        self.assertEqual(fields["Battery"], "Discharging · 120 W")
        self.assertEqual(fields["Output"], "120 W")
        self.assertEqual(fields["Sunrise in"], "4h 00m")

    def test_build_health_embed_only_shows_problem_checks(self):
        output = "\n".join(
            [
                "Growatt health check - 2026-07-04 06:10",
                "Result: WARN",
                "",
                "[OK] Config: loaded",
                "[WARN] Dashboard freshness: stale",
                "[FAIL] Growatt cloud: login failed",
            ]
        )

        embed = build_health_embed(FakeDiscord, output, return_code=1)

        self.assertIn("1 OK, 1 WARN, 1 FAIL", embed.description)
        self.assertEqual([field["name"] for field in embed.fields], ["⚠️ Dashboard freshness", "❌ Growatt cloud"])
        self.assertNotIn("Config", "\n".join(field["name"] for field in embed.fields))


if __name__ == "__main__":
    unittest.main()
