# Workflow selection

## Contents

- Selection rule
- General workflows
- Specialist workflows
- Cadence workflows
- Stacking and continuous loops

Always run `python -m toledo_orchestrator workflows` before selection. The live result owns current profiles and round caps.

## Selection rule

Choose the smallest workflow that can produce the requested evidence. A workflow is a role and state-transition contract, not a quality label. Save a variant for profile, prompt, or round changes; create a new graph only for a genuinely different execution shape.

## General workflows

- `continuous-development`: default A/B/C plan, independent review, implementation, audit, bounded repair, and next-task gate. Use for most code or documentation changes.
- `continuous-development-planner-close`: same build/audit loop, but the original planner performs strategic closure. Use when long-range planner continuity matters.
- `dev-review`: original read-only proposal/review/revision graph. Use for legacy review-only work, not implementation.

## Specialist workflows

- `strategy-council`: reconstruct, challenge, adjudicate, package, and audit consequential architecture or strategy. Slower and more costly by design.
- `test-proof-gate`: plan missing proof, challenge coverage, run bounded verification or repair, and independently audit receipts.
- `ui-studio`: plan, build, and critique a user-visible responsive slice. Browser or render acceptance still requires an environment capable of observing it.

## Cadence workflows

- `weekly-governance`: strategy station.
- `daily-dispatch`: proof and repair station.
- `hourly-station`: visible-surface station.

Cadence stacks may cross only adjacent stations. A station boundary is human-controlled and produces a sealed, hash-verified handoff.

## Stacking and continuous loops

Start with one workflow. Add `--stack WORKFLOW` only when a later phase needs a different role topology or evidence contract. All layers share the isolated worktree but receive their own snapshotted workflow and session policy.

Continuous loops support exactly three, four, or five completed cycles. They may take only controller-defined safe approvals. They still stop at human gates, consequential commands, new regressions, provider failures, ambiguity, repair caps, missing receipts, or evidence mismatch. Do not combine a continuous loop with step mode.

Examples:

```powershell
python -m toledo_orchestrator run --project PROJECT --workflow continuous-development --request-file REQUEST.md
python -m toledo_orchestrator run --project PROJECT --workflow strategy-council --stack test-proof-gate --request-file REQUEST.md
python -m toledo_orchestrator run --project PROJECT --workflow continuous-development --request-file REQUEST.md --continuous-loop 3
```
