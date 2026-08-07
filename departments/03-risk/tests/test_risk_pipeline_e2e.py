from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.pinecone_client import PineconeEvidenceClient
from risk_mandate_workers import assess_mandate


def _mandate() -> dict[str, object]:
    return {
        "mandate_id": "MND-E2E-001",
        "event_id": "EVT-E2E-001",
        "as_of": "2026-08-07T00:00:00Z",
        "investor_profile": {
            "investment_goal": "Capital preservation",
            "risk_tolerance": "CONSERVATIVE",
            "financial_experience_years": 0,
            "perceived_risk_awareness": True,
        },
        "portfolio_constraints": {
            "base_capital": 100000000,
            "max_single_stock_weight": 0.30,
            "max_total_exposure": 2.0,
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
        "portfolio_snapshot": {
            "current_var": 10,
            "var_limit": 20,
            "total_exposure": 0.50,
            "current_drawdown": -0.01,
            "positions": [],
        },
        "compliance_query": "Check the current portfolio concentration policy.",
        "policy_query_vector": [0.1, 0.2],
    }


def test_risk_e2e_keeps_risk_engine_authoritative_and_returns_policy_metadata() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "matches": [
                    {
                        "id": "policy-match-1",
                        "score": 0.93,
                        "metadata": {
                            "chunk_id": "chunk-1",
                            "document_id": "policy-001",
                            "version": "v1",
                            "clause_id": "12",
                            "effective_from": "2026-01-01",
                            "effective_to": None,
                            "title": "Concentration policy",
                            "text": "The mandate concentration limit applies.",
                        },
                    }
                ]
            },
        )

    with PineconeEvidenceClient(
        api_key="test-key",
        index_host="https://pinecone.example",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ) as client:
        result = assess_mandate(_mandate(), pinecone=client)

    assert result["pipeline_status"] == "COMPLETED"
    assert result["employees"]["risk-runner"]["authoritative"] is True
    assert result["employees"]["risk-runner"]["verdict"] == "APPROVE"
    assert result["employees"]["compliance-policy-worker"]["authoritative"] is False
    assert result["employees"]["compliance-policy-worker"]["evidence_refs"] == [
        "policy-match-1"
    ]
    assert result["risk_head"]["binding"] is False


def test_risk_e2e_records_order_compliance_tool_and_rejects_var_breach() -> None:
    payload = _mandate()
    payload["order_compliance"] = {
        "mandate_id": "MND-E2E-001",
        "symbol": "005930",
        "side": "BUY",
        "notional": "1000000",
        "current_position_notional": "1000000",
        "resulting_position_notional": "2000000",
        "current_exposure_pct": "0.10",
        "resulting_exposure_pct": "0.20",
        "max_exposure_pct": "0.30",
        "portfolio_var": "25",
        "portfolio_var_limit": "20",
        "as_of": "2026-08-07T00:00:00Z",
    }

    result = assess_mandate(payload)

    assert result["employees"]["risk-runner"]["verdict"] == "REJECT"
    assert result["employees"]["risk-runner"]["tool_calls"] == [
        "evaluate_order_compliance"
    ]
    assert (
        result["employees"]["risk-runner"]["order_compliance"]["authoritative"] is True
    )
