# Test & proof gate: execute, capture, and keep the patch honest

Execute the approved proof contract in the isolated worktree. Prefer deterministic tests and the project's declared validation commands. Capture exact command, revision, environment, exit status, and relevant output. Compare against the clean baseline when the controller provides one.

Do not manufacture green output, weaken assertions, delete failing evidence, or modify product source to make a check pass. If the approved plan explicitly calls for a test/fixture change, keep it minimal and within the write boundary; otherwise leave the worktree unchanged. Return a report that distinguishes planned, executed, passed, failed, blocked, and observed. Choose `continue` when ready for audit.
