"""Thin lifecycle supervisor for the direct Strategy Hermes worker.

The supervisor owns no research decisions. It materializes durable intake,
starts one direct Hermes process per active lab, and mechanically validates the
artifacts Hermes leaves behind. The actual researcher is Hermes.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from artifact_validator import sync_agent_artifacts
from autonomous_research_ingress import ResearchIntake
from hermes_agent import StrategyHermesAgent
from lab import ResearchLab, ResearchLabError


DEFAULT_LAB_ROOT = Path(os.getenv("AUTONOMOUS_RESEARCH_LAB_ROOT", "/var/lib/autonomous-research"))
DEFAULT_REPO_ROOT = Path(os.getenv("AUTONOMOUS_RESEARCH_REPO_ROOT", str(HERE.parents[3])))


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    intake = ResearchIntake(args.lab_root)
    for path in (intake.root, intake.intake_dir, intake.labs_dir, intake.errors_dir):
        path.mkdir(parents=True, exist_ok=True)

    reports: list[dict[str, Any]] = []
    for request_id in intake.pending_ids():
        try:
            intake.materialize(request_id, repo_root=args.repo_root)
        except (ResearchLabError, ValueError, OSError, json.JSONDecodeError) as exc:
            intake.record_error(request_id, phase="MATERIALIZE", error=f"{type(exc).__name__}: {exc}")
            reports.append({"request_id": request_id, "status": "BLOCKED", "error": str(exc)})

    for lab_path in sorted(path for path in intake.labs_dir.iterdir() if path.is_dir()):
        try:
            reports.append(_run_lab(args, lab_path))
            intake.clear_error(lab_path.name)
        except (ResearchLabError, ValueError, OSError, json.JSONDecodeError) as exc:
            intake.record_error(lab_path.name, phase="HERMES_OR_VERIFY", error=f"{type(exc).__name__}: {exc}")
            reports.append({"lab_id": lab_path.name, "status": "BLOCKED", "error": f"{type(exc).__name__}: {exc}"})
    return {"status": "STRATEGY_HERMES_CYCLE_COMPLETED", "labs": reports}


def _run_lab(args: argparse.Namespace, lab_path: Path) -> dict[str, Any]:
    lab = ResearchLab(lab_path)
    state = lab.state()
    if (lab_path / "candidate.json").exists():
        return {"lab_id": lab_path.name, "status": "CANDIDATE", "cycle": state.get("cycle", 0)}

    cycle = int(state.get("cycle", 0) or 0) + 1
    lab.update_state(cycle=cycle, last_action="HERMES_RUNNING")
    agent = StrategyHermesAgent(repo_root=args.repo_root, lab_root=lab_path, timeout_seconds=args.timeout_seconds)
    run = agent.run()
    lab.record_agent_run({
        "run_id": run.run_id,
        "plan_id": run.plan_id,
        "status": run.status,
        "returncode": run.returncode,
        "output_path": run.output_path,
        "usage_path": run.usage_path,
        "error": run.error,
        "duration_seconds": run.duration_seconds,
    })
    decisions = sync_agent_artifacts(lab)
    lab.update_state(cycle=cycle, last_action="HERMES_COMPLETED" if run.status == "COMPLETED" else "HERMES_FAILED")
    if run.status != "COMPLETED":
        raise ResearchLabError(run.error or "Strategy Hermes did not complete")
    return {
        "lab_id": lab_path.name,
        "status": "CYCLE_COMPLETED",
        "cycle": cycle,
        "agent": run.status,
        "decisions": decisions,
        "result_available": bool(decisions),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Direct Strategy Hermes lifecycle supervisor")
    parser.add_argument("--lab-root", type=Path, default=DEFAULT_LAB_ROOT)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--interval-min", type=float, default=float(os.getenv("AUTONOMOUS_RESEARCH_INTERVAL_MIN", "30")))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.getenv("AUTONOMOUS_RESEARCH_TIMEOUT_SECONDS", "1800")))
    parser.add_argument("--loop", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    while True:
        try:
            print(json.dumps(run_once(args), ensure_ascii=False), flush=True)
        except (ResearchLabError, ValueError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"status": "SUPERVISOR_BLOCKED", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), flush=True)
            if not args.loop:
                return 2
        if not args.loop:
            return 0
        time.sleep(max(30.0, args.interval_min * 60.0))


if __name__ == "__main__":
    raise SystemExit(main())
