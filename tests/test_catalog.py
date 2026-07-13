from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from toledo_orchestrator.catalog import CATALOG_SCHEMA, load_catalog, validate_selection
from toledo_orchestrator.core import atomic_write


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
