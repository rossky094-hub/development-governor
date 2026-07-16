# Five-minute quickstart

## 1. Prove the installation

```bash
governor demo
```

The command must return `status: demo_passed`, `model_invocations: 0`, one allowed
mutation, one denied out-of-scope mutation, `verification_passed`, and `closed`.

## 2. Prepare independent acceptance

Inside your project, place the acceptance entrypoint under a path that the development
task will not be allowed to modify. Record its SHA-256 before enrollment:

```bash
shasum -a 256 acceptance/run_tests.py
```

Copy `examples/project-policy.example.json` outside the project, replace the absolute
path, Owner reference, acceptance command, and 64-character lowercase hash. Choose
project-level maxima deliberately; changing an enrolled policy requires an explicit
Owner-controlled migration.

```bash
governor enroll /tmp/my-project-policy.json
```

## 3. Freeze one bounded slice

Copy `examples/task-capsule.example.json` outside the project. State one observable
result, only its constraints and evidence inputs, exact deliverable paths, existing
acceptance IDs, and smaller or equal limits. Every evidence input is an immutable file
record with its current lowercase SHA-256. Represent a directory with a separately
hash-bound manifest file; do not submit a mutable directory path as evidence.

For a serial slice use `lanes: []` and set both agent limits to 1. For parallel work,
declare at least two lanes; every lane must own disjoint deliverable paths and disjoint
acceptance IDs. Parallelism is admitted by separability, not by task size.

```bash
governor prepare /tmp/my-task-capsule.json
governor start <task-hash-from-prepare>
```

If the default Codex Hook is enabled, recognized writes outside the task's deliverable
paths are denied while the lease is active. A lease expiring or disappearing also
denies recognized project mutations. Opaque test/build commands are denied because the
Hook cannot prove their write set; run them without promoting snapshot changes:

```bash
governor check --repo /absolute/path/to/your-project -- \
  python3 -m unittest -q
```

## 4. Verify and close

```bash
governor status --repo /absolute/path/to/your-project
governor verify --repo /absolute/path/to/your-project
governor close --repo /absolute/path/to/your-project
```

`verify` rechecks protected file and evidence hashes, creates a fresh disposable
repository snapshot for each pre-enrolled argv, and runs it without a shell. Snapshot
changes are never copied back. `close` succeeds only after verification passes. If the
Owner intentionally stops a failed slice, use an explicit reason:

```bash
governor close --repo /absolute/path/to/your-project \
  --owner-abort-reason "Owner stopped this bounded slice"
```

An abort is terminal evidence, not success.
