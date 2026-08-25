"""Bounded semantic-quality signals for user-facing answers.

The evaluator deliberately runs at the application boundary, where the answer
already exists, and emits only bounded scores/codes to observability.  It does
not send prompt, answer, tool output, or evidence text to LangSmith.

This is a v1 contract-based semantic QA signal: it checks whether an answer is
usable, grounded, time-scoped, and honest about uncertainty.  It is not a
claim-truth judge.  A future judge can implement the same result contract
without changing the LangSmith feedback or QA UI shape.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any

from orchestration.answer_contract import AnswerGrade, grade_answer

SEMANTIC_QA_VERSION = "hgfinance.semantic-qa.v1"
SEMANTIC_QA_EVALUATOR = "deterministic_answer_contract"


def _score(value: bool) -> float:
    return 1.0 if value else 0.0


@dataclass(frozen=True)
class SemanticQaResult:
    """Redacted quality result retained by the trace/evaluation layer."""

    score: float
    verdict: str
    completeness: float
    groundedness: float
    temporal_consistency: float
    uncertainty_honesty: float
    finding_codes: tuple[str, ...]
    relevance: float | None = None
    evaluator: str = SEMANTIC_QA_EVALUATOR
    version: str = SEMANTIC_QA_VERSION

    def as_metadata(self) -> dict[str, Any]:
        """Return only scalar bounded fields safe for LangSmith metadata."""

        return {
            "semantic_qa_version": self.version,
            "semantic_qa_evaluator": self.evaluator,
            "semantic_qa_verdict": self.verdict,
            "semantic_qa_score": self.score,
            "semantic_qa_completeness": self.completeness,
            "semantic_qa_groundedness": self.groundedness,
            "semantic_qa_temporal_consistency": self.temporal_consistency,
            "semantic_qa_uncertainty_honesty": self.uncertainty_honesty,
            **(
                {"semantic_qa_relevance": self.relevance}
                if self.relevance is not None
                else {}
            ),
            "semantic_qa_finding_count": len(self.finding_codes),
            # Codes are bounded labels, never answer excerpts.
            "semantic_qa_finding_codes": "|".join(self.finding_codes[:12]),
            "raw_payloads_sent": False,
        }


def evaluate_answer(
    answer: str,
    *,
    summary: str = "",
    status: str = "completed",
) -> SemanticQaResult:
    """Evaluate a final answer locally and return a redacted quality result.

    ``grade_answer`` is intentionally reused as the single source of truth for
    evidence/as-of/unknown detection already used by CEO handoff logic.
    """

    grade: AnswerGrade = grade_answer(answer, summary=summary)
    findings: list[str] = []
    dimensions = {
        "completeness": _score(grade.has_body),
        "groundedness": _score(grade.has_evidence),
        "temporal_consistency": _score(grade.has_as_of),
        "uncertainty_honesty": _score(grade.states_unknowns),
    }
    if not grade.has_body:
        findings.append("ANSWER_BODY_MISSING")
    if not grade.has_evidence:
        findings.append("ANSWER_EVIDENCE_MISSING")
    if not grade.has_as_of:
        findings.append("ANSWER_AS_OF_MISSING")
    if grade.has_body and not grade.states_unknowns:
        findings.append("ANSWER_UNCERTAINTY_UNSTATED")

    normalized_status = str(status or "").strip().lower()
    if normalized_status in {"error", "failed", "blocked", "degraded", "gave_up", "timed_out"}:
        findings.append("WORKFLOW_NOT_COMPLETED")

    score = round(sum(dimensions.values()) / len(dimensions), 4)
    if normalized_status in {"error", "failed", "blocked", "degraded", "gave_up", "timed_out"}:
        score = min(score, 0.25)
    verdict = "PASS" if score >= 0.8 else "REVIEW" if score >= 0.6 else "FAIL"
    return SemanticQaResult(
        score=score,
        verdict=verdict,
        completeness=dimensions["completeness"],
        groundedness=dimensions["groundedness"],
        temporal_consistency=dimensions["temporal_consistency"],
        uncertainty_honesty=dimensions["uncertainty_honesty"],
        finding_codes=tuple(dict.fromkeys(findings)),
    )


_PROMPT_STOPWORDS = frozenset(
    {
        "그리고", "그런", "대한", "대해", "어떤", "무엇", "어떻게", "해주세요",
        "알려줘", "알려주세요", "please", "what", "which", "about", "the", "and",
    }
)


def _terms(value: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[0-9A-Za-z가-힣]{2,}", str(value or ""))
        if token.casefold() not in _PROMPT_STOPWORDS
    }


def evaluate_prompt_answer(
    prompt: str,
    answer: str,
    *,
    summary: str = "",
    status: str = "completed",
) -> SemanticQaResult:
    """Add a bounded prompt/answer relevance signal at the local boundary.

    This is intentionally lexical and conservative.  It detects an obviously
    unrelated answer and routes ambiguous overlap to review; it does not claim
    factual correctness or replace an offline benchmark/judge.
    """

    base = evaluate_answer(answer, summary=summary, status=status)
    prompt_terms = _terms(prompt)
    answer_terms = _terms(f"{answer}\n{summary}")
    if not prompt_terms:
        return base
    overlap = sum(
        1
        for prompt_term in prompt_terms
        if any(
            answer_term == prompt_term
            or answer_term.startswith(prompt_term)
            or prompt_term.startswith(answer_term)
            for answer_term in answer_terms
        )
    )
    required_overlap = max(1, min(3, (len(prompt_terms) + 1) // 2))
    relevance = 1.0 if overlap >= required_overlap else 0.5
    findings = list(base.finding_codes)
    if relevance < 1.0 and base.completeness:
        findings.append("ANSWER_PROMPT_RELEVANCE_LOW")
    score = round(base.score * 0.8 + relevance * 0.2, 4)
    verdict = base.verdict
    if relevance < 1.0 and verdict == "PASS":
        verdict = "REVIEW"
    return replace(
        base,
        score=score,
        verdict=verdict,
        relevance=relevance,
        finding_codes=tuple(dict.fromkeys(findings)),
    )


__all__ = [
    "SEMANTIC_QA_EVALUATOR",
    "SEMANTIC_QA_VERSION",
    "SemanticQaResult",
    "evaluate_answer",
    "evaluate_prompt_answer",
]
