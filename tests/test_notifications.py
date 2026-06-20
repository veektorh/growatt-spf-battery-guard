import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_power_guard import (
    notify_failure,
    read_growatt_cloud_failure_state,
    record_growatt_cloud_success,
    send_discord_message,
    truncate_discord_message,
)
from growatt_guard.notifications import (
    embed_automation_failure,
    embed_battery_alert,
    embed_battery_cleared,
    embed_cloud_failure,
    embed_cloud_recovered,
    embed_mode_not_confirmed,
    embed_mode_switch_sbu,
    embed_mode_switch_utility,
    embed_preserve_skipped,
    embed_summary,
    embed_watchdog_failed,
    embed_watchdog_repaired,
)

_COLOR_OK = 0x57F287
_COLOR_WARN = 0xFEE75C
_COLOR_FAIL = 0xED4245


class NotificationsTests(unittest.TestCase):
    def test_truncate_discord_message_keeps_short_messages(self):
        self.assertEqual(truncate_discord_message("hello"), "hello")

    def test_truncate_discord_message_limits_long_messages(self):
        self.assertLessEqual(len(truncate_discord_message("x" * 2500)), 1904)

    def test_send_discord_message_posts_json_payload(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")

        class Response:
            status_code = 204
            text = ""

        with patch("growatt_guard.notifications.requests.post", return_value=Response()) as mocked:
            self.assertTrue(send_discord_message(config, "hello"))

        self.assertEqual(mocked.call_args.args[0], "https://discord.com/api/webhooks/example")
        self.assertEqual(mocked.call_args.kwargs["json"]["content"], "hello")
        self.assertIn("User-Agent", mocked.call_args.kwargs["headers"])

    def test_growatt_cloud_failures_alert_only_after_threshold(self):
        config = make_config(
            discord_webhook_url="https://discord.com/api/webhooks/example",
            cloud_failure_alert_threshold=3,
        )

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch("growatt_guard.notifications.send_discord_embed", return_value=True) as send_mock:
            notify_failure(config, "status", "Growatt login failed: temporary cloud error")
            notify_failure(config, "status", "Growatt login failed: temporary cloud error")
            self.assertEqual(send_mock.call_count, 0)

            notify_failure(config, "status", "Growatt login failed: temporary cloud error")
            state = read_growatt_cloud_failure_state()

        self.assertEqual(send_mock.call_count, 1)
        self.assertIsNotNone(state)
        self.assertEqual(state["count"], 3)
        self.assertTrue(state["alerted"])

    def test_growatt_cloud_success_clears_alerted_streak(self):
        config = make_config(
            discord_webhook_url="https://discord.com/api/webhooks/example",
            cloud_failure_alert_threshold=2,
        )

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch("growatt_guard.notifications.send_discord_embed", return_value=True) as send_mock:
            notify_failure(config, "status", "Growatt login failed: temporary cloud error")
            notify_failure(config, "status", "Growatt login failed: temporary cloud error")
            record_growatt_cloud_success(config)
            state = read_growatt_cloud_failure_state()

        self.assertIsNone(state)
        self.assertEqual(send_mock.call_count, 2)

    def test_non_cloud_failure_sends_embed(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch("growatt_guard.notifications.send_discord_embed", return_value=True) as send_mock:
            notify_failure(config, "validate-schedule", "schedule.json is invalid")

        self.assertEqual(send_mock.call_count, 1)
        embed = send_mock.call_args.args[1]
        self.assertEqual(embed["color"], _COLOR_FAIL)
        field_names = [f["name"] for f in embed["fields"]]
        self.assertIn("Command", field_names)
        self.assertIn("Error", field_names)


class EmbedBuilderTests(unittest.TestCase):
    def _field_names(self, embed):
        return [f["name"] for f in embed["fields"]]

    def test_embed_mode_switch_utility_color_and_fields(self):
        embed = embed_mode_switch_utility(soc=45.0, previous_mode="SBU priority")
        self.assertEqual(embed["color"], _COLOR_WARN)
        self.assertIn("Utility", embed["title"])
        self.assertIn("Battery SOC", self._field_names(embed))
        self.assertIn("Mode", self._field_names(embed))

    def test_embed_mode_switch_utility_optional_fields(self):
        embed = embed_mode_switch_utility(
            soc=40.0, previous_mode="SBU", threshold=45.0,
            weather_category="rainy", reason="below threshold",
        )
        self.assertIn("Threshold", self._field_names(embed))
        self.assertIn("Reason", self._field_names(embed))

    def test_embed_mode_switch_sbu_color_and_fields(self):
        embed = embed_mode_switch_sbu(soc=70.0, previous_mode="Utility first")
        self.assertEqual(embed["color"], _COLOR_OK)
        self.assertIn("SBU", embed["title"])
        self.assertIn("Battery SOC", self._field_names(embed))
        self.assertIn("Mode", self._field_names(embed))

    def test_embed_mode_not_confirmed_is_red(self):
        embed = embed_mode_not_confirmed("return-sbu", "SBU priority")
        self.assertEqual(embed["color"], _COLOR_FAIL)
        self.assertIn("Command", self._field_names(embed))
        self.assertIn("Expected", self._field_names(embed))

    def test_embed_preserve_skipped_color_and_fields(self):
        embed = embed_preserve_skipped(soc=60.0, threshold=45.0, weather_category="sunny", reason="above threshold")
        self.assertEqual(embed["color"], _COLOR_OK)
        self.assertIn("Battery SOC", self._field_names(embed))
        self.assertIn("Threshold", self._field_names(embed))

    def test_embed_watchdog_failed_is_red(self):
        embed = embed_watchdog_failed("could not verify mode")
        self.assertEqual(embed["color"], _COLOR_FAIL)
        self.assertIn("Detail", self._field_names(embed))

    def test_embed_watchdog_repaired_is_yellow(self):
        embed = embed_watchdog_repaired(soc=65.0, previous_mode="Utility first")
        self.assertEqual(embed["color"], _COLOR_WARN)
        self.assertIn("Was", self._field_names(embed))
        self.assertIn("Repaired to", self._field_names(embed))

    def test_embed_battery_alert_is_red_with_threshold(self):
        embed = embed_battery_alert(soc=18.0, threshold=20.0, output_mode="SBU priority")
        self.assertEqual(embed["color"], _COLOR_FAIL)
        self.assertIn("Battery SOC", self._field_names(embed))
        self.assertIn("Threshold", self._field_names(embed))

    def test_embed_battery_cleared_is_green(self):
        embed = embed_battery_cleared(soc=25.0, recovery_soc=22.0, output_mode="SBU priority")
        self.assertEqual(embed["color"], _COLOR_OK)
        self.assertIn("Battery SOC", self._field_names(embed))
        self.assertIn("Recovery threshold", self._field_names(embed))

    def test_embed_cloud_failure_fields(self):
        embed = embed_cloud_failure("status", 3, 3, "connection error")
        self.assertEqual(embed["color"], _COLOR_FAIL)
        self.assertIn("Command", self._field_names(embed))
        self.assertIn("Failures", self._field_names(embed))
        self.assertIn("Latest error", self._field_names(embed))

    def test_embed_cloud_recovered_is_green(self):
        embed = embed_cloud_recovered(5)
        self.assertEqual(embed["color"], _COLOR_OK)

    def test_embed_automation_failure_fields(self):
        embed = embed_automation_failure("validate-schedule", "Invalid JSON at line 3")
        self.assertEqual(embed["color"], _COLOR_FAIL)
        self.assertIn("validate-schedule", embed["title"])
        self.assertIn("Command", self._field_names(embed))
        self.assertIn("Error", self._field_names(embed))

    def test_embed_automation_failure_truncates_long_message(self):
        long_msg = "x" * 2000
        embed = embed_automation_failure("cmd", long_msg)
        error_field = next(f for f in embed["fields"] if f["name"] == "Error")
        self.assertLessEqual(len(error_field["value"]), 1024)

    def test_embed_summary_uses_description(self):
        embed = embed_summary("Daily Summary", "line1\nline2\nline3")
        self.assertEqual(embed["color"], _COLOR_OK)
        self.assertEqual(embed["title"], "Daily Summary")
        self.assertIn("line1", embed["description"])
        self.assertEqual(embed["fields"], [])

    def test_embed_summary_truncates_long_text(self):
        embed = embed_summary("Monthly Summary", "x" * 5000)
        self.assertLessEqual(len(embed["description"]), 4096)

    def test_all_embeds_have_timestamp(self):
        embeds = [
            embed_mode_switch_utility(50.0, "SBU"),
            embed_mode_switch_sbu(70.0, "Utility first"),
            embed_mode_not_confirmed("cmd", "mode"),
            embed_preserve_skipped(60.0, 45.0, "sunny", "above"),
            embed_watchdog_failed("detail"),
            embed_watchdog_repaired(60.0, "Utility"),
            embed_battery_alert(15.0, 20.0, "SBU"),
            embed_battery_cleared(25.0, 22.0, "SBU"),
            embed_cloud_failure("status", 3, 3, "err"),
            embed_cloud_recovered(3),
            embed_automation_failure("cmd", "err"),
            embed_summary("Summary", "text"),
        ]
        for embed in embeds:
            self.assertIn("timestamp", embed, f"Missing timestamp in: {embed.get('title')}")


if __name__ == "__main__":
    unittest.main()
