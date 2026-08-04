"""Full cross-department async LangGraph fan-out/fan-in acceptance tests."""

from __future__ import annotations

import asyncio

from orchestration.workflows.portfolio_recommendation import (
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
