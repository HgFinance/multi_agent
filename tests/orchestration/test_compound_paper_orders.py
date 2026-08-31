from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from orchestration.compound_paper_orders import (
    build_compound_conditional_candidate,
    parse_analysis_then_conditional_paper_order,
    parse_compound_paper_order,
)
from orchestration.conditional_rules import (
    EvaluationContext,
    EvaluationFrame,
    evaluate_condition,
)


def test_exact_compound_market_buy_then_price_sell_is_structured() -> None:
    plan = parse_compound_paper_order(
        "삼성전자 5주 시장가로 매수해줘. 그리고 265000 넘으면 즉시 5개 매도해줘."
    )

    assert plan is not None
    assert plan.instrument_mention == "삼성전자"
    assert plan.immediate_quantity == 5
    assert plan.conditional_quantity == 5
    assert plan.trigger_price == Decimal("265000")
    assert plan.trigger_operator == "GT"

    candidate = build_compound_conditional_candidate(plan)
    assert candidate["condition"].right.unit.value == "PRICE"
    assert candidate["action"].sizing.type.value == "FIXED_SHARES"
    assert candidate["action"].sizing.value == Decimal("5")


def test_buy_then_entry_relative_sell_is_structured() -> None:
    """The shape rejected as MULTIPLE_COMMANDS on 2026-08-27.

    "…매수하고 …매도해줘" joins the two legs without "그리고", and the trigger
    is a percentage above the fill price rather than an absolute number.
    """

    plan = parse_compound_paper_order(
        "가온전선 1주 시장가 매수하고 매수가 대비 1% 상승하면 시장가로 매도해줘"
    )

    assert plan is not None
    assert plan.instrument_mention == "가온전선"
    assert plan.immediate_instruction == "가온전선 1주 시장가 매수"
    assert plan.immediate_quantity == 1
    # An omitted sell quantity means "what the first leg just bought".
    assert plan.conditional_quantity == 1
    assert plan.trigger_price is None
    assert plan.trigger_entry_percent == Decimal("1")
    assert plan.trigger_operator == "GTE"
    # The preview flags AMBIGUOUS_RETURN_BASELINE unless the generated
    # instruction names the baseline it measures from.
    assert "매수가" in plan.conditional_instruction

    candidate = build_compound_conditional_candidate(plan)
    threshold = candidate["condition"].right
    assert threshold.type.value == "ARITHMETIC"
    assert threshold.operator == "MUL"
    assert threshold.left.field == "AVG_ENTRY_PRICE"
    assert threshold.right.value == Decimal("1.01")
    assert candidate["action"].side.value == "SELL"
    assert candidate["action"].sizing.value == Decimal("1")


def test_buy_then_entry_relative_stop_loss_compares_downward() -> None:
    """A stop-loss is the same shape as a take-profit with the sign flipped."""

    plan = parse_compound_paper_order(
        "가온전선 1주 시장가 매수하고 매수가 대비 2% 하락하면 시장가로 매도해줘"
    )

    assert plan is not None
    assert plan.trigger_operator == "LTE"
    assert plan.trigger_entry_percent == Decimal("-2")
    assert "하락" in plan.conditional_instruction

    candidate = build_compound_conditional_candidate(plan)
    threshold = candidate["condition"].right
    assert threshold.left.field == "AVG_ENTRY_PRICE"
    assert threshold.right.value == Decimal("0.98")
    assert candidate["condition"].operator == "LTE"

    # "떨어지면" must reach the same rule as "하락하면".
    spoken = parse_compound_paper_order(
        "가온전선 1주 시장가 매수하고 매수가 대비 2% 이상 떨어지면 매도해줘"
    )
    assert spoken is not None
    assert spoken.trigger_operator == "LTE"
    assert spoken.trigger_entry_percent == Decimal("-2")


def test_buy_then_take_profit_and_stop_loss_is_one_atomic_exit_rule() -> None:
    plan = parse_compound_paper_order(
        "삼성전자 5주 시장가 매수하고 매수가 대비 3% 상승하면 매도하고 "
        "2% 하락하면 매도해줘"
    )

    assert plan is not None
    assert plan.is_entry_exit_bracket is True
    assert plan.entry_exit_percents == (Decimal("3"), Decimal("-2"))
    assert plan.conditional_quantity == 5
    # The second condition inherits the *explicitly named* first entry
    # baseline. Both exits are normalized so preview provenance remains clear.
    assert plan.conditional_instruction == (
        "삼성전자 매수가 대비 3% 이상 상승 시 5주 시장가 매도 또는 "
        "매수가 대비 2% 이상 하락 시 5주 시장가 매도"
    )

    candidate = build_compound_conditional_candidate(plan)
    condition = candidate["condition"]

    assert condition.type.value == "LOGICAL"
    assert condition.operator == "AND"
    position_guard, exit_condition = condition.children or ()
    assert position_guard.left.field == "POSITION_QUANTITY"
    assert position_guard.right.value == Decimal("5")
    assert exit_condition.operator == "OR"
    assert tuple(child.operator for child in exit_condition.children or ()) == ("GTE", "LTE")
    assert tuple(
        child.right.right.value for child in exit_condition.children or ()
    ) == (Decimal("1.03"), Decimal("0.98"))
    assert candidate["action"].sizing.value == Decimal("5")

    matching_position = EvaluationFrame(
        market={"LAST_PRICE": Decimal("103")},
        portfolio={"POSITION_QUANTITY": Decimal("5"), "AVG_ENTRY_PRICE": Decimal("100")},
        indicators={},
        observed_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    mixed_existing_position = EvaluationFrame(
        market={"LAST_PRICE": Decimal("103")},
        portfolio={"POSITION_QUANTITY": Decimal("10"), "AVG_ENTRY_PRICE": Decimal("95")},
        indicators={},
        observed_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    assert evaluate_condition(
        SimpleNamespace(condition=candidate["condition"]),
        EvaluationContext(current=matching_position),
    ) is True
    assert evaluate_condition(
        SimpleNamespace(condition=candidate["condition"]),
        EvaluationContext(current=mixed_existing_position),
    ) is False

    shorthand = parse_compound_paper_order(
        "삼성전자 5주 시장가 매수하고 매수가 대비 3% 익절 시 매도하고 "
        "2% 손절 시 매도"
    )
    assert shorthand is not None
    assert shorthand.entry_exit_percents == (Decimal("3"), Decimal("-2"))


def test_entry_exit_bracket_requires_one_up_and_one_down_with_full_exit_quantity() -> None:
    assert parse_compound_paper_order(
        "삼성전자 5주 시장가 매수하고 매수가 대비 3% 상승하면 매도하고 "
        "4% 상승하면 매도"
    ) is None
    assert parse_compound_paper_order(
        "삼성전자 5주 시장가 매수하고 매수가 대비 3% 상승하면 3주 매도하고 "
        "2% 하락하면 3주 매도"
    ) is None
    assert parse_compound_paper_order(
        "삼성전자 5주 시장가 매수하고 3% 상승하면 매도하고 2% 하락하면 매도"
    ) is None


def test_buy_then_entry_trailing_stop_is_fill_gated_and_position_bound() -> None:
    plan = parse_compound_paper_order(
        "하이닉스 5주 시장가 매수하고 매수가 대비 3% 수익 이후 "
        "고점 대비 1% 하락하면 매도해줘"
    )

    assert plan is not None
    assert plan.is_entry_trailing_stop is True
    assert plan.trigger_entry_percent == Decimal("3")
    assert plan.trailing_drawdown_percent == Decimal("1")
    assert plan.conditional_instruction == (
        "하이닉스 매수가 대비 3% 수익 이후 고점 대비 1% 하락 시 5주 시장가 매도"
    )

    candidate = build_compound_conditional_candidate(plan)
    condition = candidate["condition"]

    assert condition.type.value == "TRAILING_STOP"
    assert condition.parameters == {
        "DRAWDOWN": Decimal("0.01"),
        "ACTIVATION_RETURN": Decimal("0.03"),
        "EXPECTED_POSITION_QUANTITY": Decimal("5"),
    }
    assert candidate["action"].sizing.value == Decimal("5")

    assert parse_compound_paper_order(
        "하이닉스 5주 시장가 매수하고 매수가 대비 3% 수익 이후 "
        "고점 대비 1% 하락하면 3주 매도"
    ) is None


def test_compound_exit_can_explicitly_track_for_full_fill_lifetime() -> None:
    plan = parse_compound_paper_order(
        "하이닉스 5주 시장가 매수하고 매수가 대비 3% 수익 이후 "
        "고점 대비 1% 하락하면 매도해줘, 최대 5거래일 동안 추적"
    )

    assert plan is not None
    assert plan.is_entry_trailing_stop is True
    assert plan.exit_lifetime_trading_days == 5
    candidate = build_compound_conditional_candidate(plan)
    assert candidate["activation_lifetime_trading_days"] == 5
    assert "5거래일" not in plan.conditional_instruction

    next_session = parse_compound_paper_order(
        "하이닉스 5주 시장가 매수하고 매수가 대비 3% 상승하면 매도, 다음 거래일까지"
    )
    assert next_session is not None
    assert next_session.exit_lifetime_trading_days == 2

    # The grammar is deliberately bounded; a long unattended exit must be
    # expressed through a separately reviewed rule, not this entry fast lane.
    assert parse_compound_paper_order(
        "하이닉스 5주 시장가 매수하고 매수가 대비 3% 상승하면 매도, 21거래일 동안"
    ) is None


def test_entry_relative_compound_is_fail_closed() -> None:
    # A different instrument in the second leg is not a compound order.
    assert parse_compound_paper_order(
        "가온전선 1주 시장가 매수하고 현대차 2주 시장가 매도해줘"
    ) is None
    # Selling more than was bought would go net short.
    assert parse_compound_paper_order(
        "가온전선 1주 시장가 매수하고 매수가 대비 1% 상승하면 5주 시장가로 매도해줘"
    ) is None
    # An implausible move is left for a human to confirm.
    assert parse_compound_paper_order(
        "가온전선 1주 시장가 매수하고 매수가 대비 80% 상승하면 매도해줘"
    ) is None


def test_compound_requires_same_quantity_to_avoid_partial_semantics() -> None:
    assert (
        parse_compound_paper_order(
            "삼성전자 5주 시장가 매수 그리고 265000원 초과 시 3주 매도"
        )
        is None
    )


def test_compound_does_not_accept_unrelated_or_live_commands() -> None:
    assert (
        parse_compound_paper_order(
            "삼성전자 5주 시장가 매수 그리고 현대차 3주 시장가 매도"
        )
        is None
    )
    assert (
        parse_compound_paper_order(
            "삼성전자 5주 시장가로 매수해줘. 그리고 265000 넘으면 5개 실거래 매도해줘."
        )
        is None
    )


def test_research_then_conditional_is_separated_from_direct_trading_lane() -> None:
    plan = parse_analysis_then_conditional_paper_order(
        "<@1536991290842030130> research 분석 후 삼성전자 262,000원 초과 시 5주 매도 조건주문"
    )

    assert plan is not None
    assert "Research" in plan.analysis_instruction
    assert plan.conditional_instruction == (
        "삼성전자 262,000원 초과 시 5주 매도 조건주문"
    )


def test_research_then_conditional_parser_is_fail_closed() -> None:
    assert parse_analysis_then_conditional_paper_order(
        "research 분석 후 삼성전자 가격이 오르면 매도"
    ) is None
