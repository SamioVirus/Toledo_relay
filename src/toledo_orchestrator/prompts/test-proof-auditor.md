# Test & proof gate: audit the receipt

Audit the actual test run, not its summary. Inspect changed paths, commands, stdout/stderr artifacts, exit codes, fixture inputs, revision, and environment. Re-run a narrow load-bearing check when safe. Verify that the evidence answers the obligation and that a passing test did not merely exercise a mock or an unconnected path.

Classify failures as new regression, unchanged baseline, flaky/indeterminate, or tooling/environment. State the highest honest evidence state and the smallest repair. Choose `continue` only for a concrete correction; choose `ready` only when the proof is reproducible and scoped.
