from __future__ import annotations

import sys
from datetime import timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.ls_openapi import (
    LSOpenAPIClient,
    LSOpenAPIConfig,
    credential_status,
)


def test_credentials_report_presence_only() -> None:
    status = credential_status(
        {
            "LS_ENV": "PAPER",
            "LS_APP_KEY_PAPER": "key",
            "LS_APP_SECRET_KEY_PAPER": "TOPSECRET123",
            "LS_REST_BASE_URL_PAPER": "https://example.test",
        }
    )
    assert status["configured"] is True
    assert status["secret_values_exposed"] is False
    assert "TOPSECRET123" not in str(status)


def test_paper_credentials_fallback_to_shared_rest_base() -> None:
    status = credential_status(
        {
            "LS_ENV": "PAPER",
            "LS_APP_KEY_PAPER": "key",
            "LS_APP_SECRET_KEY_PAPER": "secret",
            "LS_REST_BASE_URL": "https://example.test",
        }
    )
    assert status["configured"] is True
    assert status["present"]["LS_REST_BASE_URL_PAPER"] is False
    assert status["present"]["LS_REST_BASE_URL"] is True


def test_ls_env_selects_one_credential_set_for_market_and_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LS_APP_KEY_PAPER", "paper-key")
    monkeypatch.setenv("LS_APP_SECRET_KEY_PAPER", "paper-secret")
    monkeypatch.setenv("LS_APP_KEY", "live-key")
    monkeypatch.setenv("LS_APP_SECRET_KEY", "live-secret")
    monkeypatch.setenv("LS_REST_BASE_URL", "https://example.test")

    monkeypatch.setenv("LS_ENV", "LIVE")
    live = LSOpenAPIConfig.from_env()
    monkeypatch.setenv("LS_ENV", "PAPER")
    paper = LSOpenAPIConfig.from_env()

    assert live.environment == "LIVE"
    assert live.app_key == "live-key"
    assert paper.environment == "PAPER"
    assert paper.app_key == "paper-key"


def test_ls_read_only_quote_and_portfolio_calls() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200, json={"access_token": "token", "expire_in": "300"}
            )
        if request.headers.get("tr_cd") == "t1102":
            return httpx.Response(
                200,
                json={
                    "t1102OutBlock": {"price": "100", "bidho1": "99", "offerho1": "101"}
                },
            )
        if request.headers.get("tr_cd") == "t0424":
            return httpx.Response(
                200,
                json={
                    "t0424OutBlock": {"sunamt": "1200"},
                    "t0424OutBlock1": [{"expcode": "A", "janqty": "10"}],
                },
            )
        if request.headers.get("tr_cd") == "CSPAQ12200":
            return httpx.Response(
                200,
                json={"CSPAQ12200OutBlock2": {"Dps": "500", "MnyOrdAbleAmt": "400"}},
            )
        return httpx.Response(404)

    config = LSOpenAPIConfig(
        environment="PAPER",
        base_url="https://example.test",
        app_key="key",
        app_secret_key="secret",
    )
    with LSOpenAPIClient(
        config, client=httpx.Client(transport=httpx.MockTransport(handler))
    ) as client:
        quote = client.get_quote("A")
        portfolio = client.get_portfolio_snapshot()

    assert quote.price == 100
    assert quote.observed_at.tzinfo == timezone.utc
    assert portfolio.cash == 500
    assert portfolio.buying_power == 400
    assert calls.count("/oauth2/token") == 1
    assert "/stock/order" not in calls
