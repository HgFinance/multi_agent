from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.mcp_server import build_server, execute_legal_query
from risk_mandate_workers import classify_compliance_query_mode


def test_legal_query_reuses_llm_wiki_and_fails_closed() -> None:
    calls: list[tuple[str, str, str]] = []

    def answer_fn(query: str, as_of: str, mandate: str) -> dict:
        calls.append((query, as_of, mandate))
        return {
            "answer": {
                "verdict": "breach",
                "cited_documents": ["자본시장법_제178조"],
                "rationale": "제178조 위반 소지",
                "confidence": 0.9,
                "escalate": False,
            },
            "context_chars": 500,
            "pages_visited": ["자본시장법_제178조"],
        }

    result = execute_legal_query(
        "자본시장법 제178조 위반 여부를 판례와 함께 확인해줘",
        "2026-08-26",
        answer_fn=answer_fn,
    )

    assert len(calls) == 1
    assert result["query_mode"] == "LEGAL_QUERY"
    assert result["llm_wiki_invoked"] is True
    assert result["status"] == "OK"
    assert result["verdict"] == "breach"
    assert result["escalate"] is True


def test_non_legal_query_never_invokes_llm_wiki() -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("non-legal risk query must not invoke LLM-Wiki")

    result = execute_legal_query(
        "AAPL 포지션의 변동성 25%와 익스포저를 계산해줘",
        "2026-08-26",
        answer_fn=forbidden,
    )

    assert result["status"] == "NOT_APPLICABLE"
    assert result["query_mode"] == "NOT_APPLICABLE"
    assert result["llm_wiki_invoked"] is False
    assert result["pages_visited"] == []


def test_internal_policy_only_query_never_invokes_llm_wiki() -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("policy-only query must not invoke LLM-Wiki")

    result = execute_legal_query(
        "우리 내부정책의 단일 종목 한도를 확인해줘",
        "2026-08-26",
        answer_fn=forbidden,
    )

    assert result["query_mode"] == "RISK_POLICY_REVIEW"
    assert result["llm_wiki_invoked"] is False


def test_classifier_is_shared_by_mandate_and_hermes_routes() -> None:
    assert classify_compliance_query_mode("제172조 단기매매차익 반환 기간")[0] == (
        "LEGAL_QUERY"
    )
    assert classify_compliance_query_mode("내부정책과 자본시장법을 함께 검토")[0] == (
        "MIXED_REVIEW"
    )
    assert classify_compliance_query_mode("포트폴리오 VaR 계산")[0] == (
        "NOT_APPLICABLE"
    )


def test_mcp_surface_exposes_only_the_legal_query_capability() -> None:
    server = build_server()
    names = {tool.name for tool in asyncio.run(server.list_tools())}

    assert names == {"query_risk_legal_wiki"}
