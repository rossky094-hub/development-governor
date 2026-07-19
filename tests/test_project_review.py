from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import development_governor
from development_governor.cli import main
from development_governor.lineage import lineage_ledger_path, lineage_ledger_sha256
from development_governor.project_review import (
    ProjectReviewContract,
    ProjectReviewError,
    ProjectReviewGovernor,
    build_project_review_command,
    materialize_review_context,
)


class ProjectReviewTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_root = self.root / "state"
        self.repo = self.root / "subject"
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
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "candidate.md").write_text(
            "# Candidate\n\nOne bounded capability.\n", encoding="utf-8"
        )
        (self.repo / "docs" / "goal.md").write_text(
            "# Goal\n\nShip the capability without changing Owner authority.\n",
            encoding="utf-8",
        )
        (self.repo / "docs" / "baseline.md").write_text(
            "# Accepted baseline\n\nGovernor controls execution, not semantics.\n",
            encoding="utf-8",
        )
        (self.repo / "docs" / "unlisted-noise.md").write_text(
            "# Unaccepted discussion\n", encoding="utf-8"
        )
        (self.repo / "docs" / "prior-review.json").write_text(
            '{"verdict":"targeted_revision_required"}\n', encoding="utf-8"
        )
        (self.repo / "docs" / "trusted.diff").write_text(
            "candidate v1 -> v2\n", encoding="utf-8"
        )
        (self.repo / "docs" / "dependency-map.json").write_text(
            '{"candidate":["goal"]}\n', encoding="utf-8"
        )
        (self.repo / "docs" / "prior-findings.json").write_text(
            '{"F-1":"open"}\n', encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "baseline"],
            check=True,
        )

        self.skill_root = self.root / "review-skill"
        (self.skill_root / "references").mkdir(parents=True)
        (self.skill_root / "templates").mkdir()
        (self.skill_root / "SKILL.md").write_text(
            "# Read-only spec reviewer\n", encoding="utf-8"
        )
        (self.skill_root / "references" / "gate-catalog.md").write_text(
            "# Gate catalog\n", encoding="utf-8"
        )
        (self.skill_root / "templates" / "spec-review-receipt.md").write_text(
            "# Receipt\n", encoding="utf-8"
        )

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def contract_mapping(self, **overrides):
        lineage_root_id = "project-aware-review"
        ledger = lineage_ledger_path(self.state_root, self.repo, lineage_root_id)
        data = {
            "schema_version": "development-governor.project-review-contract.v0",
            "objective": "Review the frozen candidate for Owner acceptance readiness.",
            "repo_path": str(self.repo),
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "max_elapsed_seconds": 30,
            "max_observed_total_tokens": None,
            "max_parallel_agents": 1,
            "max_total_agents": 1,
            "max_spawn_depth": 1,
            "review_mode": "full",
            "review_scope_id": "subject:spec-review",
            "owner_review_authorization_ref": "owner:review/candidate-v1",
            "owner_revision_ref": None,
            "candidate": {
                "path": "docs/candidate.md",
                "sha256": self.digest(self.repo / "docs" / "candidate.md"),
            },
            "context_inputs": [
                {
                    "role": "project_goal",
                    "path": "docs/goal.md",
                    "sha256": self.digest(self.repo / "docs" / "goal.md"),
                },
                {
                    "role": "parent_baseline",
                    "path": "docs/baseline.md",
                    "sha256": self.digest(self.repo / "docs" / "baseline.md"),
                },
            ],
            "reviewer_skill": {
                "root": str(self.skill_root),
                "files": [
                    {
                        "path": "SKILL.md",
                        "sha256": self.digest(self.skill_root / "SKILL.md"),
                    },
                    {
                        "path": "references/gate-catalog.md",
                        "sha256": self.digest(
                            self.skill_root / "references" / "gate-catalog.md"
                        ),
                    },
                    {
                        "path": "templates/spec-review-receipt.md",
                        "sha256": self.digest(
                            self.skill_root / "templates" / "spec-review-receipt.md"
                        ),
                    },
                ],
            },
            "acceptance_target_scope_ids": ["subject:implementation"],
            "review_scopes": [],
            "lineage": {
                "lineage_root_id": lineage_root_id,
                "ledger_sha256": lineage_ledger_sha256(ledger),
                "max_elapsed_seconds": 120,
                "max_invocations": 2,
                "max_review_waves": 1,
                "resume_from_reservation_id": None,
                "resume_session_id": None,
                "owner_review_credit": None,
            },
        }
        data.update(overrides)
        return data

    def fake_codex(self, body):
        path = self.root / (
            "fake-review-codex-%d" % len(list(self.root.glob("fake-review-codex-*")))
        )
        path.write_text(
            "#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8"
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_contract_binds_candidate_context_and_external_reviewer_skill(self):
        contract = ProjectReviewContract.from_mapping(self.contract_mapping())

        self.assertEqual(contract.candidate.path, "docs/candidate.md")
        self.assertEqual(
            [item.role for item in contract.context_inputs],
            ["project_goal", "parent_baseline"],
        )
        self.assertRegex(contract.review_identity_hash, r"^[0-9a-f]{64}$")
        self.assertRegex(contract.context_hash, r"^[0-9a-f]{64}$")
        self.assertRegex(contract.skill_bundle_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(contract.validate_material()["status"], "matched")

    def test_review_identity_binds_prompt_and_reviewer_execution_profile(self):
        baseline = ProjectReviewContract.from_mapping(self.contract_mapping())
        changed_objective = ProjectReviewContract.from_mapping(
            self.contract_mapping(objective="Apply a different semantic objective.")
        )
        changed_model = ProjectReviewContract.from_mapping(
            self.contract_mapping(model="gpt-5.6-terra")
        )

        self.assertNotEqual(
            baseline.review_identity_hash, changed_objective.review_identity_hash
        )
        self.assertNotEqual(
            baseline.review_identity_hash, changed_model.review_identity_hash
        )

    def test_materialized_context_and_command_are_read_only_and_context_bounded(self):
        contract = ProjectReviewContract.from_mapping(self.contract_mapping())
        output_dir = self.root / "review-run"

        workspace = materialize_review_context(
            contract, output_dir, review_batch_id="a" * 64
        )
        command = list(
            build_project_review_command(
                contract, workspace, codex_executable="codex"
            )
        )

        self.assertTrue(
            (workspace.context_root / "project" / "docs" / "candidate.md").is_file()
        )
        self.assertTrue(
            (workspace.context_root / "reviewer" / "SKILL.md").is_file()
        )
        self.assertFalse(
            (workspace.context_root / "project" / "docs" / "unlisted-noise.md").exists()
        )
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--disable", command)
        self.assertIn("multi_agent", command)
        exec_index = command.index("exec")
        self.assertLess(command.index("--sandbox"), exec_index)
        self.assertLess(command.index("--cd"), exec_index)
        self.assertGreater(command.index("--skip-git-repo-check"), exec_index)
        self.assertGreater(command.index("--ignore-user-config"), exec_index)
        self.assertGreater(command.index("--ignore-rules"), exec_index)
        self.assertEqual(command[command.index("--cd") + 1], str(workspace.context_root))
        self.assertEqual(
            command[command.index("--output-schema") + 1],
            str(workspace.output_schema_path),
        )
        self.assertEqual(
            command[command.index("--output-last-message") + 1],
            str(workspace.review_receipt_path),
        )
        prompt = command[-1]
        normalized_prompt = " ".join(prompt.split())
        self.assertIn(contract.review_identity_hash, prompt)
        self.assertIn("a" * 64, prompt)
        self.assertIn("reviewer/SKILL.md", prompt)
        self.assertIn(
            "Do not read files outside this materialized context",
            normalized_prompt,
        )

    def test_output_schema_is_closed_and_explicitly_typed_for_structured_outputs(self):
        contract = ProjectReviewContract.from_mapping(self.contract_mapping())
        workspace = materialize_review_context(
            contract, self.root / "typed-schema-review", review_batch_id="d" * 64
        )
        schema = json.loads(workspace.output_schema_path.read_text(encoding="utf-8"))

        def assert_closed_and_typed(node, path="$"):
            if not isinstance(node, dict):
                return
            self.assertNotIn("const", node, path + " uses unsupported const")
            if "enum" in node:
                self.assertIn("type", node, path + " must declare type")
            if node.get("type") == "object":
                self.assertFalse(
                    node.get("additionalProperties", True),
                    path + " must set additionalProperties=false",
                )
                properties = node.get("properties", {})
                self.assertEqual(
                    set(node.get("required", [])),
                    set(properties),
                    path + " must require every declared property",
                )
                for name, child in properties.items():
                    assert_closed_and_typed(child, path + "." + name)
            if node.get("type") == "array":
                self.assertIn("items", node, path + " must declare array items")
                assert_closed_and_typed(node["items"], path + "[]")

        assert_closed_and_typed(schema)
        properties = schema["properties"]
        self.assertEqual(properties["acceptance_target_scope_ids"]["type"], "array")
        self.assertEqual(
            properties["acceptance_target_scope_ids"]["items"],
            {"type": "string"},
        )
        self.assertEqual(
            properties["acceptance_target_scope_ids"]["minItems"],
            len(contract.acceptance_target_scope_ids),
        )
        self.assertEqual(
            properties["acceptance_target_scope_ids"]["maxItems"],
            len(contract.acceptance_target_scope_ids),
        )
        for name in (
            "batch_id",
            "owner_review_authorization_ref",
            "review_budget_reservation_ref",
            "review_mode",
            "verdict",
            "next_allowed_move",
        ):
            self.assertEqual(properties[name]["type"], "string")
        self.assertEqual(properties["candidate"]["properties"]["path"]["type"], "string")
        self.assertEqual(properties["candidate"]["properties"]["hash"]["type"], "string")

    def test_review_prompt_names_the_hash_bound_candidate_path(self):
        alternate = self.repo / "docs" / "release-spec.md"
        alternate.write_text("# Release Spec\n", encoding="utf-8")
        mapping = self.contract_mapping(
            candidate={
                "path": "docs/release-spec.md",
                "sha256": self.digest(alternate),
            }
        )
        contract = ProjectReviewContract.from_mapping(mapping)
        workspace = materialize_review_context(
            contract, self.root / "alternate-review", review_batch_id="c" * 64
        )

        prompt = build_project_review_command(contract, workspace)[-1]

        self.assertIn("project/docs/release-spec.md", prompt)
        self.assertNotIn("project/docs/candidate.md", prompt)

    def test_contract_requires_closed_project_aware_context_roles(self):
        missing_parent = self.contract_mapping()
        missing_parent["context_inputs"][1]["role"] = "contract"
        with self.assertRaisesRegex(ProjectReviewError, "parent_baseline"):
            ProjectReviewContract.from_mapping(missing_parent)

        conversation = self.contract_mapping()
        conversation["context_inputs"][0]["role"] = "conversation_history"
        with self.assertRaisesRegex(ProjectReviewError, "context input role"):
            ProjectReviewContract.from_mapping(conversation)

    def test_contract_requires_the_complete_external_reviewer_bundle(self):
        incomplete = self.contract_mapping()
        incomplete["reviewer_skill"]["files"].pop()

        with self.assertRaisesRegex(ProjectReviewError, "required reviewer skill files"):
            ProjectReviewContract.from_mapping(incomplete)

    def test_reviewer_skill_root_must_be_disjoint_from_governed_repository(self):
        shared_root = self.repo.parent
        (shared_root / "references").mkdir(exist_ok=True)
        (shared_root / "templates").mkdir(exist_ok=True)
        (shared_root / "SKILL.md").write_text("# Shared root\n", encoding="utf-8")
        (shared_root / "references" / "gate-catalog.md").write_text(
            "# Shared gates\n", encoding="utf-8"
        )
        (shared_root / "templates" / "spec-review-receipt.md").write_text(
            "# Shared receipt\n", encoding="utf-8"
        )
        mapping = self.contract_mapping()
        mapping["reviewer_skill"] = {
            "root": str(shared_root),
            "files": [
                {
                    "path": "SKILL.md",
                    "sha256": self.digest(shared_root / "SKILL.md"),
                },
                {
                    "path": "references/gate-catalog.md",
                    "sha256": self.digest(
                        shared_root / "references" / "gate-catalog.md"
                    ),
                },
                {
                    "path": "templates/spec-review-receipt.md",
                    "sha256": self.digest(
                        shared_root / "templates" / "spec-review-receipt.md"
                    ),
                },
            ],
        }

        with self.assertRaisesRegex(ProjectReviewError, "disjoint"):
            ProjectReviewContract.from_mapping(mapping)

    def test_contract_closes_reasoning_effort_and_default_review_wave_budget(self):
        unsupported_effort = self.contract_mapping(reasoning_effort="invented")
        with self.assertRaisesRegex(ProjectReviewError, "reasoning_effort"):
            ProjectReviewContract.from_mapping(unsupported_effort)

        excessive_budget = self.contract_mapping()
        excessive_budget["lineage"]["max_review_waves"] = 2
        with self.assertRaisesRegex(ProjectReviewError, "one review wave"):
            ProjectReviewContract.from_mapping(excessive_budget)

    def test_review_scopes_are_zero_or_independently_identified_parallel_lanes(self):
        one_scope = self.contract_mapping(
            review_scopes=[
                {
                    "scope_id": "semantic",
                    "objective": "Attack semantic closure.",
                    "acceptance_id": "semantic-receipt",
                }
            ]
        )
        with self.assertRaisesRegex(ProjectReviewError, "zero or at least two"):
            ProjectReviewContract.from_mapping(one_scope)

        parallel = self.contract_mapping(
            max_parallel_agents=2,
            max_total_agents=2,
            review_scopes=[
                {
                    "scope_id": "semantic",
                    "objective": "Attack semantic closure.",
                    "acceptance_id": "semantic-receipt",
                },
                {
                    "scope_id": "decision",
                    "objective": "Attack authority and route closure.",
                    "acceptance_id": "decision-receipt",
                },
            ],
        )
        contract = ProjectReviewContract.from_mapping(parallel)
        workspace = materialize_review_context(
            contract, self.root / "parallel-review", review_batch_id="b" * 64
        )
        command = list(build_project_review_command(contract, workspace))
        normalized_prompt = " ".join(command[-1].split())

        self.assertIn("--enable", command)
        self.assertIn('"acceptance_id":"semantic-receipt"', normalized_prompt)
        self.assertIn("fresh-context read-only worker", normalized_prompt)
        self.assertIn("no descendants", normalized_prompt)
        self.assertIn("maximum active logical agents: 2", normalized_prompt)
        self.assertIn("maximum total logical agents: 2", normalized_prompt)
        self.assertIn("maximum spawn depth: 1", normalized_prompt)

    def test_incremental_review_requires_hash_bound_impact_material(self):
        incomplete = self.contract_mapping(
            review_mode="incremental",
            owner_revision_ref="owner:revision/candidate-v2",
        )
        with self.assertRaisesRegex(ProjectReviewError, "incremental review requires"):
            ProjectReviewContract.from_mapping(incomplete)

        complete = self.contract_mapping(
            review_mode="incremental",
            owner_revision_ref="owner:revision/candidate-v2",
        )
        complete["context_inputs"].extend(
            [
                {
                    "role": "prior_review_receipt",
                    "path": "docs/prior-review.json",
                    "sha256": self.digest(self.repo / "docs" / "prior-review.json"),
                },
                {
                    "role": "trusted_diff",
                    "path": "docs/trusted.diff",
                    "sha256": self.digest(self.repo / "docs" / "trusted.diff"),
                },
                {
                    "role": "dependency_map",
                    "path": "docs/dependency-map.json",
                    "sha256": self.digest(self.repo / "docs" / "dependency-map.json"),
                },
                {
                    "role": "prior_finding_map",
                    "path": "docs/prior-findings.json",
                    "sha256": self.digest(self.repo / "docs" / "prior-findings.json"),
                },
            ]
        )

        contract = ProjectReviewContract.from_mapping(complete)

        self.assertEqual(contract.review_mode, "incremental")
        self.assertEqual(contract.owner_revision_ref, "owner:revision/candidate-v2")

    def test_governor_runs_reviewer_and_records_valid_hash_bound_receipt(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            context = Path(args[args.index("--cd") + 1])
            output = Path(args[args.index("--output-last-message") + 1])
            manifest = json.loads((context / "REVIEW-MANIFEST.json").read_text())
            receipt = {
                "candidate": {
                    "path": manifest["candidate"]["path"],
                    "hash": manifest["candidate"]["sha256"],
                },
                "batch_id": manifest["review_batch_id"],
                "acceptance_target_scope_ids": manifest["acceptance_target_scope_ids"],
                "owner_review_authorization_ref": manifest["owner_review_authorization_ref"],
                "review_budget_reservation_ref": manifest["review_batch_id"],
                "review_mode": manifest["review_mode"],
                "counterexample_summary": {"counterexample_succeeded": 0},
                "findings": [],
                "independent_scopes": [],
                "verdict": "accepted_for_owner_review",
                "next_allowed_move": "owner_decision",
                "can_claim": ["one review receipt completed"],
                "cannot_claim": ["Owner acceptance", "implementation authorization"],
            }
            output.write_text(json.dumps(receipt), encoding="utf-8")
            print(json.dumps({"type": "thread.started", "thread_id": "review-session-1"}), flush=True)
            """
        )
        contract = ProjectReviewContract.from_mapping(
            self.contract_mapping(max_observed_total_tokens=90)
        )

        receipt = ProjectReviewGovernor(
            str(fake), state_root=self.state_root
        ).run(contract, self.root / "complete-review")

        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["review"]["verdict"], "accepted_for_owner_review")
        self.assertEqual(receipt["session_id"], "review-session-1")
        self.assertEqual(receipt["token_usage"], {"status": "unavailable"})
        self.assertEqual(receipt["lineage"]["review_waves_spent"], 1)
        self.assertIn("serial_multi_agent_disabled", receipt["hard_controls"])
        self.assertNotIn("observed_token_cap", receipt["hard_controls"])
        self.assertEqual(
            receipt["soft_controls"], ["observed_token_cap_unavailable"]
        )
        self.assertEqual(
            receipt["authority_boundary"],
            {
                "governor_semantic_verdict": False,
                "owner_acceptance": "pending",
                "implementation_authorized": False,
            },
        )
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(status, "")

    def test_terminal_only_usage_overrun_preserves_completed_review(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            import sys
            import time

            args = sys.argv[1:]
            context = Path(args[args.index("--cd") + 1])
            output = Path(args[args.index("--output-last-message") + 1])
            manifest = json.loads((context / "REVIEW-MANIFEST.json").read_text())
            review = {
                "candidate": {
                    "path": manifest["candidate"]["path"],
                    "hash": manifest["candidate"]["sha256"],
                },
                "batch_id": manifest["review_batch_id"],
                "acceptance_target_scope_ids": manifest["acceptance_target_scope_ids"],
                "owner_review_authorization_ref": manifest["owner_review_authorization_ref"],
                "review_budget_reservation_ref": manifest["review_batch_id"],
                "review_mode": manifest["review_mode"],
                "counterexample_summary": {
                    "applicable_counterexamples": 1,
                    "counterexample_blocked": 1,
                    "counterexample_succeeded": 0,
                    "not_run": 0,
                    "not_applicable": 0,
                },
                "findings": [],
                "independent_scopes": [],
                "verdict": "accepted_for_owner_review",
                "next_allowed_move": "owner_decision",
                "can_claim": ["review output completed"],
                "cannot_claim": ["Owner acceptance"],
            }
            encoded_review = json.dumps(review, separators=(",", ":"))
            output.write_text(encoded_review, encoding="utf-8")
            print(json.dumps({"type": "thread.started", "thread_id": "terminal-review-session"}), flush=True)
            print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": encoded_review}}), flush=True)
            print(json.dumps({
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 90,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                },
            }), flush=True)
            time.sleep(0.05)
            """
        )
        contract = ProjectReviewContract.from_mapping(
            self.contract_mapping(max_observed_total_tokens=90)
        )

        receipt = ProjectReviewGovernor(
            str(fake), state_root=self.state_root
        ).run(contract, self.root / "terminal-review")

        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["review"]["verdict"], "accepted_for_owner_review")
        self.assertEqual(
            receipt["artifact_status"],
            {"review_receipt_present": True, "turn_completed": True},
        )
        self.assertEqual(
            receipt["review_validation_status"],
            {"status": "valid", "error": None},
        )
        self.assertEqual(receipt["budget_status"]["status"], "overrun")
        self.assertEqual(
            receipt["budget_status"]["token_observability_mode"],
            "terminal_only",
        )
        self.assertEqual(
            receipt["budget_status"]["enforcement"],
            "terminal_accounting_only",
        )
        self.assertNotIn("observed_token_cap", receipt["hard_controls"])
        self.assertIn("terminal_token_accounting", receipt["soft_controls"])

    def test_recovery_validates_legacy_final_output_without_model_rerun(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            context = Path(args[args.index("--cd") + 1])
            output = Path(args[args.index("--output-last-message") + 1])
            manifest = json.loads((context / "REVIEW-MANIFEST.json").read_text())
            review = {
                "candidate": {
                    "path": manifest["candidate"]["path"],
                    "hash": manifest["candidate"]["sha256"],
                },
                "batch_id": manifest["review_batch_id"],
                "acceptance_target_scope_ids": manifest["acceptance_target_scope_ids"],
                "owner_review_authorization_ref": manifest["owner_review_authorization_ref"],
                "review_budget_reservation_ref": manifest["review_batch_id"],
                "review_mode": manifest["review_mode"],
                "counterexample_summary": {
                    "applicable_counterexamples": 1,
                    "counterexample_blocked": 0,
                    "counterexample_succeeded": 1,
                    "not_run": 0,
                    "not_applicable": 0,
                },
                "findings": [{
                    "finding_id": "F-RECOVERY",
                    "severity": "important",
                    "title": "Recoverable finding",
                    "location": "docs/candidate.md:1",
                    "trigger": "A counterexample succeeds.",
                    "consequence": "The candidate cannot pass.",
                    "minimum_repair": "Close the counterexample.",
                }],
                "independent_scopes": [],
                "verdict": "major_revision_required",
                "next_allowed_move": "owner_decision",
                "can_claim": ["one finding exists"],
                "cannot_claim": ["Owner acceptance"],
            }
            encoded_review = json.dumps(review, separators=(",", ":"))
            output.write_text(encoded_review, encoding="utf-8")
            print(json.dumps({"type": "thread.started", "thread_id": "legacy-review-session"}), flush=True)
            print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": encoded_review}}), flush=True)
            print(json.dumps({
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 90,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                },
            }), flush=True)
            """
        )
        contract = ProjectReviewContract.from_mapping(
            self.contract_mapping(max_observed_total_tokens=90)
        )
        output_dir = self.root / "legacy-review"
        completed = ProjectReviewGovernor(
            str(fake), state_root=self.state_root
        ).run(contract, output_dir)
        self.assertEqual(completed["status"], "complete")

        terminal_path = output_dir / "terminal-receipt.json"
        legacy_terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
        legacy_terminal.update(
            {
                "status": "interrupted",
                "reason": "observed_token_budget_exhausted",
                "exit_code": -15,
                "review": None,
                "review_receipt_error": None,
                "review_validation_status": {
                    "status": "not_validated",
                    "error": None,
                },
            }
        )
        legacy_terminal["lineage"]["settlement"][
            "terminal_status"
        ] = "interrupted"
        terminal_path.write_text(
            json.dumps(legacy_terminal, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        original_terminal_bytes = terminal_path.read_bytes()
        ledger_path = Path(legacy_terminal["lineage"]["ledger_path"])
        original_ledger_bytes = ledger_path.read_bytes()

        recovered = development_governor.recover_project_review_receipt(
            contract, output_dir
        )

        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(
            recovered["reason"],
            "valid_review_preceded_terminal_budget_observation",
        )
        self.assertEqual(
            recovered["review"]["verdict"], "major_revision_required"
        )
        self.assertEqual(
            recovered["review_validation_status"], {"status": "valid", "error": None}
        )
        self.assertEqual(terminal_path.read_bytes(), original_terminal_bytes)
        self.assertEqual(ledger_path.read_bytes(), original_ledger_bytes)
        recovery_path = output_dir / "review-recovery-receipt.json"
        first_recovery_bytes = recovery_path.read_bytes()
        second = development_governor.recover_project_review_receipt(
            contract, output_dir
        )
        self.assertEqual(second, recovered)
        self.assertEqual(recovery_path.read_bytes(), first_recovery_bytes)

    def test_parallel_review_cannot_accept_without_declared_scope_receipts(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            context = Path(args[args.index("--cd") + 1])
            output = Path(args[args.index("--output-last-message") + 1])
            manifest = json.loads((context / "REVIEW-MANIFEST.json").read_text())
            output.write_text(json.dumps({
                "candidate": {"path": manifest["candidate"]["path"], "hash": manifest["candidate"]["sha256"]},
                "batch_id": manifest["review_batch_id"],
                "acceptance_target_scope_ids": manifest["acceptance_target_scope_ids"],
                "owner_review_authorization_ref": manifest["owner_review_authorization_ref"],
                "review_budget_reservation_ref": manifest["review_batch_id"],
                "review_mode": manifest["review_mode"],
                "counterexample_summary": {},
                "findings": [],
                "independent_scopes": [],
                "verdict": "accepted_for_owner_review",
                "next_allowed_move": "owner_decision",
                "can_claim": [],
                "cannot_claim": ["Owner acceptance"],
            }), encoding="utf-8")
            print(json.dumps({"type": "thread.started", "thread_id": "parallel-review-session"}), flush=True)
            """
        )
        mapping = self.contract_mapping(
            max_parallel_agents=2,
            max_total_agents=2,
            review_scopes=[
                {
                    "scope_id": "semantic",
                    "objective": "Attack semantic closure.",
                    "acceptance_id": "semantic-receipt",
                },
                {
                    "scope_id": "decision",
                    "objective": "Attack authority closure.",
                    "acceptance_id": "decision-receipt",
                },
            ],
        )
        contract = ProjectReviewContract.from_mapping(mapping)

        receipt = ProjectReviewGovernor(
            str(fake), state_root=self.state_root
        ).run(contract, self.root / "incomplete-parallel-review")

        self.assertEqual(receipt["status"], "stopped")
        self.assertEqual(receipt["reason"], "review_receipt_invalid")
        self.assertIn("declared review scopes", receipt["review_receipt_error"])
        self.assertIn(
            "native_worker_spawn_limits", receipt["soft_controls"]
        )

    def test_reviewer_cannot_redefine_its_frozen_context_or_schema(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            context = Path(args[args.index("--cd") + 1])
            output = Path(args[args.index("--output-last-message") + 1])
            manifest_path = context / "REVIEW-MANIFEST.json"
            manifest = json.loads(manifest_path.read_text())
            manifest_path.chmod(0o644)
            manifest_path.write_text("{}", encoding="utf-8")
            output.write_text(json.dumps({
                "candidate": {"path": manifest["candidate"]["path"], "hash": manifest["candidate"]["sha256"]},
                "batch_id": manifest["review_batch_id"],
                "acceptance_target_scope_ids": manifest["acceptance_target_scope_ids"],
                "owner_review_authorization_ref": manifest["owner_review_authorization_ref"],
                "review_budget_reservation_ref": manifest["review_batch_id"],
                "review_mode": manifest["review_mode"],
                "counterexample_summary": {},
                "findings": [],
                "independent_scopes": [],
                "verdict": "accepted_for_owner_review",
                "next_allowed_move": "owner_decision",
                "can_claim": [],
                "cannot_claim": ["Owner acceptance"],
            }), encoding="utf-8")
            print(json.dumps({"type": "thread.started", "thread_id": "tamper-session"}), flush=True)
            """
        )
        contract = ProjectReviewContract.from_mapping(self.contract_mapping())

        receipt = ProjectReviewGovernor(
            str(fake), state_root=self.state_root
        ).run(contract, self.root / "tampered-review")

        self.assertEqual(receipt["status"], "stopped")
        self.assertEqual(receipt["reason"], "review_context_changed")
        self.assertIsNone(receipt["review"])

    def test_interrupted_reviewer_resumes_same_batch_without_second_review_wave(self):
        first = self.fake_codex(
            """
            import json
            print(json.dumps({"type": "thread.started", "thread_id": "resume-review-session"}), flush=True)
            raise SystemExit(1)
            """
        )
        initial_contract = ProjectReviewContract.from_mapping(self.contract_mapping())
        output_dir = self.root / "resumable-review"

        interrupted = ProjectReviewGovernor(
            str(first), state_root=self.state_root
        ).run(initial_contract, output_dir)

        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["lineage"]["review_waves_spent"], 1)
        prior_reservation = interrupted["lineage"]["reservation"]["reservation_id"]
        ledger = lineage_ledger_path(
            self.state_root, self.repo, "project-aware-review"
        )
        resume_mapping = self.contract_mapping()
        resume_mapping["lineage"]["ledger_sha256"] = lineage_ledger_sha256(ledger)
        resume_mapping["lineage"]["resume_from_reservation_id"] = prior_reservation
        resume_mapping["lineage"]["resume_session_id"] = "resume-review-session"
        resume_contract = ProjectReviewContract.from_mapping(resume_mapping)
        argv_capture = self.root / "resume-argv.json"
        second = self.fake_codex(
            f"""
            import json
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            Path({str(argv_capture)!r}).write_text(json.dumps(args), encoding="utf-8")
            output = Path(args[args.index("--output-last-message") + 1])
            context = output.parent / "review-context"
            manifest = json.loads((context / "REVIEW-MANIFEST.json").read_text())
            output.write_text(json.dumps({{
                "candidate": {{"path": manifest["candidate"]["path"], "hash": manifest["candidate"]["sha256"]}},
                "batch_id": manifest["review_batch_id"],
                "acceptance_target_scope_ids": manifest["acceptance_target_scope_ids"],
                "owner_review_authorization_ref": manifest["owner_review_authorization_ref"],
                "review_budget_reservation_ref": manifest["review_batch_id"],
                "review_mode": manifest["review_mode"],
                "counterexample_summary": {{}},
                "findings": [],
                "independent_scopes": [],
                "verdict": "accepted_for_owner_review",
                "next_allowed_move": "owner_decision",
                "can_claim": [],
                "cannot_claim": ["Owner acceptance"],
            }}), encoding="utf-8")
            print(json.dumps({{"type": "thread.started", "thread_id": "resume-review-session"}}), flush=True)
            """
        )

        completed = ProjectReviewGovernor(
            str(second), state_root=self.state_root
        ).run(resume_contract, output_dir)

        resumed_argv = json.loads(argv_capture.read_text(encoding="utf-8"))
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["review_batch_id"], interrupted["review_batch_id"])
        self.assertEqual(completed["lineage"]["review_waves_spent"], 1)
        self.assertEqual(completed["lineage"]["invocations_spent"], 2)
        self.assertIn("resume", resumed_argv)
        self.assertIn("resume-review-session", resumed_argv)

    def test_reviewer_repository_mutation_is_terminal_not_resumable(self):
        fake = self.fake_codex(
            f"""
            import json
            from pathlib import Path
            import time

            print(json.dumps({{"type": "thread.started", "thread_id": "mutating-review-session"}}), flush=True)
            Path({str(self.repo / "docs" / "candidate.md")!r}).write_text("mutated", encoding="utf-8")
            time.sleep(2)
            """
        )
        contract = ProjectReviewContract.from_mapping(self.contract_mapping())

        receipt = ProjectReviewGovernor(
            str(fake), state_root=self.state_root
        ).run(contract, self.root / "mutating-review")

        self.assertEqual(receipt["status"], "stopped")
        self.assertEqual(receipt["reason"], "reviewer_modified_governed_repository")
        self.assertIn("docs/candidate.md", receipt["repository"]["changed_paths"])

    def test_workspace_preflight_failure_does_not_reserve_review_budget(self):
        contract = ProjectReviewContract.from_mapping(self.contract_mapping())
        output_dir = self.root / "already-exists"
        output_dir.mkdir()
        ledger = lineage_ledger_path(
            self.state_root, self.repo, "project-aware-review"
        )
        empty_ledger_hash = lineage_ledger_sha256(ledger)

        with self.assertRaisesRegex(ProjectReviewError, "already exists"):
            ProjectReviewGovernor(
                str(self.root / "unused-codex"), state_root=self.state_root
            ).run(contract, output_dir)

        self.assertEqual(lineage_ledger_sha256(ledger), empty_ledger_hash)
        self.assertFalse(ledger.exists())

    def test_supervision_failure_settles_started_review_reservation(self):
        fake = self.fake_codex(
            """
            import time
            time.sleep(30)
            """
        )
        contract = ProjectReviewContract.from_mapping(self.contract_mapping())

        with patch(
            "development_governor.project_review.supervise_root_process",
            side_effect=RuntimeError("probe failed"),
        ):
            with self.assertRaisesRegex(ProjectReviewError, "supervision failed"):
                ProjectReviewGovernor(
                    str(fake), state_root=self.state_root
                ).run(contract, self.root / "supervision-failure")

        ledger = lineage_ledger_path(
            self.state_root, self.repo, "project-aware-review"
        )
        events = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
        ]
        settlement = events[-1]
        self.assertEqual(settlement["event"], "lineage_settled")
        self.assertEqual(settlement["terminal_status"], "runner_error")
        self.assertTrue(settlement["model_started"])
        self.assertEqual(settlement["charged_invocations"], 1)
        self.assertEqual(settlement["charged_review_waves"], 1)

    def test_cli_exposes_project_aware_review_runner(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            context = Path(args[args.index("--cd") + 1])
            output = Path(args[args.index("--output-last-message") + 1])
            manifest = json.loads((context / "REVIEW-MANIFEST.json").read_text())
            output.write_text(json.dumps({
                "candidate": {"path": manifest["candidate"]["path"], "hash": manifest["candidate"]["sha256"]},
                "batch_id": manifest["review_batch_id"],
                "acceptance_target_scope_ids": manifest["acceptance_target_scope_ids"],
                "owner_review_authorization_ref": manifest["owner_review_authorization_ref"],
                "review_budget_reservation_ref": manifest["review_batch_id"],
                "review_mode": manifest["review_mode"],
                "counterexample_summary": {},
                "findings": [],
                "independent_scopes": [],
                "verdict": "accepted_for_owner_review",
                "next_allowed_move": "owner_decision",
                "can_claim": [],
                "cannot_claim": ["Owner acceptance"],
            }), encoding="utf-8")
            print(json.dumps({"type": "thread.started", "thread_id": "cli-review-session"}), flush=True)
            """
        )
        contract_path = self.root / "project-review-contract.json"
        contract_path.write_text(
            json.dumps(self.contract_mapping()), encoding="utf-8"
        )
        stdout = io.StringIO()

        with patch("development_governor.cli.DEFAULT_STATE_ROOT", self.state_root):
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "review-spec",
                        str(contract_path),
                        "--output-dir",
                        str(self.root / "cli-review"),
                        "--codex",
                        str(fake),
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["review"]["verdict"], "accepted_for_owner_review")

    def test_cli_exposes_zero_model_review_recovery(self):
        contract_path = self.root / "recovery-contract.json"
        contract_path.write_text(
            json.dumps(self.contract_mapping(max_observed_total_tokens=90)),
            encoding="utf-8",
        )
        expected = {
            "schema_version": (
                "development-governor.project-review-recovery-receipt.v0"
            ),
            "status": "recovered",
        }
        stdout = io.StringIO()

        with patch(
            "development_governor.cli.recover_project_review_receipt",
            create=True,
            return_value=expected,
        ) as recover:
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "recover-review",
                        str(contract_path),
                        "--output-dir",
                        str(self.root / "historical-review"),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), expected)
        recovered_contract, recovered_output = recover.call_args.args
        self.assertIsInstance(recovered_contract, ProjectReviewContract)
        self.assertEqual(recovered_output, self.root / "historical-review")

    def test_project_review_api_is_publicly_importable(self):
        self.assertIs(
            development_governor.ProjectReviewContract,
            ProjectReviewContract,
        )
        self.assertIs(
            development_governor.ProjectReviewGovernor,
            ProjectReviewGovernor,
        )


if __name__ == "__main__":
    unittest.main()
