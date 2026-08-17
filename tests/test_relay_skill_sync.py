from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re

from toledo_orchestrator.cli import build_parser
from toledo_orchestrator.workflow import load_workflows


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "relay-use-skill"
REFERENCES = SKILL / "references"


def _combined_skill_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in [SKILL / "SKILL.md", *sorted(REFERENCES.glob("*.md"))]
    )


def _load_manage_skill():
    path = SKILL / "scripts" / "manage_skill.py"
    spec = importlib.util.spec_from_file_location("relay_manage_skill", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_is_self_contained_and_uses_valid_identity() -> None:
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    metadata = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")
    assert "name: relay-use-skill" in skill
    assert 'display_name: "relay_use_skill"' in metadata
    assert "$relay-use-skill" in metadata
    assert "https://github.com/SamioVirus/Toledo_relay" in skill

    for target in re.findall(r"\]\(([^)]+\.md)\)", skill):
        assert (SKILL / target).is_file(), target
    for required in (
        "relay-concepts.md",
        "installation.md",
        "projects-and-configuration.md",
        "workflows.md",
        "models.md",
        "prompts.md",
        "operations.md",
        "maintenance-and-publishing.md",
    ):
        assert (REFERENCES / required).is_file()


def test_skill_names_every_packaged_workflow_prompt_and_operator_command() -> None:
    combined = _combined_skill_text()
    workflow_reference = (REFERENCES / "workflows.md").read_text(encoding="utf-8")
    prompt_reference = (REFERENCES / "prompts.md").read_text(encoding="utf-8")

    for workflow_id in load_workflows():
        assert f"`{workflow_id}`" in workflow_reference
    for prompt in (ROOT / "src" / "toledo_orchestrator" / "prompts").glob("*.md"):
        assert f"`{prompt.name}`" in prompt_reference

    parser = build_parser()
    command_action = next(action for action in parser._actions if action.dest == "command")
    commands = set(command_action.choices)
    operator_commands = {
        "check",
        "run",
        "status",
        "agent-brief",
        "show",
        "decide",
        "advance",
        "override",
        "recover",
        "validate",
        "runs",
        "workflows",
        "profiles",
        "profile-set",
        "workflow-save-as",
        "projects",
        "catalog-research",
        "pong",
        "project-add",
        "artifact",
        "export",
        "cleanup",
        "ui",
    }
    assert operator_commands <= commands
    for command in operator_commands:
        assert f"toledo_orchestrator {command}" in combined


def test_skill_uses_live_models_and_enforces_github_publication() -> None:
    combined = _combined_skill_text()
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Never choose model IDs or effort values from this file or memory" in combined
    assert "gpt-" not in combined.lower()
    assert "claude-opus-" not in combined.lower()
    assert "tests/test_relay_skill_sync.py" in agents
    assert "github.com/SamioVirus/Toledo_relay" in agents
    assert "branch is pushed" in agents


def test_manage_script_installs_same_skill_for_codex_and_claude(tmp_path: Path) -> None:
    manage = _load_manage_skill()
    source = SKILL.resolve()
    codex_root = tmp_path / "codex" / "skills"
    claude_root = tmp_path / "claude" / "skills"
    agents_root = tmp_path / "agents" / "skills"

    codex = manage.install_target(source, codex_root, remove_legacy=False)
    claude = manage.install_target(source, claude_root, remove_legacy=False)
    agents = manage.install_target(source, agents_root, remove_legacy=False)

    assert Path(codex["target"]).resolve() == source
    assert Path(claude["target"]).resolve() == source
    assert Path(agents["target"]).resolve() == source
    assert codex["action"] == "installed"
    assert claude["action"] == "installed"
    assert agents["action"] == "installed"
    assert manage.CANONICAL_URL == "https://github.com/SamioVirus/Toledo_relay.git"

    # Remove only the disposable links created inside tmp_path.
    for target in (Path(codex["target"]), Path(claude["target"]), Path(agents["target"])):
        if target.is_symlink():
            target.unlink()
        else:
            os.rmdir(target)
