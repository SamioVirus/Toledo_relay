# Install and update Relay

## Contents

- Requirements
- First installation
- Install in Codex and Claude
- Freshness and safe update behavior
- Removal or repair

## Requirements

Require:

- Git and Python 3.11 or newer;
- a local checkout of the canonical repository;
- Codex CLI and/or Claude Code installed and authenticated for workflows that use them;
- network access for the initial clone and freshness check;
- a clean, ordinary Git checkout for updates.

Relay is a local controller. Uploading only `SKILL.md` to a remote chat does not grant that chat access to the local Relay runtime, Git repositories, provider CLIs, or durable run store.

## First installation

Clone the complete repository because the skill, controller, packaged workflows, default prompts, tests, and installer evolve together:

```powershell
git clone https://github.com/SamioVirus/Toledo_relay.git
Set-Location Toledo_relay
python -m pip install -e .
python -m toledo_orchestrator check
```

Do not copy only the skill folder if the agent is expected to execute Relay. A documentation-only copy can explain Relay but cannot operate it.

## Install in Codex and Claude

Link the canonical skill directory into both app discovery locations:

```powershell
python skills/relay-use-skill/scripts/manage_skill.py install --target codex --target claude --target agents --remove-legacy
```

The defaults are:

- Codex: `~/.codex/skills/relay-use-skill`
- Claude: `~/.claude/skills/relay-use-skill`
- Agent Skills compatibility: `~/.agents/skills/relay-use-skill`

The installer creates a directory junction on Windows and a symbolic link elsewhere. It refuses to overwrite a real directory or a link to a different source. Use `--codex-root` or `--claude-root` for nonstandard app locations.

Restart or reload the app after first installation so it rediscovers the skill. Invoke it as `$relay-use-skill`. Its user-facing display label is `relay_use_skill`.

## Freshness and safe update behavior

Check freshness before each Relay operation:

```powershell
python skills/relay-use-skill/scripts/manage_skill.py status
```

`status` fetches canonical `main` without switching branches or modifying tracked files. Interpret the result as follows:

- `current`: local `HEAD` equals canonical `main`;
- `ahead`: local work contains canonical `main`; do not discard it;
- `behind`: a clean local `main` may fast-forward with `update`;
- `diverged`: stop and reconcile intentionally;
- `dirty`: preserve local work; do not auto-update.

Safe update:

```powershell
python skills/relay-use-skill/scripts/manage_skill.py update
python -m pip install -e .
python -m toledo_orchestrator check
```

`update` refuses dirty worktrees, non-`main` branches, and divergence. It never resets, force-pulls, deletes changes, or rewrites history.

## Removal or repair

Run the installer again to repair a missing correct link. With `--remove-legacy`, it removes only the old `relay-operator` link when that entry is a link or junction into this repository. It does not delete ordinary directories.

To uninstall manually, remove only the app-discovery link. Do not recursively delete the repository through a linked path.
