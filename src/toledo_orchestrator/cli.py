from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .configuration import (
    load_configured_projects,
    load_configured_workflows,
    save_project_value,
    update_profile,
)
from .core import Orchestrator
from .cycle import CycleOrchestrator


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _base(runtime_dir: Path | None) -> Orchestrator:
    return Orchestrator(runtime_dir=runtime_dir)


def _cycle(runtime_dir: Path | None) -> CycleOrchestrator:
    return CycleOrchestrator(runtime_dir=runtime_dir)


def _state_engine(runtime_dir: Path | None, run_id: str) -> tuple[object, dict[str, object]]:
    base = _base(runtime_dir)
    state = base.state(run_id)
    if state.get("schema_version") == "toledo_orchestrator.run.v2":
        engine = _cycle(runtime_dir)
        return engine, engine.state(run_id)
    return base, state


def _validation_value(raw: str) -> dict[str, str]:
    parts = raw.split("|", 2)
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("validation must be ID|ENVIRONMENT|COMMAND")
    return {"id": parts[0], "environment": parts[1], "command": parts[2]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orchestrator")
    parser.add_argument("--runtime-dir", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    run = commands.add_parser("run")
    run.add_argument("--project", required=True)
    run.add_argument("--workflow", required=True)
    run.add_argument("--request-file", type=Path, required=True)
    run.add_argument("--step", action="store_true", help="pause after each provider turn")
    status = commands.add_parser("status")
    status.add_argument("run_id")
    show = commands.add_parser("show")
    show.add_argument("run_id")
    show.add_argument("--turn", type=int, required=True)
    resume = commands.add_parser("resume")
    resume.add_argument("run_id")
    group = resume.add_mutually_exclusive_group(required=True)
    group.add_argument("--decision")
    group.add_argument("--decision-file", type=Path)
    decide = commands.add_parser("decide")
    decide.add_argument("run_id")
    decide.add_argument("--choice", required=True, choices=("yes", "no", "other"))
    decide.add_argument("--text")
    decide.add_argument("--text-file", type=Path)
    advance = commands.add_parser("advance")
    advance.add_argument("run_id")
    advance_group = advance.add_mutually_exclusive_group()
    advance_group.add_argument("--text")
    advance_group.add_argument("--text-file", type=Path)
    override = commands.add_parser("override")
    override.add_argument("run_id")
    override.add_argument("--profile")
    override.add_argument("--model")
    override.add_argument("--effort")
    override.add_argument("--session-action", choices=("new", "continue"))
    override.add_argument("--stance", choices=("ideas", "skeptic", "judge", "audit"))
    recover = commands.add_parser("recover")
    recover.add_argument("run_id")
    validate = commands.add_parser("validate")
    validate.add_argument("run_id")
    validate.add_argument("--receipt-file", type=Path, required=True)
    commands.add_parser("runs")
    profiles = commands.add_parser("profiles")
    profiles.add_argument("--workflow", default="continuous-development")
    profile_set = commands.add_parser("profile-set")
    profile_set.add_argument("--workflow", default="continuous-development")
    profile_set.add_argument("--profile", required=True)
    profile_set.add_argument("--model")
    profile_set.add_argument("--effort")
    profile_set.add_argument("--permission", choices=("read-only", "workspace-write"))
    profile_set.add_argument("--label")
    commands.add_parser("projects")
    commands.add_parser("catalog-research")
    backfill = commands.add_parser("backfill-captions")
    backfill.add_argument("run_id")
    backfill.add_argument("--opt-in", action="store_true")
    backfill.add_argument("--limit", type=int, default=1)
    fork = commands.add_parser("fork-rewind")
    fork.add_argument("run_id")
    fork.add_argument("--rewind-to-turn", type=int, required=True)
    fork.add_argument("--opt-in", action="store_true")
    project_add = commands.add_parser("project-add")
    project_add.add_argument("--id", required=True)
    project_add.add_argument("--root", type=Path, required=True)
    project_add.add_argument("--instruction-file", action="append", default=[])
    project_add.add_argument("--write-path", action="append", default=[])
    project_add.add_argument("--validation", action="append", type=_validation_value, default=[])
    project_add.add_argument("--no-implementation", action="store_true")
    project_add.add_argument("--allow-no-validations", action="store_true")
    artifact = commands.add_parser("artifact")
    artifact.add_argument("run_id")
    artifact.add_argument("path")
    export = commands.add_parser("export")
    export.add_argument("run_id")
    export.add_argument("--format", choices=("markdown", "text"), default="markdown")
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("run_id")
    cleanup.add_argument("--force", action="store_true")
    ui = commands.add_parser("ui")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-open", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        legacy = _base(args.runtime_dir).check()
        cycle = _cycle(args.runtime_dir).check()
        result = {"ready": legacy["ready"] and cycle["ready"], "legacy": legacy, "continuous": cycle}
        _emit(result)
        return 0 if result["ready"] else 1
    if args.command == "run":
        request = args.request_file.read_bytes()
        if args.workflow == "dev-review":
            if args.step:
                raise ValueError("--step is available only for continuous v2 workflows")
            engine = _base(args.runtime_dir)
            run_id = engine.create_run(request, args.project, args.workflow)
        else:
            engine = _cycle(args.runtime_dir)
            run_id = engine.create_run(
                request,
                args.project,
                args.workflow,
                run_mode="step" if args.step else "auto",
            )
        _emit(engine.run_to_stop(run_id))
        return 0
    if args.command in {"status", "show", "resume", "decide", "advance", "override", "recover", "validate", "artifact", "export", "cleanup"}:
        engine, state = _state_engine(args.runtime_dir, args.run_id)
        if args.command == "status":
            _emit(state)
            return 0
        if args.command == "show":
            sys.stdout.write(engine.show_turn(args.run_id, args.turn))
            return 0
        if args.command == "artifact":
            if not isinstance(engine, CycleOrchestrator):
                raise ValueError("artifact command currently requires a v2 run")
            sys.stdout.buffer.write(engine.artifact(args.run_id, args.path))
            return 0
        if args.command == "export":
            if not isinstance(engine, CycleOrchestrator):
                raise ValueError("export currently requires a v2 run")
            sys.stdout.buffer.write(engine.export_run(args.run_id, plain_text=args.format == "text"))
            return 0
        if args.command == "cleanup":
            if not isinstance(engine, CycleOrchestrator):
                raise ValueError("cleanup currently requires a v2 run")
            _emit(engine.cleanup_worktree(args.run_id, force=args.force))
            return 0
        if args.command == "resume":
            payload = args.decision_file.read_bytes() if args.decision_file else args.decision.encode("utf-8")
            if isinstance(engine, CycleOrchestrator):
                raise ValueError("v2 runs use decide --choice yes|no|other, or advance at an operator step")
            _emit(engine.resume(args.run_id, payload))
            return 0
        if args.command == "decide":
            if not isinstance(engine, CycleOrchestrator):
                raise ValueError("yes/no/other decisions require a v2 run")
            if args.text and args.text_file:
                raise ValueError("use either --text or --text-file")
            payload = args.text_file.read_bytes() if args.text_file else (args.text or "").encode("utf-8")
            _emit(engine.decide(args.run_id, args.choice, payload))
            return 0
        if args.command == "advance":
            if not isinstance(engine, CycleOrchestrator):
                raise ValueError("step-by-step advance requires a v2 run")
            direction = args.text_file.read_bytes() if args.text_file else (args.text or "").encode("utf-8")
            _emit(engine.continue_step(args.run_id, direction))
            return 0
        if args.command == "override":
            if not isinstance(engine, CycleOrchestrator):
                raise ValueError("next-turn overrides require a v2 run")
            _emit(engine.set_next_turn_override(
                args.run_id,
                profile=args.profile,
                model=args.model,
                effort=args.effort,
                session_action=args.session_action,
                stance=args.stance,
            ))
            return 0
        if args.command == "recover":
            if not isinstance(engine, CycleOrchestrator):
                raise ValueError("run recovery requires a v2 run")
            _emit(engine.recover_run(args.run_id))
            return 0
        if args.command == "validate":
            if isinstance(engine, CycleOrchestrator):
                _emit(engine.attach_receipt(args.run_id, args.receipt_file))
            else:
                _emit(engine.attach_receipt(args.run_id, args.receipt_file))
            return 0
    cycle = _cycle(args.runtime_dir)
    if args.command == "catalog-research":
        from .catalog import research_catalog
        _emit(research_catalog(cycle.runtime_dir))
        return 0
    if args.command == "backfill-captions":
        _emit(cycle.backfill_semantic_captions(args.run_id, opt_in=args.opt_in, limit=args.limit))
        return 0
    if args.command == "fork-rewind":
        _emit(cycle.fork_rewind(args.run_id, rewind_to_turn=args.rewind_to_turn, opt_in=args.opt_in))
        return 0
    if args.command == "runs":
        values = []
        for path in sorted(cycle.runs_dir.glob("run_*/run.json"), reverse=True):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            values.append({
                "run_id": state.get("run_id"),
                "workflow": state.get("workflow"),
                "project": state.get("project"),
                "status": state.get("status"),
                "cycle": state.get("cycle"),
                "current_turn": state.get("current_turn"),
            })
        _emit(values)
        return 0
    if args.command == "profiles":
        workflows = load_configured_workflows(cycle.runtime_dir)
        if args.workflow not in workflows:
            raise ValueError(f"unknown workflow: {args.workflow}")
        _emit(workflows[args.workflow].public_summary()["profiles"])
        return 0
    if args.command == "profile-set":
        value = update_profile(
            cycle.runtime_dir,
            args.workflow,
            args.profile,
            model=args.model,
            effort=args.effort,
            permission=args.permission,
            label=args.label,
        )
        _emit(value.public_summary()["profiles"][args.profile])
        return 0
    if args.command == "projects":
        _emit({key: value.check() for key, value in load_configured_projects(cycle.runtime_dir).items()})
        return 0
    if args.command == "project-add":
        value = {
            "id": args.id,
            "root": str(args.root.resolve()),
            "read_only": True,
            "instruction_files": args.instruction_file,
            "validations": args.validation,
            "implementation": {
                "enabled": not args.no_implementation,
                "write_allowlist": (args.write_path or ["."]) if not args.no_implementation else [],
                "commit_on_accept": True,
                "allow_no_validations": args.allow_no_validations,
                "validation_requires_approval": True,
            },
        }
        project = save_project_value(cycle.runtime_dir, value)
        _emit(project.check())
        return 0
    if args.command == "ui":
        from .web import serve

        serve(runtime_dir=cycle.runtime_dir, host=args.host, port=args.port, open_browser=not args.no_open)
        return 0
    return 2
