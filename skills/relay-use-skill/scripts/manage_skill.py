from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence


CANONICAL_URL = "https://github.com/SamioVirus/Toledo_relay.git"
CANONICAL_BRANCH = "main"
REMOTE_REF = "refs/remotes/relay-github/main"
SKILL_NAME = "relay-use-skill"
LEGACY_NAME = "relay-operator"


def repository_root() -> Path:
    candidate = Path(__file__).resolve().parents[3]
    if not (candidate / "pyproject.toml").is_file() or not (candidate / ".git").exists():
        raise RuntimeError("manage_skill.py must run from a complete Toledo Relay Git checkout")
    return candidate


def run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def is_ancestor(root: Path, older: str, newer: str) -> bool:
    return run_git(root, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def freshness(root: Path, *, fetch: bool = True) -> dict[str, object]:
    if fetch:
        run_git(
            root,
            "fetch",
            "--no-tags",
            CANONICAL_URL,
            f"+refs/heads/{CANONICAL_BRANCH}:{REMOTE_REF}",
        )
    head = run_git(root, "rev-parse", "HEAD").stdout.strip()
    remote = run_git(root, "rev-parse", REMOTE_REF).stdout.strip()
    branch = run_git(root, "branch", "--show-current").stdout.strip()
    dirty = bool(run_git(root, "status", "--porcelain").stdout.strip())
    if head == remote:
        relation = "current"
    elif is_ancestor(root, head, remote):
        relation = "behind"
    elif is_ancestor(root, remote, head):
        relation = "ahead"
    else:
        relation = "diverged"
    return {
        "schema_version": "toledo_relay.skill_status.v1",
        "canonical_url": CANONICAL_URL,
        "canonical_branch": CANONICAL_BRANCH,
        "repository": str(root),
        "branch": branch,
        "head": head,
        "canonical_head": remote,
        "relation": relation,
        "dirty": dirty,
    }


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", lambda: False)
    return path.is_symlink() or bool(is_junction())


def _remove_link(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    else:
        os.rmdir(path)


def _link_directory(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(source)],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        target.symlink_to(source, target_is_directory=True)


def install_target(source: Path, target_root: Path, *, remove_legacy: bool) -> dict[str, str]:
    target_root = target_root.expanduser().resolve()
    target = target_root / SKILL_NAME
    if target.exists() or _is_link_or_junction(target):
        if not _is_link_or_junction(target):
            raise RuntimeError(f"refusing to replace ordinary directory: {target}")
        if target.resolve(strict=False) != source.resolve():
            raise RuntimeError(f"refusing to replace link to another source: {target}")
        action = "already-installed"
    else:
        _link_directory(source, target)
        action = "installed"

    legacy_action = "not-requested"
    legacy = target_root / LEGACY_NAME
    if remove_legacy:
        if _is_link_or_junction(legacy):
            resolved = legacy.resolve(strict=False)
            allowed = {
                source.resolve(),
                (source.parent / LEGACY_NAME).resolve(strict=False),
            }
            if resolved not in allowed:
                raise RuntimeError(f"refusing to remove legacy link to another source: {legacy}")
            _remove_link(legacy)
            legacy_action = "removed"
        elif legacy.exists():
            raise RuntimeError(f"refusing to remove ordinary legacy directory: {legacy}")
        else:
            legacy_action = "absent"

    return {
        "target": str(target),
        "source": str(source),
        "action": action,
        "legacy": legacy_action,
    }


def update_checkout(root: Path) -> dict[str, object]:
    state = freshness(root, fetch=True)
    relation = str(state["relation"])
    if relation in {"current", "ahead"}:
        state["update"] = "not-needed"
        return state
    if state["dirty"]:
        raise RuntimeError("refusing to update a dirty Relay checkout")
    if state["branch"] != CANONICAL_BRANCH:
        raise RuntimeError("safe update requires the local main branch")
    if relation != "behind":
        raise RuntimeError(f"refusing to update a {relation} Relay checkout")
    run_git(root, "merge", "--ff-only", REMOTE_REF)
    state = freshness(root, fetch=False)
    state["update"] = "fast-forwarded"
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install and safely update relay-use-skill.")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="fetch canonical main and report freshness")
    status.add_argument("--no-fetch", action="store_true")
    commands.add_parser("update", help="fast-forward a clean local main checkout")
    install = commands.add_parser("install", help="link the skill into Codex, Claude, and/or Agent Skills")
    install.add_argument(
        "--target",
        action="append",
        choices=("codex", "claude", "agents"),
        required=True,
    )
    install.add_argument("--codex-root", type=Path, default=Path.home() / ".codex" / "skills")
    install.add_argument("--claude-root", type=Path, default=Path.home() / ".claude" / "skills")
    install.add_argument("--agents-root", type=Path, default=Path.home() / ".agents" / "skills")
    install.add_argument("--remove-legacy", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = repository_root()
    if args.command == "status":
        value: object = freshness(root, fetch=not args.no_fetch)
    elif args.command == "update":
        value = update_checkout(root)
    else:
        source = (root / "skills" / SKILL_NAME).resolve()
        roots = {
            "codex": args.codex_root,
            "claude": args.claude_root,
            "agents": args.agents_root,
        }
        value = [
            install_target(source, roots[target], remove_legacy=args.remove_legacy)
            for target in dict.fromkeys(args.target)
        ]
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
