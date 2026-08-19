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
    CANCEL_ALL = "CANCEL_ALL"
    SELL_ALL = "SELL_ALL"


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
    DirectiveAction.CANCEL_ALL: 2000,
    DirectiveAction.SELL_ALL: 2000,
}


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
        elif self.payload:
            raise ValueError(f"{self.action.value} payload must be an empty object")
        return self

    def place_order(self) -> PlaceOrderPayload:
        if self.action is not DirectiveAction.PLACE_ORDER:
            raise ValueError("directive is not PLACE_ORDER")
        return PlaceOrderPayload.model_validate(self.payload)

    def canonical_payload(self) -> dict[str, Any]:
        if self.action is DirectiveAction.PLACE_ORDER:
            return self.place_order().model_dump(mode="json", exclude_none=False)
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
    "DirectiveAction",
    "DirectiveLegState",
    "DirectiveState",
    "PlaceOrderPayload",
    "UserDirectiveRequest",
]
