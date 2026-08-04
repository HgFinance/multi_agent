"""Supabase read-only adapter and live-input bridge tests."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

from orchestration.workflows.portfolio_recommendation import (
    run_portfolio_recommendation_pipeline_async,
)


ROOT = Path(__file__).resolve().parents[2]
ADAPTER_PATH = ROOT / "departments/05-accounting-portfolio/portfolio/supabase_readonly.py"
SPEC = importlib.util.spec_from_file_location("test_supabase_readonly_adapter", ADAPTER_PATH)
assert SPEC is not None and SPEC.loader is not None
adapter_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter_module
SPEC.loader.exec_module(adapter_module)

AS_OF = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _catalog_row() -> dict:
    return {
        "strategy_code": "db-balanced",
        "name": "Database Balanced",
        "strategy_version_id": "version-db-balanced",
        "version": 1,
        "target_portfolio_schema": {
            "portfolio_id": "db-balanced",
            "name": "Database Balanced",
            "risk_band": "MEDIUM",
            "minimum_experience": "BEGINNER",
            "minimum_horizon_years": 3,
            "max_drawdown_pct": "0.15",
            "max_exit_days": 14,
            "target_allocations": {
                "GLOBAL_EQUITY": "0.60",
                "SHORT_TERM_BOND": "0.40",
            },
            "evidence_refs": ["research:db-catalog:v1"],
            "as_of": AS_OF.isoformat(),
        },
        "config": {},
        "effective_from": datetime(2026, 7, 1, tzinfo=timezone.utc),
    }


def test_supabase_adapter_reads_pit_context_without_writes() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(query: str, args: tuple[object, ...]):
        calls.append((query, args))
        if "strategy.versions" in query:
            return [_catalog_row()]
        if "research.documents" in query:
            return [
                {
                    "document_id": "doc-1",
                    "title": "PIT research evidence",
                    "observed_at": AS_OF,
                    "published_at": AS_OF,
                    "status": "ACTIVE",
                    "source_code": "TEST_DB",
                }
            ]
        if "execution.market_snapshots" in query:
            return [
                {
                    "market_snapshot_id": "market-1",
                    "instrument_id": "instrument-1",
                    "as_of": AS_OF,
                    "last_price": "100.00",
                    "quality_status": "PASS",
                    "source_ref": "market-test",
                }
            ]
        return [
            {
                "portfolio_snapshot_id": "snapshot-1",
                "fund_id": "fund-1",
                "as_of": AS_OF,
                "quality_status": "PASS",
                "nav": "100000.00",
            }
        ]

    adapter = adapter_module.SupabaseReadOnlyAdapter(fetcher=fetch)
    snapshot = asyncio.run(adapter.load_snapshot(as_of=AS_OF, fund_id="fund-1"))

    assert snapshot.source == "SUPABASE"
    assert snapshot.quality_status == "PASS"
    assert [item["portfolio_id"] for item in snapshot.candidates] == ["db-balanced"]
    assert snapshot.research_context["status"] == "LIVE"
    assert snapshot.market_context["status"] == "LIVE"
    assert snapshot.accounting_context["status"] == "LIVE"
    assert snapshot.read_only is True
    assert snapshot.external_writes is False
    assert len(calls) == 4
    assert all(query.lstrip().startswith("SELECT") for query, _ in calls)
    assert all(args[-1] == AS_OF for _, args in calls)


def test_missing_suitability_metadata_fails_closed() -> None:
    async def fetch(query: str, args: tuple[object, ...]):
        if "strategy.versions" in query:
            return [{"strategy_code": "schema-only", "target_portfolio_schema": {"type": "array"}}]
        return []

    adapter = adapter_module.SupabaseReadOnlyAdapter(fetcher=fetch)
    snapshot = asyncio.run(adapter.load_snapshot(as_of=AS_OF))

    assert snapshot.candidates == ()
    assert snapshot.quality_status == "WARN"
    assert "NO_VALID_PORTFOLIO_CANDIDATES" in snapshot.reasons


def test_missing_database_configuration_is_safe(monkeypatch) -> None:
    monkeypatch.delenv("SUPABASE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    adapter = adapter_module.SupabaseReadOnlyAdapter(dsn=None, fetcher=None)
    snapshot = asyncio.run(adapter.load_snapshot(as_of=AS_OF))

    assert snapshot.source == "SUPABASE_UNAVAILABLE"
    assert snapshot.quality_status == "UNAVAILABLE"
    assert snapshot.read_only is True
    assert snapshot.external_writes is False


def test_pipeline_accepts_supabase_snapshot_and_keeps_gates_non_binding() -> None:
    async def fetch(query: str, args: tuple[object, ...]):
        if "strategy.versions" in query:
            return [_catalog_row()]
        if "research.documents" in query:
            return [{"document_id": "doc-1", "observed_at": AS_OF, "status": "ACTIVE"}]
        if "execution.market_snapshots" in query:
            return [{"market_snapshot_id": "market-1", "as_of": AS_OF, "quality_status": "PASS"}]
        return []

    adapter = adapter_module.SupabaseReadOnlyAdapter(fetcher=fetch)
    result = asyncio.run(
        run_portfolio_recommendation_pipeline_async(
            {
                "user_id": "supabase-user",
                "mindset": "BALANCED",
                "experience": "INTERMEDIATE",
                "investment_horizon_years": 5,
                "max_drawdown_pct": "0.25",
                "liquidity_need": "MEDIUM",
                "as_of": AS_OF.isoformat(),
            },
            data_adapter=adapter,
        )
    )

    assert result["data_context"]["source"] == "SUPABASE"
    assert result["pipeline_status"] == "COMPLETED"
    assert result["risk_gate"]["verdict"] == "approve"
    assert result["qa_gate"]["decision"] == "PASS"
    assert result["risk_gate"]["binding"] is False
    assert result["qa_gate"]["binding"] is False
    assert result["external_writes"] is False


def test_pipeline_blocks_when_supabase_is_unavailable(monkeypatch) -> None:
    monkeypatch.delenv("SUPABASE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    adapter = adapter_module.SupabaseReadOnlyAdapter(dsn=None, fetcher=None)
    result = asyncio.run(
        run_portfolio_recommendation_pipeline_async(
            {
                "user_id": "supabase-unavailable",
                "mindset": "BALANCED",
                "experience": "BEGINNER",
                "investment_horizon_years": 3,
                "max_drawdown_pct": "0.10",
                "liquidity_need": "HIGH",
                "as_of": AS_OF.isoformat(),
            },
            data_adapter=adapter,
        )
    )

    assert result["pipeline_status"] == "DEGRADED"
    assert result["safe_action"] == "HOLD"
    assert result["data_context"]["source"] == "SUPABASE_UNAVAILABLE"
    assert result["external_writes"] is False
