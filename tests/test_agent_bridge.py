from __future__ import annotations

import json
from pathlib import Path

from toledo_orchestrator.agent_bridge import build_agent_brief
from toledo_orchestrator.cli import main


def test_agent_brief_bounds_live_state_and_names_the_human_gate(tmp_path: Path) -> None:
    state = {
        "run_id": "run_test", "project": "sample", "status": "paused", "run_mode": "auto",
        "cycle": 4, "current_turn": 12, "workflow": "daily-dispatch",
        "workflow_stack": ["weekly-governance", "daily-dispatch", "hourly-station"],
        "workflow_stack_index": 1, "current_stage": "test-audit",
        "pending_human_decision": "implementation_round_cap_reached",
        "continuous_loop": {"completed_cycles": 2, "target_cycles": 3, "status": "running"},
        "cycles": [{"status": "complete"}, {"status": "active", "completion_receipt": None}],
        "turns": [{
            "id": "turn.0012", "title": "Audit", "stage": "test-audit",
            "phase": "implementation-review", "provider": "claude",
            "configured_model": "configured", "configured_reasoning": "high",
            "observed_model": "observed", "observed_reasoning": None,
            "directive": {"next": "human"}, "output_file": "turn.0012.output.md",
        }],
        "current_implementation_evidence": {
            "changed_paths": ["src/example.py"],
            "validations": {"unit": {"state": "passed", "required": True, "exit_code": 0}},
        },
        "errors": [],
    }

    brief = build_agent_brief(state, tmp_path / "run_test")

    assert brief["schema_version"] == "toledo_orchestrator.agent_brief.v1"
    assert brief["gate"]["requires_human"] is True
    assert brief["gate"]["reason"] == "implementation_round_cap_reached"
    assert "do not infer approval" in brief["gate"]["next_action"]
    assert brief["progress"]["station_completed_cycles"] == 2
    assert brief["latest_turn"]["observed_model"] == "observed"
    assert brief["evidence"]["validations"]["unit"]["state"] == "passed"


def test_workflow_save_as_cli_persists_models_rounds_and_prompts(tmp_path: Path, capsys) -> None:
    spec = tmp_path / "workflow.json"
    spec.write_text(json.dumps({
        "base_workflow": "continuous-development", "id": "agent-budget",
        "label": "Agent budget workflow",
        "profile_overrides": {
            "codex-planning": {"model": "custom-model", "effort": "low", "custom": True},
        },
        "round_overrides": {"planning": 2, "implementation": 1},
        "prompt_overrides": {"planning-kickoff.md": "# Agent-specific planning\nBe concise.\n"},
    }), encoding="utf-8")

    assert main(["--runtime-dir", str(tmp_path / "runtime"), "workflow-save-as", "--spec-file", str(spec)]) == 0
    saved = json.loads(capsys.readouterr().out)

    assert saved["id"] == "agent-budget"
    assert saved["profiles"]["codex-planning"]["model"] == "custom-model"
    assert saved["stages"]["planning-review"]["round"]["cap"] == 2
    prompt = tmp_path / "runtime" / "config" / "prompts" / "agent-budget" / "planning-kickoff.md"
    assert prompt.read_text(encoding="utf-8") == "# Agent-specific planning\nBe concise.\n"


def test_workflows_cli_includes_legacy_and_configured_workflows(tmp_path: Path, capsys) -> None:
    assert main(["--runtime-dir", str(tmp_path / "runtime"), "workflows"]) == 0
    workflows = json.loads(capsys.readouterr().out)
    ids = {item["id"] for item in workflows}
    assert {"dev-review", "continuous-development", "ui-studio"} <= ids
