#!/usr/bin/env python3
"""Risk↔QA Redis Streams adapter.

소비는 at-least-once다. 처리 성공 후 ACK하고, Event ID를 Redis에 남겨
재시작·중복 전달에서도 handler를 한 번만 실행한다. 영속 DB handler는
동일 Event ID에 대해 추가 멱등키를 가져야 한다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

RISK_DECISION_EVENT = "risk.decision.v1"
QA_DECISION_EVENT = "qa.decision.v1"
DEFAULT_STREAM = "risk-qa-events"
DEFAULT_GROUP = "qa-risk-decision-consumers"
DEFAULT_DEDUPE_TTL_SECONDS = 7 * 24 * 60 * 60


class QaEventBusError(RuntimeError):
    """QA Event Bus 처리 실패."""


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
    ) -> None:
        if dedupe_ttl_seconds < 1:
            raise ValueError("dedupe_ttl_seconds must be positive")
        self.client = client
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.dedupe_prefix = dedupe_prefix
        self.dedupe_ttl_seconds = dedupe_ttl_seconds

    def ensure_group(self) -> None:
        try:
            self.client.xgroup_create(self.stream, self.group, id="0-0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise QaEventBusError(f"Redis Consumer Group 생성 실패: {exc}") from exc

    def publish(
        self,
        *,
        event_id: UUID,
        event_type: str,
        trace_id: UUID,
        payload: dict[str, Any],
    ) -> str:
        try:
            message_id = self.client.xadd(
                self.stream,
                {
                    "event_id": str(event_id),
                    "event_type": event_type,
                    "trace_id": str(trace_id),
                    "occurred_at": datetime.now().astimezone().isoformat(),
                    "payload": json.dumps(json_safe(payload), sort_keys=True),
                },
                maxlen=10000,
                approximate=True,
            )
        except Exception as exc:
            raise QaEventBusError(f"QA Event 발행 실패: {exc}") from exc
        return message_id.decode() if isinstance(message_id, bytes) else str(message_id)

    def consume_once(
        self,
        handler: Callable[[dict[str, Any]], None],
        *,
        count: int = 10,
        min_idle_ms: int = 0,
    ) -> int:
        """Pending과 신규 Event를 최대 count개 처리한다.

        처리 중 예외가 나면 ACK하지 않는다. 다음 실행/재시작에서 다시
        전달되며, DB의 Event ID unique 제약과 함께 중복을 차단한다.
        """

        self.ensure_group()
        messages = self._claim_pending(count=count, min_idle_ms=min_idle_ms)
        if not messages:
            try:
                streams = self.client.xreadgroup(
                    self.group,
                    self.consumer,
                    {self.stream: ">"},
                    count=count,
                    block=0,
                )
            except Exception as exc:
                raise QaEventBusError(f"Redis Event 소비 실패: {exc}") from exc
            messages = self._flatten(streams)

        processed = 0
        for message_id, fields in messages:
            normalized = self._normalize_fields(fields)
            event_id = normalized.get("event_id")
            if not event_id:
                raise QaEventBusError("Event에 event_id가 없습니다")
            dedupe_key = f"{self.dedupe_prefix}{event_id}"
            if self.client.get(dedupe_key):
                self.client.xack(self.stream, self.group, message_id)
                processed += 1
                continue
            event = {
                "event_id": normalized["event_id"],
                "event_type": normalized.get("event_type", ""),
                "trace_id": normalized.get("trace_id", ""),
                "occurred_at": normalized.get("occurred_at", ""),
                "payload": normalized.get("payload", {}),
            }
            handler(event)
            self.client.set(dedupe_key, "1", ex=self.dedupe_ttl_seconds)
            self.client.xack(self.stream, self.group, message_id)
            processed += 1
        return processed

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
            raise QaEventBusError(f"Redis Pending Event 재확보 실패: {exc}") from exc
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
    def _normalize_fields(fields: dict[Any, Any]) -> dict[str, Any]:
        def text(value: Any) -> str:
            return value.decode() if isinstance(value, bytes) else str(value)

        normalized = {text(key): text(value) for key, value in fields.items()}
        if isinstance(normalized.get("payload"), str):
            normalized["payload"] = json.loads(normalized["payload"])
        return normalized
