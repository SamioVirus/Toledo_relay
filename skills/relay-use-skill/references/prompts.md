# Prompt system and defaults

## Contents

- Prompt assembly
- Default prompt families
- Inspect exact prompts
- Safe customization
- Prompt best practices

## Prompt assembly

Relay separates:

1. stage stance instructions;
2. optional deterministic direction fragments;
3. request and typed context artifacts;
4. one-turn owner direction;
5. a strict controller-output contract.

The controller renders and snapshots exact transport bytes before generation. Provider session history is continuity, not authority. Later stages receive only declared context artifacts.

## Default prompt families

Shared controller contracts:

- `orchestrator-law.md`
- `strict-contract.md`
- `direction-closure.md`
- `direction-continuity.md`
- `direction-recheck.md`

General development:

- `planning-kickoff.md`
- `planning-review.md`
- `planning-revise.md`
- `implementation-kickoff.md`
- `implementation-review.md`
- `implementation-repair.md`
- `next-task.md`
- `next-task-revise.md`
- `next-task-planner.md`
- `next-task-planner-revise.md`

Legacy read-only review:

- `proposer.md`
- `reviewer.md`
- `reviser.md`

Strategy Council:

- `strategy-council-planner.md`
- `strategy-council-reviewer.md`
- `strategy-council-adjudicator.md`
- `strategy-council-package.md`
- `strategy-council-auditor.md`
- `strategy-council-repair.md`
- `strategy-council-next.md`
- `strategy-council-next-revise.md`

Test & Proof Gate:

- `test-proof-planner.md`
- `test-proof-reviewer.md`
- `test-proof-adjudicator.md`
- `test-proof-runner.md`
- `test-proof-auditor.md`
- `test-proof-repair.md`
- `test-proof-next.md`
- `test-proof-next-revise.md`

UI Studio:

- `ui-studio-planner.md`
- `ui-studio-reviewer.md`
- `ui-studio-adjudicator.md`
- `ui-studio-builder.md`
- `ui-studio-critic.md`
- `ui-studio-repair.md`
- `ui-studio-next.md`
- `ui-studio-next-revise.md`

These names are a synchronization inventory. Read the actual files under `src/toledo_orchestrator/prompts/` for exact current text.

## Inspect exact prompts

Before launch, inspect the selected workflow JSON and its stage prompt files. In the UI, use the exact-instruction and next-turn prompt views. For a launched run, use:

```powershell
python -m toledo_orchestrator show RUN_ID --turn N
python -m toledo_orchestrator artifact RUN_ID PATH
```

Do not infer the exact prompt from a stage label, Quick take, or prior run.

## Safe customization

Use `workflow-save-as` for reusable prompt replacements. A prompt override replaces a named stage instruction file for the new workflow; it does not append a casual note. Keep controller law and strict output contracts intact.

Use `advance --text-file` for one-turn owner direction in step mode, and `decide --choice other --text-file` for human feedback at an eligible gate. Those inputs are stored as decisions; they do not rewrite prior artifacts.

## Prompt best practices

- State purpose, evidence requirements, authority boundaries, and stop conditions.
- Keep implementation permissions in code/workflow configuration, not wishful prompt text.
- Require reviewers to name concrete unresolved issues before requesting another round.
- Require implementers to distinguish completed work, failed proof, environmental blockers, and unattempted work.
- Preserve disagreements and adjudication math rather than smoothing them into consensus.
- Do not carry secrets, raw credentials, irrelevant transcripts, or private runtime streams into requests.
- Prefer a workflow/context change over increasingly elaborate prose when the problem is missing state or wrong responsibility.
