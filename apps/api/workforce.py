"""Operator BFF proxy for the Agent Workforce(HR) service's read-only endpoints.

`risk.py`/`qa.py`와 같은 이유·같은 모양이다 - Browser는 워크포스 도메인 API를 직접
부르지 않고, BFF가 `workforce-api`(departments/07-agent-workforce, 로컬 8044,
`docker-compose.yml`이 `include:`하는 `departments/07-agent-workforce/compose.yaml`)로
읽기 전용 요청만 얇게 중계한다.

판정 로직(ACTIVE/IDLE/UNOBSERVED/UNAVAILABLE, Langfuse 조회, capacity/cost/quality
Snapshot 조회)은 전부 workforce-api 쪽(`scorecard/observability.py`,
`scorecard/postgres_scorecard_repository.py`)에 있고 이 파일에서 복제하지 않는다 -
두 벌이 되면 이벤트 이름·상태 enum 이 조용히 어긋난다(observability.py 머리말
"부서 키가 두 개인 이유"와 같은 계급의 실패 모드).

WORKFORCE_API_URL이 비어 있으면 503이다 - "조회 못 함"을 "유휴 없음"으로 위장하지
않는다(개발 원칙 9, account_snapshot.py/risk.py와 같은 원칙).
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["workforce"])
# 로컬 compose 계약은 workforce-api를 8044에 낸다(departments/07-agent-workforce/
# compose.yaml). risk.py/qa.py와 같은 이유로 문서화된 로컬 2-프로세스 구성이
# 별도 env 파일 없이 그대로 동작하게 기본값을 둔다.
WORKFORCE_API_URL = os.getenv("WORKFORCE_API_URL", "http://127.0.0.1:8044").strip().rstrip("/")
# idle-agents는 workforce-api가 등록된 Worker마다 Langfuse API를 순차 호출한다
# (observability.py check_idle_agents) - 8명 기준으로도 왕복이 쌓이면 8초를 쉽게
# 넘긴다. GOVERNANCE_API_TIMEOUT_SECONDS(30)와 같은 예산을 쓴다.
WORKFORCE_API_TIMEOUT_SECONDS = float(os.getenv("WORKFORCE_API_TIMEOUT_SECONDS", "30"))


async def _workforce_get(path: str, *, params: dict[str, Any] | None = None) -> Any:
    if not WORKFORCE_API_URL:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "workforce_api_unavailable",
                "message": "WORKFORCE_API_URL is not configured",
            },
        )
    try:
        async with httpx.AsyncClient(
            base_url=WORKFORCE_API_URL,
            timeout=WORKFORCE_API_TIMEOUT_SECONDS,
        ) as client:
            response = await client.get(path, params=params)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="workforce_api_unavailable") from exc

    try:
        payload: Any = response.json()
    except ValueError:
        payload = {"detail": f"workforce_api_http_{response.status_code}"}
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=payload)
    return payload


@router.get("/ui/workforce/idle-agents")
async def workforce_idle_agents(
    lookback_hours: float = 24.0,
    idle_threshold_hours: float = 4.0,
) -> Any:
    """6개 투자본부 Worker 전원의 ACTIVE/IDLE/UNOBSERVED/UNAVAILABLE 판정.

    순수 프록시다 - `workforce-api GET /workforce/v1/departments/idle-agents`가
    이미 자격증명 미설정·조회 실패를 501이 아니라 워커별 UNAVAILABLE로 접는다
    (observability.py). 여기서 그 판정을 다시 만들지 않는다.
    """

    if idle_threshold_hours <= 0:
        raise HTTPException(status_code=422, detail="idle_threshold_hours must be positive")
    return await _workforce_get(
        "/workforce/v1/departments/idle-agents",
        params={
            "lookback_hours": lookback_hours,
            "idle_threshold_hours": idle_threshold_hours,
        },
    )


@router.get("/ui/workforce/roster")
async def workforce_roster() -> Any:
    """등록된 Agent 전원의 고용 상태·현재 Profile Version·모델 좌표.

    순수 프록시다 - `workforce-api GET /workforce/v1/roster`가 이미
    DATABASE_URL 미설정을 501로 정직하게 응답한다(In-Memory 대체로 위장하지
    않음, api/app.py 머리말 참고). 여기서 빈 목록으로 바꿔치기하지 않는다.
    """

    return await _workforce_get("/workforce/v1/roster")


__all__ = ["WORKFORCE_API_URL", "router", "workforce_idle_agents", "workforce_roster"]
