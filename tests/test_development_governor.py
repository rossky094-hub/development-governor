import json
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from dataclasses import asdict
from unittest.mock import patch

from development_governor.runner import (
    ContractError,
    DevelopmentGovernor,
    RunContract,
    build_codex_command,
    build_coordinator_prompt,
    hash_path_set,
)
from development_governor.lineage import (
    lineage_ledger_path,
    lineage_ledger_sha256,
)
from development_governor.rerun import (
    build_evaluation_request,
    ledger_sha256,
    reserve_evaluation,
)


class DevelopmentGovernorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_root = self.root / "governor-state"
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
        (self.repo / "README.md").write_text("baseline\n", encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "existing.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "acceptance").mkdir()
        (self.repo / "acceptance" / "verify.py").write_text(
            "from pathlib import Path\nassert Path('src').is_dir()\n",
            encoding="utf-8",
        )
        (self.repo / "acceptance" / "unit-a.py").write_text(
            "from pathlib import Path\nassert Path('src/a.py').is_file()\n",
            encoding="utf-8",
        )
        (self.repo / "acceptance" / "unit-b.py").write_text(
            "from pathlib import Path\nassert Path('src/b.py').is_file()\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "baseline"],
            check=True,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def contract(self, **overrides):
        primary_mode = overrides.get("primary_mode", "product")
        data = {
            "objective": "Implement one bounded product slice",
            "repo_path": str(self.repo),
            "model": "gpt-5.6-sol",
            "primary_mode": "product",
            "reasoning_effort": "medium",
            "max_elapsed_seconds": 5,
            "product_change_deadline_seconds": 4,
            "max_observed_total_tokens": None,
            "max_parallel_agents": 1,
            "max_total_agents": 1,
            "max_spawn_depth": 1,
            "review_credits": 0,
            "allowed_paths": ["src/", "README.md"],
            "product_paths": ["src/"],
            "verification_command": [
                sys.executable,
                "acceptance/verify.py",
            ],
            "acceptance_files": self.acceptance_manifest("acceptance/verify.py"),
            "parallel_units": [],
            "lineage": self.lineage_mapping(),
            "stage_control": self.stage_control_mapping(
                owner_acceptance_ref=(
                    "owner:thread/accepted-product-slice-v1"
                    if primary_mode == "product"
                    else None
                )
            ),
        }
        data.update(overrides)
        return RunContract.from_mapping(data)

    def stage_control_mapping(self, **overrides):
        data = {
            "current_scope_id": "development-governor:implementation",
            "authorization_scopes": [
                {
                    "scope_id": "development-governor:implementation",
                    "capability_id": "development-governor",
                    "stage_id": "implementation",
                    "status": "authorized",
                    "authorized_product_paths": ["src/"],
                }
            ],
            "blockers": [],
            "gates": [],
            "owner_acceptance_ref": "owner:thread/accepted-product-slice-v1",
            "owner_revision_ref": None,
            "max_review_batches_without_owner": 1,
            "automatic_post_review_revisions": 0,
        }
        data.update(overrides)
        return data

    def lineage_mapping(self, lineage_root_id="test-lineage", **overrides):
        ledger = lineage_ledger_path(
            self.state_root, self.repo, lineage_root_id
        )
        data = {
            "lineage_root_id": lineage_root_id,
            "ledger_sha256": lineage_ledger_sha256(ledger),
            "max_elapsed_seconds": 60,
            "max_invocations": 3,
            "max_review_waves": 1,
            "resume_from_reservation_id": None,
            "resume_session_id": None,
            "owner_review_credit": None,
        }
        data.update(overrides)
        return data

    def governor(self, executable):
        return DevelopmentGovernor(str(executable), state_root=self.state_root)

    def acceptance_manifest(self, *paths):
        return [
            {
                "path": path,
                "sha256": hashlib.sha256((self.repo / path).read_bytes()).hexdigest(),
            }
            for path in paths
        ]

    def parallel_contract(self, **overrides):
        data = {
            "max_parallel_agents": 2,
            "max_total_agents": 2,
            "acceptance_files": self.acceptance_manifest(
                "acceptance/verify.py",
                "acceptance/unit-a.py",
                "acceptance/unit-b.py",
            ),
            "parallel_units": [
                {
                    "kind": "product",
                    "task_id": "unit-a",
                    "objective": "Deliver src/a.py",
                    "deliverable_paths": ["src/a.py"],
                    "acceptance_command": [sys.executable, "acceptance/unit-a.py"],
                    "acceptance_files": ["acceptance/unit-a.py"],
                },
                {
                    "kind": "product",
                    "task_id": "unit-b",
                    "objective": "Deliver src/b.py",
                    "deliverable_paths": ["src/b.py"],
                    "acceptance_command": [sys.executable, "acceptance/unit-b.py"],
                    "acceptance_files": ["acceptance/unit-b.py"],
                },
            ],
        }
        data.update(overrides)
        return self.contract(**data)

    def evaluation_mapping(self, ledger, **overrides):
        data = {
            "phase": "red",
            "catalog_scope_ids": ["scenario-a", "scenario-b"],
            "scope_ids": ["scenario-a", "scenario-b"],
            "impacted_scope_ids": [],
            "ledger_path": str(ledger),
            "ledger_sha256": ledger_sha256(ledger),
            "rerun_credit": None,
        }
        data.update(overrides)
        return data

    def fake_codex(self, body):
        path = self.root / ("fake-codex-%d" % len(list(self.root.glob("fake-codex-*"))))
        path.write_text(
            "#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8"
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_serial_contract_hard_disables_multi_agent_and_probe_spawns(self):
        contract = self.contract()

        command = build_codex_command(contract, codex_executable="codex")
        prompt = build_coordinator_prompt(contract)
        normalized = " ".join(prompt.split())

        self.assertIn("multi_agent", command)
        self.assertIn("--disable", command)
        self.assertNotIn("--enable", command)
        self.assertIn("workspace-write", command)
        self.assertEqual(command.count("exec"), 1)
        self.assertIn("execution mode: serial_root_only", prompt)
        self.assertIn("Do not spawn workers, reviewers, or read-only probes", normalized)
        self.assertEqual(contract.lineage.lineage_root_id, "test-lineage")

    def test_resume_contract_builds_governed_exec_resume_command(self):
        lineage = self.lineage_mapping(
            resume_from_reservation_id="e" * 64,
            resume_session_id="session-to-resume",
        )
        contract = self.contract(lineage=lineage)

        command = list(build_codex_command(contract, codex_executable="codex"))

        self.assertIn("resume", command)
        self.assertIn("session-to-resume", command)
        self.assertLess(command.index("exec"), command.index("resume"))
        self.assertLess(command.index("resume"), command.index("session-to-resume"))

    def test_primary_mode_and_reasoning_effort_are_closed_and_required(self):
        contract = self.contract()

        self.assertEqual(contract.primary_mode, "product")
        self.assertEqual(contract.reasoning_effort, "medium")
        with self.assertRaisesRegex(ContractError, "primary_mode"):
            self.contract(primary_mode=None)
        with self.assertRaisesRegex(ContractError, "primary_mode"):
            self.contract(primary_mode="repair")
        with self.assertRaisesRegex(ContractError, "reasoning_effort"):
            self.contract(reasoning_effort="automatic")
        with self.assertRaisesRegex(ContractError, "unsupported contract fields"):
            self.contract(secondary_mode="research")

    def test_stage_control_is_required_and_owner_acceptance_selects_product_mode(self):
        with self.assertRaisesRegex(ContractError, "stage_control"):
            self.contract(stage_control=None)
        with self.assertRaisesRegex(ContractError, "Owner-accepted slice"):
            self.contract(
                primary_mode="governance",
                product_change_deadline_seconds=None,
                review_credits=1,
                stage_control=self.stage_control_mapping(),
            )

    def test_current_scope_paths_must_equal_product_paths(self):
        mismatched = self.stage_control_mapping(
            authorization_scopes=[
                {
                    "scope_id": "unrelated:publication",
                    "capability_id": "unrelated",
                    "stage_id": "publication",
                    "status": "authorized",
                    "authorized_product_paths": ["other/"],
                }
            ],
            current_scope_id="unrelated:publication",
        )

        with self.assertRaisesRegex(ContractError, "authorized_product_paths"):
            self.contract(stage_control=mismatched)

    def test_blocked_current_scope_is_not_runnable_but_safe_scope_is_preserved(self):
        blocked = self.stage_control_mapping(
            authorization_scopes=[
                {
                    "scope_id": "development-governor:implementation",
                    "capability_id": "development-governor",
                    "stage_id": "implementation",
                    "status": "blocked",
                    "authorized_product_paths": ["src/"],
                },
                {
                    "scope_id": "docs:implementation",
                    "capability_id": "docs",
                    "stage_id": "implementation",
                    "status": "authorized",
                    "authorized_product_paths": ["README.md"],
                },
            ],
            blockers=[
                {
                    "blocker_id": "fixture-missing",
                    "affected_scope_ids": [
                        "development-governor:implementation"
                    ],
                    "required_predicate": "fixture exists",
                    "failure_if_ignored": "implementation would be invalid",
                    "earliest_stage": "implementation",
                    "safe_work_remaining_scope_ids": ["docs:implementation"],
                }
            ],
        )
        contract = self.contract(stage_control=blocked)

        self.assertEqual(
            contract.stage_control.decision.action,
            "route_to_safe_scope",
        )
        with self.assertRaisesRegex(ContractError, "route_to_safe_scope"):
            build_codex_command(contract)

    def test_blocked_current_scope_stops_before_model_and_lineage_reservation(self):
        marker = self.root / "blocked-scope-launched.txt"
        fake = self.fake_codex(
            f"""
            from pathlib import Path
            Path({str(marker)!r}).write_text("launched")
            """
        )
        blocked = self.stage_control_mapping(
            authorization_scopes=[
                {
                    "scope_id": "development-governor:implementation",
                    "capability_id": "development-governor",
                    "stage_id": "implementation",
                    "status": "blocked",
                    "authorized_product_paths": ["src/"],
                }
            ],
            blockers=[
                {
                    "blocker_id": "owner-decision-required",
                    "affected_scope_ids": [
                        "development-governor:implementation"
                    ],
                    "required_predicate": "Owner activates the current scope",
                    "failure_if_ignored": "unauthorized work would start",
                    "earliest_stage": "implementation",
                    "safe_work_remaining_scope_ids": [],
                }
            ],
        )
        contract = self.contract(stage_control=blocked)
        ledger = lineage_ledger_path(
            self.state_root,
            self.repo,
            contract.lineage.lineage_root_id,
        )

        with self.assertRaisesRegex(
            ContractError, "terminal_owner_decision_required"
        ):
            self.governor(fake).run(contract, self.root / "blocked-scope-run")

        self.assertFalse(marker.exists())
        self.assertEqual(lineage_ledger_sha256(ledger), hashlib.sha256(b"").hexdigest())

    def test_review_budget_defaults_to_one_batch_without_owner(self):
        with self.assertRaisesRegex(ContractError, "one review batch"):
            self.contract(
                lineage=self.lineage_mapping(max_review_waves=2)
            )

    def test_command_pins_reasoning_effort_in_strict_config(self):
        command = list(build_codex_command(self.contract(), codex_executable="codex"))

        self.assertIn("--strict-config", command)
        self.assertIn("-c", command)
        self.assertIn('model_reasoning_effort="medium"', command)
        self.assertLess(command.index('model_reasoning_effort="medium"'), command.index("exec"))

    def test_two_declared_independent_units_enable_native_multi_agent(self):
        contract = self.parallel_contract()

        command = build_codex_command(contract, codex_executable="codex")
        prompt = build_coordinator_prompt(contract)
        normalized = " ".join(prompt.split())

        self.assertIn("--enable", command)
        self.assertNotIn("--disable", command)
        self.assertIn("execution mode: declared_parallel_units", prompt)
        self.assertIn('"task_id":"unit-a"', prompt)
        self.assertIn('"task_id":"unit-b"', prompt)
        self.assertIn(
            "Unlisted workers, reviewers, and read-only probes are forbidden",
            normalized,
        )
        self.assertEqual([unit.kind for unit in contract.parallel_units], ["product", "product"])
        self.assertEqual(contract.review_wave_cost, 0)

    def test_parallel_unit_kind_is_closed_and_reviewers_share_one_wave(self):
        missing_kind = [asdict(unit) for unit in self.parallel_contract().parallel_units]
        for item in missing_kind:
            item.pop("kind")
        with self.assertRaisesRegex(ContractError, "parallel unit missing fields: kind"):
            self.parallel_contract(parallel_units=missing_kind)

        invalid_kind = [asdict(unit) for unit in self.parallel_contract().parallel_units]
        invalid_kind[0]["kind"] = "probe"
        with self.assertRaisesRegex(ContractError, "parallel unit kind"):
            self.parallel_contract(parallel_units=invalid_kind)

        review_units = [asdict(unit) for unit in self.parallel_contract().parallel_units]
        for item in review_units:
            item["kind"] = "review"
        review_contract = self.parallel_contract(
            primary_mode="governance",
            product_change_deadline_seconds=None,
            review_credits=1,
            parallel_units=review_units,
        )
        self.assertEqual(review_contract.review_wave_cost, 1)
        self.assertEqual(len(review_contract.review_units), 2)

        with self.assertRaisesRegex(ContractError, "review_credits"):
            self.parallel_contract(
                primary_mode="governance",
                product_change_deadline_seconds=None,
                review_credits=0,
                parallel_units=review_units,
            )

    def test_prompt_declares_dynamic_parallel_control(self):
        prompt = build_coordinator_prompt(self.parallel_contract())
        normalized = " ".join(prompt.split())

        self.assertIn("maximum active logical agents: 2", prompt)
        self.assertIn("maximum total logical agents: 2", prompt)
        self.assertIn("maximum spawn depth: 1", prompt)
        self.assertIn("review wave credits requested: 0", prompt)
        self.assertIn("review wave cost: 0", prompt)
        self.assertIn("one worker for each declared parallel unit", normalized)
        self.assertIn("Do not restart the full parallel wave", prompt)
        self.assertIn("karpathy-guidelines", prompt)

    def test_prompt_uses_scoped_authorization_and_forbids_automatic_re_review(self):
        proposed_gate = self.stage_control_mapping(
            gates=[
                {
                    "gate_id": "publication-proof",
                    "affected_scope_ids": [
                        "development-governor:implementation"
                    ],
                    "status": "proposed_nonblocking",
                    "owner_decision_ref": None,
                }
            ]
        )
        prompt = build_coordinator_prompt(
            self.contract(stage_control=proposed_gate)
        )
        normalized = " ".join(prompt.split())

        self.assertIn(
            "current authorized scope: development-governor:implementation",
            normalized,
        )
        self.assertIn("proposed Gates are nonblocking", normalized)
        self.assertIn("Automatic post-review revision or re-review is forbidden", normalized)

    def test_prompt_requires_isolated_self_contained_worker_context(self):
        prompt = build_coordinator_prompt(self.parallel_contract())
        normalized = " ".join(prompt.split())

        self.assertIn('fork_turns="none"', prompt)
        self.assertIn("never omit it and never use `all`", normalized)
        self.assertIn("self-contained task envelope", normalized)
        self.assertIn(
            "task ID, objective, dependencies, repository path, role, read/write scope, "
            "allowed paths, expected evidence, and stop conditions",
            normalized,
        )

    def test_evaluation_prompt_forbids_unchanged_external_eval_loops(self):
        ledger = self.root / "evaluation-prompt-ledger.jsonl"
        contract = self.contract(
            evaluation=self.evaluation_mapping(ledger)
        )

        prompt = build_coordinator_prompt(contract)
        normalized = " ".join(prompt.split())

        self.assertIn("external evaluation phase: red", prompt)
        self.assertIn("scenario-a, scenario-b", prompt)
        self.assertIn("RED identity ignores the product tree", normalized)
        self.assertIn(
            "Do not execute or repeat the frozen external acceptance yourself",
            normalized,
        )

    def test_invalid_parallel_limit_and_escaping_path_are_rejected(self):
        with self.assertRaisesRegex(ContractError, "max_parallel_agents"):
            self.contract(max_parallel_agents=0)

        with self.assertRaisesRegex(ContractError, "repository-relative"):
            self.contract(allowed_paths=["../outside"])

    def test_parallel_units_require_disjoint_deliverables_and_acceptance(self):
        with self.assertRaisesRegex(
            ContractError, "parallel contracts require at least 2 active agents"
        ):
            self.parallel_contract(max_parallel_agents=1)

        one_unit = self.parallel_contract().parallel_units[:1]
        with self.assertRaisesRegex(ContractError, "zero or at least two"):
            self.contract(
                max_parallel_agents=1,
                max_total_agents=1,
                acceptance_files=self.acceptance_manifest(
                    "acceptance/verify.py", "acceptance/unit-a.py"
                ),
                parallel_units=[asdict(one_unit[0])],
            )

        overlapping = [asdict(unit) for unit in self.parallel_contract().parallel_units]
        overlapping[1]["deliverable_paths"] = ["src/a.py/generated.py"]
        with self.assertRaisesRegex(ContractError, "deliverable paths must be disjoint"):
            self.parallel_contract(parallel_units=overlapping)

        shared_acceptance = [
            asdict(unit) for unit in self.parallel_contract().parallel_units
        ]
        shared_acceptance[1]["acceptance_files"] = ["acceptance/unit-a.py"]
        shared_acceptance[1]["acceptance_command"] = [
            sys.executable,
            "acceptance/unit-a.py",
            "--unit-b",
        ]
        with self.assertRaisesRegex(ContractError, "acceptance files must be disjoint"):
            self.parallel_contract(parallel_units=shared_acceptance)

        outside_product = [
            asdict(unit) for unit in self.parallel_contract().parallel_units
        ]
        outside_product[0]["deliverable_paths"] = ["README.md"]
        with self.assertRaisesRegex(
            ContractError, "product unit deliverable paths"
        ):
            self.parallel_contract(parallel_units=outside_product)

    def test_acceptance_hash_mismatch_blocks_before_model_launch(self):
        marker = self.root / "should-not-launch-acceptance.txt"
        fake = self.fake_codex(
            f"""
            from pathlib import Path
            Path({str(marker)!r}).write_text("launched")
            """
        )
        contract = self.contract()
        (self.repo / "acceptance" / "verify.py").write_text(
            "raise SystemExit('tampered')\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(ContractError, "acceptance material hash mismatch"):
            self.governor(fake).run(contract, self.root / "preflight-mismatch")

        self.assertFalse(marker.exists())

    def test_acceptance_mutation_during_run_blocks_without_executing_it(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            print(json.dumps({"type": "thread.started", "thread_id": "session-tamper"}), flush=True)
            Path("src/feature.py").write_text("ENABLED = True\\n", encoding="utf-8")
            Path("acceptance/verify.py").write_text("raise SystemExit('self-approved')\\n", encoding="utf-8")
            """
        )

        receipt = self.governor(fake).run(
            self.contract(), self.root / "postflight-mismatch"
        )

        self.assertEqual(receipt["status"], "stopped")
        self.assertEqual(receipt["reason"], "acceptance_material_changed")
        self.assertEqual(receipt["acceptance"]["postflight_status"], "mismatch")
        self.assertIsNone(receipt["verification"])
        self.assertFalse(receipt["product_evidence"])

    def test_acceptance_executes_verified_capsule_copy(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            print(json.dumps({"type": "thread.started", "thread_id": "session-capsule"}), flush=True)
            Path("src/capsule.py").write_text("CAPSULE = True\\n", encoding="utf-8")
            """
        )
        output_dir = self.root / "capsule-run"

        receipt = self.governor(fake).run(self.contract(), output_dir)

        capsule_entrypoint = (
            output_dir / "acceptance-capsule" / "acceptance" / "verify.py"
        ).resolve()
        self.assertEqual(receipt["status"], "complete")
        self.assertTrue(capsule_entrypoint.is_file())
        self.assertIn(str(capsule_entrypoint), receipt["verification"]["command"])
        self.assertNotIn(
            "acceptance/verify.py", receipt["verification"]["command"]
        )

    def test_acceptance_commands_must_reference_their_frozen_entrypoints(self):
        with self.assertRaisesRegex(
            ContractError, "verification_command must reference a frozen acceptance file"
        ):
            self.contract(verification_command=[sys.executable, "-c", "raise SystemExit(0)"])

        units = [asdict(unit) for unit in self.parallel_contract().parallel_units]
        units[1]["acceptance_command"] = [sys.executable, "-c", "raise SystemExit(0)"]
        with self.assertRaisesRegex(
            ContractError, "parallel unit acceptance_command must reference its acceptance_files"
        ):
            self.parallel_contract(parallel_units=units)

    def test_each_parallel_unit_runs_its_own_frozen_acceptance(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            print(json.dumps({"type": "thread.started", "thread_id": "session-parallel"}), flush=True)
            Path("src/a.py").write_text("A = True\\n", encoding="utf-8")
            """
        )

        receipt = self.governor(fake).run(
            self.parallel_contract(), self.root / "parallel-acceptance"
        )

        self.assertEqual(receipt["status"], "stopped")
        self.assertEqual(
            receipt["reason"],
            "post_acceptance_product_evidence_fuse_tripped",
        )
        self.assertEqual(
            [item["task_id"] for item in receipt["parallel_unit_acceptance"]],
            ["unit-a", "unit-b"],
        )
        self.assertEqual(
            [item["exit_code"] for item in receipt["parallel_unit_acceptance"]],
            [0, 1],
        )
        self.assertFalse(receipt["product_evidence"])

    def test_product_change_and_verification_yield_complete(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            print(json.dumps({"type": "thread.started", "thread_id": "session-product"}), flush=True)
            Path("src/feature.py").write_text("ENABLED = True\\n", encoding="utf-8")
            """
        )

        receipt = self.governor(fake).run(
            self.contract(), self.root / "product-run"
        )

        self.assertEqual(receipt["status"], "complete")
        self.assertTrue(receipt["product_evidence"])
        self.assertEqual(receipt["session_id"], "session-product")
        self.assertIn("src/feature.py", receipt["changed_paths"])
        self.assertEqual(receipt["invocation_count"], 1)
        self.assertEqual(receipt["repository"]["path"], str(self.repo.resolve()))
        self.assertEqual(receipt["repository"]["product_paths"], ["src/"])
        self.assertEqual(
            receipt["repository"]["final_product_tree_hash"],
            hash_path_set(self.repo, ("src/",)),
        )
        self.assertRegex(receipt["acceptance"]["interface_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(receipt["acceptance"]["test_bundle_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(receipt["acceptance"]["postflight_status"], "matched")
        self.assertEqual(
            receipt["stage_control"]["claim_scope"],
            {
                "capability_id": "development-governor",
                "stage_id": "implementation",
            },
        )
        self.assertEqual(
            receipt["stage_control"]["product_evidence_fuse"],
            "satisfied",
        )

    def test_governance_mode_can_complete_without_claiming_product_progress(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            print(json.dumps({"type": "thread.started", "thread_id": "session-governance"}), flush=True)
            Path("README.md").write_text("review evidence\\n", encoding="utf-8")
            """
        )
        receipt = self.governor(fake).run(
            self.contract(
                primary_mode="governance",
                product_change_deadline_seconds=None,
                review_credits=1,
            ),
            self.root / "governance-run",
        )

        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["reason"], "non_product_mode_verification_closed")
        self.assertFalse(receipt["product_evidence"])
        self.assertTrue(receipt["mode_evidence"])
        self.assertIn("review_wave_admission_gate", receipt["hard_controls"])
        self.assertNotIn("review_credits", receipt["soft_controls"])
        self.assertEqual(
            receipt["review_budget"],
            {
                "requested": 1,
                "review_unit_count": 0,
                "waves_required": 1,
                "waves_spent": 1,
                "remaining": 0,
            },
        )

    def test_receipt_observes_codex_token_count_event(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            print(json.dumps({"type": "thread.started", "thread_id": "session-token-event"}), flush=True)
            print(json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 120,
                            "cached_input_tokens": 30,
                            "output_tokens": 40,
                            "reasoning_output_tokens": 10,
                            "total_tokens": 160,
                        }
                    },
                },
            }), flush=True)
            Path("src/token_event.py").write_text("OBSERVED = True\\n", encoding="utf-8")
            """
        )

        receipt = self.governor(fake).run(
            self.contract(), self.root / "token-event-run"
        )

        self.assertEqual(
            receipt["token_usage"],
            {
                "status": "observed",
                "input_tokens": 120,
                "cached_input_tokens": 30,
                "output_tokens": 40,
                "reasoning_output_tokens": 10,
                "total_tokens": 160,
            },
        )
        self.assertEqual(receipt["status"], "complete")
        self.assertTrue(receipt["product_evidence"])
        self.assertEqual(receipt["invocation_count"], 1)

    def test_receipt_derives_total_for_current_codex_usage_object(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            print(json.dumps({"type": "thread.started", "thread_id": "session-direct-usage"}), flush=True)
            print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "cached_input_tokens": 6, "output_tokens": 3, "reasoning_output_tokens": 2}}), flush=True)
            Path("src/direct_usage.py").write_text("OBSERVED = True\\n", encoding="utf-8")
            """
        )

        receipt = self.governor(fake).run(
            self.contract(), self.root / "direct-usage-run"
        )

        self.assertEqual(
            receipt["token_usage"],
            {
                "status": "observed",
                "input_tokens": 7,
                "cached_input_tokens": 6,
                "output_tokens": 3,
                "reasoning_output_tokens": 2,
                "total_tokens": 10,
            },
        )
        self.assertEqual(receipt["status"], "complete")
        self.assertTrue(receipt["product_evidence"])

    def test_committed_product_change_is_still_product_evidence(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            import subprocess
            print(json.dumps({"type": "thread.started", "thread_id": "session-commit"}), flush=True)
            Path("src/committed.py").write_text("COMMITTED = True\\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/committed.py"], check=True)
            subprocess.run(["git", "commit", "-qm", "worker commit"], check=True)
            """
        )

        receipt = self.governor(fake).run(
            self.contract(), self.root / "committed-run"
        )

        self.assertEqual(receipt["status"], "complete")
        self.assertTrue(receipt["product_evidence"])
        self.assertIn("src/committed.py", receipt["changed_paths"])

    def test_docs_only_change_cannot_claim_product_evidence(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            print(json.dumps({"type": "thread.started", "thread_id": "session-docs"}), flush=True)
            Path("README.md").write_text("docs only\\n", encoding="utf-8")
            """
        )

        receipt = self.governor(fake).run(
            self.contract(), self.root / "docs-run"
        )

        self.assertEqual(receipt["status"], "stopped")
        self.assertFalse(receipt["product_evidence"])
        self.assertEqual(
            receipt["reason"],
            "post_acceptance_product_evidence_fuse_tripped",
        )

    def test_product_change_deadline_stops_docs_loop_before_elapsed_cap(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            import time
            print(json.dumps({"type": "thread.started", "thread_id": "session-doc-loop"}), flush=True)
            for index in range(20):
                Path("README.md").write_text("review-v%d\\n" % index, encoding="utf-8")
                time.sleep(0.25)
            """
        )
        started = time.monotonic()
        receipt = self.governor(fake).run(
            self.contract(
                max_elapsed_seconds=4,
                product_change_deadline_seconds=1,
            ),
            self.root / "docs-deadline-run",
        )
        elapsed = time.monotonic() - started

        self.assertEqual(receipt["status"], "stopped")
        self.assertEqual(receipt["reason"], "product_change_deadline_exhausted")
        self.assertFalse(receipt["timed_out"])
        self.assertLess(elapsed, 3)
        self.assertIsNone(receipt["verification"])

    def test_outside_path_probe_stops_and_preserves_trigger_snapshot(self):
        fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            import signal
            import sys
            import time
            target = Path("outside.txt")
            target.write_text("forbidden\\n", encoding="utf-8")
            print(json.dumps({"type": "thread.started", "thread_id": "session-outside"}), flush=True)
            def cleanup_and_exit(signum, frame):
                target.unlink(missing_ok=True)
                raise SystemExit(9)
            signal.signal(signal.SIGTERM, cleanup_and_exit)
            time.sleep(10)
            """
        )
        receipt = self.governor(fake).run(
            self.contract(
                max_elapsed_seconds=4,
                product_change_deadline_seconds=3,
            ),
            self.root / "outside-probe-run",
        )

        self.assertEqual(receipt["status"], "stopped")
        self.assertEqual(receipt["reason"], "changed_path_outside_contract")
        self.assertIn("outside.txt", receipt["outside_allowed_paths"])
        self.assertFalse((self.repo / "outside.txt").exists())
        self.assertIsNone(receipt["verification"])

    def test_observed_token_budget_stops_on_current_codex_usage_schema(self):
        fake = self.fake_codex(
            """
            import json
            import time
            print(json.dumps({"type": "thread.started", "thread_id": "session-token-cap"}), flush=True)
            print(json.dumps({"usage": {"input_tokens": 80, "cached_input_tokens": 70, "output_tokens": 20, "reasoning_output_tokens": 15}}), flush=True)
            time.sleep(10)
            """
        )
        receipt = self.governor(fake).run(
            self.contract(
                max_elapsed_seconds=4,
                product_change_deadline_seconds=3,
                max_observed_total_tokens=90,
            ),
            self.root / "token-cap-run",
        )

        self.assertEqual(receipt["status"], "stopped")
        self.assertEqual(receipt["reason"], "observed_token_budget_exhausted")
        self.assertEqual(receipt["token_usage"]["total_tokens"], 100)
        self.assertIn("observed_token_cap", receipt["hard_controls"])
        self.assertNotIn(
            "observed_token_cap_unavailable", receipt["soft_controls"]
        )
        self.assertFalse(receipt["timed_out"])

    def test_observed_token_usage_cannot_regress_below_the_cap(self):
        fake = self.fake_codex(
            """
            import json
            import os
            import time
            events = [
                {"usage": {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100}},
                {"usage": {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10}},
            ]
            payload = "".join(json.dumps(event) + "\\n" for event in events)
            os.write(1, payload.encode("utf-8"))
            time.sleep(10)
            """
        )

        receipt = self.governor(fake).run(
            self.contract(
                max_elapsed_seconds=2,
                product_change_deadline_seconds=1,
                max_observed_total_tokens=90,
            ),
            self.root / "non-regressing-token-cap-run",
        )

        self.assertEqual(receipt["reason"], "observed_token_budget_exhausted")
        self.assertEqual(receipt["token_usage"]["total_tokens"], 100)

    def test_timeout_stops_root_without_retry(self):
        fake = self.fake_codex(
            """
            import json
            import time
            print(json.dumps({"type": "thread.started", "thread_id": "session-timeout"}), flush=True)
            time.sleep(10)
            """
        )

        receipt = self.governor(fake).run(
            self.contract(
                max_elapsed_seconds=1,
                product_change_deadline_seconds=1,
            ),
            self.root / "timeout-run",
        )

        self.assertEqual(receipt["status"], "stopped")
        self.assertEqual(receipt["reason"], "elapsed_budget_exhausted")
        self.assertTrue(receipt["timed_out"])
        self.assertEqual(receipt["invocation_count"], 1)

    def test_lineage_invocation_budget_blocks_changed_candidate_before_launch(self):
        first_fake = self.fake_codex(
            """
            import json
            from pathlib import Path
            import subprocess
            print(json.dumps({"type": "thread.started", "thread_id": "lineage-first"}), flush=True)
            Path("src/first.py").write_text("FIRST = True\\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/first.py"], check=True)
            subprocess.run(["git", "commit", "-qm", "first candidate"], check=True)
            """
        )
        first_contract = self.contract(
            lineage=self.lineage_mapping(
                lineage_root_id="one-invocation",
                max_invocations=1,
            )
        )
        first_receipt = self.governor(first_fake).run(
            first_contract, self.root / "lineage-first-output"
        )
        self.assertEqual(first_receipt["lineage"]["invocations_spent"], 1)

        marker = self.root / "second-lineage-model-started.txt"
        second_fake = self.fake_codex(
            f"""
            from pathlib import Path
            Path({str(marker)!r}).write_text("started")
            """
        )
        second_contract = self.contract(
            lineage=self.lineage_mapping(
                lineage_root_id="one-invocation",
                max_invocations=1,
            )
        )
        with self.assertRaisesRegex(ContractError, "invocation budget"):
            self.governor(second_fake).run(
                second_contract, self.root / "lineage-second-output"
            )
        self.assertFalse(marker.exists())

    def test_supervision_failure_kills_root_and_settles_lineage(self):
        pid_file = self.root / "failed-supervision.pid"
        fake = self.fake_codex(
            f"""
            import os
            from pathlib import Path
            import time
            Path({str(pid_file)!r}).write_text(str(os.getpid()))
            time.sleep(10)
            """
        )
        lineage_id = "supervision-failure"
        contract = self.contract(
            lineage=self.lineage_mapping(lineage_root_id=lineage_id)
        )

        def fail_supervision(*args, **kwargs):
            time.sleep(0.1)
            raise RuntimeError("probe exploded")

        try:
            with patch(
                "development_governor.runner.supervise_root_process",
                side_effect=fail_supervision,
            ):
                with self.assertRaisesRegex(
                    ContractError, "online supervision failed"
                ):
                    self.governor(fake).run(
                        contract, self.root / "failed-supervision-output"
                    )
        finally:
            if pid_file.exists():
                pid = int(pid_file.read_text())
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass

        second_fake = self.fake_codex(
            """
            from pathlib import Path
            Path("src/after_failure.py").write_text("RECOVERED = True\\n")
            """
        )
        second = self.contract(
            lineage=self.lineage_mapping(lineage_root_id=lineage_id)
        )
        receipt = self.governor(second_fake).run(
            second, self.root / "after-supervision-failure-output"
        )
        self.assertEqual(receipt["lineage"]["invocations_spent"], 2)

    def test_supervision_failure_after_root_exit_kills_lingering_group(self):
        ready_file = self.root / "runner-orphan-ready"
        child_pid_file = self.root / "runner-orphan.pid"
        stopped_file = self.root / "runner-orphan-stopped"
        fake = self.fake_codex(
            f"""
            import subprocess
            import sys
            import time
            child = r'''
            from pathlib import Path
            import os
            import signal
            import sys
            import time
            ready, pid_file, stopped = map(Path, sys.argv[1:])
            def stop(signum, frame):
                stopped.write_text("stopped", encoding="utf-8")
                raise SystemExit(0)
            signal.signal(signal.SIGTERM, stop)
            pid_file.write_text(str(os.getpid()), encoding="utf-8")
            ready.write_text("ready", encoding="utf-8")
            time.sleep(10)
            '''
            subprocess.Popen([
                sys.executable, "-c", child,
                {str(ready_file)!r}, {str(child_pid_file)!r}, {str(stopped_file)!r},
            ])
            while not __import__("pathlib").Path({str(ready_file)!r}).exists():
                time.sleep(0.005)
            """
        )

        def fail_after_root_exit(process, **kwargs):
            deadline = time.monotonic() + 2
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertIsNotNone(process.returncode)
            raise RuntimeError("I/O failure after root exit")

        try:
            with patch(
                "development_governor.runner.supervise_root_process",
                side_effect=fail_after_root_exit,
            ):
                with self.assertRaisesRegex(
                    ContractError, "online supervision failed"
                ):
                    self.governor(fake).run(
                        self.contract(
                            lineage=self.lineage_mapping(
                                lineage_root_id="post-exit-supervision-failure"
                            )
                        ),
                        self.root / "post-exit-supervision-failure-output",
                    )
            self.assertTrue(stopped_file.exists())
        finally:
            if child_pid_file.exists():
                child_pid = int(child_pid_file.read_text())
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass

    def test_duplicate_evaluation_blocks_before_codex_launch(self):
        ledger = self.root / "evaluation-authority" / "ledger.jsonl"
        first_contract = self.contract(
            evaluation=self.evaluation_mapping(ledger)
        )
        request = build_evaluation_request(
            first_contract.evaluation,
            first_contract.acceptance_control_fingerprint,
            hash_path_set(self.repo, first_contract.product_paths),
        )
        reserve_evaluation(
            first_contract.evaluation, request, first_contract.contract_hash
        )
        second_contract = self.contract(
            evaluation=self.evaluation_mapping(ledger)
        )
        marker = self.root / "duplicate-model-launched.txt"
        fake = self.fake_codex(
            f"""
            from pathlib import Path
            Path({str(marker)!r}).write_text("launched")
            """
        )

        with self.assertRaisesRegex(ContractError, "duplicate evaluation"):
            self.governor(fake).run(
                second_contract, self.root / "duplicate-evaluation"
            )

        self.assertFalse(marker.exists())

    def test_output_directory_inside_repo_is_rejected_before_launch(self):
        marker = self.root / "should-not-launch.txt"
        fake = self.fake_codex(
            f"""
            from pathlib import Path
            Path({str(marker)!r}).write_text("launched")
            """
        )

        with self.assertRaisesRegex(ContractError, "output_dir must be outside"):
            self.governor(fake).run(
                self.contract(), self.repo / ".governor-run"
            )

        self.assertFalse(marker.exists())

    def test_nonzero_exit_with_session_is_interrupted_without_retry(self):
        marker = self.root / "interrupted-count.txt"
        fake = self.fake_codex(
            f"""
            import json
            from pathlib import Path
            import sys
            marker = Path({str(marker)!r})
            marker.write_text(marker.read_text() + "1" if marker.exists() else "1")
            print(json.dumps({{"type": "thread.started", "thread_id": "session-interrupted"}}), flush=True)
            sys.exit(7)
            """
        )

        receipt = self.governor(fake).run(
            self.contract(), self.root / "interrupted-run"
        )

        self.assertEqual(receipt["status"], "interrupted")
        self.assertEqual(receipt["session_id"], "session-interrupted")
        self.assertEqual(receipt["invocation_count"], 1)
        self.assertEqual(marker.read_text(), "1")
        self.assertIn("codex exec resume", receipt["next_action"])

    def test_malformed_token_usage_is_unavailable_and_does_not_change_terminal_decision(self):
        marker = self.root / "malformed-token-count.txt"
        fake = self.fake_codex(
            f"""
            import json
            from pathlib import Path
            import sys
            marker = Path({str(marker)!r})
            marker.write_text(marker.read_text() + "1" if marker.exists() else "1")
            print(json.dumps({{"type": "thread.started", "thread_id": "session-malformed-token"}}), flush=True)
            print(json.dumps({{"type": "event_msg", "payload": {{"type": "token_count", "info": {{"total_token_usage": {{"input_tokens": "many", "total_tokens": -1}}}}}}}}), flush=True)
            sys.exit(7)
            """
        )

        receipt = self.governor(fake).run(
            self.contract(), self.root / "malformed-token-run"
        )

        self.assertEqual(receipt["token_usage"], {"status": "unavailable"})
        self.assertEqual(receipt["status"], "interrupted")
        self.assertFalse(receipt["product_evidence"])
        self.assertEqual(receipt["invocation_count"], 1)
        self.assertEqual(marker.read_text(), "1")

    def test_receipt_labels_hard_and_soft_controls(self):
        fake = self.fake_codex(
            """
            import json
            print(json.dumps({"type": "thread.started", "thread_id": "session-labels"}), flush=True)
            """
        )

        receipt = self.governor(fake).run(
            self.contract(max_observed_total_tokens=90), self.root / "labels-run"
        )

        self.assertIn("root_elapsed_cap", receipt["hard_controls"])
        self.assertIn("frozen_acceptance_interface_and_test_hashes", receipt["hard_controls"])
        self.assertIn("serial_multi_agent_disabled", receipt["hard_controls"])
        self.assertIn("stage_capability_local_admission", receipt["hard_controls"])
        self.assertIn("owner_activated_gate_admission", receipt["hard_controls"])
        self.assertIn("post_acceptance_product_evidence_fuse", receipt["hard_controls"])
        self.assertNotIn("observed_token_cap", receipt["hard_controls"])
        self.assertIn("observed_token_cap_unavailable", receipt["soft_controls"])
        self.assertEqual(receipt["token_usage"], {"status": "unavailable"})
        written = json.loads((self.root / "labels-run" / "terminal-receipt.json").read_text())
        self.assertEqual(written, receipt)

    def test_cli_validates_contract_without_starting_codex(self):
        contract_path = self.root / "contract.json"
        contract_path.write_text(
            json.dumps(asdict(self.contract())), encoding="utf-8"
        )
        project_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(project_root / "src"))

        result = subprocess.run(
            [
                sys.executable,
                str(project_root / "scripts" / "run_development_governor.py"),
                "validate",
                str(contract_path),
            ],
            cwd=str(project_root),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "valid")
        self.assertRegex(payload["contract_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(payload["max_parallel_agents"], 1)
        self.assertEqual(payload["execution_mode"], "serial_root_only")
        self.assertEqual(payload["primary_mode"], "product")
        self.assertEqual(payload["reasoning_effort"], "medium")
        self.assertEqual(payload["review_wave_cost"], 0)
        self.assertEqual(payload["lineage"]["ledger_status"], "matched")
        self.assertRegex(payload["acceptance_interface_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(payload["acceptance_test_bundle_hash"], r"^[0-9a-f]{64}$")

    def test_cli_rejects_stale_lineage_ledger_without_model_use(self):
        contract = self.contract(
            lineage=self.lineage_mapping(lineage_root_id="stale-cli-lineage")
        )
        contract_path = self.root / "stale-lineage-contract.json"
        contract_path.write_text(json.dumps(asdict(contract)), encoding="utf-8")
        project_root = Path(__file__).resolve().parents[1]
        home = self.root / "isolated-home"
        state_root = home / ".codex" / "development-governor" / "v0"
        ledger = lineage_ledger_path(
            state_root, self.repo, contract.lineage.lineage_root_id
        )
        ledger.parent.mkdir(parents=True)
        ledger.write_text("stale\n", encoding="utf-8")
        env = dict(
            os.environ,
            HOME=str(home),
            PYTHONPATH=str(project_root / "src"),
        )

        result = subprocess.run(
            [
                sys.executable,
                str(project_root / "scripts" / "run_development_governor.py"),
                "validate",
                str(contract_path),
            ],
            cwd=str(project_root),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "invalid")
        self.assertIn("lineage ledger hash mismatch", payload["error"])

    def test_cli_rejects_contract_authored_inside_governed_repository(self):
        contract_path = self.repo / "self-authored-contract.json"
        contract_path.write_text(
            json.dumps(asdict(self.contract())), encoding="utf-8"
        )
        project_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(project_root / "src"))

        result = subprocess.run(
            [
                sys.executable,
                str(project_root / "scripts" / "run_development_governor.py"),
                "validate",
                str(contract_path),
            ],
            cwd=str(project_root),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "invalid")
        self.assertIn(
            "contract file must be outside the governed repository",
            payload["error"],
        )

    def test_cli_rejects_stale_evaluation_ledger_without_model_use(self):
        ledger = self.root / "cli-evaluation-ledger.jsonl"
        contract_path = self.root / "evaluation-contract.json"
        contract_path.write_text(
            json.dumps(
                asdict(
                    self.contract(
                        evaluation=self.evaluation_mapping(ledger)
                    )
                )
            ),
            encoding="utf-8",
        )
        ledger.write_text("tampered\n", encoding="utf-8")
        project_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(project_root / "src"))

        result = subprocess.run(
            [
                sys.executable,
                str(project_root / "scripts" / "run_development_governor.py"),
                "validate",
                str(contract_path),
            ],
            cwd=str(project_root),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertIn("evaluation ledger hash mismatch", payload["error"])


if __name__ == "__main__":
    unittest.main()
