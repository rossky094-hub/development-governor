# Changelog

All notable public changes are documented here.

## 0.1.0b4 - 2026-07-18

Deployment-parity and capsule-role hardening release.

- Reject task capsules whose mutable deliverables overlap immutable evidence inputs,
  including exact paths and ancestor/descendant directory relationships.
- Project the active runtime against an explicitly bound local Governor source
  checkout and deny external-project mutation when the runtime is stale.
- Fail closed when an explicitly bound source checkout disappears while preserving
  the Governor self-repair and explicit Owner-authorized upgrade routes.
- Preserve standalone installed-package behavior: no bound source means no remote
  version lookup, while content-addressed runtime integrity remains enforced.
- Keep the beta boundary: this is not an OS sandbox, semantic correctness oracle,
  Owner authenticator, or measured productivity claim.

## 0.1.0b3 - 2026-07-16

Deterministic hardening and Skill-boundary release.

- Bind task evidence inputs by content hash and reject stale evidence before start,
  isolated checks, and verification.
- Execute frozen checks and acceptance in disposable repository snapshots; fail closed
  when the Hook cannot establish an opaque command's write set.
- Add explicit, hash-bound policy migration and default-runtime upgrade receipts with
  managed projection integrity checks.
- Accept independently verified Skill candidates through project verification receipts;
  require `--allow-new-install` for an absent installation target.
- Separate high-stakes spec authoring, semantic drift analysis, acceptance review, and
  deterministic runtime control into non-overlapping roles.
- Preserve the experimental boundary: no production-readiness, measured savings, or
  Owner-authentication claim.

## 0.1.0b2 - 2026-07-16

Targeted verification repair.

- Return a non-zero CLI exit status when frozen verification reports
  `verification_failed`.
- Make the timeout/no-retry regression rely on controller receipt evidence instead
  of a timing-sensitive child-process marker.
- Preserve beta.1 as an immutable prerelease with its original failure receipt.

## 0.1.0b1 - 2026-07-16

First public beta of Development Governor.

- Added installable `governor` and `development-governor` console commands.
- Added a self-contained `governor demo` with zero model invocations.
- Added external project enrollment, task preparation, bounded leases, frozen
  acceptance execution, and deterministic closure.
- Added a Codex `PreToolUse` guard for recognized mutation paths.
- Preserved native multi-agent execution for declared independent lanes with
  independent acceptance IDs.
- Added default user-level Codex activation with reversible managed changes.
- Documented unsupported claims and the Hook/OS security boundary.
