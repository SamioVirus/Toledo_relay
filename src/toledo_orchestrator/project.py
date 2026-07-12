from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ValidationDefinition:
    id: str
    command: str
    environment: str
    required: bool = True


@dataclass(frozen=True)
class ProjectDefinition:
    id: str
    root: Path
    read_only: bool
    instruction_files: tuple[str, ...]
    validations: tuple[ValidationDefinition, ...]
    implementation_enabled: bool = False
    write_allowlist: tuple[str, ...] = ()
    commit_on_accept: bool = True
    allow_no_validations: bool = False
    validation_requires_approval: bool = True

    def __post_init__(self) -> None:
        for item in self.instruction_files + self.write_allowlist:
            relative = Path(item)
            target = (self.root / relative).resolve()
            if relative.is_absolute() or (target != self.root and self.root not in target.parents):
                raise ValueError(f"project path escapes root: {item}")
        validation_ids: set[str] = set()
        for validation in self.validations:
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", validation.id):
                raise ValueError(f"invalid validation id: {validation.id}")
            if validation.id in validation_ids:
                raise ValueError(f"duplicate validation id: {validation.id}")
            validation_ids.add(validation.id)
            if not validation.command.strip():
                raise ValueError(f"validation command cannot be empty: {validation.id}")
        if (
            self.implementation_enabled
            and not self.allow_no_validations
            and not any(
                item.required and item.environment in {"local", "either"}
                for item in self.validations
            )
        ):
            raise ValueError("implementation-enabled projects require a required local validation or explicit allow_no_validations")

    @classmethod
    def from_file(cls, path: Path) -> "ProjectDefinition":
        return cls.from_value(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> "ProjectDefinition":
        root = Path(str(value["root"])).expanduser().resolve()
        validations = tuple(
            ValidationDefinition(
                str(item["id"]),
                str(item["command"]),
                str(item["environment"]),
                bool(item.get("required", True)),
            )
            for item in value.get("validations", [])
        )
        if not value.get("read_only"):
            raise ValueError("M1 project definitions must be read-only")
        if any(item.environment not in {"local", "vps", "either"} for item in validations):
            raise ValueError("project validation environment must be local, vps, or either")
        implementation = value.get("implementation", {})
        implementation_enabled = bool(implementation.get("enabled", False))
        write_allowlist = tuple(str(item) for item in implementation.get("write_allowlist", []))
        if implementation_enabled and not write_allowlist:
            raise ValueError("implementation-enabled projects require a write allowlist")
        return cls(
            id=str(value["id"]),
            root=root,
            read_only=True,
            instruction_files=tuple(str(item) for item in value.get("instruction_files", [])),
            validations=validations,
            implementation_enabled=implementation_enabled,
            write_allowlist=write_allowlist,
            commit_on_accept=bool(implementation.get("commit_on_accept", True)),
            allow_no_validations=bool(implementation.get("allow_no_validations", False)),
            validation_requires_approval=bool(implementation.get("validation_requires_approval", True)),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "root": str(self.root),
            "read_only": self.read_only,
            "instruction_files": list(self.instruction_files),
            "validations": [
                {
                    "id": item.id,
                    "command": item.command,
                    "environment": item.environment,
                    "required": item.required,
                }
                for item in self.validations
            ],
            "implementation": {
                "enabled": self.implementation_enabled,
                "write_allowlist": list(self.write_allowlist),
                "commit_on_accept": self.commit_on_accept,
                "allow_no_validations": self.allow_no_validations,
                "validation_requires_approval": self.validation_requires_approval,
            },
        }

    def revision(self) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            raise ValueError(f"project revision lookup failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def git_context(self) -> tuple[str | None, bool | None]:
        branch = subprocess.run(
            ["git", "-C", str(self.root), "branch", "--show-current"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        status = subprocess.run(
            ["git", "-C", str(self.root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        return (
            branch.stdout.strip() or None if branch.returncode == 0 else None,
            bool(status.stdout) if status.returncode == 0 else None,
        )

    def check(self) -> dict[str, Any]:
        instruction_status = {item: (self.root / item).resolve().is_file() for item in self.instruction_files}
        try:
            revision = self.revision()
        except ValueError as error:
            revision = None
            error_text = str(error)
        else:
            error_text = None
        ready = self.root.is_dir() and revision is not None and all(instruction_status.values())
        branch, dirty = self.git_context() if self.root.is_dir() else (None, None)
        implementation_ready = ready and (not self.implementation_enabled or dirty is False)
        return {
            "id": self.id,
            "root": str(self.root),
            "read_only": self.read_only,
            "implementation_enabled": self.implementation_enabled,
            "write_allowlist": list(self.write_allowlist),
            "commit_on_accept": self.commit_on_accept,
            "allow_no_validations": self.allow_no_validations,
            "validation_requires_approval": self.validation_requires_approval,
            "validations": [
                {
                    "id": item.id,
                    "command": item.command,
                    "environment": item.environment,
                    "required": item.required,
                }
                for item in self.validations
            ],
            "source_revision": revision,
            "branch": branch,
            "dirty": dirty,
            "implementation_ready": implementation_ready,
            "implementation_error": (
                "source checkout must be clean" if self.implementation_enabled and dirty else
                "source checkout status is unavailable" if self.implementation_enabled and dirty is None else
                None
            ),
            "instruction_files": instruction_status,
            "ready": ready,
            "error": error_text,
        }


def load_projects(directory: Path | None = None) -> dict[str, ProjectDefinition]:
    root = directory or Path(__file__).with_name("projects")
    projects = {}
    for path in sorted(root.glob("*.json")):
        project = ProjectDefinition.from_file(path)
        if project.id in projects:
            raise ValueError(f"duplicate project definition: {project.id}")
        projects[project.id] = project
    return projects
