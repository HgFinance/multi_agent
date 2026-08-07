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
