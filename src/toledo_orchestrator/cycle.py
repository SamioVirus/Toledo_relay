from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from .catalog import load_catalog, validate_selection
from .core import (
    ClaudeAdapter,
    CodexAdapter,
    Directive,
    Orchestrator,
    ProviderAdapter,
    ProviderResult,
    RUN_ID,
    atomic_write,
    decode_text_artifact,
    extract_directive,
    extract_self_caption,
    has_substantive_work,
    parse_claude_result,
    parse_codex_result,
    read_json,
    result_text,
    sha256,
    work_product_text,
    write_json,
    write_text,
)
from .configuration import load_configured_projects, load_configured_workflows
from .director import DirectionContext, select_conditions
from .locking import run_lock
from .project import ProjectDefinition, load_projects
from .stance import CURATED_STANCES
from .validation import (
    failure_signature,
    pending_required_validations,
    required_local_validations_passed,
    run_project_validations,
)
from .workflow import (
    IDENTIFIER,
    ProfileDefinition,
    StageDefinition,
    WorkflowDefinition,
    apply_round_overrides,
    load_workflows,
    validate_prompt_overrides,
)
from .worktree import (
    assert_allowed_changes,
    current_branch,
    collect_worktree_evidence,
    commit_accepted_changes,
    create_execution_worktree,
    current_revision,
    parent_revision,
    revision_patch,
    seal_worktree_evidence,
    remove_execution_worktree,
)


T = TypeVar("T")


def _locked(method: Callable[..., T]) -> Callable[..., T]:
    @wraps(method)
    def wrapper(self: "CycleOrchestrator", run_id: str, *args: Any, **kwargs: Any) -> T:
        with self._lock(run_id):
            return method(self, run_id, *args, **kwargs)

    return wrapper


def _session_label(workflow: WorkflowDefinition, cycle_number: int, slot: str) -> str:
    if slot not in workflow.session_slots:
        raise ValueError(f"workflow does not register session slot: {slot}")
    offset = workflow.session_slots.index(slot)
    number = (cycle_number - 1) * len(workflow.session_slots) + offset + 1
    label = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        label = chr(65 + remainder) + label
    return label


class CycleOrchestrator:
    """File-authoritative A/B/C development cycles with provider session continuity."""

    def __init__(
        self,
        runtime_dir: Path | None = None,
        adapters: dict[str, ProviderAdapter] | None = None,
        projects: dict[str, ProjectDefinition] | None = None,
        workflows: dict[str, WorkflowDefinition] | None = None,
    ) -> None:
        self.runtime_dir = (runtime_dir or Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local")) / "ToledoOrchestrator").resolve()
        self.adapters = adapters or {"codex": CodexAdapter(), "claude": ClaudeAdapter()}
        self.projects = projects or load_configured_projects(self.runtime_dir)
        self.workflows = workflows or load_configured_workflows(self.runtime_dir)

    @property
    def runs_dir(self) -> Path:
        return self.runtime_dir / "runs"

    def _run_dir(self, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("invalid run id")
        return self.runs_dir / run_id

    def state(self, run_id: str) -> dict[str, Any]:
        return read_json(self._run_dir(run_id) / "run.json")

    def _save(self, run_id: str, state: dict[str, Any]) -> None:
        write_json(self._run_dir(run_id) / "run.json", state)

    @contextmanager
    def _lock(self, run_id: str) -> Iterator[None]:
        with run_lock(self._run_dir(run_id)):
            yield

    def _worktree_identity_error(self, state: dict[str, Any]) -> str | None:
        root = Path(state["execution_worktree"])
        try:
            observed_revision = current_revision(root)
            observed_branch = current_branch(root)
        except ValueError as error:
            return str(error)
        if observed_revision != state["working_revision"]:
            return f"execution_worktree_revision_changed:{state['working_revision']}:{observed_revision}"
        if observed_branch != state["execution_branch"]:
            return f"execution_worktree_branch_changed:{state['execution_branch']}:{observed_branch}"
        return None

    def _workflow(self, state: dict[str, Any]) -> WorkflowDefinition:
        snapshot = state.get("workflow_snapshot")
        if isinstance(snapshot, dict) and snapshot.get("next_task_stage"):
            return WorkflowDefinition.from_value(snapshot)
        workflow = self.workflows.get(str(state["workflow"]))
        if workflow is None:
            raise ValueError(f"workflow definition is unavailable: {state['workflow']}")
        return workflow

    def _project(self, state: dict[str, Any]) -> ProjectDefinition:
        snapshot = state.get("project_snapshot")
        if isinstance(snapshot, dict):
            return ProjectDefinition.from_value(snapshot)
        project = self.projects.get(str(state["project"]))
        if project is None:
            raise ValueError(f"project definition is unavailable: {state['project']}")
        return project

    def _profile(self, state: dict[str, Any], profile_id: str, override: dict[str, Any] | None = None) -> ProfileDefinition:
        if override and isinstance(override.get("profile_value"), dict):
            return ProfileDefinition.from_value(profile_id, override["profile_value"])
        snapshot = state.get("workflow_snapshot", {}).get("profiles", {}).get(profile_id)
        if isinstance(snapshot, dict):
            return ProfileDefinition.from_value(profile_id, snapshot)
        workflow = self._workflow(state)
        if profile_id not in workflow.profiles:
            raise ValueError(f"unknown profile: {profile_id}")
        return workflow.profiles[profile_id]

    @staticmethod
    def _cycle(state: dict[str, Any]) -> dict[str, Any]:
        return state["cycles"][state["cycle"] - 1]

    @staticmethod
    def _round_record(cycle: dict[str, Any], counter: str) -> dict[str, int]:
        rounds = cycle.setdefault("rounds", {})
        record = rounds.get(counter)
        if not isinstance(record, dict):
            record = {
                "count": int(cycle.get(f"{counter}_round", 0)),
                "extension": int(cycle.get(f"{counter}_round_extension", 0)),
            }
            rounds[counter] = record
        return record

    @staticmethod
    def _sync_round_alias(cycle: dict[str, Any], counter: str, record: dict[str, int]) -> None:
        # Preserve the early v2 fields for already-created runs and external readers.
        cycle[f"{counter}_round"] = int(record["count"])
        cycle[f"{counter}_round_extension"] = int(record["extension"])

    def _prompt_source(self, workflow: WorkflowDefinition, name: str) -> Path:
        configured_root = (self.runtime_dir / "config" / "prompts").resolve()
        configured_candidates: list[Path] = []
        for namespace in workflow.prompt_namespaces:
            candidate = (configured_root / namespace / name).resolve()
            if configured_root not in candidate.parents:
                raise ValueError(f"prompt namespace escapes the configured prompt root: {namespace}")
            configured_candidates.append(candidate)
        shared_candidate = (configured_root / name).resolve()
        if configured_root not in shared_candidate.parents:
            raise ValueError(f"prompt file escapes the configured prompt root: {name}")
        candidates = tuple(configured_candidates) + (
            shared_candidate,
            Path(__file__).with_name("prompts") / name,
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise ValueError(
            f"prompt file {name} for workflow {workflow.id} was not found in namespaces "
            f"{','.join(workflow.prompt_namespaces)}; place custom prompts under "
            f"{self.runtime_dir / 'config' / 'prompts' / workflow.id}"
        )

    @staticmethod
    def _provider_session_ids(state: dict[str, Any], provider: str) -> set[str]:
        values: set[str] = set()
        for cycle in state.get("cycles", []):
            for slot in cycle.get("sessions", {}).values():
                if slot.get("provider") != provider:
                    continue
                for item in slot.get("history", []):
                    if item.get("session_id"):
                        values.add(str(item["session_id"]))
        for turn in state.get("turns", []):
            if turn.get("provider") == provider and turn.get("session_id"):
                values.add(str(turn["session_id"]))
        return values

    def create_run(
        self,
        request: bytes,
        project: str,
        workflow: str = "continuous-development",
        *,
        run_mode: str = "auto",
        profile_overrides: dict[str, dict[str, Any]] | None = None,
        round_overrides: dict[str, Any] | None = None,
        prompt_overrides: dict[str, Any] | None = None,
    ) -> str:
        decode_text_artifact(request, "request")
        if run_mode not in {"auto", "step"}:
            raise ValueError("run mode must be auto or step")
        if project not in self.projects:
            raise ValueError(f"unknown project: {project}")
        if workflow not in self.workflows:
            raise ValueError(f"unknown continuous workflow: {workflow}")
        project_definition = self.projects[project]
        project_check = project_definition.check()
        if project_check.get("dirty"):
            raise ValueError(
                "project source checkout must be clean before a continuous run starts: "
                f"{project_definition.root} has uncommitted changes to tracked files — "
                "commit or stash them first (untracked files are ignored)"
            )
        if project_check.get("dirty") is None:
            raise ValueError("project source checkout status must be available before a continuous run starts")
        if not project_check["ready"]:
            raise ValueError(f"project is not ready: {project_check['error'] or project_check['instruction_files']}")
        if not project_definition.implementation_enabled:
            raise ValueError(f"project {project} is not enabled for isolated implementation")
        source_revision = str(project_check["source_revision"])
        workflow_definition = self.workflows[workflow]
        if profile_overrides or round_overrides:
            # Route overrides bind to this run's snapshot only; saved workflow
            # defaults change exclusively through the explicit workflow APIs.
            value = workflow_definition.snapshot()
            if profile_overrides:
                catalog = load_catalog(self.runtime_dir, refresh=False)
                for profile_id, changes in profile_overrides.items():
                    if profile_id not in value["profiles"]:
                        raise ValueError(f"unknown profile override: {profile_id}")
                    if not isinstance(changes, dict):
                        raise ValueError(f"profile override for {profile_id} must be an object")
                    target = value["profiles"][profile_id]
                    model = str(changes.get("model") or target["model"]).strip()
                    effort = str(changes.get("effort") or target["effort"]).strip()
                    custom = bool(changes.get("custom"))
                    if not model or not effort:
                        raise ValueError(f"profile override for {profile_id} requires model and effort")
                    if catalog.get("models"):
                        validate_selection(catalog, provider=str(target["provider"]), model=model, effort=effort, custom=custom)
                    target.update({"model": model, "effort": effort, "custom": custom, "label": f"{model} · {effort}"})
            if round_overrides:
                apply_round_overrides(value, round_overrides)
            workflow_definition = WorkflowDefinition.from_value(value)
        prompt_names = {
            "orchestrator-law.md",
            "strict-contract.md",
            *(stage.prompt_file for stage in workflow_definition.stages.values()),
            *(
                fragment
                for stage in workflow_definition.stages.values()
                for fragment in stage.direction.values()
            ),
        }
        stage_prompt_files = {stage.prompt_file for stage in workflow_definition.stages.values()}
        override_bytes = validate_prompt_overrides(prompt_overrides, stage_prompt_files)
        # Every override is validated above; only now does the run leave a trace
        # on disk, so a rejected launch never creates a half-built run.
        run_id = f"run_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"
        run_dir = self._run_dir(run_id)
        request_path = run_dir / "cycles" / "cycle.0001" / "request.md"
        request_hash = atomic_write(request_path, request)
        execution_worktree = self.runtime_dir / "worktrees" / run_id
        execution_branch = f"codex/orchestrator/{run_id}"
        prompt_library: dict[str, dict[str, str]] = {}
        for prompt_name in sorted(prompt_names):
            if prompt_name in override_bytes:
                # A per-run instruction edit is sealed into the run's own
                # prompt library; the configured prompt files are untouched.
                data = override_bytes[prompt_name]
                source_label = "operator-override"
            else:
                source = self._prompt_source(workflow_definition, prompt_name)
                data = source.read_bytes()
                source_label = str(source.resolve())
            relative = Path("prompt-library") / prompt_name
            digest = atomic_write(run_dir / relative, data)
            prompt_library[prompt_name] = {
                "path": str(relative).replace("\\", "/"),
                "sha256": digest,
                "source": source_label,
            }
        state: dict[str, Any] = {
            "schema_version": "toledo_orchestrator.run.v2",
            "run_id": run_id,
            "project": project,
            "workflow": workflow,
            "workflow_snapshot": workflow_definition.snapshot(),
            "project_snapshot": project_definition.snapshot(),
            "prompt_library": prompt_library,
            "project_root": str(project_definition.root),
            "source_revision": source_revision,
            "working_revision": source_revision,
            "execution_worktree": str(execution_worktree.resolve()),
            "execution_branch": execution_branch,
            "status": "created",
            "run_mode": run_mode,
            "current_turn": 0,
            "current_stage": workflow_definition.start_stage,
            "cycle": 1,
            "cycles": [self._new_cycle(1, request_path.relative_to(run_dir), request_hash)],
            "turns": [],
            "events": [],
            "event_sequence": 0,
            "decisions": [],
            "artifacts": {
                str(request_path.relative_to(run_dir)).replace("\\", "/"): {
                    "sha256": request_hash, "type": "request", "cycle": 1
                }
            },
            "validations": {},
            "current_implementation_evidence": None,
            "pending_validation": None,
            "validation_inflight": None,
            "pending_completion": None,
            "pending_commit": None,
            "completion_receipt": None,
            "pending_human_decision": None,
            "pending_round_extension": None,
            "pending_repair_stage": None,
            "pending_baseline_acceptance": None,
            "baseline_validations": None,
            "next_turn_override": None,
            "inflight": None,
            "abandoned_invocations": [],
            "abandoned_validations": [],
            "errors": [],
            "degraded": False,
        }
        self._save(run_id, state)
        try:
            create_execution_worktree(project_definition, execution_worktree, source_revision, execution_branch)
        except Exception:
            state["status"] = "failed"
            state["errors"].append("execution_worktree_creation_failed")
            self._save(run_id, state)
            raise
        self._event(state, "run.created", title="Run created", details={
            "project": project,
            "workflow": workflow,
            "source_revision": source_revision,
            "execution_worktree": str(execution_worktree),
            "execution_branch": execution_branch,
            "profile_overrides": profile_overrides or None,
            "round_overrides": round_overrides or None,
            "prompt_overrides": sorted(override_bytes) or None,
        })
        self._save(run_id, state)
        return run_id

    @staticmethod
    def _new_cycle(number: int, request_file: Path, request_hash: str) -> dict[str, Any]:
        return {
            "id": f"cycle.{number:04d}",
            "number": number,
            "status": "active",
            "request_file": str(request_file).replace("\\", "/"),
            "request_sha256": request_hash,
            "planning_round": 0,
            "planning_round_extension": 0,
            "implementation_round": 0,
            "implementation_round_extension": 0,
            "rounds": {},
            "start_turn": None,
            "end_turn": None,
            "sessions": {},
            "approved_handoff": None,
            "completion_receipt": None,
            "next_task_proposal": None,
        }

    def _event(self, state: dict[str, Any], kind: str, *, title: str, details: dict[str, Any]) -> dict[str, Any]:
        state["event_sequence"] += 1
        sequence = state["event_sequence"]
        value = {
            "sequence": sequence,
            "id": f"event.{sequence:06d}",
            "kind": kind,
            "title": title,
            "cycle": state["cycle"],
            "stage": state.get("current_stage"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **details,
        }
        path = self._run_dir(state["run_id"]) / "events" / f"event.{sequence:06d}.json"
        digest = write_json(path, value)
        state["events"].append({"id": value["id"], "kind": kind, "title": title, "sha256": digest})
        return value

    def _pause_for_step(self, state: dict[str, Any]) -> None:
        if state.get("run_mode", "auto") != "step" or state.get("status") != "running":
            return
        state["status"] = "paused"
        state["pending_human_decision"] = "operator_step"
        self._event(
            state,
            "operator.step.ready",
            title="Ready for the next turn",
            details={"next_stage": state.get("current_stage")},
        )

    def check(self) -> dict[str, Any]:
        providers = {name: adapter.check() for name, adapter in self.adapters.items()}
        projects = {name: project.check() for name, project in self.projects.items()}
        workflows: dict[str, dict[str, Any]] = {}
        for name, workflow in self.workflows.items():
            prompt_names = {
                "orchestrator-law.md",
                "strict-contract.md",
                *(stage.prompt_file for stage in workflow.stages.values()),
                *(
                    fragment
                    for stage in workflow.stages.values()
                    for fragment in stage.direction.values()
                ),
            }
            prompt_files: dict[str, dict[str, Any]] = {}
            for prompt_name in sorted(prompt_names):
                try:
                    source = self._prompt_source(workflow, prompt_name)
                    prompt_files[prompt_name] = {"ready": True, "source": str(source.resolve())}
                except ValueError as error:
                    prompt_files[prompt_name] = {"ready": False, "error": str(error)}
            summary = workflow.public_summary()
            summary["prompt_files"] = prompt_files
            summary["ready"] = all(value["ready"] for value in prompt_files.values())
            workflows[name] = summary
        ready = all(value["ready"] for value in providers.values()) and all(
            value.get("implementation_ready", value["ready"]) for value in projects.values()
        ) and all(value["ready"] for value in workflows.values())
        return {
            "status": "ready_for_generation" if ready else "blocked",
            "ready": ready,
            "generation_verified": False,
            "providers": providers,
            "projects": projects,
            "workflows": workflows,
            "runtime_dir": str(self.runtime_dir),
        }

    def _prompt_file(self, state: dict[str, Any], name: str) -> str:
        record = state.get("prompt_library", {}).get(name)
        if not isinstance(record, dict) or not record.get("path"):
            raise ValueError(f"run prompt snapshot is missing: {name}")
        return self._artifact_text(state, str(record["path"]))

    def _latest_turn(self, state: dict[str, Any], artifact_type: str) -> dict[str, Any] | None:
        for turn in reversed(state["turns"]):
            if (
                turn.get("cycle") == state["cycle"]
                and turn.get("artifact_type") == artifact_type
                and turn.get("substantive")
                and not turn.get("correction")
            ):
                return turn
        return None

    @staticmethod
    def _nested_path_hash(value: Any, relative: str) -> str | None:
        if isinstance(value, dict):
            stored_path = str(value.get("path", "")).replace("\\", "/")
            if stored_path == relative and isinstance(value.get("sha256"), str):
                return str(value["sha256"])
            for child in value.values():
                found = CycleOrchestrator._nested_path_hash(child, relative)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = CycleOrchestrator._nested_path_hash(child, relative)
                if found:
                    return found
        return None

    def _artifact_hash(self, state: dict[str, Any], relative: str) -> str | None:
        normalized = str(relative).replace("\\", "/")
        for stored_path, direct in state.get("artifacts", {}).items():
            if str(stored_path).replace("\\", "/") == normalized:
                if isinstance(direct, dict) and isinstance(direct.get("sha256"), str):
                    return str(direct["sha256"])
        for record in state.get("prompt_library", {}).values():
            if isinstance(record, dict) and record.get("path") == normalized:
                return str(record.get("sha256"))
        for decision in state.get("decisions", []):
            if decision.get("file") == normalized:
                return str(decision.get("sha256"))
        for turn in state.get("turns", []):
            for file_key, hash_key in (
                ("prompt_file", "prompt_sha256"),
                ("response_file", "response_sha256"),
                ("output_file", "output_sha256"),
                ("raw_file", "raw_sha256"),
                ("stderr_file", "stderr_sha256"),
                ("direction_file", "direction_sha256"),
                ("steer_note_file", "steer_note_sha256"),
                ("stance_override_file", "stance_override_sha256"),
            ):
                if turn.get(file_key) and f"turns/{turn.get(file_key)}" == normalized:
                    digest = turn.get(hash_key)
                    return str(digest) if isinstance(digest, str) and digest else None
            if f"turns/{turn.get('id')}.json" == normalized:
                digest = turn.get("metadata_sha256")
                return str(digest) if isinstance(digest, str) and digest else None
        for event in state.get("events", []):
            if event.get("id") and f"events/{event['id']}.json" == normalized:
                return str(event.get("sha256"))
        for container in (
            state.get("current_implementation_evidence"),
            state.get("validations"),
            state.get("baseline_validations"),
            state.get("pending_baseline_acceptance"),
        ):
            found = self._nested_path_hash(container, normalized)
            if found:
                return found
        return None

    def _artifact_bytes(self, state: dict[str, Any], relative: str) -> bytes:
        relative = str(relative).replace("\\", "/")
        path = (self._run_dir(state["run_id"]) / relative).resolve()
        run_dir = self._run_dir(state["run_id"]).resolve()
        if path != run_dir and run_dir not in path.parents:
            raise ValueError("artifact path escapes run directory")
        data = path.read_bytes()
        expected = self._artifact_hash(state, relative)
        if not expected:
            raise ValueError(f"artifact is not registered in the run manifest: {relative}")
        observed = sha256(data)
        if observed != expected:
            raise ValueError(f"artifact hash mismatch: {relative}:{expected}:{observed}")
        return data

    def _artifact_text(self, state: dict[str, Any], relative: str) -> str:
        data = self._artifact_bytes(state, relative)
        if len(data) > 2_000_000:
            raise ValueError(f"artifact exceeds the prompt transport limit: {relative}:{len(data)}")
        return decode_text_artifact(data, relative)

    def _context_section(
        self,
        state: dict[str, Any],
        token: str,
        *,
        exclude_decision_file: str | None = None,
    ) -> tuple[str, str] | None:
        cycle = self._cycle(state)
        run_dir = self._run_dir(state["run_id"])
        if token == "request":
            return "Request", self._artifact_text(state, cycle["request_file"])
        if token == "human-decisions":
            values = []
            for decision in state["decisions"]:
                if decision.get("cycle") != state["cycle"]:
                    continue
                if decision.get("choice") == "direction":
                    # Step directions are intentionally one-shot and are injected by
                    # _prompt only on the immediately following provider turn.
                    continue
                if exclude_decision_file and decision.get("file") == exclude_decision_file:
                    continue
                values.append(self._artifact_text(state, decision["file"]))
            return "Human decisions", "\n\n".join(values) if values else "None."
        if token.startswith("latest:"):
            artifact_type = token.split(":", 1)[1]
            turn = self._latest_turn(state, artifact_type)
            if not turn:
                return f"Latest {artifact_type}", "None."
            return f"Latest {artifact_type}", self._artifact_text(state, f"turns/{turn['output_file']}")
        if token == "approved-handoff":
            relative = cycle.get("approved_handoff")
            return "Sealed approved handoff", self._artifact_text(state, relative) if relative else "None."
        if token == "completion-receipt":
            relative = cycle.get("completion_receipt")
            return "Completion receipt", self._artifact_text(state, relative) if relative else "None."
        if token == "implementation-evidence":
            evidence = state.get("current_implementation_evidence")
            if not evidence:
                return "Implementation evidence", "None."
            summary = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True)
            patch = evidence.get("patch", {}).get("path")
            if patch:
                summary += "\n\n# Exact patch\n" + self._artifact_text(state, patch)
            return "Implementation evidence", summary
        return None

    def _compose_direction_text(self, state: dict[str, Any], stage: StageDefinition) -> str:
        """Assemble the situational direction from workflow-configured fragments.

        The controller detects generic conditions (has this exact stage already
        run this cycle; is this a later cycle) without referencing any stage ID or
        counter name, then reads the prose from the fragment the *stage* maps to
        that condition. Stages with no ``direction`` mapping emit nothing.
        """
        if not stage.direction:
            return ""
        stage_visits = sum(
            1
            for turn in state["turns"]
            if turn.get("cycle") == state["cycle"]
            and turn.get("stage") == stage.id
            and turn.get("substantive")
            and not turn.get("correction")
        )
        context = DirectionContext(stage_visits=stage_visits, cycle=int(state["cycle"]))
        fragments: list[str] = []
        for condition in select_conditions(context):
            fragment_name = stage.direction.get(condition)
            if fragment_name:
                fragments.append(self._prompt_file(state, fragment_name).strip())
        return ("\n\n".join(fragments) + "\n") if fragments else ""

    def _prompt(
        self,
        state: dict[str, Any],
        stage: StageDefinition,
        profile: ProfileDefinition,
        session_action: str,
        session_label: str,
        direction_text: str | None = None,
        stance_text: str | None = None,
    ) -> bytes:
        project = self._project(state)
        sections = [
            f"You are logical session {session_label}, acting as {stage.role} in the {stage.phase} phase.",
        ]
        if session_action == "new":
            sections.append(self._prompt_file(state, "orchestrator-law.md").strip())
            sections.append(
                "# Project boundary\n"
                f"Execution worktree: {state['execution_worktree']}\n"
                f"Working revision: {state['working_revision']}\n"
                f"Permission for this turn: {profile.permission}\n"
                "Follow these instruction files before acting:\n"
                + "\n".join(f"- {item}" for item in project.instruction_files)
            )
            abandoned = state.get("abandoned_invocations") or []
            if abandoned:
                sections.append(
                    "# Recovery context\n"
                    "A prior provider invocation ended without a trustworthy completion record. "
                    "Treat its work as untrusted partial state: inspect the repository and authoritative artifacts, "
                    "then continue from evidence rather than assuming it succeeded.\n"
                    + json.dumps(abandoned[-1], ensure_ascii=False, indent=2, sort_keys=True)
                )
        else:
            sections.append(
                "Continue this existing logical and provider session. Apply the orchestration law already established in the session. "
                "The compact material below is the new delta; inspect repository evidence directly when needed."
            )
        abandoned_validations = state.get("abandoned_validations") or []
        if abandoned_validations:
            sections.append(
                "# Validation recovery context\n"
                "A prior host validation process ended without a trustworthy completion record. "
                "Do not assume it passed, failed, or was safe to rerun. Inspect repository and host evidence, "
                "then repair or request the exact authority needed.\n"
                + json.dumps(abandoned_validations[-1], ensure_ascii=False, indent=2, sort_keys=True)
            )
        sections.append(self._prompt_file(state, stage.prompt_file).strip())
        if direction_text and direction_text.strip():
            sections.append("# Orchestrator direction\n" + direction_text.strip())
        if stance_text and stance_text.strip():
            sections.append("# Explicit one-turn stance override\n" + stance_text.strip())
        immediate_decision_file: str | None = None
        if state.get("decisions"):
            latest_decision = state["decisions"][-1]
            if (
                latest_decision.get("cycle") == state["cycle"]
                and latest_decision.get("after_turn") == state["current_turn"]
            ):
                immediate_decision_file = str(latest_decision["file"])
                sections.append(
                    "# Immediate human direction\n"
                    + self._artifact_text(state, immediate_decision_file)
                )
        context_tokens = list(stage.context)
        for token in context_tokens:
            context = self._context_section(
                state,
                token,
                exclude_decision_file=immediate_decision_file,
            )
            if context:
                title, text = context
                sections.append(f"# {title}\n{text}")
        sections.append(self._prompt_file(state, "strict-contract.md").strip())
        return ("\n\n".join(sections) + "\n").encode("utf-8")

    def _session(
        self,
        state: dict[str, Any],
        stage: StageDefinition,
        profile: ProfileDefinition,
        action_override: str | None = None,
    ) -> tuple[dict[str, Any], str, str | None]:
        cycle = self._cycle(state)
        sessions = cycle["sessions"]
        slot = sessions.get(stage.session_slot)
        if slot is None:
            slot = {
                "slot": stage.session_slot,
                "label": _session_label(self._workflow(state), state["cycle"], stage.session_slot),
                "provider": profile.provider,
                "active_generation": 0,
                "active_session_id": None,
                "history": [],
            }
            sessions[stage.session_slot] = slot
        if slot["provider"] != profile.provider:
            if action_override != "new" or not stage.provider_switchable:
                raise ValueError(f"session slot {stage.session_slot} cannot change providers")
            for previous in slot["history"]:
                if previous.get("status") == "active":
                    previous["status"] = "superseded"
            slot["provider"] = profile.provider
            slot["active_session_id"] = None
        if action_override is not None:
            if action_override not in {"new", "continue"}:
                raise ValueError(f"invalid next-turn session action: {action_override}")
            action = action_override
        elif stage.session_policy == "new":
            action = "new"
        elif stage.session_policy == "continue":
            action = "continue"
        else:
            action = "continue" if slot.get("active_session_id") else "new"
        session_id = slot.get("active_session_id")
        if action == "continue" and not session_id:
            raise ValueError(f"session slot {stage.session_slot} has no active session to continue")
        return slot, action, session_id

    def _store_turn(
        self,
        state: dict[str, Any],
        stage: StageDefinition,
        profile: ProfileDefinition,
        slot: dict[str, Any],
        action: str,
        result: ProviderResult,
        prompt: bytes,
        directive: Directive | None,
        *,
        correction: bool = False,
        direction_text: str | None = None,
        steer_of: str | None = None,
        steer_note: str | None = None,
        stance_override: str | None = None,
    ) -> dict[str, Any]:
        number = state["current_turn"] + 1
        turn_id = f"turn.{number:04d}"
        turns = self._run_dir(state["run_id"]) / "turns"
        prompt_name = f"{turn_id}.prompt.md"
        response_name = f"{turn_id}.response.md"
        raw_name = f"{turn_id}.output.raw"
        stderr_name = f"{turn_id}.stderr.raw"
        text_name = f"{turn_id}.output.md"
        prompt_hash = atomic_write(turns / prompt_name, prompt)
        response_hash = write_text(turns / response_name, result_text(result))
        raw_hash = atomic_write(turns / raw_name, result.stdout)
        stderr_hash = atomic_write(turns / stderr_name, result.stderr)
        output = result_text(result)
        work = work_product_text(output)
        output_hash = write_text(turns / text_name, work)
        substantive = result.exit_code == 0 and bool(work) and not correction
        interstitial = state.get("prompt_library", {}).get(stage.prompt_file, {})
        interstitial_file = str(interstitial.get("path", "")) or None
        interstitial_sha256 = str(interstitial.get("sha256", "")) or None
        direction_name: str | None = None
        direction_hash: str | None = None
        if direction_text and direction_text.strip() and not correction:
            direction_name = f"{turn_id}.direction.md"
            direction_hash = write_text(turns / direction_name, direction_text)
        steer_note_name: str | None = None
        steer_note_hash: str | None = None
        if steer_note:
            steer_note_name = f"{turn_id}.steer-note.md"
            steer_note_hash = write_text(turns / steer_note_name, steer_note)
        stance_name: str | None = None
        stance_hash: str | None = None
        if stance_override:
            stance_name = f"{turn_id}.stance-override.md"
            stance_hash = write_text(turns / stance_name, CURATED_STANCES[stance_override])
        previous_session_id = slot.get("active_session_id")
        previously_seen = bool(
            result.session_id
            and result.session_id in self._provider_session_ids(state, profile.provider)
        )
        session_promotable = bool(
            result.exit_code == 0
            and result.session_id
            and action == "new"
            and not previously_seen
        )
        record = {
            "id": turn_id,
            "cycle": state["cycle"],
            "stage": stage.id,
            "phase": stage.phase,
            "route": stage.id,
            "title": stage.title,
            "prompt_kind": stage.prompt_kind,
            "prompt_label": stage.prompt_label,
            "role": stage.role,
            "provider": profile.provider,
            "profile": profile.id,
            "profile_label": profile.label,
            "configured_model": profile.model,
            "configured_reasoning": profile.effort,
            "custom_selection": profile.custom,
            "observed_model": result.observed_model,
            "observed_reasoning": result.observed_reasoning,
            "observation_source": result.observation_source,
            "observation_error": result.observation_error,
            "model_usage": result.model_usage,
            "permission": profile.permission,
            "session_slot": stage.session_slot,
            "session_label": slot["label"],
            "session_action": action,
            "session_generation": slot["active_generation"] + (1 if session_promotable else 0),
            "session_id": result.session_id,
            "session_promoted": session_promotable,
            "prompt_file": prompt_name,
            "response_file": response_name,
            "interstitial_file": interstitial_file,
            "interstitial_sha256": interstitial_sha256,
            "direction_file": direction_name,
            "direction_sha256": direction_hash,
            "output_file": text_name,
            "raw_file": raw_name,
            "stderr_file": stderr_name,
            "prompt_sha256": prompt_hash,
            "response_sha256": response_hash,
            "output_sha256": output_hash,
            "raw_sha256": raw_hash,
            "stderr_sha256": stderr_hash,
            "exit_code": result.exit_code,
            "elapsed_ms": result.elapsed_ms,
            "usage": result.usage,
            "directive": asdict(directive) if directive else None,
            "artifact_type": "directive-correction" if correction else stage.artifact_type,
            "correction": correction,
            "substantive": substantive,
            "provider_error": result.error,
            "steer_of": steer_of,
            "steer_note_file": steer_note_name,
            "steer_note_sha256": steer_note_hash,
            "self_caption": extract_self_caption(output),
            "stance_override": stance_override,
            "stance_override_file": stance_name,
            "stance_override_sha256": stance_hash,
        }
        metadata_hash = write_json(turns / f"{turn_id}.json", record)
        record["metadata_sha256"] = metadata_hash
        state["current_turn"] = number
        state["turns"].append(record)
        cycle = self._cycle(state)
        if cycle["start_turn"] is None:
            cycle["start_turn"] = number
        if result.exit_code == 0 and result.session_id:
            if action == "new" and not previously_seen:
                for previous in slot["history"]:
                    if previous.get("status") == "active":
                        previous["status"] = "superseded"
                slot["active_generation"] += 1
                slot["active_session_id"] = result.session_id
                slot["history"].append({
                    "generation": slot["active_generation"],
                    "session_id": result.session_id,
                    "started_turn": turn_id,
                    "status": "active",
                })
            elif action == "new":
                state["errors"].append(f"session_id_not_new:{stage.session_slot}:{result.session_id}")
            elif result.session_id != slot["active_session_id"]:
                state["errors"].append(
                    f"session_id_changed:{stage.session_slot}:{slot['active_session_id']}:{result.session_id}"
                )
        self._event(state, "provider.turn.completed", title=stage.title, details={
            "turn_id": turn_id,
            "provider": profile.provider,
            "role": stage.role,
            "phase": stage.phase,
            "prompt_kind": stage.prompt_kind,
            "session_slot": stage.session_slot,
            "session_label": slot["label"],
            "session_action": action,
            "session_generation": record["session_generation"],
            "profile": profile.id,
            "configured_model": profile.model,
            "configured_reasoning": profile.effort,
            "permission": profile.permission,
            "outcome": directive.next if directive else None,
            "artifacts": {
                "prompt": {"path": f"turns/{prompt_name}", "sha256": prompt_hash},
                "interstitial": {"path": interstitial_file, "sha256": interstitial_sha256},
                "direction": {"path": f"turns/{direction_name}" if direction_name else None, "sha256": direction_hash},
                "output": {"path": f"turns/{text_name}", "sha256": output_hash},
                "metadata": {"path": f"turns/{turn_id}.json", "sha256": metadata_hash},
            },
        })
        return record

    @staticmethod
    def _correction_prompt(original_prompt: bytes, original_output: str) -> bytes:
        return Orchestrator._correction_prompt(original_prompt, original_output)

    def _prepare_validation(
        self,
        state: dict[str, Any],
        stage: StageDefinition,
        directive: Directive,
        evidence: Any,
    ) -> None:
        project = self._project(state)
        sealed = seal_worktree_evidence(
            self._run_dir(state["run_id"]),
            state["cycle"],
            state["current_turn"],
            evidence,
            kind="pre-validation",
        )
        state["pending_validation"] = {
            "stage": stage.id,
            "repair_stage": self._repair_stage(stage),
            "directive_next": directive.next,
            "turn": state["current_turn"],
            "pre_validation_evidence": sealed,
            "commands": [
                {
                    "id": item.id,
                    "command": item.command,
                    "environment": item.environment,
                    "required": item.required,
                }
                for item in project.validations
                if item.environment in {"local", "either"}
            ],
        }

    def _execute_pending_validation(self, state: dict[str, Any]) -> None:
        pending = state.get("pending_validation")
        if not isinstance(pending, dict):
            raise ValueError("no implementation validation is pending")
        workflow = self._workflow(state)
        stage = workflow.stages[str(pending["stage"])]
        directive = Directive(next=str(pending["directive_next"]))
        project = self._project(state)
        worktree = Path(state["execution_worktree"])
        identity_error = self._worktree_identity_error(state)
        if identity_error:
            raise ValueError(identity_error)
        current = collect_worktree_evidence(worktree)
        assert_allowed_changes(project, current.changed_paths)
        expected = pending["pre_validation_evidence"]
        if sha256(current.patch) != expected["patch"]["sha256"] or list(current.changed_paths) != list(expected["changed_paths"]):
            raise ValueError("implementation changed while validation approval was pending")
        if state.get("validation_inflight"):
            raise ValueError("a validation execution is already marked in flight")
        validation_execution = {
            "execution_id": f"validation_{uuid.uuid4().hex}",
            "cycle": state["cycle"],
            "turn": int(pending["turn"]),
            "stage": stage.id,
            "repair_stage": pending.get("repair_stage"),
            "commands": list(pending.get("commands", [])),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        state["validation_inflight"] = validation_execution
        state["inflight"] = None
        state["status"] = "running"
        self._event(
            state,
            "validation.execution.started",
            title="Host validation started",
            details=validation_execution,
        )
        self._save(state["run_id"], state)
        validations = run_project_validations(
            project,
            worktree,
            self._run_dir(state["run_id"]),
            state["cycle"],
            int(pending["turn"]),
        )
        identity_error = self._worktree_identity_error(state)
        if identity_error:
            raise ValueError(f"validation changed worktree identity: {identity_error}")
        evidence = collect_worktree_evidence(worktree)
        assert_allowed_changes(project, evidence.changed_paths)
        sealed = seal_worktree_evidence(
            self._run_dir(state["run_id"]), state["cycle"], int(pending["turn"]), evidence
        )
        run_dir = self._run_dir(state["run_id"])
        for value in validations.values():
            for stream in ("stdout", "stderr"):
                record = value.get(stream)
                if not isinstance(record, dict) or not record.get("path"):
                    continue
                artifact_path = run_dir / str(record["path"])
                text = artifact_path.read_text(encoding="utf-8", errors="replace")
                record["absolute_path"] = str(artifact_path)
                record["tail"] = text[-6000:]
        sealed["validations"] = validations
        state["current_implementation_evidence"] = sealed
        state["validations"] = validations
        state["pending_validation"] = None
        state["validation_inflight"] = None
        self._event(state, "implementation.evidence.sealed", title="Implementation evidence", details=sealed)
        self._event(
            state,
            "validation.execution.completed",
            title="Host validation completed",
            details={
                **validation_execution,
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "states": {key: value.get("state") for key, value in validations.items()},
            },
        )
        self._transition(state, stage, directive)

    def _finalize_acceptance(
        self,
        state: dict[str, Any],
        *,
        next_stage: str,
        accepted_revision: str,
        evidence: dict[str, Any],
    ) -> None:
        cycle = self._cycle(state)
        pending_commit = state.get("pending_commit") or {}
        review_stage = str(pending_commit.get("review_stage") or state.get("current_stage") or "")
        state["working_revision"] = accepted_revision
        state["pending_completion"] = None
        state["pending_commit"] = None
        review = next(
            (
                turn
                for turn in reversed(state["turns"])
                if turn.get("stage") == review_stage
                and turn.get("substantive")
                and not turn.get("correction")
            ),
            None,
        )
        receipt = {
            "schema_version": "toledo_orchestrator.completion.v1",
            "cycle": state["cycle"],
            "approved_handoff": cycle["approved_handoff"],
            "accepted_revision": accepted_revision,
            "implementation_evidence": evidence,
            "review_turn": review["id"] if review else None,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        relative = Path("artifacts") / f"cycle.{state['cycle']:04d}.completion-receipt.json"
        digest = write_json(self._run_dir(state["run_id"]) / relative, receipt)
        normalized = str(relative).replace("\\", "/")
        state["artifacts"][normalized] = {
            "sha256": digest,
            "type": "completion-receipt",
            "cycle": state["cycle"],
        }
        state["completion_receipt"] = normalized
        cycle["completion_receipt"] = normalized
        self._event(state, "implementation.accepted", title="Project change accepted", details={
            "accepted_revision": accepted_revision,
            "completion_receipt": normalized,
            "sha256": digest,
        })
        state["current_stage"] = next_stage
        state["status"] = "running"

    def _resume_pending_commit(self, state: dict[str, Any]) -> None:
        pending = state.get("pending_commit")
        if not isinstance(pending, dict):
            return
        worktree = Path(state["execution_worktree"])
        if current_branch(worktree) != state["execution_branch"]:
            raise ValueError("pending acceptance commit branch changed")
        base_revision = str(pending["base_revision"])
        expected_patch_hash = str(pending["patch_sha256"])
        evidence = state.get("current_implementation_evidence") or {}
        expected_patch = self._artifact_bytes(state, str(evidence["patch"]["path"]))
        if sha256(expected_patch) != expected_patch_hash:
            raise ValueError("pending acceptance patch hash changed")
        observed_revision = current_revision(worktree)
        if observed_revision == base_revision:
            current = collect_worktree_evidence(worktree)
            if sha256(current.patch) != expected_patch_hash:
                raise ValueError("pending acceptance worktree no longer matches the reviewed patch")
            accepted_revision = commit_accepted_changes(
                worktree,
                str(pending["message"]),
                expected_patch,
                self._run_dir(state["run_id"]) / "empty-git-hooks",
            )
        else:
            if parent_revision(worktree, observed_revision) != base_revision:
                raise ValueError("pending acceptance commit is not a direct child of the reviewed revision")
            if sha256(revision_patch(worktree, base_revision, observed_revision)) != expected_patch_hash:
                raise ValueError("pending acceptance commit does not match the reviewed patch")
            if collect_worktree_evidence(worktree).changed_paths:
                raise ValueError("pending acceptance commit left uncommitted changes")
            accepted_revision = observed_revision
        self._finalize_acceptance(
            state,
            next_stage=str(pending["next_stage"]),
            accepted_revision=accepted_revision,
            evidence=evidence,
        )

    @_locked
    def advance(self, run_id: str) -> dict[str, Any]:
        state = self.state(run_id)
        if state.get("pending_commit"):
            try:
                self._resume_pending_commit(state)
            except (OSError, ValueError) as error:
                state["status"] = "paused"
                state["pending_human_decision"] = "acceptance_commit_recovery_failed"
                state["errors"].append(str(error))
            self._save(run_id, state)
            return state
        if state.get("validation_inflight"):
            state["status"] = "paused"
            state["pending_human_decision"] = "unknown_validation_execution"
            if not state["errors"] or state["errors"][-1] != "unknown_validation_execution_requires_human":
                state["errors"].append("unknown_validation_execution_requires_human")
            self._event(
                state,
                "human.gate.opened",
                title="Unknown validation execution",
                details={"reason": "unknown_validation_execution", **state["validation_inflight"]},
            )
            self._save(run_id, state)
            return state
        if state["status"] in {"complete", "cancelled", "paused", "failed"}:
            return state
        if state.get("inflight"):
            state["status"] = "paused"
            state["pending_human_decision"] = "unknown_provider_invocation"
            state["errors"].append("unknown_provider_invocation_requires_human")
            self._save(run_id, state)
            return state
        workflow = self._workflow(state)
        stage = workflow.stages[state["current_stage"]]
        override = state.get("next_turn_override") or {}
        if override and override.get("target_stage") not in {None, stage.id}:
            self._event(
                state,
                "turn.override.expired",
                title="Stale next-turn override cleared",
                details={"target_stage": override.get("target_stage"), "current_stage": stage.id},
            )
            state["next_turn_override"] = None
            override = {}
        profile_id = str(override.get("profile") or stage.profile)
        if (
            not isinstance(override.get("profile_value"), dict)
            and profile_id not in workflow.profiles
            and profile_id not in state.get("workflow_snapshot", {}).get("profiles", {})
        ):
            state["status"] = "paused"
            state["pending_human_decision"] = "invalid_next_turn_profile"
            state["errors"].append(f"unknown profile override: {profile_id}")
            self._save(run_id, state)
            return state
        profile = self._profile(state, profile_id, override)
        write_allowed = stage.phase == "implementation" and stage.role == "implementer"
        if profile.permission == "workspace-write" and not write_allowed:
            state["status"] = "paused"
            state["pending_human_decision"] = "profile_permission_exceeds_stage"
            state["errors"].append(
                f"profile {profile.id} cannot grant workspace-write to stage {stage.id}"
            )
            self._save(run_id, state)
            return state
        project = self._project(state)
        worktree = Path(state["execution_worktree"])
        if not worktree.is_dir():
            state["status"] = "paused"
            state["pending_human_decision"] = "execution_worktree_missing"
            state["errors"].append("execution_worktree_missing")
            self._save(run_id, state)
            return state
        identity_error = self._worktree_identity_error(state)
        if identity_error:
            state["status"] = "paused"
            state["pending_human_decision"] = "execution_worktree_identity_changed"
            state["errors"].append(identity_error)
            self._save(run_id, state)
            return state
        try:
            slot, action, session_id = self._session(state, stage, profile, override.get("session_action"))
        except ValueError as error:
            state["status"] = "paused"
            state["pending_human_decision"] = "provider_session_missing"
            state["errors"].append(str(error))
            self._save(run_id, state)
            return state
        direction_text = self._compose_direction_text(state, stage)
        try:
            stance_override = override.get("stance")
            stance_text = CURATED_STANCES.get(str(stance_override)) if stance_override else None
            prompt = self._prompt(state, stage, profile, action, slot["label"], direction_text, stance_text)
        except (OSError, ValueError) as error:
            state["status"] = "paused"
            state["pending_human_decision"] = "artifact_integrity_failed"
            state["errors"].append(str(error))
            self._save(run_id, state)
            return state
        state["status"] = "running"
        state["inflight"] = {
            "stage": stage.id,
            "session_slot": stage.session_slot,
            "session_action": action,
            "session_id": session_id,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        state["next_turn_override"] = None
        self._event(state, "provider.turn.started", title=stage.title, details={
            "provider": profile.provider,
            "session_label": slot["label"],
            "session_action": action,
            "profile": profile.id,
            "prompt_kind": stage.prompt_kind,
        })
        self._save(run_id, state)
        adapter = self.adapters[profile.provider]
        known_session_ids = self._provider_session_ids(state, profile.provider)
        result = adapter.invoke_configured(
            stage.id,
            prompt,
            worktree,
            model=profile.model,
            reasoning=profile.effort,
            permission=profile.permission,
            session_action=action,
            session_id=session_id,
            timeout=profile.timeout_seconds,
        )
        state = self.state(run_id)
        state["inflight"] = None
        post_provider_identity_error = self._worktree_identity_error(state)
        output = result_text(result)
        directive = extract_directive(output) if result.exit_code == 0 else None
        session_mismatch = action == "continue" and result.session_id != session_id
        session_not_new = action == "new" and result.session_id in known_session_ids
        self._store_turn(state, stage, profile, slot=self._cycle(state)["sessions"][stage.session_slot], action=action,
                         result=result, prompt=prompt, directive=directive, direction_text=direction_text, stance_override=stance_override)
        if post_provider_identity_error:
            state["status"] = "paused"
            state["pending_human_decision"] = "provider_changed_worktree_identity"
            state["errors"].append(post_provider_identity_error)
            self._save(run_id, state)
            return state
        if result.exit_code != 0:
            state["status"] = "paused"
            state["pending_human_decision"] = "provider_invocation_failed"
            state["errors"].append(f"{stage.id}:exit:{result.exit_code}")
            # A quota/network failure must not silently fall back to the route
            # default on Retry. Keep the exact explicit model/effort/profile
            # the operator chose until a successful turn consumes it or they
            # deliberately replace it at the retry gate.
            if override:
                state["next_turn_override"] = override
            self._save(run_id, state)
            return state
        if not result.session_id:
            state["status"] = "paused"
            state["pending_human_decision"] = "provider_session_id_missing"
            state["errors"].append(f"{stage.id}:provider_session_id_missing")
            self._save(run_id, state)
            return state
        if session_not_new:
            state["status"] = "paused"
            state["pending_human_decision"] = "provider_session_not_new"
            state["errors"].append(f"{stage.id}:new_session_reused:{result.session_id}")
            self._save(run_id, state)
            return state
        if session_mismatch:
            state["status"] = "paused"
            state["pending_human_decision"] = "provider_session_changed_unexpectedly"
            state["errors"].append(f"{stage.id}:expected_session:{session_id}:observed:{result.session_id}")
            self._save(run_id, state)
            return state
        if not has_substantive_work(output, directive):
            state["status"] = "paused"
            state["pending_human_decision"] = "missing_substantive_output"
            state["errors"].append(f"{stage.id}:missing_substantive_output")
            self._save(run_id, state)
            return state
        if directive is None or directive.conflict or directive.valid_block_count != 1:
            correction_prompt = self._correction_prompt(prompt, output)
            state["inflight"] = {
                "stage": stage.id,
                "session_slot": stage.session_slot,
                "session_action": "continue",
                "session_id": result.session_id,
                "correction": True,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self._save(run_id, state)
            correction = adapter.invoke_configured(
                stage.id,
                correction_prompt,
                worktree,
                model=profile.model,
                reasoning=profile.effort,
                permission=profile.permission,
                session_action="continue",
                session_id=result.session_id,
                timeout=profile.timeout_seconds,
            )
            state = self.state(run_id)
            state["inflight"] = None
            directive = extract_directive(result_text(correction)) if correction.exit_code == 0 else None
            if directive and (directive.conflict or directive.valid_block_count != 1):
                directive = None
            self._store_turn(state, stage, profile, slot=self._cycle(state)["sessions"][stage.session_slot], action="continue",
                             result=correction, prompt=correction_prompt, directive=directive, correction=True)
            if correction.session_id != result.session_id:
                state["status"] = "paused"
                state["pending_human_decision"] = "provider_session_changed_unexpectedly"
                state["errors"].append(
                    f"{stage.id}:correction_expected_session:{result.session_id}:observed:{correction.session_id}"
                )
                self._save(run_id, state)
                return state
            if directive is None:
                state["status"] = "paused"
                state["pending_human_decision"] = "malformed_directive"
                state["errors"].append(f"{stage.id}:malformed_directive")
                self._save(run_id, state)
                return state
        if stage.phase == "implementation" and directive.next == "human":
            self._transition(state, stage, directive)
            self._save(run_id, state)
            return state
        if stage.phase == "implementation":
            try:
                pre_validation_evidence = collect_worktree_evidence(worktree)
                assert_allowed_changes(project, pre_validation_evidence.changed_paths)
                self._prepare_validation(state, stage, directive, pre_validation_evidence)
                if project.validation_requires_approval and state["pending_validation"]["commands"]:
                    state["status"] = "paused"
                    state["pending_human_decision"] = "validation_execution_approval"
                    self._event(
                        state,
                        "human.gate.opened",
                        title="Approve validation execution",
                        details={"reason": "validation_execution_approval", "commands": state["pending_validation"]["commands"]},
                    )
                    self._save(run_id, state)
                    return state
                self._execute_pending_validation(state)
            except ValueError as error:
                state["status"] = "paused"
                if state.get("validation_inflight"):
                    state["pending_human_decision"] = "unknown_validation_execution"
                else:
                    state["pending_human_decision"] = "implementation_boundary_failed"
                    state["pending_repair_stage"] = self._repair_stage(stage)
                state["errors"].append(str(error))
                self._save(run_id, state)
                return state
        if stage.phase != "implementation":
            try:
                self._transition(state, stage, directive)
            except (OSError, ValueError) as error:
                state["status"] = "paused"
                state["pending_human_decision"] = "artifact_integrity_failed"
                state["errors"].append(str(error))
        self._pause_for_step(state)
        self._save(run_id, state)
        return state

    def _seal_latest(self, state: dict[str, Any], artifact_type: str, sealed_type: str) -> str:
        if not IDENTIFIER.fullmatch(sealed_type):
            raise ValueError(f"invalid sealed artifact type: {sealed_type}")
        turn = self._latest_turn(state, artifact_type)
        if not turn:
            raise ValueError(f"cannot seal missing {artifact_type} artifact")
        relative = Path("artifacts") / f"cycle.{state['cycle']:04d}.{sealed_type}.md"
        source_bytes = self._artifact_bytes(state, f"turns/{turn['output_file']}")
        digest = atomic_write(self._run_dir(state["run_id"]) / relative, source_bytes)
        state["artifacts"][str(relative).replace("\\", "/")] = {
            "sha256": digest,
            "type": sealed_type,
            "cycle": state["cycle"],
            "source_turn": turn["id"],
        }
        self._event(state, "artifact.sealed", title=sealed_type.replace("-", " ").title(), details={
            "artifact_type": sealed_type,
            "path": str(relative).replace("\\", "/"),
            "sha256": digest,
            "source_turn": turn["id"],
        })
        return str(relative).replace("\\", "/")

    @staticmethod
    def _repair_stage(stage: StageDefinition) -> str | None:
        target = stage.repair_stage
        return target if target and not target.startswith("@") else None

    def _consume_round(
        self,
        state: dict[str, Any],
        stage: StageDefinition,
        *,
        target: str,
        pause_reason: str | None = None,
    ) -> bool:
        if not stage.round_counter or stage.round_cap is None:
            return True
        cycle = self._cycle(state)
        record = self._round_record(cycle, stage.round_counter)
        cap = int(stage.round_cap) + int(record["extension"])
        if int(record["count"]) >= cap:
            reason = pause_reason or stage.round_pause_reason or "round_cap_reached"
            state["status"] = "paused"
            state["pending_human_decision"] = reason
            state["pending_round_extension"] = {
                "counter": stage.round_counter,
                "stage": stage.id,
                "target": target,
                "reason": reason,
            }
            state["degraded"] = True
            return False
        record["count"] = int(record["count"]) + 1
        self._sync_round_alias(cycle, stage.round_counter, record)
        return True

    @staticmethod
    def _describe_transition(workflow: WorkflowDefinition, target: str) -> str:
        if target.startswith("@pause:"):
            reason = target.split(":", 1)[1]
            named = {
                "next_task_approval": "pause for your approval of the proposed next task",
                "provider_requested_human": "pause for your input",
            }
            return named.get(reason, f"pause: {reason.replace('_', ' ')}")
        if target.startswith("@seal:"):
            _, sealed_type, next_stage = target.split(":", 2)
            title = workflow.stages[next_stage].title if next_stage in workflow.stages else next_stage
            return f"seal the {sealed_type.replace('-', ' ')} and move to {title}"
        if target.startswith("@complete:"):
            next_stage = target.split(":", 1)[1]
            title = workflow.stages[next_stage].title if next_stage in workflow.stages else next_stage
            return f"run validation, commit the accepted change, then {title}"
        title = workflow.stages[target].title if target in workflow.stages else target
        return f"move to {title}"

    def stage_prompt_template(self, workflow_id: str, stage_id: str) -> dict[str, Any]:
        """Exact static instruction bytes for a stage, before any run exists."""
        if workflow_id not in self.workflows:
            raise ValueError(f"unknown workflow: {workflow_id}")
        workflow = self.workflows[workflow_id]
        stage = workflow.stages.get(stage_id)
        if stage is None:
            raise ValueError(f"unknown stage: {stage_id}")
        source = self._prompt_source(workflow, stage.prompt_file)
        return {
            "workflow": workflow_id,
            "stage": stage_id,
            "title": stage.title,
            "prompt_file": stage.prompt_file,
            "template": source.read_text(encoding="utf-8"),
            "source": str(source),
            "context": list(stage.context),
            "produces": stage.artifact_type,
            "afterward": [
                {"directive": key, "description": self._describe_transition(workflow, target)}
                for key, target in stage.transitions.items()
            ],
        }

    def _steer_availability(self, state: dict[str, Any]) -> dict[str, Any]:
        """Describe whether Steer can truthfully continue the current artifact."""
        if state.get("status") != "paused":
            return {"available": False, "reason": "run is not paused"}
        if state.get("inflight") or state.get("validation_inflight"):
            return {"available": False, "reason": "an operation is already active"}
        pending_controls = (
            "pending_validation",
            "pending_completion",
            "pending_commit",
            "pending_round_extension",
            "pending_baseline_acceptance",
        )
        if any(state.get(key) for key in pending_controls):
            return {
                "available": False,
                "reason": "finish the pending deterministic decision before revising a provider artifact",
            }
        pending_reason = state.get("pending_human_decision")
        if pending_reason not in {"operator_step", "provider_requested_human"}:
            return {
                "available": False,
                "reason": f"resolve {pending_reason or 'the current control gate'} before revising a provider artifact",
            }
        stage_id = state.get("current_stage")
        if not stage_id:
            return {"available": False, "reason": "run has no current stage"}
        workflow = self._workflow(state)
        stage = workflow.stages.get(str(stage_id))
        if stage is None:
            return {"available": False, "reason": f"current stage is unknown: {stage_id}"}
        slot = self._cycle(state).get("sessions", {}).get(stage.session_slot)
        if not isinstance(slot, dict) or not slot.get("active_session_id"):
            return {
                "available": False,
                "reason": f"current stage {stage.id} has no active {stage.session_slot} provider session",
                "stage": stage.id,
            }
        latest = next(
            (
                turn
                for turn in reversed(state.get("turns", []))
                if turn.get("stage") == stage.id
                and turn.get("session_slot") == stage.session_slot
                and not turn.get("correction")
            ),
            None,
        )
        if latest is None:
            return {
                "available": False,
                "reason": f"current stage {stage.id} has no artifact to replace",
                "stage": stage.id,
            }
        active_session_id = str(slot["active_session_id"])
        if str(latest.get("session_id") or "") != active_session_id:
            return {
                "available": False,
                "reason": "the latest current-stage artifact is not bound to the active provider session",
                "stage": stage.id,
                "turn_id": latest.get("id"),
            }
        if str(latest.get("provider") or "") != str(slot.get("provider") or ""):
            return {
                "available": False,
                "reason": "the current-stage artifact provider does not match the active session",
                "stage": stage.id,
                "turn_id": latest.get("id"),
            }
        if str(latest.get("permission") or "") != "read-only":
            return {
                "available": False,
                "reason": "Steer is limited to read-only artifacts; use the workflow repair path for workspace changes",
                "stage": stage.id,
                "turn_id": latest.get("id"),
            }
        return {
            "available": True,
            "reason": None,
            "stage": stage.id,
            "stage_title": stage.title,
            "turn_id": latest.get("id"),
            "turn_title": latest.get("title") or stage.title,
            "provider": latest.get("provider"),
            "model": latest.get("configured_model"),
            "effort": latest.get("configured_reasoning"),
            "session_slot": stage.session_slot,
            "session_label": latest.get("session_label") or slot.get("label"),
            "session_id": active_session_id,
        }

    def steer_availability(self, run_id: str) -> dict[str, Any]:
        return self._steer_availability(self.state(run_id))

    def next_turn_preview(self, run_id: str) -> dict[str, Any]:
        """Read-only contract describing exactly what the next provider turn will do.

        Everything here mirrors ``advance`` resolution (override, session,
        direction, prompt) without mutating run state, so the operator gate can
        show the truth instead of a generic question.
        """
        state = self.state(run_id)
        steer = self._steer_availability(state) if state.get("schema_version") == "toledo_orchestrator.run.v2" else {"available": False, "reason": "legacy run schema"}
        if state.get("schema_version") != "toledo_orchestrator.run.v2":
            return {"available": False, "reason": "legacy run schema", "steer": steer}
        stage_id = state.get("current_stage")
        pending = state.get("pending_human_decision")
        if not stage_id or state.get("inflight") or state.get("status") in {"complete", "cancelled", "failed", "stopped"}:
            return {"available": False, "reason": pending or state.get("status"), "steer": steer}
        if pending in {"validation_execution_approval", "validation_receipt_required", "unknown_validation_execution"}:
            return {"available": False, "reason": pending, "steer": steer}
        workflow = self._workflow(state)
        stage = workflow.stages.get(str(stage_id))
        if stage is None:
            return {"available": False, "reason": f"unknown stage {stage_id}", "steer": steer}
        override = state.get("next_turn_override")
        override_applies = isinstance(override, dict) and override.get("target_stage") == stage.id
        active_override = override if override_applies else None
        profile_id = str(active_override.get("profile") or stage.profile) if active_override else stage.profile
        profile = self._profile(state, profile_id, active_override)
        base_profile = self._profile(state, profile_id)
        profile_overridden = bool(active_override) and (
            profile_id != stage.profile
            or profile.model != base_profile.model
            or profile.effort != base_profile.effort
        )
        cycle = self._cycle(state)
        slot = cycle.get("sessions", {}).get(stage.session_slot) or {}
        action_override = str(active_override.get("session_action")) if active_override and active_override.get("session_action") else None
        if action_override in {"new", "continue"}:
            action = action_override
        elif stage.session_policy in {"new", "continue"}:
            action = stage.session_policy
        else:
            action = "continue" if slot.get("active_session_id") else "new"
        session_label = slot.get("label") or _session_label(workflow, int(state["cycle"]), stage.session_slot)
        generation = int(slot.get("active_generation") or 0)
        direction_text = self._compose_direction_text(state, stage)
        stance_override = active_override.get("stance") if active_override else None
        stance_text = CURATED_STANCES.get(str(stance_override)) if stance_override else None
        prompt_text: str | None = None
        prompt_error: str | None = None
        try:
            prompt_text = self._prompt(
                state, stage, profile, action, str(session_label),
                direction_text=direction_text or None,
                stance_text=stance_text,
            ).decode("utf-8")
        except (OSError, ValueError) as error:
            prompt_error = f"{type(error).__name__}: {error}"
        inputs: list[dict[str, Any]] = []
        for token in stage.context:
            try:
                section = self._context_section(state, token)
            except (OSError, ValueError):
                section = None
            if section:
                title, text = section
                inputs.append({"token": token, "title": title, "empty": text.strip() in {"", "None."}, "chars": len(text)})
            else:
                inputs.append({"token": token, "title": token, "empty": True, "chars": 0})
        rounds = None
        if stage.round_counter and stage.round_cap is not None:
            record = self._round_record(cycle, stage.round_counter)
            rounds = {
                "counter": stage.round_counter,
                "used": int(record["count"]),
                "cap": int(stage.round_cap) + int(record["extension"]),
            }
        return {
            "available": True,
            "run_id": run_id,
            "status": state.get("status"),
            "pending_human_decision": pending,
            "run_mode": state.get("run_mode"),
            "stage": {
                "id": stage.id,
                "title": stage.title,
                "phase": stage.phase,
                "role": stage.role,
                "prompt_label": stage.prompt_label,
                "prompt_file": stage.prompt_file,
                "produces": stage.artifact_type,
                "provider_switchable": stage.provider_switchable,
            },
            "profile": {
                "id": profile_id,
                "label": profile.label,
                "provider": profile.provider,
                "model": profile.model,
                "effort": profile.effort,
                "permission": profile.permission,
                "custom": profile.custom,
                "overridden": profile_overridden,
            },
            "session": {
                "slot": stage.session_slot,
                "label": session_label,
                "action": action,
                "policy": stage.session_policy,
                "generation": generation,
                "overridden": bool(action_override),
                "has_active_session": bool(slot.get("active_session_id")),
                "active_provider": slot.get("provider"),
            },
            "override": {
                "active": bool(active_override),
                "stance": stance_override,
            },
            "inputs": inputs,
            "rounds": rounds,
            "afterward": [
                {"directive": key, "description": self._describe_transition(workflow, target)}
                for key, target in stage.transitions.items()
            ],
            "direction_preview": direction_text or None,
            "prompt": prompt_text,
            "prompt_error": prompt_error,
            "steer": steer,
        }

    @_locked
    def stop_run(self, run_id: str, note: bytes = b"") -> dict[str, Any]:
        """Record a deliberate operator finish. Unlike cancel, this is not a failure label."""
        state = self.state(run_id)
        if state.get("status") != "paused" or state.get("inflight") or state.get("validation_inflight"):
            raise ValueError("only a paused run with no active operation can be finished")
        decode_text_artifact(note, "operator stop note")
        reason = str(state.get("pending_human_decision") or "operator_stop")
        payload = note if note.strip() else b"Operator finished the run here.\n"
        self._record_decision(state, payload=payload, choice="stop", reason=reason, title="Human: finish run")
        cycle = self._cycle(state)
        cycle["status"] = "stopped"
        cycle["end_turn"] = state["current_turn"]
        state["status"] = "stopped"
        state["current_stage"] = None
        state["pending_human_decision"] = None
        state["pending_validation"] = None
        state["pending_completion"] = None
        state["pending_commit"] = None
        state["pending_round_extension"] = None
        state["pending_repair_stage"] = None
        state["pending_baseline_acceptance"] = None
        state["inflight"] = None
        self._event(state, "run.stopped", title="Run finished by operator", details={"reason": reason})
        self._save(run_id, state)
        return state

    def _capture_baseline_validations(self, state: dict[str, Any]) -> dict[str, Any]:
        """Run the project validations once at the clean base revision.

        Uses a disposable detached worktree so environment-specific failures
        (present only inside worktrees) are measured in the same environment
        kind as the implementation run.
        """
        project = self._project(state)
        evidence = state.get("current_implementation_evidence") or {}
        revision = str(evidence.get("revision") or state["working_revision"])
        target = self.runtime_dir / "worktrees" / f"{state['run_id']}-baseline"
        if target.exists():
            remove_execution_worktree(project, target, force=True)
        create_execution_worktree(project, target, revision)
        try:
            results = run_project_validations(
                project, target, self._run_dir(state["run_id"]), state["cycle"], 0,
            )
        finally:
            try:
                remove_execution_worktree(project, target, force=True)
            except ValueError:
                pass
        run_dir = self._run_dir(state["run_id"])
        captured: dict[str, Any] = {}
        for validation_id, record in results.items():
            entry: dict[str, Any] = {
                "state": record.get("state"),
                "exit_code": record.get("exit_code"),
            }
            if record.get("state") == "failed":
                entry["signature"] = self._validation_signature(run_dir, record)
            captured[validation_id] = entry
        baseline = {
            "revision": revision,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "environment": "detached-worktree",
            "results": captured,
        }
        state["baseline_validations"] = baseline
        self._event(state, "validation.baseline.captured", title="Baseline validation captured", details=baseline)
        return baseline

    @staticmethod
    def _validation_signature(run_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
        def stream_text(key: str) -> str:
            value = record.get(key)
            if not isinstance(value, dict) or not value.get("path"):
                return ""
            try:
                return (run_dir / str(value["path"])).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
        return failure_signature(stream_text("stdout"), stream_text("stderr"), int(record.get("exit_code", -1)))

    def _classify_required_failures(self, state: dict[str, Any], validations: dict[str, Any]) -> dict[str, Any] | None:
        """Compare required validation failures against the clean-base baseline.

        overall: "unchanged_baseline" when every required failure matches a
        failure already present before the implementation; "new_regression"
        when any failure is new or changed; "indeterminate" when the baseline
        could not be captured or a signature is too weak to trust.
        """
        run_dir = self._run_dir(state["run_id"])
        failed = {
            key: value for key, value in validations.items()
            if value.get("required", True) and value.get("state") == "failed"
        }
        if not failed:
            return None
        baseline = state.get("baseline_validations")
        if not isinstance(baseline, dict):
            try:
                baseline = self._capture_baseline_validations(state)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                return {
                    "overall": "indeterminate",
                    "error": f"baseline capture failed: {type(error).__name__}: {error}",
                    "failures": {key: {"match": "unknown"} for key in failed},
                }
        failures: dict[str, Any] = {}
        matches: list[bool] = []
        indeterminate = False
        for validation_id, record in failed.items():
            current = self._validation_signature(run_dir, record)
            base_entry = (baseline.get("results") or {}).get(validation_id) or {}
            base_signature = base_entry.get("signature") if base_entry.get("state") == "failed" else None
            if base_signature is None:
                verdict = "new_regression"
                matches.append(False)
            elif current.get("weak") or base_signature.get("weak"):
                verdict = "indeterminate"
                indeterminate = True
            elif current["fingerprint"] == base_signature["fingerprint"]:
                verdict = "unchanged_baseline"
                matches.append(True)
            else:
                verdict = "new_regression"
                matches.append(False)
            failures[validation_id] = {
                "match": verdict,
                "current": current,
                "baseline": base_signature,
            }
        if indeterminate:
            overall = "indeterminate"
        elif matches and all(matches):
            overall = "unchanged_baseline"
        else:
            overall = "new_regression"
        return {
            "overall": overall,
            "baseline_revision": baseline.get("revision"),
            "baseline_captured_at": baseline.get("captured_at"),
            "failures": failures,
        }

    def _transition(self, state: dict[str, Any], stage: StageDefinition, directive: Directive) -> None:
        workflow = self._workflow(state)
        cycle = self._cycle(state)
        target = stage.transitions.get(directive.next)
        if target is None:
            state["status"] = "paused"
            state["pending_human_decision"] = "unsupported_stage_directive"
            state["errors"].append(f"{stage.id}:unsupported:{directive.next}")
            return
        if stage.round_counter and directive.next == stage.round_directive:
            if target.startswith("@"):
                raise ValueError(f"round-controlled stage {stage.id} requires a concrete transition target")
            if not self._consume_round(state, stage, target=target):
                return
        if target.startswith("@pause:"):
            reason = target.split(":", 1)[1]
            if stage.artifact_type == "next-task-proposal":
                turn = self._latest_turn(state, "next-task-proposal")
                cycle["next_task_proposal"] = f"turns/{turn['output_file']}" if turn else None
            state["status"] = "paused"
            state["pending_human_decision"] = reason
            self._event(state, "human.gate.opened", title="Human decision", details={"reason": reason})
            return
        if target.startswith("@seal:"):
            _, sealed_type, next_stage = target.split(":", 2)
            if not stage.seal_source:
                raise ValueError(f"stage {stage.id} has no configured seal source")
            relative = self._seal_latest(state, stage.seal_source, sealed_type)
            cycle["approved_handoff"] = relative
            state["current_stage"] = next_stage
            state["status"] = "running"
            return
        if target.startswith("@complete:"):
            next_stage = target.split(":", 1)[1]
            evidence = state.get("current_implementation_evidence") or {}
            validations = evidence.get("validations") or {}
            pending_remote = pending_required_validations(validations)
            if pending_remote:
                state["status"] = "paused"
                state["pending_human_decision"] = "validation_receipt_required"
                state["pending_completion"] = {"stage": stage.id, "next_stage": next_stage}
                self._event(state, "human.gate.opened", title="Remote validation receipt required", details={
                    "reason": "validation_receipt_required",
                    "validation_ids": pending_remote,
                    "working_revision": state["working_revision"],
                    "patch_sha256": evidence.get("patch", {}).get("sha256"),
                })
                return
            if not required_local_validations_passed(validations):
                classification = self._classify_required_failures(state, validations)
                if classification and classification.get("overall") == "unchanged_baseline":
                    # The implementation did not introduce this failure; looping
                    # repair on it burns rounds without any possible fix. The
                    # closure decision belongs to the operator.
                    state["status"] = "paused"
                    state["pending_human_decision"] = "validation_baseline_failure_decision"
                    state["pending_baseline_acceptance"] = {
                        "stage": stage.id,
                        "next_stage": next_stage,
                        "classification": classification,
                    }
                    self._event(state, "human.gate.opened", title="Pre-existing validation failure", details={
                        "reason": "validation_baseline_failure_decision",
                        "classification": classification,
                    })
                    return
                repair_stage = self._repair_stage(stage)
                if not repair_stage:
                    raise ValueError(f"stage {stage.id} has no repair stage for failed validation")
                if not self._consume_round(
                    state,
                    stage,
                    target=repair_stage,
                    pause_reason="validation_failed_at_repair_cap",
                ):
                    return
                state["errors"].append("reviewer_ready_but_validation_failed")
                if classification:
                    self._event(state, "validation.classified", title="Validation failure classified", details=classification)
                state["current_stage"] = repair_stage
                state["status"] = "running"
                return
            self._accept_implementation(state, stage, next_stage, evidence)
            return
        state["current_stage"] = target
        state["status"] = "running"

    def _accept_implementation(
        self,
        state: dict[str, Any],
        stage: StageDefinition,
        next_stage: str,
        evidence: dict[str, Any],
    ) -> None:
        project = self._project(state)
        worktree = Path(state["execution_worktree"])
        current_evidence = collect_worktree_evidence(worktree)
        try:
            assert_allowed_changes(project, current_evidence.changed_paths)
        except ValueError as error:
            state["status"] = "paused"
            state["pending_human_decision"] = "implementation_boundary_failed_before_commit"
            state["pending_repair_stage"] = self._repair_stage(stage)
            state["errors"].append(str(error))
            return
        expected_patch = evidence.get("patch", {}).get("sha256")
        if (
            expected_patch != sha256(current_evidence.patch)
            or list(current_evidence.changed_paths) != list(evidence.get("changed_paths", []))
        ):
            state["status"] = "paused"
            state["pending_human_decision"] = "implementation_changed_after_review"
            state["pending_repair_stage"] = self._repair_stage(stage)
            state["errors"].append("implementation evidence no longer matches the worktree")
            return
        if project.commit_on_accept:
            state["pending_commit"] = {
                "base_revision": state["working_revision"],
                "patch_sha256": expected_patch,
                "next_stage": next_stage,
                "review_stage": stage.id,
                "message": f"orchestrator: accept cycle {state['cycle']:04d}",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self._save(state["run_id"], state)
            try:
                accepted_revision = commit_accepted_changes(
                    worktree,
                    str(state["pending_commit"]["message"]),
                    current_evidence.patch,
                    self._run_dir(state["run_id"]) / "empty-git-hooks",
                )
            except ValueError as error:
                state["status"] = "paused"
                state["pending_human_decision"] = "acceptance_commit_failed"
                state["errors"].append(str(error))
                return
            state["pending_commit"]["accepted_revision"] = accepted_revision
            state["working_revision"] = accepted_revision
            self._save(state["run_id"], state)
        else:
            accepted_revision = current_revision(worktree)
        self._finalize_acceptance(
            state,
            next_stage=next_stage,
            accepted_revision=accepted_revision,
            evidence=evidence,
        )

    @_locked
    def run_to_stop(self, run_id: str) -> dict[str, Any]:
        state = self.state(run_id)
        if state["status"] == "created":
            state["status"] = "running"
            self._save(run_id, state)
        while self.state(run_id)["status"] not in {"complete", "cancelled", "stopped", "paused", "failed"}:
            self.advance(run_id)
        return self.state(run_id)

    @_locked
    def recover_run(self, run_id: str) -> dict[str, Any]:
        """Re-enter a v2 run after its owning CLI/UI process stopped."""

        state = self.state(run_id)
        if state.get("status") not in {"created", "running"}:
            raise ValueError("only an inactive created or running run can be recovered")
        self._event(
            state,
            "run.recovery.started",
            title="Run recovery started",
            details={
                "status": state.get("status"),
                "inflight_present": bool(state.get("inflight")),
                "stage": state.get("current_stage"),
            },
        )
        self._save(run_id, state)
        return self.run_to_stop(run_id)

    def _cancel(self, state: dict[str, Any], reason: str) -> None:
        cycle = self._cycle(state)
        cycle["status"] = "cancelled"
        cycle["end_turn"] = state["current_turn"]
        state["status"] = "cancelled"
        state["current_stage"] = None
        state["pending_human_decision"] = None
        state["pending_validation"] = None
        state["validation_inflight"] = None
        state["pending_completion"] = None
        state["pending_commit"] = None
        state["pending_round_extension"] = None
        state["pending_repair_stage"] = None
        state["pending_baseline_acceptance"] = None
        state["inflight"] = None
        self._event(state, "run.cancelled", title="Run cancelled", details={"reason": reason})

    def _force_new_session(self, state: dict[str, Any]) -> None:
        override = state.get("next_turn_override")
        preserved = dict(override) if isinstance(override, dict) else {}
        preserved.setdefault("profile", None)
        preserved.setdefault("profile_value", None)
        preserved.setdefault("target_stage", state.get("current_stage"))
        preserved["session_action"] = "new"
        state["next_turn_override"] = preserved
        state["status"] = "running"

    def _record_decision(
        self,
        state: dict[str, Any],
        *,
        payload: bytes,
        choice: str,
        reason: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        decision_number = len(state["decisions"]) + 1
        relative = Path("decisions") / f"decision.{decision_number:04d}.md"
        digest = atomic_write(self._run_dir(state["run_id"]) / relative, payload)
        record = {
            "file": str(relative).replace("\\", "/"),
            "sha256": digest,
            "choice": choice,
            "reason": reason,
            "cycle": state["cycle"],
            "after_turn": state["current_turn"],
            "title": title or f"Human: {choice}",
        }
        state["decisions"].append(record)
        self._event(
            state,
            "human.direction" if choice == "direction" else "human.decision",
            title=str(record["title"]),
            details=record,
        )
        return record

    @_locked
    def decide(self, run_id: str, choice: str, text: bytes = b"") -> dict[str, Any]:
        if choice not in {"yes", "no", "other"}:
            raise ValueError("decision choice must be yes, no, or other")
        state = self.state(run_id)
        if state["status"] != "paused":
            raise ValueError("only paused runs accept a human decision")
        decode_text_artifact(text, "decision")
        reason = str(state.get("pending_human_decision") or "human_decision")
        if reason == "operator_step":
            raise ValueError("use the advance command to start the next step")
        if reason == "validation_receipt_required" and choice == "yes":
            raise ValueError("attach the required validation receipt with the validate command")
        if choice == "other" and not text.strip():
            raise ValueError("the other decision requires explanatory text")
        if choice != "other" and text.strip():
            raise ValueError("decision text is accepted only with choice=other")
        accepted_proposal: dict[str, Any] | None = None
        accepted_proposal_bytes: bytes | None = None
        if reason == "next_task_approval" and choice == "yes":
            accepted_proposal = self._latest_turn(state, "next-task-proposal")
            if accepted_proposal is None:
                raise ValueError("accepted next task proposal is missing")
            accepted_proposal_bytes = self._artifact_bytes(
                state, f"turns/{accepted_proposal['output_file']}"
            )
        payload = text if text else (choice + "\n").encode("utf-8")
        self._record_decision(state, payload=payload, choice=choice, reason=reason)
        state["pending_human_decision"] = None
        if reason == "next_task_approval":
            if choice == "no":
                self._cycle(state)["status"] = "complete"
                self._cycle(state)["end_turn"] = state["current_turn"]
                state["status"] = "complete"
                state["current_stage"] = None
                self._save(run_id, state)
                return state
            if choice == "other":
                state["current_stage"] = self._workflow(state).next_task_revision_stage
                state["status"] = "running"
                state["next_turn_override"] = None
                self._save(run_id, state)
                return self.run_to_stop(run_id)
            proposal = accepted_proposal
            source_bytes = accepted_proposal_bytes
            assert proposal is not None and source_bytes is not None
            current_cycle = self._cycle(state)
            current_cycle["status"] = "complete"
            current_cycle["end_turn"] = state["current_turn"]
            next_number = state["cycle"] + 1
            request_path = self._run_dir(run_id) / "cycles" / f"cycle.{next_number:04d}" / "request.md"
            request_hash = atomic_write(request_path, source_bytes)
            state["cycle"] = next_number
            state["cycles"].append(self._new_cycle(next_number, request_path.relative_to(self._run_dir(run_id)), request_hash))
            request_relative = str(request_path.relative_to(self._run_dir(run_id))).replace("\\", "/")
            state["artifacts"][request_relative] = {
                "sha256": request_hash,
                "type": "request",
                "cycle": next_number,
                "source_turn": proposal["id"],
            }
            state["current_stage"] = self._workflow(state).start_stage
            state["status"] = "running"
            state["next_turn_override"] = None
            state["current_implementation_evidence"] = None
            state["completion_receipt"] = None
            self._event(state, "cycle.started", title=f"Cycle {next_number}", details={
                "request": request_relative,
                "source_proposal_turn": proposal["id"],
                "working_revision": state["working_revision"],
            })
            self._pause_for_step(state)
            self._save(run_id, state)
            return self.run_to_stop(run_id)

        if reason == "validation_execution_approval":
            if choice == "no":
                self._cancel(state, reason)
                self._save(run_id, state)
                return state
            if choice == "other":
                repair_stage = str((state.get("pending_validation") or {}).get("repair_stage") or "")
                if not repair_stage or repair_stage not in self._workflow(state).stages:
                    raise ValueError("pending validation has no configured repair stage")
                state["pending_validation"] = None
                state["current_stage"] = repair_stage
                state["status"] = "running"
                self._save(run_id, state)
                return self.run_to_stop(run_id)
            try:
                state["status"] = "running"
                self._execute_pending_validation(state)
                self._pause_for_step(state)
            except ValueError as error:
                state["status"] = "paused"
                if state.get("validation_inflight"):
                    state["pending_human_decision"] = "unknown_validation_execution"
                else:
                    state["pending_human_decision"] = "implementation_boundary_failed"
                    state["pending_repair_stage"] = str(
                        (state.get("pending_validation") or {}).get("repair_stage") or ""
                    ) or None
                state["errors"].append(str(error))
            self._save(run_id, state)
            return self.run_to_stop(run_id) if state["status"] == "running" else state

        if reason == "validation_receipt_required":
            if choice == "no":
                self._cancel(state, reason)
            else:
                state["status"] = "paused"
                state["pending_human_decision"] = reason
            self._save(run_id, state)
            return state

        if reason == "unknown_provider_invocation":
            uncertain = dict(state.get("inflight") or {})
            uncertain.update({
                "abandoned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "decision": choice,
            })
            state.setdefault("abandoned_invocations", []).append(uncertain)
            self._event(
                state,
                "provider.invocation.abandoned",
                title="Uncertain provider invocation abandoned",
                details=uncertain,
            )
            state["inflight"] = None
            if choice == "no":
                self._cancel(state, reason)
                self._save(run_id, state)
                return state
            try:
                identity_error = self._worktree_identity_error(state)
                if identity_error:
                    raise ValueError(identity_error)
                assert_allowed_changes(
                    self._project(state),
                    collect_worktree_evidence(Path(state["execution_worktree"])).changed_paths,
                )
            except ValueError as error:
                state["status"] = "paused"
                state["pending_human_decision"] = "execution_worktree_identity_changed"
                state["errors"].append(str(error))
                self._save(run_id, state)
                return state
            self._force_new_session(state)
            self._save(run_id, state)
            return self.run_to_stop(run_id)

        if reason == "unknown_validation_execution":
            uncertain = dict(state.get("validation_inflight") or {})
            if not uncertain:
                raise ValueError("the uncertain validation journal is missing")
            uncertain.update({
                "abandoned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "decision": choice,
            })
            state.setdefault("abandoned_validations", []).append(uncertain)
            self._event(
                state,
                "validation.execution.abandoned",
                title="Uncertain validation execution abandoned",
                details=uncertain,
            )
            state["validation_inflight"] = None
            state["pending_validation"] = None
            if choice == "no":
                self._cancel(state, reason)
                self._save(run_id, state)
                return state
            repair_stage = str(uncertain.get("repair_stage") or "")
            if repair_stage not in self._workflow(state).stages:
                state["status"] = "paused"
                state["pending_human_decision"] = "implementation_boundary_failed"
                state["errors"].append("uncertain validation has no configured repair stage")
                self._save(run_id, state)
                return state
            try:
                identity_error = self._worktree_identity_error(state)
                if identity_error:
                    raise ValueError(identity_error)
                assert_allowed_changes(
                    self._project(state),
                    collect_worktree_evidence(Path(state["execution_worktree"])).changed_paths,
                )
            except ValueError as error:
                state["status"] = "paused"
                state["pending_human_decision"] = "implementation_boundary_failed"
                state["pending_repair_stage"] = repair_stage
                state["errors"].append(str(error))
                self._save(run_id, state)
                return state
            state["current_stage"] = repair_stage
            state["status"] = "running"
            self._save(run_id, state)
            return self.run_to_stop(run_id)

        if reason == "validation_baseline_failure_decision":
            pending = state.get("pending_baseline_acceptance")
            if not isinstance(pending, dict):
                raise ValueError("the baseline acceptance context is missing")
            state["pending_baseline_acceptance"] = None
            workflow = self._workflow(state)
            review_stage = workflow.stages[str(pending["stage"])]
            if choice == "no":
                # Deliberate stop without commit — not a cancellation label.
                cycle = self._cycle(state)
                cycle["status"] = "stopped"
                cycle["end_turn"] = state["current_turn"]
                state["status"] = "stopped"
                state["current_stage"] = None
                state["inflight"] = None
                self._event(state, "run.stopped", title="Stopped without commit", details={"reason": reason})
                self._save(run_id, state)
                return state
            if choice == "other":
                repair_stage = self._repair_stage(review_stage)
                if not repair_stage:
                    raise ValueError("this stage has no configured repair stage")
                state["current_stage"] = repair_stage
                state["status"] = "running"
                self._save(run_id, state)
                return self.run_to_stop(run_id)
            classification = pending.get("classification") or {}
            evidence = state.get("current_implementation_evidence") or {}
            debt = {
                "schema_version": "toledo_orchestrator.baseline_debt.v1",
                "cycle": state["cycle"],
                "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "base_revision": classification.get("baseline_revision"),
                "patch_sha256": evidence.get("patch", {}).get("sha256"),
                "classification": classification,
                "note": "Accepted with a pre-existing validation failure recorded as baseline debt.",
            }
            relative = Path("artifacts") / f"cycle.{state['cycle']:04d}.baseline-debt.json"
            digest = write_json(self._run_dir(run_id) / relative, debt)
            normalized = str(relative).replace("\\", "/")
            state["artifacts"][normalized] = {"sha256": digest, "type": "baseline-debt", "cycle": state["cycle"]}
            self._event(state, "validation.baseline.debt_accepted", title="Baseline debt accepted", details={
                "file": normalized, "sha256": digest,
                "baseline_revision": classification.get("baseline_revision"),
            })
            state["status"] = "running"
            self._accept_implementation(state, review_stage, str(pending["next_stage"]), evidence)
            self._save(run_id, state)
            return self.run_to_stop(run_id) if state["status"] == "running" else state

        if choice == "no":
            self._cancel(state, reason)
            self._save(run_id, state)
            return state

        pending_round = state.get("pending_round_extension")
        if not isinstance(pending_round, dict):
            current_stage = self._workflow(state).stages.get(str(state.get("current_stage")))
            if current_stage and current_stage.round_counter and reason in {
                current_stage.round_pause_reason,
                "validation_failed_at_repair_cap",
            }:
                inferred_target = current_stage.transitions.get(current_stage.round_directive)
                if inferred_target and inferred_target.startswith("@"):
                    inferred_target = None
                if inferred_target:
                    pending_round = {
                        "counter": current_stage.round_counter,
                        "stage": current_stage.id,
                        "target": inferred_target,
                        "reason": reason,
                    }
                    state["pending_round_extension"] = pending_round
        if isinstance(pending_round, dict) and pending_round.get("reason") == reason:
            counter = str(pending_round["counter"])
            cycle = self._cycle(state)
            record = self._round_record(cycle, counter)
            record["extension"] = int(record["extension"]) + 1
            self._sync_round_alias(cycle, counter, record)
            target = str(pending_round["target"])
            if target not in self._workflow(state).stages:
                raise ValueError(f"round extension target is not a configured stage: {target}")
            state["pending_round_extension"] = None
            state["current_stage"] = target
            state["status"] = "running"
        elif reason in {
            "provider_invocation_failed",
            "provider_session_id_missing",
            "provider_session_missing",
            "provider_session_not_new",
            "provider_session_changed_unexpectedly",
            "missing_substantive_output",
            "malformed_directive",
            "unsupported_stage_directive",
        }:
            self._force_new_session(state)
        elif reason in {"invalid_next_turn_profile", "profile_permission_exceeds_stage"}:
            state["next_turn_override"] = None
            state["status"] = "running"
        elif reason in {
            "implementation_boundary_failed",
            "implementation_boundary_failed_before_commit",
            "implementation_changed_after_review",
        }:
            try:
                identity_error = self._worktree_identity_error(state)
                if identity_error:
                    raise ValueError(identity_error)
                evidence = collect_worktree_evidence(Path(state["execution_worktree"]))
                assert_allowed_changes(self._project(state), evidence.changed_paths)
            except ValueError as error:
                state["status"] = "paused"
                state["pending_human_decision"] = reason
                state["errors"].append(str(error))
                self._save(run_id, state)
                return state
            repair_stage = str(state.get("pending_repair_stage") or "")
            if not repair_stage:
                current_stage = self._workflow(state).stages.get(str(state.get("current_stage")))
                repair_stage = self._repair_stage(current_stage) if current_stage else None
            if not repair_stage or repair_stage not in self._workflow(state).stages:
                state["status"] = "paused"
                state["pending_human_decision"] = reason
                state["errors"].append("no configured repair stage for this failure")
                self._save(run_id, state)
                return state
            state["pending_validation"] = None
            state["pending_repair_stage"] = None
            state["current_stage"] = repair_stage
            state["status"] = "running"
        elif reason in {
            "execution_worktree_missing",
            "execution_worktree_identity_changed",
            "provider_changed_worktree_identity",
        }:
            if not Path(state["execution_worktree"]).is_dir():
                state["status"] = "paused"
                state["pending_human_decision"] = reason
                self._save(run_id, state)
                return state
            identity_error = self._worktree_identity_error(state)
            if identity_error:
                state["status"] = "paused"
                state["pending_human_decision"] = reason
                state["errors"].append(identity_error)
                self._save(run_id, state)
                return state
            self._force_new_session(state)
        else:
            # Provider-requested human input and operator guidance resume the same stage.
            state["status"] = "running"
        self._save(run_id, state)
        return self.run_to_stop(run_id)

    @_locked
    def continue_step(self, run_id: str, direction: bytes = b"") -> dict[str, Any]:
        state = self.state(run_id)
        if state.get("status") != "paused" or state.get("pending_human_decision") != "operator_step":
            raise ValueError("the run is not waiting at an operator step")
        decode_text_artifact(direction, "operator direction")
        if direction.strip():
            self._record_decision(
                state,
                payload=direction,
                choice="direction",
                reason="operator_step",
                title="Owner direction",
            )
        state["pending_human_decision"] = None
        state["status"] = "running"
        self._event(
            state,
            "operator.step.continued",
            title="Next turn started",
            details={"stage": state.get("current_stage"), "direction_supplied": bool(direction.strip())},
        )
        self._save(run_id, state)
        return self.run_to_stop(run_id)

    @_locked
    def record_background_failure(self, run_id: str, error: Exception) -> dict[str, Any]:
        state = self.state(run_id)
        detail = f"{type(error).__name__}: {error}"
        state["errors"].append(f"background_operation_failed:{detail}")
        if state.get("status") not in {"complete", "cancelled", "stopped", "failed"}:
            state["status"] = "paused"
            if state.get("validation_inflight"):
                state["pending_human_decision"] = "unknown_validation_execution"
            elif state.get("inflight"):
                state["pending_human_decision"] = "unknown_provider_invocation"
            elif not state.get("pending_human_decision"):
                state["pending_human_decision"] = "background_operation_failed"
        self._event(
            state,
            "background.operation.failed",
            title="Background operation failed",
            details={"error": detail, "inflight_preserved": bool(state.get("inflight"))},
        )
        self._save(run_id, state)
        return state

    def show_turn(self, run_id: str, number: int) -> str:
        if number < 1:
            raise ValueError("turn number must be positive")
        return (self._run_dir(run_id) / "turns" / f"turn.{number:04d}.output.md").read_text(encoding="utf-8")

    def artifact(self, run_id: str, relative: str) -> bytes:
        return self._artifact_bytes(self.state(run_id), relative)

    @staticmethod
    def _export_heading(lines: list[str], title: str, level: int, plain_text: bool) -> None:
        if plain_text:
            lines.extend([title, ("=" if level <= 2 else "-") * max(12, min(len(title), 88))])
        else:
            lines.append(f"{'#' * level} {title}")

    def _export_artifact_text(self, state: dict[str, Any], relative: str) -> str:
        """Read registered evidence without the prompt transport size limit."""
        try:
            data = self._artifact_bytes(state, relative)
        except ValueError as error:
            # Early v2/manual fixtures did not register every derivative file.
            # Preserve export compatibility, but retain the same containment
            # boundary and never silently bypass a recorded hash mismatch.
            if "not registered in the run manifest" not in str(error):
                return f"[Evidence unavailable: {type(error).__name__}: {error}]"
            run_dir = self._run_dir(state["run_id"]).resolve()
            path = (run_dir / str(relative).replace("\\", "/")).resolve()
            if path != run_dir and run_dir not in path.parents:
                return "[Evidence unavailable: artifact path escapes run directory]"
            try:
                data = path.read_bytes()
            except OSError as read_error:
                return f"[Evidence unavailable: {type(read_error).__name__}: {read_error}]"
        except OSError as error:
            return f"[Evidence unavailable: {type(error).__name__}: {error}]"
        try:
            return decode_text_artifact(data, relative)
        except ValueError:
            return data.decode("utf-8", errors="replace")

    def _export_redacted_value(self, state: dict[str, Any], value: Any) -> Any:
        """Redact state-only secrets and host paths; conversation bytes stay exact."""
        secret_fragments = ("secret", "token", "password", "credential", "authorization", "api_key", "apikey")
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, child in value.items():
                lowered = str(key).lower()
                if lowered == "absolute_path":
                    continue
                if any(fragment in lowered for fragment in secret_fragments):
                    redacted[str(key)] = "[redacted]"
                else:
                    redacted[str(key)] = self._export_redacted_value(state, child)
            return redacted
        if isinstance(value, list):
            return [self._export_redacted_value(state, child) for child in value]
        if isinstance(value, str):
            rendered = value
            roots = (
                (str(state.get("execution_worktree") or ""), "<worktree>"),
                (str(state.get("project_root") or ""), "<project>"),
                (str(self.runtime_dir), "<runtime>"),
                (str(Path.home()), "<home>"),
            )
            for root, replacement in roots:
                if root:
                    rendered = rendered.replace(root, replacement)
            return rendered
        return value

    def _export_turn_response(self, state: dict[str, Any], turn: dict[str, Any]) -> tuple[str, str]:
        response_file = turn.get("response_file")
        if response_file:
            return self._export_artifact_text(state, f"turns/{response_file}"), "exact stored provider response"

        # Compatibility for runs sealed before response.md became a first-class
        # artifact. Reparse the hash-bound provider envelope; never substitute a
        # caption or preview for the conversation.
        raw_file = turn.get("raw_file")
        if raw_file:
            try:
                raw = self._artifact_bytes(state, f"turns/{raw_file}")
                stderr = (
                    self._artifact_bytes(state, f"turns/{turn['stderr_file']}")
                    if turn.get("stderr_file")
                    else b""
                )
                provider = str(turn.get("provider") or "")
                if provider == "codex":
                    parsed = parse_codex_result(str(turn.get("stage") or "export"), raw, stderr)
                elif provider == "claude":
                    parsed = parse_claude_result(str(turn.get("stage") or "export"), raw, stderr)
                else:
                    parsed = ProviderResult(provider, "export", raw, stderr)
                response = result_text(parsed)
                if response:
                    return response, "exact response reconstructed from the sealed provider envelope"
                # Fixture/custom adapters may seal their response directly in
                # stdout rather than a provider JSON envelope.
                decoded = raw.decode("utf-8", errors="replace")
                if decoded.strip() and not decoded.lstrip().startswith(("{", "[")):
                    return decoded, "exact response reconstructed from sealed stdout"
            except (OSError, ValueError, KeyError):
                pass
        output_file = turn.get("output_file")
        if output_file:
            return (
                work_product_text(self._export_artifact_text(state, f"turns/{output_file}")),
                "legacy derivative work product; exact provider response was not sealed",
            )
        return "[No provider response artifact was recorded.]", "response unavailable"

    def _export_validation_set(
        self,
        lines: list[str],
        state: dict[str, Any],
        title: str,
        values: dict[str, Any],
        plain_text: bool,
        include_diagnostics: bool,
    ) -> None:
        if not values:
            return
        self._export_heading(lines, title, 2, plain_text)
        lines.append("")
        for validation_id, raw_record in values.items():
            record = raw_record if isinstance(raw_record, dict) else {"value": raw_record}
            self._export_heading(lines, f"Validation — {validation_id}", 3, plain_text)
            metadata = {key: value for key, value in record.items() if key not in {"stdout", "stderr"}}
            lines.extend([
                json.dumps(self._export_redacted_value(state, metadata), ensure_ascii=False, indent=2, sort_keys=True),
                "",
            ])
            if not include_diagnostics:
                continue
            for stream in ("stdout", "stderr"):
                stream_record = record.get(stream)
                if not isinstance(stream_record, dict) or not stream_record.get("path"):
                    continue
                text = self._export_artifact_text(state, str(stream_record["path"]))
                self._export_heading(lines, f"{stream} (exact sealed text)", 4, plain_text)
                lines.extend([text, ""])

    def export_run(
        self,
        run_id: str,
        *,
        plain_text: bool = False,
        include_prompts: bool = True,
        include_diagnostics: bool = False,
    ) -> bytes:
        """Export the complete readable transcript from hash-bound evidence.

        Raw provider envelopes stay excluded, but the exact readable provider
        response extracted from each envelope is included. ``prompts=0`` omits
        only transport prompts; requests, provider responses, directions, and
        decisions remain present. Raw operational diagnostics require an
        explicit opt-in because arbitrary stream text cannot be made safe by
        key-name redaction.
        """
        state = self.state(run_id)
        lines: list[str] = []
        self._export_heading(lines, f"Toledo complete transcript — {state['run_id']}", 1, plain_text)
        lines.extend([
            "",
            f"Project: {state.get('project', '')}",
            f"Workflow: {state.get('workflow', '')}",
            f"Source revision: {state.get('source_revision') or ''}",
            f"Working revision: {state.get('working_revision') or ''}",
            f"Status: {state.get('status', '')}",
            f"Transport prompts: {'included' if include_prompts else 'intentionally omitted by prompts=0'}",
            f"Diagnostics: {'included by explicit request; may contain sensitive raw text' if include_diagnostics else 'excluded (request diagnostics=1 explicitly to include raw operational evidence)'}",
            "",
            "Local conversation export. Exact readable prompts/responses and operator input are included. Raw provider envelopes are excluded. Diagnostic streams and event payloads are excluded unless explicitly requested because arbitrary stream text may contain secrets.",
            "",
        ])

        turns = list(state.get("turns", []))
        decisions = list(state.get("decisions", []))
        cycles = list(state.get("cycles", [])) or [{"number": 1, "request_file": None}]
        for cycle in cycles:
            number = int(cycle.get("number") or 1)
            self._export_heading(lines, f"Cycle {number}", 2, plain_text)
            lines.append("")
            request_file = cycle.get("request_file")
            self._export_heading(lines, "Cycle request (exact)", 3, plain_text)
            lines.extend([
                self._export_artifact_text(state, str(request_file)) if request_file else "[Cycle request was not recorded.]",
                "",
            ])

            items: list[tuple[int, int, int, str, dict[str, Any]]] = []
            for index, turn in enumerate(turns):
                if int(turn.get("cycle") or 1) == number:
                    turn_number = int(str(turn.get("id") or "0").split(".")[-1])
                    items.append((turn_number, 0, index, "turn", turn))
            for index, decision in enumerate(decisions):
                if int(decision.get("cycle") or 1) == number:
                    items.append((int(decision.get("after_turn") or 0), 1, index, "decision", decision))

            for _, _, index, kind, item in sorted(items):
                if kind == "decision":
                    title = str(item.get("title") or f"Human: {item.get('choice', 'decision')}")
                    self._export_heading(lines, f"{title} — decision {index + 1}", 3, plain_text)
                    lines.extend([
                        f"Choice: {item.get('choice', '')} | Gate: {item.get('reason', '')} | After turn: {item.get('after_turn', 0)}",
                        "",
                        self._export_artifact_text(state, str(item.get("file"))) if item.get("file") else "[Decision body was not recorded.]",
                        "",
                    ])
                    continue

                turn = item
                self._export_heading(
                    lines,
                    f"{turn.get('id', 'turn')} — {turn.get('title') or turn.get('stage') or turn.get('route', '')}",
                    3,
                    plain_text,
                )
                observed_model = turn.get("observed_model") or turn.get("configured_model") or ""
                observed_effort = turn.get("observed_reasoning") or turn.get("configured_reasoning") or ""
                lines.extend([
                    f"Provider: {turn.get('provider', '')} | model: {observed_model} | effort: {observed_effort} | session: {turn.get('session_action', '')} {turn.get('session_label', '')}",
                    f"Exit: {turn.get('exit_code', '')} | provider error: {turn.get('provider_error') or 'none'} | elapsed: {turn.get('elapsed_ms', 0)} ms",
                ])
                usage = turn.get("usage") or {}
                if isinstance(usage, dict) and usage.get("total_cost_usd") is not None:
                    lines.append(f"Observed cost: ${usage['total_cost_usd']}")
                lines.append("")
                for label, key in (
                    ("One-turn route direction (exact)", "direction_file"),
                    ("Operator steer note (exact)", "steer_note_file"),
                    ("One-turn stance override (exact)", "stance_override_file"),
                ):
                    if turn.get(key):
                        self._export_heading(lines, label, 4, plain_text)
                        lines.extend([self._export_artifact_text(state, f"turns/{turn[key]}"), ""])
                if include_prompts and turn.get("prompt_file"):
                    self._export_heading(lines, "Transport prompt (exact)", 4, plain_text)
                    lines.extend([self._export_artifact_text(state, f"turns/{turn['prompt_file']}"), ""])
                response, provenance = self._export_turn_response(state, turn)
                self._export_heading(lines, f"Provider response ({provenance})", 4, plain_text)
                lines.extend([response, ""])
                if include_diagnostics and turn.get("stderr_file"):
                    stderr = self._export_artifact_text(state, f"turns/{turn['stderr_file']}")
                    if stderr:
                        self._export_heading(lines, "Provider stderr (exact sealed text)", 4, plain_text)
                        lines.extend([stderr, ""])

        if include_diagnostics:
            baseline = state.get("baseline_validations") or {}
            baseline_values = baseline.get("results", {}) if isinstance(baseline, dict) else {}
            self._export_validation_set(lines, state, "Clean-base validation", baseline_values, plain_text, True)
            self._export_validation_set(lines, state, "Implementation validation", state.get("validations") or {}, plain_text, True)

        closure_artifacts = [
            (path, record)
            for path, record in state.get("artifacts", {}).items()
            if isinstance(record, dict) and record.get("type") in {"completion-receipt", "baseline-debt"}
        ]
        if include_diagnostics and closure_artifacts:
            self._export_heading(lines, "Closure evidence", 2, plain_text)
            lines.append("")
            for path, record in sorted(closure_artifacts, key=lambda value: (int(value[1].get("cycle") or 0), value[0])):
                self._export_heading(lines, f"{record.get('type')} — {path}", 3, plain_text)
                raw = self._export_artifact_text(state, path)
                try:
                    rendered: Any = json.loads(raw)
                except json.JSONDecodeError:
                    rendered = raw
                if not isinstance(rendered, str):
                    rendered = json.dumps(self._export_redacted_value(state, rendered), ensure_ascii=False, indent=2, sort_keys=True)
                lines.extend([rendered, ""])

        if include_diagnostics and state.get("events"):
            self._export_heading(lines, "Lifecycle event log (chronological)", 2, plain_text)
            lines.append("")
            for event in state["events"]:
                event_id = str(event.get("id") or "")
                raw = self._export_artifact_text(state, f"events/{event_id}.json")
                try:
                    value: Any = json.loads(raw)
                except json.JSONDecodeError:
                    value = {"id": event_id, "kind": event.get("kind"), "title": event.get("title"), "evidence": raw}
                lines.append(json.dumps(self._export_redacted_value(state, value), ensure_ascii=False, sort_keys=True))
            lines.append("")

        self._export_heading(lines, "Final run state (redacted summary)", 2, plain_text)
        final_summary = {
            "status": state.get("status"),
            "cycle": state.get("cycle"),
            "current_turn": state.get("current_turn"),
            "current_stage": state.get("current_stage"),
            "pending_human_decision": state.get("pending_human_decision"),
            "degraded": bool(state.get("degraded")),
            "error_count": len(state.get("errors") or []),
            "cycles": [
                {
                    key: cycle.get(key)
                    for key in ("id", "number", "status", "start_turn", "end_turn", "completion_receipt")
                }
                for cycle in cycles
            ],
        }
        lines.extend([
            "",
            json.dumps(self._export_redacted_value(state, final_summary), ensure_ascii=False, indent=2, sort_keys=True),
            "",
        ])
        return "\n".join(lines).encode("utf-8")

    @_locked
    def attach_receipt(self, run_id: str, receipt_path: Path) -> dict[str, Any]:
        state = self.state(run_id)
        if state.get("status") != "paused" or state.get("pending_human_decision") != "validation_receipt_required":
            raise ValueError("validation receipts are accepted only while the run is waiting for a remote receipt")
        receipt = read_json(receipt_path)
        required = {
            "validation_id", "command", "source_revision", "patch_sha256", "environment", "host",
            "started_at", "finished_at", "exit_code", "stdout_path", "stderr_path",
            "stdout_sha256", "stderr_sha256",
        }
        missing = required - receipt.keys()
        if missing:
            raise ValueError(f"receipt missing fields: {','.join(sorted(missing))}")
        try:
            exit_code = int(receipt["exit_code"])
        except (TypeError, ValueError) as error:
            raise ValueError("receipt exit_code must be an integer") from error
        timestamps: dict[str, datetime] = {}
        for field in ("started_at", "finished_at"):
            try:
                timestamps[field] = datetime.fromisoformat(str(receipt[field]).replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError(f"receipt {field} must be an ISO-8601 timestamp") from error
            if timestamps[field].tzinfo is None:
                raise ValueError(f"receipt {field} must include a timezone")
        if timestamps["finished_at"] < timestamps["started_at"]:
            raise ValueError("receipt finished_at precedes started_at")
        if not str(receipt["host"]).strip():
            raise ValueError("receipt host cannot be empty")
        evidence = state.get("current_implementation_evidence") or {}
        validation_id = str(receipt["validation_id"])
        expected = evidence.get("validations", {}).get(validation_id)
        if not isinstance(expected, dict):
            raise ValueError(f"unknown validation id: {validation_id}")
        if expected.get("state") != "pending_remote":
            raise ValueError(f"validation receipt is not pending: {validation_id}")
        if receipt["source_revision"] != state["working_revision"]:
            raise ValueError("receipt source revision mismatch")
        if receipt["patch_sha256"] != evidence.get("patch", {}).get("sha256"):
            raise ValueError("receipt implementation patch mismatch")
        if receipt["command"] != expected["command"]:
            raise ValueError("receipt command mismatch")
        if receipt["environment"] != expected["environment"]:
            raise ValueError("receipt environment mismatch")
        run_dir = self._run_dir(run_id)
        copied: dict[str, dict[str, str]] = {}
        for key in ("stdout", "stderr"):
            data = Path(receipt[f"{key}_path"]).read_bytes()
            if sha256(data) != receipt[f"{key}_sha256"]:
                raise ValueError(f"receipt hash mismatch: {key}")
            relative = Path("validations") / (
                f"cycle.{state['cycle']:04d}.turn.{state['current_turn']:04d}.{validation_id}.receipt.{key}.raw"
            )
            digest = atomic_write(run_dir / relative, data)
            copied[key] = {"path": str(relative).replace("\\", "/"), "sha256": digest}
        receipt_relative = Path("validations") / (
            f"cycle.{state['cycle']:04d}.turn.{state['current_turn']:04d}.{validation_id}.receipt.json"
        )
        receipt_hash = atomic_write(run_dir / receipt_relative, receipt_path.read_bytes())
        updated = {
            **expected,
            "state": "passed" if exit_code == 0 else "failed",
            "exit_code": exit_code,
            "receipt": {"path": str(receipt_relative).replace("\\", "/"), "sha256": receipt_hash},
            **copied,
        }
        evidence["validations"][validation_id] = updated
        state["validations"][validation_id] = updated
        self._event(state, "validation.receipt.attached", title=f"Validation receipt: {validation_id}", details={
            "validation_id": validation_id,
            "state": updated["state"],
            "receipt": updated["receipt"],
        })
        if state.get("pending_completion") and not pending_required_validations(evidence["validations"]):
            pending = state["pending_completion"]
            state["pending_completion"] = None
            state["pending_human_decision"] = None
            state["status"] = "running"
            self._transition(state, self._workflow(state).stages[pending["stage"]], Directive(next="ready"))
            self._pause_for_step(state)
        self._save(run_id, state)
        if state["status"] == "running":
            return self.run_to_stop(run_id)
        return state

    @_locked
    def fork_rewind(self, run_id: str, *, rewind_to_turn: int, opt_in: bool = False) -> dict[str, Any]:
        """Create an independent, paused evidence copy at an earlier turn.

        This never edits the source run or removes its later artifacts. The
        fork always starts its next provider call in a new physical session.
        """
        if not opt_in:
            raise ValueError("fork/rewind is off by default; explicit opt-in is required")
        source = self.state(run_id)
        if source.get("inflight"):
            raise ValueError("cannot fork/rewind while the source run has an active provider call")
        if rewind_to_turn < 1 or rewind_to_turn >= int(source.get("current_turn", 0)):
            raise ValueError("rewind turn must name an earlier completed turn")
        retained = [turn for turn in source.get("turns", []) if int(str(turn["id"]).split(".")[-1]) <= rewind_to_turn]
        if len(retained) != rewind_to_turn:
            raise ValueError("rewind turn is not a contiguous completed turn")
        new_id = f"run_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"
        source_dir = self._run_dir(run_id)
        fork_dir = self._run_dir(new_id)
        # The source lock is process-local, not evidence; copying it would also
        # fail on Windows while this method holds the source run lock.
        shutil.copytree(source_dir, fork_dir, ignore=shutil.ignore_patterns("run.lock"))
        fork = read_json(fork_dir / "run.json")
        project = self._project(source)
        execution_worktree = self.runtime_dir / "worktrees" / new_id
        execution_branch = f"codex/orchestrator/{new_id}"
        fork.update({
            "run_id": new_id, "turns": retained, "current_turn": rewind_to_turn,
            "status": "paused", "pending_human_decision": "fork_rewind_ready", "inflight": None,
            "execution_worktree": str(execution_worktree.resolve()), "execution_branch": execution_branch,
            "next_turn_override": {"profile": source["current_stage"] and self._workflow(source).stages[source["current_stage"]].profile, "session_action": "new", "target_stage": source["current_stage"], "fork_rewind": True},
            "forked_from": {"run_id": run_id, "rewind_to_turn": rewind_to_turn, "source_run_sha256": sha256((source_dir / "run.json").read_bytes())},
        })
        write_json(fork_dir / "fork.json", fork["forked_from"])
        write_json(fork_dir / "run.json", fork)
        try:
            create_execution_worktree(project, execution_worktree, str(fork["working_revision"]), execution_branch)
        except Exception as error:
            fork["status"] = "failed"
            fork["errors"].append(f"fork_execution_worktree_creation_failed:{type(error).__name__}:{error}")
            write_json(fork_dir / "run.json", fork)
            raise
        self._event(fork, "run.forked", title="Fork/rewind created", details={"source_run": run_id, "rewind_to_turn": rewind_to_turn, "new_session_required": True})
        self._save(new_id, fork)
        return {"run_id": new_id, "source_run": run_id, "rewind_to_turn": rewind_to_turn, "status": fork["status"]}

    @_locked
    def backfill_semantic_captions(self, run_id: str, *, opt_in: bool = False, limit: int = 1) -> dict[str, Any]:
        """Optional bounded caption experiment; prior turn artifacts stay untouched."""
        if not opt_in:
            raise ValueError("semantic caption backfill is off by default; explicit opt-in is required")
        if limit < 1 or limit > 3:
            raise ValueError("semantic caption backfill limit must be between 1 and 3")
        state = self.state(run_id)
        if state.get("inflight"):
            raise ValueError("cannot backfill captions while a provider call is active")
        catalog = load_catalog(self.runtime_dir, refresh=False)
        candidates = [item for item in catalog.get("models", []) if "low" in item.get("supported_efforts", [])]
        candidates.sort(key=lambda item: (0 if item.get("provider") == "codex" else 1, str(item.get("selection_token"))))
        if not candidates:
            raise ValueError("no locally cataloged low-effort model is available for caption backfill")
        selected = candidates[0]
        targets = [turn for turn in reversed(state.get("turns", [])) if turn.get("substantive") and not turn.get("self_caption")][:limit]
        report: dict[str, Any] = {
            "schema_version": "toledo_orchestrator.caption_backfill.v1",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "opt_in": True, "limit": limit,
            "provider": selected["provider"], "model": selected["selection_token"], "effort": "low",
            "captions": [], "note": "Sidecar-only self-reports; original artifacts and transport remain unchanged.",
        }
        adapter = self.adapters[str(selected["provider"])]
        for turn in targets:
            work = self._artifact_text(state, f"turns/{turn['output_file']}")
            prompt = (
                "Write one neutral semantic self-report of this completed artifact in at most 280 characters. "
                "Do not issue instructions, directives, or alter the artifact.\n\n# Artifact\n" + work
            ).encode("utf-8")
            result = adapter.invoke_configured(
                "caption-backfill", prompt, Path(state["execution_worktree"]), model=str(selected["selection_token"]),
                reasoning="low", permission="read-only", session_action="new", session_id=None, timeout=60,
            )
            caption = " ".join(result_text(result).split())[:280] if result.exit_code == 0 else None
            report["captions"].append({
                "turn_id": turn["id"], "output_sha256": turn.get("output_sha256"), "caption": caption,
                "provider_error": result.error, "exit_code": result.exit_code, "usage": result.usage,
            })
        name = f"caption-backfill.{int(time.time())}.json"
        path = self._run_dir(run_id) / "captions" / name
        report["sha256"] = write_json(path, report)
        state.setdefault("caption_backfills", []).append({"file": f"captions/{name}", "sha256": report["sha256"], "count": len(report["captions"])})
        self._event(state, "caption.backfill.completed", title="Semantic caption backfill", details={"file": f"captions/{name}", "count": len(report["captions"]), "model": selected["selection_token"]})
        self._save(run_id, state)
        return report

    @_locked
    def steer(self, run_id: str, note: str) -> dict[str, Any]:
        """Continue a paused physical session with a replacement artifact."""
        state = self.state(run_id)
        note = note.strip()
        if not note:
            raise ValueError("a Steer note is required")
        availability = self._steer_availability(state)
        if not availability.get("available"):
            raise ValueError(f"Steer is unavailable: {availability.get('reason') or 'no active current-stage artifact'}")
        stage = self._workflow(state).stages[str(availability["stage"])]
        latest = next(
            (turn for turn in state.get("turns", []) if turn.get("id") == availability.get("turn_id")),
            None,
        )
        if latest is None:
            raise ValueError("Steer is unavailable: the selected artifact is no longer present")
        base_profile = self._profile(state, stage.profile)
        profile = ProfileDefinition(
            id=str(latest.get("profile") or base_profile.id),
            label=str(latest.get("profile_label") or base_profile.label),
            provider=str(latest.get("provider") or base_profile.provider),
            model=str(latest.get("configured_model") or base_profile.model),
            effort=str(latest.get("configured_reasoning") or base_profile.effort),
            permission=str(latest.get("permission") or base_profile.permission),
            color=base_profile.color,
            timeout_seconds=base_profile.timeout_seconds,
            custom=bool(base_profile.custom or latest.get("configured_model") != base_profile.model),
        )
        slot, _, session_id = self._session(state, stage, profile, "continue")
        if not session_id:
            raise ValueError("Steer requires an active provider session")
        latest_text = self._artifact_text(state, f"turns/{latest['output_file']}")
        prompt = (
            "Continue the existing physical provider session. Produce a complete replacement artifact, "
            "not a patch or a summary. Preserve the workflow directive fence at the end.\n\n"
            f"# Operator Steer\n{note}\n\n# Artifact to replace\n{latest_text}\n"
        ).encode("utf-8")
        state["status"] = "running"
        state["inflight"] = {"stage": stage.id, "session_slot": stage.session_slot, "session_action": "continue", "session_id": session_id, "steer": True}
        self._event(state, "provider.steer.started", title="STEER", details={"stage": stage.id, "session_id": session_id, "note": note})
        self._save(run_id, state)
        result = self.adapters[profile.provider].invoke_configured(
            stage.id, prompt, Path(state["execution_worktree"]), model=profile.model, reasoning=profile.effort,
            permission=profile.permission, session_action="continue", session_id=session_id, timeout=profile.timeout_seconds,
        )
        state = self.state(run_id)
        state["inflight"] = None
        post_provider_identity_error = self._worktree_identity_error(state)
        directive = extract_directive(result_text(result)) if result.exit_code == 0 else None
        replacement = self._store_turn(state, stage, profile, slot, "continue", result, prompt, directive, steer_of=str(latest["id"]), steer_note=note)
        if post_provider_identity_error:
            state["status"] = "paused"
            state["pending_human_decision"] = "provider_changed_worktree_identity"
            state["errors"].append(post_provider_identity_error)
        elif (
            result.exit_code != 0
            or result.session_id != session_id
            or not work_product_text(result_text(result))
            or directive is None
            or directive.conflict
            or directive.valid_block_count != 1
        ):
            state["status"] = "paused"
            state["pending_human_decision"] = "provider_invocation_failed"
            state["errors"].append("steer replacement was not trustworthy")
        else:
            state["status"] = "paused"
            state["pending_human_decision"] = "operator_step"
            self._event(state, "provider.steer.completed", title="STEER", details={"replacement_turn": replacement["id"], "replaces": latest["id"], "note_file": replacement["steer_note_file"]})
        self._save(run_id, state)
        return state

    @_locked
    def set_next_turn_override(
        self,
        run_id: str,
        *,
        profile: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        session_action: str | None = None,
        custom: bool = False,
        stance: str | None = None,
    ) -> dict[str, Any]:
        state = self.state(run_id)
        if state["status"] not in {"created", "paused", "running"} or state.get("inflight"):
            raise ValueError("the next turn cannot be changed while a provider call is active or the run is terminal")
        workflow = self._workflow(state)
        if not state.get("current_stage"):
            raise ValueError("the run has no next stage")
        stage = workflow.stages[state["current_stage"]]
        if stance is not None and stance not in stage.stance_overrides:
            raise ValueError("stance override is not enabled for this workflow stage")
        selected_profile = profile or stage.profile
        if selected_profile not in workflow.profiles:
            raise ValueError(f"unknown profile: {selected_profile}")
        # A run is bound to its launch-time workflow snapshot. Selecting a
        # profile must never import later provider, permission, timeout, or
        # label changes from mutable route defaults.
        target_profile = self._profile(state, selected_profile)
        expected_provider = self._profile(state, stage.profile).provider
        provider_switch = target_profile.provider != expected_provider
        if provider_switch and (not stage.provider_switchable or session_action != "new"):
            raise ValueError("a provider switch requires an opt-in provider_switchable stage and a new physical session")
        write_allowed = stage.phase == "implementation" and stage.role == "implementer"
        if target_profile.permission == "workspace-write" and not write_allowed:
            raise ValueError("workspace-write profiles are allowed only for implementation stages")
        if session_action is not None and session_action not in {"new", "continue"}:
            raise ValueError("session action must be new or continue")
        if session_action == "continue":
            slot = self._cycle(state).get("sessions", {}).get(stage.session_slot) or {}
            if not slot.get("active_session_id"):
                raise ValueError(f"session slot {stage.session_slot} has no active session to continue")
            if str(slot.get("provider") or "") != target_profile.provider:
                raise ValueError("the active session provider does not match the selected profile")
        if model is not None and not model.strip():
            raise ValueError("model override cannot be empty")
        if effort is not None and not effort.strip():
            raise ValueError("reasoning effort override cannot be empty")
        profile_value = vars(target_profile).copy()
        if model is not None:
            profile_value["model"] = model.strip()
        if effort is not None:
            profile_value["effort"] = effort.strip()
        if model is not None or effort is not None:
            profile_value["custom"] = bool(custom)
        # A populated catalog is authoritative for the ordinary picker path.
        # Discovery failure/staleness remains a warning rather than a run
        # blocker, and the deliberate custom escape hatch records its status.
        catalog = load_catalog(self.runtime_dir, refresh=False)
        if catalog.get("models") and (model is not None or effort is not None):
            validate_selection(
                catalog,
                provider=target_profile.provider,
                model=str(profile_value["model"]),
                effort=str(profile_value["effort"]),
                custom=custom,
            )
        state["next_turn_override"] = {
            "profile": selected_profile,
            "profile_value": profile_value,
            "session_action": session_action,
            "target_stage": stage.id,
            "custom": bool(profile_value.get("custom")),
            "provider_switch": provider_switch,
            "stance": stance,
        }
        self._event(state, "turn.override.set", title="Next-turn override", details=state["next_turn_override"])
        self._save(run_id, state)
        return state

    @_locked
    def cleanup_worktree(self, run_id: str, *, force: bool = False) -> dict[str, Any]:
        state = self.state(run_id)
        if state.get("inflight"):
            raise ValueError("cannot clean a worktree while a provider invocation is active")
        if state["status"] not in {"complete", "cancelled", "stopped", "failed"} and not force:
            raise ValueError("only terminal run worktrees can be cleaned without --force")
        target = Path(state["execution_worktree"])
        expected_parent = (self.runtime_dir / "worktrees").resolve()
        resolved = target.resolve()
        if expected_parent not in resolved.parents:
            raise ValueError("execution worktree is outside the orchestrator worktree root")
        if target.exists():
            remove_execution_worktree(self._project(state), target, force=force)
        state["execution_worktree_removed"] = True
        self._event(state, "worktree.removed", title="Execution worktree removed", details={"path": str(target), "force": force})
        self._save(run_id, state)
        return state
