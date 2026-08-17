from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_packaged_project_is_a_safe_unconfigured_placeholder() -> None:
    path = ROOT / "src" / "toledo_orchestrator" / "projects" / "toledo.json"
    value = json.loads(path.read_text(encoding="utf-8"))

    assert value["root"] == r"C:\path\to\your\repo"
    assert value["read_only"] is True
    assert value["implementation"]["enabled"] is False
    assert value["instruction_files"] == []
    assert value["validations"] == []
    assert "C:\\Users" not in path.read_text(encoding="utf-8")


def test_public_release_has_license_and_no_personal_checkout_paths() -> None:
    assert (ROOT / "LICENSE").is_file()
    for relative in ("README.md", "docs/operator-control-ux-plan.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "C:\\Users" not in text
        assert "OneDrive\\Documents" not in text
