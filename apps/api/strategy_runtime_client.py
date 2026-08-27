"""`strategy-runtime-control` sidecar의 얇은 프록시 클라이언트.

`portfolio_profile_client.py`와 같은 이유·같은 모양이다 - Browser는 Domain
API를 직접 호출하지 않고, `portfolio-bff`는 docker 소켓을 직접 쥐지 않는다.
실제 docker 조작·mlpipe-paper 파일 읽기는 전부 `strategy_runtime_server.py`
(별도 컨테이너, `docker-compose.yml`의 `strategy-runtime-control`)가 한다.

`STRATEGY_RUNTIME_API_URL`이 비어 있으면 503이다 - 이 배포에 sidecar가 없다는
뜻이고, 조용히 "전략 없음"으로 보이면 안 된다(개발 원칙 9와 같은 이유,
`portfolio_profile_client.py` 머리말 참고).
"""
from __future__ import annotations

import hmac
import math
import os
from typing import Any

import httpx
from fastapi import HTTPException

STRATEGY_RUNTIME_API_URL = os.getenv("STRATEGY_RUNTIME_API_URL", "").rstrip("/")
STRATEGY_RUNTIME_SERVICE_TOKEN_ENV = "STRATEGY_RUNTIME_SERVICE_TOKEN"


def _runtime_base_url() -> str:
    return os.getenv("STRATEGY_RUNTIME_API_URL", STRATEGY_RUNTIME_API_URL).strip().rstrip("/")


def _runtime_timeout(default: float = 20.0) -> float:
    try:
        value = float(os.getenv("STRATEGY_RUNTIME_TIMEOUT_SECONDS", str(default)))
    except ValueError:
        value = default
    if not math.isfinite(value):
        value = default
    return max(1.0, min(value, 60.0))


def _runtime_auth_headers() -> dict[str, str]:
    token = _configured_runtime_token()
    if token is None:
        raise HTTPException(status_code=503, detail="strategy_runtime_auth_unconfigured")
    return {"Authorization": f"Bearer {token}"}


def _configured_runtime_token() -> str | None:
    token = os.getenv(STRATEGY_RUNTIME_SERVICE_TOKEN_ENV, "").strip()
    if len(token) < 32 or any(character.isspace() for character in token):
        return None
    return token


def runtime_service_authorized(authorization: str | None) -> bool:
    token = _configured_runtime_token()
    expected = f"Bearer {token}" if token is not None else ""
    return bool(authorization) and hmac.compare_digest(authorization, expected)


def runtime_service_token_configured() -> bool:
    return _configured_runtime_token() is not None


class StrategyRuntimeProxyError(HTTPException):
    """상류(sidecar) 오류 본문을 그대로 통과시킨다. `PortfolioProxyError`와 같은 이유."""

    def __init__(self, status_code: int, payload: object) -> None:
        super().__init__(status_code=status_code, detail=payload)
        self.payload = payload


def _response_payload(response: httpx.Response) -> Any:
    if response.status_code >= 400:
        try:
            payload: object = response.json()
        except ValueError:
            payload = {"detail": response.text[:500]}
        raise StrategyRuntimeProxyError(response.status_code, payload)

    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="strategy_runtime_returned_non_json") from exc


async def strategy_runtime_request(method: str, path: str, *, body: dict[str, object] | None = None) -> Any:
    base_url = _runtime_base_url()
    if not base_url:
        raise HTTPException(status_code=503, detail="strategy_runtime_control_unavailable")
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=_runtime_timeout(),
        ) as client:
            response = await client.request(
                method, path, json=body, headers=_runtime_auth_headers()
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503, detail=f"strategy_runtime_unreachable: {type(exc).__name__}"
        ) from exc

    return _response_payload(response)


def strategy_runtime_request_sync(
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    timeout_seconds: float = 20.0,
) -> Any:
    """Synchronous sibling for the research router's sync FastAPI handlers."""

    base_url = _runtime_base_url()
    if not base_url:
        raise HTTPException(status_code=503, detail="strategy_runtime_control_unavailable")
    try:
        with httpx.Client(base_url=base_url, timeout=_runtime_timeout(timeout_seconds)) as client:
            response = client.request(
                method, path, json=body, headers=_runtime_auth_headers()
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503, detail=f"strategy_runtime_unreachable: {type(exc).__name__}"
        ) from exc
    return _response_payload(response)


__all__ = [
    "STRATEGY_RUNTIME_API_URL",
    "STRATEGY_RUNTIME_SERVICE_TOKEN_ENV",
    "StrategyRuntimeProxyError",
    "runtime_service_authorized",
    "runtime_service_token_configured",
    "strategy_runtime_request",
    "strategy_runtime_request_sync",
]
