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

from apps.api import ls_account_stream


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


def test_ls_realtime_refreshes_rest_projection_before_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    config = SimpleNamespace(environment="PAPER")

    async def issue_token(_config: SimpleNamespace) -> tuple[str, float]:
        return "token", time.time() + 60

    async def resync(_config: SimpleNamespace, _token: str) -> None:
        calls.append("holdings")

    async def resync_today(_config: SimpleNamespace, _token: str) -> None:
        calls.append("today")

    def connect(_url: str) -> object:
        calls.append("websocket")
        raise asyncio.CancelledError

    monkeypatch.setattr(ls_account_stream, "FEED", ls_account_stream._Feed())
    monkeypatch.setattr(ls_account_stream, "_config", lambda: (config, "wss://example.test/websocket"))
    monkeypatch.setattr(ls_account_stream, "_issue_token", issue_token)
    monkeypatch.setattr(ls_account_stream, "_configured_account", lambda _environment: "account")
    monkeypatch.setattr(ls_account_stream, "_resync", resync)
    monkeypatch.setattr(ls_account_stream, "_resync_today_activity", resync_today)
    monkeypatch.setattr(ls_account_stream, "_connect_order_stream", connect)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ls_account_stream._run_feed())

    assert calls == ["holdings", "today", "websocket"]


def test_ls_account_projection_has_periodic_rest_resync():
    source = Path(ls_account_stream.__file__).read_text(encoding="utf-8")

    assert "ACCOUNT_PROJECTION_RESYNC_SECONDS" in source
    assert "socket.recv(), timeout=ACCOUNT_PROJECTION_RESYNC_SECONDS" in source
    assert "except asyncio.TimeoutError:" in source
    timeout_branch = source.split("except asyncio.TimeoutError:", 1)[1]
    assert "await _resync(config, token)" in timeout_branch
    assert "await _resync_today_activity(config, token)" in timeout_branch


def test_ls_realtime_disables_protocol_ping_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    connection = object()

    def connect(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs))
        return connection

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))

    assert ls_account_stream._connect_order_stream("wss://example.test/websocket") is connection
    assert calls == [("wss://example.test/websocket", {"ping_interval": None})]


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


def test_realtime_account_event_clears_stale_initial_account_lookup_error() -> None:
    feed = ls_account_stream._Feed()
    feed.account_error = "HTTPStatusError: 500"

    kind = feed.ingest(
        {
            "header": {"tr_cd": "SC1"},
            "body": {
                "accno1": "1234567890",
                "ordno": "42",
                "shtnIsuno": "A005930",
                "bnstp": "2",
                "execqty": "1",
                "execprc": "70000",
            },
        }
    )

    assert kind == "FILLED"
    assert feed.account == "1234567890"
    assert feed.account_error is None
