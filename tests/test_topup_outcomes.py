import unittest

from growatt_guard.topup_outcomes import build_topup_scorecard


def _start(
    timestamp: str,
    *,
    soc: float = 40,
    minutes: int = 60,
    target: float = 48,
    load_w: float = 800,
    rate_w: float = 3000,
    kind: str = "reserve",
) -> dict[str, str]:
    return {
        "timestamp": timestamp,
        "command": "auto-topup-check",
        "soc": str(soc),
        "action": "auto-topup-started",
        "dry_run": "false",
        "result": "ok",
        "note": (
            f"{minutes}min, planned_min={minutes}, target_soc={target}, "
            f"planned_load_w={load_w}, planned_charge_rate_w={rate_w}, "
            f"plan_kind={kind}"
        ),
    }


def _completion(
    timestamp: str,
    *,
    action: str,
    start_soc: float = 40,
    end_soc: float = 48,
    target: float = 48,
    actual_min: int = 60,
    implied_rate_w: float = 2400,
) -> dict[str, str]:
    return {
        "timestamp": timestamp,
        "command": "topup-complete-check",
        "soc": str(end_soc),
        "action": action,
        "dry_run": "false",
        "result": "ok",
        "note": (
            f"actual_min={actual_min}, start_soc={start_soc}, end_soc={end_soc}, "
            f"target_soc={target}, implied_rate_w={implied_rate_w}"
        ),
    }


class TopupOutcomeTests(unittest.TestCase):
    def test_target_reached_reports_soc_duration_and_measured_grid_import(self):
        rows = [
            _start("2026-08-08T01:00:00"),
            _completion("2026-08-08T02:00:00", action="topup-target-reached"),
        ]
        history = [
            {"timestamp": "2026-08-08T01:00:00", "load_w": 800, "grid_today_kwh": 1.0},
            {"timestamp": "2026-08-08T01:20:00", "load_w": 900, "grid_today_kwh": 1.8},
            {"timestamp": "2026-08-08T01:40:00", "load_w": 850, "grid_today_kwh": 2.4},
            {"timestamp": "2026-08-08T02:00:00", "load_w": 800, "grid_today_kwh": 3.0},
        ]

        scorecard = build_topup_scorecard(rows, history, configured_charge_rate_w=3000)

        outcome = scorecard.outcomes[0]
        self.assertEqual(outcome.classification, "target-reached")
        self.assertEqual(outcome.grid_use_class, "planned-reserve")
        self.assertEqual(outcome.soc_gain, 8)
        self.assertEqual(outcome.planned_minutes, 60)
        self.assertEqual(outcome.actual_minutes, 60)
        self.assertEqual(outcome.grid_import_kwh, 2.0)
        self.assertTrue(outcome.metric_coverage_complete)

    def test_expiry_near_target_is_not_called_inefficient(self):
        rows = [
            _start("2026-08-08T01:00:00", kind="late-safety"),
            _completion(
                "2026-08-08T02:00:00",
                action="topup-expired",
                end_soc=47,
            ),
        ]

        scorecard = build_topup_scorecard(rows, [], configured_charge_rate_w=3000)

        self.assertEqual(scorecard.outcomes[0].classification, "near-target-expiry")
        self.assertEqual(scorecard.outcomes[0].grid_use_class, "expected-safety-hold")

    def test_expiry_with_low_implied_rate_is_insufficient_charge(self):
        rows = [
            _start("2026-08-08T01:00:00"),
            _completion(
                "2026-08-08T02:00:00",
                action="topup-expired",
                end_soc=43,
                implied_rate_w=900,
            ),
        ]

        scorecard = build_topup_scorecard(rows, [], configured_charge_rate_w=3000)

        self.assertEqual(
            scorecard.outcomes[0].classification,
            "insufficient-utility-charge",
        )
        self.assertEqual(scorecard.outcomes[0].grid_use_class, "inefficient")

    def test_expiry_with_sustained_higher_load_is_load_overrun(self):
        rows = [
            _start("2026-08-08T01:00:00", load_w=500),
            _completion(
                "2026-08-08T02:00:00",
                action="topup-expired",
                end_soc=43,
                implied_rate_w=2200,
            ),
        ]
        history = [
            {"timestamp": "2026-08-08T01:00:00", "load_w": 900, "grid_today_kwh": 1.0},
            {"timestamp": "2026-08-08T01:20:00", "load_w": 1000, "grid_today_kwh": 1.8},
            {"timestamp": "2026-08-08T01:40:00", "load_w": 950, "grid_today_kwh": 2.4},
            {"timestamp": "2026-08-08T02:00:00", "load_w": 900, "grid_today_kwh": 3.0},
        ]

        scorecard = build_topup_scorecard(rows, history, configured_charge_rate_w=3000)

        self.assertEqual(scorecard.outcomes[0].classification, "load-overrun")

    def test_three_comparable_successes_support_current_settings(self):
        rows: list[dict[str, str]] = []
        for day, start_soc in ((1, 40), (2, 39), (3, 41)):
            rows.extend(
                [
                    _start(
                        f"2026-08-0{day}T01:00:00",
                        soc=start_soc,
                        load_w=800 + day * 10,
                    ),
                    _completion(
                        f"2026-08-0{day}T02:00:00",
                        action="topup-target-reached",
                        start_soc=start_soc,
                    ),
                ]
            )

        scorecard = build_topup_scorecard(rows, [], configured_charge_rate_w=3000)

        self.assertEqual(scorecard.comparable_count, 3)
        self.assertEqual(scorecard.recommendation_status, "keep-current")
        self.assertIn("keep them unchanged", scorecard.recommendation)

    def test_incomplete_evidence_holds_settings_and_never_claims_avoidable(self):
        rows = [
            _start("2026-08-08T01:00:00"),
            {
                "timestamp": "2026-08-08T02:00:00",
                "command": "topup-complete-check",
                "soc": "43",
                "action": "topup-expired",
                "dry_run": "false",
                "result": "ok",
                "note": "actual_min=60",
            },
        ]

        scorecard = build_topup_scorecard(rows, [], configured_charge_rate_w=3000)

        self.assertEqual(scorecard.outcomes[0].classification, "unknown")
        self.assertEqual(scorecard.recommendation_status, "hold")
        self.assertNotIn("avoidable", str(scorecard.to_dict()).lower())

    def test_preserve_hold_completion_is_not_counted_as_auto_topup(self):
        rows = [
            _completion(
                "2026-08-08T02:00:00",
                action="topup-target-reached",
            )
        ]

        scorecard = build_topup_scorecard(rows, [], configured_charge_rate_w=3000)

        self.assertEqual(scorecard.outcomes, ())


if __name__ == "__main__":
    unittest.main()
