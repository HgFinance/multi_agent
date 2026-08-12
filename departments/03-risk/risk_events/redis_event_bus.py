#!/usr/bin/env python3
"""Risk Decision을 QA가 소비하는 Redis Stream에 발행한다.

이 모듈은 계산이나 승인 권한을 갖지 않는다. Redis 장애는 호출자에게
전달되어 Risk API가 성공 응답을 내지 않도록 한다.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

EVENT_TYPE = "risk.decision.v1"
DEFAULT_STREAM = "risk-qa-events"


class RiskEventBusError(RuntimeError):
    """Risk Event를 발행하지 못한 경우."""


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return value


class RedisEventPublisher:
    """Redis Streams publisher. 연결은 호출 시점에만 수행한다."""

    def __init__(self, client: Any, stream: str = DEFAULT_STREAM) -> None:
        self._client = client
        self._stream = stream

    def publish(
        self, *, event_id: UUID, trace_id: UUID, payload: dict[str, Any]
    ) -> str:
        fields = {
            "event_id": str(event_id),
            "event_type": EVENT_TYPE,
            "trace_id": str(trace_id),
            "occurred_at": datetime.now().astimezone().isoformat(),
            "payload": json.dumps(_json_safe(payload), sort_keys=True),
        }
        try:
            message_id = self._client.xadd(
                self._stream, fields, maxlen=10000, approximate=True
            )
        except Exception as exc:
            raise RiskEventBusError(f"Risk Event 발행 실패: {exc}") from exc
        if isinstance(message_id, bytes):
            return message_id.decode()
        return str(message_id)


def decision_event_id(
    *, risk_request_id: UUID, input_hash: str, calculation_version: str
) -> UUID:
    """동일 판정 재호출에서도 같은 Event ID를 만든다."""

    return uuid5(
        NAMESPACE_URL,
        f"{EVENT_TYPE}:{risk_request_id}:{calculation_version}:{input_hash}",
    )
