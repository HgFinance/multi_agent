"""Fail-closed transport from portfolio-bff to the private trading API."""
from __future__ import annotations

import os
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx


TRADING_DIRECTIVES_PATH = "/trading/v1/user-directives"
TRADING_DIRECTIVE_STATUS_PATH = "/trading/v1/user-directives/{directive_id}"

# Only stable, user-actionable Trading error codes cross the private service
# boundary.  Messages are intentionally not forwarded because they may contain
# implementation details, but a cash/session/quote failure must not be falsely
# reported as an idempotency collision.
_CONFLICT_DETAILS = {
    "TRADING_IDEMPOTENCY_CONFLICT": "trading_idempotency_conflict",
    "TRADING_INSUFFICIENT_CASH": "trading_insufficient_cash",
    "TRADING_INSUFFICIENT_SELLABLE_POSITION": (
        "trading_insufficient_sellable_position"
    ),
    "TRADING_MARKET_SESSION_CLOSED": "trading_market_session_closed",
    "TRADING_MARKET_SESSION_UNAVAILABLE": "trading_market_session_closed",
    "TRADING_MARKET_QUOTE_STALE": "trading_market_quote_stale",
    "TRADING_MARKET_QUOTE_CROSSED": "trading_market_quote_invalid",
    "TRADING_MARKET_QUOTE_EMPTY": "trading_market_quote_invalid",
    "TRADING_MARKET_QUOTE_BINDING_DENIED": "trading_market_quote_invalid",
    "TRADING_HIGHER_PRIORITY_ACTIVE": "trading_higher_priority_directive_active",
    "TRADING_INSTRUMENT_AMBIGUOUS": "trading_directive_instrument_ambiguous",
    "TRADING_POSITION_INSTRUMENT_UNSUPPORTED": (
        "trading_position_instrument_unsupported"
    ),
}


class TradingProxyError(RuntimeError):
    def __init__(self, *, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def trading_api_url() -> str:
    raw = os.getenv("TRADING_API_URL", "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if (
        not raw
        or parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise TradingProxyError(
            status_code=503, detail="trading_api_unavailable"
        )
    return raw


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise TradingProxyError(
            status_code=502, detail="trading_api_invalid_response"
        ) from exc
    if not isinstance(value, dict):
        raise TradingProxyError(
            status_code=502, detail="trading_api_invalid_response"
        )
    return value


def _raise_for_upstream_status(response: httpx.Response, *, mutation: bool) -> None:
    if response.status_code == 409:
        try:
            upstream = response.json()
        except ValueError:
            upstream = None
        code = upstream.get("error_code") if isinstance(upstream, dict) else None
        raise TradingProxyError(
            status_code=409,
            detail=_CONFLICT_DETAILS.get(code, "trading_directive_conflict"),
        )
    if response.status_code == 422:
        raise TradingProxyError(
            status_code=422, detail="trading_directive_invalid"
        )
    if not mutation and response.status_code == 404:
        raise TradingProxyError(
            status_code=404, detail="trading_directive_not_found"
        )
    if response.status_code == 429 or response.status_code >= 500:
        raise TradingProxyError(
            status_code=503, detail="trading_api_unavailable"
        )
    # Internal 401/403 means service-proof configuration or verification drift,
    # never a browser-user authorization result and never a reason to retry.
    raise TradingProxyError(
        status_code=502, detail="trading_api_rejected_request"
    )


def submit_user_directive(
    *,
    body: Mapping[str, Any],
    proof: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Submit exactly once. Mutation retries are deliberately not configured."""

    try:
        response = httpx.post(
            f"{trading_api_url()}{TRADING_DIRECTIVES_PATH}",
            json=dict(body),
            headers={
                "Authorization": f"Bearer {proof}",
                "Idempotency-Key": idempotency_key,
                "Content-Type": "application/json",
            },
            follow_redirects=False,
            timeout=httpx.Timeout(5.0, connect=2.0),
        )
    except httpx.RequestError as exc:
        raise TradingProxyError(
            status_code=503, detail="trading_api_unavailable"
        ) from exc
    if response.status_code not in {200, 201, 202}:
        _raise_for_upstream_status(response, mutation=True)
    return _json_object(response)


def get_user_directive(
    *, directive_id: str, proof: str
) -> dict[str, Any]:
    try:
        response = httpx.get(
            f"{trading_api_url()}"
            f"{TRADING_DIRECTIVE_STATUS_PATH.format(directive_id=directive_id)}",
            headers={"Authorization": f"Bearer {proof}"},
            follow_redirects=False,
            timeout=httpx.Timeout(5.0, connect=2.0),
        )
    except httpx.RequestError as exc:
        raise TradingProxyError(
            status_code=503, detail="trading_api_unavailable"
        ) from exc
    if response.status_code != 200:
        _raise_for_upstream_status(response, mutation=False)
    return _json_object(response)


__all__ = [
    "TRADING_DIRECTIVES_PATH",
    "TRADING_DIRECTIVE_STATUS_PATH",
    "TradingProxyError",
    "get_user_directive",
    "submit_user_directive",
    "trading_api_url",
]
