from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .director import DIRECTION_CONDITIONS


SESSION_POLICIES = frozenset({"new", "continue", "new-if-missing"})
PERMISSIONS = frozenset({"read-only", "workspace-write"})
WORKFLOW_SCHEMA_VERSION = "toledo_orchestrator.workflow.v2"
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]*")
CONTEXT_TOKENS = frozenset({
    "request",
    "human-decisions",
    "approved-handoff",
    "completion-receipt",
    "implementation-evidence",
})


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
    custom: bool = False

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
            custom=bool(value.get("custom", False)),
        )


@dataclass(frozen=True)
class StageDefinition:
    id: str
    title: str
    phase: str
    role: str
    prompt_kind: str
    prompt_label: str
    prompt_file: str
    profile: str
    session_slot: str
    session_policy: str
    artifact_type: str
    context: tuple[str, ...]
    transitions: dict[str, str]
    round_counter: str | None = None
    round_cap: int | None = None
    round_directive: str = "continue"
    round_pause_reason: str | None = None
    repair_stage: str | None = None
    seal_source: str | None = None
    direction: dict[str, str] = field(default_factory=dict)
    provider_switchable: bool = False

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
        round_value = value.get("round")
        if round_value is not None and not isinstance(round_value, dict):
            raise ValueError(f"round policy for stage {stage_id} must be an object")
        round_counter = str(round_value.get("counter", "")) if round_value else None
        if round_counter == "":
            round_counter = None
        if round_counter and not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", round_counter):
            raise ValueError(f"invalid round counter for stage {stage_id}: {round_counter}")
        round_cap = int(round_value["cap"]) if round_value and "cap" in round_value else None
        if round_cap is not None and round_cap < 1:
            raise ValueError(f"round cap for stage {stage_id} must be positive")
        if bool(round_counter) != bool(round_cap):
            raise ValueError(f"round policy for stage {stage_id} requires both counter and cap")
        round_directive = str(round_value.get("directive", "continue")) if round_value else "continue"
        if round_directive not in {"continue", "ready", "human"}:
            raise ValueError(f"invalid round directive for stage {stage_id}: {round_directive}")
        round_pause_reason = str(round_value.get("pause_reason", "")) if round_value else None
        if round_pause_reason == "":
            round_pause_reason = None
        if round_counter and not round_pause_reason:
            raise ValueError(f"round policy for stage {stage_id} requires pause_reason")
        direction_value = value.get("direction", {})
        if not isinstance(direction_value, dict):
            raise ValueError(f"direction map for stage {stage_id} must be an object")
        direction = {str(key): str(target) for key, target in direction_value.items()}
        for condition, fragment in direction.items():
            if condition not in DIRECTION_CONDITIONS:
                raise ValueError(
                    f"stage {stage_id} has unsupported direction condition {condition}; "
                    f"expected one of {', '.join(DIRECTION_CONDITIONS)}"
                )
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*\.md", fragment):
                raise ValueError(f"invalid direction fragment for stage {stage_id}: {fragment}")
        return cls(
            id=stage_id,
            title=str(value.get("title", stage_id)),
            phase=str(value["phase"]),
            role=str(value["role"]),
            prompt_kind=str(value.get("prompt_kind", stage_id)),
            prompt_label=str(value.get("prompt_label", value.get("prompt_kind", stage_id))),
            prompt_file=prompt_file,
            profile=str(value["profile"]),
            session_slot=str(value["session_slot"]),
            session_policy=policy,
            artifact_type=str(value["artifact_type"]),
            context=tuple(str(item) for item in value.get("context", [])),
            transitions=transitions,
            round_counter=round_counter,
            round_cap=round_cap,
            round_directive=round_directive,
            round_pause_reason=round_pause_reason,
            repair_stage=str(value["repair_stage"]) if value.get("repair_stage") else None,
            seal_source=str(value["seal_source"]) if value.get("seal_source") else None,
            direction=direction,
            provider_switchable=bool(value.get("provider_switchable", False)),
        )


@dataclass(frozen=True)
class WorkflowDefinition:
    schema_version: str
    id: str
    label: str
    start_stage: str
    profiles: dict[str, ProfileDefinition]
    stages: dict[str, StageDefinition]
    planning_round_cap: int
    implementation_round_cap: int
    next_task_stage: str
    next_task_revision_stage: str
    session_slots: tuple[str, ...]
    prompt_namespaces: tuple[str, ...]

    @classmethod
    def from_file(cls, path: Path) -> "WorkflowDefinition":
        return cls.from_value(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> "WorkflowDefinition":
        workflow_id = str(value["id"])
        if not IDENTIFIER.fullmatch(workflow_id):
            raise ValueError(f"invalid workflow id: {workflow_id}")
        source_schema = value.get("schema_version")
        if source_schema not in {None, WORKFLOW_SCHEMA_VERSION}:
            raise ValueError(f"unsupported workflow schema: {source_schema}")
        profiles = {
            profile_id: ProfileDefinition.from_value(profile_id, profile)
            for profile_id, profile in value.get("profiles", {}).items()
        }
        stages = {
            stage_id: StageDefinition.from_value(stage_id, stage)
            for stage_id, stage in value.get("stages", {}).items()
        }
        # Compatibility migration for v2 run snapshots/config overrides created before
        # loop and repair semantics moved into stage metadata. Runtime execution remains
        # stage-driven; these names are recognized only while parsing the old schema.
        legacy_schema = (
            source_schema is None
            and workflow_id == "continuous-development"
            and ("planning_round_cap" in value or "implementation_round_cap" in value)
        )
        legacy_rounds = {
            "planning-review": (
                "planning",
                int(value.get("planning_round_cap", 3)),
                "planning_round_cap_reached",
            ),
            "implementation-review": (
                "implementation",
                int(value.get("implementation_round_cap", 2)),
                "implementation_round_cap_reached",
            ),
        }
        if legacy_schema:
            for stage_id, (counter, cap, reason) in legacy_rounds.items():
                stage = stages.get(stage_id)
                if stage and not stage.round_counter:
                    stages[stage_id] = replace(
                        stage,
                        round_counter=counter,
                        round_cap=cap,
                        round_directive="continue",
                        round_pause_reason=reason,
                    )
        legacy_repair = "implementation-repair" if legacy_schema and "implementation-repair" in stages else None
        if legacy_repair:
            for stage_id in ("implementation", "implementation-review", "implementation-repair"):
                stage = stages.get(stage_id)
                if stage and not stage.repair_stage:
                    stages[stage_id] = replace(stage, repair_stage=legacy_repair)
        planning_review = stages.get("planning-review")
        if legacy_schema and planning_review and not planning_review.seal_source:
            stages["planning-review"] = replace(planning_review, seal_source="plan")
        start_stage = str(value["start_stage"])
        if start_stage not in stages:
            raise ValueError(f"unknown start stage: {start_stage}")
        configured_slots = tuple(str(item) for item in value.get("session_slots", []))
        if configured_slots:
            if len(set(configured_slots)) != len(configured_slots) or any(not item for item in configured_slots):
                raise ValueError("workflow session_slots must contain unique non-empty names")
            session_slots = configured_slots
        else:
            session_slots = tuple(dict.fromkeys(stage.session_slot for stage in stages.values()))
        if any(not IDENTIFIER.fullmatch(item) for item in session_slots):
            raise ValueError("workflow session slots must use lowercase identifiers")
        prompt_namespaces = tuple(str(item) for item in value.get("prompt_namespaces", []))
        if not prompt_namespaces:
            prompt_namespaces = (workflow_id,)
        if len(set(prompt_namespaces)) != len(prompt_namespaces) or any(not item for item in prompt_namespaces):
            raise ValueError("workflow prompt_namespaces must contain unique non-empty names")
        if any(not IDENTIFIER.fullmatch(item) for item in prompt_namespaces):
            raise ValueError("workflow prompt_namespaces must use lowercase identifiers")
        slot_providers: dict[str, str] = {}
        for stage in stages.values():
            if stage.session_slot not in session_slots:
                raise ValueError(f"stage {stage.id} references unregistered session slot {stage.session_slot}")
            if stage.profile not in profiles:
                raise ValueError(f"stage {stage.id} references unknown profile {stage.profile}")
            for target in stage.transitions.values():
                if target.startswith("@pause:"):
                    if not target.split(":", 1)[1]:
                        raise ValueError(f"stage {stage.id} has an empty pause reason")
                    continue
                if target.startswith("@seal:"):
                    parts = target.split(":")
                    if len(parts) != 3 or not IDENTIFIER.fullmatch(parts[1]) or parts[2] not in stages:
                        raise ValueError(f"stage {stage.id} has invalid seal transition {target}")
                    continue
                if target.startswith("@complete:"):
                    parts = target.split(":", 1)
                    if len(parts) != 2 or parts[1] not in stages:
                        raise ValueError(f"stage {stage.id} has invalid completion transition {target}")
                    continue
                if target.startswith("@"):
                    raise ValueError(f"stage {stage.id} has unsupported controller transition {target}")
                if target not in stages:
                    raise ValueError(f"stage {stage.id} references unknown target {target}")
            if any(target.startswith("@seal:") for target in stage.transitions.values()) and not stage.seal_source:
                raise ValueError(f"stage {stage.id} must declare seal_source for a seal transition")
            if any(target.startswith("@complete:") for target in stage.transitions.values()) and not stage.repair_stage:
                raise ValueError(f"stage {stage.id} must declare repair_stage for a completion transition")
            if stage.phase == "implementation" and stage.role == "implementer" and not stage.repair_stage:
                raise ValueError(f"implementation stage {stage.id} must declare repair_stage")
            if stage.repair_stage and stage.repair_stage not in stages:
                raise ValueError(f"stage {stage.id} references unknown repair stage {stage.repair_stage}")
            if stage.round_counter and stage.round_directive not in stage.transitions:
                raise ValueError(
                    f"stage {stage.id} round directive {stage.round_directive} has no transition"
                )
            if stage.round_counter and stage.transitions[stage.round_directive].startswith("@"):
                raise ValueError(
                    f"stage {stage.id} round directive must target a concrete stage"
                )
            profile = profiles[stage.profile]
            previous_provider = slot_providers.setdefault(stage.session_slot, profile.provider)
            if previous_provider != profile.provider:
                raise ValueError(
                    f"session slot {stage.session_slot} changes provider between stages"
                )
            for token in stage.context:
                if token in CONTEXT_TOKENS:
                    continue
                if token.startswith("latest:") and IDENTIFIER.fullmatch(token.split(":", 1)[1]):
                    continue
                raise ValueError(f"stage {stage.id} has unsupported context token {token}")
            if profile.permission == "workspace-write" and not (
                stage.phase == "implementation" and stage.role == "implementer"
            ):
                raise ValueError(
                    f"profile {profile.id} grants workspace-write to non-implementation stage {stage.id}"
                )
        next_task_stage = str(value["next_task_stage"])
        next_task_revision_stage = str(value["next_task_revision_stage"])
        if next_task_stage not in stages:
            raise ValueError(f"unknown next-task stage: {next_task_stage}")
        if next_task_revision_stage not in stages:
            raise ValueError(f"unknown next-task revision stage: {next_task_revision_stage}")
        return cls(
            schema_version=WORKFLOW_SCHEMA_VERSION,
            id=workflow_id,
            label=str(value.get("label", value["id"])),
            start_stage=start_stage,
            profiles=profiles,
            stages=stages,
            planning_round_cap=int(value.get("planning_round_cap", 3)),
            implementation_round_cap=int(value.get("implementation_round_cap", 2)),
            next_task_stage=next_task_stage,
            next_task_revision_stage=next_task_revision_stage,
            session_slots=session_slots,
            prompt_namespaces=prompt_namespaces,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "label": self.label,
            "start_stage": self.start_stage,
            "planning_round_cap": self.planning_round_cap,
            "implementation_round_cap": self.implementation_round_cap,
            "next_task_stage": self.next_task_stage,
            "next_task_revision_stage": self.next_task_revision_stage,
            "session_slots": list(self.session_slots),
            "prompt_namespaces": list(self.prompt_namespaces),
            "profiles": {
                key: {
                    "label": profile.label,
                    "provider": profile.provider,
                    "model": profile.model,
                    "effort": profile.effort,
                    "permission": profile.permission,
                    "color": profile.color,
                    "timeout_seconds": profile.timeout_seconds,
                    "custom": profile.custom,
                }
                for key, profile in self.profiles.items()
            },
            "stages": {
                key: {
                    "title": stage.title,
                    "phase": stage.phase,
                    "role": stage.role,
                    "prompt_kind": stage.prompt_kind,
                    "prompt_label": stage.prompt_label,
                    "prompt_file": stage.prompt_file,
                    "profile": stage.profile,
                    "session_slot": stage.session_slot,
                    "session_policy": stage.session_policy,
                    "artifact_type": stage.artifact_type,
                    "context": list(stage.context),
                    "transitions": dict(stage.transitions),
                    "round": (
                        {
                            "counter": stage.round_counter,
                            "cap": stage.round_cap,
                            "directive": stage.round_directive,
                            "pause_reason": stage.round_pause_reason,
                        }
                        if stage.round_counter
                        else None
                    ),
                    "repair_stage": stage.repair_stage,
                    "seal_source": stage.seal_source,
                    "direction": dict(stage.direction),
                    "provider_switchable": stage.provider_switchable,
                }
                for key, stage in self.stages.items()
            },
        }

    def public_summary(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "label": self.label,
            "start_stage": self.start_stage,
            "planning_round_cap": self.planning_round_cap,
            "implementation_round_cap": self.implementation_round_cap,
            "session_slots": list(self.session_slots),
            "prompt_namespaces": list(self.prompt_namespaces),
            "profiles": {key: vars(value) for key, value in self.profiles.items()},
            "stages": {
                key: {
                    "id": stage.id,
                    "title": stage.title,
                    "phase": stage.phase,
                    "role": stage.role,
                    "prompt_kind": stage.prompt_kind,
                    "prompt_label": stage.prompt_label,
                    "prompt_file": stage.prompt_file,
                    "profile": stage.profile,
                    "session_slot": stage.session_slot,
                    "session_policy": stage.session_policy,
                    "artifact_type": stage.artifact_type,
                    "context": list(stage.context),
                    "transitions": stage.transitions,
                    "round": (
                        {
                            "counter": stage.round_counter,
                            "cap": stage.round_cap,
                            "directive": stage.round_directive,
                            "pause_reason": stage.round_pause_reason,
                        }
                        if stage.round_counter
                        else None
                    ),
                    "repair_stage": stage.repair_stage,
                    "seal_source": stage.seal_source,
                    "direction": dict(stage.direction),
                    "provider_switchable": stage.provider_switchable,
                }
                for key, stage in self.stages.items()
            },
        }


def merge_workflow_value(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge a small workflow variant without duplicating its whole graph."""

    merged = deepcopy(base)

    def apply(target: dict[str, Any], changes: dict[str, Any]) -> None:
        for key, value in changes.items():
            if key == "extends":
                continue
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                apply(target[key], value)
            else:
                target[key] = deepcopy(value)

    apply(merged, override)
    return merged


def load_workflow_layers(*layers: list[Path]) -> dict[str, WorkflowDefinition]:
    """Resolve inheritance after later configuration layers replace base definitions."""

    sources: dict[str, tuple[Path, dict[str, Any]]] = {}
    for layer in layers:
        seen: set[str] = set()
        for path in layer:
            value = json.loads(path.read_text(encoding="utf-8"))
            workflow_id = str(value.get("id", ""))
            if not workflow_id:
                raise ValueError(f"workflow file has no id: {path}")
            if workflow_id in seen:
                raise ValueError(f"duplicate workflow definition in one layer: {workflow_id}")
            seen.add(workflow_id)
            sources[workflow_id] = (path, value)

    resolved: dict[str, WorkflowDefinition] = {}

    def resolve(workflow_id: str, stack: tuple[str, ...] = ()) -> WorkflowDefinition:
        if workflow_id in resolved:
            return resolved[workflow_id]
        if workflow_id in stack:
            chain = " -> ".join((*stack, workflow_id))
            raise ValueError(f"workflow inheritance cycle: {chain}")
        if workflow_id not in sources:
            raise ValueError(f"unknown inherited workflow: {workflow_id}")
        _, source_value = sources[workflow_id]
        base_id = str(source_value.get("extends", ""))
        if base_id and not IDENTIFIER.fullmatch(base_id):
            raise ValueError(f"invalid inherited workflow id: {base_id}")
        value = source_value
        if base_id:
            base = resolve(base_id, (*stack, workflow_id))
            value = merge_workflow_value(base.snapshot(), source_value)
            value["prompt_namespaces"] = list(dict.fromkeys((workflow_id, *base.prompt_namespaces)))
        workflow = WorkflowDefinition.from_value(value)
        resolved[workflow_id] = workflow
        return workflow

    for workflow_id in sources:
        resolve(workflow_id)
    return resolved


def load_workflows(directory: Path | None = None) -> dict[str, WorkflowDefinition]:
    root = directory or Path(__file__).with_name("workflows")
    return load_workflow_layers(sorted(root.glob("*.json")))
