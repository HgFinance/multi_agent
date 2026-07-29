"""Retrieve, grade, and generate nodes for the agentic-rag baseline graph.

Deterministic checks (Point-in-Time window, citation grounding) are done in plain
Python, not by asking the LLM — HEDGE_FUND_MASTER_PLAN.md 5.9 keeps rule-checking out
of the LLM's hands. The LLM is only used for relevance judgment and drafting the
verdict rationale in natural language.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import TypedDict

from openai import OpenAI

from .retriever import LocalVectorIndex, ScoredChunk

CHAT_MODEL = os.environ.get("AGENTIC_RAG_CHAT_MODEL", "gpt-4o-mini")
MAX_ATTEMPTS = 3
RETRIEVE_TOP_K = 4


class ComplianceState(TypedDict, total=False):
    query: str
    as_of: str
    attempt: int
    retrieved: list[ScoredChunk]
    relevant: list[ScoredChunk]
    answer: dict
    grounded: bool
    done: bool


def _client() -> OpenAI:
    return OpenAI()


def _chat_json(system: str, user: str) -> dict:
    resp = _client().chat.completions.create(
        model=CHAT_MODEL,
        response_format={"type": "json_object"},
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return json.loads(resp.choices[0].message.content)


def _point_in_time_ok(chunk: ScoredChunk, as_of: str) -> bool:
    """Deterministic PIT check — a document not yet effective, or expired, is invisible."""
    as_of_date = dt.date.fromisoformat(as_of)
    eff_from = chunk.chunk.effective_from
    eff_to = chunk.chunk.effective_to
    if eff_from and dt.date.fromisoformat(eff_from) > as_of_date:
        return False
    if eff_to and dt.date.fromisoformat(eff_to) < as_of_date:
        return False
    return True


def make_retrieve_node(index: LocalVectorIndex):
    def retrieve_node(state: ComplianceState) -> ComplianceState:
        query = state["query"]
        retrieved = index.search(query, top_k=RETRIEVE_TOP_K)
        # Point-in-Time filter runs here, deterministically, before anything reaches the LLM.
        pit_valid = [c for c in retrieved if _point_in_time_ok(c, state["as_of"])]
        return {**state, "retrieved": pit_valid}

    return retrieve_node


def grade_node(state: ComplianceState) -> ComplianceState:
    """Ask the LLM which retrieved chunks are actually relevant to the query."""
    retrieved = state.get("retrieved", [])
    if not retrieved:
        return {**state, "relevant": []}

    docs_block = "\n\n".join(
        f"[{i}] ({c.chunk.document_type} — {c.chunk.title}, v{c.chunk.version})\n{c.chunk.text}"
        for i, c in enumerate(retrieved)
    )
    result = _chat_json(
        system=(
            "You are a document relevance grader for a trading-compliance check. "
            'Return JSON: {"relevant_indices": [int, ...]} — only indices of chunks '
            "that are directly relevant to answering the compliance question. "
            "Be strict: an on-topic but non-decisive chunk should still be included; "
            "an off-topic chunk must not be."
        ),
        user=f"Compliance question:\n{state['query']}\n\nCandidate chunks:\n{docs_block}",
    )
    keep = set(result.get("relevant_indices", []))
    relevant = [c for i, c in enumerate(retrieved) if i in keep]
    return {**state, "relevant": relevant}


def generate_node(state: ComplianceState) -> ComplianceState:
    """Draft the structured compliance verdict, grounded only in graded-relevant chunks."""
    relevant = state.get("relevant", [])
    if not relevant:
        answer = {
            "verdict": "ambiguous",
            "cited_documents": [],
            "rationale": "No policy document with sufficient relevance and Point-in-Time validity was found.",
            "confidence": 0.0,
            "escalate": True,
        }
        return {**state, "answer": answer}

    docs_block = "\n\n".join(
        f"doc_id={c.chunk.document_id} title={c.chunk.title!r} version={c.chunk.version}\n{c.chunk.text}"
        for c in relevant
    )
    result = _chat_json(
        system=(
            "You are the Compliance Policy Agent for a hedge fund's Risk department. "
            "Check the proposed order/question against ONLY the provided policy excerpts. "
            "Never state a rule that is not present in the excerpts. "
            'Return JSON: {"verdict": "no_breach"|"breach"|"ambiguous", '
            '"cited_documents": [doc_id, ...], "rationale": str, '
            '"confidence": float (0-1), "escalate": bool}. '
            "Set escalate=true whenever verdict is 'ambiguous' or 'breach', or confidence < 0.6."
        ),
        user=f"Compliance question:\n{state['query']}\n\nRelevant policy excerpts:\n{docs_block}",
    )
    return {**state, "answer": result}


def hallucination_check_node(state: ComplianceState) -> ComplianceState:
    """Deterministic grounding check: every cited doc_id must be among the relevant chunks."""
    answer = state.get("answer", {})
    relevant_ids = {c.chunk.document_id for c in state.get("relevant", [])}
    cited_ids = set(answer.get("cited_documents", []))
    grounded = cited_ids.issubset(relevant_ids) and (bool(cited_ids) or answer.get("verdict") == "ambiguous")
    return {**state, "grounded": grounded}


def should_retry(state: ComplianceState) -> str:
    if state.get("grounded"):
        return "done"
    if state.get("attempt", 1) >= MAX_ATTEMPTS:
        return "done"
    return "retry"


def bump_attempt_node(state: ComplianceState) -> ComplianceState:
    return {**state, "attempt": state.get("attempt", 1) + 1}
