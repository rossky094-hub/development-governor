# Governor / Skill Responsibility Contract

> Owner authorization: `codex-user-turn-2026-07-16:认可内容并在本对话全部执行`
>
> Product status: experimental deterministic runtime control layer; not production-ready.

## One rule

Deterministic code controls execution state. Skills may create or analyze semantic
artifacts, but they never acquire operational authority by describing it.

## Responsibility boundaries

| Actor | May produce | Must not produce |
|---|---|---|
| Development Governor | enrollment, content-bound task identity, lease decisions, isolated check/verification receipts, policy/runtime migration receipts, close/abort state | semantic correctness, product value, Owner identity authentication, reviewer verdicts |
| `authoring-high-stakes-specs` | a draft candidate, typed contract graph, explicit unknowns, failure/recovery closure | lease, commit permission, acceptance ID, Owner authority, review verdict |
| `controlling-design-drift` | read-only findings, affected scopes, safe work remaining, missing obligations, recommended route | mutable ledgers, budgets, fuse state, `allow_controller_commit`, acceptance or stop authority |
| `reviewing-specs-before-acceptance` | one hash-bound read-only verdict for declared acceptance targets | candidate repair, automatic re-review, implementation authorization |
| Owner | acceptance, route, exception, policy migration, runtime upgrade | none of these powers may be inferred from a non-empty reference alone |

The Governor preserves Owner references but does not authenticate the principal behind
them. Public claims use **authority-preserving**, never **authority-authenticating**.

## Two routes, not one universal workflow

A bounded internal Product Slice goes from Governor capsule directly to TDD, isolated
verification, and close. High-stakes specification work uses a separate authoring
capsule; the drift analyzer runs only when its trigger applies; a frozen candidate may
then receive one budgeted acceptance review and an external Owner decision. A later
implementation uses a new Governor capsule. Authoring, review, and implementation do
not share one continuous lease.

## Execution boundary

The Hook statically admits only mutations whose target paths it can establish. An
opaque shell command is denied under an active lease; it may be used only through the
Governor's isolated check entry, which runs against a disposable repository snapshot
and never copies changes back.

`verify` likewise executes every frozen acceptance command in a fresh disposable
snapshot. The governed worktree is an input, not the acceptance process working
directory. This prevents ordinary relative writes from modifying the governed
worktree. It is not an OS sandbox: malicious code that targets unrelated absolute host
paths still requires a container, VM, or operating-system sandbox.

Evidence inputs are immutable file records `{path, sha256}`. `prepare` validates the
declared digest, and `start`, isolated check, and `verify` reject stale evidence.
Directories must be represented by a separately hash-bound manifest file.

## Operational migrations

Policy migration requires the exact current policy hash, a non-empty external Owner
reference, no active lease, a valid replacement policy for the same Git identity, and
an immutable migration receipt. Runtime upgrade similarly requires an explicit Owner
reference, validates the currently managed AGENTS block and Hook group, installs a
content-addressed runtime, preserves unrelated user content, and records history.

Skill promotion accepts a hash-current complete run receipt or a project verification receipt
whose checks ran in isolated snapshots and produced Product Evidence. Replacement remains
the default. Creating an absent installed Skill requires the explicit
`promote-skill --allow-new-install` flag, and the terminal result records
`install_mode: new`; otherwise it fails closed. Promotion preserves supplied authority
references but does not authenticate the Owner or reviewer behind them.

## Frozen beta.3 acceptance

1. Changed evidence cannot start the old task; updating the declared digest changes
   the task hash.
2. An acceptance command may write and exit zero inside its snapshot while the governed
   repository remains byte-for-byte unchanged.
3. Opaque shell execution is denied by the Hook; isolated check remains usable and does
   not mutate the governed repository.
4. Runtime and policy migrations are explicit, hash-bound, lease-safe, and integrity
   checked.
5. A new Skill installation is impossible without an explicit flag and an external,
   hash-bound successful terminal receipt.

Passing these conditions does not prove that a specification is correct, the work is
valuable, or the Owner made a good decision.
