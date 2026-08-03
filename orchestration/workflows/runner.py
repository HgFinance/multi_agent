"""Small, safe workflow runner for the cross-department boundary.

The runner is intentionally not a replacement for a department ``scripts.py``.
In ``dry-run`` mode it verifies and records the handoff plan only.  In live
mode callers must provide explicit adapters; an absent adapter is BLOCKED and
never treated as a successful department execution.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from orchestration.adapters import build_paper_e2e_handlers, build_paper_handlers

from .contracts import StepRun, WorkflowRun, WorkflowSpec
from .manifest import load_workflow

StepHandler = Callable[[str, str, Mapping[str, object]], str | None]


def execute_workflow(
    spec: WorkflowSpec,
    *,
    mode: str = "dry-run",
    handlers: Mapping[str, StepHandler] | None = None,
    context: Mapping[str, object] | None = None,
    run_id: str | None = None,
) -> WorkflowRun:
    """Validate and execute an explicit orchestration plan.

    ``dry-run`` creates PLANNED records and never calls a department.  Any
    other mode requires an adapter per step.  This makes missing department
    integration visible instead of fabricating a PASS result.
    """

    if mode not in {"dry-run", "live", "paper-e2e", "paper"}:
        raise ValueError("mode는 dry-run, paper-e2e, paper 또는 live여야 합니다")
    spec.validate()
    handlers = handlers or {}
    context = dict(context or {})
    run_id = run_id or f"wf-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    step_runs: list[StepRun] = []

    for step in spec.steps:
        if mode == "dry-run":
            step_runs.append(
                StepRun(
                    step_id=step.id,
                    sequence=step.sequence,
                    status="PLANNED",
                    input_contract=step.input_contract,
                    output_contract=step.output_contract,
                    failure_action=step.failure_action,
                    attempts=0,
                    detail="boundary validated; department adapter was not called",
                )
            )
            continue

        handler = handlers.get(step.id)
        if handler is None:
            step_runs.append(
                StepRun(
                    step_id=step.id,
                    sequence=step.sequence,
                    status="BLOCKED",
                    input_contract=step.input_contract,
                    output_contract=step.output_contract,
                    failure_action=step.failure_action,
                    attempts=0,
                    detail="no explicit department adapter registered",
                )
            )
            return WorkflowRun(
                run_id=run_id,
                workflow=spec.name,
                mode=mode,
                status="BLOCKED",
 safe_action=step.failure_action,
 steps=tuple(step_runs),
 metadata=dict(context.get("workflow_metadata", {})),
            )

        detail = ""
        attempts = 0
        last_error: Exception | None = None
        for attempts in range(1, step.max_attempts + 1):
            try:
                detail = handler(step.input_contract, step.output_contract, context) or ""
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - boundary must fail closed
                last_error = exc

        if last_error is not None:
            step_runs.append(
                StepRun(
                    step_id=step.id,
                    sequence=step.sequence,
                    status="FAILED",
                    input_contract=step.input_contract,
                    output_contract=step.output_contract,
                    failure_action=step.failure_action,
                    attempts=attempts,
                    detail=f"{type(last_error).__name__}: {last_error}",
                )
            )
            return WorkflowRun(
                run_id=run_id,
                workflow=spec.name,
                mode=mode,
                status="FAILED",
 safe_action=step.failure_action,
 steps=tuple(step_runs),
 metadata=dict(context.get("workflow_metadata", {})),
            )

        step_runs.append(
            StepRun(
                step_id=step.id,
                sequence=step.sequence,
                status="DISPATCHED",
                input_contract=step.input_contract,
                output_contract=step.output_contract,
                failure_action=step.failure_action,
                attempts=attempts,
                detail=detail,
            )
        )

    return WorkflowRun(
        run_id=run_id,
        workflow=spec.name,
        mode=mode,
        status="VALIDATED" if mode == "dry-run" else "COMPLETED",
 safe_action=None,
 steps=tuple(step_runs),
 metadata=dict(context.get("workflow_metadata", {})),
    )


def _jsonable(run: WorkflowRun) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "workflow": run.workflow,
        "mode": run.mode,
        "status": run.status,
        "safe_action": run.safe_action,
 "steps": [step.__dict__ for step in run.steps],
 "metadata": dict(run.metadata),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate/run cross-department workflow boundaries")
    parser.add_argument("--workflow", default="investment-case")
    parser.add_argument(
        "--mode",
 choices=("dry-run", "paper-e2e", "paper", "live"),
        default="dry-run",
 help="dry-run은 계획만, paper-e2e는 smoke만, paper는 읽기 전용 handoff, live는 승인 adapter만 실행합니다.",
    )
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--quantity", type=int, default=100)
    parser.add_argument("--limit-price", default="200.00")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    try:
        repo_root = Path(__file__).resolve().parents[2]
        if args.mode == "paper":
            _load_repo_env(repo_root)
        handlers = None
        context: Mapping[str, object] | None = None
        if args.mode == "paper-e2e":
            handlers = build_paper_e2e_handlers(repo_root)
            context = {
                "case_request": {
                    "case_id": f"paper-e2e-{uuid4().hex[:12]}",
                    "symbol": args.symbol,
                    "side": "BUY",
                    "quantity": args.quantity,
                    "order_type": "LIMIT",
                    "limit_price": args.limit_price,
                    "stage": "paper",
                }
            }
        elif args.mode == "paper":
            handlers = build_paper_handlers(repo_root)
            context = {
                "case_request": {
                    "case_id": f"paper-{uuid4().hex[:12]}",
                    "symbol": args.symbol,
                    "side": "BUY",
                    "quantity": args.quantity,
                    "order_type": "LIMIT",
                    "limit_price": args.limit_price,
                    "stage": "paper",
                }
            }
        run = execute_workflow(
            load_workflow(args.workflow),
            mode=args.mode,
            handlers=handlers,
            context=context,
        )
    except Exception as exc:  # noqa: BLE001 - CLI returns a clear non-zero result
        payload = {"status": "INVALID", "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(payload, ensure_ascii=False) if args.as_json else payload["error"])
        return 2

    payload = _jsonable(run)
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.as_json else f"{run.workflow}: {run.status}")
    return 0 if run.status in {"VALIDATED", "COMPLETED"} else 1


def _load_repo_env(repo_root: Path) -> None:
    """Load root secrets for paper adapters without printing or overriding env."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(repo_root / ".env", override=False)


if __name__ == "__main__":
    raise SystemExit(main())
