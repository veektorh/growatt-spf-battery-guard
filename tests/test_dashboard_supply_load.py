import datetime as dt
import json
import re
import unittest
from unittest.mock import patch

from growatt_guard.dashboard import build_dashboard_data_payload, build_dashboard_html
from growatt_guard.dashboard_render_components import (
    _load_profile_night_text,
    _render_supply_rows,
    _supply_strip_text,
)
from growatt_guard.weather import ThresholdDecision

WAT = dt.timezone(dt.timedelta(hours=1))
SCHEDULE = {
    "timezone": "Africa/Lagos",
    "jobs": [],
    "outage_windows": [{"days": "weekdays", "start": "15:30", "end": "18:00"}],
}
STATUS = {
    "device": {"capacity": "66%"},
    "storage_params": {
        "storageBean": {"outputConfig": "0", "ppv": 0, "outPutPower": 1800},
        "storageDetailBean": {"bmsSoc": 66, "pDischarge": 1800, "vGrid": 0},
    },
}
DISCHARGE_HISTORY = [
    {"day_type": "weekday", "rate_w": 1900, "bands": {
        "evening": {"rate_w": 2000, "sample_count": 10},
        "overnight": {"rate_w": 1800, "sample_count": 20},
    }},
]


def _history():
    rows = []
    for offset in range(4):
        day = dt.date(2026, 11, 13) + dt.timedelta(days=offset)
        for hour in range(24):
            for minute in (5, 20, 35, 50):
                rows.append({
                    "timestamp": dt.datetime.combine(day, dt.time(hour, minute), tzinfo=WAT).isoformat(),
                    "load_w": 2000 if 18 <= hour < 22 else 1500,
                    "soc": 70,
                })
    # Grid voltage arrives with the release that records it: on until 15:36, then off.
    rows += [
        {"timestamp": "2026-11-17T15:20:00+01:00", "grid_v": 229.0, "soc": 70, "load_w": 1500},
        {"timestamp": "2026-11-17T15:36:00+01:00", "grid_v": 0.0, "soc": 68, "load_w": 1500},
    ]
    return rows


def _payload(history_rows):
    with patch("growatt_guard.dashboard_viewmodel.read_pause_state", return_value=None), patch(
        "growatt_guard.dashboard_viewmodel.read_battery_alert_state", return_value=None
    ), patch("growatt_guard.dashboard_viewmodel.read_growatt_cloud_failure_state", return_value={}), patch(
        "growatt_guard.dashboard_viewmodel.read_pvoutput_state", return_value=None
    ), patch("growatt_guard.dashboard_viewmodel.read_mode_audit_rows", return_value=[]), patch(
        "growatt_guard.dashboard_viewmodel.build_chart_data", return_value={"labels": [], "soc": []}
    ), patch("growatt_guard.dashboard_viewmodel.read_utility_hold_state", return_value=None), patch(
        "growatt_guard.dashboard_viewmodel.read_discharge_rate_history", return_value=DISCHARGE_HISTORY
    ), patch("growatt_guard.dashboard_insights.read_discharge_rate_history", return_value=[]):
        return build_dashboard_data_payload(
            STATUS, SCHEDULE, {"dates": {}}, ThresholdDecision(50, "test"),
            battery_capacity_wh=10000, battery_bms_cutoff_soc=20,
            metrics_history=history_rows,
            now=dt.datetime(2026, 11, 17, 16, 0, tzinfo=WAT),
        )


class DashboardSupplyLoadTests(unittest.TestCase):
    def test_payload_carries_supply_and_load_profile(self):
        payload = _payload(_history())
        supply = payload["supply"]
        self.assertFalse(supply["current"]["on"])
        self.assertEqual(supply["current"]["off_by"], "2026-11-17T15:36+01:00")
        cut = supply["events"][-1]
        self.assertEqual((cut["kind"], cut["scheduled"], cut["ongoing"]), ("cut", "15:30-18:00", True))
        json.dumps(payload)  # the dashboard writes this payload as JSON

        profile = payload["planner"]["load_profile"]
        self.assertEqual(profile["hours"]["19"]["median_w"], 2000)
        self.assertEqual(profile["night"]["hourly"]["median_wh"], 4 * 2000 + 8 * 1500)
        self.assertEqual(profile["night"]["bands"]["energy_wh"], 4 * 2000 + 8 * 1800)

    def test_rendered_page_shows_both_views(self):
        html = build_dashboard_html(_payload(_history()))
        self.assertIn("<span>Estate Supply</span> <b>Off since 15:20–15:36</b>", html)
        self.assertIn('id="observed-supply"', html)
        self.assertIn("<td>15:20–15:36</td>", html)
        self.assertIn("<td>still off</td>", html)
        self.assertIn('id="load-profile-chart"', html)
        self.assertIn("Night from 18:00 for 12h: hourly profile 20.0 kWh", html)
        self.assertIn("band planner 22.4 kWh", html)
        data = re.search(r'<script id="load-profile-data" type="application/json">(.*?)</script>', html, re.S)
        self.assertIsNotNone(data)
        self.assertEqual(json.loads(data.group(1))["19"]["median_w"], 2000)

    def test_page_without_history_still_renders(self):
        html = build_dashboard_html(_payload([]))
        self.assertIn("<b>Off, seen at 16:00</b>", html)  # the live reading carries grid_v 0
        self.assertIn("Night from 18:00 for 12h: hourly profile still learning", html)

    def test_payload_without_supply_or_profile_renders(self):
        payload = _payload([])
        payload["supply"] = None
        payload["planner"]["load_profile"] = None
        html = build_dashboard_html(payload)
        self.assertIn("<b>no reading yet</b>", html)
        self.assertIn("No grid-voltage readings yet.", html)
        self.assertIn("Not enough history yet.", html)


class SupplyRenderTests(unittest.TestCase):
    def test_strip_text(self):
        self.assertEqual(_supply_strip_text(None), "no reading yet")
        self.assertEqual(_supply_strip_text({"on": True}), "On")
        self.assertEqual(_supply_strip_text({"on": False}), "Off")
        self.assertEqual(
            _supply_strip_text({"on": False, "off_after": None, "off_by": "2026-11-17T15:36+01:00"}),
            "Off, seen at 15:36",
        )

    def test_rows_newest_first_with_unscheduled_badge(self):
        events = [
            {"date": "2026-11-17", "kind": "no-cut", "scheduled": "08:00-10:30"},
            {"date": "2026-11-17", "kind": "unscheduled-cut", "scheduled": None,
             "start_after": "2026-11-17T20:00+01:00", "start_by": "2026-11-17T20:14+01:00",
             "end_after": "2026-11-17T20:14+01:00", "end_by": "2026-11-17T20:29+01:00",
             "ongoing": False, "soc_at_start": 71.0},
        ]
        html = _render_supply_rows(events)
        self.assertLess(html.index("unscheduled"), html.index("supply stayed on"))
        self.assertIn("<td>20:14–20:29</td>", html)
        self.assertIn('<td class="r">71%</td>', html)

    def test_night_text_without_bands(self):
        profile = {"night": {"start": "2026-11-17T18:00+01:00", "hours": 12,
                             "hourly": {"median_wh": 21200, "low_wh": 18500, "high_wh": 25500}, "bands": None}}
        self.assertEqual(
            _load_profile_night_text(profile),
            "Night from 18:00 for 12h: hourly profile 21.2 kWh (likely 18.5–25.5).",
        )


if __name__ == "__main__":
    unittest.main()
