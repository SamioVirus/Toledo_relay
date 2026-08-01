from __future__ import annotations

import pytest

from toledo_orchestrator.cadence import (
    build_backbone,
    build_handoff,
    cadence_direction,
    complete_station,
    record_handoff,
    validate_cadence_path,
    verify_recorded_handoff,
)
from toledo_orchestrator.workflow import load_workflows


def test_cadence_backbone_requires_adjacent_stations() -> None:
    assert validate_cadence_path(["weekly", "daily", "hourly"]) == ["weekly", "daily", "hourly"]
    assert cadence_direction("weekly", "daily") == "downward"
    assert cadence_direction("hourly", "daily") == "upward"
    with pytest.raises(ValueError, match="adjacent stations"):
        validate_cadence_path(["weekly", "hourly"])


def test_backbone_starts_at_first_station_and_records_the_rail() -> None:
    backbone = build_backbone([
        ("weekly-governance", "weekly"),
        ("daily-dispatch", "daily"),
        ("hourly-station", "hourly"),
    ])

    assert backbone["schema_version"] == "toledo_orchestrator.cadence_backbone.v1"
    assert backbone["rail"] == ["weekly", "daily", "hourly"]
    assert [station["status"] for station in backbone["stations"]] == [
        "active", "pending", "pending"
    ]


def test_multi_station_backbone_requires_typed_cadence_on_every_layer() -> None:
    with pytest.raises(ValueError, match="cadence is required"):
        build_backbone([
            ("weekly-governance", "weekly"),
            ("untyped-custom-workflow", None),
        ])


def test_handoff_recording_is_ordered_bound_and_terminal() -> None:
    backbone = build_backbone([
        ("weekly-governance", "weekly"),
        ("daily-dispatch", "daily"),
        ("hourly-station", "hourly"),
    ])
    first = build_handoff(
        run_id="run_test",
        source_workflow="weekly-governance",
        target_workflow="daily-dispatch",
        source_cadence="weekly",
        target_cadence="daily",
        source_layer_index=0,
        target_layer_index=1,
        source_cycle=1,
        target_cycle=2,
        source_turn=8,
        working_revision="abc123",
        completion_receipt="cycles/cycle.0001/completion.json",
        approved_handoff="cycles/cycle.0001/approved.md",
        request_file="cycles/cycle.0002/request.md",
        request_sha256="a" * 64,
    ).as_dict()
    record_handoff(
        backbone,
        first,
        artifact_file="handoffs/cadence-handoff.0001.json",
        artifact_sha256="b" * 64,
    )

    assert [station["status"] for station in backbone["stations"]] == [
        "complete", "active", "pending"
    ]
    assert verify_recorded_handoff(
        backbone,
        first,
        artifact_file="handoffs/cadence-handoff.0001.json",
        artifact_sha256="b" * 64,
    )["handoff_id"] == "run_test:cadence:0->1"
    with pytest.raises(ValueError, match="station order"):
        record_handoff(
            backbone,
            first,
            artifact_file="handoffs/cadence-handoff.0001.json",
            artifact_sha256="b" * 64,
        )
    with pytest.raises(ValueError, match="final station"):
        complete_station(backbone, 1)

    second = build_handoff(
        run_id="run_test",
        source_workflow="daily-dispatch",
        target_workflow="hourly-station",
        source_cadence="daily",
        target_cadence="hourly",
        source_layer_index=1,
        target_layer_index=2,
        source_cycle=2,
        target_cycle=3,
        source_turn=16,
        working_revision="def456",
        completion_receipt="cycles/cycle.0002/completion.json",
        approved_handoff="cycles/cycle.0002/approved.md",
        request_file="cycles/cycle.0003/request.md",
        request_sha256="c" * 64,
    ).as_dict()
    record_handoff(
        backbone,
        second,
        artifact_file="handoffs/cadence-handoff.0002.json",
        artifact_sha256="d" * 64,
    )
    complete_station(backbone, 2)

    assert [station["status"] for station in backbone["stations"]] == [
        "complete", "complete", "complete"
    ]


def test_packaged_cadence_stations_preserve_role_specific_model_routes() -> None:
    workflows = load_workflows()

    assert workflows["weekly-governance"].cadence == "weekly"
    assert workflows["daily-dispatch"].cadence == "daily"
    assert workflows["hourly-station"].cadence == "hourly"
    assert workflows["weekly-governance"].profiles["strategy-planner"].model == "gpt-5.6-sol"
    assert workflows["weekly-governance"].profiles["strategy-planner"].effort == "xhigh"
    assert workflows["weekly-governance"].profiles["strategy-operator"].model == "gpt-5.6-sol"
    assert workflows["weekly-governance"].profiles["strategy-operator"].effort == "high"
    assert workflows["weekly-governance"].profiles["strategy-reviewer"].model == "claude-sonnet-5"
    assert workflows["daily-dispatch"].profiles["test-planner"].model == "gpt-5.6-terra"
    assert workflows["daily-dispatch"].profiles["test-planner"].effort == "medium"
    assert workflows["daily-dispatch"].profiles["test-runner"].model == "gpt-5.6-terra"
    assert workflows["daily-dispatch"].profiles["test-reviewer"].model == "claude-sonnet-5"
    assert workflows["hourly-station"].profiles["ui-planner"].model == "gpt-5.6-sol"
    assert workflows["hourly-station"].profiles["ui-builder"].effort == "high"
    assert workflows["hourly-station"].profiles["ui-critic"].model == "claude-sonnet-5"
