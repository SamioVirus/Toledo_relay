from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from toledo_orchestrator.core import ClaudeAdapter, CodexAdapter, ProviderAdapter, ProviderResult, atomic_write, sha256, write_json
from toledo_orchestrator.catalog import CATALOG_SCHEMA
from toledo_orchestrator.configuration import load_configured_workflows
from toledo_orchestrator.cycle import CycleOrchestrator
from toledo_orchestrator.project import ProjectDefinition, ValidationDefinition
from toledo_orchestrator.stance import CURATED_STANCES
from toledo_orchestrator.workflow import WorkflowDefinition, load_workflows
from toledo_orchestrator.worktree import collect_worktree_evidence, seal_worktree_evidence


FIXTURES = Path(__file__).with_name("fixtures")


def response(next_value: str, work: str) -> str:
    return f"{work}\n```orchestrator\n{{\"next\":\"{next_value}\"}}\n```"


def sentinel(next_value: str, work: str) -> str:
    return f'{work}\nORCHESTRATOR_DIRECTIVE_V2: {{"next":"{next_value}"}}'


class SessionAdapter(ProviderAdapter):
    def __init__(self, provider: str, scripted: list[tuple[str, str]], writer: bool = False) -> None:
        self.provider = provider
        self.scripted = iter(scripted)
        self.writer = writer
        self.invocations: list[dict[str, Any]] = []
        self.counter = 0

    def invoke(self, route: str, prompt: bytes, working_directory: Path) -> ProviderResult:
        raise AssertionError("cycle tests must use invoke_configured")

    def invoke_configured(self, route: str, prompt: bytes, working_directory: Path, **kwargs: Any) -> ProviderResult:
        expected_route, output = next(self.scripted)
        assert route == expected_route
        action = kwargs["session_action"]
        if action == "new":
            self.counter += 1
            session_id = f"{self.provider}-session-{self.counter}"
        else:
            session_id = kwargs["session_id"]
            assert session_id
        if self.writer and route in {"implementation", "implementation-repair"}:
            target = working_directory / "built.txt"
            previous = target.read_text(encoding="utf-8") if target.exists() else ""
            target.write_text(previous + route + "\n", encoding="utf-8")
        self.invocations.append({**kwargs, "route": route, "prompt": prompt, "cwd": working_directory, "session_id": session_id})
        return ProviderResult(
            self.provider,
            route,
            output.encode("utf-8"),
            response_text=output,
            session_id=session_id,
            configured_model=kwargs["model"],
            configured_reasoning=kwargs["reasoning"],
            session_action=action,
        )

    def check(self) -> dict[str, object]:
        return {"provider": self.provider, "ready": True, "generation": "fixture"}


class ReviewMutatingAdapter(SessionAdapter):
    def invoke_configured(self, route: str, prompt: bytes, working_directory: Path, **kwargs: Any) -> ProviderResult:
        result = super().invoke_configured(route, prompt, working_directory, **kwargs)
        if route == "implementation-review":
            (working_directory / "post-review.txt").write_text("unreviewed\n", encoding="utf-8")
        return result


class FailedSecondNewAdapter(SessionAdapter):
    def invoke_configured(self, route: str, prompt: bytes, working_directory: Path, **kwargs: Any) -> ProviderResult:
        if not self.invocations:
            return super().invoke_configured(route, prompt, working_directory, **kwargs)
        expected_route, _ = next(self.scripted)
        assert route == expected_route and kwargs["session_action"] == "new"
        session_id = f"{self.provider}-failed-session"
        self.invocations.append({**kwargs, "route": route, "prompt": prompt, "cwd": working_directory, "session_id": session_id})
        return ProviderResult(
            self.provider,
            route,
            b"",
            stderr=b"failed",
            exit_code=1,
            session_id=session_id,
            error="fixture failure",
        )


class ReusedSecondNewAdapter(SessionAdapter):
    def invoke_configured(self, route: str, prompt: bytes, working_directory: Path, **kwargs: Any) -> ProviderResult:
        if not self.invocations:
            return super().invoke_configured(route, prompt, working_directory, **kwargs)
        expected_route, output = next(self.scripted)
        assert route == expected_route and kwargs["session_action"] == "new"
        session_id = str(kwargs["session_id"])
        self.invocations.append({**kwargs, "route": route, "prompt": prompt, "cwd": working_directory, "session_id": session_id})
        return ProviderResult(
            self.provider,
            route,
            output.encode("utf-8"),
            response_text=output,
            session_id=session_id,
        )


class CrossCycleReusingAdapter(SessionAdapter):
    def invoke_configured(self, route: str, prompt: bytes, working_directory: Path, **kwargs: Any) -> ProviderResult:
        prior_plans = sum(item["route"] == "planning-propose" for item in self.invocations)
        if route != "planning-propose" or prior_plans == 0:
            return super().invoke_configured(route, prompt, working_directory, **kwargs)
        expected_route, output = next(self.scripted)
        assert expected_route == route and kwargs["session_action"] == "new"
        session_id = f"{self.provider}-session-1"
        self.invocations.append({**kwargs, "route": route, "prompt": prompt, "cwd": working_directory, "session_id": session_id})
        return ProviderResult(
            self.provider,
            route,
            output.encode("utf-8"),
            response_text=output,
            session_id=session_id,
        )


class FailedThenReusedAdapter(ProviderAdapter):
    provider = "codex"

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def invoke(self, route: str, prompt: bytes, working_directory: Path) -> ProviderResult:
        raise AssertionError("configured invocation required")

    def invoke_configured(self, route: str, prompt: bytes, working_directory: Path, **kwargs: Any) -> ProviderResult:
        self.invocations.append({**kwargs, "route": route, "prompt": prompt})
        session_id = "codex-partial-session"
        if len(self.invocations) == 1:
            return ProviderResult("codex", route, b"", stderr=b"failed", exit_code=1, session_id=session_id)
        output = sentinel("human", "Second attempt")
        return ProviderResult(
            "codex", route, output.encode("utf-8"), response_text=output, session_id=session_id
        )

    def check(self) -> dict[str, object]:
        return {"provider": self.provider, "ready": True, "generation": "fixture"}


class FailedOnceThenSessionAdapter(SessionAdapter):
    def invoke_configured(self, route: str, prompt: bytes, working_directory: Path, **kwargs: Any) -> ProviderResult:
        if not self.invocations:
            self.invocations.append({**kwargs, "route": route, "prompt": prompt, "cwd": working_directory})
            return ProviderResult(
                self.provider,
                route,
                b'{"type":"result","subtype":"success","is_error":true,"api_error_status":429}',
                exit_code=1,
                error="claude_api_error_429",
                configured_model=kwargs["model"],
                configured_reasoning=kwargs["reasoning"],
                session_action=kwargs["session_action"],
            )
        return super().invoke_configured(route, prompt, working_directory, **kwargs)


class FailedNthInvocationAdapter(SessionAdapter):
    """Succeeds normally except for the Nth invocation, which fails like a
    quota/network cut-off without consuming a scripted response."""

    def __init__(self, provider: str, scripted: list[tuple[str, str]], fail_on: int) -> None:
        super().__init__(provider, scripted)
        self.fail_on = fail_on

    def invoke_configured(self, route: str, prompt: bytes, working_directory: Path, **kwargs: Any) -> ProviderResult:
        if len(self.invocations) + 1 == self.fail_on:
            self.invocations.append({**kwargs, "route": route, "prompt": prompt, "cwd": working_directory})
            return ProviderResult(
                self.provider,
                route,
                b"",
                stderr=b"quota exhausted",
                exit_code=1,
                error="fixture quota failure",
                configured_model=kwargs["model"],
                configured_reasoning=kwargs["reasoning"],
                session_action=kwargs["session_action"],
            )
        return super().invoke_configured(route, prompt, working_directory, **kwargs)


@pytest.fixture
def writable_project(tmp_path: Path) -> ProjectDefinition:
    root = tmp_path / "project"
    root.mkdir()
    (root / "AGENTS.md").write_text("# Instructions\n", encoding="utf-8")
    (root / "plan.md").write_text("# Plan\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "seed"],
        check=True,
        capture_output=True,
    )
    return ProjectDefinition(
        id="test",
        root=root.resolve(),
        read_only=True,
        instruction_files=("AGENTS.md", "plan.md"),
        validations=(),
        implementation_enabled=True,
        write_allowlist=(".",),
        commit_on_accept=True,
        allow_no_validations=True,
        validation_requires_approval=False,
    )


def make_cycle(tmp_path: Path, project: ProjectDefinition, codex: ProviderAdapter, claude: ProviderAdapter) -> CycleOrchestrator:
    return CycleOrchestrator(
        tmp_path / "runtime",
        {"codex": codex, "claude": claude},
        {"test": project},
        load_workflows(),
    )


def test_continuous_cycle_preserves_a_and_b_and_starts_fresh_c(tmp_path: Path, writable_project: ProjectDefinition):
    codex = SessionAdapter("codex", [
        ("planning-propose", response("continue", "Initial plan")),
        ("planning-revise", response("continue", "Revised plan")),
        ("implementation", response("continue", "Implemented once")),
        ("implementation-repair", response("continue", "Implemented repair")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", response("continue", "Plan finding")),
        ("planning-review", response("ready", "Plan approved")),
        ("implementation-review", response("continue", "Implementation defect")),
        ("implementation-review", response("ready", "Implementation accepted")),
        ("next-task", response("human", "Proposed next task")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Build the useful thing", "test")
    state = app.run_to_stop(run_id)

    assert state["status"] == "paused"
    assert state["pending_human_decision"] == "next_task_approval"
    assert state["working_revision"] != state["source_revision"]
    assert not (writable_project.root / "built.txt").exists()
    assert (Path(state["execution_worktree"]) / "built.txt").read_text(encoding="utf-8") == (
        "implementation\nimplementation-repair\n"
    )

    planner = state["cycles"][0]["sessions"]["planner"]
    reviewer = state["cycles"][0]["sessions"]["reviewer"]
    implementer = state["cycles"][0]["sessions"]["implementer"]
    assert planner["label"] == "A" and planner["active_session_id"] == "codex-session-1"
    assert reviewer["label"] == "B" and reviewer["active_session_id"] == "claude-session-1"
    assert implementer["label"] == "C" and implementer["active_session_id"] == "codex-session-2"
    assert claude.invocations[0]["model"] == "claude-fable-5"
    assert claude.invocations[2]["model"] == "claude-opus-5"
    assert claude.invocations[-1]["route"] == "next-task"
    assert claude.invocations[-1]["model"] == "claude-fable-5"
    assert all(item["session_id"] == "claude-session-1" for item in claude.invocations)

    handoff = state["cycles"][0]["approved_handoff"]
    assert app.artifact(run_id, handoff).decode("utf-8") == "Revised plan"
    implementation_prompt = next(item["prompt"] for item in codex.invocations if item["route"] == "implementation")
    assert b"Revised plan" in implementation_prompt
    assert b"Plan finding" not in implementation_prompt
    assert state["cycles"][0]["completion_receipt"]
    terminal = app.decide(run_id, "no")
    assert terminal["status"] == "complete"
    branch = terminal["execution_branch"]
    assert subprocess.run(
        ["git", "-C", str(writable_project.root), "show-ref", "--verify", f"refs/heads/{branch}"],
        capture_output=True,
    ).returncode == 0
    cleaned = app.cleanup_worktree(run_id)
    assert cleaned["execution_worktree_removed"] is True
    assert not Path(terminal["execution_worktree"]).exists()


def test_workflow_stack_advances_on_explicit_layer_boundary(tmp_path: Path, writable_project: ProjectDefinition):
    codex = SessionAdapter("codex", [
        ("planning-propose", response("continue", "Initial plan")),
        ("planning-revise", response("continue", "Revised plan")),
        ("implementation", response("continue", "Implemented once")),
        ("implementation-repair", response("continue", "Implemented repair")),
        ("strategy-propose", response("continue", "Strategy plan")),
        ("strategy-operator", response("continue", "Handoff package")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", response("continue", "Plan finding")),
        ("planning-review", response("ready", "Plan approved")),
        ("implementation-review", response("continue", "Implementation defect")),
        ("implementation-review", response("ready", "Implementation accepted")),
        ("next-task", response("human", "Proposed next task")),
        ("strategy-review", response("ready", "Strategy approved")),
        ("strategy-audit", response("ready", "Strategy verified")),
        ("strategy-next-task", response("human", "Next layer proposal")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(
        b"Build the useful thing",
        "test",
        workflow_stack=["continuous-development", "strategy-council"],
    )
    first_pause = app.run_to_stop(run_id)
    assert first_pause["status"] == "paused"
    assert first_pause["pending_human_decision"] == "next_task_approval"
    second_pause = app.decide(run_id, "no")
    assert second_pause["status"] == "paused"
    assert second_pause["pending_human_decision"] == "next_task_approval"
    assert second_pause["workflow"] == "strategy-council"
    assert second_pause["workflow_stack_index"] == 1
    assert second_pause["workflow_stack"] == ["continuous-development", "strategy-council"]
    assert len(second_pause["cycles"]) == 2
    assert second_pause["cycles"][0]["status"] == "complete"
    assert second_pause["cycles"][1]["completion_receipt"]
    assert second_pause["stack_handoff"]["workflow"] == "continuous-development"
    assert any(event["kind"] == "workflow_stack.layer_started" for event in second_pause["events"])
    assert b"Previous workflow layer" in next(
        item["prompt"] for item in codex.invocations if item["route"] == "strategy-propose"
    )
    terminal = app.decide(run_id, "no")
    assert terminal["status"] == "complete"
    branch = terminal["execution_branch"]
    cleaned = app.cleanup_worktree(run_id)
    assert cleaned["execution_worktree_removed"] is True
    assert not Path(terminal["execution_worktree"]).exists()
    assert subprocess.run(
        ["git", "-C", str(writable_project.root), "show-ref", "--verify", f"refs/heads/{branch}"],
        capture_output=True,
    ).returncode == 0


def test_workflow_stack_runs_strategy_proof_and_ui_layers_in_order(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [
        ("planning-propose", response("continue", "Initial plan")),
        ("planning-revise", response("continue", "Revised plan")),
        ("implementation", response("continue", "Implemented once")),
        ("implementation-repair", response("continue", "Implemented repair")),
        ("strategy-propose", response("continue", "Strategy plan")),
        ("strategy-operator", response("continue", "Strategy package")),
        ("test-propose", response("continue", "Proof plan")),
        ("test-run", response("continue", "Proof executed")),
        ("ui-propose", response("continue", "UI plan")),
        ("ui-build", response("continue", "UI built")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", response("continue", "Plan finding")),
        ("planning-review", response("ready", "Plan approved")),
        ("implementation-review", response("continue", "Implementation defect")),
        ("implementation-review", response("ready", "Implementation accepted")),
        ("next-task", response("human", "Strategy layer next")),
        ("strategy-review", response("ready", "Strategy approved")),
        ("strategy-audit", response("ready", "Strategy verified")),
        ("strategy-next-task", response("human", "Proof layer next")),
        ("test-review", response("ready", "Proof plan approved")),
        ("test-audit", response("ready", "Proof verified")),
        ("test-next-task", response("human", "UI layer next")),
        ("ui-review", response("ready", "UI plan approved")),
        ("ui-critic", response("ready", "UI verified")),
        ("ui-next-task", response("human", "Final next task")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(
        b"Build the useful thing",
        "test",
        workflow_stack=["continuous-development", "strategy-council", "test-proof-gate", "ui-studio"],
    )
    state = app.run_to_stop(run_id)
    assert state["workflow"] == "continuous-development"
    assert state["workflow_stack_index"] == 0
    for expected_index, expected_workflow in enumerate(
        ("strategy-council", "test-proof-gate", "ui-studio"),
        start=1,
    ):
        state = app.decide(run_id, "no")
        assert state["status"] == "paused"
        assert state["pending_human_decision"] == "next_task_approval"
        assert state["workflow"] == expected_workflow
        assert state["workflow_stack_index"] == expected_index
        assert state["cycles"][expected_index - 1]["status"] == "complete"
        assert state["cycles"][expected_index]["completion_receipt"]
    terminal = app.decide(run_id, "no")
    assert terminal["status"] == "complete"
    assert terminal["workflow"] == "ui-studio"
    assert terminal["workflow_stack_index"] == 3
    assert [item["route"] for item in codex.invocations] == [
        "planning-propose", "planning-revise", "implementation", "implementation-repair",
        "strategy-propose", "strategy-operator", "test-propose", "test-run", "ui-propose", "ui-build",
    ]
    assert [item["route"] for item in claude.invocations] == [
        "planning-review", "planning-review", "implementation-review", "implementation-review", "next-task",
        "strategy-review", "strategy-audit", "strategy-next-task", "test-review", "test-audit",
        "test-next-task", "ui-review", "ui-critic", "ui-next-task",
    ]
    assert len([event for event in terminal["events"] if event["kind"] == "workflow_stack.layer_started"]) == 3


def test_cadence_station_stack_seals_weekly_daily_hourly_rails(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [
        ("strategy-propose", response("continue", "Weekly strategy")),
        ("strategy-operator", response("continue", "Weekly package")),
        ("test-propose", response("continue", "Daily proof plan")),
        ("test-run", response("continue", "Daily proof run")),
        ("ui-propose", response("continue", "Hourly surface plan")),
        ("ui-build", response("continue", "Hourly surface build")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("strategy-review", response("ready", "Weekly strategy approved")),
        ("strategy-audit", response("ready", "Weekly strategy verified")),
        ("strategy-next-task", response("human", "Daily dispatch next")),
        ("test-review", response("ready", "Daily proof approved")),
        ("test-audit", response("ready", "Daily proof verified")),
        ("test-next-task", response("human", "Hourly station next")),
        ("ui-review", response("ready", "Hourly surface approved")),
        ("ui-critic", response("ready", "Hourly surface verified")),
        ("ui-next-task", response("human", "Backbone next")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(
        b"Repair the cadence backbone",
        "test",
        workflow="weekly-governance",
        workflow_stack=["weekly-governance", "daily-dispatch", "hourly-station"],
    )
    state = app.run_to_stop(run_id)
    assert state["pending_human_decision"] == "next_task_approval"

    state = app.decide(run_id, "no")
    assert state["workflow"] == "daily-dispatch"
    assert state["stack_handoff"]["source_cadence"] == "weekly"
    assert state["stack_handoff"]["target_cadence"] == "daily"
    assert state["stack_handoff"]["direction"] == "downward"
    assert b"Cadence rail: weekly" in next(
        item["prompt"] for item in codex.invocations if item["route"] == "test-propose"
    )
    first_handoff = state["cadence_backbone"]["handoffs"][0]
    first_payload = json.loads(app.artifact(run_id, first_handoff["artifact_file"]))
    assert first_payload["schema_version"] == "toledo_orchestrator.cadence_handoff.v1"
    assert first_payload["request_sha256"] == state["cycles"][1]["request_sha256"]
    context = app._context_section(state, "stack-handoff")
    assert context is not None
    assert "Verified cadence handoff artifact" in context[1]

    handoff_path = app._run_dir(run_id) / first_handoff["artifact_file"]
    sealed_bytes = handoff_path.read_bytes()
    handoff_path.write_bytes(sealed_bytes + b"\n")
    try:
        with pytest.raises(ValueError, match="artifact hash mismatch"):
            app._context_section(state, "stack-handoff")
    finally:
        handoff_path.write_bytes(sealed_bytes)

    mismatched_projection = json.loads(json.dumps(state))
    mismatched_projection["stack_handoff"]["request_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="projection does not match sealed field"):
        app._context_section(mismatched_projection, "stack-handoff")

    state = app.decide(run_id, "no")
    assert state["workflow"] == "hourly-station"
    assert state["stack_handoff"]["source_cadence"] == "daily"
    assert state["stack_handoff"]["target_cadence"] == "hourly"
    assert state["stack_handoff"]["direction"] == "downward"
    terminal = app.decide(run_id, "no")
    assert terminal["status"] == "complete"
    assert [station["status"] for station in terminal["cadence_backbone"]["stations"]] == [
        "complete", "complete", "complete"
    ]
    assert [handoff["direction"] for handoff in terminal["cadence_backbone"]["handoffs"]] == [
        "downward", "downward"
    ]
    app.cleanup_worktree(run_id)


def test_cadence_stack_rejects_weekly_to_hourly_skip(
    tmp_path: Path, writable_project: ProjectDefinition
):
    app = make_cycle(tmp_path, writable_project, SessionAdapter("codex", []), SessionAdapter("claude", []))
    with pytest.raises(ValueError, match="adjacent stations"):
        app.create_run(
            b"Do not skip the daily station",
            "test",
            workflow="weekly-governance",
            workflow_stack=["weekly-governance", "hourly-station"],
        )


def test_weekly_daily_cadence_boundary_resets_continuous_loop_budget(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [
        ("strategy-propose", sentinel("continue", "Weekly plan one")),
        ("strategy-operator", sentinel("continue", "Weekly dispatch one")),
        ("strategy-propose", sentinel("continue", "Weekly plan two")),
        ("strategy-operator", sentinel("continue", "Weekly dispatch two")),
        ("strategy-propose", sentinel("continue", "Weekly plan three")),
        ("strategy-operator", sentinel("continue", "Weekly dispatch three")),
        ("test-propose", sentinel("human", "Daily proof needs host-side evidence")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("strategy-review", sentinel("ready", "Weekly plan one ready")),
        ("strategy-audit", sentinel("ready", "Weekly dispatch one ready")),
        ("strategy-next-task", sentinel("human", "Weekly next one")),
        ("strategy-review", sentinel("ready", "Weekly plan two ready")),
        ("strategy-audit", sentinel("ready", "Weekly dispatch two ready")),
        ("strategy-next-task", sentinel("human", "Weekly next two")),
        ("strategy-review", sentinel("ready", "Weekly plan three ready")),
        ("strategy-audit", sentinel("ready", "Weekly dispatch three ready")),
        ("strategy-next-task", sentinel("human", "Daily dispatch next")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)

    with pytest.raises(ValueError, match="adjacent stations"):
        app.create_run(
            b"Reject a direct weekly-to-hourly move",
            "test",
            workflow="weekly-governance",
            workflow_stack=["weekly-governance", "hourly-station"],
            continuous_loop_enabled=True,
            continuous_loop_cycles=3,
        )

    run_id = app.create_run(
        b"Prove the weekly-to-daily station budget reset",
        "test",
        workflow="weekly-governance",
        workflow_stack=["weekly-governance", "daily-dispatch", "hourly-station"],
        continuous_loop_enabled=True,
        continuous_loop_cycles=3,
    )
    weekly = app.run_to_stop(run_id)
    assert weekly["workflow"] == "weekly-governance"
    assert weekly["cadence_backbone"]["stations"][0]["status"] == "active"
    assert weekly["continuous_loop"]["status"] == "target_reached"
    assert weekly["continuous_loop"]["station_base_cycle"] == 0
    assert weekly["continuous_loop"]["completed_cycles"] == 3
    assert sum(event["kind"] == "continuous_loop.target_reached" for event in weekly["events"]) == 1

    daily = app.decide(run_id, "no")
    assert daily["workflow"] == "daily-dispatch"
    assert daily["workflow_stack_index"] == 1
    assert daily["cadence_backbone"]["stations"] == [
        {"cadence": "weekly", "index": 0, "status": "complete", "workflow": "weekly-governance"},
        {"cadence": "daily", "index": 1, "status": "active", "workflow": "daily-dispatch"},
        {"cadence": "hourly", "index": 2, "status": "pending", "workflow": "hourly-station"},
    ]
    assert daily["continuous_loop"] == {
        "enabled": True,
        "target_cycles": 3,
        "completed_cycles": 0,
        "station_base_cycle": 3,
        "status": "running",
    }
    resets = [
        event for event in daily["events"]
        if event["kind"] == "continuous_loop.station_reset"
    ]
    assert len(resets) == 1
    reset_artifact = json.loads(app.artifact(run_id, f"events/{resets[0]['id']}.json"))
    assert reset_artifact["station"] == 1
    assert reset_artifact["workflow"] == "daily-dispatch"
    assert reset_artifact["target_cycles"] == 3
    assert reset_artifact["previous_station_base_cycle"] == weekly["continuous_loop"]["station_base_cycle"]
    assert reset_artifact["previous_completed_cycles"] == weekly["continuous_loop"]["completed_cycles"]
    assert reset_artifact["station_base_cycle"] == daily["continuous_loop"]["station_base_cycle"]
    assert reset_artifact["completed_cycles"] == daily["continuous_loop"]["completed_cycles"]

    handoff = daily["cadence_backbone"]["handoffs"][0]
    handoff_bytes = app.artifact(run_id, handoff["artifact_file"])
    assert handoff["source_cadence"] == "weekly"
    assert handoff["target_cadence"] == "daily"
    assert handoff["artifact_sha256"] == sha256(handoff_bytes)
    assert daily["artifacts"][handoff["artifact_file"]]["sha256"] == sha256(handoff_bytes)
    assert app._context_section(daily, "stack-handoff") is not None


def test_other_revises_next_task_in_same_reviewer_session(tmp_path: Path, writable_project: ProjectDefinition):
    codex = SessionAdapter("codex", [
        ("planning-propose", response("continue", "Plan")),
        ("implementation", response("continue", "Implementation")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", response("ready", "Plan ready")),
        ("implementation-review", response("ready", "Implementation ready")),
        ("next-task", response("human", "First next task")),
        ("next-task-revise", response("human", "Revised next task")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Task", "test")
    state = app.run_to_stop(run_id)
    state = app.decide(run_id, "other", b"Make it smaller and evidence-first.\r\n")
    assert state["status"] == "paused" and state["pending_human_decision"] == "next_task_approval"
    assert claude.invocations[-1]["route"] == "next-task-revise"
    assert claude.invocations[-1]["session_id"] == "claude-session-1"
    assert b"Make it smaller and evidence-first.\r\n" in claude.invocations[-1]["prompt"]
    assert app.show_turn(run_id, state["current_turn"]) == "Revised next task"


def test_yes_starts_fresh_d_e_f_cycle_from_exact_claude_proposal(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [
        ("planning-propose", sentinel("continue", "Plan one")),
        ("implementation", sentinel("continue", "Build one")),
        ("planning-propose", sentinel("continue", "Plan two")),
        ("implementation", sentinel("continue", "Build two")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", sentinel("ready", "Plan one ready")),
        ("implementation-review", sentinel("ready", "Build one ready")),
        ("next-task", sentinel("human", "Cycle two request\r\n")),
        ("planning-review", sentinel("ready", "Plan two ready")),
        ("implementation-review", sentinel("ready", "Build two ready")),
        ("next-task", sentinel("human", "Cycle three request")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Cycle one request", "test")
    first = app.run_to_stop(run_id)
    first_revision = first["working_revision"]
    second = app.decide(run_id, "yes")
    assert second["cycle"] == 2 and second["pending_human_decision"] == "next_task_approval"
    request_path = app._run_dir(run_id) / second["cycles"][1]["request_file"]
    assert request_path.read_bytes() == b"Cycle two request"
    sessions = second["cycles"][1]["sessions"]
    assert sessions["planner"]["label"] == "D" and sessions["planner"]["active_session_id"] == "codex-session-3"
    assert sessions["reviewer"]["label"] == "E" and sessions["reviewer"]["active_session_id"] == "claude-session-2"
    assert sessions["implementer"]["label"] == "F" and sessions["implementer"]["active_session_id"] == "codex-session-4"
    assert second["working_revision"] != first_revision
    assert second["execution_branch"] == first["execution_branch"]
    assert [item["model"] for item in claude.invocations] == [
        "claude-fable-5", "claude-opus-5", "claude-fable-5",
        "claude-fable-5", "claude-opus-5", "claude-fable-5",
    ]
    assert [item["session_id"] for item in claude.invocations[:3]] == ["claude-session-1"] * 3
    assert [item["session_id"] for item in claude.invocations[3:]] == ["claude-session-2"] * 3


def test_continuous_loop_runs_exact_bounded_cycle_count_then_pauses(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [
        ("planning-propose", sentinel("continue", "Plan one")),
        ("implementation", sentinel("continue", "Build one")),
        ("planning-propose", sentinel("continue", "Plan two")),
        ("implementation", sentinel("continue", "Build two")),
        ("planning-propose", sentinel("continue", "Plan three")),
        ("implementation", sentinel("continue", "Build three")),
        ("planning-propose", sentinel("continue", "Plan four")),
        ("implementation", sentinel("continue", "Build four")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", sentinel("ready", "Plan one ready")),
        ("implementation-review", sentinel("ready", "Build one ready")),
        ("next-task", sentinel("human", "Cycle two request\n")),
        ("planning-review", sentinel("ready", "Plan two ready")),
        ("implementation-review", sentinel("ready", "Build two ready")),
        ("next-task", sentinel("human", "Cycle three request\n")),
        ("planning-review", sentinel("ready", "Plan three ready")),
        ("implementation-review", sentinel("ready", "Build three ready")),
        ("next-task", sentinel("human", "Cycle four request\n")),
        ("planning-review", sentinel("ready", "Plan four ready")),
        ("implementation-review", sentinel("ready", "Build four ready")),
        ("next-task", sentinel("human", "Cycle five request\n")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)

    state = app.run_to_stop(app.create_run(
        b"Cycle one request",
        "test",
        continuous_loop_enabled=True,
        continuous_loop_cycles=3,
    ))

    assert state["status"] == "paused"
    assert state["pending_human_decision"] == "next_task_approval"
    assert state["cycle"] == 3
    assert state["continuous_loop"] == {
        "enabled": True,
        "target_cycles": 3,
        "completed_cycles": 3,
        "station_base_cycle": 0,
        "status": "target_reached",
    }
    assert [cycle["status"] for cycle in state["cycles"]] == ["complete", "complete", "active"]
    automatic = [decision for decision in state["decisions"] if decision.get("actor") == "relay"]
    assert [(item["cycle"], item["choice"], item["reason"]) for item in automatic] == [
        (1, "yes", "next_task_approval"),
        (2, "yes", "next_task_approval"),
    ]
    assert all(item["title"] == "Relay: continuous loop" for item in automatic)
    assert sum(event["kind"] == "automation.decision" for event in state["events"]) == 2
    assert sum(event["kind"] == "continuous_loop.target_reached" for event in state["events"]) == 1
    assert len(codex.invocations) == 6
    assert len(claude.invocations) == 9
    second_request = app._run_dir(state["run_id"]) / state["cycles"][1]["request_file"]
    third_request = app._run_dir(state["run_id"]) / state["cycles"][2]["request_file"]
    assert second_request.read_bytes() == b"Cycle two request"
    assert third_request.read_bytes() == b"Cycle three request"

    extended = app.decide(state["run_id"], "yes")
    assert extended["cycle"] == 4
    assert extended["status"] == "paused"
    assert extended["pending_human_decision"] == "next_task_approval"
    assert extended["continuous_loop"]["status"] == "manual_extension"
    assert sum(item.get("actor") == "relay" for item in extended["decisions"]) == 2


def test_continuous_loop_station_budget_resets_at_stack_boundary(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [
        ("planning-propose", sentinel("continue", "Weekly plan one")),
        ("implementation", sentinel("continue", "Weekly build one")),
        ("planning-propose", sentinel("continue", "Weekly plan two")),
        ("implementation", sentinel("continue", "Weekly build two")),
        ("planning-propose", sentinel("continue", "Weekly plan three")),
        ("implementation", sentinel("continue", "Weekly build three")),
        ("strategy-propose", sentinel("continue", "Daily plan one")),
        ("strategy-operator", sentinel("continue", "Daily dispatch one")),
        ("strategy-propose", sentinel("continue", "Daily plan two")),
        ("strategy-operator", sentinel("continue", "Daily dispatch two")),
        ("strategy-propose", sentinel("continue", "Daily plan three")),
        ("strategy-operator", sentinel("continue", "Daily dispatch three")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", sentinel("ready", "Weekly plan one ready")),
        ("implementation-review", sentinel("ready", "Weekly build one ready")),
        ("next-task", sentinel("human", "Weekly next one")),
        ("planning-review", sentinel("ready", "Weekly plan two ready")),
        ("implementation-review", sentinel("ready", "Weekly build two ready")),
        ("next-task", sentinel("human", "Weekly next two")),
        ("planning-review", sentinel("ready", "Weekly plan three ready")),
        ("implementation-review", sentinel("ready", "Weekly build three ready")),
        ("next-task", sentinel("human", "Daily dispatch proposal")),
        ("strategy-review", sentinel("ready", "Daily plan one ready")),
        ("strategy-audit", sentinel("ready", "Daily dispatch one ready")),
        ("strategy-next-task", sentinel("human", "Daily next one")),
        ("strategy-review", sentinel("ready", "Daily plan two ready")),
        ("strategy-audit", sentinel("ready", "Daily dispatch two ready")),
        ("strategy-next-task", sentinel("human", "Daily next two")),
        ("strategy-review", sentinel("ready", "Daily plan three ready")),
        ("strategy-audit", sentinel("ready", "Daily dispatch three ready")),
        ("strategy-next-task", sentinel("human", "Daily terminal proposal")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(
        b"Run each station within its own budget",
        "test",
        workflow_stack=["continuous-development", "strategy-council"],
        continuous_loop_enabled=True,
        continuous_loop_cycles=3,
    )

    first_station = app.run_to_stop(run_id)
    assert first_station["workflow_stack_index"] == 0
    assert first_station["cycle"] == 3
    assert first_station["continuous_loop"]["station_base_cycle"] == 0
    assert first_station["continuous_loop"]["completed_cycles"] == 3
    assert sum(event["kind"] == "continuous_loop.target_reached" for event in first_station["events"]) == 1

    second_station = app.decide(run_id, "no")
    assert second_station["workflow"] == "strategy-council"
    assert second_station["workflow_stack_index"] == 1
    assert second_station["cycle"] == 6
    assert second_station["continuous_loop"] == {
        "enabled": True,
        "target_cycles": 3,
        "completed_cycles": 3,
        "station_base_cycle": 3,
        "status": "target_reached",
    }
    assert sum(event["kind"] == "continuous_loop.target_reached" for event in second_station["events"]) == 2
    resets = [event for event in second_station["events"] if event["kind"] == "continuous_loop.station_reset"]
    assert len(resets) == 1
    reset_artifact = json.loads(app.artifact(run_id, f"events/{resets[0]['id']}.json"))
    assert reset_artifact["station"] == 1
    assert reset_artifact["workflow"] == "strategy-council"
    assert len([item for item in codex.invocations if item["route"] in {"planning-propose", "implementation"}]) == 6
    assert len([item for item in codex.invocations if item["route"] in {"strategy-propose", "strategy-operator"}]) == 6

    terminal = app.decide(run_id, "no")
    assert terminal["status"] == "complete"
    app.cleanup_worktree(run_id)


def test_continuous_loop_boundary_resets_completed_count_before_next_station(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [
        ("planning-propose", sentinel("continue", "Plan one")),
        ("implementation", sentinel("continue", "Build one")),
        ("planning-propose", sentinel("continue", "Plan two")),
        ("implementation", sentinel("continue", "Build two")),
        ("planning-propose", sentinel("continue", "Plan three")),
        ("implementation", sentinel("continue", "Build three")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", sentinel("ready", "Plan one ready")),
        ("implementation-review", sentinel("ready", "Build one ready")),
        ("next-task", sentinel("human", "Next one")),
        ("planning-review", sentinel("ready", "Plan two ready")),
        ("implementation-review", sentinel("ready", "Build two ready")),
        ("next-task", sentinel("human", "Next two")),
        ("planning-review", sentinel("ready", "Plan three ready")),
        ("implementation-review", sentinel("ready", "Build three ready")),
        ("next-task", sentinel("human", "Next station")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(
        b"Reset the station budget",
        "test",
        workflow_stack=["continuous-development", "strategy-council"],
        continuous_loop_enabled=True,
        continuous_loop_cycles=3,
    )
    state = app.run_to_stop(run_id)
    assert state["continuous_loop"]["completed_cycles"] == 3

    mutable = app.state(run_id)
    mutable["continuous_loop"]["status"] = "manual_extension"
    assert app._advance_stack_layer(mutable, reason="test_boundary")
    assert mutable["continuous_loop"]["station_base_cycle"] == 3
    assert mutable["continuous_loop"]["completed_cycles"] == 0
    assert mutable["continuous_loop"]["status"] == "running"


def test_legacy_stack_station_loop_remains_paused_without_station_origin(
    tmp_path: Path, writable_project: ProjectDefinition
):
    app = make_cycle(
        tmp_path,
        writable_project,
        SessionAdapter("codex", []),
        SessionAdapter("claude", []),
    )
    run_id = app.create_run(
        b"Keep legacy station behavior safe",
        "test",
        workflow_stack=["continuous-development", "strategy-council"],
        continuous_loop_enabled=True,
        continuous_loop_cycles=3,
    )
    state = app.state(run_id)
    state["workflow_stack_index"] = 1
    state["status"] = "paused"
    state["pending_human_decision"] = "next_task_approval"
    state["continuous_loop"].pop("station_base_cycle")
    app._save(run_id, state)

    assert not app.continuous_loop_auto_resume_available(run_id)
    assert not app._auto_continue_continuous_loop(app.state(run_id))


@pytest.mark.parametrize(
    ("run_mode", "enabled", "cycles", "message"),
    [
        ("step", True, 3, "requires auto run mode"),
        ("auto", True, 2, "must be 3, 4, or 5"),
        ("auto", True, 6, "must be 3, 4, or 5"),
    ],
)
def test_continuous_loop_rejects_unbounded_or_incompatible_settings(
    tmp_path: Path,
    writable_project: ProjectDefinition,
    run_mode: str,
    enabled: bool,
    cycles: int,
    message: str,
):
    app = make_cycle(
        tmp_path,
        writable_project,
        SessionAdapter("codex", []),
        SessionAdapter("claude", []),
    )

    with pytest.raises(ValueError, match=message):
        app.create_run(
            b"Task",
            "test",
            run_mode=run_mode,
            continuous_loop_enabled=enabled,
            continuous_loop_cycles=cycles,
        )

    assert not app.runs_dir.exists() or not list(app.runs_dir.iterdir())


def test_continuous_loop_auto_resume_is_limited_to_safe_approval_gates(
    tmp_path: Path, writable_project: ProjectDefinition
):
    app = make_cycle(
        tmp_path,
        writable_project,
        SessionAdapter("codex", []),
        SessionAdapter("claude", []),
    )
    run_id = app.create_run(
        b"Task",
        "test",
        continuous_loop_enabled=True,
        continuous_loop_cycles=3,
    )
    state = app.state(run_id)
    state["status"] = "paused"
    for reason in ("validation_baseline_failure_decision", "next_task_approval"):
        state["pending_human_decision"] = reason
        app._save(run_id, state)
        assert app.continuous_loop_auto_resume_available(run_id)
    for reason in (
        "validation_execution_approval",
        "provider_invocation_failed",
        "validation_receipt_required",
        "implementation_changed_after_review",
    ):
        state["pending_human_decision"] = reason
        app._save(run_id, state)
        assert not app.continuous_loop_auto_resume_available(run_id)


def test_step_mode_pauses_before_d_and_accepts_d_override(tmp_path: Path, writable_project: ProjectDefinition):
    codex = SessionAdapter("codex", [
        ("planning-propose", sentinel("continue", "Plan one")),
        ("implementation", sentinel("continue", "Build one")),
        ("planning-propose", sentinel("human", "Plan two")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", sentinel("ready", "Plan ready")),
        ("implementation-review", sentinel("ready", "Build ready")),
        ("next-task", sentinel("human", "Cycle two request")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Cycle one", "test", run_mode="step")
    state = app.run_to_stop(run_id)
    while state.get("pending_human_decision") == "operator_step":
        state = app.continue_step(run_id)
    assert state["pending_human_decision"] == "next_task_approval"
    before = state["current_turn"]
    state = app.decide(run_id, "yes")
    assert state["pending_human_decision"] == "operator_step"
    assert state["cycle"] == 2 and state["current_turn"] == before and state["current_stage"] == "planning-propose"
    override = app.set_next_turn_override(run_id, profile="codex-planning", session_action="new")
    assert override["next_turn_override"]["target_stage"] == "planning-propose"
    state = app.continue_step(run_id)
    assert state["turns"][-1]["session_label"] == "D"


def test_step_mode_transports_exact_optional_owner_direction(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [
        ("planning-propose", sentinel("continue", "Plan")),
        ("planning-revise", sentinel("human", "Revision")),
    ])
    claude = SessionAdapter("claude", [
        ("planning-review", sentinel("continue", "Concrete finding")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Task", "test", run_mode="step")
    paused = app.run_to_stop(run_id)
    assert paused["pending_human_decision"] == "operator_step"

    direction = b"Use your judgment; fix real issues and push back on empty fear.\r\n"
    state = app.continue_step(run_id, direction)
    assert state["pending_human_decision"] == "operator_step"
    next_prompt = claude.invocations[-1]["prompt"]
    assert next_prompt.count(direction) == 1
    assert next_prompt.count(b"# Immediate human direction") == 1
    record = state["decisions"][-1]
    assert record["choice"] == "direction" and record["title"] == "Owner direction"
    assert (app._run_dir(run_id) / record["file"]).read_bytes() == direction
    final = app.continue_step(run_id)
    assert final["pending_human_decision"] == "provider_requested_human"
    assert direction not in codex.invocations[-1]["prompt"]


def test_planner_close_variant_resumes_a_after_b_accepts_c(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [
        ("planning-propose", sentinel("continue", "Plan")),
        ("implementation", sentinel("continue", "Build")),
        ("next-task", sentinel("human", "Strategic next task")),
        ("next-task-revise", sentinel("human", "Revised strategic next task")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", sentinel("ready", "Plan approved")),
        ("implementation-review", sentinel("ready", "Build approved")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(
        b"Task", "test", workflow="continuous-development-planner-close"
    )
    state = app.run_to_stop(run_id)
    assert state["pending_human_decision"] == "next_task_approval"
    assert [item["route"] for item in claude.invocations] == [
        "planning-review", "implementation-review"
    ]
    assert codex.invocations[0]["session_id"] == "codex-session-1"
    assert codex.invocations[1]["session_id"] == "codex-session-2"
    assert codex.invocations[2]["session_id"] == "codex-session-1"
    assert codex.invocations[2]["model"] == "gpt-5.6-sol"
    assert state["turns"][-1]["prompt_label"] == "Next"

    revised = app.decide(run_id, "other", b"Make it operationally narrower.\n")
    assert revised["pending_human_decision"] == "next_task_approval"
    assert codex.invocations[-1]["route"] == "next-task-revise"
    assert codex.invocations[-1]["session_id"] == "codex-session-1"
    assert b"Make it operationally narrower.\n" in codex.invocations[-1]["prompt"]


def test_custom_prompt_and_renamed_round_stages_are_declarative(
    tmp_path: Path, writable_project: ProjectDefinition
):
    workflow_value = {
        "id": "custom-debate",
        "label": "Custom debate",
        "start_stage": "draft-anything",
        "next_task_stage": "draft-anything",
        "next_task_revision_stage": "revise-anything",
        "profiles": {
            "draft": {"provider": "codex", "permission": "read-only"},
            "critic": {"provider": "claude", "permission": "read-only"},
        },
        "stages": {
            "draft-anything": {
                "phase": "planning", "role": "planner", "prompt_kind": "draft",
                "prompt_label": "Draft", "prompt_file": "custom-draft.md", "profile": "draft",
                "session_slot": "architect", "session_policy": "new-if-missing", "artifact_type": "plan",
                "context": ["request"],
                "transitions": {"continue": "critic-anything", "ready": "critic-anything", "human": "@pause:provider_requested_human"},
            },
            "critic-anything": {
                "phase": "review", "role": "reviewer", "prompt_kind": "critic",
                "prompt_label": "Critic", "prompt_file": "custom-critic.md", "profile": "critic",
                "session_slot": "skeptic", "session_policy": "new-if-missing", "artifact_type": "review",
                "context": ["latest:plan"],
                "direction": {"revisit": "custom-direction.md"},
                "round": {"counter": "debate", "cap": 1, "directive": "continue", "pause_reason": "custom_round_cap"},
                "transitions": {"continue": "revise-anything", "ready": "@pause:done", "human": "@pause:provider_requested_human"},
            },
            "revise-anything": {
                "phase": "planning", "role": "planner", "prompt_kind": "revise",
                "prompt_label": "Revise", "prompt_file": "custom-revise.md", "profile": "draft",
                "session_slot": "architect", "session_policy": "continue", "artifact_type": "plan",
                "context": ["latest:review"],
                "transitions": {"continue": "critic-anything", "ready": "critic-anything", "human": "@pause:provider_requested_human"},
            },
        },
    }
    workflow = WorkflowDefinition.from_value(workflow_value)
    runtime = tmp_path / "runtime"
    custom_prompts = runtime / "config" / "prompts" / "custom-debate"
    custom_prompts.mkdir(parents=True)
    (custom_prompts / "custom-draft.md").write_text("# Begin from purpose\n", encoding="utf-8")
    (custom_prompts / "custom-critic.md").write_text("# Find the real weakness\n", encoding="utf-8")
    (custom_prompts / "custom-revise.md").write_text("# Judge the critique\n", encoding="utf-8")
    (custom_prompts / "custom-direction.md").write_text(
        "Recheck this renamed stage using runtime-local guidance.\n", encoding="utf-8"
    )
    codex = SessionAdapter("codex", [
        ("draft-anything", sentinel("continue", "Draft")),
        ("revise-anything", sentinel("continue", "Revision")),
    ])
    claude = SessionAdapter("claude", [
        ("critic-anything", sentinel("continue", "Finding one")),
        ("critic-anything", sentinel("continue", "Finding two")),
    ])
    app = CycleOrchestrator(
        runtime,
        {"codex": codex, "claude": claude},
        {"test": writable_project},
        {"custom-debate": workflow},
    )
    state = app.run_to_stop(app.create_run(b"Task", "test", workflow="custom-debate"))
    assert state["pending_human_decision"] == "custom_round_cap"
    assert state["cycles"][0]["rounds"]["debate"]["count"] == 1
    assert state["cycles"][0]["sessions"]["architect"]["label"] == "A"
    assert state["cycles"][0]["sessions"]["skeptic"]["label"] == "B"
    assert b"# Begin from purpose" in codex.invocations[0]["prompt"]
    first_turn = state["turns"][0]
    assert first_turn["interstitial_file"] == "prompt-library/custom-draft.md"
    assert app.artifact(state["run_id"], first_turn["interstitial_file"]).decode("utf-8").splitlines() == [
        "# Begin from purpose"
    ]
    critic_turns = [turn for turn in state["turns"] if turn["stage"] == "critic-anything"]
    assert critic_turns[0]["direction_file"] is None
    assert critic_turns[1]["direction_file"] == f"{critic_turns[1]['id']}.direction.md"
    direction = app.artifact(
        state["run_id"], f"turns/{critic_turns[1]['direction_file']}"
    ).decode("utf-8")
    assert direction == "Recheck this renamed stage using runtime-local guidance.\n"
    assert b"Recheck this renamed stage using runtime-local guidance." in claude.invocations[1]["prompt"]


def test_legacy_v2_workflow_snapshot_recovers_caps_repairs_and_seal_source():
    old_snapshot = load_workflows()["continuous-development"].snapshot()
    old_snapshot.pop("schema_version", None)
    old_snapshot.pop("session_slots", None)
    for stage in old_snapshot["stages"].values():
        stage.pop("round", None)
        stage.pop("repair_stage", None)
        stage.pop("seal_source", None)
        stage.pop("prompt_label", None)
    restored = WorkflowDefinition.from_value(old_snapshot)
    planning_review = restored.stages["planning-review"]
    implementation_review = restored.stages["implementation-review"]
    assert planning_review.round_counter == "planning"
    assert planning_review.round_cap == 3
    assert planning_review.seal_source == "plan"
    assert implementation_review.round_counter == "implementation"
    assert implementation_review.round_cap == 2
    assert implementation_review.repair_stage == "implementation-repair"
    assert restored.stages["implementation"].repair_stage == "implementation-repair"
    assert restored.session_slots == ("planner", "reviewer", "implementer")


def test_current_schema_does_not_infer_legacy_semantics_from_stage_names():
    value = {
        "schema_version": "toledo_orchestrator.workflow.v2",
        "id": "custom-named-stage",
        "label": "Custom named stage",
        "start_stage": "planning-review",
        "next_task_stage": "planning-review",
        "next_task_revision_stage": "planning-review",
        "profiles": {"critic": {"provider": "claude", "permission": "read-only"}},
        "stages": {
            "planning-review": {
                "phase": "custom", "role": "critic", "prompt_kind": "critic",
                "prompt_file": "reviewer.md", "profile": "critic", "session_slot": "critic",
                "session_policy": "new-if-missing", "artifact_type": "critique",
                "transitions": {"ready": "@pause:done", "human": "@pause:human"},
            }
        },
    }
    parsed = WorkflowDefinition.from_value(value)
    assert parsed.stages["planning-review"].round_counter is None
    restored = WorkflowDefinition.from_value(parsed.snapshot())
    assert restored.stages["planning-review"].round_counter is None


def test_inherited_workflow_resolves_base_prompt_namespace(
    tmp_path: Path, writable_project: ProjectDefinition
):
    runtime = tmp_path / "runtime"
    workflow_dir = runtime / "config" / "workflows"
    workflow_dir.mkdir(parents=True)
    base_value = {
        "schema_version": "toledo_orchestrator.workflow.v2",
        "id": "custom-base",
        "label": "Custom base",
        "start_stage": "draft",
        "next_task_stage": "draft",
        "next_task_revision_stage": "draft",
        "profiles": {"draft": {"provider": "codex", "permission": "read-only"}},
        "stages": {
            "draft": {
                "phase": "planning", "role": "planner", "prompt_kind": "draft",
                "prompt_file": "base-only.md", "profile": "draft", "session_slot": "planner",
                "session_policy": "new-if-missing", "artifact_type": "plan",
                "transitions": {"human": "@pause:provider_requested_human"},
            }
        },
    }
    child_value = {
        "id": "custom-child",
        "extends": "custom-base",
        "label": "Custom child",
    }
    (workflow_dir / "custom-base.json").write_text(
        json.dumps(base_value), encoding="utf-8"
    )
    (workflow_dir / "custom-child.json").write_text(
        json.dumps(child_value), encoding="utf-8"
    )
    base_prompts = runtime / "config" / "prompts" / "custom-base"
    base_prompts.mkdir(parents=True)
    (base_prompts / "base-only.md").write_text("# Base-only direction\n", encoding="utf-8")

    workflows = load_configured_workflows(runtime)
    child = workflows["custom-child"]
    assert child.prompt_namespaces == ("custom-child", "custom-base")
    codex = SessionAdapter("codex", [("draft", sentinel("human", "Drafted"))])
    app = CycleOrchestrator(
        runtime,
        {"codex": codex, "claude": SessionAdapter("claude", [])},
        {"test": writable_project},
        workflows,
    )
    state = app.run_to_stop(app.create_run(b"Task", "test", workflow="custom-child"))
    assert state["pending_human_decision"] == "provider_requested_human"
    assert b"# Base-only direction" in codex.invocations[0]["prompt"]


def test_renamed_stage_seals_its_declared_non_plan_artifact(
    tmp_path: Path, writable_project: ProjectDefinition
):
    workflow = WorkflowDefinition.from_value({
        "schema_version": "toledo_orchestrator.workflow.v2",
        "id": "custom-seal",
        "label": "Custom seal",
        "start_stage": "shape-spec",
        "next_task_stage": "after-seal",
        "next_task_revision_stage": "after-seal",
        "profiles": {
            "author": {"provider": "codex", "permission": "read-only"},
            "checker": {"provider": "claude", "permission": "read-only"},
        },
        "stages": {
            "shape-spec": {
                "phase": "design", "role": "author", "prompt_kind": "shape",
                "prompt_file": "proposer.md", "profile": "author", "session_slot": "author",
                "session_policy": "new-if-missing", "artifact_type": "spec",
                "transitions": {"continue": "approve-spec", "ready": "approve-spec", "human": "@pause:human"},
            },
            "approve-spec": {
                "phase": "review", "role": "checker", "prompt_kind": "check",
                "prompt_file": "reviewer.md", "profile": "checker", "session_slot": "checker",
                "session_policy": "new-if-missing", "artifact_type": "spec-review", "seal_source": "spec",
                "transitions": {"continue": "shape-spec", "ready": "@seal:approved-handoff:after-seal", "human": "@pause:human"},
            },
            "after-seal": {
                "phase": "closure", "role": "author", "prompt_kind": "close",
                "prompt_file": "reviser.md", "profile": "author", "session_slot": "author",
                "session_policy": "continue", "artifact_type": "closure", "context": ["approved-handoff"],
                "transitions": {"human": "@pause:done"},
            },
        },
    })
    codex = SessionAdapter("codex", [
        ("shape-spec", sentinel("continue", "Exact spec bytes")),
        ("after-seal", sentinel("human", "Closure")),
    ])
    claude = SessionAdapter("claude", [("approve-spec", sentinel("ready", "Approved"))])
    app = CycleOrchestrator(
        tmp_path / "runtime",
        {"codex": codex, "claude": claude},
        {"test": writable_project},
        {"custom-seal": workflow},
    )
    state = app.run_to_stop(app.create_run(b"Task", "test", workflow="custom-seal"))
    handoff = state["cycles"][0]["approved_handoff"]
    assert app.artifact(state["run_id"], handoff) == b"Exact spec bytes"
    assert b"Exact spec bytes" in codex.invocations[-1]["prompt"]


def test_failed_validation_uses_declared_renamed_repair_stage(
    tmp_path: Path, writable_project: ProjectDefinition
):
    value = load_workflows()["continuous-development"].snapshot()
    value["id"] = "renamed-repair"
    stages = value["stages"]
    stages["fix-code"] = stages.pop("implementation-repair")
    for stage in stages.values():
        if stage.get("repair_stage") == "implementation-repair":
            stage["repair_stage"] = "fix-code"
        stage["transitions"] = {
            directive: ("fix-code" if target == "implementation-repair" else target)
            for directive, target in stage["transitions"].items()
        }
    workflow = WorkflowDefinition.from_value(value)
    failing_project = replace(
        writable_project,
        validations=(ValidationDefinition(
            "always-fails", 'python -c "raise SystemExit(1)"', "local", required=True
        ),),
        allow_no_validations=False,
        validation_requires_approval=False,
    )
    codex = SessionAdapter("codex", [
        ("planning-propose", sentinel("continue", "Plan")),
        ("implementation", sentinel("continue", "Build")),
        ("fix-code", sentinel("human", "Need repair direction")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", sentinel("ready", "Plan ready")),
        ("implementation-review", sentinel("ready", "Build looks ready")),
    ])
    app = CycleOrchestrator(
        tmp_path / "runtime",
        {"codex": codex, "claude": claude},
        {"test": failing_project},
        {"renamed-repair": workflow},
    )
    state = app.run_to_stop(app.create_run(b"Task", "test", workflow="renamed-repair"))
    assert state["pending_human_decision"] == "provider_requested_human"
    assert codex.invocations[-1]["route"] == "fix-code"
    assert state["cycles"][0]["rounds"]["implementation"]["count"] == 1


def test_new_cycle_rejects_reuse_of_a_prior_physical_session(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = CrossCycleReusingAdapter("codex", [
        ("planning-propose", sentinel("continue", "Plan one")),
        ("implementation", sentinel("continue", "Build one")),
        ("planning-propose", sentinel("human", "Plan two")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", sentinel("ready", "Plan ready")),
        ("implementation-review", sentinel("ready", "Build ready")),
        ("next-task", sentinel("human", "Cycle two request")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Cycle one", "test")
    app.run_to_stop(run_id)
    state = app.decide(run_id, "yes")
    assert state["cycle"] == 2
    assert state["pending_human_decision"] == "provider_session_not_new"
    assert state["cycles"][0]["sessions"]["planner"]["active_session_id"] == "codex-session-1"
    new_planner = state["cycles"][1]["sessions"]["planner"]
    assert new_planner["active_session_id"] is None and new_planner["history"] == []
    assert state["turns"][-1]["session_promoted"] is False


def test_retry_rejects_session_id_observed_on_failed_new_invocation(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = FailedThenReusedAdapter()
    app = make_cycle(tmp_path, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    first = app.run_to_stop(run_id)
    assert first["pending_human_decision"] == "provider_invocation_failed"
    second = app.decide(run_id, "yes")
    assert second["pending_human_decision"] == "provider_session_not_new"
    planner = second["cycles"][0]["sessions"]["planner"]
    assert planner["active_session_id"] is None and planner["history"] == []
    assert second["turns"][-1]["session_promoted"] is False


def test_missing_continuation_session_pauses_without_starting_over(tmp_path: Path, writable_project: ProjectDefinition):
    codex = SessionAdapter("codex", [])
    app = make_cycle(tmp_path, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    state = app.state(run_id)
    state["current_stage"] = "planning-revise"
    state["status"] = "running"
    app._save(run_id, state)
    paused = app.advance(run_id)
    assert paused["status"] == "paused"
    assert paused["pending_human_decision"] == "provider_session_missing"
    assert codex.invocations == []


def test_provider_requested_human_direction_reaches_the_resumed_session(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [
        ("planning-propose", sentinel("human", "Choose scope")),
        ("planning-propose", sentinel("human", "Direction received")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    app.run_to_stop(run_id)
    state = app.decide(run_id, "other", b"Use the narrow scope.\r\n")
    assert state["pending_human_decision"] == "provider_requested_human"
    assert codex.invocations[-1]["session_action"] == "continue"
    resumed_prompt = codex.invocations[-1]["prompt"]
    assert resumed_prompt.count(b"# Immediate human direction") == 1
    assert resumed_prompt.count(b"Use the narrow scope.\r\n") == 1


def test_provider_command_builders_select_profile_and_resume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured: list[list[str]] = []

    def fake_run(command: list[str], prompt: bytes, cwd: Path, timeout: int):
        captured.append(command)
        if "codex" in command[0]:
            return (FIXTURES / "codex_success.jsonl").read_bytes(), b"", 0, 10
        return (FIXTURES / "claude_success.json").read_bytes(), b"", 0, 10

    codex = CodexAdapter(executable="codex-test")
    claude = ClaudeAdapter(executable="claude-test")
    monkeypatch.setattr(codex, "_run", fake_run)
    monkeypatch.setattr(claude, "_run", fake_run)
    codex.invoke_configured(
        "planning-revise", b"prompt", tmp_path, model="gpt-test", reasoning="xhigh",
        permission="read-only", session_action="continue", session_id="codex-session-fixture",
    )
    claude.invoke_configured(
        "implementation-review", b"prompt", tmp_path, model="claude-test-model", reasoning="max",
        permission="read-only", session_action="continue", session_id="claude-session-fixture",
    )
    codex_command, claude_command = captured
    assert codex_command[:3] == ["codex-test", "exec", "resume"]
    assert "gpt-test" in codex_command and 'model_reasoning_effort="xhigh"' in codex_command
    assert codex_command[-2:] == ["codex-session-fixture", "-"]
    assert "--resume" in claude_command and "claude-session-fixture" in claude_command
    assert "claude-test-model" in claude_command and "max" in claude_command

    unusual_effort = 'xhigh"\nmodel="unexpected'
    codex.invoke_configured(
        "planning-propose", b"prompt", tmp_path, model="gpt-test", reasoning=unusual_effort,
        permission="read-only", session_action="new",
    )
    assert f"model_reasoning_effort={json.dumps(unusual_effort)}" in captured[-1]


def test_run_profiles_are_snapshotted_and_explicit_override_is_recorded(tmp_path: Path, writable_project: ProjectDefinition):
    codex = SessionAdapter("codex", [("planning-propose", response("human", "Need owner"))])
    app = make_cycle(tmp_path, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    workflow = app.workflows["continuous-development"]
    workflow.profiles["codex-planning"] = replace(workflow.profiles["codex-planning"], effort="low", label="Changed default")
    app.run_to_stop(run_id)
    assert codex.invocations[0]["reasoning"] == "xhigh"

    codex_override = SessionAdapter("codex", [("planning-propose", response("human", "Need owner"))])
    app_override = make_cycle(tmp_path / "override", writable_project, codex_override, SessionAdapter("claude", []))
    run_override = app_override.create_run(b"Task", "test")
    workflow_override = app_override.workflows["continuous-development"]
    workflow_override.profiles["codex-planning"] = replace(
        workflow_override.profiles["codex-planning"], effort="low", label="Changed default"
    )
    state = app_override.set_next_turn_override(
        run_override,
        profile="codex-planning",
        model="gpt-one-turn",
        effort="medium",
        session_action="new",
    )
    assert state["next_turn_override"]["profile_value"]["model"] == "gpt-one-turn"
    assert state["next_turn_override"]["profile_value"]["effort"] == "medium"
    assert state["next_turn_override"]["profile_value"]["label"] == "gpt-one-turn · medium"
    app_override.run_to_stop(run_override)
    assert codex_override.invocations[0]["model"] == "gpt-one-turn"
    assert codex_override.invocations[0]["reasoning"] == "medium"


def test_next_turn_override_uses_run_snapshot_and_rejects_missing_continue_session(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [("planning-propose", response("human", "Plan"))])
    app = make_cycle(tmp_path, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    with pytest.raises(ValueError, match="no active session to continue"):
        app.set_next_turn_override(run_id, session_action="continue")
    assert codex.invocations == []

    launch_profile = app.state(run_id)["workflow_snapshot"]["profiles"]["codex-planning"]
    app.workflows["continuous-development"].profiles["codex-planning"] = replace(
        app.workflows["continuous-development"].profiles["codex-planning"],
        label="Changed after launch",
        provider="claude",
        permission="workspace-write",
        timeout_seconds=9,
    )
    saved = app.set_next_turn_override(run_id, profile="codex-planning", session_action="new")
    applied = saved["next_turn_override"]["profile_value"]
    for key in ("label", "provider", "permission", "timeout_seconds"):
        assert applied[key] == launch_profile[key]


def test_custom_next_turn_selection_stays_visibly_unverified_on_preview_and_turn(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [("planning-propose", response("human", "Custom plan"))])
    app = make_cycle(tmp_path, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    saved = app.set_next_turn_override(
        run_id,
        model="gpt-owner-experiment",
        effort="owner-effort",
        session_action="new",
        custom=True,
    )
    assert saved["next_turn_override"]["profile_value"]["custom"] is True
    assert app.next_turn_preview(run_id)["profile"]["custom"] is True
    result = app.run_to_stop(run_id)
    assert result["turns"][-1]["custom_selection"] is True


def test_steer_continues_same_session_and_preserves_round_accounting(tmp_path: Path, writable_project: ProjectDefinition):
    codex = SessionAdapter("codex", [
        ("planning-propose", response("human", "Initial artifact")),
        ("planning-propose", response("human", "Replacement artifact")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    paused = app.run_to_stop(run_id)
    before_rounds = json.loads(json.dumps(paused["cycles"][0].get("rounds", {})))
    for pending_key in (
        "pending_validation",
        "pending_completion",
        "pending_commit",
        "pending_round_extension",
        "pending_baseline_acceptance",
    ):
        guarded = app.state(run_id)
        guarded[pending_key] = {"reason": "test"}
        app._save(run_id, guarded)
        assert app.steer_availability(run_id)["available"] is False
        guarded[pending_key] = None
        app._save(run_id, guarded)
    guarded = app.state(run_id)
    guarded["turns"][-1]["permission"] = "workspace-write"
    app._save(run_id, guarded)
    assert "read-only" in app.steer_availability(run_id)["reason"]
    guarded["turns"][-1]["permission"] = "read-only"
    app._save(run_id, guarded)
    result = app.steer(run_id, "Address the missing acceptance criterion.")
    replacement = result["turns"][-1]
    assert codex.invocations[-1]["session_action"] == "continue"
    assert codex.invocations[-1]["session_id"] == paused["turns"][-1]["session_id"]
    assert replacement["steer_of"] == paused["turns"][-1]["id"]
    assert replacement["steer_note_file"] and result["cycles"][0].get("rounds", {}) == before_rounds
    prompt = codex.invocations[-1]["prompt"].decode("utf-8")
    assert "Address the missing acceptance criterion." in prompt


def test_provider_switch_requires_opt_in_and_forces_new_session(tmp_path: Path, writable_project: ProjectDefinition):
    codex = SessionAdapter("codex", [])
    claude = SessionAdapter("claude", [("planning-propose", response("human", "Claude replacement"))])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Task", "test")
    with pytest.raises(ValueError, match="provider switch requires"):
        app.set_next_turn_override(run_id, profile="claude-planning-review", session_action="new")
    stage = app.workflows["continuous-development"].stages["planning-propose"]
    app.workflows["continuous-development"].stages["planning-propose"] = replace(stage, provider_switchable=True)
    state = app.state(run_id)
    state["workflow_snapshot"]["stages"]["planning-propose"]["provider_switchable"] = True
    app._save(run_id, state)
    state = app.set_next_turn_override(run_id, profile="claude-planning-review", session_action="new")
    assert state["next_turn_override"]["provider_switch"] is True
    result = app.run_to_stop(run_id)
    assert result["turns"][-1]["provider"] == "claude"
    assert claude.invocations[-1]["session_action"] == "new"


def test_curated_stance_override_is_opt_in_and_recorded_as_sidecar(tmp_path: Path, writable_project: ProjectDefinition):
    codex = SessionAdapter("codex", [("planning-propose", response("human", "Ideas artifact"))])
    app = make_cycle(tmp_path, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    with pytest.raises(ValueError, match="not enabled"):
        app.set_next_turn_override(run_id, stance="ideas")
    state = app.state(run_id)
    state["workflow_snapshot"]["stages"]["planning-propose"]["stance_overrides"] = ["ideas"]
    app._save(run_id, state)
    saved = app.set_next_turn_override(run_id, stance="ideas")
    assert saved["next_turn_override"]["stance"] == "ideas"
    state_path = app._run_dir(run_id) / "run.json"
    before_preview = state_path.read_bytes()
    preview = app.next_turn_preview(run_id)
    assert state_path.read_bytes() == before_preview
    assert "# Explicit one-turn stance override" in preview["prompt"]
    assert CURATED_STANCES["ideas"] in preview["prompt"]
    assert preview["override"] == {"active": True, "stance": "ideas"}
    result = app.run_to_stop(run_id)
    turn = result["turns"][-1]
    assert turn["stance_override"] == "ideas" and turn["stance_override_file"]
    assert b"Explicit one-turn stance override" in codex.invocations[-1]["prompt"]
    assert preview["prompt"].encode("utf-8") == codex.invocations[-1]["prompt"]


def test_caption_backfill_is_opt_in_bounded_and_sidecar_only(tmp_path: Path, writable_project: ProjectDefinition):
    codex = SessionAdapter("codex", [
        ("planning-propose", response("human", "Original artifact")),
        ("caption-backfill", "A producing model self-report."),
    ])
    app = make_cycle(tmp_path, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    state = app.run_to_stop(run_id)
    output = app._run_dir(run_id) / "turns" / state["turns"][-1]["output_file"]
    before = output.read_bytes()
    write_json(app.runtime_dir / "catalog" / "capabilities.v1.json", {
        "schema_version": CATALOG_SCHEMA, "verified_at": "2026-07-13T00:00:00+00:00",
        "models": [{"provider": "codex", "selection_token": "fixture-low", "supported_efforts": ["low"]}],
        "observed_models": [], "sources": {},
    })
    with pytest.raises(ValueError, match="off by default"):
        app.backfill_semantic_captions(run_id)
    report = app.backfill_semantic_captions(run_id, opt_in=True, limit=1)
    assert report["model"] == "fixture-low" and report["captions"][0]["caption"] == "A producing model self-report."
    assert output.read_bytes() == before
    assert (app._run_dir(run_id) / "captions").is_dir()
    assert b"Original artifact" in codex.invocations[-1]["prompt"]


def test_fork_rewind_requires_opt_in_and_preserves_source_evidence(tmp_path: Path, writable_project: ProjectDefinition):
    codex = SessionAdapter("codex", [
        ("planning-propose", response("human", "Original artifact")),
        ("planning-propose", response("human", "Replacement artifact")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    app.run_to_stop(run_id)
    app.steer(run_id, "Replace it.")
    source_path = app._run_dir(run_id) / "run.json"
    source_before = source_path.read_bytes()
    with pytest.raises(ValueError, match="off by default"):
        app.fork_rewind(run_id, rewind_to_turn=1)
    result = app.fork_rewind(run_id, rewind_to_turn=1, opt_in=True)
    fork = app.state(result["run_id"])
    assert source_path.read_bytes() == source_before
    assert fork["forked_from"]["run_id"] == run_id and fork["current_turn"] == 1
    assert len(fork["turns"]) == 1 and fork["next_turn_override"]["session_action"] == "new"
    assert (app._run_dir(result["run_id"]) / "turns" / "turn.0002.output.md").is_file()
    assert (app._run_dir(result["run_id"]) / "fork.json").is_file()


def test_provider_failure_gate_unlocks_provider_switch_without_stage_opt_in(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [
        ("planning-propose", response("continue", "Initial plan")),
        ("planning-review", response("human", "Codex rescue review")),
    ])
    claude = FailedOnceThenSessionAdapter("claude", [])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Task", "test")
    failed = app.run_to_stop(run_id)
    assert failed["pending_human_decision"] == "provider_invocation_failed"

    # planning-review does not opt in to provider_switchable, but the failure
    # gate unlocks the switch so the operator can route around the outage.
    with pytest.raises(ValueError, match="new physical session"):
        app.set_next_turn_override(run_id, profile="codex-planning", session_action="continue")
    saved = app.set_next_turn_override(run_id, profile="codex-planning", session_action="new")
    assert saved["next_turn_override"]["provider_switch"] is True

    retried = app.decide(run_id, "yes")
    assert retried["turns"][-1]["provider"] == "codex"
    assert codex.invocations[-1]["route"] == "planning-review"
    assert codex.invocations[-1]["session_action"] == "new"


def test_cutoff_retry_can_resume_the_interrupted_session(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [
        ("planning-propose", response("continue", "Initial plan")),
        ("planning-revise", response("continue", "Revised plan")),
    ])
    claude = FailedNthInvocationAdapter("claude", [
        ("planning-review", response("continue", "Needs one revision")),
        ("planning-review", response("human", "Review complete after the cut-off")),
    ], fail_on=2)
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Task", "test")
    failed = app.run_to_stop(run_id)
    assert failed["pending_human_decision"] == "provider_invocation_failed"
    assert failed["cycles"][0]["sessions"]["reviewer"]["active_session_id"] == "claude-session-1"

    app.set_next_turn_override(run_id, session_action="continue")
    state = app.decide(run_id, "other", b"We were cut off. Please continue.")
    resumed = claude.invocations[-1]
    assert resumed["session_action"] == "continue"
    assert resumed["session_id"] == "claude-session-1"
    assert b"We were cut off. Please continue." in resumed["prompt"]
    assert state["pending_human_decision"] == "provider_requested_human"


def test_provider_failure_retry_preserves_the_selected_profile_model_and_effort(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [("planning-propose", response("continue", "Initial plan"))])
    claude = FailedOnceThenSessionAdapter(
        "claude", [("planning-review", response("human", "Review completed"))]
    )
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Task", "test")
    failed = app.run_to_stop(run_id)
    assert failed["pending_human_decision"] == "provider_invocation_failed"

    saved = app.set_next_turn_override(
        run_id,
        profile="claude-implementation-review",
        model="claude-opus-4-8",
        effort="max",
        session_action="new",
    )
    assert saved["next_turn_override"]["profile"] == "claude-implementation-review"

    retried = app.decide(run_id, "yes")
    invocation = claude.invocations[-1]
    assert invocation["model"] == "claude-opus-4-8"
    assert invocation["reasoning"] == "max"
    assert invocation["session_action"] == "new"
    assert retried["turns"][-1]["profile"] == "claude-implementation-review"
    assert retried["turns"][-1]["configured_model"] == "claude-opus-4-8"
    assert retried["turns"][-1]["configured_reasoning"] == "max"
    assert retried["next_turn_override"] is None


def test_failed_explicit_turn_keeps_the_same_override_visible_for_retry(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [("planning-propose", response("continue", "Initial plan"))])
    claude = FailedOnceThenSessionAdapter(
        "claude", [("planning-review", response("human", "Review completed"))]
    )
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Task", "test", run_mode="step")
    app.run_to_stop(run_id)
    app.set_next_turn_override(
        run_id,
        profile="claude-implementation-review",
        model="claude-opus-4-8",
        effort="low",
        session_action="new",
    )

    failed = app.continue_step(run_id)

    assert failed["pending_human_decision"] == "provider_invocation_failed"
    retained = failed["next_turn_override"]
    assert retained["profile"] == "claude-implementation-review"
    assert retained["profile_value"]["model"] == "claude-opus-4-8"
    assert retained["profile_value"]["effort"] == "low"
    preview = app.next_turn_preview(run_id)
    assert preview["profile"]["model"] == "claude-opus-4-8"
    assert preview["profile"]["effort"] == "low"

    retried = app.decide(run_id, "yes")
    assert claude.invocations[-1]["model"] == "claude-opus-4-8"
    assert claude.invocations[-1]["reasoning"] == "low"
    assert retried["next_turn_override"] is None


def test_round_caps_allow_the_configured_number_of_corrections(tmp_path: Path, writable_project: ProjectDefinition):
    codex = SessionAdapter("codex", [
        ("planning-propose", response("continue", "Plan 0")),
        ("planning-revise", response("continue", "Plan 1")),
        ("planning-revise", response("continue", "Plan 2")),
        ("planning-revise", response("continue", "Plan 3")),
    ])
    claude = SessionAdapter("claude", [
        ("planning-review", response("continue", "Finding 1")),
        ("planning-review", response("continue", "Finding 2")),
        ("planning-review", response("continue", "Finding 3")),
        ("planning-review", response("continue", "Finding 4")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    state = app.run_to_stop(app.create_run(b"Task", "test"))
    assert state["status"] == "paused" and state["pending_human_decision"] == "planning_round_cap_reached"
    assert state["cycles"][0]["planning_round"] == 3
    assert [item["route"] for item in codex.invocations].count("planning-revise") == 3

    codex_impl = SessionAdapter("codex", [
        ("planning-propose", response("continue", "Plan")),
        ("implementation", response("continue", "Implementation 0")),
        ("implementation-repair", response("continue", "Implementation 1")),
        ("implementation-repair", response("continue", "Implementation 2")),
    ], writer=True)
    claude_impl = SessionAdapter("claude", [
        ("planning-review", response("ready", "Plan ready")),
        ("implementation-review", response("continue", "Finding 1")),
        ("implementation-review", response("continue", "Finding 2")),
        ("implementation-review", response("continue", "Finding 3")),
    ])
    app_impl = make_cycle(tmp_path / "implementation", writable_project, codex_impl, claude_impl)
    state_impl = app_impl.run_to_stop(app_impl.create_run(b"Task", "test"))
    assert state_impl["status"] == "paused"
    assert state_impl["pending_human_decision"] == "implementation_round_cap_reached"
    assert state_impl["cycles"][0]["implementation_round"] == 2
    assert [item["route"] for item in codex_impl.invocations].count("implementation-repair") == 2


def test_validation_and_post_review_mutations_cannot_escape_sealed_evidence(tmp_path: Path, writable_project: ProjectDefinition):
    validation_project = replace(
        writable_project,
        validations=(ValidationDefinition(
            "mutating-validation",
            'python -c "from pathlib import Path; Path(\'generated.txt\').write_text(\'x\')"',
            "local",
        ),),
        write_allowlist=("built.txt",),
    )
    codex = SessionAdapter("codex", [
        ("planning-propose", response("continue", "Plan")),
        ("implementation", response("continue", "Implementation")),
    ], writer=True)
    claude = SessionAdapter("claude", [("planning-review", response("ready", "Plan ready"))])
    app = make_cycle(tmp_path, validation_project, codex, claude)
    state = app.run_to_stop(app.create_run(b"Task", "test"))
    assert state["status"] == "paused"
    assert state["pending_human_decision"] == "unknown_validation_execution"
    assert state["validation_inflight"]["commands"][0]["id"] == "mutating-validation"
    assert "generated.txt" in state["errors"][-1]

    codex_review = SessionAdapter("codex", [
        ("planning-propose", response("continue", "Plan")),
        ("implementation", response("continue", "Implementation")),
    ], writer=True)
    claude_review = ReviewMutatingAdapter("claude", [
        ("planning-review", response("ready", "Plan ready")),
        ("implementation-review", response("ready", "Looks good")),
    ])
    app_review = make_cycle(tmp_path / "review-mutation", writable_project, codex_review, claude_review)
    review_state = app_review.run_to_stop(app_review.create_run(b"Task", "test"))
    assert review_state["status"] == "paused"
    assert review_state["pending_human_decision"] == "implementation_changed_after_review"


def test_cycle_correction_chatter_cannot_supersede_primary_artifact(tmp_path: Path, writable_project: ProjectDefinition):
    codex = SessionAdapter("codex", [
        ("planning-propose", "Original plan"),
        ("planning-propose", sentinel("continue", "correction chatter")),
    ])
    claude = SessionAdapter("claude", [("planning-review", sentinel("human", "Need owner input"))])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    state = app.run_to_stop(app.create_run(b"Task", "test"))
    assert state["pending_human_decision"] == "provider_requested_human"
    assert state["turns"][0]["artifact_type"] == "plan" and state["turns"][0]["substantive"] is True
    assert state["turns"][1]["artifact_type"] == "directive-correction"
    assert state["turns"][1]["correction"] is True and state["turns"][1]["substantive"] is False
    assert app._latest_turn(state, "plan")["id"] == "turn.0001"
    assert b"Original plan" in claude.invocations[0]["prompt"]
    assert b"correction chatter" not in claude.invocations[0]["prompt"]


def test_failed_or_reused_new_session_never_replaces_active_generation(
    tmp_path: Path, writable_project: ProjectDefinition
):
    for name, adapter_type, expected_reason in (
        ("failed", FailedSecondNewAdapter, "provider_invocation_failed"),
        ("reused", ReusedSecondNewAdapter, "provider_session_not_new"),
    ):
        codex = adapter_type("codex", [
            ("planning-propose", sentinel("continue", "Plan")),
            ("planning-revise", sentinel("human", "Revised")),
        ])
        claude = SessionAdapter("claude", [("planning-review", sentinel("continue", "Finding"))])
        app = make_cycle(tmp_path / name, writable_project, codex, claude)
        run_id = app.create_run(b"Task", "test", run_mode="step")
        app.run_to_stop(run_id)
        app.continue_step(run_id)
        app.set_next_turn_override(run_id, session_action="new")
        state = app.continue_step(run_id)
        planner = state["cycles"][0]["sessions"]["planner"]
        assert state["pending_human_decision"] == expected_reason
        assert planner["active_session_id"] == "codex-session-1"
        assert planner["active_generation"] == 1
        assert len(planner["history"]) == 1 and planner["history"][0]["status"] == "active"
        assert state["turns"][-1]["session_promoted"] is False


def test_unknown_inflight_can_be_abandoned_restarted_or_cancelled(tmp_path: Path, writable_project: ProjectDefinition):
    codex = SessionAdapter("codex", [
        ("planning-propose", sentinel("continue", "Plan")),
        ("planning-revise", sentinel("human", "Recovered revision")),
    ])
    claude = SessionAdapter("claude", [("planning-review", sentinel("continue", "Finding"))])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Task", "test", run_mode="step")
    app.run_to_stop(run_id)
    app.continue_step(run_id)
    state = app.state(run_id)
    state["status"] = "running"
    state["pending_human_decision"] = None
    state["inflight"] = {
        "stage": "planning-revise",
        "session_slot": "planner",
        "session_action": "continue",
        "session_id": "codex-session-1",
        "started_at": "2026-07-12T12:00:00Z",
    }
    app._save(run_id, state)
    paused = app.advance(run_id)
    assert paused["pending_human_decision"] == "unknown_provider_invocation"
    recovered = app.decide(run_id, "yes")
    assert recovered["pending_human_decision"] == "provider_requested_human"
    assert recovered["inflight"] is None and len(recovered["abandoned_invocations"]) == 1
    assert codex.invocations[-1]["session_action"] == "new"
    assert codex.invocations[-1]["session_id"] == "codex-session-2"
    assert b"Recovery context" in codex.invocations[-1]["prompt"]
    assert any(event["kind"] == "provider.invocation.abandoned" for event in recovered["events"])

    cancel_app = make_cycle(tmp_path / "cancel", writable_project, SessionAdapter("codex", []), SessionAdapter("claude", []))
    cancel_id = cancel_app.create_run(b"Task", "test")
    cancel_state = cancel_app.state(cancel_id)
    cancel_state["status"] = "running"
    cancel_state["inflight"] = {"stage": "planning-propose", "started_at": "2026-07-12T12:00:00Z"}
    cancel_app._save(cancel_id, cancel_state)
    cancel_app.advance(cancel_id)
    cancelled = cancel_app.decide(cancel_id, "no")
    assert cancelled["status"] == "cancelled" and cancelled["inflight"] is None
    assert cancel_app.cleanup_worktree(cancel_id)["execution_worktree_removed"] is True


def test_public_recovery_reenters_running_run_and_surfaces_stale_inflight(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [("planning-propose", sentinel("human", "Recovered"))])
    app = make_cycle(tmp_path / "clean", writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    interrupted = app.state(run_id)
    interrupted["status"] = "running"
    app._save(run_id, interrupted)
    recovered = app.recover_run(run_id)
    assert recovered["pending_human_decision"] == "provider_requested_human"
    assert codex.invocations[0]["route"] == "planning-propose"
    assert any(event["kind"] == "run.recovery.started" for event in recovered["events"])

    stale_app = make_cycle(
        tmp_path / "stale",
        writable_project,
        SessionAdapter("codex", []),
        SessionAdapter("claude", []),
    )
    stale_id = stale_app.create_run(b"Task", "test")
    stale = stale_app.state(stale_id)
    stale["status"] = "running"
    stale["inflight"] = {
        "stage": "planning-propose",
        "session_slot": "planner",
        "session_action": "new",
        "session_id": None,
        "started_at": "2026-07-12T12:00:00Z",
    }
    stale_app._save(stale_id, stale)
    surfaced = stale_app.recover_run(stale_id)
    assert surfaced["status"] == "paused"
    assert surfaced["pending_human_decision"] == "unknown_provider_invocation"
    assert surfaced["inflight"]["stage"] == "planning-propose"


def test_validation_crash_journal_requires_explicit_inspection_before_any_rerun(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    writable_project: ProjectDefinition,
):
    import toledo_orchestrator.cycle as cycle_module

    validation_project = replace(
        writable_project,
        validations=(ValidationDefinition(
            "host-check", 'python -c "print(\'safe\')"', "local", required=True
        ),),
        allow_no_validations=False,
        validation_requires_approval=False,
    )
    codex = SessionAdapter("codex", [
        ("planning-propose", sentinel("continue", "Plan")),
        ("implementation", sentinel("continue", "Build")),
        ("implementation-repair", sentinel("human", "Inspect before rerun")),
    ], writer=True)
    claude = SessionAdapter("claude", [("planning-review", sentinel("ready", "Plan ready"))])
    app = make_cycle(tmp_path, validation_project, codex, claude)
    original_runner = cycle_module.run_project_validations

    def crash_after_start(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated process loss during host validation")

    monkeypatch.setattr(cycle_module, "run_project_validations", crash_after_start)
    run_id = app.create_run(b"Task", "test")
    with pytest.raises(RuntimeError, match="simulated process loss"):
        app.run_to_stop(run_id)
    on_disk = app.state(run_id)
    assert on_disk["status"] == "running"
    assert on_disk["inflight"] is None
    assert on_disk["validation_inflight"]["commands"][0]["id"] == "host-check"

    monkeypatch.setattr(cycle_module, "run_project_validations", original_runner)
    surfaced = app.recover_run(run_id)
    assert surfaced["pending_human_decision"] == "unknown_validation_execution"
    invocation_count = len(codex.invocations)
    repaired = app.decide(run_id, "yes")
    assert repaired["pending_human_decision"] == "provider_requested_human"
    assert len(codex.invocations) == invocation_count + 1
    assert codex.invocations[-1]["route"] == "implementation-repair"
    assert b"Validation recovery context" in codex.invocations[-1]["prompt"]
    assert repaired["validation_inflight"] is None
    assert repaired["abandoned_validations"][-1]["execution_id"] == on_disk["validation_inflight"]["execution_id"]


def test_workflow_rejects_prompt_namespace_and_sealed_type_path_escape():
    bad_namespace = load_workflows()["continuous-development"].snapshot()
    bad_namespace["id"] = "bad-namespace"
    bad_namespace["prompt_namespaces"] = ["../../outside"]
    with pytest.raises(ValueError, match="prompt_namespaces"):
        WorkflowDefinition.from_value(bad_namespace)

    bad_seal = load_workflows()["continuous-development"].snapshot()
    bad_seal["id"] = "bad-seal"
    bad_seal["stages"]["planning-review"]["transitions"]["ready"] = "@seal:../escape:implementation"
    with pytest.raises(ValueError, match="invalid seal transition"):
        WorkflowDefinition.from_value(bad_seal)

    bad_round = load_workflows()["continuous-development"].snapshot()
    bad_round["id"] = "bad-round"
    bad_round["stages"]["planning-review"]["round"]["directive"] = "ready"
    with pytest.raises(ValueError, match="round directive must target a concrete stage"):
        WorkflowDefinition.from_value(bad_round)

    bad_slot = load_workflows()["continuous-development"].snapshot()
    bad_slot["id"] = "bad-slot"
    bad_slot["stages"]["next-task"]["profile"] = "codex-planning"
    with pytest.raises(ValueError, match="changes provider"):
        WorkflowDefinition.from_value(bad_slot)

    bad_context = load_workflows()["continuous-development"].snapshot()
    bad_context["id"] = "bad-context"
    bad_context["stages"]["implementation"]["context"] = ["approved_handof"]
    with pytest.raises(ValueError, match="unsupported context token"):
        WorkflowDefinition.from_value(bad_context)

    bad_direction_condition = load_workflows()["continuous-development"].snapshot()
    bad_direction_condition["id"] = "bad-direction-condition"
    bad_direction_condition["stages"]["planning-review"]["direction"] = {
        "second_guess": "direction-closure.md"
    }
    with pytest.raises(ValueError, match="unsupported direction condition second_guess"):
        WorkflowDefinition.from_value(bad_direction_condition)

    bad_direction_path = load_workflows()["continuous-development"].snapshot()
    bad_direction_path["id"] = "bad-direction-path"
    bad_direction_path["stages"]["planning-review"]["direction"] = {
        "revisit": "../outside.md"
    }
    with pytest.raises(ValueError, match="invalid direction fragment"):
        WorkflowDefinition.from_value(bad_direction_path)


def test_create_run_rejects_dirty_source_before_creating_run(tmp_path: Path, writable_project: ProjectDefinition):
    (writable_project.root / "AGENTS.md").write_text("# Instructions\nedited but uncommitted\n", encoding="utf-8")
    app = make_cycle(tmp_path, writable_project, SessionAdapter("codex", []), SessionAdapter("claude", []))
    with pytest.raises(ValueError, match="must be clean"):
        app.create_run(b"Task", "test")
    assert not app.runs_dir.exists() or not list(app.runs_dir.iterdir())
    assert not list((tmp_path / "runtime" / "worktrees").glob("*"))


def test_project_status_probe_avoids_optional_locks_and_preserves_git_error(
    monkeypatch: pytest.MonkeyPatch,
    writable_project: ProjectDefinition,
):
    from toledo_orchestrator import project as project_module

    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "branch" in command:
            return subprocess.CompletedProcess(command, 0, stdout="main\n", stderr="")
        assert command[1] == "--no-optional-locks"
        return subprocess.CompletedProcess(
            command,
            128,
            stdout="",
            stderr="fatal: unable to read the index",
        )

    monkeypatch.setattr(project_module.subprocess, "run", fake_run)

    branch, dirty, error = writable_project.git_context()

    assert branch == "main"
    assert dirty is None
    assert error == "git status failed: fatal: unable to read the index"
    assert len(commands) == 2


def test_create_run_surfaces_source_checkout_probe_failure(
    tmp_path: Path,
    writable_project: ProjectDefinition,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        ProjectDefinition,
        "git_context",
        lambda self: ("main", None, "git status failed: fatal: unable to read the index"),
    )
    app = make_cycle(tmp_path, writable_project, SessionAdapter("codex", []), SessionAdapter("claude", []))

    with pytest.raises(ValueError, match="fatal: unable to read the index"):
        app.create_run(b"Task", "test")
    assert not app.runs_dir.exists() or not list(app.runs_dir.iterdir())


def test_create_run_ignores_untracked_files_in_source_checkout(tmp_path: Path, writable_project: ProjectDefinition):
    # Untracked notes never reach the isolated worktree and the accepted commit
    # lands on the run branch, so they must not block a continuous run.
    (writable_project.root / "research-notes.md").write_text("scratch\n", encoding="utf-8")
    codex = SessionAdapter("codex", [("planning-propose", sentinel("human", "Pause immediately"))])
    app = make_cycle(tmp_path, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    state = app.state(run_id)
    assert state["status"] == "created"
    assert not (Path(state["execution_worktree"]) / "research-notes.md").exists()


@pytest.mark.parametrize("drift", ["branch", "head"])
def test_execution_worktree_identity_drift_pauses_before_provider(
    tmp_path: Path, writable_project: ProjectDefinition, drift: str
):
    codex = SessionAdapter("codex", [("planning-propose", sentinel("human", "unused"))])
    app = make_cycle(tmp_path / drift, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    worktree = Path(app.state(run_id)["execution_worktree"])
    if drift == "branch":
        subprocess.run(["git", "-C", str(worktree), "switch", "-c", "unexpected"], check=True, capture_output=True)
    else:
        subprocess.run(
            [
                "git", "-C", str(worktree), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "--allow-empty", "-m", "unexpected",
            ],
            check=True,
            capture_output=True,
        )
    state = app.run_to_stop(run_id)
    assert state["pending_human_decision"] == "execution_worktree_identity_changed"
    assert ("branch_changed" if drift == "branch" else "revision_changed") in state["errors"][-1]
    assert codex.invocations == [] and state["turns"] == []


@pytest.mark.parametrize(
    "command",
    [
        'python -c "from pathlib import Path; assert Path(\'source.md\').is_file()"',
        "python -m pytest -q",
        "python interview_prep/verify_package.py",
        "npm test",
        "docker compose config",
        "curl http://127.0.0.1:8765/health",
    ],
)
def test_routine_validation_commands_do_not_require_approval(command: str):
    approval = ValidationDefinition("routine", command, "local").approval()

    assert approval == {
        "required": False,
        "risk": "routine",
        "summary": "Routine project check; no operator approval is needed.",
        "reasons": [],
    }


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("Remove-Item -Recurse build", "delete or irreversibly replace"),
        ('python -c "Path(\'marker\').write_text(\'x\')"', "create or change files"),
        ("python -m pip install package", "install or update software"),
        ("sudo systemctl restart relay", "administrator access"),
        ("docker compose up -d", "containers or infrastructure"),
        ("git push origin main", "external system"),
        ("curl -X POST https://example.invalid/deploy", "external system"),
    ],
)
def test_only_clearly_consequential_validation_commands_require_approval(command: str, reason: str):
    approval = ValidationDefinition("consequential", command, "local").approval()

    assert approval["required"] is True
    assert approval["risk"] == "high"
    assert approval["summary"] == "This check can affect more than the isolated working copy."
    assert any(reason in item for item in approval["reasons"])


def test_legacy_pending_validation_gets_current_risk_projection(
    tmp_path: Path, writable_project: ProjectDefinition
):
    project = replace(writable_project, validation_requires_approval=True)
    app = make_cycle(
        tmp_path,
        project,
        SessionAdapter("codex", []),
        SessionAdapter("claude", []),
    )
    run_id = app.create_run(b"Task", "test")
    state = app.state(run_id)
    state["pending_validation"] = {
        "commands": [
            {"id": "routine", "command": "python -m pytest -q", "environment": "local"},
            {"id": "publish", "command": "git push origin main", "environment": "local"},
        ]
    }
    state["pending_human_decision"] = "validation_execution_approval"
    app._save(run_id, state)

    projected = app.state(run_id)

    routine, publish = projected["pending_validation"]["commands"]
    assert routine["approval"]["required"] is False
    assert routine["approval"]["legacy_projection"] is True
    assert publish["approval"]["required"] is True
    assert publish["approval"]["legacy_projection"] is True


def test_routine_validation_runs_without_interrupting_for_approval(
    tmp_path: Path, writable_project: ProjectDefinition
):
    command = (
        f'"{sys.executable}" -c "from pathlib import Path; '
        "assert Path('AGENTS.md').is_file()\""
    )
    project = replace(
        writable_project,
        validations=(ValidationDefinition("routine-local", command, "local"),),
        allow_no_validations=False,
        validation_requires_approval=True,
    )
    app = make_cycle(
        tmp_path,
        project,
        SessionAdapter("codex", [
            ("planning-propose", sentinel("continue", "Plan")),
            ("implementation", sentinel("continue", "Built")),
        ], writer=True),
        SessionAdapter("claude", [
            ("planning-review", sentinel("ready", "Plan ready")),
            ("implementation-review", sentinel("human", "Review pause")),
        ]),
    )

    state = app.run_to_stop(app.create_run(b"Task", "test"))

    assert state["pending_human_decision"] == "provider_requested_human"
    assert state["validations"]["routine-local"]["state"] == "passed"
    assert not any(
        event.get("reason") == "validation_execution_approval"
        for event in state["events"]
    )


def test_high_risk_validation_requires_explicit_approval_and_no_cancels(
    tmp_path: Path, writable_project: ProjectDefinition
):
    marker = tmp_path / "validation-ran.txt"
    command = f'"{sys.executable}" -c "from pathlib import Path; Path({str(marker)!r}).write_text(\'ran\', encoding=\'utf-8\')"'
    project = replace(
        writable_project,
        validations=(ValidationDefinition("safe-local", command, "local"),),
        allow_no_validations=False,
        validation_requires_approval=True,
    )
    codex = SessionAdapter("codex", [
        ("planning-propose", sentinel("continue", "Plan")),
        ("implementation", sentinel("continue", "Built")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", sentinel("ready", "Plan ready")),
        ("implementation-review", sentinel("human", "Stop after validation")),
    ])
    app = make_cycle(tmp_path, project, codex, claude)
    run_id = app.create_run(
        b"Task",
        "test",
        continuous_loop_enabled=True,
        continuous_loop_cycles=3,
    )
    paused = app.run_to_stop(run_id)
    assert paused["pending_human_decision"] == "validation_execution_approval"
    assert paused["continuous_loop"]["status"] == "running"
    pending_command = paused["pending_validation"]["commands"][0]
    assert not marker.exists() and pending_command["command"] == command
    assert pending_command["approval"]["required"] is True
    assert pending_command["approval"]["risk"] == "high"
    assert pending_command["approval"]["reasons"] == ["It can create or change files while it runs."]
    resumed = app.decide(run_id, "yes")
    assert marker.read_text(encoding="utf-8") == "ran"
    assert resumed["validations"]["safe-local"]["state"] == "passed"
    assert resumed["pending_human_decision"] == "provider_requested_human"
    assert [item["route"] for item in codex.invocations].count("implementation") == 1

    no_marker = tmp_path / "no-validation.txt"
    no_command = f'"{sys.executable}" -c "from pathlib import Path; Path({str(no_marker)!r}).write_text(\'ran\')"'
    no_project = replace(project, validations=(ValidationDefinition("safe-local", no_command, "local"),))
    no_app = make_cycle(
        tmp_path / "no",
        no_project,
        SessionAdapter("codex", [
            ("planning-propose", sentinel("continue", "Plan")),
            ("implementation", sentinel("continue", "Built")),
        ], writer=True),
        SessionAdapter("claude", [("planning-review", sentinel("ready", "Plan ready"))]),
    )
    no_id = no_app.create_run(b"Task", "test")
    no_app.run_to_stop(no_id)
    cancelled = no_app.decide(no_id, "no")
    assert cancelled["status"] == "cancelled" and not no_marker.exists()


def _remote_validation_run(
    tmp_path: Path, writable_project: ProjectDefinition
) -> tuple[CycleOrchestrator, str, dict[str, Any]]:
    project = replace(
        writable_project,
        validations=(
            ValidationDefinition("local", f'"{sys.executable}" -c "print(\'ok\')"', "local"),
            ValidationDefinition("remote", "docker compose config", "vps"),
        ),
        allow_no_validations=False,
        validation_requires_approval=False,
    )
    codex = SessionAdapter("codex", [
        ("planning-propose", sentinel("continue", "Plan")),
        ("implementation", sentinel("continue", "Built")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", sentinel("ready", "Plan ready")),
        ("implementation-review", sentinel("ready", "Implementation ready")),
        ("next-task", sentinel("human", "Next task")),
    ])
    app = make_cycle(tmp_path, project, codex, claude)
    run_id = app.create_run(b"Task", "test")
    state = app.run_to_stop(run_id)
    assert state["pending_human_decision"] == "validation_receipt_required"
    return app, run_id, state


def _write_remote_receipt(tmp_path: Path, state: dict[str, Any], **overrides: Any) -> Path:
    stdout = tmp_path / "remote.stdout"
    stderr = tmp_path / "remote.stderr"
    stdout.write_bytes(b"remote ok\n")
    stderr.write_bytes(b"")
    receipt = {
        "validation_id": "remote",
        "command": "docker compose config",
        "source_revision": state["working_revision"],
        "patch_sha256": state["current_implementation_evidence"]["patch"]["sha256"],
        "environment": "vps",
        "host": "test-vps",
        "started_at": "2026-07-12T12:00:00Z",
        "finished_at": "2026-07-12T12:00:01Z",
        "exit_code": 0,
        "stdout_path": str(stdout),
        "stderr_path": str(stderr),
        "stdout_sha256": sha256(stdout.read_bytes()),
        "stderr_sha256": sha256(stderr.read_bytes()),
    }
    receipt.update(overrides)
    path = tmp_path / "remote-receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


def test_required_remote_receipt_is_patch_bound_and_completes_acceptance(
    tmp_path: Path, writable_project: ProjectDefinition
):
    app, run_id, state = _remote_validation_run(tmp_path, writable_project)
    with pytest.raises(ValueError, match="patch mismatch"):
        app.attach_receipt(run_id, _write_remote_receipt(tmp_path, state, patch_sha256="wrong"))
    unchanged = app.state(run_id)
    assert unchanged["validations"]["remote"]["state"] == "pending_remote"
    accepted = app.attach_receipt(run_id, _write_remote_receipt(tmp_path, state))
    assert accepted["validations"]["remote"]["state"] == "passed"
    assert accepted["working_revision"] != accepted["source_revision"]
    assert accepted["completion_receipt"]
    assert accepted["pending_human_decision"] == "next_task_approval"


def test_failed_required_remote_receipt_routes_to_repair_without_committing(
    tmp_path: Path, writable_project: ProjectDefinition
):
    app, run_id, state = _remote_validation_run(tmp_path, writable_project)
    # Supply the repair response after the receipt sends the run back to session C.
    codex = app.adapters["codex"]
    assert isinstance(codex, SessionAdapter)
    codex.scripted = iter([("implementation-repair", sentinel("human", "Repair needed"))])
    failed = app.attach_receipt(run_id, _write_remote_receipt(tmp_path, state, exit_code=1))
    assert failed["working_revision"] == failed["source_revision"]
    assert failed["completion_receipt"] is None
    assert failed["validations"]["remote"]["state"] == "failed"
    assert failed["pending_human_decision"] == "provider_requested_human"


def test_run_uses_sealed_prompt_after_package_prompt_changes(
    tmp_path: Path, writable_project: ProjectDefinition, monkeypatch: pytest.MonkeyPatch
):
    import toledo_orchestrator.cycle as cycle_module

    package = tmp_path / "package"
    package.mkdir()
    shutil.copytree(Path(cycle_module.__file__).with_name("prompts"), package / "prompts")
    fake_module = package / "cycle.py"
    fake_module.write_text("# fixture\n", encoding="utf-8")
    monkeypatch.setattr(cycle_module, "__file__", str(fake_module))
    codex = SessionAdapter("codex", [("planning-propose", sentinel("human", "Plan"))])
    app = make_cycle(tmp_path, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    sealed = app.state(run_id)["prompt_library"]["planning-kickoff.md"]
    (package / "prompts" / "planning-kickoff.md").write_text("MUTATED PROMPT\n", encoding="utf-8")
    state = app.run_to_stop(run_id)
    assert b"MUTATED PROMPT" not in codex.invocations[0]["prompt"]
    prompt_bytes = app.artifact(run_id, sealed["path"])
    assert sha256(prompt_bytes) == sealed["sha256"]
    assert state["pending_human_decision"] == "provider_requested_human"


def test_default_relay_prompts_preserve_continuity_and_truthful_evidence_states():
    import toledo_orchestrator.cycle as cycle_module

    prompts = Path(cycle_module.__file__).with_name("prompts")
    law = (prompts / "orchestrator-law.md").read_text(encoding="utf-8")
    plan = (prompts / "planning-kickoff.md").read_text(encoding="utf-8")
    review = (prompts / "implementation-review.md").read_text(encoding="utf-8")
    next_task = (prompts / "next-task.md").read_text(encoding="utf-8")

    assert "A component archive is not a repository archive" in law
    assert "implemented" in law and "operationally verified" in law and "observed" in law
    assert "continuity ledger" in plan
    assert "deterministic invariant or test" in plan
    assert "Do not choose the nicer number" in review
    assert "privacy/retention classification" in review
    assert "baseline debt discovered but not caused by this cycle" in next_task
    assert "original-objective" in next_task
    assert "required-proof" in next_task
    assert "new-objective" in next_task


def test_specialized_workflows_use_distinct_evidence_contracts():
    import toledo_orchestrator.cycle as cycle_module

    prompts = Path(cycle_module.__file__).with_name("prompts")
    workflows = load_workflows()
    assert workflows["strategy-council"].profiles["strategy-planner"].effort == "xhigh"
    assert workflows["strategy-council"].profiles["strategy-reviewer"].model == "claude-sonnet-5"
    assert workflows["test-proof-gate"].profiles["test-runner"].effort == "medium"
    assert workflows["ui-studio"].profiles["ui-planner"].model == "gpt-5.6-sol"
    assert "rejected alternative" in (prompts / "strategy-council-planner.md").read_text(encoding="utf-8")
    assert "baseline" in (prompts / "test-proof-planner.md").read_text(encoding="utf-8")
    assert "desktop and mobile" in (prompts / "ui-studio-builder.md").read_text(encoding="utf-8")


def test_workflow_rejects_workspace_write_profile_on_planning_stage():
    value = load_workflows()["continuous-development"].snapshot()
    value["profiles"]["codex-planning"]["permission"] = "workspace-write"
    with pytest.raises(ValueError, match="non-implementation stage planning-propose"):
        WorkflowDefinition.from_value(value)


def test_run_lock_excludes_another_process_without_state_corruption(tmp_path: Path, writable_project: ProjectDefinition):
    app = make_cycle(tmp_path, writable_project, SessionAdapter("codex", []), SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    run_dir = app._run_dir(run_id)
    before = (run_dir / "run.json").read_bytes()
    source_root = Path(__file__).parents[1] / "src"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from toledo_orchestrator.locking import run_lock\n"
        "with run_lock(Path(sys.argv[1])):\n"
        " print('LOCKED', flush=True)\n"
        " sys.stdin.read(1)\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source_root) + os.pathsep + environment.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(run_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        assert process.stdout and process.stdout.readline().strip() == "LOCKED"
        with pytest.raises(ValueError, match="active operation"):
            app.set_next_turn_override(run_id, session_action="new")
        assert (run_dir / "run.json").read_bytes() == before
    finally:
        if process.stdin:
            process.stdin.write("x")
            process.stdin.flush()
        process.wait(timeout=5)
    state = app.set_next_turn_override(run_id, session_action="new")
    assert state["next_turn_override"]["session_action"] == "new"


def test_registered_artifact_hashes_are_enforced_before_transport(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [("planning-propose", sentinel("human", "unused"))])
    app = make_cycle(tmp_path, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(b"Task", "test")
    state = app.state(run_id)
    prompt_record = state["prompt_library"]["planning-kickoff.md"]
    (app._run_dir(run_id) / prompt_record["path"]).write_text("tampered\n", encoding="utf-8")
    paused = app.run_to_stop(run_id)
    assert paused["pending_human_decision"] == "artifact_integrity_failed"
    assert "hash mismatch" in paused["errors"][-1]
    assert codex.invocations == []


def test_oversized_implementation_patch_uses_verified_out_of_line_context(
    tmp_path: Path, writable_project: ProjectDefinition
):
    app = make_cycle(
        tmp_path,
        writable_project,
        SessionAdapter("codex", []),
        SessionAdapter("claude", []),
    )
    run_id = app.create_run(b"Task", "test")
    state = app.state(run_id)
    relative = "evidence/oversized.implementation.patch"
    patch = b"diff --git a/x b/x\n" + b"+x\n" * 700_000
    digest = atomic_write(app._run_dir(run_id) / relative, patch)
    evidence = {
        "changed_paths": [f"src/file-{index}.py" for index in range(250)],
        "file_hashes": [{"path": f"src/file-{index}.py", "sha256": "a" * 64} for index in range(75)],
        "patch": {"path": relative, "sha256": digest},
    }
    state["current_implementation_evidence"] = evidence

    context = app._implementation_evidence_context(state, evidence)

    assert "verified, stored out of line" in context
    assert "No content was summarized or discarded in storage" in context
    assert "git diff --binary HEAD --" in context
    assert '"omitted": 50' in context
    assert sha256((app._run_dir(run_id) / relative).read_bytes()) == digest


def test_continuous_loop_repacks_legacy_oversized_evidence_with_untracked_exclusions(
    tmp_path: Path, writable_project: ProjectDefinition
):
    project = replace(writable_project, evidence_exclude_paths=("scratch",))
    app = make_cycle(
        tmp_path,
        project,
        SessionAdapter("codex", []),
        SessionAdapter("claude", []),
    )
    run_id = app.create_run(
        b"Task",
        "test",
        continuous_loop_enabled=True,
        continuous_loop_cycles=3,
    )
    state = app.state(run_id)
    worktree = Path(state["execution_worktree"])
    (worktree / "source.py").write_text("meaningful = True\n", encoding="utf-8")
    (worktree / "scratch").mkdir()
    (worktree / "scratch" / "generated.txt").write_bytes(b"x" * 2_100_000)
    old = seal_worktree_evidence(
        app._run_dir(run_id),
        1,
        1,
        collect_worktree_evidence(worktree),
    )
    old["validations"] = {"tests": {"state": "passed"}}
    state["current_implementation_evidence"] = old
    state["current_stage"] = "implementation-review"
    state["status"] = "paused"
    state["pending_human_decision"] = "artifact_integrity_failed"
    state["errors"].append(
        f"artifact exceeds the prompt transport limit: {old['patch']['path']}:{len((app._run_dir(run_id) / old['patch']['path']).read_bytes())}"
    )
    app._save(run_id, state)

    assert app.continuous_loop_auto_resume_available(run_id)
    assert app._auto_continue_continuous_loop(app.state(run_id))
    repaired = app.state(run_id)
    assert repaired["status"] == "running"
    assert repaired["pending_human_decision"] is None
    assert repaired["current_implementation_evidence"]["changed_paths"] == ["source.py"]
    assert repaired["current_implementation_evidence"]["excluded_untracked"]["count"] == 1
    assert repaired["current_implementation_evidence"]["validations"]["tests"]["state"] == "passed"


def test_validation_definitions_reject_traversal_duplicates_and_empty_commands(
    writable_project: ProjectDefinition
):
    with pytest.raises(ValueError, match="invalid validation id"):
        replace(
            writable_project,
            validations=(ValidationDefinition("../../escape", "echo x", "local"),),
            allow_no_validations=False,
        )
    with pytest.raises(ValueError, match="duplicate validation id"):
        replace(
            writable_project,
            validations=(
                ValidationDefinition("tests", "echo one", "local"),
                ValidationDefinition("tests", "echo two", "vps"),
            ),
            allow_no_validations=False,
        )
    with pytest.raises(ValueError, match="cannot be empty"):
        replace(
            writable_project,
            validations=(ValidationDefinition("tests", "  ", "local"),),
            allow_no_validations=False,
        )
    with pytest.raises(ValueError, match="escapes root"):
        replace(writable_project, evidence_exclude_paths=("../outside",))
    with pytest.raises(ValueError, match="must name a repository subpath"):
        replace(writable_project, evidence_exclude_paths=(".",))


def test_implementation_human_pauses_before_host_validation(tmp_path: Path, writable_project: ProjectDefinition):
    marker = tmp_path / "must-not-run.txt"
    command = f'"{sys.executable}" -c "from pathlib import Path; Path({str(marker)!r}).write_text(\'ran\')"'
    project = replace(
        writable_project,
        validations=(ValidationDefinition("local", command, "local"),),
        allow_no_validations=False,
        validation_requires_approval=False,
    )
    app = make_cycle(
        tmp_path,
        project,
        SessionAdapter("codex", [
            ("planning-propose", sentinel("continue", "Plan")),
            ("implementation", sentinel("human", "Need a decision before finishing")),
        ], writer=True),
        SessionAdapter("claude", [("planning-review", sentinel("ready", "Plan ready"))]),
    )
    state = app.run_to_stop(app.create_run(b"Task", "test"))
    assert state["pending_human_decision"] == "provider_requested_human"
    assert not marker.exists() and state["validations"] == {}


def test_acceptance_commit_failure_is_persisted_as_pause(
    tmp_path: Path, writable_project: ProjectDefinition, monkeypatch: pytest.MonkeyPatch
):
    import toledo_orchestrator.cycle as cycle_module

    def fail_commit(*args: Any, **kwargs: Any) -> str:
        raise ValueError("fixture commit failure")

    monkeypatch.setattr(cycle_module, "commit_accepted_changes", fail_commit)
    app = make_cycle(
        tmp_path,
        writable_project,
        SessionAdapter("codex", [
            ("planning-propose", sentinel("continue", "Plan")),
            ("implementation", sentinel("continue", "Build")),
        ], writer=True),
        SessionAdapter("claude", [
            ("planning-review", sentinel("ready", "Plan ready")),
            ("implementation-review", sentinel("ready", "Build ready")),
        ]),
    )
    state = app.run_to_stop(app.create_run(b"Task", "test"))
    assert state["status"] == "paused"
    assert state["pending_human_decision"] == "acceptance_commit_failed"
    assert "fixture commit failure" in state["errors"][-1]


def test_committed_but_unsaved_acceptance_is_reconciled_from_journal(
    tmp_path: Path, writable_project: ProjectDefinition, monkeypatch: pytest.MonkeyPatch
):
    import toledo_orchestrator.cycle as cycle_module

    original_commit = cycle_module.commit_accepted_changes

    def commit_then_crash(*args: Any, **kwargs: Any) -> str:
        original_commit(*args, **kwargs)
        raise RuntimeError("simulated controller crash after git commit")

    monkeypatch.setattr(cycle_module, "commit_accepted_changes", commit_then_crash)
    app = make_cycle(
        tmp_path,
        writable_project,
        SessionAdapter("codex", [
            ("planning-propose", sentinel("continue", "Plan")),
            ("implementation", sentinel("continue", "Build")),
        ], writer=True),
        SessionAdapter("claude", [
            ("planning-review", sentinel("ready", "Plan ready")),
            ("implementation-review", sentinel("ready", "Build ready")),
        ]),
    )
    run_id = app.create_run(b"Task", "test")
    with pytest.raises(RuntimeError, match="simulated controller crash"):
        app.run_to_stop(run_id)
    uncertain = app.state(run_id)
    assert uncertain["pending_commit"] and uncertain["completion_receipt"] is None
    monkeypatch.setattr(cycle_module, "commit_accepted_changes", original_commit)
    reconciled = app.advance(run_id)
    assert reconciled["pending_commit"] is None
    assert reconciled["completion_receipt"]
    assert reconciled["current_stage"] == "next-task"


def test_situational_direction_selects_configured_fragments(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [
        ("planning-propose", response("continue", "Plan")),
        ("planning-revise", response("continue", "Revised plan")),
        ("implementation", response("continue", "Implementation")),
        ("implementation-repair", response("continue", "Repair")),
        ("planning-propose", response("continue", "Cycle two plan")),
        ("implementation", response("continue", "Cycle two build")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", response("continue", "First review finding")),
        ("planning-review", response("ready", "Plan approved")),
        ("implementation-review", response("continue", "Defect found")),
        ("implementation-review", response("ready", "Implementation accepted")),
        ("next-task", response("human", "Proposed next task")),
        ("planning-review", response("ready", "Cycle two plan ready")),
        ("implementation-review", response("ready", "Cycle two accepted")),
        ("next-task", response("human", "Cycle three proposal")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Build the thing", "test")
    state = app.run_to_stop(run_id)

    def stage_turns(name):
        return [t for t in state["turns"] if t["stage"] == name and t["substantive"] and not t["correction"]]

    # First visit in cycle 1 stays silent — the stance prompt already carries that language.
    first_ideate = stage_turns("planning-propose")[0]
    assert first_ideate["direction_file"] is None
    assert stage_turns("planning-review")[0]["direction_file"] is None
    assert stage_turns("implementation-review")[0]["direction_file"] is None

    # A revisited reviewer stage emits the closure fragment the workflow maps to it,
    # and it is threaded into the exact transport prompt.
    second_review = stage_turns("planning-review")[1]
    assert second_review["direction_file"] == f"{second_review['id']}.direction.md"
    closure = app.artifact(run_id, f"turns/{second_review['direction_file']}").decode("utf-8")
    assert "do not manufacture objections" in closure
    second_review_prompt = [
        item["prompt"] for item in claude.invocations if item["route"] == "planning-review"
    ][1].decode("utf-8")
    assert "# Orchestrator direction" in second_review_prompt
    assert "do not manufacture objections" in second_review_prompt

    # A revisited audit after a repair emits the recheck fragment, not the closure one.
    second_audit = stage_turns("implementation-review")[1]
    recheck = app.artifact(run_id, f"turns/{second_audit['direction_file']}").decode("utf-8")
    assert "fake/real divergence" in recheck

    # Cycle two's first planner turn emits the later-cycle continuity fragment.
    app.decide(run_id, "yes")
    state = app.state(run_id)
    cycle_two_ideate = next(
        t for t in state["turns"]
        if t["stage"] == "planning-propose" and t["cycle"] == 2 and t["substantive"]
    )
    continuity = app.artifact(run_id, f"turns/{cycle_two_ideate['direction_file']}").decode("utf-8")
    assert "already-accepted branch" in continuity


def test_create_run_round_and_prompt_overrides_bind_to_run_snapshot(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [("planning-propose", sentinel("human", "Pause immediately"))])
    app = make_cycle(tmp_path, writable_project, codex, SessionAdapter("claude", []))
    run_id = app.create_run(
        b"Task",
        "test",
        round_overrides={"planning": 5, "implementation": 1},
        prompt_overrides={"planning-kickoff.md": "# Custom ideation\nDo exactly this.\n"},
    )
    snapshot = app.state(run_id)["workflow_snapshot"]
    assert snapshot["stages"]["planning-review"]["round"]["cap"] == 5
    assert snapshot["stages"]["implementation-review"]["round"]["cap"] == 1
    library = app.state(run_id)["prompt_library"]["planning-kickoff.md"]
    assert library["source"] == "operator-override"
    stored = app.artifact(run_id, library["path"]).decode("utf-8")
    assert stored == "# Custom ideation\nDo exactly this.\n"
    # The configured workflow itself is untouched.
    assert load_workflows()["continuous-development"].stages["planning-review"].round_cap == 3
    app.run_to_stop(run_id)
    planner_prompt = codex.invocations[0]["prompt"].decode("utf-8")
    assert "Do exactly this." in planner_prompt


def test_create_run_rejects_bad_round_and_prompt_overrides(
    tmp_path: Path, writable_project: ProjectDefinition
):
    app = make_cycle(tmp_path, writable_project, SessionAdapter("codex", []), SessionAdapter("claude", []))
    with pytest.raises(ValueError, match="unknown round counter"):
        app.create_run(b"Task", "test", round_overrides={"reviewing": 2})
    with pytest.raises(ValueError, match="between 1 and"):
        app.create_run(b"Task", "test", round_overrides={"planning": 0})
    with pytest.raises(ValueError, match="unknown prompt override"):
        app.create_run(b"Task", "test", prompt_overrides={"orchestrator-law.md": "override the law"})
    with pytest.raises(ValueError, match="cannot be empty"):
        app.create_run(b"Task", "test", prompt_overrides={"planning-kickoff.md": "  "})
    assert not app.runs_dir.exists() or not list(app.runs_dir.iterdir())
