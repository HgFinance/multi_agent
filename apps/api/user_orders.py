"""Highest-priority local-fixture directives for PAPER trading only.

The browser never talks to OMS or a broker directly. This router uses the fixed
local demo identity, authorizes one active fund/book, deterministically parses an
optional Korean instruction, and forwards a payload-bound service proof to the
private trading API. Alpha and Risk services are intentionally absent from this
read-only local UI lane.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

try:
    from .conditional_rule_workflow import (
        ConditionalRuleUnavailable,
        conditional_rule_repository,
    )
    from .conditional_rules import conditional_status_message
    from .current_user import (
        current_user,
        require_trading_book_access,
        resolve_active_trading_instrument,
    )
    from .service_token import (
        TRADING_DIRECTIVE_READ_SCOPE,
        TradingProofConfigurationError,
        canonical_json,
        issue_trading_directive_proof,
        payload_sha256,
    )
    from .trading_client import (
        TradingProxyError,
        get_user_directive,
        submit_user_directive,
    )
    from .user_order_workflow import (
        UserOrderWorkflowError,
        UserOrderWorkflowUnavailable,
        directive_execution_event_payload,
        recover_committed_directive,
        user_order_repository,
    )
except ImportError:  # pragma: no cover - direct module execution compatibility
    from conditional_rule_workflow import (  # type: ignore[no-redef]
        ConditionalRuleUnavailable,
        conditional_rule_repository,
    )
    from conditional_rules import conditional_status_message  # type: ignore[no-redef]
    from current_user import (
        current_user,
        require_trading_book_access,
        resolve_active_trading_instrument,
    )
    from service_token import (
        TRADING_DIRECTIVE_READ_SCOPE,
        TradingProofConfigurationError,
        canonical_json,
        issue_trading_directive_proof,
        payload_sha256,
    )
    from trading_client import (
        TradingProxyError,
        get_user_directive,
        submit_user_directive,
    )
    from user_order_workflow import (  # type: ignore[no-redef]
        UserOrderWorkflowError,
        UserOrderWorkflowUnavailable,
        directive_execution_event_payload,
        recover_committed_directive,
        user_order_repository,
    )


router = APIRouter(tags=["paper-user-orders"])

logger = logging.getLogger(__name__)

USER_DIRECTIVES_PATH = "/trading/v1/user-directives"
USER_DIRECTIVE_STATUS_PATH = "/trading/v1/user-directives/{directive_id}"
USER_DIRECTIVE_PRIORITY_CLASS = "USER_DIRECTIVE_HIGHEST"
USER_DIRECTIVE_MODE = "PAPER"
_KRX_TRADING_SYMBOL = re.compile(r"^[0-9A-Z]{6}$")


class DirectiveAction(str, Enum):
    PLACE_ORDER = "PLACE_ORDER"
    SELL_ALL = "SELL_ALL"
    CANCEL_ALL = "CANCEL_ALL"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class DirectiveState(str, Enum):
    RECEIVED = "RECEIVED"
    RUNNING = "RUNNING"
    IN_PROGRESS = "IN_PROGRESS"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class PaperOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    instrument_id: UUID | None = None
    symbol: str = Field(min_length=1, max_length=80)
    side: OrderSide
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=0)
    order_type: OrderType
    time_in_force: Literal["DAY"] = "DAY"
    limit_price: Decimal | None = Field(
        default=None, gt=0, max_digits=24, decimal_places=8
    )

    @field_validator("symbol")
    @classmethod
    def _clean_symbol(cls, value: str) -> str:
        cleaned = " ".join(value.strip().split())
        if not cleaned or any(ord(character) < 32 for character in cleaned):
            raise ValueError("symbol is invalid")
        canonical_code = cleaned.upper()
        if _KRX_TRADING_SYMBOL.fullmatch(canonical_code):
            return canonical_code
        return cleaned

    @model_validator(mode="after")
    def _price_matches_order_type(self) -> "PaperOrderInput":
        if self.order_type == OrderType.MARKET and self.limit_price is not None:
            raise ValueError("market orders cannot include limit_price")
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        return self


class PaperOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_id: UUID
    book_id: UUID
    action: Literal[DirectiveAction.PLACE_ORDER] = DirectiveAction.PLACE_ORDER
    order: PaperOrderInput | None = None
    query: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("query")
    @classmethod
    def _clean_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            raise ValueError("query is empty")
        return cleaned

    @model_validator(mode="after")
    def _one_instruction_source(self) -> "PaperOrderRequest":
        if (self.order is None) == (self.query is None):
            raise ValueError("provide exactly one of order or query")
        return self


class PaperAggregateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_id: UUID
    book_id: UUID


class DirectiveLeg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leg_id: UUID
    leg_index: int = Field(ge=0)
    instrument_id: UUID | None = None
    symbol: str | None = None
    side: OrderSide | None = None
    order_type: OrderType | None = None
    requested_quantity: str | None = None
    limit_price: str | None = None
    filled_quantity: str
    average_fill_price: str | None = None
    target_filled_quantity: str = "0"
    state: str = Field(min_length=1, max_length=64)
    reduce_only: bool
    linked_order_id: UUID | None = None
    client_order_id: str | None = None
    broker_order_id: str | None = None
    broker_event_id: str | None = None
    expires_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None


class UserDirectiveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    directive_id: UUID
    state: DirectiveState
    action: DirectiveAction
    priority: Literal[1000, 2000]
    priority_class: Literal["USER_DIRECTIVE_HIGHEST"] = USER_DIRECTIVE_PRIORITY_CLASS
    mode: Literal["PAPER"] = USER_DIRECTIVE_MODE
    fund_id: UUID
    book_id: UUID
    idempotency_key: str
    instruction_ref: str
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    legs: list[DirectiveLeg]


class ConditionalRuleOutcome(BaseModel):
    """What actually happened to a rule this request created."""

    model_config = ConfigDict(extra="forbid")

    rule_id: UUID
    state: str
    last_execution_state: str | None = None
    last_guard_code: str | None = None
    last_error_code: str | None = None
    status_message: str | None = None


class PaperOrderWorkflowStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["user-paper-order-status.v1"] = "user-paper-order-status.v1"
    order_request_id: UUID
    client_request_id: str
    request_source: Literal["DISCORD", "WEB_OR_API"]
    mode: Literal["PAPER"] = "PAPER"
    state: str
    action: DirectiveAction | None = None
    ceo_root_task_id: str | None = None
    trading_task_id: str | None = None
    clarification_code: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    directive: UserDirectiveResponse | None = None
    correlation: dict[str, Any] | None = None
    # A conditional request finishes its *activation* workflow and is marked
    # COMPLETED, but the rule it created can still fail at execution minutes
    # later with no directive ever produced.  The request state alone therefore
    # read as success while the rule was FAILED (2026-08-28), so the rule
    # outcome travels beside it.
    conditional_rules: list[ConditionalRuleOutcome] | None = None


class ClarificationRequired(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_BUY_PATTERN = re.compile(
    r"(?<![가-힣A-Za-z0-9])(?:매수(?:해\s*줘|해줘|해주세요|해|하자|할게)?|"
    r"구매(?:해\s*줘|해줘|해주세요|해)?|사줘|사주세요|사라|사자|사)(?![가-힣A-Za-z0-9])"
)
_SELL_PATTERN = re.compile(
    r"(?<![가-힣A-Za-z0-9])(?:매도(?:해\s*줘|해줘|해주세요|해|하자|할게)?|"
    r"팔아\s*줘|팔아줘|팔아|파세요|팔자)(?![가-힣A-Za-z0-9])"
)
_GROUPED_INTEGER = r"(?:[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*)"
_QUANTITY_PATTERN = re.compile(
    rf"(?<![\d,])({_GROUPED_INTEGER})\s*(?:주식|주)(?![\d,])"
)
_CODE_PATTERN = re.compile(r"(?<![0-9A-Za-z,])([0-9A-Za-z]{6})(?![0-9A-Za-z,])")
_WON_PRICE_PATTERN = re.compile(rf"(?<![\d,])({_GROUPED_INTEGER})\s*원")
_LIMIT_PRICE_PATTERN = re.compile(rf"지정가(?:는|로|에)?\s*({_GROUPED_INTEGER})\s*원?")
_MARKET_PATTERN = re.compile(r"시장가(?:로|에)?")
_LIMIT_MARKER_PATTERN = re.compile(r"지정가(?:는|로|에)?")

# Aggregate directives are deliberately full-sentence grammars.  A substring
# such as "취소" or "전량 매도" inside a question, negation, or audit request
# must never become an executable instruction.
_SELL_ALL_PATTERN = re.compile(
    r"^(?:(?:내|현재)\s*)?"
    r"(?:(?:보유\s*)?계좌(?:에|의|에서)?\s*)?"
    r"(?:(?:보유(?:한|중인)?|있는)\s*)?"
    r"(?:(?:종목|주식)\s*)?"
    r"(?:전량|전부|모두|전체|다)\s*"
    r"(?:매도(?:해\s*줘|해줘|해주세요|해|하세요|하자|할게)?|"
    r"팔아\s*줘|팔아줘|팔아|파세요|팔자)"
    r"(?:\s*(?:주세요|줘))?[.!]*$"
)
_CANCEL_ALL_PATTERN = re.compile(
    r"^(?:(?:내|현재)\s*)?"
    r"(?:미체결|대기\s*중인|대기|열린)\s*(?:주문|오더)\s*"
    r"(?:전량|전부|모두|전체|다)\s*"
    r"(?:취소(?:해\s*줘|해줘|해주세요|해|하세요|하자)?|"
    r"철회(?:해\s*줘|해줘|해주세요|해|하세요|하자)?)"
    r"(?:\s*(?:주세요|줘))?[.!]*$"
)
_ALLOWED_ORDER_RESIDUAL = re.compile(r"^(?:(?:을|를|은|는|에|로|으로|좀|주문)\s*)*$")


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _natural_name(query: str, quantity_match: re.Match[str]) -> str:
    before = query[: quantity_match.start()]
    after = query[quantity_match.end() :]
    candidate = before if before.strip() else after
    candidate = re.sub(
        r"(?:시장가(?:로)?|지정가(?:는|로|에)?\s*[\d,.]*\s*원?|"
        r"매수(?:해\s*줘|해줘|해주세요|해|하자|할게)?|"
        r"매도(?:해\s*줘|해줘|해주세요|해|하자|할게)?|"
        r"구매(?:해\s*줘|해줘|해주세요|해)?|사줘|사주세요|사라|사자|"
        r"팔아\s*줘|팔아줘|팔아|파세요|팔자)",
        " ",
        candidate,
    )
    candidate = re.sub(
        r"^(?:내\s*)?(?:계좌(?:에|의|에서)?\s*)?(?:보유(?:한)?\s*)?",
        "",
        candidate.strip(),
    )
    candidate = re.sub(r"^(?:종목|주식)\s+", "", candidate)
    candidate = re.sub(r"\s+(?:종목|주식)$", "", candidate)
    candidate = " ".join(candidate.strip(" ,.!?을를").split())
    if (
        not candidate
        or len(candidate) > 40
        or not re.fullmatch(r"[가-힣A-Za-z][가-힣A-Za-z0-9&+._\- ]*", candidate)
    ):
        raise ClarificationRequired("instrument")
    return candidate


def parse_user_order_query(query: str) -> tuple[DirectiveAction, dict[str, Any]]:
    """Parse a narrow Korean order grammar; ambiguity is never guessed by an LLM."""

    normalized = " ".join(query.strip().split())
    if not normalized:
        raise ClarificationRequired("empty_query")

    if _SELL_ALL_PATTERN.fullmatch(normalized):
        return DirectiveAction.SELL_ALL, {}
    if _CANCEL_ALL_PATTERN.fullmatch(normalized):
        return DirectiveAction.CANCEL_ALL, {}

    buy_matches = list(_BUY_PATTERN.finditer(normalized))
    sell_matches = list(_SELL_PATTERN.finditer(normalized))
    if bool(buy_matches) == bool(sell_matches):
        raise ClarificationRequired("side")
    side_matches = buy_matches if buy_matches else sell_matches
    if len(side_matches) != 1:
        raise ClarificationRequired("side")
    side = OrderSide.BUY if buy_matches else OrderSide.SELL

    quantities = list(_QUANTITY_PATTERN.finditer(normalized))
    if len(quantities) != 1:
        raise ClarificationRequired("quantity")
    quantity_text = quantities[0].group(1).replace(",", "")
    quantity = Decimal(quantity_text)

    won_price_matches = list(_WON_PRICE_PATTERN.finditer(normalized))
    limit_price_matches = list(_LIMIT_PRICE_PATTERN.finditer(normalized))
    price_matches = won_price_matches if won_price_matches else limit_price_matches

    # A six-digit price (for example 700000원) is not a stock code.  Exclude
    # every numeric span already consumed as quantity or price before resolving
    # an instrument code.
    occupied_spans = [quantities[0].span(), *(match.span() for match in price_matches)]
    code_matches = [
        match
        for match in _CODE_PATTERN.finditer(normalized)
        if not any(
            match.start() < occupied_end and match.end() > occupied_start
            for occupied_start, occupied_end in occupied_spans
        )
    ]
    codes = list(dict.fromkeys(match.group(1).upper() for match in code_matches))
    if len(codes) > 1:
        raise ClarificationRequired("instrument")
    symbol = codes[0] if codes else _natural_name(normalized, quantities[0])

    market_matches = list(_MARKET_PATTERN.finditer(normalized))
    limit_marker_matches = list(_LIMIT_MARKER_PATTERN.finditer(normalized))
    if (
        len(market_matches) > 1
        or len(limit_marker_matches) > 1
        or len(price_matches) > 1
    ):
        raise ClarificationRequired("order_type")
    market = bool(market_matches)
    if market and (limit_marker_matches or price_matches):
        raise ClarificationRequired("order_type")
    if market:
        order_type = OrderType.MARKET
        limit_price = None
        selected_price_match = None
    elif len(price_matches) == 1:
        order_type = OrderType.LIMIT
        limit_price = Decimal(price_matches[0].group(1).replace(",", ""))
        selected_price_match = price_matches[0]
    elif limit_marker_matches:
        raise ClarificationRequired("limit_price")
    else:
        # An otherwise complete PAPER phrase without price/type language uses
        # the deterministic MARKET default. Structured order objects still
        # require an explicit ``order_type`` in ``PaperOrderInput``.
        order_type = OrderType.MARKET
        limit_price = None
        selected_price_match = None

    consumed_spans = [quantities[0].span(), side_matches[0].span()]
    if codes:
        consumed_spans.append(code_matches[0].span())
    else:
        symbol_start = normalized.find(symbol)
        if symbol_start < 0:
            raise ClarificationRequired("instrument")
        consumed_spans.append((symbol_start, symbol_start + len(symbol)))
    if market_matches:
        consumed_spans.append(market_matches[0].span())
    elif selected_price_match is not None:
        consumed_spans.append(selected_price_match.span())
        consumed_spans.extend(
            match.span()
            for match in _LIMIT_MARKER_PATTERN.finditer(normalized)
            if not (
                selected_price_match.start() <= match.start()
                and match.end() <= selected_price_match.end()
            )
        )

    remaining = list(normalized)
    for start, end in consumed_spans:
        remaining[start:end] = " " * (end - start)
    residual = " ".join("".join(remaining).strip(" ,.!").split())
    if "?" in residual or not _ALLOWED_ORDER_RESIDUAL.fullmatch(residual):
        raise ClarificationRequired("unsupported_text")

    order = PaperOrderInput(
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
    )
    return DirectiveAction.PLACE_ORDER, _order_payload(order)


def _order_payload(order: PaperOrderInput) -> dict[str, Any]:
    return {
        "instrument_id": str(order.instrument_id) if order.instrument_id else None,
        "symbol": order.symbol,
        "side": order.side.value,
        "quantity": _decimal_text(order.quantity),
        "order_type": order.order_type.value,
        "time_in_force": order.time_in_force,
        "limit_price": (
            _decimal_text(order.limit_price) if order.limit_price is not None else None
        ),
    }


def _idempotency_key(value: str | None) -> str:
    key = (value or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="idempotency_key_required")
    if (
        len(key) < 8
        or len(key) > 128
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", key)
    ):
        raise HTTPException(status_code=400, detail="idempotency_key_invalid")
    return key


def _instruction_ref(
    *,
    subject: str,
    fund_id: str,
    book_id: str,
    action: str,
    key: str,
    payload_hash: str,
) -> str:
    identity = canonical_json(
        {
            "sub": subject,
            "fund_id": fund_id,
            "book_id": book_id,
            "action": action,
            "idempotency_key": key,
            "payload_sha256": payload_hash,
        }
    )
    return str(uuid5(NAMESPACE_URL, identity))


def _validated_response(
    raw: dict[str, Any],
    *,
    directive_id: UUID | str | None = None,
    fund_id: UUID | str | None = None,
    book_id: UUID | str | None = None,
    action: DirectiveAction | str | None = None,
    instruction_ref: str | None = None,
    idempotency_key: str | None = None,
    expected_payload_sha256: str | None = None,
) -> UserDirectiveResponse:
    try:
        response = UserDirectiveResponse.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=502, detail="trading_api_invalid_response"
        ) from exc
    expected = {
        "directive_id": str(directive_id) if directive_id is not None else None,
        "fund_id": str(fund_id) if fund_id is not None else None,
        "book_id": str(book_id) if book_id is not None else None,
        "action": (action.value if isinstance(action, DirectiveAction) else action),
        "instruction_ref": instruction_ref,
        "idempotency_key": idempotency_key,
        "payload_sha256": expected_payload_sha256,
    }
    actual = {
        "directive_id": str(response.directive_id),
        "fund_id": str(response.fund_id),
        "book_id": str(response.book_id),
        "action": response.action.value,
        "instruction_ref": response.instruction_ref,
        "idempotency_key": response.idempotency_key,
        "payload_sha256": response.payload_sha256,
    }
    if any(
        expected_value is not None and actual[field] != expected_value
        for field, expected_value in expected.items()
    ):
        # Never reflect the mismatched authority identifiers to the browser.
        raise HTTPException(status_code=502, detail="trading_api_invalid_response")
    return response


def _submit(
    *,
    subject: str | None,
    fund_id: UUID,
    book_id: UUID,
    action: DirectiveAction,
    payload: dict[str, Any],
    idempotency_header: str | None,
) -> UserDirectiveResponse:
    access = require_trading_book_access(subject, str(fund_id), str(book_id))
    key = _idempotency_key(idempotency_header)
    if action == DirectiveAction.PLACE_ORDER:
        normalized_order = PaperOrderInput.model_validate(payload)
        resolved = resolve_active_trading_instrument(
            normalized_order.symbol,
            str(normalized_order.instrument_id)
            if normalized_order.instrument_id is not None
            else None,
        )
        normalized_order = normalized_order.model_copy(
            update={
                "instrument_id": UUID(resolved["instrument_id"]),
                "symbol": resolved["symbol"],
            }
        )
        normalized_payload = _order_payload(normalized_order)
    else:
        normalized_payload = {}
    payload_hash = payload_sha256(normalized_payload)
    instruction_ref = _instruction_ref(
        subject=access["user_id"],
        fund_id=access["fund_id"],
        book_id=access["book_id"],
        action=action.value,
        key=key,
        payload_hash=payload_hash,
    )
    body = {
        "fund_id": access["fund_id"],
        "book_id": access["book_id"],
        "action": action.value,
        "instruction_ref": instruction_ref,
        "idempotency_key": key,
        "payload": normalized_payload,
    }
    try:
        proof = issue_trading_directive_proof(
            subject=access["user_id"],
            fund_id=access["fund_id"],
            book_id=access["book_id"],
            action=action.value,
            instruction_ref=instruction_ref,
            idempotency_key=key,
            payload_hash=payload_hash,
        )
        raw = submit_user_directive(body=body, proof=proof, idempotency_key=key)
    except TradingProofConfigurationError as exc:
        raise HTTPException(
            status_code=503, detail="trading_service_auth_unavailable"
        ) from exc
    except TradingProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return _validated_response(
        raw,
        fund_id=access["fund_id"],
        book_id=access["book_id"],
        action=action,
        instruction_ref=instruction_ref,
        idempotency_key=key,
        expected_payload_sha256=payload_hash,
    )


def submit_verified_paper_directive(
    *,
    subject: str,
    fund_id: str,
    book_id: str,
    action: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> UserDirectiveResponse:
    """Submit a deterministically verified Hermes result through the same gate.

    This is deliberately not an HTTP route. The trusted order orchestrator is
    the only caller; `_submit` still rechecks current user/Fund/Book authority,
    resolves the instrument, and mints a fresh 20-second payload-bound proof.
    """

    directive_action = DirectiveAction(action)
    normalized_payload = dict(payload)
    if directive_action is DirectiveAction.PLACE_ORDER:
        instrument_mention = normalized_payload.pop("instrument_mention", None)
        if instrument_mention is None or "symbol" in normalized_payload:
            raise HTTPException(status_code=422, detail="paper_order_payload_invalid")
        normalized_payload["symbol"] = instrument_mention
    elif normalized_payload:
        raise HTTPException(status_code=422, detail="paper_order_payload_invalid")
    return _submit(
        subject=subject,
        fund_id=UUID(str(fund_id)),
        book_id=UUID(str(book_id)),
        action=directive_action,
        payload=normalized_payload,
        idempotency_header=idempotency_key,
    )


def read_verified_paper_directive_status(
    *,
    subject: str,
    fund_id: str,
    book_id: str,
    directive_id: str,
) -> UserDirectiveResponse:
    """Read a previously linked directive with a fresh scoped read proof."""

    return _status(
        directive_id=UUID(str(directive_id)),
        fund_id=UUID(str(fund_id)),
        book_id=UUID(str(book_id)),
        subject=subject,
    )


def read_paper_directive_status_for_admitted_authority(
    *,
    user_id: str,
    fund_id: str,
    book_id: str,
    directive_id: str,
) -> UserDirectiveResponse:
    """Read status for a system workflow whose authority was already admitted.

    This is not an HTTP route and deliberately does not repeat interactive
    portfolio authorization.  Its caller must first match all four identifiers
    against the immutable user-order admission and the conditional execution
    event.  The Trading read still uses the existing short-lived, scoped proof
    and the same response validation as every user-facing status lookup.
    """

    access = {
        "user_id": str(UUID(str(user_id))),
        "fund_id": str(UUID(str(fund_id))),
        "book_id": str(UUID(str(book_id))),
    }
    return _status_for_access(
        directive_id=UUID(str(directive_id)),
        access=access,
    )


@router.post("/ui/paper-orders", response_model=UserDirectiveResponse, status_code=202)
@router.post(
    "/trading/agent/order",
    response_model=UserDirectiveResponse,
    status_code=202,
    include_in_schema=True,
)
def place_paper_order(
    request: PaperOrderRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    subject: str | None = Depends(current_user),
) -> UserDirectiveResponse:
    try:
        action, payload = (
            parse_user_order_query(request.query)
            if request.query is not None
            else (DirectiveAction.PLACE_ORDER, _order_payload(request.order))
        )
    except ClarificationRequired as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "paper_order_clarification_required",
                "field": exc.reason,
            },
        ) from exc
    return _submit(
        subject=subject,
        fund_id=request.fund_id,
        book_id=request.book_id,
        action=action,
        payload=payload,
        idempotency_header=idempotency_key,
    )


@router.post(
    "/ui/paper-orders/sell-all",
    response_model=UserDirectiveResponse,
    status_code=202,
)
def sell_all_paper_positions(
    request: PaperAggregateRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    subject: str | None = Depends(current_user),
) -> UserDirectiveResponse:
    return _submit(
        subject=subject,
        fund_id=request.fund_id,
        book_id=request.book_id,
        action=DirectiveAction.SELL_ALL,
        payload={},
        idempotency_header=idempotency_key,
    )


@router.post(
    "/ui/paper-orders/cancel-all",
    response_model=UserDirectiveResponse,
    status_code=202,
)
def cancel_all_paper_orders(
    request: PaperAggregateRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    subject: str | None = Depends(current_user),
) -> UserDirectiveResponse:
    return _submit(
        subject=subject,
        fund_id=request.fund_id,
        book_id=request.book_id,
        action=DirectiveAction.CANCEL_ALL,
        payload={},
        idempotency_header=idempotency_key,
    )


def _status(
    *, directive_id: UUID, fund_id: UUID, book_id: UUID, subject: str | None
) -> UserDirectiveResponse:
    access = require_trading_book_access(subject, str(fund_id), str(book_id))
    return _status_for_access(directive_id=directive_id, access=access)


def _status_for_access(
    *, directive_id: UUID, access: dict[str, str]
) -> UserDirectiveResponse:
    payload_hash = payload_sha256({})
    status_key = f"status:{directive_id}"
    try:
        proof = issue_trading_directive_proof(
            subject=access["user_id"],
            fund_id=access["fund_id"],
            book_id=access["book_id"],
            action="GET_STATUS",
            instruction_ref=str(directive_id),
            idempotency_key=status_key,
            payload_hash=payload_hash,
            scope=TRADING_DIRECTIVE_READ_SCOPE,
        )
        raw = get_user_directive(directive_id=str(directive_id), proof=proof)
    except TradingProofConfigurationError as exc:
        raise HTTPException(
            status_code=503, detail="trading_service_auth_unavailable"
        ) from exc
    except TradingProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return _validated_response(
        raw,
        directive_id=directive_id,
        fund_id=access["fund_id"],
        book_id=access["book_id"],
    )


@router.get("/ui/paper-orders/{directive_id}", response_model=UserDirectiveResponse)
@router.get(
    "/ui/paper-orders/{directive_id}/status",
    response_model=UserDirectiveResponse,
)
def paper_order_status(
    directive_id: UUID,
    fund_id: Annotated[UUID, Query()],
    book_id: Annotated[UUID, Query()],
    subject: str | None = Depends(current_user),
) -> UserDirectiveResponse:
    return _status(
        directive_id=directive_id,
        fund_id=fund_id,
        book_id=book_id,
        subject=subject,
    )


def _workflow_state_from_directive(response: UserDirectiveResponse) -> str:
    # Accounting acknowledgment is the completion boundary for this workflow.
    # Fail closed even if an inconsistent/stale Trading response also labels
    # the directive COMPLETED: a fill alone is not user-visible completion.
    if response.error_code == "TRADING_FILL_ACCOUNTING_PENDING":
        return "ACCOUNTING_PENDING"
    if response.state is DirectiveState.COMPLETED:
        return "COMPLETED"
    if response.state is DirectiveState.FAILED:
        return "FAILED"
    if response.state is DirectiveState.UNKNOWN:
        return "UNKNOWN"
    return "IN_PROGRESS"


def _conditional_rule_outcomes(record: Any) -> list[ConditionalRuleOutcome] | None:
    """Report what became of the rules this request created, if any.

    Rules carry the request's ``client_request_id`` verbatim for a single
    action, and a derived ``conditional-set:<digest>:<n>`` for a batch, so the
    link needs no new column.  A lookup failure returns ``None`` rather than
    raising: the request status must stay readable even when the rule store is
    briefly unavailable.
    """

    base = str(getattr(record, "client_request_id", "") or "")
    if not base:
        return None
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:48]
    prefix = f"conditional-set:{digest}:"
    try:
        rules = conditional_rule_repository().list_for_user(record.user_id)
    except ConditionalRuleUnavailable:
        return None
    except Exception:  # pragma: no cover - the status must stay readable
        logger.exception("conditional rule outcome lookup failed request=%s", base)
        return None
    linked = [
        rule
        for rule in rules
        if rule.client_request_id == base or rule.client_request_id.startswith(prefix)
    ]
    if not linked:
        return None
    return [
        ConditionalRuleOutcome(
            rule_id=UUID(rule.rule_id),
            state=str(getattr(rule.state, "value", rule.state)),
            last_execution_state=rule.last_execution_state,
            last_guard_code=rule.last_guard_code,
            last_error_code=rule.last_error_code,
            status_message=conditional_status_message(
                last_error_code=rule.last_error_code,
                last_guard_code=rule.last_guard_code,
            ),
        )
        for rule in linked
    ]


@router.get(
    "/ui/paper-order-requests/{order_request_id}",
    response_model=PaperOrderWorkflowStatusResponse,
)
def paper_order_workflow_status(
    order_request_id: UUID,
    subject: str | None = Depends(current_user),
) -> PaperOrderWorkflowStatusResponse:
    """Expose the admitted request through final accounting-aware state."""

    try:
        repository = user_order_repository()
        record = repository.get(str(order_request_id))
    except UserOrderWorkflowUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="paper_order_workflow_unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="paper_order_request_not_found")
    access = require_trading_book_access(
        subject,
        record.fund_id,
        record.book_id,
    )
    if access["user_id"] != record.user_id:
        raise HTTPException(status_code=404, detail="paper_order_request_not_found")

    directive: UserDirectiveResponse | None = None
    correlation: dict[str, Any] | None = None
    if record.state == "UNKNOWN" and not record.directive_id:
        try:
            record = recover_committed_directive(repository, record)
        except UserOrderWorkflowError as exc:
            raise HTTPException(
                status_code=503, detail="paper_order_workflow_unavailable"
            ) from exc
    if record.directive_id:
        directive = read_verified_paper_directive_status(
            subject=record.user_id,
            fund_id=record.fund_id,
            book_id=record.book_id,
            directive_id=record.directive_id,
        )
        state = _workflow_state_from_directive(directive)
        if state != record.state:
            try:
                correlation = directive_execution_event_payload(record, directive)
                record = repository.mark_outcome(
                    record.order_request_id,
                    state=state,
                    directive_id=str(directive.directive_id),
                    error_code=directive.error_code,
                    error_message=directive.error_message,
                    event_type="BROKER_EXECUTION_SNAPSHOT",
                    event_payload=correlation,
                )
            except UserOrderWorkflowUnavailable as exc:
                raise HTTPException(
                    status_code=503, detail="paper_order_workflow_unavailable"
                ) from exc
        if correlation is None:
            correlation = directive_execution_event_payload(record, directive)

    return PaperOrderWorkflowStatusResponse(
        conditional_rules=_conditional_rule_outcomes(record),
        order_request_id=UUID(record.order_request_id),
        client_request_id=record.client_request_id,
        request_source=(
            "DISCORD"
            if record.client_request_id.startswith("discord:")
            else "WEB_OR_API"
        ),
        state=record.state,
        action=DirectiveAction(record.action) if record.action else None,
        ceo_root_task_id=record.ceo_root_task_id,
        trading_task_id=record.trading_task_id,
        clarification_code=record.clarification_code,
        error_code=record.error_code,
        error_message=record.error_message,
        directive=directive,
        correlation=correlation,
    )


__all__ = [
    "ClarificationRequired",
    "DirectiveAction",
    "DirectiveState",
    "OrderSide",
    "OrderType",
    "PaperAggregateRequest",
    "PaperOrderInput",
    "PaperOrderRequest",
    "PaperOrderWorkflowStatusResponse",
    "USER_DIRECTIVE_MODE",
    "USER_DIRECTIVE_PRIORITY_CLASS",
    "USER_DIRECTIVES_PATH",
    "USER_DIRECTIVE_STATUS_PATH",
    "UserDirectiveResponse",
    "parse_user_order_query",
    "read_verified_paper_directive_status",
    "router",
    "submit_verified_paper_directive",
]
