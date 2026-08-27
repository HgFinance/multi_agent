from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from orchestration.conditional_rules.market_data import (
    LSPaperMarketPriceResolver,
    LSTimescaleMarketPriceResolver,
    MarketPriceResolverError,
)


class FakeTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request_sync(self, **kwargs):
        self.calls.append(kwargs)
        return self.payload


class FakeCursor:
    def __init__(self, row):
        self.row = row
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=()):
        self.executed.append((query, params))

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, row):
        self.cursor_instance = FakeCursor(row)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    def __init__(self, row):
        self.connection = FakeConnection(row)
        self.returned = []

    def getconn(self):
        return self.connection

    def putconn(self, connection):
        self.returned.append(connection)


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


def test_shared_realtime_tick_resolver_reads_one_latest_tick_by_instrument() -> None:
    instrument_id = UUID("40000000-0000-0000-0000-000000000001")
    event_time = datetime(2026, 8, 24, 5, 0, tzinfo=timezone.utc)
    observed_at = datetime(2026, 8, 24, 5, 0, 1, tzinfo=timezone.utc)
    pool = FakePool((event_time, observed_at, "258000", "KRX", "LS"))
    resolver = LSTimescaleMarketPriceResolver(pool, lookback_days=7)

    snapshot = resolver.snapshot_for_instrument("005930", instrument_id)

    assert snapshot.symbol == "005930"
    assert snapshot.price == Decimal("258000")
    assert snapshot.observed_at == observed_at
    assert snapshot.source == "LS_REALTIME_TICK:LS:KRX"
    assert pool.connection.cursor_instance.executed[1][1] == (instrument_id, 7)
    assert "market.market_ticks" in pool.connection.cursor_instance.executed[1][0]
    assert "event_time >= now()" in pool.connection.cursor_instance.executed[1][0]
    assert "limit 1" in pool.connection.cursor_instance.executed[1][0].lower()
    assert pool.connection.commits == 1
    assert pool.returned == [pool.connection]


def test_shared_realtime_tick_resolver_requires_instrument_id() -> None:
    resolver = LSTimescaleMarketPriceResolver(FakePool(None))

    with pytest.raises(MarketPriceResolverError) as raised:
        resolver.snapshot("005930")

    assert raised.value.code == "MARKET_PRICE_INSTRUMENT_REQUIRED"


def test_shared_snapshot_staleness_uses_the_exchange_clock() -> None:
    """A backlogged collector must not make an old price look current.

    On 2026-08-27 the shared tick for 000500 carried event_time 06:06:47 with
    observed_at 06:10:45.  Judged on observed_at the price was one second old,
    so the REST fallback never engaged and a +1% take-profit evaluated against
    207,500 while the stock actually traded at 215,500.
    """

    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from apps.api.conditional_rule_worker import HttpRuntimeClient
    from orchestration.conditional_rules.market_data import MarketPriceSnapshot

    client = HttpRuntimeClient(
        trading_api_url="http://trading-api:8000",
        market_api_url="http://market-api:8036",
        shared_price_max_age_seconds=30.0,
    )
    now = datetime.now(timezone.utc)
    backlogged = MarketPriceSnapshot(
        symbol="000500",
        price=Decimal("208000"),
        observed_at=now - timedelta(seconds=1),
        source="LS_REALTIME_TICK",
        event_time=now - timedelta(seconds=240),
    )
    assert client._shared_snapshot_is_stale(backlogged) is True

    current = MarketPriceSnapshot(
        symbol="000500",
        price=Decimal("215500"),
        observed_at=now - timedelta(seconds=1),
        source="LS_REALTIME_TICK",
        event_time=now - timedelta(seconds=2),
    )
    assert client._shared_snapshot_is_stale(current) is False

    # A source without a separate exchange clock still falls back to observed_at.
    no_event_clock = MarketPriceSnapshot(
        symbol="000500",
        price=Decimal("215500"),
        observed_at=now - timedelta(seconds=240),
        source="LS_T1102_READONLY_RECEIPT",
    )
    assert client._shared_snapshot_is_stale(no_event_clock) is True
