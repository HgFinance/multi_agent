from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "measure_intraday_microstructure.py"
SPEC = importlib.util.spec_from_file_location(
    "measure_intraday_microstructure_stock_scope", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


STOCK_ID = "00000000-0000-0000-0000-000000000001"


class _Connection:
    def __init__(self):
        self.closed = False
        self.executed = []
        self.rollback_count = 0

    def cursor(self):
        connection = self

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, _params=None):
                connection.executed.append(sql)

        return _Cursor()

    def set_session(self, **_kwargs):
        raise AssertionError("diagnostic must not change pooled session defaults")

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        symbol=STOCK_ID,
        start=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
        minutes=30,
        sample_seconds=5,
        lookback_seconds=30,
        horizons=[5, 30],
        latency_ms=250,
        max_quote_age_seconds=5.0,
        fee_bps=11.5,
        maker_fee_bps=11.5,
        threshold=0.0,
        walk_forward_splits=3,
        minimum_edge_bps=0.0,
        as_known_at=None,
        allow_auction=False,
    )


def test_non_stock_reference_failure_happens_before_market_replay(monkeypatch):
    market = _Connection()
    reference = _Connection()
    replay_called = False

    monkeypatch.setattr(MODULE, "_connection", lambda: market)
    monkeypatch.setattr(MODULE, "_reference_connection", lambda: reference)

    def reject(*_args, **_kwargs):
        raise RuntimeError("invalid=1")

    def replay(*_args, **_kwargs):
        nonlocal replay_called
        replay_called = True
        return [], []

    monkeypatch.setattr(MODULE, "assert_stock_instrument_ids", reject)
    monkeypatch.setattr(MODULE, "load_instrument_events", replay)

    with pytest.raises(RuntimeError, match="invalid=1"):
        MODULE.run(_args())

    assert replay_called is False
    assert market.closed is True
    assert reference.closed is True
    assert market.executed == ["SET TRANSACTION READ ONLY"]
    assert reference.executed == ["SET TRANSACTION READ ONLY"]
    assert market.rollback_count == 1
    assert reference.rollback_count == 1


def test_reference_connect_does_not_change_pooled_session_default(
        monkeypatch) -> None:
    reference = _Connection()
    calls = []
    psycopg2 = types.ModuleType("psycopg2")

    def connect(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return reference

    psycopg2.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg2", psycopg2)
    monkeypatch.setenv("DATABASE_URL", "postgresql://transaction-pool:6543/db")

    assert MODULE._reference_connection() is reference
    assert calls == [(
        "postgresql://transaction-pool:6543/db", {"connect_timeout": 20})]
    assert reference.executed == []
