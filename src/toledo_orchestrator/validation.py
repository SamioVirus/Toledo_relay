from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .core import atomic_write, sha256
from .project import ProjectDefinition


# Test-runner failure identity lines (pytest and friends). The part after
# " - " carries exception text with absolute paths, which differ between the
# execution worktree and a baseline worktree, so identity stops at the node id.
FAILURE_LINE = re.compile(r"^(?:FAILED|ERROR)\s+\S+", re.MULTILINE)


def failure_signature(stdout_text: str, stderr_text: str, exit_code: int) -> dict[str, Any]:
    """Environment-independent identity of a failed validation.

    Two runs of the same command fail "the same way" when their signature
    fingerprints match. A signature without recognizable failure lines is
    weak: it identifies only the exit code and must not be treated as strong
    evidence of sameness.
    """
    lines = FAILURE_LINE.findall(stdout_text) or FAILURE_LINE.findall(stderr_text)
    normalized = sorted({" ".join(line.split()) for line in lines})
    weak = not normalized
    if weak:
        normalized = [f"exit:{exit_code}"]
    digest = sha256("\n".join([str(exit_code), *normalized]).encode("utf-8"))
    return {"exit_code": exit_code, "lines": normalized, "weak": weak, "fingerprint": digest}


def run_project_validations(
    project: ProjectDefinition,
    worktree: Path,
    run_dir: Path,
    cycle_number: int,
    turn_number: int,
    timeout_seconds: int = 1800,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    validation_dir = run_dir / "validations"
    for definition in project.validations:
        if definition.environment == "vps":
            results[definition.id] = {
                "state": "pending_remote",
                "command": definition.command,
                "environment": definition.environment,
                "required": definition.required,
            }
            continue
        started = time.monotonic()
        try:
            completed = subprocess.run(
                definition.command,
                cwd=worktree,
                shell=True,
                capture_output=True,
                timeout=timeout_seconds,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            exit_code = completed.returncode
            state = "passed" if exit_code == 0 else "failed"
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout or b""
            stderr = error.stderr or b""
            exit_code = 124
            state = "failed"
        elapsed_ms = int((time.monotonic() - started) * 1000)
        prefix = f"cycle.{cycle_number:04d}.turn.{turn_number:04d}.{definition.id}"
        stdout_path = validation_dir / f"{prefix}.stdout.raw"
        stderr_path = validation_dir / f"{prefix}.stderr.raw"
        results[definition.id] = {
            "state": state,
            "command": definition.command,
            "environment": definition.environment,
            "required": definition.required,
            "exit_code": exit_code,
            "elapsed_ms": elapsed_ms,
            "stdout": {"path": str(stdout_path.relative_to(run_dir)), "sha256": atomic_write(stdout_path, stdout)},
            "stderr": {"path": str(stderr_path.relative_to(run_dir)), "sha256": atomic_write(stderr_path, stderr)},
        }
    return results


def required_local_validations_passed(results: dict[str, dict[str, Any]]) -> bool:
    return all(not value.get("required", True) or value.get("state") == "passed" for value in results.values())


def pending_required_validations(results: dict[str, dict[str, Any]]) -> list[str]:
    return [
        key for key, value in results.items()
        if value.get("required", True) and value.get("state") == "pending_remote"
    ]
