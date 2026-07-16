import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from development_governor.project_entry import (
    ProjectEntryError,
    authorize_mutation,
    canonical_project_identity,
    close_task,
    enroll_project,
    prepare_task,
    project_status,
    start_task,
    verify_task,
)


class ProjectEntryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_root = self.root / "state"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Test User"],
            check=True,
        )
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "acceptance").mkdir()
        for name, product in (("verify.py", "app.py"), ("a.py", "a.py"), ("b.py", "b.py")):
            (self.repo / "acceptance" / name).write_text(
                "from pathlib import Path\n"
                f"assert Path('src/{product}').exists()\n",
                encoding="utf-8",
            )
        (self.repo / "README.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "baseline"], check=True)
        self.policy_path = self.root / "policy.json"
        self.write_json(self.policy_path, self.policy())

    def tearDown(self):
        self.tempdir.cleanup()

    def digest(self, relative):
        return hashlib.sha256((self.repo / relative).read_bytes()).hexdigest()

    def policy(self, **overrides):
        raw = {
            "schema_version": "development-governor-project-policy.v0",
            "repo_path": str(self.repo),
            "owner_authorization_ref": "owner:codex/enable-project",
            "allowed_paths": ["src/", "README.md"],
            "protected_paths": ["acceptance/"],
            "acceptance_definitions": [
                {
                    "acceptance_id": "verify",
                    "argv": [sys.executable, "acceptance/verify.py"],
                    "files": [
                        {"path": "acceptance/verify.py", "sha256": self.digest("acceptance/verify.py")}
                    ],
                },
                {
                    "acceptance_id": "lane-a",
                    "argv": [sys.executable, "acceptance/a.py"],
                    "files": [
                        {"path": "acceptance/a.py", "sha256": self.digest("acceptance/a.py")}
                    ],
                },
                {
                    "acceptance_id": "lane-b",
                    "argv": [sys.executable, "acceptance/b.py"],
                    "files": [
                        {"path": "acceptance/b.py", "sha256": self.digest("acceptance/b.py")}
                    ],
                },
            ],
            "limits": {
                "max_attempts": 3,
                "max_review_waves": 1,
                "max_elapsed_seconds": 3600,
                "lease_seconds": 1800,
                "max_parallel_agents": 3,
                "max_total_agents": 3,
            },
        }
        raw.update(overrides)
        return raw

    def capsule(self, **overrides):
        raw = {
            "schema_version": "development-governor-task-capsule.v1",
            "repo_path": str(self.repo),
            "owner_request_ref": "codex:user-turn/test-task",
            "result": "Deliver one working product slice",
            "constraints": ["Do not edit acceptance files"],
            "evidence_inputs": [
                {"path": "README.md", "sha256": self.digest("README.md")},
                {"path": "src/app.py", "sha256": self.digest("src/app.py")},
            ],
            "acceptance_ids": ["verify"],
            "deliverable_paths": ["src/"],
            "limits": {
                "max_attempts": 2,
                "max_review_waves": 0,
                "max_elapsed_seconds": 600,
                "lease_seconds": 300,
                "max_parallel_agents": 1,
                "max_total_agents": 1,
            },
            "lanes": [],
        }
        raw.update(overrides)
        return raw

    @staticmethod
    def write_json(path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def enroll(self):
        return enroll_project(self.policy_path, state_root=self.state_root)

    def prepare(self, **overrides):
        self.enroll()
        path = self.root / ("capsule-%d.json" % len(list(self.root.glob("capsule-*.json"))))
        self.write_json(path, self.capsule(**overrides))
        return prepare_task(path, state_root=self.state_root)

    def test_enrollment_is_idempotent_but_conflicting_policy_is_rejected(self):
        first = self.enroll()
        second = self.enroll()

        self.assertEqual(first["status"], "enrolled")
        self.assertEqual(second["status"], "already_enrolled")
        self.assertEqual(first["project_id"], second["project_id"])

        changed = self.policy()
        changed["limits"]["max_attempts"] = 2
        changed_path = self.root / "changed-policy.json"
        self.write_json(changed_path, changed)
        with self.assertRaisesRegex(ProjectEntryError, "conflicting enrolled policy"):
            enroll_project(changed_path, state_root=self.state_root)

    def test_linked_worktrees_share_the_git_common_dir_project_identity(self):
        linked = self.root / "linked"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-qb", "linked-test", str(linked)],
            check=True,
        )
        try:
            self.assertEqual(
                canonical_project_identity(self.repo)["project_id"],
                canonical_project_identity(linked)["project_id"],
            )
        finally:
            subprocess.run(
                ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(linked)],
                check=True,
            )

    def test_prepare_freezes_valid_serial_capsule_without_issuing_a_lease(self):
        self.enroll()
        capsule_path = self.root / "capsule.json"
        self.write_json(capsule_path, self.capsule())

        prepared = prepare_task(capsule_path, state_root=self.state_root)
        status = project_status(self.repo, state_root=self.state_root)

        self.assertEqual(prepared["status"], "prepared")
        self.assertTrue(Path(prepared["task_path"]).is_file())
        self.assertEqual(status["lease_status"], "none")
        self.assertEqual(len(prepared["task_hash"]), 64)

    def test_evidence_content_hash_is_bound_and_rechecked_before_start(self):
        self.enroll()
        raw = self.capsule(
            schema_version="development-governor-task-capsule.v1",
            evidence_inputs=[
                {"path": "README.md", "sha256": self.digest("README.md")}
            ],
        )
        first_path = self.root / "content-bound-task.json"
        self.write_json(first_path, raw)
        first = prepare_task(first_path, state_root=self.state_root)

        (self.repo / "README.md").write_text("changed evidence\n", encoding="utf-8")
        with self.assertRaisesRegex(ProjectEntryError, "evidence input hash mismatch"):
            start_task(first["task_hash"], state_root=self.state_root)

        raw["evidence_inputs"][0]["sha256"] = self.digest("README.md")
        second_path = self.root / "updated-content-bound-task.json"
        self.write_json(second_path, raw)
        second = prepare_task(second_path, state_root=self.state_root)

        self.assertNotEqual(first["task_hash"], second["task_hash"])

    def test_prepare_rejects_incomplete_unknown_and_over_budget_capsules(self):
        self.enroll()
        invalid_cases = [
            ("result", "", "result"),
            ("constraints", [], "constraints"),
            ("evidence_inputs", [], "evidence_inputs"),
            ("acceptance_ids", ["invented"], "unknown acceptance"),
            ("deliverable_paths", ["../outside"], "repository-relative"),
        ]
        for index, (field, value, message) in enumerate(invalid_cases):
            with self.subTest(field=field):
                raw = self.capsule(**{field: value})
                path = self.root / f"invalid-{index}.json"
                self.write_json(path, raw)
                with self.assertRaisesRegex(ProjectEntryError, message):
                    prepare_task(path, state_root=self.state_root)

        over = self.capsule()
        over["limits"]["max_attempts"] = 4
        path = self.root / "over-budget.json"
        self.write_json(path, over)
        with self.assertRaisesRegex(ProjectEntryError, "exceeds project policy"):
            prepare_task(path, state_root=self.state_root)

    def test_prepare_allows_independent_parallel_lanes_and_rejects_overlap(self):
        self.enroll()
        parallel = self.capsule(
            acceptance_ids=["lane-a", "lane-b"],
            deliverable_paths=["src/a.py", "src/b.py"],
            limits={
                "max_attempts": 2,
                "max_review_waves": 0,
                "max_elapsed_seconds": 600,
                "lease_seconds": 300,
                "max_parallel_agents": 2,
                "max_total_agents": 2,
            },
            lanes=[
                {"lane_id": "a", "deliverable_paths": ["src/a.py"], "acceptance_ids": ["lane-a"]},
                {"lane_id": "b", "deliverable_paths": ["src/b.py"], "acceptance_ids": ["lane-b"]},
            ],
        )
        path = self.root / "parallel.json"
        self.write_json(path, parallel)
        self.assertEqual(prepare_task(path, state_root=self.state_root)["status"], "prepared")

        parallel["lanes"][1]["deliverable_paths"] = ["src/a.py"]
        overlap = self.root / "overlap.json"
        self.write_json(overlap, parallel)
        with self.assertRaisesRegex(ProjectEntryError, "independent deliverable"):
            prepare_task(overlap, state_root=self.state_root)

        parallel["lanes"][1]["deliverable_paths"] = ["src/b.py"]
        parallel["lanes"][1]["acceptance_ids"] = ["lane-a"]
        reused = self.root / "reused.json"
        self.write_json(reused, parallel)
        with self.assertRaisesRegex(ProjectEntryError, "independent acceptance"):
            prepare_task(reused, state_root=self.state_root)

    def test_lease_lifecycle_denies_before_and_after_but_allows_while_active(self):
        prepared = self.prepare()

        before = authorize_mutation(self.repo, state_root=self.state_root, now=100.0)
        started = start_task(prepared["task_path"], state_root=self.state_root, now=100.0)
        repeated = start_task(prepared["task_hash"], state_root=self.state_root, now=101.0)
        active = authorize_mutation(self.repo, state_root=self.state_root, now=102.0)
        verified = verify_task(self.repo, state_root=self.state_root, now=103.0)
        closed = close_task(self.repo, state_root=self.state_root, now=104.0)
        after = authorize_mutation(self.repo, state_root=self.state_root, now=105.0)

        self.assertFalse(before["allowed"])
        self.assertEqual(started["status"], "active")
        self.assertEqual(repeated["status"], "already_active")
        self.assertTrue(active["allowed"])
        self.assertEqual(verified["status"], "verification_passed")
        self.assertEqual(closed["status"], "closed")
        self.assertFalse(after["allowed"])
        self.assertEqual(project_status(self.repo, state_root=self.state_root)["lease_status"], "none")

    def test_different_task_is_blocked_until_active_lease_closes(self):
        first = self.prepare(result="First slice")
        second = self.prepare(result="Second slice")
        start_task(first["task_path"], state_root=self.state_root, now=100.0)

        with self.assertRaisesRegex(ProjectEntryError, "another task owns the active project lease"):
            start_task(second["task_path"], state_root=self.state_root, now=101.0)

    def test_expired_lease_denies_and_restart_consumes_bounded_attempts(self):
        prepared = self.prepare(
            limits={
                "max_attempts": 2,
                "max_review_waves": 0,
                "max_elapsed_seconds": 600,
                "lease_seconds": 10,
                "max_parallel_agents": 1,
                "max_total_agents": 1,
            }
        )
        start_task(prepared["task_hash"], state_root=self.state_root, now=100.0)
        self.assertEqual(
            authorize_mutation(self.repo, state_root=self.state_root, now=111.0)["reason"],
            "lease_expired",
        )
        start_task(prepared["task_hash"], state_root=self.state_root, now=112.0)
        with self.assertRaisesRegex(ProjectEntryError, "attempt budget exhausted"):
            start_task(prepared["task_hash"], state_root=self.state_root, now=123.0)

    def test_verification_rejects_changed_acceptance_material(self):
        prepared = self.prepare()
        start_task(prepared["task_hash"], state_root=self.state_root, now=100.0)
        (self.repo / "acceptance" / "verify.py").write_text("raise SystemExit(0)\n", encoding="utf-8")

        with self.assertRaisesRegex(ProjectEntryError, "acceptance file hash mismatch"):
            verify_task(self.repo, state_root=self.state_root, now=101.0)

    def test_verification_runs_in_snapshot_without_mutating_source_repository(self):
        (self.repo / "acceptance" / "verify.py").write_text(
            "from pathlib import Path\n"
            "Path('src/app.py').write_text('MUTATED\\n', encoding='utf-8')\n"
            "Path('created-by-acceptance.txt').write_text('created\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        self.write_json(self.policy_path, self.policy())
        original = (self.repo / "src" / "app.py").read_bytes()
        prepared = self.prepare()
        start_task(prepared["task_hash"], state_root=self.state_root, now=100.0)

        result = verify_task(self.repo, state_root=self.state_root, now=101.0)

        self.assertEqual(result["status"], "verification_passed")
        self.assertEqual(result["results"][0]["execution_mode"], "isolated_snapshot")
        self.assertEqual((self.repo / "src" / "app.py").read_bytes(), original)
        self.assertFalse((self.repo / "created-by-acceptance.txt").exists())

    def test_unverified_task_requires_explicit_owner_abort_to_close(self):
        prepared = self.prepare()
        start_task(prepared["task_hash"], state_root=self.state_root, now=100.0)
        with self.assertRaisesRegex(ProjectEntryError, "verification has not passed"):
            close_task(self.repo, state_root=self.state_root, now=101.0)

        aborted = close_task(
            self.repo,
            state_root=self.state_root,
            owner_abort_reason="Owner cancelled this slice",
            now=102.0,
        )
        self.assertEqual(aborted["status"], "aborted")

    def test_start_rejects_task_reference_outside_external_state(self):
        prepared = self.prepare()
        forged_dir = self.root / prepared["task_hash"]
        forged_dir.mkdir()
        forged_task = forged_dir / "task.json"
        forged_task.write_bytes(Path(prepared["task_path"]).read_bytes())

        with self.assertRaisesRegex(ProjectEntryError, "outside Governor external state"):
            start_task(str(forged_task), state_root=self.state_root, now=100.0)

    def test_verified_closed_task_is_terminal_and_cannot_restart(self):
        prepared = self.prepare()
        start_task(prepared["task_hash"], state_root=self.state_root, now=100.0)
        verify_task(self.repo, state_root=self.state_root, now=101.0)
        close_task(self.repo, state_root=self.state_root, now=102.0)

        with self.assertRaisesRegex(ProjectEntryError, "closed task is terminal"):
            start_task(prepared["task_hash"], state_root=self.state_root, now=103.0)

    def test_owner_aborted_task_is_terminal_even_when_attempt_budget_remains(self):
        prepared = self.prepare()
        start_task(prepared["task_hash"], state_root=self.state_root, now=100.0)
        close_task(
            self.repo,
            state_root=self.state_root,
            owner_abort_reason="Owner ended this lineage",
            now=101.0,
        )

        with self.assertRaisesRegex(ProjectEntryError, "aborted task is terminal"):
            start_task(prepared["task_hash"], state_root=self.state_root, now=102.0)

    def test_task_elapsed_budget_is_cumulative_across_restarts(self):
        prepared = self.prepare(
            limits={
                "max_attempts": 3,
                "max_review_waves": 0,
                "max_elapsed_seconds": 20,
                "lease_seconds": 10,
                "max_parallel_agents": 1,
                "max_total_agents": 1,
            }
        )
        first = start_task(prepared["task_hash"], state_root=self.state_root, now=100.0)
        second = start_task(prepared["task_hash"], state_root=self.state_root, now=111.0)

        self.assertEqual(first["expires_at"], 110.0)
        self.assertEqual(second["expires_at"], 120.0)
        with self.assertRaisesRegex(ProjectEntryError, "elapsed budget exhausted"):
            start_task(prepared["task_hash"], state_root=self.state_root, now=121.0)

    def test_competing_processes_cannot_acquire_different_project_leases(self):
        first = self.prepare(result="First competing slice")
        second = self.prepare(result="Second competing slice")
        code = """
import json
import sys
from pathlib import Path
from development_governor.project_entry import ProjectEntryError, start_task
try:
    result = start_task(sys.argv[1], state_root=Path(sys.argv[2]), now=100.0)
except ProjectEntryError as error:
    result = {"status": "error", "error": str(error)}
print(json.dumps(result))
"""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code, item["task_hash"], str(self.state_root)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for item in (first, second)
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, msg=stderr)
            results.append(json.loads(stdout))

        self.assertEqual(sorted(item["status"] for item in results), ["active", "error"])
        self.assertIn(
            "another task owns the active project lease",
            next(item["error"] for item in results if item["status"] == "error"),
        )


if __name__ == "__main__":
    unittest.main()
