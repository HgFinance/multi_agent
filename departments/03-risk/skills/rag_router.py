"""Deterministic RAG routing plan for Risk Workers.

The router selects a bounded retrieval strategy and never retrieves data or
calls an LLM. Worker policy is enforced here so a hot path cannot opt into RAG
through an input flag.
"""

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
    """Per-worker retrieval boundary; deterministic and auditable."""

    allowed_routes: frozenset[RAGRoute]
    forced_route: RAGRoute | None = None


WORKER_RAG_POLICIES: dict[str, WorkerRAGPolicy] = {
    "market-liquidity-worker": WorkerRAGPolicy(frozenset({"NO_RAG"}), "NO_RAG"),
    "pre-trade-risk-worker": WorkerRAGPolicy(frozenset({"NO_RAG"}), "NO_RAG"),
    "derivatives-counterparty-worker": WorkerRAGPolicy(frozenset({"NO_RAG"}), "NO_RAG"),
    "compliance-policy-worker": WorkerRAGPolicy(
        frozenset({"NO_RAG", "HYBRID", "GRAPH"})
    ),
}


def rag_policy_for_worker(worker_id: str) -> WorkerRAGPolicy:
    try:
        return WORKER_RAG_POLICIES[worker_id]
    except KeyError as exc:
        raise ValueError(f"unknown Risk Worker RAG policy: {worker_id}") from exc


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
    """Choose a bounded route from explicit signals, not model judgment."""

    policy = rag_policy_for_worker(worker_id)
    if policy.forced_route is not None:
        return _plan(policy.forced_route, "worker policy forbids retrieval")

    explicit = str(payload.get("rag_route", "")).upper()
    if explicit in {"NO_RAG", "HYBRID", "GRAPH", "HYPERGRAPH"}:
        route = explicit if explicit in policy.allowed_routes else "NO_RAG"
        reason = (
            "explicit route requested"
            if route == explicit
            else "worker policy denied route"
        )
        return _plan(route, reason)
    if payload.get("hypergraph") or payload.get("hyper_extract"):
        route = "HYPERGRAPH" if "HYPERGRAPH" in policy.allowed_routes else "NO_RAG"
        return _plan(route, "explicit multi-entity extraction signal")

    text = " ".join(
        str(payload.get(key, ""))
        for key in ("query", "question", "claim", "policy", "evidence")
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
            "counterparty",
            "dependency",
        )
    )
    policy_signal = any(
        marker in text
        for marker in ("policy", "compliance", "regulation", "citation", "pit")
    )
    if graph_signal:
        route = "GRAPH" if "GRAPH" in policy.allowed_routes else "NO_RAG"
        return _plan(route, "claim or relationship consistency signal")
    if "compliance" in worker_id and policy_signal:
        route = "HYBRID" if "HYBRID" in policy.allowed_routes else "NO_RAG"
        return _plan(route, "policy or point-in-time evidence signal")
    route = "HYBRID" if "HYBRID" in policy.allowed_routes else "NO_RAG"
    return _plan(route, "bounded semantic lexical context requested")
