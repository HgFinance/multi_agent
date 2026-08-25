from __future__ import annotations

import ast
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apps.api import current_user as auth
from apps.api import service_token, trading_client, user_orders

SUBJECT = uuid4()
FUND_ID = uuid4()
BOOK_ID = uuid4()
INSTRUMENT_ID = uuid4()


def _app(*, authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(user_orders.router)
    if authenticated:
        app.dependency_overrides[user_orders.current_user] = lambda: str(SUBJECT)
    return app


def _directive_response(
    *, action: str = "PLACE_ORDER", priority: int = 1000, payload_hash: str = "a" * 64
) -> dict[str, object]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "directive_id": str(uuid4()),
        "state": "RECEIVED",
        "action": action,
        "priority": priority,
        "fund_id": str(FUND_ID),
        "book_id": str(BOOK_ID),
        "instruction_ref": str(uuid4()),
        "idempotency_key": "request-0001",
        "payload_sha256": payload_hash,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "error_code": None,
        "error_message": None,
        "legs": [],
    }


@pytest.mark.parametrize(
    ("query", "action", "symbol", "side", "order_type"),
    [
        ("삼성전자 2주 매수해", "PLACE_ORDER", "삼성전자", "BUY", "MARKET"),
        ("삼성전자 10주 시장가로 사줘", "PLACE_ORDER", "삼성전자", "BUY", "MARKET"),
        ("005930 5주 70,000원에 매수", "PLACE_ORDER", "005930", "BUY", "LIMIT"),
        ("현대자동차 3주 시장가 매도해", "PLACE_ORDER", "현대자동차", "SELL", "MARKET"),
        ("보유종목 전량 매도해", "SELL_ALL", None, None, None),
        ("전량 매도해", "SELL_ALL", None, None, None),
        ("미체결 주문 전부 취소", "CANCEL_ALL", None, None, None),
    ],
)
def test_deterministic_parser_classifies_supported_korean_orders(
    query: str,
    action: str,
    symbol: str | None,
    side: str | None,
    order_type: str | None,
) -> None:
    parsed_action, payload = user_orders.parse_user_order_query(query)
    assert parsed_action.value == action
    if action == "PLACE_ORDER":
        assert payload["symbol"] == symbol
        assert payload["side"] == side
        assert payload["order_type"] == order_type
    else:
        assert payload == {}


def test_bff_canonicalizes_exact_alphanumeric_krx_codes_but_preserves_names() -> None:
    action, payload = user_orders.parse_user_order_query("00088k 5주 시장가 매수")
    assert action is user_orders.DirectiveAction.PLACE_ORDER
    assert payload["symbol"] == "00088K"

    structured = user_orders.PaperOrderInput(
        symbol=" 00088k ", side="BUY", quantity="1", order_type="MARKET"
    )
    assert structured.symbol == "00088K"

    named = user_orders.PaperOrderInput(
        symbol="Samsung Electronics",
        side="BUY",
        quantity="1",
        order_type="MARKET",
    )
    assert named.symbol == "Samsung Electronics"


def test_parser_does_not_treat_korean_sa_substring_as_buy() -> None:
    with pytest.raises(user_orders.ClarificationRequired) as error:
        user_orders.parse_user_order_query("회사를 조사해줘")
    assert error.value.reason == "side"


def test_individual_symbol_total_sell_is_not_account_sell_all() -> None:
    with pytest.raises(user_orders.ClarificationRequired) as error:
        user_orders.parse_user_order_query("삼성전자 전량 매도")
    assert error.value.reason == "quantity"


def test_account_words_do_not_hide_an_individual_symbol_total_sell() -> None:
    with pytest.raises(user_orders.ClarificationRequired) as error:
        user_orders.parse_user_order_query("내 계좌 삼성전자 전량 매도")
    assert error.value.reason == "quantity"


@pytest.mark.parametrize(
    "query",
    [
        "삼성전자 10주 지정가로 매수",
        "삼성전자 10주 시장가 70000원에 매수",
        "삼성전자 10주 시장가 매수 매도",
        "삼성전자 시장가 매수",
    ],
)
def test_parser_requires_clarification_instead_of_guessing(query: str) -> None:
    with pytest.raises(user_orders.ClarificationRequired):
        user_orders.parse_user_order_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "미체결 주문 전부 취소하지 마",
        "미체결 주문 전부 취소 내역 알려줘",
        "미체결 주문 전체 취소율 알려줘",
        "삼성전자 10주 시장가 매수 취소",
        "삼성전자 10주 시장가 매수 내역 알려줘",
        "삼성전자 10주 시장가 매수?",
        "삼성전자 1,2주 시장가 매수",
        "삼성전자 2주 7,0,000원에 매수",
        "10주 700000원에 매수",
    ],
)
def test_parser_rejects_negation_read_requests_residual_text_and_bad_numbers(
    query: str,
) -> None:
    with pytest.raises(user_orders.ClarificationRequired):
        user_orders.parse_user_order_query(query)


def test_service_proof_binds_every_authority_and_payload_claim(monkeypatch) -> None:
    secret = "proof-secret-that-is-more-than-thirty-two-bytes"
    monkeypatch.setenv("TRADING_SERVICE_AUTH_SECRET", secret)
    monkeypatch.setenv("TRADING_SERVICE_AUTH_ISSUER", "portfolio-bff")
    monkeypatch.setenv("TRADING_SERVICE_AUTH_AUDIENCE", "trading-api")
    payload = {
        "instrument_id": None,
        "symbol": "005930",
        "side": "BUY",
        "quantity": "10",
        "order_type": "MARKET",
        "time_in_force": "DAY",
        "limit_price": None,
    }
    digest = service_token.payload_sha256(payload)
    token = service_token.issue_trading_directive_proof(
        subject=str(SUBJECT),
        fund_id=str(FUND_ID),
        book_id=str(BOOK_ID),
        action="PLACE_ORDER",
        instruction_ref="instruction-0001",
        idempotency_key="request-0001",
        payload_hash=digest,
        now=2_000_000_000,
    )
    claims = jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        audience="trading-api",
        issuer="portfolio-bff",
        options={"verify_exp": False, "verify_nbf": False, "verify_iat": False},
    )
    assert claims == {
        "iss": "portfolio-bff",
        "aud": "trading-api",
        "sub": str(SUBJECT),
        "fund_id": str(FUND_ID),
        "book_id": str(BOOK_ID),
        "action": "PLACE_ORDER",
        "instruction_ref": "instruction-0001",
        "idempotency_key": "request-0001",
        "payload_sha256": digest,
        "jti": claims["jti"],
        "iat": 2_000_000_000,
        "nbf": 1_999_999_999,
        "exp": 2_000_000_020,
        "scope": "trading.user-directive.execute",
    }
    assert UUID(claims["jti"])


def test_service_proof_rejects_short_secret(monkeypatch) -> None:
    monkeypatch.setenv("TRADING_SERVICE_AUTH_SECRET", "too-short")
    with pytest.raises(service_token.TradingProofConfigurationError):
        service_token.trading_proof_settings()


@pytest.mark.parametrize(
    "secret",
    [
        "CHANGE_ME_RANDOM_TRADING_PROOF_SECRET_32_BYTES_MINIMUM",
        "replace-with-one-random-secret-of-at-least-32-bytes",
        "this-is-an-example-secret-that-is-long-enough",
    ],
)
def test_service_proof_rejects_long_placeholder_secret(
    monkeypatch, secret: str
) -> None:
    monkeypatch.setenv("TRADING_SERVICE_AUTH_SECRET", secret)
    with pytest.raises(service_token.TradingProofConfigurationError):
        service_token.trading_proof_settings()


def _db_connection(row: tuple[object, ...] | None):
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    cursor_context = MagicMock()
    cursor_context.__enter__.return_value = cursor
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value = cursor_context
    return connection, cursor


def test_trading_book_access_fixture_executes_real_uuid_canonicalization(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("PORTFOLIO_AUTH_MODE", "fixture")
    monkeypatch.setenv(
        "PORTFOLIO_FIXTURE_TRADING_BOOKS_JSON",
        json.dumps(
            [
                {
                    "user_id": str(SUBJECT),
                    "fund_id": str(FUND_ID),
                    "book_id": str(BOOK_ID),
                    "name": "Main Paper",
                    "role": "TRADER",
                    "fund_status": "ACTIVE",
                    "book_status": "ACTIVE",
                }
            ]
        ),
    )
    access = auth.require_trading_book_access(str(SUBJECT), str(FUND_ID), str(BOOK_ID))
    assert access == {
        "user_id": str(SUBJECT),
        "fund_id": str(FUND_ID),
        "book_id": str(BOOK_ID),
        "role": "FIXTURE",
    }


def test_trading_book_access_fixture_has_no_implicit_book(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("PORTFOLIO_AUTH_MODE", "fixture")
    monkeypatch.setenv("PORTFOLIO_FIXTURE_TRADING_BOOKS_JSON", "[]")
    with pytest.raises(HTTPException) as error:
        auth.require_trading_book_access(str(SUBJECT), str(FUND_ID), str(BOOK_ID))
    assert error.value.status_code == 403
    assert error.value.detail == "portfolio_trading_book_forbidden"


def test_fixture_trading_books_require_explicit_active_trader_seed(monkeypatch) -> None:
    blocked_book = uuid4()
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("PORTFOLIO_AUTH_MODE", "fixture")
    monkeypatch.setenv(
        "PORTFOLIO_FIXTURE_TRADING_BOOKS_JSON",
        json.dumps(
            [
                {
                    "user_id": str(SUBJECT),
                    "fund_id": str(FUND_ID),
                    "book_id": str(BOOK_ID),
                    "name": "Main Paper",
                    "role": "TRADER",
                    "fund_status": "ACTIVE",
                    "book_status": "ACTIVE",
                },
                {
                    "user_id": str(SUBJECT),
                    "fund_id": str(FUND_ID),
                    "book_id": str(blocked_book),
                    "name": "Viewer Book",
                    "role": "VIEWER",
                    "fund_status": "ACTIVE",
                    "book_status": "ACTIVE",
                },
            ]
        ),
    )
    assert auth.authorized_trading_books(str(SUBJECT)) == [
        {
            "fund_id": str(FUND_ID),
            "book_id": str(BOOK_ID),
            "name": "Main Paper",
        }
    ]


def test_exact_name_resolves_to_one_canonical_active_stock(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_DATABASE_URL", "postgresql://control/test")
    connection, cursor = _db_connection(None)
    cursor.fetchall.return_value = [(str(INSTRUMENT_ID), "005930")]
    with patch.object(auth.psycopg2, "connect", return_value=connection):
        resolved = auth.resolve_active_trading_instrument("삼성 전자")
    assert resolved == {
        "instrument_id": str(INSTRUMENT_ID),
        "symbol": "005930",
    }
    sql = " ".join(cursor.execute.call_args.args[0].split()).casefold()
    assert "i.status = 'active'" in sql
    assert "i.market = 'krx'" in sql
    assert "upper(i.instrument_type) = 'stock'" in sql
    assert "sy.valid_from <= now()" in sql
    params = cursor.execute.call_args.args[1]
    assert params[:3] == (None, None, "삼성 전자")


def test_known_korean_alias_resolves_via_symbol_code(monkeypatch) -> None:
    """ "네이버"의 공시 표시명은 "NAVER"라 exact-match만으로는 안 잡힌다 -
    별칭이 심볼 코드로 치환돼 같은 안전 질의를 그대로 타는지 확인한다."""
    monkeypatch.setenv("CONTROL_DATABASE_URL", "postgresql://control/test")
    connection, cursor = _db_connection(None)
    cursor.fetchall.return_value = [(str(INSTRUMENT_ID), "035420")]
    with patch.object(auth.psycopg2, "connect", return_value=connection):
        resolved = auth.resolve_active_trading_instrument("네이버")
    assert resolved == {
        "instrument_id": str(INSTRUMENT_ID),
        "symbol": "035420",
    }
    params = cursor.execute.call_args.args[1]
    assert params[:3] == ("035420", "035420", "네이버")


def test_alphanumeric_code_resolution_is_strip_upper_and_format_bounded(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTROL_DATABASE_URL", "postgresql://control/test")
    connection, cursor = _db_connection(None)
    cursor.fetchall.return_value = [(str(INSTRUMENT_ID), "00088K")]
    with patch.object(auth.psycopg2, "connect", return_value=connection):
        resolved = auth.resolve_active_trading_instrument(" 00088k ")

    assert resolved == {
        "instrument_id": str(INSTRUMENT_ID),
        "symbol": "00088K",
    }
    sql = " ".join(cursor.execute.call_args.args[0].split())
    params = cursor.execute.call_args.args[1]
    assert "sy.symbol ~ '^[0-9A-Z]{6}$'" in sql
    assert params[:3] == ("00088K", "00088K", "00088k")


def test_leading_six_digit_code_wins_over_trailing_display_name(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_DATABASE_URL", "postgresql://control/test")
    connection, cursor = _db_connection(None)
    cursor.fetchall.return_value = [(str(INSTRUMENT_ID), "124500")]
    with patch.object(auth.psycopg2, "connect", return_value=connection):
        resolved = auth.resolve_active_trading_instrument("124500 아이티센글로벌")

    assert resolved == {
        "instrument_id": str(INSTRUMENT_ID),
        "symbol": "124500",
    }
    params = cursor.execute.call_args.args[1]
    assert params[:3] == ("124500", "124500", "124500 아이티센글로벌")


@pytest.mark.parametrize(
    "rows", [[], [(str(uuid4()), "005930"), (str(uuid4()), "005930")]]
)
def test_unknown_or_ambiguous_name_requires_clarification(monkeypatch, rows) -> None:
    monkeypatch.setenv("CONTROL_DATABASE_URL", "postgresql://control/test")
    connection, cursor = _db_connection(None)
    cursor.fetchall.return_value = rows
    with (
        patch.object(auth.psycopg2, "connect", return_value=connection),
        pytest.raises(HTTPException) as error,
    ):
        auth.resolve_active_trading_instrument("이름")
    assert error.value.status_code == 422
    assert error.value.detail == "paper_order_instrument_clarification_required"


def test_business_route_requires_authentication(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("PORTFOLIO_AUTH_MODE", "fixture")
    monkeypatch.setenv("PORTFOLIO_AUTH_REQUIRED", "true")
    response = TestClient(_app(authenticated=False)).post(
        "/ui/paper-orders/sell-all",
        headers={"Idempotency-Key": "request-0001"},
        json={"fund_id": str(FUND_ID), "book_id": str(BOOK_ID)},
    )
    assert response.status_code == 401


def test_body_cannot_spoof_user_or_account_scope() -> None:
    response = TestClient(_app()).post(
        "/ui/paper-orders/sell-all",
        headers={"Idempotency-Key": "request-0001"},
        json={
            "fund_id": str(FUND_ID),
            "book_id": str(BOOK_ID),
            "user_id": str(uuid4()),
            "account_no": "spoofed",
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("key", "expected"),
    [(None, "idempotency_key_required"), ("1234567", "idempotency_key_invalid")],
)
def test_mutation_requires_domain_compatible_idempotency_key(key, expected) -> None:
    with patch.object(
        user_orders,
        "require_trading_book_access",
        return_value={
            "user_id": str(SUBJECT),
            "fund_id": str(FUND_ID),
            "book_id": str(BOOK_ID),
            "role": "TRADER",
        },
    ):
        headers = {"Idempotency-Key": key} if key is not None else {}
        response = TestClient(_app()).post(
            "/ui/paper-orders/sell-all",
            headers=headers,
            json={"fund_id": str(FUND_ID), "book_id": str(BOOK_ID)},
        )
    assert response.status_code == 400
    assert response.json()["detail"] == expected


def test_eight_character_idempotency_key_is_accepted(monkeypatch) -> None:
    raw = _directive_response(action="SELL_ALL", priority=2000)
    raw["idempotency_key"] = "12345678"

    def submit(**kwargs):
        body = kwargs["body"]
        raw["instruction_ref"] = body["instruction_ref"]
        raw["payload_sha256"] = service_token.payload_sha256(body["payload"])
        return raw

    with (
        patch.object(
            user_orders,
            "require_trading_book_access",
            return_value={
                "user_id": str(SUBJECT),
                "fund_id": str(FUND_ID),
                "book_id": str(BOOK_ID),
                "role": "OWNER",
            },
        ),
        patch.object(
            user_orders, "issue_trading_directive_proof", return_value="proof"
        ),
        patch.object(user_orders, "submit_user_directive", side_effect=submit),
    ):
        response = TestClient(_app()).post(
            "/ui/paper-orders/sell-all",
            headers={"Idempotency-Key": "12345678"},
            json={"fund_id": str(FUND_ID), "book_id": str(BOOK_ID)},
        )
    assert response.status_code == 202
    assert response.json()["mode"] == "PAPER"
    assert response.json()["priority"] == 2000


def test_admitted_authority_status_reuses_scoped_read_without_interactive_auth() -> (
    None
):
    directive_id = uuid4()
    raw = _directive_response()
    raw["directive_id"] = str(directive_id)
    raw["instruction_ref"] = f"conditional:{uuid4()}"
    with (
        patch.object(user_orders, "require_trading_book_access") as require_access,
        patch.object(
            user_orders, "issue_trading_directive_proof", return_value="proof"
        ) as issue_proof,
        patch.object(user_orders, "get_user_directive", return_value=raw) as get_status,
    ):
        response = user_orders.read_paper_directive_status_for_admitted_authority(
            user_id=str(SUBJECT),
            fund_id=str(FUND_ID),
            book_id=str(BOOK_ID),
            directive_id=str(directive_id),
        )

    assert response.directive_id == directive_id
    require_access.assert_not_called()
    issue_proof.assert_called_once()
    assert issue_proof.call_args.kwargs["subject"] == str(SUBJECT)
    assert issue_proof.call_args.kwargs["scope"] == "trading.user-directive.read"
    get_status.assert_called_once_with(directive_id=str(directive_id), proof="proof")


def test_place_order_resolves_canonical_symbol_and_never_calls_risk() -> None:
    captured: dict[str, object] = {}
    raw = _directive_response()

    def submit(**kwargs):
        captured.update(kwargs)
        raw["payload_sha256"] = service_token.payload_sha256(kwargs["body"]["payload"])
        raw["instruction_ref"] = kwargs["body"]["instruction_ref"]
        return raw

    with (
        patch.object(
            user_orders,
            "require_trading_book_access",
            return_value={
                "user_id": str(SUBJECT),
                "fund_id": str(FUND_ID),
                "book_id": str(BOOK_ID),
                "role": "CIO",
            },
        ),
        patch.object(
            user_orders,
            "resolve_active_trading_instrument",
            return_value={"instrument_id": str(INSTRUMENT_ID), "symbol": "005930"},
        ),
        patch.object(
            user_orders, "issue_trading_directive_proof", return_value="proof"
        ),
        patch.object(user_orders, "submit_user_directive", side_effect=submit),
    ):
        response = TestClient(_app()).post(
            "/ui/paper-orders",
            headers={"Idempotency-Key": "request-0001"},
            json={
                "fund_id": str(FUND_ID),
                "book_id": str(BOOK_ID),
                "query": "삼성전자 2주 매수해",
            },
        )
    assert response.status_code == 202
    body = captured["body"]
    assert body["payload"] == {
        "instrument_id": str(INSTRUMENT_ID),
        "symbol": "005930",
        "side": "BUY",
        "quantity": "2",
        "order_type": "MARKET",
        "time_in_force": "DAY",
        "limit_price": None,
    }
    assert "user_id" not in body
    imports = {
        alias.name
        for node in ast.walk(
            ast.parse(open(user_orders.__file__, encoding="utf-8").read())
        )
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("risk" in name.casefold() for name in imports)


def test_upstream_idempotency_conflict_is_preserved() -> None:
    with (
        patch.object(
            user_orders,
            "require_trading_book_access",
            return_value={
                "user_id": str(SUBJECT),
                "fund_id": str(FUND_ID),
                "book_id": str(BOOK_ID),
                "role": "TRADER",
            },
        ),
        patch.object(
            user_orders, "issue_trading_directive_proof", return_value="proof"
        ),
        patch.object(
            user_orders,
            "submit_user_directive",
            side_effect=trading_client.TradingProxyError(
                status_code=409, detail="trading_idempotency_conflict"
            ),
        ),
    ):
        response = TestClient(_app()).post(
            "/ui/paper-orders/cancel-all",
            headers={"Idempotency-Key": "request-0001"},
            json={"fund_id": str(FUND_ID), "book_id": str(BOOK_ID)},
        )
    assert response.status_code == 409


def test_mutation_transport_calls_httpx_exactly_once(monkeypatch) -> None:
    monkeypatch.setenv("TRADING_API_URL", "http://trading-api:8000")
    response = httpx.Response(
        409,
        json={"error_code": "TRADING_IDEMPOTENCY_CONFLICT", "message": "conflict"},
    )
    with patch.object(trading_client.httpx, "post", return_value=response) as post:
        with pytest.raises(trading_client.TradingProxyError) as error:
            trading_client.submit_user_directive(
                body={"payload": {}},
                proof="not-logged",
                idempotency_key="request-0001",
            )
    assert error.value.status_code == 409
    assert error.value.detail == "trading_idempotency_conflict"
    assert post.call_count == 1


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        ("TRADING_INSUFFICIENT_CASH", "trading_insufficient_cash"),
        ("TRADING_MARKET_SESSION_UNAVAILABLE", "trading_market_session_closed"),
        ("TRADING_MARKET_QUOTE_STALE", "trading_market_quote_stale"),
        ("TRADING_HIGHER_PRIORITY_ACTIVE", "trading_higher_priority_directive_active"),
        ("TRADING_PROOF_REPLAY", "trading_directive_conflict"),
    ],
)
def test_upstream_conflicts_are_not_misreported_as_idempotency(
    monkeypatch, error_code: str, expected: str
) -> None:
    monkeypatch.setenv("TRADING_API_URL", "http://trading-api:8000")
    response = httpx.Response(
        409, json={"error_code": error_code, "message": "private"}
    )
    with patch.object(trading_client.httpx, "post", return_value=response):
        with pytest.raises(trading_client.TradingProxyError) as error:
            trading_client.submit_user_directive(
                body={"payload": {}},
                proof="not-logged",
                idempotency_key="request-0001",
            )
    assert error.value.status_code == 409
    assert error.value.detail == expected


def test_invalid_upstream_state_fails_closed() -> None:
    raw = _directive_response()
    raw["state"] = "MADE_UP_SUCCESS"
    with pytest.raises(HTTPException) as error:
        user_orders._validated_response(raw)
    assert error.value.status_code == 502


@pytest.mark.parametrize(
    ("field", "wrong", "expected_kwargs"),
    [
        ("fund_id", str(uuid4()), {"fund_id": FUND_ID}),
        ("book_id", str(uuid4()), {"book_id": BOOK_ID}),
        ("action", "SELL_ALL", {"action": "PLACE_ORDER"}),
        ("instruction_ref", "swapped-ref", {"instruction_ref": "expected-ref"}),
        ("idempotency_key", "other-key", {"idempotency_key": "request-0001"}),
        (
            "payload_sha256",
            "b" * 64,
            {"expected_payload_sha256": "a" * 64},
        ),
    ],
)
def test_upstream_response_must_bind_to_submitted_authority_and_identity(
    field: str, wrong: str, expected_kwargs: dict[str, object]
) -> None:
    raw = _directive_response()
    raw[field] = wrong
    with pytest.raises(HTTPException) as error:
        user_orders._validated_response(raw, **expected_kwargs)
    assert error.value.status_code == 502
    assert error.value.detail == "trading_api_invalid_response"
