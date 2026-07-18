import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GitignoreHygieneTests(unittest.TestCase):
    def test_generated_ops_artifacts_are_ignored(self):
        ignored = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

        for pattern in (
            ".env",
            "logs/",
            "state/",
            "dashboard.html",
            "dashboard.json",
            "growatt-probe-*.json",
            "backups/",
            ".deploy/",
            "dist/",
            "*.egg-info/",
            "*.backup.json",
            "*.ics",
        ):
            self.assertIn(pattern, ignored)

    def test_local_verification_runs_shell_syntax_check(self):
        verify_script = (ROOT / "verify_local.sh").read_text(encoding="utf-8")

        self.assertIn('echo "== Shell syntax =="', verify_script)
        self.assertIn("git ls-files '*.sh'", verify_script)
        self.assertIn('bash -n "$script"', verify_script)

    def test_packaged_entrypoint_is_executable(self):
        entrypoint = ROOT / "packaged_entrypoint.sh"

        self.assertTrue(entrypoint.stat().st_mode & 0o100)


if __name__ == "__main__":
    unittest.main()
