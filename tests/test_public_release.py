import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from development_governor.default_activation import default_enable
from development_governor.public_demo import run_demo


ROOT = Path(__file__).resolve().parents[1]


class PublicReleaseTests(unittest.TestCase):
    def test_package_metadata_exposes_stable_console_entrypoints(self):
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('name = "development-governor"', metadata)
        self.assertIn('version = "0.1.0b2"', metadata)
        self.assertIn('dependencies = []', metadata)
        self.assertIn('development-governor = "development_governor.cli:main"', metadata)
        self.assertIn('governor = "development_governor.cli:main"', metadata)
        self.assertIn('include = ["development_governor*"]', metadata)

    def test_module_entrypoint_has_public_program_name(self):
        completed = subprocess.run(
            [sys.executable, "-m", "development_governor", "--help"],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("usage: governor", completed.stdout)
        self.assertIn("demo", completed.stdout)

    def test_demo_proves_lease_scope_and_frozen_acceptance_without_model_use(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_demo(Path(directory))

        self.assertEqual(result["status"], "demo_passed")
        self.assertEqual(result["model_invocations"], 0)
        self.assertEqual(result["enrollment"], "enrolled")
        self.assertEqual(result["lease"], "active")
        self.assertEqual(result["allowed_mutation"], "allowed")
        self.assertEqual(result["blocked_mutation"], "denied_outside_scope")
        self.assertEqual(result["verification"], "verification_passed")
        self.assertEqual(result["closure"], "closed")
        self.assertEqual(result["acceptance_stdout"].strip(), "demo acceptance passed")

    def test_cli_demo_emits_one_machine_readable_result(self):
        completed = subprocess.run(
            [sys.executable, "-m", "development_governor", "demo"],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "demo_passed")
        self.assertEqual(result["model_invocations"], 0)

    def test_installed_package_can_enable_without_a_governor_git_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            result = default_enable(
                codex_home=codex_home,
                source_package=ROOT / "src" / "development_governor",
                governor_repo=None,
            )
            manifest = json.loads(
                Path(result["manifest_path"]).read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "enabled")
        self.assertIsNone(manifest["governor_project_identity"])


if __name__ == "__main__":
    unittest.main()
