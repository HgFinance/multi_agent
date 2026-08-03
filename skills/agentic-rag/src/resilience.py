"""Risk/QA Agentic-RAG resilience primitives.

The module is deliberately dependency-light: Redis and structured logging are
optional, while circuit breaking and safe cache misses work in local tests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


LOGGER = logging.getLogger("risk_qa.agentic_rag")
_CIRCUIT_BREAKER_OBSERVERS: list[Callable[[str, str], None]] = []


def register_circuit_breaker_observer(observer: Callable[[str, str], None]) -> None:
    """Register a local metrics sink without making telemetry a hard dependency."""
    if observer not in _CIRCUIT_BREAKER_OBSERVERS:
        _CIRCUIT_BREAKER_OBSERVERS.append(observer)


def _notify_circuit_breaker(name: str, state: str) -> None:
    for observer in tuple(_CIRCUIT_BREAKER_OBSERVERS):
        try:
            observer(name, state)
        except Exception:
            LOGGER.debug("circuit breaker observer failed", exc_info=True)


class CircuitOpenError(RuntimeError):
    """Raised when an external dependency is temporarily fenced off."""


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 3
    recovery_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.failure_threshold < 1 or self.recovery_timeout_seconds <= 0:
            raise ValueError("invalid circuit breaker configuration")
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "CLOSED"
            if time.monotonic() - self._opened_at >= self.recovery_timeout_seconds:
                return "HALF_OPEN"
            return "OPEN"

    def before_call(self) -> None:
        with self._lock:
            if self._opened_at is None:
                return
            elapsed = time.monotonic() - self._opened_at
            if elapsed < self.recovery_timeout_seconds:
                raise CircuitOpenError(f"{self.name} circuit is OPEN")
            if self._half_open:
                raise CircuitOpenError(f"{self.name} circuit is HALF_OPEN")
            self._half_open = True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._half_open = False
        _notify_circuit_breaker(self.name, self.state)

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._half_open = False
            if self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()
        _notify_circuit_breaker(self.name, self.state)

    def call(self, fn: Callable[[], Any]) -> Any:
        self.before_call()
        try:
            result = fn()
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    return str(value)


class RedisJsonCache:
    """Best-effort Redis cache; cache failure never changes a safe decision."""

    def __init__(
        self,
        namespace: str,
        ttl_seconds: int = 7 * 24 * 60 * 60,
        client: Any | None = None,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self.namespace = namespace.strip(":")
        self.ttl_seconds = ttl_seconds
        self._client = client if client is not None else self._build_client()

    @staticmethod
    def _build_client() -> Any | None:
        url = (
            os.environ.get("AGENTIC_RAG_REDIS_URL")
            or os.environ.get("RISK_QA_EVENT_REDIS_URL")
            or os.environ.get("REDIS_URL")
        )
        if not url:
            return None
        try:
            import redis

            return redis.Redis.from_url(url, socket_connect_timeout=2)
        except Exception:
            return None

    def key(self, fingerprint: str) -> str:
        return f"{self.namespace}:{fingerprint}"

    def get(self, fingerprint: str) -> Any | None:
        if self._client is None:
            return None
        try:
            raw = self._client.get(self.key(fingerprint))
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return json.loads(raw)
        except Exception as exc:
            LOGGER.warning("%s", json.dumps({"event": "cache_get_failed", "error": type(exc).__name__}))
            return None

    def set(self, fingerprint: str, value: Any) -> None:
        if self._client is None:
            return
        try:
            self._client.set(
                self.key(fingerprint),
                json.dumps(_json_safe(value), ensure_ascii=False),
                ex=self.ttl_seconds,
            )
        except Exception as exc:
            LOGGER.warning("%s", json.dumps({"event": "cache_set_failed", "error": type(exc).__name__}))

    @staticmethod
    def fingerprint(*parts: Any) -> str:
        payload = json.dumps(_json_safe(parts), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_METRIC_CACHE = RedisJsonCache("risk-qa:rag:metrics", ttl_seconds=30 * 24 * 60 * 60)


def record_latency(node: str, latency_ms: float) -> None:
    client = _METRIC_CACHE._client
    if client is None:
        return
    key = _METRIC_CACHE.key(f"latency:{node}")
    try:
        client.rpush(key, str(float(latency_ms)))
        client.ltrim(key, -1000, -1)
        client.expire(key, _METRIC_CACHE.ttl_seconds)
    except Exception as exc:
        LOGGER.warning("%s", json.dumps({"event": "latency_record_failed", "error": type(exc).__name__}))


def latency_summary(node: str) -> dict[str, float | int]:
    client = _METRIC_CACHE._client
    if client is None:
        return {"count": 0, "p50_ms": 0.0, "p99_ms": 0.0}
    try:
        values = client.lrange(_METRIC_CACHE.key(f"latency:{node}"), 0, -1)
        parsed = sorted(float(v.decode() if isinstance(v, bytes) else v) for v in values)
    except Exception:
        parsed = []
    if not parsed:
        return {"count": 0, "p50_ms": 0.0, "p99_ms": 0.0}
    return {
        "count": len(parsed),
        "p50_ms": round(parsed[int((len(parsed) - 1) * 0.50)], 2),
        "p99_ms": round(parsed[int((len(parsed) - 1) * 0.99)], 2),
    }


def emit_metric(event: str, **fields: Any) -> None:
    """Emit JSON logs without ever logging prompts, credentials, or raw evidence."""

    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **{k: _json_safe(v) for k, v in fields.items()},
    }
    LOGGER.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if "latency_ms" in fields and "node" in fields:
        record_latency(str(fields["node"]), float(fields["latency_ms"]))
