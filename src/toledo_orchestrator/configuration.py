from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import atomic_write
from .project import ProjectDefinition, load_projects
from .workflow import WorkflowDefinition, load_workflow_layers


def configuration_dir(runtime_dir: Path) -> Path:
    return runtime_dir / "config"


def load_configured_workflows(runtime_dir: Path) -> dict[str, WorkflowDefinition]:
    packaged = sorted(Path(__file__).with_name("workflows").glob("*.json"))
    override_dir = configuration_dir(runtime_dir) / "workflows"
    overrides = sorted(override_dir.glob("*.json")) if override_dir.is_dir() else []
    return load_workflow_layers(packaged, overrides)


def load_configured_projects(runtime_dir: Path) -> dict[str, ProjectDefinition]:
    projects = load_projects()
    override_dir = configuration_dir(runtime_dir) / "projects"
    if override_dir.is_dir():
        for path in sorted(override_dir.glob("*.json")):
            value = ProjectDefinition.from_file(path)
            projects[value.id] = value
    return projects


def workflow_source(workflow_id: str, runtime_dir: Path) -> Path:
    override = configuration_dir(runtime_dir) / "workflows" / f"{workflow_id}.json"
    if override.is_file():
        return override
    packaged = Path(__file__).with_name("workflows") / f"{workflow_id}.json"
    if not packaged.is_file():
        raise ValueError(f"unknown workflow: {workflow_id}")
    return packaged


def workflow_value(workflow_id: str, runtime_dir: Path) -> dict[str, Any]:
    workflows = load_configured_workflows(runtime_dir)
    if workflow_id not in workflows:
        raise ValueError(f"unknown workflow: {workflow_id}")
    return workflows[workflow_id].snapshot()


def save_workflow_value(runtime_dir: Path, value: dict[str, Any]) -> WorkflowDefinition:
    workflow_id = str(value.get("id", ""))
    if not workflow_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in workflow_id):
        raise ValueError("workflow id must use lowercase letters, digits, hyphen, or underscore")
    target = configuration_dir(runtime_dir) / "workflows" / f"{workflow_id}.json"
    candidate = target.with_suffix(".candidate.json")
    atomic_write(candidate, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    try:
        parsed = WorkflowDefinition.from_file(candidate)
    finally:
        candidate.unlink(missing_ok=True)
    atomic_write(target, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return parsed


def update_profile(
    runtime_dir: Path,
    workflow_id: str,
    profile_id: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    permission: str | None = None,
    label: str | None = None,
) -> WorkflowDefinition:
    value = workflow_value(workflow_id, runtime_dir)
    profiles = value.get("profiles", {})
    if profile_id not in profiles:
        raise ValueError(f"unknown profile: {profile_id}")
    changes = {"model": model, "effort": effort, "permission": permission, "label": label}
    for key, selected in changes.items():
        if selected is not None:
            profiles[profile_id][key] = selected
    # A label is presentation only.  When an operator changes selection fields
    # without deliberately setting a custom label, keep the visible label
    # truthful by deriving it from the persisted values.
    if label is None and (model is not None or effort is not None):
        profiles[profile_id]["label"] = f"{profiles[profile_id]['model']} · {profiles[profile_id]['effort']}"
    return save_workflow_value(runtime_dir, value)


def save_project_value(runtime_dir: Path, value: dict[str, Any]) -> ProjectDefinition:
    project_id = str(value.get("id", ""))
    if not project_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in project_id):
        raise ValueError("project id must use lowercase letters, digits, hyphen, or underscore")
    target = configuration_dir(runtime_dir) / "projects" / f"{project_id}.json"
    candidate = target.with_suffix(".candidate.json")
    atomic_write(candidate, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    try:
        parsed = ProjectDefinition.from_file(candidate)
    finally:
        candidate.unlink(missing_ok=True)
    atomic_write(target, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return parsed
