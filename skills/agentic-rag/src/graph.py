"""LangGraph definition: retrieve -> grade -> generate -> hallucination-check -> retry.

This is the baseline Agentic RAG loop referenced in HEDGE_FUND_MASTER_PLAN.md 5.10 and
in risk-management.yaml / qa-department.yaml's "Note on Agentic RAG". It intentionally
does NOT include query rewriting, reranking, fusion, or a semantic cache — those are
backlog items to add once this loop is proven, not day-one scope (see
HEDGE_FUND_MASTER_PLAN.md 13.1: don't over-proliferate services early).
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph import END, StateGraph

from .nodes import (
    ComplianceState,
    bump_attempt_node,
    generate_node,
    grade_node,
    hallucination_check_node,
    make_retrieve_node,
    should_retry,
)
from .retriever import LocalVectorIndex


def build_compliance_graph(corpus_dir: Path):
    index = LocalVectorIndex(corpus_dir)
    retrieve_node = make_retrieve_node(index)

    graph = StateGraph(ComplianceState)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("grade", grade_node)
    graph.add_node("generate", generate_node)
    graph.add_node("hallucination_check", hallucination_check_node)
    graph.add_node("bump_attempt", bump_attempt_node)

    graph.set_entry_point("retrieve")
    graph.add_edge("retrieve", "grade")
    graph.add_edge("grade", "generate")
    graph.add_edge("generate", "hallucination_check")
    graph.add_conditional_edges(
        "hallucination_check",
        should_retry,
        {"done": END, "retry": "bump_attempt"},
    )
    graph.add_edge("bump_attempt", "retrieve")

    return graph.compile()


def run_compliance_check(query: str, as_of: str, corpus_dir: Path | None = None) -> dict:
    corpus_dir = corpus_dir or (Path(__file__).resolve().parent.parent / "corpus" / "compliance")
    app = build_compliance_graph(corpus_dir)
    final_state = app.invoke({"query": query, "as_of": as_of, "attempt": 1})
    return {
        "answer": final_state.get("answer"),
        "grounded": final_state.get("grounded"),
        "attempts": final_state.get("attempt"),
        "relevant_documents": [
            {
                "document_id": c.chunk.document_id,
                "title": c.chunk.title,
                "version": c.chunk.version,
                "score": round(c.score, 4),
            }
            for c in final_state.get("relevant", [])
        ],
    }
