"""CEO Office employee Worker registry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from departments.employee_worker_runtime import WorkerLLM, WorkerSpec, run_worker_registry, tools_for_specs
except ModuleNotFoundError:  # direct department-local execution
    from employee_worker_runtime import WorkerLLM, WorkerSpec, run_worker_registry, tools_for_specs

WORKER_SPECS = (
    WorkerSpec("executive-briefing-worker", "Executive briefing and cross-department handoff analyst", ("ceo.department_reports.read",), "always", ("research_packet", "order_intent", "risk_decision", "qa_assessment", "accounting_snapshot", "strategy_report")),
)


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None) -> dict[str, Any]:
    return run_worker_registry(WORKER_SPECS, payload, tools=tools_for_specs(WORKER_SPECS), llm=llm)
