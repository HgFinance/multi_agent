"""Versioned contracts for user-authenticated conditional PAPER rules.

Hermes may propose these objects, but every field remains untrusted until the
schema and semantic validators pass and the authenticated user confirms the
exact canonical fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExpressionType(StrEnum):
    LITERAL = "LITERAL"
    TIME = "TIME"
    MARKET = "MARKET"
    PORTFOLIO = "PORTFOLIO"
    INDICATOR = "INDICATOR"
    ARITHMETIC = "ARITHMETIC"
    COMPARISON = "COMPARISON"
    LOGICAL = "LOGICAL"
    NOT = "NOT"
    CROSS = "CROSS"


class IndicatorSource(StrEnum):
    LOCAL = "LOCAL"
    BROKER = "BROKER"
    MICROSTRUCTURE = "MICROSTRUCTURE"
    DERIVED_REALTIME = "DERIVED_REALTIME"
    PORTFOLIO = "PORTFOLIO"


class ValueUnit(StrEnum):
    BOOL = "BOOL"
    NUMBER = "NUMBER"
    RATIO = "RATIO"
    PRICE = "PRICE"
    KRW = "KRW"
    SHARES = "SHARES"
    VOLUME = "VOLUME"


class Timeframe(StrEnum):
    M1 = "1M"
    M3 = "3M"
    M5 = "5M"
    M15 = "15M"
    H1 = "1H"
    D1 = "1D"


class EvaluationClock(StrEnum):
    BAR_CLOSE = "BAR_CLOSE"
    QUOTE = "QUOTE"


class MarketClosedPolicy(StrEnum):
    REJECT_TRIGGER = "REJECT_TRIGGER"


class ExecutionMode(StrEnum):
    PAPER = "PAPER"


class RepeatPolicy(StrEnum):
    ONCE = "ONCE"


class RuleState(StrEnum):
    DRAFT = "DRAFT"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    VALIDATED = "VALIDATED"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    ACTIVE = "ACTIVE"
    TRIGGERED = "TRIGGERED"
    EXECUTION_PENDING = "EXECUTION_PENDING"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class ActionSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SizingType(StrEnum):
    FIXED_SHARES = "FIXED_SHARES"
    POSITION_PERCENT = "POSITION_PERCENT"
    ALL = "ALL"


class ExpressionNode(BaseModel):
    """One strict recursive node.

    A single model keeps JSON Schema stable across the MCP and database while
    the validator below still rejects fields that do not belong to the chosen
    node type.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: ExpressionType
    value: Decimal | bool | None = None
    unit: ValueUnit | None = None
    field: str | None = None
    name: str | None = None
    output: str | None = None
    source: IndicatorSource | None = None
    provider: str | None = None
    timeframe: Timeframe | None = None
    parameters: dict[str, Decimal | int | str] | None = None
    operator: str | None = None
    left: "ExpressionNode | None" = None
    right: "ExpressionNode | None" = None
    operand: "ExpressionNode | None" = None
    children: tuple["ExpressionNode", ...] | None = None


    @model_validator(mode="before")
    @classmethod
    def _drop_matching_market_unit_hint(cls, value: Any) -> Any:
        """Accept only a redundant, correct unit hint on a MARKET leaf."""

        if not isinstance(value, dict):
            return value
        node_type = str(value.get("type") or "").strip().upper()
        field = str(value.get("field") or "").strip().upper()
        supplied = str(value.get("unit") or "").strip().upper()
        inferred = {
            "LAST_PRICE": "PRICE",
            "OPEN": "PRICE",
            "HIGH": "PRICE",
            "LOW": "PRICE",
            "CLOSE": "PRICE",
            "VOLUME": "VOLUME",
        }.get(field)
        if node_type == "MARKET" and inferred is not None and supplied == inferred:
            normalized = dict(value)
            normalized.pop("unit", None)
            return normalized
        return value

    @model_validator(mode="after")
    def _shape_matches_type(self) -> "ExpressionNode":
        populated = {
            name
            for name in (
                "value", "unit", "field", "name", "output", "source", "provider", "timeframe",
                "parameters", "operator", "left", "right", "operand",
                "children",
            )
            if getattr(self, name) is not None
        }
        allowed: dict[ExpressionType, set[str]] = {
            ExpressionType.LITERAL: {"value", "unit"},
            ExpressionType.TIME: {"field"},
            ExpressionType.MARKET: {"field"},
            ExpressionType.PORTFOLIO: {"field"},
            ExpressionType.INDICATOR: {
                "name", "output", "source", "provider", "timeframe", "parameters",
            },
            ExpressionType.ARITHMETIC: {"operator", "left", "right"},
            ExpressionType.COMPARISON: {"operator", "left", "right"},
            ExpressionType.LOGICAL: {"operator", "children"},
            ExpressionType.NOT: {"operand"},
            ExpressionType.CROSS: {"operator", "left", "right"},
        }
        required: dict[ExpressionType, set[str]] = {
            ExpressionType.LITERAL: {"value", "unit"},
            ExpressionType.TIME: {"field"},
            ExpressionType.MARKET: {"field"},
            ExpressionType.PORTFOLIO: {"field"},
            ExpressionType.INDICATOR: {"name", "timeframe"},
            ExpressionType.ARITHMETIC: {"operator", "left", "right"},
            ExpressionType.COMPARISON: {"operator", "left", "right"},
            ExpressionType.LOGICAL: {"operator", "children"},
            ExpressionType.NOT: {"operand"},
            ExpressionType.CROSS: {"operator", "left", "right"},
        }
        if not required[self.type].issubset(populated):
            missing = sorted(required[self.type] - populated)
            raise ValueError(f"{self.type.value} node is missing {missing}")
        unexpected = populated - allowed[self.type]
        if unexpected:
            raise ValueError(
                f"{self.type.value} node has unexpected fields {sorted(unexpected)}"
            )
        if self.type is ExpressionType.LOGICAL and len(self.children or ()) < 2:
            raise ValueError("LOGICAL node requires at least two children")
        if self.type is ExpressionType.INDICATOR:
            object.__setattr__(self, "output", (self.output or "value").upper())
            if self.provider is not None:
                object.__setattr__(self, "provider", self.provider.strip().upper())
            object.__setattr__(self, "parameters", self.parameters or {})
        return self

    @field_validator("field", "name", "output", "operator", mode="before")
    @classmethod
    def _canonical_token(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value


class SizingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: SizingType
    value: Decimal | None = None

    @model_validator(mode="after")
    def _valid_sizing(self) -> "SizingPolicy":
        if self.type is SizingType.ALL:
            if self.value is not None:
                raise ValueError("ALL sizing must not include value")
            return self
        if self.value is None or not self.value.is_finite() or self.value <= 0:
            raise ValueError(f"{self.type.value} sizing requires a positive value")
        if self.type is SizingType.FIXED_SHARES and self.value != self.value.to_integral_value():
            raise ValueError("FIXED_SHARES must be an integer")
        if self.type is SizingType.POSITION_PERCENT and self.value > 1:
            raise ValueError("POSITION_PERCENT must be within (0, 1]")
        return self


class RuleAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    side: ActionSide
    sizing: SizingPolicy
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_price: Decimal | None = Field(
        default=None, gt=0, max_digits=30, decimal_places=10
    )
    time_in_force: Literal["DAY"] = "DAY"

    @model_validator(mode="after")
    def _valid_order_and_sizing(self) -> "RuleAction":
        if (
            self.side is ActionSide.BUY
            and self.sizing.type in {SizingType.POSITION_PERCENT, SizingType.ALL}
        ):
            raise ValueError("BUY supports FIXED_SHARES only in v1")
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("LIMIT requires limit_price")
        if self.order_type == "MARKET" and self.limit_price is not None:
            raise ValueError("MARKET must not include limit_price")
        return self


class EvaluationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    clock: EvaluationClock
    primary_timeframe: Timeframe | None = None
    market_closed_policy: MarketClosedPolicy = MarketClosedPolicy.REJECT_TRIGGER
    max_data_age_seconds: int = Field(default=30, ge=1, le=300)

    @model_validator(mode="after")
    def _clock_contract(self) -> "EvaluationPolicy":
        if self.clock is EvaluationClock.BAR_CLOSE and self.primary_timeframe is None:
            raise ValueError("BAR_CLOSE requires primary_timeframe")
        if self.clock is EvaluationClock.QUOTE and self.primary_timeframe is not None:
            raise ValueError("QUOTE must not include primary_timeframe")
        return self


class RuleAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: UUID
    fund_id: UUID
    book_id: UUID


class ConditionalRuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["conditional-trade-rule.v1"]
    authority: RuleAuthority
    instrument_id: UUID
    symbol: str = Field(pattern=r"^[0-9A-Z]{6}$")
    condition: ExpressionNode
    action: RuleAction
    evaluation: EvaluationPolicy
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    repeat_policy: RepeatPolicy = RepeatPolicy.ONCE
    expires_at: datetime
    raw_instruction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("symbol", mode="before")
    @classmethod
    def _canonical_symbol(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("expires_at")
    @classmethod
    def _aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expires_at must include timezone")
        return value


def _canonical_payload(value: BaseModel) -> bytes:
    dumped = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        dumped,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def expression_fingerprint(expression: ExpressionNode) -> str:
    return hashlib.sha256(_canonical_payload(expression)).hexdigest()


def rule_fingerprint(rule: ConditionalRuleSpec) -> str:
    return hashlib.sha256(_canonical_payload(rule)).hexdigest()


ExpressionNode.model_rebuild()


__all__ = [
    "ActionSide",
    "ConditionalRuleSpec",
    "EvaluationClock",
    "EvaluationPolicy",
    "ExecutionMode",
    "ExpressionNode",
    "ExpressionType",
    "IndicatorSource",
    "MarketClosedPolicy",
    "RepeatPolicy",
    "RuleAction",
    "RuleAuthority",
    "RuleState",
    "SizingPolicy",
    "SizingType",
    "Timeframe",
    "ValueUnit",
    "expression_fingerprint",
    "rule_fingerprint",
]
