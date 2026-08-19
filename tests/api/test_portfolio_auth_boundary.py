from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from apps.api import main as bff_main
from apps.api import ceo_mirror_api
from apps.api.ceo_mirror import CanonicalIngress, InMemoryMirrorStore, execute_once
from apps.api.main import app


def test_ui_boundary_does_not_require_a_bearer_token() -> None:
    with patch.dict(
        os.environ,
        {
            "APP_ENV": "production",
            "PORTFOLIO_AUTH_MODE": "supabase_jwt",
            "PORTFOLIO_AUTH_REQUIRED": "false",
            "SUPABASE_URL": "https://test-project.supabase.co",
        },
        clear=False,
    ):
        response = TestClient(app).get("/ui/integrations")
    assert response.status_code == 200


def test_ui_boundary_does_not_interpret_fixture_mode() -> None:
    with patch.dict(
        os.environ,
        {"APP_ENV": "production", "PORTFOLIO_AUTH_MODE": "fixture"},
        clear=False,
    ):
        response = TestClient(app).get("/ui/integrations")
    assert response.status_code == 200


def test_ui_boundary_allows_explicit_local_fixture_mode() -> None:
    with patch.dict(
        os.environ,
        {
            "APP_ENV": "test",
            "PORTFOLIO_AUTH_MODE": "fixture",
            "PORTFOLIO_AUTH_REQUIRED": "false",
        },
        clear=False,
    ):
        response = TestClient(app).get("/ui/integrations")
    assert response.status_code == 200


def test_x_user_id_preflight_is_explicitly_allowed() -> None:
    response = TestClient(app).options(
        "/ui/mandates/mnd-1",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "x-user-id,content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "PUT" in response.headers["access-control-allow-methods"]
    assert "X-User-Id" in response.headers["access-control-allow-headers"]
    assert response.headers.get("access-control-allow-credentials") is None


def test_allowed_origin_receives_cors_headers_without_authentication() -> None:
    with patch.dict(
        os.environ,
        {
            "APP_ENV": "production",
            "PORTFOLIO_AUTH_MODE": "supabase_jwt",
            "PORTFOLIO_AUTH_REQUIRED": "false",
        },
        clear=False,
    ):
        response = TestClient(app).get(
            "/ui/integrations", headers={"Origin": "http://localhost:3000"}
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_cors_origin_configuration_is_exact_and_fail_closed() -> None:
    with patch.dict(
        os.environ,
        {
            "APP_ENV": "production",
            "PORTFOLIO_CORS_ALLOW_ORIGINS": (
                "https://app.example.com,https://ops.example.com/"
            ),
        },
        clear=False,
    ):
        assert bff_main._portfolio_cors_origins() == [
            "https://app.example.com",
            "https://ops.example.com",
        ]

    with patch.dict(
        os.environ,
        {"APP_ENV": "production", "PORTFOLIO_CORS_ALLOW_ORIGINS": ""},
        clear=False,
    ):
        assert bff_main._portfolio_cors_origins() == []

    with patch.dict(
        os.environ,
        {"APP_ENV": "production", "PORTFOLIO_CORS_ALLOW_ORIGINS": ""},
        clear=False,
    ):
        origins = bff_main._portfolio_cors_origins()
        backend_only = FastAPI()

        @backend_only.get("/private")
        def private() -> dict[str, bool]:
            return {"ok": True}

        backend_only.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET"],
            allow_headers=["Authorization"],
        )
        client = TestClient(backend_only)
        simple = client.get(
            "/private", headers={"Origin": "https://untrusted.example"}
        )
        preflight = client.options(
            "/private",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )

    assert origins == []
    assert simple.status_code == 200
    assert "access-control-allow-origin" not in simple.headers
    assert preflight.status_code == 400
    assert "access-control-allow-origin" not in preflight.headers


def test_production_cors_allows_an_explicit_http_origin() -> None:
    with patch.dict(
        os.environ,
        {
            "APP_ENV": "production",
            "PORTFOLIO_CORS_ALLOW_ORIGINS": "http://localhost:3002",
        },
        clear=False,
    ):
        assert bff_main._portfolio_cors_origins() == ["http://localhost:3002"]


@pytest.mark.parametrize(
    "value",
    (
        "*",
        "https://*.example.com",
        "https://user:password@app.example.com",
        "https://app.example.com/path",
        "https://app.example.com?query=1",
        "https://app.example.com#fragment",
        "https://app.example.com:99999",
        "https://[::1",
        "https://app.example.com,",
        "https://app.example.com,,https://ops.example.com",
        "not-an-origin",
    ),
)
def test_production_cors_rejects_every_non_exact_or_malformed_origin(
    value: str,
) -> None:
    with patch.dict(
        os.environ,
        {"APP_ENV": "production", "PORTFOLIO_CORS_ALLOW_ORIGINS": value},
        clear=False,
    ):
        with pytest.raises(RuntimeError, match="invalid PORTFOLIO_CORS"):
            bff_main._portfolio_cors_origins()


def test_ui_me_returns_only_verified_profile_and_effective_funds() -> None:
    owner_id = str(uuid4())
    fund_id = str(uuid4())
    with (
        patch.dict(
            os.environ,
            {
                "APP_ENV": "test",
                "PORTFOLIO_AUTH_MODE": "fixture",
                "PORTFOLIO_AUTH_REQUIRED": "false",
            },
            clear=False,
        ),
        patch.object(
            bff_main,
            "active_user_profile",
            return_value={"display_name": "Operator", "status": "ACTIVE"},
        ),
        patch.object(
            bff_main,
            "authorized_fund_memberships",
            return_value=[
                {"fund_id": fund_id, "role": "TRADER"},
                {"fund_id": fund_id, "role": "OWNER"},
            ],
        ),
        patch.object(
            bff_main,
            "authorized_trading_books",
            return_value=[
                {
                    "fund_id": fund_id,
                    "book_id": "00000000-0000-0000-0000-000000000123",
                    "name": "Main Paper Book",
                }
            ],
        ),
    ):
        response = TestClient(app).get(
            "/ui/me", headers={"X-User-Id": owner_id}
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "portfolio.current-user.v1",
        "user_id": owner_id,
        "display_name": "Operator",
        "status": "ACTIVE",
        "funds": [
            {
                "fund_id": fund_id,
                "roles": ["OWNER", "TRADER"],
                "books": [
                    {
                        "book_id": "00000000-0000-0000-0000-000000000123",
                        "name": "Main Paper Book",
                    }
                ],
            }
        ],
        "onboarding_required": False,
    }


def test_ui_me_requires_an_identity_even_in_optional_fixture_mode() -> None:
    with patch.dict(
        os.environ,
        {
            "APP_ENV": "test",
            "PORTFOLIO_AUTH_MODE": "fixture",
            "PORTFOLIO_AUTH_REQUIRED": "false",
        },
        clear=False,
    ):
        response = TestClient(app).get("/ui/me")
    assert response.status_code == 401


def _production_auth_environment() -> dict[str, str]:
    return {
        "APP_ENV": "production",
        "PORTFOLIO_AUTH_MODE": "supabase_jwt",
        "PORTFOLIO_AUTH_REQUIRED": "true",
    }


def test_opaque_mandate_id_is_authorized_by_canonical_fund() -> None:
    owner_id = str(uuid4())
    fund_id = str(uuid4())
    governance = AsyncMock(return_value={"mandate_id": "m-foreign", "fund_id": fund_id})
    with (
        patch.dict(os.environ, _production_auth_environment(), clear=False),
        patch.object(bff_main, "_governance_request", governance),
        patch.object(
            bff_main,
            "require_fund_membership",
            side_effect=HTTPException(403, "portfolio_fund_forbidden"),
        ) as membership,
    ):
        response = TestClient(app).get(
            "/ui/mandates/m-foreign/current", headers={"X-User-Id": owner_id}
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "portfolio_fund_forbidden"}
    membership.assert_called_once_with(owner_id, fund_id)
    governance.assert_awaited_once_with(
        "GET", "/governance/v1/mandates/m-foreign/current"
    )


def test_mandate_mutation_rejects_caller_fund_mismatch_before_put() -> None:
    owner_id = str(uuid4())
    canonical_fund_id = str(uuid4())
    submitted_fund_id = str(uuid4())
    governance = AsyncMock(
        return_value={"mandate_id": "m-1", "fund_id": canonical_fund_id}
    )
    with (
        patch.dict(os.environ, _production_auth_environment(), clear=False),
        patch.object(bff_main, "_governance_request", governance),
        patch.object(bff_main, "require_fund_membership"),
    ):
        response = TestClient(app).put(
            "/ui/mandates/m-1",
            headers={"X-User-Id": owner_id},
            json={"fund_id": submitted_fund_id},
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "portfolio_canonical_fund_binding_mismatch"
    }
    assert governance.await_count == 1


def test_opaque_approval_id_is_checked_before_decision() -> None:
    owner_id = str(uuid4())
    fund_id = str(uuid4())
    governance = AsyncMock(
        return_value={"approval_id": "a-foreign", "fund_id": fund_id}
    )
    with (
        patch.dict(os.environ, _production_auth_environment(), clear=False),
        patch.object(bff_main, "_governance_request", governance),
        patch.object(
            bff_main,
            "require_fund_membership",
            side_effect=HTTPException(403, "portfolio_fund_forbidden"),
        ),
    ):
        response = TestClient(app).post(
            "/ui/mandate-approvals/a-foreign/decide",
            headers={"X-User-Id": owner_id},
            json={"decision": "APPROVED"},
        )

    assert response.status_code == 403
    assert governance.await_count == 1
    governance.assert_awaited_once_with(
        "GET", "/governance/v1/approvals/a-foreign"
    )


@pytest.mark.parametrize(
    "path", ["/ui/research", "/ui/strategy", "/ui/risk", "/ui/qa", "/ui/risk-qa"]
)
def test_global_operator_projection_requires_a_provisioned_fund(path: str) -> None:
    owner_id = str(uuid4())
    with (
        patch.dict(os.environ, _production_auth_environment(), clear=False),
        patch.object(
            bff_main,
            "require_any_fund_membership",
            side_effect=HTTPException(403, "portfolio_fund_membership_required"),
        ),
    ):
        response = TestClient(app).get(path, headers={"X-User-Id": owner_id})
    assert response.status_code == 403


def test_command_audit_is_filtered_to_authorized_funds() -> None:
    owner_id = str(uuid4())
    allowed_fund = str(uuid4())
    foreign_fund = str(uuid4())
    with (
        patch.dict(os.environ, _production_auth_environment(), clear=False),
        patch.object(
            bff_main,
            "require_any_fund_membership",
            return_value=[{"fund_id": allowed_fund, "role": "OWNER"}],
        ),
        patch.object(
            bff_main.COMMAND_SERVICE,
            "audit_events",
            return_value=[
                {"audit_event_id": "allowed", "fund_id": allowed_fund},
                {"audit_event_id": "foreign", "fund_id": foreign_fund},
            ],
        ),
    ):
        response = TestClient(app).get(
            "/ui/commands/audit", headers={"X-User-Id": owner_id}
        )

    assert response.status_code == 200
    assert response.json()["events"] == [
        {"audit_event_id": "allowed", "fund_id": allowed_fund}
    ]


def test_ceo_mirror_journal_rejects_a_different_authenticated_owner() -> None:
    owner_id = str(uuid4())
    foreign_owner = str(uuid4())
    store = InMemoryMirrorStore()
    ingress = CanonicalIngress(
        query="private request",
        request_id="request-private-owner",
        source="web",
        actor_id=foreign_owner,
        actor_type="user",
    )
    execute_once(ingress, store=store, execute=lambda: {"task_id": "task-private"})

    with (
        patch.dict(os.environ, _production_auth_environment(), clear=False),
        patch.object(ceo_mirror_api, "MIRROR_STORE", store),
    ):
        response = TestClient(app).get(
            "/ui/ceo/events",
            params={"request_id": ingress.request_id},
            headers={"X-User-Id": owner_id},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "ceo_mirror_request_forbidden"}


def test_ceo_mirror_ingress_binds_actor_to_verified_subject() -> None:
    owner_id = str(uuid4())
    foreign_owner = str(uuid4())
    with (
        patch.dict(os.environ, _production_auth_environment(), clear=False),
        patch.object(ceo_mirror_api, "_execute") as execute,
    ):
        response = TestClient(app).post(
            "/ui/ceo/ingress",
            headers={"X-User-Id": owner_id},
            json={
                "query": "spoofed actor",
                "request_id": "request-spoofed-actor",
                "source": "web",
                "actor_id": foreign_owner,
                "actor_type": "user",
            },
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "ceo_mirror_actor_mismatch"}
    execute.assert_not_called()


def test_discord_mirror_ingress_accepts_only_the_private_bridge_key() -> None:
    bridge_key = "discord-ingress-boundary-key-0123456789abcdef"
    with (
        patch.dict(
            os.environ,
            _production_auth_environment()
            | {"CEO_DISCORD_INGRESS_API_KEY": bridge_key},
            clear=False,
        ),
        patch.object(ceo_mirror_api, "_execute") as execute,
    ):
        execute.return_value = type(
            "Execution",
            (),
            {
                "response": {"task_id": "t_discord"},
                "accepted": True,
                "duplicate": False,
                "ignored": False,
                "reason": None,
            },
        )()
        accepted = TestClient(app).post(
            "/ui/ceo/ingress",
            headers={"Authorization": f"Bearer {bridge_key}"},
            json={
                "query": "삼성전자 2주 시장가 매수해",
                "request_id": "discord:991122334455667788",
                "source": "discord",
                "source_message_id": "991122334455667788",
                "actor_id": "123456789012345678",
                "actor_type": "user",
            },
        )

    assert accepted.status_code == 202
    assert accepted.json()["task_id"] == "t_discord"
    execute.assert_called_once()


def test_discord_source_cannot_use_a_normal_user_ingress_identity() -> None:
    owner_id = str(uuid4())
    with (
        patch.dict(os.environ, _production_auth_environment(), clear=False),
        patch.object(ceo_mirror_api, "_execute") as execute,
    ):
        response = TestClient(app).post(
            "/ui/ceo/ingress",
            headers={
                "Authorization": "Bearer user-jwt-placeholder",
                "X-User-Id": owner_id,
            },
            json={
                "query": "spoof Discord",
                "request_id": "discord:998877665544332211",
                "source": "discord",
                "source_message_id": "998877665544332211",
                "actor_id": "123456789012345678",
                "actor_type": "user",
            },
        )

    assert response.status_code == 401
    assert response.json() == {
        "detail": "discord_ingress_authentication_required"
    }
    execute.assert_not_called()
