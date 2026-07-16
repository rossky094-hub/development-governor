# Development Governor beta.3 Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `test-driven-development`. Execute this plan inline as serial, independently accepted Governor tasks; do not dispatch reviewer or probe lanes.

**Goal:** Close the three beta.2 enforcement gaps, add explicit operational migration, and reduce the upstream Skills to non-overlapping semantic roles.

**Architecture:** Task evidence becomes content-addressed. Opaque shell commands fail closed and tests run through a disposable-snapshot check entry. Frozen acceptance also runs in a fresh snapshot. Runtime and policy changes use explicit hash-bound migrations; Skills remain outside the deterministic authority plane.

**Tech Stack:** Python 3.9+, standard library, Git, `unittest`, Codex Agent Skills.

---

### Task 1: Bind evidence content

**Files:**
- Modify: `src/development_governor/project_entry.py`
- Modify: `tests/test_project_entry.py`
- Modify: `src/development_governor/public_demo.py`
- Modify: `examples/task-capsule.example.json`

- [ ] Add a failing test that supplies `evidence_inputs` as `{path, sha256}`, changes the file after `prepare`, and expects `start` to reject `evidence input hash mismatch`.
- [ ] Run `PYTHONPATH=src python3 -m unittest tests.test_project_entry -v` and confirm the v1 capsule is rejected before implementation.
- [ ] Change `TASK_SCHEMA` to `development-governor-task-capsule.v1`; validate non-empty file records and lowercase SHA-256; recompute the file hash at `prepare` and before `start`, check, and verify.
- [ ] Update all built-in producers and fixtures to emit v1 evidence records.
- [ ] Rerun the focused test and commit only after GREEN.

### Task 2: Isolate verification

**Files:**
- Modify: `src/development_governor/project_entry.py`
- Modify: `tests/test_project_entry.py`

- [ ] Add a failing acceptance fixture that overwrites `src/app.py`, creates a new file, and exits zero; assert the source repository remains unchanged.
- [ ] Run the focused test and confirm the current implementation mutates the source repository.
- [ ] Implement a deterministic repository snapshot containing tracked and untracked non-ignored files, excluding Git administration state; execute each acceptance in a fresh temporary snapshot.
- [ ] Add `execution_mode: isolated_snapshot` to each result and never copy snapshot changes back.
- [ ] Rerun the focused test and existing verification lifecycle tests.

### Task 3: Fail closed for opaque commands and add isolated checks

**Files:**
- Modify: `src/development_governor/hook_guard.py`
- Modify: `src/development_governor/project_entry.py`
- Modify: `src/development_governor/cli.py`
- Modify: `tests/test_hook_guard.py`
- Modify: `tests/test_project_entry.py`
- Modify: `tests/test_default_entry_integration.py`

- [ ] Replace the existing green test for unnamed test commands with a failing expectation that active-lease opaque shell execution is denied.
- [ ] Confirm RED against the current Hook.
- [ ] Deny mutation-capable shell commands when no explicit path set can be established.
- [ ] Add `run_isolated_check(repo, argv)` and CLI `governor check --repo PATH -- ARGV`; require an active lease and fresh evidence, run in a disposable snapshot, and return a receipt without closing the task.
- [ ] Verify explicit in-scope patch/direct-path commands remain admitted.

### Task 4: Align public boundary and regression

**Files:**
- Modify: `README.md`
- Modify: `docs/public/control-boundary.md`
- Modify: `docs/public/quickstart.md`
- Modify: `src/development_governor/public_demo.py`

- [ ] Document authority-preserving language, snapshot isolation, opaque-command denial, and the remaining OS-sandbox boundary.
- [ ] Run the frozen external `beta3_core_acceptance.py` and require `BETA3_CORE_ACCEPTANCE=PASS`.
- [ ] Run all unit tests with `PYTHONPATH=src`; require zero failures.
- [ ] Commit the core slice, run `governor verify`, then `governor close` before starting operations work.

### Task 5: Add policy and runtime migrations

**Files:**
- Modify: `src/development_governor/project_entry.py`
- Modify: `src/development_governor/default_activation.py`
- Modify: `src/development_governor/cli.py`
- Modify: `tests/test_project_entry.py`
- Modify: `tests/test_default_activation.py`
- Modify: `tests/test_default_entry_integration.py`

- [ ] Write failing tests for active-lease migration rejection, stale expected policy hash, immutable migration receipt, runtime drift detection, explicit upgrade, unrelated-content preservation, and managed-block tamper detection.
- [ ] Implement `migrate-policy` with exact old hash, same project identity, Owner reference, no active lease, atomic replacement, and receipt history.
- [ ] Implement `default-upgrade` with managed-block/Hook integrity validation, new content-addressed runtime installation, atomic manifest replacement, and history.
- [ ] Run frozen `beta3_ops_acceptance.py` and full regression; close the operations lease.

### Task 6: Refactor and create Skills one at a time

**Files:**
- Replace candidate: `controlling-design-drift/SKILL.md`
- Amend candidate: `reviewing-specs-before-acceptance/SKILL.md`
- Create candidate: `authoring-high-stakes-specs/SKILL.md`

- [ ] Snapshot each installed Skill and create RED pressure scenarios before editing it.
- [ ] Reduce `controlling-design-drift` to findings, affected scopes, safe work, missing obligations, and recommendations; remove Controller, budget, fuse, and commit authority.
- [ ] Record v4-002 as incomplete and superseded rather than manufacturing a green receipt.
- [ ] Keep spec review independent, but require explicit acceptance targets and Owner-reserved review budget before high-stakes independent scopes.
- [ ] Create the authoring Skill with draft-only authority and three pressure scenarios covering self-acceptance, invented authority, and unnecessary use on a bounded Product Slice.
- [ ] Promote each Skill only after its own hash-bound eval receipt; never batch unverified Skills.

### Task 7: Integrate, publish, and activate

**Files:**
- Modify: `pyproject.toml`
- Modify: `CHANGELOG.md`
- Create: `docs/releases/v0.1.0-beta.3.md`
- Modify: `tests/test_public_release.py`

- [ ] Run one real high-stakes-spec flow through authoring, optional drift analysis, freeze, bounded review, Owner-decision boundary, and a separate Governor implementation capsule without executing product mutation.
- [ ] Verify wheel installation, `governor demo`, frozen core/ops acceptance, full unit regression, and Git cleanliness.
- [ ] Publish `v0.1.0-beta.3` only after CI succeeds; never move beta.1 or beta.2 tags.
- [ ] Run `default-upgrade` against the verified beta.3 source with this Owner authorization, confirm `governor demo`, and archive the upgrade receipt.
