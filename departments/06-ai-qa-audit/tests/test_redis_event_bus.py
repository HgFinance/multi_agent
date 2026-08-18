"""Risk↔QA Redis Stream의 장애·중복·재시작 시나리오."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qa_events.redis_event_bus as event_bus_module
from qa_events.redis_event_bus import (
    FORWARD_QA_DLQ_STREAM,
    FORWARD_QA_GROUP,
    FORWARD_QA_STREAM,
    RISK_QA_DLQ_STREAM,
    QaEventBusError,
    QaEventPoisonError,
    RedisEventBus,
)


class FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[tuple[str, str], dict[str, str]] = {}
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int | None] = {}
        self.sequence = 0
        self.last_read_block = "unset"
        self.xadd_kwargs: list[dict] = []

    def xgroup_create(self, stream, group, id="0-0", mkstream=False):
        self.streams.setdefault(stream, [])
        key = (stream, group)
        if key in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.groups[key] = {}

    def xadd(self, stream, fields, **kwargs):
        self.xadd_kwargs.append(dict(kwargs))
        self.sequence += 1
        message_id = f"{self.sequence}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    def eval(self, _script, numkeys, *args):
        assert numkeys == 2
        marker_key, stream = args[:2]
        argv = args[2:]
        existing = self.values.get(marker_key)
        if existing is not None:
            return existing
        maxlen = argv[0]
        fields = dict(zip(argv[1::2], argv[2::2]))
        kwargs = (
            {"maxlen": int(maxlen), "approximate": True}
            if maxlen
            else {}
        )
        message_id = self.xadd(stream, fields, **kwargs)
        self.set(marker_key, message_id)
        return message_id

    def xreadgroup(self, group, consumer, streams, count=10, block=0):
        self.last_read_block = block
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
        for message_id in list(pending):
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

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            deleted += int(self.values.pop(key, None) is not None)
            self.expirations.pop(key, None)
        return deleted


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


def test_empty_stream_poll_is_nonblocking_by_default():
    client = FakeRedis()
    bus = RedisEventBus(client, consumer="c1")

    assert bus.consume_once(lambda _event: None) == 0
    assert client.last_read_block is None

    with pytest.raises(ValueError, match="positive or None"):
        bus.consume_once(lambda _event: None, block_ms=0)


def test_stream_retention_is_explicit_and_forward_can_be_unbounded():
    bounded_client = FakeRedis()
    bounded = RedisEventBus(bounded_client, consumer="bounded")
    publish(bounded)
    assert bounded_client.xadd_kwargs[-1] == {
        "maxlen": 10000,
        "approximate": True,
    }

    forward_client = FakeRedis()
    forward = RedisEventBus(
        forward_client,
        stream=FORWARD_QA_STREAM,
        group=FORWARD_QA_GROUP,
        consumer="forward",
        stream_maxlen=None,
    )
    publish(forward)
    assert forward_client.xadd_kwargs[-1] == {}


def test_forward_publish_is_atomic_and_idempotent_by_event_id():
    client = FakeRedis()
    bus = RedisEventBus(
        client,
        stream=FORWARD_QA_STREAM,
        group=FORWARD_QA_GROUP,
        stream_maxlen=None,
    )
    event_id = uuid4()
    event = {
        "event_id": event_id,
        "event_type": "quant.intraday.forward.qa_requested.v1",
        "trace_id": uuid4(),
        "payload": {"decision": "PASS"},
        "idempotency_key": event_id,
    }

    first = bus.publish(**event)
    second = bus.publish(**event)

    assert second == first
    assert len(client.streams[FORWARD_QA_STREAM]) == 1
    assert len(client.xadd_kwargs) == 1


def test_pending_event_is_reclaimed_after_consumer_restart(monkeypatch):
    now = [500.0]
    monkeypatch.setattr(event_bus_module.time, "time", lambda: now[0])
    client = FakeRedis()
    first = RedisEventBus(client, consumer="c1")
    publish(first)

    def failed_handler(_event):
        raise RuntimeError("simulated QA worker crash")

    assert first.consume_once(failed_handler) == 0
    now[0] += 1

    restarted = RedisEventBus(client, consumer="c2")
    received = []
    assert restarted.consume_once(received.append) == 1
    assert len(received) == 1


def test_transient_database_outage_exceeds_poison_budget_then_recovers(
    monkeypatch,
):
    now = [1000.0]
    monkeypatch.setattr(event_bus_module.time, "time", lambda: now[0])
    client = FakeRedis()
    bus = RedisEventBus(
        client,
        stream=FORWARD_QA_STREAM,
        group=FORWARD_QA_GROUP,
        consumer="forward",
        dead_letter_stream=FORWARD_QA_DLQ_STREAM,
        max_delivery_attempts=2,
        failure_prefix="forward:failures:",
        transient_retry_base_seconds=2,
        transient_retry_max_seconds=8,
    )
    event_id = publish(bus)
    calls = []

    def database_down(_event):
        calls.append(now[0])
        raise RuntimeError("metadata database unavailable")

    for attempt in range(1, 8):
        assert bus.consume_once(database_down) == 0
        retry_key = f"forward:failures:transient:{event_id}"
        state = json.loads(client.values[retry_key])
        assert state["attempt"] == attempt
        assert client.groups[(FORWARD_QA_STREAM, FORWARD_QA_GROUP)]
        assert client.streams.get(FORWARD_QA_DLQ_STREAM, []) == []
        now[0] = state["not_before"]

    received = []
    assert bus.consume_once(received.append) == 1
    assert [item["event_id"] for item in received] == [str(event_id)]
    assert client.groups[(FORWARD_QA_STREAM, FORWARD_QA_GROUP)] == {}
    assert f"forward:failures:transient:{event_id}" not in client.values
    assert len(calls) == 7


def test_transient_backoff_skips_handler_until_retry_is_due(monkeypatch):
    now = [2000.0]
    monkeypatch.setattr(event_bus_module.time, "time", lambda: now[0])
    client = FakeRedis()
    bus = RedisEventBus(
        client,
        consumer="c1",
        transient_retry_base_seconds=10,
        transient_retry_max_seconds=10,
    )
    event_id = publish(bus)
    calls = []

    def database_down(_event):
        calls.append(True)
        raise RuntimeError("database unavailable")

    assert bus.consume_once(database_down) == 0
    assert bus.consume_once(database_down) == 0
    assert calls == [True]
    assert client.groups[(bus.stream, bus.group)]
    assert f"{bus.failure_prefix}{event_id}" not in client.values


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


def test_forward_qa_poison_event_is_retried_then_dead_lettered():
    client = FakeRedis()
    first = RedisEventBus(
        client,
        stream=FORWARD_QA_STREAM,
        group=FORWARD_QA_GROUP,
        consumer="c1",
        dead_letter_stream=FORWARD_QA_DLQ_STREAM,
        max_delivery_attempts=2,
        failure_prefix="forward:failures:",
    )
    event_id = publish(first)

    def poison(_event):
        raise ValueError("bad canonical payload")

    assert first.consume_once(poison) == 0
    assert client.groups[(FORWARD_QA_STREAM, FORWARD_QA_GROUP)]

    restarted = RedisEventBus(
        client,
        stream=FORWARD_QA_STREAM,
        group=FORWARD_QA_GROUP,
        consumer="c2",
        dead_letter_stream=FORWARD_QA_DLQ_STREAM,
        max_delivery_attempts=2,
        failure_prefix="forward:failures:",
    )
    assert restarted.consume_once(poison) == 0
    assert client.groups[(FORWARD_QA_STREAM, FORWARD_QA_GROUP)] == {}
    assert len(client.streams[FORWARD_QA_DLQ_STREAM]) == 1
    dlq = client.streams[FORWARD_QA_DLQ_STREAM][0][1]
    assert dlq["event_id"] == str(event_id)
    assert dlq["delivery_attempts"] == "2"
    assert "bad canonical payload" in dlq["error"]


def test_malformed_json_is_bounded_then_dead_lettered():
    client = FakeRedis()
    bus = RedisEventBus(
        client,
        stream=FORWARD_QA_STREAM,
        group=FORWARD_QA_GROUP,
        consumer="forward",
        dead_letter_stream=FORWARD_QA_DLQ_STREAM,
        max_delivery_attempts=2,
        failure_prefix="forward:failures:",
    )
    event_id = uuid4()
    client.xadd(
        FORWARD_QA_STREAM,
        {
            "event_id": str(event_id),
            "event_type": "quant.intraday.forward.qa_requested.v1",
            "trace_id": str(uuid4()),
            "payload": "{not-json",
        },
    )

    assert bus.consume_once(lambda _event: None) == 0
    assert bus.consume_once(lambda _event: None) == 0

    assert client.groups[(FORWARD_QA_STREAM, FORWARD_QA_GROUP)] == {}
    assert len(client.streams[FORWARD_QA_DLQ_STREAM]) == 1
    dlq = client.streams[FORWARD_QA_DLQ_STREAM][0][1]
    assert dlq["event_id"] == str(event_id)
    assert dlq["delivery_attempts"] == "2"
    assert "malformed" in dlq["error"]


def test_explicit_poison_error_is_deterministic():
    client = FakeRedis()
    bus = RedisEventBus(
        client,
        stream=FORWARD_QA_STREAM,
        group=FORWARD_QA_GROUP,
        consumer="forward",
        dead_letter_stream=FORWARD_QA_DLQ_STREAM,
        max_delivery_attempts=1,
    )
    publish(bus)

    def poison(_event):
        raise QaEventPoisonError("immutable contract conflict")

    assert bus.consume_once(poison) == 0
    assert len(client.streams[FORWARD_QA_DLQ_STREAM]) == 1


def test_primary_bus_factory_configures_bounded_poison_dlq(monkeypatch):
    from api import app as qa_app

    configured = {}

    class CapturingBus:
        def __init__(self, client, **kwargs):
            configured["client"] = client
            configured.update(kwargs)

    redis_client = object()
    redis_module = SimpleNamespace(
        Redis=SimpleNamespace(from_url=lambda _url: redis_client)
    )
    monkeypatch.setitem(sys.modules, "redis", redis_module)
    monkeypatch.setattr(qa_app, "RedisEventBus", CapturingBus)
    monkeypatch.setattr(qa_app, "_event_bus", None)
    monkeypatch.setenv("RISK_QA_EVENT_REDIS_URL", "redis://qa-test")
    monkeypatch.setenv("RISK_QA_EVENT_MAX_DELIVERIES", "3")
    monkeypatch.delenv("RISK_QA_EVENT_DLQ_STREAM", raising=False)

    assert isinstance(qa_app._qa_event_bus(), CapturingBus)
    assert configured["client"] is redis_client
    assert configured["dead_letter_stream"] == RISK_QA_DLQ_STREAM
    assert configured["max_delivery_attempts"] == 3
    assert configured["failure_prefix"] == "risk-qa:event-failures:"


@pytest.mark.parametrize("poison_kind", ["malformed", "unsupported"])
def test_primary_poison_does_not_starve_forward_qa(poison_kind):
    from api import app as qa_app

    client = FakeRedis()
    risk_bus = RedisEventBus(
        client,
        consumer="risk-worker",
        dead_letter_stream=RISK_QA_DLQ_STREAM,
        max_delivery_attempts=2,
    )
    risk_event_id = uuid4()
    if poison_kind == "malformed":
        client.xadd(
            risk_bus.stream,
            {
                "event_id": str(risk_event_id),
                "event_type": "risk.decision.v1",
                "trace_id": str(uuid4()),
                "payload": "{not-json",
            },
        )
    else:
        risk_bus.publish(
            event_id=risk_event_id,
            event_type="risk.unsupported.v1",
            trace_id=uuid4(),
            payload={"immutable": True},
        )

    forward_bus = RedisEventBus(
        client,
        stream=FORWARD_QA_STREAM,
        group=FORWARD_QA_GROUP,
        consumer="forward-worker",
        dead_letter_stream=FORWARD_QA_DLQ_STREAM,
        max_delivery_attempts=2,
        stream_maxlen=None,
    )
    forward_event_id = publish(forward_bus)
    forwarded = []

    # Mirror the worker's risk-then-forward polling order.  A deterministic
    # primary-stream failure must return control so the forward lane still runs.
    assert risk_bus.consume_once(qa_app._record_risk_event) == 0
    assert forward_bus.consume_once(forwarded.append) == 1
    assert [event["event_id"] for event in forwarded] == [str(forward_event_id)]
    assert client.groups[(risk_bus.stream, risk_bus.group)]

    assert risk_bus.consume_once(qa_app._record_risk_event) == 0
    assert client.groups[(risk_bus.stream, risk_bus.group)] == {}
    assert len(client.streams[RISK_QA_DLQ_STREAM]) == 1
    assert client.streams[RISK_QA_DLQ_STREAM][0][1]["event_id"] == str(
        risk_event_id
    )


def test_api_handler_unsupported_event_uses_bounded_dlq():
    from api import app as qa_app

    client = FakeRedis()
    bus = RedisEventBus(
        client,
        stream=FORWARD_QA_STREAM,
        group=FORWARD_QA_GROUP,
        consumer="forward",
        dead_letter_stream=FORWARD_QA_DLQ_STREAM,
        max_delivery_attempts=1,
    )
    bus.publish(
        event_id=uuid4(),
        event_type="quant.unsupported.v1",
        trace_id=uuid4(),
        payload={"immutable": True},
    )

    assert bus.consume_once(qa_app._record_risk_event) == 0
    assert client.groups[(FORWARD_QA_STREAM, FORWARD_QA_GROUP)] == {}
    assert len(client.streams[FORWARD_QA_DLQ_STREAM]) == 1
    assert "unsupported event" in client.streams[FORWARD_QA_DLQ_STREAM][0][1][
        "error"
    ]


def test_api_handler_database_unavailable_remains_pending(monkeypatch):
    from api import app as qa_app

    monkeypatch.setattr(qa_app, "_audit_repository", None)
    client = FakeRedis()
    bus = RedisEventBus(
        client,
        stream=FORWARD_QA_STREAM,
        group=FORWARD_QA_GROUP,
        consumer="forward",
        dead_letter_stream=FORWARD_QA_DLQ_STREAM,
        max_delivery_attempts=1,
    )
    event_id = uuid4()
    bus.publish(
        event_id=event_id,
        event_type="risk.decision.v1",
        trace_id=uuid4(),
        payload={"decision": "REJECT"},
    )

    assert bus.consume_once(qa_app._record_risk_event) == 0
    assert client.groups[(FORWARD_QA_STREAM, FORWARD_QA_GROUP)]
    assert client.streams.get(FORWARD_QA_DLQ_STREAM, []) == []
    assert f"{bus.failure_prefix}transient:{event_id}" in client.values
