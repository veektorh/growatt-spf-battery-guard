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


class SolarSkipTopupTests(unittest.TestCase):
    """Tests for AUTO_TOPUP_SOLAR_SKIP_KWH_M2 solar-forecast skip in command_auto_topup_check."""

    def _make_status(self, soc: float = 35.0, discharge_w: float = 1800.0) -> dict:
        return {
            "datalogSn": "SN1",
            "deviceSn": "SN1",
            "data": {"soc": soc, "pDischarge": discharge_w, "outputConfig": "0"},
        }

    def _base_cfg(self, **overrides) -> "Config":
        return make_config(
            auto_topup_enabled=True,
            auto_topup_min_hours_to_sunrise=4.0,
            battery_capacity_wh=30000.0,
            battery_charge_rate_w=3000.0,
            battery_bms_cutoff_soc=25.0,
            dry_run=True,
            **overrides,
        )

    def _run(self, cfg, status, tomorrow_kwh=None):
        from growatt_guard.topup import command_auto_topup_check
        from growatt_guard import state as state_mod
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory

        written = {}

        def fake_write_hold(**kwargs):
            written["minutes"] = kwargs["minutes"]

        buf = StringIO()
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            patches = [
                patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup_active.json"),
                patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
                patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", tmp / "dr.json"),
                patch("growatt_guard.topup.read_pause_state", return_value=None),
                patch("growatt_guard.topup.topup_is_active", return_value=False),
                patch("growatt_guard.topup.hours_until_next_sunrise", return_value=5.0),
                patch("growatt_guard.topup.load_context", return_value=(None, None, status)),
                patch("growatt_guard.topup.set_mode", return_value="ok"),
                patch("growatt_guard.topup.command_pause", return_value=0),
                patch("growatt_guard.topup.write_utility_hold_state", side_effect=fake_write_hold),
                patch("growatt_guard.topup.append_mode_audit"),
                patch("growatt_guard.weather.get_tomorrow_solar_kwh_m2", return_value=tomorrow_kwh),
            ]
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                 patches[5], patches[6], patches[7], patches[8], patches[9], \
                 patches[10], patches[11], redirect_stdout(buf):
                rc = command_auto_topup_check(cfg)

        return rc, buf.getvalue(), written

    def test_topup_skipped_when_solar_forecast_above_threshold(self):
        cfg = self._base_cfg(auto_topup_solar_skip_kwh_m2=4.0, auto_topup_target_soc=50.0)
        rc, out, written = self._run(cfg, self._make_status(soc=55.0, discharge_w=1000.0), tomorrow_kwh=5.2)
        self.assertEqual(rc, 0)
        self.assertIn("skipping", out)
        self.assertIn("5.2 kWh/m²", out)
        self.assertNotIn("minutes", written)

    def test_topup_proceeds_when_solar_forecast_below_threshold(self):
        cfg = self._base_cfg(auto_topup_solar_skip_kwh_m2=4.0)
        rc, out, written = self._run(cfg, self._make_status(), tomorrow_kwh=2.8)
        self.assertIn("minutes", written)

    def test_topup_proceeds_when_sunny_but_survival_margin_needs_topup(self):
        cfg = self._base_cfg(auto_topup_solar_skip_kwh_m2=4.0)
        rc, out, written = self._run(cfg, self._make_status(soc=35.0, discharge_w=1800.0), tomorrow_kwh=5.2)
        self.assertIn("minutes", written)
        self.assertIn("Auto-topup started", out)

    def test_topup_proceeds_when_feature_disabled(self):
        cfg = self._base_cfg(auto_topup_solar_skip_kwh_m2=0.0)
        rc, out, written = self._run(cfg, self._make_status(), tomorrow_kwh=9.9)
        self.assertIn("minutes", written)

    def test_topup_proceeds_when_solar_forecast_unavailable(self):
        cfg = self._base_cfg(auto_topup_solar_skip_kwh_m2=4.0)
        rc, out, written = self._run(cfg, self._make_status(), tomorrow_kwh=None)
        self.assertIn("minutes", written)

    def test_topup_skipped_exactly_at_threshold(self):
        cfg = self._base_cfg(auto_topup_solar_skip_kwh_m2=4.0, auto_topup_target_soc=50.0)
        rc, out, written = self._run(cfg, self._make_status(soc=55.0, discharge_w=1000.0), tomorrow_kwh=4.0)
        self.assertNotIn("minutes", written)
        self.assertIn("skipping", out)


class TopupStatsWeeklySummaryTests(unittest.TestCase):
    """Tests for auto-topup stats section in build_weekly_summary."""

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

    def test_topup_count_and_minutes_in_summary(self):
        import datetime as dt
        from growatt_guard.audit import build_weekly_summary, MODE_AUDIT_FILE
        now = dt.datetime(2026, 6, 21, 21, 0)
        rows = [
            {"timestamp": "2026-06-19T01:00:00", "action": "auto-topup-started", "note": "45min, 5.0h to sunrise"},
            {"timestamp": "2026-06-20T02:00:00", "action": "auto-topup-started", "note": "30min, 4.5h to sunrise"},
        ]
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                result = build_weekly_summary(now=now)
        self.assertIn("Auto-topups: 2", result)
        self.assertIn("75 min", result)

    def test_kwh_estimate_shown_when_charge_rate_set(self):
        import datetime as dt
        from growatt_guard.audit import build_weekly_summary
        now = dt.datetime(2026, 6, 21, 21, 0)
        rows = [
            {"timestamp": "2026-06-19T01:00:00", "action": "auto-topup-started", "note": "60min, 5.0h to sunrise"},
        ]
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                # 60 min * 3000 W / 60 / 1000 = 3.0 kWh
                result = build_weekly_summary(now=now, charge_rate_w=3000.0)
        self.assertIn("3.0 kWh", result)

    def test_no_topups_shows_zero(self):
        import datetime as dt
        from growatt_guard.audit import build_weekly_summary
        now = dt.datetime(2026, 6, 21, 21, 0)
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, [])
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                result = build_weekly_summary(now=now)
        self.assertIn("Auto-topups: 0", result)

    def test_threshold_tuning_warns_when_soc_near_cutoff(self):
        import datetime as dt
        from growatt_guard.audit import build_weekly_summary
        now = dt.datetime(2026, 6, 21, 21, 0)
        rows = [
            {
                "timestamp": "2026-06-20T01:00:00",
                "command": "auto-topup-check",
                "soc": "29",
                "action": "auto-topup-started",
                "note": "20min, 5.0h to sunrise",
            },
            {
                "timestamp": "2026-06-20T06:30:00",
                "command": "preserve-battery",
                "soc": "35",
                "threshold": "50",
                "action": "switch-to-utility",
            },
        ]
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                result = build_weekly_summary(
                    now=now,
                    low_battery_soc=50.0,
                    battery_bms_cutoff_soc=25.0,
                )

        self.assertIn("Threshold tuning:", result)
        self.assertIn("Near-cutoff readings (<= 30%): 1", result)
        self.assertIn("Do not lower yet", result)

    def test_threshold_tuning_allows_small_lower_trial_when_week_is_comfortable(self):
        import datetime as dt
        from growatt_guard.audit import build_weekly_summary
        now = dt.datetime(2026, 6, 21, 21, 0)
        rows = [
            {
                "timestamp": f"2026-06-{15 + (i // 2):02d}T{6 + (i % 2):02d}:30:00",
                "command": "preserve-battery",
                "soc": "56",
                "threshold": "50",
                "action": "no-change",
            }
            for i in range(12)
        ]
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                result = build_weekly_summary(
                    now=now,
                    low_battery_soc=50.0,
                    battery_bms_cutoff_soc=25.0,
                )

        self.assertIn("Observed SOC range: 56% to 56%", result)
        self.assertIn("Could trial lowering LOW_BATTERY_SOC by 2-3%", result)


class DailyTomorrowForecastTests(unittest.TestCase):
    """Tests for tomorrow's solar forecast line in build_daily_summary."""

    def test_forecast_line_present_when_provided(self):
        from growatt_guard.audit import build_daily_summary
        with patch("growatt_guard.audit.summarize_today_log_counts", return_value={
            "success": 0, "failure": 0, "watchdog_repairs": 0,
            "preserve_actions": 0, "return_sbu_actions": 0,
        }):
            result = build_daily_summary({}, tomorrow_kwh_m2=5.3)
        self.assertIn("Tomorrow's solar forecast: 5.3 kWh/m²", result)

    def test_forecast_line_absent_when_none(self):
        from growatt_guard.audit import build_daily_summary
        with patch("growatt_guard.audit.summarize_today_log_counts", return_value={
            "success": 0, "failure": 0, "watchdog_repairs": 0,
            "preserve_actions": 0, "return_sbu_actions": 0,
        }):
            result = build_daily_summary({}, tomorrow_kwh_m2=None)
        self.assertNotIn("Tomorrow's solar forecast", result)


class PruneAuditTests(unittest.TestCase):
    """Tests for prune_audit_rows."""

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

    def test_removes_old_rows_keeps_recent(self):
        import datetime as dt
        from growatt_guard.audit import prune_audit_rows, MODE_AUDIT_FILE
        cutoff = dt.datetime(2026, 6, 1)
        rows = [
            {"timestamp": "2026-05-01T10:00:00", "action": "switch-to-utility"},  # old
            {"timestamp": "2026-06-15T10:00:00", "action": "switch-to-sbu"},       # recent
        ]
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                removed, kept = prune_audit_rows(cutoff)
        self.assertEqual(removed, 1)
        self.assertEqual(kept, 1)

    def test_returns_zero_when_nothing_to_prune(self):
        import datetime as dt
        from growatt_guard.audit import prune_audit_rows, MODE_AUDIT_FILE
        cutoff = dt.datetime(2026, 1, 1)
        rows = [
            {"timestamp": "2026-06-15T10:00:00", "action": "switch-to-sbu"},
        ]
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                removed, kept = prune_audit_rows(cutoff)
        self.assertEqual(removed, 0)
        self.assertEqual(kept, 1)

    def test_returns_zero_zero_when_file_missing(self):
        import datetime as dt
        from growatt_guard.audit import prune_audit_rows, MODE_AUDIT_FILE
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "missing.csv"
            with patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path):
                removed, kept = prune_audit_rows(dt.datetime(2026, 6, 1))
        self.assertEqual((removed, kept), (0, 0))

    def test_command_prune_audit_prints_result(self):
        import datetime as dt
        from growatt_guard.reports import command_prune_audit
        from growatt_guard.audit import MODE_AUDIT_FILE
        rows = [
            {"timestamp": "2026-03-01T10:00:00", "action": "switch-to-utility"},
            {"timestamp": "2026-06-15T10:00:00", "action": "switch-to-sbu"},
        ]
        with TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "mode_decisions.csv"
            self._write_audit(audit_path, rows)
            cfg = make_config(audit_retention_days=90)
            buf = StringIO()
            with (patch("growatt_guard.audit.MODE_AUDIT_FILE", audit_path),
                  redirect_stdout(buf)):
                command_prune_audit(cfg)
        self.assertIn("pruned", buf.getvalue())


class DischargeRateHistoryTests(unittest.TestCase):
    """Tests for discharge rate rolling average in state.py."""

    def test_appends_reading_and_returns_list(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "dr.json"):
                readings = state_mod.append_discharge_rate_reading(1500.0)
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0]["rate_w"], 1500)

    def test_trims_to_max_readings(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "dr.json"):
                for i in range(12):
                    readings = state_mod.append_discharge_rate_reading(float(1000 + i * 100))
        self.assertEqual(len(readings), state_mod._DISCHARGE_RATE_MAX_READINGS)
        self.assertEqual(readings[-1]["rate_w"], 2100)

    def test_read_returns_empty_list_when_missing(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "dr.json"):
                result = state_mod.read_discharge_rate_history()
        self.assertEqual(result, [])

    def test_accumulates_across_calls(self):
        from growatt_guard import state as state_mod
        with TemporaryDirectory() as tmpdir:
            with patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", Path(tmpdir) / "dr.json"):
                state_mod.append_discharge_rate_reading(1200.0)
                state_mod.append_discharge_rate_reading(1800.0)
                readings = state_mod.read_discharge_rate_history()
        self.assertEqual(len(readings), 2)
        self.assertEqual(readings[0]["rate_w"], 1200)
        self.assertEqual(readings[1]["rate_w"], 1800)


class DischargeRateAverageTests(unittest.TestCase):
    """Tests that command_auto_topup_check uses the rolling discharge average."""

    def _run_check(self, cfg, tmpdir, discharge_w: float, prior_readings: list[float]) -> dict:
        from growatt_guard.topup import command_auto_topup_check
        from growatt_guard import state as state_mod

        status = {
            "datalogSn": "SN1", "deviceSn": "SN1",
            "data": {"soc": 40.0, "pDischarge": discharge_w, "outputConfig": "0"},
        }
        captured: dict = {}

        def fake_write(**kwargs):
            captured["minutes"] = kwargs["minutes"]
            captured["start_load_w"] = kwargs["start_load_w"]

        tmp = Path(tmpdir)
        dr_file = tmp / "dr.json"
        with patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", dr_file):
            for r in prior_readings:
                state_mod.append_discharge_rate_reading(r)

        with (patch.object(state_mod, "DISCHARGE_RATE_HISTORY_FILE", dr_file),
              patch.object(state_mod, "TOPUP_STATE_FILE", tmp / "topup.json"),
              patch.object(state_mod, "PAUSE_FILE", tmp / "pause.json"),
              patch("growatt_guard.topup.read_pause_state", return_value=None),
              patch("growatt_guard.topup.topup_is_active", return_value=False),
              patch("growatt_guard.topup.hours_until_next_sunrise", return_value=5.0),
              patch("growatt_guard.topup.load_context", return_value=(None, None, status)),
              patch("growatt_guard.topup.set_mode", return_value="ok"),
              patch("growatt_guard.topup.command_pause", return_value=0),
              patch("growatt_guard.topup.write_utility_hold_state", side_effect=fake_write),
              patch("growatt_guard.topup.append_mode_audit"),
              redirect_stdout(StringIO())):
            command_auto_topup_check(cfg)

        return captured

    def _make_cfg(self):
        return make_config(
            auto_topup_enabled=True,
            battery_capacity_wh=30000.0,
            battery_charge_rate_w=3000.0,
            battery_bms_cutoff_soc=25.0,
            dry_run=True,
        )

    def test_uses_live_reading_when_history_has_only_one_entry(self):
        cfg = self._make_cfg()
        with TemporaryDirectory() as tmpdir:
            result = self._run_check(cfg, tmpdir, discharge_w=1500.0, prior_readings=[])
        # After appending live (1500), history has 1 entry → uses live
        self.assertAlmostEqual(result["start_load_w"], 1500.0, delta=1.0)

    def test_uses_average_when_history_has_prior_readings(self):
        cfg = self._make_cfg()
        with TemporaryDirectory() as tmpdir:
            # Seed one prior reading at 600 W; live spike = 3000 W
            # After appending live: history = [600, 3000] → avg = 1800 W
            result = self._run_check(cfg, tmpdir, discharge_w=3000.0, prior_readings=[600.0])
        self.assertAlmostEqual(result["start_load_w"], 1800.0, delta=1.0)

    def test_average_produces_different_topup_than_spike(self):
        cfg = self._make_cfg()
        with TemporaryDirectory() as tmpdir:
            spike_result = self._run_check(cfg, tmpdir, discharge_w=3000.0, prior_readings=[])
        with TemporaryDirectory() as tmpdir:
            avg_result = self._run_check(cfg, tmpdir, discharge_w=3000.0, prior_readings=[600.0])
        # Spike (3000 W) should demand more topup minutes than the smoothed avg (1800 W)
        self.assertGreater(spike_result.get("minutes", 0), avg_result.get("minutes", 0))


if __name__ == "__main__":
    unittest.main()
