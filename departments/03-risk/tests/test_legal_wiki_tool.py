from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.legal_wiki_tool import LegalWikiQueryInput, query_legal_wiki


def _query_with_answer(answer: dict, *, pages: list[str] | None = None):
    def fake_answer_fn(query: str, as_of: str, mandate: str) -> dict:
        return {
            "answer": answer,
            "context_chars": 100,
            "pages_visited": pages if pages is not None else ["law:178"],
        }

    return query_legal_wiki(
        LegalWikiQueryInput(query="법률 질의", as_of=date(2026, 8, 26)),
        answer_fn=fake_answer_fn,
    )


def test_breach_cannot_disable_escalation() -> None:
    result = _query_with_answer(
        {
            "verdict": "breach",
            "cited_documents": ["law:178"],
            "rationale": "위반 소지",
            "confidence": 1.0,
            "escalate": False,
        }
    )

    assert result.status == "OK"
    assert result.escalate is True


def test_low_confidence_no_breach_cannot_disable_escalation() -> None:
    result = _query_with_answer(
        {
            "verdict": "no_breach",
            "cited_documents": ["law:178"],
            "rationale": "위반 근거 없음",
            "confidence": 0.59,
            "escalate": False,
        }
    )

    assert result.escalate is True


def test_out_of_range_confidence_cannot_disable_escalation() -> None:
    result = _query_with_answer(
        {
            "verdict": "no_breach",
            "cited_documents": ["law:178"],
            "rationale": "잘못된 confidence",
            "confidence": 1.1,
            "escalate": False,
        }
    )

    assert result.escalate is True


def test_sourced_no_breach_still_requires_human_legal_review() -> None:
    page_id = "자본시장법_제172조_내부자의단기매매차익반환"
    result = _query_with_answer(
        {
            "verdict": "no_breach",
            "cited_documents": [page_id],
            "rationale": "제공된 근거상 위반 아님",
            "confidence": 0.8,
            "escalate": False,
        },
        pages=[page_id],
    )

    assert result.status == "OK"
    assert result.escalate is True
    assert len(result.source_references) == 1
    source = result.source_references[0]
    assert source.document_id == "law-283193-172"
    assert source.clause_id == "제172조"
    assert source.authority == "금융위원회"
    assert source.origin_url.startswith("https://www.law.go.kr/")
    assert source.source_sha256.startswith("sha256:")


def test_no_breach_without_official_source_reference_escalates() -> None:
    result = _query_with_answer(
        {
            "verdict": "no_breach",
            "cited_documents": ["unresolvable-document"],
            "rationale": "출처 좌표 없음",
            "confidence": 0.9,
            "escalate": False,
        },
        pages=["unresolvable-document"],
    )

    assert result.status == "OK"
    assert result.source_references == []
    assert result.escalate is True


def test_no_evidence_always_escalates() -> None:
    result = _query_with_answer(
        {
            "verdict": "no_breach",
            "cited_documents": ["law:178"],
            "rationale": "근거 없음",
            "confidence": 0.9,
            "escalate": False,
        },
        pages=[],
    )

    assert result.status == "NO_EVIDENCE"
    assert result.escalate is True
