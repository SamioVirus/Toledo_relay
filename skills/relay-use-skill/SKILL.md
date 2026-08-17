---
name: relay-use-skill
description: "Install, update, configure, operate, supervise, recover, and maintain Toledo Relay from Codex or Claude. Use when an agent must send a task or another agent's challenge into Relay, create or register a repository, choose or customize workflows, prompts, providers, models, reasoning, or round caps, launch or babysit a run, handle human gates, retrieve evidence, or change Relay itself and publish the synchronized change to GitHub."
---

# Relay use skill

Operate Relay as a durable controller, not as a chat convention. Relay coordinates provider sessions, immutable prompts and outputs, isolated Git worktrees, validation evidence, explicit gates, and sealed handoffs. The controller state and files are authoritative; provider prose and UI summaries are not.

This package is self-contained. Use only the bundled references linked below plus live Relay output. Do not assume a separate Toledo documentation checkout exists.

## Establish the installation and freshness boundary

Relay's canonical public repository is `https://github.com/SamioVirus/Toledo_relay`.

At the start of every Relay operation, locate this skill's repository and run:

```powershell
python skills/relay-use-skill/scripts/manage_skill.py status
```

The command fetches the canonical `main` reference and reports whether the checkout is current, behind, ahead, or diverged. If it is safely behind on a clean `main` checkout, update it and then reread this `SKILL.md`:

```powershell
python skills/relay-use-skill/scripts/manage_skill.py update
```

Never discard local changes or force a divergent checkout. If Relay is absent, the skill is not linked into the current app, or an update cannot fast-forward safely, read [references/installation.md](references/installation.md) and repair the installation before starting provider work.

## Start from live truth

Run these commands from the Relay repository or an environment where `toledo_orchestrator` is installed:

```powershell
python -m toledo_orchestrator check
python -m toledo_orchestrator projects
python -m toledo_orchestrator workflows
python -m toledo_orchestrator runs
```

Require `check` to report a ready controller, authenticated required providers, valid workflows, and an implementation-ready target project. Treat live workflow profiles and successful observed turns as model truth. Never choose a model ID, effort, workflow default, run status, or repository mapping from memory.

## Package the request

Turn the user's task or another agent's challenge into a bounded Markdown request containing:

- goal and why it matters;
- target repository and relevant current context;
- evidence or source material, including the outside agent's challenge verbatim when important;
- allowed and forbidden changes;
- required tests, runtime proof, and acceptance conditions;
- human-only decisions and stop conditions;
- explicit exclusions for secrets and private runtime material.

Use an existing clean Git repository. Relay registers repositories but does not silently create, initialize, publish, retarget, or clean them. Read [references/projects-and-configuration.md](references/projects-and-configuration.md) when the project is missing or its permissions and validations need configuration.

## Choose the smallest route

Read [references/workflows.md](references/workflows.md) before selecting, stacking, or modifying workflows. For ordinary plan-review-build-audit work, prefer `continuous-development`. Start with one workflow. Add a specialist or cadence stack only when a distinct phase needs different roles, evidence, or reasoning.

Inspect current profiles before launch:

```powershell
python -m toledo_orchestrator profiles --workflow WORKFLOW
```

Read [references/models.md](references/models.md) before changing providers, models, effort, or sessions. Read [references/prompts.md](references/prompts.md) before changing a default prompt, stage context, stance, or prompt override. Save a variant when the graph still fits; create a new graph only when roles, contexts, permissions, session slots, or transitions truly differ.

## Launch

Read [references/operations.md](references/operations.md) completely before launching, advancing, deciding, recovering, validating, cleaning up, or supervising a run.

The ordinary headless launch is:

```powershell
python -m toledo_orchestrator run --project PROJECT --workflow WORKFLOW --request-file REQUEST.md
```

Use `--continuous-loop 3|4|5` only for an explicitly bounded self-running sequence. Use `--step` for a pause before every provider turn. Never combine step mode with a continuous loop. Record the returned run ID.

## Babysit until a real stopping condition

Use the compact machine contract instead of repeatedly reading the full run:

```powershell
python -m toledo_orchestrator agent-brief RUN_ID
python skills/relay-use-skill/scripts/wait_for_relay.py RUN_ID --after-turn N --after-status running --max-wait 30
```

Continue bounded waits while a known UI or worker owns the run. Keep the user informed at least once per minute. Never start a duplicate worker.

Stop when:

- the run is terminal, then verify receipts and report;
- `gate.requires_human=true`, then present exact evidence and wait for the user's decision;
- a provider or validation fails, then report the exact failure and safe recovery choices;
- a nonterminal run has no known owner, then inspect in-flight evidence before considering recovery;
- scope, credentials, destructive actions, publishing, deployment, or permission expansion needs new authority.

Never auto-answer a human gate, rewrite immutable run files, infer that an unknown provider call finished, or claim success from provider prose alone.

## Return evidence

At completion or pause, report:

1. run ID, workflow path, and controller status;
2. completed work and changed paths;
3. exact validations and receipts;
4. configured-versus-observed provider/model/effort evidence;
5. human decisions or unresolved gates;
6. remaining risks and the recommended next move.

## Maintain Relay and this skill together

When changing Relay itself, read [references/maintenance-and-publishing.md](references/maintenance-and-publishing.md). Any change to CLI commands, repository configuration, workflow schema or defaults, prompts, provider/model controls, loop behavior, gates, evidence, recovery, UI operating behavior, or final outputs must update this skill in the same commit when operator behavior changes.

Run the synchronization test and skill validator before claiming completion. A Relay change is not fully delivered until its branch is present in the canonical GitHub repository. If external-write authorization is absent, stop before pushing and report the exact unpublished commit and branch as the remaining gate.
