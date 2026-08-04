"""Deterministic RAG routing plan for AI-QA Workers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

RAGRoute = Literal["NO_RAG", "HYBRID", "GRAPH", "HYPERGRAPH"]


@dataclass(frozen=True)
class RAGPlan:
    route: RAGRoute
    methods: tuple[str, ...]
    max_chunks: int
    max_hops: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def choose_rag_route(payload: dict[str, Any], *, worker_id: str = "") -> RAGPlan:
    """Choose a bounded route from explicit signals, not model judgment."""

    explicit = str(payload.get("rag_route", "")).upper()
    if explicit in {"NO_RAG", "HYBRID", "GRAPH", "HYPERGRAPH"}:
        return _plan(explicit, "explicit route requested")
    if payload.get("hypergraph") or payload.get("hyper_extract"):
        return _plan("HYPERGRAPH", "explicit multi-entity extraction signal")

    text = " ".join(
        str(payload.get(key, ""))
        for key in ("query", "question", "claim", "evidence", "assessment")
    ).lower()
    if not text.strip():
        return _plan("NO_RAG", "no retrieval query or evidence signal")

    graph_signal = any(
        marker in text
        for marker in (
            "contradict",
            "unsupported",
            "entity",
            "relationship",
            "citation",
            "dependency",
        )
    )
    if "hallucination" in worker_id or graph_signal:
        return _plan("GRAPH", "claim, citation, or relationship consistency signal")
    return _plan("HYBRID", "bounded semantic and lexical QA context requested")


def _plan(route: RAGRoute, reason: str) -> RAGPlan:
    if route == "NO_RAG":
        return RAGPlan(route, (), 0, 0, reason)
    if route == "HYBRID":
        return RAGPlan(route, ("lexical", "vector", "rerank"), 12, 0, reason)
    if route == "GRAPH":
        return RAGPlan(route, ("lexical", "vector", "entity_link", "graph_context"), 16, 2, reason)
    return RAGPlan(
        route,
        ("lexical", "vector", "entity_link", "hyper_extract", "graph_context"),
        20,
        3,
        reason,
    )
