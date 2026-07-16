# Control boundary and threat model

## Enforced by deterministic code

- Enrollment and task state live outside the governed repository.
- One active lease owns the Git common-directory identity shared by linked worktrees.
- Attempt, elapsed, review-wave, parallel-agent, and total-agent maxima are validated
  against the enrolled policy.
- Task deliverables must remain under enrolled allowed paths and outside protected
  paths.
- Verification resolves only pre-enrolled acceptance IDs, rechecks declared file
  hashes, and executes argv arrays with `shell=False`.
- A verified task closes; an explicit Owner abort records a distinct terminal state.
- The supported Codex `PreToolUse` Hook denies recognizable mutations without a valid
  lease and rejects recognizable paths outside the task scope.

## Intentionally not claimed

The Hook is not a mandatory access-control system. It cannot intercept all macOS or
Linux writes, manual editor changes, other agents or applications, an MCP server that
does not pass through the Hook, or a future Codex mutation surface the parser does not
recognize. Ambiguous Hook input fails open with a diagnostic to avoid pretending that
an unevaluated action was securely blocked.

The root-run supervisor can terminate its process group after a deterministic limit,
but it cannot recover compute already spent or prove that work was valuable. Observed
token limits only apply when unambiguous telemetry is present; unavailable telemetry
does not become an estimated cost signal.

The Governor separates authority and evidence. It cannot determine whether the Owner
chose the right outcome, whether the acceptance test encodes the right product value,
or whether a malicious acceptance program is safe to execute. Run untrusted code in a
real sandbox or disposable environment.

## Multi-agent boundary

A serial TDD slice has one implementation lane and no automatic read-only probes or
duplicate reviewers. A parallel capsule must declare at least two lanes with disjoint
deliverables and acceptance IDs. That validates the root topology; native Codex worker
spawn details remain partly dependent on the current Codex runtime.

## Experimental success criterion

This beta is useful only if controlled projects show fewer repeated attempts or review
waves without lower acceptance quality. Repository stars, clone counts, document
volume, and Governor events are not product-quality evidence by themselves.
