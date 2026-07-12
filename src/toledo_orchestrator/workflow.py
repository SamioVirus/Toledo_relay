from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SESSION_POLICIES = frozenset({"new", "continue", "new-if-missing"})
PERMISSIONS = frozenset({"read-only", "workspace-write"})


@dataclass(frozen=True)
class ProfileDefinition:
    id: str
    label: str
    provider: str
    model: str
    effort: str
    permission: str
    color: str
    timeout_seconds: int = 900

    @classmethod
    def from_value(cls, profile_id: str, value: dict[str, Any]) -> "ProfileDefinition":
        permission = str(value.get("permission", "read-only"))
        if permission not in PERMISSIONS:
            raise ValueError(f"invalid permission for profile {profile_id}: {permission}")
        provider = str(value["provider"])
        if provider not in {"codex", "claude"}:
            raise ValueError(f"unsupported provider for profile {profile_id}: {provider}")
        return cls(
            id=profile_id,
            label=str(value.get("label", profile_id)),
            provider=provider,
            model=str(value.get("model", "provider-default")),
            effort=str(value.get("effort", "provider-default")),
            permission=permission,
            color=str(value.get("color", "#7c8aa5")),
            timeout_seconds=int(value.get("timeout_seconds", 900)),
        )


@dataclass(frozen=True)
class StageDefinition:
    id: str
    title: str
    phase: str
    role: str
    prompt_kind: str
    prompt_file: str
    profile: str
    session_slot: str
    session_policy: str
    artifact_type: str
    context: tuple[str, ...]
    transitions: dict[str, str]

    @classmethod
    def from_value(cls, stage_id: str, value: dict[str, Any]) -> "StageDefinition":
        policy = str(value.get("session_policy", "new-if-missing"))
        if policy not in SESSION_POLICIES:
            raise ValueError(f"invalid session policy for stage {stage_id}: {policy}")
        transitions = {str(key): str(target) for key, target in value.get("transitions", {}).items()}
        if not transitions:
            raise ValueError(f"stage {stage_id} must define transitions")
        prompt_file = str(value["prompt_file"])
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*\.md", prompt_file):
            raise ValueError(f"invalid prompt file for stage {stage_id}: {prompt_file}")
        return cls(
            id=stage_id,
            title=str(value.get("title", stage_id)),
            phase=str(value["phase"]),
            role=str(value["role"]),
            prompt_kind=str(value.get("prompt_kind", stage_id)),
            prompt_file=prompt_file,
            profile=str(value["profile"]),
            session_slot=str(value["session_slot"]),
            session_policy=policy,
            artifact_type=str(value["artifact_type"]),
            context=tuple(str(item) for item in value.get("context", [])),
            transitions=transitions,
        )


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    label: str
    start_stage: str
    profiles: dict[str, ProfileDefinition]
    stages: dict[str, StageDefinition]
    planning_round_cap: int
    implementation_round_cap: int
    next_task_stage: str
    next_task_revision_stage: str

    @classmethod
    def from_file(cls, path: Path) -> "WorkflowDefinition":
        return cls.from_value(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> "WorkflowDefinition":
        profiles = {
            profile_id: ProfileDefinition.from_value(profile_id, profile)
            for profile_id, profile in value.get("profiles", {}).items()
        }
        stages = {
            stage_id: StageDefinition.from_value(stage_id, stage)
            for stage_id, stage in value.get("stages", {}).items()
        }
        start_stage = str(value["start_stage"])
        if start_stage not in stages:
            raise ValueError(f"unknown start stage: {start_stage}")
        for stage in stages.values():
            if stage.profile not in profiles:
                raise ValueError(f"stage {stage.id} references unknown profile {stage.profile}")
            for target in stage.transitions.values():
                if target.startswith("@"):
                    continue
                if target not in stages:
                    raise ValueError(f"stage {stage.id} references unknown target {target}")
            profile = profiles[stage.profile]
            if profile.permission == "workspace-write" and not (
                stage.phase == "implementation" and stage.role == "implementer"
            ):
                raise ValueError(
                    f"profile {profile.id} grants workspace-write to non-implementation stage {stage.id}"
                )
        return cls(
            id=str(value["id"]),
            label=str(value.get("label", value["id"])),
            start_stage=start_stage,
            profiles=profiles,
            stages=stages,
            planning_round_cap=int(value.get("planning_round_cap", 3)),
            implementation_round_cap=int(value.get("implementation_round_cap", 2)),
            next_task_stage=str(value["next_task_stage"]),
            next_task_revision_stage=str(value["next_task_revision_stage"]),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "start_stage": self.start_stage,
            "planning_round_cap": self.planning_round_cap,
            "implementation_round_cap": self.implementation_round_cap,
            "next_task_stage": self.next_task_stage,
            "next_task_revision_stage": self.next_task_revision_stage,
            "profiles": {
                key: {
                    "label": profile.label,
                    "provider": profile.provider,
                    "model": profile.model,
                    "effort": profile.effort,
                    "permission": profile.permission,
                    "color": profile.color,
                    "timeout_seconds": profile.timeout_seconds,
                }
                for key, profile in self.profiles.items()
            },
            "stages": {
                key: {
                    "title": stage.title,
                    "phase": stage.phase,
                    "role": stage.role,
                    "prompt_kind": stage.prompt_kind,
                    "prompt_file": stage.prompt_file,
                    "profile": stage.profile,
                    "session_slot": stage.session_slot,
                    "session_policy": stage.session_policy,
                    "artifact_type": stage.artifact_type,
                    "context": list(stage.context),
                    "transitions": dict(stage.transitions),
                }
                for key, stage in self.stages.items()
            },
        }

    def public_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "start_stage": self.start_stage,
            "planning_round_cap": self.planning_round_cap,
            "implementation_round_cap": self.implementation_round_cap,
            "profiles": {key: vars(value) for key, value in self.profiles.items()},
            "stages": {
                key: {
                    "id": stage.id,
                    "title": stage.title,
                    "phase": stage.phase,
                    "role": stage.role,
                    "prompt_kind": stage.prompt_kind,
                    "profile": stage.profile,
                    "session_slot": stage.session_slot,
                    "session_policy": stage.session_policy,
                    "artifact_type": stage.artifact_type,
                    "transitions": stage.transitions,
                }
                for key, stage in self.stages.items()
            },
        }


def load_workflows(directory: Path | None = None) -> dict[str, WorkflowDefinition]:
    root = directory or Path(__file__).with_name("workflows")
    workflows: dict[str, WorkflowDefinition] = {}
    for path in sorted(root.glob("*.json")):
        workflow = WorkflowDefinition.from_file(path)
        if workflow.id in workflows:
            raise ValueError(f"duplicate workflow definition: {workflow.id}")
        workflows[workflow.id] = workflow
    return workflows
