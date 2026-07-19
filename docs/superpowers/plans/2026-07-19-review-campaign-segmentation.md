# Project Review Campaign Identity and Segmentation Plan

> **Execution rule:** One serial TDD implementation lane. Do not launch semantic reviewers while developing this controller slice.

**Goal:** Make review budget lineage non-resettable by caller-selected IDs and replace unbounded in-agent scope fan-out with controller-managed, independently checkpointed review segments.

**Architecture:** A deterministic campaign ID is derived from the canonical Git identity, frozen candidate hash, normalized acceptance targets, and exact Owner review authorization reference. Segmented review uses a two-level graph: at least two independent segments plus exactly one cross-scope join segment. The Governor starts one model process per ready segment, may run independent segments concurrently up to the declared cap, writes immutable segment checkpoints, skips already valid checkpoints on recovery, and produces a zero-model deterministic aggregate only after every segment is valid.

**Non-goals:** No semantic review by the Governor, no Owner authentication, no automatic Owner acceptance, no arbitrary workflow engine, no global runtime upgrade, push, merge, tag, or publication.

## Task 1: Freeze campaign identity

- [x] Add RED tests proving callers cannot reset a frozen review budget with a new `lineage_root_id`.
- [x] Bind the derived campaign to canonical Git identity, candidate hash, normalized targets, and exact Owner review authorization ref.
- [x] Keep legacy lineage parsing recovery-only; it cannot launch a model.
- [x] Add the zero-model `review-campaign-id` helper.

## Task 2: Account for batched segment invocations

- [x] Reserve an explicit positive invocation count in one lineage reservation.
- [x] Settle only the number of model processes actually started.
- [x] Preserve one-invocation defaults and replay compatibility.

## Task 3: Minimal segmented protocol

- [x] Close segment definitions over kind, identity, context, dependencies, and budgets.
- [x] Require at least two independent segments and one join over all independents.
- [x] Run ready independent segments concurrently under the controller cap; disable descendants inside each segment.
- [x] Write hash-bound checkpoints only for valid segments and supply them as files to the join.
- [x] On retry, skip valid checkpoints and run only missing segments and unresolved dependants.

## Task 4: Deterministic aggregation

- [x] Aggregate only complete, revalidated checkpoints with fixed verdict precedence.
- [x] Preserve every finding with its segment identity and keep `owner_decision` as the next move.
- [x] Return no aggregate when any segment is incomplete.

## Task 5: Verification

- [x] Prove two independent segments, one join, and a zero-model second invocation with fake Codex processes.
- [x] Prove one failed segment reruns without changing the completed independent checkpoint.
- [x] Run focused review and lineage modules.
- [x] Run the full test suite before final documentation.
- [x] Run frozen Governor acceptance `beta3-regression`, then close the task.

## Frozen acceptance

- Acceptance ID: `beta3-regression`
- Command: `/usr/bin/env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v`
- No semantic reviewer, global runtime upgrade, push, merge, tag, or publication is authorized in this slice.
