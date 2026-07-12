from __future__ import annotations

import json
from pathlib import Path

from toledo_orchestrator.core import Orchestrator, ProviderAdapter, ProviderResult, atomic_write, extract_directive, sha256


class ScriptedAdapter(ProviderAdapter):
    def __init__(self, provider: str, responses: list[str]) -> None:
        self.provider = provider; self.responses = iter(responses); self.prompts: list[bytes] = []

    def invoke(self, route: str, prompt: bytes, working_directory: Path) -> ProviderResult:
        self.prompts.append(prompt)
        return ProviderResult(self.provider, route, next(self.responses).encode("utf-8"))


def block(next_value: str, extra: str = "") -> str:
    return f"work\n```orchestrator\n{{\"next\":\"{next_value}\"{extra}}}\n```"


def test_atomic_storage_is_byte_exact_for_utf8_and_crlf(tmp_path: Path):
    payload = "café — curly 😀\r\n```powershell\r\n$HOME\r\n```\r\n".encode("utf-8")
    target = tmp_path / "artifact.raw"
    assert atomic_write(target, payload) == sha256(payload)
    assert target.read_bytes() == payload
    assert not target.read_bytes().startswith(b"\xef\xbb\xbf")


def test_directive_uses_last_valid_fence_and_ignores_unknown_fields():
    directive = extract_directive("```orchestrator\n{bad}\n```\n```orchestrator\n{\"next\":\"human\",\"route\":\"bad\"}\n```")
    assert directive and directive.next == "human" and directive.ignored_fields == ("route",)


def test_directive_accepts_native_structured_json_before_fenced_fallback():
    directive = extract_directive('{"next":"ready","ignored":true}')
    assert directive and directive.source == "native" and directive.ignored_fields == ("ignored",)


def test_fixed_workflow_transports_prior_output_and_completes(tmp_path: Path):
    codex = ScriptedAdapter("codex", [block("continue"), block("ready")])
    claude = ScriptedAdapter("claude", [block("continue")])
    app = Orchestrator(tmp_path, {"codex": codex, "claude": claude})
    run_id = app.create_run("Need café review".encode())
    state = app.run_to_stop(run_id)
    assert state["status"] == "complete" and state["round"] == 1
    assert [turn["route"] for turn in state["turns"]] == ["codex-propose", "claude-review", "codex-revise"]
    assert b"Need caf\xc3\xa9 review" in codex.prompts[0]
    assert b"codex-propose" in claude.prompts[0]
    turn = tmp_path / "runs" / run_id / "turns" / "turn.0001.output.raw"
    assert turn.read_bytes() == block("continue").encode()


def test_missing_directive_gets_one_correction_then_pauses(tmp_path: Path):
    codex = ScriptedAdapter("codex", ["no directive", "still bad"])
    app = Orchestrator(tmp_path, {"codex": codex, "claude": ScriptedAdapter("claude", [])})
    state = app.run_to_stop(app.create_run(b"x"))
    assert state["status"] == "paused" and state["pending_human_decision"] == "malformed_directive"
    assert state["current_turn"] == 2 and state["turns"][-1]["correction"] is True


def test_round_cap_is_enforced_without_model_agreement(tmp_path: Path):
    codex = ScriptedAdapter("codex", [block("continue")] * 4)
    claude = ScriptedAdapter("claude", [block("continue")] * 3)
    state = Orchestrator(tmp_path, {"codex": codex, "claude": claude}).run_to_stop(Orchestrator(tmp_path, {"codex": codex, "claude": claude}).create_run(b"x"))
    assert state["round"] == 3 and state["degraded"] is True and "round_cap_reached" in state["errors"]


def test_receipt_hash_mismatch_is_rejected(tmp_path: Path):
    app = Orchestrator(tmp_path, {"codex": ScriptedAdapter("codex", []), "claude": ScriptedAdapter("claude", [])})
    run_id = app.create_run(b"x")
    stdout = tmp_path / "stdout.raw"; atomic_write(stdout, b"ok")
    receipt = {"validation_id":"unit", "command":"python -m pytest", "source_revision":"abc", "environment":"vps", "host":"test", "started_at":"a", "finished_at":"b", "exit_code":0, "stdout_path":str(stdout), "stderr_path":str(stdout), "stdout_sha256":"wrong", "stderr_sha256":"wrong"}
    path = tmp_path / "receipt.json"; path.write_text(json.dumps(receipt), encoding="utf-8")
    try: app.attach_receipt(run_id, path)
    except ValueError as error: assert "hash mismatch" in str(error)
    else: raise AssertionError("hash mismatch was accepted")


def test_resume_stores_the_exact_decision_bytes(tmp_path: Path):
    codex = ScriptedAdapter("codex", [block("human")])
    app = Orchestrator(tmp_path, {"codex": codex, "claude": ScriptedAdapter("claude", [])})
    run_id = app.create_run(b"x")
    app.run_to_stop(run_id)
    decision = b"Approve caf\xc3\xa9\r\n"
    state = app.resume(run_id, decision)
    saved = tmp_path / "runs" / run_id / "decisions" / "decision.0001.md"
    assert state["status"] == "running" and saved.read_bytes() == decision
