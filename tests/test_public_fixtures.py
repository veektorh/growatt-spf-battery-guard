import datetime as dt
import json
import unittest
from pathlib import Path

from growatt_guard.dashboard import extract_dashboard_metric_sources, extract_dashboard_metrics


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


class PublicFixtureTests(unittest.TestCase):
    def test_sbu_fixture_extracts_dashboard_totals_and_sources(self):
        status = load_fixture("spf_sbu_discharging.json")

        metrics = extract_dashboard_metrics(status, now=dt.datetime(2026, 6, 25, 8, 30))
        sources = extract_dashboard_metric_sources(status)

        self.assertEqual(metrics["soc"], 47)
        self.assertEqual(metrics["mode"], "SBU priority")
        self.assertEqual(metrics["pv_w"], 1029)
        self.assertEqual(metrics["grid_today_kwh"], 13.7)
        self.assertEqual(metrics["charge_today_kwh"], 10.5)
        self.assertEqual(metrics["load_today_kwh"], 12.5)
        self.assertEqual(metrics["pv_total"], "2.86 MWh")
        self.assertIn("useEnergyToday", sources["load_today_kwh"])
        self.assertIn("eChargeToday", sources["charge_today_kwh"])

    def test_utility_fixture_extracts_charging_mode(self):
        status = load_fixture("spf_utility_charging.json")

        metrics = extract_dashboard_metrics(status, now=dt.datetime(2026, 6, 25, 23, 30))

        self.assertEqual(metrics["soc"], 53)
        self.assertEqual(metrics["mode"], "Utility first")
        self.assertEqual(metrics["grid_w"], 3880)
        self.assertEqual(metrics["charge_w"], 2400)
        self.assertEqual(metrics["battery_net_w"], -2400)
