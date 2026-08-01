"""Typed cadence stations and the rail between them.

The Relay workflow stack is intentionally linear, but the work it carries has
three useful operating scales.  This module keeps that scale relationship in
code instead of leaving it as a naming convention in workflow JSON or prompt
text.

The first implementation is deliberately small: a stack may stay at one
station or move between adjacent stations only.  A weekly-to-hourly jump is
rejected so daily work cannot be silently skipped.  Handoffs are compact,
reference-based records; the full evidence remains in the run artifact store.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any


CADENCES = ("weekly", "daily", "hourly")
CADENCE_ORDER = {value: index for index, value in enumerate(CADENCES)}
CADENCE_HANDOFF_SCHEMA = "toledo_orchestrator.cadence_handoff.v1"
CADENCE_BACKBONE_SCHEMA = "toledo_orchestrator.cadence_backbone.v1"
STATION_STATUSES = frozenset({"pending", "active", "complete"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"cadence {field} must be a non-empty string")
    return value.strip()


def _non_negative_int(value: Any, field: str, *, positive: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"cadence {field} must be an integer")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"cadence {field} must be {qualifier}")
    return value


def _artifact_path(value: Any, field: str) -> str:
    text = _required_text(value, field).replace("\\", "/")
    path = PurePosixPath(text)
    if not path.parts or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise ValueError(f"cadence {field} must stay inside the run artifact directory")
    return text


def _sha256(value: Any, field: str) -> str:
    digest = _required_text(value, field)
    if not SHA256_PATTERN.fullmatch(digest):
        raise ValueError(f"cadence {field} must be a lowercase sha256 digest")
    return digest


def validate_cadence(value: str | None, *, required: bool = False) -> str | None:
    """Normalize and validate a workflow cadence label."""

    if value is None or not str(value).strip():
        if required:
            raise ValueError("workflow cadence is required for a multi-station stack")
        return None
    normalized = str(value).strip().lower()
    if normalized not in CADENCE_ORDER:
        raise ValueError(
            f"unsupported workflow cadence {value!r}; expected one of {', '.join(CADENCES)}"
        )
    return normalized


def cadence_direction(source: str, target: str) -> str:
    """Describe travel direction along the weekly → daily → hourly rail."""

    source_value = validate_cadence(source, required=True)
    target_value = validate_cadence(target, required=True)
    assert source_value is not None and target_value is not None
    if source_value == target_value:
        return "same-station"
    return "downward" if CADENCE_ORDER[target_value] > CADENCE_ORDER[source_value] else "upward"


def validate_adjacent_cadence(source: str, target: str) -> str:
    """Allow a same-station handoff or one adjacent station move only."""

    source_value = validate_cadence(source, required=True)
    target_value = validate_cadence(target, required=True)
    assert source_value is not None and target_value is not None
    distance = abs(CADENCE_ORDER[source_value] - CADENCE_ORDER[target_value])
    if distance > 1:
        raise ValueError(
            "cadence backbone may connect only adjacent stations "
            f"(weekly ↔ daily ↔ hourly), not {source_value} → {target_value}"
        )
    return cadence_direction(source_value, target_value)


def validate_cadence_path(cadences: Iterable[str | None]) -> list[str]:
    """Validate a complete stack path and return normalized cadence labels."""

    values: list[str] = []
    for raw_value in cadences:
        value = validate_cadence(raw_value, required=True)
        assert value is not None
        values.append(value)
    for source, target in zip(values, values[1:]):
        validate_adjacent_cadence(source, target)
    return values


def build_backbone(stations: Iterable[tuple[str, str | None]]) -> dict[str, Any]:
    """Create the compact, launch-time backbone manifest for a workflow stack."""

    values = [
        (_required_text(workflow, "station workflow"), validate_cadence(cadence))
        for workflow, cadence in stations
    ]
    if not values:
        raise ValueError("cadence backbone requires at least one station")
    workflows = [workflow for workflow, _ in values]
    if len(set(workflows)) != len(workflows):
        raise ValueError("cadence backbone cannot repeat a workflow station")
    if len(values) > 1:
        validate_cadence_path(cadence for _, cadence in values)
    backbone = {
        "schema_version": CADENCE_BACKBONE_SCHEMA,
        "rail": list(CADENCES),
        "stations": [
            {
                "index": index,
                "workflow": workflow,
                "cadence": cadence,
                "status": "active" if index == 0 else "pending",
            }
            for index, (workflow, cadence) in enumerate(values)
        ],
        "handoffs": [],
    }
    validate_backbone(backbone)
    return backbone


def validate_backbone(backbone: Mapping[str, Any]) -> None:
    """Fail closed when persisted station state no longer forms one linear rail."""

    if not isinstance(backbone, Mapping):
        raise ValueError("cadence backbone must be an object")
    if backbone.get("schema_version") != CADENCE_BACKBONE_SCHEMA:
        raise ValueError("cadence backbone schema version is unsupported")
    if backbone.get("rail") != list(CADENCES):
        raise ValueError("cadence backbone rail does not match the canonical cadence order")
    stations = backbone.get("stations")
    handoffs = backbone.get("handoffs")
    if not isinstance(stations, list) or not stations:
        raise ValueError("cadence backbone stations are missing")
    if not isinstance(handoffs, list):
        raise ValueError("cadence backbone handoffs must be a list")

    workflows: list[str] = []
    cadences: list[str | None] = []
    statuses: list[str] = []
    for index, station in enumerate(stations):
        if not isinstance(station, Mapping):
            raise ValueError("cadence backbone station must be an object")
        if station.get("index") != index:
            raise ValueError("cadence backbone station indexes must be contiguous")
        workflows.append(_required_text(station.get("workflow"), "station workflow"))
        cadences.append(validate_cadence(station.get("cadence"), required=len(stations) > 1))
        status = str(station.get("status") or "")
        if status not in STATION_STATUSES:
            raise ValueError(f"unsupported cadence station status: {status or 'missing'}")
        statuses.append(status)
    if len(set(workflows)) != len(workflows):
        raise ValueError("cadence backbone cannot repeat a workflow station")
    if len(stations) > 1:
        validate_cadence_path(cadences)

    all_complete = all(status == "complete" for status in statuses)
    if all_complete:
        expected_handoffs = len(stations) - 1
    else:
        active = [index for index, status in enumerate(statuses) if status == "active"]
        if len(active) != 1:
            raise ValueError("cadence backbone must have exactly one active station")
        active_index = active[0]
        expected_statuses = [
            "complete" if index < active_index else "active" if index == active_index else "pending"
            for index in range(len(stations))
        ]
        if statuses != expected_statuses:
            raise ValueError("cadence backbone station statuses are out of order")
        expected_handoffs = active_index
    if len(handoffs) != expected_handoffs:
        raise ValueError("cadence backbone handoff count does not match station progress")

    seen_ids: set[str] = set()
    for index, record in enumerate(handoffs):
        if not isinstance(record, Mapping):
            raise ValueError("cadence backbone handoff record must be an object")
        handoff_id = _required_text(record.get("handoff_id"), "handoff id")
        if handoff_id in seen_ids:
            raise ValueError("cadence backbone cannot repeat a handoff")
        seen_ids.add(handoff_id)
        source = stations[index]
        target = stations[index + 1]
        expected = {
            "source_workflow": source["workflow"],
            "target_workflow": target["workflow"],
            "source_cadence": source["cadence"],
            "target_cadence": target["cadence"],
        }
        for field, value in expected.items():
            if record.get(field) != value:
                raise ValueError(f"cadence backbone handoff {field} does not match its stations")
        if record.get("direction") != cadence_direction(str(source["cadence"]), str(target["cadence"])):
            raise ValueError("cadence backbone handoff direction does not match its stations")
        _artifact_path(record.get("artifact_file"), "handoff artifact file")
        _sha256(record.get("artifact_sha256"), "handoff artifact sha256")


@dataclass(frozen=True)
class CadenceHandoff:
    """A sealed reference packet connecting two adjacent workflow stations."""

    run_id: str
    handoff_id: str
    source_workflow: str
    target_workflow: str
    source_cadence: str
    target_cadence: str
    direction: str
    source_layer_index: int
    target_layer_index: int
    source_cycle: int
    target_cycle: int
    source_turn: int
    working_revision: str
    completion_receipt: str | None
    approved_handoff: str | None
    request_file: str
    request_sha256: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CADENCE_HANDOFF_SCHEMA,
            "run_id": self.run_id,
            "handoff_id": self.handoff_id,
            "source_workflow": self.source_workflow,
            "target_workflow": self.target_workflow,
            "source_cadence": self.source_cadence,
            "target_cadence": self.target_cadence,
            "direction": self.direction,
            "source_layer_index": self.source_layer_index,
            "target_layer_index": self.target_layer_index,
            "source_cycle": self.source_cycle,
            "target_cycle": self.target_cycle,
            "source_turn": self.source_turn,
            "working_revision": self.working_revision,
            "completion_receipt": self.completion_receipt,
            "approved_handoff": self.approved_handoff,
            "request_file": self.request_file,
            "request_sha256": self.request_sha256,
            "created_at": self.created_at,
        }


def build_handoff(
    *,
    run_id: str,
    source_workflow: str,
    target_workflow: str,
    source_cadence: str | None,
    target_cadence: str | None,
    source_layer_index: int,
    target_layer_index: int,
    source_cycle: int,
    target_cycle: int,
    source_turn: int,
    working_revision: str,
    completion_receipt: str | None,
    approved_handoff: str | None,
    request_file: str,
    request_sha256: str,
) -> CadenceHandoff:
    """Build a validated handoff without copying private provider output."""

    source_value = validate_cadence(source_cadence, required=True)
    target_value = validate_cadence(target_cadence, required=True)
    assert source_value is not None and target_value is not None
    direction = validate_adjacent_cadence(source_value, target_value)
    if target_layer_index != source_layer_index + 1:
        raise ValueError("cadence handoff layers must advance exactly one station")
    handoff = CadenceHandoff(
        run_id=run_id,
        handoff_id=f"{run_id}:cadence:{source_layer_index}->{target_layer_index}",
        source_workflow=source_workflow,
        target_workflow=target_workflow,
        source_cadence=source_value,
        target_cadence=target_value,
        direction=direction,
        source_layer_index=source_layer_index,
        target_layer_index=target_layer_index,
        source_cycle=source_cycle,
        target_cycle=target_cycle,
        source_turn=source_turn,
        working_revision=working_revision,
        completion_receipt=completion_receipt,
        approved_handoff=approved_handoff,
        request_file=request_file,
        request_sha256=request_sha256,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    validate_handoff(handoff.as_dict())
    return handoff


def validate_handoff(handoff: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a sealed cadence packet independently from mutable run state."""

    if not isinstance(handoff, Mapping):
        raise ValueError("cadence handoff must be an object")
    if handoff.get("schema_version") != CADENCE_HANDOFF_SCHEMA:
        raise ValueError("cadence handoff schema version is unsupported")
    value = dict(handoff)
    run_id = _required_text(value.get("run_id"), "run id")
    source_index = _non_negative_int(value.get("source_layer_index"), "source layer index")
    target_index = _non_negative_int(value.get("target_layer_index"), "target layer index")
    if target_index != source_index + 1:
        raise ValueError("cadence handoff layers must advance exactly one station")
    source_cycle = _non_negative_int(value.get("source_cycle"), "source cycle", positive=True)
    target_cycle = _non_negative_int(value.get("target_cycle"), "target cycle", positive=True)
    if target_cycle != source_cycle + 1:
        raise ValueError("cadence handoff cycles must advance exactly one cycle")
    _non_negative_int(value.get("source_turn"), "source turn")
    source_workflow = _required_text(value.get("source_workflow"), "source workflow")
    target_workflow = _required_text(value.get("target_workflow"), "target workflow")
    if source_workflow == target_workflow:
        raise ValueError("cadence handoff must connect distinct workflow stations")
    source_cadence = validate_cadence(value.get("source_cadence"), required=True)
    target_cadence = validate_cadence(value.get("target_cadence"), required=True)
    assert source_cadence is not None and target_cadence is not None
    expected_direction = validate_adjacent_cadence(source_cadence, target_cadence)
    if value.get("direction") != expected_direction:
        raise ValueError("cadence handoff direction does not match its cadence pair")
    expected_id = f"{run_id}:cadence:{source_index}->{target_index}"
    if value.get("handoff_id") != expected_id:
        raise ValueError("cadence handoff id does not match its run and station indexes")
    _required_text(value.get("working_revision"), "working revision")
    for field in ("completion_receipt", "approved_handoff"):
        if value.get(field) is not None:
            _artifact_path(value.get(field), field.replace("_", " "))
    _artifact_path(value.get("request_file"), "request file")
    _sha256(value.get("request_sha256"), "request sha256")
    _required_text(value.get("created_at"), "creation timestamp")
    return value


def verify_recorded_handoff(
    backbone: Mapping[str, Any],
    handoff: Mapping[str, Any],
    *,
    artifact_file: str,
    artifact_sha256: str,
) -> dict[str, Any]:
    """Bind a hash-verified packet back to its recorded station transition."""

    validate_backbone(backbone)
    value = validate_handoff(handoff)
    registered_file = _artifact_path(artifact_file, "handoff artifact file")
    registered_sha256 = _sha256(artifact_sha256, "handoff artifact sha256")
    records = backbone["handoffs"]
    matching = [record for record in records if record.get("handoff_id") == value["handoff_id"]]
    if len(matching) != 1:
        raise ValueError("cadence handoff is not recorded exactly once in the backbone")
    record = matching[0]
    for field in (
        "source_workflow",
        "target_workflow",
        "source_cadence",
        "target_cadence",
        "direction",
        "source_cycle",
        "target_cycle",
    ):
        if record.get(field) != value.get(field):
            raise ValueError(f"recorded cadence handoff {field} does not match its sealed packet")
    if record.get("artifact_file") != registered_file or record.get("artifact_sha256") != registered_sha256:
        raise ValueError("recorded cadence handoff artifact reference does not match the run manifest")
    return value


def record_handoff(backbone: dict[str, Any], handoff: dict[str, Any], *, artifact_file: str, artifact_sha256: str) -> None:
    """Advance station status and retain only auditable handoff metadata."""

    validate_backbone(backbone)
    value = validate_handoff(handoff)
    stations = backbone["stations"]
    source_index = value["source_layer_index"]
    target_index = value["target_layer_index"]
    if target_index >= len(stations):
        raise ValueError("cadence handoff station index is outside the backbone")
    if source_index != len(backbone["handoffs"]):
        raise ValueError("cadence handoffs must be recorded in station order")
    source = stations[source_index]
    target = stations[target_index]
    expected = {
        "source_workflow": source["workflow"],
        "target_workflow": target["workflow"],
        "source_cadence": source["cadence"],
        "target_cadence": target["cadence"],
    }
    for field, expected_value in expected.items():
        if value[field] != expected_value:
            raise ValueError(f"cadence handoff {field} does not match the launch-time backbone")
    if source["status"] != "active" or target["status"] != "pending":
        raise ValueError("cadence handoff must move from the active station to the next pending station")
    registered_file = _artifact_path(artifact_file, "handoff artifact file")
    registered_sha256 = _sha256(artifact_sha256, "handoff artifact sha256")
    stations[source_index]["status"] = "complete"
    stations[target_index]["status"] = "active"
    backbone.setdefault("handoffs", []).append({
        "handoff_id": value["handoff_id"],
        "source_workflow": value["source_workflow"],
        "target_workflow": value["target_workflow"],
        "source_cadence": value["source_cadence"],
        "target_cadence": value["target_cadence"],
        "direction": value["direction"],
        "source_layer_index": source_index,
        "target_layer_index": target_index,
        "source_cycle": value["source_cycle"],
        "target_cycle": value["target_cycle"],
        "request_file": value["request_file"],
        "request_sha256": value["request_sha256"],
        "artifact_file": registered_file,
        "artifact_sha256": registered_sha256,
    })
    validate_backbone(backbone)


def complete_station(backbone: dict[str, Any], index: int) -> None:
    """Mark the active station complete when the whole stack closes."""

    validate_backbone(backbone)
    stations = backbone["stations"]
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(stations):
        raise ValueError("cadence completion station index is outside the backbone")
    if index != len(stations) - 1:
        raise ValueError("cadence backbone may complete only at its final station")
    if stations[index]["status"] != "active":
        raise ValueError("cadence backbone final station must be active before completion")
    stations[index]["status"] = "complete"
    validate_backbone(backbone)
