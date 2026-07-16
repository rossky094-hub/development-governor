# Changelog

All notable public changes are documented here.

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
