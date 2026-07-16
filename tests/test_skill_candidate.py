import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from development_governor.runner import ContractError, RunContract, hash_path_set
from development_governor.rerun import ledger_sha256
from development_governor.skill_candidate import (
    SkillCandidateError,
    promote_skill_candidate,
    stage_skill_candidate,
)


class SkillCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.installed = self.root / "installed-skill"
        self.installed.mkdir()
        (self.installed / "SKILL.md").write_text("installed\n", encoding="utf-8")
        (self.installed / "scripts").mkdir()
        (self.installed / "scripts" / "check.py").write_text(
            "VALUE = 'installed'\n", encoding="utf-8"
        )
        self.acceptance = self.root / "owner-acceptance"
        self.acceptance.mkdir()
        (self.acceptance / "test_contract.py").write_text(
            "assert True\n", encoding="utf-8"
        )
        self.candidate = self.root / "candidate-repo"
        self.terminal_receipt = self.root / "terminal-receipt.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_stage_skill_candidate_creates_clean_git_repo_with_frozen_sources(self):
        receipt = stage_skill_candidate(
            self.installed, self.acceptance, self.candidate
        )

        self.assertEqual(receipt["status"], "staged")
        self.assertTrue((self.candidate / ".git").is_dir())
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.candidate), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout,
            "",
        )
        self.assertEqual(
            (self.candidate / "skill" / "SKILL.md").read_text(encoding="utf-8"),
            "installed\n",
        )
        self.assertEqual(
            (self.candidate / "acceptance" / "test_contract.py").read_text(
                encoding="utf-8"
            ),
            "assert True\n",
        )
        self.assertEqual(
            receipt["skill_tree_hash"],
            hash_path_set(self.candidate, ("skill/",)),
        )
        manifest = json.loads(
            (self.candidate / ".governor" / "skill-candidate.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["source_skill_tree_hash"], receipt["skill_tree_hash"])
        self.assertRegex(receipt["baseline_commit"], r"^[0-9a-f]{40}$")

    def test_staged_skill_candidate_requires_rerun_gate_contract(self):
        stage_skill_candidate(self.installed, self.acceptance, self.candidate)
        acceptance_file = self.candidate / "acceptance" / "test_contract.py"
        raw = {
            "objective": "Change one staged Skill behavior",
            "repo_path": str(self.candidate),
            "model": "gpt-5.6-sol",
            "primary_mode": "product",
            "reasoning_effort": "medium",
            "max_elapsed_seconds": 60,
            "product_change_deadline_seconds": 30,
            "max_observed_total_tokens": None,
            "max_parallel_agents": 1,
            "max_total_agents": 1,
            "max_spawn_depth": 1,
            "review_credits": 0,
            "allowed_paths": ["skill/"],
            "product_paths": ["skill/"],
            "verification_command": [
                sys.executable,
                "acceptance/test_contract.py",
            ],
            "acceptance_files": [
                {
                    "path": "acceptance/test_contract.py",
                    "sha256": hashlib.sha256(
                        acceptance_file.read_bytes()
                    ).hexdigest(),
                }
            ],
            "parallel_units": [],
            "stage_control": {
                "current_scope_id": "skill:implementation",
                "authorization_scopes": [
                    {
                        "scope_id": "skill:implementation",
                        "capability_id": "skill",
                        "stage_id": "implementation",
                        "status": "authorized",
                        "authorized_product_paths": ["skill/"],
                    }
                ],
                "blockers": [],
                "gates": [],
                "owner_acceptance_ref": "owner:test/accepted-skill-slice",
                "owner_revision_ref": None,
                "max_review_batches_without_owner": 1,
                "automatic_post_review_revisions": 0,
            },
            "lineage": {
                "lineage_root_id": "skill-candidate-test",
                "ledger_sha256": hashlib.sha256(b"").hexdigest(),
                "max_elapsed_seconds": 60,
                "max_invocations": 1,
                "max_review_waves": 0,
                "resume_from_reservation_id": None,
                "resume_session_id": None,
                "owner_review_credit": None,
            },
        }

        with self.assertRaisesRegex(
            ContractError, "Skill candidate runs require evaluation"
        ):
            RunContract.from_mapping(raw)

        ledger = self.root / "candidate-evaluation-ledger.jsonl"
        raw["evaluation"] = {
            "phase": "red",
            "catalog_scope_ids": ["skill-contract"],
            "scope_ids": ["skill-contract"],
            "impacted_scope_ids": [],
            "ledger_path": str(ledger),
            "ledger_sha256": ledger_sha256(ledger),
            "rerun_credit": None,
        }
        contract = RunContract.from_mapping(raw)
        self.assertEqual(contract.product_paths, ("skill/",))
        self.assertIsNotNone(contract.evaluation)

    def test_promotion_requires_complete_external_hash_bound_receipt(self):
        stage_skill_candidate(self.installed, self.acceptance, self.candidate)
        (self.candidate / "skill" / "SKILL.md").write_text(
            "candidate\n", encoding="utf-8"
        )
        self._write_terminal_receipt(status="need_owner", product_evidence=False)

        with self.assertRaisesRegex(
            SkillCandidateError, "complete terminal receipt"
        ):
            promote_skill_candidate(
                self.candidate, self.installed, self.terminal_receipt
            )

        self.assertEqual(
            (self.installed / "SKILL.md").read_text(encoding="utf-8"),
            "installed\n",
        )

    def test_promotion_atomically_replaces_installed_skill_after_hash_match(self):
        stage_skill_candidate(self.installed, self.acceptance, self.candidate)
        (self.candidate / "skill" / "SKILL.md").write_text(
            "candidate\n", encoding="utf-8"
        )
        self._write_terminal_receipt()

        result = promote_skill_candidate(
            self.candidate, self.installed, self.terminal_receipt
        )

        self.assertEqual(result["status"], "promoted")
        self.assertEqual(
            (self.installed / "SKILL.md").read_text(encoding="utf-8"),
            "candidate\n",
        )
        self.assertEqual(
            result["installed_skill_tree_hash"],
            hash_path_set(self.installed.parent, (self.installed.name + "/",)),
        )
        self.assertEqual(
            (self.candidate / "skill" / "SKILL.md").read_text(encoding="utf-8"),
            "candidate\n",
        )

    def test_promotion_rejects_stale_or_candidate_local_receipt(self):
        stage_skill_candidate(self.installed, self.acceptance, self.candidate)
        self._write_terminal_receipt(final_hash="0" * 64)
        with self.assertRaisesRegex(SkillCandidateError, "product tree hash"):
            promote_skill_candidate(
                self.candidate, self.installed, self.terminal_receipt
            )

        local_receipt = self.candidate / "self-approved.json"
        self._write_terminal_receipt(path=local_receipt)
        with self.assertRaisesRegex(SkillCandidateError, "outside candidate_repo"):
            promote_skill_candidate(self.candidate, self.installed, local_receipt)

    def test_stage_skill_cli_creates_candidate_without_model_use(self):
        result = self._run_cli(
            "stage-skill",
            "--source",
            str(self.installed),
            "--acceptance-source",
            str(self.acceptance),
            "--candidate-repo",
            str(self.candidate),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "staged")
        self.assertTrue((self.candidate / ".git").is_dir())

    def test_promote_skill_cli_rejects_unbound_receipt(self):
        stage_skill_candidate(self.installed, self.acceptance, self.candidate)
        self._write_terminal_receipt(status="need_owner", product_evidence=False)

        result = self._run_cli(
            "promote-skill",
            "--candidate-repo",
            str(self.candidate),
            "--installed-skill",
            str(self.installed),
            "--terminal-receipt",
            str(self.terminal_receipt),
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "invalid")
        self.assertIn("complete terminal receipt", payload["error"])
        self.assertEqual(
            (self.installed / "SKILL.md").read_text(encoding="utf-8"),
            "installed\n",
        )

    def _write_terminal_receipt(
        self,
        *,
        path=None,
        status="complete",
        product_evidence=True,
        final_hash=None,
    ):
        receipt_path = Path(path or self.terminal_receipt)
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": "development-governor.run-receipt.v0",
                    "status": status,
                    "product_evidence": product_evidence,
                    "exit_code": 0,
                    "timed_out": False,
                    "outside_allowed_paths": [],
                    "acceptance": {
                        "preflight_status": "matched",
                        "postflight_status": "matched",
                        "capsule_status": "matched",
                    },
                    "verification": {"exit_code": 0},
                    "repository": {
                        "path": str(self.candidate.resolve()),
                        "product_paths": ["skill/"],
                        "final_product_tree_hash": final_hash
                        or hash_path_set(self.candidate, ("skill/",)),
                    },
                }
            ),
            encoding="utf-8",
        )

    def _run_cli(self, *args):
        project_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(project_root / "src"))
        return subprocess.run(
            [
                sys.executable,
                str(project_root / "scripts" / "run_development_governor.py"),
                *args,
            ],
            cwd=str(project_root),
            env=env,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
