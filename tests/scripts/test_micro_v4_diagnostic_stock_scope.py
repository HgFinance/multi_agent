from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "measure_micro_v4_ast_candidates.py"
SPEC = importlib.util.spec_from_file_location(
    "measure_micro_v4_ast_candidates_stock_scope", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _Cursor:
    def fetchall(self):
        return []


class _Connection:
    def cursor(self):
        return _Cursor()


def test_diagnostic_fails_closed_without_reference_stock_scope() -> None:
    with pytest.raises(RuntimeError, match="reference-plane"):
        MODULE.measure(_Connection())
    with pytest.raises(RuntimeError, match="UUID allowlist"):
        MODULE.measure(_Connection(), meta_conn=_Connection())


def test_diagnostic_uses_current_safe_helper_signature(monkeypatch) -> None:
    stock_id = "00000000-0000-0000-0000-000000000001"
    calls = []

    monkeypatch.setattr(
        MODULE, "assert_stock_instrument_ids",
        lambda *_args, **_kwargs: {
            "asset_scope": "KRX_ACTIVE_STOCK_ONLY",
            "version": "krx-active-stock-only-v1",
            "instrument_count": 1,
        },
    )
    monkeypatch.setattr(
        MODULE, "_dates",
        lambda cursor, horizon, feature_set_version, stock_ids: (
            calls.append((horizon, feature_set_version, stock_ids)) or []),
    )

    assert MODULE.measure(
        _Connection(), meta_conn=_Connection(),
        stock_instrument_ids=[stock_id]) == []
    assert calls == [(2, MODULE.FSV, [stock_id])]


class _TransactionCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params=None):
        self.connection.executed.append(" ".join(sql.split()))

    def fetchall(self):
        return []


class _TransactionConnection:
    def __init__(self):
        self.executed = []
        self.rollback_count = 0
        self.closed = False

    def cursor(self):
        return _TransactionCursor(self)

    def set_session(self, **_kwargs):
        raise AssertionError("diagnostic must not change pooled session defaults")

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True


def test_main_uses_transaction_local_read_only_and_rolls_back(monkeypatch) -> None:
    market = _TransactionConnection()
    metadata = _TransactionConnection()
    connections = iter((market, metadata))

    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.connect = lambda *_args, **_kwargs: next(connections)
    source_registry = types.ModuleType("source_registry")
    source_registry.load_project_env = lambda: {
        "TIMESCALE_DATABASE_URL": "postgresql://market",
        "DATABASE_URL": "postgresql://metadata",
    }
    monkeypatch.setitem(sys.modules, "psycopg2", psycopg2)
    monkeypatch.setitem(sys.modules, "source_registry", source_registry)
    monkeypatch.setattr(MODULE, "measure", lambda *_args, **_kwargs: [])

    assert MODULE.main([]) == 0

    assert market.executed == ["SET TRANSACTION READ ONLY"]
    assert metadata.executed[0] == "SET TRANSACTION READ ONLY"
    assert "from reference.instruments" in metadata.executed[1].lower()
    assert market.rollback_count == metadata.rollback_count == 1
    assert market.closed is metadata.closed is True
