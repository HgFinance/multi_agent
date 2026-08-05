"""Read-only portfolio service with explicit asset toggle branching."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from backend.app.schemas.portfolio import (
    DerivativeItem,
    PortfolioAssets,
    PortfolioResponse,
    PortfolioSummary,
    StockItem,
)


class PortfolioRepository(Protocol):
    """Small repository port, replaceable by a DB adapter later."""

    def list_stocks(self) -> list[StockItem]: ...

    def list_derivatives(self) -> list[DerivativeItem]: ...


class InMemoryPortfolioRepository:
    """Deterministic sample source used until the canonical DB read model lands."""

    def list_stocks(self) -> list[StockItem]:
        return [
            StockItem(symbol="005930", name="삼성전자", quantity="10", avg_price="72000", current_price="81000"),
            StockItem(symbol="000660", name="SK하이닉스", quantity="4", avg_price="180000", current_price="205000"),
        ]

    def list_derivatives(self) -> list[DerivativeItem]:
        return [
            DerivativeItem(
                symbol="KOSPI200-FUT",
                name="KOSPI 200 선물",
                underlying="KOSPI200",
                position_type="LONG",
                contracts="1",
                avg_price="350.25",
            ),
            DerivativeItem(
                symbol="SPX-PUT-6000",
                name="S&P 500 보호 풋",
                underlying="SPX",
                position_type="LONG",
                contracts="2",
                avg_price="42.50",
            ),
        ]


class PortfolioService:
    """Build a validated projection and avoid disabled-source work."""

    def __init__(self, repository: PortfolioRepository | None = None) -> None:
        self._repository = repository or InMemoryPortfolioRepository()

    def get_portfolio(
        self,
        *,
        include_stock: bool = True,
        include_derivatives: bool = True,
    ) -> PortfolioResponse:
        stocks = self._repository.list_stocks() if include_stock else None
        derivatives = self._repository.list_derivatives() if include_derivatives else None

        total = Decimal("0")
        if stocks is not None:
            total += sum((item.quantity * item.current_price for item in stocks), Decimal("0"))
        if derivatives is not None:
            total += sum((item.contracts * item.avg_price for item in derivatives), Decimal("0"))

        return PortfolioResponse(
            summary=PortfolioSummary(total_asset_value=total, currency="KRW"),
            assets=PortfolioAssets(stocks=stocks, derivatives=derivatives),
        )
