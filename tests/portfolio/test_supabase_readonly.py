"""Private control-database read adapter and live-input bridge tests."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from orchestration.workflows.portfolio_recommendation import (
    run_portfolio_recommendation_pipeline_async,
)

os.environ["PORTFOLIO_WORKER_RUNTIME"] = "deterministic_test"

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
                "KOREA_EQUITY": "1.00",
            },
            "evidence_refs": ["research:db-catalog:v1"],
            "as_of": AS_OF.isoformat(),
        },
        "config": {},
        "effective_from": datetime(2026, 7, 1, tzinfo=timezone.utc),
    }


def test_control_db_adapter_reads_pit_context_without_writes() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(query: str, args: tuple[object, ...]):
        calls.append((query, args))
        if "reference.instruments" in query:
            return [
                {
                    "instrument_id": "instrument-1",
                    "symbol": "005930",
                    "exchange": "KRX",
                    "name": "삼성전자",
                    "instrument_type": "EQUITY",
                    "asset_class": "KOREA_EQUITY",
                    "currency": "KRW",
                    "status": "ACTIVE",
                    "market_snapshot_id": "market-1",
                    "market_as_of": AS_OF,
                    "last_price": "100.00",
                    "quality_status": "PASS",
                    "source_ref": "market-test",
                }
            ]
        if "strategy.versions" in query:
            return [_catalog_row()]
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

    adapter = adapter_module.ControlDbReadOnlyAdapter(fetcher=fetch)
    snapshot = asyncio.run(adapter.load_snapshot(as_of=AS_OF, fund_id="fund-1"))

    assert snapshot.source == "CONTROL_DB"
    assert snapshot.quality_status == "PASS"
    assert [item["portfolio_id"] for item in snapshot.candidates] == ["db-balanced"]
    assert snapshot.research_context == {
        "status": "REQUEST_TIME_MCP",
        "source": "REQUEST_TIME_MCP",
        "as_of": AS_OF.isoformat(),
        "documents": [],
        "evidence_refs": [],
        "persistence": "DISABLED",
        "read_only": True,
    }
    assert snapshot.market_context["status"] == "LIVE"
    assert snapshot.market_context["instrument_universe"]["status"] == "LIVE"
    assert snapshot.market_context["instrument_universe"]["instruments"][0]["symbol"] == "005930"
    assert snapshot.accounting_context["status"] == "LIVE"
    assert snapshot.read_only is True
    assert snapshot.external_writes is False
    assert len(calls) == 4
    assert all("research.documents" not in query for query, _ in calls)
    assert all(query.lstrip().startswith("SELECT") for query, _ in calls)
    assert all(args[-1] == AS_OF for _, args in calls)


def test_missing_suitability_metadata_fails_closed() -> None:
    async def fetch(query: str, args: tuple[object, ...]):
        if "strategy.versions" in query:
            return [{"strategy_code": "schema-only", "target_portfolio_schema": {"type": "array"}}]
        return []

    adapter = adapter_module.ControlDbReadOnlyAdapter(fetcher=fetch)
    snapshot = asyncio.run(adapter.load_snapshot(as_of=AS_OF))

    assert snapshot.candidates == ()
    assert snapshot.quality_status == "WARN"
    assert "NO_VALID_PORTFOLIO_CANDIDATES" in snapshot.reasons


def test_missing_database_configuration_is_safe(monkeypatch) -> None:
    monkeypatch.delenv("CONTROL_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    adapter = adapter_module.ControlDbReadOnlyAdapter(dsn=None, fetcher=None)
    snapshot = asyncio.run(adapter.load_snapshot(as_of=AS_OF))

    assert snapshot.source == "CONTROL_DB_UNAVAILABLE"
    assert snapshot.research_context["status"] == "REQUEST_TIME_MCP"
    assert snapshot.quality_status == "UNAVAILABLE"
    assert snapshot.read_only is True
    assert snapshot.external_writes is False


def test_control_db_dsn_contract_ignores_hosted_supabase(monkeypatch) -> None:
    hosted = "postgresql://hosted.invalid/postgres"
    database = "postgresql://private-alias:5432/postgres"
    control = "postgresql://control-db:5432/postgres"
    monkeypatch.setenv("SUPABASE_DATABASE_URL", hosted)
    monkeypatch.delenv("CONTROL_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert adapter_module.ControlDbReadOnlyAdapter().dsn is None

    monkeypatch.setenv("DATABASE_URL", database)
    assert adapter_module.ControlDbReadOnlyAdapter().dsn == database

    monkeypatch.setenv("CONTROL_DATABASE_URL", control)
    assert adapter_module.ControlDbReadOnlyAdapter().dsn == control


def test_legacy_supabase_class_names_are_import_aliases_only() -> None:
    assert adapter_module.SupabaseReadOnlyAdapter is adapter_module.ControlDbReadOnlyAdapter
    assert adapter_module.SupabaseReadSnapshot is adapter_module.ControlDbReadSnapshot


def test_pipeline_accepts_control_db_snapshot_and_keeps_gates_non_binding() -> None:
    async def fetch(query: str, args: tuple[object, ...]):
        if "reference.instruments" in query:
            return [
                {
                    "instrument_id": "instrument-1",
                    "symbol": "005930",
                    "exchange": "KRX",
                    "name": "삼성전자",
                    "instrument_type": "EQUITY",
                    "asset_class": "KOREA_EQUITY",
                    "currency": "KRW",
                    "status": "ACTIVE",
                    "market_snapshot_id": "market-1",
                    "market_as_of": AS_OF,
                    "last_price": "100.00",
                    "quality_status": "PASS",
                    "source_ref": "market-test",
                }
            ]
        if "strategy.versions" in query:
            return [_catalog_row()]
        if "execution.market_snapshots" in query:
            return [{"market_snapshot_id": "market-1", "as_of": AS_OF, "quality_status": "PASS"}]
        return []

    adapter = adapter_module.ControlDbReadOnlyAdapter(fetcher=fetch)
    result = asyncio.run(
        run_portfolio_recommendation_pipeline_async(
            {
                "user_id": "control-db-user",
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

    assert result["data_context"]["source"] == "CONTROL_DB"
    assert result["data_context"]["research"]["status"] == "REQUEST_TIME_MCP"
    assert result["pipeline_status"] == "COMPLETED"
    assert result["risk_gate"]["verdict"] == "approve"
    assert result["qa_gate"]["decision"] == "PASS"
    assert result["risk_gate"]["binding"] is False
    assert result["qa_gate"]["binding"] is False
    assert result["external_writes"] is False


def test_preflight_reports_missing_dsn_without_connecting_legacy(monkeypatch) -> None:
    monkeypatch.delenv("CONTROL_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    diagnostics = asyncio.run(
        adapter_module.ControlDbReadOnlyAdapter(dsn=None).diagnose_connection()
    )

    assert diagnostics.status == "FAIL"
    assert diagnostics.reasons == ("DSN_NOT_CONFIGURED",)
    assert diagnostics.external_writes is False


def test_preflight_classifies_dns_failure_without_exposing_dsn_legacy(monkeypatch) -> None:
    def fail_dns(*_args, **_kwargs):
        raise OSError("simulated DNS failure")

    monkeypatch.setattr(adapter_module.socket, "getaddrinfo", fail_dns)
    adapter = adapter_module.ControlDbReadOnlyAdapter(
        dsn="postgresql://user:secret@example.invalid:5432/postgres",
        driver="psycopg2",
    )

    diagnostics = asyncio.run(adapter.diagnose_connection())

    assert diagnostics.status == "FAIL"
    assert diagnostics.dns_status == "FAIL"
    assert diagnostics.reasons == ("DNS_RESOLUTION_FAILED:OSError",)
    assert "secret" not in str(diagnostics.as_dict())


def test_pipeline_blocks_when_control_db_is_unavailable(monkeypatch) -> None:
    monkeypatch.delenv("CONTROL_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    adapter = adapter_module.ControlDbReadOnlyAdapter(dsn=None, fetcher=None)
    result = asyncio.run(
        run_portfolio_recommendation_pipeline_async(
            {
                "user_id": "control-db-unavailable",
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
    assert result["data_context"]["source"] == "CONTROL_DB_UNAVAILABLE"
    assert result["external_writes"] is False
    assert result["worker_reports"]
    assert {item["status"] for item in result["worker_reports"]} == {"SKIPPED_SAFE"}
    assert all(
        not item["output"]["summary"].startswith("TEST")
        for item in result["worker_reports"]
    )
    assert result["pipeline_events"]
    assert result["pipeline_event_count"] == len(result["pipeline_events"])
    # ▶ **인원수를 박아두지 않는다** (2026-08-11 실측). 이 검사가 지키려는 것은
    #   "자료가 없으면 LLM 워커가 **한 명도 안 돌고** 전원 SKIPPED_SAFE 로 떨어진다"
    #   이지 특정 인원수가 아니다. 그런데 6/1/2/1 을 박아둬서 워커를 개편할 때마다
    #   깨졌고(research 6 -> 2), 그 실패가 **fail-closed 회귀와 구분되지 않았다.**
    #   등록부에서 세면 개편은 통과하고 진짜 회귀만 잡힌다.
    #   risk-runner/qa-runner 같은 결정론 러너는 WORKER_SPECS 밖이라 안 잡힌다.
    from orchestration.workflows.portfolio_recommendation import registered_worker_ids

    registered = registered_worker_ids()
    for stage in ("research", "risk", "qa", "ceo"):
        report = result["department_reports"][stage]
        expected_skipped = len(registered.get(stage, ()))
        assert expected_skipped > 0, f"{stage} 에 등록된 LLM 워커가 없다 - 검사가 무의미"
        assert report["executed"] == 0
        assert report["completed"] == 0
        assert report["skipped_safe"] == expected_skipped, (stage, report)
        assert report["failed_count"] == 0
        assert report["skip_reason"] == "LIVE_DATA_NOT_READY"


def test_preflight_reports_missing_dsn_without_connecting_legacy_duplicate(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CONTROL_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    diagnostics = asyncio.run(
        adapter_module.ControlDbReadOnlyAdapter(dsn=None).diagnose_connection()
    )

    assert diagnostics.status == "FAIL"
    assert diagnostics.reasons == ("DSN_NOT_CONFIGURED",)
    assert diagnostics.external_writes is False


def test_preflight_classifies_dns_failure_without_exposing_dsn_legacy_duplicate(
    monkeypatch,
) -> None:
    def fail_dns(*_args, **_kwargs):
        raise OSError("simulated DNS failure")

    monkeypatch.setattr(adapter_module.socket, "getaddrinfo", fail_dns)
    adapter = adapter_module.ControlDbReadOnlyAdapter(
        dsn="postgresql://user:secret@example.invalid:5432/postgres",
        driver="psycopg2",
    )

    diagnostics = asyncio.run(adapter.diagnose_connection())

    assert diagnostics.status == "FAIL"
    assert diagnostics.dns_status == "FAIL"
    assert diagnostics.reasons == ("DNS_RESOLUTION_FAILED:OSError",)
    assert "secret" not in str(diagnostics.as_dict())
