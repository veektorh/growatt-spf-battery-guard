import datetime as dt
import json
import os
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helpers import make_config
from growatt_guard.backups import (
    build_backup_payload,
    command_backup_state,
    command_restore_state,
)
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import DeviceRef
from growatt_guard.state import write_json_state


class BackupTests(unittest.TestCase):
    def _paths(self, root: Path) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch("growatt_guard.schedule_overrides.SCHEDULE_OVERRIDES_FILE", root / "schedule_overrides.json"))
        stack.enter_context(patch("growatt_guard.audit.LOG_DIR", root / "logs"))
        stack.enter_context(patch("growatt_guard.audit.MODE_AUDIT_FILE", root / "logs" / "mode_decisions.csv"))
        stack.enter_context(patch("growatt_guard.dashboard_metrics.DASHBOARD_METRICS_FILE", root / "logs" / "dashboard_metrics.jsonl"))
        stack.enter_context(patch("growatt_guard.state.FORECAST_CALIBRATION_FILE", root / "state" / "forecast_calibration.json"))
        stack.enter_context(patch("growatt_guard.state.UTILITY_HOLD_FILE", root / "state" / "utility_hold.json"))
        stack.enter_context(patch("growatt_guard.state.TOPUP_STATE_FILE", root / "state" / "topup_active.json"))
        return stack

    def test_backup_is_selective_and_excludes_hold_by_default(self):
        with TemporaryDirectory() as tmpdir, self._paths(Path(tmpdir)):
            root = Path(tmpdir)
            (root / "schedule_overrides.json").write_text('{"dates": {}}', encoding="utf-8")
            (root / "logs").mkdir()
            (root / "logs" / "dashboard_metrics.jsonl").write_text(
                '{"timestamp":"2026-07-12T12:00:00+01:00","pv_today_kwh":4.2}\n', encoding="utf-8"
            )
            write_json_state(
                root / "state" / "utility_hold.json",
                {
                    "ownership": "owned", "completion_policy": "soc",
                    "started_at": "2026-07-12T10:00:00+00:00",
                    "max_expiry": "2026-07-12T12:00:00+00:00", "target_soc": 50,
                },
            )
            payload = build_backup_payload()

        self.assertIn("schedule_overrides", payload["sections"])
        self.assertIn("dashboard_metrics", payload["sections"])
        self.assertNotIn("utility_hold", payload["sections"])
        self.assertNotIn("session", json.dumps(payload).lower())

    def test_backup_command_writes_private_file(self):
        with TemporaryDirectory() as tmpdir, self._paths(Path(tmpdir)), redirect_stdout(StringIO()):
            output = Path(tmpdir) / "snapshot.backup.json"
            self.assertEqual(command_backup_state(str(output)), 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            mode = os.stat(output).st_mode & 0o777

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(mode, 0o600)

    def test_restore_rejects_active_hold_without_explicit_flag(self):
        now = dt.datetime.now(dt.timezone.utc)
        payload = {
            "schema_version": 1,
            "sections": {"utility_hold": {
                "ownership": "owned", "completion_policy": "soc",
                "started_at": (now - dt.timedelta(minutes=5)).isoformat(),
                "max_expiry": (now + dt.timedelta(hours=1)).isoformat(),
                "target_soc": 50,
            }},
        }
        with TemporaryDirectory() as tmpdir, self._paths(Path(tmpdir)):
            backup = Path(tmpdir) / "hold.backup.json"
            backup.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(GrowattGuardError, "--allow-active-hold"):
                command_restore_state(make_config(), str(backup))

    def test_restore_rejects_expired_hold_before_live_read(self):
        now = dt.datetime.now(dt.timezone.utc)
        payload = {
            "schema_version": 1,
            "sections": {"utility_hold": {
                "ownership": "owned", "completion_policy": "soc",
                "started_at": (now - dt.timedelta(hours=2)).isoformat(),
                "max_expiry": (now - dt.timedelta(hours=1)).isoformat(),
                "target_soc": 50,
            }},
        }
        with TemporaryDirectory() as tmpdir, self._paths(Path(tmpdir)), patch(
            "growatt_guard.backups.load_context"
        ) as load_mock:
            backup = Path(tmpdir) / "expired.backup.json"
            backup.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(GrowattGuardError, "expired"):
                command_restore_state(make_config(), str(backup), allow_active_hold=True)
        load_mock.assert_not_called()

    def test_restore_rejects_hold_missing_explicit_policy(self):
        now = dt.datetime.now(dt.timezone.utc)
        payload = {
            "schema_version": 1,
            "sections": {"utility_hold": {
                "ownership": "owned",
                "started_at": (now - dt.timedelta(minutes=5)).isoformat(),
                "max_expiry": (now + dt.timedelta(hours=1)).isoformat(),
                "target_soc": 50,
            }},
        }
        with TemporaryDirectory() as tmpdir, self._paths(Path(tmpdir)):
            backup = Path(tmpdir) / "missing-policy.backup.json"
            backup.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(GrowattGuardError, "completion_policy"):
                command_restore_state(make_config(), str(backup), allow_active_hold=True)

    def test_restore_active_hold_requires_and_accepts_live_utility_confirmation(self):
        now = dt.datetime.now(dt.timezone.utc)
        hold = {
            "ownership": "owned", "completion_policy": "soc",
            "started_at": (now - dt.timedelta(minutes=5)).isoformat(),
            "max_expiry": (now + dt.timedelta(hours=1)).isoformat(),
            "target_soc": 50,
        }
        payload = {"schema_version": 1, "sections": {"utility_hold": hold}}
        status = {"storage_params": {"storageBean": {"outputConfig": "2"}}}
        with TemporaryDirectory() as tmpdir, self._paths(Path(tmpdir)), patch(
            "growatt_guard.backups.load_context",
            return_value=(None, DeviceRef("plant", "device", "storage", {}), status),
        ), redirect_stdout(StringIO()):
            backup = Path(tmpdir) / "hold.backup.json"
            backup.write_text(json.dumps(payload), encoding="utf-8")
            result = command_restore_state(make_config(), str(backup), allow_active_hold=True)
            restored = json.loads((Path(tmpdir) / "state" / "utility_hold.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(restored["ownership"], "owned")
        self.assertEqual(restored["target_soc"], 50)


if __name__ == "__main__":
    unittest.main()
