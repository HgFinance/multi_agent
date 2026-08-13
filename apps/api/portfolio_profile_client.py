#!/usr/bin/env python3
"""`accounting-api`의 `/portfolio/v1/investor-profiles` 얇은 프록시 클라이언트.

근거: docs/02-engineering/USER_INPUT_API_SPEC.md 2.3, 6.1 #2
      docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 6(명령 경계)

**Browser는 Domain API를 직접 호출하지 않는다.** `governance_client.py`와 같은
이유이고 같은 모양으로 만들었다 - 두 프록시가 서로 다르게 실패하면 운영자가
장애를 한 가지로 읽을 수 없다.

**BFF가 DB를 직접 읽지 않는 이유**: `apps/api/accounting.py`의
`portfolio_snapshot`은 psycopg2로 직접 조회하는 선례가 있지만, InvestorProfile은
쓰기 경로(`POST`)가 있고 거기에 version 할당·`effective_risk_band` 계산이 붙는다.
그 판정을 BFF에도 복제하면 두 곳이 갈라진다 - 계산은 회계·포트폴리오본부가
소유하고(USER_INPUT_API_SPEC 1.5) BFF는 전달만 한다.

`PORTFOLIO_API_URL`이 비어 있으면 503이다. 인메모리·직접조회로 후퇴하지 않는다 -
저장된 것처럼 응답하고 실제로 저장되지 않으면 사용자는 온보딩을 다시 해야 하는데
그 사실을 알 방법이 없다(개발 원칙 9).
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import HTTPException

# accounting-api는 compose에서 127.0.0.1:8046으로 뜬다(같은 앱에 원장 경로와
# /portfolio/v1이 함께 있다). 컨테이너 간 호출은 서비스 이름을 쓴다.
PORTFOLIO_API_URL = os.getenv(
    "PORTFOLIO_API_URL", os.getenv("ACCOUNTING_API_URL", "")
).rstrip("/")
PORTFOLIO_API_TIMEOUT_SECONDS = float(os.getenv("PORTFOLIO_API_TIMEOUT_SECONDS", "8"))


class PortfolioProxyError(HTTPException):
    """상류 오류 본문을 그대로 통과시킨다.

    `governance_client.GovernanceProxyError`와 같은 이유다 - 평범한
    `HTTPException(detail=payload)`는 FastAPI가 `{"detail": payload}`로 한 겹 더
    감싸서, 프론트가 최상위에서 읽는 `error_code`/`message`가 한 단계 깊어진다.
    accounting-api의 에러 봉투는 이미 `{error_code, message, ...}` 모양이라
    접지 않고 그대로 보낸다.
    """

    def __init__(self, status_code: int, payload: object) -> None:
        super().__init__(status_code=status_code, detail=payload)
        self.payload = payload


async def portfolio_request(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
) -> Any:
    """accounting-api로 한 번 왕복한다. 상태코드·본문을 그대로 옮긴다."""

    if not PORTFOLIO_API_URL:
        raise HTTPException(status_code=503, detail="portfolio_api_unavailable")
    try:
        async with httpx.AsyncClient(
            base_url=PORTFOLIO_API_URL,
            timeout=PORTFOLIO_API_TIMEOUT_SECONDS,
        ) as client:
            response = await client.request(method, path, params=params, json=body)
    except httpx.HTTPError as exc:
        # 상류에 닿지 못한 것과 상류가 거절한 것을 구분한다 - 전자는 재시도가
        # 의미 있고 후자는 요청을 고쳐야 한다.
        raise HTTPException(
            status_code=503, detail=f"portfolio_api_unreachable: {type(exc).__name__}"
        ) from exc

    if response.status_code >= 400:
        try:
            payload: object = response.json()
        except ValueError:
            payload = {"error_code": "PORTFOLIO_API_ERROR", "message": response.text[:500]}
        raise PortfolioProxyError(response.status_code, payload)

    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502, detail="portfolio_api_returned_non_json"
        ) from exc


__all__ = [
    "PORTFOLIO_API_URL",
    "PortfolioProxyError",
    "portfolio_request",
]
