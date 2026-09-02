"""The order path reads the ranking snapshot, and refuses when it cannot."""

from __future__ import annotations

import asyncio
import sys
import time
from types import ModuleType

import pytest
from fastapi import HTTPException

from apps.api import ceo
from apps.api import ls_account_stream as ls
from apps.api.ceo import (
    _LS_ACCOUNT_STREAM_NAMES,
    CeoAsk,
    _expand_dynamic_universe_request,
    _live_ls_account_stream,
)
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
QUERY = (
    "현재 KRX 시가총액 상위 10개 종목을 각각 최대 300만원씩 "
    "PAPER 시장가로 매수해줘."
)


@pytest.fixture(autouse=True)
def _clear_snapshot():
    """Clear every ls_account_stream copy this interpreter has loaded.

    PYTHONPATH makes `ls_account_stream` and `apps.api.ls_account_stream`
    separate module objects; a test that seeded only one of them would pass
    while production read the other.
    """

    modules = [
        module
        for module in (
            sys.modules.get("ls_account_stream"),
            sys.modules.get("apps.api.ls_account_stream"),
        )
        if module is not None
    ]
    original = [(module, module._market_cap_universe) for module in modules]
    for module in modules:
        module._market_cap_universe = None
    yield
    for module, value in original:
        module._market_cap_universe = value


def _seed(snapshot: object) -> None:
    """Seed the copy the expansion actually reads, whichever one that is."""

    live = _live_ls_account_stream()
    assert live is not None
    live._market_cap_universe = snapshot


def _ask(query: str = QUERY) -> CeoAsk:
    return CeoAsk(query=query, request_id="rq-universe-0001")


def test_a_fresh_snapshot_turns_the_ranking_into_an_explicit_basket() -> None:
    _seed((time.time(), "2026-09-01T05:30:00+00:00", TOP_ROWS))
    expanded = _expand_dynamic_universe_request(_ask())

    assert expanded.query != QUERY
    candidate = deterministic_order_candidate(expanded.query)
    assert candidate is not None
    assert candidate.action.value == "PLACE_BASKET"
    assert len(candidate.basket_instrument_mentions) == 10
    assert str(candidate.notional_krw) == "3000000"
    assert _live_ls_account_stream().market_cap_universe_rows() is not None


@pytest.mark.parametrize(
    ("label", "snapshot", "detail"),
    (
        ("no snapshot at all", None, "market_cap_universe_unavailable"),
        (
            "older than the staleness limit",
            (
                time.time() - ls.MARKET_CAP_UNIVERSE_MAX_AGE_SECONDS - 1,
                "stale",
                TOP_ROWS,
            ),
            "market_cap_universe_unavailable",
        ),
        (
            "fewer names than requested",
            (time.time(), "short", TOP_ROWS[:9]),
            "market_cap_universe_incomplete",
        ),
    ),
)
def test_an_untrusted_ranking_fails_before_order_admission(
    label: str, snapshot: object, detail: str
) -> None:
    """Failing closed keeps a basket the user never approved from being admitted.

    It must never pass the original sentence to another order lane. A silently
    shortened basket would also be a different order (개발 원칙 9).
    """

    _seed(snapshot)
    with pytest.raises(HTTPException) as raised:
        _expand_dynamic_universe_request(_ask())
    assert raised.value.status_code == 503, label
    assert raised.value.detail == detail, label


def test_missing_snapshot_is_acquired_on_demand_before_expansion(monkeypatch) -> None:
    live = _live_ls_account_stream()
    assert live is not None
    live._market_cap_universe = None
    calls = 0

    async def acquire():
        nonlocal calls
        calls += 1
        live._market_cap_universe = (time.time(), "on-demand", TOP_ROWS)
        return list(TOP_ROWS), "on-demand"

    monkeypatch.setattr(live, "acquire_market_cap_universe_rows", acquire)
    monkeypatch.setattr(
        ceo.anyio.from_thread,
        "run",
        lambda async_fn: asyncio.run(async_fn()),
    )

    expanded = _expand_dynamic_universe_request(_ask())

    assert calls == 1
    assert deterministic_order_candidate(expanded.query).action.value == "PLACE_BASKET"
    assert live.market_cap_universe_rows() is not None


def test_ls_acquisition_populates_the_shared_read_only_snapshot(monkeypatch) -> None:
    calls = 0

    async def load(ranking: str):
        nonlocal calls
        calls += 1
        assert ranking == "market_cap"
        return {"rows": list(TOP_ROWS)}

    monkeypatch.setattr(ls, "ENABLE_LS_MARKET_DATA", True)
    monkeypatch.setattr(ls, "_load_market_ranking", load)
    ls._market_cap_universe = None

    rows, as_of = asyncio.run(ls.acquire_market_cap_universe_rows())

    assert calls == 1
    assert rows == TOP_ROWS
    assert ls.market_cap_universe_rows() == (TOP_ROWS, as_of)


def test_an_ordinary_question_is_never_rewritten() -> None:
    _seed((time.time(), "fresh", TOP_ROWS))
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


def test_the_expansion_reads_the_copy_main_py_imported(monkeypatch) -> None:
    """The 2026-09-01 silent no-op, after the fix was already deployed.

    PYTHONPATH carries /app and /app/apps/api, so `ls_account_stream` and
    `apps.api.ls_account_stream` are separate module objects with separate
    globals. `main.py` imports the top-level one, so only that copy runs the
    lifespan that fills the snapshot. The helper imported the package-relative
    one, reached a second module whose snapshot is permanently None, and every
    ranking order fell through to the conditional lane with no error anywhere.
    """

    live = ModuleType("ls_account_stream")
    live._market_cap_universe = (time.time(), "live", TOP_ROWS)
    live.market_cap_universe_rows = lambda **_: (list(TOP_ROWS), "live")
    monkeypatch.setitem(sys.modules, "ls_account_stream", live)

    # The package-relative copy stays empty, exactly as in production.
    pkg = sys.modules.get("apps.api.ls_account_stream")
    if pkg is not None:
        monkeypatch.setattr(pkg, "_market_cap_universe", None, raising=False)

    assert _live_ls_account_stream() is live
    assert _expand_dynamic_universe_request(_ask()).query != QUERY


def test_the_resolution_order_names_the_module_main_py_imports() -> None:
    assert _LS_ACCOUNT_STREAM_NAMES[0] == "ls_account_stream"
