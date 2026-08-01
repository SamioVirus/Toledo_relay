from __future__ import annotations

import subprocess
from collections import Counter
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
    excluded_untracked_count: int = 0
    excluded_untracked_roots: tuple[tuple[str, int], ...] = ()


def _git(
    root: Path,
    *args: str,
    timeout: int = 60,
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        timeout=timeout,
        input=input_data,
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


def _normalized_prefixes(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({value.replace("\\", "/").strip("/") for value in values}))


def _under_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in prefixes)


def collect_worktree_evidence(
    root: Path,
    evidence_exclude_paths: tuple[str, ...] = (),
) -> WorktreeEvidence:
    """Collect tracked changes plus relevant untracked files.

    Project evidence exclusions apply only to untracked files. A tracked file is
    always reviewable even if its path later falls under an excluded scratch
    directory, preventing configuration from hiding an accepted source change.
    """

    prefixes = _normalized_prefixes(evidence_exclude_paths)
    tracked = tuple(
        value.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for value in _git(root, "diff", "--name-only", "-z", "HEAD", "--").stdout.split(b"\0")
        if value
    )
    untracked = tuple(
        value.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for value in _git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout.split(b"\0")
        if value
    )
    included_untracked = tuple(path for path in untracked if not _under_prefix(path, prefixes))
    excluded_untracked = tuple(path for path in untracked if _under_prefix(path, prefixes))
    tracked_status = _git(root, "status", "--short", "--untracked-files=no").stdout.decode(
        "utf-8", errors="replace"
    )
    if included_untracked:
        # Intent-to-add exposes selected new files to `git diff` without staging
        # their content. Feeding NUL-delimited paths avoids command-line limits.
        pathspec = b"\0".join(path.encode("utf-8", errors="surrogateescape") for path in included_untracked) + b"\0"
        _git(
            root,
            "add",
            "-N",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            timeout=120,
            input_data=pathspec,
        )
    status = tracked_status + "".join(f"?? {path}\n" for path in included_untracked)
    paths = tuple(sorted(set(tracked + included_untracked)))
    patch = _git(root, "diff", "--binary", "HEAD", "--").stdout
    # Evidence collection must be side-effect-free: drop the intent-to-add
    # entries once the snapshot is taken. The per-worktree index lives under
    # the project's shared .git directory, outside the sandboxed implementer's
    # writable workspace, so an entry left behind here is one the implementer
    # can see (`git status` reports "A") but can never remove itself.
    if included_untracked:
        _git(root, "reset", "-q")
    roots = Counter(path.split("/", 1)[0] for path in excluded_untracked)
    return WorktreeEvidence(
        revision=current_revision(root),
        changed_paths=paths,
        patch=patch,
        status=status,
        excluded_untracked_count=len(excluded_untracked),
        excluded_untracked_roots=tuple(sorted(roots.items())),
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
        "excluded_untracked": {
            "count": evidence.excluded_untracked_count,
            "top_level_paths": dict(evidence.excluded_untracked_roots),
            "policy": "project evidence exclusions; untracked files only",
        },
    }


def read_worktree_path(run_dir: Path) -> str:
    import json

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    return str(state["execution_worktree"])


def commit_accepted_changes(
    root: Path,
    message: str,
    expected_patch: bytes,
    hooks_dir: Path,
    evidence_exclude_paths: tuple[str, ...] = (),
) -> str:
    evidence = collect_worktree_evidence(root, evidence_exclude_paths)
    if not evidence.changed_paths:
        if expected_patch:
            raise ValueError("reviewed patch is non-empty but the worktree has no changes")
        return evidence.revision
    stage_paths = b"\0".join(
        path.encode("utf-8", errors="surrogateescape") for path in evidence.changed_paths
    ) + b"\0"
    _git(
        root,
        "add",
        "-A",
        "--pathspec-from-file=-",
        "--pathspec-file-nul",
        timeout=120,
        input_data=stage_paths,
    )
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
    if collect_worktree_evidence(root, evidence_exclude_paths).changed_paths:
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
    try:
        _git(project.root, *arguments, timeout=120)
    except ValueError as error:
        if force and resolved.exists():
            # Observed failure mode on Windows: directories created inside the
            # worktree by the sandboxed provider are owned by a discarded
            # sandbox SID and deny access even to the owning user, so git
            # cannot delete them. Only an elevated shell can reclaim them.
            raise ValueError(
                f"{error}\n"
                "If access was denied, the worktree may contain directories created "
                "under a discarded provider-sandbox identity. From an elevated shell: "
                f'takeown /f "{resolved}" /r /d y, then icacls "{resolved}" /reset /t, '
                f'then remove the directory and run git worktree prune in {project.root}.'
            ) from None
        raise
