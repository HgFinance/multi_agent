from __future__ import annotations

import os
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.api import main as bff_main
from apps.api.main import app


def test_removed_external_auth_mode_fails_closed_at_the_bff_boundary() -> None:
    with patch.dict(
        os.environ,
        {
            "PORTFOLIO_AUTH_MODE": "external",
        },
        clear=False,
    ):
        response = TestClient(app).get("/ui/snapshot")
    assert response.status_code == 503
    assert response.json()["detail"] == "portfolio_authentication_unavailable"


def test_local_mock_bff_accepts_the_fixed_identity_header() -> None:
    subject = str(uuid4())
    with patch.dict(
        os.environ,
        {
            "APP_ENV": "local",
            "PORTFOLIO_AUTH_MODE": "fixture",
            "ACCOUNTING_MODE": "OFFLINE",
            "DATABASE_URL": "",
        },
        clear=False,
    ):
        response = TestClient(app).get(
            "/ui/snapshot",
            headers={"X-User-Id": subject},
        )
    assert response.status_code == 200
    assert response.json()["mode"] in {"DEMO", "PAPER"}


def test_local_mock_bff_allows_anonymous_read_only_snapshot_when_configured() -> None:
    with patch.dict(
        os.environ,
        {
            "APP_ENV": "local",
            "PORTFOLIO_AUTH_MODE": "fixture",
            "ACCOUNTING_MODE": "OFFLINE",
            "DATABASE_URL": "",
        },
        clear=False,
    ):
        response = TestClient(app).get("/ui/snapshot")
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


def test_cors_origin_configuration_is_exact_and_fail_closed() -> None:
    with patch.dict(
        os.environ,
        {
            "APP_ENV": "production",
            "PORTFOLIO_CORS_ALLOW_ORIGINS": "https://app.example.com,https://ops.example.com/",
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


@pytest.mark.parametrize(
    "value",
    (
        "*",
        "https://*.example.com",
        "https://user:password@app.example.com",
        "https://app.example.com/path",
        "https://app.example.com?query=1",
        "not-an-origin",
    ),
)
def test_cors_rejects_non_exact_origins(value: str) -> None:
    with patch.dict(os.environ, {"PORTFOLIO_CORS_ALLOW_ORIGINS": value}, clear=False):
        with pytest.raises(RuntimeError, match="invalid PORTFOLIO_CORS"):
            bff_main._portfolio_cors_origins()
