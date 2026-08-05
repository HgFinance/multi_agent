"""Risk Event 발행의 멱등 식별자와 Redis 장애 처리."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from risk_events.redis_event_bus import (
    RedisEventPublisher,
    RiskEventBusError,
    decision_event_id,
)


class RecordingRedis:
    def __init__(self) -> None:
        self.calls = []

    def xadd(self, stream, fields, **kwargs):
        self.calls.append((stream, fields, kwargs))
        return b"1-0"


def test_same_decision_has_stable_event_id():
    request_id = uuid4()
    first = decision_event_id(
        risk_request_id=request_id,
        input_hash="abc",
        calculation_version="risk-p0-v1",
    )
    second = decision_event_id(
        risk_request_id=request_id,
        input_hash="abc",
        calculation_version="risk-p0-v1",
    )
    assert first == second


def test_publisher_emits_versioned_event():
    client = RecordingRedis()
    publisher = RedisEventPublisher(client)
    event_id = uuid4()
    publisher.publish(
        event_id=event_id, trace_id=uuid4(), payload={"decision": "REJECT"}
    )
    assert client.calls[0][0] == "risk-qa-events"
    assert client.calls[0][1]["event_type"] == "risk.decision.v1"
    assert client.calls[0][1]["event_id"] == str(event_id)


def test_redis_failure_is_fail_closed():
    class BrokenRedis:
        def xadd(self, *_args, **_kwargs):
            raise OSError("Redis unavailable")

    with pytest.raises(RiskEventBusError):
        RedisEventPublisher(BrokenRedis()).publish(
            event_id=uuid4(), trace_id=uuid4(), payload={"decision": "REJECT"}
        )
