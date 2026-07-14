"""Baseline-aware completion, next-turn preview, operator stop, and launch overrides."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from toledo_orchestrator.project import ProjectDefinition, ValidationDefinition
from toledo_orchestrator.validation import failure_signature

from test_cycle import SessionAdapter, make_cycle, response, writable_project  # noqa: F401


def _commit_all(root: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", message],
        check=True,
        capture_output=True,
    )


def _validating_project(writable_project: ProjectDefinition, script: str) -> ProjectDefinition:
    (writable_project.root / "check.py").write_text(script, encoding="utf-8")
    _commit_all(writable_project.root, "add validation check")
    return replace(
        writable_project,
        validations=(ValidationDefinition(id="local-checks", command=f'"{sys.executable}" check.py', environment="local"),),
        allow_no_validations=False,
        validation_requires_approval=False,
    )


def test_failure_signature_is_environment_independent():
    first = failure_signature("FAILED tests/t.py::x - FileNotFoundError: C:\\wt1\\fixture.json\n1 failed", "", 1)
    second = failure_signature("FAILED tests/t.py::x - FileNotFoundError: C:\\wt2\\fixture.json\n1 failed", "", 1)
    assert first["fingerprint"] == second["fingerprint"] and not first["weak"]
    different = failure_signature("FAILED tests/t.py::y - boom", "", 1)
    assert different["fingerprint"] != first["fingerprint"]
    weak = failure_signature("no recognizable failure lines", "", 2)
    assert weak["weak"] and weak["lines"] == ["exit:2"]


def test_unchanged_baseline_failure_pauses_for_closure_and_accepts_with_debt(
    tmp_path: Path, writable_project: ProjectDefinition
):
    # The failure message embeds the working directory, exactly like a
    # missing-fixture pytest failure whose path differs per worktree.
    project = _validating_project(writable_project, (
        "import os, sys\n"
        "sys.stdout.write('FAILED tests/test_math.py::test_fixture - FileNotFoundError: %s\\n'"
        " % os.path.join(os.getcwd(), 'fixture.json'))\n"
        "sys.exit(2)\n"
    ))
    codex = SessionAdapter("codex", [
        ("planning-propose", response("ready", "Plan")),
        ("implementation", response("ready", "Built")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", response("ready", "Approved")),
        ("implementation-review", response("ready", "Implementation accepted")),
        ("next-task", response("human", "Proposed next task")),
    ])
    app = make_cycle(tmp_path, project, codex, claude)
    run_id = app.create_run(b"Build with a known-broken baseline", "test")
    state = app.run_to_stop(run_id)

    assert state["status"] == "paused"
    assert state["pending_human_decision"] == "validation_baseline_failure_decision"
    classification = state["pending_baseline_acceptance"]["classification"]
    assert classification["overall"] == "unchanged_baseline"
    assert classification["failures"]["local-checks"]["match"] == "unchanged_baseline"
    assert state["baseline_validations"]["results"]["local-checks"]["state"] == "failed"
    # The repair loop was not entered for a failure the change did not introduce.
    assert "reviewer_ready_but_validation_failed" not in state["errors"]

    state = app.decide(run_id, "yes")
    assert state["pending_human_decision"] == "next_task_approval"
    assert state["working_revision"] != state["source_revision"]
    debt = [key for key, value in state["artifacts"].items() if value.get("type") == "baseline-debt"]
    assert len(debt) == 1
    receipt = json.loads((app.runs_dir / run_id / debt[0]).read_text(encoding="utf-8"))
    assert receipt["classification"]["overall"] == "unchanged_baseline"
    assert receipt["patch_sha256"]


def test_new_regression_still_routes_to_repair_and_round_cap(
    tmp_path: Path, writable_project: ProjectDefinition
):
    # Baseline (clean worktree) fails one way; once the implementation writes
    # built.txt the failure identity changes, so it is a regression.
    project = _validating_project(writable_project, (
        "import os, sys\n"
        "if os.path.exists('built.txt'):\n"
        "    sys.stdout.write('FAILED tests/test_new.py::test_regression - assert 1 == 2\\n')\n"
        "else:\n"
        "    sys.stdout.write('FAILED tests/test_math.py::test_fixture - missing\\n')\n"
        "sys.exit(1)\n"
    ))
    codex = SessionAdapter("codex", [
        ("planning-propose", response("ready", "Plan")),
        ("implementation", response("ready", "Built")),
        ("implementation-repair", response("ready", "Repair one")),
        ("implementation-repair", response("ready", "Repair two")),
    ], writer=True)
    claude = SessionAdapter("claude", [
        ("planning-review", response("ready", "Approved")),
        ("implementation-review", response("ready", "Looks done")),
        ("implementation-review", response("ready", "Still looks done")),
        ("implementation-review", response("ready", "Still looks done")),
    ])
    app = make_cycle(tmp_path, project, codex, claude)
    run_id = app.create_run(b"Introduce a regression", "test")
    state = app.run_to_stop(run_id)

    assert state["status"] == "paused"
    assert state["pending_human_decision"] == "validation_failed_at_repair_cap"
    assert "reviewer_ready_but_validation_failed" in state["errors"]
    classified = [event for event in state["events"] if event["kind"] == "validation.classified"]
    assert classified
    sealed = json.loads(
        (app.runs_dir / run_id / "events" / f"{classified[-1]['id']}.json").read_text(encoding="utf-8")
    )
    assert sealed["overall"] == "new_regression"


def test_next_turn_preview_describes_stage_profile_session_and_prompt(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [("planning-propose", response("ready", "Initial plan body"))])
    claude = SessionAdapter("claude", [])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Preview me", "test", run_mode="step")
    state = app.run_to_stop(run_id)
    assert state["pending_human_decision"] == "operator_step"

    preview = app.next_turn_preview(run_id)
    assert preview["available"] is True
    assert preview["stage"]["id"] == "planning-review"
    assert preview["profile"]["provider"] == "claude"
    assert preview["profile"]["overridden"] is False
    assert preview["session"]["action"] == "new"
    assert any(item["token"] == "latest:plan" and not item["empty"] for item in preview["inputs"])
    assert "Initial plan body" in preview["prompt"]
    assert any("seal the approved handoff" in item["description"] for item in preview["afterward"])
    assert preview["rounds"] == {"counter": "planning", "used": 0, "cap": 3}

    app.set_next_turn_override(run_id, model="claude-opus-4-8", effort="max", custom=True)
    preview = app.next_turn_preview(run_id)
    assert preview["profile"]["model"] == "claude-opus-4-8"
    assert preview["profile"]["effort"] == "max"
    assert preview["profile"]["overridden"] is True
    # Preview never mutates run state.
    assert app.state(run_id)["current_turn"] == 1


def test_stop_run_records_operator_finish_not_cancellation(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [("planning-propose", response("ready", "Plan"))])
    claude = SessionAdapter("claude", [])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Stop me politely", "test", run_mode="step")
    app.run_to_stop(run_id)

    state = app.stop_run(run_id, b"Good stopping point.")
    assert state["status"] == "stopped"
    assert state["cycles"][0]["status"] == "stopped"
    assert state["decisions"][-1]["choice"] == "stop"
    assert state["events"][-1]["kind"] == "run.stopped"
    with pytest.raises(ValueError):
        app.decide(run_id, "yes")


def test_create_run_profile_overrides_bind_to_snapshot_only(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [("planning-propose", response("ready", "Plan"))])
    claude = SessionAdapter("claude", [])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(
        b"Override the planner for this run",
        "test",
        run_mode="step",
        profile_overrides={"codex-planning": {"model": "gpt-custom", "effort": "high", "custom": True}},
    )
    state = app.run_to_stop(run_id)
    snapshot_profile = state["workflow_snapshot"]["profiles"]["codex-planning"]
    assert snapshot_profile["model"] == "gpt-custom"
    assert snapshot_profile["effort"] == "high"
    assert snapshot_profile["custom"] is True
    assert snapshot_profile["label"] == "gpt-custom · high"
    assert state["turns"][0]["configured_model"] == "gpt-custom"
    # Saved workflow defaults are untouched.
    assert app.workflows["continuous-development"].profiles["codex-planning"].model == "gpt-5.6-sol"
    with pytest.raises(ValueError, match="unknown profile override"):
        app.create_run(b"x", "test", profile_overrides={"nope": {"model": "a", "effort": "b"}})
