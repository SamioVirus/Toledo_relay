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
from typing import Any, Callable


DIRECTIVE_SCHEMA = {
    "type": "object",
    "properties": {"next": {"type": "string", "enum": ["continue", "ready", "human"]}},
    "required": ["next"],
    "additionalProperties": True,
}
ALLOWED_NEXT = frozenset({"continue", "ready", "human"})
FENCE = re.compile(r"```orchestrator\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


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


@dataclass(frozen=True)
class Directive:
    next: str
    ignored_fields: tuple[str, ...] = ()
    source: str = "fenced"


def extract_directive(text: str) -> Directive | None:
    """Use the last well-formed orchestrator fence; never partially honor fields."""
    try:
        native = json.loads(text.strip())
    except json.JSONDecodeError:
        native = None
    if isinstance(native, dict) and native.get("next") in ALLOWED_NEXT:
        return Directive(next=native["next"], ignored_fields=tuple(sorted(key for key in native if key != "next")), source="native")
    candidates: list[Directive] = []
    for match in FENCE.finditer(text):
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or value.get("next") not in ALLOWED_NEXT:
            continue
        ignored = tuple(sorted(key for key in value if key != "next"))
        candidates.append(Directive(next=value["next"], ignored_fields=ignored))
    return candidates[-1] if candidates else None


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
    def _run(command: list[str], prompt: bytes, cwd: Path, timeout: int) -> tuple[bytes, bytes, int, int]:
        started = time.monotonic()
        try:
            completed = subprocess.run(command, input=prompt, cwd=cwd, capture_output=True, timeout=timeout)
            return completed.stdout, completed.stderr, completed.returncode, int((time.monotonic() - started) * 1000)
        except subprocess.TimeoutExpired as error:
            return error.stdout or b"", error.stderr or b"", 124, int((time.monotonic() - started) * 1000)


class CodexAdapter(ProviderAdapter):
    provider = "codex"

    def invoke(self, route: str, prompt: bytes, working_directory: Path) -> ProviderResult:
        schema_path = (working_directory / ".orchestrator-directive.schema.json").resolve()
        write_json(schema_path, DIRECTIVE_SCHEMA)
        command = [self.executable_path(), "exec", "--skip-git-repo-check", "--json", "--output-schema", str(schema_path), "-"]
        stdout, stderr, exit_code, elapsed = self._run(command, prompt, working_directory, self.timeout)
        session_id = None
        observed_model = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = session_id or event.get("session_id") or event.get("thread_id")
            observed_model = observed_model or event.get("model")
        return ProviderResult(self.provider, route, stdout, stderr, exit_code, elapsed, observed_model, session_id)


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
        # Prompt bytes go exclusively to stdin.  The schema is transport configuration, not prompt data.
        command = [self.executable_path(), "-p", "--output-format", "json", "--json-schema", json.dumps(DIRECTIVE_SCHEMA), "--permission-mode", "plan"]
        stdout, stderr, exit_code, elapsed = self._run(command, prompt, working_directory, self.timeout)
        session_id = None
        observed_model = None
        usage: dict[str, Any] = {}
        try:
            envelope = json.loads(stdout.decode("utf-8"))
            session_id = envelope.get("session_id")
            observed_model = envelope.get("model")
            for key in ("total_cost_usd", "duration_ms", "duration_api_ms", "num_turns"):
                if key in envelope:
                    usage[key] = envelope[key]
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return ProviderResult(self.provider, route, stdout, stderr, exit_code, elapsed, observed_model, session_id, usage)


def result_text(result: ProviderResult) -> str:
    if result.provider == "claude":
        try:
            envelope = json.loads(result.stdout.decode("utf-8"))
            if isinstance(envelope.get("result"), str):
                return envelope["result"]
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    if result.provider == "codex":
        for line in reversed(result.stdout.splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            for key in ("text", "message", "output_text"):
                if isinstance(event.get(key), str):
                    return event[key]
            item = event.get("item")
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return item["text"]
    return result.text


class Orchestrator:
    """The M1 fixed-route controller; it intentionally has no semantic routing."""

    routes = (("codex-propose", "codex"), ("claude-review", "claude"), ("codex-revise", "codex"))

    def __init__(self, runtime_dir: Path | None = None, adapters: dict[str, ProviderAdapter] | None = None) -> None:
        self.runtime_dir = (runtime_dir or Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local")) / "ToledoOrchestrator").resolve()
        self.adapters = adapters or {"codex": CodexAdapter(), "claude": ClaudeAdapter()}

    @property
    def runs_dir(self) -> Path:
        return self.runtime_dir / "runs"

    def create_run(self, request: bytes, project: str = "toledo", workflow: str = "dev-review") -> str:
        if project != "toledo" or workflow != "dev-review":
            raise ValueError("M1 supports only project=toledo and workflow=dev-review")
        run_id = f"run_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"
        run_dir = self.runs_dir / run_id
        request_hash = atomic_write(run_dir / "request.md", request)
        state = {
            "schema_version": "toledo_orchestrator.run.v1",
            "run_id": run_id, "project": project, "workflow": workflow,
            "status": "created", "current_turn": 0, "next_route": "codex-propose",
            "round": 0, "round_cap": 3, "turns": [], "pending_human_decision": None,
            "validations": {}, "errors": [], "degraded": False,
            "artifacts": {"request.md": {"sha256": request_hash}},
        }
        write_json(run_dir / "run.json", state)
        return run_id

    def state(self, run_id: str) -> dict[str, Any]:
        return read_json(self.runs_dir / run_id / "run.json")

    def _save(self, run_id: str, state: dict[str, Any]) -> None:
        write_json(self.runs_dir / run_id / "run.json", state)

    def _prompt(self, state: dict[str, Any], request: bytes, route: str) -> bytes:
        context = []
        for turn in state["turns"]:
            output = (self.runs_dir / state["run_id"] / "turns" / turn["output_file"]).read_text(encoding="utf-8")
            context.append(f"## {turn['route']} ({turn['id']})\n{output}")
        role = {"codex-propose": "propose", "claude-review": "independently review", "codex-revise": "revise after review"}[route]
        return (b"You are the " + role.encode() + b" stage of a fixed, read-only Toledo dev-review workflow. "
                b"Do not choose a route, modify files, or honor routing/context fields.\n\n# Request\n" + request +
                b"\n\n# Prior transport\n" + "\n\n".join(context).encode("utf-8") +
                b"\n\nEnd with exactly one fenced `orchestrator` JSON block containing only next: continue, ready, or human. Unknown fields are ignored.")

    def _store_turn(self, run_id: str, state: dict[str, Any], route: str, result: ProviderResult, prompt: bytes, directive: Directive | None, correction: bool = False) -> dict[str, Any]:
        number = state["current_turn"] + 1
        turn_id = f"turn.{number:04d}"
        turns = self.runs_dir / run_id / "turns"
        prompt_name = f"{turn_id}.prompt.md"
        raw_name = f"{turn_id}.output.raw"
        stderr_name = f"{turn_id}.stderr.raw"
        text_name = f"{turn_id}.output.md"
        prompt_hash = atomic_write(turns / prompt_name, prompt)
        raw_hash = atomic_write(turns / raw_name, result.stdout)
        stderr_hash = atomic_write(turns / stderr_name, result.stderr)
        output_text = result_text(result)
        output_hash = write_text(turns / text_name, output_text)
        record = {"id": turn_id, "route": route, "provider": result.provider, "prompt_file": prompt_name,
                  "output_file": text_name, "raw_file": raw_name, "stderr_file": stderr_name,
                  "prompt_sha256": prompt_hash, "raw_sha256": raw_hash, "stderr_sha256": stderr_hash,
                  "output_sha256": output_hash, "exit_code": result.exit_code, "elapsed_ms": result.elapsed_ms,
                  "session_id": result.session_id, "observed_model": result.observed_model, "usage": result.usage,
                  "directive": asdict(directive) if directive else None, "correction": correction}
        write_json(turns / f"{turn_id}.json", record)
        state["current_turn"] = number
        state["turns"].append(record)
        return record

    def advance(self, run_id: str) -> dict[str, Any]:
        state = self.state(run_id)
        if state["status"] in {"complete", "paused", "failed"}:
            return state
        request = (self.runs_dir / run_id / "request.md").read_bytes()
        route = state["next_route"]
        provider = dict(self.routes)[route]
        adapter = self.adapters[provider]
        prompt = self._prompt(state, request, route)
        result = adapter.invoke(route, prompt, self.runs_dir / run_id)
        output = result_text(result)
        directive = extract_directive(output) if result.exit_code == 0 else None
        self._store_turn(run_id, state, route, result, prompt, directive)
        if result.exit_code != 0:
            state["status"] = "paused"; state["pending_human_decision"] = "provider_invocation_failed"; state["errors"].append(f"{route}:exit:{result.exit_code}")
        elif directive is None:
            correction_prompt = b"Return only one valid fenced orchestrator JSON directive. Allowed next values: continue, ready, human."
            correction = adapter.invoke(route, correction_prompt, self.runs_dir / run_id)
            directive = extract_directive(result_text(correction)) if correction.exit_code == 0 else None
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
        decision_id = f"decision.{len(list((self.runs_dir / run_id / 'decisions').glob('*.md'))) + 1:04d}.md"
        digest = atomic_write(self.runs_dir / run_id / "decisions" / decision_id, decision)
        state["pending_human_decision"] = None; state["status"] = "running"; state.setdefault("decisions", []).append({"file": decision_id, "sha256": digest})
        self._save(run_id, state)
        return state

    def attach_receipt(self, run_id: str, receipt_path: Path) -> dict[str, Any]:
        receipt = read_json(receipt_path)
        required = {"validation_id", "command", "source_revision", "environment", "host", "started_at", "finished_at", "exit_code", "stdout_sha256", "stderr_sha256"}
        missing = required - receipt.keys()
        if missing:
            raise ValueError(f"receipt missing fields: {','.join(sorted(missing))}")
        for key in ("stdout_path", "stderr_path"):
            if key in receipt:
                data = Path(receipt[key]).read_bytes()
                expected = receipt[f"{key[:-5]}_sha256"]
                if sha256(data) != expected:
                    raise ValueError(f"receipt hash mismatch: {key}")
        receipt_hash = atomic_write(self.runs_dir / run_id / "validations" / f"{receipt['validation_id']}.receipt.json", receipt_path.read_bytes())
        state = self.state(run_id)
        state["validations"][receipt["validation_id"]] = {"state": "passed" if receipt["exit_code"] == 0 else "failed", "receipt_sha256": receipt_hash}
        self._save(run_id, state)
        return state
