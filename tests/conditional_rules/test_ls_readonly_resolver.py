from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from orchestration.conditional_rules import ExpressionNode, IndicatorProviderError
from orchestration.conditional_rules.indicators.broker import (
    LSReadOnlyIndicatorResolver,
    LSReadOnlyTransportError,
)


KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)
QUOTE_NOW = datetime(2026, 8, 20, 1, 0, tzinfo=KST)


def _spec(name: str):
    return ExpressionNode.model_validate(
        {
            "type": "INDICATOR",
            "name": name,
            "output": "VALUE",
            "timeframe": "1D",
            "parameters": {},
            "source": "BROKER",
            "provider": "LS",
        }
    )


def _context(*, clock: str = "BAR_CLOSE", observed_at=NOW) -> dict:
    return {
        "clock": clock,
        "observed_at": observed_at,
        "market_data_source_id": "LS_PAPER_MARKET_DATA",
    }


class FakeReadOnlyTransport:
    def __init__(self, responses=None, *, request_error=None, realtime=None):
        self.responses = responses or {}
        self.request_error = request_error
        self.realtime = realtime
        self.calls: list[dict] = []
        self.realtime_calls: list[dict] = []

    async def request(self, *, path, tr_code, payload):
        self.calls.append({"path": path, "tr_code": tr_code, "payload": payload})
        if self.request_error is not None:
            raise self.request_error
        return self.responses[tr_code]

    async def realtime_snapshot(self, *, tr_code, symbol):
        self.realtime_calls.append({"tr_code": tr_code, "symbol": symbol})
        if self.request_error is not None:
            raise self.request_error
        return self.realtime


@pytest.mark.parametrize(
    ("name", "tr_code", "path", "block", "field", "expected"),
    [
        (
            "FOREIGN_NET_BUY_AMOUNT",
            "t1702",
            "/stock/frgr-itt",
            "t1702OutBlock1",
            "tjj0016",
            Decimal("123"),
        ),
        (
            "INSTITUTION_NET_BUY_AMOUNT",
            "t1702",
            "/stock/frgr-itt",
            "t1702OutBlock1",
            "tjj0018",
            Decimal("-50"),
        ),
        (
            "FOREIGN_NET_BUY_VOLUME",
            "t1717",
            "/stock/frgr-itt",
            "t1717OutBlock",
            "tjj0016_vol",
            Decimal("12"),
        ),
        (
            "INSTITUTION_NET_BUY_VOLUME",
            "t1717",
            "/stock/frgr-itt",
            "t1717OutBlock",
            "tjj0018_vol",
            Decimal("-4"),
        ),
        (
            "PROGRAM_NET_BUY_VOLUME",
            "t1637",
            "/stock/program",
            "t1637OutBlock1",
            "svolume",
            Decimal("7"),
        ),
        (
            "PROGRAM_NET_BUY_AMOUNT",
            "t1637",
            "/stock/program",
            "t1637OutBlock1",
            "svalue",
            Decimal("7000"),
        ),
        (
            "SHORT_SELL_VOLUME",
            "t1927",
            "/stock/etc",
            "t1927OutBlock1",
            "gm_vo",
            Decimal("2"),
        ),
        (
            "SHORT_SELL_RATIO",
            "t1927",
            "/stock/etc",
            "t1927OutBlock1",
            "gm_per",
            Decimal("1.5"),
        ),
    ],
)
def test_rest_field_mapping_returns_only_normalized_value(
    name, tr_code, path, block, field, expected
):
    transport = FakeReadOnlyTransport(
        {tr_code: {block: [{"date": "20260819", field: str(expected)}]}}
    )
    value = asyncio.run(
        LSReadOnlyIndicatorResolver(transport=transport)(
            "005930", _spec(name), _context()
        )
    )

    assert value.value == expected
    assert value.indicator == name
    assert value.source == "BROKER"
    assert value.provider == "LS"
    assert transport.calls[0]["path"] == path
    assert transport.calls[0]["tr_code"] == tr_code
    assert "stock/order" not in transport.calls[0]["path"]


def test_program_amount_request_uses_documented_gubun1_mapping():
    transport = FakeReadOnlyTransport(
        {"t1637": {"t1637OutBlock1": [{"date": "20260819", "svalue": "7"}]}}
    )
    asyncio.run(
        LSReadOnlyIndicatorResolver(transport=transport)(
            "005930", _spec("PROGRAM_NET_BUY_AMOUNT"), _context()
        )
    )
    block = transport.calls[0]["payload"]["t1637InBlock"]
    assert block["gubun1"] == "1"
    assert block["gubun2"] == "1"
    assert block["cts_idx"] == "9999"


def test_market_warning_empty_block_is_a_valid_false_match():
    transport = FakeReadOnlyTransport({"t1405": {"t1405OutBlock1": []}})
    value = asyncio.run(
        LSReadOnlyIndicatorResolver(transport=transport)(
            "005930", _spec("MARKET_WARNING_STATUS"), _context()
        )
    )
    assert value.value is False
    assert transport.calls[0]["path"] == "/stock/market-data"


def test_vi_is_quote_only_and_uses_realtime_transport():
    transport = FakeReadOnlyTransport(
        realtime={"shcode": "005930", "vi_gubun": "1", "time": "010000"}
    )
    value = asyncio.run(
        LSReadOnlyIndicatorResolver(transport=transport)(
            "005930", _spec("VI_STATUS"), _context(clock="QUOTE", observed_at=QUOTE_NOW)
        )
    )
    assert value.value is True
    assert transport.calls == []
    assert transport.realtime_calls == [{"tr_code": "VI_", "symbol": "005930"}]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("slow"), "INDICATOR_PROVIDER_TIMEOUT"),
        (
            LSReadOnlyTransportError("INDICATOR_PROVIDER_RATE_LIMITED", "429"),
            "INDICATOR_PROVIDER_RATE_LIMITED",
        ),
    ],
)
def test_transport_failure_is_fail_closed(error, expected):
    transport = FakeReadOnlyTransport(request_error=error)
    with pytest.raises(IndicatorProviderError) as raised:
        asyncio.run(
            LSReadOnlyIndicatorResolver(transport=transport)(
                "005930", _spec("FOREIGN_NET_BUY_AMOUNT"), _context()
            )
        )
    assert raised.value.code == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"t1702OutBlock1": "not-a-list"}, "INDICATOR_PROVIDER_PARTIAL_DATA"),
        ({"t1702OutBlock1": [{"date": "20260819"}]}, "INDICATOR_PROVIDER_PARTIAL_DATA"),
        (
            {"t1702OutBlock1": [{"date": "20200101", "tjj0016": "1"}]},
            "INDICATOR_PROVIDER_STALE",
        ),
        ({"t1702OutBlock1": [{"date": "20260819", "tjj0016": "NaN"}]}, "INDICATOR_PROVIDER_INVALID_PAYLOAD"),
    ],
)
def test_partial_invalid_and_stale_payloads_fail_closed(payload, expected):
    transport = FakeReadOnlyTransport({"t1702": payload})
    with pytest.raises(IndicatorProviderError) as raised:
        asyncio.run(
            LSReadOnlyIndicatorResolver(transport=transport)(
                "005930", _spec("FOREIGN_NET_BUY_AMOUNT"), _context()
            )
        )
    assert raised.value.code == expected


def test_clock_mismatch_and_unsupported_tr_are_terminal():
    transport = FakeReadOnlyTransport(
        {"t1702": {"t1702OutBlock1": [{"date": "20260819", "tjj0016": "1"}]}}
    )
    with pytest.raises(IndicatorProviderError) as clock:
        asyncio.run(
            LSReadOnlyIndicatorResolver(transport=transport)(
                "005930",
                _spec("FOREIGN_NET_BUY_AMOUNT"),
                _context(clock="QUOTE"),
            )
        )
    assert clock.value.code == "INDICATOR_CLOCK_MISMATCH"

    with pytest.raises(IndicatorProviderError) as tr:
        asyncio.run(
            LSReadOnlyIndicatorResolver(transport=transport)(
                "005930",
                _spec("FOREIGN_NET_BUY_AMOUNT"),
                {**_context(), "tr_code": "t9999"},
            )
        )
    assert tr.value.code == "INDICATOR_TR_UNSUPPORTED"
    assert transport.calls == []


def test_capability_without_production_request_mapping_fails_closed():
    transport = FakeReadOnlyTransport()
    with pytest.raises(IndicatorProviderError) as raised:
        asyncio.run(
            LSReadOnlyIndicatorResolver(transport=transport)(
                "005930", _spec("BROKER_SEARCH_MATCH"), _context()
            )
        )
    assert raised.value.code == "INDICATOR_TR_UNSUPPORTED"
    assert transport.calls == []
