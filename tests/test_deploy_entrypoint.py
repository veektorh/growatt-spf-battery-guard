import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "deploy" / "growatt-deploy.sh"


class GrowattDeployEntrypointTests(unittest.TestCase):
    def test_entrypoint_is_executable(self):
        self.assertTrue(ENTRYPOINT.stat().st_mode & stat.S_IXUSR)

    def test_rejects_invalid_release_sha_before_deployment(self):
        result = subprocess.run(
            [str(ENTRYPOINT), "not-a-release"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(64, result.returncode)
        self.assertIn("40-character-release-sha", result.stderr)

    def test_deploys_only_the_verified_origin_main_sha(self):
        release_sha = "a" * 40

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            deploy_root = temp / "automation"
            bin_dir = temp / "bin"
            deploy_root.mkdir()
            (deploy_root / ".git").mkdir()
            bin_dir.mkdir()
            invocation = temp / "update-invocation"

            git = bin_dir / "git"
            git.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                'if [[ "$1" == "fetch" ]]; then exit 0; fi\n'
                'if [[ "$1" == "rev-parse" && "$2" == "origin/main" ]]; then\n'
                f"  echo {release_sha}\n"
                "  exit 0\n"
                "fi\n"
                "exit 2\n",
                encoding="utf-8",
            )
            git.chmod(0o755)

            update_server = deploy_root / "update_server.sh"
            update_server.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                f'printf "%s\\n" "$*" > "{invocation}"\n',
                encoding="utf-8",
            )
            update_server.chmod(0o755)

            env = os.environ.copy()
            env["GROWATT_DEPLOY_ROOT"] = str(deploy_root)
            env["PATH"] = f"{bin_dir}:{env['PATH']}"

            result = subprocess.run(
                [str(ENTRYPOINT), release_sha],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                f"--no-notify --wait-for-clear 15 --release-sha {release_sha}\n",
                invocation.read_text(encoding="utf-8"),
            )

    def test_refuses_a_release_that_is_not_verified_main(self):
        requested_sha = "a" * 40
        verified_sha = "b" * 40

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            deploy_root = temp / "automation"
            bin_dir = temp / "bin"
            deploy_root.mkdir()
            (deploy_root / ".git").mkdir()
            bin_dir.mkdir()

            git = bin_dir / "git"
            git.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                'if [[ "$1" == "fetch" ]]; then exit 0; fi\n'
                'if [[ "$1" == "rev-parse" && "$2" == "origin/main" ]]; then\n'
                f"  echo {verified_sha}\n"
                "  exit 0\n"
                "fi\n"
                "exit 2\n",
                encoding="utf-8",
            )
            git.chmod(0o755)

            update_server = deploy_root / "update_server.sh"
            update_server.write_text(
                "#!/usr/bin/env bash\nexit 99\n",
                encoding="utf-8",
            )
            update_server.chmod(0o755)

            env = os.environ.copy()
            env["GROWATT_DEPLOY_ROOT"] = str(deploy_root)
            env["PATH"] = f"{bin_dir}:{env['PATH']}"

            result = subprocess.run(
                [str(ENTRYPOINT), requested_sha],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("does not match requested release", result.stderr)


if __name__ == "__main__":
    unittest.main()
