# Development Governor beta.4 Runtime Parity Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a bounded beta.4 that carries the capsule role-disjointness repair and stops external project mutation when the active runtime is stale relative to its bound Governor source checkout.

**Architecture:** Keep the content-addressed runtime and explicit `default-upgrade` route. Add one pure activation-status projection in `default_activation.py`; the Hook consumes that projection after exempting the Governor repository itself and before authorizing an external project write. Standalone installs without a bound source checkout retain runtime-integrity checking and do not acquire a remote-version dependency.

**Tech Stack:** Python 3.9+, `unittest`, Git worktrees, existing Development Governor v0 state and CLI.

---

### Task 1: Carry the frozen capsule-role repair onto beta.4

**Files:**
- Modify: `src/development_governor/project_entry.py`
- Modify: `tests/test_project_entry.py`
- Modify: `tests/test_default_entry_integration.py`
- Modify: `docs/public/quickstart.md`

- [x] **Step 1: Cherry-pick the existing bounded repair**

Run:

```bash
git cherry-pick 11354cab56a0ef9c83d33509db09d7ef284a97b6
```

Expected: the exact/prefix overlap cases reject a mutable deliverable that is also declared immutable evidence.

- [x] **Step 2: Verify the focused regression**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest \
  tests.test_project_entry.ProjectEntryTests.test_prepare_rejects_deliverable_paths_overlapping_evidence_inputs -v
```

Expected: one test passes.

### Task 2: Project bound-source parity as deterministic state

**Files:**
- Modify: `src/development_governor/default_activation.py`
- Modify: `tests/test_default_activation.py`

- [x] **Step 1: Write a failing status-projection test**

Add a test that constructs an activation manifest with a bound source checkout and asserts:

```python
self.assertEqual(bound_source_runtime_status(manifest)["status"], "current")
(source / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
self.assertEqual(bound_source_runtime_status(manifest)["status"], "upgrade_required")
shutil.rmtree(source)
self.assertEqual(bound_source_runtime_status(manifest)["status"], "source_unavailable")
```

- [x] **Step 2: Run RED**

Run the single test and confirm it fails because `bound_source_runtime_status` does not exist.

- [x] **Step 3: Implement the minimal projection**

Add:

```python
def bound_source_runtime_status(manifest):
    identity = manifest.get("governor_project_identity")
    runtime = manifest.get("runtime")
    if not isinstance(identity, Mapping) or not isinstance(runtime, Mapping):
        return {"status": "unbound"}
    repo_path = identity.get("repo_path")
    runtime_hash = runtime.get("package_hash")
    if not isinstance(repo_path, str) or not isinstance(runtime_hash, str):
        raise ActivationError("activation manifest is missing bound source metadata")
    source_package = Path(repo_path) / "src" / "development_governor"
    try:
        available_hash = _package_hash(source_package)
    except (ActivationError, OSError):
        return {"status": "source_unavailable", "current_runtime_hash": runtime_hash}
    return {
        "status": "current" if available_hash == runtime_hash else "upgrade_required",
        "current_runtime_hash": runtime_hash,
        "available_runtime_hash": available_hash,
    }
```

- [x] **Step 4: Run GREEN**

Run the focused test, then all default-activation tests.

### Task 3: Deny external writes under stale runtime

**Files:**
- Modify: `src/development_governor/hook_guard.py`
- Modify: `tests/test_hook_guard.py`

- [x] **Step 1: Write the failing Hook counterexample**

Create a bound source package and active subject-project lease. Assert a matching source permits an in-scope patch, changing the source produces a deny containing `runtime upgrade`, and removing the source also denies. Preserve the existing Governor-repository self-repair exemption.

- [x] **Step 2: Run RED**

Run only the new Hook test. Expected: the stale source mutation is incorrectly allowed.

- [x] **Step 3: Implement the minimal Hook gate**

Import `bound_source_runtime_status`. After canonical project identity and the Governor-repository exemption:

```python
source_status = bound_source_runtime_status(activation)
if source_status["status"] == "upgrade_required":
    return _deny(
        "Active Development Governor runtime is stale relative to its bound source; "
        "run default-upgrade from the approved Governor source checkout with explicit "
        "Owner authority before external project mutation."
    )
if source_status["status"] == "source_unavailable":
    return _deny(
        "Bound Development Governor source is unavailable; restore the approved "
        "source checkout or explicitly rebind and upgrade before external project mutation."
    )
```

- [x] **Step 4: Run GREEN**

Run the focused Hook test and the complete Hook test module.

### Task 4: Package the bounded beta.4

**Files:**
- Modify: `pyproject.toml`
- Modify: `tests/test_public_release.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Create: `docs/releases/v0.1.0-beta.4.md`

- [x] **Step 1: Write the failing package-version assertion**

Change the public release test to require `version = "0.1.0b4"`; run it and confirm RED while metadata remains beta.3.

- [x] **Step 2: Update package and public documentation**

Set version `0.1.0b4`, installation tag `v0.1.0-beta.4`, add the runtime-parity and capsule-role changes, and retain explicit boundaries: experimental, not an OS sandbox, no semantic-correctness claim, and Owner references are not authenticated.

- [x] **Step 3: Run package tests**

Run `tests.test_public_release` and confirm GREEN.

### Task 5: Verify, deploy, and publish

**Files:**
- No new source files.

- [x] **Step 1: Run full local regression**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Expected: 146 tests, zero failures.

- [ ] **Step 2: Run frozen Governor acceptance**

Run `governor verify` and require core, ops, and regression receipts to pass; then `governor close`.

- [ ] **Step 3: Upgrade the global runtime under the current Owner authorization**

Run beta.4 source `default-upgrade` with `codex-user-turn-2026-07-18:进行修改并发布`, then verify the active manifest package hash equals the beta.4 source hash and the overlap counterexample is rejected by the installed runtime.

- [ ] **Step 4: Publish through GitHub**

Push `agent/beta4-runtime-parity`, open a ready PR, wait for CI, merge only if green, create annotated tag `v0.1.0-beta.4`, and create a prerelease whose notes preserve the beta boundary.
