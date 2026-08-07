"""Mandate-to-Risk employee contract tests."""

from __future__ import annotations

import sys
from pathlib import Path

RISK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RISK_DIR))

from risk_mandate_workers import (
    PineconeEvidenceClient,
    RiskMandateAssessmentRequest,
    assess_mandate,
    run_compliance_policy_worker,
    run_risk_runner,
)


def _mandate(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "mandate_id": "MND-20260806-001",
        "investor_profile": {
            "investment_goal": "장기적인 자산 가치 보존 및 안정적 수익 창출",
            "risk_tolerance": "CONSERVATIVE",
            "financial_experience_years": 0,
            "perceived_risk_awareness": True,
        },
        "portfolio_constraints": {
            "base_capital": 100000000,
            "max_single_stock_weight": 0.30,
            "max_total_exposure": 2.00,
            "max_drawdown_limit": -0.15,
        },
        "asset_policy": {
            "single_stocks": "ALLOWED",
            "etf": "ALLOWED",
            "leverage": "ALLOWED",
            "futures": "PROHIBITED",
            "options": "PROHIBITED",
            "crypto": "PROHIBITED",
        },
        "order_mode": "MANUAL_APPROVAL",
    }
    payload.update(overrides)
    return payload


def test_mandate_is_dispatched_to_two_independent_risk_employees() -> None:
    result = assess_mandate(_mandate())

    assert (
        result["pipeline_status"] == "DEGRADED"
    )  # no observed state/evidence is fail-closed
    assert result["dispatch"]["dispatcher"] == "risk-head"
    assert result["dispatch"]["mutation_allowed"] is False
    assert (
        result["dispatch"]["worker_inputs"]["risk-runner"]["input_hash"]
        == result["dispatch"]["worker_inputs"]["compliance-policy-worker"]["input_hash"]
    )
    assert set(result["employees"]) == {"risk-runner", "compliance-policy-worker"}
    risk_report = result["employees"]["risk-runner"]
    compliance_report = result["employees"]["compliance-policy-worker"]
    assert risk_report["authoritative"] is True
    assert risk_report["binding"] is False
    assert compliance_report["authoritative"] is False
    assert risk_report["input_hash"] == compliance_report["input_hash"]
    assert result["risk_head"]["manual_approval_required"] is True
    assert result["risk_head"]["safe_action"] == "HOLD"


def test_risk_runner_reports_var_and_concentration_breaches_without_order_side_effects() -> (
    None
):
    request = RiskMandateAssessmentRequest.model_validate(
        _mandate(
            portfolio_snapshot={
                "current_var": 6500000000,
                "var_limit": 5000000000,
                "total_exposure": 1.2,
                "current_drawdown": -0.10,
                "positions": [
                    {
                        "instrument_id": "005930",
                        "asset_class": "SINGLE_STOCK",
                        "weight": 0.35,
                        "issuer": "삼성전자",
                    }
                ],
            }
        )
    )
    report = run_risk_runner(request)

    assert report["verdict"] == "RESIZE"
    assert report["reason_codes"] == [
        "SINGLE_STOCK_LIMIT_BREACH:005930",
        "VAR_LIMIT_BREACH",
    ]
    assert all(
        action["mode"] == "MANUAL_APPROVAL" for action in report["suggested_actions"]
    )


def test_prohibited_asset_is_rejected_deterministically() -> None:
    request = RiskMandateAssessmentRequest.model_validate(
        _mandate(
            portfolio_snapshot={
                "total_exposure": 0.5,
                "current_drawdown": -0.01,
                "current_var": 100,
                "var_limit": 1000,
                "positions": [
                    {"instrument_id": "FUT-1", "asset_class": "FUTURES", "weight": 0.1}
                ],
            }
        )
    )
    assert run_risk_runner(request)["verdict"] == "REJECT"
    assert "PROHIBITED_ASSET:FUTURES" in run_risk_runner(request)["reason_codes"]


def test_compliance_worker_escalates_when_pinecone_evidence_is_unavailable() -> None:
    request = RiskMandateAssessmentRequest.model_validate(
        _mandate(compliance_query="삼성전자 단일 종목 한도")
    )
    report = run_compliance_policy_worker(
        request, pinecone=PineconeEvidenceClient(api_key="", index_host="")
    )

    assert report["status"] == "DEGRADED"
    assert report["verdict"] == "ESCALATE"
    assert report["error"] is None  # no vector means no network was attempted


def test_compliance_worker_marks_supplied_policy_violation_as_advisory() -> None:
    request = RiskMandateAssessmentRequest.model_validate(
        _mandate(
            compliance_evidence=[
                {
                    "evidence_id": "internal-policy-12",
                    "title": "단일 종목 편입 한도",
                    "text": "단일 종목은 15%를 초과할 수 없다.",
                    "source": "internal-risk-policy",
                    "violation": True,
                    "reason_code": "SINGLE_ISSUER_LIMIT",
                }
            ]
        )
    )
    report = run_compliance_policy_worker(request)

    assert report["verdict"] == "ESCALATE"
    assert report["authoritative"] is False
    assert report["reason_codes"] == ["SINGLE_ISSUER_LIMIT"]


def test_not_applicable_mode_skips_all_search() -> None:
    request = RiskMandateAssessmentRequest.model_validate(
        _mandate(query_mode="NOT_APPLICABLE")
    )
    report = run_compliance_policy_worker(request)

    assert report["verdict"] == "NOT_APPLICABLE"
    assert report["tool_calls"] == []
    assert report["action_required"] is False


def test_mandate_review_flags_experience_leverage_mismatch_without_legal_search() -> None:
    request = RiskMandateAssessmentRequest.model_validate(
        _mandate(query_mode="MANDATE_REVIEW")
    )
    report = run_compliance_policy_worker(request)

    assert report["verdict"] == "NEEDS_CLARIFICATION"
    assert "EXPERIENCE_LEVERAGE_MISMATCH" in report["reason_codes"]
    assert report["tool_calls"] == []  # 법률·정책 검색은 하지 않는다
    assert report["questions"]


def test_mandate_review_is_ready_when_no_mismatch_found() -> None:
    request = RiskMandateAssessmentRequest.model_validate(
        _mandate(
            query_mode="MANDATE_REVIEW",
            asset_policy={
                "single_stocks": "ALLOWED",
                "etf": "ALLOWED",
                "leverage": "PROHIBITED",
                "futures": "PROHIBITED",
                "options": "PROHIBITED",
                "crypto": "PROHIBITED",
            },
        )
    )
    report = run_compliance_policy_worker(request)

    assert report["verdict"] == "READY"
    assert report["findings"] == []


def test_legal_query_mode_uses_llm_wiki_grep_bm25_arm() -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_answer_fn(query: str, as_of: str, mandate: str) -> dict[str, object]:
        calls.append((query, as_of, mandate))
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

    request = RiskMandateAssessmentRequest.model_validate(
        _mandate(query_mode="LEGAL_QUERY", compliance_query="부정거래행위 판단 기준")
    )
    report = run_compliance_policy_worker(request, legal_answer_fn=fake_answer_fn)

    assert calls and calls[0][0] == "부정거래행위 판단 기준"
    assert report["verdict"] == "ESCALATE"  # breach는 사람 확인이 필요하므로 ESCALATE, REJECT는 risk-runner만
    assert report["legal_status"] == "OK"
    assert report["cited_documents"] == ["자본시장법_제178조_부정거래행위등의금지"]
    assert report["tool_calls"] == ["llm_wiki_grep_bm25_answer"]


def test_legal_query_mode_escalates_without_defaulting_to_no_violation_when_search_fails() -> (
    None
):
    def boom(query: str, as_of: str, mandate: str) -> dict[str, object]:
        raise RuntimeError("openai unavailable")

    request = RiskMandateAssessmentRequest.model_validate(
        _mandate(query_mode="LEGAL_QUERY", compliance_query="아무 질문")
    )
    report = run_compliance_policy_worker(request, legal_answer_fn=boom)

    assert report["verdict"] == "ESCALATE"
    assert report["legal_status"] == "UNAVAILABLE"


def test_mixed_review_combines_policy_and_legal_reports() -> None:
    def fake_answer_fn(query: str, as_of: str, mandate: str) -> dict[str, object]:
        return {
            "answer": {
                "verdict": "no_breach",
                "cited_documents": [],
                "rationale": "근거 없음",
                "confidence": 0.6,
                "escalate": False,
            },
            "context_chars": 500,
            "pages_visited": ["자본시장법_제63조_임직원의금융투자상품매매"],
        }

    request = RiskMandateAssessmentRequest.model_validate(
        _mandate(query_mode="MIXED_REVIEW", compliance_query="임직원 매매명세 통지 주기")
    )
    report = run_compliance_policy_worker(request, legal_answer_fn=fake_answer_fn)

    assert set(report["sub_reports"]) == {"risk_policy_review", "legal_query"}
    assert report["sub_reports"]["legal_query"]["legal_verdict"] == "no_breach"
    # 정책 evidence가 없으므로 ESCALATE로 합쳐진다(evidence 없음을 위반없음으로 단정하지 않음)
    assert report["verdict"] == "ESCALATE"
