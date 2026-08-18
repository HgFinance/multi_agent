from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

import feature_catalog  # noqa: E402


STOCK_ID = "00000000-0000-0000-0000-000000000001"


class _MetaCursor:
    def __init__(self, identity):
        self.identity = identity

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _sql, _params=None):
        return None

    def fetchall(self):
        return [self.identity]


class _MetaConnection:
    def __init__(self, *, instrument_type="STOCK", listed_to=None):
        self.identity = (
            STOCK_ID, instrument_type, "EQUITY", "KRX", "ACTIVE",
            None, listed_to, False,
        )

    def cursor(self):
        return _MetaCursor(self.identity)


class _MarketCursor:
    def __init__(self, calls):
        self.calls = calls
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = sql
        self.calls.append((sql, params))

    def fetchall(self):
        if "select distinct event_time::date" in self.sql:
            return [(date(2026, 8, 14),), (date(2026, 8, 15),),
                    (date(2026, 8, 18),)]
        if "from market.market_bars" in self.sql:
            return [
                (STOCK_ID, date(2026, 8, 14), 100.0),
                (STOCK_ID, date(2026, 8, 15), 101.0),
                (STOCK_ID, date(2026, 8, 18), 102.0),
            ]
        if "from market.pit_provenance" in self.sql:
            return [("EXCHANGE_TIMESTAMP", "source", date(2026, 8, 14),
                     date(2026, 8, 18))]
        return []


class _MarketConnection:
    def __init__(self):
        self.calls = []

    def cursor(self):
        return _MarketCursor(self.calls)

    def rollback(self):
        return None


def test_measure_fails_closed_without_reference_validation() -> None:
    with pytest.raises(RuntimeError, match="reference-plane"):
        feature_catalog.measure(_MarketConnection(), stock_instrument_ids=[STOCK_ID])
    with pytest.raises(RuntimeError, match="UUID allowlist"):
        feature_catalog.measure(_MarketConnection(), meta_conn=_MetaConnection())


def test_measure_rejects_non_stock_allowlist() -> None:
    with pytest.raises(RuntimeError, match="invalid=1"):
        feature_catalog.measure(
            _MarketConnection(), meta_conn=_MetaConnection(instrument_type="ETF"),
            stock_instrument_ids=[STOCK_ID], features=(), horizon=1,
        )


def test_measure_scopes_dates_and_forward_returns_to_validated_stock_ids() -> None:
    market = _MarketConnection()

    catalog = feature_catalog.measure(
        market, meta_conn=_MetaConnection(), stock_instrument_ids=[STOCK_ID],
        features=(), horizon=1,
    )

    assert catalog.stock_scope["asset_scope"] == "KRX_ACTIVE_STOCK_ONLY"
    assert catalog.stock_scope["instrument_count"] == 1
    assert catalog.stock_scope["feature_last_session"] == "2026-08-15"
    assert catalog.stock_scope["label_read_through"] == "2026-08-18"
    scoped = [(sql, params) for sql, params in market.calls
              if "market.microstructure_features" in sql
              or "market.market_bars" in sql]
    assert scoped
    assert all("instrument_id = any(%s::uuid[])" in sql for sql, _ in scoped)
    assert all(any(
        isinstance(value, (list, tuple)) and STOCK_ID in value
        for value in params
    ) for _, params in scoped)


def test_measure_validates_listing_through_forward_label_exit() -> None:
    with pytest.raises(RuntimeError, match="outside_listing_interval=1"):
        feature_catalog.measure(
            _MarketConnection(),
            meta_conn=_MetaConnection(listed_to=date(2026, 8, 17)),
            stock_instrument_ids=[STOCK_ID], features=(), horizon=1,
        )
