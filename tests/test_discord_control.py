import unittest
from types import SimpleNamespace

from helpers import make_config
from growatt_guard.discord_control import (
    command_result_text,
    is_authorized_interaction,
    trim_output,
    validate_control_config,
)
from growatt_guard.exceptions import GrowattGuardError


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


if __name__ == "__main__":
    unittest.main()
