# Development Governor

[![CI](https://github.com/rossky094-hub/development-governor/actions/workflows/ci.yml/badge.svg)](https://github.com/rossky094-hub/development-governor/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Development Governor is an experimental, deterministic control layer for bounded
Codex development work. An LLM can propose and implement a change; the Governor
decides whether the project is enrolled, whether a scoped lease is active, which
paths may change, which acceptance command is authoritative, and whether the slice
can close.

It was built for a practical failure mode: capable coding agents can spend hours in
repeated review, test, restart, and documentation loops while producing little new
product evidence.

> 中文摘要：这是一个面向 Codex 开发任务的实验性监督器。它把 Owner 授权、可写
> 路径、独立验收、尝试/时长/评审预算和多智能体拓扑冻结在模型之外；它不是通过
> 禁止多智能体来换取安全，也不声称能拦截操作系统中的所有写入。

## Try it in two commands

Requires Python 3.9+ and Git.

```bash
pipx install git+https://github.com/rossky094-hub/development-governor.git@v0.1.0-beta.4
governor demo
```

`uv tool install` works with the same Git URL. A virtual environment also works:

```bash
python3 -m venv .venv
.venv/bin/pip install git+https://github.com/rossky094-hub/development-governor.git@v0.1.0-beta.4
.venv/bin/governor demo
```

The demo invokes no model and does not read or modify your active Governor state. It
creates a temporary Git repository and proves one complete transition:

```json
{
  "allowed_mutation": "allowed",
  "blocked_mutation": "denied_outside_scope",
  "closure": "closed",
  "enrollment": "enrolled",
  "lease": "active",
  "model_invocations": 0,
  "status": "demo_passed",
  "verification": "verification_passed"
}
```

## Control flow

```text
Owner policy ──> enroll ──> task capsule ──> prepare ──> start
                                                        │
                         allowed scoped mutation <──────┤
                         denied out-of-scope mutation <─┤
                                                        │
                     frozen acceptance ──> verify ──> close
```

The canonical project route is:

```bash
governor enroll /outside/project-policy.json
governor migrate-policy /outside/replacement-policy.json \
  --expected-policy-hash <current-policy-hash> \
  --owner-authorization-ref <external-owner-reference>
governor prepare /outside/task-capsule.json
governor start <task-hash>
governor status --repo /path/to/project
governor check --repo /path/to/project -- python3 -m unittest -q
governor verify --repo /path/to/project
governor close --repo /path/to/project
```

Policies and task capsules are stored outside the governed repository. Acceptance
commands are argv arrays executed with `shell=False` in disposable repository
snapshots; declared acceptance files and task evidence inputs are content-hash checked.

See the [five-minute quickstart](docs/public/quickstart.md) and the annotated
[policy](examples/project-policy.example.json) and
[task capsule](examples/task-capsule.example.json) templates.

### Project-aware Spec review

The optional `review-spec` route launches a dedicated reviewer agent; the Governor
does not issue the semantic verdict itself. The contract supplies a hash-bound frozen
candidate, closed project context, external reviewer Skill, one review-wave lineage,
and explicit acceptance target scopes:

```bash
governor review-spec /outside/project-review-contract.json \
  --output-dir /outside/project-review-run
```

The reviewer runs against a materialized read-only context and returns one
schema-bound receipt for an external Owner decision. Serial review disables native
multi-agent execution. Parallel review requires at least two independently identified
review scopes and acceptance IDs; it is not created merely to add read-only probes.
See the [project-aware review guide](docs/public/project-aware-spec-review.md) and
[contract example](examples/project-review-capsule.example.json).

## What it controls

- External, append-only project policy and task state.
- A bounded lease before supported Codex mutation paths may write.
- Exact deliverable paths and protected acceptance material.
- Frozen acceptance IDs, commands, file hashes, and content-bound evidence inputs.
- Fail-closed handling for opaque shell commands plus a non-promoting isolated check
  entry for tests and other commands whose write set cannot be inferred.
- Attempts, elapsed time, review waves, and declared agent limits.
- Serial TDD slices with no extra probe/reviewer lanes.
- Native multi-agent work when two or more lanes have disjoint deliverables and
  independent acceptance IDs.
- Optional root-process supervision, product-change deadlines, and observed token
  caps when telemetry is actually available.
- One hash-bound, project-aware Spec reviewer process with a machine-validated receipt,
  one-wave lineage accounting, and same-session interruption recovery.

## What it does not control

- It is not an OS sandbox or a replacement for Git, CI, containers, or human review.
- The Codex `PreToolUse` Hook covers supported, recognizable mutation paths; manual
  edits, other applications, unhooked MCP tools, and future Codex behavior may bypass
  it.
- Snapshot execution protects the governed repository from ordinary relative writes;
  it does not confine malicious absolute writes to unrelated host paths.
- It cannot guarantee that a specification is correct or that an accepted change is
  useful.
- It validates reviewer context, topology, budget, nonmutation, and receipt identity;
  the dedicated reviewer agent, not the Governor, owns the semantic verdict.
- Owner references are preserved and audited but not cryptographically authenticated.
- Token telemetry is optional and may be unavailable. This beta makes no measured
  cost-saving, quality-improvement, or productivity claim.
- It does not make every task single-agent. Parallelism is admitted by independently
  verifiable lanes, not disabled globally.

Read the full [control boundary and threat model](docs/public/control-boundary.md).
The separate [Governor / Skill responsibility contract](docs/public/governor-skill-responsibility-contract.md)
defines why semantic authoring and review Skills never acquire leases or Owner authority.

## Optional default Codex entry

After reviewing the managed changes, install the user-level routing rule and Hook:

```bash
governor default-enable
```

This adds a marked block to `~/.codex/AGENTS.md`, merges one `PreToolUse` handler into
`~/.codex/hooks.json`, and installs a stable local runtime under
`~/.codex/development-governor/v0/`. It does not launch a model. Disable it with:

If a newer source package is available, `default-enable` returns
`upgrade_required` instead of silently replacing the active runtime. Upgrade only
under an explicit external Owner reference:

```bash
governor default-upgrade \
  --owner-authorization-ref <external-owner-reference>
```

The upgrade verifies the existing managed AGENTS/Hook projections and stable
launcher hashes, preserves unrelated configuration, installs a content-addressed
runtime, and writes an upgrade receipt. When activation is explicitly bound to a
Governor source checkout, the Hook also denies external-project writes if that source
has advanced past the active runtime or is unavailable. The Governor repository itself
and the explicit upgrade route remain available for recovery. Disable it with:

```bash
governor default-disable
```

## Status

`v0.1.0-beta.4` is a public experiment. The deterministic kernel and default-entry
path have local test coverage; live savings and broad compatibility have not been
established. Please report a minimal reproduction through
[GitHub Issues](https://github.com/rossky094-hub/development-governor/issues).

## Development

```bash
git clone https://github.com/rossky094-hub/development-governor.git
cd development-governor
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m development_governor demo
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[CHANGELOG.md](CHANGELOG.md). Licensed under Apache-2.0.
