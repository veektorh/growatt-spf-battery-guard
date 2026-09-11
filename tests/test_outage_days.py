import datetime as dt
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_power_guard import (
    GrowattGuardError,
    build_parser,
    command_outage_status,
    command_set_outage_days,
    ensure_outage_day,
    outage_days_message,
    outage_mode,
    read_outage_days_state,
    today_is_outage_day,
    write_outage_days_state,
)


class OutageDaysStateTests(unittest.TestCase):
    def test_missing_state_defaults_to_all(self):
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
            "growatt_guard.state.OUTAGE_DAYS_FILE", Path(tmpdir) / "outage_days.json"
        ):
            self.assertIsNone(read_outage_days_state())
            self.assertEqual(outage_mode(), "all")

    def test_write_and_read_state_round_trip(self):
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
            "growatt_guard.state.OUTAGE_DAYS_FILE", Path(tmpdir) / "outage_days.json"
        ):
            state = write_outage_days_state("weekdays", "estate paused weekend cuts")
            read_back = read_outage_days_state()
            final_mode = outage_mode()

        self.assertEqual(state["mode"], "weekdays")
        self.assertEqual(state["reason"], "estate paused weekend cuts")
        self.assertEqual(read_back["mode"], "weekdays")
        self.assertEqual(final_mode, "weekdays")

    def test_write_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            write_outage_days_state("sundays")

    def test_invalid_state_file_falls_back_to_all(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "outage_days.json"
            path.write_text('{"mode": "sundays"}', encoding="utf-8")
            with patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
                "growatt_guard.state.OUTAGE_DAYS_FILE", path
            ):
                self.assertIsNone(read_outage_days_state())
                self.assertEqual(outage_mode(), "all")

    def test_message_covers_both_modes(self):
        self.assertEqual(outage_days_message({"mode": "all"}), "mode-changing jobs run every day")
        self.assertIn("weekdays", outage_days_message({"mode": "weekdays"}))
        self.assertIn("every day", outage_days_message(None))


class OutageDayGateTests(unittest.TestCase):
    def test_all_mode_never_skips(self):
        self.assertTrue(today_is_outage_day("all", now=dt.datetime(2026, 9, 12)))
        self.assertTrue(today_is_outage_day(None, now=dt.datetime(2026, 9, 12)))

    def test_weekdays_mode_allows_mon_to_fri(self):
        for day in (7, 8, 9, 10, 11):  # Mon 2026-09-07 .. Fri 2026-09-11
            self.assertTrue(today_is_outage_day("weekdays", now=dt.datetime(2026, 9, day)))

    def test_weekdays_mode_skips_weekend(self):
        for day in (12, 13):  # Sat/Sun 2026-09-12/13
            self.assertFalse(today_is_outage_day("weekdays", now=dt.datetime(2026, 9, day)))

    def test_ensure_outage_day_skips_on_weekend(self):
        config = make_config(discord_notify_skip=False)
        with patch("growatt_guard.outage_days.today_is_outage_day", return_value=False), redirect_stdout(
            StringIO()
        ) as output:
            self.assertTrue(ensure_outage_day(config, "preserve-battery"))
        self.assertIn("not an outage day", output.getvalue())

    def test_ensure_outage_day_passes_on_outage_day(self):
        config = make_config(discord_notify_skip=False)
        with patch("growatt_guard.outage_days.today_is_outage_day", return_value=True), redirect_stdout(StringIO()):
            self.assertFalse(ensure_outage_day(config, "preserve-battery"))


class OutageDaysCommandTests(unittest.TestCase):
    def test_command_set_outage_days_writes_state_and_prints(self):
        config = make_config()
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
            "growatt_guard.state.OUTAGE_DAYS_FILE", Path(tmpdir) / "outage_days.json"
        ), redirect_stdout(StringIO()) as output:
            return_code = command_set_outage_days(config, "weekdays", "estate resumed cuts")
            final_mode = outage_mode()

        self.assertEqual(return_code, 0)
        self.assertIn("weekdays", output.getvalue())
        self.assertEqual(final_mode, "weekdays")

    def test_command_set_outage_days_rejects_bad_mode(self):
        config = make_config()
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
            "growatt_guard.state.OUTAGE_DAYS_FILE", Path(tmpdir) / "outage_days.json"
        ):
            with self.assertRaises(GrowattGuardError):
                command_set_outage_days(config, "never")

    def test_command_outage_status_prints_mode(self):
        config = make_config()
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
            "growatt_guard.state.OUTAGE_DAYS_FILE", Path(tmpdir) / "outage_days.json"
        ), redirect_stdout(StringIO()) as output:
            return_code = command_outage_status(config)

        self.assertEqual(return_code, 0)
        self.assertIn("Outage days mode: all", output.getvalue())

    def test_cli_parses_set_outage_days(self):
        parser = build_parser()
        args = parser.parse_args(["set-outage-days", "weekdays", "--reason", "estate cuts"])
        self.assertEqual(args.mode, "weekdays")
        self.assertEqual(args.reason, "estate cuts")

    def test_cli_parses_outage_status(self):
        parser = build_parser()
        args = parser.parse_args(["outage-status"])
        self.assertEqual(args.command, "outage-status")


if __name__ == "__main__":
    unittest.main()
