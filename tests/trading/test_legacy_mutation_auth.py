from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.security.service_auth_test_utils import make_token


TRADING_ROOT = Path(__file__).resolve().parents[2] / "departments" / "02-trading"
sys.path.insert(0, str(TRADING_ROOT / "api"))
sys.modules.pop("app", None)
import app as trading_api  # noqa: E402


SECRET = "test-internal-trading-auth-secret-0123456789"
USER_PROOF_SECRET = "test-user-directive-proof-secret-0123456789"
ISSUER = "test-service-issuer"
AUDIENCE = "trading-api"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TRADING_LEGACY_OFFLINE_MODE", "fixture")
    monkeypatch.setenv("TRADING_EXECUTION_MODE", "PAPER")
    monkeypatch.setenv("TRADING_BROKER_ADAPTER", "paper")
    monkeypatch.setenv("TRADING_DIRECTIVE_REPOSITORY", "memory")
    monkeypatch.setenv("TRADING_AUTH_MODE", "fixture")
    monkeypatch.setenv("TRADING_INTERNAL_SERVICE_AUTH_SECRET", SECRET)
    monkeypatch.setenv("TRADING_INTERNAL_SERVICE_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("TRADING_INTERNAL_SERVICE_AUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("TRADING_INTERNAL_AUTH_CLOCK_SKEW_SECONDS", "0")
    monkeypatch.setenv("TRADING_INTERNAL_AUTH_MAX_TTL_SECONDS", "300")
    monkeypatch.setenv("TRADING_SERVICE_AUTH_SECRET", USER_PROOF_SECRET)
    monkeypatch.setattr(trading_api, "_paper_db_error", None)
    monkeypatch.setattr(trading_api, "_paper_db_durable", False)
    return TestClient(trading_api.app)


def _headers(
    *,
    subject: str,
    department: str,
    service: str,
    scopes: list[str],
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    issued_at: int | None = None,
    expires_at: int | None = None,
) -> dict[str, str]:
    now = int(time.time()) if issued_at is None else issued_at
    token = make_token(
        SECRET,
        subject=subject,
        department=department,
        service=service,
        scopes=scopes,
        iss=issuer,
        aud=audience,
        jti=f"test-{uuid4()}",
        iat=now,
        nbf=now,
        # Parametrized headers are materialized during collection.  Keep the
        # default comfortably inside the configured 300-second maximum while
        # allowing the full Windows suite to run for more than one minute.
        exp=expires_at if expires_at is not None else now + 240,
    )
    return {"Authorization": f"Bearer {token}"}


def _intent_body(*, created_by: str = "svc-trading-hermes") -> dict:
    now = datetime.now(timezone.utc)
    return {
        "trade_case_id": str(uuid4()),
        "fund_id": str(uuid4()),
        "book_id": str(uuid4()),
        "strategy_id": str(uuid4()),
        "instrument_id": str(uuid4()),
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": "1",
        "limit_price": "70000",
        "valid_until": (now + timedelta(hours=1)).isoformat(),
        "snapshot": {
            "market_snapshot_id": f"snapshot-{uuid4()}",
            "as_of": now.isoformat(),
            "bid": "69900",
            "ask": "70000",
        },
        "idempotency_key": f"intent-{uuid4()}",
        "created_by": created_by,
        "trace_id": f"trace-{uuid4()}",
    }


def _risk_body(intent_id: str, *, decided_by: str = "svc-risk-api") -> dict:
    return {
        "risk_decision_id": str(uuid4()),
        "order_intent_id": intent_id,
        "verdict": "approve",
        "approved_quantity": "1",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "reason": "test",
        "decided_by": decided_by,
    }


def _broker_event() -> dict:
    return {
        "event_type": "ack",
        "broker_event_id": f"broker-event-{uuid4()}",
        "event_time": datetime.now(timezone.utc).isoformat(),
        "payload": {"broker_order_id": "paper-test"},
    }


def test_every_legacy_mutation_is_fail_closed_without_service_auth(client: TestClient) -> None:
    intent_id = str(uuid4())
    order_id = str(uuid4())
    case_id = str(uuid4())
    calls = (
        ("post", "/trading/v1/order-intents", _intent_body()),
        ("post", f"/trading/v1/order-intents/{intent_id}/risk-review", None),
        ("post", f"/trading/v1/order-intents/{intent_id}/risk-decision", _risk_body(intent_id)),
        ("post", "/trading/v1/orders", {"order_intent_id": intent_id}),
        ("post", f"/trading/v1/orders/{order_id}/submit", None),
        ("post", f"/trading/v1/orders/{order_id}/cancel", {"reason": "test"}),
        ("post", f"/trading/v1/orders/{order_id}/broker-events", _broker_event()),
        ("post", f"/trading/v1/orders/{order_id}/unknown", {"reason": "timeout"}),
        ("post", f"/investment-cases/{case_id}/paper-orders", {"order_intent_id": intent_id}),
        ("post", f"/investment-cases/{case_id}/cancel", {"reason": "test"}),
    )

    for method, path, body in calls:
        response = client.request(method, path, json=body)
        assert response.status_code == 401, (path, response.text)
        assert response.json()["error_code"] == "TRADING_INTERNAL_AUTH_REQUIRED"


def test_missing_internal_auth_configuration_fails_closed_as_503(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRADING_INTERNAL_SERVICE_AUTH_SECRET", raising=False)

    response = client.post("/trading/v1/order-intents", json=_intent_body())

    assert response.status_code == 503
    assert response.json()["error_code"] == "TRADING_INTERNAL_AUTH_NOT_CONFIGURED"


def test_internal_and_user_directive_planes_cannot_share_a_signing_secret(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_SERVICE_AUTH_SECRET", SECRET)

    response = client.post("/trading/v1/order-intents", json=_intent_body())

    assert response.status_code == 503
    assert response.json()["error_code"] == "TRADING_INTERNAL_AUTH_NOT_CONFIGURED"


def test_trading_hermes_intent_scope_cannot_approve_submit_fill_or_cancel(
    client: TestClient,
) -> None:
    hermes = _headers(
        subject="svc-trading-hermes",
        department="trading-department",
        service="trading-hermes",
        scopes=["trading.intent.write"],
    )
    created = client.post("/trading/v1/order-intents", json=_intent_body(), headers=hermes)
    assert created.status_code == 201, created.text
    intent_id = created.json()["order_intent_id"]
    reviewed = client.post(
        f"/trading/v1/order-intents/{intent_id}/risk-review", headers=hermes
    )
    assert reviewed.status_code == 200, reviewed.text

    order_id = str(uuid4())
    denied = (
        client.post(
            f"/trading/v1/order-intents/{intent_id}/risk-decision",
            json=_risk_body(intent_id, decided_by="svc-trading-hermes"),
            headers=hermes,
        ),
        client.post(
            "/trading/v1/orders", json={"order_intent_id": intent_id}, headers=hermes
        ),
        client.post(f"/trading/v1/orders/{order_id}/submit", headers=hermes),
        client.post(
            f"/trading/v1/orders/{order_id}/broker-events",
            json=_broker_event(),
            headers=hermes,
        ),
        client.post(
            f"/trading/v1/orders/{order_id}/cancel",
            json={"reason": "attempted escalation"},
            headers=hermes,
        ),
    )
    assert all(response.status_code == 403 for response in denied)
    assert all(
        response.json()["error_code"]
        in {"TRADING_INTERNAL_AUTH_IDENTITY_DENIED", "TRADING_INTERNAL_AUTH_SCOPE_DENIED"}
        for response in denied
    )


@pytest.mark.parametrize(
    ("headers", "expected_code"),
    [
        (
            _headers(
                subject="svc-trading-hermes",
                department="trading-department",
                service="trading-hermes",
                scopes=["trading.intent.write"],
                issuer="wrong-issuer",
            ),
            "TRADING_INTERNAL_AUTH_ISSUER_DENIED",
        ),
        (
            _headers(
                subject="svc-trading-hermes",
                department="trading-department",
                service="trading-hermes",
                scopes=["trading.intent.write"],
                audience="wrong-audience",
            ),
            "TRADING_INTERNAL_AUTH_AUDIENCE_DENIED",
        ),
        (
            _headers(
                subject="svc-trading-hermes",
                department="trading-department",
                service="other-service",
                scopes=["trading.intent.write"],
            ),
            "TRADING_INTERNAL_AUTH_IDENTITY_DENIED",
        ),
        (
            _headers(
                subject="svc-trading-hermes",
                department="trading-department",
                service="trading-hermes",
                scopes=["trading.order.submit"],
            ),
            "TRADING_INTERNAL_AUTH_SCOPE_DENIED",
        ),
        (
            _headers(
                subject="svc-trading-hermes",
                department="trading-department",
                service="trading-hermes",
                scopes=["trading.intent.write"],
                issued_at=int(time.time()) - 120,
                expires_at=int(time.time()) - 60,
            ),
            "TRADING_INTERNAL_AUTH_EXPIRED",
        ),
    ],
)
def test_bad_issuer_audience_identity_scope_and_expiry_are_denied(
    client: TestClient,
    headers: dict[str, str],
    expected_code: str,
) -> None:
    response = client.post("/trading/v1/order-intents", json=_intent_body(), headers=headers)
    assert response.status_code in {401, 403}
    assert response.json()["error_code"] == expected_code


def test_identity_is_bound_to_created_by_and_decided_by(client: TestClient) -> None:
    intent_headers = _headers(
        subject="svc-trading-hermes",
        department="trading-department",
        service="trading-hermes",
        scopes=["trading.intent.write"],
    )
    mismatch = client.post(
        "/trading/v1/order-intents",
        json=_intent_body(created_by="some-other-principal"),
        headers=intent_headers,
    )
    assert mismatch.status_code == 403
    assert mismatch.json()["error_code"] == "TRADING_INTERNAL_AUTH_SUBJECT_MISMATCH"

    intent_id = str(uuid4())
    risk_headers = _headers(
        subject="svc-risk-api",
        department="risk-management",
        service="risk-api",
        scopes=["trading.risk_decision.write"],
    )
    risk_mismatch = client.post(
        f"/trading/v1/order-intents/{intent_id}/risk-decision",
        json=_risk_body(intent_id, decided_by="some-other-principal"),
        headers=risk_headers,
    )
    assert risk_mismatch.status_code == 403
    assert risk_mismatch.json()["error_code"] == "TRADING_INTERNAL_AUTH_SUBJECT_MISMATCH"


def test_legitimate_service_roles_pass_auth_before_domain_validation(client: TestClient) -> None:
    intent_id = str(uuid4())
    order_id = str(uuid4())
    case_id = str(uuid4())
    risk = _headers(
        subject="svc-risk-api",
        department="risk-management",
        service="risk-api",
        scopes=["trading.risk_decision.write"],
    )
    submit = _headers(
        subject="svc-trading-oms",
        department="trading-department",
        service="trading-oms",
        scopes=["trading.order.submit", "trading.broker_event.write"],
    )
    broker = _headers(
        subject="svc-paper-broker",
        department="trading-department",
        service="paper-broker-adapter",
        scopes=["trading.broker_event.write"],
    )
    cancel = _headers(
        subject="svc-trading-oms",
        department="trading-department",
        service="trading-oms",
        scopes=["trading.order.cancel"],
    )

    responses = (
        client.post(
            f"/trading/v1/order-intents/{intent_id}/risk-decision",
            json=_risk_body(intent_id),
            headers=risk,
        ),
        client.post(
            "/trading/v1/orders", json={"order_intent_id": intent_id}, headers=submit
        ),
        client.post(f"/trading/v1/orders/{order_id}/submit", headers=submit),
        client.post(
            f"/trading/v1/orders/{order_id}/broker-events",
            json=_broker_event(),
            headers=broker,
        ),
        client.post(
            f"/trading/v1/orders/{order_id}/unknown",
            json={"reason": "timeout"},
            headers=broker,
        ),
        client.post(
            f"/trading/v1/orders/{order_id}/cancel",
            json={"reason": "test"},
            headers=cancel,
        ),
        client.post(
            f"/investment-cases/{case_id}/paper-orders",
            json={"order_intent_id": intent_id},
            headers=submit,
        ),
        client.post(
            f"/investment-cases/{case_id}/cancel",
            json={"reason": "test"},
            headers=cancel,
        ),
    )
    statuses = [response.status_code for response in responses]
    assert statuses == [404, 404, 404, 404, 404, 404, 404, 200], [
        response.text for response in responses
    ]
    assert not any(status in {401, 403} for status in statuses)


def test_production_legacy_mutation_requires_durable_paper_store(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("TRADING_LEGACY_OFFLINE_MODE", raising=False)
    monkeypatch.setattr(trading_api, "_paper_db_error", None)
    monkeypatch.setattr(trading_api, "_paper_db_durable", False)
    headers = _headers(
        subject="svc-trading-hermes",
        department="trading-department",
        service="trading-hermes",
        scopes=["trading.intent.write"],
    )

    response = client.post("/trading/v1/order-intents", json=_intent_body(), headers=headers)

    assert response.status_code == 503
    assert response.json()["error_code"] == "TRADING_DURABLE_STORE_REQUIRED"


@pytest.mark.parametrize(
    ("app_env", "offline_mode"),
    [
        ("development", "fixture"),
        ("staging", "fixture"),
        ("local", ""),
        ("test", ""),
    ],
)
def test_memory_store_requires_both_local_test_env_and_explicit_fixture_flag(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
    offline_mode: str,
) -> None:
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("TRADING_LEGACY_OFFLINE_MODE", offline_mode)
    headers = _headers(
        subject="svc-trading-hermes",
        department="trading-department",
        service="trading-hermes",
        scopes=["trading.intent.write"],
    )

    response = client.post("/trading/v1/order-intents", json=_intent_body(), headers=headers)

    assert response.status_code == 503
    assert response.json()["error_code"] == "TRADING_DURABLE_STORE_REQUIRED"


def test_health_and_read_only_routes_remain_public(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    tick = client.get("/trading/v1/market-rules/tick-size", params={"price": "70000"})
    assert tick.status_code == 200
    missing = client.get(f"/trading/v1/orders/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "TRADING_ORDER_NOT_FOUND"


def test_trading_image_and_compose_ship_the_internal_auth_boundary() -> None:
    dockerfile = (TRADING_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY api ./api" in dockerfile
    assert (TRADING_ROOT / "api" / "internal_service_auth.py").is_file()

    for path in (
        TRADING_ROOT / "compose.yaml",
        Path(__file__).resolve().parents[2] / "deploy" / "eb" / "docker-compose.yml",
    ):
        compose = path.read_text(encoding="utf-8")
        assert "TRADING_INTERNAL_SERVICE_AUTH_SECRET:" in compose
        assert "TRADING_INTERNAL_SERVICE_AUTH_ISSUER:" in compose
        assert "TRADING_INTERNAL_SERVICE_AUTH_AUDIENCE:" in compose

    root = Path(__file__).resolve().parents[2]
    compose_paths = [root / "docker-compose.yml", *root.glob("departments/*/compose.yaml")]
    for compose_path in compose_paths:
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
        for service_name, service in (compose.get("services") or {}).items():
            if "hermes" not in service_name:
                continue
            environment = service.get("environment") or {}
            if isinstance(environment, dict):
                assert "TRADING_INTERNAL_SERVICE_AUTH_SECRET" not in environment
            else:
                assert not any(
                    str(item).startswith("TRADING_INTERNAL_SERVICE_AUTH_SECRET=")
                    for item in environment
                )

    for profile in root.glob("departments/*/hermes/config.yaml"):
        assert "TRADING_INTERNAL_SERVICE_AUTH_SECRET" not in profile.read_text(
            encoding="utf-8"
        )
