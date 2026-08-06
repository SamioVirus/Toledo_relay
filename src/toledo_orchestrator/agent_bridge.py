from __future__ import annotations

from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {"complete", "cancelled", "failed", "stopped"}


def _latest_cycle(state: dict[str, Any]) -> dict[str, Any]:
    cycles = state.get("cycles") or []
    return cycles[-1] if cycles else {}


def _latest_turn(state: dict[str, Any]) -> dict[str, Any]:
    turns = state.get("turns") or []
    return turns[-1] if turns else {}


def _validation_summary(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    evidence = state.get("current_implementation_evidence") or {}
    validations = evidence.get("validations") or {}
    return {
        str(key): {
            "state": value.get("state"),
            "environment": value.get("environment"),
            "exit_code": value.get("exit_code"),
            "required": value.get("required"),
        }
        for key, value in validations.items()
        if isinstance(value, dict)
    }


def _next_action(status: str, gate: str | None) -> str:
    if status == "complete":
        return "Read the completion receipt and final turn, export if useful, and report the outcome."
    if status in {"cancelled", "failed", "stopped"}:
        return "Inspect the latest turn and errors, then report the truthful terminal outcome."
    if gate:
        return (
            "Read the latest turn and gate evidence. Ask the user for the required decision; "
            "do not infer approval."
        )
    if status == "paused":
        return "Inspect errors and inflight evidence before deciding whether recovery is safe."
    return "Wait briefly and read agent-brief again; do not start a duplicate operation."


def build_agent_brief(state: dict[str, Any], run_dir: Path | None = None) -> dict[str, Any]:
    """Return a bounded, stable view for an initiating AI agent."""

    status = str(state.get("status") or "unknown")
    gate = str(state.get("pending_human_decision") or "") or None
    latest_cycle = _latest_cycle(state)
    latest_turn = _latest_turn(state)
    loop = state.get("continuous_loop") or {}
    workflow_stack = state.get("workflow_stack") or [state.get("workflow")]
    stack_index = int(state.get("workflow_stack_index") or 0)
    evidence = state.get("current_implementation_evidence") or {}
    cadence = state.get("cadence_backbone") or {}
    latest_output = latest_turn.get("output_file")
    latest_output_path = None
    if run_dir is not None and latest_output:
        latest_output_path = str((run_dir / "turns" / str(latest_output)).resolve())

    return {
        "schema_version": "toledo_orchestrator.agent_brief.v1",
        "run": {
            "run_id": state.get("run_id"),
            "project": state.get("project"),
            "status": status,
            "cycle": state.get("cycle"),
            "current_turn": state.get("current_turn"),
            "run_mode": state.get("run_mode"),
            "terminal": status in TERMINAL_STATUSES,
        },
        "workflow": {
            "current": state.get("workflow"),
            "stack": workflow_stack,
            "stack_index": stack_index,
            "current_stage": state.get("current_stage"),
            "cadence_stations": cadence.get("stations") or [],
        },
        "progress": {
            "completed_cycles_total": sum(
                1 for item in state.get("cycles") or [] if item.get("status") == "complete"
            ),
            "current_cycle_status": latest_cycle.get("status"),
            "station_completed_cycles": loop.get("completed_cycles"),
            "station_target_cycles": loop.get("target_cycles"),
            "station_loop_status": loop.get("status"),
        },
        "gate": {
            "requires_human": bool(gate),
            "reason": gate,
            "pending_validation": state.get("pending_validation"),
            "pending_commit": state.get("pending_commit"),
            "next_action": _next_action(status, gate),
        },
        "latest_turn": {
            "id": latest_turn.get("id"),
            "number": state.get("current_turn"),
            "title": latest_turn.get("title"),
            "stage": latest_turn.get("stage"),
            "phase": latest_turn.get("phase"),
            "provider": latest_turn.get("provider"),
            "configured_model": latest_turn.get("configured_model"),
            "configured_reasoning": latest_turn.get("configured_reasoning"),
            "observed_model": latest_turn.get("observed_model"),
            "observed_reasoning": latest_turn.get("observed_reasoning"),
            "directive": (latest_turn.get("directive") or {}).get("next"),
            "provider_error": latest_turn.get("provider_error"),
            "output_file": latest_output,
            "output_path": latest_output_path,
        },
        "evidence": {
            "working_revision": state.get("working_revision"),
            "changed_paths": evidence.get("changed_paths") or [],
            "validations": _validation_summary(state),
            "completion_receipt": latest_cycle.get("completion_receipt") or state.get("completion_receipt"),
            "next_task_proposal": latest_cycle.get("next_task_proposal"),
            "errors": state.get("errors") or [],
        },
    }
