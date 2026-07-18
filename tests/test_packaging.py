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

    def test_production_lock_is_fully_pinned_and_contains_direct_dependencies(self):
        project = load_pyproject()["project"]
        locked = {
            line.strip().split("==", 1)[0].lower(): line.strip()
            for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }

        self.assertTrue(locked)
        self.assertTrue(all("==" in requirement for requirement in locked.values()))
        for dependency in project["dependencies"]:
            name = dependency.split("==", 1)[0].lower()
            self.assertEqual(locked[name], dependency)

    def test_build_tool_lock_is_fully_pinned(self):
        requirements = [
            line.strip()
            for line in (ROOT / "requirements-build.lock").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]

        self.assertEqual(len(requirements), 2)
        self.assertTrue(all("==" in requirement for requirement in requirements))
        self.assertTrue(any(requirement.startswith("setuptools==") for requirement in requirements))
        self.assertTrue(any(requirement.startswith("wheel==") for requirement in requirements))

    def test_wheel_includes_compatibility_module(self):
        setuptools = load_pyproject()["tool"]["setuptools"]

        self.assertIn("growatt_power_guard", setuptools["py-modules"])
        self.assertIn("scripts*", setuptools["packages"]["find"]["include"])


if __name__ == "__main__":
    unittest.main()
