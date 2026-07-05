import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install_cloud_cron.sh"


class InstallCloudCronTests(unittest.TestCase):
    def test_help_documents_dry_run(self):
        completed = subprocess.run(
            [str(INSTALLER), "--help"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("--dry-run", completed.stdout)
        self.assertIn("without installing", completed.stdout)

    def test_dry_run_prints_diff_without_installing_crontab(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            fake_crontab = tmp_path / "crontab"
            fake_crontab.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [[ ${1:-} == '-l' ]]; then\n"
                "  printf '%s\\n' 'MAILTO=ops@example.invalid'\n"
                "  printf '%s\\n' '0 1 * * * cd /old && python old.py # growatt-power-guard'\n"
                "  exit 0\n"
                "fi\n"
                "echo install attempted >&2\n"
                "exit 23\n",
                encoding="utf-8",
            )
            fake_crontab.chmod(fake_crontab.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
            env["PYTHON_BIN"] = sys.executable

            completed = subprocess.run(
                [str(INSTALLER), "--dry-run"],
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertIn("Dry run only", completed.stdout)
        self.assertIn("--- current-crontab", completed.stdout)
        self.assertIn("+++ proposed-crontab", completed.stdout)
        self.assertIn("-0 1 * * * cd /old && python old.py # growatt-power-guard", completed.stdout)
        self.assertIn("MAILTO=ops@example.invalid", completed.stdout)
        self.assertIn("growatt_power_guard.py run-scheduled", completed.stdout)
        self.assertNotIn("install attempted", completed.stderr)

    def test_script_syntax_is_valid(self):
        subprocess.run(["bash", "-n", str(INSTALLER)], cwd=ROOT, check=True)


if __name__ == "__main__":
    unittest.main()
