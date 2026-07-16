import json
from pathlib import Path
import unittest

from development_governor.hook_guard import evaluate_hook_event
from development_governor.project_entry import canonical_project_identity, start_task
from tests import test_project_entry as project_fixture


class HookGuardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = project_fixture.ProjectEntryTests(
            "test_enrollment_is_idempotent_but_conflicting_policy_is_rejected"
        )
        self.fixture.setUp()
        self.root = self.fixture.root
        self.state_root = self.fixture.state_root
        self.repo = self.fixture.repo

    def tearDown(self):
        self.fixture.tearDown()

    def enroll(self):
        return self.fixture.enroll()

    def prepare(self, **overrides):
        return self.fixture.prepare(**overrides)

    def event(self, tool_name, command, *, cwd=None):
        return {
            "cwd": str(cwd or self.repo),
            "hook_event_name": "PreToolUse",
            "model": "gpt-5.6-sol",
            "permission_mode": "default",
            "session_id": "session-test",
            "tool_input": {"command": command},
            "tool_name": tool_name,
            "tool_use_id": "tool-test",
            "transcript_path": str(self.root / "transcript.jsonl"),
            "turn_id": "turn-test",
        }

    def raw_event(self, tool_name, tool_input, *, cwd=None):
        event = self.event(tool_name, "placeholder", cwd=cwd)
        event["tool_input"] = tool_input
        return event

    def test_read_only_and_non_git_commands_are_allowed_without_a_lease(self):
        self.enroll()
        for command in ("pwd", "ls -la", "rg -n VALUE src", "git status --short", "git diff --check"):
            with self.subTest(command=command):
                self.assertEqual(
                    evaluate_hook_event(self.event("Bash", command), state_root=self.state_root, now=100.0),
                    {},
                )

        non_git = self.root / "notes"
        non_git.mkdir()
        self.assertEqual(
            evaluate_hook_event(self.event("apply_patch", "patch", cwd=non_git), state_root=self.state_root),
            {},
        )

    def test_mutations_are_denied_until_matching_lease_is_active(self):
        self.enroll()
        denied = evaluate_hook_event(
            self.event("apply_patch", "*** Begin Patch\n*** Update File: src/app.py"),
            state_root=self.state_root,
            now=100.0,
        )
        self.assertEqual(
            denied["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )

        prepared = self.prepare()
        start_task(prepared["task_hash"], state_root=self.state_root, now=100.0)
        self.assertEqual(
            evaluate_hook_event(
                self.event("apply_patch", "*** Begin Patch\n*** Update File: src/app.py"),
                state_root=self.state_root,
                now=101.0,
            ),
            {},
        )

    def test_active_lease_enforces_apply_patch_paths_and_protected_acceptance(self):
        prepared = self.prepare()
        start_task(prepared["task_hash"], state_root=self.state_root, now=100.0)

        for path in ("acceptance/verify.py", ".github/workflows/ci.yml", "README.md"):
            with self.subTest(path=path):
                denied = evaluate_hook_event(
                    self.event(
                        "apply_patch",
                        f"*** Begin Patch\n*** Update File: {path}\n@@\n-old\n+new\n*** End Patch",
                    ),
                    state_root=self.state_root,
                    now=101.0,
                )
                self.assertEqual(
                    denied["hookSpecificOutput"]["permissionDecision"], "deny"
                )

        allowed = evaluate_hook_event(
            self.event(
                "apply_patch",
                "*** Begin Patch\n*** Update File: src/app.py\n@@\n-old\n+new\n*** End Patch",
            ),
            state_root=self.state_root,
            now=101.0,
        )
        self.assertEqual(allowed, {})

    def test_unified_exec_without_command_field_is_not_fail_open(self):
        self.enroll()
        source = (
            "const patch = `*** Begin Patch\\n*** Update File: src/app.py\\n*** End Patch`;"
            " text(await tools.apply_patch(patch));"
        )
        denied = evaluate_hook_event(
            self.raw_event("functions.exec", {"input": source}),
            state_root=self.state_root,
            now=100.0,
        )

        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("systemMessage", denied)

    def test_unified_exec_cannot_hide_out_of_scope_patch_behind_active_lease(self):
        prepared = self.prepare()
        start_task(prepared["task_hash"], state_root=self.state_root, now=100.0)
        source = (
            "const patch = `*** Begin Patch\n*** Update File: acceptance/verify.py\n*** End Patch`;"
            " text(await tools.apply_patch(patch));"
        )
        denied = evaluate_hook_event(
            self.raw_event("functions.exec", {"input": source}),
            state_root=self.state_root,
            now=101.0,
        )

        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_unregistered_project_mutation_is_denied_with_enrollment_route(self):
        denied = evaluate_hook_event(
            self.event("Bash", "touch src/new.py"),
            state_root=self.state_root,
            now=100.0,
        )
        self.assertIn("enroll", denied["hookSpecificOutput"]["permissionDecisionReason"])

    def test_unknown_or_indirect_shell_is_mutation_capable(self):
        self.enroll()
        for command in (
            "python3 -c 'open(\"src/x\",\"w\").write(\"x\")'",
            "sed -i '' s/a/b/ src/app.py",
            "echo x > src/x",
            "git commit -am change",
            "npm install package",
            "custom-script --maybe-write",
        ):
            with self.subTest(command=command):
                result = evaluate_hook_event(self.event("Bash", command), state_root=self.state_root)
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_git_branch_writes_and_lookalike_governor_commands_are_not_read_only(self):
        self.enroll()
        for command in (
            "git branch new-work",
            "git branch -D old-work",
            "git diff --output=outside.patch",
            "git worktree add /tmp/new-worktree",
            "governor prepare /tmp/capsule.json",
        ):
            with self.subTest(command=command):
                result = evaluate_hook_event(
                    self.event("Bash", command), state_root=self.state_root
                )
                self.assertEqual(
                    result["hookSpecificOutput"]["permissionDecision"], "deny"
                )

    def test_expired_lease_is_denied_and_malformed_input_fails_open(self):
        prepared = self.prepare(
            limits={
                "max_attempts": 1,
                "max_review_waves": 0,
                "max_elapsed_seconds": 600,
                "lease_seconds": 10,
                "max_parallel_agents": 1,
                "max_total_agents": 1,
            }
        )
        start_task(prepared["task_hash"], state_root=self.state_root, now=100.0)
        expired = evaluate_hook_event(self.event("apply_patch", "patch"), state_root=self.state_root, now=111.0)
        self.assertIn("expired", expired["hookSpecificOutput"]["permissionDecisionReason"])

        malformed = evaluate_hook_event({"tool_name": "apply_patch"}, state_root=self.state_root)
        self.assertIn("systemMessage", malformed)
        self.assertNotIn("hookSpecificOutput", malformed)

    def test_governor_repository_is_excluded_from_recursive_guarding(self):
        activation = self.state_root / "activation" / "current.json"
        activation.parent.mkdir(parents=True)
        activation.write_text(
            json.dumps(
                {
                    "governor_project_identity": dict(
                        canonical_project_identity(self.repo)
                    )
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            evaluate_hook_event(
                self.event("apply_patch", "*** Begin Patch\n*** Update File: src/app.py"),
                state_root=self.state_root,
            ),
            {},
        )

    def test_only_manifest_bound_launcher_can_bootstrap_without_a_lease(self):
        self.enroll()
        launcher = self.root / "stable" / "governor"
        activation = self.state_root / "activation" / "current.json"
        activation.parent.mkdir(parents=True)
        activation.write_text(
            json.dumps(
                {
                    "governor_project_identity": {"project_id": "not-this-project"},
                    "runtime": {"launcher_path": str(launcher)},
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            evaluate_hook_event(
                self.event(
                    "Bash",
                    f"{launcher} prepare --json-base64 eyJyZXN1bHQiOiAieCJ9",
                ),
                state_root=self.state_root,
            ),
            {},
        )

    def test_active_lease_allows_test_commands_that_do_not_name_write_targets(self):
        prepared = self.prepare()
        start_task(prepared["task_hash"], state_root=self.state_root, now=100.0)

        result = evaluate_hook_event(
            self.event(
                "Bash",
                "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_example -q",
            ),
            state_root=self.state_root,
            now=101.0,
        )

        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
