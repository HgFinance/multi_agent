#!/usr/bin/env python3
"""F24 Notification: 다른 본부의 Domain Event를 소비하는 Redis Streams adapter.

담당: 영주 (CEO Office)
근거: docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md 3.4절(Redis Streams +
      Transactional Outbox), 8.2절(Event Envelope), 8.3절(Stream 분리 - hf:governance),
      departments/06-ai-qa-audit/qa_events/redis_event_bus.py 패턴(risk_events와 동일 컨벤션).

이 모듈은 판정 로직이 없다. Redis Stream Publish/Consume 배관만 한다 - 실제로 알림을
만들지 여부는 notification.py의 NotificationService가 결정한다 (CLAUDE.md "LLM은 관련성
판단과 서술 작성에만 쓴다"와 같은 원칙: 여기 Bus도 규칙 판정 없이 전달만 한다).

소비는 at-least-once다. 처리 성공 후에만 ACK하고, Event ID를 Redis에 남겨 재시작·중복
전달에서도 handler를 한 번만 실행한다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

DEFAULT_STREAM = "hf:governance"
DEFAULT_GROUP = "governance-notification-consumers"
DEFAULT_DEDUPE_TTL_SECONDS = 7 * 24 * 60 * 60


class GovernanceEventBusError(RuntimeError):
    """Governance Event Bus 처리 실패."""


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
    """Redis Stream publisher/consumer with restart-safe group handling.

    qa_events/risk_events의 RedisEventBus와 동일 구현이다 - 공용 패키지로 아직 안 뽑은 이유는
    각 본부가 Image·의존성을 독립적으로 소유한다는 원칙(계획서 3.2 "공통 계약: 다른 본부의
    Python Module을 Import하지 않는다") 때문이다. 이 클래스가 부서 3곳에서 같은 모양으로
    복제된 것 자체가 "공용 Contract Package로 뽑아도 되는 후보"라는 신호이지만, 지금 임의로
    새 공유 패키지를 만들지 않는다(다른 부서 소유 코드 구조를 이 세션에서 바꾸지 않는다).
    """

    def __init__(
        self,
        client: Any,
        *,
        stream: str = DEFAULT_STREAM,
        group: str = DEFAULT_GROUP,
        consumer: str = "governance-api",
        dedupe_prefix: str = "governance:event-processed:",
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
                raise GovernanceEventBusError(f"Redis Consumer Group 생성 실패: {exc}") from exc

    def publish(self, *, event_id: UUID, event_type: str, trace_id: UUID, payload: dict[str, Any]) -> str:
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
            raise GovernanceEventBusError(f"Governance Event 발행 실패: {exc}") from exc
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
        전달되며, dedupe Key로 handler 중복 실행을 막는다.
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
                raise GovernanceEventBusError(f"Redis Event 소비 실패: {exc}") from exc
            messages = self._flatten(streams)

        processed = 0
        for message_id, fields in messages:
            normalized = self._normalize_fields(fields)
            event_id = normalized.get("event_id")
            if not event_id:
                raise GovernanceEventBusError("Event에 event_id가 없습니다")
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

    def _claim_pending(self, *, count: int, min_idle_ms: int) -> list[tuple[Any, dict[Any, Any]]]:
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
            raise GovernanceEventBusError(f"Redis Pending Event 재확보 실패: {exc}") from exc
        # redis-py: (next_id, [(message_id, fields)], deleted_ids)
        return self._flatten([("ignored", result[1])]) if result and len(result) > 1 else []

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
