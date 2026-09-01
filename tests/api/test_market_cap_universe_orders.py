"""The order path reads the ranking snapshot, and refuses when it cannot."""

from __future__ import annotations

import time

import pytest

from apps.api import ls_account_stream as ls
from apps.api.ceo import CeoAsk, _expand_dynamic_universe_request
from orchestration.user_order_language import deterministic_order_candidate


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
QUERY = "현재 기준 시가총액 상위 10종목 300만원씩 매수해줘"


@pytest.fixture(autouse=True)
def _clear_snapshot():
    original = ls._market_cap_universe
    ls._market_cap_universe = None
    yield
    ls._market_cap_universe = original


def _ask(query: str = QUERY) -> CeoAsk:
    return CeoAsk(query=query, request_id="rq-universe-0001")


def test_a_fresh_snapshot_turns_the_ranking_into_an_explicit_basket() -> None:
    ls._market_cap_universe = (time.time(), "2026-09-01T05:30:00+00:00", TOP_ROWS)
    expanded = _expand_dynamic_universe_request(_ask())

    assert expanded.query != QUERY
    candidate = deterministic_order_candidate(expanded.query)
    assert candidate is not None
    assert candidate.action.value == "PLACE_BASKET"
    assert len(candidate.basket_instrument_mentions) == 10
    assert str(candidate.notional_krw) == "3000000"


@pytest.mark.parametrize(
    ("label", "snapshot"),
    (
        ("no snapshot at all", None),
        (
            "older than the staleness limit",
            (
                time.time() - ls.MARKET_CAP_UNIVERSE_MAX_AGE_SECONDS - 1,
                "stale",
                TOP_ROWS,
            ),
        ),
        ("fewer names than requested", (time.time(), "short", TOP_ROWS[:9])),
    ),
)
def test_the_sentence_is_left_alone_when_the_ranking_cannot_be_trusted(
    label: str, snapshot: object
) -> None:
    """Failing closed keeps a basket the user never approved from being admitted.

    An unexpanded sentence is refused downstream exactly as before; a silently
    shortened one would have been admitted as a different order (개발 원칙 9).
    """

    ls._market_cap_universe = snapshot
    assert _expand_dynamic_universe_request(_ask()).query == QUERY, label


def test_an_ordinary_question_is_never_rewritten() -> None:
    ls._market_cap_universe = (time.time(), "fresh", TOP_ROWS)
    ask = _ask("시가총액 상위 종목이 뭐야?")
    assert _expand_dynamic_universe_request(ask).query == ask.query


def test_the_widget_row_count_and_the_order_path_limit_are_separate() -> None:
    """One constant served both, so an order asking for ten was cut to five."""

    assert ls.MARKET_RANKING_DEFAULT_ROWS == 5
    assert ls.MARKET_RANKING_MAX_ROWS >= 100

    payload = {
        "t1444OutBlock1": [
            {"shcode": row["symbol"], "hname": row["name"], "price": "1000"}
            for row in TOP_ROWS
        ]
    }
    assert len(ls.normalize_market_ranking(payload, "market_cap")["rows"]) == 10
    assert (
        len(ls.normalize_market_ranking(payload, "market_cap", limit=3)["rows"]) == 3
    )
