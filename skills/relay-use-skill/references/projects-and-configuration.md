# Repositories and configuration

## Contents

- Repository prerequisites
- Register a project
- Project fields and boundaries
- Runtime configuration
- Workflow variants and new graphs

## Repository prerequisites

Relay operates on an existing Git repository. Before registration, verify:

```powershell
git -C C:\path\to\repo status --short --branch
git -C C:\path\to\repo rev-parse --show-toplevel
```

The source must have a committed revision and be clean before an implementation run. Relay does not create a repository, choose its license, create a remote, publish history, or clean unrelated changes. Perform those actions only when the user explicitly authorizes them.

## Register a project

The packaged `toledo` project is intentionally an unconfigured placeholder. It is not an operating target: register an actual repository before running a workflow. The saved definition belongs in Relay's runtime configuration directory, so configuring a project does not modify the source checkout.

Use `project-add` to save a runtime-local project definition:

```powershell
python -m toledo_orchestrator project-add `
  --id my-project `
  --root "C:\path\to\repo" `
  --instruction-file AGENTS.md `
  --write-path . `
  --evidence-exclude-path .relay-tmp `
  --validation "unit|local|python -m pytest -q"
```

Then verify:

```powershell
python -m toledo_orchestrator projects
python -m toledo_orchestrator check
```

Use a new run when changing projects. Never retarget a live run.

## Project fields and boundaries

- `--id`: stable lowercase runtime identifier.
- `--root`: exact existing source checkout.
- `--instruction-file`: repeat for repository instructions that every role must receive.
- `--write-path`: repeat to define implementation allowlists. Use the narrowest practical scope.
- `--evidence-exclude-path`: repeat only for disposable untracked output. Tracked changes remain evidence.
- `--validation`: `ID|ENVIRONMENT|COMMAND`; repeat for relevant checks.
- `--no-implementation`: make the project review-only.
- `--allow-no-validations`: use only when the lack of automated checks is deliberate and explicit.

Do not put secrets in project JSON, prompts, requests, validations, or tracked instructions.

## Runtime configuration

Saved projects, profile overrides, workflow variants, and custom prompt files live under the Relay runtime configuration directory. Packaged source defaults remain in Git. Run snapshots are immutable after launch.

Inspect current values before changing them:

```powershell
python -m toledo_orchestrator projects
python -m toledo_orchestrator workflows
python -m toledo_orchestrator profiles --workflow WORKFLOW
```

## Workflow variants and new graphs

Use `workflow-save-as` when roles and transitions still fit but profiles, round caps, labels, or stage prompts need adjustment:

```json
{
  "base_workflow": "continuous-development",
  "id": "bounded-variant",
  "label": "Bounded variant",
  "profile_overrides": {
    "PROFILE": {"model": "LIVE_MODEL_ID", "effort": "SUPPORTED_EFFORT"}
  },
  "round_overrides": {"planning": 2, "implementation": 1},
  "prompt_overrides": {
    "planning-kickoff.md": "# Purpose\nComplete replacement stage instructions.\n"
  }
}
```

```powershell
python -m toledo_orchestrator workflow-save-as --spec-file C:\path\variant.json
```

Create a new source workflow graph only when stages, contexts, session slots, permissions, artifacts, or transitions must differ. Start from the closest packaged workflow, preserve schema validation and bounded repair behavior, add traversal tests, update the bundled workflow/prompt references, and publish the synchronized change.
