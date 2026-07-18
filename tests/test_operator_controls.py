"""Baseline-aware completion, next-turn preview, operator stop, and launch overrides."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from toledo_orchestrator.core import write_text
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

    state = app.decide(run_id, "yes", follow_up="baseline_failure")
    assert state["pending_human_decision"] == "next_task_approval"
    assert state["working_revision"] != state["source_revision"]
    debt = [key for key, value in state["artifacts"].items() if value.get("type") == "baseline-debt"]
    assert len(debt) == 1
    receipt = json.loads((app.runs_dir / run_id / debt[0]).read_text(encoding="utf-8"))
    assert receipt["classification"]["overall"] == "unchanged_baseline"
    assert receipt["patch_sha256"]
    transcript = app.export_run(run_id, plain_text=True, include_diagnostics=True).decode("utf-8")
    assert "Clean-base validation" in transcript
    assert "Implementation validation" in transcript
    assert "FAILED tests/test_math.py::test_fixture" in transcript
    assert "toledo_orchestrator.baseline_debt.v1" in transcript
    assert "toledo_orchestrator.completion.v1" in transcript
    assert state["decisions"][-1]["follow_up"] == "baseline_failure"
    next_task_prompt = claude.invocations[-1]["prompt"]
    assert b"Make the recorded older issue the next task" in next_task_prompt
    assert b"FAILED tests/test_math.py::test_fixture" in next_task_prompt


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


def test_export_is_a_complete_exact_chronological_transcript(
    tmp_path: Path, writable_project: ProjectDefinition
):
    codex = SessionAdapter("codex", [("planning-propose", response("ready", "# Exact plan heading\nExportable plan body"))])
    claude = SessionAdapter("claude", [
        ("planning-review", response("human", "Review needs owner input")),
        ("planning-review", response("human", "Replacement review body")),
    ])
    app = make_cycle(tmp_path, writable_project, codex, claude)
    run_id = app.create_run(b"Export me fully", "test", run_mode="step")
    first_pause = app.run_to_stop(run_id)
    assert first_pause["pending_human_decision"] == "operator_step"
    # The current stage has not run yet, so offering Steer here would be a dead
    # control even though the run contains a prior planner turn.
    unavailable = app.steer_availability(run_id)
    assert unavailable["available"] is False and "no active reviewer provider session" in unavailable["reason"]
    with pytest.raises(ValueError, match="Steer is unavailable"):
        app.steer(run_id, "This must not target the previous stage.")

    review_pause = app.continue_step(run_id, b"Owner one-turn direction, verbatim.")
    assert review_pause["pending_human_decision"] == "provider_requested_human"
    available = app.steer_availability(run_id)
    assert available["available"] is True
    assert available["turn_id"] == review_pause["turns"][-1]["id"]
    assert available["stage"] == "planning-review"
    blocked = app.state(run_id)
    blocked["pending_validation"] = {"commands": ["pytest"], "stage": "planning-review"}
    blocked["pending_human_decision"] = "validation_execution_approval"
    app._save(run_id, blocked)
    unavailable_during_validation = app.steer_availability(run_id)
    assert unavailable_during_validation["available"] is False
    assert "pending deterministic decision" in unavailable_during_validation["reason"]
    blocked["pending_validation"] = None
    blocked["pending_human_decision"] = "provider_requested_human"
    app._save(run_id, blocked)
    app.steer(run_id, "Operator steer note, verbatim.")
    app.stop_run(run_id, b"Final operator stop note, verbatim.")

    diagnostic_secret = "Bearer TEST_DIAGNOSTIC_SECRET"
    with_diagnostic = app.state(run_id)
    first_turn = with_diagnostic["turns"][0]
    first_stderr = app._run_dir(run_id) / "turns" / first_turn["stderr_file"]
    first_turn["stderr_sha256"] = write_text(first_stderr, diagnostic_secret)
    app._save(run_id, with_diagnostic)

    full = app.export_run(run_id).decode("utf-8")
    assert "### Cycle request (exact)" in full
    assert "#### Transport prompt (exact)" in full
    assert "#### Provider response (exact stored provider response)" in full
    assert "Export me fully" in full
    assert "Exportable plan body" in full
    # Exact provider responses retain the process-control fence; the old export
    # silently reduced them to derivative work products.
    assert '{"next":"ready"}' in full
    assert "Owner one-turn direction, verbatim." in full
    assert "Operator steer note, verbatim." in full
    assert "Replacement review body" in full
    assert "Final operator stop note, verbatim." in full
    assert full.index("Export me fully") < full.index("Exportable plan body")
    assert full.index("Owner one-turn direction, verbatim.") < full.index("Review needs owner input")
    assert full.index("Operator steer note, verbatim.") < full.index("Replacement review body")
    assert diagnostic_secret not in full
    assert "Provider stderr (exact sealed text)" not in full
    assert "Lifecycle event log (chronological)" not in full
    assert "Diagnostics: excluded" in full
    assert "Final run state (redacted summary)" in full

    diagnostics = app.export_run(run_id, include_diagnostics=True).decode("utf-8")
    assert diagnostic_secret in diagnostics
    assert "Provider stderr (exact sealed text)" in diagnostics
    assert "Lifecycle event log (chronological)" in diagnostics
    assert "may contain sensitive raw text" in diagnostics

    without = app.export_run(run_id, include_prompts=False).decode("utf-8")
    assert "Transport prompt (exact)" not in without
    assert "Transport prompts: intentionally omitted by prompts=0" in without
    assert "Export me fully" in without  # explicit cycle request remains
    assert "Exportable plan body" in without
    assert "Owner one-turn direction, verbatim." in without
    assert "Final operator stop note, verbatim." in without

    plain = app.export_run(run_id, plain_text=True).decode("utf-8")
    assert "Toledo complete transcript" in plain
    # Wrapper headings are plain text without mutating hashes/headings inside
    # exact provider content.
    assert "# Exact plan heading" in plain
    assert "#Exact plan heading" not in plain
