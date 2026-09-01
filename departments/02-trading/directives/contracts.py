"""Contracts for authenticated user-priority PAPER directives.

Natural-language interpretation is deliberately outside this contract.  The
BFF must submit a canonical, structured payload after authenticating the user
and checking fund membership.  Trading verifies the signed bindings again.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DirectiveAction(StrEnum):
    PLACE_ORDER = "PLACE_ORDER"
    PLACE_BASKET = "PLACE_BASKET"
    CANCEL_ALL = "CANCEL_ALL"
    SELL_ALL = "SELL_ALL"
    # Liquidate the whole holding of ONE instrument.  Trading sizes it from the
    # account snapshot, so the payload carries a symbol but never a quantity.
    SELL_POSITION = "SELL_POSITION"


class DirectiveState(StrEnum):
    RECEIVED = "RECEIVED"
    RUNNING = "RUNNING"
    IN_PROGRESS = "IN_PROGRESS"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class DirectiveLegState(StrEnum):
    PENDING = "PENDING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    SKIPPED = "SKIPPED"


DIRECTIVE_PRIORITIES: dict[DirectiveAction, int] = {
    DirectiveAction.PLACE_ORDER: 1000,
    DirectiveAction.PLACE_BASKET: 1000,
    DirectiveAction.CANCEL_ALL: 2000,
    DirectiveAction.SELL_ALL: 2000,
    DirectiveAction.SELL_POSITION: 2000,
}


class SellPositionPayload(BaseModel):
    """One instrument to liquidate; the account snapshot sets the quantity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: UUID | None = None
    symbol: str = Field(pattern=r"^[0-9A-Z]{6}$")
    side: str = Field(pattern=r"^SELL$")
    order_type: str = Field(pattern=r"^MARKET$")
    time_in_force: str = Field(pattern=r"^DAY$")
    reduce_only: bool = True

    @field_validator("symbol", mode="before")
    @classmethod
    def _canonical_symbol(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @model_validator(mode="after")
    def _reduce_only(self) -> "SellPositionPayload":
        if not self.reduce_only:
            raise ValueError("SELL_POSITION must stay reduce_only")
        return self


class PlaceOrderPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # The BFF does not own the reference catalog.  A missing UUID is resolved
    # fail-closed from the six-character KRX symbol by Trading.
    instrument_id: UUID | None = None
    symbol: str = Field(pattern=r"^[0-9A-Z]{6}$")
    side: str = Field(pattern=r"^(BUY|SELL)$")
    quantity: Decimal = Field(gt=0, max_digits=30, decimal_places=10)
    order_type: str = Field(pattern=r"^(MARKET|LIMIT)$")
    limit_price: Decimal | None = Field(default=None, gt=0, max_digits=30, decimal_places=10)
    time_in_force: str = Field(pattern=r"^DAY$")

    @field_validator("symbol", mode="before")
    @classmethod
    def _canonical_symbol(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @model_validator(mode="after")
    def _price_matches_type(self) -> "PlaceOrderPayload":
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("LIMIT order requires limit_price")
        if self.order_type == "MARKET" and self.limit_price is not None:
            raise ValueError("MARKET order must not include limit_price")
        return self


class BasketOrderItem(BaseModel):
    """One market leg in a bounded PAPER basket.

    A KRW notional member is a BUY allocation and Trading derives its integer
    share quantity from a fresh executable ask.  An explicit quantity member
    instead preserves the user's concrete same-direction BUY or SELL size.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: UUID | None = None
    symbol: str = Field(pattern=r"^[0-9A-Z]{6}$")
    # Exactly one sizing policy is allowed. NOTIONAL_KRW is a maximum BUY
    # allocation; explicit quantity is available for a same-direction basket
    # of concrete BUY or SELL legs.
    notional_krw: Decimal | None = Field(
        default=None, gt=0, max_digits=30, decimal_places=0
    )
    quantity: Decimal | None = Field(
        default=None, gt=0, max_digits=30, decimal_places=10
    )
    side: str = Field(default="BUY", pattern=r"^(BUY|SELL)$")
    order_type: str = Field(default="MARKET", pattern=r"^MARKET$")
    time_in_force: str = Field(default="DAY", pattern=r"^DAY$")

    @field_validator("symbol", mode="before")
    @classmethod
    def _canonical_symbol(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @model_validator(mode="after")
    def _sizing_policy(self) -> "BasketOrderItem":
        if (self.notional_krw is None) == (self.quantity is None):
            raise ValueError("basket member requires exactly one sizing policy")
        if self.notional_krw is not None and self.side != "BUY":
            raise ValueError("notional basket member must be BUY")
        return self


class PlaceBasketPayload(BaseModel):
    """One or more independently executable same-side PAPER allocation legs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    orders: tuple[BasketOrderItem, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _unique_symbols(self) -> "PlaceBasketPayload":
        symbols = [item.symbol for item in self.orders]
        if len(set(symbols)) != len(symbols):
            raise ValueError("basket cannot contain a duplicate symbol")
        ids = [item.instrument_id for item in self.orders if item.instrument_id]
        if len(set(ids)) != len(ids):
            raise ValueError("basket cannot contain a duplicate instrument_id")
        sides = {item.side for item in self.orders}
        if len(sides) != 1:
            raise ValueError("basket members must have one shared side")
        return self


class UserDirectiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fund_id: UUID
    book_id: UUID
    action: DirectiveAction
    instruction_ref: str = Field(min_length=8, max_length=128)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _action_payload(self) -> "UserDirectiveRequest":
        if self.action is DirectiveAction.PLACE_ORDER:
            # Validation here makes the canonical object identical on both the
            # proof and execution paths, including explicit null instrument_id.
            PlaceOrderPayload.model_validate(self.payload)
        elif self.action is DirectiveAction.PLACE_BASKET:
            PlaceBasketPayload.model_validate(self.payload)
        elif self.action is DirectiveAction.SELL_POSITION:
            SellPositionPayload.model_validate(self.payload)
        elif self.payload:
            raise ValueError(f"{self.action.value} payload must be an empty object")
        return self

    def place_order(self) -> PlaceOrderPayload:
        if self.action is not DirectiveAction.PLACE_ORDER:
            raise ValueError("directive is not PLACE_ORDER")
        return PlaceOrderPayload.model_validate(self.payload)

    def place_basket(self) -> PlaceBasketPayload:
        if self.action is not DirectiveAction.PLACE_BASKET:
            raise ValueError("directive is not PLACE_BASKET")
        return PlaceBasketPayload.model_validate(self.payload)

    def sell_position(self) -> SellPositionPayload:
        if self.action is not DirectiveAction.SELL_POSITION:
            raise ValueError("directive is not SELL_POSITION")
        return SellPositionPayload.model_validate(self.payload)

    def canonical_payload(self) -> dict[str, Any]:
        if self.action is DirectiveAction.PLACE_ORDER:
            return self.place_order().model_dump(mode="json", exclude_none=False)
        if self.action is DirectiveAction.PLACE_BASKET:
            return self.place_basket().model_dump(mode="json", exclude_none=False)
        if self.action is DirectiveAction.SELL_POSITION:
            return self.sell_position().model_dump(mode="json", exclude_none=False)
        return {}

    def payload_sha256(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def priority(self) -> int:
        return DIRECTIVE_PRIORITIES[self.action]


__all__ = [
    "DIRECTIVE_PRIORITIES",
    "BasketOrderItem",
    "DirectiveAction",
    "DirectiveLegState",
    "DirectiveState",
    "PlaceOrderPayload",
    "PlaceBasketPayload",
    "UserDirectiveRequest",
]
