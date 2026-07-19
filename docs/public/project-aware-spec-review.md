# Project-aware Spec review

`governor review-spec` launches one dedicated Codex reviewer for one frozen Spec
candidate. The Governor does not review the Spec. It freezes the review identity,
materializes the declared project context and external reviewer Skill, launches the
reviewer, accounts for the review wave, and validates the terminal receipt.

```text
Owner review authorization
        |
        v
external review contract --hashes--> materialized read-only context
        |                                      |
        |                                      v
        +----------------------------> dedicated reviewer agent
                                               |
                                               v
                                  schema-bound review receipt
                                               |
                                               v
                                         Owner decision
```

The semantic verdict belongs to the reviewer agent. Owner acceptance and any later
implementation authorization remain external decisions.

## Frozen inputs

The contract binds all of the following before the model starts:

- the candidate path and SHA-256;
- an explicit project goal and parent baseline;
- any additional contracts, decisions, authority records, evidence, or open
  obligations needed to understand the project;
- an external `reviewing-specs-before-acceptance`-compatible Skill bundle, including
  `SKILL.md`, `references/gate-catalog.md`, and
  `templates/spec-review-receipt.md`;
- the acceptance target scope IDs, review mode, and Owner review authorization
  reference;
- one lineage budget, agent topology, model, reasoning effort, and elapsed/token
  limits.

The lineage root is not a free label. `review-campaign-id` derives it from the
canonical Git common directory, frozen candidate hash, normalized acceptance targets,
and exact Owner review authorization reference. The derivation intentionally excludes
prompt wording, model choice, context-pack composition, and output path so those
execution details cannot reset the review-wave budget.

Conversation history is not a valid context role. Unlisted project files are not
copied into the review workspace. The output directory must be outside the governed
repository and must not already exist for an initial run.

See [`examples/project-review-capsule.example.json`](../../examples/project-review-capsule.example.json).
Replace every placeholder with an actual absolute path, external authority reference,
and current lowercase SHA-256. For a new lineage, the SHA-256 of an empty ledger is
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
After the first reservation, use the current ledger hash reported by the preceding
terminal receipt.

Run one review:

```bash
governor review-spec /outside/project-review-contract.json \
  --output-dir /outside/project-review-run
```

Before writing the lineage block for a new contract, obtain its required root and
current ledger digest without launching a model:

```bash
governor review-campaign-id /outside/project-review-contract.json
```

The command returns exit status `0` only when the reviewer process completed and its
receipt passed deterministic identity and shape validation. A semantic
`major_revision_required` verdict is still a successfully completed review; it is not
Owner acceptance.

## Review modes and budget

`full` is the first review of a frozen candidate. `incremental` is allowed only when
the contract also binds an Owner revision reference plus four impact artifacts:

- the prior review receipt;
- a trusted candidate diff;
- a dependency map;
- a prior-finding disposition map.

The lineage must default to exactly one review wave. A serial reviewer interruption
may resume the same Codex session, materialized context, batch ID, and output
directory; that consumes another invocation but not another review wave. Segmented
review does not resume one opaque multi-agent session: it retains valid segment
checkpoints and starts only missing segments under the same campaign. A new review
wave after a revision requires a new candidate hash and a distinct external Owner
review credit. The Governor never starts that wave automatically.

If preflight or materialization fails before Codex starts, the reservation is either
not created or is settled with zero charged invocations, elapsed time, and review
waves.

## Serial and segmented review

The default is serial:

```json
{
  "max_parallel_agents": 1,
  "max_total_agents": 1,
  "max_spawn_depth": 1,
  "review_scopes": []
}
```

This hard-disables the native `multi_agent` feature for the serial reviewer process.
Segmented review uses a deliberately closed two-level graph:

- at least two `independent` segments with unique scope and acceptance IDs;
- exactly one `join` segment whose dependencies are all independent segments;
- explicit context paths, elapsed limit, and optional observed-token limit for every
  segment;
- total segment budgets bounded by the contract, and total model invocations reserved
  in the campaign ledger before any segment starts.

The Governor may run ready independent segments concurrently up to
`max_parallel_agents`. Each process has its own read-only context, schema, supervision,
and terminal receipt; it cannot spawn descendants. Only a valid completed receipt
becomes an immutable checkpoint. The join receives copies of those checkpoint files,
not conversation history or model memory. The final aggregate uses a fixed verdict
precedence and preserves segment findings; it does not perform semantic reasoning or
create Owner acceptance.

## Enforced and not enforced

Deterministic checks cover input hashes, the materialized context tree, output schema,
lineage reservation, batched invocation charges, process limits, governed-repository
Git changes, segment dependencies and checkpoints, receipt identity, deterministic
aggregation, and the authority boundary.

This route does not prove that the reviewer found every defect, that its verdict is
correct, or that two model agents are epistemically independent. Codex read-only mode
and the Git mutation probe are guardrails, not a container or OS sandbox. Run
untrusted review tools in a stronger isolated environment.
