from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

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
    has_substantive_work,
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
from .validation import pending_required_validations, required_local_validations_passed, run_project_validations
from .workflow import IDENTIFIER, ProfileDefinition, StageDefinition, WorkflowDefinition, load_workflows
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
            raise ValueError("project source checkout must be clean before a continuous run starts")
        if project_check.get("dirty") is None:
            raise ValueError("project source checkout status must be available before a continuous run starts")
        if not project_check["ready"]:
            raise ValueError(f"project is not ready: {project_check['error'] or project_check['instruction_files']}")
        if not project_definition.implementation_enabled:
            raise ValueError(f"project {project} is not enabled for isolated implementation")
        source_revision = str(project_check["source_revision"])
        run_id = f"run_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"
        run_dir = self._run_dir(run_id)
        request_path = run_dir / "cycles" / "cycle.0001" / "request.md"
        request_hash = atomic_write(request_path, request)
        execution_worktree = self.runtime_dir / "worktrees" / run_id
        execution_branch = f"codex/orchestrator/{run_id}"
        workflow_definition = self.workflows[workflow]
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
        prompt_library: dict[str, dict[str, str]] = {}
        for prompt_name in sorted(prompt_names):
            source = self._prompt_source(workflow_definition, prompt_name)
            relative = Path("prompt-library") / prompt_name
            digest = atomic_write(run_dir / relative, source.read_bytes())
            prompt_library[prompt_name] = {
                "path": str(relative).replace("\\", "/"),
                "sha256": digest,
                "source": str(source.resolve()),
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
                ("output_file", "output_sha256"),
                ("raw_file", "raw_sha256"),
                ("stderr_file", "stderr_sha256"),
                ("direction_file", "direction_sha256"),
            ):
                if turn.get(file_key) and f"turns/{turn.get(file_key)}" == normalized:
                    return str(turn.get(hash_key))
            if f"turns/{turn.get('id')}.json" == normalized:
                return str(turn.get("metadata_sha256"))
        for container in (state.get("current_implementation_evidence"), state.get("validations")):
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
            raise ValueError(f"session slot {stage.session_slot} cannot change providers")
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
    ) -> dict[str, Any]:
        number = state["current_turn"] + 1
        turn_id = f"turn.{number:04d}"
        turns = self._run_dir(state["run_id"]) / "turns"
        prompt_name = f"{turn_id}.prompt.md"
        raw_name = f"{turn_id}.output.raw"
        stderr_name = f"{turn_id}.stderr.raw"
        text_name = f"{turn_id}.output.md"
        prompt_hash = atomic_write(turns / prompt_name, prompt)
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
            "interstitial_file": interstitial_file,
            "interstitial_sha256": interstitial_sha256,
            "direction_file": direction_name,
            "direction_sha256": direction_hash,
            "output_file": text_name,
            "raw_file": raw_name,
            "stderr_file": stderr_name,
            "prompt_sha256": prompt_hash,
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
            prompt = self._prompt(state, stage, profile, action, slot["label"], direction_text)
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
                         result=result, prompt=prompt, directive=directive, direction_text=direction_text)
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
                state["current_stage"] = repair_stage
                state["status"] = "running"
                return
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
            return
        state["current_stage"] = target
        state["status"] = "running"

    @_locked
    def run_to_stop(self, run_id: str) -> dict[str, Any]:
        state = self.state(run_id)
        if state["status"] == "created":
            state["status"] = "running"
            self._save(run_id, state)
        while self.state(run_id)["status"] not in {"complete", "cancelled", "paused", "failed"}:
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
        if state.get("status") not in {"complete", "cancelled", "failed"}:
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

    def export_run(self, run_id: str, *, plain_text: bool = False) -> bytes:
        """Create a local, chronological evidence export without raw envelopes."""
        state = self.state(run_id)
        run_dir = self._run_dir(run_id)
        lines = [
            f"# Toledo run {state['run_id']}", "",
            f"- Project: {state.get('project', '')}",
            f"- Workflow: {state.get('workflow', '')}",
            f"- Revision: {state.get('working_revision') or state.get('source_revision') or ''}",
            f"- Status: {state.get('status', '')}", "",
            "This is local evidence. Raw provider stdout/stderr envelopes are excluded.", "",
        ]
        for turn in state.get("turns", []):
            lines.extend([
                f"## {turn.get('id', 'turn')} — {turn.get('title') or turn.get('stage') or turn.get('route', '')}",
                f"Provider: {turn.get('provider', '')} | model: {turn.get('observed_model') or turn.get('configured_model') or ''} | effort: {turn.get('observed_reasoning') or turn.get('configured_reasoning') or ''}",
            ])
            usage = turn.get("usage") or {}
            if isinstance(usage, dict) and usage.get("total_cost_usd") is not None:
                lines.append(f"Observed cost: ${usage['total_cost_usd']}")
            for label, key in (("Direction", "direction_file"), ("Output", "output_file")):
                name = turn.get(key)
                if not name:
                    continue
                try:
                    text = (run_dir / "turns" / str(name)).read_text(encoding="utf-8")
                except OSError:
                    continue
                lines.extend([f"### {label}", work_product_text(text).strip(), ""])
        for decision in state.get("decisions", []):
            lines.append(f"## Decision: {decision.get('choice', '')}")
            if decision.get("reason"):
                lines.append(f"Reason: {decision['reason']}")
        lines.extend(["## Validation summary", json.dumps(state.get("validations", {}), ensure_ascii=False, indent=2, sort_keys=True), ""])
        text = "\n".join(lines)
        if plain_text:
            text = text.replace("# ", "").replace("## ", "").replace("### ", "")
        return text.encode("utf-8")

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
    def set_next_turn_override(
        self,
        run_id: str,
        *,
        profile: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        session_action: str | None = None,
    ) -> dict[str, Any]:
        state = self.state(run_id)
        if state["status"] not in {"created", "paused", "running"} or state.get("inflight"):
            raise ValueError("the next turn cannot be changed while a provider call is active or the run is terminal")
        workflow = self._workflow(state)
        configured_workflow = self.workflows.get(str(state["workflow"]), workflow)
        if not state.get("current_stage"):
            raise ValueError("the run has no next stage")
        stage = workflow.stages[state["current_stage"]]
        selected_profile = profile or stage.profile
        if selected_profile not in configured_workflow.profiles:
            raise ValueError(f"unknown profile: {selected_profile}")
        target_profile = (
            configured_workflow.profiles[selected_profile]
            if profile is not None
            else self._profile(state, selected_profile)
        )
        expected_provider = self._profile(state, stage.profile).provider
        if target_profile.provider != expected_provider:
            raise ValueError("a one-turn profile override cannot change the stage provider")
        write_allowed = stage.phase == "implementation" and stage.role == "implementer"
        if target_profile.permission == "workspace-write" and not write_allowed:
            raise ValueError("workspace-write profiles are allowed only for implementation stages")
        if session_action is not None and session_action not in {"new", "continue"}:
            raise ValueError("session action must be new or continue")
        if model is not None and not model.strip():
            raise ValueError("model override cannot be empty")
        if effort is not None and not effort.strip():
            raise ValueError("reasoning effort override cannot be empty")
        profile_value = vars(target_profile).copy()
        if model is not None:
            profile_value["model"] = model.strip()
        if effort is not None:
            profile_value["effort"] = effort.strip()
        state["next_turn_override"] = {
            "profile": selected_profile,
            "profile_value": profile_value,
            "session_action": session_action,
            "target_stage": stage.id,
        }
        self._event(state, "turn.override.set", title="Next-turn override", details=state["next_turn_override"])
        self._save(run_id, state)
        return state

    @_locked
    def cleanup_worktree(self, run_id: str, *, force: bool = False) -> dict[str, Any]:
        state = self.state(run_id)
        if state.get("inflight"):
            raise ValueError("cannot clean a worktree while a provider invocation is active")
        if state["status"] not in {"complete", "cancelled", "failed"} and not force:
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
