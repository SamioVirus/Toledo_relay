from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .core import atomic_write, sha256
from .project import ProjectDefinition


@dataclass(frozen=True)
class WorktreeEvidence:
    revision: str
    changed_paths: tuple[str, ...]
    patch: bytes
    status: str


def _git(root: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git {' '.join(args)} failed: {detail}")
    return result


def create_execution_worktree(project: ProjectDefinition, target: Path, revision: str, branch: str | None = None) -> Path:
    if not project.implementation_enabled:
        raise ValueError(f"project {project.id} does not permit implementation")
    target = target.resolve()
    if target.exists():
        raise ValueError(f"execution worktree already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if branch:
        _git(project.root, "worktree", "add", "-b", branch, str(target), revision, timeout=120)
    else:
        _git(project.root, "worktree", "add", "--detach", str(target), revision, timeout=120)
    return target


def current_revision(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.decode("ascii", errors="strict").strip()


def current_branch(root: Path) -> str | None:
    value = _git(root, "branch", "--show-current").stdout.decode("utf-8", errors="replace").strip()
    return value or None


def parent_revision(root: Path, revision: str = "HEAD") -> str:
    return _git(root, "rev-parse", f"{revision}^").stdout.decode("ascii", errors="strict").strip()


def revision_patch(root: Path, base: str, target: str = "HEAD") -> bytes:
    return _git(root, "diff", "--binary", base, target, "--").stdout


def collect_worktree_evidence(root: Path) -> WorktreeEvidence:
    # Intent-to-add exposes new files to `git diff` without staging their content.
    _git(root, "add", "-N", "--", ".")
    status_bytes = _git(root, "status", "--short").stdout
    status = status_bytes.decode("utf-8", errors="replace")
    paths: list[str] = []
    for line in status.splitlines():
        value = line[3:].strip() if len(line) >= 4 else ""
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        if value:
            paths.append(value.replace("\\", "/"))
    patch = _git(root, "diff", "--binary", "HEAD", "--").stdout
    return WorktreeEvidence(
        revision=current_revision(root),
        changed_paths=tuple(sorted(set(paths))),
        patch=patch,
        status=status,
    )


def assert_allowed_changes(project: ProjectDefinition, paths: tuple[str, ...]) -> None:
    allowed = tuple(item.replace("\\", "/").rstrip("/") for item in project.write_allowlist)
    if "." in allowed:
        return
    denied = []
    for path in paths:
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not any(normalized == item or normalized.startswith(item + "/") for item in allowed):
            denied.append(path)
    if denied:
        raise ValueError(f"implementation changed paths outside the allowlist: {', '.join(denied)}")


def seal_worktree_evidence(
    run_dir: Path,
    cycle_number: int,
    turn_number: int,
    evidence: WorktreeEvidence,
    kind: str = "implementation",
) -> dict[str, object]:
    prefix = f"cycle.{cycle_number:04d}.turn.{turn_number:04d}.{kind}"
    patch_path = run_dir / "evidence" / f"{prefix}.patch"
    status_path = run_dir / "evidence" / f"{prefix}.status.txt"
    patch_hash = atomic_write(patch_path, evidence.patch)
    status_hash = atomic_write(status_path, evidence.status.encode("utf-8"))
    untracked: list[dict[str, str]] = []
    worktree = Path(read_worktree_path(run_dir))
    for relative in evidence.changed_paths:
        target = worktree / relative
        if target.is_file():
            data = target.read_bytes()
            untracked.append({"path": relative, "sha256": sha256(data)})
    return {
        "revision": evidence.revision,
        "changed_paths": list(evidence.changed_paths),
        "patch": {"path": str(patch_path.relative_to(run_dir)), "sha256": patch_hash},
        "status": {"path": str(status_path.relative_to(run_dir)), "sha256": status_hash},
        "file_hashes": untracked,
    }


def read_worktree_path(run_dir: Path) -> str:
    import json

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    return str(state["execution_worktree"])


def commit_accepted_changes(root: Path, message: str, expected_patch: bytes, hooks_dir: Path) -> str:
    evidence = collect_worktree_evidence(root)
    if not evidence.changed_paths:
        if expected_patch:
            raise ValueError("reviewed patch is non-empty but the worktree has no changes")
        return evidence.revision
    _git(root, "add", "-A")
    staged_patch = _git(root, "diff", "--cached", "--binary", "HEAD", "--").stdout
    if staged_patch != expected_patch:
        _git(root, "reset")
        raise ValueError("staged implementation does not match the reviewed patch")
    hooks_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "git", "-C", str(root),
            "-c", "user.name=Toledo Orchestrator",
            "-c", "user.email=orchestrator@local.invalid",
            "-c", f"core.hooksPath={hooks_dir}",
            "-c", "commit.gpgSign=false",
            "commit", "-m", message,
        ],
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"accepted implementation commit failed: {detail}")
    accepted = current_revision(root)
    if collect_worktree_evidence(root).changed_paths:
        raise ValueError("accepted implementation commit left a dirty execution worktree")
    return accepted


def remove_execution_worktree(project: ProjectDefinition, root: Path, *, force: bool = False) -> None:
    resolved = root.resolve()
    if resolved == project.root or project.root in resolved.parents:
        raise ValueError("refusing to remove the source checkout as an execution worktree")
    arguments = ["worktree", "remove"]
    if force:
        arguments.append("--force")
    arguments.append(str(resolved))
    _git(project.root, *arguments, timeout=120)
