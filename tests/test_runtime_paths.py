import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimePathTests(unittest.TestCase):
    def test_release_and_mutable_data_roots_are_separate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app_home = Path(tmpdir) / "release"
            data_home = Path(tmpdir) / "shared"
            app_home.mkdir()
            data_home.mkdir()
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            env["GROWATT_GUARD_HOME"] = str(app_home)
            env["GROWATT_GUARD_DATA_DIR"] = str(data_home)
            env["GROWATT_GUARD_STATE_DIR"] = str(data_home / "state")
            code = """
import json
from growatt_guard import audit, config, dashboard_metrics, pvoutput, schedule, state
print(json.dumps({
    'schedule': str(schedule.SCHEDULE_FILE),
    'overrides': str(schedule.SCHEDULE_OVERRIDES_FILE),
    'env': str(config.BASE_DIR / '.env'),
    'log': str(audit.LOG_FILE),
    'dashboard': str(dashboard_metrics.DASHBOARD_FILE),
    'pvoutput': str(pvoutput.PVOUTPUT_STATE_FILE),
    'state': str(state.STATE_DIR),
}))
"""

            completed = subprocess.run(
                [sys.executable, "-c", code], cwd=ROOT, env=env,
                check=True, capture_output=True, text=True,
            )
            paths = json.loads(completed.stdout)

        self.assertEqual(paths["schedule"], str(app_home / "schedule.json"))
        self.assertEqual(paths["overrides"], str(data_home / "schedule_overrides.json"))
        self.assertEqual(paths["env"], str(data_home / ".env"))
        self.assertEqual(paths["log"], str(data_home / "logs" / "growatt_power_guard.log"))
        self.assertEqual(paths["dashboard"], str(data_home / "dashboard.html"))
        self.assertEqual(paths["pvoutput"], str(data_home / "state" / "pvoutput_last.json"))
        self.assertEqual(paths["state"], str(data_home / "state"))
