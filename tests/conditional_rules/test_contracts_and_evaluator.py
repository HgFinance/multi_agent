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


def test_impossible_same_scalar_conjunction_is_rejected() -> None:
    """I07: a clear but impossible condition must never become ACTIVE."""

    impossible = rule(
        {
            "type": "LOGICAL",
            "operator": "AND",
            "children": [
                {
                    "type": "COMPARISON",
                    "operator": "LTE",
                    "left": {"type": "MARKET", "field": "LAST_PRICE"},
                    "right": literal("70000", "PRICE"),
                },
                {
                    "type": "COMPARISON",
                    "operator": "GTE",
                    "left": {"type": "MARKET", "field": "LAST_PRICE"},
                    "right": literal("80000", "PRICE"),
                },
            ],
        },
        evaluation={"clock": "QUOTE"},
    )

    with pytest.raises(RuleSemanticError) as rejected:
        validate_rule_spec(impossible)

    assert rejected.value.code == "CONTRADICTORY_CONDITION"


def test_boundary_touch_and_or_branch_are_not_false_positive_contradictions() -> None:
    exact_boundary = rule(
        {
            "type": "LOGICAL",
            "operator": "AND",
            "children": [
                {
                    "type": "COMPARISON",
                    "operator": "GTE",
                    "left": {"type": "MARKET", "field": "LAST_PRICE"},
                    "right": literal("70000", "PRICE"),
                },
                {
                    "type": "COMPARISON",
                    "operator": "LTE",
                    "left": {"type": "MARKET", "field": "LAST_PRICE"},
                    "right": literal("70000", "PRICE"),
                },
            ],
        },
        evaluation={"clock": "QUOTE"},
    )
    alternative = rule(
        {
            "type": "LOGICAL",
            "operator": "OR",
            "children": [
                {
                    "type": "LOGICAL",
                    "operator": "AND",
                    "children": [
                        {
                            "type": "COMPARISON",
                            "operator": "LTE",
                            "left": {"type": "MARKET", "field": "LAST_PRICE"},
                            "right": literal("70000", "PRICE"),
                        },
                        {
                            "type": "COMPARISON",
                            "operator": "GTE",
                            "left": {"type": "MARKET", "field": "LAST_PRICE"},
                            "right": literal("80000", "PRICE"),
                        },
                    ],
                },
                {
                    "type": "COMPARISON",
                    "operator": "EQ",
                    "left": {"type": "MARKET", "field": "LAST_PRICE"},
                    "right": literal("75000", "PRICE"),
                },
            ],
        },
        evaluation={"clock": "QUOTE"},
    )

    assert validate_rule_spec(exact_boundary) is exact_boundary
    assert validate_rule_spec(alternative) is alternative


def test_explicit_limit_action_preserves_exact_krw_price() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "GTE",
            "left": {"type": "MARKET", "field": "LAST_PRICE"},
            "right": literal("299500", "PRICE"),
        },
        action={
            "side": "SELL",
            "sizing": {"type": "FIXED_SHARES", "value": "1"},
            "order_type": "LIMIT",
            "limit_price": "299500",
        },
        evaluation={"clock": "QUOTE"},
    )

    assert spec.action.order_type == "LIMIT"
    assert spec.action.limit_price == Decimal("299500")
    assert validate_rule_spec(spec) is spec

    with pytest.raises(ValueError, match="MARKET must not include limit_price"):
        rule(
            {
                "type": "COMPARISON",
                "operator": "GTE",
                "left": {"type": "MARKET", "field": "LAST_PRICE"},
                "right": literal("299500", "PRICE"),
            },
            action={
                "side": "SELL",
                "sizing": {"type": "FIXED_SHARES", "value": "1"},
                "order_type": "MARKET",
                "limit_price": "299500",
            },
            evaluation={"clock": "QUOTE"},
        )


def test_trailing_stop_is_a_quote_only_existing_position_sell_rule() -> None:
    spec = rule(
        {
            "type": "TRAILING_STOP",
            "parameters": {"drawdown": "0.03", "activation_return": "0.02"},
        },
        action={"side": "SELL", "sizing": {"type": "ALL"}},
        evaluation={"clock": "QUOTE"},
    )

    assert validate_rule_spec(spec) is spec

    with pytest.raises(RuleSemanticError) as buy_rejected:
        validate_rule_spec(
            rule(
                {"type": "TRAILING_STOP", "parameters": {"DRAWDOWN": "0.03"}},
                evaluation={"clock": "QUOTE"},
            )
        )
    assert buy_rejected.value.code == "TRAILING_STOP_SELL_ONLY"

    with pytest.raises(RuleSemanticError) as bar_rejected:
        validate_rule_spec(
            rule(
                {"type": "TRAILING_STOP", "parameters": {"DRAWDOWN": "0.03"}},
                action={"side": "SELL", "sizing": {"type": "ALL"}},
            )
        )
    assert bar_rejected.value.code == "TRAILING_STOP_REQUIRES_QUOTE"


def test_trailing_stop_rejects_unsafe_parameters_and_boolean_composition() -> None:
    with pytest.raises(RuleSemanticError) as invalid_parameter:
        validate_rule_spec(
            rule(
                {"type": "TRAILING_STOP", "parameters": {"DRAWDOWN": "1"}},
                action={"side": "SELL", "sizing": {"type": "ALL"}},
                evaluation={"clock": "QUOTE"},
            )
        )
    assert invalid_parameter.value.code == "INVALID_TRAILING_STOP_PARAMETER"

    with pytest.raises(RuleSemanticError) as composed:
        validate_rule_spec(
            rule(
                {
                    "type": "LOGICAL",
                    "operator": "AND",
                    "children": [
                        {
                            "type": "TRAILING_STOP",
                            "parameters": {"DRAWDOWN": "0.03"},
                        },
                        {
                            "type": "COMPARISON",
                            "operator": "GT",
                            "left": {"type": "MARKET", "field": "LAST_PRICE"},
                            "right": literal("100", "PRICE"),
                        },
                    ],
                },
                action={"side": "SELL", "sizing": {"type": "ALL"}},
                evaluation={"clock": "QUOTE"},
            )
        )
    assert composed.value.code == "TRAILING_STOP_COMPOSITION_UNSUPPORTED"


def test_trailing_stop_expected_position_quantity_must_be_a_positive_integer() -> None:
    valid = rule(
        {"type": "TRAILING_STOP", "parameters": {
            "DRAWDOWN": "0.01",
            "EXPECTED_POSITION_QUANTITY": "5",
        }},
        action={"side": "SELL", "sizing": {"type": "FIXED_SHARES", "value": "5"}},
        evaluation={"clock": "QUOTE"},
    )

    assert validate_rule_spec(valid) is valid
    with pytest.raises(RuleSemanticError) as rejected:
        validate_rule_spec(
            rule(
                {"type": "TRAILING_STOP", "parameters": {
                    "DRAWDOWN": "0.01",
                    "EXPECTED_POSITION_QUANTITY": "5.5",
                }},
                action={"side": "SELL", "sizing": {"type": "FIXED_SHARES", "value": "5"}},
                evaluation={"clock": "QUOTE"},
            )
        )

    assert rejected.value.code == "INVALID_TRAILING_STOP_PARAMETER"



def test_matching_market_unit_hint_is_removed_before_strict_shape_check() -> None:
    node = ExpressionNode.model_validate(
        {"type": "MARKET", "field": "LAST_PRICE", "unit": "PRICE"}
    )

    assert node.model_dump(mode="json", exclude_none=True) == {
        "type": "MARKET",
        "field": "LAST_PRICE",
    }


def test_mismatched_market_unit_hint_still_fails_closed() -> None:
    with pytest.raises(ValueError, match="MARKET node has unexpected fields"):
        ExpressionNode.model_validate(
            {"type": "MARKET", "field": "LAST_PRICE", "unit": "KRW"}
        )



def test_evaluation_policy_defaults_to_thirty_second_quote_freshness() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "GT",
            "left": {"type": "MARKET", "field": "LAST_PRICE"},
            "right": literal("1", "PRICE"),
        },
        evaluation={"clock": "QUOTE"},
    )

    assert spec.evaluation.max_data_age_seconds == 30


def test_time_condition_uses_authoritative_observation_timestamp() -> None:
    trigger_at = NOW + timedelta(minutes=4)
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "GTE",
            "left": {
                "type": "TIME",
                "field": "OBSERVED_AT_EPOCH_SECONDS",
            },
            "right": literal(str(int(trigger_at.timestamp()))),
        },
        evaluation={"clock": "QUOTE"},
    )

    assert validate_rule_spec(spec) is spec
    before = EvaluationFrame(
        market={"LAST_PRICE": Decimal("100")},
        portfolio={},
        indicators={},
        observed_at=trigger_at - timedelta(seconds=1),
    )
    at_trigger = EvaluationFrame(
        market=before.market,
        portfolio=before.portfolio,
        indicators=before.indicators,
        observed_at=trigger_at,
    )

    assert evaluate_condition(spec, EvaluationContext(current=before)) is False
    assert evaluate_condition(spec, EvaluationContext(current=at_trigger)) is True


def test_time_condition_rejects_unknown_clock_field() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "GTE",
            "left": {"type": "TIME", "field": "WALL_CLOCK_NOW"},
            "right": literal(str(int(NOW.timestamp()))),
        },
        evaluation={"clock": "QUOTE"},
    )

    with pytest.raises(RuleSemanticError) as raised:
        validate_rule_spec(spec)
    assert raised.value.code == "UNSUPPORTED_TIME_FIELD"


def test_kst_time_window_uses_authoritative_observation_clock() -> None:
    spec = rule(
        {
            "type": "LOGICAL",
            "operator": "AND",
            "children": [
                {
                    "type": "COMPARISON",
                    "operator": "GTE",
                    "left": {"type": "TIME", "field": "KST_SECONDS_SINCE_MIDNIGHT"},
                    "right": literal("36000"),
                },
                {
                    "type": "COMPARISON",
                    "operator": "LTE",
                    "left": {"type": "TIME", "field": "KST_SECONDS_SINCE_MIDNIGHT"},
                    "right": literal("52200"),
                },
            ],
        },
        evaluation={"clock": "QUOTE"},
    )
    assert validate_rule_spec(spec) is spec
    frame = EvaluationFrame(
        market={"LAST_PRICE": Decimal("100")},
        portfolio={},
        indicators={},
        # 01:00 UTC is 10:00 KST.
        observed_at=datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc),
    )
    after_window = EvaluationFrame(
        market=frame.market,
        portfolio=frame.portfolio,
        indicators=frame.indicators,
        observed_at=datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc),
    )

    assert evaluate_condition(spec, EvaluationContext(current=frame)) is True
    assert evaluate_condition(spec, EvaluationContext(current=after_window)) is False


def test_kst_time_window_rejects_equality_and_non_time_range_shapes() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "EQ",
            "left": {"type": "TIME", "field": "KST_SECONDS_SINCE_MIDNIGHT"},
            "right": literal("36000"),
        },
        evaluation={"clock": "QUOTE"},
    )

    with pytest.raises(RuleSemanticError) as raised:
        validate_rule_spec(spec)
    assert raised.value.code == "TIME_WINDOW_OPERATOR_UNSUPPORTED"


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


def test_cross_rejects_mixed_timeframes_and_requires_logical_confirmation() -> None:
    spec = rule(
        {
            "type": "CROSS",
            "operator": "ABOVE",
            "left": {
                "type": "INDICATOR",
                "name": "SMA",
                "timeframe": "3M",
                "parameters": {"PERIOD": 5},
            },
            "right": {
                "type": "INDICATOR",
                "name": "SMA",
                "timeframe": "15M",
                "parameters": {"PERIOD": 20},
            },
        },
        evaluation={"clock": "BAR_CLOSE", "primary_timeframe": "3M"},
    )

    with pytest.raises(RuleSemanticError) as raised:
        validate_rule_spec(spec)
    assert raised.value.code == "CROSS_TIMEFRAME_MISMATCH"


def test_primary_timeframe_cannot_be_slower_than_an_explicit_indicator() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "LT",
            "left": {
                "type": "INDICATOR",
                "name": "RSI",
                "timeframe": "3M",
            },
            "right": literal("70"),
        },
        evaluation={"clock": "BAR_CLOSE", "primary_timeframe": "5M"},
    )

    with pytest.raises(RuleSemanticError) as raised:
        validate_rule_spec(spec)
    assert raised.value.code == "PRIMARY_TIMEFRAME_TOO_SLOW"


def test_intraday_warmup_that_exceeds_chart_continuation_limit_is_rejected() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "GT",
            "left": {
                "type": "INDICATOR",
                "name": "SMA",
                "timeframe": "1H",
                "parameters": {"PERIOD": 100},
            },
            "right": literal("1", "PRICE"),
        },
        evaluation={"clock": "BAR_CLOSE", "primary_timeframe": "1H"},
    )

    with pytest.raises(RuleSemanticError) as raised:
        validate_rule_spec(spec)
    assert raised.value.code == "INDICATOR_HISTORY_UNAVAILABLE"


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


def test_price_state_and_cross_below_have_distinct_runtime_semantics() -> None:
    """A01/A02: an already-low price satisfies state, never a new crossing."""

    state = rule(
        {
            "type": "COMPARISON",
            "operator": "LTE",
            "left": {"type": "MARKET", "field": "LAST_PRICE"},
            "right": literal("70000", "PRICE"),
        },
        evaluation={"clock": "QUOTE"},
    )
    crossing = rule(
        {
            "type": "CROSS",
            "operator": "BELOW",
            "left": {"type": "MARKET", "field": "CLOSE"},
            "right": literal("70000", "PRICE"),
        },
        evaluation={"clock": "BAR_CLOSE", "primary_timeframe": "5M"},
    )
    below_now = EvaluationFrame(
        {"LAST_PRICE": Decimal("69000"), "CLOSE": Decimal("69000")},
        {},
        {},
        NOW,
    )
    below_before = EvaluationFrame(
        {"CLOSE": Decimal("68000")}, {}, {}, NOW - timedelta(minutes=5)
    )
    above_before = EvaluationFrame(
        {"CLOSE": Decimal("71000")}, {}, {}, NOW - timedelta(minutes=5)
    )

    assert state.condition.type.value == "COMPARISON"
    assert crossing.condition.type.value == "CROSS"
    assert evaluate_condition(state, EvaluationContext(below_now)) is True
    assert evaluate_condition(
        crossing, EvaluationContext(below_now, below_before)
    ) is False
    assert evaluate_condition(
        crossing, EvaluationContext(below_now, above_before)
    ) is True


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


def test_krw_notional_sizing_is_price_capped_and_lot_floored_at_trigger_time() -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "GT",
            "left": {"type": "MARKET", "field": "LAST_PRICE"},
            "right": literal("100000", "PRICE"),
        },
        action={
            "side": "BUY",
            "sizing": {"type": "NOTIONAL_KRW", "value": "1000000"},
        },
        evaluation={"clock": "QUOTE"},
    )

    allowed = guard_rule_execution(
        spec,
        guard_input(current_price="123000", lot_size="2"),
    )
    insufficient_cash = guard_rule_execution(
        spec,
        guard_input(
            current_price="123000",
            lot_size="2",
            available_cash="900000",
        ),
    )

    # 1,000,000 / 123,000 = 8.13...; KRX quantity must be a 2-share lot.
    assert allowed.allowed is True
    assert allowed.quantity == Decimal("8")
    assert allowed.quantity * Decimal("123000") <= Decimal("1000000")
    assert insufficient_cash.code == "INSUFFICIENT_CASH"


def test_krw_notional_requires_whole_krw_market_order() -> None:
    condition = {
        "type": "COMPARISON",
        "operator": "GT",
        "left": {"type": "MARKET", "field": "LAST_PRICE"},
        "right": literal("1", "PRICE"),
    }
    with pytest.raises(ValueError, match="NOTIONAL_KRW must be an integer"):
        rule(
            condition,
            action={
                "side": "BUY",
                "sizing": {"type": "NOTIONAL_KRW", "value": "1000000.5"},
            },
            evaluation={"clock": "QUOTE"},
        )
    with pytest.raises(ValueError, match="NOTIONAL_KRW supports MARKET only"):
        rule(
            condition,
            action={
                "side": "BUY",
                "sizing": {"type": "NOTIONAL_KRW", "value": "1000000"},
                "order_type": "LIMIT",
                "limit_price": "100000",
            },
            evaluation={"clock": "QUOTE"},
        )


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


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    (
        ({"rule_state": "PAUSED"}, "RULE_NOT_ACTIVE"),
        ({"evaluated_rule_version": 1, "active_rule_version": 2}, "RULE_VERSION_CHANGED"),
        ({"trigger_already_claimed": True}, "DUPLICATE_TRIGGER"),
        ({"now": NOW + timedelta(days=31)}, "RULE_EXPIRED"),
        ({"membership_active": False}, "MEMBERSHIP_INACTIVE"),
        ({"fund_active": False}, "TRADING_SCOPE_INACTIVE"),
        ({"book_active": False}, "TRADING_SCOPE_INACTIVE"),
        ({"market_session_available": False}, "MARKET_SESSION_UNAVAILABLE"),
        ({"market_open": False}, "MARKET_CLOSED_NO_ORDER"),
        ({"data_complete": False}, "MARKET_DATA_INCOMPLETE"),
        ({"quote_fresh": False}, "MARKET_QUOTE_STALE"),
    ),
)
def test_p0_execution_guard_failure_matrix_never_allows_an_order(
    changes: dict, expected_code: str
) -> None:
    spec = rule(
        {
            "type": "COMPARISON",
            "operator": "GT",
            "left": {"type": "MARKET", "field": "LAST_PRICE"},
            "right": literal("100000", "PRICE"),
        },
        evaluation={"clock": "QUOTE"},
    )

    decision = guard_rule_execution(spec, guard_input(**changes))

    assert decision.allowed is False
    assert decision.quantity is None
    assert decision.code == expected_code


def test_p0_user_text_cannot_add_a_guard_override_field() -> None:
    payload = guard_input().model_dump(mode="json")
    payload["ignore_risk_and_audit"] = True

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        ExecutionGuardInput.model_validate(payload)


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
