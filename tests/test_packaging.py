import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


class PackagingMetadataTests(unittest.TestCase):
    def test_console_script_points_at_cli_main(self):
        project = load_pyproject()["project"]

        self.assertEqual(project["scripts"]["growatt-guard"], "growatt_guard.cli:main")

    def test_project_dependencies_match_requirements_file(self):
        project = load_pyproject()["project"]
        requirements = [
            line.strip()
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        self.assertEqual(project["dependencies"], requirements)


if __name__ == "__main__":
    unittest.main()
