import datetime as dt
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_power_guard import (
    DeviceRef,
    SPF_EXPECTED_OUTPUT_CONFIG,
    extract_soc,
    extract_spf_output_source,
    render_params,
    set_mode,
    summarize_status,
    verify_mode_switch,
    command_watchdog_sbu,
)
from growatt_guard.growatt_api import detect_grid_bypass, detect_unexpected_grid_bypass, extract_battery_status, estimate_runtime, estimate_charge_time, estimate_topup_for_sunrise, format_duration_minutes


class AutoTopupTargetSocTests(unittest.TestCase):
    """Tests for AUTO_TOPUP_TARGET_SOC sunrise buffer in command_auto_topup_check."""

    CAPACITY = 30_000.0
    CUTOFF = 25.0
    CHARGE_RATE = 3_000.0

    def test_higher_target_soc_produces_longer_topup(self):
        # Same conditions, higher target → more charging needed
        result_cutoff = estimate_topup_for_sunrise(40.0, 1500.0, self.CAPACITY, self.CUTOFF, self.CHARGE_RATE, 5.0)
        result_target = estimate_topup_for_sunrise(40.0, 1500.0, self.CAPACITY, 35.0, self.CHARGE_RATE, 5.0)
        self.assertGreater(result_target, result_cutoff)

    def test_target_soc_equal_to_cutoff_same_result(self):
        r1 = estimate_topup_for_sunrise(40.0, 1500.0, self.CAPACITY, self.CUTOFF, self.CHARGE_RATE, 5.0)
        r2 = estimate_topup_for_sunrise(40.0, 1500.0, self.CAPACITY, self.CUTOFF, self.CHARGE_RATE, 5.0)
        self.assertAlmostEqual(r1, r2)

    def test_command_uses_effective_target_soc(self):
        from growatt_guard.topup import command_auto_topup_check
        from growatt_guard import state as state_mod
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory

        status = {
            "datalogSn": "SN1", "deviceSn": "SN1",
            "data": {"soc": 40.0, "pDischarge": 1500.0, "outputConfig": "0"},
        }

        written_cutoff: dict = {}
        written_target: dict = {}

        def make_fake_write(store):
            def _fake(**kwargs):
                store["minutes"] = kwargs["minutes"]
            return _fake

        common_patches = lambda store: [
            patch("growatt_guard.topup.read_pause_state", return_value=None),
            patch("growatt_guard.topup.topup_is_active", return_value=False),
            patch("growatt_guard.topup.hours_until_next_sunrise", return_value=5.0),
            patch("growatt_guard.topup.load_context", return_value=(None, None, status)),
            patch("growatt_guard.topup.set_mode", return_value="ok"),
            patch("growatt_guard.topup.command_pause", return_value=0),
            patch("growatt_guard.topup.write_utility_hold_state", side_effect=make_fake_write(store)),
            patch("growatt_guard.topup.append_mode_audit"),
        ]

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            cfg_cutoff = make_config(
                auto_topup_enabled=True, battery_capacity_wh=30000.0,
                battery_charge_rate_w=3000.0, battery_bms_cutoff_soc=25.0,
                auto_topup_target_soc=0.0, dry_run=True,
            )
            ps = common_patches(written_cutoff)
            with (patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "t1.json"),
                  patch.object(state_mod, "PAUSE_FILE", tmp / "p1.json"),
                  patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr1.json"),
                  ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], redirect_stdout(StringIO())):
                command_auto_topup_check(cfg_cutoff)

            cfg_target = make_config(
                auto_topup_enabled=True, battery_capacity_wh=30000.0,
                battery_charge_rate_w=3000.0, battery_bms_cutoff_soc=25.0,
                auto_topup_target_soc=35.0, dry_run=True,
            )
            ps2 = common_patches(written_target)
            with (patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "t2.json"),
                  patch.object(state_mod, "PAUSE_FILE", tmp / "p2.json"),
                  patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr2.json"),
                  ps2[0], ps2[1], ps2[2], ps2[3], ps2[4], ps2[5], ps2[6], ps2[7], redirect_stdout(StringIO())):
                command_auto_topup_check(cfg_target)

        self.assertIn("minutes", written_cutoff)
        self.assertIn("minutes", written_target)
        self.assertGreater(written_target["minutes"], written_cutoff["minutes"])

    def test_command_stores_completion_target_after_topup_window(self):
        from growatt_guard.topup import command_auto_topup_check
        from growatt_guard import state as state_mod
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory

        status = {
            "datalogSn": "SN1", "deviceSn": "SN1",
            "data": {"soc": 33.0, "pDischarge": 1707.1, "outputConfig": "0"},
        }
        cfg = make_config(
            auto_topup_enabled=True,
            auto_topup_min_hours_to_sunrise=5.0,
            auto_topup_solar_skip_min_margin_minutes=0.0,
            battery_capacity_wh=30000.0,
            battery_charge_rate_w=2100.0,
            battery_bms_cutoff_soc=25.0,
            discord_topup_max_minutes=210,
            dry_run=True,
        )

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            with (patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "t.json"),
                  patch.object(state_mod, "PAUSE_FILE", tmp / "p.json"),
                  patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr.json"),
                  patch("growatt_guard.topup.read_pause_state", return_value=None),
                  patch("growatt_guard.topup.topup_is_active", return_value=False),
                  patch("growatt_guard.topup.hours_until_next_sunrise", return_value=8.6),
                  patch("growatt_guard.topup.load_context", return_value=(None, None, status)),
                  patch("growatt_guard.topup.set_mode", return_value="ok"),
                  patch("growatt_guard.topup.command_pause", return_value=0),                  patch("growatt_guard.topup.write_utility_hold_state") as hold_write,
                  patch("growatt_guard.topup.append_mode_audit"),
                  redirect_stdout(StringIO())):
                command_auto_topup_check(cfg)

        hold_write.assert_called_once()
        target_soc = hold_write.call_args.kwargs["target_soc"]
        self.assertAlmostEqual(target_soc, 55.5, delta=0.6)
        self.assertLess(target_soc, 60.0)

    def test_target_soc_below_cutoff_uses_cutoff(self):
        # target_soc=10 < bms_cutoff=25 → effective target = 25 (cutoff wins via max())
        result_low_target = estimate_topup_for_sunrise(40.0, 1500.0, self.CAPACITY, max(self.CUTOFF, 10.0), self.CHARGE_RATE, 5.0)
        result_cutoff = estimate_topup_for_sunrise(40.0, 1500.0, self.CAPACITY, self.CUTOFF, self.CHARGE_RATE, 5.0)
        self.assertAlmostEqual(result_low_target, result_cutoff)

    def test_target_note_appears_in_output(self):
        from growatt_guard.topup import command_auto_topup_check
        from growatt_guard import state as state_mod
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory

        status = {
            "datalogSn": "SN1", "deviceSn": "SN1",
            "data": {"soc": 40.0, "pDischarge": 1500.0, "outputConfig": "0"},
        }
        cfg = make_config(
            auto_topup_enabled=True, battery_capacity_wh=30000.0,
            battery_charge_rate_w=3000.0, battery_bms_cutoff_soc=25.0,
            auto_topup_target_soc=35.0, dry_run=True,
        )
        buf = StringIO()
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            with (patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "t.json"),
                  patch.object(state_mod, "PAUSE_FILE", tmp / "p.json"),
                  patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr.json"),
                  patch("growatt_guard.topup.read_pause_state", return_value=None),
                  patch("growatt_guard.topup.topup_is_active", return_value=False),
                  patch("growatt_guard.topup.hours_until_next_sunrise", return_value=5.0),
                  patch("growatt_guard.topup.load_context", return_value=(None, None, status)),
                  patch("growatt_guard.topup.set_mode", return_value="ok"),
                  patch("growatt_guard.topup.command_pause", return_value=0),                  patch("growatt_guard.topup.write_utility_hold_state"),
                  patch("growatt_guard.topup.append_mode_audit"),
                  redirect_stdout(buf)):
                command_auto_topup_check(cfg)
        self.assertIn("target 35%", buf.getvalue())

    def test_pause_reason_and_embed_include_topup_target(self):
        from growatt_guard.topup import command_auto_topup_check
        from growatt_guard import state as state_mod
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory

        status = {
            "datalogSn": "SN1", "deviceSn": "SN1",
            "data": {"soc": 26.0, "pDischarge": 1682.4, "outputConfig": "0"},
        }
        cfg = make_config(
            auto_topup_enabled=True,
            battery_capacity_wh=30000.0,
            battery_charge_rate_w=3000.0,
            battery_bms_cutoff_soc=25.0,
            auto_topup_solar_skip_min_margin_minutes=60.0,
            discord_notify_success=True,
            dry_run=False,
        )

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            with (patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "t.json"),
                  patch.object(state_mod, "PAUSE_FILE", tmp / "p.json"),
                  patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr.json"),
                  patch("growatt_guard.topup.read_pause_state", return_value=None),
                  patch("growatt_guard.topup.topup_is_active", return_value=False),
                  patch("growatt_guard.topup.hours_until_next_sunrise", return_value=8.6),
                  patch("growatt_guard.topup.load_context", return_value=(None, None, status)),
                  patch("growatt_guard.topup.set_mode", return_value={"success": True}),
                  patch("growatt_guard.topup.command_pause", return_value=0) as pause_mock,                  patch("growatt_guard.topup.write_utility_hold_state"),
                  patch("growatt_guard.topup.append_mode_audit"),
                  patch("growatt_guard.topup.send_discord_embed", return_value=True) as send_mock,
                  redirect_stdout(StringIO())):
                command_auto_topup_check(cfg)

        self.assertIn("topup target", pause_mock.call_args.args[2])
        embed = send_mock.call_args.args[1]
        field_names = [field["name"] for field in embed["fields"]]
        self.assertIn("Topup target", field_names)


class AutoTopupMinMinutesTests(unittest.TestCase):
    """Tests for AUTO_TOPUP_MIN_MINUTES skip threshold in command_auto_topup_check."""

    def _make_status(self, soc: float, discharge_w: float) -> dict:
        return {
            "datalogSn": "SN1",
            "deviceSn": "SN1",
            "data": {
                "soc": soc,
                "pDischarge": discharge_w,
                "outputConfig": "0",
            },
        }

    def test_skips_when_calculated_topup_below_minimum(self):
        from growatt_guard.topup import command_auto_topup_check
        from growatt_guard import state as state_mod
        from contextlib import redirect_stdout
        from io import StringIO

        # SOC=60%, load=2021W, 5.5h to sunrise → ~9 min calculated
        status = self._make_status(soc=60.0, discharge_w=2021.0)
        cfg = make_config(
            auto_topup_enabled=True,
            auto_topup_min_hours_to_sunrise=4.0,
            auto_topup_min_minutes=20.0,
            auto_topup_solar_skip_min_margin_minutes=0.0,
            battery_capacity_wh=30000.0,
            battery_charge_rate_w=3000.0,
            battery_bms_cutoff_soc=25.0,
            dry_run=True,
        )

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            written = {}

            def fake_write_hold(**kwargs):
                written["minutes"] = kwargs["minutes"]

            buf = StringIO()
            with (
                patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup_active.json"),
                patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
                patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr.json"),
                patch("growatt_guard.topup.read_pause_state", return_value=None),
                patch("growatt_guard.topup.topup_is_active", return_value=False),
                patch("growatt_guard.topup.hours_until_next_sunrise", return_value=5.5),
                patch("growatt_guard.topup.load_context", return_value=(None, None, status)),
                patch("growatt_guard.topup.set_mode", return_value="ok") as set_mode_mock,
                patch("growatt_guard.topup.command_pause", return_value=0) as pause_mock,
                patch("growatt_guard.topup.write_utility_hold_state", side_effect=fake_write_hold),
                patch("growatt_guard.topup.append_mode_audit") as audit_mock,
                redirect_stdout(buf),
            ):
                command_auto_topup_check(cfg)

        self.assertNotIn("minutes", written)
        set_mode_mock.assert_not_called()
        pause_mock.assert_not_called()
        audit_mock.assert_called_once()
        self.assertEqual(audit_mock.call_args.kwargs["action"], "topup-skipped-short")
        self.assertIn("below AUTO_TOPUP_MIN_MINUTES=20", buf.getvalue())

    def test_minimum_does_not_skip_when_calculated_topup_exceeds_minimum(self):
        from growatt_guard.topup import command_auto_topup_check
        from growatt_guard import state as state_mod
        from contextlib import redirect_stdout
        from io import StringIO

        # SOC=30%, load=1500W, 6h to sunrise → ~100 min calculated
        status = self._make_status(soc=30.0, discharge_w=1500.0)
        cfg = make_config(
            auto_topup_enabled=True,
            auto_topup_min_hours_to_sunrise=4.0,
            auto_topup_min_minutes=20.0,
            battery_capacity_wh=30000.0,
            battery_charge_rate_w=3000.0,
            battery_bms_cutoff_soc=25.0,
            dry_run=True,
        )

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            written = {}

            def fake_write_hold(**kwargs):
                written["minutes"] = kwargs["minutes"]

            buf = StringIO()
            with (
                patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup_active.json"),
                patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
                patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr.json"),
                patch("growatt_guard.topup.read_pause_state", return_value=None),
                patch("growatt_guard.topup.topup_is_active", return_value=False),
                patch("growatt_guard.topup.hours_until_next_sunrise", return_value=6.0),
                patch("growatt_guard.topup.load_context", return_value=(None, None, status)),
                patch("growatt_guard.topup.set_mode", return_value="ok"),
                patch("growatt_guard.topup.command_pause", return_value=0),
                patch("growatt_guard.topup.write_utility_hold_state", side_effect=fake_write_hold),
                patch("growatt_guard.topup.append_mode_audit"),
                redirect_stdout(buf),
            ):
                command_auto_topup_check(cfg)

        self.assertIn("minutes", written)
        self.assertGreater(written["minutes"], 20)


class AutoTopupLateSafetyTests(unittest.TestCase):
    def _make_status(self, soc: float, discharge_w: float) -> dict:
        return {
            "datalogSn": "SN1",
            "deviceSn": "SN1",
            "data": {
                "soc": soc,
                "pDischarge": discharge_w,
                "outputConfig": "0",
            },
        }

    def _run_check(self, *, hours: float, soc: float, discharge_w: float):
        from growatt_guard.topup import command_auto_topup_check
        from growatt_guard import state as state_mod

        cfg = make_config(
            auto_topup_enabled=True,
            auto_topup_min_hours_to_sunrise=4.0,
            auto_topup_min_minutes=20.0,
            auto_topup_target_soc=35.0,
            auto_topup_sunrise_floor_soc=35.0,
            auto_topup_solar_skip_kwh_m2=4.0,
            auto_topup_solar_skip_min_margin_minutes=0.0,
            battery_capacity_wh=30000.0,
            battery_charge_rate_w=3000.0,
            battery_bms_cutoff_soc=25.0,
            dry_run=True,
        )
        status = self._make_status(soc, discharge_w)
        captured: dict = {}

        def fake_write_hold(**kwargs):
            captured.update(kwargs)

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            with (
                patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup_active.json"),
                patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
                patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr.json"),
                patch("growatt_guard.topup.read_pause_state", return_value=None),
                patch("growatt_guard.topup.topup_is_active", return_value=False),
                patch("growatt_guard.topup.hours_until_next_sunrise", return_value=hours),
                patch("growatt_guard.topup.load_context", return_value=(None, None, status)) as load_mock,
                patch("growatt_guard.topup.set_mode", return_value="ok") as set_mode_mock,
                patch("growatt_guard.topup.command_pause", return_value=0),
                patch("growatt_guard.topup.write_utility_hold_state", side_effect=fake_write_hold),
                patch("growatt_guard.topup.append_mode_audit"),
                patch("growatt_guard.weather.get_tomorrow_solar_kwh_m2") as solar_mock,
                redirect_stdout(StringIO()) as output,
            ):
                command_auto_topup_check(cfg)

        return captured, load_mock, set_mode_mock, solar_mock, output.getvalue()

    def test_late_safety_starts_subminimum_topup_for_projected_floor_breach(self):
        captured, _, set_mode_mock, solar_mock, output = self._run_check(
            hours=3.0,
            soc=44.0,
            discharge_w=1000.0,
        )

        self.assertGreater(captured["minutes"], 0)
        self.assertLess(captured["minutes"], 20)
        self.assertIn("Late safety topup", captured["reason"])
        set_mode_mock.assert_called_once()
        solar_mock.assert_not_called()
        self.assertIn(f"Auto-topup started: {captured['minutes']}min", output)

    def test_late_safety_does_nothing_when_floor_is_safe(self):
        captured, _, set_mode_mock, solar_mock, output = self._run_check(
            hours=3.0,
            soc=60.0,
            discharge_w=1000.0,
        )

        self.assertEqual(captured, {})
        set_mode_mock.assert_not_called()
        solar_mock.assert_not_called()
        self.assertIn("projected sunrise SOC 50%", output)

    def test_late_safety_hard_stop_avoids_api_call(self):
        captured, load_mock, set_mode_mock, solar_mock, output = self._run_check(
            hours=0.8,
            soc=30.0,
            discharge_w=2000.0,
        )

        self.assertEqual(captured, {})
        load_mock.assert_not_called()
        set_mode_mock.assert_not_called()
        solar_mock.assert_not_called()
        self.assertIn("safety cutoff", output)


class ChargeRateHistoryTests(unittest.TestCase):
    def test_append_and_read_single_reading(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "cr.json"):
                readings = state_mod.append_charge_rate_reading(2800.0)
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0]["rate_w"], 2800)

    def test_trims_to_max_readings(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "cr.json"):
                for i in range(12):
                    readings = state_mod.append_charge_rate_reading(float(2000 + i * 100))
        self.assertEqual(len(readings), state_mod._CHARGE_RATE_MAX_READINGS)
        self.assertEqual(readings[-1]["rate_w"], 3100)

    def test_read_returns_empty_list_when_missing(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "cr.json"):
                result = state_mod.read_charge_rate_history()
        self.assertEqual(result, [])

    def test_accumulates_across_calls(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "cr.json"):
                state_mod.append_charge_rate_reading(2800.0)
                state_mod.append_charge_rate_reading(3000.0)
                readings = state_mod.read_charge_rate_history()
        self.assertEqual(len(readings), 2)
        self.assertEqual(readings[0]["rate_w"], 2800)
        self.assertEqual(readings[1]["rate_w"], 3000)


class TopupCompleteFeedbackTests(unittest.TestCase):
    """Tests for command_topup_complete_check charge-rate feedback."""

    def _write_audit(self, path: Path, rows: list[dict]) -> None:
        import csv as csv_mod
        from growatt_guard.audit import MODE_AUDIT_FIELDS
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=MODE_AUDIT_FIELDS)
            writer.writeheader()
            for row in rows:
                full = {k: "" for k in MODE_AUDIT_FIELDS}
                full.update(row)
                writer.writerow(full)

    def _make_status(self, soc: float) -> dict:
        return {
            "datalogSn": "SN1",
            "deviceSn": "SN1",
            "data": {
                "soc": soc,
                "pDischarge": 1000.0,
                "outputConfig": "1",
            },
        }

    def _make_topup_state(self, started_minutes_ago: float, start_soc: float, load_w: float, minutes: int) -> dict:
        import datetime as dt
        started_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=started_minutes_ago)).isoformat()
        paused_until = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()
        return {
            "started_at": started_at,
            "minutes": minutes,
            "paused_until": paused_until,
            "reason": "Auto-topup: test",
            "start_soc": start_soc,
            "start_load_w": load_w,
        }

    def test_topup_complete_prints_avg_rate_after_multiple_topups(self):
        from growatt_guard.topup import command_topup_complete_check
        from growatt_guard import state as state_mod

        state = self._make_topup_state(started_minutes_ago=100, start_soc=48.0, load_w=1000.0, minutes=100)
        end_status = self._make_status(soc=74.0)

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = make_config(
                battery_capacity_wh=30000.0,
                battery_charge_rate_w=3000.0,
                dry_run=True,
            )
            # Seed one prior reading so avg kicks in after this topup
            with patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", tmp / "cr.json"):
                state_mod.append_charge_rate_reading(2900.0)

            buf = StringIO()
            with (
                patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup_active.json"),
                patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
                patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", tmp / "cr.json"),
                patch("growatt_guard.audit.MODE_AUDIT_FILE", tmp / "mode_decisions.csv"),
                patch("growatt_guard.topup.read_topup_state", return_value=state),
                patch("growatt_guard.topup.read_utility_hold_state", return_value=None),
                patch("growatt_guard.topup.topup_is_active", return_value=False),
                patch("growatt_guard.topup.load_context", return_value=(None, None, end_status)),
                patch("growatt_guard.topup.command_resume", return_value=0),
                patch("growatt_guard.topup.clear_topup_state"),
                patch("growatt_guard.topup.command_return_sbu", return_value=0),
                redirect_stdout(buf),
            ):
                command_topup_complete_check(cfg)

        output = buf.getvalue()
        self.assertIn("Avg charge rate", output)
        self.assertIn("2 readings", output)

    def test_topup_complete_prints_implied_rate(self):
        from growatt_guard.topup import command_topup_complete_check
        from growatt_guard import state as state_mod

        state = self._make_topup_state(started_minutes_ago=100, start_soc=48.0, load_w=1000.0, minutes=100)
        end_status = self._make_status(soc=74.0)

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = make_config(
                battery_capacity_wh=30000.0,
                battery_charge_rate_w=3000.0,
                dry_run=True,
            )
            buf = StringIO()
            with (
                patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup_active.json"),
                patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
                patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", tmp / "cr.json"),
                patch("growatt_guard.audit.MODE_AUDIT_FILE", tmp / "mode_decisions.csv"),
                patch("growatt_guard.topup.read_topup_state", return_value=state),
                patch("growatt_guard.topup.read_utility_hold_state", return_value=None),
                patch("growatt_guard.topup.topup_is_active", return_value=False),
                patch("growatt_guard.topup.load_context", return_value=(None, None, end_status)),
                patch("growatt_guard.topup.command_resume", return_value=0),
                patch("growatt_guard.topup.clear_topup_state"),
                patch("growatt_guard.topup.command_return_sbu", return_value=0),
                redirect_stdout(buf),
            ):
                command_topup_complete_check(cfg)

        output = buf.getvalue()
        self.assertIn("48%", output)
        self.assertIn("74%", output)
        self.assertIn("Implied charge rate", output)

    def test_topup_complete_prints_fallback_when_no_soc_data(self):
        from growatt_guard.topup import command_topup_complete_check
        from growatt_guard import state as state_mod

        state = self._make_topup_state(started_minutes_ago=100, start_soc=48.0, load_w=1000.0, minutes=100)
        state.pop("start_soc")

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = make_config(
                battery_capacity_wh=30000.0,
                battery_charge_rate_w=3000.0,
                dry_run=True,
            )
            buf = StringIO()
            with (
                patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup_active.json"),
                patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
                patch.object(state_mod, "CHARGE_RATE_HISTORY_FILE", tmp / "cr.json"),
                patch("growatt_guard.audit.MODE_AUDIT_FILE", tmp / "mode_decisions.csv"),
                patch("growatt_guard.topup.read_topup_state", return_value=state),
                patch("growatt_guard.topup.read_utility_hold_state", return_value=None),
                patch("growatt_guard.topup.topup_is_active", return_value=False),
                patch("growatt_guard.topup.load_context", return_value=(None, None, self._make_status(74.0))),
                patch("growatt_guard.topup.command_resume", return_value=0),
                patch("growatt_guard.topup.clear_topup_state"),
                patch("growatt_guard.topup.command_return_sbu", return_value=0),
                redirect_stdout(buf),
            ):
                command_topup_complete_check(cfg)

        output = buf.getvalue()
        self.assertIn("Topup complete", output)
        self.assertNotIn("Implied charge rate", output)

    def test_topup_complete_skips_when_still_active(self):
        from growatt_guard.topup import command_topup_complete_check

        import datetime as dt
        paused_until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat()
        state = {
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "minutes": 60,
            "paused_until": paused_until,
            "reason": "test",
        }

        buf = StringIO()
        with (
            patch("growatt_guard.topup.read_topup_state", return_value=state),
            patch("growatt_guard.topup.topup_is_active", return_value=True),
            redirect_stdout(buf),
        ):
            rc = command_topup_complete_check(make_config())

        self.assertEqual(rc, 0)
        self.assertIn("remaining", buf.getvalue())

    def test_topup_complete_repairs_overdue_unclosed_topup_when_state_is_missing(self):
        from growatt_guard.topup import command_topup_complete_check

        import datetime as dt
        started = (dt.datetime.now() - dt.timedelta(minutes=60)).isoformat(timespec="seconds")
        rows = [
            {
                "timestamp": started,
                "command": "auto-topup-check",
                "soc": "66",
                "previous_mode": "SBU priority [0]",
                "action": "auto-topup-started",
                "result": "ok",
                "note": "20min, 8.2h to sunrise",
            }
        ]

        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            buf = StringIO()
            with (
                patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path),
                patch("growatt_guard.topup.read_topup_state", return_value=None),
                patch("growatt_guard.topup.read_utility_hold_state", return_value=None),
                patch("growatt_guard.topup.command_return_sbu", return_value=0) as return_mock,
                redirect_stdout(buf),
            ):
                rc = command_topup_complete_check(make_config())

        self.assertEqual(rc, 0)
        return_mock.assert_called_once()
        self.assertIn("overdue auto-topup", buf.getvalue())

    def test_topup_complete_does_not_repair_when_a_later_sbu_return_exists(self):
        from growatt_guard.topup import command_topup_complete_check

        import datetime as dt
        started = (dt.datetime.now() - dt.timedelta(minutes=60)).isoformat(timespec="seconds")
        returned = (dt.datetime.now() - dt.timedelta(minutes=30)).isoformat(timespec="seconds")
        rows = [
            {
                "timestamp": started,
                "command": "auto-topup-check",
                "action": "auto-topup-started",
                "note": "20min, 8.2h to sunrise",
            },
            {
                "timestamp": returned,
                "command": "return-sbu",
                "action": "switch-to-sbu",
            },
        ]

        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            buf = StringIO()
            with (
                patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path),
                patch("growatt_guard.topup.read_topup_state", return_value=None),
                patch("growatt_guard.topup.read_utility_hold_state", return_value=None),
                patch("growatt_guard.topup.command_return_sbu", return_value=0) as return_mock,
                redirect_stdout(buf),
            ):
                rc = command_topup_complete_check(make_config())

        self.assertEqual(rc, 0)
        return_mock.assert_not_called()
        self.assertIn("No active topup", buf.getvalue())

    def test_blocked_sbu_return_preserves_topup_and_hold_state(self):
        from growatt_guard.topup import _return_sbu_and_clear_topup

        with (
            patch("growatt_guard.topup.command_return_sbu", return_value=2),
            patch("growatt_guard.topup.clear_utility_hold_state") as clear_hold,
            patch("growatt_guard.topup.clear_topup_state") as clear_topup,
        ):
            result = _return_sbu_and_clear_topup(make_config())

        self.assertEqual(result, 2)
        clear_hold.assert_not_called()
        clear_topup.assert_not_called()

    def test_canonical_time_hold_completes_without_discord_sleep(self):
        from growatt_guard.topup import command_topup_complete_check

        now = dt.datetime.now(dt.timezone.utc)
        hold = {
            "ownership": "owned",
            "completion_policy": "time",
            "started_at": (now - dt.timedelta(minutes=61)).isoformat(),
            "max_expiry": (now - dt.timedelta(minutes=1)).isoformat(),
            "minutes": 60,
            "reason": "Discord top-up for 60 minute(s)",
        }
        normalized_topup = {
            "started_at": hold["started_at"],
            "paused_until": hold["max_expiry"],
            "minutes": 60,
            "reason": hold["reason"],
        }

        with (
            patch("growatt_guard.topup.read_utility_hold_state", return_value=hold),
            patch("growatt_guard.topup.read_topup_state", return_value=normalized_topup),
            patch("growatt_guard.topup.topup_is_active", return_value=False),
            patch("growatt_guard.topup._read_topup_end_soc", return_value=None),
            patch("growatt_guard.topup.command_resume") as resume,
            patch("growatt_guard.topup._return_sbu_and_clear_topup", return_value=0) as finish,
            redirect_stdout(StringIO()),
        ):
            result = command_topup_complete_check(make_config())

        self.assertEqual(result, 0)
        resume.assert_called_once()
        finish.assert_called_once()
