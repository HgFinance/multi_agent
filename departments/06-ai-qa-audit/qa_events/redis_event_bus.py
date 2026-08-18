#!/usr/bin/env python3
"""At-least-once Redis Streams transport for QA-bound events.

Successful handling is acknowledged and deduplicated by event ID.  Dependency
failures remain pending with bounded exponential backoff.  Only malformed or
deterministically conflicting immutable messages consume the bounded poison
budget and enter the dead-letter stream.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

RISK_DECISION_EVENT = "risk.decision.v1"
QA_DECISION_EVENT = "qa.decision.v1"
INTRADAY_FORWARD_QA_REQUESTED_EVENT = (
    "quant.intraday.forward.qa_requested.v1"
)
DEFAULT_STREAM = "risk-qa-events"
DEFAULT_GROUP = "qa-risk-decision-consumers"
RISK_QA_DLQ_STREAM = "risk-qa-events-dlq"
FORWARD_QA_STREAM = "quant-qa-events"
FORWARD_QA_GROUP = "qa-intraday-forward-consumers"
FORWARD_QA_DLQ_STREAM = "quant-qa-events-dlq"
DEFAULT_DEDUPE_TTL_SECONDS = 7 * 24 * 60 * 60


class QaEventBusError(RuntimeError):
    """The QA event transport could not complete an operation."""


class QaEventTransientError(QaEventBusError):
    """The event could not be handled because a dependency is unavailable."""


class QaEventPoisonError(QaEventBusError):
    """The immutable event is malformed or deterministically unacceptable."""


def is_deterministic_event_error(error: BaseException) -> bool:
    """Return whether retrying the exact immutable input cannot help.

    Dependency and persistence errors are transient by default.  Payload
    validators may raise common built-in validation errors, while callers with
    richer rules can raise :class:`QaEventPoisonError`.  Causes are inspected
    because the API envelope validator wraps built-in validation failures.
    """

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, QaEventTransientError):
            return False
        if isinstance(current, QaEventPoisonError):
            return True
        if isinstance(current, (KeyError, TypeError, ValueError, UnicodeError)):
            return True
        current = current.__cause__ or current.__context__
    return False


def json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    return value


class RedisEventBus:
    """Redis Stream publisher/consumer with restart-safe group handling."""

    def __init__(
        self,
        client: Any,
        *,
        stream: str = DEFAULT_STREAM,
        group: str = DEFAULT_GROUP,
        consumer: str = "qa-api",
        dedupe_prefix: str = "risk-qa:event-processed:",
        dedupe_ttl_seconds: int = DEFAULT_DEDUPE_TTL_SECONDS,
        dead_letter_stream: str | None = None,
        max_delivery_attempts: int = 5,
        failure_prefix: str = "risk-qa:event-failures:",
        stream_maxlen: int | None = 10000,
        transient_retry_base_seconds: float = 1.0,
        transient_retry_max_seconds: float = 300.0,
        published_event_prefix: str | None = None,
    ) -> None:
        if dedupe_ttl_seconds < 1:
            raise ValueError("dedupe_ttl_seconds must be positive")
        if max_delivery_attempts < 1:
            raise ValueError("max_delivery_attempts must be positive")
        if stream_maxlen is not None and stream_maxlen < 1:
            raise ValueError("stream_maxlen must be positive or None")
        if transient_retry_base_seconds <= 0:
            raise ValueError("transient_retry_base_seconds must be positive")
        if transient_retry_max_seconds < transient_retry_base_seconds:
            raise ValueError(
                "transient_retry_max_seconds must be at least the base"
            )
        self.client = client
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.dedupe_prefix = dedupe_prefix
        self.dedupe_ttl_seconds = dedupe_ttl_seconds
        self.dead_letter_stream = dead_letter_stream
        self.max_delivery_attempts = max_delivery_attempts
        self.failure_prefix = failure_prefix
        self.stream_maxlen = stream_maxlen
        self.transient_retry_base_seconds = transient_retry_base_seconds
        self.transient_retry_max_seconds = transient_retry_max_seconds
        self.published_event_prefix = (
            published_event_prefix
            if published_event_prefix is not None
            else f"{stream}:event-published:"
        )

    def ensure_group(self) -> None:
        try:
            self.client.xgroup_create(
                self.stream, self.group, id="0-0", mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise QaEventBusError(
                    f"Redis consumer group creation failed: {exc}"
                ) from exc

    def publish(
        self,
        *,
        event_id: UUID,
        event_type: str,
        trace_id: UUID,
        payload: dict[str, Any],
        occurred_at: datetime | str | None = None,
        idempotency_key: str | UUID | None = None,
    ) -> str:
        event_time = occurred_at or datetime.now().astimezone()
        event_time_text = (
            event_time.isoformat()
            if isinstance(event_time, datetime)
            else str(event_time)
        )
        try:
            fields = {
                "event_id": str(event_id),
                "event_type": event_type,
                "trace_id": str(trace_id),
                "occurred_at": event_time_text,
                "payload": json.dumps(json_safe(payload), sort_keys=True),
            }
            trim = (
                {"maxlen": self.stream_maxlen, "approximate": True}
                if self.stream_maxlen is not None
                else {}
            )
            if idempotency_key is None:
                message_id = self.client.xadd(self.stream, fields, **trim)
            else:
                message_id = self._xadd_idempotent(
                    idempotency_key=str(idempotency_key),
                    fields=fields,
                )
        except Exception as exc:
            raise QaEventBusError(f"QA event publication failed: {exc}") from exc
        return message_id.decode() if isinstance(message_id, bytes) else str(message_id)

    def _xadd_idempotent(
        self,
        *,
        idempotency_key: str,
        fields: dict[str, str],
    ) -> Any:
        """Atomically publish at most one stream entry per logical event.

        The persistent marker and XADD live in one Redis script, so a crash
        cannot leave a marker without the message.  Reconciliation after a
        long metadata-DB outage performs bounded key reads instead of appending
        duplicate stream records indefinitely.
        """

        marker_key = f"{self.published_event_prefix}{idempotency_key}"
        maxlen = "" if self.stream_maxlen is None else str(self.stream_maxlen)
        argv: list[str] = [maxlen]
        for key, value in fields.items():
            argv.extend((str(key), str(value)))
        script = """
local existing = redis.call('GET', KEYS[1])
if existing then
  return existing
end
local message_id
if ARGV[1] == '' then
  message_id = redis.call('XADD', KEYS[2], '*', unpack(ARGV, 2))
else
  message_id = redis.call(
    'XADD', KEYS[2], 'MAXLEN', '~', ARGV[1], '*', unpack(ARGV, 2)
  )
end
redis.call('SET', KEYS[1], message_id)
return message_id
"""
        if not hasattr(self.client, "eval"):
            raise QaEventBusError(
                "idempotent forward publishing requires Redis EVAL support"
            )
        return self.client.eval(
            script,
            2,
            marker_key,
            self.stream,
            *argv,
        )

    def consume_once(
        self,
        handler: Callable[[dict[str, Any]], None],
        *,
        count: int = 10,
        min_idle_ms: int = 0,
        block_ms: int | None = None,
    ) -> int:
        """Process pending and new events without losing transient failures."""

        if block_ms is not None and block_ms < 1:
            raise ValueError("block_ms must be positive or None")

        self.ensure_group()
        messages = self._claim_pending(count=count, min_idle_ms=min_idle_ms)
        if not messages:
            try:
                streams = self.client.xreadgroup(
                    self.group,
                    self.consumer,
                    {self.stream: ">"},
                    count=count,
                    # BLOCK 0 is an infinite wait.  The QA worker polls two
                    # independent streams sequentially, so idle must not starve
                    # an already-published event on the other stream.
                    block=block_ms,
                )
            except Exception as exc:
                raise QaEventBusError(f"Redis event consumption failed: {exc}") from exc
            messages = self._flatten(streams)

        processed = 0
        for message_id, fields in messages:
            try:
                normalized = self._normalize_fields(fields)
            except (TypeError, ValueError, UnicodeError) as exc:
                fallback = self._normalize_fields(fields, parse_payload=False)
                self._handle_deterministic_failure(
                    message_id=message_id,
                    normalized=fallback,
                    event_id=(
                        fallback.get("event_id")
                        or f"stream-message:{self._message_id_text(message_id)}"
                    ),
                    error=QaEventPoisonError(
                        f"event fields are malformed: {exc}"
                    ),
                )
                continue
            event_id = normalized.get("event_id")
            if not event_id:
                self._handle_deterministic_failure(
                    message_id=message_id,
                    normalized=normalized,
                    event_id=f"stream-message:{self._message_id_text(message_id)}",
                    error=QaEventPoisonError("event_id is missing"),
                )
                continue
            dedupe_key = f"{self.dedupe_prefix}{event_id}"
            if self.client.get(dedupe_key):
                self.client.xack(self.stream, self.group, message_id)
                processed += 1
                continue
            if not self._transient_retry_is_due(event_id):
                continue
            event = {
                "event_id": normalized["event_id"],
                "event_type": normalized.get("event_type", ""),
                "trace_id": normalized.get("trace_id", ""),
                "occurred_at": normalized.get("occurred_at", ""),
                "payload": normalized.get("payload", {}),
            }
            try:
                handler(event)
            except Exception as exc:
                if is_deterministic_event_error(exc):
                    self._handle_deterministic_failure(
                        message_id=message_id,
                        normalized=normalized,
                        event_id=event_id,
                        error=exc,
                    )
                else:
                    self._handle_transient_failure(
                        event_id=event_id,
                        error=exc,
                    )
                continue
            self.client.set(dedupe_key, "1", ex=self.dedupe_ttl_seconds)
            if hasattr(self.client, "delete"):
                self.client.delete(
                    f"{self.failure_prefix}{event_id}",
                    self._transient_retry_key(event_id),
                )
            self.client.xack(self.stream, self.group, message_id)
            processed += 1
        return processed

    @staticmethod
    def _message_id_text(message_id: Any) -> str:
        return (
            message_id.decode()
            if isinstance(message_id, bytes)
            else str(message_id)
        )

    def _transient_retry_key(self, event_id: str) -> str:
        return f"{self.failure_prefix}transient:{event_id}"

    def _transient_retry_state(self, event_id: str) -> dict[str, Any]:
        raw = self.client.get(self._transient_retry_key(event_id))
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not raw:
            return {"attempt": 0, "not_before": 0.0}
        try:
            state = json.loads(str(raw))
            return {
                "attempt": max(0, int(state.get("attempt", 0))),
                "not_before": max(0.0, float(state.get("not_before", 0.0))),
            }
        except (TypeError, ValueError):
            return {"attempt": 0, "not_before": 0.0}

    def _transient_retry_is_due(self, event_id: str) -> bool:
        return time.time() >= self._transient_retry_state(event_id)["not_before"]

    def _handle_transient_failure(
        self,
        *,
        event_id: str,
        error: Exception,
    ) -> None:
        """Keep dependency failures pending forever with bounded backoff."""

        state = self._transient_retry_state(event_id)
        attempt = state["attempt"] + 1
        delay = min(
            self.transient_retry_max_seconds,
            self.transient_retry_base_seconds
            * (2 ** min(attempt - 1, 20)),
        )
        retry_state = json.dumps(
            {
                "attempt": attempt,
                "not_before": time.time() + delay,
                "last_error": str(error)[:500],
            },
            sort_keys=True,
        )
        try:
            self.client.set(
                self._transient_retry_key(event_id),
                retry_state,
                ex=self.dedupe_ttl_seconds,
            )
        except Exception as exc:
            raise QaEventBusError(
                f"QA transient retry state could not be stored: {exc}"
            ) from exc

    def _handle_deterministic_failure(
        self,
        *,
        message_id: Any,
        normalized: dict[str, Any],
        event_id: str,
        error: Exception,
    ) -> None:
        if not self.dead_letter_stream:
            raise QaEventPoisonError(str(error)) from error
        self._handle_poison_message(
            message_id=message_id,
            normalized=normalized,
            event_id=event_id,
            error=error,
        )

    def _handle_poison_message(
        self,
        *,
        message_id: Any,
        normalized: dict[str, Any],
        event_id: str,
        error: Exception,
    ) -> None:
        """Retry deterministic failures a bounded number, then dead-letter."""

        failure_key = f"{self.failure_prefix}{event_id}"
        current = self.client.get(failure_key)
        if isinstance(current, bytes):
            current = current.decode()
        try:
            attempt = int(current or 0) + 1
        except (TypeError, ValueError):
            attempt = 1
        self.client.set(
            failure_key,
            str(attempt),
            ex=self.dedupe_ttl_seconds,
        )
        if attempt < self.max_delivery_attempts:
            return

        dlq_fields = {
            "source_stream": self.stream,
            "source_group": self.group,
            "source_message_id": self._message_id_text(message_id),
            "event_id": event_id,
            "event_type": str(normalized.get("event_type", "")),
            "trace_id": str(normalized.get("trace_id", "")),
            "occurred_at": str(normalized.get("occurred_at", "")),
            "payload": json.dumps(
                json_safe(normalized.get("payload", {})), sort_keys=True
            ),
            "delivery_attempts": str(attempt),
            "error": str(error)[:2000],
            "dead_lettered_at": datetime.now().astimezone().isoformat(),
        }
        try:
            self.client.xadd(
                self.dead_letter_stream,
                dlq_fields,
                maxlen=10000,
                approximate=True,
            )
            self.client.xack(self.stream, self.group, message_id)
            if hasattr(self.client, "delete"):
                self.client.delete(
                    failure_key,
                    self._transient_retry_key(event_id),
                )
        except Exception as exc:
            raise QaEventBusError(
                f"QA poison event dead-letter failed: {exc}"
            ) from exc

    def _claim_pending(
        self, *, count: int, min_idle_ms: int
    ) -> list[tuple[Any, dict[Any, Any]]]:
        if not hasattr(self.client, "xautoclaim"):
            return []
        try:
            result = self.client.xautoclaim(
                self.stream,
                self.group,
                self.consumer,
                min_idle_ms,
                start_id="0-0",
                count=count,
            )
        except Exception as exc:
            raise QaEventBusError(
                f"Redis pending-event claim failed: {exc}"
            ) from exc
        # redis-py: (next_id, [(message_id, fields)], deleted_ids)
        return (
            self._flatten([("ignored", result[1])])
            if result and len(result) > 1
            else []
        )

    @staticmethod
    def _flatten(streams: Any) -> list[tuple[Any, dict[Any, Any]]]:
        flattened: list[tuple[Any, dict[Any, Any]]] = []
        for _stream_name, messages in streams or []:
            flattened.extend(messages)
        return flattened

    @staticmethod
    def _normalize_fields(
        fields: dict[Any, Any], *, parse_payload: bool = True
    ) -> dict[str, Any]:
        def text(value: Any) -> str:
            return value.decode() if isinstance(value, bytes) else str(value)

        normalized = {text(key): text(value) for key, value in fields.items()}
        if parse_payload and isinstance(normalized.get("payload"), str):
            normalized["payload"] = json.loads(normalized["payload"])
        return normalized
