from __future__ import annotations

import ipaddress
import json
import secrets
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .configuration import (
    load_configured_projects,
    load_configured_workflows,
    save_project_value,
    update_profile,
)
from .core import read_json
from .cycle import CycleOrchestrator


class RunWorkers:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._errors: dict[str, str] = {}

    def start(
        self,
        run_id: str,
        operation: Callable[[], object],
        on_error: Callable[[Exception], object] | None = None,
    ) -> None:
        with self._lock:
            active = self._threads.get(run_id)
            if active and active.is_alive():
                raise ValueError("this run already has an active operation")
            self._errors.pop(run_id, None)

            def target() -> None:
                try:
                    operation()
                except Exception as error:  # UI surfaces the failure; run evidence remains authoritative.
                    with self._lock:
                        self._errors[run_id] = f"{type(error).__name__}: {error}"
                    if on_error is not None:
                        try:
                            on_error(error)
                        except Exception:
                            pass

            thread = threading.Thread(target=target, name=f"orchestrator-{run_id}", daemon=True)
            self._threads[run_id] = thread
            thread.start()

    def status(self, run_id: str) -> dict[str, object]:
        with self._lock:
            thread = self._threads.get(run_id)
            return {"active": bool(thread and thread.is_alive()), "error": self._errors.get(run_id)}


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _run_summaries(engine: CycleOrchestrator, workers: RunWorkers) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for path in sorted(engine.runs_dir.glob("run_*/run.json"), reverse=True):
        try:
            state = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        run_id = str(state.get("run_id"))
        values.append({
            "run_id": run_id,
            "workflow": state.get("workflow"),
            "project": state.get("project"),
            "status": state.get("status"),
            "cycle": state.get("cycle", 1),
            "current_turn": state.get("current_turn", 0),
            "schema_version": state.get("schema_version"),
            "worker": workers.status(run_id),
        })
    return values


def _timeline_compatible(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("schema_version") == "toledo_orchestrator.run.v2":
        return state
    route_values = {
        "codex-propose": ("planning", "planner", "Ideas and why", "ideate", "A"),
        "claude-review": ("planning-review", "reviewer", "Independent review", "skeptic", "B"),
        "codex-revise": ("planning", "planner", "Adjudicate and refine", "adjudicate", "C"),
    }
    for turn in state.get("turns", []):
        phase, role, title, prompt_kind, label = route_values.get(
            str(turn.get("route")), ("legacy", "agent", str(turn.get("route")), "prompt", "?")
        )
        turn.setdefault("cycle", 1)
        turn.setdefault("stage", turn.get("route"))
        turn.setdefault("phase", phase)
        turn.setdefault("role", role)
        turn.setdefault("title", title)
        turn.setdefault("prompt_kind", prompt_kind)
        turn.setdefault("session_label", label)
        turn.setdefault("session_slot", role)
        turn.setdefault("session_action", "new")
        turn.setdefault("profile", f"{turn.get('provider')}-default")
        turn.setdefault("profile_label", f"{str(turn.get('provider')).title()} default")
        turn.setdefault("configured_model", "provider-default")
        turn.setdefault("configured_reasoning", "provider-default")
        turn.setdefault("permission", "read-only")
        turn.setdefault("artifact_type", "work-product")
    state.setdefault("cycle", 1)
    state.setdefault("working_revision", state.get("source_revision"))
    state["cycles"] = [{
        "id": "cycle.0001",
        "number": 1,
        "status": state.get("status"),
        "approved_handoff": None,
        "completion_receipt": None,
        "sessions": {},
    }]
    return state


def make_handler(engine: CycleOrchestrator, workers: RunWorkers, nonce: str) -> type[BaseHTTPRequestHandler]:
    ui_root = Path(__file__).with_name("ui").resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ToledoOrchestrator/0.2"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _headers(self, content_type: str, length: int, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()

        def _send(self, value: object, status: int = 200) -> None:
            payload = _json_bytes(value)
            self._headers("application/json; charset=utf-8", len(payload), status)
            self.wfile.write(payload)

        def _error(self, error: Exception, status: int = 400) -> None:
            self._send({"error": f"{type(error).__name__}: {error}"}, status)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise ValueError("request body is too large")
            data = self.rfile.read(length)
            value = json.loads(data.decode("utf-8")) if data else {}
            if not isinstance(value, dict):
                raise ValueError("JSON request body must be an object")
            return value

        def _authorize_write(self) -> None:
            if self.headers.get("X-Orchestrator-Nonce") != nonce:
                raise PermissionError("missing or invalid launch nonce")
            origin = self.headers.get("Origin")
            if origin:
                parsed = urllib.parse.urlparse(origin)
                if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                    raise PermissionError("state-changing requests must originate from the local UI")

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                if path == "/api/bootstrap":
                    projects = load_configured_projects(engine.runtime_dir)
                    workflows = load_configured_workflows(engine.runtime_dir)
                    self._send({
                        "nonce": nonce,
                        "runtime_dir": str(engine.runtime_dir),
                        "projects": {key: value.check() for key, value in projects.items()},
                        "workflows": {key: value.public_summary() for key, value in workflows.items()},
                        "runs": _run_summaries(engine, workers),
                    })
                    return
                if path == "/api/runs":
                    self._send(_run_summaries(engine, workers))
                    return
                parts = [value for value in path.split("/") if value]
                if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                    run_id = parts[2]
                    state = _timeline_compatible(engine.state(run_id))
                    run_dir = engine._run_dir(run_id)
                    for turn in state.get("turns", []):
                        try:
                            output = (run_dir / "turns" / turn["output_file"]).read_text(encoding="utf-8")
                        except (OSError, KeyError):
                            output = ""
                        turn["preview"] = output[:600]
                    state["worker"] = workers.status(run_id)
                    self._send(state)
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "artifact":
                    query = urllib.parse.parse_qs(parsed.query)
                    relative = query.get("path", [""])[0]
                    data = engine.artifact(parts[2], relative)
                    self._headers("text/plain; charset=utf-8", len(data))
                    self.wfile.write(data)
                    return
                self._static(path)
            except ValueError as error:
                self._error(error, 404)
            except Exception as error:
                self._error(error, 500)

        def _static(self, path: str) -> None:
            relative = "index.html" if path in {"", "/"} else path.lstrip("/")
            target = (ui_root / relative).resolve()
            if target != ui_root and ui_root not in target.parents:
                raise ValueError("static path escapes UI root")
            if not target.is_file():
                raise ValueError("resource does not exist")
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(target.suffix, "application/octet-stream")
            data = target.read_bytes()
            self._headers(content_type, len(data))
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._authorize_write()
                value = self._read_json()
                parsed = urllib.parse.urlparse(self.path)
                parts = [item for item in parsed.path.split("/") if item]
                if parsed.path == "/api/runs":
                    request = str(value.get("request", "")).encode("utf-8")
                    if not request.strip():
                        raise ValueError("request cannot be empty")
                    run_id = engine.create_run(
                        request,
                        str(value.get("project", "toledo")),
                        str(value.get("workflow", "continuous-development")),
                        run_mode=str(value.get("run_mode", "auto")),
                    )
                    workers.start(
                        run_id,
                        lambda: engine.run_to_stop(run_id),
                        lambda error: engine.record_background_failure(run_id, error),
                    )
                    self._send({"run_id": run_id, "worker": workers.status(run_id)}, HTTPStatus.ACCEPTED)
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "decision":
                    run_id = parts[2]
                    choice = str(value.get("choice", ""))
                    text = str(value.get("text", "")).encode("utf-8")
                    workers.start(
                        run_id,
                        lambda: engine.decide(run_id, choice, text),
                        lambda error: engine.record_background_failure(run_id, error),
                    )
                    self._send({"run_id": run_id, "worker": workers.status(run_id)}, HTTPStatus.ACCEPTED)
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "continue":
                    run_id = parts[2]
                    workers.start(
                        run_id,
                        lambda: engine.continue_step(run_id),
                        lambda error: engine.record_background_failure(run_id, error),
                    )
                    self._send({"run_id": run_id, "worker": workers.status(run_id)}, HTTPStatus.ACCEPTED)
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "override":
                    state = engine.set_next_turn_override(
                        parts[2],
                        profile=str(value["profile"]) if value.get("profile") else None,
                        session_action=str(value["session_action"]) if value.get("session_action") else None,
                    )
                    self._send(state)
                    return
                if parsed.path == "/api/profile":
                    workflow = update_profile(
                        engine.runtime_dir,
                        str(value.get("workflow", "continuous-development")),
                        str(value["profile"]),
                        model=str(value["model"]) if "model" in value else None,
                        effort=str(value["effort"]) if "effort" in value else None,
                        permission=str(value["permission"]) if "permission" in value else None,
                        label=str(value["label"]) if "label" in value else None,
                    )
                    engine.workflows = load_configured_workflows(engine.runtime_dir)
                    self._send(workflow.public_summary()["profiles"][str(value["profile"])])
                    return
                if parsed.path == "/api/project":
                    project = save_project_value(engine.runtime_dir, value)
                    engine.projects = load_configured_projects(engine.runtime_dir)
                    self._send(project.check())
                    return
                raise ValueError("unknown API operation")
            except PermissionError as error:
                self._error(error, 403)
            except ValueError as error:
                self._error(error, 400)
            except Exception as error:
                self._error(error, 500)

    return Handler


def serve(runtime_dir: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    if host == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
        if not address.is_loopback:
            raise ValueError("the UI may bind only to a loopback address")
    except ValueError:
        raise ValueError("the UI may bind only to localhost or an IPv4 loopback address")
    if address.version != 4:
        raise ValueError("the UI currently supports IPv4 loopback; use 127.0.0.1")
    engine = CycleOrchestrator(runtime_dir=runtime_dir)
    workers = RunWorkers()
    nonce = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((host, port), make_handler(engine, workers, nonce))
    url = f"http://{host}:{server.server_port}/"
    print(f"Toledo Orchestrator UI: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
