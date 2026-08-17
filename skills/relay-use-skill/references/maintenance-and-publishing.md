# Maintain and publish Relay

## Contents

- Canonical repository
- Coupled change contract
- Required verification
- Publishing workflow
- Safe update policy

## Canonical repository

The public source of truth is:

```text
https://github.com/SamioVirus/Toledo_relay
```

Keep a Git remote pointing to that repository. A local bare mirror may remain as a secondary durability remote, but it is not the public distribution source.

## Coupled change contract

Update `skills/relay-use-skill/` in the same change whenever operator-visible behavior changes, including:

- CLI commands or arguments;
- project registration or isolation rules;
- workflow schema, packaged workflows, transitions, caps, or stacks;
- stage prompts, prompt assembly, context, or owner direction;
- provider catalogs, profile controls, models, effort, sessions, or observation;
- continuous-loop, gate, validation, recovery, cleanup, or evidence semantics;
- UI behavior an operating agent relies on;
- installation, update, or final-report contracts.

Do not duplicate volatile model IDs in the skill. Make the skill query live Relay state. Keep all required operating references inside the skill package.

## Required verification

Run at minimum:

```powershell
python -m pytest tests/test_agent_bridge.py tests/test_relay_skill_sync.py -q
python C:\path\to\skill-creator\scripts\quick_validate.py skills/relay-use-skill
python -m compileall -q src skills/relay-use-skill/scripts
git diff --check
```

Run focused subsystem tests for the actual Relay change, and the full suite when risk warrants it. Test management scripts without touching a real app directory before installing them.

## Publishing workflow

Before declaring a Relay change delivered:

1. inspect `git status` and the complete intended diff;
2. preserve unrelated work and stage explicit paths;
3. commit on a scoped branch;
4. push that branch to `https://github.com/SamioVirus/Toledo_relay`;
5. open or update a pull request against canonical `main`;
6. report the branch, commit, checks, and pull-request URL.

Do not publish secrets, runtime run directories, provider streams, local configuration, or private user requests. Do not force-push or rewrite shared history unless the user explicitly authorizes it.

If the current task does not authorize external writes, stop before pushing. Report publication as an explicit remaining gate; do not call the change fully delivered.

## Safe update policy

The installed Codex and Claude skills should be links to the repository copy. `manage_skill.py status` fetches canonical `main`; `update` performs only a clean fast-forward on local `main`. Dirty, ahead, or divergent work requires deliberate Git handling.

After an update, reinstall the editable Python package if source code changed, rerun `check`, and restart long-lived UI/controller processes before assuming they use the new code.
