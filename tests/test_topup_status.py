import datetime as dt
import unittest

from helpers import make_config
from growatt_guard.topup_status import build_topup_status_payload, format_topup_status


class TopupStatusTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 7, 12, 20, 0, tzinfo=dt.timezone.utc)
        self.config = make_config(battery_capacity_wh=30_000, battery_charge_rate_w=3_000)

    def _hold(self, **overrides):
        hold = {
            "ownership": "owned",
            "completion_policy": "soc",
            "started_at": (self.now - dt.timedelta(minutes=30)).isoformat(),
            "max_expiry": (self.now + dt.timedelta(minutes=90)).isoformat(),
            "start_soc": 50,
            "target_soc": 60,
        }
        hold.update(overrides)
        return hold

    def test_observed_soc_gain_revises_projection(self):
        payload = build_topup_status_payload(self._hold(), 56, self.config, now=self.now)

        self.assertEqual(payload["soc_gain"], 6)
        self.assertEqual(payload["observed_charge_rate_w"], 3600)
        self.assertEqual(payload["projected_completion_minutes"], 20)
        self.assertEqual(payload["projected_basis"], "observed SOC gain")

    def test_learned_rate_is_used_before_configured_rate(self):
        payload = build_topup_status_payload(
            self._hold(start_soc=None),
            56,
            self.config,
            learned_rate_w=2400,
            learned_rate_samples=4,
            now=self.now,
        )

        self.assertEqual(payload["projection_charge_rate_w"], 2400)
        self.assertEqual(payload["projected_completion_minutes"], 30)
        self.assertEqual(payload["projected_basis"], "learned rate (4 samples)")

    def test_low_charge_power_warns_after_stall_window(self):
        payload = build_topup_status_payload(self._hold(), 50, self.config, charge_w=25, now=self.now)

        self.assertTrue(any("stalled" in warning for warning in payload["warnings"]))

    def test_inactive_text_does_not_add_percent_to_unavailable_soc(self):
        payload = build_topup_status_payload(None, None, self.config, now=self.now)

        self.assertEqual(format_topup_status(payload), "No active Guard-owned top-up.")


if __name__ == "__main__":
    unittest.main()
