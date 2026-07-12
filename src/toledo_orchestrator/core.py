from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .project import ProjectDefinition, load_projects


ALLOWED_NEXT = frozenset({"continue", "ready", "human"})
FENCE = re.compile(r"```orchestrator\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
RUN_ID = re.compile(r"run_\d{8}T\d{6}Z_[0-9a-f]{8}")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> str:
    """Write bytes without shell mediation or newline normalization."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(data)
        temp = Path(handle.name)
    os.replace(temp, path)
    return sha256(data)


def write_text(path: Path, text: str) -> str:
    return atomic_write(path, text.encode("utf-8"))


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict[str, Any]) -> str:
    return write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def decode_text_artifact(data: bytes, label: str) -> str:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} must be valid UTF-8") from error
    if data.startswith(b"\xef\xbb\xbf") or text.startswith("\ufeff"):
        raise ValueError(f"{label} must be UTF-8 without BOM")
    return text


@dataclass(frozen=True)
class Directive:
    next: str
    ignored_fields: tuple[str, ...] = ()
    source: str = "fenced"
    conflict: bool = False
    valid_block_count: int = 1


def extract_directive(text: str) -> Directive | None:
    """Read native JSON or the final well-formed fence and flag ambiguity."""
    try:
        native = json.loads(text.strip())
    except json.JSONDecodeError:
        native = None
    if isinstance(native, dict) and native.get("next") in ALLOWED_NEXT:
        return Directive(next=native["next"], ignored_fields=tuple(sorted(key for key in native if key != "next")), source="native")
    candidates: list[tuple[re.Match[str], dict[str, Any]]] = []
    for match in FENCE.finditer(text):
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or value.get("next") not in ALLOWED_NEXT:
            continue
        candidates.append((match, value))
    final = [(match, value) for match, value in candidates if not text[match.end():].strip()]
    if not final:
        return None
    _, value = final[-1]
    values = {candidate["next"] for _, candidate in candidates}
    return Directive(
        next=value["next"],
        ignored_fields=tuple(sorted(key for key in value if key != "next")),
        conflict=len(values) > 1,
        valid_block_count=len(candidates),
    )


def has_substantive_work(text: str, directive: Directive | None) -> bool:
    if directive is not None and directive.source == "native":
        return False
    return bool(work_product_text(text))


def work_product_text(text: str) -> str:
    """Remove process-control fences from the derivative work-product channel."""
    return FENCE.sub("", text).strip()


@dataclass
class ProviderResult:
    provider: str
    route: str
    stdout: bytes
    stderr: bytes = b""
    exit_code: int = 0
    elapsed_ms: int = 0
    observed_model: str | None = None
    session_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    response_text: str | None = None

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")


class ProviderAdapter:
    provider: str

    def __init__(self, executable: str | None = None, timeout: int = 300) -> None:
        self.executable = executable or self.provider
        self.timeout = timeout

    def executable_path(self) -> str:
        """Prefer real Windows executables over npm's extensionless/PowerShell shims."""
        if self.executable != self.provider:
            return self.executable
        if os.name == "nt":
            # The desktop-app copy can be non-executable to child processes; npm's .cmd shim is reliable.
            shim = shutil.which(f"{self.provider}.cmd")
            if shim:
                return shim
            native = shutil.which(f"{self.provider}.exe")
            if native:
                return native
        return self.executable

    def invoke(self, route: str, prompt: bytes, working_directory: Path) -> ProviderResult:
        raise NotImplementedError

    @staticmethod
    def _probe(command: list[str], cwd: Path | None = None, timeout: int = 15) -> tuple[int, str, str]:
        try:
            result = subprocess.run(command, cwd=cwd, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            return 1, "", str(error)
        return result.returncode, result.stdout.decode("utf-8", errors="replace"), result.stderr.decode("utf-8", errors="replace")

    def check(self) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _run(command: list[str], prompt: bytes, cwd: Path, timeout: int) -> tuple[bytes, bytes, int, int]:
        started = time.monotonic()
        try:
            completed = subprocess.run(command, input=prompt, cwd=cwd, capture_output=True, timeout=timeout)
            return completed.stdout, completed.stderr, completed.returncode, int((time.monotonic() - started) * 1000)
        except subprocess.TimeoutExpired as error:
            return error.stdout or b"", error.stderr or b"", 124, int((time.monotonic() - started) * 1000)
        except OSError as error:
            return b"", str(error).encode("utf-8", errors="replace"), 127, int((time.monotonic() - started) * 1000)


class CodexAdapter(ProviderAdapter):
    provider = "codex"

    def invoke(self, route: str, prompt: bytes, working_directory: Path) -> ProviderResult:
        command = [
            self.executable_path(), "exec", "--sandbox", "read-only",
            "-c", 'approval_policy="never"', "--skip-git-repo-check", "--json", "-",
        ]
        stdout, stderr, exit_code, elapsed = self._run(command, prompt, working_directory, self.timeout)
        return parse_codex_result(route, stdout, stderr, exit_code, elapsed)

    def check(self) -> dict[str, Any]:
        executable = self.executable_path()
        version_code, version_out, version_err = self._probe([executable, "--version"])
        auth_code, auth_out, auth_err = self._probe([executable, "login", "status"])
        help_code, help_out, help_err = self._probe([executable, "exec", "--help"])
        help_text = help_out + help_err
        capabilities = {
            "stdin": "instructions are read from stdin" in help_text,
            "jsonl": "--json" in help_text,
            "read_only": "--sandbox" in help_text and "read-only" in help_text,
            "session_resume": "resume" in help_text,
            "model_and_reasoning_controls": "--model" in help_text and "--config" in help_text,
            "structured_output_verified_but_unused": "--output-schema" in help_text,
        }
        authenticated = auth_code == 0 and "logged in" in (auth_out + auth_err).lower()
        ready = version_code == 0 and help_code == 0 and authenticated and all(capabilities.values())
        return {
            "provider": self.provider,
            "ready": ready,
            "version": (version_out or version_err).strip() or None,
            "authenticated": authenticated,
            "capabilities": capabilities,
            "generation": "not_probed",
            "model": "provider-default; observed per successful turn",
            "error": None if ready else "codex readiness check failed",
        }


class ClaudeAdapter(ProviderAdapter):
    provider = "claude"

    def executable_path(self) -> str:
        if self.executable != self.provider:
            return self.executable
        if os.name == "nt":
            appdata = os.environ.get("APPDATA", "")
            native = Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            if native.is_file():
                return str(native)
        return super().executable_path()

    def invoke(self, route: str, prompt: bytes, working_directory: Path) -> ProviderResult:
        command = [self.executable_path(), "-p", "--output-format", "json", "--permission-mode", "plan"]
        stdout, stderr, exit_code, elapsed = self._run(command, prompt, working_directory, self.timeout)
        return parse_claude_result(route, stdout, stderr, exit_code, elapsed)

    def check(self) -> dict[str, Any]:
        executable = self.executable_path()
        version_code, version_out, version_err = self._probe([executable, "--version"])
        auth_code, auth_out, _ = self._probe([executable, "auth", "status"])
        help_code, help_out, help_err = self._probe([executable, "--help"])
        help_text = help_out + help_err
        try:
            auth = json.loads(auth_out)
        except json.JSONDecodeError:
            auth = {}
        capabilities = {
            "stdin": "--input-format" in help_text,
            "json_envelope": "--output-format" in help_text,
            "read_only": "--permission-mode" in help_text and "plan" in help_text,
            "session_resume": "--resume" in help_text,
            "model_and_reasoning_controls": "--model" in help_text and "--effort" in help_text,
            "structured_output_verified_but_unused": "--json-schema" in help_text,
        }
        authenticated = auth_code == 0 and auth.get("loggedIn") is True
        ready = version_code == 0 and help_code == 0 and authenticated and all(capabilities.values())
        return {
            "provider": self.provider,
            "ready": ready,
            "version": (version_out or version_err).strip() or None,
            "authenticated": authenticated,
            "capabilities": capabilities,
            "generation": "not_probed",
            "model": "provider-default; observed per successful turn",
            "error": None if ready else "claude readiness check failed",
        }


def _nested_string(value: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(value, dict):
        for key in keys:
            found = value.get(key)
            if isinstance(found, str) and found.strip():
                return found.strip()
        for child in value.values():
            found = _nested_string(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _nested_string(child, keys)
            if found:
                return found
    return None


def parse_codex_result(route: str, stdout: bytes, stderr: bytes = b"", exit_code: int = 0, elapsed_ms: int = 0) -> ProviderResult:
    session_id = None
    observed_model = None
    usage: dict[str, Any] = {}
    provider_error = None
    response = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = session_id or _nested_string(event, ("thread_id", "session_id"))
        observed_model = observed_model or _nested_string(event, ("model",))
        if isinstance(event, dict) and event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = dict(event["usage"])
        if isinstance(event, dict) and event.get("type") in {"error", "turn.failed"}:
            provider_error = _nested_string(event, ("message",)) or "codex_turn_failed"
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            response = item["text"]
    effective_exit = exit_code or (1 if provider_error else 0)
    return ProviderResult("codex", route, stdout, stderr, effective_exit, elapsed_ms, observed_model, session_id, usage, provider_error, response)


def parse_claude_result(route: str, stdout: bytes, stderr: bytes = b"", exit_code: int = 0, elapsed_ms: int = 0) -> ProviderResult:
    session_id = None
    observed_model = None
    usage: dict[str, Any] = {}
    provider_error = None
    response = None
    try:
        envelope = json.loads(stdout.decode("utf-8"))
        session_id = envelope.get("session_id")
        observed_model = envelope.get("model")
        if isinstance(envelope.get("result"), str):
            response = envelope["result"]
        for key in ("total_cost_usd", "duration_ms", "duration_api_ms", "num_turns"):
            if key in envelope:
                usage[key] = envelope[key]
        if envelope.get("is_error") is True or str(envelope.get("subtype") or "").startswith("error"):
            provider_error = str(envelope.get("subtype") or "claude_result_error")
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    effective_exit = exit_code or (1 if provider_error else 0)
    return ProviderResult("claude", route, stdout, stderr, effective_exit, elapsed_ms, observed_model, session_id, usage, provider_error, response)


def result_text(result: ProviderResult) -> str:
    if result.response_text is not None:
        return result.response_text
    if result.provider in {"codex", "claude"}:
        return ""
    return result.text


class Orchestrator:
    """The M1 fixed-route controller; it intentionally has no semantic routing."""

    routes = (("codex-propose", "codex"), ("claude-review", "claude"), ("codex-revise", "codex"))

    prompt_files = {
        "codex-propose": "proposer.md",
        "claude-review": "reviewer.md",
        "codex-revise": "reviser.md",
    }

    def __init__(
        self,
        runtime_dir: Path | None = None,
        adapters: dict[str, ProviderAdapter] | None = None,
        projects: dict[str, ProjectDefinition] | None = None,
    ) -> None:
        self.runtime_dir = (runtime_dir or Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local")) / "ToledoOrchestrator").resolve()
        self.adapters = adapters or {"codex": CodexAdapter(), "claude": ClaudeAdapter()}
        self.projects = projects or load_projects()

    @property
    def runs_dir(self) -> Path:
        return self.runtime_dir / "runs"

    def _run_dir(self, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("invalid run id")
        return self.runs_dir / run_id

    def create_run(self, request: bytes, project: str = "toledo", workflow: str = "dev-review") -> str:
        if project not in self.projects or workflow != "dev-review":
            raise ValueError("M1 supports only a configured project and workflow=dev-review")
        definition = self.projects[project]
        decode_text_artifact(request, "request")
        project_check = definition.check()
        if not project_check["ready"]:
            raise ValueError(f"project is not ready: {project_check['error'] or project_check['instruction_files']}")
        source_revision = str(project_check["source_revision"])
        run_id = f"run_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"
        run_dir = self._run_dir(run_id)
        request_hash = atomic_write(run_dir / "request.md", request)
        state = {
            "schema_version": "toledo_orchestrator.run.v1",
            "run_id": run_id, "project": project, "workflow": workflow,
            "source_revision": source_revision,
            "project_root": str(definition.root),
            "status": "created", "current_turn": 0, "next_route": "codex-propose",
            "round": 0, "round_cap": 3, "turns": [], "pending_human_decision": None,
            "validations": {
                item.id: {"state": "pending", "command": item.command, "environment": item.environment}
                for item in definition.validations
            },
            "errors": [], "degraded": False, "decisions": [],
            "artifacts": {"request.md": {"sha256": request_hash}},
        }
        write_json(run_dir / "run.json", state)
        return run_id

    def state(self, run_id: str) -> dict[str, Any]:
        return read_json(self._run_dir(run_id) / "run.json")

    def _save(self, run_id: str, state: dict[str, Any]) -> None:
        write_json(self._run_dir(run_id) / "run.json", state)

    def check(self) -> dict[str, Any]:
        providers = {name: adapter.check() for name, adapter in self.adapters.items()}
        projects = {name: project.check() for name, project in self.projects.items()}
        route_status = [
            {"route": route, "provider": provider, "configured": provider in self.adapters, "permission": "read-only"}
            for route, provider in self.routes
        ]
        process_ready = (
            all(item["ready"] for item in providers.values())
            and all(item["ready"] for item in projects.values())
            and all(item["configured"] for item in route_status)
        )
        return {
            "status": "ready_for_generation_probe" if process_ready else "blocked",
            "ready": process_ready,
            "generation_verified": False,
            "providers": providers,
            "projects": projects,
            "workflow": {"id": "dev-review", "routes": route_status, "round_cap": 3},
            "runtime_dir": str(self.runtime_dir),
        }

    @staticmethod
    def _prompt_text(name: str) -> str:
        return (Path(__file__).with_name("prompts") / name).read_text(encoding="utf-8")

    def _prompt(self, state: dict[str, Any], request: bytes, route: str) -> bytes:
        context = []
        for turn in state["turns"]:
            if turn.get("correction") or not turn.get("substantive"):
                continue
            output = (self._run_dir(state["run_id"]) / "turns" / turn["output_file"]).read_text(encoding="utf-8")
            context.append(f"## {turn['route']} ({turn['id']})\n{output}")
        decisions = []
        for decision in state.get("decisions", []):
            text = (self._run_dir(state["run_id"]) / "decisions" / decision["file"]).read_text(encoding="utf-8")
            decisions.append(f"## {decision['file']} ({decision['reason']})\n{text}")
        project = self.projects[state["project"]]
        instructions = "\n".join(f"- {item}" for item in project.instruction_files)
        sections = [
            "You are operating one stage of the fixed, read-only Toledo dev-review workflow.",
            self._prompt_text(self.prompt_files[route]).strip(),
            "# Project boundary\n"
            f"Project root: {project.root}\nSource revision: {state['source_revision']}\n"
            "You may inspect this project, but you must not modify it. Follow these project instruction files:\n"
            f"{instructions}",
            "# Request\n" + request.decode("utf-8", errors="strict"),
            "# Prior substantive work\n" + ("\n\n".join(context) if context else "None."),
            "# Human decisions\n" + ("\n\n".join(decisions) if decisions else "None."),
            self._prompt_text("strict-contract.md").strip(),
        ]
        return ("\n\n".join(sections) + "\n").encode("utf-8")

    @staticmethod
    def _correction_prompt(original_prompt: bytes, original_output: str) -> bytes:
        return (
            "The response below contains substantive work but its process-control directive is missing, malformed, duplicated, or conflicting.\n"
            "Using the complete original exchange below, return only one valid directive fence. Do not repeat or revise the work product.\n\n"
            "# Original stage prompt\n" + original_prompt.decode("utf-8", errors="strict") + "\n\n"
            "# Original provider output\n" + original_output + "\n\n"
            "# Required correction\n```orchestrator\n{\"next\":\"continue\"}\n```\n"
            "Replace `continue` with `ready` or `human` only if that is the justified control decision. Return no other text.\n"
        ).encode("utf-8")

    def _store_turn(self, run_id: str, state: dict[str, Any], route: str, result: ProviderResult, prompt: bytes, directive: Directive | None, correction: bool = False) -> dict[str, Any]:
        number = state["current_turn"] + 1
        turn_id = f"turn.{number:04d}"
        turns = self._run_dir(run_id) / "turns"
        prompt_name = f"{turn_id}.prompt.md"
        raw_name = f"{turn_id}.output.raw"
        stderr_name = f"{turn_id}.stderr.raw"
        text_name = f"{turn_id}.output.md"
        prompt_hash = atomic_write(turns / prompt_name, prompt)
        raw_hash = atomic_write(turns / raw_name, result.stdout)
        stderr_hash = atomic_write(turns / stderr_name, result.stderr)
        output_text = result_text(result)
        work_text = work_product_text(output_text)
        output_hash = write_text(turns / text_name, work_text)
        substantive = result.exit_code == 0 and bool(work_text)
        record = {"id": turn_id, "route": route, "provider": result.provider, "prompt_file": prompt_name,
                  "output_file": text_name, "raw_file": raw_name, "stderr_file": stderr_name,
                  "prompt_sha256": prompt_hash, "raw_sha256": raw_hash, "stderr_sha256": stderr_hash,
                  "output_sha256": output_hash, "exit_code": result.exit_code, "elapsed_ms": result.elapsed_ms,
                  "session_id": result.session_id, "observed_model": result.observed_model, "usage": result.usage,
                  "directive": asdict(directive) if directive else None, "correction": correction,
                  "substantive": substantive, "provider_error": result.error}
        write_json(turns / f"{turn_id}.json", record)
        state["current_turn"] = number
        state["turns"].append(record)
        return record

    def advance(self, run_id: str) -> dict[str, Any]:
        state = self.state(run_id)
        if state["status"] in {"complete", "paused", "failed"}:
            return state
        request = (self._run_dir(run_id) / "request.md").read_bytes()
        route = state["next_route"]
        provider = dict(self.routes)[route]
        adapter = self.adapters[provider]
        prompt = self._prompt(state, request, route)
        project = self.projects[state["project"]]
        if str(project.root) != state["project_root"]:
            state["status"] = "paused"
            state["pending_human_decision"] = "project_definition_changed"
            state["errors"].append("project_definition_changed")
            self._save(run_id, state)
            return state
        current_revision = project.revision()
        if current_revision != state["source_revision"]:
            state["status"] = "paused"
            state["pending_human_decision"] = "project_revision_changed"
            state["errors"].append(f"project_revision_changed:{state['source_revision']}:{current_revision}")
            self._save(run_id, state)
            return state
        result = adapter.invoke(route, prompt, project.root)
        output = result_text(result)
        directive = extract_directive(output) if result.exit_code == 0 else None
        self._store_turn(run_id, state, route, result, prompt, directive)
        if result.exit_code != 0:
            state["status"] = "paused"; state["pending_human_decision"] = "provider_invocation_failed"; state["errors"].append(f"{route}:exit:{result.exit_code}")
        elif not has_substantive_work(output, directive):
            state["status"] = "paused"
            state["pending_human_decision"] = "missing_substantive_output"
            state["errors"].append(f"{route}:missing_substantive_output")
            directive = None
        elif directive is None or directive.conflict or directive.valid_block_count != 1:
            correction_prompt = self._correction_prompt(prompt, output)
            correction = adapter.invoke(route, correction_prompt, project.root)
            directive = extract_directive(result_text(correction)) if correction.exit_code == 0 else None
            if directive and (directive.conflict or directive.valid_block_count != 1):
                directive = None
            self._store_turn(run_id, state, route, correction, correction_prompt, directive, correction=True)
            if directive is None:
                state["status"] = "paused"; state["pending_human_decision"] = "malformed_directive"; state["errors"].append(f"{route}:malformed_directive")
        if directive:
            self._transition(state, route, directive)
        self._save(run_id, state)
        return state

    def _transition(self, state: dict[str, Any], route: str, directive: Directive) -> None:
        if directive.next == "human":
            state["status"] = "paused"; state["pending_human_decision"] = "provider_requested_human"; return
        if route == "codex-propose":
            state["next_route"] = "claude-review"; state["status"] = "running"; return
        if route == "claude-review":
            state["next_route"] = "codex-revise"; state["status"] = "running"; return
        state["round"] += 1
        if directive.next == "ready":
            state["status"] = "complete"; state["next_route"] = None
        elif state["round"] >= state["round_cap"]:
            state["status"] = "complete"; state["next_route"] = None; state["degraded"] = True; state["errors"].append("round_cap_reached")
        else:
            state["next_route"] = "claude-review"; state["status"] = "running"

    def run_to_stop(self, run_id: str) -> dict[str, Any]:
        while self.state(run_id)["status"] not in {"complete", "paused", "failed"}:
            self.advance(run_id)
        return self.state(run_id)

    def resume(self, run_id: str, decision: bytes) -> dict[str, Any]:
        state = self.state(run_id)
        if state["status"] != "paused":
            raise ValueError("only paused runs can be resumed")
        decode_text_artifact(decision, "decision")
        reason = str(state["pending_human_decision"] or "human_decision")
        decision_dir = self._run_dir(run_id) / "decisions"
        decision_id = f"decision.{len(list(decision_dir.glob('*.md'))) + 1:04d}.md"
        digest = atomic_write(decision_dir / decision_id, decision)
        state["pending_human_decision"] = None
        state["status"] = "running"
        state.setdefault("decisions", []).append({"file": decision_id, "sha256": digest, "reason": reason, "after_turn": state["current_turn"]})
        self._save(run_id, state)
        return self.run_to_stop(run_id)

    def attach_receipt(self, run_id: str, receipt_path: Path) -> dict[str, Any]:
        receipt = read_json(receipt_path)
        required = {"validation_id", "command", "source_revision", "environment", "host", "started_at", "finished_at", "exit_code", "stdout_path", "stderr_path", "stdout_sha256", "stderr_sha256"}
        missing = required - receipt.keys()
        if missing:
            raise ValueError(f"receipt missing fields: {','.join(sorted(missing))}")
        state = self.state(run_id)
        validation_id = str(receipt["validation_id"])
        expected_validation = state["validations"].get(validation_id)
        if expected_validation is None:
            raise ValueError(f"unknown validation id: {validation_id}")
        if receipt["source_revision"] != state["source_revision"]:
            raise ValueError("receipt source revision mismatch")
        if receipt["command"] != expected_validation["command"]:
            raise ValueError("receipt command mismatch")
        if receipt["environment"] not in {expected_validation["environment"], "either"}:
            raise ValueError("receipt environment mismatch")
        for key in ("stdout_path", "stderr_path"):
            data = Path(receipt[key]).read_bytes()
            expected = receipt[f"{key[:-5]}_sha256"]
            if sha256(data) != expected:
                raise ValueError(f"receipt hash mismatch: {key}")
        validation_dir = self._run_dir(run_id) / "validations"
        stdout_hash = atomic_write(validation_dir / f"{validation_id}.stdout.raw", Path(receipt["stdout_path"]).read_bytes())
        stderr_hash = atomic_write(validation_dir / f"{validation_id}.stderr.raw", Path(receipt["stderr_path"]).read_bytes())
        receipt_hash = atomic_write(validation_dir / f"{validation_id}.receipt.json", receipt_path.read_bytes())
        state["validations"][validation_id] = {
            **expected_validation,
            "state": "passed" if receipt["exit_code"] == 0 else "failed",
            "receipt_sha256": receipt_hash,
            "stdout_sha256": stdout_hash,
            "stderr_sha256": stderr_hash,
        }
        self._save(run_id, state)
        return state

    def show_turn(self, run_id: str, number: int) -> str:
        if number < 1:
            raise ValueError("turn number must be positive")
        return (self._run_dir(run_id) / "turns" / f"turn.{number:04d}.output.md").read_text(encoding="utf-8")
