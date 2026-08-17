# How Relay works

## Contents

- Purpose
- Authority model
- A/B/C execution shape
- Durable state
- Isolation and permissions
- Gates and completion

## Purpose

Relay is a local, file-authoritative orchestration system for multi-agent development and review. It gives each role an explicit provider profile, session policy, prompt contract, input context, output artifact, transition, and permission boundary. It is designed to preserve useful disagreement while making execution reproducible.

## Authority model

Trust evidence in this order:

1. controller state and immutable run snapshot;
2. sealed prompts, outputs, handoffs, validation receipts, decisions, and completion receipts;
3. the actual isolated worktree and Git revision;
4. compact `agent-brief` projections;
5. UI projections and local Quick takes;
6. provider prose.

Quick takes are optional local summaries. They never enter prompts or drive transitions.

## A/B/C execution shape

The general development graph is:

```text
planner A -> independent reviewer B -> planner revision loop
          -> sealed approved handoff
implementer C -> reviewer B audit -> bounded repair loop
              -> sealed completion receipt
strategic closer -> human next-task gate
```

Logical roles and physical provider sessions are distinct. A workflow may continue a session for continuity or create a new one for independence. The workflow snapshot fixes that policy for the run.

## Durable state

Relay stores runtime data outside the source repository, under the configured runtime directory. A run snapshots:

- source project and revision;
- resolved workflow graph and profiles;
- exact prompt bytes;
- request and owner direction;
- provider session identifiers and observed metadata;
- outputs, hashes, decisions, validations, handoffs, and receipts.

Never edit a run snapshot, sealed artifact, event, provider output, or receipt by hand. Use controller commands.

## Isolation and permissions

Implementation-enabled runs start from a clean committed source revision and use a dedicated Git worktree and run branch. Read-only roles may inspect but not modify it. Workspace-write roles may edit only within the configured allowlist. Relay does not automatically merge, push, deploy, or copy changes back to the source checkout.

Repository registration declares instructions, write paths, evidence exclusions, validations, and whether implementation is allowed. It is a capability boundary, not just a label.

## Gates and completion

Automatic transitions are limited by the workflow graph. Human gates cover next-task choices, consequential validation, provider questions, ambiguous recovery, exhausted repair caps, and other unsafe boundaries.

Provider output saying “done” is not completion. Completion requires controller state plus the expected sealed receipt and validation evidence. A paused run is not failed; a stopped or cancelled run is not successfully complete.
