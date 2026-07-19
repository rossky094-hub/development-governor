# Project Review Finalization and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `systematic-debugging`, `test-driven-development`, and `executing-plans`. Execute inline as one serial Governor task; do not dispatch duplicate reviewer or probe lanes.

**Goal:** Preserve a schema-valid completed Spec review when Codex reports token usage only in `turn.completed`, and recover existing review artifacts without another model call.

**Architecture:** Separate result validity from token-budget accounting. The supervisor classifies token telemetry as `streaming`, `terminal_only`, or `unavailable`; only streaming telemetry can enforce a live cap. Project review terminalization validates a completed output independently, records artifact/review/budget axes, and an append-only recovery command validates historical workspaces without rewriting their terminal receipts or lineage ledgers.

**Tech Stack:** Python 3.9+, standard library, `unittest`, existing JSONL Codex runner and content-addressed review workspace.

---

### Task 1: Reproduce terminal-only finalization

**Files:** `tests/test_project_review.py`, `tests/test_supervisor.py`

- [x] Add a fake Codex run that writes a schema-valid output-last-message and then emits over-cap usage only in `turn.completed`.
- [x] Assert the supervisor does not convert terminal accounting into a stop signal.
- [x] Assert project review retains the valid verdict and records separate artifact, validation, and budget states.
- [x] Run the two focused tests and confirm RED against the pre-fix implementation.

### Task 2: Implement telemetry capability and three-axis terminal state

**Files:** `src/development_governor/supervisor.py`, `src/development_governor/runner.py`, `src/development_governor/project_review.py`

- [x] Classify JSONL token visibility as `streaming`, `terminal_only`, or `unavailable`.
- [x] Enforce token caps only under streaming telemetry; keep elapsed and process-group controls hard.
- [x] Add closed artifact, review-validation, and budget projections to project-review receipts.
- [x] Run supervisor and project-review tests; require GREEN.

### Task 3: Recover a completed historical review without a model call

**Files:** `src/development_governor/project_review.py`, `src/development_governor/cli.py`, `src/development_governor/__init__.py`, `tests/test_project_review.py`

- [x] Add a RED historical-workspace test with `interrupted/review:null` terminal state and a matching valid final model output.
- [x] Bind contract, review identity, batch, context tree, schema, session, token usage, repository nonmutation, raw final agent message, and output-last-message.
- [x] Write an immutable, idempotent `review-recovery-receipt.json`; never edit the original terminal receipt or lineage ledger.
- [x] Add `governor recover-review CONTRACT --output-dir DIR`, with no Codex/model parameter.
- [x] Run focused API and CLI tests; require GREEN.

### Task 4: Public boundary and acceptance

**Files:** `README.md`, this plan

- [x] Document that gross observed tokens are not paid-token equivalence and terminal-only usage is accounting-only.
- [x] Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_supervisor tests.test_project_review -v`.
- [x] Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -q`.
- [x] Run frozen Governor acceptance `beta3-regression`, then close the task.

### Frozen boundaries

- No global runtime upgrade, push, merge, tag, publication, V6 Spec mutation, or new model review is authorized in this slice.
