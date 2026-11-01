import datetime as dt
import unittest
from unittest.mock import patch

from helpers import make_config
from growatt_guard.solar_reconcile import build_reconciliation, command_solar_reconcile


class ReconciliationPlanTests(unittest.TestCase):
    def test_classifies_matches_conflicts_and_gaps(self):
        ledger = {"2026-10-01": 30200, "2026-10-02": 32000, "2026-10-03": 16200}
        reference = {
            "2026-10-01": 30200,
            "2026-10-02": 32300,
            "2026-10-03": 11200,
            "2026-10-04": 20000,
        }
        plan = build_reconciliation(ledger, reference)

        self.assertEqual(plan["missing"], (("2026-10-04", 20000),))
        self.assertEqual(plan["conflicts"], (("2026-10-03", 16200, 11200),))
        self.assertEqual(plan["matching"], 2)
        # Ledger total change if every PVOutput day were applied: matches contribute
        # rounding, the conflict replaces 16.2 with 11.2, and the gap adds 20.0.
        self.assertEqual(plan["ledger_delta_wh"], 300 - 5000 + 20000)

    def test_relative_tolerance_absorbs_rounding_noise(self):
        ledger = {"2026-09-01": 32300}
        plan = build_reconciliation(ledger, {"2026-09-01": 32299})

        self.assertEqual(plan["conflicts"], ())
        self.assertEqual(plan["matching"], 1)


class ReconciliationCommandTests(unittest.TestCase):
    def _config(self):
        return make_config(
            pvoutput_enabled=True,
            pvoutput_api_key="k",
            pvoutput_system_id="s",
            dry_run=False,
            discord_webhook_url="https://example.invalid/hook",
        )

    def test_repairs_drift_and_posts_summary(self):
        ledger_rows = [
            {"date": "2026-10-03", "generated_wh": 16200, "source": "growatt-observability"},
            {"date": "2026-10-05", "generated_wh": 11200, "source": "growatt-observability"},
        ]
        written: list[list[dict]] = []
        reference = {"2026-10-03": 11200, "2026-10-04": 20000, "2026-10-05": 11200}
        with patch(
            "growatt_guard.pvoutput.fetch_pvoutput_daily_outputs", return_value=reference
        ), patch(
            "growatt_guard.solar_reconcile.read_daily_generation", return_value=list(ledger_rows)
        ), patch(
            "growatt_guard.solar_reconcile._write_daily_generation",
            side_effect=lambda rows: written.append(list(rows)),
        ), patch(
            "growatt_guard.solar_reconcile.send_discord_embed", return_value=True
        ) as send:
            exit_code = command_solar_reconcile(self._config(), days=7, apply=True, notify=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(written), 1)
        by_date = {row["date"]: row for row in written[0]}
        self.assertEqual(by_date["2026-10-03"]["generated_wh"], 11200)
        self.assertEqual(by_date["2026-10-03"]["previous_wh"], 16200)
        self.assertEqual(by_date["2026-10-04"]["source"], "pvoutput")
        self.assertEqual(send.call_count, 1)

    def test_preview_reports_drift_without_writing(self):
        with patch(
            "growatt_guard.pvoutput.fetch_pvoutput_daily_outputs",
            return_value={"2026-10-03": 11200},
        ), patch(
            "growatt_guard.solar_reconcile.read_daily_generation",
            return_value=[{"date": "2026-10-03", "generated_wh": 16200}],
        ), patch(
            "growatt_guard.solar_reconcile._write_daily_generation"
        ) as write:
            command_solar_reconcile(self._config(), days=7, apply=False, notify=False)

        write.assert_not_called()

    def test_skips_when_pvoutput_is_disabled(self):
        config = make_config(pvoutput_enabled=False)
        with patch("growatt_guard.pvoutput.fetch_pvoutput_daily_outputs") as fetch:
            self.assertEqual(command_solar_reconcile(config), 0)
        fetch.assert_not_called()

    def test_dry_run_leaves_the_ledger_untouched(self):
        config = make_config(
            pvoutput_enabled=True, pvoutput_api_key="k", pvoutput_system_id="s", dry_run=True
        )
        with patch(
            "growatt_guard.pvoutput.fetch_pvoutput_daily_outputs",
            return_value={"2026-10-03": 11200},
        ), patch(
            "growatt_guard.solar_reconcile.read_daily_generation",
            return_value=[{"date": "2026-10-03", "generated_wh": 16200}],
        ), patch(
            "growatt_guard.solar_reconcile._write_daily_generation"
        ) as write:
            command_solar_reconcile(config, days=7, apply=True, notify=False)

        write.assert_not_called()


class MonthWindowTests(unittest.TestCase):
    def test_january_reports_the_previous_december(self):
        from growatt_guard.audit import month_window

        self.assertEqual(
            month_window(dt.date(2027, 1, 1)),
            (dt.date(2026, 12, 1), dt.date(2026, 12, 31)),
        )

    def test_week_header_matches_the_solar_window(self):
        from growatt_guard.audit import build_weekly_summary

        summary = build_weekly_summary(now=dt.datetime(2026, 6, 21, 21, 10))

        self.assertIn("Growatt weekly performance - 2026-06-15 to 2026-06-21", summary)


if __name__ == "__main__":
    unittest.main()
