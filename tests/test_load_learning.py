import datetime as dt
import unittest

from growatt_guard.load_learning import select_overnight_load


class OvernightLoadLearningTests(unittest.TestCase):
    def test_uses_matching_weekday_history_after_three_nights(self):
        history = [
            {"rate_w": 1000, "day_type": "weekday"},
            {"rate_w": 1200, "day_type": "weekday"},
            {"rate_w": 1400, "day_type": "weekday"},
            {"rate_w": 3000, "day_type": "weekend"},
        ]

        result = select_overnight_load(
            history,
            now=dt.datetime(2026, 7, 13, 20, 0, tzinfo=dt.timezone.utc),
        )

        self.assertTrue(result["ready"])
        self.assertEqual(result["rate_w"], 1200)
        self.assertEqual(result["source"], "weekday history (3 nights)")

    def test_falls_back_until_matching_evidence_is_ready(self):
        history = [
            {"rate_w": 1000, "day_type": "weekday"},
            {"rate_w": 1400, "day_type": "weekday"},
            {"rate_w": 3000, "day_type": "weekend"},
        ]

        result = select_overnight_load(
            history,
            now=dt.datetime(2026, 7, 13, 20, 0, tzinfo=dt.timezone.utc),
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["rate_w"], 1800)
        self.assertIn("2/3 matching weekday", result["source"])

    def test_old_timestamped_rows_are_classified_without_migration(self):
        history = [
            {"rate_w": 900, "recorded_at": "2026-07-06T20:00:00+00:00"},
            {"rate_w": 1200, "recorded_at": "2026-07-07T20:00:00+00:00"},
            {"rate_w": 1500, "recorded_at": "2026-07-08T20:00:00+00:00"},
        ]

        result = select_overnight_load(
            history,
            now=dt.datetime(2026, 7, 13, 20, 0, tzinfo=dt.timezone.utc),
        )

        self.assertTrue(result["ready"])
        self.assertEqual(result["rate_w"], 1200)


if __name__ == "__main__":
    unittest.main()
