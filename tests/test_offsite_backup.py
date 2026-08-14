import stat
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OffsiteBackupTests(unittest.TestCase):
    def test_b2_helper_contract_is_offline_verified(self):
        completed = subprocess.run(
            [str(ROOT / "deploy" / "test-b2-upload.sh")],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("B2 upload helper tests passed", completed.stdout)

    def test_backup_is_restore_verified_before_ciphertext_upload(self):
        script = (ROOT / "deploy" / "growatt-backup.sh").read_text(encoding="utf-8")

        self.assertNotIn("--include-active-hold", script)
        self.assertIn("--symmetric --cipher-algo AES256", script)
        self.assertLess(script.index('"${restore_command}"'), script.index('b2_upload "${encrypted}"'))
        self.assertLess(script.index("backup_complete=true"), script.index('b2_upload "${encrypted}"'))
        self.assertNotIn('source "${data_root}/.env"', script)
        self.assertNotIn("tar ", script)
        self.assertIn('readonly backup_dir="/opt/growatt-guard/backups"', script)
        self.assertIn(
            'mktemp -d "${backup_dir}/.gnupg-${timestamp}.XXXXXX"',
            script,
        )
        self.assertIn('readonly passphrase_file="${gpg_home}/passphrase"', script)
        self.assertIn('--passphrase-file "${passphrase_file}"', script)
        self.assertNotIn("--passphrase-fd", script)

    def test_restore_rehearsal_is_offline_and_refuses_live_target(self):
        script = (ROOT / "deploy" / "growatt-restore-backup.sh").read_text(encoding="utf-8")

        self.assertIn("DRY_RUN=true", script)
        self.assertIn('if "utility_hold" in sections', script)
        self.assertIn("Refusing a live or system restore target", script)
        self.assertNotIn("--allow-active-hold", script)

    def test_installed_entrypoints_are_executable(self):
        for relative in (
            "deploy/growatt-backup.sh",
            "deploy/growatt-restore-backup.sh",
            "deploy/test-b2-upload.sh",
            "install_growatt_backup_service.sh",
        ):
            mode = (ROOT / relative).stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR, relative)

    def test_service_is_hardened_and_timer_is_persistent(self):
        service = (ROOT / "deploy" / "growatt-backup.service").read_text(encoding="utf-8")
        timer = (ROOT / "deploy" / "growatt-backup.timer").read_text(encoding="utf-8")

        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("ProtectHome=read-only", service)
        self.assertIn(
            "ReadWritePaths=/opt/growatt-guard/backups /home/ubuntu/automation/logs",
            service,
        )
        self.assertIn("Persistent=true", timer)


if __name__ == "__main__":
    unittest.main()
