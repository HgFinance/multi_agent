from decimal import Decimal

from orchestration.compound_paper_orders import (
    build_compound_conditional_candidate,
    parse_analysis_then_conditional_paper_order,
    parse_compound_paper_order,
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
