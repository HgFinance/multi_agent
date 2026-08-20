import asyncio
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest


API_ROOT = Path(__file__).resolve().parents[2] / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))
RISK_INTEGRATIONS_ROOT = Path(__file__).resolve().parents[2] / "departments" / "03-risk" / "integrations"
if str(RISK_INTEGRATIONS_ROOT) not in sys.path:
    sys.path.insert(0, str(RISK_INTEGRATIONS_ROOT))

from apps.api import fact_router, ls_account_stream


def test_bff_fact_symbol_parser_accepts_exact_alphanumeric_code_only() -> None:
    match = fact_router._SYMBOL.search("00088k 현재가")
    assert match is not None
    assert match.group(1).upper() == "00088K"
    assert fact_router._SYMBOL.search("prefix00088ksuffix 현재가") is None


def test_ls_account_symbol_normalizer_supports_exact_code_and_known_prefix() -> None:
    assert ls_account_stream._symbol(" 00088k ") == "00088K"
    assert ls_account_stream._symbol("A00088k") == "00088K"
    assert ls_account_stream._symbol("Samsung Electronics") is None


def test_market_and_account_rest_config_use_single_ls_env_without_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LS_ENV", "LIVE")
    monkeypatch.setenv("LS_APP_KEY_PAPER", "paper-key")
    monkeypatch.setenv("LS_APP_SECRET_KEY_PAPER", "paper-secret")
    monkeypatch.setenv("LS_APP_KEY", "live-key")
    monkeypatch.setenv("LS_APP_SECRET_KEY", "live-secret")
    monkeypatch.setenv("LS_REST_BASE_URL", "https://example.test")
    monkeypatch.delenv("LS_REST_BASE_URL_PAPER", raising=False)
    monkeypatch.delenv("LS_WS_BASE_URL_PAPER", raising=False)
    monkeypatch.delenv("LS_WS_BASE_URL", raising=False)

    config, ws_url = ls_account_stream._config(require_ws=False)

    assert config.environment == "LIVE"
    assert config.app_key == "live-key"
    assert config.base_url == "https://example.test"
    assert ws_url == ""


def test_ls_token_cache_isolated_by_environment_and_app_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued: list[str] = []

    async def issue(config: SimpleNamespace) -> tuple[str, float]:
        issued.append(config.environment)
        # 두 번째 값은 만료 epoch초다(남은 초가 아니다).
        return f"token-{config.environment.lower()}", time.time() + 70503.0

    monkeypatch.setattr(ls_account_stream, "_issue_token", issue)
    ls_account_stream._token_cache.clear()
    live = SimpleNamespace(environment="LIVE", app_key="live-key")
    paper = SimpleNamespace(environment="PAPER", app_key="paper-key")

    async def scenario() -> None:
        assert await ls_account_stream._access_token(live) == "token-live"
        assert await ls_account_stream._access_token(paper) == "token-paper"
        assert await ls_account_stream._access_token(live) == "token-live"

    try:
        asyncio.run(scenario())
    finally:
        ls_account_stream._token_cache.clear()

    assert issued == ["LIVE", "PAPER"]


def test_direct_market_quote_uses_ls_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected: list[bool] = []

    class Client:
        @classmethod
        def from_env(cls) -> "Client":
            selected.append(True)
            return cls()

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def get_quote(symbol: str) -> SimpleNamespace:
            return SimpleNamespace(
                symbol=symbol,
                price="100",
                bid="99",
                ask="101",
                observed_at=SimpleNamespace(isoformat=lambda: "2026-08-20T00:00:00+00:00"),
                source="ls-openapi",
            )

    monkeypatch.setenv("LS_ENV", "LIVE")
    monkeypatch.setitem(sys.modules, "ls_openapi", SimpleNamespace(LSOpenAPIClient=Client))

    fact = fact_router._fetch_market_quote("000660 현재가")

    assert fact.data["symbol"] == "000660"
    assert selected == [True]


def test_ls_auth_error_detail_keeps_broker_reason_without_credentials() -> None:
    class Response:
        status_code = 403

        @staticmethod
        def json() -> dict[str, str]:
            return {"error_description": "모의투자 비밀번호 오류입니다."}

    class Error(Exception):
        response = Response()

    assert "모의투자 비밀번호 오류입니다." in ls_account_stream._ls_error_detail(Error())
    assert "HTTP 403" in ls_account_stream._ls_error_detail(Error())
