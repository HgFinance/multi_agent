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


@dataclass(frozen=True)
class WorkerRAGPolicy:
    allowed_routes: frozenset[RAGRoute]
    forced_route: RAGRoute | None = None


WORKER_RAG_POLICIES: dict[str, WorkerRAGPolicy] = {
    "evidence-qa-worker": WorkerRAGPolicy(frozenset({"NO_RAG", "HYBRID", "GRAPH"})),
    "hallucination-critic-worker": WorkerRAGPolicy(
        frozenset({"NO_RAG", "HYBRID", "GRAPH"})
    ),
    "model-and-internal-audit-worker": WorkerRAGPolicy(
        frozenset({"NO_RAG", "GRAPH"})
    ),
    "ops-and-permission-worker": WorkerRAGPolicy(frozenset({"NO_RAG"}), "NO_RAG"),
    "incident-postmortem-worker": WorkerRAGPolicy(
        frozenset({"NO_RAG", "HYBRID", "GRAPH", "HYPERGRAPH"})
    ),
}


def rag_policy_for_worker(worker_id: str) -> WorkerRAGPolicy:
    try:
        return WORKER_RAG_POLICIES[worker_id]
    except KeyError as exc:
        raise ValueError(f"unknown QA Worker RAG policy: {worker_id}") from exc


def _plan(route: RAGRoute, reason: str) -> RAGPlan:
    if route == "NO_RAG":
        return RAGPlan(route, (), 0, 0, reason)
    if route == "HYBRID":
        return RAGPlan(route, ("lexical", "vector", "rerank"), 12, 0, reason)
    if route == "GRAPH":
        return RAGPlan(
            route,
            ("lexical", "vector", "entity_link", "graph_context"),
            16,
            2,
            reason,
        )
    return RAGPlan(
        route,
        ("lexical", "vector", "entity_link", "hyper_extract", "graph_context"),
        20,
        3,
        reason,
    )


def choose_rag_route(payload: dict[str, Any], *, worker_id: str = "") -> RAGPlan:
    """Choose a bounded route from explicit signals, never from model judgment."""

    policy = rag_policy_for_worker(worker_id)
    if policy.forced_route is not None:
        return _plan(policy.forced_route, "worker policy forbids retrieval")

    explicit = str(payload.get("rag_route", "")).upper()
    if explicit in {"NO_RAG", "HYBRID", "GRAPH", "HYPERGRAPH"}:
        route = explicit if explicit in policy.allowed_routes else "NO_RAG"
        reason = "explicit route requested" if route == explicit else "worker policy denied route"
        return _plan(route, reason)
    if payload.get("hypergraph") or payload.get("hyper_extract"):
        route = "HYPERGRAPH" if "HYPERGRAPH" in policy.allowed_routes else "NO_RAG"
        return _plan(route, "explicit multi-entity extraction signal")

    text = " ".join(
        str(payload.get(key, ""))
        for key in ("query", "question", "claim", "evidence", "assessment")
    ).lower()
    if not text.strip():
        return _plan("NO_RAG", "no retrieval query or evidence signal")
    graph_signal = any(
        marker in text
        for marker in ("contradict", "unsupported", "entity", "relationship", "citation", "dependency")
    )
    if graph_signal:
        route = "GRAPH" if "GRAPH" in policy.allowed_routes else "NO_RAG"
        return _plan(route, "claim, citation, relationship consistency signal")
    route = "HYBRID" if "HYBRID" in policy.allowed_routes else "NO_RAG"
    return _plan(route, "bounded semantic lexical QA context requested")
