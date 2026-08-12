"""Research evidence handoff read model for Risk and QA consumers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

try:
    from .cache import normalized_record_hash
except ImportError:  # direct worker/module execution
    from cache import normalized_record_hash  # type: ignore


def _record_content_hash(item: Mapping[str, Any]) -> str:
    """Hash the normalized evidence record when raw bytes are unavailable."""
    supplied = item.get("content_hash")
    if supplied:
        return str(supplied)
    return normalized_record_hash(item)


def build_evidence_handoff(
    bundle: Mapping[str, Any],
    *,
    cache_metadata: list[Mapping[str, Any]] | None = None,
    trace_id: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Build additive provenance metadata without changing ResearchPacketV2."""

    refs: list[str] = []
    provenance: list[dict[str, Any]] = []
    for key, source_type in (("news_headlines", "news"), ("disclosures_7d", "disclosure")):
        for item in bundle.get(key, []) or []:
            if not isinstance(item, Mapping):
                continue
            evidence_id = str(item.get("evidence_id") or "").strip()
            if not evidence_id or evidence_id in refs:
                continue
            refs.append(evidence_id)
            provenance.append(
                {
                    "evidence_ref": evidence_id,
                    "canonical_url": item.get("canonical_url") or item.get("url"),
                    "source_type": item.get("source_type") or source_type,
                    "content_hash": _record_content_hash(item),
                    "content_hash_scope": "source_record",
                    "fetched_at": item.get("fetched_at"),
                    "artifact_ref": item.get("artifact_ref") or evidence_id,
                }
            )
    for item in cache_metadata or []:
        if not isinstance(item, Mapping):
            continue
        artifact_ref = str(item.get("artifact_ref") or "").strip()
        if artifact_ref and artifact_ref not in {str(p.get("artifact_ref")) for p in provenance}:
            provenance.append(dict(item))
    return {
        "schema_version": "research.evidence-handoff.v1",
        "trace_id": trace_id,
        "as_of": as_of,
        "evidence_refs": refs,
        "provenance": provenance,
        "reuse_policy": {
            "reuse_if": ["artifact_present", "content_hash_matches", "provenance_valid", "fresh_enough"],
            "refetch_if": ["stale", "artifact_missing", "artifact_corrupt", "provenance_invalid"],
        },
    }


def reusable_evidence_refs(
    handoff: Mapping[str, Any],
    *,
    as_of: str | None = None,
    freshness_seconds: int | None = None,
) -> tuple[str, ...]:
    """Return validated refs for Risk/QA input_refs; never silently invent refs."""

    refs = handoff.get("evidence_refs")
    provenance = handoff.get("provenance")
    if not isinstance(refs, list) or not isinstance(provenance, list):
        return ()
    cutoff = None
    if freshness_seconds is not None:
        if as_of:
            try:
                parsed_cutoff = datetime.fromisoformat(as_of)
            except ValueError:
                return ()
            if parsed_cutoff.tzinfo is None:
                return ()
            cutoff = parsed_cutoff.astimezone(timezone.utc)
        else:
            cutoff = datetime.now(timezone.utc)

    valid: set[str] = set()
    for item in provenance:
        if not isinstance(item, Mapping):
            continue
        if not item.get("evidence_ref") or not item.get("artifact_ref") or not item.get("content_hash"):
            continue
        if item.get("fetch_status") not in (None, "success"):
            continue
        if cutoff is not None:
            fetched_at = item.get("fetched_at")
            if not fetched_at:
                continue
            try:
                parsed_fetched = datetime.fromisoformat(str(fetched_at))
            except ValueError:
                continue
            if parsed_fetched.tzinfo is None:
                continue
            fetched = parsed_fetched.astimezone(timezone.utc)
            if (cutoff - fetched).total_seconds() > freshness_seconds:
                continue
        valid.add(str(item["evidence_ref"]))
    return tuple(ref for ref in dict.fromkeys(str(ref) for ref in refs) if ref in valid)


__all__ = ["build_evidence_handoff", "reusable_evidence_refs"]
