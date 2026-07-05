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
            "*.backup.json",
            "*.ics",
        ):
            self.assertIn(pattern, ignored)


if __name__ == "__main__":
    unittest.main()
