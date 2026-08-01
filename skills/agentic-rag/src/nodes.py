"""Retrieve, grade, and generate nodes for the agentic-rag baseline graph.

Deterministic checks (Point-in-Time window, citation grounding) are done in plain
Python, not by asking the LLM — HEDGE_FUND_MASTER_PLAN.md 5.9 keeps rule-checking out
of the LLM's hands. The LLM is only used for relevance judgment and drafting the
verdict rationale in natural language.

Persona-specific system prompts and verdict vocabulary live in PERSONA_PROMPTS below —
per SKILL.md's "Extending to other personas" guidance, a new persona adds a table entry
here instead of copy-pasting compliance-policy-agent's prompts.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import TypedDict

from langsmith.wrappers import wrap_openai
from openai import OpenAI

from .retriever import LocalVectorIndex, ScoredChunk

CHAT_MODEL = os.environ.get("AGENTIC_RAG_CHAT_MODEL", "gpt-4o-mini")
MAX_ATTEMPTS = 3
RETRIEVE_TOP_K = 4

PERSONA_PROMPTS: dict[str, dict[str, object]] = {
    "compliance-policy-agent": {
        "grade_system": (
            "You are a document relevance grader for a trading-compliance check. "
            'Return JSON: {"relevant_indices": [int, ...]} — only indices of chunks '
            "that are directly relevant to answering the compliance question. "
            "Be strict: an on-topic but non-decisive chunk should still be included; "
            "an off-topic chunk must not be."
        ),
        "generate_system": (
            "You are the Compliance Policy Agent for a hedge fund's Risk department. "
            "Check the proposed order/question against ONLY the provided policy excerpts. "
            "Never state a rule that is not present in the excerpts. "
            'Return JSON: {"verdict": "no_breach"|"breach"|"ambiguous", '
            '"cited_documents": [doc_id, ...], "rationale": str, '
            '"confidence": float (0-1), "escalate": bool}. '
            "Set escalate=true whenever verdict is 'ambiguous' or 'breach', or confidence < 0.6."
        ),
        "no_evidence_verdict": "ambiguous",
        "no_evidence_rationale": "No policy document with sufficient relevance and Point-in-Time validity was found.",
        "query_label": "Compliance question",
        "docs_label": "Relevant policy excerpts",
    },
    "evidence-qa-agent": {
        "grade_system": (
            "You are an evidence relevance grader for a hedge fund's Evidence QA process. "
            'Return JSON: {"relevant_indices": [int, ...]} — only indices of chunks that '
            "could plausibly support or contradict the claim under review. "
            "Be strict: an on-topic but non-decisive chunk should still be included; "
            "an off-topic chunk must not be."
        ),
        "generate_system": (
            "You are the Evidence QA Agent for a hedge fund's AI QA/Audit department. "
            "Check the claim under review against ONLY the provided evidence excerpts. "
            "Never assert a number, date or fact that is not present in the excerpts, and "
            "never upgrade an analyst's forecast or opinion into a confirmed fact. "
            'Return JSON: {"verdict": "SUPPORTED"|"PARTIAL"|"UNSUPPORTED"|"CONTRADICTED", '
            '"cited_documents": [doc_id, ...], "rationale": str, '
            '"confidence": float (0-1), "escalate": bool}. '
            "Use SUPPORTED only if the excerpts fully back the claim, PARTIAL if some but not "
            "all of the claim is backed, UNSUPPORTED if no excerpt addresses the claim, and "
            "CONTRADICTED if an excerpt states something inconsistent with the claim. "
            "Set escalate=true whenever verdict is not 'SUPPORTED', or confidence < 0.6."
        ),
        "no_evidence_verdict": "UNSUPPORTED",
        "no_evidence_rationale": "No evidence document with sufficient relevance and Point-in-Time validity was found.",
        "query_label": "Claim under review",
        "docs_label": "Relevant evidence excerpts",
    },
    "hallucination-critic": {
        "grade_system": (
            "You are an evidence relevance grader for a hedge fund's Hallucination Critic review. "
            'Return JSON: {"relevant_indices": [int, ...]} — only indices of chunks that '
            "could plausibly explain why the flagged claim lacks support or is contradicted. "
            "Be strict: an on-topic but non-decisive chunk should still be included; "
            "an off-topic chunk must not be."
        ),
        "generate_system": (
            "You are the Hallucination Critic for a hedge fund's AI QA/Audit department. A claim was "
            "already flagged UNSUPPORTED or CONTRADICTED by the deterministic Evidence QA engine — "
            "that verdict is final and not yours to overturn. Your job is only to classify the failure "
            "using ONLY the provided evidence excerpts and write the escalation rationale. "
            'Return JSON: {"verdict": "HALLUCINATION"|"OVERCONFIDENT_CLAIM"|"CONTRADICTION"|"TOOL_MISUSE", '
            '"cited_documents": [doc_id, ...], "rationale": str, '
            '"confidence": float (0-1), "escalate": bool}. '
            "Use CONTRADICTION if an excerpt states something inconsistent with the claim, HALLUCINATION "
            "if the claim asserts a fact absent from every excerpt, OVERCONFIDENT_CLAIM if the claim "
            "upgrades a forecast/opinion into a stated fact, and TOOL_MISUSE if the excerpts show the "
            "originating agent queried the wrong scope or timeframe. Always set escalate=true — every "
            "input to this node was already flagged for escalation upstream."
        ),
        "no_evidence_verdict": "HALLUCINATION",
        "no_evidence_rationale": "No supporting or contradicting evidence document was found for the flagged claim; the original UNSUPPORTED/CONTRADICTED verdict stands as-is.",
        "query_label": "Flagged claim under hallucination review",
        "docs_label": "Relevant evidence excerpts",
    },
}


class ComplianceState(TypedDict, total=False):
    persona: str
    query: str
    as_of: str
    attempt: int
    retrieved: list[ScoredChunk]
    relevant: list[ScoredChunk]
    answer: dict
    grounded: bool
    done: bool


def _client() -> OpenAI:
    # wrap_openai는 LANGSMITH_TRACING이 꺼져 있으면 아무 동작 없이 그대로 통과한다 -
    # .env.example 3-1절(임시, risk/qa 부서 유휴 에이전트 점검용)이 켜졌을 때만 토큰/비용이 잡힌다.
    return wrap_openai(OpenAI())


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


def _prompts(persona: str) -> dict[str, object]:
    return PERSONA_PROMPTS[persona]


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

    prompts = _prompts(state["persona"])
    docs_block = "\n\n".join(
        f"[{i}] ({c.chunk.document_type} — {c.chunk.title}, v{c.chunk.version})\n{c.chunk.text}"
        for i, c in enumerate(retrieved)
    )
    result = _chat_json(
        system=prompts["grade_system"],
        user=f"{prompts['query_label']}:\n{state['query']}\n\nCandidate chunks:\n{docs_block}",
    )
    keep = set(result.get("relevant_indices", []))
    relevant = [c for i, c in enumerate(retrieved) if i in keep]
    return {**state, "relevant": relevant}


def generate_node(state: ComplianceState) -> ComplianceState:
    """Draft the structured verdict, grounded only in graded-relevant chunks."""
    prompts = _prompts(state["persona"])
    relevant = state.get("relevant", [])
    if not relevant:
        answer = {
            "verdict": prompts["no_evidence_verdict"],
            "cited_documents": [],
            "rationale": prompts["no_evidence_rationale"],
            "confidence": 0.0,
            "escalate": True,
        }
        return {**state, "answer": answer}

    docs_block = "\n\n".join(
        f"doc_id={c.chunk.document_id} title={c.chunk.title!r} version={c.chunk.version}\n{c.chunk.text}"
        for c in relevant
    )
    result = _chat_json(
        system=prompts["generate_system"],
        user=f"{prompts['query_label']}:\n{state['query']}\n\n{prompts['docs_label']}:\n{docs_block}",
    )
    return {**state, "answer": result}


def hallucination_check_node(state: ComplianceState) -> ComplianceState:
    """Deterministic grounding check: every cited doc_id must be among the relevant chunks."""
    prompts = _prompts(state["persona"])
    answer = state.get("answer", {})
    relevant_ids = {c.chunk.document_id for c in state.get("relevant", [])}
    cited_ids = set(answer.get("cited_documents", []))
    no_evidence_verdict = prompts["no_evidence_verdict"]
    grounded = cited_ids.issubset(relevant_ids) and (
        bool(cited_ids) or answer.get("verdict") == no_evidence_verdict
    )
    return {**state, "grounded": grounded}


def should_retry(state: ComplianceState) -> str:
    if state.get("grounded"):
        return "done"
    if state.get("attempt", 1) >= MAX_ATTEMPTS:
        return "done"
    return "retry"


def bump_attempt_node(state: ComplianceState) -> ComplianceState:
    return {**state, "attempt": state.get("attempt", 1) + 1}
