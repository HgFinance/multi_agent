"""Risk Domain mandate endpoint tests without external DB or Pinecone."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

RISK_API_DIR = Path(__file__).resolve().parents[2] / "departments" / "03-risk" / "api"
sys.path.insert(0, str(RISK_API_DIR))
sys.modules.pop("app", None)

from app import app


def _body() -> dict[str, object]:
    return {
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


def test_domain_endpoint_dispatches_to_risk_head_and_two_employees() -> None:
    response = TestClient(app).post(
        "/risk/v1/mandates/MND-20260806-001/assess",
        json=_body(),
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dispatch"]["dispatcher"] == "risk-head"
    assert set(payload["employees"]) == {"risk-runner", "compliance-policy-worker"}
    assert payload["employees"]["risk-runner"]["input_hash"] == payload["employees"]["compliance-policy-worker"]["input_hash"]


def test_domain_endpoint_rejects_mismatched_path_mandate_id() -> None:
    response = TestClient(app).post(
        "/risk/v1/mandates/MND-OTHER/assess",
        json=_body(),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "MANDATE_ID_MISMATCH"
