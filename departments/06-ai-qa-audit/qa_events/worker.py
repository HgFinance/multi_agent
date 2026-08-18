#!/usr/bin/env python3
"""Relay durable forward-QA outbox events and consume QA-bound streams.

The handoff transaction writes the immutable outbox row.  This worker only
relays that canonical payload.  Delivery is logically at-least-once: a crash
after XADD and before the database receipt retries the same UUIDv5 event, while
the Redis event-ID marker returns the original stream record without another
XADD.  QA acceptance is independently exact-content idempotent.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

_QA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_QA_DIR))

from qa_events.redis_event_bus import (  # noqa: E402
    INTRADAY_FORWARD_QA_REQUESTED_EVENT,
    QaEventPoisonError,
    is_deterministic_event_error,
    json_safe,
)
from audit.db_session import (  # noqa: E402
    configure_writer_connection,
    runtime_role,
    runtime_session_dsn,
)


FORWARD_QA_DISPATCHER = "qa-worker/forward-dispatch-v2"
FORWARD_QA_SOURCE_DEPARTMENT = "quant-backtest-department"
DEFAULT_HEALTH_CONNECT_TIMEOUT_SECONDS = 3

_CLAIM_DUE_OUTBOX_SQL = """
    select outbox.outbox_id,
           outbox.event_id::text,
           outbox.qa_handoff_id::text,
           outbox.message_id,
           outbox.event_type,
           outbox.trace_id::text,
           outbox.occurred_at,
           outbox.event_payload,
           outbox.payload_fingerprint,
           delivery.status,
           delivery.attempt_count,
           delivery.max_attempts
      from quant.intraday_forward_qa_outbox outbox
      join quant.intraday_forward_qa_delivery_state delivery
        on delivery.outbox_id = outbox.outbox_id
     where (
       (delivery.status in ('PENDING', 'FAILED')
        and delivery.available_at <= now())
       or
       (delivery.status = 'SENT'
        and delivery.sent_at <= now() - (%s * interval '1 second')
        and not exists (
          select 1
            from audit.intraday_forward_reproduction_requests accepted
           where accepted.event_id = outbox.event_id
        ))
     )
     order by delivery.available_at, outbox.outbox_id
     limit 1
       for update of delivery skip locked
"""

_INSERT_DISPATCH_SQL = """
    insert into quant.intraday_forward_qa_dispatches (
      event_id, outbox_id, qa_handoff_id, message_id, event_type,
      source_department, trace_id, transport_stream,
      transport_message_id, payload, payload_fingerprint, dispatched_by
    ) values (
      %s::uuid, %s, %s::uuid, %s, %s, %s, %s::uuid, %s, %s,
      %s::jsonb, %s, %s
    )
    on conflict (event_id) do nothing
"""

_MARK_SENT_SQL = """
    update quant.intraday_forward_qa_delivery_state
       set status = 'SENT',
           attempt_count = least(attempt_count + 1, max_attempts),
           available_at = null,
           last_error = null,
           sent_at = now() + (%s * interval '1 second'),
           updated_at = now()
     where outbox_id = %s
       and status in ('PENDING', 'FAILED', 'SENT')
       and attempt_count = %s
"""

_MARK_RECONCILIATION_FAILED_SQL = """
    update quant.intraday_forward_qa_delivery_state
       set status = %s,
           attempt_count = %s,
           available_at = null,
           last_error = %s,
           sent_at = case
             when %s = 'DLQ' then null
             else now() + (%s * interval '1 second')
           end,
           updated_at = now()
     where outbox_id = %s
       and status = 'SENT'
       and attempt_count = %s
"""

_MARK_FAILED_SQL = """
    update quant.intraday_forward_qa_delivery_state
       set status = %s,
           attempt_count = %s,
           available_at = case
             when %s = 'DLQ' then null
             else now() + (%s * interval '1 second')
           end,
           last_error = %s,
           sent_at = null,
           updated_at = now()
     where outbox_id = %s
       and status in ('PENDING', 'FAILED')
       and attempt_count = %s
"""

_READINESS_SCOPE_SQL = """
    select outbox.outbox_id
      from quant.intraday_forward_qa_outbox outbox
      join quant.intraday_forward_qa_delivery_state delivery
        on delivery.outbox_id = outbox.outbox_id
      left join audit.intraday_forward_reproduction_requests accepted
        on accepted.event_id = outbox.event_id
     limit 0
"""


def forward_qa_message_id(qa_handoff_id: UUID | str) -> str:
    """Return the immutable logical message key for one handoff."""

    handoff_id = UUID(str(qa_handoff_id))
    return f"{INTRADAY_FORWARD_QA_REQUESTED_EVENT}:{handoff_id}"


def forward_qa_event_id(qa_handoff_id: UUID | str) -> UUID:
    """Mirror quant.intraday_forward_qa_event_id(uuid)."""

    return uuid5(NAMESPACE_URL, forward_qa_message_id(qa_handoff_id))


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    try:
        if isinstance(value, str):
            value = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise QaEventPoisonError(f"{field} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise QaEventPoisonError(f"{field} must be a JSON object")
    return json_safe(value)


def _claim_next_outbox(
    connection: Any,
    *,
    acceptance_retry_seconds: int,
) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(_CLAIM_DUE_OUTBOX_SQL, (acceptance_retry_seconds,))
        row = cursor.fetchone()
    if row is None:
        connection.rollback()
        return None
    return {
        "outbox_id": int(row[0]),
        "event_id": str(row[1]),
        "qa_handoff_id": str(row[2]),
        "message_id": str(row[3]),
        "event_type": str(row[4]),
        "trace_id": str(row[5]),
        "occurred_at": row[6],
        # Validate canonical payload shape inside the per-row dispatch try so
        # malformed immutable rows consume the deterministic poison budget.
        "event_payload": row[7],
        "payload_fingerprint": str(row[8]),
        "delivery_status": str(row[9]),
        "attempt_count": int(row[10]),
        "max_attempts": int(row[11]),
    }


def _mark_delivery_failure(
    connection: Any,
    outbox: dict[str, Any],
    error: Exception,
) -> None:
    deterministic = is_deterministic_event_error(error)
    attempted = outbox["attempt_count"] + 1
    attempt = min(attempted, outbox["max_attempts"])
    terminal = deterministic and attempted >= outbox["max_attempts"]
    backoff_seconds = min(300, 2 ** min(max(attempt, 1), 8))
    if outbox["delivery_status"] == "SENT":
        status = "DLQ" if terminal else "SENT"
        with connection.cursor() as cursor:
            cursor.execute(
                _MARK_RECONCILIATION_FAILED_SQL,
                (
                    status,
                    attempt,
                    str(error)[:2000],
                    status,
                    backoff_seconds,
                    outbox["outbox_id"],
                    outbox["attempt_count"],
                ),
            )
        connection.commit()
        return

    status = "DLQ" if terminal else "FAILED"
    with connection.cursor() as cursor:
        cursor.execute(
            _MARK_FAILED_SQL,
            (
                status,
                attempt,
                status,
                backoff_seconds,
                str(error)[:2000],
                outbox["outbox_id"],
                outbox["attempt_count"],
            ),
        )
    connection.commit()


def dispatch_forward_qa_handoffs(
    connection: Any,
    bus: Any,
    *,
    count: int = 50,
    acceptance_retry_seconds: int = 30,
) -> int:
    """Relay due immutable outbox events and commit durable receipts.

    Publishing intentionally precedes the receipt.  If anything fails after
    XADD, the transaction rolls back and delivery state retries the same event
    ID and exact canonical payload.  Redis resolves that retry to the original
    stream message rather than appending another record.
    """

    if count < 1:
        raise ValueError("count must be positive")
    if acceptance_retry_seconds < 1:
        raise ValueError("acceptance_retry_seconds must be positive")
    dispatched = 0
    for _ in range(count):
        outbox = _claim_next_outbox(
            connection,
            acceptance_retry_seconds=acceptance_retry_seconds,
        )
        if outbox is None:
            break
        try:
            event_payload = _json_object(
                outbox["event_payload"], field="event_payload"
            )
            transport_message_id = bus.publish(
                event_id=UUID(outbox["event_id"]),
                event_type=outbox["event_type"],
                trace_id=UUID(outbox["trace_id"]),
                payload=event_payload,
                occurred_at=outbox["occurred_at"],
                # Redis atomically stores one XADD plus a persistent event-ID
                # marker.  Reconciliation can poll forever during a metadata
                # outage without growing the stream/AOF forever.
                idempotency_key=outbox["event_id"],
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    _INSERT_DISPATCH_SQL,
                    (
                        outbox["event_id"],
                        outbox["outbox_id"],
                        outbox["qa_handoff_id"],
                        outbox["message_id"],
                        outbox["event_type"],
                        FORWARD_QA_SOURCE_DEPARTMENT,
                        outbox["trace_id"],
                        str(bus.stream),
                        str(transport_message_id),
                        json.dumps(
                            event_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        outbox["payload_fingerprint"],
                        FORWARD_QA_DISPATCHER,
                    ),
                )
                cursor.execute(
                    _MARK_SENT_SQL,
                    (
                        (
                            min(
                                300,
                                2 ** min(outbox["attempt_count"] + 1, 8),
                            )
                            if outbox["delivery_status"] == "SENT"
                            else 0
                        ),
                        outbox["outbox_id"],
                        outbox["attempt_count"],
                    ),
                )
            connection.commit()
            dispatched += 1
        except Exception as exc:
            connection.rollback()
            _mark_delivery_failure(connection, outbox, exc)
    return dispatched


def _connect_dispatch_database(
    dsn: str,
    *,
    connect_timeout_seconds: int | None = None,
) -> Any:
    try:
        import psycopg2
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "forward QA dispatch requires psycopg2-binary"
        ) from exc
    connect_kwargs = {}
    if connect_timeout_seconds is not None:
        if not 1 <= int(connect_timeout_seconds) <= 10:
            raise ValueError("connect timeout must be between 1 and 10 seconds")
        connect_kwargs["connect_timeout"] = int(connect_timeout_seconds)
    connection = psycopg2.connect(runtime_session_dsn(dsn), **connect_kwargs)
    try:
        # The managed database deliberately has a read-only cluster default.
        # This relay owns the delivery-state transaction, so it must opt in to
        # a read-write session before issuing SELECT ... FOR UPDATE or writing
        # the immutable dispatch receipt.
        configure_writer_connection(connection)
    except Exception:
        connection.close()
        raise
    return connection


def probe_readiness(
    *,
    dsn: str,
    redis_url: str,
    connect_timeout_seconds: int = DEFAULT_HEALTH_CONNECT_TIMEOUT_SECONDS,
) -> None:
    """Prove Redis and the scoped QA database role without mutating queues.

    The database probe uses the same session-safe role selection as the relay,
    then opens a transaction-local read-only scope. LIMIT 0 checks the exact
    relations required by the outbox claim while reading no business row. The
    transaction is always rolled back and the dedicated connection closed.
    """

    if not 1 <= int(connect_timeout_seconds) <= 10:
        raise ValueError("connect timeout must be between 1 and 10 seconds")

    try:
        import redis
    except ModuleNotFoundError as exc:
        raise RuntimeError("QA worker readiness requires redis") from exc

    redis_client = redis.Redis.from_url(
        redis_url,
        socket_connect_timeout=int(connect_timeout_seconds),
        socket_timeout=int(connect_timeout_seconds),
    )
    connection = None
    try:
        if redis_client.ping() is not True:
            raise RuntimeError("QA worker Redis readiness probe failed")
        connection = _connect_dispatch_database(
            dsn,
            connect_timeout_seconds=int(connect_timeout_seconds),
        )
        with connection.cursor() as cursor:
            cursor.execute("set transaction read only")
            cursor.execute("show transaction_read_only")
            read_only = cursor.fetchone()
            if read_only is None or str(read_only[0]).lower() != "on":
                raise RuntimeError("QA worker readiness transaction is not read-only")
            cursor.execute("select current_user, 1")
            identity = cursor.fetchone()
            expected_role = runtime_role()
            if identity is None or identity[1] != 1:
                raise RuntimeError("QA worker database readiness probe failed")
            if expected_role and str(identity[0]) != expected_role:
                raise RuntimeError("QA worker database runtime role is not active")
            cursor.execute(_READINESS_SCOPE_SQL)
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass
        close_redis = getattr(redis_client, "close", None)
        if close_redis is not None:
            try:
                close_redis()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--healthcheck"]:
        try:
            dsn = (
                os.environ.get("RISK_QA_DATABASE_URL", "").strip()
                or os.environ.get("DATABASE_URL", "").strip()
            )
            redis_url = (
                os.environ.get("RISK_QA_EVENT_REDIS_URL", "").strip()
                or os.environ.get("REDIS_URL", "").strip()
            )
            if not dsn or not redis_url:
                raise RuntimeError("QA worker dependency configuration is missing")
            try:
                connect_timeout_seconds = int(
                    os.environ.get(
                        "QA_WORKER_HEALTH_CONNECT_TIMEOUT_SECONDS",
                        str(DEFAULT_HEALTH_CONNECT_TIMEOUT_SECONDS),
                    )
                )
            except ValueError as exc:
                raise RuntimeError(
                    "QA worker health connect timeout must be an integer"
                ) from exc
            probe_readiness(
                dsn=dsn,
                redis_url=redis_url,
                connect_timeout_seconds=connect_timeout_seconds,
            )
        except Exception as exc:
            # Docker stores this bounded output in State.Health.Log. Do not
            # print exception text because driver errors may echo a credentialed
            # DSN; the exception class is sufficient for operational triage.
            print(
                f"qa worker dependencies unavailable ({type(exc).__name__})",
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(1) from None
        print("qa worker dependencies ready", flush=True)
        return
    if arguments:
        raise SystemExit(f"unknown QA worker arguments: {arguments}")

    from api.app import (  # noqa: PLC0415
        _forward_qa_event_bus,
        _qa_event_bus,
        _record_risk_event,
    )

    risk_bus = _qa_event_bus()
    forward_bus = _forward_qa_event_bus()
    if risk_bus is None or forward_bus is None:
        raise SystemExit("RISK_QA_EVENT_REDIS_URL or REDIS_URL is required")
    dsn = (
        os.environ.get("RISK_QA_DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )
    if not dsn:
        raise SystemExit("RISK_QA_DATABASE_URL or DATABASE_URL is required")
    try:
        interval = float(os.environ.get("QA_EVENT_POLL_INTERVAL_SECONDS", "1"))
        acceptance_retry_seconds = int(
            os.environ.get("QA_FORWARD_ACCEPTANCE_RETRY_SECONDS", "30")
        )
    except ValueError as exc:
        raise SystemExit("QA worker polling settings must be numeric") from exc
    if interval < 0 or acceptance_retry_seconds < 1:
        raise SystemExit("QA worker polling settings are out of range")
    connection = _connect_dispatch_database(dsn)
    try:
        while True:
            dispatch_forward_qa_handoffs(
                connection,
                forward_bus,
                count=50,
                acceptance_retry_seconds=acceptance_retry_seconds,
            )
            risk_bus.consume_once(_record_risk_event, count=50, min_idle_ms=1000)
            forward_bus.consume_once(
                _record_risk_event, count=50, min_idle_ms=1000
            )
            time.sleep(interval)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
