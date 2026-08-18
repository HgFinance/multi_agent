from __future__ import annotations

from datetime import date
from pathlib import Path
import sys
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

import pit_dataset
import backtest_runner
from stock_universe import (
    STOCK_ASSET_SCOPE,
    STOCK_UNIVERSE_VERSION,
    assert_stock_only_universe,
    build_stock_evaluation_identity,
)


def _id(value: int) -> str:
    return str(uuid.UUID(int=value))


def _identity(
    *,
    instrument_type: str = "STOCK",
    asset_class: str = "EQUITY",
    market: str = "KRX",
    status: str = "ACTIVE",
    listed_from: date | None = date(2020, 1, 1),
    listed_to: date | None = None,
    is_spac: bool = False,
) -> dict[str, object]:
    return {
        "instrument_type": instrument_type,
        "asset_class": asset_class,
        "market": market,
        "status": status,
        "listed_from": listed_from,
        "listed_to": listed_to,
        "is_spac": is_spac,
    }


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection
        self.rows: list[tuple] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        normalized = " ".join(sql.lower().split())
        self.connection.queries.append((normalized, params))
        if "from quant.universe_members" in normalized:
            self.rows = [self._metadata_row(instrument_id, include_missing=True)
                         for instrument_id in sorted(self.connection.members)]
            return
        if "from quant.current_krx_stock_instrument_identity" in normalized:
            requested = {str(value) for value in (params or ([],))[0]}
            self.rows = [self._metadata_row(instrument_id)
                         for instrument_id in sorted(requested)
                         if instrument_id in self.connection.metadata]
            return
        raise AssertionError(f"unexpected SQL in stock-only contract test: {normalized}")

    def _metadata_row(self, instrument_id: str, *,
                      include_missing: bool = False) -> tuple:
        identity = self.connection.metadata.get(instrument_id)
        if identity is None:
            if include_missing:
                return (instrument_id, None, None, None, None, None, None,
                        None)
            raise AssertionError("missing metadata row should have been filtered")
        return (
            instrument_id,
            identity["instrument_type"],
            identity["asset_class"],
            identity["market"],
            identity["status"],
            identity["listed_from"],
            identity["listed_to"],
            identity["is_spac"],
        )

    def fetchall(self) -> list[tuple]:
        return list(self.rows)


class _Connection:
    def __init__(self, *, members=(), metadata=None) -> None:
        self.members = {str(value) for value in members}
        self.metadata = {str(key): value
                         for key, value in (metadata or {}).items()}
        self.queries: list[tuple[str, object]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


def test_valid_krx_active_stock_universe_passes() -> None:
    samsung, hynix = _id(1), _id(2)
    connection = _Connection(
        members={samsung, hynix},
        metadata={
            samsung: _identity(listed_from=date(1975, 6, 11)),
            hynix: _identity(listed_from=date(1996, 12, 26)),
        },
    )

    result = assert_stock_only_universe(
        connection,
        _id(100),
        row_instrument_ids=[hynix, samsung, samsung],
        row_dates={
            samsung: (date(2024, 1, 2), date(2026, 8, 14)),
            hynix: (date(2024, 1, 2), date(2026, 8, 14)),
        },
    )

    assert result == {
        "version": STOCK_UNIVERSE_VERSION,
        "asset_scope": STOCK_ASSET_SCOPE,
        "member_count": 2,
        "member_ids": {samsung, hynix},
        "unknown_identity_policy": "FAIL_CLOSED",
    }
    assert all(
        "reference.instruments" not in sql
        for sql, _params in connection.queries
    )
    assert any(
        "quant.current_krx_stock_instrument_identity" in sql
        for sql, _params in connection.queries
    )


@pytest.mark.parametrize(
    "identity",
    [
        _identity(instrument_type="ETF"),
        _identity(instrument_type="ETN"),
        _identity(asset_class="FUND"),
        _identity(market="NASDAQ"),
        _identity(status="HALTED"),
        _identity(status="DELISTED"),
        _identity(is_spac=True),
    ],
    ids=["etf", "etn", "non-equity", "non-krx", "halted", "delisted",
         "spac"],
)
def test_non_krx_active_stock_products_are_rejected(identity) -> None:
    instrument_id = _id(10)
    connection = _Connection(
        members={instrument_id}, metadata={instrument_id: identity})

    with pytest.raises(RuntimeError, match="not KRX ACTIVE STOCK only"):
        assert_stock_only_universe(connection, _id(100))


def test_unregistered_reference_uuid_is_rejected_fail_closed() -> None:
    missing_reference_id = _id(20)
    connection = _Connection(members={missing_reference_id}, metadata={})

    with pytest.raises(RuntimeError, match="not KRX ACTIVE STOCK only"):
        assert_stock_only_universe(connection, _id(100))


@pytest.mark.parametrize(
    "bounds",
    [
        (date(2023, 12, 28), date(2024, 6, 28)),
        (date(2024, 1, 2), date(2025, 1, 2)),
    ],
    ids=["before-listing", "after-delisting"],
)
def test_rows_outside_listing_interval_are_rejected(bounds) -> None:
    instrument_id = _id(30)
    connection = _Connection(
        members={instrument_id},
        metadata={instrument_id: _identity(
            listed_from=date(2024, 1, 2), listed_to=date(2024, 12, 30))},
    )

    with pytest.raises(RuntimeError, match="outside reference listing intervals"):
        assert_stock_only_universe(
            connection,
            _id(100),
            row_instrument_ids={instrument_id},
            row_dates={instrument_id: bounds},
        )


@pytest.mark.parametrize(
    ("observed_factory", "message"),
    [
        (lambda first, _second: {first}, "missing=1 unexpected=0"),
        (lambda first, second: {first, second, _id(43)},
         "missing=0 unexpected=1"),
    ],
    ids=["missing-member-rows", "unexpected-row-instrument"],
)
def test_dataset_rows_must_exactly_match_immutable_universe(
    observed_factory, message,
) -> None:
    first, second = _id(41), _id(42)
    connection = _Connection(
        members={first, second},
        metadata={first: _identity(), second: _identity()},
    )

    with pytest.raises(RuntimeError, match=message):
        assert_stock_only_universe(
            connection,
            _id(100),
            row_instrument_ids=observed_factory(first, second),
        )


def test_pit_dataset_filter_keeps_only_governed_stock_rows_with_audit() -> None:
    good = _id(50)
    etf, etn, non_equity = _id(51), _id(52), _id(53)
    overseas, halted, delisted = _id(54), _id(55), _id(56)
    missing, bounded, spac = _id(57), _id(58), _id(59)
    connection = _Connection(metadata={
        good: _identity(),
        etf: _identity(instrument_type="ETF"),
        etn: _identity(instrument_type="ETN"),
        non_equity: _identity(asset_class="FUND"),
        overseas: _identity(market="NASDAQ"),
        halted: _identity(status="HALTED"),
        delisted: _identity(status="DELISTED"),
        bounded: _identity(
            listed_from=date(2024, 1, 2), listed_to=date(2024, 12, 30)),
        spac: _identity(is_spac=True),
    })
    good_first = {"instrument_id": good, "trade_date": date(2024, 1, 2)}
    good_last = {"instrument_id": good, "trade_date": date(2026, 8, 14)}
    rows = [
        good_first,
        good_last,
        {"instrument_id": etf, "trade_date": date(2024, 6, 3)},
        {"instrument_id": etn, "trade_date": date(2024, 6, 3)},
        {"instrument_id": non_equity, "trade_date": date(2024, 6, 3)},
        {"instrument_id": overseas, "trade_date": date(2024, 6, 3)},
        {"instrument_id": halted, "trade_date": date(2024, 6, 3)},
        {"instrument_id": delisted, "trade_date": date(2024, 6, 3)},
        {"instrument_id": missing, "trade_date": date(2024, 6, 3)},
        {"instrument_id": bounded, "trade_date": date(2023, 12, 28)},
        {"instrument_id": bounded, "trade_date": date(2025, 1, 2)},
        {"instrument_id": spac, "trade_date": date(2024, 6, 3)},
    ]

    kept, audit = pit_dataset.filter_stock_rows(connection, rows)

    assert kept == [good_first, good_last]
    assert kept[0] is good_first and kept[1] is good_last
    assert audit == {
        "version": STOCK_UNIVERSE_VERSION,
        "asset_scope": STOCK_ASSET_SCOPE,
        "requested_instruments": 10,
        "accepted_instruments": 1,
        "accepted_rows": 2,
        "excluded_rows": 10,
        "excluded": {
            "INACTIVE": 2,
            "MISSING_REFERENCE_METADATA": 1,
            "NON_EQUITY": 1,
            "NON_KRX": 1,
            "NON_STOCK": 2,
            "OUTSIDE_LISTING_INTERVAL": 2,
            "SPAC": 1,
        },
        "unknown_identity_policy": "FAIL_CLOSED",
        "listing_interval_policy": "ENFORCE_WHEN_PRESENT",
    }


def test_evaluation_identity_binds_exact_dataset_universe_windows_and_cost() -> None:
    common = {
        "dataset_id": _id(70),
        "dataset_content_hash": "a" * 64,
        "universe_version_id": _id(71),
        "instrument_ids": [_id(1), _id(2)],
        "windows": [
            {"window": "2026H1", "start_session": "2026-01-02",
             "end_session": "2026-06-30"},
            {"window": "2026H2", "start_session": "2026-07-01",
             "end_session": "2026-08-14"},
        ],
        "cost_model_version": "krx-cost-v1",
        "evaluation_scope": "DAILY_WALK_FORWARD",
        "evaluation_plan_fingerprint": "c" * 64,
    }

    identity = build_stock_evaluation_identity(**common)
    reordered = build_stock_evaluation_identity(
        **{**common, "instrument_ids": list(reversed(common["instrument_ids"]))})
    changed = build_stock_evaluation_identity(
        **{**common, "instrument_ids": [*common["instrument_ids"], _id(3)]})

    assert identity["evaluation_identity_complete"] is True
    assert identity["asset_class"] == "EQUITY"
    assert identity["asset_scope"] == STOCK_ASSET_SCOPE
    assert identity["evaluation_fingerprint"] == \
        reordered["evaluation_fingerprint"]
    assert identity["evaluation_fingerprint"] != changed["evaluation_fingerprint"]


def test_evaluation_identity_fails_closed_without_exact_window_boundaries() -> None:
    with pytest.raises(RuntimeError, match="exact boundaries"):
        build_stock_evaluation_identity(
            dataset_id=_id(80), dataset_content_hash="b" * 64,
            universe_version_id=_id(81), instrument_ids=[_id(1)],
            windows=[{"window": "2026H1", "start_session": "",
                      "end_session": "2026-06-30"}],
            cost_model_version="krx-cost-v1",
            evaluation_scope="DAILY_WALK_FORWARD",
            evaluation_plan_fingerprint="c" * 64,
        )


@pytest.mark.parametrize("instrument_id", ["ETF_SYNTHETIC", _id(90)])
def test_public_backtest_rejects_every_unattested_in_memory_market(
    instrument_id,
) -> None:
    """A stock-looking UUID is no substitute for reference-backed evidence."""

    session = date(2026, 8, 14)
    market = backtest_runner.Market.from_rows([{
        "instrument_id": instrument_id,
        "trade_date": session,
        "open": 100.0,
        "close": 101.0,
        "notional": 1_000_000.0,
    }])

    with pytest.raises(RuntimeError, match="reference-validated KRX ACTIVE"):
        backtest_runner.run_backtest(
            market, dict(backtest_runner.DEFAULT_CONFIG, lookback_days=1))


def test_public_backtest_rejects_a_caller_supplied_fake_receipt() -> None:
    session = date(2026, 8, 14)
    market = backtest_runner.Market(
        dates=[session],
        opens={(session, "ETF_SYNTHETIC"): 100.0},
        closes={(session, "ETF_SYNTHETIC"): 101.0},
        symbols=["ETF_SYNTHETIC"],
    )
    market._stock_scope_receipt = object()

    with pytest.raises(RuntimeError, match="reference-validated KRX ACTIVE"):
        backtest_runner.run_backtest(
            market, dict(backtest_runner.DEFAULT_CONFIG, lookback_days=1))
