# Relay repository operating contract

The canonical public repository is `https://github.com/SamioVirus/Toledo_relay`.

Before changing Relay, read `skills/relay-use-skill/SKILL.md` and the bundled reference that owns the affected behavior. Preserve runtime runs, provider output, secrets, user requests, and unrelated working-tree changes.

Any operator-visible change to Relay must update `skills/relay-use-skill/` in the same commit when the operating contract changes. This includes CLI commands, project configuration, workflow graphs or defaults, prompts, provider/model/session controls, loop and gate behavior, evidence, validation, recovery, cleanup, UI operating behavior, and final outputs.

Run focused tests, `tests/test_relay_skill_sync.py`, the skill validator, Python compilation, and `git diff --check`. Do not call a Relay change delivered until its branch is pushed to the canonical GitHub repository and the branch or pull request is reported. If the active task does not authorize an external write, stop before pushing and report publication as the remaining gate.

Never publish local runtime data, raw provider streams, credentials, private requests, environment files, or generated run worktrees. Never rewrite shared history or force-push without explicit user authorization.
