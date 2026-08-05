"""Portfolio projection endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from backend.app.schemas.portfolio import PortfolioResponse
from backend.app.services.portfolio_service import PortfolioService

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


def get_portfolio_service() -> PortfolioService:
    return PortfolioService()


@router.get(
    "",
    response_model=PortfolioResponse,
    summary="주식·파생상품 포트폴리오 조회",
    description=(
        "읽기 전용 포트폴리오 Projection입니다. include_stock와 "
        "include_derivatives가 False인 자산은 조회·계산·응답에서 제외됩니다. "
        "채권 자산은 이 API의 공개 계약에 포함되지 않습니다."
    ),
)
async def get_portfolio(
    include_stock: bool = Query(
        default=True,
        description="주식 데이터를 포함합니다. False이면 stocks는 null입니다.",
    ),
    include_derivatives: bool = Query(
        default=True,
        description="파생상품 데이터를 포함합니다. False이면 derivatives는 null입니다.",
    ),
    service: PortfolioService = Depends(get_portfolio_service),
) -> PortfolioResponse:
    return service.get_portfolio(
        include_stock=include_stock,
        include_derivatives=include_derivatives,
    )
