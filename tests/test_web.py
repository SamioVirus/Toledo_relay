from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from toledo_orchestrator.configuration import load_configured_workflows, update_profile, update_profiles
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


def test_profile_provider_switch_is_persisted_and_slot_consistent(tmp_path: Path):
    # The implementer slot is served by one profile, so flipping its provider
    # keeps every stage in the slot consistent.
    update_profile(
        tmp_path, "continuous-development", "codex-implementation",
        provider="claude", model="claude-fable-5", effort="max",
    )
    after = load_configured_workflows(tmp_path)["continuous-development"].profiles["codex-implementation"]
    assert after.provider == "claude" and after.model == "claude-fable-5" and after.effort == "max"
    # The reviewer slot is shared by both review profiles; a one-sided flip
    # would resume a Claude session with Codex, so it must be rejected and the
    # stored workflow must stay loadable.
    with pytest.raises(ValueError, match="session slot"):
        update_profile(
            tmp_path, "continuous-development", "claude-implementation-review",
            provider="codex", model="gpt-5.6-terra", effort="high",
        )
    reloaded = load_configured_workflows(tmp_path)["continuous-development"].profiles
    assert reloaded["claude-implementation-review"].provider == "claude"
    assert reloaded["codex-implementation"].provider == "claude"


def test_shared_session_provider_defaults_can_be_switched_atomically(tmp_path: Path):
    saved = update_profiles(tmp_path, "continuous-development", {
        "claude-planning-review": {
            "provider": "codex", "model": "gpt-5.6-terra", "effort": "high",
        },
        "claude-implementation-review": {
            "provider": "codex", "model": "gpt-5.6-sol", "effort": "xhigh",
        },
    })
    assert saved.profiles["claude-planning-review"].provider == "codex"
    assert saved.profiles["claude-implementation-review"].provider == "codex"
    assert load_configured_workflows(tmp_path)["continuous-development"].profiles[
        "claude-implementation-review"
    ].model == "gpt-5.6-sol"
    with pytest.raises(ValueError, match="requires model and effort"):
        update_profiles(tmp_path, "continuous-development", {
            "codex-planning": {"model": ""},
        })


def test_workflow_variant_accepts_provider_overrides_with_slot_rule(tmp_path: Path):
    from toledo_orchestrator.configuration import save_workflow_variant

    saved = save_workflow_variant(
        tmp_path, "continuous-development", "claude-builds", "Claude builds",
        profile_overrides={"codex-implementation": {"provider": "claude", "model": "claude-fable-5", "effort": "max"}},
    )
    profile = saved.profiles["codex-implementation"]
    assert profile.provider == "claude" and profile.model == "claude-fable-5" and profile.effort == "max"
    with pytest.raises(ValueError, match="session slot"):
        save_workflow_variant(
            tmp_path, "continuous-development", "mixed-reviewers", "Mixed reviewers",
            profile_overrides={"claude-implementation-review": {"provider": "codex", "model": "gpt-5.6-terra", "effort": "high"}},
            prompt_overrides={"planning-kickoff.md": "# Must not be written\n"},
        )
    assert not (tmp_path / "config" / "prompts" / "mixed-reviewers").exists()


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
        status, saved_slot = request_json(base + "/api/profile", method="POST", nonce="test-nonce", value={
            "workflow": "continuous-development",
            "profile_overrides": {
                "claude-planning-review": {
                    "provider": "codex", "model": "review-token", "effort": "special", "custom": True,
                },
                "claude-implementation-review": {
                    "provider": "codex", "model": "audit-token", "effort": "special", "custom": True,
                },
            },
        })
        assert status == 200
        assert saved_slot["profiles"]["claude-planning-review"]["provider"] == "codex"
        assert saved_slot["profiles"]["claude-implementation-review"]["model"] == "audit-token"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_catalog_research_api_is_nonce_protected_and_local(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, workers, "test-nonce"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, report = request_json(
            f"http://127.0.0.1:{server.server_port}/api/catalog/research",
            method="POST", nonce="test-nonce", value={},
        )
        assert status == 200 and report["method"] == "deterministic-local-audit"
        assert (engine.runtime_dir / "catalog" / "research.v1.json").is_file()
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
            assert '<div class="run-context">' in html
            assert '<button class="quiet-button" id="jump-active">Active</button>' in html
            assert 'id="new-continuous-loop"' in html
            assert 'id="new-continuous-loop-cycles"' in html
            assert "3 complete cycles" in html and "5 complete cycles" in html

        with urllib.request.urlopen(base + "/styles.css", timeout=5) as response:
            css = response.read().decode("utf-8")
            assert ".run-context { position:sticky; top:0;" in css
            assert ".timeline-toolbar { min-height:46px;" in css
            assert ".turn-quick-take summary" in css
            assert ".continuous-loop-control" in css

        with urllib.request.urlopen(base + "/app.js", timeout=5) as response:
            javascript = response.read().decode("utf-8")
            assert "function quickTakeMarkup(turn)" in javascript
            assert "head.summary_sequence" in javascript
            assert 'model.complete = {label: "Complete project", inMore: true}' in javascript
            assert "source has uncommitted tracked changes" in javascript
            assert "source status unavailable" in javascript
            assert "button.disabled = !Boolean(value.implementation_ready)" in javascript
            assert "function validationApprovalCommands(state)" in javascript
            assert "Routine tests and read-only checks run automatically." in javascript
            assert "This routine check can continue" in javascript
            assert "See what would run and why" in javascript
            assert "Local project check (routine checks run automatically)" in javascript
            assert "function syncContinuousLoopControls()" in javascript
            assert "Continuous loop finished" in javascript
            assert "Safety, failure, and permission gates still stop immediately." in javascript
            assert 'runStatusLabel(status)' in javascript

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
            "summary_sequence": 0,
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


def test_run_projection_includes_async_quick_take_and_summary_revision(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    run_id = "run_20260717T120000Z_1234abcd"
    turns = engine.runs_dir / run_id / "turns"
    turns.mkdir(parents=True)
    (turns / "turn.0001.output.md").write_text("Plan output", encoding="utf-8")
    write_json(engine.runs_dir / run_id / "run.json", {
        "run_id": run_id,
        "status": "paused",
        "current_turn": 1,
        "pending_human_decision": "operator_step",
        "turns": [{
            "id": "turn.0001", "route": "codex-propose", "provider": "codex",
            "output_file": "turn.0001.output.md", "substantive": True,
        }],
    })

    class FakeSummaryWorkers:
        def revision(self, selected_run_id: str) -> int:
            assert selected_run_id == run_id
            return 7

        def enrich(self, selected_run_id: str, state: dict[str, object]) -> None:
            assert selected_run_id == run_id
            state["turns"][0]["quick_take"] = {
                "status": "ready", "text": "Planner produced a concise plan.",
                "model": "gemma4:test", "generated_at": "2026-07-17T12:00:00Z", "error": None,
            }

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(engine, workers, "test-nonce", summary_workers=FakeSummaryWorkers()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        _, head = request_json(base + f"/api/runs/{run_id}/head")
        _, state = request_json(base + f"/api/runs/{run_id}")
        assert head["summary_sequence"] == 7
        assert state["summary_sequence"] == 7
        assert state["turns"][0]["quick_take"]["text"] == "Planner produced a concise plan."
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
    assert "orchestrator" not in exported and "Diagnostics: excluded" in exported


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


def test_local_web_complete_marks_the_project_without_scheduling_a_worker(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    received: list[tuple[str, bytes]] = []

    def complete_project(run_id: str, note: bytes = b"") -> dict[str, object]:
        received.append((run_id, note))
        return {"run_id": run_id, "status": "complete"}

    engine.complete_project = complete_project  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, workers, "test-nonce"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, state = request_json(
            base + "/api/runs/run_20260718T120000Z_1234abcd/complete",
            method="POST",
            nonce="test-nonce",
            value={},
        )
        assert status == 200
        assert state["status"] == "complete"
        assert received == [("run_20260718T120000Z_1234abcd", b"")]
        assert workers.status("run_20260718T120000Z_1234abcd")["active"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("endpoint", ["continue", "decision"])
def test_gate_action_persists_displayed_override_before_scheduling(
    tmp_path: Path,
    endpoint: str,
):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    events: list[tuple[str, object]] = []

    def set_next_turn_override(run_id: str, **values: object) -> dict[str, object]:
        events.append(("override", {"run_id": run_id, **values}))
        return {"run_id": run_id}

    def continue_step(run_id: str, direction: bytes = b"") -> dict[str, object]:
        events.append(("continue", direction))
        return {"run_id": run_id}

    def decide(
        run_id: str,
        choice: str,
        text: bytes = b"",
        follow_up: str | None = None,
    ) -> dict[str, object]:
        events.append(("decision", (choice, text, follow_up)))
        return {"run_id": run_id}

    engine.set_next_turn_override = set_next_turn_override  # type: ignore[method-assign]
    engine.continue_step = continue_step  # type: ignore[method-assign]
    engine.decide = decide  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, workers, "test-nonce"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    run_id = "run_20260712T120000Z_1234abcd"
    value: dict[str, object] = {
        "next_turn_override": {
            "profile": "claude-planning-review",
            "model": "claude-opus-4-8",
            "effort": "low",
            "session_action": "new",
            "custom": False,
        },
    }
    value.update(
        {"direction": "Run with these settings."}
        if endpoint == "continue"
        else {"choice": "yes", "text": "", "follow_up": "baseline_failure"}
    )
    try:
        status, _ = request_json(
            base + f"/api/runs/{run_id}/{endpoint}",
            method="POST",
            nonce="test-nonce",
            value=value,
        )
        assert status == 202
        deadline = time.monotonic() + 5
        while len(events) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [event[0] for event in events] == ["override", endpoint]
        persisted = events[0][1]
        assert isinstance(persisted, dict)
        assert persisted["model"] == "claude-opus-4-8"
        assert persisted["effort"] == "low"
        assert persisted["session_action"] == "new"
        if endpoint == "decision":
            assert events[1][1] == ("yes", b"", "baseline_failure")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_decision_treats_json_null_follow_up_as_absent(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    decisions: list[tuple[str, bytes, str | None]] = []

    def decide(
        run_id: str,
        choice: str,
        text: bytes = b"",
        follow_up: str | None = None,
    ) -> dict[str, object]:
        decisions.append((choice, text, follow_up))
        return {"run_id": run_id}

    engine.decide = decide  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, workers, "test-nonce"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    run_id = "run_20260712T120000Z_1234abcd"
    try:
        status, _ = request_json(
            base + f"/api/runs/{run_id}/decision",
            method="POST",
            nonce="test-nonce",
            value={"choice": "no", "text": "", "follow_up": None},
        )
        assert status == 202
        deadline = time.monotonic() + 5
        while not decisions and time.monotonic() < deadline:
            time.sleep(0.01)
        assert decisions == [("no", b"", None)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_run_create_forwards_bounded_continuous_loop_settings(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    received: dict[str, object] = {}

    def create_run(
        request: bytes,
        project: str,
        workflow: str,
        **options: object,
    ) -> str:
        received.update({
            "request": request,
            "project": project,
            "workflow": workflow,
            **options,
        })
        return "run_20260731T120000Z_1234abcd"

    engine.create_run = create_run  # type: ignore[method-assign]
    engine.run_to_stop = lambda run_id: {"run_id": run_id}  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, workers, "test-nonce"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, result = request_json(
            base + "/api/runs",
            method="POST",
            nonce="test-nonce",
            value={
                "request": "Run three ideas",
                "project": "jobs",
                "workflow": "continuous-development",
                "workflow_stack": ["continuous-development", "strategy-council"],
                "run_mode": "auto",
                "continuous_loop": {"enabled": True, "target_cycles": 3},
            },
        )
        assert status == 202
        assert result["run_id"] == "run_20260731T120000Z_1234abcd"
        assert received["continuous_loop_enabled"] is True
        assert received["continuous_loop_cycles"] == 3
        assert received["run_mode"] == "auto"
        assert received["workflow_stack"] == ["continuous-development", "strategy-council"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_gate_action_override_failure_schedules_no_operation(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    continued: list[str] = []

    def reject_override(run_id: str, **values: object) -> dict[str, object]:
        raise ValueError("selected effort is unavailable")

    def continue_step(run_id: str, direction: bytes = b"") -> dict[str, object]:
        continued.append(run_id)
        return {"run_id": run_id}

    engine.set_next_turn_override = reject_override  # type: ignore[method-assign]
    engine.continue_step = continue_step  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, workers, "test-nonce"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    run_id = "run_20260712T120000Z_1234abcd"
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as rejected:
            request_json(
                base + f"/api/runs/{run_id}/continue",
                method="POST",
                nonce="test-nonce",
                value={
                    "direction": "Must not run.",
                    "next_turn_override": {
                        "profile": "claude-planning-review",
                        "model": "claude-opus-4-8",
                        "effort": "not-supported",
                        "session_action": "new",
                    },
                },
            )
        assert rejected.value.code == 400
        assert "selected effort is unavailable" in rejected.value.read().decode("utf-8")
        assert continued == []
        assert workers.status(run_id) == {"active": False, "error": None}
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


def test_save_workflow_variant_api_creates_selectable_workflow(tmp_path: Path):
    engine = CycleOrchestrator(runtime_dir=tmp_path / "runtime")
    workers = RunWorkers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, workers, "test-nonce"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, saved = request_json(base + "/api/workflows/save-as", method="POST", nonce="test-nonce", value={
            "base_workflow": "continuous-development",
            "id": "abc-budget",
            "label": "A/B/C · Budget",
            "profile_overrides": {"claude-planning-review": {"model": "claude-haiku-4-5-20251001", "effort": "default"}},
            "round_overrides": {"planning": 5},
            "prompt_overrides": {"planning-kickoff.md": "# Custom ideation\nBe brief.\n"},
        })
        assert status == 200
        assert saved["id"] == "abc-budget"
        assert saved["label"] == "A/B/C · Budget"
        assert saved["profiles"]["claude-planning-review"]["model"] == "claude-haiku-4-5-20251001"
        assert saved["stages"]["planning-review"]["round"]["cap"] == 5

        # The variant is offered by bootstrap and its custom prompt resolves.
        status, bootstrap = request_json(base + "/api/bootstrap")
        assert status == 200 and "abc-budget" in bootstrap["workflows"]
        status, prompt = request_json(base + "/api/workflows/abc-budget/stage-prompt?stage=planning-propose")
        assert status == 200 and prompt["template"].startswith("# Custom ideation")
        # The base workflow keeps its packaged instruction and caps.
        status, base_prompt = request_json(base + "/api/workflows/continuous-development/stage-prompt?stage=planning-propose")
        assert status == 200 and not base_prompt["template"].startswith("# Custom ideation")
        assert bootstrap["workflows"]["continuous-development"]["stages"]["planning-review"]["round"]["cap"] == 3

        # A second save under the same name is rejected instead of clobbering.
        try:
            request_json(base + "/api/workflows/save-as", method="POST", nonce="test-nonce", value={
                "base_workflow": "continuous-development", "id": "abc-budget", "label": "Again",
            })
        except urllib.error.HTTPError as error:
            assert error.code == 400
        else:
            raise AssertionError("duplicate workflow id accepted")
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
