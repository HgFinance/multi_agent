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
    assert result["qa_gate"]["decision"] == "WARN"

    expected_counts = {
        "research": 6,
        "trading": 6,
        "risk": 4,
        "qa": 5,
        "accounting": 8,
        "ceo": 1,
    }
    assert set(result["department_reports"]) == set(expected_counts)
    for stage, count in expected_counts.items():
        report = result["department_reports"][stage]
        assert report["status"] == "COMPLETED"
        assert report["executed"] == count
        assert report["failed"] == []
        assert report["fan_out"] is True
        assert report["fan_in"] is True

    assert all(worker["binding"] is False for worker in result["worker_reports"])


def test_worker_registry_loading_is_atomic_under_parallel_fanout():
    module_name = "portfolio_full_pipeline_risk_workers"
    portfolio_pipeline.sys.modules.pop(module_name, None)

    with ThreadPoolExecutor(max_workers=8) as pool:
        modules = list(pool.map(lambda _: portfolio_pipeline._load_module("risk"), range(8)))

    assert all(module is modules[0] for module in modules)
    assert len(modules[0].WORKER_SPECS) == 4
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
    assert result["qa_gate"]["decision"] == "WARN"


def test_live_stage_payload_does_not_invent_conditional_worker_signals():
    payload = _stage_payload(
        {
            "trace_id": "trace-live",
            "case_id": "case-live",
            "as_of": "2026-08-04T00:00:00+00:00",
            "data_context": {"source": "SUPABASE", "quality_status": "WARN"},
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
            _profile(),
            [_candidate("balanced-core")],
        )
    )

    assert result["risk_gate"]["reason"] == "UPSTREAM_WORKER_CONTRACT_FAILED"
    assert result["risk_gate"]["safe_action"] == "HOLD"
    assert result["pipeline_status"] == "DEGRADED"
    assert result["safe_action"] == "HOLD"
