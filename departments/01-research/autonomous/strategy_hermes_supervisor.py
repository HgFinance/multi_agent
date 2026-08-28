"""Thin lifecycle supervisor for the direct Strategy Hermes worker.

The supervisor owns no research decisions. It materializes durable intake,
starts one direct Hermes process per active lab, and mechanically validates the
artifacts Hermes leaves behind. The actual researcher is Hermes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from artifact_validator import sync_agent_artifacts
from autonomous_research_ingress import ResearchIntake
from hermes_agent import StrategyHermesAgent
from lab import ResearchLab, ResearchLabError
from models import ExperimentResult

DEFAULT_LAB_ROOT = Path(os.getenv("AUTONOMOUS_RESEARCH_LAB_ROOT", "/var/lib/autonomous-research"))
DEFAULT_REPO_ROOT = Path(os.getenv("AUTONOMOUS_RESEARCH_REPO_ROOT", str(HERE.parents[3])))
MANAGED_MARKER = ".strategy-hermes-managed"


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    cycle_started = time.monotonic()
    intake = ResearchIntake(args.lab_root)
    for path in (intake.root, intake.intake_dir, intake.labs_dir, intake.errors_dir):
        path.mkdir(parents=True, exist_ok=True)

    reports: list[dict[str, Any]] = []
    lab_reports: dict[Path, dict[str, Any]] = {}
    request_filter = str(getattr(args, "request_id", "") or "").strip()
    retry_blocked = bool(getattr(args, "retry_blocked", False))
    pending_ids = intake.pending_ids()
    if request_filter:
        pending_ids = tuple(request_id for request_id in pending_ids if request_id == request_filter)
    for request_id in pending_ids:
        try:
            intake.materialize(request_id, repo_root=args.repo_root)
        except (ResearchLabError, ValueError, OSError, json.JSONDecodeError) as exc:
            intake.record_error(request_id, phase="MATERIALIZE", error=f"{type(exc).__name__}: {exc}")
            reports.append({"request_id": request_id, "status": "BLOCKED", "error": str(exc)})

    lab_paths = sorted(path for path in intake.labs_dir.iterdir() if path.is_dir())
    if request_filter:
        lab_paths = [path for path in lab_paths if path.name == request_filter]
    runnable_labs: list[Path] = []
    max_concurrency = 1
    for lab_path in lab_paths:
        if not _is_strategy_hermes_lab(lab_path):
            # Existing labs from the retired Python/factory-era worker remain
            # readable for rollback and audit, but are never re-executed by the
            # new direct Strategy Hermes worker.
            lab_reports[lab_path] = {
                "lab_id": lab_path.name,
                "status": "PRESERVED_LEGACY_LAB",
            }
            continue
        error_path = intake.errors_dir / f"{lab_path.name}.json"
        if error_path.exists() and not retry_blocked:
            # A durable worker/validation error is a human-visible stop, not a
            # polling trigger. Re-running the same invalid result every cycle
            # burns model budget and can make an old request look active. An
            # operator must explicitly opt into a retry after changing the
            # input, data or runtime contract.
            try:
                error = intake._read_json(error_path)
            except (OSError, json.JSONDecodeError, ValueError):
                error = {"error": "unreadable persisted worker error"}
            lab_reports[lab_path] = {
                "lab_id": lab_path.name,
                "status": "BLOCKED",
                "error": str(error.get("error") or "persisted worker error"),
            }
            continue
        runnable_labs.append(lab_path)

    max_concurrency = _configured_max_concurrency(args, len(runnable_labs))
    if max_concurrency == 1:
        for lab_path in runnable_labs:
            lab_reports[lab_path] = _run_managed_lab(args, intake, lab_path)
    else:
        # Each lab owns a separate directory and subprocess workspace. The
        # shared intake was fully materialized above, so only independent lab
        # execution is concurrent. Results are restored to sorted lab order to
        # keep JSON output and replay comparisons deterministic.
        results: dict[Path, dict[str, Any]] = {}
        with ThreadPoolExecutor(
            max_workers=max_concurrency,
            thread_name_prefix="strategy-hermes-lab",
        ) as pool:
            futures = {
                pool.submit(_run_managed_lab, args, intake, lab_path): lab_path
                for lab_path in runnable_labs
            }
            for future in as_completed(futures):
                lab_path = futures[future]
                results[lab_path] = future.result()
        lab_reports.update(results)

    reports.extend(lab_reports[lab_path] for lab_path in lab_paths if lab_path in lab_reports)

    return {
        "status": "STRATEGY_HERMES_CYCLE_COMPLETED",
        "labs": reports,
        "execution": {
            "managed_lab_count": len(runnable_labs),
            "max_concurrency": max_concurrency,
            "duration_seconds": round(time.monotonic() - cycle_started, 3),
        },
    }


def _configured_max_concurrency(args: argparse.Namespace, lab_count: int) -> int:
    """Return a bounded execution fan-out for independent research labs."""

    if lab_count <= 0:
        return 1
    configured = getattr(args, "max_concurrency", None)
    if configured is None:
        configured = os.getenv("AUTONOMOUS_RESEARCH_MAX_CONCURRENCY", "2")
    try:
        requested = int(configured)
    except (TypeError, ValueError):
        requested = 2
    return max(1, min(requested, lab_count))


def _run_managed_lab(
    args: argparse.Namespace,
    intake: ResearchIntake,
    lab_path: Path,
) -> dict[str, Any]:
    """Run one isolated lab and preserve the previous durable error policy."""

    try:
        report = _run_lab(args, lab_path)
        intake.clear_error(lab_path.name)
        return report
    except (ResearchLabError, ValueError, OSError, json.JSONDecodeError) as exc:
        intake.record_error(
            lab_path.name,
            phase="HERMES_OR_VERIFY",
            error=f"{type(exc).__name__}: {exc}",
        )
        return {
            "lab_id": lab_path.name,
            "status": "BLOCKED",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _run_lab(args: argparse.Namespace, lab_path: Path) -> dict[str, Any]:
    lab = ResearchLab(lab_path)
    state = lab.state()
    if (lab_path / "candidate.json").exists():
        return {"lab_id": lab_path.name, "status": "CANDIDATE", "cycle": state.get("cycle", 0)}
    existing_results = lab.results()
    retry_blocked = bool(getattr(args, "retry_blocked", False))
    if existing_results and existing_results[-1].status == "BLOCKED":
        # A result can have been authored by Hermes before a supervisor
        # restart.  Finish the mechanical ingest before waiting for new data;
        # do not spend another expensive model turn on the same blocked plan.
        # An operator-requested retry is the narrow exception for a transient
        # live market-data timeout; schema/contract failures remain terminal.
        if not (retry_blocked and _is_retryable_market_data_block(existing_results[-1])):
            decisions = sync_agent_artifacts(lab)
            lab.update_state(last_action="AWAITING_NEW_DATA")
            return {
                "lab_id": lab_path.name,
                "status": "AWAITING_NEW_DATA",
                "cycle": state.get("cycle", 0),
                "last_result": existing_results[-1].plan_id,
                "decisions": decisions,
            }
        # An operator-requested retry is meaningful for transient sources such
        # as LS t1444.  The old guard ignored --retry-blocked here and merely
        # returned AWAITING_NEW_DATA forever, so a recovered provider could
        # never be tested without creating a new lab. Hermes still owns the
        # next plan and all prior blocked artifacts remain append-only history.
        lab.update_state(last_action="RETRY_BLOCKED_REQUESTED")
    if existing_results and existing_results[-1].status == "COMPLETED":
        # A completed result is durable evidence, not a polling trigger.  The
        # previous loop would start Hermes again on every supervisor cycle
        # because it only special-cased BLOCKED results.  That could overwrite
        # the visible state with HERMES_RUNNING and make one finished request
        # look perpetually active (and spend model/API budget repeatedly).
        lab.update_state(active_plan_id=None, last_action="RESULT_RECORDED")
        return {
            "lab_id": lab_path.name,
            "status": "COMPLETED",
            "cycle": state.get("cycle", 0),
            "last_result": existing_results[-1].plan_id,
            "decisions": [],
            "result_available": True,
        }

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
    # A process timeout can happen after Hermes has preregistered a plan but
    # before it writes a result.  Keep the lab auditable and stop the service
    # loop from replaying the same expensive request forever.  This result is
    # deliberately BLOCKED/FAILED evidence, never a fabricated zero metric.
    if run.status != "COMPLETED" and not any(lab.results_dir.glob("*.json")):
        lab.record_result(_agent_failure_result(lab, run))
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


def _is_strategy_hermes_lab(lab_path: Path) -> bool:
    """Select only new-worker labs without deleting or mutating legacy labs."""

    marker = lab_path / MANAGED_MARKER
    if marker.exists():
        return True

    events_path = lab_path / "events.jsonl"
    if not events_path.exists() or events_path.stat().st_size == 0:
        marker.touch()
        return True

    try:
        event_types = {
            json.loads(line).get("event_type")
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    except (OSError, json.JSONDecodeError):
        return False

    # The current worker writes AGENT_RUN before verification.  This permits
    # the already-started Discord run to finish after a supervisor upgrade,
    # while historical labs with hypotheses/plans are kept untouched.
    if event_types and event_types <= {"AGENT_RUN"}:
        marker.touch()
        return True
    return False


def _agent_failure_result(lab: ResearchLab, run: object) -> ExperimentResult:
    """Create a valid terminal result when the agent leaves no result file."""

    state = lab.state()
    plan_id = str(
        getattr(run, "plan_id", None)
        or state.get("active_plan_id")
        or f"agent-run-{getattr(run, 'run_id', 'unknown')}"
    )
    plan = next((item for item in lab.plans() if item.get("plan_id") == plan_id), {})
    run_status = str(getattr(run, "status", "FAILED") or "FAILED").upper()
    status = "BLOCKED" if run_status == "TIMED_OUT" else "FAILED"
    error = str(getattr(run, "error", None) or f"Hermes returned {run_status}")
    return ExperimentResult(
        plan_id=plan_id,
        status=status,
        cost_included=False,
        oos_evaluated=False,
        leakage_detected=False,
        robustness={
            "agent_run_completed": False,
            "result_contract_written": True,
            "point_in_time_availability_checked": False,
            "forward_or_paper_observation_checked": False,
        },
        metrics={
            "agent_duration_seconds": float(getattr(run, "duration_seconds", 0.0) or 0.0),
            "measured_strategy_metrics_available": False,
        },
        artifacts=tuple(
            item
            for item in (
                str(getattr(run, "output_path", "") or ""),
                str(getattr(run, "usage_path", "") or ""),
            )
            if item
        ),
        failure_modes=(
            "The Hermes process ended without a valid experiment result artifact.",
            "No strategy performance metric was inferred from the incomplete run.",
        ),
        limitations=(
            "This terminal result records an operational failure only; it is not a candidate.",
            "The registered plan remains unmeasured and requires a new request for another attempt.",
        ),
        preregistration_hash=(str(plan.get("preregistration_hash") or "").strip() or None),
        failure_reason=f"{status}: {error}; no valid result artifact was recorded",
    )


def _is_retryable_market_data_block(result: object) -> bool:
    """Allow explicit recovery only for a bounded, external data timeout."""

    reason = str(getattr(result, "failure_reason", "") or "").casefold()
    if not reason:
        return False
    return (
        ("timeout" in reason or "timed out" in reason)
        and (
            "market-data" in reason
            or "market data" in reason
            or "ranking" in reason
            or "t1444" in reason
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Direct Strategy Hermes lifecycle supervisor")
    parser.add_argument("--lab-root", type=Path, default=DEFAULT_LAB_ROOT)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--interval-min", type=float, default=float(os.getenv("AUTONOMOUS_RESEARCH_INTERVAL_MIN", "0.5")))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.getenv("AUTONOMOUS_RESEARCH_TIMEOUT_SECONDS", "1800")))
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=int(os.getenv("AUTONOMOUS_RESEARCH_MAX_CONCURRENCY", "2")),
        help="Maximum number of independent labs to execute concurrently",
    )
    parser.add_argument(
        "--request-id",
        help="Process only this request/lab (manual tracing; the service loop remains unfiltered)",
    )
    parser.add_argument(
        "--retry-blocked",
        action="store_true",
        help="Explicitly retry labs with a persisted worker/validation error",
    )
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
