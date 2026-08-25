from orchestration.risk_plan_projection import format_position_risk_plan


def test_risk_projection_preserves_canonical_numbers_and_authority_boundary():
    plan = {
        "risk_plan_id": "risk-plan-1",
        "mandate_version_id": "mandate-v1",
        "state": "VALIDATED",
        "action": "PROPOSE",
        "regime": "CAUTION",
        "as_of": "2026-08-25T02:00:00Z",
        "quantity_cap": "3",
        "current_quantity": "2",
        "entry_reference": "1603000",
        "stop_price": "1540000",
        "take_profit_price": "1660000",
        "trailing_activation_price": "1630000",
        "trailing_distance": "30000",
        "position_risk_amount": "126000",
        "reward_risk_ratio": "1.25",
        "calculation_version": "dynamic-position-risk-planner.v1",
        "reason_codes": ["REGIME:CAUTION"],
        "expires_at": "2026-08-25T06:00:00Z",
        "review_triggers": ["REGIME_CHANGED"],
        "data_quality": "VALID",
    }
    rendered = format_position_risk_plan(plan)
    for exact in ("1603000", "1540000", "1660000", "126000", "3"):
        assert exact in rendered
    assert "주문 승인이 아닙니다" in rendered
