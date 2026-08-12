from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
sys.modules.pop("app", None)
import app as risk_api

from tests.security.service_auth_test_utils import make_token

RISK_TEST_SECRET = "test-risk-service-auth-secret-0123456789"


class _FakeStateStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def set_state(self, scope, state, reason, set_by):
        self.calls.append((scope, state.value, reason, set_by))
        return {
            "scope": scope,
            "state": state.value,
            "reason": reason,
            "set_by": set_by,
        }

    def clear_state(self, scope):
        self.calls.append((scope, "CLEARED", "", ""))


def _headers(
    subject: str,
    scopes: list[str],
    *,
    service: str = "risk-api",
) -> dict[str, str]:
    token = make_token(
        RISK_TEST_SECRET,
        subject=subject,
        department="risk-management",
        service=service,
        scopes=scopes,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RISK_SERVICE_AUTH_SECRET", RISK_TEST_SECRET)
    store = _FakeStateStore()
    monkeypatch.setattr(risk_api, "_state_store", store)
    return TestClient(risk_api.app), store


def test_trading_state_write_requires_signed_service_identity(client):
    http, store = client
    body = {"state": "ENTRY_BLOCKED", "reason": "test", "set_by": "svc-risk-operator"}

    missing = http.put("/risk/v1/trading-state/fund:test", json=body)
    assert missing.status_code == 401

    wrong_scope = http.put(
        "/risk/v1/trading-state/fund:test",
        json=body,
        headers=_headers("svc-risk-operator", ["risk.trading_state.clear"]),
    )
    assert wrong_scope.status_code == 403
    wrong_service = http.put(
        "/risk/v1/trading-state/fund:test",
        json=body,
        headers=_headers(
            "svc-risk-operator", ["risk.trading_state.write"], service="other-api"
        ),
    )
    assert wrong_service.status_code == 403

    accepted = http.put(
        "/risk/v1/trading-state/fund:test",
        json=body,
        headers=_headers("svc-risk-operator", ["risk.trading_state.write"]),
    )
    assert accepted.status_code == 200
    assert store.calls[-1][-1] == "svc-risk-operator"


def test_trading_state_clear_requires_scope_and_is_identity_attributed(client):
    http, store = client

    cleared = http.delete(
        "/risk/v1/trading-state/fund:test",
        headers=_headers("svc-risk-operator", ["risk.trading_state.clear"]),
    )

    assert cleared.status_code == 200
    assert cleared.json()["cleared_by"] == "svc-risk-operator"
    assert store.calls[-1][1] == "CLEARED"
