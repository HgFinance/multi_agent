"""Deterministic Trading helpers.

The Trading department has no fixed LLM employee registry or employee RAG
policy. Quant strategy workers are immutable, request-scoped deterministic
executors, so Bull/Bear evidence and routing exports are intentionally absent.
"""

from .citations import KNOWN_PREFIXES, apply_citation_checks, verify_refs
from .trigger_payload import DERIVED_TRIGGERS, enrich_payload

__all__ = [
    "KNOWN_PREFIXES",
    "apply_citation_checks",
    "verify_refs",
    "DERIVED_TRIGGERS",
    "enrich_payload",
]
