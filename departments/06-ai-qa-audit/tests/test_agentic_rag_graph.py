"""Agentic-RAG loop tests with deterministic fake retrieval/LLM boundaries."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "agentic-rag"))

from src import nodes
from src.nodes import (
    generate_node,
    grade_node,
    hallucination_check_node,
    make_retrieve_node,
    should_retry,
)
from src.retriever import DocumentChunk, LocalVectorIndex, ScoredChunk


def _chunk() -> ScoredChunk:
    return ScoredChunk(
        chunk=DocumentChunk(
            chunk_id="policy-1::0",
            document_id="policy-1",
            document_type="policy",
            title="Mandate",
            text="Long positions require an approved mandate.",
            version="1",
            effective_from="2026-01-01",
            effective_to=None,
            source_path="test",
        ),
        score=0.9,
    )


class FakeIndex:
    def search(self, _query, top_k=4):
        return [_chunk()][:top_k]


class NoopQueryCache:
    @staticmethod
    def fingerprint(*parts):
        return "test"

    def get(self, _key):
        return None

    def set(self, _key, _value):
        return None


def test_agentic_rag_grounded_loop_reaches_done(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "_safe_chat_json",
        lambda **kwargs: (
            {"relevant_indices": [0]}
            if kwargs["task"] == "grade"
            else {
                "verdict": "no_breach",
                "cited_documents": ["policy-1"],
                "rationale": "Mandate is present.",
                "confidence": 0.9,
                "escalate": False,
            },
            None,
        ),
    )
    state = {"persona": "compliance-policy-agent", "query": "Can I open?", "as_of": "2026-07-29", "attempt": 1}
    state = make_retrieve_node(FakeIndex())(state)
    state = grade_node(state)
    state = generate_node(state)
    state = hallucination_check_node(state)

    assert state["grounded"] is True
    assert should_retry(state) == "done"


def test_agentic_rag_llm_failure_is_safe_and_does_not_retry(monkeypatch):
    monkeypatch.setattr(nodes, "_safe_chat_json", lambda **_kwargs: (None, "grade:CircuitOpenError"))
    state = {"persona": "compliance-policy-agent", "query": "Can I open?", "as_of": "2026-07-29", "attempt": 1}
    state = make_retrieve_node(FakeIndex())(state)
    state = grade_node(state)
    state = generate_node(state)
    state = hallucination_check_node(state)

    assert state["fallback"] is True
    assert state["grounded"] is False
    assert state["answer"]["escalate"] is True
    assert should_retry(state) == "done"


def test_graph_augmented_retrieval_mode_preserves_interface(monkeypatch):
    index = object.__new__(LocalVectorIndex)
    index.chunks = [_chunk().chunk]
    index._graph_terms = {"mandate": {0}}
    index._embeddings = np.array([[1.0, 0.0]], dtype=np.float32)
    index._query_cache = NoopQueryCache()
    index._embed = lambda _texts: [[1.0, 0.0]]
    monkeypatch.setenv("AGENTIC_RAG_RETRIEVAL_MODE", "graphrag")

    results = index.search("mandate", top_k=1)

    assert len(results) == 1
    assert results[0].chunk.document_id == "policy-1"
