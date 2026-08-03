"""Research employee Worker registry: evidence roles only, no trade decision."""

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
    WorkerSpec("research-data-worker", "Universe and market-data stewardship analyst", ("research.universe.read", "research.market_snapshot.read"), "always", ("universe", "market_snapshot")),
    WorkerSpec("microstructure-worker", "Microstructure and liquidity evidence analyst", ("research.microstructure.read",), "market_snapshot", ("market_snapshot", "order_book")),
    WorkerSpec("technical-signal-worker", "Multi-timeframe technical signal analyst", ("research.technical_features.read",), "market_features", ("technical_features", "price_history")),
    WorkerSpec("fundamental-valuation-worker", "Point-in-time fundamental and valuation analyst", ("research.fundamentals.read",), "fundamentals", ("fundamentals", "filings")),
    WorkerSpec("news-macro-worker", "News, sentiment, macro and geopolitical evidence analyst", ("research.news.read", "research.macro.read"), "news_or_macro", ("news", "macro", "geopolitical")),
    WorkerSpec("evidence-rag-worker", "Evidence retrieval and citation curator", ("research.evidence.search",), "evidence_request", ("evidence", "documents")),
)


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None) -> dict[str, Any]:
    return run_worker_registry(WORKER_SPECS, payload, tools=tools_for_specs(WORKER_SPECS), llm=llm)
