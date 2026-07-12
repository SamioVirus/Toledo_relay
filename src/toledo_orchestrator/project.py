from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ValidationDefinition:
    id: str
    command: str
    environment: str


@dataclass(frozen=True)
class ProjectDefinition:
    id: str
    root: Path
    read_only: bool
    instruction_files: tuple[str, ...]
    validations: tuple[ValidationDefinition, ...]

    def __post_init__(self) -> None:
        for item in self.instruction_files:
            relative = Path(item)
            target = (self.root / relative).resolve()
            if relative.is_absolute() or (target != self.root and self.root not in target.parents):
                raise ValueError(f"project instruction path escapes root: {item}")

    @classmethod
    def from_file(cls, path: Path) -> "ProjectDefinition":
        value = json.loads(path.read_text(encoding="utf-8"))
        root = Path(str(value["root"])).expanduser().resolve()
        validations = tuple(
            ValidationDefinition(str(item["id"]), str(item["command"]), str(item["environment"]))
            for item in value.get("validations", [])
        )
        if not value.get("read_only"):
            raise ValueError("M1 project definitions must be read-only")
        if any(item.environment not in {"local", "vps", "either"} for item in validations):
            raise ValueError("project validation environment must be local, vps, or either")
        return cls(
            id=str(value["id"]),
            root=root,
            read_only=True,
            instruction_files=tuple(str(item) for item in value.get("instruction_files", [])),
            validations=validations,
        )

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
        return {
            "id": self.id,
            "root": str(self.root),
            "read_only": self.read_only,
            "source_revision": revision,
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
