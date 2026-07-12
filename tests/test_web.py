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
    before = load_configured_workflows(tmp_path)["continuous-development"].profiles["codex-planning"]
    assert before.effort == "xhigh"
    update_profile(tmp_path, "continuous-development", "codex-planning", model="gpt-test", effort="high")
    after = load_configured_workflows(tmp_path)["continuous-development"].profiles["codex-planning"]
    assert after.model == "gpt-test" and after.effort == "high"
    assert (tmp_path / "config" / "workflows" / "continuous-development.json").is_file()


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
