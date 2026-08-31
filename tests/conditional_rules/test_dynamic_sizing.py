from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from orchestration.conditional_rules import (
    ConditionalRuleSpec,
    ExecutionGuardInput,
    RuleState,
    guard_rule_execution,
    validate_rule_spec,
)


NOW = datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)


def _spec(*, side: str, sizing: dict) -> ConditionalRuleSpec:
    spec = ConditionalRuleSpec.model_validate(
        {
            "schema_version": "conditional-trade-rule.v1",
            "authority": {
                "user_id": "10000000-0000-0000-0000-000000000001",
                "fund_id": "20000000-0000-0000-0000-000000000001",
                "book_id": "30000000-0000-0000-0000-000000000001",
            },
            "instrument_id": "40000000-0000-0000-0000-000000000001",
            "symbol": "005930",
            "condition": {
                "type": "COMPARISON",
                "operator": "GT",
                "left": {"type": "MARKET", "field": "LAST_PRICE"},
                "right": {"type": "LITERAL", "value": "1", "unit": "PRICE"},
            },
            "action": {"side": side, "sizing": sizing},
            "evaluation": {"clock": "QUOTE"},
            "expires_at": (NOW + timedelta(days=1)).isoformat(),
            "raw_instruction_sha256": "0" * 64,
        }
    )
    return validate_rule_spec(spec)


def _snapshot(**changes) -> ExecutionGuardInput:
    values = {
        "now": NOW,
        "rule_state": RuleState.ACTIVE,
        "evaluated_rule_version": 1,
        "active_rule_version": 1,
        "membership_active": True,
        "fund_active": True,
        "book_active": True,
        "market_session_available": True,
        "market_open": True,
        "data_complete": True,
        "quote_fresh": True,
        "current_price": Decimal("1000"),
        "available_cash": Decimal("20000000"),
        "portfolio_nav": Decimal("1000000"),
        "position_quantity": Decimal("300"),
        "sellable_quantity": Decimal("300"),
        "lot_size": Decimal("1"),
    }
    values.update(changes)
    return ExecutionGuardInput(**values)


def test_target_position_weight_sells_only_the_whole_share_excess() -> None:
    spec = _spec(
        side="SELL",
        sizing={"type": "TARGET_POSITION_WEIGHT", "value": "0.20"},
    )

    decision = guard_rule_execution(spec, _snapshot())

    assert decision.allowed is True
    assert decision.code == "READY_FOR_PAPER_DIRECTIVE"
    assert decision.quantity == Decimal("100")


def test_target_position_weight_fails_closed_without_nav_or_excess() -> None:
    spec = _spec(
        side="SELL",
        sizing={"type": "TARGET_POSITION_WEIGHT", "value": "0.20"},
    )

    missing = guard_rule_execution(spec, _snapshot(portfolio_nav=None))
    at_target = guard_rule_execution(
        spec,
        _snapshot(position_quantity=Decimal("200"), sellable_quantity=Decimal("200")),
    )

    assert (missing.allowed, missing.code) == (False, "PORTFOLIO_NAV_UNAVAILABLE")
    assert (at_target.allowed, at_target.code) == (False, "TARGET_WEIGHT_NOT_EXCEEDED")


def test_available_cash_percent_uses_the_lower_of_ratio_and_krw_cap() -> None:
    spec = _spec(
        side="BUY",
        sizing={
            "type": "AVAILABLE_CASH_PERCENT_CAPPED",
            "value": "0.10",
            "cap_krw": "1000000",
        },
    )

    capped = guard_rule_execution(
        spec,
        _snapshot(current_price=Decimal("70000"), position_quantity=Decimal("0"), sellable_quantity=Decimal("0")),
    )
    ratio_limited = guard_rule_execution(
        spec,
        _snapshot(
            current_price=Decimal("70000"),
            available_cash=Decimal("5000000"),
            position_quantity=Decimal("0"),
            sellable_quantity=Decimal("0"),
        ),
    )

    assert capped.quantity == Decimal("14")
    assert ratio_limited.quantity == Decimal("7")


def test_dynamic_sizing_contract_rejects_wrong_side_or_missing_cap() -> None:
    with pytest.raises(ValueError, match="BUY supports"):
        _spec(
            side="BUY",
            sizing={"type": "TARGET_POSITION_WEIGHT", "value": "0.20"},
        )
    with pytest.raises(ValueError, match="BUY only"):
        _spec(
            side="SELL",
            sizing={
                "type": "AVAILABLE_CASH_PERCENT_CAPPED",
                "value": "0.10",
                "cap_krw": "1000000",
            },
        )
    with pytest.raises(ValueError, match="cap_krw"):
        _spec(
            side="BUY",
            sizing={"type": "AVAILABLE_CASH_PERCENT_CAPPED", "value": "0.10"},
        )
