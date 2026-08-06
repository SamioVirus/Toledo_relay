# Operate and supervise Relay

## Contents

- Preflight
- Launch
- Supervision
- Human gates
- Recovery and validation
- Evidence retrieval and cleanup

## Preflight

1. Confirm the skill/repository is current with `manage_skill.py status`.
2. Run `check`, `projects`, `workflows`, and the selected workflow's `profiles` view.
3. Confirm the project root, source revision, clean state, instruction files, write allowlist, evidence exclusions, and validations.
4. Inspect exact configured provider/model/effort/session values. Do not silently substitute.
5. Write a bounded request file outside tracked source or in an approved disposable path.
6. Decide ordinary, step, or three-to-five-cycle continuous mode. Never combine step and continuous modes.

## Launch

```powershell
python -m toledo_orchestrator run --project PROJECT --workflow WORKFLOW --request-file REQUEST.md
```

Optional controls:

```powershell
python -m toledo_orchestrator run --project PROJECT --workflow WORKFLOW --stack LATER_WORKFLOW --request-file REQUEST.md
python -m toledo_orchestrator run --project PROJECT --workflow WORKFLOW --request-file REQUEST.md --step
python -m toledo_orchestrator run --project PROJECT --workflow WORKFLOW --request-file REQUEST.md --continuous-loop 3
```

The CLI runs until a gate or terminal state. The UI may observe the same run after `python -m toledo_orchestrator ui`. Record the run ID and identify which process owns execution.

## Supervision

Read the bounded contract:

```powershell
python -m toledo_orchestrator agent-brief RUN_ID
python -m toledo_orchestrator status RUN_ID
```

If a known UI or background worker owns the run, wait in increments no longer than 45 seconds:

```powershell
python skills/relay-use-skill/scripts/wait_for_relay.py RUN_ID --after-turn N --after-status running --max-wait 30
```

Use current `run.current_turn` and `run.status` values as the next wait cursor. Do not start a duplicate run operation. Do not narrate unchanged snapshots, but update the user at least once per minute.

In step mode, advance only after reviewing the exact next turn. Optional owner direction may be supplied once:

```powershell
python -m toledo_orchestrator advance RUN_ID --text-file DIRECTION.md
```

For detail:

```powershell
python -m toledo_orchestrator show RUN_ID --turn N
python -m toledo_orchestrator artifact RUN_ID PATH
python -m toledo_orchestrator export RUN_ID --format markdown
```

Avoid diagnostic export unless needed; raw streams may contain sensitive text.

## Human gates

- `next_task_approval`: show the proposal. Execute `decide --choice yes|no|other` only from the user's decision. In a stack, `no` may advance to the next layer.
- `provider_requested_human`: show the exact provider request and obtain the answer.
- planning or implementation cap: show remaining disputes and evidence. Never silently extend or seal.
- consequential validation: show the exact command and effect before approval.
- baseline failure follow-up: carry only the typed, receipt-bound failure selected by the user.
- `seal_evidence`: human-only and valid only when required evidence is complete, passing, and unchanged.

Examples:

```powershell
python -m toledo_orchestrator decide RUN_ID --choice yes
python -m toledo_orchestrator decide RUN_ID --choice no
python -m toledo_orchestrator decide RUN_ID --choice other --text-file FEEDBACK.md
```

Never fabricate or infer the user's gate choice.

## Recovery and validation

If a run is nonterminal without a known owner, inspect `agent-brief`, the latest turn, provider errors, and in-flight markers. Only then consider:

```powershell
python -m toledo_orchestrator recover RUN_ID
```

Recovery takes the per-run lock and fails closed on an unknown provider invocation. Never edit run files to clear a lock or invent a completed turn.

Remote or human-executed validation requires a revision- and patch-bound receipt:

```powershell
python -m toledo_orchestrator validate RUN_ID --receipt-file RECEIPT.json
```

Do not claim a remote check ran without its accepted receipt.

## Evidence retrieval and cleanup

At a terminal state, inspect the latest substantive output, completion receipt, changed paths, validation results, configured-versus-observed provider evidence, remaining debt, and next-task proposal.

Cleanup removes the execution worktree while retaining its durable branch:

```powershell
python -m toledo_orchestrator cleanup RUN_ID
```

Cleanup is destructive to the disposable worktree. Run it only after exact target verification and when the user wants cleanup. It does not merge or publish the run branch.
