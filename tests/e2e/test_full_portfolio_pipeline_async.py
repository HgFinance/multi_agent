"""Full cross-department async LangGraph fan-out/fan-in acceptance tests."""

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor

import orchestration.workflows.portfolio_recommendation as portfolio_pipeline
from orchestration.workflows.portfolio_recommendation import (
    _stage_payload,
    run_portfolio_recommendation_pipeline_async,
)


def _profile(**overrides):
    value = {
        "user_id": "user-full-pipeline-test",
        "mindset": "RISK_SEEKING",
        "experience": "INTERMEDIATE",
        "investment_horizon_years": 5,
        "max_drawdown_pct": "0.25",
        "liquidity_need": "MEDIUM",
        "as_of": "2026-08-04T00:00:00+00:00",
    }
    value.update(overrides)
    return value


def _candidate(portfolio_id, **overrides):
    value = {
        "portfolio_id": portfolio_id,
        "name": f"Portfolio {portfolio_id}",
        "risk_band": "MEDIUM",
        "minimum_experience": "BEGINNER",
        "minimum_horizon_years": 3,
        "max_drawdown_pct": "0.15",
        "max_exit_days": 14,
        "target_allocations": {"GLOBAL_EQUITY": "0.60", "SHORT_TERM_BOND": "0.40"},
        "evidence_refs": ["research:portfolio-catalog:v1"],
        "as_of": "2026-08-04T00:00:00+00:00",
    }
    value.update(overrides)
    return value


def test_full_pipeline_uses_async_langgraph_fanout_and_fanin():
    result = asyncio.run(
        run_portfolio_recommendation_pipeline_async(
            _profile(),
            [
                _candidate("balanced-core"),
                _candidate(
                    "aggressive-growth",
                    risk_band="HIGH",
                    minimum_experience="EXPERIENCED",
                    minimum_horizon_years=7,
                    max_drawdown_pct="0.35",
                    max_exit_days=30,
                ),
            ],
        )
    )

    assert result["pipeline_status"] == "COMPLETED"
    assert result["safe_action"] == "NO_ACTION"
    assert result["production_enabled"] is False
    assert result["external_writes"] is False
    assert result["suitability"]["recommendations"][0]["portfolio_id"] == "balanced-core"
    assert result["risk_gate"]["verdict"] == "approve"
    # QA audits the exact delivered CEO envelope after this response-plane
    # graph returns. The durable runtime projection resolves this PENDING row
    # from the post-response QA completion event.
    assert result["qa_gate"]["decision"] == "PENDING"

    expected_counts = {
        # Research workers are conditional: without a holdings question or an
        # experiment proposal, the department must not invent work.
        "research": 0,
        # 2026-08-06: Risk는 LLM 1명(compliance-policy-worker)과
        # 결정론 risk-runner 1명으로 축소했다. 이 파이프라인 count는 LLM만 센다.
        "risk": 1,
        # Trading has no fixed LLM workers; strategy-bound workers are dynamic and deterministic.
        "trading": 0,
        # Quant workers require an experiment card or an authoring request.
        "quant": 0,
        # 2026-08-07: 회계는 LLM 1명(exception-investigation-worker)과 결정론
        # back-office-runner 1명으로 축소했다. 헌장상(마스터플랜 19.12) 에이전트 일이
        # "예외 조사와 설명" 하나뿐이라 도메인별 7명이 전부 결정론 전달 계층이었다.
        # 이 파이프라인 count는 LLM만 센다.
        "accounting": 1,
        "ceo": 1,
    }
    assert set(result["department_reports"]) == set(expected_counts) | {"qa"}
    for stage, count in expected_counts.items():
        report = result["department_reports"][stage]
        expected_status = "NOT_APPLICABLE" if stage == "trading" else "COMPLETED"
        assert report["status"] == expected_status
        assert report["executed"] == count
        assert report["failed"] == []
        assert report["fan_out"] is True
        assert report["fan_in"] is True
        if stage == "trading":
            assert report["skip_reason"] == "NO_VALID_STRATEGY_BUNDLE"
            assert report["skipped_safe"] == 1

    qa_report = result["department_reports"]["qa"]
    assert qa_report["status"] == "PENDING"
    assert qa_report["executed"] == 0
    assert qa_report["skip_reason"] == "POST_RESPONSE_AUDIT_PENDING"
    assert qa_report["fan_out"] is True
    assert qa_report["fan_in"] is True

    assert all(worker["binding"] is False for worker in result["worker_reports"])
    response_plane_risk_workers = [
        worker for worker in result["worker_reports"] if worker["stage"] == "risk"
    ]
    assert len(response_plane_risk_workers) == 1
    assert all(
        worker["technology"]["write_capability"] == "NONE"
        for worker in response_plane_risk_workers
    )
    assert all(worker["technology"]["stack"] for worker in response_plane_risk_workers)
    # QA workers are intentionally absent from this response-plane envelope;
    # their completion is projected by the post-response audit event.
    assert not any(worker["stage"] == "qa" for worker in result["worker_reports"])


def test_worker_registry_loading_is_atomic_under_parallel_fanout():
    module_name = "portfolio_full_pipeline_risk_workers"
    portfolio_pipeline.sys.modules.pop(module_name, None)

    with ThreadPoolExecutor(max_workers=8) as pool:
        modules = list(pool.map(lambda _: portfolio_pipeline._load_module("risk"), range(8)))

    assert all(module is modules[0] for module in modules)
    assert len(modules[0].WORKER_SPECS) == 1
    assert sys.modules[module_name] is modules[0]


def test_full_pipeline_holds_when_no_suitable_candidate_exists():
    result = asyncio.run(
        run_portfolio_recommendation_pipeline_async(
            _profile(mindset="SAFETY_FIRST", experience="BEGINNER", max_drawdown_pct="0.05"),
            [_candidate("aggressive", risk_band="HIGH", max_drawdown_pct="0.25")],
        )
    )

    assert result["pipeline_status"] == "COMPLETED"
    assert result["safe_action"] == "HOLD"
    assert result["suitability"]["status"] == "NO_MATCH"
    assert result["suitability"]["recommendations"] == []
    assert result["risk_gate"]["verdict"] == "reject"
    assert result["qa_gate"]["decision"] == "PENDING"


def test_live_stage_payload_does_not_invent_conditional_worker_signals():
    payload = _stage_payload(
        {
            "trace_id": "trace-live",
            "case_id": "case-live",
            "as_of": "2026-08-04T00:00:00+00:00",
            "data_context": {"source": "CONTROL_DB", "quality_status": "WARN"},
        },
        "qa",
    )

    assert payload["compliance"] == {}
    assert payload["counterparty"] == {}
    assert payload["derivatives"] == {}
    assert payload["assessment"] == {}
    assert payload["model_risk"] == {}
    assert payload["internal_audit"] == {}
    assert payload["ops_assessment"] == {}
    assert payload["permission_check"] == {}
    assert payload["incident"] == {}


def test_request_time_evidence_is_loaded_once_per_pipeline(monkeypatch):
    calls = {"broker": 0, "price": 0, "news": 0, "ownership": 0}

    def broker():
        calls["broker"] += 1
        return {"status": "OK", "authoritative": False}

    def price(_query):
        calls["price"] += 1
        return {"status": "OK", "symbol": "005930"}

    def news(_query):
        calls["news"] += 1
        return {"status": "OK", "symbol": "005930", "headlines": []}

    def ownership():
        calls["ownership"] += 1
        return {"status": "OK"}

    monkeypatch.setenv("PORTFOLIO_WORKER_RUNTIME", "deterministic_test")
    monkeypatch.setattr(portfolio_pipeline, "_broker_account_context", broker)
    monkeypatch.setattr(portfolio_pipeline, "_query_price_levels", price)
    monkeypatch.setattr(portfolio_pipeline, "_query_news_evidence", news)
    monkeypatch.setattr(portfolio_pipeline, "_query_ownership_scan", ownership)

    result = asyncio.run(
        run_portfolio_recommendation_pipeline_async(
            _profile(query="삼성전자 가격과 뉴스 분석"),
            [_candidate("balanced-core")],
        )
    )

    assert result["pipeline_status"] == "COMPLETED"
    assert calls == {"broker": 1, "price": 1, "news": 1, "ownership": 0}


def test_failed_research_contract_cannot_surface_as_no_action(monkeypatch):
    original_invoke = portfolio_pipeline._invoke_worker

    async def invalid_research_worker(stage, spec, payload, *, event_callback=None):
        if stage != "research":
            return await original_invoke(
                stage,
                spec,
                payload,
                event_callback=event_callback,
            )
        return {
            "stage": stage,
            "worker_id": spec.worker_id,
            "status": "DEGRADED",
            "attempts": 1,
            "output": {
                "worker_id": spec.worker_id,
                "summary": "invalid research contract",
                "confidence": 0.0,
                "evidence_refs": [],
                "escalate": True,
                "schema_valid": False,
            },
            "error": "worker_context_contract_invalid",
            "contract_validation": {
                "status": "FAIL",
                "safe_action": "HOLD",
            },
            "output_contract": spec.output_contract,
            "input_hash": payload["input_hash"],
            "binding": False,
        }

    monkeypatch.setattr(portfolio_pipeline, "_invoke_worker", invalid_research_worker)
    result = asyncio.run(
        run_portfolio_recommendation_pipeline_async(
            _profile(query="보유 종목을 검토해줘"),
            [_candidate("balanced-core")],
        )
    )

    assert result["risk_gate"]["reason"] == "UPSTREAM_WORKER_CONTRACT_FAILED"
    assert result["risk_gate"]["safe_action"] == "HOLD"
    assert result["pipeline_status"] == "DEGRADED"
    assert result["safe_action"] == "HOLD"
