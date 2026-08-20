from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from orchestration.conditional_rules import (
    Candle,
    ConditionalRuleSpec,
    EvaluationContext,
    EvaluationError,
    EvaluationFrame,
    ExecutionGuardInput,
    ExpressionNode,
    IndicatorEngine,
    RuleSemanticError,
    RuleState,
    evaluate_condition,
    guard_rule_execution,
    validate_rule_spec,
)
from orchestration.conditional_rules.evaluator import indicator_key


NOW = datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)


def literal(value: str, unit: str = "NUMBER") -> dict:
    return {"type": "LITERAL", "value": value, "unit": unit}


def rule(
    condition: dict,
    *,
    action: dict | None = None,
    evaluation: dict | None = None,
) -> ConditionalRuleSpec:
    return ConditionalRuleSpec.model_validate(
        {
            "schema_version": "conditional-trade-rule.v1",
            "authority": {
                "user_id": "10000000-0000-0000-0000-000000000001",
                "fund_id": "20000000-0000-0000-0000-000000000001",
                "book_id": "30000000-0000-0000-0000-000000000001",
            },
            "instrument_id": "40000000-0000-0000-0000-000000000001",
            "symbol": "005930",
            "condition": condition,
            "action": action
            or {
                "side": "BUY",
                "sizing": {"type": "FIXED_SHARES", "value": "2"},
            },
            "evaluation": evaluation
            or {"clock": "BAR_CLOSE", "primary_timeframe": "5M"},
            "execution_mode": "PAPER",
            "repeat_policy": "ONCE",
            "expires_at": (NOW + timedelta(days=30)).isoformat(),
            "raw_instruction_sha256": "0" * 64,
        }
    )


def test_rsi_rule_is_semantically_valid() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "LTE",
            "left": {
                "type": "INDICATOR",
                "name": "RSI",
                "timeframe": "5M",
                "parameters": {"period": 14},
            },
            "right": literal("30"),
        }
    )

    assert validate_rule_spec(spec) is spec


def test_quote_clock_rejects_indicator_and_non_last_price_fields() -> None:
    with pytest.raises(RuleSemanticError, match="completed bars"):
        validate_rule_spec(
            rule(
                {
                    "type": "COMPARISON",
                    "operator": "GT",
                    "left": {
                        "type": "INDICATOR",
                        "name": "SMA",
                        "timeframe": "5M",
                    },
                    "right": literal("70000", "PRICE"),
                },
                evaluation={"clock": "QUOTE"},
            )
        )


def test_unit_mismatch_is_rejected_before_evaluation() -> None:
    with pytest.raises(RuleSemanticError) as raised:
        validate_rule_spec(
            rule(
                {
                    "type": "COMPARISON",
                    "operator": "GTE",
                    "left": {"type": "MARKET", "field": "CLOSE"},
                    "right": literal("20", "SHARES"),
                }
            )
        )
    assert raised.value.code == "UNIT_MISMATCH"


def test_position_profit_rule_uses_canonical_portfolio_values() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "GTE",
            "left": {
                "type": "ARITHMETIC",
                "operator": "SUB",
                "left": {
                    "type": "ARITHMETIC",
                    "operator": "DIV",
                    "left": {"type": "MARKET", "field": "LAST_PRICE"},
                    "right": {"type": "PORTFOLIO", "field": "AVG_ENTRY_PRICE"},
                },
                "right": literal("1", "RATIO"),
            },
            "right": literal("0.05", "RATIO"),
        },
        action={
            "side": "SELL",
            "sizing": {"type": "POSITION_PERCENT", "value": "0.20"},
        },
        evaluation={"clock": "QUOTE"},
    )
    validate_rule_spec(spec)
    frame = EvaluationFrame(
        market={"LAST_PRICE": Decimal("105000")},
        portfolio={"AVG_ENTRY_PRICE": Decimal("100000")},
        indicators={},
        observed_at=NOW,
    )

    assert evaluate_condition(spec, EvaluationContext(current=frame)) is True


def test_indicator_engine_uses_completed_bars_only() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "GT",
            "left": {
                "type": "INDICATOR",
                "name": "SMA",
                "timeframe": "5M",
                "parameters": {"period": 3},
            },
            "right": literal("12", "PRICE"),
        }
    )
    candles = [
        Candle(
            bucket_time=NOW + timedelta(minutes=5 * index),
            open=Decimal(value),
            high=Decimal(value),
            low=Decimal(value),
            close=Decimal(value),
            volume=Decimal("100"),
        )
        for index, value in enumerate(("10", "11", "12", "14"))
    ]
    candles.append(
        Candle(
            bucket_time=NOW + timedelta(minutes=25),
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("100"),
            is_final=False,
        )
    )

    context = IndicatorEngine().build_context(
        spec, bars={spec.evaluation.primary_timeframe: candles}, portfolio={}
    )

    assert evaluate_condition(spec, context) is True


def test_non_cross_indicator_does_not_require_an_extra_previous_bar() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "GT",
            "left": {
                "type": "INDICATOR",
                "name": "SMA",
                "timeframe": "5M",
                "parameters": {"period": 3},
            },
            "right": literal("1", "PRICE"),
        }
    )
    candles = [
        Candle(
            bucket_time=NOW + timedelta(minutes=5 * index),
            open=Decimal(value),
            high=Decimal(value),
            low=Decimal(value),
            close=Decimal(value),
            volume=Decimal("100"),
        )
        for index, value in enumerate(("10", "11", "12"))
    ]

    context = IndicatorEngine().build_context(
        spec, bars={spec.evaluation.primary_timeframe: candles}, portfolio={}
    )

    assert context.previous is None
    assert evaluate_condition(spec, context) is True


def test_cross_rejects_non_durable_previous_portfolio_values() -> None:
    spec = rule(
        {
            "type": "CROSS",
            "operator": "ABOVE",
            "left": {"type": "PORTFOLIO", "field": "PNL_PERCENT"},
            "right": literal("0.05", "RATIO"),
        }
    )

    with pytest.raises(RuleSemanticError) as raised:
        validate_rule_spec(spec)

    assert raised.value.code == "CROSS_PORTFOLIO_UNSUPPORTED"


def test_indicator_period_is_bounded_by_market_api_history_contract() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "GT",
            "left": {
                "type": "INDICATOR",
                "name": "SMA",
                "timeframe": "5M",
                "parameters": {"period": 501},
            },
            "right": literal("1", "PRICE"),
        }
    )

    with pytest.raises(RuleSemanticError) as raised:
        validate_rule_spec(spec)

    assert raised.value.code == "INDICATOR_PARAMETER_TOO_LARGE"


def test_cross_requires_previous_observation_and_is_edge_triggered() -> None:
    indicator = ExpressionNode.model_validate(
        {
            "type": "INDICATOR",
            "name": "SMA",
            "timeframe": "5M",
            "parameters": {"period": 2},
        }
    )
    spec = rule(
        {
            "type": "CROSS",
            "operator": "ABOVE",
            "left": indicator.model_dump(mode="json", exclude_none=True),
            "right": literal("10", "PRICE"),
        }
    )
    key = indicator_key(indicator)
    current = EvaluationFrame({}, {}, {key: Decimal("11")}, NOW)
    previous = EvaluationFrame({}, {}, {key: Decimal("10")}, NOW - timedelta(minutes=5))

    assert evaluate_condition(spec, EvaluationContext(current, previous)) is True
    with pytest.raises(EvaluationError) as raised:
        evaluate_condition(spec, EvaluationContext(current))
    assert raised.value.code == "PREVIOUS_FRAME_REQUIRED"


def guard_input(**changes) -> ExecutionGuardInput:
    data = {
        "now": NOW,
        "rule_state": "ACTIVE",
        "evaluated_rule_version": 1,
        "active_rule_version": 1,
        "membership_active": True,
        "fund_active": True,
        "book_active": True,
        "market_session_available": True,
        "market_open": True,
        "data_complete": True,
        "quote_fresh": True,
        "current_price": "105000",
        "available_cash": "10000000",
        "position_quantity": "103",
        "sellable_quantity": "103",
        "lot_size": "1",
    }
    data.update(changes)
    return ExecutionGuardInput.model_validate(data)


def test_position_percent_sizing_is_computed_at_trigger_time() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "GT",
            "left": {"type": "MARKET", "field": "LAST_PRICE"},
            "right": literal("100000", "PRICE"),
        },
        action={
            "side": "SELL",
            "sizing": {"type": "POSITION_PERCENT", "value": "0.20"},
        },
        evaluation={"clock": "QUOTE"},
    )

    decision = guard_rule_execution(spec, guard_input())

    assert decision.allowed is True
    assert decision.quantity == Decimal("20")


def test_market_closed_and_duplicate_trigger_fail_closed() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "GT",
            "left": {"type": "MARKET", "field": "LAST_PRICE"},
            "right": literal("100000", "PRICE"),
        },
        evaluation={"clock": "QUOTE"},
    )

    closed = guard_rule_execution(spec, guard_input(market_open=False))
    duplicate = guard_rule_execution(spec, guard_input(trigger_already_claimed=True))

    assert closed.code == "MARKET_CLOSED_NO_ORDER"
    assert "주문·체결·원장 반영" in closed.message
    assert duplicate.code == "DUPLICATE_TRIGGER"


def test_market_session_unavailable_is_not_reported_as_closed() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "GT",
            "left": {"type": "MARKET", "field": "LAST_PRICE"},
            "right": literal("100000", "PRICE"),
        },
        evaluation={"clock": "QUOTE"},
    )

    decision = guard_rule_execution(
        spec,
        guard_input(market_session_available=False, market_open=False),
    )

    assert decision.code == "MARKET_SESSION_UNAVAILABLE"


def test_boolean_comparison_is_rejected_before_runtime() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "EQ",
            "left": {
                "type": "COMPARISON",
                "operator": "GT",
                "left": {"type": "MARKET", "field": "LAST_PRICE"},
                "right": literal("100000", "PRICE"),
            },
            "right": {"type": "LITERAL", "value": True, "unit": "BOOL"},
        },
        evaluation={"clock": "QUOTE"},
    )

    with pytest.raises(RuleSemanticError) as raised:
        validate_rule_spec(spec)

    assert raised.value.code == "BOOLEAN_COMPARISON_UNSUPPORTED"


def test_buy_all_is_rejected_by_schema() -> None:
    with pytest.raises(ValueError, match="BUY supports FIXED_SHARES"):
        rule(
            {
                "type": "COMPARISON",
                "operator": "GT",
                "left": {"type": "MARKET", "field": "LAST_PRICE"},
                "right": literal("1", "PRICE"),
            },
            action={"side": "BUY", "sizing": {"type": "ALL"}},
            evaluation={"clock": "QUOTE"},
        )
