"""실 Redis Streams에서 Risk→QA 중복/재시작 시나리오를 검증한다.

REDIS_URL이 없거나 Redis가 응답하지 않으면 외부 인프라 의존 테스트이므로 skip한다.
장애 시 ACK하지 않는 순수 로직은 test_redis_event_bus.py의 격리 테스트에서 검증한다.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

redis = pytest.importorskip("redis")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "agentic-rag"))

from qa_events.redis_event_bus import RedisEventBus
from src.resilience import RedisJsonCache


@pytest.fixture()
def redis_bus():
    redis_url = os.environ.get("RISK_QA_EVENT_REDIS_URL") or os.environ.get("REDIS_URL")
    if not redis_url:
        pytest.skip("RISK_QA_EVENT_REDIS_URL or REDIS_URL is not configured")

    client = redis.Redis.from_url(
        redis_url, socket_connect_timeout=2, decode_responses=True
    )
    try:
        client.ping()
    except redis.RedisError as exc:
        client.close()
        pytest.skip(f"Redis is unavailable: {exc}")

    stream = f"risk-qa-integration-{uuid4().hex}"
    group = f"qa-integration-{uuid4().hex}"
    yield client, stream, group

    try:
        client.delete(stream)
    finally:
        client.close()


def _publish(bus: RedisEventBus, event_id):
    bus.publish(
        event_id=event_id,
        event_type="risk.decision.v1",
        trace_id=uuid4(),
        payload={"decision": "REJECT", "source": "integration-test"},
    )


def test_real_redis_duplicate_event_is_processed_once(redis_bus):
    client, stream, group = redis_bus
    bus = RedisEventBus(client, stream=stream, group=group, consumer="qa-1")
    event_id = uuid4()
    _publish(bus, event_id)
    _publish(bus, event_id)

    received = []
    assert bus.consume_once(received.append, count=10, min_idle_ms=0) == 2
    assert [event["event_id"] for event in received] == [str(event_id)]
    dedupe_ttl = client.ttl(f"risk-qa:event-processed:{event_id}")
    assert 0 < dedupe_ttl <= 7 * 24 * 60 * 60


def test_real_redis_pending_event_is_reclaimed_after_restart(redis_bus):
    client, stream, group = redis_bus
    first = RedisEventBus(
        client,
        stream=stream,
        group=group,
        consumer="qa-1",
        transient_retry_base_seconds=0.01,
    )
    _publish(first, uuid4())

    def failed_handler(_event):
        raise RuntimeError("simulated QA worker restart")

    assert first.consume_once(failed_handler, min_idle_ms=0) == 0
    time.sleep(0.02)

    restarted = RedisEventBus(client, stream=stream, group=group, consumer="qa-2")
    received = []
    assert restarted.consume_once(received.append, min_idle_ms=0) == 1
    assert len(received) == 1


def test_real_redis_rag_cache_round_trip_has_ttl(redis_bus):
    client, _stream, _group = redis_bus
    cache = RedisJsonCache(
        f"risk-qa-rag-integration-{uuid4().hex}", ttl_seconds=60, client=client
    )
    cache.set("prompt-fingerprint", {"decision": "HOLD"})

    assert cache.get("prompt-fingerprint") == {"decision": "HOLD"}
    assert 0 < client.ttl(cache.key("prompt-fingerprint")) <= 60
    client.delete(cache.key("prompt-fingerprint"))
