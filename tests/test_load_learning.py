import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from growatt_guard.load_learning import select_night_load_profile, select_overnight_load


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


class BandedNightLoadTests(unittest.TestCase):
    TZ = dt.timezone(dt.timedelta(hours=1))

    def _history(self) -> list[dict]:
        return [
            {
                "rate_w": 2450,
                "day_type": "weekday",
                "sample_date": "2026-11-10",
                "bands": {
                    "evening": {"rate_w": 2450, "sample_count": 6},
                    "overnight": {"rate_w": 1600, "sample_count": 8},
                },
            },
            {
                "rate_w": 2350,
                "day_type": "weekday",
                "sample_date": "2026-11-11",
                "bands": {
                    "evening": {"rate_w": 2350, "sample_count": 5},
                    "overnight": {"rate_w": 1500, "sample_count": 7},
                },
            },
            {
                "rate_w": 2550,
                "day_type": "weekday",
                "sample_date": "2026-11-12",
                "bands": {
                    "evening": {"rate_w": 2550, "sample_count": 4},
                    "overnight": {"rate_w": 1700, "sample_count": 9},
                },
            },
        ]

    def test_one_night_row_carries_both_bands(self):
        from growatt_guard import state as state_mod

        with TemporaryDirectory() as tmpdir, patch.object(
            state_mod, "DISCHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "dr.json"
        ):
            for hour, rate in ((19, 2400), (20, 2600), (1, 1600), (3, 1500)):
                day = 12 if hour >= 18 else 13
                state_mod.append_discharge_rate_reading(
                    rate,
                    recorded_at=dt.datetime(2026, 11, day, hour, 0, tzinfo=self.TZ),
                    aggregate_nightly=True,
                    tz=self.TZ,
                )
            rows = state_mod.read_discharge_rate_history()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sample_date"], "2026-11-12")
        self.assertEqual(rows[0]["day_type"], "weekday")
        self.assertEqual(rows[0]["sample_count"], 4)
        self.assertEqual(rows[0]["bands"]["evening"], {"rate_w": 2500, "sample_count": 2})
        self.assertEqual(rows[0]["bands"]["overnight"], {"rate_w": 1550, "sample_count": 2})

    def test_profile_weights_bands_by_the_remaining_horizon(self):
        result = select_night_load_profile(
            self._history(),
            now=dt.datetime(2026, 11, 12, 18, 20, tzinfo=dt.timezone.utc),
            hours=11.2,
            tz=self.TZ,
        )

        self.assertTrue(result["ready"])
        self.assertAlmostEqual(result["bands"]["evening"]["rate_w"], 2443.33, places=2)
        self.assertAlmostEqual(result["bands"]["overnight"]["rate_w"], 1608.33, places=2)
        self.assertAlmostEqual(result["bands"]["evening"]["hours"], 2.67, places=2)
        self.assertAlmostEqual(result["bands"]["overnight"]["hours"], 8.53, places=2)
        # 2.67h at the evening rate plus 8.53h at the overnight rate.
        self.assertAlmostEqual(result["rate_w"], 1807.1, delta=1.0)

    def test_profile_needs_three_banded_nights_not_three_flat_ones(self):
        banded = {
            "evening": {"rate_w": 2050, "sample_count": 10},
            "overnight": {"rate_w": 2120, "sample_count": 15},
        }
        history = [
            {"rate_w": 2046, "day_type": "weekday", "bands": banded},
            {"rate_w": 1960, "day_type": "weekday"},
            {"rate_w": 1659, "day_type": "weekday"},
        ]

        result = select_night_load_profile(
            history,
            now=dt.datetime(2026, 11, 13, 22, 0, tzinfo=self.TZ),
            hours=8.6,
            tz=self.TZ,
        )

        self.assertFalse(result["ready"])
        self.assertIsNone(result["rate_w"])
        self.assertEqual(result["matching_nights"], 3)
        self.assertEqual(result["banded_nights"], {"evening": 1, "overnight": 1})

    def test_profile_is_not_ready_when_a_needed_band_has_no_history(self):
        history = [
            {
                "rate_w": 2400,
                "day_type": "weekday",
                "bands": {"evening": {"rate_w": 2400, "sample_count": 5}},
            }
        ] * 3

        result = select_night_load_profile(
            history,
            now=dt.datetime(2026, 11, 12, 19, 20, tzinfo=self.TZ),
            hours=11.2,
        )

        self.assertFalse(result["ready"])
        self.assertIsNone(result["rate_w"])
