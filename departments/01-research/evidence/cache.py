"""Task-scoped evidence cache used by Research fetch boundaries."""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from .observability import ResearchRunMetrics, redacted_span, update_span_metadata
except ImportError:  # direct worker/module execution
    from observability import ResearchRunMetrics, redacted_span, update_span_metadata  # type: ignore


def canonical_url(url: str) -> str:
    parts = urlsplit(str(url).strip())
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def content_hash(value: Any) -> str:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def normalized_record_hash(item: Mapping[str, Any]) -> str:
    """Hash stable source-record fields, excluding volatile ranking fields."""
    stable = {
        key: item.get(key)
        for key in (
            "evidence_id",
            "document_id",
            "canonical_url",
            "url",
            "source",
            "title",
            "relation_type",
            "published_at",
            "observed_at",
        )
    }
    return content_hash(stable)


@dataclass(frozen=True)
class EvidenceRecord:
    canonical_url: str
    source_type: str
    fetched_at: str
    content_hash: str
    artifact_ref: str
    fetch_status: str
    value: Any

    def metadata(self) -> dict[str, str]:
        return {
            "canonical_url": self.canonical_url,
            "source_type": self.source_type,
            "fetched_at": self.fetched_at,
            "content_hash": self.content_hash,
            "artifact_ref": self.artifact_ref,
            "fetch_status": self.fetch_status,
        }


class EvidenceCache:
    """In-memory cache whose lifetime is one case/task execution."""

    def __init__(self, *, metrics: ResearchRunMetrics | None = None) -> None:
        self.metrics = metrics
        self._records: dict[tuple[str, str], EvidenceRecord] = {}
        self._hashes: set[str] = set()

    def get(self, url: str, source_type: str) -> EvidenceRecord | None:
        record = self._records.get((canonical_url(url), source_type))
        if record is not None and record.fetch_status == "success":
            with redacted_span(
                "research.evidence.cache_hit",
                run_type="tool",
                metadata={
                    "source_type": source_type,
                    "cache_hit": True,
                    "content_hash": record.content_hash,
                    "status": "success",
                },
                tags=("cache",),
            ):
                if self.metrics:
                    self.metrics.record_cache_hit()
                    self.metrics.record_evidence(record.content_hash, duplicate=True)
            return record
        return None

    def put(
        self,
        url: str,
        source_type: str,
        value: Any,
        *,
        artifact_ref: str | None = None,
        fetch_status: str = "success",
    ) -> EvidenceRecord:
        with redacted_span(
            "research.evidence.normalize_dedup",
            run_type="chain",
            metadata={"source_type": source_type, "status": fetch_status},
            tags=("evidence",),
        ) as span:
            normalized_url = canonical_url(url)
            digest = content_hash(value)
            record = EvidenceRecord(
                canonical_url=normalized_url,
                source_type=source_type,
                fetched_at=datetime.now(timezone.utc).isoformat(),
                content_hash=digest,
                artifact_ref=artifact_ref or f"evidence:{digest[7:23]}",
                fetch_status=fetch_status,
                value=value,
            )
            duplicate = digest in self._hashes
            self._records[(normalized_url, source_type)] = record
            self._hashes.add(digest)
            if self.metrics:
                self.metrics.record_evidence(digest, duplicate=duplicate)
            if span is not None:
                update_span_metadata(span, {"content_hash": digest, "duplicate": duplicate})
            return record

    def metadata(self) -> list[dict[str, str]]:
        return [record.metadata() for record in self._records.values()]


_CURRENT_CACHE: contextvars.ContextVar[EvidenceCache | None] = contextvars.ContextVar(
    "research_evidence_cache", default=None
)


def current_cache() -> EvidenceCache | None:
    return _CURRENT_CACHE.get()


@contextlib.contextmanager
def activate_cache(cache: EvidenceCache):
    token = _CURRENT_CACHE.set(cache)
    try:
        yield cache
    finally:
        _CURRENT_CACHE.reset(token)


__all__ = [
    "EvidenceCache",
    "EvidenceRecord",
    "activate_cache",
    "canonical_url",
    "content_hash",
    "current_cache",
    "normalized_record_hash",
]
