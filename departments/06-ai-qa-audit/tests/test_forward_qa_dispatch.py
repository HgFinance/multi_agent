"""Durable outbox relay behavior for stock-only forward QA requests."""

from __future__ import annotations

import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "audit"))

import qa_events.worker as qa_worker  # noqa: E402
from qa_events.worker import (  # noqa: E402
    _connect_dispatch_database,
    dispatch_forward_qa_handoffs,
    forward_qa_event_id,
    forward_qa_message_id,
)
from qa_events.redis_event_bus import QaEventPoisonError  # noqa: E402
from repository import (  # noqa: E402
    ForwardQaRequestConflict,
    PostgresAuditRepository,
)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(query.split()).lower()
        self.connection.executions.append((normalized, params))
        if normalized.startswith("select outbox.outbox_id"):
            self.row = (
                self.connection.claim_rows.pop(0)
                if self.connection.claim_rows
                else None
            )
        elif "insert into quant.intraday_forward_qa_dispatches" in normalized:
            if self.connection.fail_receipt_once:
                self.connection.fail_receipt_once = False
                raise RuntimeError("simulated crash after XADD")

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, rows, *, fail_receipt_once=False):
        self.claim_rows = list(rows)
        self.fail_receipt_once = fail_receipt_once
        self.executions = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeBus:
    stream = "quant-qa-events"

    def __init__(self, *, error=None):
        self.error = error
        self.events = []
        self.publish_calls = []
        self.published = {}

    def publish(self, **event):
        self.publish_calls.append(deepcopy(event))
        if self.error is not None:
            raise self.error
        idempotency_key = str(event.get("idempotency_key", ""))
        if idempotency_key and idempotency_key in self.published:
            return self.published[idempotency_key]
        self.events.append(deepcopy(event))
        message_id = f"{len(self.events)}-0"
        if idempotency_key:
            self.published[idempotency_key] = message_id
        return message_id


class DispatchConnection:
    def __init__(self, *, fail_session=False):
        self.fail_session = fail_session
        self.session_modes = []
        self.closed = False

    def set_session(self, **kwargs):
        self.session_modes.append(kwargs)
        if self.fail_session:
            raise RuntimeError("cannot select write mode")

    def close(self):
        self.closed = True


class DispatchRoleCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql):
        self.connection.role_sql.append(sql)

    def fetchone(self):
        return ("svc_qa_worker",)


class DispatchRoleConnection(DispatchConnection):
    def __init__(self):
        super().__init__()
        self.role_sql = []
        self.commits = 0

    def cursor(self):
        return DispatchRoleCursor(self)

    def commit(self):
        self.commits += 1


def test_dispatch_connection_explicitly_selects_read_write_session(monkeypatch):
    connection = DispatchConnection()
    monkeypatch.setitem(
        sys.modules,
        "psycopg2",
        SimpleNamespace(connect=lambda dsn: connection),
    )

    assert _connect_dispatch_database("postgresql://qa") is connection
    assert connection.session_modes == [{"readonly": False}]
    assert connection.closed is False


def test_dispatch_connection_closes_when_session_mode_fails(monkeypatch):
    connection = DispatchConnection(fail_session=True)
    monkeypatch.setitem(
        sys.modules,
        "psycopg2",
        SimpleNamespace(connect=lambda dsn: connection),
    )

    with pytest.raises(RuntimeError, match="cannot select write mode"):
        _connect_dispatch_database("postgresql://qa")

    assert connection.closed is True


def test_dispatch_connection_drops_to_qa_runtime_role(monkeypatch):
    connection = DispatchRoleConnection()
    monkeypatch.setenv("DATABASE_RUNTIME_ROLE", "svc_qa_worker")
    monkeypatch.setitem(
        sys.modules,
        "psycopg2",
        SimpleNamespace(connect=lambda dsn: connection),
    )

    assert _connect_dispatch_database("postgresql://qa") is connection
    assert connection.role_sql == [
        'SET ROLE "svc_qa_worker"',
        "select current_user",
    ]
    assert connection.commits == 2


class HealthCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params=None):
        normalized = " ".join(sql.split()).lower()
        self.connection.statements.append(normalized)
        if normalized == "show transaction_read_only":
            self.row = (self.connection.read_only_mode,)
        elif normalized == "select current_user, 1":
            self.row = (self.connection.current_user, 1)
        else:
            self.row = None

    def fetchone(self):
        return self.row


class HealthConnection:
    def __init__(self, *, current_user="svc_qa_worker"):
        self.current_user = current_user
        self.read_only_mode = "on"
        self.statements = []
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return HealthCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class HealthRedis:
    def __init__(self):
        self.closed = False
        self.pings = 0

    def ping(self):
        self.pings += 1
        return True

    def close(self):
        self.closed = True


def test_worker_readiness_proves_redis_scoped_role_and_read_only_query(
    monkeypatch,
):
    connection = HealthConnection()
    redis_client = HealthRedis()
    redis_calls = []
    connect_calls = []
    monkeypatch.setenv("DATABASE_RUNTIME_ROLE", "svc_qa_worker")
    monkeypatch.setitem(
        sys.modules,
        "redis",
        SimpleNamespace(
            Redis=SimpleNamespace(
                from_url=lambda url, **kwargs: (
                    redis_calls.append((url, kwargs)) or redis_client
                )
            )
        ),
    )
    monkeypatch.setattr(
        qa_worker,
        "_connect_dispatch_database",
        lambda dsn, **kwargs: (
            connect_calls.append((dsn, kwargs)) or connection
        ),
    )

    qa_worker.probe_readiness(
        dsn="postgresql://metadata",
        redis_url="redis://cache",
        connect_timeout_seconds=2,
    )

    assert redis_calls == [
        (
            "redis://cache",
            {"socket_connect_timeout": 2, "socket_timeout": 2},
        )
    ]
    assert redis_client.pings == 1
    assert redis_client.closed is True
    assert connect_calls == [
        ("postgresql://metadata", {"connect_timeout_seconds": 2})
    ]
    assert connection.statements[:3] == [
        "set transaction read only",
        "show transaction_read_only",
        "select current_user, 1",
    ]
    assert any(
        "from quant.intraday_forward_qa_outbox" in statement
        and "limit 0" in statement
        for statement in connection.statements
    )
    assert connection.rollbacks == 1
    assert connection.closed is True


def test_worker_readiness_rejects_wrong_role_and_closes_dependencies(monkeypatch):
    connection = HealthConnection(current_user="postgres")
    redis_client = HealthRedis()
    monkeypatch.setenv("DATABASE_RUNTIME_ROLE", "svc_qa_worker")
    monkeypatch.setitem(
        sys.modules,
        "redis",
        SimpleNamespace(
            Redis=SimpleNamespace(
                from_url=lambda *_args, **_kwargs: redis_client
            )
        ),
    )
    monkeypatch.setattr(
        qa_worker,
        "_connect_dispatch_database",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(RuntimeError, match="runtime role is not active"):
        qa_worker.probe_readiness(
            dsn="postgresql://metadata",
            redis_url="redis://cache",
        )

    assert connection.rollbacks == 1
    assert connection.closed is True
    assert redis_client.closed is True


def test_worker_healthcheck_does_not_print_credentialed_driver_error(
    monkeypatch, capsys
):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://qa:top-secret@metadata.example/qa"
    )
    monkeypatch.setenv("RISK_QA_EVENT_REDIS_URL", "redis://cache")

    def fail_probe(**_kwargs):
        raise RuntimeError(
            "connection failed for "
            "postgresql://qa:top-secret@metadata.example/qa"
        )

    monkeypatch.setattr(qa_worker, "probe_readiness", fail_probe)

    with pytest.raises(SystemExit) as exited:
        qa_worker.main(["--healthcheck"])

    captured = capsys.readouterr()
    assert exited.value.code == 1
    assert "RuntimeError" in captured.err
    assert "top-secret" not in captured.err
    assert "postgresql://" not in captured.err


def outbox_row(*, delivery_status="PENDING", attempt_count=0, max_attempts=5):
    handoff_id = uuid4()
    event_id = forward_qa_event_id(handoff_id)
    trace_id = uuid4()
    occurred_at = datetime(2026, 8, 18, 1, 2, 3, tzinfo=timezone.utc)
    message_id = forward_qa_message_id(handoff_id)
    payload = {
        "message_id": message_id,
        "envelope": {
            "event_id": str(event_id),
            "event_type": "quant.intraday.forward.qa_requested.v1",
            "trace_id": str(trace_id),
        },
        "reproduction_contract": {
            "asset_class": "EQUITY",
            "instrument_type": "STOCK",
            "asset_scope": "KRX_ACTIVE_STOCK_ONLY",
            "promotion_authority": False,
        },
    }
    return (
        7,
        str(event_id),
        str(handoff_id),
        message_id,
        "quant.intraday.forward.qa_requested.v1",
        str(trace_id),
        occurred_at,
        payload,
        "a" * 64,
        delivery_status,
        attempt_count,
        max_attempts,
    )


def test_relay_publishes_canonical_payload_before_durable_receipt():
    row = outbox_row()
    connection = FakeConnection([row])
    bus = FakeBus()

    assert dispatch_forward_qa_handoffs(connection, bus, count=1) == 1

    assert bus.events[0]["event_id"] == UUID(row[1])
    assert bus.events[0]["payload"] is not row[7]
    assert bus.events[0]["payload"] == row[7]
    statements = [query for query, _params in connection.executions]
    assert next(i for i, q in enumerate(statements) if "insert into" in q) < next(
        i for i, q in enumerate(statements) if "set status = 'sent'" in q
    )
    assert connection.commits == 1


def test_crash_after_publish_retries_same_event_id_and_content():
    row = outbox_row()
    connection = FakeConnection([row], fail_receipt_once=True)
    bus = FakeBus()

    assert dispatch_forward_qa_handoffs(connection, bus, count=1) == 0
    connection.claim_rows.append(row)
    assert dispatch_forward_qa_handoffs(connection, bus, count=1) == 1

    assert len(bus.publish_calls) == 2
    assert len(bus.events) == 1
    assert bus.publish_calls[0]["event_id"] == bus.publish_calls[1]["event_id"]
    assert bus.publish_calls[0]["payload"] == bus.publish_calls[1]["payload"]
    assert bus.publish_calls[0]["idempotency_key"] == row[1]
    assert connection.rollbacks == 1
    assert any(
        params and params[0] == "FAILED"
        for query, params in connection.executions
        if "update quant.intraday_forward_qa_delivery_state" in query
    )


def test_long_redis_outage_never_moves_delivery_state_to_dlq():
    row = outbox_row(attempt_count=4, max_attempts=5)
    connection = FakeConnection([row])
    bus = FakeBus(error=OSError("Redis unavailable"))

    assert dispatch_forward_qa_handoffs(connection, bus, count=1) == 0

    failure_params = next(
        params
        for query, params in connection.executions
        if "update quant.intraday_forward_qa_delivery_state" in query
    )
    assert failure_params[0] == "FAILED"
    assert failure_params[1] == 5
    assert "Redis unavailable" in failure_params[4]


def test_unaccepted_sent_event_is_republished_from_durable_outbox():
    row = outbox_row(delivery_status="SENT", attempt_count=1)
    connection = FakeConnection([row])
    bus = FakeBus()

    assert dispatch_forward_qa_handoffs(
        connection, bus, count=1, acceptance_retry_seconds=30
    ) == 1
    assert len(bus.events) == 1
    assert bus.events[0]["event_id"] == UUID(row[1])
    statements = [query for query, _params in connection.executions]
    assert any("not exists" in query for query in statements)
    assert any("on conflict (event_id) do nothing" in query for query in statements)
    assert any(
        "attempt_count = least(attempt_count + 1, max_attempts)" in query
        for query in statements
    )
    assert bus.events[0]["idempotency_key"] == row[1]


def test_republish_failure_consumes_a_bounded_reconciliation_attempt():
    row = outbox_row(delivery_status="SENT", attempt_count=1)
    connection = FakeConnection([row])
    bus = FakeBus(error=OSError("Redis restarted"))

    assert dispatch_forward_qa_handoffs(connection, bus, count=1) == 0

    updates = [
        (query, params)
        for query, params in connection.executions
        if "update quant.intraday_forward_qa_delivery_state" in query
    ]
    assert len(updates) == 1
    assert "set status = %s" in updates[0][0]
    assert updates[0][1][0] == "SENT"
    assert updates[0][1][1] == 2
    assert updates[0][1][2] == "Redis restarted"


def test_unaccepted_sent_event_at_retry_cap_stays_recoverable():
    row = outbox_row(
        delivery_status="SENT", attempt_count=5, max_attempts=5)
    connection = FakeConnection([row])
    bus = FakeBus()

    assert dispatch_forward_qa_handoffs(
        connection, bus, count=1, acceptance_retry_seconds=30) == 1

    assert len(bus.events) == 1
    assert bus.events[0]["idempotency_key"] == row[1]
    update = next(
        (query, params) for query, params in connection.executions
        if "set status = 'sent'" in query)
    assert "least(attempt_count + 1, max_attempts)" in update[0]
    assert update[1][1:] == (row[0], 5)


def test_long_acceptance_outage_does_not_append_duplicate_stream_events():
    base_row = outbox_row(
        delivery_status="SENT", attempt_count=1, max_attempts=5
    )
    bus = FakeBus()

    for attempt in range(1, 8):
        row = base_row[:10] + (min(attempt, 5), 5)
        connection = FakeConnection([row])
        assert dispatch_forward_qa_handoffs(connection, bus, count=1) == 1

    assert len(bus.publish_calls) == 7
    assert len(bus.events) == 1
    assert {
        call["idempotency_key"] for call in bus.publish_calls
    } == {base_row[1]}


def test_terminal_republish_transport_failure_keeps_sent_event_recoverable():
    row = outbox_row(
        delivery_status="SENT", attempt_count=4, max_attempts=5)
    connection = FakeConnection([row])
    bus = FakeBus(error=OSError("Redis unavailable during reconciliation"))

    assert dispatch_forward_qa_handoffs(connection, bus, count=1) == 0

    update = next(
        (query, params) for query, params in connection.executions
        if "set status = %s" in query)
    assert update[1][0] == "SENT"
    assert update[1][1] == 5
    assert update[1][3] == "SENT"


def test_deterministic_outbox_poison_uses_bounded_dlq():
    row = outbox_row(attempt_count=4, max_attempts=5)
    connection = FakeConnection([row])
    bus = FakeBus(error=QaEventPoisonError("invalid immutable payload"))

    assert dispatch_forward_qa_handoffs(connection, bus, count=1) == 0

    update = next(
        (query, params) for query, params in connection.executions
        if "update quant.intraday_forward_qa_delivery_state" in query
    )
    assert update[1][0] == "DLQ"
    assert update[1][1] == 5
    assert "invalid immutable payload" in update[1][4]


def test_reconciliation_recovers_after_outage_longer_than_attempt_cap():
    bus = FakeBus(error=OSError("metadata acceptance path unavailable"))
    base_row = outbox_row(
        delivery_status="SENT", attempt_count=1, max_attempts=5
    )

    for attempt in range(1, 7):
        row = base_row[:10] + (min(attempt, 5), 5)
        connection = FakeConnection([row])
        assert dispatch_forward_qa_handoffs(connection, bus, count=1) == 0
        update = next(
            params
            for query, params in connection.executions
            if "set status = %s" in query
        )
        assert update[0] == "SENT"
        assert update[1] == min(attempt + 1, 5)

    bus.error = None
    recovery_row = base_row[:10] + (5, 5)
    recovery_connection = FakeConnection([recovery_row])
    assert dispatch_forward_qa_handoffs(
        recovery_connection, bus, count=1
    ) == 1
    assert bus.events[-1]["event_id"] == UUID(recovery_row[1])
    sent_update = next(
        params
        for query, params in recovery_connection.executions
        if "set status = 'sent'" in query
    )
    assert sent_update[1:] == (recovery_row[0], 5)


def test_acceptance_retry_interval_must_be_positive():
    with pytest.raises(ValueError, match="acceptance_retry_seconds"):
        dispatch_forward_qa_handoffs(
            FakeConnection([]), FakeBus(), acceptance_retry_seconds=0
        )


class AcceptanceCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        normalized = " ".join(query.split()).lower()
        self.connection.statements.append(normalized)
        if normalized.startswith("select outbox_id"):
            self.row = self.connection.outbox
        elif normalized.startswith("insert into audit.domain_events"):
            self.row = (self.connection.outbox[2],)
        elif normalized.startswith(
            "insert into audit.intraday_forward_reproduction_requests"
        ):
            self.row = (self.connection.outbox[2],)
        else:
            self.row = None

    def fetchone(self):
        return self.row


class AcceptanceConnection:
    def __init__(self, outbox):
        self.outbox = outbox
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return AcceptanceCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class AcceptancePool:
    def __init__(self, connection):
        self.connection = connection

    def getconn(self):
        return self.connection

    def putconn(self, _connection):
        pass


def acceptance_fixture():
    event_id = uuid4()
    handoff_id = uuid4()
    trace_id = uuid4()
    occurred_at = datetime(2026, 8, 18, 4, 5, 6, tzinfo=timezone.utc)
    contract = {
        "forward_confirmation_id": str(uuid4()),
        "report_revision_id": str(uuid4()),
        "experiment_id": str(uuid4()),
        "hypothesis_id": str(uuid4()),
        "decision": "PASS",
        "hypothesis_status": "SUPPORTED",
        "asset_class": "EQUITY",
        "instrument_type": "STOCK",
        "asset_scope": "KRX_ACTIVE_STOCK_ONLY",
        "product_filter": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
        "requested_action": "INDEPENDENT_QA_REPRODUCTION",
        "promotion_authority": False,
        "instrument_count": 42,
        "instrument_set_fingerprint": "1" * 64,
        "session_count": 20,
        "session_set_fingerprint": "2" * 64,
    }
    payload_ref = {
        "artifact_type": "INTRADAY_FORWARD_REPORT_REVISION",
        "artifact_id": contract["report_revision_id"],
        "artifact_schema": "intraday-forward-report-revision-v1",
        "content_hash": f"sha256:{'3' * 64}",
    }
    payload = {
        "message_id": f"quant.intraday.forward.qa_requested.v1:{handoff_id}",
        "envelope": {
            "event_id": str(event_id),
            "event_type": "quant.intraday.forward.qa_requested.v1",
            "trace_id": str(trace_id),
            "occurred_at": occurred_at.isoformat(),
            "payload_ref": payload_ref,
        },
        "reproduction_contract": contract,
    }
    outbox = (
        9,
        str(handoff_id),
        str(event_id),
        "quant.intraday.forward.qa_requested.v1",
        "quant-backtest-department",
        str(trace_id),
        occurred_at,
        payload,
        payload_ref,
        contract,
        "4" * 64,
    )
    event = {
        "event_id": str(event_id),
        "event_type": "quant.intraday.forward.qa_requested.v1",
        "trace_id": str(trace_id),
        "occurred_at": occurred_at.isoformat(),
        "payload": deepcopy(payload),
    }
    return outbox, event


def test_forward_qa_acceptance_commits_ledger_request_and_work_atomically():
    outbox, event = acceptance_fixture()
    connection = AcceptanceConnection(outbox)
    repository = PostgresAuditRepository(AcceptancePool(connection))

    repository.accept_intraday_forward_qa_request(event)

    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert any("insert into audit.domain_events" in q for q in connection.statements)
    assert any(
        "insert into audit.intraday_forward_reproduction_requests" in q
        for q in connection.statements
    )
    assert any(
        "insert into audit.intraday_forward_reproduction_work_items" in q
        for q in connection.statements
    )


def test_forward_qa_acceptance_rejects_payload_drift_and_rolls_back():
    outbox, event = acceptance_fixture()
    event["payload"]["reproduction_contract"]["instrument_count"] = 41
    connection = AcceptanceConnection(outbox)
    repository = PostgresAuditRepository(AcceptancePool(connection))

    try:
        repository.accept_intraday_forward_qa_request(event)
    except ForwardQaRequestConflict:
        pass
    else:  # pragma: no cover - explicit assertion message
        raise AssertionError("payload drift must be rejected")

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not any(
        "insert into audit.domain_events" in q for q in connection.statements
    )
