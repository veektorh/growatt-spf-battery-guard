"""Tests for the ownership model, topup/adopt commands, waste alerts, and dashboard."""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.helpers import make_config


# ---------------------------------------------------------------------------
# State layer tests
# ---------------------------------------------------------------------------

class TestUtilityHoldState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._state_dir = Path(self._tmp.name)
        import growatt_guard.state as _state
        self._orig_state_dir = _state.STATE_DIR
        self._orig_hold_file = _state.UTILITY_HOLD_FILE
        self._orig_waste_file = _state.WASTE_ALERT_FILE
        _state.STATE_DIR = self._state_dir
        _state.UTILITY_HOLD_FILE = self._state_dir / "utility_hold.json"
        _state.WASTE_ALERT_FILE = self._state_dir / "waste_alert.json"
        _state.TOPUP_STATE_FILE = self._state_dir / "topup_active.json"

    def tearDown(self):
        import growatt_guard.state as _state
        _state.STATE_DIR = self._orig_state_dir
        _state.UTILITY_HOLD_FILE = self._orig_hold_file
        _state.WASTE_ALERT_FILE = self._orig_waste_file
        self._tmp.cleanup()

    def test_read_missing_returns_none(self):
        from growatt_guard.state import read_utility_hold_state
        self.assertIsNone(read_utility_hold_state())

    def test_write_read_owned(self):
        from growatt_guard.state import (
            write_utility_hold_state, read_utility_hold_state, utility_hold_ownership,
        )
        expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        write_utility_hold_state("owned", 40.0, expiry, start_soc=35.0)
        state = read_utility_hold_state()
        self.assertIsNotNone(state)
        self.assertEqual(state["ownership"], "owned")
        self.assertAlmostEqual(state["target_soc"], 40.0)
        self.assertEqual(utility_hold_ownership(), "owned")

    def test_write_adopted(self):
        from growatt_guard.state import write_utility_hold_state, utility_hold_ownership
        expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        write_utility_hold_state("adopted", 38.0, expiry)
        self.assertEqual(utility_hold_ownership(), "adopted")

    def test_clear(self):
        from growatt_guard.state import (
            write_utility_hold_state, clear_utility_hold_state, utility_hold_ownership,
        )
        expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        write_utility_hold_state("owned", 40.0, expiry)
        clear_utility_hold_state()
        self.assertIsNone(utility_hold_ownership())

    def test_utility_hold_is_active_within_expiry(self):
        from growatt_guard.state import write_utility_hold_state, utility_hold_is_active
        expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        write_utility_hold_state("owned", 40.0, expiry)
        self.assertTrue(utility_hold_is_active())

    def test_utility_hold_is_inactive_after_expiry(self):
        from growatt_guard.state import write_utility_hold_state, utility_hold_is_active
        expiry = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
        write_utility_hold_state("owned", 40.0, expiry)
        self.assertFalse(utility_hold_is_active())

    def test_topup_is_active_includes_hold(self):
        from growatt_guard.state import write_utility_hold_state, topup_is_active
        expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        write_utility_hold_state("owned", 40.0, expiry)
        self.assertTrue(topup_is_active())

    def test_topup_is_active_expired_hold(self):
        from growatt_guard.state import write_utility_hold_state, topup_is_active
        expiry = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        write_utility_hold_state("owned", 40.0, expiry)
        self.assertFalse(topup_is_active())


class TestWasteAlertState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._state_dir = Path(self._tmp.name)
        import growatt_guard.state as _state
        self._orig = _state.WASTE_ALERT_FILE
        _state.WASTE_ALERT_FILE = self._state_dir / "waste_alert.json"

    def tearDown(self):
        import growatt_guard.state as _state
        _state.WASTE_ALERT_FILE = self._orig
        self._tmp.cleanup()

    def test_not_snoozed_initially(self):
        from growatt_guard.state import waste_alert_is_snoozed
        self.assertFalse(waste_alert_is_snoozed())

    def test_snooze_active(self):
        from growatt_guard.state import write_waste_alert_snooze, waste_alert_is_snoozed
        until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
        write_waste_alert_snooze(until)
        self.assertTrue(waste_alert_is_snoozed())

    def test_snooze_expired(self):
        from growatt_guard.state import write_waste_alert_snooze, waste_alert_is_snoozed
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        write_waste_alert_snooze(past)
        self.assertFalse(waste_alert_is_snoozed())

    def test_alert_due_initially(self):
        from growatt_guard.state import waste_alert_is_due
        self.assertTrue(waste_alert_is_due(cooldown_minutes=30.0))

    def test_alert_not_due_after_send(self):
        from growatt_guard.state import write_waste_alert_last_sent, waste_alert_is_due
        write_waste_alert_last_sent()
        self.assertFalse(waste_alert_is_due(cooldown_minutes=30.0))

    def test_alert_due_after_cooldown(self):
        from growatt_guard.state import write_waste_alert_last_sent, waste_alert_is_due
        import growatt_guard.state as _state
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=35)
        state = {"last_sent_at": past.isoformat()}
        import json, os
        _state.WASTE_ALERT_FILE.write_text(json.dumps(state), encoding="utf-8")
        self.assertTrue(waste_alert_is_due(cooldown_minutes=30.0))


class TestWasteAlertMetrics(unittest.TestCase):
    def test_pv_can_cover_load_unpacks_channel_sum(self):
        from growatt_guard.modes import _pv_can_cover_load

        status = {
            "storage_params": {
                "storageBean": {
                    "pPv1": 700,
                    "pPv2": 500,
                    "outPutPower": 900,
                }
            }
        }

        pv_w, load_w, can_cover = _pv_can_cover_load(status)

        self.assertEqual(pv_w, 1200.0)
        self.assertEqual(load_w, 900.0)
        self.assertTrue(can_cover)


# ---------------------------------------------------------------------------
# Projected sunrise SOC helper tests
# ---------------------------------------------------------------------------

class TestProjectedSunriseSoc(unittest.TestCase):
    def test_basic_projection(self):
        from growatt_guard.modes import _projected_sunrise_soc
        # 800W load, 30000Wh battery, 7h to sunrise, 55% SOC
        # drain = 800 * 7 / 30000 * 100 = 18.67%
        result = _projected_sunrise_soc(55.0, 800.0, 30000.0, 25.0, 7.0)
        self.assertAlmostEqual(result, 55.0 - 800.0 * 7.0 / 30000.0 * 100.0, places=1)

    def test_capped_at_bms_cutoff(self):
        from growatt_guard.modes import _projected_sunrise_soc
        # Very high drain: 2000W, 30000Wh, 8h, 30% SOC â€” will drain below 25%
        result = _projected_sunrise_soc(30.0, 2000.0, 30000.0, 25.0, 8.0)
        self.assertGreaterEqual(result, 25.0)

    def test_returns_none_for_zero_capacity(self):
        from growatt_guard.modes import _projected_sunrise_soc
        self.assertIsNone(_projected_sunrise_soc(50.0, 800.0, 0.0, 25.0, 7.0))

    def test_returns_none_for_zero_hours(self):
        from growatt_guard.modes import _projected_sunrise_soc
        self.assertIsNone(_projected_sunrise_soc(50.0, 800.0, 30000.0, 25.0, 0.0))


# ---------------------------------------------------------------------------
# ETA minutes helper tests
# ---------------------------------------------------------------------------

class TestEtaMinutes(unittest.TestCase):
    def test_basic_eta(self):
        from growatt_guard.modes import _eta_minutes
        # 8% SOC needed, 30000Wh battery, 1800W charge rate
        # 8% * 30000 = 2400Wh, 2400 / 1800 * 60 = 80 min
        result = _eta_minutes(32.0, 40.0, 30000.0, 1800.0)
        self.assertAlmostEqual(result, 80.0, places=1)

    def test_returns_none_for_no_gain(self):
        from growatt_guard.modes import _eta_minutes
        self.assertIsNone(_eta_minutes(45.0, 40.0, 30000.0, 1800.0))

    def test_returns_none_for_zero_rate(self):
        from growatt_guard.modes import _eta_minutes
        self.assertIsNone(_eta_minutes(32.0, 40.0, 30000.0, 0.0))


# ---------------------------------------------------------------------------
# command_topup_soc tests
# ---------------------------------------------------------------------------

def _make_topup_config(**kwargs):
    return make_config(
        battery_capacity_wh=30000.0,
        battery_charge_rate_w=1800.0,
        battery_bms_cutoff_soc=25.0,
        **kwargs,
    )


def _make_status_mock(soc=35.0, on_utility=False):
    """Return a fake Growatt status dict."""
    output_raw = "2" if on_utility else "0"
    return {
        "obj": {
            "SOC": str(soc),
            "outputConfig": output_raw,
        }
    }


class TestCommandAdoptUtility(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._state_dir = Path(self._tmp.name)
        import growatt_guard.state as _state
        self._orig_hold = _state.UTILITY_HOLD_FILE
        self._orig_pause = _state.PAUSE_FILE
        _state.UTILITY_HOLD_FILE = self._state_dir / "utility_hold.json"
        _state.PAUSE_FILE = self._state_dir / "automation_pause.json"

    def tearDown(self):
        import growatt_guard.state as _state
        _state.UTILITY_HOLD_FILE = self._orig_hold
        _state.PAUSE_FILE = self._orig_pause
        self._tmp.cleanup()

    def test_fails_when_not_on_utility(self):
        from growatt_guard.modes import command_adopt_utility
        from growatt_guard.exceptions import GrowattGuardError
        cfg = _make_topup_config()
        status = _make_status_mock(soc=37.0, on_utility=False)
        with self.assertRaises(GrowattGuardError) as ctx:
            with patch("growatt_guard.modes.load_context", return_value=(MagicMock(), MagicMock(), status)):
                command_adopt_utility(cfg, 40.0)
        self.assertIn("not currently on Utility", str(ctx.exception))

    def test_adopts_when_on_utility(self):
        from growatt_guard.modes import command_adopt_utility
        cfg = _make_topup_config()
        status = _make_status_mock(soc=37.0, on_utility=True)
        with (
            patch("growatt_guard.modes.load_context", return_value=(MagicMock(), MagicMock(), status)),
            patch("growatt_guard.modes.append_mode_audit"),
        ):
            import io, sys
            captured = io.StringIO()
            sys.stdout = captured
            try:
                rc = command_adopt_utility(cfg, 40.0)
            finally:
                sys.stdout = sys.__stdout__
        self.assertEqual(rc, 0)
        self.assertIn("Adopted Utility", captured.getvalue())


# ---------------------------------------------------------------------------
# command_snooze_waste tests
# ---------------------------------------------------------------------------

class TestCommandSnoozeWaste(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._state_dir = Path(self._tmp.name)
        import growatt_guard.state as _state
        self._orig = _state.WASTE_ALERT_FILE
        _state.WASTE_ALERT_FILE = self._state_dir / "waste_alert.json"

    def tearDown(self):
        import growatt_guard.state as _state
        _state.WASTE_ALERT_FILE = self._orig
        self._tmp.cleanup()

    def test_snooze_2h(self):
        from growatt_guard.modes import command_snooze_waste
        from growatt_guard.state import waste_alert_is_snoozed
        cfg = make_config()
        rc = command_snooze_waste(cfg, "2h")
        self.assertEqual(rc, 0)
        self.assertTrue(waste_alert_is_snoozed())

    def test_snooze_30m(self):
        from growatt_guard.modes import command_snooze_waste
        from growatt_guard.state import waste_alert_is_snoozed
        cfg = make_config()
        rc = command_snooze_waste(cfg, "30m")
        self.assertEqual(rc, 0)
        self.assertTrue(waste_alert_is_snoozed())

    def test_snooze_invalid_raises(self):
        from growatt_guard.modes import command_snooze_waste
        from growatt_guard.exceptions import GrowattGuardError
        cfg = make_config()
        with self.assertRaises(GrowattGuardError):
            command_snooze_waste(cfg, "5days")


# ---------------------------------------------------------------------------
# watchdog-sbu ownership tests
# ---------------------------------------------------------------------------

class TestWatchdogSbuOwnership(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._state_dir = Path(self._tmp.name)
        import growatt_guard.state as _state
        self._orig_hold = _state.UTILITY_HOLD_FILE
        self._orig_pause = _state.PAUSE_FILE
        self._orig_lock = _state.COMMAND_LOCK_FILE
        _state.UTILITY_HOLD_FILE = self._state_dir / "utility_hold.json"
        _state.PAUSE_FILE = self._state_dir / "automation_pause.json"
        _state.COMMAND_LOCK_FILE = self._state_dir / "mode_command.lock"

    def tearDown(self):
        import growatt_guard.state as _state
        _state.UTILITY_HOLD_FILE = self._orig_hold
        _state.PAUSE_FILE = self._orig_pause
        _state.COMMAND_LOCK_FILE = self._orig_lock
        self._tmp.cleanup()

    def test_observed_utility_skips_repair(self):
        """watchdog-sbu must NOT return to SBU when Utility is observed (no hold)."""
        from growatt_guard.modes import command_watchdog_sbu
        cfg = make_config()
        status = _make_status_mock(soc=60.0, on_utility=True)
        with (
            patch("growatt_guard.modes.load_context", return_value=(MagicMock(), MagicMock(), status)),
            patch("growatt_guard.modes.append_mode_audit"),
        ):
            import io, sys
            captured = io.StringIO()
            sys.stdout = captured
            try:
                rc = command_watchdog_sbu(cfg)
            finally:
                sys.stdout = sys.__stdout__
        self.assertEqual(rc, 0)
        self.assertIn("observed", captured.getvalue().lower())

    def test_sbu_mode_returns_ok(self):
        from growatt_guard.modes import command_watchdog_sbu
        cfg = make_config()
        status = _make_status_mock(soc=60.0, on_utility=False)
        with (
            patch("growatt_guard.modes.load_context", return_value=(MagicMock(), MagicMock(), status)),
            patch("growatt_guard.modes.append_mode_audit"),
        ):
            import io, sys
            captured = io.StringIO()
            sys.stdout = captured
            try:
                rc = command_watchdog_sbu(cfg)
            finally:
                sys.stdout = sys.__stdout__
        self.assertEqual(rc, 0)
        self.assertIn("OK", captured.getvalue())

    def test_owned_utility_below_target_holds(self):
        """watchdog-sbu must hold off when SOC < target_soc for an owned hold."""
        from growatt_guard.modes import command_watchdog_sbu
        from growatt_guard.state import write_utility_hold_state
        cfg = make_config()
        status = _make_status_mock(soc=35.0, on_utility=True)
        expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        write_utility_hold_state("owned", 40.0, expiry, start_soc=32.0)
        with (
            patch("growatt_guard.modes.load_context", return_value=(MagicMock(), MagicMock(), status)),
            patch("growatt_guard.modes.append_mode_audit"),
        ):
            import io, sys
            captured = io.StringIO()
            sys.stdout = captured
            try:
                rc = command_watchdog_sbu(cfg)
            finally:
                sys.stdout = sys.__stdout__
        self.assertEqual(rc, 0)
        self.assertIn("ceiling", captured.getvalue().lower())


# ---------------------------------------------------------------------------
# topup-complete-check SOC-based completion tests
# ---------------------------------------------------------------------------

class TestTopupCompleteCheckSoc(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._state_dir = Path(self._tmp.name)
        import growatt_guard.state as _state
        self._orig_hold = _state.UTILITY_HOLD_FILE
        self._orig_topup = _state.TOPUP_STATE_FILE
        self._orig_pause = _state.PAUSE_FILE
        self._orig_lock = _state.COMMAND_LOCK_FILE
        _state.UTILITY_HOLD_FILE = self._state_dir / "utility_hold.json"
        _state.TOPUP_STATE_FILE = self._state_dir / "topup_active.json"
        _state.PAUSE_FILE = self._state_dir / "automation_pause.json"
        _state.COMMAND_LOCK_FILE = self._state_dir / "mode_command.lock"

    def tearDown(self):
        import growatt_guard.state as _state
        _state.UTILITY_HOLD_FILE = self._orig_hold
        _state.TOPUP_STATE_FILE = self._orig_topup
        _state.PAUSE_FILE = self._orig_pause
        _state.COMMAND_LOCK_FILE = self._orig_lock
        self._tmp.cleanup()

    def test_no_state_prints_no_active(self):
        from growatt_guard.modes import command_topup_complete_check
        cfg = make_config()
        with patch("growatt_guard.audit.find_overdue_unclosed_topup", return_value=None):
            import io, sys
            captured = io.StringIO()
            sys.stdout = captured
            try:
                rc = command_topup_complete_check(cfg)
            finally:
                sys.stdout = sys.__stdout__
        self.assertEqual(rc, 0)
        self.assertIn("No active topup", captured.getvalue())

    def test_target_reached_returns_sbu(self):
        from growatt_guard.modes import command_topup_complete_check
        from growatt_guard.state import write_utility_hold_state, read_utility_hold_state
        cfg = make_config()
        expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        write_utility_hold_state("owned", 40.0, expiry, start_soc=32.0)
        status_at_target = _make_status_mock(soc=41.0, on_utility=True)
        sbu_calls = []
        def fake_return_sbu(c):
            sbu_calls.append(True)
            return 0
        with (
            patch("growatt_guard.modes.load_context", return_value=(MagicMock(), MagicMock(), status_at_target)),
            patch("growatt_guard.modes.command_return_sbu", side_effect=fake_return_sbu),
            patch("growatt_guard.modes.command_resume"),
            patch("growatt_guard.modes.append_charge_rate_reading", return_value=[]),
            patch("growatt_guard.audit.find_overdue_unclosed_topup", return_value=None),
        ):
            import io, sys
            captured = io.StringIO()
            sys.stdout = captured
            try:
                rc = command_topup_complete_check(cfg)
            finally:
                sys.stdout = sys.__stdout__
        self.assertEqual(rc, 0)
        self.assertTrue(sbu_calls, "return_sbu should have been called")
        self.assertIn("Topup complete", captured.getvalue())
        # State should be cleared.
        self.assertIsNone(read_utility_hold_state())

    def test_still_active_prints_remaining(self):
        from growatt_guard.modes import command_topup_complete_check
        from growatt_guard.state import write_utility_hold_state
        cfg = make_config()
        expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        write_utility_hold_state("owned", 40.0, expiry, start_soc=32.0)
        status_low = _make_status_mock(soc=36.0, on_utility=True)
        with (
            patch("growatt_guard.modes.load_context", return_value=(MagicMock(), MagicMock(), status_low)),
            patch("growatt_guard.audit.find_overdue_unclosed_topup", return_value=None),
        ):
            import io, sys
            captured = io.StringIO()
            sys.stdout = captured
            try:
                rc = command_topup_complete_check(cfg)
            finally:
                sys.stdout = sys.__stdout__
        self.assertEqual(rc, 0)
        self.assertIn("active", captured.getvalue().lower())

    def test_expiry_above_floor_returns_sbu_below_target(self):
        from growatt_guard.modes import command_topup_complete_check
        from growatt_guard.state import write_utility_hold_state
        cfg = make_config(auto_topup_sunrise_floor_soc=35.0)
        expiry = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)  # already expired
        write_utility_hold_state("owned", 40.0, expiry, start_soc=32.0)
        status_below = _make_status_mock(soc=38.0, on_utility=True)
        sbu_calls = []
        def fake_return_sbu(c):
            sbu_calls.append(True)
            return 0
        with (
            patch("growatt_guard.modes.load_context", return_value=(MagicMock(), MagicMock(), status_below)),
            patch("growatt_guard.modes.command_return_sbu", side_effect=fake_return_sbu),
            patch("growatt_guard.modes.command_resume"),
            patch("growatt_guard.audit.find_overdue_unclosed_topup", return_value=None),
        ):
            import io, sys
            captured = io.StringIO()
            sys.stdout = captured
            try:
                rc = command_topup_complete_check(cfg)
            finally:
                sys.stdout = sys.__stdout__
        self.assertTrue(sbu_calls, "return_sbu should have been called")
        self.assertIn("below target", captured.getvalue().lower())

    def test_expiry_at_or_below_floor_sends_alert(self):
        from growatt_guard.modes import command_topup_complete_check
        from growatt_guard.state import write_utility_hold_state
        cfg = make_config(auto_topup_sunrise_floor_soc=35.0, discord_notify_failure=True, dry_run=False)
        expiry = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
        write_utility_hold_state("owned", 40.0, expiry, start_soc=32.0)
        status_low = _make_status_mock(soc=34.0, on_utility=True)
        embeds_sent = []
        def fake_return_sbu(c):
            return 0
        with (
            patch("growatt_guard.modes.load_context", return_value=(MagicMock(), MagicMock(), status_low)),
            patch("growatt_guard.modes.command_return_sbu", side_effect=fake_return_sbu),
            patch("growatt_guard.modes.command_resume"),
            patch("growatt_guard.modes.send_discord_embed", side_effect=lambda c, e: embeds_sent.append(e)),
            patch("growatt_guard.audit.find_overdue_unclosed_topup", return_value=None),
        ):
            command_topup_complete_check(cfg)
        self.assertTrue(any("failed" in str(e.get("title", "")).lower() or "low" in str(e.get("title", "")).lower()
                            for e in embeds_sent), f"Expected failure embed, got: {embeds_sent}")


# ---------------------------------------------------------------------------
# Dashboard tonight-safe tests
# ---------------------------------------------------------------------------

class TestComputeTonightSafe(unittest.TestCase):
    def test_before_cutoff_shows_nothing(self):
        from growatt_guard.dashboard import compute_tonight_safe
        result = compute_tonight_safe(proj_sunrise_soc := 38.0, hours_to_sunset=2.0)
        self.assertFalse(result.get("show"))

    def test_after_cutoff_below_floor(self):
        from growatt_guard.dashboard import compute_tonight_safe
        result = compute_tonight_safe(30.0, hours_to_sunset=-1.0, floor_soc=35.0)
        self.assertTrue(result["show"])
        self.assertIn("Topup needed", result["headline"])
        self.assertEqual(result["level"], "danger")

    def test_after_cutoff_comfortable(self):
        from growatt_guard.dashboard import compute_tonight_safe
        result = compute_tonight_safe(50.0, hours_to_sunset=-1.0, comfortable_soc=45.0)
        self.assertTrue(result["show"])
        self.assertEqual(result["headline"], "Tonight safe: 100%")
        self.assertEqual(result["level"], "ok")

    def test_after_cutoff_watch(self):
        from growatt_guard.dashboard import compute_tonight_safe
        result = compute_tonight_safe(38.0, hours_to_sunset=-1.0, floor_soc=35.0, comfortable_soc=45.0)
        self.assertTrue(result["show"])
        self.assertIn("38", result["headline"])
        self.assertEqual(result["level"], "watch")

    def test_none_sunset_shows_after_cutoff(self):
        """If hours_to_sunset is None we assume past cutoff."""
        from growatt_guard.dashboard import compute_tonight_safe
        result = compute_tonight_safe(40.0, hours_to_sunset=None)
        self.assertTrue(result["show"])

    def test_none_projection_shows_nothing(self):
        from growatt_guard.dashboard import compute_tonight_safe
        result = compute_tonight_safe(None, hours_to_sunset=-1.0)
        self.assertFalse(result.get("show"))


if __name__ == "__main__":
    unittest.main()
