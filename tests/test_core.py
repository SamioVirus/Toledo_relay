from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from toledo_orchestrator.core import (
    ClaudeAdapter,
    CodexAdapter,
    Orchestrator,
    ProviderAdapter,
    ProviderResult,
    atomic_write,
    extract_directive,
    has_substantive_work,
    parse_claude_result,
    parse_codex_result,
    observe_codex_rollout,
    result_text,
    sha256,
    work_product_text,
)
from toledo_orchestrator.project import ProjectDefinition, ValidationDefinition


FIXTURES = Path(__file__).with_name("fixtures")


class ScriptedAdapter(ProviderAdapter):
    def __init__(self, provider: str, responses: list[str]) -> None:
        self.provider = provider
        self.responses = iter(responses)
        self.prompts: list[bytes] = []
        self.working_directories: list[Path] = []

    def invoke(self, route: str, prompt: bytes, working_directory: Path) -> ProviderResult:
        self.prompts.append(prompt)
        self.working_directories.append(working_directory)
        response = next(self.responses)
        return ProviderResult(self.provider, route, response.encode("utf-8"), response_text=response)

    def check(self) -> dict[str, object]:
        return {"provider": self.provider, "ready": True, "generation": "fixture"}


def block(next_value: str, work: str = "work", extra: str = "") -> str:
    return f"{work}\n```orchestrator\n{{\"next\":\"{next_value}\"{extra}}}\n```"


@pytest.fixture
def project(tmp_path: Path) -> ProjectDefinition:
    root = tmp_path / "toledo"
    root.mkdir()
    (root / "AGENTS.md").write_text("# Test instructions\n", encoding="utf-8")
    (root / "plan.md").write_text("# Test plan\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "seed"],
        check=True,
        capture_output=True,
    )
    return ProjectDefinition(
        id="toledo",
        root=root.resolve(),
        read_only=True,
        instruction_files=("AGENTS.md", "plan.md"),
        validations=(
            ValidationDefinition("unit-tests", "python -m pytest -q", "local"),
            ValidationDefinition("compose-config", "docker compose config", "vps"),
        ),
    )


def make_app(tmp_path: Path, project: ProjectDefinition, codex: ProviderAdapter, claude: ProviderAdapter) -> Orchestrator:
    return Orchestrator(tmp_path / "runtime", {"codex": codex, "claude": claude}, {"toledo": project})


def test_atomic_storage_is_byte_exact_for_utf8_and_crlf(tmp_path: Path):
    payload = "café — curly 😀\r\n```powershell\r\n$HOME\r\n```\r\n".encode("utf-8")
    target = tmp_path / "artifact.raw"
    assert atomic_write(target, payload) == sha256(payload)
    assert target.read_bytes() == payload
    assert not target.read_bytes().startswith(b"\xef\xbb\xbf")


def test_directive_requires_a_final_block_and_reports_unknown_fields():
    assert extract_directive(block("ready") + "\ntrailing prose") is None
    directive = extract_directive(block("human", extra=',"route":"bad"'))
    assert directive and directive.next == "human" and directive.ignored_fields == ("route",)


def test_directive_flags_conflicting_blocks_and_native_control_only_output():
    directive = extract_directive(block("continue") + "\n" + block("ready"))
    assert directive and directive.next == "ready" and directive.conflict is True and directive.valid_block_count == 2
    native = extract_directive('{"next":"ready","ignored":true}')
    assert native and native.source == "native" and not has_substantive_work('{"next":"ready"}', native)


def test_v2_sentinel_preserves_embedded_orchestrator_fence():
    embedded = '```orchestrator\n{"next":"ready"}\n```'
    output = f"Protocol example:\n{embedded}\n\nKeep this example.\nORCHESTRATOR_DIRECTIVE_V2: {{\"next\":\"continue\"}}"
    directive = extract_directive(output)
    assert directive and directive.next == "continue"
    assert directive.source == "sentinel-v2" and directive.conflict is False
    assert directive.valid_block_count == 1
    work = work_product_text(output)
    assert embedded in work
    assert work == f"Protocol example:\n{embedded}\n\nKeep this example."
    assert has_substantive_work(output, directive)


def test_multiple_v2_sentinels_are_ambiguous_and_never_leak_into_work_product():
    output = (
        'Plan\nORCHESTRATOR_DIRECTIVE_V2: {"next":"continue"}\n'
        'More work\nORCHESTRATOR_DIRECTIVE_V2: {"next":"ready"}'
    )
    directive = extract_directive(output)
    assert directive and directive.next == "ready"
    assert directive.conflict is True and directive.valid_block_count == 2
    assert work_product_text(output) == "Plan\nMore work"


def test_fixed_workflow_transports_substantive_outputs_in_project_context(tmp_path: Path, project: ProjectDefinition):
    codex = ScriptedAdapter("codex", [block("continue", "Concrete proposal"), block("ready", "Revised proposal")])
    claude = ScriptedAdapter("claude", [block("continue", "Concrete review finding")])
    app = make_app(tmp_path, project, codex, claude)
    run_id = app.create_run("Need café review".encode())
    state = app.run_to_stop(run_id)
    assert state["status"] == "complete" and state["round"] == 1
    assert state["source_revision"] == project.revision()
    assert [turn["route"] for turn in state["turns"]] == ["codex-propose", "claude-review", "codex-revise"]
    assert b"Concrete proposal" in claude.prompts[0]
    assert b"Concrete review finding" in codex.prompts[1]
    assert b"Choose `continue` only when you can name at least one concrete unresolved issue" in claude.prompts[0]
    assert set(codex.working_directories + claude.working_directories) == {project.root}
    stored = tmp_path / "runtime" / "runs" / run_id / "turns" / "turn.0001.output.md"
    stored_text = stored.read_text(encoding="utf-8")
    assert "Concrete proposal" in stored_text and "orchestrator" not in stored_text


def test_provider_commands_keep_prompt_off_argv_and_do_not_constrain_work_product(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    marker = b"PRIVATE PROMPT caf\xc3\xa9"
    captured: list[tuple[list[str], bytes, Path]] = []

    def fake_run(command: list[str], prompt: bytes, cwd: Path, timeout: int):
        captured.append((command, prompt, cwd))
        if "codex" in Path(command[0]).name.lower():
            return (FIXTURES / "codex_success.jsonl").read_bytes(), b"", 0, 10
        return (FIXTURES / "claude_success.json").read_bytes(), b"", 0, 10

    codex = CodexAdapter(executable="codex-test")
    claude = ClaudeAdapter(executable="claude-test")
    monkeypatch.setattr(codex, "_run", fake_run)
    monkeypatch.setattr(claude, "_run", fake_run)
    codex.invoke("codex-propose", marker, tmp_path)
    claude.invoke("claude-review", marker, tmp_path)
    codex_command, claude_command = captured[0][0], captured[1][0]
    assert captured[0][1] == marker and captured[1][1] == marker
    assert all(marker.decode("utf-8") not in arg for command, _, _ in captured for arg in command)
    assert "--output-schema" not in codex_command and "--json-schema" not in claude_command
    assert "read-only" in codex_command and 'approval_policy="never"' in codex_command
    assert "--permission-mode" in claude_command and "plan" in claude_command


def test_codex_transcript_fixture_preserves_work_session_and_usage():
    stdout = (FIXTURES / "codex_success.jsonl").read_bytes()
    result = parse_codex_result("codex-propose", stdout)
    assert result.session_id == "codex-session-fixture"
    assert result.usage == {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 40}
    assert result_text(result).startswith("Concrete proposal with evidence.")
    assert extract_directive(result_text(result)).next == "continue"


def test_claude_transcript_fixture_preserves_work_session_and_usage():
    stdout = (FIXTURES / "claude_success.json").read_bytes()
    result = parse_claude_result("claude-review", stdout)
    assert result.session_id == "claude-session-fixture"
    assert result.usage["total_cost_usd"] == 0.0123
    assert result.observed_model == "claude-fable-5"
    assert result.observation_source == "claude-modelUsage"
    assert list(result.model_usage) == ["claude-fable-5"]
    assert result_text(result).startswith("Independent review finding.")
    assert extract_directive(result_text(result)).next == "continue"


def test_codex_rollout_observation_is_best_effort_and_reads_effective_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "codex-home"
    session_id = "019f57ad-4863-7671-b450-ad0d9c3cd25e"
    rollout = home / "sessions" / "2026" / "07" / "12" / f"rollout-test-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps({"type": "turn_context", "payload": {"model": "gpt-test", "effort": "xhigh"}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert observe_codex_rollout(session_id) == ("gpt-test", "xhigh", "codex-rollout", None)
    missing = observe_codex_rollout("missing")
    assert missing[:3] == (None, None, None) and "not found" in missing[3]


def test_provider_error_envelopes_never_become_work_product():
    codex = parse_codex_result("codex-propose", (FIXTURES / "codex_usage_limit.jsonl").read_bytes(), exit_code=0)
    claude = parse_claude_result(
        "claude-review",
        b'{"type":"result","subtype":"error_during_execution","is_error":true,"result":"failure detail"}',
        exit_code=0,
    )
    assert codex.exit_code == 1 and codex.error == "usage limit reached" and result_text(codex) == ""
    assert codex.session_id == "codex-limited-session-fixture"
    assert claude.exit_code == 1 and claude.error == "error_during_execution"

    rate_limited = parse_claude_result(
        "claude-review",
        (FIXTURES / "claude_rate_limit.json").read_bytes(),
        exit_code=1,
    )
    assert rate_limited.error == "claude_api_error_429"
    assert rate_limited.exit_code == 1
    assert result_text(rate_limited) == "You've hit your limit"


def test_missing_provider_executable_becomes_captured_failure(tmp_path: Path):
    result = CodexAdapter(executable=str(tmp_path / "missing-codex.exe"), timeout=1).invoke("codex-propose", b"prompt", tmp_path)
    assert result.exit_code == 127 and result.stdout == b"" and result.stderr


def test_context_preserving_correction_does_not_replace_work_product(tmp_path: Path, project: ProjectDefinition):
    codex = ScriptedAdapter("codex", ["Proposal missing its directive", block("continue", work=""), block("ready", "Final revision")])
    claude = ScriptedAdapter("claude", [block("continue", "Review")])
    app = make_app(tmp_path, project, codex, claude)
    state = app.run_to_stop(app.create_run(b"request"))
    assert state["status"] == "complete"
    assert state["turns"][1]["correction"] is True
    assert b"# Original stage prompt" in codex.prompts[1]
    assert b"Proposal missing its directive" in codex.prompts[1]
    assert b"Proposal missing its directive" in claude.prompts[0]
    assert b"# Required correction" not in claude.prompts[0]


def test_conflicting_directives_are_corrected_and_stripped_from_transport(tmp_path: Path, project: ProjectDefinition):
    conflicting = "Proposal\n```orchestrator\n{\"next\":\"continue\"}\n```\n```orchestrator\n{\"next\":\"ready\"}\n```"
    codex = ScriptedAdapter("codex", [conflicting, block("continue", work=""), block("ready", "Final revision")])
    claude = ScriptedAdapter("claude", [block("continue", "Review")])
    app = make_app(tmp_path, project, codex, claude)
    state = app.run_to_stop(app.create_run(b"request"))
    assert state["status"] == "complete" and state["turns"][0]["directive"]["conflict"] is True
    assert b"Proposal" in claude.prompts[0]
    first_output = tmp_path / "runtime" / "runs" / state["run_id"] / "turns" / "turn.0001.output.md"
    assert first_output.read_text(encoding="utf-8") == "Proposal"


def test_control_only_primary_output_pauses_instead_of_completing_empty(tmp_path: Path, project: ProjectDefinition):
    codex = ScriptedAdapter("codex", ['{"next":"ready"}'])
    app = make_app(tmp_path, project, codex, ScriptedAdapter("claude", []))
    state = app.run_to_stop(app.create_run(b"request"))
    assert state["status"] == "paused"
    assert state["pending_human_decision"] == "missing_substantive_output"
    assert len(codex.prompts) == 1


def test_resume_moves_decision_and_drives_run_to_next_stop(tmp_path: Path, project: ProjectDefinition):
    codex = ScriptedAdapter(
        "codex",
        [block("human", "Need scope choice"), block("continue", "Proposal using approved scope"), block("ready", "Final revision")],
    )
    claude = ScriptedAdapter("claude", [block("continue", "Review")])
    app = make_app(tmp_path, project, codex, claude)
    run_id = app.create_run(b"request")
    paused = app.run_to_stop(run_id)
    assert paused["status"] == "paused"
    decision = "Use the narrow scope — approved.\r\n".encode("utf-8")
    complete = app.resume(run_id, decision)
    assert complete["status"] == "complete"
    assert codex.prompts[0] != codex.prompts[1]
    assert decision.rstrip() in codex.prompts[1]
    saved = tmp_path / "runtime" / "runs" / run_id / "decisions" / "decision.0001.md"
    assert saved.read_bytes() == decision


def test_missing_directive_gets_one_correction_then_pauses(tmp_path: Path, project: ProjectDefinition):
    codex = ScriptedAdapter("codex", ["substantive proposal", "still bad"])
    app = make_app(tmp_path, project, codex, ScriptedAdapter("claude", []))
    state = app.run_to_stop(app.create_run(b"x"))
    assert state["status"] == "paused" and state["pending_human_decision"] == "malformed_directive"
    assert state["current_turn"] == 2 and state["turns"][-1]["correction"] is True


def test_round_cap_is_enforced_without_model_agreement(tmp_path: Path, project: ProjectDefinition):
    codex = ScriptedAdapter("codex", [block("continue", f"codex {index}") for index in range(4)])
    claude = ScriptedAdapter("claude", [block("continue", f"review {index}") for index in range(3)])
    app = make_app(tmp_path, project, codex, claude)
    state = app.run_to_stop(app.create_run(b"x"))
    assert state["round"] == 3 and state["degraded"] is True and "round_cap_reached" in state["errors"]


def test_project_revision_change_pauses_before_provider_invocation(tmp_path: Path, project: ProjectDefinition):
    codex = ScriptedAdapter("codex", [block("continue", "proposal")])
    app = make_app(tmp_path, project, codex, ScriptedAdapter("claude", []))
    run_id = app.create_run(b"x")
    (project.root / "new.md").write_text("new\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project.root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(project.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "move"],
        check=True,
        capture_output=True,
    )
    state = app.run_to_stop(run_id)
    assert state["status"] == "paused" and state["pending_human_decision"] == "project_revision_changed"
    assert codex.prompts == []


def test_request_rejects_invalid_utf8_and_bom(tmp_path: Path, project: ProjectDefinition):
    app = make_app(tmp_path, project, ScriptedAdapter("codex", []), ScriptedAdapter("claude", []))
    with pytest.raises(ValueError, match="valid UTF-8"):
        app.create_run(b"\xff")
    with pytest.raises(ValueError, match="without BOM"):
        app.create_run(b"\xef\xbb\xbfrequest")


def test_run_ids_and_project_instruction_paths_reject_traversal(tmp_path: Path, project: ProjectDefinition):
    app = make_app(tmp_path, project, ScriptedAdapter("codex", []), ScriptedAdapter("claude", []))
    with pytest.raises(ValueError, match="invalid run id"):
        app.state("../outside")
    with pytest.raises(ValueError, match="escapes root"):
        ProjectDefinition("bad", project.root, True, ("../secret.md",), ())


def test_resume_rejects_non_utf8_decision_before_writing(tmp_path: Path, project: ProjectDefinition):
    codex = ScriptedAdapter("codex", [block("human", "Need decision")])
    app = make_app(tmp_path, project, codex, ScriptedAdapter("claude", []))
    run_id = app.create_run(b"request")
    app.run_to_stop(run_id)
    with pytest.raises(ValueError, match="valid UTF-8"):
        app.resume(run_id, b"\xff")
    assert not (tmp_path / "runtime" / "runs" / run_id / "decisions").exists()


def receipt_for(project: ProjectDefinition, stdout: Path, stderr: Path, **overrides: object) -> dict[str, object]:
    receipt: dict[str, object] = {
        "validation_id": "unit-tests",
        "command": "python -m pytest -q",
        "source_revision": project.revision(),
        "environment": "local",
        "host": "test",
        "started_at": "a",
        "finished_at": "b",
        "exit_code": 0,
        "stdout_path": str(stdout),
        "stderr_path": str(stderr),
        "stdout_sha256": sha256(stdout.read_bytes()),
        "stderr_sha256": sha256(stderr.read_bytes()),
    }
    receipt.update(overrides)
    return receipt


def test_receipt_rejects_hash_and_revision_mismatches(tmp_path: Path, project: ProjectDefinition):
    app = make_app(tmp_path, project, ScriptedAdapter("codex", []), ScriptedAdapter("claude", []))
    run_id = app.create_run(b"x")
    stdout = tmp_path / "stdout.raw"
    stderr = tmp_path / "stderr.raw"
    atomic_write(stdout, b"ok")
    atomic_write(stderr, b"")
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt_for(project, stdout, stderr, stdout_sha256="wrong")), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        app.attach_receipt(run_id, path)
    path.write_text(json.dumps(receipt_for(project, stdout, stderr, source_revision="wrong")), encoding="utf-8")
    with pytest.raises(ValueError, match="revision mismatch"):
        app.attach_receipt(run_id, path)


def test_valid_receipt_closes_configured_validation(tmp_path: Path, project: ProjectDefinition):
    app = make_app(tmp_path, project, ScriptedAdapter("codex", []), ScriptedAdapter("claude", []))
    run_id = app.create_run(b"x")
    stdout = tmp_path / "stdout.raw"
    stderr = tmp_path / "stderr.raw"
    atomic_write(stdout, b"ok")
    atomic_write(stderr, b"")
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt_for(project, stdout, stderr)), encoding="utf-8")
    state = app.attach_receipt(run_id, path)
    assert state["validations"]["unit-tests"]["state"] == "passed"
    validation_dir = tmp_path / "runtime" / "runs" / run_id / "validations"
    assert (validation_dir / "unit-tests.stdout.raw").read_bytes() == b"ok"
    assert (validation_dir / "unit-tests.stderr.raw").read_bytes() == b""


def test_check_reports_auth_capability_and_project_readiness(tmp_path: Path, project: ProjectDefinition):
    app = make_app(tmp_path, project, ScriptedAdapter("codex", []), ScriptedAdapter("claude", []))
    result = app.check()
    assert result["ready"] is True
    assert result["generation_verified"] is False
    assert result["projects"]["toledo"]["source_revision"] == project.revision()
    assert [item["route"] for item in result["workflow"]["routes"]] == ["codex-propose", "claude-review", "codex-revise"]
