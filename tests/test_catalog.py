from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from toledo_orchestrator.catalog import CATALOG_SCHEMA, load_catalog, research_catalog, validate_selection
from toledo_orchestrator.core import ClaudeAdapter, ProviderResult, atomic_write


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, value: bytes) -> int:
        self.writes.append(value)
        return len(value)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeStdout:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.lines = [(json.dumps(message) + "\n").encode("utf-8") for message in messages]

    def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""


class _FakeProcess:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(messages)
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_catalog_selection_requires_known_provider_model_effort_combination():
    catalog = {"models": [{"provider": "codex", "selection_token": "gpt-test", "supported_efforts": ["low", "high"]}]}
    validate_selection(catalog, provider="codex", model="gpt-test", effort="high")
    with pytest.raises(ValueError, match="not supported"):
        validate_selection(catalog, provider="codex", model="gpt-test", effort="max")
    with pytest.raises(ValueError, match="not available"):
        validate_selection(catalog, provider="claude", model="gpt-test", effort="high")
    validate_selection(catalog, provider="claude", model="operator-token", effort="special", custom=True)


def test_catalog_cache_is_stale_warning_not_a_run_blocker(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    target = tmp_path / "catalog" / "capabilities.v1.json"
    atomic_write(target, json.dumps({
        "schema_version": CATALOG_SCHEMA, "verified_at": old, "models": [], "observed_models": [], "sources": {},
    }).encode("utf-8"))
    assert load_catalog(tmp_path, refresh=False)["stale"] is True


def test_catalog_research_is_local_audit_not_capability_discovery(tmp_path: Path):
    target = tmp_path / "catalog" / "capabilities.v1.json"
    atomic_write(target, json.dumps({
        "schema_version": CATALOG_SCHEMA, "verified_at": datetime.now(timezone.utc).isoformat(),
        "models": [{"provider": "codex", "selection_token": "gpt-test"}], "observed_models": [], "sources": {},
    }).encode("utf-8"))
    report = research_catalog(tmp_path)
    assert report["method"] == "deterministic-local-audit"
    assert "does not add entitlement" in report["note"]
    assert (tmp_path / "catalog" / "research.v1.json").is_file()


def test_claude_efforts_are_per_model_and_haiku_gets_default_only(monkeypatch):
    from toledo_orchestrator import catalog

    def fake_run_text(command):
        if "--help" in command:
            return "--model <model>  --effort <level>", None
        return "2.1.185 (Claude Code)", None

    monkeypatch.setattr(catalog, "_run_text", fake_run_text)
    models, metadata = catalog._claude_models()
    by_token = {item["selection_token"]: item for item in models}
    assert by_token["claude-fable-5"]["supported_efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert by_token["claude-opus-4-8"]["supported_efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert by_token["claude-sonnet-5"]["supported_efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert by_token["claude-fable-5"]["default_effort"] == "high"
    assert by_token["claude-opus-4-8"]["default_effort"] == "high"
    assert by_token["claude-sonnet-5"]["default_effort"] == "high"
    # Haiku 4.5 is not in the official effort-support list; it exposes only
    # the provider default and the adapter omits the effort flag.
    assert by_token["claude-haiku-4-5-20251001"]["supported_efforts"] == []
    assert metadata["supports_effort"] is True


def test_selection_for_effortless_model_accepts_only_default():
    catalog_value = {"models": [{"provider": "claude", "selection_token": "claude-haiku-4-5-20251001", "supported_efforts": []}]}
    validate_selection(catalog_value, provider="claude", model="claude-haiku-4-5-20251001", effort="default")
    with pytest.raises(ValueError, match="no documented reasoning-effort control"):
        validate_selection(catalog_value, provider="claude", model="claude-haiku-4-5-20251001", effort="low")


def test_pong_matrix_refuses_to_spend_without_live_flag(tmp_path: Path):
    from toledo_orchestrator.catalog import run_pong_matrix
    with pytest.raises(ValueError, match="--live"):
        run_pong_matrix(tmp_path, {}, live=False)


def test_codex_app_server_catalog_pages_and_preserves_metadata(monkeypatch):
    from toledo_orchestrator import catalog

    process = _FakeProcess([
        {"id": 1, "result": {"userAgent": "codex-test"}},
        {"id": 2, "result": {"data": [{
            "id": "gpt-a", "model": "gpt-a", "displayName": "GPT A", "description": "first",
            "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": "Low"}],
            "defaultReasoningEffort": "low", "additionalSpeedTiers": ["fast"],
            "serviceTiers": [{"id": "priority", "name": "Fast", "description": "More usage"}],
            "defaultServiceTier": "priority", "inputModalities": ["text", "image"],
            "supportsPersonality": True, "isDefault": True, "hidden": False,
        }], "nextCursor": "page-2"}},
        {"id": 3, "result": {"data": [{
            "id": "gpt-b", "displayName": "GPT B", "supportedReasoningEfforts": ["medium"],
            "defaultReasoningEffort": "medium", "additionalSpeedTiers": [], "serviceTiers": [],
            "hidden": False,
        }], "nextCursor": None}},
    ])
    monkeypatch.setattr(catalog, "resolve_cli_executable", lambda name: name)
    monkeypatch.setattr(catalog.subprocess, "Popen", lambda *args, **kwargs: process)

    models, error = catalog._codex_models_app_server(timeout_seconds=0.5)

    assert error is None
    assert [item["selection_token"] for item in models] == ["gpt-a", "gpt-b"]
    assert models[0]["default_effort"] == "low"
    assert models[0]["description"] == "first"
    assert models[0]["additional_speed_tiers"] == ["fast"]
    assert models[0]["service_tiers"] == [{"id": "priority", "name": "Fast", "description": "More usage"}]
    assert models[0]["default_service_tier"] == "priority"
    assert models[0]["is_default"] is True
    requests = [json.loads(value) for value in process.stdin.writes]
    assert requests[1] == {"method": "initialized", "params": {}}
    assert requests[2]["params"]["limit"] == catalog.APP_SERVER_PAGE_LIMIT
    assert requests[3]["params"]["cursor"] == "page-2"


def test_codex_app_server_timeout_is_wall_clock_bounded(monkeypatch):
    from toledo_orchestrator import catalog

    released = threading.Event()

    class BlockingStdout:
        def readline(self) -> bytes:
            released.wait(2)
            return b""

    class BlockingProcess(_FakeProcess):
        def __init__(self) -> None:
            super().__init__([])
            self.stdout = BlockingStdout()

        def terminate(self) -> None:
            released.set()
            super().terminate()

    process = BlockingProcess()
    monkeypatch.setattr(catalog, "resolve_cli_executable", lambda name: name)
    monkeypatch.setattr(catalog.subprocess, "Popen", lambda *args, **kwargs: process)
    started = time.monotonic()

    models, error = catalog._codex_models_app_server(timeout_seconds=0.03)

    assert time.monotonic() - started < 0.5
    assert models == []
    assert "within 0.03s" in str(error)
    assert process.terminated is True


def test_catalog_refetches_when_recorded_cli_version_changes(tmp_path: Path, monkeypatch):
    from toledo_orchestrator import catalog

    target = tmp_path / "catalog" / "capabilities.v1.json"
    atomic_write(target, json.dumps({
        "schema_version": CATALOG_SCHEMA,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "models": [],
        "observed_models": [],
        "sources": {"codex": {"cli_version": "codex-cli 0.1"}},
    }).encode("utf-8"))
    monkeypatch.setattr(catalog, "_installed_cli_versions", lambda providers=None: {"codex": "codex-cli 0.2"})
    monkeypatch.setattr(catalog, "refresh_catalog", lambda runtime_dir: {"refreshed": True})

    assert catalog.load_catalog(tmp_path) == {"refreshed": True}


def test_partial_refresh_retains_last_good_claude_while_updating_codex(tmp_path: Path, monkeypatch):
    from toledo_orchestrator import catalog

    prior_verified_at = "2026-07-13T12:00:00+00:00"
    target = tmp_path / "catalog" / "capabilities.v1.json"
    atomic_write(target, json.dumps({
        "schema_version": CATALOG_SCHEMA,
        "discovered_at": prior_verified_at,
        "verified_at": prior_verified_at,
        "models": [
            {"provider": "codex", "selection_token": "gpt-old"},
            {"provider": "claude", "selection_token": "claude-last-good"},
        ],
        "observed_models": [],
        "sources": {
            "codex": {"cli_version": "codex-old", "verified_at": prior_verified_at},
            "claude": {"cli_version": "claude-old", "verified_at": prior_verified_at},
        },
    }).encode("utf-8"))
    monkeypatch.setattr(catalog, "_codex_models", lambda: (
        [{"provider": "codex", "selection_token": "gpt-new"}],
        {"cli_version": "codex-new", "error": None},
    ))
    monkeypatch.setattr(catalog, "_claude_models", lambda: (
        [], {"cli_version": "claude-new", "error": "claude discovery failed"},
    ))
    monkeypatch.setattr(catalog, "_observed_models", lambda runtime_dir: [])
    monkeypatch.setattr(catalog, "_merge_pong_evidence", lambda runtime_dir, models: None)

    refreshed = catalog.refresh_catalog(tmp_path)

    assert {(item["provider"], item["selection_token"]) for item in refreshed["models"]} == {
        ("codex", "gpt-new"),
        ("claude", "claude-last-good"),
    }
    assert refreshed["sources"]["codex"]["stale"] is False
    assert refreshed["sources"]["codex"]["cli_version"] == "codex-new"
    assert refreshed["sources"]["claude"]["stale"] is True
    assert refreshed["sources"]["claude"]["error"] == "claude discovery failed"
    assert refreshed["sources"]["claude"]["retained_model_count"] == 1
    assert refreshed["sources"]["claude"]["last_good_at"] == prior_verified_at
    assert refreshed["last_refresh"]["succeeded"] is False
    assert refreshed["last_refresh"]["partial"] is True


def test_partial_refresh_retains_last_good_codex_while_updating_claude(tmp_path: Path, monkeypatch):
    from toledo_orchestrator import catalog

    prior_verified_at = "2026-07-13T12:00:00+00:00"
    target = tmp_path / "catalog" / "capabilities.v1.json"
    atomic_write(target, json.dumps({
        "schema_version": CATALOG_SCHEMA,
        "discovered_at": prior_verified_at,
        "verified_at": prior_verified_at,
        "models": [
            {"provider": "codex", "selection_token": "gpt-last-good"},
            {"provider": "claude", "selection_token": "claude-old"},
        ],
        "observed_models": [],
        "sources": {
            "codex": {"cli_version": "codex-old", "verified_at": prior_verified_at},
            "claude": {"cli_version": "claude-old", "verified_at": prior_verified_at},
        },
    }).encode("utf-8"))
    monkeypatch.setattr(catalog, "_codex_models", lambda: (
        [], {"cli_version": "codex-new", "error": "codex discovery failed"},
    ))
    monkeypatch.setattr(catalog, "_claude_models", lambda: (
        [{"provider": "claude", "selection_token": "claude-new"}],
        {"cli_version": "claude-new", "error": None},
    ))
    monkeypatch.setattr(catalog, "_observed_models", lambda runtime_dir: [])
    monkeypatch.setattr(catalog, "_merge_pong_evidence", lambda runtime_dir, models: None)

    refreshed = catalog.refresh_catalog(tmp_path)

    assert {(item["provider"], item["selection_token"]) for item in refreshed["models"]} == {
        ("codex", "gpt-last-good"),
        ("claude", "claude-new"),
    }
    assert refreshed["sources"]["codex"]["stale"] is True
    assert refreshed["sources"]["codex"]["error"] == "codex discovery failed"
    assert refreshed["sources"]["codex"]["retained_model_count"] == 1
    assert refreshed["sources"]["codex"]["last_good_at"] == prior_verified_at
    assert refreshed["sources"]["claude"]["stale"] is False
    assert refreshed["sources"]["claude"]["cli_version"] == "claude-new"
    assert refreshed["last_refresh"]["succeeded"] is False
    assert refreshed["last_refresh"]["partial"] is True


def test_pong_requires_explicit_cases_and_enforces_filters_before_invocation(tmp_path: Path):
    from toledo_orchestrator.catalog import run_pong_matrix

    class NeverAdapter:
        calls = 0

        def invoke_configured(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("provider must not be invoked")

    adapter = NeverAdapter()
    with pytest.raises(ValueError, match="no provider calls"):
        run_pong_matrix(tmp_path, {"claude": adapter}, live=True)
    with pytest.raises(ValueError, match="all explicit pong cases were excluded"):
        run_pong_matrix(
            tmp_path, {"claude": adapter}, live=True,
            matrix=[("claude", "claude-fable-5", "low")],
            excluded_models={"claude-fable-5"},
        )
    with pytest.raises(ValueError, match="exceeding max_calls=1"):
        run_pong_matrix(
            tmp_path, {"claude": adapter}, live=True,
            matrix=[("claude", "claude-opus-4-8", "low"), ("claude", "claude-sonnet-5", "low")],
            max_calls=1,
        )
    assert adapter.calls == 0


def test_pong_merges_prior_evidence_and_does_not_claim_claude_effort_observation(tmp_path: Path):
    from toledo_orchestrator import catalog

    prior = {
        "schema_version": "toledo_orchestrator.pong.v1",
        "started_at": "2026-07-14T00:00:00+00:00",
        "finished_at": "2026-07-14T00:00:01+00:00",
        "pings": [{
            "provider": "codex", "model": "gpt-old", "effort": "low", "ok": True,
            "verified_model": "gpt-old", "observed_reasoning": "low",
        }],
        "resume_probes": [],
        "totals": {"attempted": 1, "ok": 1, "cost_usd": 0},
    }
    atomic_write(tmp_path / catalog.PONG_RELATIVE_PATH, json.dumps(prior).encode("utf-8"))

    class ClaudeAdapter:
        def invoke_configured(self, route, prompt, working_directory, **kwargs):
            return ProviderResult(
                provider="claude", route=route, stdout=b"", response_text="pong",
                observed_model=None, session_id="session-1",
                configured_model=kwargs["model"], configured_reasoning=kwargs["reasoning"],
                model_usage={kwargs["model"]: {"outputTokens": 1}},
                observation_source="claude-modelUsage",
            )

    report = catalog.run_pong_matrix(
        tmp_path, {"claude": ClaudeAdapter()}, live=True,
        matrix=[("claude", "claude-opus-4-8", "low")],
    )

    assert len(report["pings"]) == 2
    latest = report["pings"][-1]
    assert latest["model_verified"] is True
    assert latest["effort_cli_accepted"] is True
    assert latest["effort_verified"] is False
    assert latest["effort_evidence"] == "cli-accepted-unobserved"
    models = [{"provider": "claude", "selection_token": "claude-opus-4-8"}]
    catalog._merge_pong_evidence(tmp_path, models)
    assert models[0]["live_verified"]["observed_efforts"] == []
    assert models[0]["live_verified"]["accepted_efforts"] == ["low"]


def test_pong_evidence_keeps_historical_timestamp_and_exposes_latest_failure(tmp_path: Path):
    from toledo_orchestrator import catalog

    old_finish = "2026-07-14T20:45:40+00:00"
    latest_finish = "2026-07-15T03:10:03+00:00"
    report = {
        "schema_version": "toledo_orchestrator.pong.v1",
        "finished_at": latest_finish,
        "pings": [
            {
                "provider": "claude",
                "requested_model": "claude-opus-4-8",
                "requested_effort": "low",
                "verified_model": "claude-opus-4-8",
                "model_verified": True,
                "effort_cli_accepted": True,
                "ok": True,
                # Legacy record intentionally has no per-ping timestamp.
            },
            {
                "provider": "claude",
                "requested_model": "claude-opus-4-8",
                "requested_effort": "medium",
                "model_verified": False,
                "effort_verified": False,
                "provider_error": "claude_api_error_429",
                "text_head": "You've hit your session limit",
                "at": "2026-07-15T03:09:51+00:00",
                "ok": False,
            },
        ],
        "runs": [
            {"finished_at": old_finish, "totals": {"attempted": 1, "ok": 1}},
            {"finished_at": latest_finish, "totals": {"attempted": 1, "ok": 0}},
        ],
        "last_run_totals": {"attempted": 1, "ok": 0},
    }
    atomic_write(tmp_path / catalog.PONG_RELATIVE_PATH, json.dumps(report).encode("utf-8"))
    models = [{"provider": "claude", "selection_token": "claude-opus-4-8"}]

    catalog._merge_pong_evidence(tmp_path, models)

    assert models[0]["live_verified"]["at"] == old_finish
    assert models[0]["live_verified"]["latest_run_observed"] is False
    assert models[0]["last_live_attempt"]["ok"] is False
    assert models[0]["last_live_attempt"]["error"] == "claude_api_error_429"


def test_real_claude_pong_uses_exec_form_stop_hook_to_observe_effective_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from toledo_orchestrator import catalog

    adapter = ClaudeAdapter(executable="claude-test")
    temporary_paths: list[Path] = []

    def fake_run(command, prompt, cwd, timeout):
        assert prompt == b"Reply with exactly: pong"
        settings_path = Path(command[command.index("--settings") + 1])
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        handler = settings["hooks"]["Stop"][0]["hooks"][0]
        assert handler["type"] == "command"
        assert handler["command"] == sys.executable
        assert handler["timeout"] == 5
        assert handler["args"][0].endswith("capture_stop_effort.py")
        capture_path = Path(handler["args"][1])
        hook_input = json.dumps({
            "hook_event_name": "Stop",
            "session_id": "session-observed",
            "effort": {"level": "medium"},
        }).encode("utf-8")
        completed = subprocess.run(
            [handler["command"], *handler["args"]], input=hook_input,
            capture_output=True, check=False, timeout=5,
        )
        assert completed.returncode == 0 and completed.stdout == b"" and completed.stderr == b""
        temporary_paths.extend([settings_path, Path(handler["args"][0]), capture_path])
        envelope = {
            "type": "result", "subtype": "success", "session_id": "session-observed",
            "result": "pong", "modelUsage": {"claude-opus-4-8": {"outputTokens": 1}},
        }
        return json.dumps(envelope).encode("utf-8"), b"", 0, 12

    monkeypatch.setattr(adapter, "_run", fake_run)
    report = catalog.run_pong_matrix(
        tmp_path, {"claude": adapter}, live=True,
        matrix=[("claude", "claude-opus-4-8", "medium")],
    )

    ping = report["pings"][-1]
    assert ping["ok"] is True
    assert ping["effort_observation_expected"] is True
    assert ping["effort_verified"] is True
    assert ping["observed_effort"] == "medium"
    assert ping["effort_evidence"] == "claude-stop-hook"
    assert ping["effort_observation_error"] is None
    assert all(not path.exists() for path in temporary_paths)


def test_claude_stop_hook_rejects_effort_from_a_different_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from toledo_orchestrator import catalog

    adapter = ClaudeAdapter(executable="claude-test")

    def fake_run(command, prompt, cwd, timeout):
        settings_path = Path(command[command.index("--settings") + 1])
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        capture_path = Path(settings["hooks"]["Stop"][0]["hooks"][0]["args"][1])
        capture_path.write_text(json.dumps({
            "hook_event_name": "Stop",
            "session_id": "other-session",
            "effort": {"level": "max"},
        }) + "\n", encoding="utf-8")
        envelope = {
            "type": "result", "subtype": "success", "session_id": "expected-session",
            "result": "pong", "modelUsage": {"claude-opus-4-8": {"outputTokens": 1}},
        }
        return json.dumps(envelope).encode("utf-8"), b"", 0, 12

    monkeypatch.setattr(adapter, "_run", fake_run)
    report = catalog.run_pong_matrix(
        tmp_path, {"claude": adapter}, live=True,
        matrix=[("claude", "claude-opus-4-8", "max")],
    )

    ping = report["pings"][-1]
    assert ping["call_succeeded"] is True and ping["model_verified"] is True
    assert ping["effort_cli_accepted"] is True
    assert ping["effort_verified"] is False
    assert ping["ok"] is False
    assert ping["effort_evidence"] == "cli-accepted-unobserved"
    assert "matched" in ping["effort_observation_error"]


def test_catalog_stamps_verified_same_session_switch_on_target_model(tmp_path: Path):
    from toledo_orchestrator import catalog

    switched_at = "2026-07-14T12:34:56+00:00"
    report = {
        "schema_version": "toledo_orchestrator.pong.v1",
        "finished_at": switched_at,
        "pings": [{
            "provider": "claude",
            "requested_model": "claude-opus-4-8",
            "requested_effort": "low",
            "verified_model": "claude-opus-4-8",
            "model_verified": True,
            "effort_verified": True,
            "effort_cli_accepted": True,
            "observed_effort": "low",
            "kind": "resume-model-switch",
            "session_action": "continue",
            "at": switched_at,
            "ok": True,
        }, {
            "provider": "claude",
            "requested_model": "claude-sonnet-5",
            "verified_model": "claude-opus-4-8",
            "model_verified": False,
            "kind": "resume-model-switch",
            "session_action": "continue",
            "at": switched_at,
            "ok": False,
        }],
    }
    atomic_write(tmp_path / catalog.PONG_RELATIVE_PATH, json.dumps(report).encode("utf-8"))
    models = [
        {"provider": "claude", "selection_token": "claude-opus-4-8"},
        {"provider": "claude", "selection_token": "claude-sonnet-5"},
    ]

    catalog._merge_pong_evidence(tmp_path, models)

    opus = models[0]["live_verified"]
    assert opus["resume_switch_observed"] is True
    assert opus["resume_switch_observed_at"] == switched_at
    assert "live_verified" not in models[1]
