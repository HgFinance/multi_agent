from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.main import app
from backend.app.schemas.portfolio import StockItem
from backend.app.services.portfolio_service import PortfolioService


client = TestClient(app)


def test_stock_on_derivatives_off_returns_only_stocks() -> None:
    response = client.get("/api/v1/portfolio", params={"include_stock": True, "include_derivatives": False})

    assert response.status_code == 200
    body = response.json()
    assert body["assets"]["stocks"]
    assert body["assets"]["derivatives"] is None
    assert all(item["asset_type"] == "STOCK" for item in body["assets"]["stocks"])
    assert "bond" not in str(body).lower()


def test_stock_off_derivatives_on_returns_only_derivatives() -> None:
    response = client.get("/api/v1/portfolio", params={"include_stock": False, "include_derivatives": True})

    assert response.status_code == 200
    body = response.json()
    assert body["assets"]["stocks"] is None
    assert body["assets"]["derivatives"]
    assert all(item["asset_type"] == "DERIVATIVE" for item in body["assets"]["derivatives"])


def test_both_off_is_a_valid_empty_projection() -> None:
    response = client.get("/api/v1/portfolio", params={"include_stock": False, "include_derivatives": False})

    assert response.status_code == 200
    body = response.json()
    assert body["assets"] == {"stocks": None, "derivatives": None}
    assert Decimal(body["summary"]["total_asset_value"]) == 0


def test_negative_quantity_is_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        StockItem(
            symbol="005930",
            name="삼성전자",
            quantity=Decimal("-1"),
            avg_price=Decimal("70000"),
            current_price=Decimal("80000"),
        )


def test_disabled_sources_are_not_read_or_calculated() -> None:
    class SpyRepository:
        def __init__(self) -> None:
            self.stock_calls = 0
            self.derivative_calls = 0

        def list_stocks(self):
            self.stock_calls += 1
            return []

        def list_derivatives(self):
            self.derivative_calls += 1
            return []

    repository = SpyRepository()
    result = PortfolioService(repository).get_portfolio(include_stock=False, include_derivatives=True)

    assert result.assets.stocks is None
    assert result.assets.derivatives == []
    assert repository.stock_calls == 0
    assert repository.derivative_calls == 1
