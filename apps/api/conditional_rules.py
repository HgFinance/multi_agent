"""Authenticated management API for conditional PAPER rules."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestration.conditional_rules import (
    ConditionalRuleSpec,
    EvaluationPolicy,
    ExpressionNode,
    Timeframe,
    RuleAction,
    RuleSemanticError,
    RuleState,
    rule_fingerprint,
    validate_rule_spec,
)
from orchestration.user_order_language import DelayedPaperOrderPlan

try:
    from .conditional_rule_language import clarification_codes, preview_assumptions
    from .conditional_rule_workflow import (
        ConditionalRuleConflict,
        ConditionalRuleNotFound,
        ConditionalRuleRecord,
        ConditionalRuleUnavailable,
        conditional_rule_repository,
    )
    from .current_user import (
        current_user,
        require_trading_book_access,
        resolve_active_trading_instrument,
    )
except ImportError:  # pragma: no cover - direct module execution compatibility
    from conditional_rule_language import clarification_codes, preview_assumptions
    from conditional_rule_workflow import (  # type: ignore[no-redef]
        ConditionalRuleConflict,
        ConditionalRuleNotFound,
        ConditionalRuleRecord,
        ConditionalRuleUnavailable,
        conditional_rule_repository,
    )
    from current_user import (  # type: ignore[no-redef]
        current_user,
        require_trading_book_access,
        resolve_active_trading_instrument,
    )


router = APIRouter(prefix="/ui/conditional-rules", tags=["conditional-paper-rules"])


class ConditionalRuleCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1, max_length=80)
    condition: ExpressionNode
    action: RuleAction
    evaluation: EvaluationPolicy
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include timezone")
        return value


DELAYED_ORDER_EXECUTION_WINDOW_SECONDS = 5 * 60


def build_delayed_order_candidate(
    plan: DelayedPaperOrderPlan,
    *,
    admitted_at: datetime,
) -> ConditionalRuleCandidate:
    """Map a strict relative-time order onto the existing rule contract."""

    if admitted_at.tzinfo is None:
        raise ValueError("admitted_at must include timezone")
    trigger_at = admitted_at.astimezone(timezone.utc) + timedelta(
        seconds=plan.delay_seconds
    )
    expires_at = trigger_at + timedelta(
        seconds=DELAYED_ORDER_EXECUTION_WINDOW_SECONDS
    )
    payload = plan.payload
    return ConditionalRuleCandidate.model_validate(
        {
            "symbol": payload.instrument_mention,
            "condition": {
                "type": "COMPARISON",
                "operator": "GTE",
                "left": {
                    "type": "TIME",
                    "field": "OBSERVED_AT_EPOCH_SECONDS",
                },
                "right": {
                    "type": "LITERAL",
                    "value": str(int(trigger_at.timestamp())),
                    "unit": "NUMBER",
                },
            },
            "action": {
                "side": payload.side.value,
                "sizing": {
                    "type": "FIXED_SHARES",
                    "value": payload.quantity,
                },
                "order_type": payload.order_type.value,
                "limit_price": payload.limit_price,
                "time_in_force": payload.time_in_force,
            },
            "evaluation": {"clock": "QUOTE"},
            "expires_at": expires_at,
        }
    )


def relative_time_trigger_at(condition: ExpressionNode) -> datetime | None:
    """Read the canonical trigger instant from a deterministic time rule."""

    if (
        condition.type.value == "COMPARISON"
        and condition.operator == "GTE"
        and condition.left is not None
        and condition.left.type.value == "TIME"
        and condition.left.field == "OBSERVED_AT_EPOCH_SECONDS"
        and condition.right is not None
        and condition.right.type.value == "LITERAL"
        and condition.right.unit is not None
        and condition.right.unit.value == "NUMBER"
        and not isinstance(condition.right.value, bool)
    ):
        try:
            epoch = Decimal(str(condition.right.value))
            if not epoch.is_finite() or epoch != epoch.to_integral_value():
                return None
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


class ConditionalRulePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fund_id: UUID
    book_id: UUID
    raw_instruction: str = Field(min_length=1, max_length=4000)
    candidate: ConditionalRuleCandidate


class ConditionalRulePreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    activatable: bool
    clarification_codes: tuple[str, ...]
    assumptions: tuple[str, ...]
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: ConditionalRuleSpec
    summary: dict[str, Any]


class ConditionalRuleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    raw_instruction: str = Field(min_length=1, max_length=4000)
    expected_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: ConditionalRuleSpec


class ConditionalRuleConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConditionalRuleView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: UUID
    state: RuleState
    rule_version: int
    spec_sha256: str
    confirmed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    spec: ConditionalRuleSpec
    last_execution_state: str | None = None
    last_guard_code: str | None = None
    last_error_code: str | None = None
    directive_id: UUID | None = None
    status_message: str | None = None


_THREE_MINUTE_BAR = re.compile(r"(?<!\d)3\s*분봉")
_KST = ZoneInfo("Asia/Seoul")
_KRX_REGULAR_CLOSE = (15, 30)


def _rewrite_three_minute_nodes(node: ExpressionNode) -> tuple[ExpressionNode, bool]:
    """Map an explicit 3분봉 request to the supported 5분봉 feed.

    The system has no independent 3M market-data capability. This boundary
    rewrites only the requested timeframe to the existing 5M feed; it never
    aggregates 1M candles or creates a second market-data path.
    """

    changed = False
    updates: dict[str, Any] = {}
    if node.timeframe is Timeframe.M3:
        updates["timeframe"] = Timeframe.M5
        changed = True
    for field in ("left", "right", "operand"):
        child = getattr(node, field)
        if child is None:
            continue
        replacement, child_changed = _rewrite_three_minute_nodes(child)
        if child_changed:
            updates[field] = replacement
            changed = True
    if node.children:
        children = []
        children_changed = False
        for child in node.children:
            replacement, child_changed = _rewrite_three_minute_nodes(child)
            children.append(replacement)
            children_changed = children_changed or child_changed
        if children_changed:
            updates["children"] = tuple(children)
            changed = True
    return (node.model_copy(update=updates), changed) if changed else (node, False)


def _normalize_supported_timeframe(
    candidate: ConditionalRuleCandidate, raw_instruction: str
) -> tuple[ConditionalRuleCandidate, bool, bool]:
    """Return candidate, fallback-notice flag, and interpretation-mismatch flag."""

    if not _THREE_MINUTE_BAR.search(raw_instruction):
        return candidate, False, False
    condition, condition_changed = _rewrite_three_minute_nodes(candidate.condition)
    primary = candidate.evaluation.primary_timeframe
    evaluation_changed = primary is Timeframe.M3
    if evaluation_changed:
        evaluation = candidate.evaluation.model_copy(
            update={"primary_timeframe": Timeframe.M5}
        )
    else:
        evaluation = candidate.evaluation
    if condition_changed or evaluation_changed:
        candidate = candidate.model_copy(
            update={"condition": condition, "evaluation": evaluation}
        )
        return candidate, True, False
    if primary is Timeframe.M5:
        return candidate, True, False
    return candidate, False, True


def _subject(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=401, detail="authentication_required")
    return value


def _raw_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _default_krx_close(now: datetime) -> datetime:
    """Return the current or next weekday KRX regular-session close.

    Explicit expiries remain authoritative.  This bounded DAY-style fallback
    replaces the surprising ten-minute TTL while ensuring an omitted expiry
    never becomes an unattended multi-day instruction.  The order guard still
    fail-closes against the authoritative market-session calendar.
    """

    local_now = now.astimezone(_KST)
    day = local_now.date()
    while True:
        close = datetime(
            day.year,
            day.month,
            day.day,
            *_KRX_REGULAR_CLOSE,
            tzinfo=_KST,
        )
        if day.weekday() < 5 and close > local_now:
            return close.astimezone(timezone.utc)
        day += timedelta(days=1)


def _expiry(value: datetime | None, *, now: datetime) -> datetime:
    expiry = (
        value.astimezone(timezone.utc)
        if value
        else _default_krx_close(now)
    )
    if expiry <= now:
        raise HTTPException(status_code=422, detail="conditional_rule_expiry_in_past")
    if expiry > now + timedelta(days=365):
        raise HTTPException(status_code=422, detail="conditional_rule_expiry_too_far")
    return expiry


def _view(record: ConditionalRuleRecord) -> ConditionalRuleView:
    messages = {
        "MARKET_CLOSED_NO_ORDER": (
            "현재 장이 열려 있지 않아 주문을 제출하지 않았습니다. "
            "체결·원장 반영도 없습니다."
        ),
        "MARKET_QUOTE_STALE": (
            "현재가가 최신 상태가 아니어서 주문을 제출하지 않았습니다."
        ),
        "MARKET_SESSION_UNAVAILABLE": (
            "장 운영 상태를 확인할 수 없어 주문을 제출하지 않았습니다."
        ),
        "TRADING_MARKET_SESSION_CLOSED": (
            "현재 장이 열려 있지 않아 주문을 제출하지 않았습니다. "
            "체결·원장 반영도 없습니다."
        ),
        "TRADING_MARKET_SESSION_UNAVAILABLE": (
            "장 운영 상태를 확인할 수 없어 주문을 제출하지 않았습니다."
        ),
        "INSUFFICIENT_CASH": "현재 PAPER 현금 잔고가 부족해 주문하지 않았습니다.",
        "INSUFFICIENT_POSITION": "현재 매도 가능 수량이 부족해 주문하지 않았습니다.",
    }
    return ConditionalRuleView(
        rule_id=UUID(record.rule_id),
        state=record.state,
        rule_version=record.rule_version,
        spec_sha256=record.spec_sha256,
        confirmed_at=record.confirmed_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
        spec=record.spec,
        last_execution_state=record.last_execution_state,
        last_guard_code=record.last_guard_code,
        last_error_code=record.last_error_code,
        directive_id=UUID(record.directive_id) if record.directive_id else None,
        status_message=messages.get(record.last_error_code or record.last_guard_code or ""),
    )


def _summary(
    spec: ConditionalRuleSpec, *, timeframe_fallback: bool = False
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "symbol": spec.symbol,
        "condition": spec.condition.model_dump(mode="json", exclude_none=True),
        "side": spec.action.side.value,
        "sizing": spec.action.sizing.model_dump(mode="json", exclude_none=True),
        "order_type": spec.action.order_type,
        "limit_price": (
            str(spec.action.limit_price) if spec.action.limit_price is not None else None
        ),
        "evaluation_clock": spec.evaluation.clock.value,
        "primary_timeframe": (
            spec.evaluation.primary_timeframe.value
            if spec.evaluation.primary_timeframe
            else None
        ),
        "execution_mode": "PAPER",
        "repeat_policy": "ONCE",
        "expires_at": spec.expires_at.isoformat(),
    }
    if timeframe_fallback:
        result["timeframe_fallback"] = {
            "requested": "3M",
            "used": "5M",
            "reason": "3M_UNSUPPORTED",
        }
    return result


def _validate_semantics(spec: ConditionalRuleSpec) -> None:
    """Turn a semantic rejection into a client error that names the cause.

    ``validate_rule_spec`` raises ``RuleSemanticError``, which is a ValueError
    and used to escape as an unhandled 500.  The caller then saw only the bare
    code with no offending field or operator, which is how an AST rejected for
    naming a non-existent portfolio field looked like a server fault
    (2026-08-27).  The message carries the name; keep it.
    """

    try:
        validate_rule_spec(spec)
    except RuleSemanticError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


def _build_preview(
    request: ConditionalRulePreviewRequest,
    *,
    subject: str,
    now: datetime | None = None,
) -> ConditionalRulePreviewResponse:
    instant = now or datetime.now(timezone.utc)
    candidate, timeframe_fallback, timeframe_mismatch = _normalize_supported_timeframe(
        request.candidate, request.raw_instruction
    )
    access = require_trading_book_access(
        subject, str(request.fund_id), str(request.book_id)
    )
    resolved = resolve_active_trading_instrument(candidate.symbol, None)
    spec = ConditionalRuleSpec.model_validate(
        {
            "schema_version": "conditional-trade-rule.v1",
            "authority": {
                "user_id": access["user_id"],
                "fund_id": access["fund_id"],
                "book_id": access["book_id"],
            },
            "instrument_id": resolved["instrument_id"],
            "symbol": resolved["symbol"],
            "condition": candidate.condition,
            "action": candidate.action,
            "evaluation": candidate.evaluation,
            "execution_mode": "PAPER",
            "repeat_policy": "ONCE",
            "expires_at": _expiry(candidate.expires_at, now=instant),
            "raw_instruction_sha256": _raw_sha256(request.raw_instruction),
        }
    )
    _validate_semantics(spec)
    clarifications = list(clarification_codes(request.raw_instruction, spec))
    if timeframe_mismatch:
        clarifications.append("TIMEFRAME_3M_UNSUPPORTED")
    clarifications = tuple(dict.fromkeys(clarifications))
    assumptions = list(preview_assumptions(request.raw_instruction, spec))
    if candidate.expires_at is None:
        assumptions.append("DEFAULT_EXPIRY_KRX_REGULAR_CLOSE")
    if timeframe_fallback:
        assumptions.append("TIMEFRAME_FALLBACK_3M_TO_5M")
    digest = rule_fingerprint(spec)
    return ConditionalRulePreviewResponse(
        activatable=not clarifications,
        clarification_codes=clarifications,
        assumptions=tuple(dict.fromkeys(assumptions)),
        spec_sha256=digest,
        spec=spec,
        summary=_summary(spec, timeframe_fallback=timeframe_fallback),
    )


def _validate_create(
    request: ConditionalRuleCreateRequest, *, subject: str
) -> ConditionalRuleSpec:
    spec = request.spec
    digest = rule_fingerprint(spec)
    if digest != request.expected_spec_sha256:
        raise HTTPException(status_code=409, detail="conditional_rule_preview_changed")
    if spec.raw_instruction_sha256 != _raw_sha256(request.raw_instruction):
        raise HTTPException(status_code=409, detail="conditional_rule_instruction_changed")
    access = require_trading_book_access(
        subject, str(spec.authority.fund_id), str(spec.authority.book_id)
    )
    if str(spec.authority.user_id) != str(access["user_id"]):
        raise HTTPException(status_code=403, detail="conditional_rule_authority_mismatch")
    resolved = resolve_active_trading_instrument(spec.symbol, str(spec.instrument_id))
    if (
        str(resolved["instrument_id"]) != str(spec.instrument_id)
        or resolved["symbol"] != spec.symbol
    ):
        raise HTTPException(status_code=409, detail="conditional_rule_instrument_changed")
    now = datetime.now(timezone.utc)
    if spec.expires_at <= now or spec.expires_at > now + timedelta(days=365):
        raise HTTPException(status_code=422, detail="conditional_rule_expiry_invalid")
    _validate_semantics(spec)
    clarifications = clarification_codes(request.raw_instruction, spec)
    if clarifications:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "conditional_rule_clarification_required",
                "fields": list(clarifications),
            },
        )
    return spec


def _workflow_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ConditionalRuleNotFound):
        return HTTPException(status_code=404, detail="conditional_rule_not_found")
    if isinstance(exc, ConditionalRuleConflict):
        return HTTPException(status_code=409, detail="conditional_rule_conflict")
    return HTTPException(status_code=503, detail="conditional_rule_store_unavailable")


@router.post("/preview", response_model=ConditionalRulePreviewResponse)
def preview_conditional_rule(
    request: ConditionalRulePreviewRequest,
    subject: str | None = Depends(current_user),
) -> ConditionalRulePreviewResponse:
    return _build_preview(request, subject=_subject(subject))


@router.post("", response_model=ConditionalRuleView, status_code=201)
def create_conditional_rule(
    request: ConditionalRuleCreateRequest,
    subject: str | None = Depends(current_user),
) -> ConditionalRuleView:
    owner = _subject(subject)
    spec = _validate_create(request, subject=owner)
    try:
        record = conditional_rule_repository().create_pending(
            spec=spec,
            raw_instruction=request.raw_instruction,
            client_request_id=request.client_request_id,
            parser_source="HERMES",
        )
    except (ConditionalRuleUnavailable, ConditionalRuleConflict) as exc:
        raise _workflow_error(exc) from exc
    return _view(record)


@router.post("/{rule_id}/activate", response_model=ConditionalRuleView)
def activate_conditional_rule(
    rule_id: UUID,
    request: ConditionalRuleConfirmation,
    subject: str | None = Depends(current_user),
) -> ConditionalRuleView:
    owner = _subject(subject)
    try:
        repository = conditional_rule_repository()
        current = repository.get(str(rule_id), user_id=owner)
        if current is None:
            raise ConditionalRuleNotFound("conditional rule not found")
        require_trading_book_access(owner, current.fund_id, current.book_id)
        return _view(
            repository.activate(
                str(rule_id),
                user_id=owner,
                confirmation_sha256=request.spec_sha256,
            )
        )
    except (ConditionalRuleUnavailable, ConditionalRuleConflict, ConditionalRuleNotFound) as exc:
        raise _workflow_error(exc) from exc


def _transition(rule_id: UUID, subject: str | None, target: RuleState) -> ConditionalRuleView:
    owner = _subject(subject)
    try:
        repository = conditional_rule_repository()
        current = repository.get(str(rule_id), user_id=owner)
        if current is None:
            raise ConditionalRuleNotFound("conditional rule not found")
        require_trading_book_access(owner, current.fund_id, current.book_id)
        return _view(repository.transition(str(rule_id), user_id=owner, target=target))
    except (ConditionalRuleUnavailable, ConditionalRuleConflict, ConditionalRuleNotFound) as exc:
        raise _workflow_error(exc) from exc


@router.post("/{rule_id}/pause", response_model=ConditionalRuleView)
def pause_conditional_rule(
    rule_id: UUID, subject: str | None = Depends(current_user)
) -> ConditionalRuleView:
    return _transition(rule_id, subject, RuleState.PAUSED)


@router.post("/{rule_id}/resume", response_model=ConditionalRuleView)
def resume_conditional_rule(
    rule_id: UUID, subject: str | None = Depends(current_user)
) -> ConditionalRuleView:
    return _transition(rule_id, subject, RuleState.ACTIVE)


@router.delete("/{rule_id}", response_model=ConditionalRuleView)
def cancel_conditional_rule(
    rule_id: UUID, subject: str | None = Depends(current_user)
) -> ConditionalRuleView:
    return _transition(rule_id, subject, RuleState.CANCELLED)


@router.get("/{rule_id}", response_model=ConditionalRuleView)
def get_conditional_rule(
    rule_id: UUID, subject: str | None = Depends(current_user)
) -> ConditionalRuleView:
    owner = _subject(subject)
    try:
        record = conditional_rule_repository().get(str(rule_id), user_id=owner)
    except ConditionalRuleUnavailable as exc:
        raise _workflow_error(exc) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="conditional_rule_not_found")
    require_trading_book_access(owner, record.fund_id, record.book_id)
    return _view(record)


@router.get("", response_model=list[ConditionalRuleView])
def list_conditional_rules(
    subject: str | None = Depends(current_user),
) -> list[ConditionalRuleView]:
    owner = _subject(subject)
    try:
        records = conditional_rule_repository().list_for_user(owner)
    except ConditionalRuleUnavailable as exc:
        raise _workflow_error(exc) from exc
    return [_view(record) for record in records]


__all__ = [
    "ConditionalRuleCandidate",
    "ConditionalRuleCreateRequest",
    "ConditionalRulePreviewRequest",
    "ConditionalRulePreviewResponse",
    "ConditionalRuleView",
    "_build_preview",
    "router",
]
