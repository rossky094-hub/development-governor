import hashlib
import base64
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


class DefaultEntryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.project_root = Path(__file__).resolve().parents[1]
        self.script = self.project_root / "scripts" / "run_development_governor.py"
        self.state_root = self.root / "state"
        self.codex_home = self.root / ".codex"
        self.repo = self.root / "subject"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test User"], check=True)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "acceptance").mkdir()
        (self.repo / "acceptance" / "verify.py").write_text(
            "from pathlib import Path\nassert Path('src/app.py').is_file()\n",
            encoding="utf-8",
        )
        (self.repo / "README.md").write_text("subject\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "baseline"], check=True)
        self.marker = self.root / "codex-launched"
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            "#!/bin/sh\n" + f"touch {str(self.marker)!r}\n",
            encoding="utf-8",
        )
        fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)
        self.env = dict(os.environ)
        self.env.update(
            {
                "PYTHONPATH": str(self.project_root / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "DEVELOPMENT_GOVERNOR_STATE_ROOT": str(self.state_root),
                "PATH": str(fake_bin) + os.pathsep + self.env.get("PATH", ""),
            }
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def call(self, *args, input_text=None, expected=0):
        completed = subprocess.run(
            [sys.executable, str(self.script), *args],
            cwd=self.project_root,
            env=self.env,
            input=input_text,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, expected, msg=completed.stdout + "\n" + completed.stderr)
        return json.loads(completed.stdout)

    def policy(self):
        acceptance = self.repo / "acceptance" / "verify.py"
        return {
            "schema_version": "development-governor-project-policy.v0",
            "repo_path": str(self.repo),
            "owner_authorization_ref": "owner:integration/accepted",
            "allowed_paths": ["src/", "README.md"],
            "protected_paths": ["acceptance/"],
            "acceptance_definitions": [
                {
                    "acceptance_id": "verify",
                    "argv": [sys.executable, "acceptance/verify.py"],
                    "files": [
                        {"path": "acceptance/verify.py", "sha256": hashlib.sha256(acceptance.read_bytes()).hexdigest()}
                    ],
                }
            ],
            "limits": {
                "max_attempts": 2,
                "max_review_waves": 1,
                "max_elapsed_seconds": 600,
                "lease_seconds": 300,
                "max_parallel_agents": 2,
                "max_total_agents": 2,
            },
        }

    def capsule(self):
        return {
            "schema_version": "development-governor-task-capsule.v0",
            "repo_path": str(self.repo),
            "owner_request_ref": "codex:user-turn/integration",
            "result": "Keep one executable product slice working",
            "constraints": ["Acceptance files are frozen"],
            "evidence_inputs": ["README.md", "src/app.py"],
            "acceptance_ids": ["verify"],
            "deliverable_paths": ["src/"],
            "limits": {
                "max_attempts": 1,
                "max_review_waves": 0,
                "max_elapsed_seconds": 300,
                "lease_seconds": 120,
                "max_parallel_agents": 1,
                "max_total_agents": 1,
            },
            "lanes": [],
        }

    def hook_event(self):
        return {
            "cwd": str(self.repo),
            "hook_event_name": "PreToolUse",
            "model": "gpt-5.6-sol",
            "permission_mode": "default",
            "session_id": "integration-session",
            "tool_input": {"command": "*** Begin Patch\n*** Update File: src/app.py"},
            "tool_name": "apply_patch",
            "tool_use_id": "integration-tool",
            "transcript_path": str(self.root / "transcript"),
            "turn_id": "integration-turn",
        }

    def test_complete_route_changes_guard_state_without_launching_codex(self):
        policy_encoded = base64.urlsafe_b64encode(
            json.dumps(self.policy()).encode("utf-8")
        ).decode("ascii")
        capsule_encoded = base64.urlsafe_b64encode(
            json.dumps(self.capsule()).encode("utf-8")
        ).decode("ascii")
        self.assertEqual(
            self.call("enroll", "--json-base64", policy_encoded)["status"],
            "enrolled",
        )
        prepared = self.call("prepare", "--json-base64", capsule_encoded)

        denied_before = self.call("hook-guard", input_text=json.dumps(self.hook_event()))
        self.assertEqual(denied_before["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.call("start", prepared["task_hash"])["status"], "active")
        self.assertEqual(self.call("hook-guard", input_text=json.dumps(self.hook_event())), {})
        self.assertEqual(self.call("verify", "--repo", str(self.repo))["status"], "verification_passed")
        self.assertEqual(self.call("close", "--repo", str(self.repo))["status"], "closed")
        denied_after = self.call("hook-guard", input_text=json.dumps(self.hook_event()))
        self.assertEqual(denied_after["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertFalse(self.marker.exists())

    def test_cli_enables_and_disables_temporary_global_entry_without_model_use(self):
        enabled = self.call("default-enable", "--codex-home", str(self.codex_home))
        self.assertEqual(enabled["status"], "enabled")
        self.assertTrue((self.codex_home / "AGENTS.md").is_file())
        self.assertTrue((self.codex_home / "hooks.json").is_file())
        governor_event = self.hook_event()
        governor_event["cwd"] = str(self.project_root)
        installed_hook = subprocess.run(
            [enabled["hook_command"]],
            input=json.dumps(governor_event),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(installed_hook.stdout), {})
        subject_hook = subprocess.run(
            [enabled["hook_command"]],
            input=json.dumps(self.hook_event()),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            json.loads(subject_hook.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertFalse(self.marker.exists())

        disabled = self.call("default-disable", "--codex-home", str(self.codex_home))
        self.assertEqual(disabled["status"], "disabled")
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
