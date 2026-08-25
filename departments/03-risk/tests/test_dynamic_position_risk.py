from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

RISK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RISK))

from mandate_limit_compiler import compile_mandate_limits
from mandate_presets import RISK_PRESETS, resolve_risk_preset
from position_risk_lifecycle import (
    RiskPlanState,
    RiskPlanTransition,
    conditional_rule_idempotency_key,
    validate_superseding_plan,
    validate_transition,
)
from position_risk_planner import plan_position_risk

NOW = datetime(2026, 8, 25, 2, 0, tzinfo=timezone.utc)


def _request(**overrides):
    payload = {
        "fund_id": str(uuid4()),
        "instrument_id": "000660",
        "mandate_version_id": str(uuid4()),
        "portfolio_snapshot_id": "portfolio-1",
        "portfolio_snapshot_observed_at": (NOW - timedelta(seconds=30)).isoformat(),
        "portfolio_snapshot_authoritative": True,
        "market": {
            "market_snapshot_id": "market-1",
            "observed_at": (NOW - timedelta(seconds=10)).isoformat(),
            "authoritative": True,
            "last_price": "1600000",
            "atr": "30000",
            "realized_vol_annualized": "0.22",
            "trend_score": "0.10",
            "spread_bps": "8",
            "gap_risk_pct": "0.01",
            "tradable": True,
        },
        "mandate": {
            "base_capital": "100000000",
            "trade_risk_budget_pct": "0.005",
            "max_instrument_weight": "0.10",
            "min_reward_risk_ratio": "1.20",
        },
        "as_of": NOW.isoformat(),
        "task_id": "t-risk-1",
        "trace_id": "trace-risk-1",
        "current_quantity": "3",
    }
    payload.update(overrides)
    return payload


def test_all_nine_presets_follow_one_effective_score_matrix():
    assert len(RISK_PRESETS) == 9
    beginner_aggressive = resolve_risk_preset("RISK_SEEKING", "BEGINNER")
    conservative_expert = resolve_risk_preset("SAFETY_FIRST", "EXPERIENCED")
    aggressive_expert = resolve_risk_preset("RISK_SEEKING", "EXPERIENCED")
    assert beginner_aggressive.max_instrument_weight == Decimal("0.10")
    assert conservative_expert.max_instrument_weight == Decimal("0.10")
    assert aggressive_expert.max_gross_exposure == Decimal("2.50")
    assert aggressive_expert.max_drawdown_pct == Decimal("0.35")


def test_unversioned_mandate_is_never_compiled_or_planned():
    compilation = compile_mandate_limits(
        {
            "fund_id": str(uuid4()),
            "mandate_id": str(uuid4()),
            "mandate_version_id": "unversioned",
            "mandate_version": None,
            "mandate_status": "ACTIVE",
            "approval_status": "APPROVED",
            "mindset": "SAFETY_FIRST",
            "experience": "BEGINNER",
            "limits": {
                "base_capital": "100000000",
                "max_instrument_weight": "0.10",
                "max_sector_weight": "0.25",
                "max_gross_exposure": "1.00",
                "max_concurrent_positions": 5,
                "max_daily_loss_pct": "0.02",
                "max_drawdown_pct": "0.15",
                "trade_risk_budget_pct": "0.005",
            },
            "effective_from": NOW.isoformat(),
            "trace_id": "trace-compile",
        }
    )
    assert compilation.status == "REQUIRES_USER_REVIEW"
    assert "UNVERSIONED_MANDATE" in compilation.reason_codes

    plan = plan_position_risk(_request(mandate_version_id="unversioned"))
    assert plan.action == "DEFER"
    assert plan.stop_price is None and plan.quantity_cap is None


def test_wider_stop_reduces_quantity_and_preserves_loss_budget():
    normal = plan_position_risk(_request())
    wide_payload = _request()
    wide_payload["market"]["atr"] = "60000"
    wide = plan_position_risk(wide_payload)
    assert normal.action == wide.action == "PROPOSE"
    assert wide.stop_price < normal.stop_price
    assert wide.quantity_cap < normal.quantity_cap
    for plan in (normal, wide):
        loss = plan.quantity_cap * abs(plan.entry_reference - plan.stop_price)
        assert loss <= plan.position_risk_amount


def test_downtrend_with_bad_reward_risk_defers_and_stress_reduce_only():
    down = _request()
    down["market"]["trend_score"] = "-0.60"
    result = plan_position_risk(down)
    assert result.action == "DEFER"
    assert "MIN_REWARD_RISK_NOT_MET" in result.reason_codes

    stress = _request()
    stress["market"]["realized_vol_annualized"] = "0.70"
    result = plan_position_risk(stress)
    assert result.action == "REDUCE_ONLY"
    assert result.stop_price is None


def test_stale_or_non_authoritative_snapshot_never_gets_prices():
    stale = _request(
        portfolio_snapshot_observed_at=(NOW - timedelta(hours=2)).isoformat()
    )
    result = plan_position_risk(stale)
    assert result.data_quality == "STALE"
    assert result.stop_price is None

    non_authoritative = _request(portfolio_snapshot_authoritative=False)
    result = plan_position_risk(non_authoritative)
    assert result.data_quality == "NON_AUTHORITATIVE"
    assert result.take_profit_price is None


def test_plan_is_deterministic_and_replay_key_is_stable():
    payload = _request()
    one = plan_position_risk(payload)
    two = plan_position_risk(payload)
    assert one == two
    assert conditional_rule_idempotency_key(one) == conditional_rule_idempotency_key(two)


def test_lifecycle_authority_and_relaxation_rules():
    transition = RiskPlanTransition(
        risk_plan_id=str(uuid4()),
        from_state=RiskPlanState.PROPOSED,
        to_state=RiskPlanState.VALIDATED,
        occurred_at=NOW,
        actor_type="RISK",
        actor_id="risk-management",
        reason="deterministic invariants passed",
        trace_id="trace-1",
        task_id="task-1",
        idempotency_key="transition-1",
    )
    validate_transition(transition)
    with pytest.raises(ValueError, match="cannot enter"):
        validate_transition(transition.model_copy(update={"actor_type": "TRADING"}))

    current = plan_position_risk(_request())
    proposed_payload = _request(
        fund_id=str(current.fund_id),
        instrument_id=current.instrument_id,
    )
    proposed_payload["market"]["atr"] = "45000"
    proposed = plan_position_risk(proposed_payload)
    with pytest.raises(ValueError, match="approval"):
        validate_superseding_plan(current, proposed)
    validate_superseding_plan(current, proposed, user_approved_relaxation=True)
