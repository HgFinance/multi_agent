from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt.exceptions import PyJWKClientConnectionError

from apps.api import current_user as auth


ISSUER = "https://test-project.supabase.co/auth/v1"
AUDIENCE = "authenticated"


def _jwt_env() -> dict[str, str]:
    return {
        "APP_ENV": "production",
        "PORTFOLIO_AUTH_MODE": "supabase_jwt",
        "PORTFOLIO_AUTH_REQUIRED": "false",
        "SUPABASE_AUTH_ISSUER": ISSUER,
        "SUPABASE_AUTH_AUDIENCE": AUDIENCE,
        "SUPABASE_AUTH_JWKS_URL": f"{ISSUER}/.well-known/jwks.json",
    }


def _token(private_key, subject: str, **overrides: object) -> str:
    now = datetime.now(timezone.utc)
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": subject,
        "role": "authenticated",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})


def _legacy_token(secret: str, subject: str, **overrides: object) -> str:
    now = datetime.now(timezone.utc)
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": subject,
        "role": "authenticated",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture
def signing_key():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, SimpleNamespace(key=private_key.public_key())


def test_fixture_header_requires_explicit_nonproduction_mode() -> None:
    with patch.dict(
        os.environ,
        {"APP_ENV": "production", "PORTFOLIO_AUTH_MODE": "fixture"},
        clear=False,
    ):
        with pytest.raises(HTTPException) as error:
            auth.authenticate_request_headers(
                authorization=None, x_user_id=str(uuid4()), required=False
            )
    assert error.value.status_code == 503
    assert error.value.detail == "portfolio_authentication_unavailable"


def test_explicit_test_fixture_accepts_header_and_can_be_optional() -> None:
    subject = str(uuid4())
    with patch.dict(
        os.environ,
        {
            "APP_ENV": "test",
            "PORTFOLIO_AUTH_MODE": "fixture",
            "PORTFOLIO_AUTH_REQUIRED": "false",
        },
        clear=False,
    ):
        assert (
            auth.authenticate_request_headers(
                authorization=None, x_user_id=subject, required=False
            )
            == subject
        )
        assert (
            auth.authenticate_request_headers(
                authorization=None, x_user_id=None, required=False
            )
            is None
        )


def test_current_user_uses_the_frontend_selected_user_without_bearer_verification() -> None:
    subject = str(uuid4())
    with patch.dict(os.environ, _jwt_env(), clear=False):
        assert auth.current_user(x_user_id=subject) == subject


def test_jwt_mode_never_accepts_x_user_id_without_bearer() -> None:
    with patch.dict(os.environ, _jwt_env(), clear=False):
        with pytest.raises(HTTPException) as error:
            auth.authenticate_request_headers(
                authorization=None, x_user_id=str(uuid4()), required=False
            )
    assert error.value.status_code == 401
    assert error.value.detail == "portfolio_authentication_required"


def test_valid_supabase_jwt_returns_sub_and_ignores_auth_required_false(signing_key) -> None:
    private_key, public_signing_key = signing_key
    subject = str(uuid4())
    token = _token(private_key, subject)
    client = SimpleNamespace(get_signing_key_from_jwt=lambda value: public_signing_key)
    with (
        patch.dict(os.environ, _jwt_env(), clear=False),
        patch.object(auth, "_jwks_client", return_value=client),
        patch.object(auth, "_project_verified_subject") as project_subject,
    ):
        assert (
            auth.authenticate_request_headers(
                authorization=f"Bearer {token}", x_user_id=subject, required=False
            )
            == subject
        )
    project_subject.assert_called_once_with(subject)


def _projection_connection(status: str = "ACTIVE"):
    cursor = MagicMock()
    cursor.fetchone.return_value = (status,)
    cursor_context = MagicMock()
    cursor_context.__enter__.return_value = cursor
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value = cursor_context
    return connection, cursor


def test_verified_subject_projection_is_idempotent_and_pii_minimal() -> None:
    subject = str(uuid4())
    connection, cursor = _projection_connection()
    with (
        patch.dict(
            os.environ,
            {
                "CONTROL_DATABASE_URL": "postgresql://control-db/portfolio",
                "DATABASE_URL": "postgresql://ignored-hosted-db/portfolio",
            },
            clear=False,
        ),
        patch.object(auth.psycopg2, "connect", return_value=connection) as connect,
    ):
        auth._project_verified_subject(subject)

    connect.assert_called_once_with(
        "postgresql://control-db/portfolio", connect_timeout=5
    )
    sql, parameters = cursor.execute.call_args.args
    normalized_sql = " ".join(sql.split()).casefold()
    assert "on conflict (user_id) do update" in normalized_sql
    assert "auth_subject_observed_at" in normalized_sql
    assert "status = excluded.status" not in normalized_sql
    assert parameters == (subject, "Authenticated Supabase user")
    assert "@" not in parameters[1]


@pytest.mark.parametrize("status", ["SUSPENDED", "CLOSED"])
def test_verified_subject_projection_never_revives_inactive_users(status: str) -> None:
    connection, _ = _projection_connection(status)
    with (
        patch.dict(
            os.environ,
            {"CONTROL_DATABASE_URL": "postgresql://control-db/portfolio"},
            clear=False,
        ),
        patch.object(auth.psycopg2, "connect", return_value=connection),
        pytest.raises(HTTPException) as error,
    ):
        auth._project_verified_subject(str(uuid4()))

    assert error.value.status_code == 403
    assert error.value.detail == "portfolio_user_inactive"


def test_verified_subject_projection_database_failure_is_fail_closed() -> None:
    with (
        patch.dict(
            os.environ,
            {"CONTROL_DATABASE_URL": "postgresql://control-db/portfolio"},
            clear=False,
        ),
        patch.object(
            auth.psycopg2,
            "connect",
            side_effect=auth.psycopg2.OperationalError("offline"),
        ),
        pytest.raises(HTTPException) as error,
    ):
        auth._project_verified_subject(str(uuid4()))

    assert error.value.status_code == 503
    assert error.value.detail == "portfolio_identity_projection_unavailable"


def test_verified_subject_projection_requires_control_database() -> None:
    with (
        patch.dict(
            os.environ,
            {"CONTROL_DATABASE_URL": "", "DATABASE_URL": ""},
            clear=False,
        ),
        pytest.raises(HTTPException) as error,
    ):
        auth._project_verified_subject(str(uuid4()))

    assert error.value.status_code == 503
    assert error.value.detail == "portfolio_identity_projection_unavailable"


def test_authorized_funds_include_only_rows_returned_by_active_grant_query() -> None:
    subject = str(uuid4())
    fund_id = str(uuid4())
    now = datetime.now(timezone.utc)
    connection, cursor = _projection_connection()
    cursor.fetchall.return_value = [
        (fund_id, "OWNER", "ACTIVE", now, None),
        (fund_id, "TRADER", "ACTIVE", now, now + timedelta(days=1)),
    ]
    with (
        patch.dict(
            os.environ,
            {"CONTROL_DATABASE_URL": "postgresql://control-db/portfolio"},
            clear=False,
        ),
        patch.object(auth.psycopg2, "connect", return_value=connection),
    ):
        memberships = auth.authorized_fund_memberships(subject)

    assert [row["role"] for row in memberships] == ["OWNER", "TRADER"]
    sql = " ".join(cursor.execute.call_args.args[0].split()).casefold()
    assert "up.status = 'active'" in sql
    assert "fm.status = 'active'" in sql
    assert "fm.effective_from <= now()" in sql
    assert "fm.effective_to is null or fm.effective_to > now()" in sql


def test_fund_scope_without_effective_membership_is_forbidden() -> None:
    subject = str(uuid4())
    with (
        patch.dict(os.environ, _jwt_env(), clear=False),
        patch.object(auth, "authorized_fund_memberships", return_value=[]),
        pytest.raises(HTTPException) as error,
    ):
        auth.require_fund_membership(subject, str(uuid4()))

    assert error.value.status_code == 403
    assert error.value.detail == "portfolio_fund_forbidden"


def test_fixture_identity_never_creates_or_requires_a_fund_grant() -> None:
    with (
        patch.dict(
            os.environ,
            {"APP_ENV": "test", "PORTFOLIO_AUTH_MODE": "fixture"},
            clear=False,
        ),
        patch.object(auth, "authorized_fund_memberships") as memberships,
    ):
        auth.require_fund_membership("fixture-user", "fixture-fund")

    memberships.assert_not_called()


def test_active_user_profile_rejects_unprovisioned_subject() -> None:
    connection, cursor = _projection_connection()
    cursor.fetchone.return_value = None
    with (
        patch.dict(
            os.environ,
            {"CONTROL_DATABASE_URL": "postgresql://control-db/portfolio"},
            clear=False,
        ),
        patch.object(auth.psycopg2, "connect", return_value=connection),
        pytest.raises(HTTPException) as error,
    ):
        auth.active_user_profile(str(uuid4()))

    assert error.value.status_code == 403
    assert error.value.detail == "portfolio_user_not_provisioned"


@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"iss": "https://attacker.example/auth/v1"},
        {"aud": "service_role"},
        {"role": "service_role"},
        {"exp": datetime.now(timezone.utc) - timedelta(seconds=1)},
    ],
)
def test_supabase_jwt_rejects_invalid_security_claims(signing_key, claim_overrides) -> None:
    private_key, public_signing_key = signing_key
    token = _token(private_key, str(uuid4()), **claim_overrides)
    client = SimpleNamespace(get_signing_key_from_jwt=lambda value: public_signing_key)
    with (
        patch.dict(os.environ, _jwt_env(), clear=False),
        patch.object(auth, "_jwks_client", return_value=client),
    ):
        with pytest.raises(HTTPException) as error:
            auth.authenticate_request_headers(
                authorization=f"Bearer {token}", x_user_id=None
            )
    assert error.value.status_code == 401
    assert error.value.detail == "portfolio_access_token_invalid"


def test_signed_subject_cannot_be_overridden_by_unsigned_header(signing_key) -> None:
    private_key, public_signing_key = signing_key
    token = _token(private_key, str(uuid4()))
    client = SimpleNamespace(get_signing_key_from_jwt=lambda value: public_signing_key)
    with (
        patch.dict(os.environ, _jwt_env(), clear=False),
        patch.object(auth, "_jwks_client", return_value=client),
    ):
        with pytest.raises(HTTPException) as error:
            auth.authenticate_request_headers(
                authorization=f"Bearer {token}", x_user_id=str(uuid4())
            )
    assert error.value.status_code == 403
    assert error.value.detail == "portfolio_identity_header_mismatch"


def test_asymmetric_token_rejects_signature_from_untrusted_key(signing_key) -> None:
    private_key, _ = signing_key
    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _token(private_key, str(uuid4()))
    client = SimpleNamespace(
        get_signing_key_from_jwt=lambda value: SimpleNamespace(
            key=other_private_key.public_key()
        )
    )
    with (
        patch.dict(os.environ, _jwt_env(), clear=False),
        patch.object(auth, "_jwks_client", return_value=client),
    ):
        with pytest.raises(HTTPException) as error:
            auth.verify_supabase_access_token(token)
    assert error.value.status_code == 401
    assert error.value.detail == "portfolio_access_token_invalid"


def test_jwks_network_failure_is_availability_error_not_bad_identity(signing_key) -> None:
    private_key, _ = signing_key
    token = _token(private_key, str(uuid4()))

    def unavailable(_token: str):
        raise PyJWKClientConnectionError("offline")

    client = SimpleNamespace(get_signing_key_from_jwt=unavailable)
    with (
        patch.dict(os.environ, _jwt_env(), clear=False),
        patch.object(auth, "_jwks_client", return_value=client),
    ):
        with pytest.raises(HTTPException) as error:
            auth.authenticate_request_headers(
                authorization=f"Bearer {token}", x_user_id=None
            )
    assert error.value.status_code == 503
    assert error.value.detail == "portfolio_authentication_unavailable"


def test_legacy_hs256_uses_auth_user_endpoint_and_binds_returned_user() -> None:
    subject = str(uuid4())
    token = _legacy_token("test-only-secret-that-is-not-used-by-the-bff", subject)
    environment = {
        **_jwt_env(),
        "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test-only",
    }
    with (
        patch.dict(os.environ, environment, clear=False),
        patch.object(auth, "_fetch_supabase_user_id", return_value=subject) as fetch,
    ):
        assert auth.verify_supabase_access_token(token) == subject
    fetch.assert_called_once_with(
        user_url=f"{ISSUER}/user",
        api_key="sb_publishable_test-only",
        token=token,
    )


def test_legacy_hs256_rejects_user_endpoint_subject_mismatch() -> None:
    token = _legacy_token(
        "test-only-secret-that-is-not-used-by-the-bff", str(uuid4())
    )
    environment = {
        **_jwt_env(),
        "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test-only",
    }
    with (
        patch.dict(os.environ, environment, clear=False),
        patch.object(auth, "_fetch_supabase_user_id", return_value=str(uuid4())),
    ):
        with pytest.raises(HTTPException) as error:
            auth.verify_supabase_access_token(token)
    assert error.value.status_code == 401
    assert error.value.detail == "portfolio_access_token_invalid"


def test_legacy_hs256_rejects_service_role_key_configuration() -> None:
    subject = str(uuid4())
    token = _legacy_token("test-only-secret-that-is-not-used-by-the-bff", subject)
    now = datetime.now(timezone.utc)
    service_role_key = jwt.encode(
        {
            "role": "service_role",
            "iss": "supabase",
            "iat": now,
            "exp": now + timedelta(days=1),
        },
        "test-key-secret-at-least-thirty-two-bytes",
        algorithm="HS256",
    )
    environment = {
        **_jwt_env(),
        "SUPABASE_PUBLISHABLE_KEY": "",
        "SUPABASE_ANON_KEY": service_role_key,
    }
    with patch.dict(os.environ, environment, clear=False):
        with pytest.raises(HTTPException) as error:
            auth.verify_supabase_access_token(token)
    assert error.value.status_code == 503
    assert error.value.detail == "portfolio_authentication_unavailable"
