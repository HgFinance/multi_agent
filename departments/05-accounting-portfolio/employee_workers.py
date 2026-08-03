"""Accounting/Portfolio employee Worker registry: official figures stay deterministic."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from departments.employee_worker_runtime import WorkerLLM, WorkerSpec, run_worker_registry, tools_for_specs
except ModuleNotFoundError:
    from employee_worker_runtime import WorkerLLM, WorkerSpec, run_worker_registry, tools_for_specs

WORKER_SPECS = (
    WorkerSpec("portfolio-control-worker", "Portfolio control and position-state analyst", ("accounting.portfolio_snapshot.read",), "always", ("portfolio_snapshot", "positions", "cash")),
    WorkerSpec("ledger-reconciliation-worker", "Ledger, fund-accounting and broker-reconciliation analyst", ("accounting.ledger.read", "accounting.reconciliation.read"), "always", ("ledger_snapshot", "fills", "reconciliation")),
    WorkerSpec("nav-close-worker", "NAV close and official-figure readiness analyst", ("accounting.nav_close.read",), "nav_close", ("nav_snapshot", "open_breaks", "approval_state")),
    WorkerSpec("treasury-liquidity-worker", "Treasury, collateral and liquidity analyst", ("accounting.treasury.read",), "treasury_signal", ("cash", "margin", "collateral")),
    WorkerSpec("pnl-attribution-worker", "PnL and performance attribution analyst", ("accounting.pnl.read",), "pnl_request", ("pnl_snapshot", "fills", "costs")),
    WorkerSpec("investor-reporting-worker", "Investor reporting and disclosure consistency analyst", ("accounting.reporting.read",), "investor_report", ("reporting_snapshot", "pnl_snapshot", "risk_snapshot")),
    WorkerSpec("valuation-corporate-actions-worker", "Valuation and corporate-actions analyst", ("accounting.valuation.read", "accounting.corporate_actions.read"), "corporate_action", ("valuation", "corporate_action")),
    WorkerSpec("fee-accrual-tax-worker", "Fee, expense and tax-accrual consistency analyst", ("accounting.fees_tax.read",), "fee_accrual", ("fees", "expenses", "tax_accruals")),
)


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None) -> dict[str, Any]:
    return run_worker_registry(WORKER_SPECS, payload, tools=tools_for_specs(WORKER_SPECS), llm=llm)
