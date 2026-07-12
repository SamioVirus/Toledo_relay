# Toledo Orchestrator

Toledo Orchestrator is a file-authoritative, session-aware development relay. It preserves the original read-only `dev-review` walking skeleton and adds the common A/B/C workflow:

```text
Codex planning session A  <->  Claude review session B
              approved handoff
Codex implementation C   <->  Claude review session B
            accepted implementation
          Claude session B proposes next
                 human yes/no/other
```

Session identity, provider session ID, model/effort profile, repository, permission, and workflow stage are independent controls. Provider history improves continuity; exact prompts, outputs, handoffs, patches, validations, decisions, and completion receipts remain authoritative files under `%LOCALAPPDATA%\ToledoOrchestrator`.

## Local UI

Install the package in editable mode and launch the loopback-only UI:

```powershell
python -m pip install -e .
python -m toledo_orchestrator ui
```

The UI shows Codex on the left, Claude on the right, stable colors per logical session, named interstitial prompts on the center line, sealed handoff/completion milestones, configured and observed model/effort evidence, prompt/output inspectors, filters, zoom, and the human `yes`/`no`/`other` gate. It also exposes editable route profiles, repository configuration, and a recorded one-turn `new`/`continue` session override.

## Readiness and profiles

```powershell
python -m toledo_orchestrator check
python -m toledo_orchestrator profiles --workflow continuous-development
python -m toledo_orchestrator profile-set --profile codex-planning --model gpt-5.6-sol --effort xhigh
python -m toledo_orchestrator projects
```

The packaged A/B/C defaults reflect the owner's current workflow and remain editable under the runtime configuration directory:

- Planning A: Codex `gpt-5.6-sol`, `xhigh`, read-only.
- Planning review B: Claude `claude-fable-5`, `xhigh`, read-only.
- Implementation C: Codex `gpt-5.6-terra`, `high`, isolated workspace-write.
- Implementation review B: Claude `claude-opus-4-8`, `max`, read-only.

Friendly display labels are separate from exact CLI arguments. `profile-set` and the UI write runtime-local overrides; Python code and packaged defaults remain unchanged.

Add another repository from the CLI:

```powershell
python -m toledo_orchestrator project-add --id my-repo --root "C:\src\my-repo" --instruction-file AGENTS.md --write-path . --validation "tests|local|python -m pytest -q"
```

Repository selection is easy but intentionally run-scoped: choose the project when starting a run. To change repositories, start a separate run so an existing branch, worktree, evidence chain, or provider session can never be silently retargeted.

## Running the continuous workflow

```powershell
python -m toledo_orchestrator run --project toledo --workflow continuous-development --request-file "C:\path\to\request.md"
```

The source checkout must be clean so the selected committed revision cannot silently omit local work. The command runs until it completes or reaches a human/failure gate. It creates a dedicated execution worktree and durable branch named `codex/orchestrator/<run-id>` from that revision. Planning/review turns remain read-only; only the implementation profile receives workspace-write access inside that worktree. Accepted changes are committed on the execution branch but are never merged, pushed, deployed, or copied into the user's source checkout automatically.

Use step mode when you want a deliberate control point before every provider turn:

```powershell
python -m toledo_orchestrator run --project toledo --workflow continuous-development --request-file "C:\path\to\request.md" --step
python -m toledo_orchestrator profile-set --profile codex-planning --model MODEL --effort EFFORT
python -m toledo_orchestrator advance RUN_ID
```

At each step pause, the UI can override the next turn's same-provider profile and choose `new`, `continue`, or the workflow default. Automatic mode remains the default.

At the next-task gate:

```powershell
python -m toledo_orchestrator decide RUN_ID --choice yes
python -m toledo_orchestrator decide RUN_ID --choice no
python -m toledo_orchestrator decide RUN_ID --choice other --text "Make the next task narrower."
```

- `yes` seals the proposal as the next cycle request and starts fresh D/E/F logical sessions (then G/H/I, and so on).
- `no` ends the continuous run.
- `other` returns the exact feedback to Claude review session B and reopens the gate with its revised proposal.

Configured local validation commands are displayed and require explicit approval before they execute on the host. Required remote validations pause for a patch- and revision-bound receipt:

```powershell
python -m toledo_orchestrator validate RUN_ID --receipt-file "C:\path\to\receipt.json"
```

A rejected technical gate produces a truthful `cancelled` run; only an accepted implementation followed by “no next task” is a successful `complete` run.

After a terminal run, remove the execution worktree while retaining its durable branch:

```powershell
python -m toledo_orchestrator cleanup RUN_ID
```

## Inspecting evidence

```powershell
python -m toledo_orchestrator runs
python -m toledo_orchestrator status RUN_ID
python -m toledo_orchestrator show RUN_ID --turn 1
python -m toledo_orchestrator artifact RUN_ID "artifacts/cycle.0001.approved-handoff.md"
```

Each provider turn stores the exact prompt, raw stdout/stderr, directive-free work product, metadata, hashes, logical/physical session identity, configured profile, and best-effort observed model/reasoning evidence. `check` shows configured profiles and repository identity/readiness; successful turn metadata and the UI inspector show what the provider actually used. A missing, reused, or changed session ID pauses visibly; it never silently starts over.

The original CLI-only workflow remains available:

```powershell
python -m toledo_orchestrator run --project toledo --workflow dev-review --request-file .\request.md
```

See [session-cycle-design.md](docs/session-cycle-design.md) for the reusable interaction principles extracted from the manual A/B/C transcripts.

## Durability

The working repository remains outside OneDrive. Its `origin` is the bare mirror at `C:\Users\sammo\OneDrive\Documents\toledo-orchestrator.git`. Push each accepted orchestrator code commit with `git push origin main`. Runtime run artifacts remain private and outside Git.
