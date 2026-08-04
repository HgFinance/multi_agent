"""Deterministic investor-profile to portfolio-list contract tests."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "departments/05-accounting-portfolio/portfolio/suitability.py"
SPEC = importlib.util.spec_from_file_location("portfolio_suitability", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
suitability = importlib.util.module_from_spec(SPEC)
sys.modules["portfolio_suitability"] = suitability
SPEC.loader.exec_module(suitability)


AS_OF = datetime(2026, 8, 4, tzinfo=timezone.utc)


def profile(**overrides):
    value = {
        "user_id": "user-test-001",
        "mindset": "RISK_SEEKING",
        "experience": "INTERMEDIATE",
        "investment_horizon_years": 5,
        "max_drawdown_pct": 0.25,
        "liquidity_need": "MEDIUM",
        "as_of": AS_OF,
    }
    value.update(overrides)
    return value


def candidate(portfolio_id, **overrides):
    value = {
        "portfolio_id": portfolio_id,
        "name": f"Portfolio {portfolio_id}",
        "risk_band": "MEDIUM",
        "minimum_experience": "BEGINNER",
        "minimum_horizon_years": 3,
        "max_drawdown_pct": 0.15,
        "max_exit_days": 14,
        "target_allocations": {"GLOBAL_EQUITY": 0.6, "SHORT_TERM_BOND": 0.4},
        "evidence_refs": ["research:portfolio-catalog:v1"],
        "as_of": AS_OF,
    }
    value.update(overrides)
    return value


def test_profile_is_capped_by_experience_and_returns_ranked_list():
    result = suitability.recommend_portfolios(
        profile(experience="BEGINNER"),
        [
            candidate("balanced", risk_band="MEDIUM"),
            candidate("safe", risk_band="LOW", max_drawdown_pct=0.08),
            candidate("aggressive", risk_band="HIGH", max_drawdown_pct=0.20),
        ],
    )

    assert result.status == suitability.SuitabilityStatus.MATCHED
    assert result.effective_risk_band == suitability.PortfolioRiskBand.LOW
    assert [item.portfolio_id for item in result.recommendations] == ["safe"]
    assert [item.portfolio_id for item in result.exclusions] == ["aggressive", "balanced"]
    assert result.manual_review_required is True
    assert len(result.input_hash) == 64


def test_user_constraints_exclude_candidate_without_fallback_upgrade():
    result = suitability.recommend_portfolios(
        profile(mindset="SAFETY_FIRST", experience="BEGINNER", max_drawdown_pct=0.05),
        [candidate("too-risky", risk_band="MEDIUM", max_drawdown_pct=0.10)],
    )

    assert result.status == suitability.SuitabilityStatus.NO_MATCH
    assert result.recommendations == []
    assert result.exclusions[0].reasons == [
        "RISK_BAND_EXCEEDS_EFFECTIVE_PROFILE_LIMIT",
        "MAX_DRAWDOWN_EXCEEDS_USER_TOLERANCE",
    ]


def test_candidate_contract_requires_balanced_allocations_and_evidence():
    with pytest.raises(ValueError, match="sum to 1"):
        suitability.PortfolioCandidate(**candidate("bad-weight", target_allocations={"EQUITY": 0.9}))

    with pytest.raises(ValueError, match="evidence_refs"):
        suitability.PortfolioCandidate(**candidate("bad-evidence", evidence_refs=[" "]))


def test_future_candidate_is_excluded_by_point_in_time_rule():
    future = datetime(2026, 8, 5, tzinfo=timezone.utc)
    result = suitability.recommend_portfolios(
        profile(),
        [candidate("future", as_of=future)],
    )

    assert result.status == suitability.SuitabilityStatus.NO_MATCH
    assert result.exclusions[0].reasons == ["CANDIDATE_AFTER_PROFILE_AS_OF"]


def test_same_input_is_reproducible():
    candidates = [candidate("a"), candidate("b", risk_band="LOW", max_drawdown_pct=0.08)]
    first = suitability.recommend_portfolios(profile(), candidates)
    second = suitability.recommend_portfolios(profile(), list(reversed(candidates)))

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
