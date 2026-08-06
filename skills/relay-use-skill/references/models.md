# Providers, models, effort, and sessions

## Contents

- Source of truth
- Selection method
- Persistent and one-turn changes
- Verification
- Cost and reliability practices

## Source of truth

Never choose model IDs or effort values from this file or memory. Provider catalogs, entitlements, aliases, and accepted reasoning controls change. Use live Relay inspection and requested-versus-observed turn evidence.

```powershell
python -m toledo_orchestrator check
python -m toledo_orchestrator workflows
python -m toledo_orchestrator profiles --workflow WORKFLOW
```

`check` verifies installed provider CLI versions, authentication, capabilities, current catalog state, project readiness, and resolved workflow profiles. Catalog metadata shows what Relay knows; it does not prove account entitlement. A bounded successful turn is stronger evidence.

## Selection method

1. Identify the role: classification, planning, independent review, implementation, audit, or strategic escalation.
2. Preserve the workflow's read-only or workspace-write permission boundary.
3. Inspect currently supported models and effort values.
4. Prefer the least expensive live route that meets the role's quality requirement.
5. Use higher reasoning for consequential planning or unresolved disagreement, not by default on every stage.
6. Confirm the launch preflight. Never silently substitute a provider, model, or effort.

Refresh catalog research only when model availability needs investigation:

```powershell
python -m toledo_orchestrator catalog-research
```

Live provider probes incur calls. Run `python -m toledo_orchestrator pong --live` only with explicit bounded `--case PROVIDER:MODEL:EFFORT` entries and an appropriate `--max-calls`.

## Persistent and one-turn changes

Change a saved workflow profile:

```powershell
python -m toledo_orchestrator profile-set --workflow WORKFLOW --profile PROFILE --model MODEL --effort EFFORT
```

Prefer a saved variant when the change belongs to one reusable workflow rather than the global route default.

Change only the next turn of an existing run:

```powershell
python -m toledo_orchestrator override RUN_ID --model MODEL --effort EFFORT --session-action new
```

A provider change requires a new physical session. Continue a session only when the provider, role boundary, and desired continuity are compatible. Never force session continuation across independent-review boundaries merely to save latency.

## Verification

For each substantive run, compare:

- configured provider, model, effort, and session action;
- observed provider metadata after successful execution;
- provider errors or missing observations;
- latency and validation quality where relevant.

Report mismatches explicitly. Do not reinterpret a missing observed value as a match.

## Cost and reliability practices

- Keep ordinary loops bounded.
- Use specialist workflows only when their distinct review topology adds value.
- Avoid probing every model.
- Preserve provider independence for genuine critique.
- Prefer deterministic local checks over asking another model to infer test results.
- Stop repeated provider failures and diagnose authentication, entitlement, CLI version, timeout, or session state before retrying.
