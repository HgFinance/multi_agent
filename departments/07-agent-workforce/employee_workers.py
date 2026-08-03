"""Agent Workforce employee Worker registry: proposals only, no self-approval or IAM grant."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from departments.employee_worker_runtime import (
        WorkerLLM,
        WorkerSpec,
        run_worker_registry,
        tools_for_specs,
    )
except ModuleNotFoundError:
    from employee_worker_runtime import (
        WorkerLLM,
        WorkerSpec,
        run_worker_registry,
        tools_for_specs,
    )

WORKER_SPECS = (
    WorkerSpec("workforce-planning-worker", "Queue, SLA, cost and staffing-gap analyst", ("workforce.queue.read", "workforce.sla.read"), "always", ("queue_metrics", "sla_metrics", "cost_metrics")),
    WorkerSpec("profile-architecture-worker", "Agent profile and role-boundary architect", ("workforce.profile.read",), "always", ("profile", "role_requirements", "tool_catalog")),
    WorkerSpec("selection-performance-worker", "Candidate selection and performance-evaluation analyst", ("workforce.evaluation.read",), "performance_signal", ("evaluation", "scorecard", "probation")),
    WorkerSpec("lifecycle-coordination-worker", "Joiner, mover and leaver lifecycle coordinator", ("workforce.lifecycle.read",), "lifecycle_event", ("lifecycle_event", "access_request", "memory_namespace")),
    WorkerSpec("workforce-governance-worker", "Workforce approval routing and segregation-of-duties analyst", ("workforce.governance.read",), "governance_request", ("approval_route", "separation_of_duties", "department_boundary")),
)


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None) -> dict[str, Any]:
    return run_worker_registry(WORKER_SPECS, payload, tools=tools_for_specs(WORKER_SPECS), llm=llm)
