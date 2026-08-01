from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from toledo_orchestrator.worktree import collect_worktree_evidence


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True,
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "user.email", "test@local.invalid")
    (root / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_collect_evidence_reports_untracked_files(repo: Path) -> None:
    (repo / "new-file.txt").write_text("hello\n", encoding="utf-8")
    evidence = collect_worktree_evidence(repo)
    assert "new-file.txt" in evidence.changed_paths
    assert b"new-file.txt" in evidence.patch


def test_collect_evidence_leaves_index_clean(repo: Path) -> None:
    """Evidence collection must not leave intent-to-add entries behind.

    The per-worktree index lives under the project's shared .git directory,
    which the sandboxed implementer cannot write. A leftover "A" entry is
    therefore visible to the implementer but impossible for it to remove,
    which previously trapped runs in a repair loop.
    """
    (repo / "scratch.txt").write_text("temp\n", encoding="utf-8")
    collect_worktree_evidence(repo)
    staged = _git(repo, "diff", "--cached", "--name-only")
    assert staged.strip() == ""
    porcelain = _git(repo, "status", "--porcelain")
    assert "?? scratch.txt" in porcelain
    assert "A" not in {line[:1] for line in porcelain.splitlines()}


def test_collect_evidence_is_repeatable_after_reset(repo: Path) -> None:
    (repo / "again.txt").write_text("x\n", encoding="utf-8")
    first = collect_worktree_evidence(repo)
    second = collect_worktree_evidence(repo)
    assert first.changed_paths == second.changed_paths
    assert first.patch == second.patch


def test_evidence_exclusions_remove_only_untracked_scratch(repo: Path) -> None:
    scratch = repo / "scratch"
    scratch.mkdir()
    (scratch / "runtime.json").write_text("generated\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

    evidence = collect_worktree_evidence(repo, ("scratch",))

    assert evidence.changed_paths == ("tracked.txt",)
    assert b"runtime.json" not in evidence.patch
    assert evidence.excluded_untracked_count == 1
    assert evidence.excluded_untracked_roots == (("scratch", 1),)


def test_evidence_exclusions_never_hide_tracked_changes(repo: Path) -> None:
    generated = repo / "scratch"
    generated.mkdir()
    tracked = generated / "tracked.txt"
    tracked.write_text("original\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tracked scratch fixture")
    tracked.write_text("changed\n", encoding="utf-8")

    evidence = collect_worktree_evidence(repo, ("scratch",))

    assert evidence.changed_paths == ("scratch/tracked.txt",)
    assert b"scratch/tracked.txt" in evidence.patch
