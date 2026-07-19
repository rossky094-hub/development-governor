import json
import subprocess
from pathlib import Path
import tempfile
import unittest

from development_governor.lineage import (
    LineageError,
    LineagePolicy,
    lineage_ledger_path,
    lineage_ledger_sha256,
    reserve_lineage,
    settle_lineage,
)


class LineageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.ledger = self.root / "state" / "lineage.jsonl"
        self.contract_a = "a" * 64
        self.contract_b = "b" * 64
        self.candidate_a = "c" * 64
        self.candidate_b = "d" * 40

    def tearDown(self):
        self.tempdir.cleanup()

    def policy(self, *, ledger=None, **overrides):
        ledger = Path(ledger or self.ledger)
        raw = {
            "lineage_root_id": "lineage:test",
            "ledger_sha256": lineage_ledger_sha256(ledger),
            "max_elapsed_seconds": 100,
            "max_invocations": 4,
            "max_review_waves": 1,
            "resume_from_reservation_id": None,
            "resume_session_id": None,
            "owner_review_credit": None,
        }
        raw.update(overrides)
        return LineagePolicy.from_mapping(raw)

    def reserve(self, policy, *, ledger=None, contract=None, candidate=None,
                elapsed=10, reviews=0, scope=None, mode=None,
                owner_acceptance_ref=None, owner_revision_ref=None):
        return reserve_lineage(
            policy,
            ledger_path=Path(ledger or self.ledger),
            contract_hash=contract or self.contract_a,
            candidate_hash=candidate or self.candidate_a,
            requested_elapsed_seconds=elapsed,
            requested_review_waves=reviews,
            current_scope_id=scope,
            primary_mode=mode,
            owner_acceptance_ref=owner_acceptance_ref,
            owner_revision_ref=owner_revision_ref,
        )

    def settle(self, reservation, *, ledger=None, status="complete",
               started=True, actual=1.0, session="session-1"):
        return settle_lineage(
            Path(ledger or self.ledger),
            reservation["reservation_id"],
            terminal_status=status,
            model_started=started,
            actual_elapsed_seconds=actual,
            session_id=session,
        )

    def test_policy_requires_closed_mapping_and_resume_pair(self):
        raw = {
            "lineage_root_id": "lineage:test",
            "ledger_sha256": "0" * 64,
            "max_elapsed_seconds": 10,
            "max_invocations": 1,
            "max_review_waves": 0,
            "resume_from_reservation_id": None,
            "resume_session_id": None,
            "owner_review_credit": None,
        }
        missing = dict(raw)
        missing.pop("max_invocations")
        with self.assertRaisesRegex(LineageError, "missing fields"):
            LineagePolicy.from_mapping(missing)
        with self.assertRaisesRegex(LineageError, "both be null or both be set"):
            LineagePolicy.from_mapping(
                dict(raw, resume_from_reservation_id="e" * 64)
            )
        with self.assertRaisesRegex(LineageError, "owner_review_credit"):
            LineagePolicy.from_mapping(
                dict(raw, owner_review_credit={"credit_id": "credit-only"})
            )

    def test_batched_reservation_charges_only_started_segment_invocations(self):
        reservation = reserve_lineage(
            self.policy(),
            ledger_path=self.ledger,
            contract_hash=self.contract_a,
            candidate_hash=self.candidate_a,
            requested_elapsed_seconds=30,
            requested_invocations=3,
            requested_review_waves=1,
        )

        self.assertEqual(reservation["projection"]["invocations_reserved"], 3)
        settled = settle_lineage(
            self.ledger,
            reservation["reservation_id"],
            terminal_status="stopped",
            model_started=True,
            actual_elapsed_seconds=5.2,
            actual_invocations=2,
            session_id=None,
        )

        self.assertEqual(settled["projection"]["invocations_spent"], 2)
        self.assertEqual(settled["projection"]["invocations_remaining"], 2)
        self.assertEqual(settled["projection"]["review_waves_spent"], 1)

    def test_candidate_change_does_not_reset_review_budget(self):
        first = self.reserve(self.policy(), reviews=1)
        self.settle(first, actual=1.1)

        with self.assertRaisesRegex(LineageError, "review wave budget"):
            self.reserve(
                self.policy(),
                contract=self.contract_b,
                candidate=self.candidate_b,
                reviews=1,
            )

    def test_owner_credit_allows_exactly_one_additional_review_wave(self):
        first = self.reserve(self.policy(), reviews=1)
        self.settle(first)
        credit = {
            "credit_id": "owner-wave-2",
            "owner_authority_ref": "owner:rossky",
            "reason": "authorize one additional review wave",
        }

        second = self.reserve(
            self.policy(owner_review_credit=credit),
            contract=self.contract_b,
            candidate=self.candidate_b,
            reviews=1,
        )

        self.assertTrue(second["owner_review_credit_consumed"])
        self.assertEqual(second["projection"]["review_waves_spent"], 1)
        self.assertEqual(second["projection"]["review_waves_reserved"], 2)
        self.assertEqual(second["projection"]["review_waves_remaining"], 0)
        self.settle(second, session="session-2")
        with self.assertRaisesRegex(LineageError, "credit.*already"):
            self.reserve(
                self.policy(owner_review_credit=credit),
                contract="e" * 64,
                candidate="f" * 64,
                reviews=1,
            )

    def test_owner_accepted_scope_cannot_switch_back_to_governance(self):
        first = self.reserve(
            self.policy(),
            scope="retrieval:implementation",
            mode="product",
            owner_acceptance_ref="owner:accepted-retrieval-v1",
        )
        self.settle(first)

        with self.assertRaisesRegex(LineageError, "accepted scope.*product mode"):
            self.reserve(
                self.policy(),
                contract=self.contract_b,
                candidate=self.candidate_b,
                scope="retrieval:implementation",
                mode="governance",
            )

    def test_post_review_candidate_change_requires_owner_revision_ref(self):
        reviewed = self.reserve(
            self.policy(),
            reviews=1,
            scope="retrieval:implementation",
            mode="governance",
        )
        self.settle(reviewed)

        with self.assertRaisesRegex(LineageError, "post-review revision"):
            self.reserve(
                self.policy(),
                contract=self.contract_b,
                candidate=self.candidate_b,
                scope="retrieval:implementation",
                mode="governance",
            )

        authorized = self.reserve(
            self.policy(),
            contract=self.contract_b,
            candidate=self.candidate_b,
            scope="retrieval:implementation",
            mode="governance",
            owner_revision_ref="owner:authorized-revision-v1",
        )
        self.assertEqual(
            authorized["owner_revision_ref"],
            "owner:authorized-revision-v1",
        )

    def test_started_authorized_revision_closes_the_post_review_gate(self):
        reviewed = self.reserve(
            self.policy(),
            reviews=1,
            scope="retrieval:implementation",
            mode="governance",
        )
        self.settle(reviewed)
        authorized = self.reserve(
            self.policy(),
            contract=self.contract_b,
            candidate=self.candidate_b,
            scope="retrieval:implementation",
            mode="governance",
            owner_revision_ref="owner:authorized-revision-v1",
        )
        self.settle(authorized, session="session-2")

        follow_up = self.reserve(
            self.policy(),
            contract="e" * 64,
            candidate="f" * 64,
            scope="retrieval:implementation",
            mode="governance",
        )

        self.assertIsNone(follow_up["owner_revision_ref"])

    def test_elapsed_and_invocation_budgets_accumulate_actual_spend(self):
        first = self.reserve(
            self.policy(max_elapsed_seconds=10, max_invocations=2), elapsed=5
        )
        first_settlement = self.settle(first, actual=2.2)
        self.assertEqual(first_settlement["projection"]["elapsed_seconds_spent"], 3)
        second = self.reserve(
            self.policy(max_elapsed_seconds=10, max_invocations=2),
            contract=self.contract_b,
            candidate=self.candidate_b,
            elapsed=7,
        )
        self.assertEqual(second["projection"]["elapsed_seconds_reserved"], 10)
        self.assertEqual(second["projection"]["invocations_reserved"], 2)
        second_settlement = self.settle(second, actual=4.1, session="session-2")
        self.assertEqual(second_settlement["projection"]["elapsed_seconds_spent"], 8)
        self.assertEqual(second_settlement["projection"]["elapsed_seconds_remaining"], 2)
        with self.assertRaisesRegex(LineageError, "invocation budget"):
            self.reserve(
                self.policy(max_elapsed_seconds=10, max_invocations=2), elapsed=1
            )

    def test_unstarted_settlement_releases_all_reserved_budget(self):
        policy = self.policy(
            max_elapsed_seconds=5, max_invocations=1, max_review_waves=1
        )
        first = self.reserve(policy, elapsed=5, reviews=1)

        released = self.settle(
            first, status="runner_error", started=False, actual=0,
            session=None,
        )

        projection = released["projection"]
        self.assertEqual(projection["elapsed_seconds_spent"], 0)
        self.assertEqual(projection["elapsed_seconds_reserved"], 0)
        self.assertEqual(projection["invocations_reserved"], 0)
        self.assertEqual(projection["review_waves_reserved"], 0)
        again = self.reserve(
            self.policy(
                max_elapsed_seconds=5, max_invocations=1, max_review_waves=1
            ),
            elapsed=5,
            reviews=1,
        )
        self.assertEqual(again["projection"]["invocations_reserved"], 1)

    def test_active_reservation_blocks_another_admission(self):
        self.reserve(self.policy())

        with self.assertRaisesRegex(LineageError, "active reservation"):
            self.reserve(self.policy())

    def test_stale_hash_is_rejected_before_new_reservation(self):
        stale = self.policy()
        first = self.reserve(stale)
        self.settle(first, started=False, actual=0, session=None,
                    status="runner_error")

        with self.assertRaisesRegex(LineageError, "ledger hash mismatch"):
            self.reserve(stale)

    def test_ledger_freezes_lineage_and_maximum_budgets(self):
        first = self.reserve(self.policy())
        self.settle(first, started=False, actual=0, session=None,
                    status="runner_error")
        with self.assertRaisesRegex(LineageError, "frozen budgets"):
            self.reserve(self.policy(max_elapsed_seconds=101))
        with self.assertRaisesRegex(LineageError, "lineage root"):
            self.reserve(self.policy(lineage_root_id="lineage:other"))

    def test_worktrees_with_same_git_common_dir_share_ledger_key(self):
        repo = self.root / "repo"
        linked = self.root / "linked"
        state_root = self.root / "external-state"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "README.md").write_text("initial\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=Test",
             "-c", "user.email=test@example.invalid", "commit", "-qm", "init"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", "-b",
             "feature", str(linked)],
            check=True,
        )

        main_path = lineage_ledger_path(state_root, repo, "lineage:shared")
        linked_path = lineage_ledger_path(state_root, linked, "lineage:shared")

        self.assertEqual(main_path, linked_path)
        self.assertTrue(main_path.is_relative_to(state_root.resolve()))
        self.assertNotEqual(
            main_path, lineage_ledger_path(state_root, repo, "lineage:other")
        )

    def test_resume_requires_matching_interrupted_session_and_costs_invocation(self):
        first = self.reserve(self.policy(max_invocations=2))
        self.settle(first, status="interrupted", actual=1.2,
                    session="session-resume")
        resumed = self.reserve(
            self.policy(
                max_invocations=2,
                resume_from_reservation_id=first["reservation_id"],
                resume_session_id="session-resume",
            ),
            contract=self.contract_b,
            candidate=self.candidate_b,
        )

        self.assertEqual(resumed["projection"]["invocations_spent"], 1)
        self.assertEqual(resumed["projection"]["invocations_reserved"], 2)

    def test_resume_rejects_missing_noninterrupted_or_wrong_session_target(self):
        with self.assertRaisesRegex(LineageError, "resume reservation"):
            self.reserve(
                self.policy(
                    resume_from_reservation_id="e" * 64,
                    resume_session_id="missing",
                )
            )

        completed_ledger = self.root / "completed.jsonl"
        completed = self.reserve(self.policy(ledger=completed_ledger),
                                 ledger=completed_ledger)
        self.settle(completed, ledger=completed_ledger)
        with self.assertRaisesRegex(LineageError, "interrupted"):
            self.reserve(
                self.policy(
                    ledger=completed_ledger,
                    resume_from_reservation_id=completed["reservation_id"],
                    resume_session_id="session-1",
                ),
                ledger=completed_ledger,
            )

        interrupted_ledger = self.root / "interrupted.jsonl"
        interrupted = self.reserve(self.policy(ledger=interrupted_ledger),
                                   ledger=interrupted_ledger)
        self.settle(interrupted, ledger=interrupted_ledger,
                    status="interrupted", session="session-right")
        with self.assertRaisesRegex(LineageError, "session"):
            self.reserve(
                self.policy(
                    ledger=interrupted_ledger,
                    resume_from_reservation_id=interrupted["reservation_id"],
                    resume_session_id="session-wrong",
                ),
                ledger=interrupted_ledger,
            )

    def test_settlement_same_retry_is_idempotent_but_conflict_is_rejected(self):
        reservation = self.reserve(self.policy())
        first = self.settle(reservation, actual=1.2, session="session-idempotent")
        after_first = self.ledger.read_bytes()

        repeated = self.settle(
            reservation, actual=1.2, session="session-idempotent"
        )

        self.assertFalse(first["idempotent"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(self.ledger.read_bytes(), after_first)
        events = [json.loads(line) for line in after_first.splitlines()]
        self.assertEqual(len(events), 3)
        self.assertTrue(all(
            event["schema_version"] == "development-governor.lineage-ledger.v0"
            for event in events
        ))
        with self.assertRaisesRegex(LineageError, "conflicting settlement"):
            self.settle(
                reservation,
                status="stopped",
                actual=1.2,
                session="session-idempotent",
            )

    def test_started_noninterrupted_run_may_lack_observable_session_id(self):
        reservation = self.reserve(self.policy())

        settled = self.settle(
            reservation,
            status="stopped",
            started=True,
            actual=0.2,
            session=None,
        )

        self.assertEqual(settled["projection"]["invocations_spent"], 1)

    def test_actual_elapsed_overrun_is_charged_instead_of_stranding_reservation(self):
        reservation = self.reserve(
            self.policy(max_elapsed_seconds=10), elapsed=1
        )

        settled = self.settle(
            reservation,
            status="stopped",
            started=True,
            actual=1.2,
            session=None,
        )

        self.assertEqual(settled["projection"]["elapsed_seconds_spent"], 2)
        self.assertEqual(settled["projection"]["elapsed_seconds_remaining"], 8)


if __name__ == "__main__":
    unittest.main()
