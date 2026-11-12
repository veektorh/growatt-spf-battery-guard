import datetime as dt
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
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

    def test_message_covers_every_mode(self):
        self.assertEqual(outage_days_message({"mode": "all"}), "preserve jobs run every day")
        self.assertIn("weekday", outage_days_message({"mode": "weekdays"}))
        self.assertIn("profile", outage_days_message({"mode": "auto"}))
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


class OutageGateResolutionTests(unittest.TestCase):
    """Hourly-gate resolution: profile windows in `auto`, manual modes otherwise."""

    WEEKDAY_ONLY = {
        "active_profile": "estate-current",
        "outage_windows": [{"days": "weekdays", "start": "08:00", "end": "10:30"}],
    }
    SIXTEEN_H = {
        "active_profile": "estate-16h",
        "outage_windows": [
            {"days": "weekdays", "start": "08:00", "end": "12:00"},
            {"days": "weekends", "start": "08:00", "end": "11:00"},
        ],
    }
    NO_PROFILE = {"active_profile": None, "outage_windows": []}

    def test_auto_follows_a_weekday_only_profile(self):
        from growatt_guard.outage_days import today_is_outage_day

        self.assertTrue(
            today_is_outage_day("auto", dt.datetime(2026, 11, 10), schedule=self.WEEKDAY_ONLY),
            "Tuesday must be an outage day",
        )
        self.assertFalse(
            today_is_outage_day("auto", dt.datetime(2026, 11, 7), schedule=self.WEEKDAY_ONLY),
            "This fixture declares no weekend cuts",
        )

    def test_auto_follows_estate_16h_weekend_windows(self):
        from growatt_guard.outage_days import today_is_outage_day

        self.assertTrue(today_is_outage_day("auto", dt.datetime(2026, 11, 7), schedule=self.SIXTEEN_H))
        self.assertTrue(today_is_outage_day("auto", dt.datetime(2026, 11, 10), schedule=self.SIXTEEN_H))

    def test_auto_without_a_profile_behaves_like_all(self):
        from growatt_guard.outage_days import today_is_outage_day

        self.assertTrue(today_is_outage_day("auto", dt.datetime(2026, 11, 7), schedule=self.NO_PROFILE))

    def test_manual_weekdays_overrides_profile_windows(self):
        from growatt_guard.outage_days import today_is_outage_day

        self.assertFalse(today_is_outage_day("weekdays", dt.datetime(2026, 11, 7), schedule=self.SIXTEEN_H))

    def test_auto_reads_the_shipped_schedule(self):
        from growatt_guard.outage_days import today_is_outage_day
        from growatt_guard.schedule import load_schedule_document, outages_for_date

        self.assertTrue(today_is_outage_day("auto", dt.datetime(2026, 11, 10)))
        for day in (dt.date(2026, 11, 14), dt.date(2026, 11, 15)):  # Sat, Sun
            self.assertTrue(
                today_is_outage_day("auto", dt.datetime(day.year, day.month, day.day)),
                f"{day} has an 08:00-10:30 estate cut and must be an outage day",
            )

        windows = load_schedule_document()["profiles"]["estate-current"]["outages"]
        weekend = [(w["start"], w["end"]) for w in outages_for_date(windows, dt.date(2026, 11, 14))]
        self.assertEqual(weekend, [("08:00", "10:30")])

    def test_gate_source_names_the_authority(self):
        from growatt_guard.outage_days import outage_gate_source

        self.assertEqual(outage_gate_source("auto", schedule=self.WEEKDAY_ONLY), "profile:estate-current")
        self.assertEqual(outage_gate_source("auto", schedule=self.NO_PROFILE), "fallback:all")
        self.assertEqual(outage_gate_source("all"), "manual:all")
        self.assertEqual(outage_gate_source("weekdays"), "manual:weekdays")

    def test_mode_label_reports_today(self):
        from growatt_guard.outage_days import outage_mode_label

        weekday = outage_mode_label("auto", dt.datetime(2026, 11, 10), schedule=self.WEEKDAY_ONLY)
        weekend = outage_mode_label("auto", dt.datetime(2026, 11, 7), schedule=self.WEEKDAY_ONLY)
        self.assertIn("today: outage day", weekday)
        self.assertIn("today: no outage", weekend)
        self.assertIn("profile:estate-current", weekday)

    def test_status_command_lists_the_gate_and_upcoming_days(self):
        config = make_config()
        with TemporaryDirectory() as tmpdir, patch("growatt_guard.state.STATE_DIR", Path(tmpdir)), patch(
            "growatt_guard.state.OUTAGE_DAYS_FILE", Path(tmpdir) / "outage_days.json"
        ), redirect_stdout(StringIO()) as output:
            return_code = command_outage_status(config)

        text = output.getvalue()
        self.assertEqual(return_code, 0)
        self.assertIn("Outage days mode: all", text)
        self.assertIn("only preserve-battery is skipped", text)
        self.assertIn("Next 7 day(s)", text)


class OutageGateScopeTests(unittest.TestCase):
    """Preserve may be skipped on a non-outage day; SBU returns must never be."""

    STATUS = {
        "device": {"capacity": "70 %"},
        "storage_params": {"storageBean": {"outputConfig": "0"}},
    }

    def _run(self, command_name: str):
        from growatt_guard import modes
        from growatt_power_guard import DeviceRef

        config = make_config(discord_notify_skip=False)
        with (
            TemporaryDirectory() as tmpdir,
            patch("growatt_guard.audit.LOG_DIR", Path(tmpdir)),
            patch("growatt_guard.audit.MODE_AUDIT_FILE", Path(tmpdir) / "mode_decisions.csv"),
            patch("growatt_guard.modes.ensure_not_paused", return_value=False),
            patch("growatt_guard.modes.ensure_device_reporting", return_value=False),
            patch(
                "growatt_guard.modes.load_context",
                return_value=(None, DeviceRef("p", "s", "storage", {}), self.STATUS),
            ),
            patch("growatt_guard.modes.ensure_outage_day", return_value=False) as gate,
            redirect_stdout(StringIO()),
        ):
            result = getattr(modes, command_name)(config)
        return result, gate

    def test_return_sbu_never_consults_the_outage_gate(self):
        result, gate = self._run("command_return_sbu")

        self.assertEqual(result, 0)
        gate.assert_not_called()

    def test_watchdog_sbu_never_consults_the_outage_gate(self):
        result, gate = self._run("command_watchdog_sbu")

        self.assertEqual(result, 0)
        gate.assert_not_called()

    def test_preserve_battery_is_outage_gated(self):
        from growatt_guard import modes

        config = make_config(discord_notify_skip=False)
        with (
            patch("growatt_guard.modes.ensure_not_paused", return_value=False),
            patch("growatt_guard.modes.ensure_outage_day", return_value=True) as gate,
            redirect_stdout(StringIO()),
        ):
            result = modes.command_preserve_battery(config)

        self.assertEqual(result, 0)
        gate.assert_called_once()


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

    def test_cli_accepts_auto_mode(self):
        parser = build_parser()
        args = parser.parse_args(["set-outage-days", "auto", "--reason", "follow profile"])
        self.assertEqual(args.mode, "auto")

    def test_cli_parses_outage_status(self):
        parser = build_parser()
        args = parser.parse_args(["outage-status"])
        self.assertEqual(args.command, "outage-status")


class OutageDaysDiscordEmbedTests(unittest.TestCase):
    def _stub_discord(self):
        class Field:
            def __init__(self):
                self.name = None
                self.value = None

        class Embed:
            def __init__(self, **kwargs):
                self.title = kwargs.get("title")
                self.color = kwargs.get("color")
                self.fields = []
                self.timestamp = None

            def add_field(self, **kwargs):
                field = Field()
                field.name = kwargs["name"]
                field.value = kwargs["value"]
                self.fields.append(field)

        return SimpleNamespace(Embed=Embed)

    def _embed(self, output: str):
        from growatt_guard.discord_control import build_status_embed

        return build_status_embed(self._stub_discord(), output, 0)

    def test_status_embed_shows_outage_days_when_present(self):
        output = (
            "INFO Current status: plant=plant123, device=SN123, soc=86%, output=SBU priority [0]\n"
            "Outage days mode: weekdays"
        )
        fields = {field.name: field.value for field in self._embed(output).fields}
        self.assertEqual(fields.get("Outage Days"), "weekdays")

    def test_status_embed_omits_outage_field_when_missing(self):
        output = "INFO Current status: plant=plant123, device=SN123, soc=86%, output=SBU priority [0]"
        fields = {field.name: field.value for field in self._embed(output).fields}
        self.assertNotIn("Outage Days", fields)


if __name__ == "__main__":
    unittest.main()
