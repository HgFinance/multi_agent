"""Risk input projection worker for market/portfolio/mandate events.

The worker stores immutable input snapshots and never makes an approval
decision.  P1 calculation remains an explicit, deterministic service call after
the required inputs are available.  Redis ACK happens only after the canonical
store accepts the event, preserving at-least-once delivery.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

DEFAULT_STREAMS = (
    "market.snapshot.v1",
    "portfolio.snapshot.v1",
    "governance.mandate.changed.v1",
)
DEFAULT_GROUP = "risk-p1-projection"


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


class RiskProjectionError(RuntimeError):
    """Raised when a projection cannot be safely acknowledged."""


@dataclass(frozen=True)
class RiskProjectionEvent:
    message_id: str
    event_id: UUID
    event_type: str
    source_stream: str
    trace_id: UUID
    occurred_at: datetime
    payload: Mapping[str, Any]


class ProjectionSink(Protocol):
    def persist(self, event: RiskProjectionEvent) -> None: ...


class PostgresRiskProjectionStore:
    """Append/idempotently upsert raw Risk input events in canonical PostgreSQL."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def persist(self, event: RiskProjectionEvent) -> None:
        cursor = self._connection.cursor()
        try:
            cursor.execute(
                """
                insert into risk.input_snapshots
                    (event_id, event_type, source_stream, trace_id, occurred_at, payload)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (event_id) do update set
                    payload = excluded.payload,
                    received_at = now()
                """,
                (
                    event.event_id,
                    event.event_type,
                    event.source_stream,
                    event.trace_id,
                    event.occurred_at,
                    json.dumps(event.payload, sort_keys=True, default=str),
                ),
            )
            self._connection.commit()
        except Exception as exc:
            self._connection.rollback()
            raise RiskProjectionError(
                "Risk input projection transaction rolled back"
            ) from exc
        finally:
            cursor.close()


class InMemoryProjectionStore:
    """Deterministic test sink with the same idempotency contract as PostgreSQL."""

    def __init__(self) -> None:
        self.events: dict[UUID, RiskProjectionEvent] = {}

    def persist(self, event: RiskProjectionEvent) -> None:
        self.events[event.event_id] = event


class RiskProjectionWorker:
    def __init__(
        self,
        client: Any,
        sink: ProjectionSink,
        *,
        streams: Iterable[str] = DEFAULT_STREAMS,
        group: str = DEFAULT_GROUP,
        consumer: str = "risk-projection-worker",
    ) -> None:
        self.client = client
        self.sink = sink
        self.streams = tuple(streams)
        self.group = group
        self.consumer = consumer

    def ensure_groups(self) -> None:
        for stream in self.streams:
            try:
                self.client.xgroup_create(stream, self.group, id="0-0", mkstream=True)
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    raise RiskProjectionError(
                        f"projection group setup failed for {stream}"
                    ) from exc

    def consume_once(self, *, count: int = 50) -> int:
        self.ensure_groups()
        response = self.client.xreadgroup(
            self.group,
            self.consumer,
            {stream: ">" for stream in self.streams},
            count=count,
            block=1,
        )
        handled = 0
        for stream_name, messages in response or []:
            stream = _as_text(stream_name)
            for message_id, fields in messages:
                event = _parse_event(stream, _as_text(message_id), fields)
                self.sink.persist(event)
                self.client.xack(stream, self.group, message_id)
                handled += 1
        return handled


def _parse_event(
    stream: str, message_id: str, fields: Mapping[Any, Any]
) -> RiskProjectionEvent:
    normalized = {_as_text(key): _as_text(value) for key, value in fields.items()}
    raw_payload = normalized.get("payload", "{}")
    try:
        payload = (
            json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        )
        event_id = UUID(normalized["event_id"])
        trace_id = UUID(normalized["trace_id"])
        occurred_at = datetime.fromisoformat(normalized["occurred_at"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RiskProjectionError(
            f"invalid projection event {stream}:{message_id}"
        ) from exc
    if not isinstance(payload, dict):
        raise RiskProjectionError(
            f"projection payload is not an object {stream}:{message_id}"
        )
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise RiskProjectionError(
            f"projection event timestamp is naive {stream}:{message_id}"
        )
    return RiskProjectionEvent(
        message_id=message_id,
        event_id=event_id,
        event_type=normalized.get("event_type", stream),
        source_stream=stream,
        trace_id=trace_id,
        occurred_at=occurred_at.astimezone(timezone.utc),
        payload=payload,
    )


def run_forever(worker: RiskProjectionWorker, *, interval_seconds: float = 1.0) -> None:
    while True:
        worker.consume_once()
        time.sleep(interval_seconds)


def main() -> None:
    redis_url = os.environ.get("RISK_PROJECTION_REDIS_URL") or os.environ.get(
        "REDIS_URL"
    )
    database_url = (
        os.environ.get("RISK_QA_DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )
    if not redis_url or not database_url:
        raise SystemExit(
            "RISK_PROJECTION_REDIS_URL/REDIS_URL and DATABASE_URL are required"
        )
    import psycopg2
    import redis

    with psycopg2.connect(database_url) as connection:
        worker = RiskProjectionWorker(
            redis.Redis.from_url(redis_url),
            PostgresRiskProjectionStore(connection),
        )
        run_forever(
            worker,
            interval_seconds=float(os.environ.get("RISK_PROJECTION_POLL_SECONDS", "1")),
        )


if __name__ == "__main__":
    main()
