from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from toledo_orchestrator.configuration import load_configured_workflows, update_profile
from toledo_orchestrator.catalog import CATALOG_SCHEMA
from toledo_orchestrator.core import sha256, write_json
from toledo_orchestrator.cycle import CycleOrchestrator
from toledo_orchestrator.web import RunWorkers, make_handler


def request_json(url: str, *, method: str = "GET", value: dict[str, object] | None = None, nonce: str | None = None):
    data = json.dumps(value).encode("utf-8") if value is not None else None
    headers = {"Content-Type": "application/json"}
    if nonce:
        headers["X-Orchestrator-Nonce"] = nonce
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_profile_overrides_are_runtime_local_and_validated(tmp_path: Path):
    workflows = load_configured_workflows(tmp_path)
    before = workflows["continuous-development"].profiles["codex-planning"]
    assert before.effort == "xhigh"
    planner_close = workflows["continuous-development-planner-close"]
    assert planner_close.stages["next-task"].session_slot == "planner"
    assert planner_close.stages["next-task"].profile == "codex-planning"
    update_profile(tmp_path, "continuous-development", "codex-planning", model="gpt-test", effort="high")
    after = load_configured_workflows(tmp_path)["continuous-development"].profiles["codex-planning"]
    assert after.model == "gpt-test" and after.effort == "high"
    inherited = load_configured_workflows(tmp_path)["continuous-development-planner-close"].profiles["codex-planning"]
    assert inherited.model == "gpt-test" and inherited.effort == "high"
    assert (tmp_path / "config" / "workflows" / "continuous-development.json").is_file()


def test_profile_api_rejects_unknown_catalog_selection_but_records_custom(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    write_json(engine.runtime_dir / "catalog" / "capabilities.v1.json", {
        "schema_version": CATALOG_SCHEMA,
        "verified_at": "2026-07-13T00:00:00+00:00",
        "models": [{
            "provider": "codex", "selection_token": "gpt-catalog",
            "display_name": "Catalog model", "supported_efforts": ["low"],
            "special_modes": [], "availability": "installed-account", "source": "fixture",
        }], "observed_models": [], "sources": {},
    })
    workers = RunWorkers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, workers, "test-nonce"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as rejected:
            request_json(base + "/api/profile", method="POST", nonce="test-nonce", value={
                "workflow": "continuous-development", "profile": "codex-planning",
                "model": "not-in-catalog", "effort": "high",
            })
        assert rejected.value.code == 400
        status, saved = request_json(base + "/api/profile", method="POST", nonce="test-nonce", value={
            "workflow": "continuous-development", "profile": "codex-planning",
            "model": "operator-token", "effort": "special", "custom": True,
        })
        assert status == 200 and saved["custom"] is True and saved["model"] == "operator-token"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_local_web_api_serves_ui_requires_nonce_and_blocks_artifact_traversal(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, workers, "test-nonce"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/", timeout=5) as response:
            html = response.read().decode("utf-8")
            assert response.status == 200 and "Toledo Relay" in html
            assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")

        status, bootstrap = request_json(base + "/api/bootstrap")
        assert status == 200 and bootstrap["nonce"] == "test-nonce"
        assert bootstrap["server"]["started_at"]
        assert bootstrap["server"]["revision"] == "unknown"
        assert "continuous-development" in bootstrap["workflows"]

        try:
            request_json(
                base + "/api/profile",
                method="POST",
                value={"workflow": "continuous-development", "profile": "codex-planning", "effort": "high"},
            )
        except urllib.error.HTTPError as error:
            assert error.code == 403
        else:
            raise AssertionError("profile update accepted without the launch nonce")

        status, profile = request_json(
            base + "/api/profile",
            method="POST",
            nonce="test-nonce",
            value={"workflow": "continuous-development", "profile": "codex-planning", "effort": "high"},
        )
        assert status == 200 and profile["effort"] == "high"

        evil_request = urllib.request.Request(
            base + "/api/profile",
            data=json.dumps({
                "workflow": "continuous-development",
                "profile": "codex-planning",
                "effort": "low",
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Orchestrator-Nonce": "test-nonce",
                "Origin": "https://evil.example",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as blocked_origin:
            urllib.request.urlopen(evil_request, timeout=5)
        assert blocked_origin.value.code == 403

        run_id = "run_20260712T120000Z_1234abcd"
        run_dir = engine.runs_dir / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "safe.txt").write_text("safe", encoding="utf-8")
        write_json(run_dir / "run.json", {
            "run_id": run_id,
            "schema_version": "toledo_orchestrator.run.v2",
            "artifacts": {"safe.txt": {"sha256": sha256(b"safe")}},
        })
        outside = engine.runs_dir / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        with urllib.request.urlopen(base + f"/api/runs/{run_id}/artifact?path=safe.txt", timeout=5) as response:
            assert response.read() == b"safe"
        try:
            urllib.request.urlopen(base + f"/api/runs/{run_id}/artifact?path=../outside.txt", timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError("artifact traversal was not rejected")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_profile_save_with_browser_nonce_from_before_server_restart_is_rejected(tmp_path: Path):
    """Capture the stale page nonce incident before adding retry behavior.

    A page bootstrapped by the first UI process retains its launch nonce.  If
    the process is restarted on the same URL before the page reloads, its
    profile-save request must currently receive the explicit nonce rejection.
    """
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    first_server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(engine, workers, "nonce-before-restart")
    )
    port = first_server.server_port
    first_thread = threading.Thread(target=first_server.serve_forever, daemon=True)
    first_thread.start()
    try:
        _, bootstrap = request_json(f"http://127.0.0.1:{port}/api/bootstrap")
    finally:
        first_server.shutdown()
        first_server.server_close()
        first_thread.join(timeout=5)

    second_server = ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(engine, workers, "nonce-after-restart")
    )
    second_thread = threading.Thread(target=second_server.serve_forever, daemon=True)
    second_thread.start()
    try:
        stale_profile_save = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/profile",
            data=json.dumps({
                "workflow": "continuous-development",
                "profile": "codex-planning",
                "model": "gpt-5.6-sol",
                "effort": "high",
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Orchestrator-Nonce": bootstrap["nonce"],
                "Origin": f"http://127.0.0.1:{port}",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(stale_profile_save, timeout=5)
        assert rejected.value.code == 403
        assert json.loads(rejected.value.read().decode("utf-8")) == {
            "error": "PermissionError: missing or invalid launch nonce"
        }
        saved = load_configured_workflows(engine.runtime_dir)["continuous-development"]
        assert saved.profiles["codex-planning"].effort == "xhigh"
    finally:
        second_server.shutdown()
        second_server.server_close()
        second_thread.join(timeout=5)


def test_profile_value_persists_across_ui_server_restart(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    first = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, workers, "first-nonce"))
    port = first.server_port
    first_thread = threading.Thread(target=first.serve_forever, daemon=True)
    first_thread.start()
    try:
        status, saved = request_json(
            f"http://127.0.0.1:{port}/api/profile", method="POST", nonce="first-nonce",
            value={"workflow": "continuous-development", "profile": "codex-planning", "effort": "high"},
        )
        assert status == 200 and saved["effort"] == "high"
    finally:
        first.shutdown(); first.server_close(); first_thread.join(timeout=5)
    restarted = ThreadingHTTPServer(("127.0.0.1", port), make_handler(engine, workers, "second-nonce"))
    restarted_thread = threading.Thread(target=restarted.serve_forever, daemon=True)
    restarted_thread.start()
    try:
        _, bootstrap = request_json(f"http://127.0.0.1:{port}/api/bootstrap")
        assert bootstrap["nonce"] == "second-nonce"
        assert bootstrap["workflows"]["continuous-development"]["profiles"]["codex-planning"]["effort"] == "high"
    finally:
        restarted.shutdown(); restarted.server_close(); restarted_thread.join(timeout=5)


def test_head_endpoint_returns_only_change_detection_fields(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    run_id = "run_20260712T120000Z_1234abcd"
    write_json(engine.runs_dir / run_id / "run.json", {
        "run_id": run_id,
        "schema_version": "toledo_orchestrator.run.v2",
        "status": "paused",
        "current_turn": 7,
        "pending_human_decision": "next_task_approval",
        "event_sequence": 42,
        "events": [{"id": f"event.{n:06d}"} for n in range(42)],
        "turns": [{"id": f"turn.{n:04d}", "output_file": f"turn.{n:04d}.output.md"} for n in range(7)],
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, workers, "test-nonce"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, head = request_json(base + f"/api/runs/{run_id}/head")
        assert status == 200
        assert head == {
            "run_id": run_id,
            "event_sequence": 42,
            "status": "paused",
            "current_turn": 7,
            "pending_human_decision": "next_task_approval",
            "worker": {"active": False, "error": None},
        }
        # The head response must stay small: it never carries turns or events.
        assert "turns" not in head and "events" not in head
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_export_is_chronological_and_excludes_raw_envelopes(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    run_id = "run_20260712T120000Z_deadbeef"
    run_dir = engine.runs_dir / run_id
    (run_dir / "turns").mkdir(parents=True)
    (run_dir / "turns" / "turn.0001.output.md").write_text("Useful work\n```orchestrator\n{\"next\":\"ready\"}\n```", encoding="utf-8")
    write_json(run_dir / "run.json", {
        "run_id": run_id, "schema_version": "toledo_orchestrator.run.v2", "project": "toledo", "workflow": "continuous-development",
        "status": "paused", "turns": [{"id": "turn.0001", "stage": "planning-propose", "provider": "codex", "output_file": "turn.0001.output.md", "usage": {"total_cost_usd": 0.12}}],
        "decisions": [], "validations": {}, "artifacts": {},
    })
    exported = engine.export_run(run_id).decode("utf-8")
    assert "Useful work" in exported and "Observed cost: $0.12" in exported
    assert "orchestrator" not in exported and "stderr" in exported


def test_background_worker_failure_is_persisted_in_run_state(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    run_id = "run_20260712T120000Z_deadbeef"
    write_json(engine.runs_dir / run_id / "run.json", {
        "run_id": run_id,
        "schema_version": "toledo_orchestrator.run.v2",
        "status": "running",
        "cycle": 1,
        "current_stage": "planning-propose",
        "event_sequence": 0,
        "events": [],
        "errors": [],
        "inflight": None,
    })

    def fail() -> None:
        raise RuntimeError("fixture worker crash")

    workers.start(run_id, fail, lambda error: engine.record_background_failure(run_id, error))
    deadline = time.monotonic() + 5
    while workers.status(run_id)["active"] and time.monotonic() < deadline:
        time.sleep(0.01)
    state = engine.state(run_id)
    assert state["status"] == "paused"
    assert state["pending_human_decision"] == "background_operation_failed"
    assert "fixture worker crash" in state["errors"][-1]
    assert state["events"][-1]["kind"] == "background.operation.failed"


def test_local_web_continue_forwards_optional_owner_direction(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    received: list[tuple[str, bytes]] = []

    def continue_step(run_id: str, direction: bytes = b"") -> dict[str, object]:
        received.append((run_id, direction))
        return {"run_id": run_id}

    engine.continue_step = continue_step  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, workers, "test-nonce"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, _ = request_json(
            base + "/api/runs/run_20260712T120000Z_1234abcd/continue",
            method="POST",
            nonce="test-nonce",
            value={"direction": "How about now?\r\n"},
        )
        assert status == 202
        deadline = time.monotonic() + 5
        while not received and time.monotonic() < deadline:
            time.sleep(0.01)
        assert received == [("run_20260712T120000Z_1234abcd", b"How about now?\r\n")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_local_web_exposes_inactive_run_recovery(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    received: list[str] = []

    def recover_run(run_id: str) -> dict[str, object]:
        received.append(run_id)
        return {"run_id": run_id}

    engine.recover_run = recover_run  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, workers, "test-nonce"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    run_id = "run_20260712T120000Z_1234abcd"
    try:
        status, _ = request_json(
            base + f"/api/runs/{run_id}/recover",
            method="POST",
            nonce="test-nonce",
            value={},
        )
        assert status == 202
        deadline = time.monotonic() + 5
        while not received and time.monotonic() < deadline:
            time.sleep(0.01)
        assert received == [run_id]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
