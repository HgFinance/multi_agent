"""Portfolio recommendation purpose acceptance through TEST Risk -> QA graphs."""

from __future__ import annotations

from datetime import datetime, timezone

from departments.risk_qa_testkit import run_portfolio_recommendation_pipeline


AS_OF = datetime(2026, 8, 4, tzinfo=timezone.utc).isoformat()


def _profile(**overrides):
    value = {
        "user_id": "user-test-portfolio-001",
        "mindset": "RISK_SEEKING",
        "experience": "INTERMEDIATE",
        "investment_horizon_years": 5,
        "max_drawdown_pct": "0.25",
        "liquidity_need": "MEDIUM",
        "as_of": AS_OF,
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
        "target_allocations": {"GLOBAL_EQUITY": "0.6", "SHORT_TERM_BOND": "0.4"},
        "evidence_refs": ["research:portfolio-catalog:v1"],
        "as_of": AS_OF,
    }
    value.update(overrides)
    return value


def test_recommendation_path_preserves_risk_qa_separation():
    result = run_portfolio_recommendation_pipeline(
        _profile(),
        [
            _candidate("balanced"),
            _candidate("high-risk", risk_band="HIGH", max_drawdown_pct="0.30"),
        ],
    )

    assert result["purpose"] == "portfolio_recommendation"
    assert result["pipeline_status"] == "COMPLETED"
    assert result["safe_action"] == "NO_ACTION"
    assert result["suitability"]["status"] == "MATCHED"
    assert result["suitability"]["recommendations"][0]["portfolio_id"] == "balanced"
    assert result["suitability"]["exclusions"][0]["portfolio_id"] == "high-risk"
    assert result["suitability_context"]["binding"] is False
    assert result["risk_qa"]["risk_gate"]["binding"] is False
    assert result["risk_qa"]["qa_gate"]["binding"] is False
    assert result["risk_qa"]["qa_gate"]["decision"] == "WARN"
    assert result["risk_qa"]["risk"]["head"]["input_hash"] == result["risk_qa"]["packet"]["input_hash"]
    assert result["risk_qa"]["qa"]["received_department_handoff"]["binding"] is False


def test_no_match_holds_and_does_not_upgrade_to_risky_fallback():
    result = run_portfolio_recommendation_pipeline(
        _profile(mindset="SAFETY_FIRST", experience="BEGINNER", max_drawdown_pct="0.05"),
        [_candidate("too-risky", risk_band="HIGH", max_drawdown_pct="0.20")],
    )

    assert result["pipeline_status"] == "COMPLETED"
    assert result["safe_action"] == "HOLD"
    assert result["suitability"]["status"] == "NO_MATCH"
    assert result["suitability"]["recommendations"] == []
    assert result["suitability"]["exclusions"][0]["portfolio_id"] == "too-risky"
    assert result["risk_qa"]["risk_gate"]["verdict"] == "reject"
