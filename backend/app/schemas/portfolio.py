"""Portfolio API contracts.

The public asset model deliberately contains only stocks and derivatives.
There is no bond field or bond enum in this contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AssetType(StrEnum):
    STOCK = "STOCK"
    DERIVATIVE = "DERIVATIVE"


class PositionType(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class StockItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type: AssetType = AssetType.STOCK
    symbol: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    quantity: Decimal = Field(ge=Decimal("0"))
    avg_price: Decimal = Field(ge=Decimal("0"))
    current_price: Decimal = Field(ge=Decimal("0"))


class DerivativeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type: AssetType = AssetType.DERIVATIVE
    symbol: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    underlying: str = Field(min_length=1, max_length=32)
    position_type: PositionType
    contracts: Decimal = Field(ge=Decimal("0"))
    avg_price: Decimal = Field(ge=Decimal("0"))


class PortfolioAssets(BaseModel):
    """Nullable groups make include toggles explicit in the response."""

    model_config = ConfigDict(extra="forbid")

    stocks: list[StockItem] | None = None
    derivatives: list[DerivativeItem] | None = None


class PortfolioSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_asset_value: Decimal = Field(ge=Decimal("0"))
    currency: str = Field(default="KRW", pattern=r"^[A-Z]{3}$")


class PortfolioResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: PortfolioSummary
    assets: PortfolioAssets
    as_of: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
