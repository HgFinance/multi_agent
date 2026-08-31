from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

QA_ROOT = Path(__file__).resolve().parents[1]
if str(QA_ROOT) not in sys.path:
    sys.path.insert(0, str(QA_ROOT))

from qa_events import reproduction_worker as worker  # noqa: E402


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split()).lower()
        self.connection.executions.append((normalized, params))
        operation = next((name for name in (
            "claim", "heartbeat", "complete", "fail")
            if f"{name}_intraday_forward_reproduction_work" in normalized),
            None)
        if operation and operation in self.connection.operation_errors:
            raise self.connection.operation_errors[operation]
        if "has_intraday_forward_reproduction_work" in normalized:
            self.row = (self.connection.has_pending_work,)
        elif "claim_intraday_forward_reproduction_work" in normalized:
            self.row = (self.connection.bundle,)
        elif "heartbeat_intraday_forward_reproduction_work" in normalized:
            self.row = (self.connection.heartbeat_owned,)
        elif "complete_intraday_forward_reproduction_work" in normalized:
            self.row = ("90000000-0000-0000-0000-000000000001",)
        elif "fail_intraday_forward_reproduction_work" in normalized:
            self.row = (self.connection.failure_status,)
        elif normalized == "show transaction_read_only":
            self.row = (self.connection.readonly_mode,)
        elif normalized == "select current_user":
            self.row = (self.connection.current_user,)
        elif normalized == "select 1":
            self.row = (1,)
        else:
            self.row = None

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, bundle=None):
        self.bundle = bundle
        self.has_pending_work = True
        self.heartbeat_owned = True
        self.failure_status = "RETRY"
        self.readonly_mode = "on"
        self.current_user = "svc_qa_reproducer"
        self.operation_errors = {}
        self.executions = []
        self.session_modes = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def set_session(self, **kwargs):
        self.session_modes.append(kwargs)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def bundle():
    return {
        "contract_version": "intraday-forward-qa-reproduction-input-v1",
        "work_item": {
            "work_item_id": "10000000-0000-0000-0000-000000000001",
            "reproduction_request_id":
                "10000000-0000-0000-0000-000000000002",
            "lease_token": "10000000-0000-0000-0000-000000000003",
        },
    }


def result(verdict="PASS"):
    value = {
        "version": worker.QA_REPRODUCTION_VERSION,
        "verdict": verdict,
        "promotion_authority": False,
    }
    value["result_fingerprint"] = worker.stable_fingerprint(value)
    return value


def test_process_completes_scientific_pass_with_heartbeats_and_closes_market():
    metadata = Connection(bundle())
    market = Connection()
    guard_calls = []

    def reproduce(supplied_market, supplied_bundle, *, lease_guard):
        assert supplied_market is market
        assert supplied_bundle is metadata.bundle
        guard_calls.append(True)
        lease_guard(True)
        return result("PASS")

    output = worker.process_once(
        metadata, market_connect=lambda: market, reproduce=reproduce,
        worker="qa-reproducer/test", monotonic_fn=lambda: 100.0)

    assert output == {
        "status": "COMPLETED",
        "verdict": "PASS",
        "result_id": "90000000-0000-0000-0000-000000000001",
        "work_item_id": "10000000-0000-0000-0000-000000000001",
    }
    assert guard_calls == [True]
    assert market.closed is True
    statements = [sql for sql, _params in metadata.executions]
    assert sum("heartbeat_intraday" in sql for sql in statements) == 3
    assert sum("complete_intraday" in sql for sql in statements) == 1
    assert not any("fail_intraday" in sql for sql in statements)


def test_empty_queue_skips_expensive_claim():
    metadata = Connection()
    metadata.has_pending_work = False

    assert worker.process_once(
        metadata, market_connect=Connection,
        worker="qa-reproducer/test", monotonic_fn=lambda: 100.0) is None

    statements = [sql for sql, _params in metadata.executions]
    assert any("has_intraday_forward_reproduction_work" in sql
               for sql in statements)
    assert not any("claim_intraday" in sql for sql in statements)
    assert metadata.commits == 1


def test_scientific_fail_is_completed_not_retried():
    metadata = Connection(bundle())
    market = Connection()

    output = worker.process_once(
        metadata, market_connect=lambda: market,
        reproduce=lambda *_args, **_kwargs: result("FAIL"),
        worker="qa-reproducer/test", monotonic_fn=lambda: 100.0)

    assert output["status"] == "COMPLETED"
    assert output["verdict"] == "FAIL"
    statements = [sql for sql, _params in metadata.executions]
    assert any("complete_intraday" in sql for sql in statements)
    assert not any("fail_intraday" in sql for sql in statements)


def test_missing_frozen_runtime_completes_before_market_connect():
    metadata = Connection(bundle())
    market_calls = []

    def reproduce_without_market(supplied_market, *_args, **_kwargs):
        assert supplied_market is None
        return result("INCONCLUSIVE")

    output = worker.process_once(
        metadata,
        market_connect=lambda: market_calls.append(True),
        reproduce=reproduce_without_market,
        runtime_preflight=lambda _bundle: {
            "reproduction_route_available": False,
            "status": "FROZEN_RUNTIME_ARTIFACT_UNAVAILABLE",
        },
        worker="qa-reproducer/test", monotonic_fn=lambda: 100.0)

    assert output["status"] == "COMPLETED"
    assert output["verdict"] == "INCONCLUSIVE"
    assert market_calls == []
    statements = [sql for sql, _params in metadata.executions]
    assert any("complete_intraday" in sql for sql in statements)
    assert not any("fail_intraday" in sql for sql in statements)


def test_infrastructure_error_uses_fenced_retry_api():
    metadata = Connection(bundle())
    market = Connection()

    def broken(*_args, **_kwargs):
        raise RuntimeError("raw store unavailable")

    output = worker.process_once(
        metadata, market_connect=lambda: market, reproduce=broken,
        worker="qa-reproducer/test", monotonic_fn=lambda: 100.0)

    assert output["status"] == "RETRY"
    assert "raw store unavailable" in output["error"]
    statements = [sql for sql, _params in metadata.executions]
    assert any("fail_intraday" in sql for sql in statements)
    assert not any("complete_intraday" in sql for sql in statements)
    assert market.closed is True


def test_lost_lease_never_writes_failure_under_an_old_fence():
    metadata = Connection(bundle())
    metadata.heartbeat_owned = False

    with pytest.raises(worker.ReproductionLeaseLost):
        worker.process_once(
            metadata, market_connect=lambda: Connection(),
            reproduce=lambda *_args, **_kwargs: result(),
            worker="qa-reproducer/test", monotonic_fn=lambda: 100.0)

    statements = [sql for sql, _params in metadata.executions]
    assert not any("complete_intraday" in sql for sql in statements)
    assert not any("fail_intraday" in sql for sql in statements)


def test_stale_completion_sql_error_is_lease_lost_without_failure_recall():
    metadata = Connection(bundle())
    metadata.operation_errors["complete"] = RuntimeError(
        "QA reproduction lease is stale or owned by another worker")

    with pytest.raises(worker.ReproductionLeaseLost):
        worker.process_once(
            metadata, market_connect=Connection,
            reproduce=lambda *_args, **_kwargs: result(),
            worker="qa-reproducer/test", monotonic_fn=lambda: 100.0)

    statements = [sql for sql, _params in metadata.executions]
    assert sum("complete_intraday" in sql for sql in statements) == 1
    assert not any("fail_intraday" in sql for sql in statements)


def test_stale_failure_sql_error_is_lease_lost_without_second_failure_call():
    metadata = Connection(bundle())
    metadata.operation_errors["fail"] = RuntimeError(
        "QA reproduction lease is stale or already completed")

    with pytest.raises(worker.ReproductionLeaseLost):
        worker.process_once(
            metadata, market_connect=Connection,
            reproduce=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("market timeout")),
            worker="qa-reproducer/test", monotonic_fn=lambda: 100.0)

    statements = [sql for sql, _params in metadata.executions]
    assert sum("fail_intraday" in sql for sql in statements) == 1


def test_stale_heartbeat_sql_error_is_lease_lost_without_failure_call():
    metadata = Connection(bundle())
    metadata.operation_errors["heartbeat"] = RuntimeError(
        "QA reproduction lease was lost during heartbeat")

    with pytest.raises(worker.ReproductionLeaseLost):
        worker.process_once(
            metadata, market_connect=Connection,
            reproduce=lambda *_args, **_kwargs: result(),
            worker="qa-reproducer/test", monotonic_fn=lambda: 100.0)

    statements = [sql for sql, _params in metadata.executions]
    assert sum("heartbeat_intraday" in sql for sql in statements) == 1
    assert not any("fail_intraday" in sql for sql in statements)


def test_completion_rejects_a_tampered_result_fingerprint_before_sql():
    metadata = Connection(bundle())
    tampered = result()
    tampered["verdict"] = "FAIL"

    with pytest.raises(RuntimeError, match="fingerprint"):
        worker.complete(
            metadata, bundle=metadata.bundle,
            worker="qa-reproducer/test", result=tampered)

    assert not metadata.executions


def test_market_connection_proves_transaction_read_only(monkeypatch):
    connection = Connection()
    connect_calls = []
    monkeypatch.setitem(
        sys.modules, "psycopg2",
        SimpleNamespace(connect=lambda *args, **kwargs: (
            connect_calls.append((args, kwargs)) or connection)))

    assert worker.connect_market_database(
        "postgresql://user:secret@pool.example:6543/market") is connection
    assert connection.session_modes == []
    assert [sql for sql, _params in connection.executions] == [
        "set transaction read only",
        "show transaction_read_only",
    ]
    # The proved read-only transaction stays open for the complete replay.
    # Closing the connection later discards it; no session GUC survives.
    assert connection.rollbacks == 0
    assert connect_calls[0][1] == {
        "connect_timeout": worker.DEFAULT_CONNECT_TIMEOUT_SECONDS,
        "options": "-c statement_timeout=300000 -c lock_timeout=5000",
    }

    broken = Connection()
    broken.readonly_mode = "off"
    monkeypatch.setitem(
        sys.modules, "psycopg2",
        SimpleNamespace(connect=lambda *_args, **_kwargs: broken))
    with pytest.raises(RuntimeError, match="not read-only"):
        worker.connect_market_database("postgresql://market")
    assert broken.closed is True


def test_metadata_connection_selects_reproducer_role(monkeypatch):
    connection = Connection()
    connect_calls = []
    monkeypatch.setenv("DATABASE_RUNTIME_ROLE", "svc_qa_reproducer")
    monkeypatch.setitem(
        sys.modules, "psycopg2",
        SimpleNamespace(connect=lambda *args, **kwargs: (
            connect_calls.append((args, kwargs)) or connection)))

    assert worker.connect_metadata_database(
        "postgresql://metadata") is connection
    assert connection.session_modes == [{"readonly": False}]
    assert connection.executions[:2] == [
        ('set role "svc_qa_reproducer"', ()),
        ("select current_user", ()),
    ]
    assert connect_calls[0][1] == {
        "connect_timeout": worker.DEFAULT_CONNECT_TIMEOUT_SECONDS,
        "options": "-c statement_timeout=30000 -c lock_timeout=5000",
    }


def test_runtime_settings_match_database_lease_bound_and_cap_queries(
        monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://metadata")
    monkeypatch.setenv(
        "QA_REPRODUCTION_TIMESCALE_DATABASE_URL", "postgresql://market")
    monkeypatch.setenv("QA_REPRODUCTION_LEASE_SECONDS", "7201")
    with pytest.raises(RuntimeError, match="between 30 and 7200"):
        worker._runtime_settings()

    monkeypatch.setenv("QA_REPRODUCTION_LEASE_SECONDS", "30")
    monkeypatch.setenv("QA_REPRODUCTION_HEARTBEAT_SECONDS", "10")
    monkeypatch.setenv(
        "QA_REPRODUCTION_MARKET_STATEMENT_TIMEOUT_MS", "300000")
    settings = worker._runtime_settings()
    assert settings.lease_seconds == 30
    assert settings.market_statement_timeout_ms == 20_000
    assert settings.metadata_statement_timeout_ms == 20_000
    assert settings.market_lock_timeout_ms <= \
        settings.market_statement_timeout_ms


def test_readiness_executes_claim_under_rollback_and_reads_market(monkeypatch):
    settings = worker.RuntimeSettings(
        metadata_dsn="postgresql://metadata",
        market_dsn="postgresql://market",
        lease_seconds=7_200,
        heartbeat_seconds=60,
        poll_seconds=15,
        connect_timeout_seconds=10,
        metadata_statement_timeout_ms=30_000,
        metadata_lock_timeout_ms=5_000,
        market_statement_timeout_ms=300_000,
        market_lock_timeout_ms=5_000,
    )
    metadata = Connection(None)
    market = Connection()
    monkeypatch.setattr(
        worker, "connect_metadata_database",
        lambda *_args, **_kwargs: metadata)
    monkeypatch.setattr(
        worker, "connect_market_database",
        lambda *_args, **_kwargs: market)

    worker.probe_readiness(settings)

    assert any("claim_intraday" in sql for sql, _ in metadata.executions)
    assert metadata.rollbacks == 1
    assert metadata.closed is True
    assert any(sql == "select 1" for sql, _ in market.executions)
    assert market.rollbacks == 1
    assert market.closed is True
