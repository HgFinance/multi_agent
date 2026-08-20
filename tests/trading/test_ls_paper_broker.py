from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

import httpx
import pytest

TRADING_ROOT = Path(__file__).resolve().parents[2] / "departments" / "02-trading"
sys.path.insert(0, str(TRADING_ROOT))

from broker.ls_paper_broker import (
    LSPaperBroker,
    LSPaperBrokerConfig,
    LSPaperBrokerError,
)


def _broker(handler) -> LSPaperBroker:
    config = LSPaperBrokerConfig(
        base_url="https://ls.example.test",
        app_key="paper-key",
        app_secret_key="paper-secret",
        mac_address="001122AABBCC",
    )
    return LSPaperBroker(
        config,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_config_never_falls_back_to_live_credentials(monkeypatch) -> None:
    monkeypatch.setenv("LS_ENV", "PAPER")
    monkeypatch.setenv("LS_APP_KEY", "live-key")
    monkeypatch.setenv("LS_APP_SECRET_KEY", "live-secret")
    monkeypatch.delenv("LS_APP_KEY_PAPER", raising=False)
    monkeypatch.delenv("LS_APP_SECRET_KEY_PAPER", raising=False)
    with pytest.raises(LSPaperBrokerError) as caught:
        LSPaperBrokerConfig.from_env()
    assert caught.value.code == "LS_PAPER_CREDENTIALS_REQUIRED"


def test_config_rejects_live_environment(monkeypatch) -> None:
    monkeypatch.setenv("LS_ENV", "LIVE")
    monkeypatch.setenv("LS_APP_KEY_PAPER", "paper-key")
    monkeypatch.setenv("LS_APP_SECRET_KEY_PAPER", "paper-secret")
    with pytest.raises(LSPaperBrokerError) as caught:
        LSPaperBrokerConfig.from_env()
    assert caught.value.code == "LS_PAPER_ENV_REQUIRED"


def test_config_requires_and_normalizes_paper_mac(monkeypatch) -> None:
    monkeypatch.setenv("LS_ENV", "PAPER")
    monkeypatch.setenv("LS_APP_KEY_PAPER", "paper-key")
    monkeypatch.setenv("LS_APP_SECRET_KEY_PAPER", "paper-secret")
    monkeypatch.delenv("LS_MAC_ADDRESS", raising=False)
    with pytest.raises(LSPaperBrokerError) as caught:
        LSPaperBrokerConfig.from_env()
    assert caught.value.code == "LS_PAPER_MAC_REQUIRED"

    monkeypatch.setenv("LS_MAC_ADDRESS", "00:11:22:aa:bb:cc")
    assert LSPaperBrokerConfig.from_env().mac_address == "001122AABBCC"


def test_place_market_buy_uses_paper_cash_order_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.headers["tr_cd"] == "CSPAT00601"
        assert request.headers["mac_address"] == "001122AABBCC"
        body = __import__("json").loads(request.content)
        block = body["CSPAT00601InBlock1"]
        assert block == {
            "IsuNo": "A005930",
            "OrdQty": 2,
            "OrdPrc": "0",
            "BnsTpCode": "2",
            "OrdprcPtnCode": "03",
            "MgntrnCode": "000",
            "LoanDt": "",
            "OrdCndiTpCode": "0",
            "MbrNo": "",
        }
        return httpx.Response(
            200,
            json={
                "rsp_cd": "00000",
                "CSPAT00601OutBlock2": {
                    "OrdNo": 6439,
                    "OrdTime": "111951000",
                    "ShtnIsuNo": "A005930",
                },
            },
        )

    broker = _broker(handler)
    ack = broker.place_order(
        symbol="005930",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal(2),
        limit_price=None,
    )
    assert ack.broker_order_id == "6439"
    assert [request.url.path for request in requests] == ["/oauth2/token", "/stock/order"]


def test_transport_failure_is_ambiguous_and_must_not_be_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expire_in": 3600})
        raise httpx.ReadTimeout("unknown outcome", request=request)

    broker = _broker(handler)
    with pytest.raises(LSPaperBrokerError) as caught:
        broker.place_order(
            symbol="005930",
            side="BUY",
            order_type="MARKET",
            quantity=Decimal(1),
            limit_price=None,
        )
    assert caught.value.code == "LS_PAPER_ORDER_AMBIGUOUS"
    assert caught.value.ambiguous is True


def test_order_status_reads_cumulative_fill() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.headers["tr_cd"] == "CSPAQ13700"
        return httpx.Response(
            200,
            json={
                "rsp_cd": "00000",
                "CSPAQ13700OutBlock3": [
                    {
                        "OrdNo": 6439,
                        "OrdQty": 2,
                        "AllExecQty": 2,
                        "ExecPrc": 269000,
                        "OrdTrxPtnNm": "정상주문",
                    }
                ],
            },
        )

    broker = _broker(handler)
    status = broker.order_status("6439", order_date=date(2026, 8, 20))
    assert status is not None
    assert status.state == "FILLED"
    assert status.filled_quantity == 2
    assert status.fill_price == 269000


def test_order_status_accepts_ls_account_query_success_code_and_caches_snapshot() -> None:
    history_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal history_requests
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        history_requests += 1
        return httpx.Response(
            200,
            json={
                "rsp_cd": "00136",
                "CSPAQ13700OutBlock1": {},
                "CSPAQ13700OutBlock2": {},
                "CSPAQ13700OutBlock3": [
                    {
                        "OrdNo": 6439,
                        "OrdQty": 2,
                        "AllExecQty": 2,
                        "ExecPrc": 269000,
                        "OrdTrxPtnNm": "정상주문",
                    },
                    {
                        "OrdNo": 6440,
                        "OrdQty": 1,
                        "AllExecQty": 0,
                        "ExecPrc": 0,
                        "OrdTrxPtnNm": "정상주문",
                    },
                ],
            },
        )

    broker = _broker(handler)
    first = broker.order_status("6439", order_date=date(2026, 8, 20))
    second = broker.order_status("6440", order_date=date(2026, 8, 20))
    assert first is not None and first.state == "FILLED"
    assert second is not None and second.state == "ACKNOWLEDGED"
    assert history_requests == 1


def test_success_response_without_order_confirmation_is_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(200, json={"rsp_cd": "00000"})

    broker = _broker(handler)
    with pytest.raises(LSPaperBrokerError) as caught:
        broker.place_order(
            symbol="005930",
            side="BUY",
            order_type="MARKET",
            quantity=Decimal(1),
            limit_price=None,
        )
    assert caught.value.code == "LS_PAPER_ORDER_AMBIGUOUS"
    assert caught.value.ambiguous is True


def test_cancel_order_uses_original_order_number_and_leaves_quantity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.headers["tr_cd"] == "CSPAT00801"
        body = __import__("json").loads(request.content)
        assert body["CSPAT00801InBlock1"] == {
            "OrgOrdNo": 6439,
            "IsuNo": "A005930",
            "OrdQty": 1,
        }
        return httpx.Response(
            200,
            json={
                "rsp_cd": "00000",
                "CSPAT00801OutBlock2": {
                    "OrdNo": 6440,
                    "OrdTime": "112001000",
                },
            },
        )

    broker = _broker(handler)
    ack = broker.cancel_order(
        broker_order_id="6439",
        symbol="005930",
        quantity=Decimal(1),
    )
    assert ack.broker_order_id == "6440"
    assert [request.url.path for request in requests] == [
        "/oauth2/token",
        "/stock/order",
    ]
