"""Trading employee Worker registry: proposals and execution plans, never order submission."""

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
    WorkerSpec("market-thesis-worker", "Bull and bear market-thesis debate analyst", ("trading.research_packet.read",), "always", ("research_packet", "market_snapshot")),
    WorkerSpec("trade-proposal-worker", "Trade proposal and OrderIntent analyst", ("trading.portfolio_state.read",), "always", ("research_packet", "portfolio_snapshot", "strategy_bundle")),
    WorkerSpec("order-constraint-worker", "Risk and compliance constraint mapping analyst", ("trading.risk_decision.read",), "risk_decision", ("risk_decision", "order_constraints")),
    WorkerSpec("execution-planning-worker", "Risk-approved execution planning analyst", ("trading.execution_constraints.read",), "approved_risk", ("risk_decision", "order_constraints", "market_snapshot")),
    WorkerSpec("venue-cost-worker", "Broker venue, slippage and transaction-cost analyst", ("trading.venue_cost.read",), "execution_request", ("order_intent", "market_snapshot", "venue_costs")),
    WorkerSpec("derivatives-structure-worker", "Derivatives structure and margin planning analyst", ("trading.derivatives.read",), "derivatives_signal", ("derivatives", "risk_decision")),
)


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None) -> dict[str, Any]:
    return run_worker_registry(WORKER_SPECS, payload, tools=tools_for_specs(WORKER_SPECS), llm=llm)
