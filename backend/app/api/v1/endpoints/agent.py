"""LangGraph upstream health and invocation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from backend.app.core.config import get_settings
from backend.app.schemas.agent import AgentHealthResponse, AgentInvokeRequest, AgentInvokeResponse
from backend.app.services.langgraph_service import LangGraphService, LangGraphServiceError

router = APIRouter(prefix="/agent", tags=["agent"])


def get_langgraph_service() -> LangGraphService:
    return LangGraphService(get_settings())


@router.get(
    "/health",
    response_model=AgentHealthResponse,
    summary="부서 LangGraph upstream 상태 확인",
    description="부서 LangGraph 서버의 health endpoint를 비동기로 확인합니다.",
)
async def langgraph_health(
    service: LangGraphService = Depends(get_langgraph_service),
) -> AgentHealthResponse:
    return AgentHealthResponse(**(await service.health_check()))


@router.post(
    "/invoke",
    response_model=AgentInvokeResponse,
    summary="부서 LangGraph 작업 요청 전달",
    description="주문·Risk 승인·원장 변경이 아닌 부서 작업 입력만 upstream에 전달합니다.",
)
async def invoke_langgraph(
    request: AgentInvokeRequest,
    service: LangGraphService = Depends(get_langgraph_service),
) -> AgentInvokeResponse:
    try:
        result = await service.invoke(
            department=request.department,
            query=request.query,
            context=request.context,
        )
    except LangGraphServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="langgraph_upstream_unavailable",
        ) from exc
    return AgentInvokeResponse(status="accepted", department=request.department, result=result)
