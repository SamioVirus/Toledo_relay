from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .core import Orchestrator


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator")
    parser.add_argument("--runtime-dir", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    run = commands.add_parser("run"); run.add_argument("--project", required=True); run.add_argument("--workflow", required=True); run.add_argument("--request-file", type=Path, required=True)
    status = commands.add_parser("status"); status.add_argument("run_id")
    show = commands.add_parser("show"); show.add_argument("run_id"); show.add_argument("--turn", type=int, required=True)
    resume = commands.add_parser("resume"); resume.add_argument("run_id"); group = resume.add_mutually_exclusive_group(required=True); group.add_argument("--decision"); group.add_argument("--decision-file", type=Path)
    validate = commands.add_parser("validate"); validate.add_argument("run_id"); validate.add_argument("--receipt-file", type=Path, required=True)
    args = parser.parse_args(argv)
    orchestrator = Orchestrator(runtime_dir=args.runtime_dir)
    if args.command == "check":
        _emit({"codex": shutil.which("codex") is not None, "claude": shutil.which("claude") is not None, "runtime_dir": str(orchestrator.runtime_dir)}); return 0
    if args.command == "run":
        run_id = orchestrator.create_run(args.request_file.read_bytes(), args.project, args.workflow); _emit(orchestrator.run_to_stop(run_id)); return 0
    if args.command == "status": _emit(orchestrator.state(args.run_id)); return 0
    if args.command == "show":
        path = orchestrator.runs_dir / args.run_id / "turns" / f"turn.{args.turn:04d}.output.md"; sys.stdout.write(path.read_text(encoding="utf-8")); return 0
    if args.command == "resume":
        payload = args.decision_file.read_bytes() if args.decision_file else args.decision.encode("utf-8"); _emit(orchestrator.resume(args.run_id, payload)); return 0
    if args.command == "validate": _emit(orchestrator.attach_receipt(args.run_id, args.receipt_file)); return 0
    return 2
