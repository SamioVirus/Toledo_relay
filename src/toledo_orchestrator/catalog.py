"""Deterministic, local provider capability catalog.

The catalog deliberately records what this machine can select rather than what
provider documentation happens to mention.  It is cacheable evidence, not a
network dependency or a run precondition.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import atomic_write, read_json, resolve_cli_executable


CATALOG_SCHEMA = "toledo_orchestrator.capability_catalog.v1"
CATALOG_RELATIVE_PATH = Path("catalog") / "capabilities.v1.json"
RESEARCH_RELATIVE_PATH = Path("catalog") / "research.v1.json"
STALE_AFTER_SECONDS = 7 * 24 * 60 * 60

# These are official model identifiers, intentionally marked curated rather
# than account-entitled.  Claude Code's installed binary has no account-aware
# listing command; successful Toledo observations add account evidence later.
# Full IDs only: headless `--model` silently ignores family aliases.
# Effort support is PER MODEL, from the official effort documentation
# (https://platform.claude.com/docs/en/build-with-claude/effort): Fable 5,
# Opus 4.8, and Sonnet 5 support low..max; Haiku 4.5 is not in the supported
# list, so it exposes only the provider default.
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CURATED_CLAUDE_MODELS = (
    ("claude-fable-5", "Claude Fable 5", CLAUDE_EFFORTS, "high"),
    ("claude-opus-4-8", "Claude Opus 4.8", CLAUDE_EFFORTS, "high"),
    ("claude-sonnet-5", "Claude Sonnet 5", CLAUDE_EFFORTS, "high"),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5", [], None),
)
DEFAULT_EFFORT_TOKEN = "default"
PONG_RELATIVE_PATH = Path("catalog") / "pong.v1.json"
APP_SERVER_TIMEOUT_SECONDS = 30.0
APP_SERVER_PAGE_LIMIT = 100
APP_SERVER_MAX_PAGES = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_text(command: list[str]) -> tuple[str, str | None]:
    resolved = [resolve_cli_executable(command[0]), *command[1:]]
    try:
        # Bytes, not text=True: on Windows text mode decodes with the console
        # codepage (cp1252) and CLI output containing UTF-8 punctuation kills
        # the reader thread, silently yielding stdout=None.
        completed = subprocess.run(resolved, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return "", f"{type(error).__name__}: {error}"
    stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
    stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
    if completed.returncode:
        return "", (stderr.strip() or f"exit {completed.returncode}")
    return stdout, None


def _observed_models(runtime_dir: Path) -> list[dict[str, str]]:
    observed: dict[tuple[str, str], dict[str, str]] = {}
    for path in runtime_dir.glob("runs/run_*/run.json"):
        try:
            state = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        for turn in state.get("turns", []):
            provider = str(turn.get("provider", ""))
            model = str(turn.get("observed_model") or "")
            if provider in {"codex", "claude"} and model:
                observed[(provider, model)] = {"provider": provider, "model": model}
    return list(observed.values())


def _codex_models_app_server(
    *, timeout_seconds: float = APP_SERVER_TIMEOUT_SECONDS,
) -> tuple[list[dict[str, Any]], str | None]:
    """Query the supported rich-client interface: `codex app-server` model/list.

    Protocol per the official app-server documentation: newline-delimited
    JSON-RPC over stdio, initialize handshake, then model/list whose
    `supportedReasoningEfforts` array order must be preserved. A reader thread
    is intentional: a blocking pipe ``readline`` cannot honor a wall-clock
    timeout on Windows.
    """
    executable = resolve_cli_executable("codex")
    try:
        process = subprocess.Popen(
            [executable, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        return [], f"{type(error).__name__}: {error}"

    messages: queue.Queue[tuple[str, Any]] = queue.Queue()

    def read_stdout() -> None:
        assert process.stdout is not None
        try:
            while True:
                line = process.stdout.readline()
                if not line:
                    messages.put(("eof", None))
                    return
                messages.put(("line", line))
        except (OSError, ValueError) as error:
            messages.put(("reader-error", error))

    reader = threading.Thread(target=read_stdout, name="codex-model-list-reader", daemon=True)
    reader.start()
    deadline = time.monotonic() + max(0.01, timeout_seconds)

    def send(message: dict[str, Any]) -> str | None:
        try:
            assert process.stdin is not None
            process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            process.stdin.flush()
        except (OSError, ValueError) as error:
            return f"app-server write failed: {type(error).__name__}: {error}"
        return None

    def wait_for(request_id: int) -> tuple[dict[str, Any] | None, str | None]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, f"no app-server response for request {request_id} within {timeout_seconds:g}s"
            try:
                kind, value = messages.get(timeout=remaining)
            except queue.Empty:
                return None, f"no app-server response for request {request_id} within {timeout_seconds:g}s"
            if kind == "eof":
                return None, f"app-server closed stdout before response {request_id}"
            if kind == "reader-error":
                return None, f"app-server stdout failed: {type(value).__name__}: {value}"
            try:
                message = json.loads(bytes(value).decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message, None

    raw_models: list[dict[str, Any]] = []
    try:
        assert process.stdin is not None and process.stdout is not None
        error = send({"id": 1, "method": "initialize", "params": {"clientInfo": {
            "name": "toledo_orchestrator", "title": "Toledo Orchestrator", "version": "0.2",
        }}})
        if error:
            return [], error
        initialized, error = wait_for(1)
        if error:
            return [], error
        if initialized is None or "error" in initialized:
            detail = (initialized or {}).get("error")
            return [], f"app-server initialize error: {json.dumps(detail)[:300]}"
        error = send({"method": "initialized", "params": {}})
        if error:
            return [], error

        cursor: str | None = None
        seen_cursors: set[str] = set()
        request_id = 2
        for _ in range(APP_SERVER_MAX_PAGES):
            params: dict[str, Any] = {"includeHidden": False, "limit": APP_SERVER_PAGE_LIMIT}
            if cursor is not None:
                params["cursor"] = cursor
            error = send({"id": request_id, "method": "model/list", "params": params})
            if error:
                return [], error
            listing, error = wait_for(request_id)
            if error:
                return [], error
            if listing is None or "error" in listing:
                detail = (listing or {}).get("error")
                return [], f"model/list error: {json.dumps(detail)[:300]}"
            result = listing.get("result") or {}
            data = result.get("data")
            if not isinstance(data, list):
                return [], "model/list response has no data array"
            raw_models.extend(item for item in data if isinstance(item, dict))
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                break
            cursor = str(next_cursor)
            if not cursor or cursor in seen_cursors:
                return [], "model/list returned an empty or repeated nextCursor"
            seen_cursors.add(cursor)
            request_id += 1
        else:
            return [], f"model/list exceeded {APP_SERVER_MAX_PAGES} pages"
    finally:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
    models: list[dict[str, Any]] = []
    for item in raw_models:
        if not isinstance(item, dict) or not item.get("id") or item.get("hidden"):
            continue
        efforts = []
        for level in item.get("supportedReasoningEfforts", []):
            # Entries are objects ({reasoningEffort, description}); order is
            # authoritative per the protocol documentation.
            value = level.get("reasoningEffort") if isinstance(level, dict) else level
            if value:
                efforts.append(str(value))
        speed_tiers = [str(value) for value in item.get("additionalSpeedTiers", []) if value]
        service_tiers = [
            {
                "id": str(tier["id"]),
                "name": str(tier.get("name") or tier["id"]),
                "description": str(tier.get("description") or "") or None,
            }
            for tier in item.get("serviceTiers", [])
            if isinstance(tier, dict) and tier.get("id")
        ]
        models.append({
            "provider": "codex", "selection_token": str(item["id"]),
            "display_name": str(item.get("displayName") or item.get("display_name") or item["id"]),
            "aliases": [], "supported_efforts": efforts,
            "default_effort": str(item.get("defaultReasoningEffort") or "") or None,
            "description": str(item.get("description") or "") or None,
            "model": str(item.get("model") or item["id"]),
            "is_default": bool(item.get("isDefault")),
            "input_modalities": [str(value) for value in item.get("inputModalities", []) if value],
            "supports_personality": bool(item.get("supportsPersonality")),
            "additional_speed_tiers": speed_tiers,
            "special_modes": speed_tiers,
            "service_tiers": service_tiers,
            "default_service_tier": item.get("defaultServiceTier"),
            "upgrade": item.get("upgrade"),
            "upgrade_info": item.get("upgradeInfo"),
            "availability_nux": item.get("availabilityNux"),
            "availability": "installed-account", "source": "codex app-server model/list",
        })
    if not models:
        return [], "model/list returned no visible models"
    return models, None


def _codex_models_debug() -> tuple[list[dict[str, Any]], str | None]:
    """Fallback listing via `codex debug models` (older builds)."""
    raw, error = _run_text(["codex", "debug", "models"])
    if error:
        return [], error
    try:
        values = json.loads(raw).get("models", [])
    except (json.JSONDecodeError, AttributeError) as parse_error:
        return [], f"invalid codex debug models JSON: {parse_error}"
    models = []
    for item in values:
        if not isinstance(item, dict) or not item.get("slug"):
            continue
        # Internal entries (e.g. codex-auto-review) report visibility "hide";
        # offering them in a picker would record selections the CLI never meant
        # to expose.
        if item.get("visibility") not in {None, "list"}:
            continue
        efforts = [str(level.get("effort")) for level in item.get("supported_reasoning_levels", []) if isinstance(level, dict) and level.get("effort")]
        models.append({
            "provider": "codex", "selection_token": str(item["slug"]),
            "display_name": str(item.get("display_name") or item["slug"]),
            "aliases": [], "supported_efforts": efforts,
            "default_effort": str(item.get("default_reasoning_level") or "") or None,
            "special_modes": [str(value) for value in item.get("additional_speed_tiers", [])],
            "availability": "installed-account", "source": "codex debug models",
            "supported_in_api": bool(item.get("supported_in_api")), "visibility": item.get("visibility"),
        })
    return models, None


def _codex_models() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    version, _ = _run_text(["codex", "--version"])
    metadata: dict[str, Any] = {"cli": "codex", "cli_version": version.strip(), "error": None}
    models, app_server_error = _codex_models_app_server()
    if models:
        metadata["listing"] = "app-server model/list"
        return models, metadata
    metadata["app_server_error"] = app_server_error
    models, debug_error = _codex_models_debug()
    if models:
        metadata["listing"] = "debug models (fallback)"
        return models, metadata
    metadata["error"] = f"app-server: {app_server_error}; debug models: {debug_error}"
    return [], metadata


def _claude_models() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    help_text, error = _run_text(["claude", "--help"])
    version, _ = _run_text(["claude", "--version"])
    supports_model = "--model" in help_text
    supports_effort = "--effort" in help_text
    # Do not infer this from documentation or similarly named commands. The
    # installed binary must advertise the exact execution capability.
    supports_ultracode = "ultracode" in help_text.lower()
    metadata: dict[str, Any] = {
        "cli": "claude", "cli_version": version.strip(), "error": error,
        "supports_model": supports_model, "supports_effort": supports_effort,
        "supports_ultracode": supports_ultracode,
    }
    if error or not supports_model:
        return [], metadata
    return [{
        "provider": "claude", "selection_token": token, "display_name": name,
        "aliases": [], "supported_efforts": list(efforts) if supports_effort else [],
        "default_effort": default_effort if supports_effort else None,
        "special_modes": ["ultracode"] if supports_ultracode else [],
        "availability": "curated-cli-compatible", "source": "curated official manifest + claude --help",
        "subscription_entitlement": "unknown",
    } for token, name, efforts, default_effort in CURATED_CLAUDE_MODELS], metadata


def catalog_path(runtime_dir: Path) -> Path:
    return runtime_dir / CATALOG_RELATIVE_PATH


def _installed_cli_versions(providers: set[str] | None = None) -> dict[str, str]:
    versions: dict[str, str] = {}
    for provider in sorted(providers or {"codex", "claude"}):
        value, error = _run_text([provider, "--version"])
        if not error and value.strip():
            versions[provider] = value.strip()
    return versions


def _catalog_cli_versions_changed(value: dict[str, Any]) -> bool:
    sources = value.get("sources") if isinstance(value.get("sources"), dict) else {}
    recorded = {
        provider: str(source.get("cli_version") or "").strip()
        for provider, source in sources.items()
        if provider in {"codex", "claude"} and isinstance(source, dict) and source.get("cli_version")
    }
    if not recorded:
        return False
    current = _installed_cli_versions(set(recorded))
    return any(current.get(provider) and current[provider] != version for provider, version in recorded.items())


def research_catalog(runtime_dir: Path) -> dict[str, Any]:
    """Build a read-only, local catalog research report without provider calls.

    It intentionally treats observed/configured values as leads, not
    entitlement. The launch catalog remains the only selectable authority.
    """
    catalog = load_catalog(runtime_dir)
    available = {
        (str(item.get("provider")), str(item.get("selection_token")))
        for item in catalog.get("models", [])
    }
    configured: list[dict[str, Any]] = []
    try:
        from .configuration import load_configured_workflows
        for workflow in load_configured_workflows(runtime_dir).values():
            for profile in workflow.profiles.values():
                configured.append({
                    "workflow": workflow.id, "profile": profile.id, "provider": profile.provider,
                    "model": profile.model, "effort": profile.effort,
                    "catalog_available": (profile.provider, profile.model) in available,
                    "custom": profile.custom,
                })
    except (OSError, ValueError) as error:
        configured.append({"error": f"{type(error).__name__}: {error}"})
    report = {
        "schema_version": "toledo_orchestrator.catalog_research.v1",
        "researched_at": _now(), "method": "deterministic-local-audit",
        "catalog_verified_at": catalog.get("verified_at"), "catalog_stale": bool(catalog.get("stale")),
        "observed_models": catalog.get("observed_models", []), "configured_profiles": configured,
        "note": "This report does not add entitlement or launch capabilities.",
    }
    atomic_write(runtime_dir / RESEARCH_RELATIVE_PATH, (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return report


def refresh_catalog(runtime_dir: Path) -> dict[str, Any]:
    """Query local CLIs only, atomically retaining the prior good catalog.

    Refreshes are merged per provider.  One broken CLI must not erase the
    other provider's last-good picker entries, while a successful discovery
    must replace that provider's old entries.  The refresh outcome is always
    persisted — including partial and total discovery failures. An unwritten
    failure would make every subsequent load retry discovery (a multi-second
    hang per page load) and would leave the UI with an empty picker and no
    explanation.
    """
    previous = load_catalog(runtime_dir, refresh=False)
    codex, codex_source = _codex_models()
    claude, claude_source = _claude_models()
    discovered_at = _now()
    discovered = {"codex": codex, "claude": claude}
    discovered_sources = {"codex": codex_source, "claude": claude_source}
    previous_models = previous.get("models") if isinstance(previous.get("models"), list) else []
    previous_sources = previous.get("sources") if isinstance(previous.get("sources"), dict) else {}

    models: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    provider_success: dict[str, bool] = {}
    for provider in ("codex", "claude"):
        fresh_models = discovered[provider]
        source = dict(discovered_sources[provider])
        provider_success[provider] = bool(fresh_models)
        if fresh_models:
            models.extend(dict(item) for item in fresh_models)
            source["stale"] = False
            source["verified_at"] = discovered_at
        else:
            retained = [
                dict(item) for item in previous_models
                if isinstance(item, dict) and item.get("provider") == provider
            ]
            models.extend(retained)
            source["error"] = str(source.get("error") or "discovery returned no selectable models")
            source["stale"] = True
            source["retained_model_count"] = len(retained)
            prior_source = previous_sources.get(provider)
            if isinstance(prior_source, dict):
                last_good_at = prior_source.get("verified_at")
            else:
                last_good_at = None
            last_good_at = last_good_at or previous.get("verified_at") or previous.get("discovered_at")
            if last_good_at:
                source["last_good_at"] = last_good_at
        sources[provider] = source

    observed = _observed_models(runtime_dir)
    _merge_pong_evidence(runtime_dir, models)
    all_succeeded = all(provider_success.values())
    any_succeeded = any(provider_success.values())
    verified_at = discovered_at if all_succeeded else (
        previous.get("verified_at") or discovered_at
    )
    value = {
        "schema_version": CATALOG_SCHEMA, "discovered_at": discovered_at,
        "verified_at": verified_at, "models": models,
        "observed_models": observed, "sources": sources,
        "last_refresh": {
            "at": discovered_at,
            "sources": sources,
            "succeeded": all_succeeded,
            "partial": any_succeeded and not all_succeeded,
        },
    }
    atomic_write(catalog_path(runtime_dir), (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return value


def run_pong_matrix(
    runtime_dir: Path,
    adapters: dict[str, Any],
    *,
    live: bool = False,
    matrix: list[tuple[str, str, str]] | None = None,
    excluded_models: set[str] | None = None,
    max_calls: int = 10,
    include_resume_probe: bool = False,
) -> dict[str, Any]:
    """Prove selectable model/effort combinations actually run, cheaply.

    Every provider call must be named explicitly in ``matrix``. There is no
    "all models" default: that made it too easy to consume a scarce model by
    accident. Requested and observed model/effort evidence are recorded as
    separate facts. Claude's JSON result proves the model through
    ``modelUsage``; the real Claude adapter additionally uses a temporary Stop
    hook to observe the active (including provider-downgraded) effort.

    Spends real provider capacity; refuse without explicit ``live=True``.
    """
    if not live:
        raise ValueError("the pong matrix spends provider capacity; pass --live to run it")
    if max_calls < 1:
        raise ValueError("max_calls must be at least 1")
    if not matrix:
        raise ValueError("no provider calls are selected; pass one or more explicit pong cases")
    exclusions = {str(value).strip() for value in (excluded_models or set()) if str(value).strip()}
    selected: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_provider, raw_model, raw_effort in matrix:
        case = (str(raw_provider).strip(), str(raw_model).strip(), str(raw_effort).strip())
        provider, model, effort = case
        if not provider or not model or not effort:
            raise ValueError("each pong case requires provider, model, and effort")
        if provider not in adapters:
            raise ValueError(f"pong provider is not configured: {provider}")
        if model in exclusions or case in seen:
            continue
        seen.add(case)
        selected.append(case)
    if not selected:
        raise ValueError("all explicit pong cases were excluded")

    potential_resume_calls = 0
    if include_resume_probe:
        if any(provider == "claude" and model == "claude-sonnet-5" for provider, model, _ in selected) and "claude-opus-4-8" not in exclusions:
            potential_resume_calls += 1
        if any(provider == "codex" and model == "gpt-5.6-sol" for provider, model, _ in selected) and "gpt-5.4-mini" not in exclusions:
            potential_resume_calls += 1
    if len(selected) + potential_resume_calls > max_calls:
        raise ValueError(
            f"explicit pong matrix could make {len(selected) + potential_resume_calls} calls, exceeding max_calls={max_calls}"
        )

    workspace = runtime_dir / "pong-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    prompt = b"Reply with exactly: pong"
    started = _now()
    pings: list[dict[str, Any]] = []

    def ping(provider: str, model: str, effort: str, *, session_action: str = "new", session_id: str | None = None, label: str | None = None) -> dict[str, Any]:
        adapter = adapters[provider]
        effort_is_explicit = effort not in {DEFAULT_EFFORT_TOKEN, "provider-default"}
        effort_observer = getattr(adapter, "invoke_configured_with_effort_observation", None)
        effort_observation_expected = bool(provider == "claude" and effort_is_explicit and callable(effort_observer))
        record: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "effort": effort,
            "requested_model": model,
            "requested_effort": effort,
            "session_action": session_action,
            "kind": label or "pong",
            "at": _now(),
            "effort_observation_expected": effort_observation_expected,
        }
        try:
            invoke = effort_observer if effort_observation_expected else adapter.invoke_configured
            result = invoke(
                "pong", prompt, workspace,
                model=model,
                reasoning=effort,
                permission="read-only",
                session_action=session_action,
                session_id=session_id,
                timeout=300,
            )
        except (KeyError, OSError, ValueError) as error:
            record.update({
                "ok": False,
                "call_succeeded": False,
                "model_verified": False,
                "effort_verified": False,
                "effort_cli_accepted": False,
                "error": f"{type(error).__name__}: {error}",
            })
            pings.append(record)
            return record
        text = (result.response_text or "").strip()
        observed = result.observed_model
        # Claude Code bills helper models (a Haiku sidecar) alongside the
        # requested one, and on a one-word ping the helper can out-token the
        # main model — so neither single-entry nor dominance is the right
        # test. Verified means: the requested model appears in modelUsage
        # with nonzero output.
        model_usage = dict(getattr(result, "model_usage", {}) or {})
        requested_usage = model_usage.get(model) or {}
        usage_observed_model = model if requested_usage.get("outputTokens", 0) > 0 else None
        verified_model = observed if observed == model else usage_observed_model
        call_succeeded = result.exit_code == 0 and not result.error and bool(text)
        model_verified = call_succeeded and verified_model == model
        observed_effort = result.observed_reasoning
        effort_verified = bool(effort_is_explicit and observed_effort == effort)
        effort_cli_accepted = bool(call_succeeded and model_verified and effort_is_explicit)
        effort_proof_required = bool(effort_is_explicit and (provider == "codex" or effort_observation_expected))
        # A real adapter that advertises an observation channel must prove the
        # requested effort. Fixture/custom adapters remain compatible, but are
        # labeled as CLI-accepted rather than observed.
        ok = bool(call_succeeded and model_verified and (not effort_proof_required or effort_verified))
        reasoning_source = getattr(result, "reasoning_observation_source", None)
        reasoning_error = getattr(result, "reasoning_observation_error", None)
        record.update({
            "ok": ok,
            "call_succeeded": call_succeeded,
            "model_verified": model_verified,
            "effort_verified": effort_verified,
            "effort_cli_accepted": effort_cli_accepted,
            "exit_code": result.exit_code,
            "provider_error": result.error,
            "configured_model": result.configured_model,
            "observed_model": observed,
            "observed_reasoning": observed_effort,
            "observed_effort": observed_effort,
            "verified_model": verified_model,
            "model_evidence": result.observation_source if model_verified else None,
            "effort_evidence": reasoning_source if effort_verified else ("cli-accepted-unobserved" if effort_cli_accepted else None),
            "effort_observation_error": reasoning_error,
            "billed_models": sorted(model_usage),
            "session_id": result.session_id,
            "elapsed_ms": result.elapsed_ms,
            "cost_usd": (result.usage or {}).get("total_cost_usd") if isinstance(result.usage, dict) else None,
            "text_head": text[:80],
        })
        pings.append(record)
        return record

    for provider, model, effort in selected:
        ping(provider, model, effort)
    resume_probes: list[dict[str, Any]] = []
    if include_resume_probe:
        # Current Claude documentation says an explicit --model wins over the
        # model restored from a transcript. Keep this opt-in proof bounded by
        # the same max_calls budget as the named cases above.
        base = next((p for p in pings if p["provider"] == "claude" and p["model"] == "claude-sonnet-5" and p.get("ok") and p.get("session_id")), None)
        if base and "claude-opus-4-8" not in exclusions:
            probe = ping("claude", "claude-opus-4-8", "low", session_action="continue", session_id=str(base["session_id"]), label="resume-model-switch")
            resume_probes.append({
                "provider": "claude",
                "resumed_session_model": "claude-sonnet-5",
                "requested_model": "claude-opus-4-8",
                "observed_model": probe.get("verified_model") or probe.get("observed_model"),
                "billed_models": probe.get("billed_models"),
                "switched": probe.get("verified_model") == "claude-opus-4-8",
            })
        codex_base = next((p for p in pings if p["provider"] == "codex" and p["model"] == "gpt-5.6-sol" and p.get("ok") and p.get("session_id")), None)
        if codex_base and "gpt-5.4-mini" not in exclusions:
            probe = ping("codex", "gpt-5.4-mini", "low", session_action="continue", session_id=str(codex_base["session_id"]), label="resume-model-switch")
            resume_probes.append({
                "provider": "codex",
                "resumed_session_model": "gpt-5.6-sol",
                "requested_model": "gpt-5.4-mini",
                "observed_model": probe.get("verified_model") or probe.get("observed_model"),
                "switched": probe.get("verified_model") == "gpt-5.4-mini",
            })
    finished = _now()
    previous = load_pong_report(runtime_dir) or {}
    previous_pings = [item for item in previous.get("pings", []) if isinstance(item, dict)]
    previous_probes = [item for item in previous.get("resume_probes", []) if isinstance(item, dict)]
    combined_pings = previous_pings + pings
    run_totals = {
        "attempted": len(pings),
        "ok": sum(1 for item in pings if item.get("ok")),
        "cost_usd": round(sum(float(item.get("cost_usd") or 0) for item in pings), 6),
    }
    runs = [item for item in previous.get("runs", []) if isinstance(item, dict)]
    if not runs and previous_pings:
        runs.append({
            "started_at": previous.get("started_at"),
            "finished_at": previous.get("finished_at"),
            "totals": previous.get("totals") or {
                "attempted": len(previous_pings),
                "ok": sum(1 for item in previous_pings if item.get("ok")),
            },
            "legacy": True,
        })
    runs.append({"started_at": started, "finished_at": finished, "totals": run_totals})
    report = {
        "schema_version": "toledo_orchestrator.pong.v1",
        "started_at": previous.get("started_at") or started,
        "finished_at": finished,
        "prompt": prompt.decode("utf-8"),
        "pings": combined_pings,
        "resume_probes": previous_probes + resume_probes,
        "runs": runs,
        "last_run_totals": run_totals,
        "totals": {
            "attempted": len(combined_pings),
            "ok": sum(1 for item in combined_pings if item.get("ok")),
            "cost_usd": round(sum(float(item.get("cost_usd") or 0) for item in combined_pings), 6),
        },
    }
    atomic_write(runtime_dir / PONG_RELATIVE_PATH, (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return report


def load_catalog(runtime_dir: Path, *, refresh: bool = True) -> dict[str, Any]:
    path = catalog_path(runtime_dir)
    if refresh and not path.exists():
        return refresh_catalog(runtime_dir)
    try:
        value = read_json(path)
    except (OSError, json.JSONDecodeError):
        return {"schema_version": CATALOG_SCHEMA, "models": [], "observed_models": [], "sources": {}, "stale": True}
    if value.get("schema_version") != CATALOG_SCHEMA:
        return {"schema_version": CATALOG_SCHEMA, "models": [], "observed_models": [], "sources": {}, "stale": True}
    if refresh and _catalog_cli_versions_changed(value):
        return refresh_catalog(runtime_dir)
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(value["verified_at"]))).total_seconds()
    except (KeyError, TypeError, ValueError):
        age = STALE_AFTER_SECONDS + 1
    value["stale"] = age > STALE_AFTER_SECONDS
    return value


def validate_selection(catalog: dict[str, Any], *, provider: str, model: str, effort: str, custom: bool = False) -> None:
    if provider not in {"codex", "claude"}:
        raise ValueError(f"unsupported provider: {provider}")
    if not model.strip() or not effort.strip():
        raise ValueError("model and reasoning effort are required")
    if custom:
        return
    matches = [item for item in catalog.get("models", []) if item.get("provider") == provider and item.get("selection_token") == model]
    if not matches:
        raise ValueError("model is not available for this provider; use Custom... to record an unverified selection")
    supported = matches[0].get("supported_efforts", [])
    # Models without a documented effort ladder (e.g. Haiku 4.5) accept only
    # the provider default; the adapters then omit the effort flag entirely.
    if not supported:
        if effort != DEFAULT_EFFORT_TOKEN:
            raise ValueError("this model has no documented reasoning-effort control; use the provider default")
        return
    if effort not in supported:
        raise ValueError("reasoning effort is not supported by the selected model")


def load_pong_report(runtime_dir: Path) -> dict[str, Any] | None:
    try:
        value = read_json(runtime_dir / PONG_RELATIVE_PATH)
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _merge_pong_evidence(runtime_dir: Path, models: list[dict[str, Any]]) -> None:
    """Stamp models with cumulative, explicitly typed live evidence."""
    report = load_pong_report(runtime_dir)
    if not report:
        return
    pings = [item for item in report.get("pings", []) if isinstance(item, dict)]
    # Older reports did not stamp each ping. Their run ledger still preserves
    # ordered attempt counts and finish times, so attribute those pings to the
    # run that actually made them instead of falsely using the newest report
    # timestamp for every historical success.
    run_times: list[str | None] = []
    for run in report.get("runs", []):
        if not isinstance(run, dict):
            continue
        count = int((run.get("totals") or {}).get("attempted") or 0)
        run_times.extend([run.get("finished_at")] * max(0, count))
    latest_count = int((report.get("last_run_totals") or {}).get("attempted") or 0)
    latest_start = max(0, len(pings) - latest_count) if latest_count else len(pings)
    latest_attempts: dict[tuple[str, str], dict[str, Any]] = {}
    for index, ping in enumerate(pings):
        if index < latest_start:
            continue
        requested_model = str(ping.get("requested_model") or ping.get("model") or "")
        if not requested_model:
            continue
        latest_attempts[(str(ping.get("provider")), requested_model)] = {
            "at": ping.get("at") or (run_times[index] if index < len(run_times) else report.get("finished_at")),
            "ok": bool(ping.get("ok")),
            "requested_effort": ping.get("requested_effort") or ping.get("effort"),
            "model_observed": bool(ping.get("model_verified")),
            "effort_observed": bool(ping.get("effort_verified")),
            "error": ping.get("provider_error") or ping.get("error"),
            "text_head": ping.get("text_head") if not ping.get("ok") else None,
        }
    verified: dict[tuple[str, str], dict[str, Any]] = {}
    for index, ping in enumerate(pings):
        requested_model = str(ping.get("requested_model") or ping.get("model") or "")
        observed_model = str(ping.get("verified_model") or ping.get("observed_model") or "")
        model_verified = bool(ping.get("model_verified")) if "model_verified" in ping else bool(ping.get("ok") and observed_model == requested_model)
        if not model_verified or not requested_model:
            continue
        key = (str(ping.get("provider")), requested_model)
        evidence_at = ping.get("at") or (run_times[index] if index < len(run_times) else report.get("finished_at"))
        entry = verified.setdefault(key, {
            "model_observed": True,
            "observed_efforts": [],
            "accepted_efforts": [],
            # Backward-compatible UI field. It contains only efforts proven
            # from provider evidence, never merely accepted CLI arguments.
            "efforts": [],
            "at": evidence_at,
            "latest_run_observed": False,
        })
        if index >= latest_start:
            entry["latest_run_observed"] = True
        if ping.get("kind") == "resume-model-switch" and ping.get("session_action") == "continue":
            entry["resume_switch_observed"] = True
            entry["resume_switch_observed_at"] = evidence_at
        effort = str(ping.get("requested_effort") or ping.get("effort") or DEFAULT_EFFORT_TOKEN)
        observed_effort = str(ping.get("observed_effort") or ping.get("observed_reasoning") or "")
        effort_verified = bool(ping.get("effort_verified")) if "effort_verified" in ping else bool(observed_effort and observed_effort == effort)
        effort_accepted = bool(ping.get("effort_cli_accepted")) if "effort_cli_accepted" in ping else bool(ping.get("ok"))
        if effort_verified and effort not in entry["observed_efforts"]:
            entry["observed_efforts"].append(effort)
            entry["efforts"].append(effort)
        if effort_accepted and effort not in entry["accepted_efforts"]:
            entry["accepted_efforts"].append(effort)
        entry["at"] = evidence_at or entry["at"]
    for model in models:
        key = (str(model.get("provider")), str(model.get("selection_token")))
        entry = verified.get(key)
        if entry:
            model["live_verified"] = entry
        if key in latest_attempts:
            model["last_live_attempt"] = latest_attempts[key]
