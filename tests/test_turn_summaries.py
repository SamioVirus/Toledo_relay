from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from toledo_orchestrator.core import read_json, write_json
from toledo_orchestrator.turn_summaries import OllamaSummaryClient, TurnSummaryWorkers, redact_summary


class FakeSummaryClient:
    model = "gemma4:test"

    def __init__(self, text: str = "Reviewer found three major defects and four minor issues.") -> None:
        self.text = text
        self.calls: list[tuple[dict[str, Any], str]] = []

    def generate(self, turn: dict[str, Any], output: str) -> dict[str, Any]:
        self.calls.append((turn, output))
        return {
            "text": self.text,
            "elapsed_ms": 12,
            "source_truncated": False,
            "usage": {"prompt_tokens": 20, "output_tokens": 10},
        }


class BlockingSummaryClient(FakeSummaryClient):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def generate(self, turn: dict[str, Any], output: str) -> dict[str, Any]:
        self.started.set()
        assert self.release.wait(2)
        return super().generate(turn, output)


class FailingSummaryClient(FakeSummaryClient):
    def generate(self, turn: dict[str, Any], output: str) -> dict[str, Any]:
        self.calls.append((turn, output))
        raise RuntimeError("ollama offline: private detail must not escape")


def _turn(output: str = "Major findings (3) and minor findings (4).") -> dict[str, Any]:
    return {
        "id": "turn.0001",
        "role": "reviewer",
        "title": "Review the plan",
        "phase": "planning-review",
        "exit_code": 0,
        "substantive": True,
        "correction": False,
        "output_file": "turn.0001.output.md",
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def _runtime(tmp_path: Path, output: str = "Major findings (3) and minor findings (4).") -> tuple[Path, str, dict[str, Any]]:
    runtime = tmp_path / "runtime"
    run_id = "run_20260717T120000Z_summary"
    turns = runtime / "runs" / run_id / "turns"
    turns.mkdir(parents=True)
    (turns / "turn.0001.output.md").write_text(output, encoding="utf-8")
    state = {"run_id": run_id, "turns": [_turn(output)]}
    write_json(runtime / "runs" / run_id / "run.json", state)
    return runtime, run_id, state


def _wait_ready(workers: TurnSummaryWorkers, run_id: str, state: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        projected = {"turns": [dict(state["turns"][0])]}
        workers.enrich(run_id, projected)
        quick_take = projected["turns"][0]["quick_take"]
        if quick_take["status"] in {"ready", "failed"}:
            return quick_take
        time.sleep(0.01)
    raise AssertionError("summary worker did not finish")


def test_summary_is_nonblocking_and_never_mutates_run_state(tmp_path: Path):
    runtime, run_id, state = _runtime(tmp_path)
    client = BlockingSummaryClient()
    workers = TurnSummaryWorkers(runtime, client)

    projected = {"turns": [dict(state["turns"][0])]}
    workers.enrich(run_id, projected)
    assert client.started.wait(1)
    assert projected["turns"][0]["quick_take"]["status"] in {"queued", "writing"}
    assert read_json(runtime / "runs" / run_id / "run.json") == state

    client.release.set()
    ready = _wait_ready(workers, run_id, state)
    assert ready["status"] == "ready"
    assert ready["text"] == "Reviewer found three major defects and four minor issues."
    assert read_json(runtime / "runs" / run_id / "run.json") == state


def test_ready_summary_is_idempotent_across_worker_instances(tmp_path: Path):
    runtime, run_id, state = _runtime(tmp_path)
    first_client = FakeSummaryClient()
    first = TurnSummaryWorkers(runtime, first_client)
    assert _wait_ready(first, run_id, state)["status"] == "ready"
    assert len(first_client.calls) == 1

    restarted_client = FakeSummaryClient("This replacement must never run.")
    restarted = TurnSummaryWorkers(runtime, restarted_client)
    projected = {"turns": [dict(state["turns"][0])]}
    restarted.enrich(run_id, projected)
    assert projected["turns"][0]["quick_take"]["status"] == "ready"
    assert restarted_client.calls == []


def test_failed_summary_is_visible_sanitized_and_not_retried_before_cooldown(tmp_path: Path):
    runtime, run_id, state = _runtime(tmp_path)
    client = FailingSummaryClient()
    workers = TurnSummaryWorkers(runtime, client)
    failed = _wait_ready(workers, run_id, state)
    assert failed["status"] == "failed"
    assert failed["text"] is None
    assert failed["model"] == "gemma4:test"
    assert failed["error"] == "Quick take unavailable (RuntimeError). Full output is unaffected."
    assert failed["retry_after"] > time.time()
    projected = {"turns": [dict(state["turns"][0])]}
    workers.enrich(run_id, projected)
    assert len(client.calls) == 1
    assert "private detail" not in str(projected)


def test_failed_summary_retries_on_later_open_after_cooldown(tmp_path: Path):
    runtime, run_id, state = _runtime(tmp_path)
    failing = FailingSummaryClient()
    first = TurnSummaryWorkers(runtime, failing)
    assert _wait_ready(first, run_id, state)["status"] == "failed"
    artifact = runtime / "runs" / run_id / "turns" / "turn.0001.summary.json"
    failed_value = read_json(artifact)
    failed_value["retry_after_unix"] = 0
    write_json(artifact, failed_value)

    recovered = FakeSummaryClient("Reviewer recovered after Ollama became available.")
    reopened = TurnSummaryWorkers(runtime, recovered)
    ready = _wait_ready(reopened, run_id, state)
    assert ready["status"] == "ready"
    assert ready["text"] == "Reviewer recovered after Ollama became available."
    assert read_json(artifact)["attempts"] == 2
    assert len(recovered.calls) == 1


def test_redact_summary_removes_common_credentials_and_bounds_text():
    value = redact_summary(
        "Reviewer found token=super-secret-value, Bearer abcdefghijklmnopqrstuvwxyz, "
        "and sk-testcredential1234567890. " + ("Long detail " * 100)
    )
    assert "super-secret" not in value
    assert "abcdefghijklmnopqrstuvwxyz" not in value
    assert "sk-testcredential" not in value
    assert "[redacted]" in value
    assert len(value) <= 480


def test_summary_endpoint_is_restricted_to_loopback():
    with pytest.raises(ValueError, match="loopback"):
        OllamaSummaryClient(endpoint="https://summary.example.com/api/chat").generate(_turn(), "output")
    assert OllamaSummaryClient(endpoint="http://localhost:11434/api/chat").endpoint.endswith("/api/chat")


def test_ineligible_turns_do_not_queue_or_gain_quick_take(tmp_path: Path):
    runtime, run_id, state = _runtime(tmp_path)
    state["turns"][0]["substantive"] = False
    client = FakeSummaryClient()
    workers = TurnSummaryWorkers(runtime, client)
    workers.enrich(run_id, state)
    assert "quick_take" not in state["turns"][0]
    assert client.calls == []


def test_crlf_output_is_hashed_as_exact_bytes_before_decode(tmp_path: Path):
    output = "First finding.\r\nSecond finding.\r\n"
    runtime, run_id, state = _runtime(tmp_path, output)
    output_path = runtime / "runs" / run_id / "turns" / "turn.0001.output.md"
    output_path.write_bytes(output.encode("utf-8"))
    client = FakeSummaryClient()
    workers = TurnSummaryWorkers(runtime, client)
    assert _wait_ready(workers, run_id, state)["status"] == "ready"
    assert client.calls[0][1] == output


def test_unchanged_watch_scan_uses_run_mtime_cache(tmp_path: Path, monkeypatch):
    runtime, _, _ = _runtime(tmp_path)
    client = FakeSummaryClient()
    workers = TurnSummaryWorkers(runtime, client)
    calls = 0
    original_read_json = read_json

    def counting_read_json(path: Path) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return original_read_json(path)

    monkeypatch.setattr("toledo_orchestrator.turn_summaries.read_json", counting_read_json)
    assert workers._discover_turns()
    assert calls == 1
    assert workers._discover_turns(changed_only=True) == {}
    assert calls == 1


def test_watcher_summarizes_new_turn_without_a_run_state_request(tmp_path: Path):
    runtime, run_id, state = _runtime(tmp_path)
    run_json = runtime / "runs" / run_id / "run.json"
    write_json(run_json, {"run_id": run_id, "turns": []})
    client = FakeSummaryClient()
    workers = TurnSummaryWorkers(runtime, client)
    workers.start_watching(poll_seconds=0.01)
    try:
        write_json(run_json, state)
        deadline = time.monotonic() + 3
        artifact = runtime / "runs" / run_id / "turns" / "turn.0001.summary.json"
        while time.monotonic() < deadline and not artifact.is_file():
            time.sleep(0.01)
        assert artifact.is_file()
        assert read_json(artifact)["status"] == "ready"
        assert len(client.calls) == 1
    finally:
        workers.stop_watching()
