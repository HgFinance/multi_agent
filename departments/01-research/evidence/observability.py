"""Small, in-process Research latency and evidence metrics.

This module deliberately has no exporter or new observability dependency.  A
single Research task owns one metrics object; callers may serialize
``as_dict()`` into the existing task/artifact metadata.
"""

from __future__ import annotations

import contextlib
import contextvars
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass
class ResearchRunMetrics:
    """Bounded, JSON-safe metrics for one Research run."""

    trace_id: str | None = None
    queued_at: datetime | None = None
    claimed_at: datetime | None = None
    research_started_at: datetime | None = None
    evidence_started_at: datetime | None = None
    evidence_finished_at: datetime | None = None
    generation_started_at: datetime | None = None
    generation_finished_at: datetime | None = None
    completed_at: datetime | None = None
    total_duration_ms: int = 0
    dispatch_wait_ms: int = 0
    evidence_collection_duration_ms: int = 0
    llm_duration_ms: int = 0
    tool_call_count: int = 0
    network_fetch_count: int = 0
    cache_hit_count: int = 0
    retry_count: int = 0
    fallback_count: int = 0
    unique_evidence_count: int = 0
    duplicate_evidence_avoided_count: int = 0
    _started_perf: float = field(default_factory=time.perf_counter, repr=False)
    _stage_started: dict[str, float] = field(default_factory=dict, repr=False)
    _evidence_hashes: set[str] = field(default_factory=set, repr=False)

    def mark(self, name: str, value: datetime | None = None) -> datetime:
        timestamp = value or _now()
        if name == "queued_at":
            self.queued_at = timestamp
        elif name == "claimed_at":
            self.claimed_at = timestamp
        elif name == "research_started_at":
            self.research_started_at = timestamp
        elif name == "evidence_started_at":
            self.evidence_started_at = timestamp
        elif name == "evidence_finished_at":
            self.evidence_finished_at = timestamp
        elif name == "generation_started_at":
            self.generation_started_at = timestamp
        elif name == "generation_finished_at":
            self.generation_finished_at = timestamp
        elif name == "completed_at":
            self.completed_at = timestamp
        else:
            raise ValueError(f"unknown Research metric timestamp: {name}")
        if self.queued_at and self.claimed_at:
            self.dispatch_wait_ms = max(
                0, int((self.claimed_at - self.queued_at).total_seconds() * 1000)
            )
        return timestamp

    @contextlib.contextmanager
    def span(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        self._stage_started[name] = started
        if name == "evidence_collection" and self.evidence_started_at is None:
            self.mark("evidence_started_at")
        if name == "generation" and self.generation_started_at is None:
            self.mark("generation_started_at")
        try:
            yield
        finally:
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            if name == "evidence_collection":
                self.evidence_collection_duration_ms += elapsed_ms
                self.mark("evidence_finished_at")
            elif name == "generation":
                self.llm_duration_ms += elapsed_ms
                self.mark("generation_finished_at")

    def record_tool_call(self) -> None:
        self.tool_call_count += 1

    def record_network_fetch(self) -> None:
        self.network_fetch_count += 1

    def record_cache_hit(self) -> None:
        self.cache_hit_count += 1

    def record_retry(self) -> None:
        self.retry_count += 1

    def record_fallback(self) -> None:
        self.fallback_count += 1

    def record_evidence(self, content_hash: str, *, duplicate: bool = False) -> None:
        if content_hash in self._evidence_hashes or duplicate:
            self.duplicate_evidence_avoided_count += 1
            return
        self._evidence_hashes.add(content_hash)
        self.unique_evidence_count = len(self._evidence_hashes)

    def finish(self, *, completed_at: datetime | None = None) -> None:
        self.mark("completed_at", completed_at)
        self.total_duration_ms = max(
            0, int((self.completed_at - (self.research_started_at or self.completed_at)).total_seconds() * 1000)
        )
        if not self.total_duration_ms:
            self.total_duration_ms = max(0, int((time.perf_counter() - self._started_perf) * 1000))

    def as_dict(self, *, status: str | None = None, error: str | None = None) -> dict[str, Any]:
        if self.completed_at is None:
            self.finish()
        return {
            "schema_version": "research.observability.v1",
            "trace_id": self.trace_id,
            "status": status,
            "error": error,
            "total_duration_ms": self.total_duration_ms,
            "dispatch_wait_ms": self.dispatch_wait_ms,
            "evidence_collection_duration_ms": self.evidence_collection_duration_ms,
            "llm_duration_ms": self.llm_duration_ms,
            "tool_call_count": self.tool_call_count,
            "network_fetch_count": self.network_fetch_count,
            "cache_hit_count": self.cache_hit_count,
            "retry_count": self.retry_count,
            "fallback_count": self.fallback_count,
            "unique_evidence_count": self.unique_evidence_count,
            "duplicate_evidence_avoided_count": self.duplicate_evidence_avoided_count,
            "queued_at": _iso(self.queued_at),
            "claimed_at": _iso(self.claimed_at),
            "research_started_at": _iso(self.research_started_at),
            "evidence_started_at": _iso(self.evidence_started_at),
            "evidence_finished_at": _iso(self.evidence_finished_at),
            "generation_started_at": _iso(self.generation_started_at),
            "generation_finished_at": _iso(self.generation_finished_at),
            "completed_at": _iso(self.completed_at),
        }


_CURRENT_METRICS: contextvars.ContextVar[ResearchRunMetrics | None] = contextvars.ContextVar(
    "research_run_metrics", default=None
)


def current_metrics() -> ResearchRunMetrics | None:
    return _CURRENT_METRICS.get()


@contextlib.contextmanager
def activate_metrics(metrics: ResearchRunMetrics) -> Iterator[ResearchRunMetrics]:
    token = _CURRENT_METRICS.set(metrics)
    try:
        yield metrics
    finally:
        _CURRENT_METRICS.reset(token)


__all__ = ["ResearchRunMetrics", "activate_metrics", "current_metrics"]
