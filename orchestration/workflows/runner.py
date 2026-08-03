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
from uuid import uuid4

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

    if mode not in {"dry-run", "live"}:
        raise ValueError("mode는 dry-run 또는 live여야 합니다")
    spec.validate()
    handlers = handlers or {}
    context = context or {}
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
            )

        try:
            detail = handler(step.input_contract, step.output_contract, context) or ""
        except Exception as exc:  # noqa: BLE001 - boundary must fail closed
            step_runs.append(
                StepRun(
                    step_id=step.id,
                    sequence=step.sequence,
                    status="FAILED",
                    input_contract=step.input_contract,
                    output_contract=step.output_contract,
                    failure_action=step.failure_action,
                    attempts=1,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            return WorkflowRun(
                run_id=run_id,
                workflow=spec.name,
                mode=mode,
                status="FAILED",
                safe_action=step.failure_action,
                steps=tuple(step_runs),
            )

        step_runs.append(
            StepRun(
                step_id=step.id,
                sequence=step.sequence,
                status="DISPATCHED",
                input_contract=step.input_contract,
                output_contract=step.output_contract,
                failure_action=step.failure_action,
                attempts=1,
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
    )


def _jsonable(run: WorkflowRun) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "workflow": run.workflow,
        "mode": run.mode,
        "status": run.status,
        "safe_action": run.safe_action,
        "steps": [step.__dict__ for step in run.steps],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate/run cross-department workflow boundaries")
    parser.add_argument("--workflow", default="investment-case")
    parser.add_argument(
        "--mode",
        choices=("dry-run", "live"),
        default="dry-run",
        help="dry-run은 계획만 검증합니다. live는 명시적 adapter 없이는 BLOCKED입니다.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    try:
        run = execute_workflow(load_workflow(args.workflow), mode=args.mode)
    except Exception as exc:  # noqa: BLE001 - CLI returns a clear non-zero result
        payload = {"status": "INVALID", "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(payload, ensure_ascii=False) if args.as_json else payload["error"])
        return 2

    payload = _jsonable(run)
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.as_json else f"{run.workflow}: {run.status}")
    return 0 if run.status == "VALIDATED" else 1


if __name__ == "__main__":
    raise SystemExit(main())

