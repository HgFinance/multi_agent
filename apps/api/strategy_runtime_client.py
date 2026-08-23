#!/usr/bin/env python3
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

import os
from typing import Any

import httpx
from fastapi import HTTPException

STRATEGY_RUNTIME_API_URL = os.getenv("STRATEGY_RUNTIME_API_URL", "").rstrip("/")
STRATEGY_RUNTIME_TIMEOUT_SECONDS = float(os.getenv("STRATEGY_RUNTIME_TIMEOUT_SECONDS", "20"))


class StrategyRuntimeProxyError(HTTPException):
    """상류(sidecar) 오류 본문을 그대로 통과시킨다. `PortfolioProxyError`와 같은 이유."""

    def __init__(self, status_code: int, payload: object) -> None:
        super().__init__(status_code=status_code, detail=payload)
        self.payload = payload


async def strategy_runtime_request(method: str, path: str, *, body: dict[str, object] | None = None) -> Any:
    if not STRATEGY_RUNTIME_API_URL:
        raise HTTPException(status_code=503, detail="strategy_runtime_control_unavailable")
    try:
        async with httpx.AsyncClient(
            base_url=STRATEGY_RUNTIME_API_URL,
            timeout=STRATEGY_RUNTIME_TIMEOUT_SECONDS,
        ) as client:
            response = await client.request(method, path, json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503, detail=f"strategy_runtime_unreachable: {type(exc).__name__}"
        ) from exc

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


__all__ = [
    "STRATEGY_RUNTIME_API_URL",
    "StrategyRuntimeProxyError",
    "strategy_runtime_request",
]
