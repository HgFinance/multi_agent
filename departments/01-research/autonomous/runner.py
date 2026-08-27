#!/usr/bin/env python3
"""Entry point for Hermes Autonomous Strategy Research.

Examples:
  python runner.py init --goal "Find a robust short-horizon alpha" --universe krx
  python runner.py cycle --lab-root ./strategy_lab
  python runner.py cycle --lab-root ./strategy_lab --agent
  python runner.py cycle --lab-root ./strategy_lab --agent --loop --interval-min 30
  python runner.py ingest --lab-root ./strategy_lab ./result.json
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

from discovery import discover
from director import ResearchDirector
from hermes_agent import HermesResearchAgent
from lab import ResearchLab, ResearchLabError
from models import Objective, to_dict, utc_now
from result import candidate_report, decision_for, parse_result
from autonomous_research_ingress import ResearchIntake


DEFAULT_LAB_ROOT = Path(os.getenv("AUTONOMOUS_RESEARCH_LAB_ROOT", "strategy_lab"))
DEFAULT_REPO_ROOT = Path(os.getenv("AUTONOMOUS_RESEARCH_REPO_ROOT", str(HERE.parents[3])))
DEFAULT_MAX_AGENT_ATTEMPTS = int(os.getenv("AUTONOMOUS_RESEARCH_MAX_AGENT_ATTEMPTS", "3"))


def init_lab(args: argparse.Namespace) -> dict[str, Any]:
    lab = ResearchLab(args.lab_root)
    objective = Objective(
        goal=args.goal,
        universe=args.universe,
        horizon=args.horizon,
        constraints=tuple(args.constraint or ()),
    )
    lab.initialize(objective, replace=args.replace)
    resources = discover(args.repo_root)
    lab.write_resource_map(resources, repo_root=args.repo_root)
    return {"status": "INITIALIZED", "lab_root": str(lab.root), "objective": to_dict(objective), "resources": len(resources)}


def run_cycle(args: argparse.Namespace) -> dict[str, Any]:
    lab = ResearchLab(args.lab_root)
    objective = lab.objective()
    resources = discover(args.repo_root)
    lab.write_resource_map(resources, repo_root=args.repo_root)
    state = lab.state()
    cycle = int(state.get("cycle", 0)) + 1
    events = lab.events()
    plans = lab.plans()
    results = lab.results()
    director = ResearchDirector(objective, events, plans, results)

    active_plan_id = state.get("active_plan_id")
    plan = next((item for item in plans if item.get("plan_id") == active_plan_id), None)
    action = "AWAIT_RESULT" if plan and len(results) < len(plans) else director.next_action()
    if plan and action == "AWAIT_RESULT":
        attempts = sum(
            1 for event in events
            if event.get("event_type") == "AGENT_RUN"
            and (event.get("payload") or {}).get("plan_id") == plan["plan_id"]
        )
        if attempts >= args.max_agent_attempts:
            action = "PAUSE"
    if plan is None and action != "AWAIT_RESULT":
        hypotheses = director.seed_hypotheses(cycle)
        for hypothesis in hypotheses:
            if not (lab.hypotheses_dir / f"{hypothesis.hypothesis_id}.json").exists():
                lab.record_hypothesis(hypothesis)
        selected = director.choose_hypothesis(hypotheses, action)
        plan_obj = director.make_plan(selected, cycle=cycle, action=action)
        lab.record_plan(plan_obj)
        plan = to_dict(plan_obj)
    if plan is None:
        raise ResearchLabError("no active plan could be selected")

    lab.update_state(cycle=cycle, last_action=action, uncertainties=[director.intervention(action)])
    result_path = lab.results_dir / f"{plan['plan_id']}.json"
    agent_payload: dict[str, Any] | None = None
    attempts = sum(
        1 for event in lab.events()
        if event.get("event_type") == "AGENT_RUN"
        and (event.get("payload") or {}).get("plan_id") == plan["plan_id"]
    )
    if args.agent and not result_path.exists() and attempts < args.max_agent_attempts and action != "PAUSE":
        agent = HermesResearchAgent(repo_root=args.repo_root, lab_root=lab.root, timeout_seconds=args.timeout_seconds)
        run = agent.run(plan)
        agent_payload = to_dict(run)
        lab.record_agent_run(agent_payload)
    if result_path.exists():
        ingest_result(lab, result_path, plan)
    payload = {
        "status": "CYCLE_COMPLETED",
        "cycle": cycle,
        "action": action,
        "plan_id": plan["plan_id"],
        "agent": agent_payload,
        "result_available": result_path.exists(),
        "lab_root": str(lab.root),
    }
    return payload


def ingest_result(lab: ResearchLab, path: Path, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("result JSON must be an object")
    if plan is None:
        plan = next((item for item in lab.plans() if item.get("plan_id") == payload.get("plan_id")), None)
        if plan is None:
            raise ValueError("result does not reference a registered experiment plan")
    expected = plan.get("plan_id")
    result = parse_result(
        payload,
        expected_plan_id=expected,
        expected_preregistration_hash=plan.get("preregistration_hash"),
    )
    # Avoid appending duplicate result events when a loop sees the same file.
    if any(event.get("event_type") == "EXPERIMENT_RESULT" and (event.get("payload") or {}).get("plan_id") == result.plan_id for event in lab.events()):
        return {"status": "ALREADY_RECORDED", "plan_id": result.plan_id}
    lab.record_result(result)
    decision, rationale = decision_for(result)
    lab.append_event("DECISION", {"plan_id": result.plan_id, "decision": decision, "rationale": rationale})
    if decision == "CANDIDATE":
        plan_payload = next((item for item in lab.plans() if item.get("plan_id") == result.plan_id), {})
        hypothesis_id = plan_payload.get("hypothesis_id")
        hypothesis = next((item for item in lab._read_objects(lab.hypotheses_dir) if item.get("hypothesis_id") == hypothesis_id), {})
        report = candidate_report(result, hypothesis=hypothesis, plan=plan_payload)
        lab._write_json(lab.root / "candidate.json", report)
        lab.append_event("CANDIDATE_PUBLISHED", report)
    return {"status": "RECORDED", "plan_id": result.plan_id, "decision": decision, "rationale": rationale}


def run_worker(args: argparse.Namespace) -> dict[str, Any]:
    """Materialize queued requests and advance every active lab once."""

    intake = ResearchIntake(args.lab_root)
    intake.intake_dir.mkdir(parents=True, exist_ok=True)
    intake.labs_dir.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, Any]] = []
    while True:
        for request_id in intake.pending_ids():
            try:
                intake.materialize(request_id, repo_root=args.repo_root)
            except (ResearchLabError, ValueError, OSError, json.JSONDecodeError) as exc:
                intake.record_error(request_id, phase="MATERIALIZE", error=f"{type(exc).__name__}: {exc}")
                reports.append({"request_id": request_id, "status": "BLOCKED", "error": f"{type(exc).__name__}: {exc}"})

        lab_paths = sorted(path for path in intake.labs_dir.iterdir() if path.is_dir()) if intake.labs_dir.exists() else []
        for lab_path in lab_paths:
            cycle_args = argparse.Namespace(
                lab_root=lab_path,
                repo_root=args.repo_root,
                agent=args.agent,
                loop=False,
                interval_min=args.interval_min,
                timeout_seconds=args.timeout_seconds,
                max_agent_attempts=args.max_agent_attempts,
            )
            try:
                reports.append(run_cycle(cycle_args))
                intake.clear_error(lab_path.name)
            except (ResearchLabError, ValueError, OSError, json.JSONDecodeError) as exc:
                intake.record_error(lab_path.name, phase="CYCLE", error=f"{type(exc).__name__}: {exc}")
                reports.append({"lab_id": lab_path.name, "status": "BLOCKED", "error": f"{type(exc).__name__}: {exc}"})

        snapshot = {"status": "WORKER_CYCLE_COMPLETED", "labs": reports[-len(lab_paths):] if lab_paths else []}
        if not args.loop:
            return snapshot
        print(json.dumps(snapshot, ensure_ascii=False), flush=True)
        time.sleep(max(30.0, args.interval_min * 60.0))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes Autonomous Strategy Research")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--goal", required=True)
    init.add_argument("--universe", default="unspecified")
    init.add_argument("--horizon", default="unspecified")
    init.add_argument("--constraint", action="append")
    init.add_argument("--replace", action="store_true")
    cycle = sub.add_parser("cycle")
    cycle.add_argument("--agent", action="store_true")
    cycle.add_argument("--loop", action="store_true")
    cycle.add_argument("--interval-min", type=float, default=float(os.getenv("AUTONOMOUS_RESEARCH_INTERVAL_MIN", "30")))
    cycle.add_argument("--timeout-seconds", type=int, default=int(os.getenv("AUTONOMOUS_RESEARCH_TIMEOUT_SECONDS", "1800")))
    cycle.add_argument("--max-agent-attempts", type=int, default=DEFAULT_MAX_AGENT_ATTEMPTS)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("result_path", type=Path)
    worker = sub.add_parser("worker")
    worker.add_argument("--agent", action="store_true")
    worker.add_argument("--loop", action="store_true")
    worker.add_argument("--interval-min", type=float, default=float(os.getenv("AUTONOMOUS_RESEARCH_INTERVAL_MIN", "30")))
    worker.add_argument("--timeout-seconds", type=int, default=int(os.getenv("AUTONOMOUS_RESEARCH_TIMEOUT_SECONDS", "1800")))
    worker.add_argument("--max-agent-attempts", type=int, default=DEFAULT_MAX_AGENT_ATTEMPTS)
    for command in (init, cycle, ingest, worker):
        command.add_argument("--lab-root", type=Path, default=DEFAULT_LAB_ROOT)
        command.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            result = init_lab(args)
        elif args.command == "ingest":
            lab = ResearchLab(args.lab_root)
            result = ingest_result(lab, args.result_path)
        elif args.command == "worker":
            result = run_worker(args)
        elif args.loop:
            while True:
                result = run_cycle(args)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                time.sleep(max(30.0, args.interval_min * 60.0))
        else:
            result = run_cycle(args)
    except (ResearchLabError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
