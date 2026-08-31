"""Last-mile deterministic guard for conditional PAPER actions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_FLOOR

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import (
    ActionSide,
    ConditionalRuleSpec,
    ExecutionMode,
    RuleState,
    SizingType,
)


class ExecutionGuardInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    now: datetime
    rule_state: RuleState
    evaluated_rule_version: int = Field(gt=0)
    active_rule_version: int = Field(gt=0)
    membership_active: bool
    fund_active: bool
    book_active: bool
    market_session_available: bool = True
    market_open: bool
    data_complete: bool
    quote_fresh: bool
    current_price: Decimal = Field(gt=0)
    available_cash: Decimal = Field(ge=0)
    position_quantity: Decimal = Field(ge=0)
    sellable_quantity: Decimal = Field(ge=0)
    lot_size: Decimal = Field(default=Decimal("1"), gt=0)
    trigger_already_claimed: bool = False

    @field_validator("now")
    @classmethod
    def _aware_now(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("now must include timezone")
        return value


class GuardDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    code: str
    message: str
    quantity: Decimal | None = None


def _deny(code: str, message: str) -> GuardDecision:
    return GuardDecision(allowed=False, code=code, message=message)


def _floor_to_lot(value: Decimal, lot_size: Decimal) -> Decimal:
    lots = (value / lot_size).to_integral_value(rounding=ROUND_FLOOR)
    return lots * lot_size


def guard_rule_execution(
    rule: ConditionalRuleSpec,
    snapshot: ExecutionGuardInput,
) -> GuardDecision:
    """Revalidate authority and mechanics immediately before Trading admission."""

    if rule.execution_mode is not ExecutionMode.PAPER:
        return _deny("LIVE_MODE_FORBIDDEN", "조건주문 v1은 PAPER 주문만 허용합니다.")
    if snapshot.rule_state not in {RuleState.ACTIVE, RuleState.TRIGGERED}:
        return _deny("RULE_NOT_ACTIVE", "활성 상태가 아닌 규칙은 주문할 수 없습니다.")
    if snapshot.evaluated_rule_version != snapshot.active_rule_version:
        return _deny("RULE_VERSION_CHANGED", "평가 후 규칙 버전이 변경되어 주문하지 않았습니다.")
    if snapshot.trigger_already_claimed:
        return _deny("DUPLICATE_TRIGGER", "이미 처리된 조건 발생이므로 중복 주문하지 않았습니다.")
    if snapshot.now >= rule.expires_at:
        return _deny("RULE_EXPIRED", "규칙이 만료되어 주문하지 않았습니다.")
    if not snapshot.membership_active:
        return _deny("MEMBERSHIP_INACTIVE", "현재 사용자 계좌 권한을 확인할 수 없습니다.")
    if not snapshot.fund_active or not snapshot.book_active:
        return _deny("TRADING_SCOPE_INACTIVE", "현재 Fund/Book이 활성 상태가 아닙니다.")
    if not snapshot.market_session_available:
        return _deny(
            "MARKET_SESSION_UNAVAILABLE",
            "장 운영 상태를 확인할 수 없어 주문하지 않았습니다.",
        )
    if not snapshot.market_open:
        return _deny(
            "MARKET_CLOSED_NO_ORDER",
            "현재 장이 열려 있지 않아 주문·체결·원장 반영을 하지 않았습니다.",
        )
    if not snapshot.data_complete:
        return _deny("MARKET_DATA_INCOMPLETE", "완료된 시장 데이터가 없어 주문하지 않았습니다.")
    if not snapshot.quote_fresh:
        return _deny("MARKET_QUOTE_STALE", "현재가가 최신 상태가 아니어서 주문하지 않았습니다.")

    sizing = rule.action.sizing
    if sizing.type is SizingType.FIXED_SHARES:
        requested = sizing.value or Decimal("0")
    elif sizing.type is SizingType.NOTIONAL_KRW:
        # A Korean equity order ultimately needs an integral, lot-aligned
        # quantity.  The user-confirmed KRW value is therefore a ceiling, not
        # a promise to spend an impossible fractional share amount.
        requested = (sizing.value or Decimal("0")) / snapshot.current_price
    elif sizing.type is SizingType.POSITION_PERCENT:
        requested = snapshot.sellable_quantity * (sizing.value or Decimal("0"))
    elif sizing.type is SizingType.ALL:
        requested = snapshot.sellable_quantity
    else:  # pragma: no cover - enum and schema make this unreachable.
        return _deny("SIZING_UNSUPPORTED", "지원하지 않는 주문 수량 방식입니다.")

    quantity = _floor_to_lot(requested, snapshot.lot_size)
    if quantity <= 0:
        return _deny("ZERO_EXECUTABLE_QUANTITY", "현재 매매 가능한 수량이 0주입니다.")

    if rule.action.side is ActionSide.SELL:
        if quantity > snapshot.sellable_quantity:
            return _deny("INSUFFICIENT_POSITION", "현재 매도 가능 수량이 부족합니다.")
    else:
        required_cash = quantity * snapshot.current_price
        if required_cash > snapshot.available_cash:
            return _deny("INSUFFICIENT_CASH", "현재 PAPER 현금 잔고가 부족합니다.")

    return GuardDecision(
        allowed=True,
        code="READY_FOR_PAPER_DIRECTIVE",
        message="결정론적 조건주문 가드를 통과했습니다.",
        quantity=quantity,
    )


__all__ = [
    "ExecutionGuardInput",
    "GuardDecision",
    "guard_rule_execution",
]
