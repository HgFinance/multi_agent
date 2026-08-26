"""LEGAL_QUERY 조회 도구 — LLM-Wiki grep+BM25(Arm C)를 결정론적 I/O로 감싼다.

`departments/03-risk/experiments/llm_wiki`의 golden set(15문항) 평가에서 Arm C가
Arm A(plain RAG)를 verdict_acc(0.87 vs 0.53)·semantic_acc(0.73 vs 0.33) 전 지표에서
앞서 LEGAL_QUERY의 검색 경로로 채택됐다(results/comparison_report_final_judged.md).

# ponytail: 실험 모듈(experiments/llm_wiki)을 프로덕션이 직접 import한다 — 실험
# 격리 원칙(departments/03-risk/experiments/llm_wiki 계획 문서)상 golden set 게이트를
# 통과하면 다음 단계는 skills/policy_rag.py로 승격하는 것이다. 지금은 그 전 단계라
# import 경로만 열어둔다; 실험 파일 구조가 바뀌면 이 얇은 wrapper만 갱신하면 된다.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_LLM_WIKI_DIR = Path(__file__).resolve().parents[1] / "experiments" / "llm_wiki"

# golden_set.json의 고정 컴플라이언스 mandate 문구(실험에서 검증된 그대로) — 사용자의
# 개인 투자 Mandate(investor_profile 등)가 아니라 펀드 차원의 상시 준수 선언이다.
COMPLIANCE_MANDATE_CONTEXT = (
    "Mandate: 이 펀드는 미공개중요정보 이용행위, 시세조종행위, 부정거래행위, 임직원 "
    "사적 매매 규정을 엄격히 준수합니다. 모든 판정은 제공된 법령·행정규칙·판례 근거에만 "
    "기반해야 하며, 추측이나 일반 상식으로 결론을 내리지 않습니다."
)

LegalWikiAnswerFn = Callable[[str, str, str], dict[str, Any]]


class LegalWikiQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    mandate_context: str = COMPLIANCE_MANDATE_CONTEXT
    as_of: date


class LegalWikiQueryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["OK", "NO_EVIDENCE", "UNAVAILABLE"]
    verdict: str | None = None
    rationale: str | None = None
    cited_documents: list[str] = Field(default_factory=list)
    confidence: float | None = None
    escalate: bool = True
    pages_visited: list[str] = Field(default_factory=list)
    context_chars: int = 0
    retrieved_at: datetime
    error_code: str | None = None


def _default_answer_fn() -> LegalWikiAnswerFn:
    if str(_LLM_WIKI_DIR) not in sys.path:
        sys.path.insert(0, str(_LLM_WIKI_DIR))
    # lazy import: avoids OpenAI/BM25 load cost for callers that never use LEGAL_QUERY
    from arms import llm_wiki_grep_bm25_answer

    return llm_wiki_grep_bm25_answer


def query_legal_wiki(
    request: LegalWikiQueryInput,
    *,
    answer_fn: LegalWikiAnswerFn | None = None,
) -> LegalWikiQueryOutput:
    """실제 법령·행정규칙·판례 근거를 조회한다. 근거가 없으면 위반 아님으로 단정하지 않는다."""

    try:
        fn = answer_fn or _default_answer_fn()
        result = fn(request.query, request.as_of.isoformat(), request.mandate_context)
    except Exception as exc:  # noqa: BLE001 - fail-closed boundary; legal search must never crash the worker
        return LegalWikiQueryOutput(
            status="UNAVAILABLE",
            retrieved_at=datetime.now(timezone.utc),
            error_code=type(exc).__name__,
        )

    answer = result.get("answer") or {}
    pages = list(result.get("pages_visited") or [])
    verdict = answer.get("verdict")
    cited_documents = list(answer.get("cited_documents") or [])
    try:
        confidence = float(answer["confidence"])
    except (KeyError, TypeError, ValueError):
        confidence = None

    # Guided JSON guarantees shape and primitive types, not cross-field legal
    # semantics.  Only a well-supported, sufficiently confident no-breach
    # result may suppress escalation; every other state fails closed.
    supported_no_breach = (
        verdict == "no_breach"
        and confidence is not None
        and 0.6 <= confidence <= 1.0
        and bool(cited_documents)
        and bool(pages)
    )
    return LegalWikiQueryOutput(
        status="OK" if pages else "NO_EVIDENCE",
        verdict=verdict,
        rationale=answer.get("rationale"),
        cited_documents=cited_documents,
        confidence=confidence,
        escalate=bool(answer.get("escalate", True)) or not supported_no_breach,
        pages_visited=pages,
        context_chars=int(result.get("context_chars", 0)),
        retrieved_at=datetime.now(timezone.utc),
    )


__all__ = [
    "COMPLIANCE_MANDATE_CONTEXT",
    "LegalWikiAnswerFn",
    "LegalWikiQueryInput",
    "LegalWikiQueryOutput",
    "query_legal_wiki",
]


if __name__ == "__main__":
    from datetime import date as _date

    def _fake_answer_fn(query: str, as_of: str, mandate: str) -> dict[str, Any]:
        assert mandate == COMPLIANCE_MANDATE_CONTEXT
        return {
            "answer": {
                "verdict": "breach",
                "cited_documents": ["자본시장법_제178조_부정거래행위등의금지"],
                "rationale": "제178조제1항제1호 위반 소지.",
                "confidence": 0.8,
                "escalate": False,
            },
            "context_chars": 900,
            "pages_visited": ["자본시장법_제178조_부정거래행위등의금지"],
        }

    ok = query_legal_wiki(
        LegalWikiQueryInput(query="부정거래행위 판단 기준", as_of=_date(2026, 8, 7)),
        answer_fn=_fake_answer_fn,
    )
    assert ok.status == "OK"
    assert ok.verdict == "breach"
    assert ok.escalate is True

    def _boom(query: str, as_of: str, mandate: str) -> dict[str, Any]:
        raise RuntimeError("openai unavailable")

    down = query_legal_wiki(
        LegalWikiQueryInput(query="아무 질문", as_of=_date(2026, 8, 7)), answer_fn=_boom
    )
    assert down.status == "UNAVAILABLE"
    assert down.escalate is True
    assert down.error_code == "RuntimeError"

    def _no_pages(query: str, as_of: str, mandate: str) -> dict[str, Any]:
        return {"answer": {"verdict": "ambiguous", "escalate": True}, "context_chars": 0, "pages_visited": []}

    empty = query_legal_wiki(
        LegalWikiQueryInput(query="코퍼스 밖 질문", as_of=_date(2026, 8, 7)), answer_fn=_no_pages
    )
    assert empty.status == "NO_EVIDENCE"

    print("legal_wiki_tool self-check OK")
