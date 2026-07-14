"""Deterministic, local provider capability catalog.

The catalog deliberately records what this machine can select rather than what
provider documentation happens to mention.  It is cacheable evidence, not a
network dependency or a run precondition.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import atomic_write, read_json


CATALOG_SCHEMA = "toledo_orchestrator.capability_catalog.v1"
CATALOG_RELATIVE_PATH = Path("catalog") / "capabilities.v1.json"
STALE_AFTER_SECONDS = 7 * 24 * 60 * 60

# These are official model identifiers, intentionally marked curated rather
# than account-entitled.  Claude Code's installed binary has no account-aware
# listing command; successful Toledo observations add account evidence later.
CURATED_CLAUDE_MODELS = (
    ("claude-fable-5", "Claude Fable 5"),
    ("claude-opus-4-8", "Claude Opus 4.8"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_text(command: list[str]) -> tuple[str, str | None]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return "", f"{type(error).__name__}: {error}"
    if completed.returncode:
        return "", (completed.stderr.strip() or f"exit {completed.returncode}")
    return completed.stdout, None


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


def _codex_models() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw, error = _run_text(["codex", "debug", "models"])
    version, _ = _run_text(["codex", "--version"])
    metadata: dict[str, Any] = {"cli": "codex", "cli_version": version.strip(), "error": error}
    if error:
        return [], metadata
    try:
        values = json.loads(raw).get("models", [])
    except (json.JSONDecodeError, AttributeError) as parse_error:
        metadata["error"] = f"invalid codex debug models JSON: {parse_error}"
        return [], metadata
    models = []
    for item in values:
        if not isinstance(item, dict) or not item.get("slug"):
            continue
        efforts = [str(level.get("effort")) for level in item.get("supported_reasoning_levels", []) if isinstance(level, dict) and level.get("effort")]
        models.append({
            "provider": "codex", "selection_token": str(item["slug"]),
            "display_name": str(item.get("display_name") or item["slug"]),
            "aliases": [], "supported_efforts": efforts,
            "special_modes": [str(value) for value in item.get("additional_speed_tiers", [])],
            "availability": "installed-account", "source": "codex debug models",
            "supported_in_api": bool(item.get("supported_in_api")), "visibility": item.get("visibility"),
        })
    return models, metadata


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
    efforts = ["low", "medium", "high", "xhigh", "max"] if supports_effort else []
    return [{
        "provider": "claude", "selection_token": token, "display_name": name,
        "aliases": [], "supported_efforts": efforts,
        "special_modes": ["ultracode"] if supports_ultracode else [],
        "availability": "curated-cli-compatible", "source": "curated official manifest + claude --help",
        "subscription_entitlement": "unknown",
    } for token, name in CURATED_CLAUDE_MODELS], metadata


def catalog_path(runtime_dir: Path) -> Path:
    return runtime_dir / CATALOG_RELATIVE_PATH


def refresh_catalog(runtime_dir: Path) -> dict[str, Any]:
    """Query local CLIs only, atomically retaining the prior good catalog."""
    codex, codex_source = _codex_models()
    claude, claude_source = _claude_models()
    if not codex and not claude:
        return load_catalog(runtime_dir, refresh=False)
    discovered_at = _now()
    observed = _observed_models(runtime_dir)
    value = {
        "schema_version": CATALOG_SCHEMA, "discovered_at": discovered_at,
        "verified_at": discovered_at, "models": codex + claude,
        "observed_models": observed, "sources": {"codex": codex_source, "claude": claude_source},
    }
    atomic_write(catalog_path(runtime_dir), (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return value


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
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(value["verified_at"]))).total_seconds()
    except (KeyError, ValueError):
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
    if effort not in matches[0].get("supported_efforts", []):
        raise ValueError("reasoning effort is not supported by the selected model")
