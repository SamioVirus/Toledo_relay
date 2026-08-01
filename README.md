# Toledo Orchestrator

Toledo Orchestrator is a file-authoritative, session-aware development relay. It preserves the original read-only `dev-review` walking skeleton and adds the common A/B/C workflow:

```text
Codex planning session A  <->  Claude review session B
              approved handoff
Codex implementation C   <->  Claude review session B
            accepted implementation
     strategic session B or A proposes next
                 human yes/no/other
```

Session identity, provider session ID, model/effort profile, repository, permission, and workflow stage are independent controls. Provider history improves continuity; exact prompts, outputs, handoffs, patches, validations, decisions, and completion receipts remain authoritative files under `%LOCALAPPDATA%\ToledoOrchestrator`.

## Local UI

Install the package in editable mode and launch the loopback-only UI:

```powershell
python -m pip install -e .
python -m toledo_orchestrator ui
```

The UI shows Codex on the left, Claude on the right, stable colors and physical-session generations per logical session, named interstitial prompts on the center line, sealed handoff/completion milestones, configured and observed model/effort evidence, direction/transport/output inspectors, semantic overview/detail density, and human gates. It also exposes editable route profiles, repository configuration, exact optional owner direction at every step, and recorded one-turn model/effort plus `new`/`continue` overrides.

Each successful substantive turn also gets a collapsed **Quick take**: a one- or two-sentence, human-review digest generated locally with Gemma 4 through Ollama. It is an additive side artifact, never controller input or a replacement for the exact output. Generation starts asynchronously after a new turn is saved, so continuous runs and automatic transitions do not wait; opening an older run backfills missing digests. An unavailable local model leaves the full Relay run unaffected, and reopening the run after the cooldown retries the digest. The defaults are `gemma4:12b-it-qat` at `http://127.0.0.1:11434/api/chat`; advanced local setups may override them with `TOLEDO_RELAY_SUMMARY_MODEL` and `TOLEDO_RELAY_OLLAMA_URL`.

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
- Implementation review B: Claude `claude-opus-5`, `max`, read-only.

Friendly display labels are separate from exact CLI arguments. `profile-set` and the UI write runtime-local overrides; Python code and packaged defaults remain unchanged.

Two inherited workflow variants cover both strategic-closure patterns observed in the owner's manual process:

- `continuous-development`: Claude reviewer B switches back to its planning profile and proposes the next build.
- `continuous-development-planner-close`: original Codex planner A resumes after implementation acceptance, performs strategic closure, and proposes the next build. This mirrors the transcript where A caught a longer-horizon issue after B had accepted C.

Both preserve a fresh implementation C, the same physical B session across review profiles, and fresh D/E/F sessions after human approval. Choose either in the New Cycle dialog; the route preflight shows the exact actor, model, effort, permission, and session policy before launch.

Add another repository from the CLI:

```powershell
python -m toledo_orchestrator project-add --id my-repo --root "C:\src\my-repo" --instruction-file AGENTS.md --write-path . --evidence-exclude-path .relay-tmp --validation "tests|local|python -m pytest -q"
```

Implementation projects may declare `implementation.evidence_exclude_paths` for disposable in-worktree test output. Exclusions apply only to untracked files; tracked changes are always sealed and reviewed. Prefer a dedicated path such as `.relay-tmp` rather than a source directory.

Repository selection is easy but intentionally run-scoped: choose the project when starting a run. To change repositories, start a separate run so an existing branch, worktree, evidence chain, or provider session can never be silently retargeted.

## Running the continuous workflow

```powershell
python -m toledo_orchestrator run --project toledo --workflow continuous-development --request-file "C:\path\to\request.md"
```

The source checkout must be clean so the selected committed revision cannot silently omit local work. The command runs until it completes or reaches a human/failure gate. It creates a dedicated execution worktree and durable branch named `codex/orchestrator/<run-id>` from that revision. Planning/review turns remain read-only; only the implementation profile receives workspace-write access inside that worktree. Accepted changes are committed on the execution branch but are never merged, pushed, deployed, or copied into the user's source checkout automatically.

For a bounded self-running sequence, enable **Continuous loop** in New Cycle and choose 3, 4, or 5 cycles, or use `--continuous-loop 3` on the CLI. Relay takes safe recommended approvals: it commits a reviewed implementation, records and carries an unchanged older test failure into the next task when present, accepts each sealed next-task proposal, and then runs a fresh idea → plan/review → implementation/audit cycle. It stops after the selected number of completed cycles with the following idea ready for human review. Consequential-command approval, new validation regressions, provider failures, ambiguity, repair caps, required receipts, and recovery gates still stop immediately.

```powershell
python -m toledo_orchestrator run --project toledo --workflow continuous-development --request-file "C:\path\to\request.md" --continuous-loop 3
```

### Specialized workflow layers

The New Cycle dialog can add, remove, and reorder workflow layers before launch. Layers share one isolated worktree but not an implicit session: each layer receives a launch-time workflow/prompt snapshot, closes at its next-task gate, and starts the next layer only when you explicitly continue. The same stack is available from the CLI:

```powershell
python -m toledo_orchestrator run --project toledo --workflow continuous-development --stack strategy-council --stack test-proof-gate --stack ui-studio --request-file "C:\path\to\request.md"
```

The packaged specialist routes are:

- `strategy-council`: high-reasoning strategic reconstruction and adversarial review;
- `test-proof-gate`: medium-reasoning proof planning, bounded execution, and receipt audit;
- `ui-studio`: responsive UI planning, implementation, and render/accessibility critique.

The cadence stations are explicit workflow presets for the same stack transport:

- `weekly-governance`: weekly strategy station, inheriting the quality-first Sol strategy route;
- `daily-dispatch`: daily proof/repair station, inheriting the balanced Terra proof route;
- `hourly-station`: hourly visible-surface station, inheriting the Sol UI route.

Cadence-aware stacks may stay at one station or move only across adjacent `weekly <-> daily <-> hourly` stations. Each move produces a sealed `toledo_orchestrator.cadence_handoff.v1` artifact in the run; downstream stack context verifies the registered artifact hash and its exact backbone projection before use.

Keep the cheap continuous loop as the scout. Add Strategy Council for consequential architecture, Test & Proof when evidence is the bottleneck, and UI Studio only when a user-visible surface is part of the requested outcome.

Use step mode when you want a deliberate control point before every provider turn:

```powershell
python -m toledo_orchestrator run --project toledo --workflow continuous-development --request-file "C:\path\to\request.md" --step
python -m toledo_orchestrator profile-set --profile codex-planning --model MODEL --effort EFFORT
python -m toledo_orchestrator override RUN_ID --model MODEL --effort EFFORT --session-action continue
python -m toledo_orchestrator advance RUN_ID --text "Use your judgment; fix real issues and push back on empty fear."
python -m toledo_orchestrator advance RUN_ID --text-file "C:\path\to\direction.md"
python -m toledo_orchestrator recover RUN_ID
```

At each step pause, the UI can override the next turn's same-provider profile, exact model/effort, and `new`, `continue`, or workflow-default session action. Optional owner direction is stored byte-for-byte, shown on the timeline, and injected once into the next turn. Automatic mode remains the default.

If the owning CLI or UI process exits while a v2 run is `created` or `running`, `recover` safely re-enters it under the per-run lock. A stale in-flight provider marker becomes an explicit `unknown_provider_invocation` gate rather than being guessed complete; the UI exposes the same recovery action whenever it sees an inactive nonterminal run.

At the next-task gate:

```powershell
python -m toledo_orchestrator decide RUN_ID --choice yes
python -m toledo_orchestrator decide RUN_ID --choice no
python -m toledo_orchestrator decide RUN_ID --choice other --text "Make the next task narrower."
```

- `yes` seals the proposal as the next cycle request and starts fresh D/E/F logical sessions (then G/H/I, and so on).
- `no` ends the continuous run, or advances to the next explicitly stacked layer when one remains.
- `other` returns the exact feedback to whichever strategic session produced the proposal and reopens the gate with its revision.

Configured local validations use a high-threshold approval policy: routine tests, linters, compilers, read-only assertions, and smoke reads run automatically in the isolated worktree. Relay pauses only when a command clearly advertises consequential effects such as destructive file or Git changes, software installation, elevated/system operations, container or infrastructure mutation, deployment, publishing, or external writes. The approval gate explains the detected risk in plain language and still exposes the exact command. Required remote validations pause for a patch- and revision-bound receipt:

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

Workflow graphs are declarative. Stage prompt labels, prompt files, context, session slots, round caps, cap reasons, repair targets, seal sources, profiles, and transitions are validated from JSON rather than inferred from fixed stage names. Small variants can use `"extends": "base-workflow-id"`; inherited stages also inherit the base prompt namespace, while child-specific prompt files take precedence. Runtime-only custom prompt files live under `%LOCALAPPDATA%\ToledoOrchestrator\config\prompts\WORKFLOW_ID\`; identifiers and paths are containment-checked, and `check` refuses readiness when a configured prompt is missing. Every run snapshots the resolved workflow and exact prompt bytes before generation.

The original CLI-only workflow remains available:

```powershell
python -m toledo_orchestrator run --project toledo --workflow dev-review --request-file .\request.md
```

See [session-cycle-design.md](docs/session-cycle-design.md) for the reusable interaction principles extracted from the manual A/B/C transcripts.

## Durability

The working repository remains outside OneDrive. Its `origin` is the bare mirror at `C:\Users\sammo\OneDrive\Documents\toledo-orchestrator.git`. Push each accepted orchestrator code commit with `git push origin main`. Runtime run artifacts remain private and outside Git.
