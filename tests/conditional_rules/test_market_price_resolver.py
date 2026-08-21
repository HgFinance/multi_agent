from __future__ import annotations

from decimal import Decimal

import pytest

from orchestration.conditional_rules.market_data import (
    LSPaperMarketPriceResolver,
    MarketPriceResolverError,
)


class FakeTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request_sync(self, **kwargs):
        self.calls.append(kwargs)
        return self.payload


def test_ls_t1102_price_is_read_only_and_normalized() -> None:
    transport = FakeTransport(
        {"t1102OutBlock": {"shcode": "005930", "price": "299500"}}
    )

    snapshot = LSPaperMarketPriceResolver(transport).snapshot("005930")

    assert snapshot.symbol == "005930"
    assert snapshot.price == Decimal("299500")
    assert snapshot.source == "LS_T1102_READONLY_RECEIPT"
    assert transport.calls[0]["tr_code"] == "t1102"
    assert transport.calls[0]["payload"] == {
        "t1102InBlock": {"shcode": "005930", "exchgubun": "U"}
    }


@pytest.mark.parametrize(
    "payload,code",
    [
        ({}, "MARKET_PRICE_INVALID"),
        ({"t1102OutBlock": {"shcode": "005930", "price": "bad"}}, "MARKET_PRICE_INVALID"),
        ({"t1102OutBlock": {"shcode": "005931", "price": "299500"}}, "MARKET_PRICE_SYMBOL_MISMATCH"),
        ({"t1102OutBlock": {"shcode": "005930", "price": "299500.5"}}, "MARKET_PRICE_INVALID"),
    ],
)
def test_invalid_or_mismatched_ls_price_fails_closed(payload, code: str) -> None:
    with pytest.raises(MarketPriceResolverError) as raised:
        LSPaperMarketPriceResolver(FakeTransport(payload)).snapshot("005930")

    assert raised.value.code == code


def test_paper_environment_is_required(monkeypatch) -> None:
    monkeypatch.setenv("LS_ENV", "LIVE")

    with pytest.raises(MarketPriceResolverError) as raised:
        LSPaperMarketPriceResolver.from_env()

    assert raised.value.code == "MARKET_PRICE_PAPER_ENV_REQUIRED"
