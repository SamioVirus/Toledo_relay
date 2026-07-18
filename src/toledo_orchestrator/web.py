from __future__ import annotations

import ipaddress
import json
import secrets
import subprocess
import threading
import urllib.parse
from datetime import datetime, timezone
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .configuration import (
    load_configured_projects,
    load_configured_workflows,
    save_project_value,
    save_workflow_variant,
    update_profiles,
)
from .catalog import load_catalog, refresh_catalog, research_catalog, validate_selection
from .core import read_json
from .cycle import CycleOrchestrator
from .director import state_caption
from .turn_summaries import TurnSummaryWorkers


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
        before_start: Callable[[], object] | None = None,
    ) -> None:
        with self._lock:
            active = self._threads.get(run_id)
            if active and active.is_alive():
                raise ValueError("this run already has an active operation")
            self._errors.pop(run_id, None)
            # Gate actions use this synchronous hook to validate and persist
            # their displayed override after the worker slot is reserved but
            # before a provider-capable thread can exist.
            if before_start is not None:
                before_start()

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


def _persist_submitted_next_turn_override(
    engine: CycleOrchestrator,
    run_id: str,
    value: dict[str, Any],
) -> None:
    """Validate and persist a gate selection before its action is scheduled.

    Run/Retry submit the displayed picker state with the action request.  This
    synchronous boundary is deliberate: an invalid selection returns 400 and
    no background worker (and therefore no provider invocation) is started.
    """

    override = value.get("next_turn_override")
    if override is None:
        return
    if not isinstance(override, dict):
        raise ValueError("next_turn_override must be an object")
    engine.set_next_turn_override(
        run_id,
        profile=str(override["profile"]) if override.get("profile") else None,
        model=str(override["model"]) if override.get("model") else None,
        effort=str(override["effort"]) if override.get("effort") else None,
        session_action=(
            str(override["session_action"])
            if override.get("session_action")
            else None
        ),
        custom=bool(override.get("custom")),
        stance=str(override["stance"]) if override.get("stance") else None,
    )


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


def make_handler(
    engine: CycleOrchestrator,
    workers: RunWorkers,
    nonce: str,
    server_started_at: str | None = None,
    server_revision: str | None = None,
    summary_workers: TurnSummaryWorkers | None = None,
) -> type[BaseHTTPRequestHandler]:
    ui_root = Path(__file__).with_name("ui").resolve()
    server_started_at = server_started_at or datetime.now(timezone.utc).isoformat()
    server_revision = server_revision or "unknown"

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

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 1_000_000:
                raise ValueError("request body is too large")
            return self.rfile.read(length)

        def _parse_json(self, data: bytes) -> dict[str, Any]:
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
                        "server": {"started_at": server_started_at, "revision": server_revision},
                        "runtime_dir": str(engine.runtime_dir),
                        "catalog": load_catalog(engine.runtime_dir),
                        "projects": {key: value.check() for key, value in projects.items()},
                        "workflows": {key: value.public_summary() for key, value in workflows.items()},
                        "runs": _run_summaries(engine, workers),
                    })
                    return
                if path == "/api/runs":
                    self._send(_run_summaries(engine, workers))
                    return
                parts = [value for value in path.split("/") if value]
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "next-turn":
                    self._send(engine.next_turn_preview(parts[2]))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "workflows"] and parts[3] == "stage-prompt":
                    query = urllib.parse.parse_qs(parsed.query)
                    self._send(engine.stage_prompt_template(parts[2], query.get("stage", [""])[0]))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "head":
                    # Cheap change-detection so idle polling does not re-read every
                    # turn artifact each tick. event_sequence advances on every run
                    # event; worker status is not an event, so it is included too.
                    run_id = parts[2]
                    state = engine.state(run_id)
                    self._send({
                        "run_id": run_id,
                        "event_sequence": state.get("event_sequence", 0),
                        "summary_sequence": summary_workers.revision(run_id) if summary_workers else 0,
                        "status": state.get("status"),
                        "current_turn": state.get("current_turn", 0),
                        "pending_human_decision": state.get("pending_human_decision"),
                        "worker": workers.status(run_id),
                    })
                    return
                if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                    run_id = parts[2]
                    state = _timeline_compatible(engine.state(run_id))
                    if summary_workers:
                        summary_workers.enrich(run_id, state)
                        state["summary_sequence"] = summary_workers.revision(run_id)
                    state["state_caption"] = state_caption(state)
                    state["steer"] = (
                        engine.steer_availability(run_id)
                        if state.get("schema_version") == "toledo_orchestrator.run.v2"
                        else {"available": False, "reason": "legacy run schema"}
                    )
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
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "export":
                    query = urllib.parse.parse_qs(parsed.query)
                    plain_text = query.get("format", ["markdown"])[0] == "text"
                    include_prompts = query.get("prompts", ["1"])[0] != "0"
                    include_diagnostics = query.get("diagnostics", ["0"])[0] == "1"
                    data = engine.export_run(
                        parts[2],
                        plain_text=plain_text,
                        include_prompts=include_prompts,
                        include_diagnostics=include_diagnostics,
                    )
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
                # Drain the request body before rejecting authentication. On
                # Windows, closing a connection with unread request bytes can
                # reset the socket before the client receives the intended 403.
                body = self._read_body()
                self._authorize_write()
                value = self._parse_json(body)
                parsed = urllib.parse.urlparse(self.path)
                parts = [item for item in parsed.path.split("/") if item]
                if parsed.path == "/api/runs":
                    request = str(value.get("request", "")).encode("utf-8")
                    if not request.strip():
                        raise ValueError("request cannot be empty")
                    overrides = value.get("profile_overrides")
                    if overrides is not None and not isinstance(overrides, dict):
                        raise ValueError("profile_overrides must be an object")
                    round_overrides = value.get("round_overrides")
                    if round_overrides is not None and not isinstance(round_overrides, dict):
                        raise ValueError("round_overrides must be an object")
                    prompt_overrides = value.get("prompt_overrides")
                    if prompt_overrides is not None and not isinstance(prompt_overrides, dict):
                        raise ValueError("prompt_overrides must be an object")
                    run_id = engine.create_run(
                        request,
                        str(value.get("project", "toledo")),
                        str(value.get("workflow", "continuous-development")),
                        run_mode=str(value.get("run_mode", "auto")),
                        profile_overrides=overrides,
                        round_overrides=round_overrides,
                        prompt_overrides=prompt_overrides,
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
                    follow_up = str(value.get("follow_up", "")).strip() or None
                    workers.start(
                        run_id,
                        lambda: engine.decide(run_id, choice, text, follow_up),
                        lambda error: engine.record_background_failure(run_id, error),
                        before_start=lambda: _persist_submitted_next_turn_override(
                            engine, run_id, value
                        ),
                    )
                    self._send({"run_id": run_id, "worker": workers.status(run_id)}, HTTPStatus.ACCEPTED)
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "continue":
                    run_id = parts[2]
                    direction = str(value.get("direction", "")).encode("utf-8")
                    workers.start(
                        run_id,
                        lambda: engine.continue_step(run_id, direction),
                        lambda error: engine.record_background_failure(run_id, error),
                        before_start=lambda: _persist_submitted_next_turn_override(
                            engine, run_id, value
                        ),
                    )
                    self._send({"run_id": run_id, "worker": workers.status(run_id)}, HTTPStatus.ACCEPTED)
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "stop":
                    note = str(value.get("note", "")).encode("utf-8")
                    self._send(engine.stop_run(parts[2], note))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "recover":
                    run_id = parts[2]
                    workers.start(
                        run_id,
                        lambda: engine.recover_run(run_id),
                        lambda error: engine.record_background_failure(run_id, error),
                    )
                    self._send({"run_id": run_id, "worker": workers.status(run_id)}, HTTPStatus.ACCEPTED)
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "override":
                    state = engine.set_next_turn_override(
                        parts[2],
                        profile=str(value["profile"]) if value.get("profile") else None,
                        model=str(value["model"]) if value.get("model") else None,
                        effort=str(value["effort"]) if value.get("effort") else None,
                        session_action=str(value["session_action"]) if value.get("session_action") else None,
                        custom=bool(value.get("custom")),
                        stance=str(value["stance"]) if value.get("stance") else None,
                    )
                    self._send(state)
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "steer":
                    run_id = parts[2]
                    workers.start(run_id, lambda: engine.steer(run_id, str(value.get("note", ""))), lambda error: engine.record_background_failure(run_id, error))
                    self._send({"run_id": run_id, "worker": workers.status(run_id)}, HTTPStatus.ACCEPTED)
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "backfill-captions":
                    self._send(engine.backfill_semantic_captions(parts[2], opt_in=bool(value.get("opt_in")), limit=int(value.get("limit", 1))))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "fork":
                    self._send(engine.fork_rewind(parts[2], rewind_to_turn=int(value.get("rewind_to_turn", 0)), opt_in=bool(value.get("opt_in"))))
                    return
                if parsed.path == "/api/profile":
                    workflow_id = str(value.get("workflow", "continuous-development"))
                    workflow = load_configured_workflows(engine.runtime_dir)[workflow_id]
                    bulk = value.get("profile_overrides")
                    if bulk is not None:
                        if not isinstance(bulk, dict) or not bulk:
                            raise ValueError("profile_overrides must be a non-empty object")
                        overrides = bulk
                        profile_id = None
                    else:
                        profile_id = str(value["profile"])
                        overrides = {
                            profile_id: {
                                key: value[key]
                                for key in ("provider", "model", "effort", "permission", "label", "custom")
                                if key in value
                            }
                        }
                    catalog = load_catalog(engine.runtime_dir, refresh=False)
                    # Discovery can be unavailable (for example a locked-down
                    # field host).  A stale/empty catalog warns the UI but is
                    # never a persistence or run blocker; when it has entries,
                    # the combination is validated deterministically.
                    if catalog.get("models"):
                        for selected_profile, changes in overrides.items():
                            if selected_profile not in workflow.profiles:
                                raise ValueError(f"unknown profile: {selected_profile}")
                            if not isinstance(changes, dict):
                                raise ValueError(f"profile override for {selected_profile} must be an object")
                            current = workflow.profiles[selected_profile]
                            provider = str(changes.get("provider") or current.provider)
                            model = str(changes["model"]) if "model" in changes else current.model
                            effort = str(changes["effort"]) if "effort" in changes else current.effort
                            validate_selection(
                                catalog, provider=provider, model=model,
                                effort=effort, custom=bool(changes.get("custom", current.custom)),
                            )
                    workflow = update_profiles(engine.runtime_dir, workflow_id, overrides)
                    engine.workflows = load_configured_workflows(engine.runtime_dir)
                    summary = workflow.public_summary()
                    saved = summary["profiles"][profile_id] if profile_id else {
                        "profiles": {key: summary["profiles"][key] for key in overrides}
                    }
                    self._send(saved)
                    return
                if parsed.path == "/api/workflows/save-as":
                    catalog = load_catalog(engine.runtime_dir, refresh=False)
                    validate_profile = (
                        (lambda **kwargs: validate_selection(catalog, **kwargs))
                        if catalog.get("models")
                        else None
                    )
                    workflow = save_workflow_variant(
                        engine.runtime_dir,
                        str(value.get("base_workflow", "")),
                        str(value.get("id", "")),
                        str(value.get("label", "")),
                        profile_overrides=value.get("profile_overrides") or None,
                        round_overrides=value.get("round_overrides") or None,
                        prompt_overrides=value.get("prompt_overrides") or None,
                        validate_profile=validate_profile,
                    )
                    engine.workflows = load_configured_workflows(engine.runtime_dir)
                    self._send(workflow.public_summary())
                    return
                if parsed.path == "/api/catalog/refresh":
                    self._send(refresh_catalog(engine.runtime_dir))
                    return
                if parsed.path == "/api/catalog/research":
                    self._send(research_catalog(engine.runtime_dir))
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
    summary_workers = TurnSummaryWorkers(runtime_dir)
    summary_workers.start_watching()
    nonce = secrets.token_urlsafe(24)
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path.cwd(), capture_output=True, text=True, timeout=3, check=False
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        revision = "unknown"
    started_at = datetime.now(timezone.utc).isoformat()
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(engine, workers, nonce, started_at, revision, summary_workers),
    )
    url = f"http://{host}:{server.server_port}/"
    print(f"Toledo Orchestrator UI: {url}")
    print(f"Server start: {started_at} revision: {revision}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        summary_workers.stop_watching()
        server.server_close()
