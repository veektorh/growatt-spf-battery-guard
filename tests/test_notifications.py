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
        ), patch("growatt_guard.notifications.send_discord_message", return_value=True) as send_mock:
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
        ), patch("growatt_guard.notifications.send_discord_message", return_value=True) as send_mock:
            notify_failure(config, "status", "Growatt login failed: temporary cloud error")
            notify_failure(config, "status", "Growatt login failed: temporary cloud error")
            record_growatt_cloud_success(config)
            state = read_growatt_cloud_failure_state()

        self.assertIsNone(state)
        self.assertEqual(send_mock.call_count, 2)

    def test_non_cloud_failure_still_alerts_immediately(self):
        config = make_config(discord_webhook_url="https://discord.com/api/webhooks/example")

        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.GROWATT_CLOUD_FAILURE_FILE", Path(tmpdir) / "growatt_cloud_failures.json"
        ), patch("growatt_guard.notifications.send_discord_message", return_value=True) as send_mock:
            notify_failure(config, "validate-schedule", "schedule.json is invalid")

        self.assertEqual(send_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
