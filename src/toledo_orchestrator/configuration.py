from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import atomic_write
from .project import ProjectDefinition, load_projects
from .workflow import (
    WorkflowDefinition,
    apply_round_overrides,
    load_workflow_layers,
    validate_prompt_overrides,
)


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


def save_workflow_variant(
    runtime_dir: Path,
    base_workflow_id: str,
    new_id: str,
    label: str,
    *,
    profile_overrides: dict[str, Any] | None = None,
    round_overrides: dict[str, Any] | None = None,
    prompt_overrides: dict[str, Any] | None = None,
    validate_profile: Any = None,
) -> WorkflowDefinition:
    """Persist New-cycle adjustments as a named workflow the selector can offer.

    The variant is a full standalone definition: the base snapshot with the
    submitted model/effort, round-cap, and instruction edits applied. Edited
    instructions are written under the new workflow's prompt namespace so the
    packaged prompt files stay untouched.
    """

    workflows = load_configured_workflows(runtime_dir)
    if base_workflow_id not in workflows:
        raise ValueError(f"unknown base workflow: {base_workflow_id}")
    new_id = str(new_id).strip()
    if not new_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in new_id):
        raise ValueError("workflow id must use lowercase letters, digits, hyphen, or underscore")
    if new_id in workflows:
        raise ValueError(f"a workflow named {new_id} already exists; choose a different name")
    label = str(label).strip() or new_id
    value = workflows[base_workflow_id].snapshot()
    value["id"] = new_id
    value["label"] = label
    if profile_overrides:
        if not isinstance(profile_overrides, dict):
            raise ValueError("profile_overrides must be an object")
        for profile_id, changes in profile_overrides.items():
            if profile_id not in value["profiles"]:
                raise ValueError(f"unknown profile override: {profile_id}")
            if not isinstance(changes, dict):
                raise ValueError(f"profile override for {profile_id} must be an object")
            target = value["profiles"][profile_id]
            provider = str(changes.get("provider") or target["provider"]).strip()
            model = str(changes.get("model") or target["model"]).strip()
            effort = str(changes.get("effort") or target["effort"]).strip()
            custom = bool(changes.get("custom"))
            if not model or not effort:
                raise ValueError(f"profile override for {profile_id} requires model and effort")
            if validate_profile is not None:
                validate_profile(provider=provider, model=model, effort=effort, custom=custom)
            target.update({"provider": provider, "model": model, "effort": effort, "custom": custom, "label": f"{model} · {effort}"})
    if round_overrides:
        apply_round_overrides(value, round_overrides)
    stage_prompt_files = {
        str(stage.get("prompt_file"))
        for stage in value.get("stages", {}).values()
        if stage.get("prompt_file")
    }
    override_bytes = validate_prompt_overrides(prompt_overrides, stage_prompt_files)
    if override_bytes:
        namespaces = [new_id, *[item for item in value.get("prompt_namespaces", []) if item != new_id]]
        value["prompt_namespaces"] = namespaces
    # Validate the complete adjusted graph before writing prompt files. A
    # rejected provider/session combination must not leave an orphan prompt
    # namespace behind for a workflow that was never saved.
    WorkflowDefinition.from_value(value)
    if override_bytes:
        for name, data in override_bytes.items():
            atomic_write(configuration_dir(runtime_dir) / "prompts" / new_id / name, data)
    return save_workflow_value(runtime_dir, value)


def update_profiles(
    runtime_dir: Path,
    workflow_id: str,
    profile_overrides: dict[str, dict[str, Any]],
) -> WorkflowDefinition:
    """Persist one or more profile defaults as one validated workflow write."""

    if not isinstance(profile_overrides, dict) or not profile_overrides:
        raise ValueError("profile_overrides must be a non-empty object")
    value = workflow_value(workflow_id, runtime_dir)
    profiles = value.get("profiles", {})
    allowed = {"provider", "model", "effort", "permission", "label", "custom"}
    for profile_id, changes in profile_overrides.items():
        if profile_id not in profiles:
            raise ValueError(f"unknown profile: {profile_id}")
        if not isinstance(changes, dict):
            raise ValueError(f"profile override for {profile_id} must be an object")
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown profile fields for {profile_id}: {', '.join(sorted(unknown))}")
        for key, selected in changes.items():
            if selected is not None:
                profiles[profile_id][key] = selected
        if not str(profiles[profile_id].get("model", "")).strip() or not str(
            profiles[profile_id].get("effort", "")
        ).strip():
            raise ValueError(f"profile override for {profile_id} requires model and effort")
        # A label is presentation only. Keep it truthful unless the caller
        # deliberately supplied a custom label.
        if "label" not in changes and {"provider", "model", "effort"}.intersection(changes):
            profiles[profile_id]["label"] = f"{profiles[profile_id]['model']} · {profiles[profile_id]['effort']}"
    target = configuration_dir(runtime_dir) / "workflows" / f"{workflow_id}.json"
    previous = target.read_bytes() if target.is_file() else None
    parsed = save_workflow_value(runtime_dir, value)
    # The saved file validates standalone, but workflows that extend it are
    # only re-validated on load.  A provider flip that mixes providers inside a
    # dependent workflow's session slot would otherwise brick every subsequent
    # load, so verify the whole layered set and roll back on failure.
    try:
        load_configured_workflows(runtime_dir)
    except ValueError as error:
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            atomic_write(target, previous)
        raise ValueError(f"not saved; this change breaks a dependent workflow: {error}")
    return parsed


def update_profile(
    runtime_dir: Path,
    workflow_id: str,
    profile_id: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    permission: str | None = None,
    label: str | None = None,
    custom: bool | None = None,
) -> WorkflowDefinition:
    changes = {
        key: selected
        for key, selected in {
            "provider": provider,
            "model": model,
            "effort": effort,
            "permission": permission,
            "label": label,
            "custom": custom,
        }.items()
        if selected is not None
    }
    return update_profiles(runtime_dir, workflow_id, {profile_id: changes})


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
