"""Canonical, deterministic status projection for conditional PAPER orders.

The Trading API remains the execution source of truth.  This module only
validates and formats an already-authorized directive response so MCP, Discord,
Kanban, and Notion cannot grow separate interpretations of the same fill.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConditionalStatusError(RuntimeError):
    """The authoritative snapshot is missing or internally inconsistent."""


class ConditionalExecutionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "conditional-paper-execution-status.v1"
    authority_source: str = "trading.user_directives"
    authority_verified: bool = True
    mode: str = "PAPER"
    rule_id: str
    rule_execution_id: str | None = None
    directive_id: str
    directive_state: str
    workflow_state: str
    accounting_acknowledged: bool
    symbol: str | None = None
    side: str | None = None
    order_type: str | None = None
    requested_quantity: str | None = None
    filled_quantity: str = "0"
    average_fill_price: str | None = None
    broker_order_id: str | None = None
    error_code: str | None = None
    verified_at: datetime
    final_answer: str = Field(min_length=1, max_length=4000)


def _object(value: Any) -> dict[str, Any]:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        value = dump(mode="json")
    if not isinstance(value, Mapping):
        raise ConditionalStatusError("directive status is not an object")
    return dict(value)


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError) as exc:
        raise ConditionalStatusError(f"invalid {field}") from exc


def _display_decimal(value: Decimal) -> str:
    """Render an exact Decimal without database scale padding."""

    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def _display_krw(value: Decimal) -> str:
    """Render KRW with grouping while retaining any real fractional digits."""

    plain = _display_decimal(value)
    whole, separator, fraction = plain.partition(".")
    grouped = f"{int(whole):,}"
    return f"{grouped}{separator}{fraction}" if separator else grouped


def build_conditional_execution_status(
    *,
    rule_id: str,
    directive: Any,
    rule_execution_id: str | None = None,
    expected_directive_id: str | None = None,
    workflow_state: str | None = None,
    verified_at: datetime | None = None,
) -> ConditionalExecutionStatus:
    """Validate one Trading snapshot and create the sole user-facing report."""

    raw = _object(directive)
    directive_id = str(raw.get("directive_id") or "").strip()
    if not directive_id:
        raise ConditionalStatusError("directive_id is missing")
    if expected_directive_id and directive_id != str(expected_directive_id):
        raise ConditionalStatusError("conditional event and Trading directive mismatch")
    if str(raw.get("mode") or "").upper() != "PAPER":
        raise ConditionalStatusError("non-PAPER directive reached conditional status")

    legs = raw.get("legs") or []
    if not isinstance(legs, list) or not all(
        isinstance(item, Mapping) for item in legs
    ):
        raise ConditionalStatusError("directive legs are invalid")
    executable = [item for item in legs if item.get("side") and item.get("symbol")]
    if len(executable) != 1:
        raise ConditionalStatusError(
            "conditional directive must contain exactly one order leg"
        )
    leg = dict(executable[0])
    filled = _decimal(leg.get("filled_quantity"), field="filled_quantity")
    requested = _decimal(leg.get("requested_quantity"), field="requested_quantity")
    if filled < 0 or requested <= 0 or filled > requested:
        raise ConditionalStatusError("directive fill quantities are inconsistent")
    average = leg.get("average_fill_price")
    average_decimal = (
        _decimal(average, field="average_fill_price") if average is not None else None
    )
    if filled > 0 and (average_decimal is None or average_decimal <= 0):
        raise ConditionalStatusError("filled directive has no valid average fill price")

    directive_state = str(raw.get("state") or "UNKNOWN").upper()
    error_code = str(raw.get("error_code") or "").strip() or None
    effective_workflow_state = str(workflow_state or "").upper()
    if not effective_workflow_state:
        if error_code == "TRADING_FILL_ACCOUNTING_PENDING":
            effective_workflow_state = "ACCOUNTING_PENDING"
        elif directive_state == "COMPLETED":
            effective_workflow_state = "COMPLETED"
        elif directive_state in {"FAILED", "UNKNOWN"}:
            effective_workflow_state = directive_state
        else:
            effective_workflow_state = "IN_PROGRESS"
    accounting_acknowledged = effective_workflow_state == "COMPLETED"

    symbol = str(leg.get("symbol") or "").strip() or None
    side = str(leg.get("side") or "").strip().upper() or None
    order_type = str(leg.get("order_type") or "").strip().upper() or None
    broker_order_id = str(leg.get("broker_order_id") or "").strip() or None
    verified = verified_at or datetime.now(timezone.utc)
    accounting_status = (
        "완료" if accounting_acknowledged else ("대기" if filled > 0 else "미반영")
    )
    if effective_workflow_state == "COMPLETED":
        unknowns = "없음"
    elif filled > 0:
        unknowns = "회계 원장 반영"
    else:
        unknowns = "체결 및 회계 원장 반영"

    lines = [
        f"Ticker : {symbol or '-'}",
        f"Status : {side or '-'}",
        f"체결 수량 : {_display_decimal(filled)}주",
        f"주문 유형 : {order_type or '-'}",
        "평균 체결가 : "
        + (f"{_display_krw(average_decimal)}원" if average_decimal is not None else "-"),
        f"브로커 주문 ID : {broker_order_id or '-'}",
        f"처리 상태 : {effective_workflow_state}",
        f"회계 반영 : {accounting_status}",
        f"검증 시각 : {verified.isoformat()}",
        "권위 근거 : trading.user_directives "
        f"directive_id={directive_id}",
        f"조건 규칙 ID : {rule_id}",
        f"미확인 항목 : {unknowns}",
    ]
    if error_code:
        lines.append(f"오류 코드 : {error_code}")
    answer = "\n".join(lines)

    return ConditionalExecutionStatus(
        rule_id=str(rule_id),
        rule_execution_id=str(rule_execution_id) if rule_execution_id else None,
        directive_id=directive_id,
        directive_state=directive_state,
        workflow_state=effective_workflow_state,
        accounting_acknowledged=accounting_acknowledged,
        symbol=symbol,
        side=side,
        order_type=order_type,
        requested_quantity=_display_decimal(requested),
        filled_quantity=_display_decimal(filled),
        average_fill_price=_display_decimal(average_decimal)
        if average_decimal is not None
        else None,
        broker_order_id=broker_order_id,
        error_code=error_code,
        verified_at=verified,
        final_answer=answer,
    )


__all__ = [
    "ConditionalExecutionStatus",
    "ConditionalStatusError",
    "build_conditional_execution_status",
]
