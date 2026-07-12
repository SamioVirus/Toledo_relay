# Toledo Orchestrator

The walking skeleton is a transport controller: it moves byte-stable prompts and provider results through the fixed read-only `dev-review` sequence:

`Codex proposal -> Claude review -> Codex revision`.

Runs are stored outside Git under `%LOCALAPPDATA%\ToledoOrchestrator\runs` by default.  The run directory is authoritative; prompts, provider stdout/stderr, parsed output, decisions, and validation receipts are independently hashed.

```powershell
python -m pip install -e .
orchestrator check
orchestrator run --project toledo --workflow dev-review --request-file .\request.md
```

The first milestone is deliberately read-only. It has no general routing, database, UI, implementation worktree, remote execution, or automatic retry after an unknown provider invocation.
