# Toledo Orchestrator

The walking skeleton is a transport controller: it moves byte-stable prompts and provider results through the fixed read-only `dev-review` sequence:

`Codex proposal -> Claude review -> Codex revision`.

Runs are stored outside Git under `%LOCALAPPDATA%\ToledoOrchestrator\runs` by default. The run directory is authoritative; prompts, provider stdout/stderr, substantive work products, decisions, and validation receipts are independently hashed. Process-control directives are recorded in turn metadata and removed from the work-product channel before later stages receive it.

```powershell
python -m pip install -e .
orchestrator check
orchestrator run --project toledo --workflow dev-review --request-file .\request.md
```

`orchestrator check` verifies CLI version, authentication, required transport/permission/control flags, the fixed route map, the Toledo project root, its instruction files, and its current source revision. It does not spend provider capacity; `generation_verified=false` remains explicit until a real run succeeds.

The Toledo project definition is packaged at `src/toledo_orchestrator/projects/toledo.json`. Stage and strict-contract prompts are separate package files under `src/toledo_orchestrator/prompts/`. A paused run resumes from its existing cursor, transports the human decision into the next prompt, and runs until the next terminal or pause state.

The first milestone is deliberately read-only. It has no general routing, database, UI, implementation worktree, remote execution, or automatic retry after an unknown provider invocation.

This repository is intentionally unsynced per the implementation plan and currently has no backup remote. Configure an approved remote separately before treating it as durable against local disk loss.
