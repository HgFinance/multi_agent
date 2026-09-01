"""The ranking phrase becomes an ordinary explicit basket, or nothing at all."""

from __future__ import annotations

import pytest

from orchestration.dynamic_universe_orders import (
    MAX_UNIVERSE_MEMBERS,
    expand_to_basket_instruction,
    parse_dynamic_universe_order,
    universe_members,
)
from orchestration.user_order_language import (
    deterministic_order_candidate,
    verify_order_candidate,
)


TOP_ROWS = [
    {"symbol": "005930", "name": "삼성전자"},
    {"symbol": "000660", "name": "SK하이닉스"},
    {"symbol": "005935", "name": "삼성전자우"},
    {"symbol": "402340", "name": "SK스퀘어"},
    {"symbol": "009150", "name": "삼성전기"},
    {"symbol": "207940", "name": "삼성바이오로직스"},
    {"symbol": "373220", "name": "LG에너지솔루션"},
    {"symbol": "012450", "name": "한화에어로스페이스"},
    {"symbol": "105560", "name": "KB금융"},
    {"symbol": "329180", "name": "HD현대중공업"},
]


def test_the_rejected_sentence_becomes_a_ten_leg_basket() -> None:
    """The 2026-09-01 rejection: UNSUPPORTED_DYNAMIC_UNIVERSE.

    The sentence carries no condition - "현재 기준" is now - so nothing about it
    needs a rule that re-picks its universe later. Reading the ranking once, at
    admission, turns it into the same sentence a user could have typed by hand.
    """

    plan = parse_dynamic_universe_order("현재 기준 시가총액 상위 10종목 300만원씩 매수해줘")
    assert plan is not None
    assert (plan.top_n, plan.notional_krw) == (10, 3_000_000)

    sentence = expand_to_basket_instruction(plan, TOP_ROWS)
    assert sentence is not None
    candidate = deterministic_order_candidate(sentence)
    assert candidate is not None
    assert candidate.action.value == "PLACE_BASKET"
    assert list(candidate.basket_instrument_mentions) == [
        row["symbol"] for row in TOP_ROWS
    ]
    verified = verify_order_candidate(sentence, candidate)
    assert verified.decision.value == "EXECUTE"
    assert len(verified.payload.orders) == 10
    assert {str(item.notional_krw) for item in verified.payload.orders} == {"3000000"}
    assert {item.side.value for item in verified.payload.orders} == {"BUY"}


@pytest.mark.parametrize(
    "sentence",
    (
        # A short ranking must never become a smaller basket.
        "시가총액 상위 10종목 300만원씩 매수",
    ),
)
def test_a_ranking_shorter_than_the_request_expands_to_nothing(sentence: str) -> None:
    plan = parse_dynamic_universe_order(sentence)
    assert plan is not None
    assert universe_members(plan, TOP_ROWS[:9]) == ()
    assert expand_to_basket_instruction(plan, TOP_ROWS[:9]) is None


def test_malformed_rows_are_skipped_rather_than_trusted() -> None:
    plan = parse_dynamic_universe_order("시총 상위 2종목 100만원씩 매수")
    assert plan is not None
    rows = [
        {"symbol": "bad", "name": "이상한종목"},
        {"symbol": "005930", "name": ""},
        {"symbol": "005930", "name": "삼성전자"},
        {"symbol": "005930", "name": "삼성전자"},
        {"symbol": "000660", "name": "SK하이닉스"},
    ]
    assert universe_members(plan, rows) == (
        ("005930", "삼성전자"),
        ("000660", "SK하이닉스"),
    )


@pytest.mark.parametrize(
    "sentence",
    (
        # No metric named: "상위" alone does not say by what.
        "상위 10종목 300만원씩 매수",
        # A share count is a different allocation entirely.
        "시가총액 상위 10종목 3주씩 매수",
        # Selling a ranking is not the same request and is not expanded.
        "시가총액 상위 10종목 300만원씩 매도",
        # A limit price is not an equal-KRW market allocation.
        "시가총액 상위 10종목 지정가 300만원씩 매수",
        # Two counts read two ways; neither is chosen here.
        "시가총액 상위 10종목 5종목 300만원씩 매수",
        # One member is an ordinary order, not a basket.
        "시가총액 상위 1종목 300만원씩 매수",
    ),
)
def test_sentences_outside_the_grammar_are_left_alone(sentence: str) -> None:
    assert parse_dynamic_universe_order(sentence) is None


def test_member_count_beyond_the_basket_limit_is_refused() -> None:
    """PLACE_BASKET carries 20 legs; a 21-name request is not silently trimmed."""

    assert parse_dynamic_universe_order(
        f"시가총액 상위 {MAX_UNIVERSE_MEMBERS + 1}종목 100만원씩 매수"
    ) is None
    assert (
        parse_dynamic_universe_order(
            f"시가총액 상위 {MAX_UNIVERSE_MEMBERS}종목 100만원씩 매수"
        )
        is not None
    )


def test_the_member_count_is_not_read_as_a_share_count() -> None:
    """"10개 종목" names the membership, not a quantity per member."""

    plan = parse_dynamic_universe_order("시가총액 상위 10개 종목 300만원씩 매수")
    assert plan is not None
    assert plan.top_n == 10
