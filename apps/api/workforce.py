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


@router.get("/ui/workforce/capacity")
async def workforce_capacity(lookback_hours: float = 24.0) -> Any:
    """6개 투자본부 전체의 Langfuse 기반 Capacity(용량) 관측.

    순수 프록시다 - `workforce-api GET /workforce/v1/departments/capacity`가
    이미 Langfuse 실행 이벤트를 직접 집계해서 준다(idle-agents와 같은 원리,
    별도 DB Snapshot 파이프라인 없이). 여기서 그 집계를 다시 하지 않는다.
    """

    return await _workforce_get(
        "/workforce/v1/departments/capacity", params={"lookback_hours": lookback_hours}
    )


@router.get("/ui/workforce/roster")
async def workforce_roster() -> Any:
    """등록된 Agent 전원의 고용 상태·현재 Profile Version·모델 좌표.

    순수 프록시다 - `workforce-api GET /workforce/v1/roster`가 이미
    DATABASE_URL 미설정을 501로 정직하게 응답한다(In-Memory 대체로 위장하지
    않음, api/app.py 머리말 참고). 여기서 빈 목록으로 바꿔치기하지 않는다.
    """

    return await _workforce_get("/workforce/v1/roster")


@router.get("/ui/workforce/agents/{agent_id}/access")
async def workforce_agent_access(agent_id: str) -> Any:
    """Agent 한 명의 Access Assignment 목록 - Roster 카드를 펼쳤을 때만 호출한다.

    Roster는 벌크 조회지만 Access는 workforce-api에도 Agent 단건 엔드포인트
    (`GET /workforce/v1/agents/{agent_id}/access`)만 있다. 등록 Agent 전원을
    로드 시점에 N+1로 훑지 않고, 화면에서 사용자가 펼친 행 하나만 그때 부른다 -
    벌크 Access 조회가 필요해지면 그때 workforce-api에 department 단위 API를
    새로 만든다(roster 목록에서 미리 다 부르지 않는다).
    """

    return await _workforce_get(f"/workforce/v1/agents/{agent_id}/access")


@router.get("/ui/workforce/hiring-requests")
async def workforce_hiring_requests(status: str | None = None) -> Any:
    """채용 제안 전원 - DRAFT/OPEN/EVALUATING/APPROVED/REJECTED/CLOSED.

    순수 프록시다. workforce-api 쪽 Hiring Repository는 access.py와 같은 이유로
    DATABASE_URL 미설정 시 501이 아니라 In-Memory로 조용히 대체된다(roster와
    다른 설계 - api/app.py 머리말 참고) - 이 화면에서 빈 목록이 "미설정"인지
    "정말 0건"인지는 workforce-api 쪽 설정을 봐야 한다.
    """

    return await _workforce_get("/workforce/v1/hiring-requests", params={"status": status} if status else None)


@router.get("/ui/workforce/improvements")
async def workforce_improvements() -> Any:
    """자기 개선 후보(ImprovementCandidate) 전원 - PROPOSED부터 종료 상태까지.

    순수 프록시다. hiring-requests와 같은 이유로 DATABASE_URL 미설정 시 501이
    아니라 In-Memory 대체다.
    """

    return await _workforce_get("/workforce/v1/improvements")


@router.get("/ui/workforce/workforce-plans")
async def workforce_plans() -> Any:
    """6개 투자본부 전체의 Workforce Plan(HR-01 Capacity Report/Staffing Scenario).

    순수 프록시다 - `workforce-api GET /workforce/v1/workforce-plans`가 이미
    department_code 단위 API 대신 전체를 모아서 준다(idle-agents와 같은 이유:
    이 화면의 소비자가 항상 전체를 본다). 여기서 부서별로 N번 부르지 않는다.
    """

    return await _workforce_get("/workforce/v1/workforce-plans")


__all__ = [
    "WORKFORCE_API_URL",
    "router",
    "workforce_idle_agents",
    "workforce_capacity",
    "workforce_roster",
    "workforce_agent_access",
    "workforce_hiring_requests",
    "workforce_improvements",
    "workforce_plans",
]
