import datetime as dt
import json
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from helpers import make_config
from growatt_guard.discord_control import (
    build_dashboard_embed,
    build_health_embed,
    build_topup_status_embed,
    build_topup_status_payload,
    command_result_text,
    finalize_topup_state_after_sbu,
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

    def test_topup_state_clears_only_after_resume_sbu_and_hold_cleanup(self):
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.discord_control.state_module.UTILITY_HOLD_FILE", Path(tmpdir) / "utility_hold.json"
        ), patch("growatt_guard.discord_control.clear_topup_state") as clear_topup:
            result = finalize_topup_state_after_sbu(0, 0)

        self.assertTrue(result)
        clear_topup.assert_called_once()

    def test_topup_state_is_preserved_when_sbu_is_blocked(self):
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.discord_control.state_module.UTILITY_HOLD_FILE", Path(tmpdir) / "utility_hold.json"
        ), patch("growatt_guard.discord_control.clear_topup_state") as clear_topup:
            result = finalize_topup_state_after_sbu(0, 2)

        self.assertFalse(result)
        clear_topup.assert_not_called()

    def test_topup_state_is_preserved_when_raw_utility_hold_remains(self):
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.discord_control.state_module.UTILITY_HOLD_FILE", Path(tmpdir) / "utility_hold.json"
        ), patch("growatt_guard.discord_control.clear_topup_state") as clear_topup:
            (Path(tmpdir) / "utility_hold.json").write_text("{}", encoding="utf-8")
            result = finalize_topup_state_after_sbu(0, 0)

        self.assertFalse(result)
        clear_topup.assert_not_called()

    def test_topup_state_is_preserved_when_resume_fails(self):
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.discord_control.state_module.UTILITY_HOLD_FILE", Path(tmpdir) / "utility_hold.json"
        ), patch("growatt_guard.discord_control.clear_topup_state") as clear_topup:
            result = finalize_topup_state_after_sbu(1, 0)

        self.assertFalse(result)
        clear_topup.assert_not_called()

    def test_topup_status_payload_reports_progress_and_projection(self):
        now = dt.datetime(2026, 7, 12, 20, 0, tzinfo=dt.timezone.utc)
        hold = {
            "ownership": "owned",
            "completion_policy": "soc",
            "started_at": (now - dt.timedelta(minutes=30)).isoformat(),
            "max_expiry": (now + dt.timedelta(minutes=90)).isoformat(),
            "target_soc": 60,
            "reason": "Discord top-up",
        }

        payload = build_topup_status_payload(
            hold,
            56,
            make_config(battery_capacity_wh=30_000, battery_charge_rate_w=3_000),
            now=now,
        )

        self.assertTrue(payload["active"])
        self.assertEqual(payload["ownership"], "owned")
        self.assertEqual(payload["current_soc"], 56)
        self.assertEqual(payload["target_soc"], 60)
        self.assertEqual(payload["elapsed_minutes"], 30)
        self.assertEqual(payload["projected_completion_minutes"], 24)
        self.assertEqual(payload["projected_basis"], "configured capacity and charge rate")

    def test_topup_status_payload_uses_expiry_for_time_policy(self):
        now = dt.datetime(2026, 7, 12, 20, 0, tzinfo=dt.timezone.utc)
        expiry = now + dt.timedelta(minutes=45)
        hold = {
            "ownership": "owned",
            "completion_policy": "time",
            "started_at": (now - dt.timedelta(minutes=15)).isoformat(),
            "max_expiry": expiry.isoformat(),
            "minutes": 60,
        }

        payload = build_topup_status_payload(hold, 50, make_config(), now=now)

        self.assertEqual(payload["projected_completion"], expiry)
        self.assertEqual(payload["projected_completion_minutes"], 45)
        self.assertEqual(payload["projected_basis"], "maximum expiry")

    def test_topup_status_embed_contains_requested_fields(self):
        now = dt.datetime(2026, 7, 12, 20, 0, tzinfo=dt.timezone.utc)
        payload = {
            "active": True,
            "valid": True,
            "current_soc": 56.0,
            "target_soc": 60.0,
            "ownership": "owned",
            "completion_policy": "soc",
            "elapsed_minutes": 30,
            "max_expiry": now + dt.timedelta(minutes=90),
            "projected_completion": now + dt.timedelta(minutes=24),
            "projected_basis": "configured capacity and charge rate",
            "reason": "Discord top-up",
        }

        embed = build_topup_status_embed(FakeDiscord, payload)
        fields = {field["name"]: field["value"] for field in embed.fields}

        self.assertEqual(fields["Battery SOC"], "56%")
        self.assertEqual(fields["Target"], "60%")
        self.assertEqual(fields["Ownership"], "owned")
        self.assertEqual(fields["Elapsed"], "30min")
        self.assertIn("Maximum expiry", fields)
        self.assertIn("Projected completion", fields)

    def test_discord_topup_no_longer_sleeps_for_duration(self):
        source = (Path(__file__).resolve().parents[1] / "growatt_guard" / "discord_control.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("await asyncio.sleep(effective_minutes * 60)", source)
        self.assertIn("Completion is persisted and monitored every 10 minutes", source)

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
