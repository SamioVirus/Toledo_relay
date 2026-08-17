from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time


def brief(run_id: str, runtime_dir: str | None) -> dict[str, object]:
    command = [sys.executable, "-m", "toledo_orchestrator"]
    if runtime_dir:
        command.extend(["--runtime-dir", runtime_dir])
    command.extend(["agent-brief", run_id])
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description="Wait briefly for a Relay run to change.")
    parser.add_argument("run_id")
    parser.add_argument("--runtime-dir")
    parser.add_argument("--after-turn", type=int, default=-1)
    parser.add_argument("--after-status", default="")
    parser.add_argument("--max-wait", type=int, default=30, choices=range(1, 46), metavar="SECONDS")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    deadline = time.monotonic() + args.max_wait
    value: dict[str, object] = {}
    while True:
        value = brief(args.run_id, args.runtime_dir)
        run = value.get("run") or {}
        gate = value.get("gate") or {}
        if (
            int(run.get("current_turn") or 0) > args.after_turn
            or str(run.get("status") or "") != args.after_status
            or bool(gate.get("requires_human"))
            or bool(run.get("terminal"))
        ):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.25, args.interval))
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
