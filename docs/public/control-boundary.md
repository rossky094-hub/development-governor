# Control boundary and threat model

## Enforced by deterministic code

- Enrollment and task state live outside the governed repository.
- One active lease owns the Git common-directory identity shared by linked worktrees.
- Attempt, elapsed, review-wave, parallel-agent, and total-agent maxima are validated
  against the enrolled policy.
- Task deliverables must remain under enrolled allowed paths and outside protected
  paths.
- Task evidence is stored as `{path, sha256}` and rechecked before lease activation,
  isolated checks, and verification.
- Verification resolves only pre-enrolled acceptance IDs, rechecks declared file
  hashes, and executes argv arrays with `shell=False` in fresh disposable snapshots.
- A verified task closes; an explicit Owner abort records a distinct terminal state.
- Policy replacement requires the exact enrolled policy hash, the same Git identity,
  an external Owner reference, no active lease, and a migration receipt.
- Default-runtime replacement is explicit: managed AGENTS/Hook projections and stable
  launcher hashes are checked before a content-addressed upgrade is installed and
  recorded. Unrelated configuration is outside the managed projection.
- The supported Codex `PreToolUse` Hook denies recognizable mutations without a valid
  lease, rejects recognizable paths outside the task scope, and fails closed for an
  active-lease shell command whose write set cannot be established. Such commands may
  use the isolated, non-promoting `governor check` entry.
- `review-spec` binds the candidate, closed project context, external reviewer Skill,
  acceptance targets, agent topology, and one-wave lineage by content identity. It
  copies only those files into a separate context, launches a read-only Codex reviewer,
  probes the governed Git worktree for changes, and validates one schema-bound receipt.
- Initial review workspace failures occur before reservation when predictable. An
  unexpected materialization failure is settled as model-not-started and charges no
  invocation, elapsed time, or review wave. An interrupted session can resume only the
  same frozen review identity and batch.

## Intentionally not claimed

The Hook is not a mandatory access-control system. It cannot intercept all macOS or
Linux writes, manual editor changes, other agents or applications, an MCP server that
does not pass through the Hook, or a future Codex mutation surface the parser does not
recognize. Malformed Hook envelopes still return a diagnostic rather than pretending
that an event was securely evaluated. An opaque command recognized as mutation-capable
is denied, but parsing is not an operating-system security boundary.

The disposable snapshot is not a container or mandatory access-control sandbox. It
prevents ordinary relative writes by changing the command working tree and never
copying results back. Code that deliberately writes to unrelated absolute host paths
still requires a VM, container, or OS sandbox.

The root-run supervisor can terminate its process group after a deterministic limit,
but it cannot recover compute already spent or prove that work was valuable. Observed
token limits only apply when unambiguous telemetry is present; unavailable telemetry
does not become an estimated cost signal.

The Governor preserves authority references but does not authenticate their principals.
It separates authority and evidence, and cannot determine whether the Owner
chose the right outcome, whether the acceptance test encodes the right product value,
or whether a malicious acceptance program is safe to execute. Run untrusted code in a
real sandbox or disposable environment.

The project-aware reviewer is still an LLM. The Governor can prove which files and
Skill version were supplied and whether the receipt matches the frozen identity; it
cannot prove that the reviewer understood the project, found every defect, or issued
the correct semantic verdict. A Git status probe can detect persistent governed-tree
changes but is not a complete audit of transient or absolute-path writes.

## Multi-agent boundary

A serial TDD slice has one implementation lane and no automatic read-only probes or
duplicate reviewers. A parallel capsule must declare at least two lanes with disjoint
deliverables and acceptance IDs. That validates the root topology; native Codex worker
spawn details remain partly dependent on the current Codex runtime.

The same rule applies to Spec review. Serial review hard-disables `multi_agent`.
Parallel review requires at least two declared scopes with unique acceptance IDs, and
the terminal receipt must close every scope. The Governor validates this declared
topology and receipt, but does not claim an OS-level count of every runtime worker.

## Experimental success criterion

This beta is useful only if controlled projects show fewer repeated attempts or review
waves without lower acceptance quality. Repository stars, clone counts, document
volume, and Governor events are not product-quality evidence by themselves.
