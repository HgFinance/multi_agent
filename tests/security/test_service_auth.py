from __future__ import annotations

import time

import pytest

from apps.security.service_auth import ServiceAuthError, authenticate_service_token
from tests.security.service_auth_test_utils import make_token

SECRET = "test-service-auth-secret-0123456789abcdef"


def _authorization(**kwargs: object) -> str:
    return "Bearer " + make_token(
        SECRET,
        subject="svc-risk-operator",
        department="risk-management",
        service="risk-api",
        scopes=["risk.trading_state.write"],
        **kwargs,
    )


def test_service_token_requires_signature_and_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_AUTH_SECRET", SECRET)

    identity = authenticate_service_token(
        _authorization(),
        required_scope="risk.trading_state.write",
        expected_department="risk-management",
        expected_subject="svc-risk-operator",
        secret_env="SERVICE_AUTH_SECRET",
        now=time.time(),
    )

    assert identity.subject == "svc-risk-operator"
    assert "risk.trading_state.write" in identity.scopes


def test_service_token_denies_expired_or_wrong_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_AUTH_SECRET", SECRET)

    expired = _authorization(exp=int(time.time()) - 1)
    with pytest.raises(ServiceAuthError, match="만료"):
        authenticate_service_token(
            expired,
            required_scope="risk.trading_state.write",
            expected_department="risk-management",
            secret_env="SERVICE_AUTH_SECRET",
        )

    wrong_scope = "Bearer " + make_token(
        SECRET,
        subject="svc-risk-operator",
        department="risk-management",
        service="risk-api",
        scopes=["risk.trading_state.clear"],
    )
    with pytest.raises(ServiceAuthError) as exc_info:
        authenticate_service_token(
            wrong_scope,
            required_scope="risk.trading_state.write",
            expected_department="risk-management",
            secret_env="SERVICE_AUTH_SECRET",
        )
    assert exc_info.value.code == "SERVICE_SCOPE_DENIED"


def test_service_claim_can_be_bound_by_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_AUTH_SECRET", SECRET)
    authorization = "Bearer " + make_token(
        SECRET,
        subject="svc-risk-operator",
        department="risk-management",
        service="other-api",
        scopes=["risk.trading_state.write"],
    )
    with pytest.raises(ServiceAuthError) as exc_info:
        authenticate_service_token(
            authorization,
            required_scope="risk.trading_state.write",
            expected_department="risk-management",
            expected_service="risk-api",
            secret_env="SERVICE_AUTH_SECRET",
        )
    assert exc_info.value.code == "SERVICE_SERVICE_DENIED"


def test_missing_server_secret_is_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERVICE_AUTH_SECRET", raising=False)

    with pytest.raises(ServiceAuthError) as exc_info:
        authenticate_service_token(
            None,
            required_scope="risk.trading_state.write",
            expected_department="risk-management",
            secret_env="SERVICE_AUTH_SECRET",
        )

    assert exc_info.value.code == "SERVICE_AUTH_NOT_CONFIGURED"
    assert exc_info.value.status_code == 503
