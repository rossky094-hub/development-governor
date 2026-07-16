from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from development_governor.rerun import (
    EvaluationPolicy,
    RerunGateError,
    build_evaluation_request,
    ledger_sha256,
    reserve_evaluation,
    settle_evaluation,
)


class RerunGateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.ledger = self.root / "authority" / "evaluation-ledger.jsonl"
        self.control_hash = "a" * 64
        self.product_hash = "b" * 64

    def tearDown(self):
        self.tempdir.cleanup()

    def policy(self, **overrides):
        raw = {
            "phase": "red",
            "catalog_scope_ids": ["scenario-a", "scenario-b"],
            "scope_ids": ["scenario-a", "scenario-b"],
            "impacted_scope_ids": [],
            "ledger_path": str(self.ledger),
            "ledger_sha256": ledger_sha256(self.ledger),
            "rerun_credit": None,
        }
        raw.update(overrides)
        return EvaluationPolicy.from_mapping(raw, self.repo)

    def test_red_identity_has_no_treatment_fingerprint(self):
        policy = self.policy()

        first = build_evaluation_request(
            policy, self.control_hash, self.product_hash
        )
        changed = build_evaluation_request(
            policy, self.control_hash, "c" * 64
        )

        self.assertIsNone(first.treatment_fingerprint)
        self.assertEqual(first.evaluation_fingerprint, changed.evaluation_fingerprint)

    def test_duplicate_evaluation_is_denied_without_credit(self):
        policy = self.policy()
        request = build_evaluation_request(
            policy, self.control_hash, self.product_hash
        )
        first = reserve_evaluation(policy, request, "1" * 64)
        current_policy = replace(
            policy, ledger_sha256=ledger_sha256(self.ledger)
        )

        with self.assertRaisesRegex(RerunGateError, "duplicate evaluation"):
            reserve_evaluation(current_policy, request, "2" * 64)

        self.assertFalse(first["rerun_credit_consumed"])
        self.assertEqual(first["prior_attempt_count"], 0)

    def test_unique_owner_credit_permits_one_duplicate_only(self):
        policy = self.policy()
        request = build_evaluation_request(
            policy, self.control_hash, self.product_hash
        )
        reserve_evaluation(policy, request, "1" * 64)
        credit = {
            "credit_id": "owner-rerun-001",
            "owner_authority_ref": "owner:rossky",
            "reason": "repair runner infrastructure",
        }
        credited_policy = self.policy(
            ledger_sha256=ledger_sha256(self.ledger), rerun_credit=credit
        )

        credited = reserve_evaluation(
            credited_policy, request, "2" * 64
        )

        self.assertTrue(credited["rerun_credit_consumed"])
        reused_policy = self.policy(
            ledger_sha256=ledger_sha256(self.ledger), rerun_credit=credit
        )
        with self.assertRaisesRegex(RerunGateError, "credit was already consumed"):
            reserve_evaluation(reused_policy, request, "3" * 64)

    def test_green_scope_must_equal_declared_impact_set(self):
        with self.assertRaisesRegex(RerunGateError, "impacted scope"):
            self.policy(
                phase="green",
                scope_ids=["scenario-a", "scenario-b"],
                impacted_scope_ids=["scenario-a"],
            )

    def test_green_identity_binds_product_tree_and_impacted_scope(self):
        policy = self.policy(
            phase="green",
            scope_ids=["scenario-a"],
            impacted_scope_ids=["scenario-a"],
        )

        first = build_evaluation_request(
            policy, self.control_hash, self.product_hash
        )
        changed = build_evaluation_request(
            policy, self.control_hash, "c" * 64
        )

        self.assertEqual(first.treatment_fingerprint, self.product_hash)
        self.assertNotEqual(first.evaluation_fingerprint, changed.evaluation_fingerprint)

    def test_settlement_is_append_only_and_cannot_repeat(self):
        policy = self.policy()
        request = build_evaluation_request(
            policy, self.control_hash, self.product_hash
        )
        reservation = reserve_evaluation(policy, request, "1" * 64)

        settlement = settle_evaluation(
            self.ledger, reservation["reservation_id"], "complete"
        )

        self.assertEqual(settlement["terminal_status"], "complete")
        with self.assertRaisesRegex(RerunGateError, "already settled"):
            settle_evaluation(
                self.ledger, reservation["reservation_id"], "complete"
            )


if __name__ == "__main__":
    unittest.main()
