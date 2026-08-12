"""Risk↔QA Redis Stream의 장애·중복·재시작 시나리오."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qa_events.redis_event_bus import QaEventBusError, RedisEventBus


class FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[tuple[str, str], dict[str, str]] = {}
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int | None] = {}
        self.sequence = 0

    def xgroup_create(self, stream, group, id="0-0", mkstream=False):
        self.streams.setdefault(stream, [])
        key = (stream, group)
        if key in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.groups[key] = {}

    def xadd(self, stream, fields, **_kwargs):
        self.sequence += 1
        message_id = f"{self.sequence}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    def xreadgroup(self, group, consumer, streams, count=10, block=0):
        result = []
        for stream, start in streams.items():
            if start != ">":
                continue
            pending = self.groups[(stream, group)]
            messages = []
            for message_id, fields in self.streams.get(stream, []):
                if message_id not in pending:
                    pending[message_id] = consumer
                    messages.append((message_id, fields))
                    if len(messages) >= count:
                        break
            if messages:
                result.append((stream, messages))
        return result

    def xautoclaim(self, stream, group, consumer, _min_idle, start_id="0-0", count=10):
        pending = self.groups[(stream, group)]
        messages = []
        for message_id, owner in list(pending.items()):
            if owner != consumer:
                pending[message_id] = consumer
                fields = next(
                    fields for mid, fields in self.streams[stream] if mid == message_id
                )
                messages.append((message_id, fields))
                if len(messages) >= count:
                    break
        return ("0-0", messages, [])

    def xack(self, stream, group, message_id):
        self.groups[(stream, group)].pop(message_id, None)

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        self.expirations[key] = ex
        return True


def publish(bus: RedisEventBus, event_id=None):
    event_id = event_id or uuid4()
    bus.publish(
        event_id=event_id,
        event_type="risk.decision.v1",
        trace_id=uuid4(),
        payload={"decision": "REJECT"},
    )
    return event_id


def test_duplicate_event_is_handled_once():
    client = FakeRedis()
    bus = RedisEventBus(client, consumer="c1")
    event_id = uuid4()
    publish(bus, event_id)
    publish(bus, event_id)
    received = []

    assert bus.consume_once(received.append) == 2
    assert len(received) == 1
    assert received[0]["event_id"] == str(event_id)


def test_pending_event_is_reclaimed_after_consumer_restart():
    client = FakeRedis()
    first = RedisEventBus(client, consumer="c1")
    publish(first)

    def failed_handler(_event):
        raise RuntimeError("simulated QA worker crash")

    with pytest.raises(RuntimeError):
        first.consume_once(failed_handler)

    restarted = RedisEventBus(client, consumer="c2")
    received = []
    assert restarted.consume_once(received.append) == 1
    assert len(received) == 1


def test_redis_failure_does_not_ack_or_report_success():
    class BrokenRedis(FakeRedis):
        def xreadgroup(self, *args, **kwargs):
            raise OSError("Redis unavailable")

    bus = RedisEventBus(BrokenRedis(), consumer="c1")
    with pytest.raises(QaEventBusError):
        bus.consume_once(lambda _event: None)


def test_bytes_response_fields_are_normalized():
    normalized = RedisEventBus._normalize_fields(
        {
            b"event_id": b"event-1",
            b"event_type": b"risk.decision.v1",
            b"payload": b'{"decision":"REJECT"}',
        }
    )

    assert normalized == {
        "event_id": "event-1",
        "event_type": "risk.decision.v1",
        "payload": {"decision": "REJECT"},
    }


def test_dedupe_key_uses_bounded_ttl():
    client = FakeRedis()
    bus = RedisEventBus(client, consumer="c1", dedupe_ttl_seconds=123)
    publish(bus)

    bus.consume_once(lambda _event: None)

    assert list(client.expirations.values()) == [123]
