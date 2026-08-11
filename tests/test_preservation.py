import datetime as dt
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from helpers import make_config
from growatt_guard.preservation import (
    SolarBridgeDecision,
    build_preservation_scorecard,
    build_solar_bridge_decision,
)
from growatt_guard.weather import ThresholdDecision


def _forecast(day: dt.date, radiation: list[float]) -> dict:
    return {
        "hourly": {
            "time": [f"{day.isoformat()}T{hour:02d}:00" for hour in range(6, 6 + len(radiation))],
            "shortwave_radiation": radiation,
        }
    }


class SolarBridgeDecisionTests(unittest.TestCase):
    def test_bridge_is_eligible_when_forecast_recovers_target_above_floor(self):
        now = dt.datetime(2026, 8, 11, 6, 30)
        decision = build_solar_bridge_decision(
            forecast=_forecast(now.date(), [0, 500, 900, 900]),
            now=now,
            soc=42,
            target_soc=50,
            safety_floor_soc=35,
            start_hour=6,
            recovery_hour=10,
            load_w=1000,
            load_factor=1.25,
            battery_capacity_wh=30000,
            panel_kwp=10,
            performance_ratio=0.75,
            solar_factor=0.75,
        )

        self.assertTrue(decision.eligible)
        self.assertGreaterEqual(decision.projected_min_soc, 35)
        self.assertGreaterEqual(decision.projected_recovery_soc, 50)
        self.assertEqual(decision.forecast_factor, 0.75)

    def test_bridge_fails_closed_when_projected_minimum_crosses_floor(self):
        now = dt.datetime(2026, 8, 11, 6, 30)
        decision = build_solar_bridge_decision(
            forecast=_forecast(now.date(), [0, 0, 0, 0]),
            now=now,
            soc=36,
            target_soc=50,
            safety_floor_soc=35,
            start_hour=6,
            recovery_hour=10,
            load_w=1500,
            load_factor=1.25,
            battery_capacity_wh=30000,
            panel_kwp=10,
            performance_ratio=0.75,
        )

        self.assertEqual(decision.status, "ineligible")
        self.assertLess(decision.projected_min_soc, 35)

    def test_bridge_fails_closed_for_incomplete_hourly_coverage(self):
        now = dt.datetime(2026, 8, 11, 6, 30)
        decision = build_solar_bridge_decision(
            forecast=_forecast(now.date(), [800]),
            now=now,
            soc=50,
            target_soc=50,
            safety_floor_soc=35,
            start_hour=6,
            recovery_hour=10,
            load_w=1000,
            load_factor=1.25,
            battery_capacity_wh=30000,
            panel_kwp=10,
            performance_ratio=0.75,
        )

        self.assertEqual(decision.status, "unavailable")

    def test_bridge_does_not_apply_before_configured_morning_window(self):
        now = dt.datetime(2026, 8, 11, 2, 0)
        decision = build_solar_bridge_decision(
            forecast=_forecast(now.date(), [900, 900, 900, 900]),
            now=now,
            soc=50,
            target_soc=50,
            safety_floor_soc=35,
            start_hour=6,
            recovery_hour=10,
            load_w=1000,
            load_factor=1.25,
            battery_capacity_wh=30000,
            panel_kwp=10,
            performance_ratio=0.75,
        )

        self.assertEqual(decision.status, "ineligible")

    def test_bridge_fails_closed_for_malformed_forecast_payload(self):
        now = dt.datetime(2026, 8, 11, 6, 30)
        decision = build_solar_bridge_decision(
            forecast=[],  # type: ignore[arg-type]
            now=now,
            soc=50,
            target_soc=50,
            safety_floor_soc=35,
            start_hour=6,
            recovery_hour=10,
            load_w=1000,
            load_factor=1.25,
            battery_capacity_wh=30000,
            panel_kwp=10,
            performance_ratio=0.75,
        )

        self.assertEqual(decision.status, "unavailable")


class PreservationScorecardTests(unittest.TestCase):
    @staticmethod
    def _history(day: dt.date, *, pv_w: float, load_w: float, grid_end: float) -> list[dict]:
        rows = []
        start = dt.datetime.combine(day, dt.time(6, 30))
        for index in range(15):
            at = start + dt.timedelta(minutes=15 * index)
            rows.append(
                {
                    "timestamp": at.isoformat(),
                    "pv_w": pv_w,
                    "load_w": load_w,
                    "grid_today_kwh": min(grid_end, grid_end * index / 5),
                }
            )
        return rows

    @staticmethod
    def _audit(day: dt.date, *, start_soc: float = 42, target_soc: float = 50) -> list[dict[str, str]]:
        prefix = day.isoformat()
        return [
            {
                "timestamp": f"{prefix}T06:30:00",
                "command": "preserve-battery",
                "soc": str(start_soc),
                "threshold": str(target_soc),
                "action": "switch-to-utility",
                "note": "bridge=eligible, projected_min_soc=39, projected_recovery_soc=55",
            },
            {
                "timestamp": f"{prefix}T07:45:00",
                "command": "return-sbu",
                "soc": str(target_soc),
                "action": "switch-to-sbu",
            },
        ]

    def test_scorecard_marks_safe_solar_recovery_as_likely_avoidable(self):
        day = dt.date(2026, 8, 11)
        scorecard = build_preservation_scorecard(
            self._audit(day),
            self._history(day, pv_w=5000, load_w=1000, grid_end=3.8),
            battery_capacity_wh=30000,
            safety_floor_soc=35,
            start_hour=6,
            recovery_hour=10,
        )

        self.assertEqual(scorecard.comparable_count, 1)
        self.assertEqual(scorecard.outcomes[0].classification, "likely-avoidable")
        self.assertEqual(scorecard.outcomes[0].grid_import_kwh, 3.8)

    def test_scorecard_marks_floor_crossing_as_safety_preserving(self):
        day = dt.date(2026, 8, 11)
        scorecard = build_preservation_scorecard(
            self._audit(day, start_soc=36),
            self._history(day, pv_w=0, load_w=2000, grid_end=2.0),
            battery_capacity_wh=30000,
            safety_floor_soc=35,
            start_hour=6,
            recovery_hour=10,
        )

        self.assertEqual(scorecard.outcomes[0].classification, "safety-preserving")

    def test_three_safe_recoveries_recommend_opt_in_trial_review(self):
        rows: list[dict[str, str]] = []
        history: list[dict] = []
        for offset in range(3):
            day = dt.date(2026, 8, 9) + dt.timedelta(days=offset)
            rows.extend(self._audit(day))
            history.extend(self._history(day, pv_w=5000, load_w=1000, grid_end=3.0))

        scorecard = build_preservation_scorecard(
            rows,
            history,
            battery_capacity_wh=30000,
            safety_floor_soc=35,
            start_hour=6,
            recovery_hour=10,
        )

        self.assertEqual(scorecard.comparable_count, 3)
        self.assertEqual(scorecard.recommendation_status, "review-solar-bridge")

    def test_unconfirmed_utility_switch_is_not_comparable(self):
        day = dt.date(2026, 8, 11)
        rows = self._audit(day)
        rows.insert(
            1,
            {
                "timestamp": f"{day.isoformat()}T06:31:00",
                "command": "preserve-battery",
                "soc": "42",
                "threshold": "50",
                "action": "switch-to-utility-unconfirmed",
                "result": "error",
            },
        )
        scorecard = build_preservation_scorecard(
            rows,
            self._history(day, pv_w=5000, load_w=1000, grid_end=3.8),
            battery_capacity_wh=30000,
            safety_floor_soc=35,
            start_hour=6,
            recovery_hour=10,
        )

        self.assertEqual(scorecard.comparable_count, 0)
        self.assertEqual(scorecard.outcomes[0].classification, "unknown")

    def test_historical_hold_without_shadow_decision_is_not_comparable(self):
        day = dt.date(2026, 8, 11)
        rows = self._audit(day)
        rows[0]["note"] = ""
        scorecard = build_preservation_scorecard(
            rows,
            self._history(day, pv_w=5000, load_w=1000, grid_end=3.8),
            battery_capacity_wh=30000,
            safety_floor_soc=35,
            start_hour=6,
            recovery_hour=10,
        )

        self.assertEqual(scorecard.outcomes[0].classification, "likely-avoidable")
        self.assertEqual(scorecard.outcomes[0].bridge_status, "unknown")
        self.assertEqual(scorecard.comparable_count, 0)

    def test_one_floor_crossing_blocks_bridge_recommendation(self):
        rows: list[dict[str, str]] = []
        history: list[dict] = []
        for offset in range(3):
            day = dt.date(2026, 8, 9) + dt.timedelta(days=offset)
            start_soc = 36 if offset == 2 else 42
            rows.extend(self._audit(day, start_soc=start_soc))
            history.extend(
                self._history(
                    day,
                    pv_w=0 if offset == 2 else 5000,
                    load_w=2000 if offset == 2 else 1000,
                    grid_end=3.0,
                )
            )

        scorecard = build_preservation_scorecard(
            rows,
            history,
            battery_capacity_wh=30000,
            safety_floor_soc=35,
            start_hour=6,
            recovery_hour=10,
        )

        self.assertEqual(scorecard.recommendation_status, "keep-current")


class PreserveCommandBridgeTests(unittest.TestCase):
    def test_enabled_eligible_bridge_defers_without_mode_write(self):
        from growatt_guard.modes import command_preserve_battery

        status = {"data": {"soc": 42, "outPutPower": 1000, "outputConfig": "0"}}
        decision = SolarBridgeDecision(
            status="eligible",
            reason="forecast reaches 55% by 10:00 without crossing 39%",
            forecast_pv_kwh=8.0,
            forecast_load_kwh=4.0,
            projected_min_soc=39.0,
            projected_recovery_soc=55.0,
        )
        audit_calls = []
        decision_calls = []
        with (
            patch("growatt_guard.modes.ensure_not_paused", return_value=False),
            patch("growatt_guard.modes.load_context", return_value=(object(), object(), status)),
            patch(
                "growatt_guard.modes.choose_preserve_threshold",
                return_value=ThresholdDecision(50, "rainy", "rainy/cloudy"),
            ),
            patch("growatt_guard.modes.fetch_weather_forecast", return_value={}),
            patch(
                "growatt_guard.modes.summarize_forecast_calibration",
                return_value={"rainy_adjustment_factor": 0.76},
            ),
            patch(
                "growatt_guard.modes.build_solar_bridge_decision",
                side_effect=lambda **kwargs: decision_calls.append(kwargs) or decision,
            ),
            patch("growatt_guard.modes._solar_bridge_evidence_allows", return_value=(True, "ready")),
            patch("growatt_guard.modes.append_mode_audit", side_effect=lambda *a, **kw: audit_calls.append(kw)),
            patch("growatt_guard.modes.set_mode") as set_mode,
            redirect_stdout(StringIO()),
        ):
            result = command_preserve_battery(
                make_config(
                    weather_enabled=True,
                    morning_solar_bridge_enabled=True,
                    battery_capacity_wh=30000,
                    panel_kwp=10,
                )
            )

        self.assertEqual(result, 0)
        set_mode.assert_not_called()
        self.assertEqual(audit_calls[0]["action"], "solar-bridge-deferred")
        self.assertEqual(decision_calls[0]["solar_factor"], 0.76)

    def test_eligible_bridge_without_three_outcomes_retains_utility(self):
        from growatt_guard.modes import command_preserve_battery

        status = {"data": {"soc": 42, "outPutPower": 1000, "outputConfig": "0"}}
        decision = SolarBridgeDecision(
            status="eligible",
            reason="forecast reaches 55%",
            projected_min_soc=39,
            projected_recovery_soc=55,
        )
        audit_calls = []
        with (
            patch("growatt_guard.modes.ensure_not_paused", return_value=False),
            patch("growatt_guard.modes.load_context", return_value=(object(), object(), status)),
            patch(
                "growatt_guard.modes.choose_preserve_threshold",
                return_value=ThresholdDecision(50, "rainy", "rainy/cloudy"),
            ),
            patch("growatt_guard.modes.fetch_weather_forecast", return_value={}),
            patch("growatt_guard.modes.summarize_forecast_calibration", return_value={}),
            patch("growatt_guard.modes.build_solar_bridge_decision", return_value=decision),
            patch(
                "growatt_guard.modes._solar_bridge_evidence_allows",
                return_value=(False, "Need 2 more comparable outcomes."),
            ),
            patch("growatt_guard.modes.write_utility_hold_state"),
            patch(
                "growatt_guard.modes.append_mode_audit",
                side_effect=lambda *args, **kwargs: audit_calls.append(kwargs),
            ),
            patch("growatt_guard.modes.verify_mode_switch", return_value=True),
            patch("growatt_guard.modes.set_mode", return_value="ok") as set_mode,
            redirect_stdout(StringIO()),
        ):
            result = command_preserve_battery(
                make_config(
                    weather_enabled=True,
                    morning_solar_bridge_enabled=True,
                    battery_capacity_wh=30000,
                    panel_kwp=10,
                )
            )

        self.assertEqual(result, 0)
        set_mode.assert_called_once()
        self.assertIn("bridge=eligible", audit_calls[-1]["note"])
        self.assertIn("evidence=hold", audit_calls[-1]["note"])

    def test_enabled_unavailable_bridge_retains_utility_switch(self):
        from growatt_guard.modes import command_preserve_battery

        status = {"data": {"soc": 42, "outPutPower": 1000, "outputConfig": "0"}}
        decision = SolarBridgeDecision("unavailable", "bridge inputs are incomplete")
        with (
            patch("growatt_guard.modes.ensure_not_paused", return_value=False),
            patch("growatt_guard.modes.load_context", return_value=(object(), object(), status)),
            patch(
                "growatt_guard.modes.choose_preserve_threshold",
                return_value=ThresholdDecision(50, "rainy", "rainy/cloudy"),
            ),
            patch("growatt_guard.modes.fetch_weather_forecast", return_value={}),
            patch("growatt_guard.modes.build_solar_bridge_decision", return_value=decision),
            patch("growatt_guard.modes.write_utility_hold_state"),
            patch("growatt_guard.modes.append_mode_audit"),
            patch("growatt_guard.modes.verify_mode_switch", return_value=True),
            patch("growatt_guard.modes.set_mode", return_value="ok") as set_mode,
            redirect_stdout(StringIO()),
        ):
            result = command_preserve_battery(
                make_config(
                    weather_enabled=True,
                    morning_solar_bridge_enabled=True,
                    battery_capacity_wh=30000,
                    panel_kwp=10,
                )
            )

        self.assertEqual(result, 0)
        set_mode.assert_called_once()

    def test_invalid_weather_timezone_fails_closed_to_utility(self):
        from growatt_guard.modes import command_preserve_battery

        status = {"data": {"soc": 42, "outPutPower": 1000, "outputConfig": "0"}}
        decisions = []
        with (
            patch("growatt_guard.modes.ensure_not_paused", return_value=False),
            patch("growatt_guard.modes.load_context", return_value=(object(), object(), status)),
            patch(
                "growatt_guard.modes.choose_preserve_threshold",
                return_value=ThresholdDecision(50, "unavailable", "unavailable"),
            ),
            patch("growatt_guard.modes.fetch_weather_forecast", return_value={"hourly": {}}),
            patch(
                "growatt_guard.modes.build_solar_bridge_decision",
                side_effect=lambda **kwargs: decisions.append(kwargs) or SolarBridgeDecision(
                    "unavailable", "bridge inputs are incomplete"
                ),
            ),
            patch("growatt_guard.modes.write_utility_hold_state"),
            patch("growatt_guard.modes.append_mode_audit"),
            patch("growatt_guard.modes.verify_mode_switch", return_value=True),
            patch("growatt_guard.modes.set_mode", return_value="ok") as set_mode,
            redirect_stdout(StringIO()),
        ):
            result = command_preserve_battery(
                make_config(
                    weather_enabled=True,
                    weather_timezone="Not/AZone",
                    morning_solar_bridge_enabled=True,
                    battery_capacity_wh=30000,
                    panel_kwp=10,
                )
            )

        self.assertEqual(result, 0)
        self.assertIsNone(decisions[0]["forecast"])
        set_mode.assert_called_once()


if __name__ == "__main__":
    unittest.main()
