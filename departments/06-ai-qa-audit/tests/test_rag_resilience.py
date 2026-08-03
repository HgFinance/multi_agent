"""Risk/QA Agentic-RAG resilience contract tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "agentic-rag"))

from src.nodes import should_retry
from src.resilience import CircuitBreaker, CircuitOpenError, RedisJsonCache


class FakeCacheRedis:
    def __init__(self):
        self.values = {}
        self.expirations = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        self.expirations[key] = ex
        return True


def test_circuit_breaker_opens_and_fences_calls():
    breaker = CircuitBreaker("test", failure_threshold=2, recovery_timeout_seconds=60)

    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert breaker.state == "OPEN"
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: "must not run")


def test_circuit_breaker_success_resets_failure_state():
    breaker = CircuitBreaker("test", failure_threshold=2, recovery_timeout_seconds=0.01)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("down")))
    import time

    time.sleep(0.02)
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.state == "CLOSED"


def test_redis_json_cache_uses_ttl_and_round_trips():
    client = FakeCacheRedis()
    cache = RedisJsonCache("test:rag", ttl_seconds=123, client=client)
    fingerprint = cache.fingerprint("persona", "query")
    cache.set(fingerprint, {"decision": "HOLD"})

    assert cache.get(fingerprint) == {"decision": "HOLD"}
    assert client.expirations[cache.key(fingerprint)] == 123


def test_rag_fallback_stops_retry_loop():
    assert should_retry({"fallback": True, "grounded": False, "attempt": 1}) == "done"
