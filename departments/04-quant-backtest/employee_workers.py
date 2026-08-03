"""Quant/Backtest employee Worker registry: research artifacts, no promotion authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from departments.employee_worker_runtime import WorkerLLM, WorkerSpec, run_worker_registry, tools_for_specs
except ModuleNotFoundError:
    from employee_worker_runtime import WorkerLLM, WorkerSpec, run_worker_registry, tools_for_specs

WORKER_SPECS = (
    WorkerSpec("strategy-hypothesis-worker", "Falsifiable strategy hypothesis analyst", ("quant.hypothesis.read",), "always", ("hypothesis", "market_regime")),
    WorkerSpec("dataset-feature-worker", "Point-in-time dataset and feature quality analyst", ("quant.dataset.read",), "always", ("dataset_manifest", "features", "labels")),
    WorkerSpec("backtest-optimization-worker", "Cost-aware backtest and capacity optimization analyst", ("quant.backtest.run", "quant.capacity.check"), "backtest_request", ("backtest_request", "cost_model", "capacity")),
    WorkerSpec("strategy-release-worker", "Champion/challenger strategy release reviewer", ("quant.release.read",), "release_candidate", ("release_candidate", "evaluation")),
    WorkerSpec("ml-quant-worker", "ML quant research and leakage review analyst", ("quant.ml_evaluation.read",), "ml_research", ("ml_evaluation", "dataset_manifest")),
    WorkerSpec("execution-cost-worker", "Execution-cost and turnover sensitivity analyst", ("quant.cost_sensitivity.read",), "cost_sensitivity", ("cost_model", "turnover", "slippage")),
    WorkerSpec("regime-robustness-worker", "Regime robustness and stress-sensitivity analyst", ("quant.regime_robustness.read",), "regime_analysis", ("market_regime", "stress_results", "evaluation")),
)


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None) -> dict[str, Any]:
    return run_worker_registry(WORKER_SPECS, payload, tools=tools_for_specs(WORKER_SPECS), llm=llm)
