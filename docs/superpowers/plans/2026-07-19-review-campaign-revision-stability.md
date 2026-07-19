# Review Campaign Revision-Stability Repair

**Goal:** Prevent candidate content edits or path moves from creating a fresh review budget while preserving exact candidate identity in every review batch.

## TDD route

- [x] Add RED evidence that candidate content changes produced a different campaign.
- [x] Add RED evidence that moving the candidate path also produced a different campaign.
- [x] Derive campaign identity from canonical Git identity, stable review scope ID, normalized acceptance targets, and exact Owner review authorization reference.
- [x] Exclude candidate path, candidate hash, model, prompt, context, and output path from campaign identity while retaining them in batch identity and receipts.
- [x] Prove a revised candidate enters the same ledger and triggers the existing Owner revision-reference control.
- [x] Update public boundary wording.
- [x] Run focused and full regression, frozen `beta3-regression`, then close.

**Non-goals:** No schema expansion, new workflow, semantic reviewer, global runtime upgrade, push, merge, tag, or publication.
