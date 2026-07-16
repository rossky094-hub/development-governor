import unittest

from development_governor.stage_control import (
    StageControlError,
    StageControlPolicy,
)


class StageControlPolicyTests(unittest.TestCase):
    def policy_mapping(self, **overrides):
        data = {
            "current_scope_id": "retrieval:implementation",
            "authorization_scopes": [
                {
                    "scope_id": "retrieval:implementation",
                    "capability_id": "retrieval",
                    "stage_id": "implementation",
                    "status": "authorized",
                    "authorized_product_paths": ["src/retrieval/"],
                },
                {
                    "scope_id": "retrieval:publication",
                    "capability_id": "retrieval",
                    "stage_id": "publication",
                    "status": "blocked",
                    "authorized_product_paths": ["publish/retrieval/"],
                },
                {
                    "scope_id": "report:implementation",
                    "capability_id": "report",
                    "stage_id": "implementation",
                    "status": "authorized",
                    "authorized_product_paths": ["src/report/"],
                },
            ],
            "blockers": [
                {
                    "blocker_id": "publication-proof-missing",
                    "affected_scope_ids": ["retrieval:publication"],
                    "required_predicate": "publication proof is accepted",
                    "failure_if_ignored": "an unsupported public claim could ship",
                    "earliest_stage": "publication",
                    "safe_work_remaining_scope_ids": [
                        "retrieval:implementation",
                        "report:implementation",
                    ],
                }
            ],
            "gates": [],
            "owner_acceptance_ref": "owner:thread/accepted-slice-v1",
            "owner_revision_ref": None,
            "max_review_batches_without_owner": 1,
            "automatic_post_review_revisions": 0,
        }
        data.update(overrides)
        return data

    def test_future_stage_blocker_does_not_block_authorized_current_scope(self):
        policy = StageControlPolicy.from_mapping(self.policy_mapping())

        self.assertEqual(policy.decision.action, "allow_current_scope")
        self.assertEqual(
            policy.decision.current_scope_id,
            "retrieval:implementation",
        )
        self.assertEqual(
            policy.decision.blocked_scope_ids,
            ("retrieval:publication",),
        )
        self.assertIn(
            "retrieval:implementation",
            policy.decision.safe_work_remaining_scope_ids,
        )

    def test_blocker_requires_affected_scope_and_complete_safe_work_proof(self):
        missing_affected = self.policy_mapping()
        missing_affected["blockers"][0]["affected_scope_ids"] = []
        with self.assertRaisesRegex(StageControlError, "affected_scope_ids"):
            StageControlPolicy.from_mapping(missing_affected)

        incomplete_safe_work = self.policy_mapping()
        incomplete_safe_work["blockers"][0][
            "safe_work_remaining_scope_ids"
        ] = ["retrieval:implementation"]
        with self.assertRaisesRegex(StageControlError, "complete safe work"):
            StageControlPolicy.from_mapping(incomplete_safe_work)

    def test_proposed_gate_is_nonblocking_until_owner_activation(self):
        raw = self.policy_mapping(
            gates=[
                {
                    "gate_id": "future-publication-gate",
                    "affected_scope_ids": ["retrieval:publication"],
                    "status": "proposed_nonblocking",
                    "owner_decision_ref": None,
                }
            ]
        )

        policy = StageControlPolicy.from_mapping(raw)

        self.assertEqual(policy.decision.action, "allow_current_scope")
        self.assertEqual(policy.decision.active_gate_ids, ())
        self.assertEqual(
            policy.decision.proposed_gate_ids,
            ("future-publication-gate",),
        )

    def test_activated_gate_requires_owner_decision_reference(self):
        raw = self.policy_mapping(
            gates=[
                {
                    "gate_id": "current-implementation-gate",
                    "affected_scope_ids": ["retrieval:implementation"],
                    "status": "owner_activated",
                    "owner_decision_ref": None,
                }
            ]
        )

        with self.assertRaisesRegex(StageControlError, "owner_decision_ref"):
            StageControlPolicy.from_mapping(raw)

    def test_blocked_current_scope_routes_to_safe_work_instead_of_global_stop(self):
        scopes = self.policy_mapping()["authorization_scopes"]
        scopes[0]["status"] = "blocked"
        publication_blocker = self.policy_mapping()["blockers"][0]
        publication_blocker["safe_work_remaining_scope_ids"] = [
            "report:implementation"
        ]
        blockers = [
            {
                "blocker_id": "retrieval-implementation-risk",
                "affected_scope_ids": ["retrieval:implementation"],
                "required_predicate": "fixture is available",
                "failure_if_ignored": "implementation result would be invalid",
                "earliest_stage": "implementation",
                "safe_work_remaining_scope_ids": ["report:implementation"],
            },
            publication_blocker,
        ]
        raw = self.policy_mapping(
            authorization_scopes=scopes,
            blockers=blockers,
        )

        policy = StageControlPolicy.from_mapping(raw)

        self.assertEqual(policy.decision.action, "route_to_safe_scope")
        self.assertEqual(
            policy.decision.safe_work_remaining_scope_ids,
            ("report:implementation",),
        )
        self.assertNotEqual(policy.decision.action, "global_stop")

    def test_post_acceptance_product_evidence_fuse_is_deterministic(self):
        policy = StageControlPolicy.from_mapping(self.policy_mapping())

        self.assertEqual(policy.product_evidence_fuse(True), "satisfied")
        self.assertEqual(policy.product_evidence_fuse(False), "tripped")

        not_accepted = self.policy_mapping(owner_acceptance_ref=None)
        research_policy = StageControlPolicy.from_mapping(not_accepted)
        self.assertEqual(
            research_policy.product_evidence_fuse(False),
            "not_applicable",
        )

    def test_review_loop_policy_is_closed(self):
        with self.assertRaisesRegex(StageControlError, "one batch"):
            StageControlPolicy.from_mapping(
                self.policy_mapping(max_review_batches_without_owner=2)
            )
        with self.assertRaisesRegex(StageControlError, "automatic post-review"):
            StageControlPolicy.from_mapping(
                self.policy_mapping(automatic_post_review_revisions=1)
            )

    def test_scope_paths_are_normalized_and_bound_to_scope_identity(self):
        policy = StageControlPolicy.from_mapping(self.policy_mapping())

        self.assertEqual(
            policy.current_scope.authorized_product_paths,
            ("src/retrieval/",),
        )

        escaping = self.policy_mapping()
        escaping["authorization_scopes"][0]["authorized_product_paths"] = [
            "../outside"
        ]
        with self.assertRaisesRegex(StageControlError, "repository-relative"):
            StageControlPolicy.from_mapping(escaping)


if __name__ == "__main__":
    unittest.main()
