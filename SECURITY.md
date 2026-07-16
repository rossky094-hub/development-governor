# Security policy

Development Governor is an experimental development guardrail, not a security
boundary. Do not rely on it to isolate hostile code or users.

For suspected vulnerabilities, do not publish secrets, credentials, or private
repository contents in an issue. Send a minimal, redacted report to the repository
owner through GitHub's private vulnerability reporting feature when available.

Include the affected release, operating system, Codex/CLI version, activation mode,
the exact command or Hook event, expected decision, and observed decision. Reports
that demonstrate an out-of-scope mutation being incorrectly allowed are highest
priority.

Only the latest beta is supported during the v0 experiment.
