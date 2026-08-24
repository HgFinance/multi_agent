"""api/app.py의 __main__ 자체 점검을 pytest로 옮긴 것.

소유: 동규 (리스크본부). trading-state(REDIS_URL 필요)/compliance(OPENAI_API_KEY 필요)
엔드포인트는 원본과 동일하게 자체 점검에서 제외한다 - 각 모듈 자체 점검이 그 경로를
이미 검증한다.

실행: python -m pytest departments/03-risk/tests/test_risk_app.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

sys.modules.pop("app", None)  # 06-ai-qa-audit도 모듈명 app이라 캐시 충돌 방지
from app import app
from risk_engine import RiskVerdict

now = datetime.now(timezone.utc)
fund, book, strategy, aapl = uuid4(), uuid4(), uuid4(), uuid4()
client = TestClient(app)


@pytest.fixture(autouse=True)
def request_context_mode(monkeypatch: pytest.MonkeyPatch):
    """Keep this request-contract suite independent from production .env."""

    monkeypatch.setenv("RISK_QA_RUNTIME", "")
    monkeypatch.setenv("RISK_CONTEXT_SOURCE", "request")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("RISK_QA_DATABASE_URL", "")


def order_intent_payload(qty="100", side="BUY") -> dict:
    return {
        "trade_case_id": str(uuid4()),
        "fund_id": str(fund),
        "book_id": str(book),
        "strategy_id": str(strategy),
        "instrument_id": str(aapl),
        "side": side,
        "order_type": "LIMIT",
        "quantity": qty,
        "limit_price": "70000",
        "time_in_force": "DAY",
        "valid_until": (now + timedelta(hours=1)).isoformat(),
        "snapshot": {
            "market_snapshot_id": "s1",
            "as_of": now.isoformat(),
            "bid": "69900",
            "ask": "70000",
        },
        "idempotency_key": "idem_api_001",
        "created_by": "trader-pm-agent",
        "trace_id": "t1",
    }


def context_payload(**overrides) -> dict:
    payload = {
        "mandate": {
            "fund_id": str(fund),
            "allowed_instrument_ids": None,
            "min_order_notional": "100000",
            "max_order_notional": "50000000",
        },
        "limits": {
            "soft_single_issuer_pct": "0.20",
            "hard_single_issuer_pct": "0.30",
            "max_daily_turnover_notional": "100000000",
            "max_daily_order_count": 50,
            "max_daily_loss": "10000000",
            "max_drawdown_pct": "0.20",
        },
        "restricted_items": [],
        "portfolio": {
            "fund_id": str(fund),
            "cash": "100000000",
            "buying_power": "100000000",
            "gross_exposure": "100000000",
            "peak_equity": "1000000000",
            "equity": "1000000000",
        },
        "market_status": {"tradable": True},
        "counterparty": {"broker_adapter": "paper", "health": "ok"},
        "trading_state": "ENABLED",
        "as_of": now.isoformat(),
    }
    payload.update(overrides)
    return payload


def test_01_normal_order_200_approve():
    r1 = client.post(
        "/investment-cases/case-1/risk-check",
        json={"order_intent": order_intent_payload(), "context": context_payload()},
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["decision"]["verdict"] == RiskVerdict.APPROVE.value, body1
    assert body1["calculation_version"] == "risk-p0-v1"
    assert len(body1["check_results"]) == 10


def test_02_halted_state_200_reject_with_reason_codes():
    r2 = client.post(
        "/investment-cases/case-1/risk-check",
        json={
            "order_intent": order_intent_payload(),
            "context": context_payload(trading_state="HALTED"),
        },
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["decision"]["verdict"] == RiskVerdict.REJECT.value, body2
    assert "trading_state_blocked" in body2["reason_codes"]


def test_03_invalid_trading_state_422_with_spec_envelope():
    r3 = client.post(
        "/investment-cases/case-1/risk-check",
        json={
            "order_intent": order_intent_payload(),
            "context": context_payload(trading_state="NOT_A_STATE"),
        },
    )
    assert r3.status_code == 422, r3.text
    assert r3.json()["error_code"] == "RequestValidationError", r3.json()


def test_04_risk_request_id_passthrough():
    fixed_id = str(uuid4())
    r4 = client.post(
        "/investment-cases/case-1/risk-check",
        json={
            "risk_request_id": fixed_id,
            "order_intent": order_intent_payload(qty="101"),
            "context": context_payload(),
        },
    )
    assert r4.json()["risk_request_id"] == fixed_id
