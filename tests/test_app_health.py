import datetime as dt
import subprocess
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from helpers import make_config
from growatt_guard.app_health import command_app_health_monitor
from growatt_guard.config import AppHealthTarget
from growatt_guard.state import read_app_health_monitor_state, write_app_health_monitor_state


class Response:
    def __init__(self, status_code=200, text="Healthy"):
        self.status_code = status_code
        self.text = text


class AppHealthMonitorTests(unittest.TestCase):
    def setUp(self):
        self.target = AppHealthTarget("Garage", "http://127.0.0.1:5081/health", "garage-app-1")

    def config(self, **overrides):
        return make_config(
            app_health_targets=(self.target,),
            app_health_recovery_enabled=True,
            app_health_recovery_wait_seconds=0,
            **overrides,
        )

    def test_healthy_target_records_clean_state_without_recovery(self):
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.APP_HEALTH_MONITOR_FILE", Path(tmpdir) / "app_health.json"
        ), patch("growatt_guard.app_health.requests.get", return_value=Response()), patch(
            "growatt_guard.app_health.restart_app_container"
        ) as restart, redirect_stdout(StringIO()):
            result = command_app_health_monitor(self.config())
            state = read_app_health_monitor_state()

        self.assertEqual(result, 0)
        restart.assert_not_called()
        self.assertEqual(state["apps"]["garage"]["consecutive_failures"], 0)

    def test_failure_streak_waits_for_threshold(self):
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.APP_HEALTH_MONITOR_FILE", Path(tmpdir) / "app_health.json"
        ), patch(
            "growatt_guard.app_health.requests.get",
            side_effect=requests.ConnectionError("connection refused"),
        ), patch("growatt_guard.app_health.restart_app_container") as restart, patch(
            "growatt_guard.app_health.send_discord_embed"
        ) as send, redirect_stdout(StringIO()):
            self.assertEqual(command_app_health_monitor(self.config()), 0)
            self.assertEqual(command_app_health_monitor(self.config()), 0)
            state = read_app_health_monitor_state()

        restart.assert_not_called()
        send.assert_not_called()
        self.assertEqual(state["apps"]["garage"]["consecutive_failures"], 2)

    def test_threshold_failure_restarts_once_and_reports_auto_recovery(self):
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.APP_HEALTH_MONITOR_FILE", Path(tmpdir) / "app_health.json"
        ), patch(
            "growatt_guard.app_health.requests.get",
            side_effect=[
                requests.ConnectionError("refused"),
                requests.ConnectionError("refused"),
                requests.ConnectionError("refused"),
                Response(),
            ],
        ), patch(
            "growatt_guard.app_health.restart_app_container",
            return_value=(True, "container restart completed"),
        ) as restart, patch(
            "growatt_guard.app_health.send_discord_embed", return_value=True
        ) as send, redirect_stdout(StringIO()):
            self.assertEqual(command_app_health_monitor(self.config()), 0)
            self.assertEqual(command_app_health_monitor(self.config()), 0)
            self.assertEqual(command_app_health_monitor(self.config()), 0)
            state = read_app_health_monitor_state()

        restart.assert_called_once_with(self.target)
        self.assertEqual(send.call_count, 1)
        self.assertIn("recovered automatically", send.call_args.args[1]["title"])
        self.assertEqual(state["apps"]["garage"]["consecutive_failures"], 0)
        self.assertTrue(state["apps"]["garage"]["last_recovery_attempt_at"])

    def test_failed_recovery_alerts_and_does_not_restart_again_in_incident(self):
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.APP_HEALTH_MONITOR_FILE", Path(tmpdir) / "app_health.json"
        ), patch(
            "growatt_guard.app_health.requests.get",
            side_effect=requests.ConnectionError("refused"),
        ), patch(
            "growatt_guard.app_health.restart_app_container",
            return_value=(False, "docker unavailable"),
        ) as restart, patch(
            "growatt_guard.app_health.send_discord_embed", return_value=True
        ) as send, redirect_stdout(StringIO()):
            for expected in (0, 0, 1, 1):
                self.assertEqual(command_app_health_monitor(self.config()), expected)
            state = read_app_health_monitor_state()

        restart.assert_called_once_with(self.target)
        self.assertEqual(send.call_count, 1)
        self.assertTrue(state["apps"]["garage"]["alerted"])
        self.assertEqual(state["apps"]["garage"]["consecutive_failures"], 4)

    def test_later_health_recovery_sends_clear_notification(self):
        now = dt.datetime(2026, 8, 2, 12, tzinfo=dt.timezone.utc)
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.APP_HEALTH_MONITOR_FILE", Path(tmpdir) / "app_health.json"
        ), patch("growatt_guard.app_health.requests.get", return_value=Response()), patch(
            "growatt_guard.app_health.send_discord_embed", return_value=True
        ) as send, patch("growatt_guard.app_health.utc_now", return_value=now), redirect_stdout(StringIO()):
            write_app_health_monitor_state(
                {
                    "apps": {
                        "garage": {
                            "active": True,
                            "alerted": True,
                            "consecutive_failures": 3,
                            "recovery_attempted": True,
                        }
                    }
                }
            )
            self.assertEqual(command_app_health_monitor(self.config()), 0)
            state = read_app_health_monitor_state()

        self.assertEqual(send.call_count, 1)
        self.assertIn("health recovered", send.call_args.args[1]["title"])
        self.assertFalse(state["apps"]["garage"]["active"])

    def test_new_incident_inside_cooldown_alerts_without_restart(self):
        now = dt.datetime(2026, 8, 2, 12, tzinfo=dt.timezone.utc)
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.APP_HEALTH_MONITOR_FILE", Path(tmpdir) / "app_health.json"
        ), patch(
            "growatt_guard.app_health.requests.get",
            side_effect=requests.ConnectionError("refused"),
        ), patch("growatt_guard.app_health.restart_app_container") as restart, patch(
            "growatt_guard.app_health.send_discord_embed", return_value=True
        ) as send, patch("growatt_guard.app_health.utc_now", return_value=now), redirect_stdout(StringIO()):
            write_app_health_monitor_state(
                {
                    "apps": {
                        "garage": {
                            "active": True,
                            "alerted": False,
                            "consecutive_failures": 2,
                            "first_failure_at": now.isoformat(),
                            "last_recovery_attempt_at": (now - dt.timedelta(minutes=10)).isoformat(),
                            "recovery_attempted": False,
                        }
                    }
                }
            )
            self.assertEqual(command_app_health_monitor(self.config()), 1)

        restart.assert_not_called()
        self.assertEqual(send.call_count, 1)
        self.assertIn("failed", send.call_args.args[1]["title"])

    def test_malformed_per_app_state_resets_safely(self):
        with TemporaryDirectory() as tmpdir, patch(
            "growatt_guard.state.APP_HEALTH_MONITOR_FILE", Path(tmpdir) / "app_health.json"
        ), patch("growatt_guard.app_health.requests.get", return_value=Response()), redirect_stdout(StringIO()):
            write_app_health_monitor_state({"apps": {"garage": "invalid"}})

            self.assertEqual(command_app_health_monitor(self.config()), 0)
            state = read_app_health_monitor_state()

        self.assertEqual(state["apps"]["garage"]["consecutive_failures"], 0)

    def test_restart_command_uses_argument_list_without_shell(self):
        from growatt_guard.app_health import restart_app_container

        completed = subprocess.CompletedProcess([], 0, stdout="garage-app-1\n", stderr="")
        with patch("growatt_guard.app_health.subprocess.run", return_value=completed) as run:
            success, detail = restart_app_container(self.target)

        self.assertTrue(success)
        self.assertIn("completed", detail)
        run.assert_called_once_with(
            ["docker", "restart", "--time", "10", "garage-app-1"],
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )


if __name__ == "__main__":
    unittest.main()
