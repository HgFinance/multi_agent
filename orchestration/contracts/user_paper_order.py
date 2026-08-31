"""Strict contracts for a non-authoritative Hermes PAPER-order interpretation.

Hermes may structure a user's words, but it never owns the user's identity,
fund/book authority, an instrument identifier, or an OMS capability.  A
candidate from this module becomes useful only after
``orchestration.user_order_language.verify_order_candidate`` independently
checks it against the exact original text.

The verified directive is still *not* an authorization.  The authenticated
BFF must resolve the instrument, re-check fund/book membership, mint a fresh
payload-bound service proof, and submit the resulting PAPER directive.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

INTERPRETATION_SCHEMA_VERSION = "user-paper-order-interpretation.v1"
PAPER_MODE = "PAPER"


class CandidateDecision(StrEnum):
    """The only decisions that an interpretation-only Hermes may propose."""

    EXECUTE = "EXECUTE"
    CLARIFY = "CLARIFY"
    NOT_ORDER = "NOT_ORDER"


class DirectiveAction(StrEnum):
    PLACE_ORDER = "PLACE_ORDER"
    PLACE_BASKET = "PLACE_BASKET"
    SELL_ALL = "SELL_ALL"
    # Liquidate the whole position of ONE named instrument.  Like SELL_ALL the
    # account sizes it, so the candidate carries no quantity - only the
    # unresolved instrument mention.
    SELL_POSITION = "SELL_POSITION"
    CANCEL_ALL = "CANCEL_ALL"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class EvidenceField(StrEnum):
    ACTION = "ACTION"
    AGGREGATE_SCOPE = "AGGREGATE_SCOPE"
    INSTRUMENT = "INSTRUMENT"
    SIDE = "SIDE"
    QUANTITY = "QUANTITY"
    ORDER_TYPE = "ORDER_TYPE"
    LIMIT_PRICE = "LIMIT_PRICE"
    BASKET_INSTRUMENTS = "BASKET_INSTRUMENTS"
    NOTIONAL = "NOTIONAL"


class OrderReasonCode(StrEnum):
    INVALID_CANDIDATE_SCHEMA = "INVALID_CANDIDATE_SCHEMA"
    RAW_TEXT_HASH_MISMATCH = "RAW_TEXT_HASH_MISMATCH"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    EVIDENCE_SPAN_INVALID = "EVIDENCE_SPAN_INVALID"
    EVIDENCE_TEXT_MISMATCH = "EVIDENCE_TEXT_MISMATCH"
    EVIDENCE_FIELD_MISMATCH = "EVIDENCE_FIELD_MISMATCH"
    QUESTION_OR_ADVICE = "QUESTION_OR_ADVICE"
    NEGATED_OR_PROHIBITED = "NEGATED_OR_PROHIBITED"
    CONDITIONAL_OR_HYPOTHETICAL = "CONDITIONAL_OR_HYPOTHETICAL"
    READ_ONLY_REQUEST = "READ_ONLY_REQUEST"
    EXAMPLE_OR_QUOTED_TEXT = "EXAMPLE_OR_QUOTED_TEXT"
    LIVE_MODE_FORBIDDEN = "LIVE_MODE_FORBIDDEN"
    MULTIPLE_COMMANDS = "MULTIPLE_COMMANDS"
    APPROXIMATE_VALUE = "APPROXIMATE_VALUE"
    NOTIONAL_UNSUPPORTED = "NOTIONAL_UNSUPPORTED"
    NO_ORDER_COMMAND = "NO_ORDER_COMMAND"
    MISSING_OR_CONFLICTING_ACTION = "MISSING_OR_CONFLICTING_ACTION"
    MISSING_OR_CONFLICTING_SIDE = "MISSING_OR_CONFLICTING_SIDE"
    MISSING_OR_CONFLICTING_INSTRUMENT = "MISSING_OR_CONFLICTING_INSTRUMENT"
    MISSING_OR_CONFLICTING_QUANTITY = "MISSING_OR_CONFLICTING_QUANTITY"
    MISSING_OR_CONFLICTING_NOTIONAL = "MISSING_OR_CONFLICTING_NOTIONAL"
    INVALID_NUMBER = "INVALID_NUMBER"
    MISSING_OR_CONFLICTING_ORDER_TYPE = "MISSING_OR_CONFLICTING_ORDER_TYPE"
    MISSING_LIMIT_PRICE = "MISSING_LIMIT_PRICE"
    CONFLICTING_MARKET_AND_PRICE = "CONFLICTING_MARKET_AND_PRICE"
    UNSUPPORTED_DELAY_EXPRESSION = "UNSUPPORTED_DELAY_EXPRESSION"
    UNSUPPORTED_TEXT = "UNSUPPORTED_TEXT"
    CANDIDATE_MISMATCH = "CANDIDATE_MISMATCH"
    HERMES_DID_NOT_PROPOSE_EXECUTION = "HERMES_DID_NOT_PROPOSE_EXECUTION"


class TextEvidence(BaseModel):
    """One exact, code-point-indexed substring of the user's original text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: EvidenceField
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=500)
    normalized: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Required for execution evidence. INSTRUMENT must exactly equal "
            "instrument_mention; enum and numeric fields use canonical values."
        ),
    )

    @model_validator(mode="after")
    def _ordered_span(self) -> "TextEvidence":
        if self.end <= self.start:
            raise ValueError("evidence end must be greater than start")
        return self


class HermesOrderCandidate(BaseModel):
    """Strict, explicitly non-binding structure emitted by Trading Hermes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["user-paper-order-interpretation.v1"] = (
        INTERPRETATION_SCHEMA_VERSION
    )
    mode: Literal["PAPER"] = PAPER_MODE
    binding: Literal[False] = False
    raw_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: CandidateDecision
    action: DirectiveAction | None = None
    instrument_mention: str | None = Field(default=None, min_length=1, max_length=80)
    basket_instrument_mentions: tuple[str, ...] = Field(
        default=(), max_length=20
    )
    basket_quantities: tuple[str, ...] = Field(default=(), max_length=20)
    basket_notionals_krw: tuple[str, ...] = Field(default=(), max_length=20)
    side: OrderSide | None = None
    quantity: str | None = Field(default=None, pattern=r"^[1-9]\d*$", max_length=30)
    notional_krw: str | None = Field(
        default=None, pattern=r"^[1-9]\d*$", max_length=30
    )
    order_type: OrderType | None = None
    limit_price: str | None = Field(
        default=None, pattern=r"^[1-9]\d*$", max_length=30
    )
    evidence: tuple[TextEvidence, ...] = ()
    reason_codes: tuple[OrderReasonCode, ...] = ()

    @field_validator("instrument_mention")
    @classmethod
    def _clean_instrument_mention(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("instrument_mention must be an exact printable substring")
        return value

    @field_validator("basket_instrument_mentions")
    @classmethod
    def _clean_basket_instrument_mentions(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        for mention in value:
            if (
                not isinstance(mention, str)
                or not mention
                or mention != mention.strip()
                or len(mention) > 80
                or any(ord(character) < 32 for character in mention)
            ):
                raise ValueError("basket instrument mention is invalid")
        return value

    @field_validator("basket_quantities")
    @classmethod
    def _clean_basket_quantities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for quantity in value:
            if not re.fullmatch(r"[1-9]\d*", quantity):
                raise ValueError("basket quantity is invalid")
        return value

    @field_validator("basket_notionals_krw")
    @classmethod
    def _clean_basket_notionals_krw(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        for notional in value:
            if not re.fullmatch(r"[1-9]\d*", notional):
                raise ValueError("basket notional is invalid")
        return value

    @model_validator(mode="after")
    def _decision_shape(self) -> "HermesOrderCandidate":
        execution_fields = (
            self.action,
            self.instrument_mention,
            self.basket_instrument_mentions,
            self.basket_quantities,
            self.basket_notionals_krw,
            self.side,
            self.quantity,
            self.notional_krw,
            self.order_type,
            self.limit_price,
        )
        if self.decision is not CandidateDecision.EXECUTE:
            if (
                any(value is not None for value in execution_fields if value != ())
                or self.basket_instrument_mentions
                or self.basket_quantities
                or self.basket_notionals_krw
                or self.evidence
            ):
                raise ValueError("non-execution candidates cannot carry execution fields")
            if not self.reason_codes:
                raise ValueError("non-execution candidates require a reason code")
            return self

        if self.reason_codes:
            raise ValueError("execution candidates cannot carry reason codes")
        if self.action is None:
            raise ValueError("execution candidates require action")
        if self.action is DirectiveAction.PLACE_ORDER:
            if (
                self.instrument_mention is None
                or self.basket_instrument_mentions
                or self.basket_quantities
                or self.basket_notionals_krw
                or self.side is None
                or self.quantity is None
            ):
                raise ValueError("PLACE_ORDER candidate is incomplete")
            # A complete natural-language PAPER order may omit the order-type
            # marker. The verifier owns the managed default (MARKET), while
            # an omitted type next to a price remains invalid rather than
            # silently turning a limit intent into a market order.
            if self.order_type is None and self.limit_price is not None:
                raise ValueError(
                    "PLACE_ORDER candidate with limit_price requires order_type"
                )
            if self.order_type is OrderType.MARKET and self.limit_price is not None:
                raise ValueError("MARKET candidate cannot carry limit_price")
            if self.order_type is OrderType.LIMIT and self.limit_price is None:
                raise ValueError("LIMIT candidate requires limit_price")
            if self.notional_krw is not None:
                raise ValueError("PLACE_ORDER candidate cannot carry notional_krw")
        elif self.action is DirectiveAction.PLACE_BASKET:
            if (
                self.instrument_mention is not None
                or len(self.basket_instrument_mentions) < 2
                or self.quantity is not None
                or self.order_type is not OrderType.MARKET
                or self.limit_price is not None
            ):
                raise ValueError("PLACE_BASKET candidate is incomplete or unsupported")
            if self.notional_krw is not None:
                if (
                    self.side is None
                    or self.basket_quantities
                    or self.basket_notionals_krw
                ):
                    raise ValueError("notional basket candidate is invalid")
            elif self.basket_notionals_krw:
                if (
                    self.side is None
                    or self.basket_quantities
                    or len(self.basket_notionals_krw)
                    != len(self.basket_instrument_mentions)
                ):
                    raise ValueError("member-notional basket candidate is invalid")
            elif (
                self.side is None
                or len(self.basket_quantities) != len(self.basket_instrument_mentions)
            ):
                raise ValueError("quantity basket candidate is incomplete")
        elif self.action is DirectiveAction.SELL_POSITION:
            # The instrument is the only order field a position liquidation
            # may carry; the holding decides the quantity.
            if self.instrument_mention is None or any(
                value is not None
                for value in (
                    self.side,
                    self.quantity,
                    self.notional_krw,
                    self.order_type,
                    self.limit_price,
                )
            ) or (
                self.basket_instrument_mentions
                or self.basket_quantities
                or self.basket_notionals_krw
            ):
                raise ValueError("SELL_POSITION candidate is incomplete or unsupported")
        elif any(
            value is not None
            for value in (
                self.instrument_mention,
                self.side,
                self.quantity,
                self.notional_krw,
                self.order_type,
                self.limit_price,
            )
        ) or (
            self.basket_instrument_mentions
            or self.basket_quantities
            or self.basket_notionals_krw
        ):
            raise ValueError("aggregate candidate cannot carry order fields")
        if not self.evidence:
            raise ValueError("execution candidates require evidence")
        return self


class CanonicalPlaceOrderPayload(BaseModel):
    """Language-layer payload; ``instrument_mention`` is not a trading symbol."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_mention: str = Field(min_length=1, max_length=80)
    side: OrderSide
    quantity: str = Field(pattern=r"^[1-9]\d*$", max_length=30)
    order_type: OrderType
    time_in_force: Literal["DAY"] = "DAY"
    limit_price: str | None = Field(
        default=None, pattern=r"^[1-9]\d*$", max_length=30
    )

    @model_validator(mode="after")
    def _price_matches_type(self) -> "CanonicalPlaceOrderPayload":
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("MARKET payload cannot carry limit_price")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT payload requires limit_price")
        return self


class CanonicalSellPositionPayload(BaseModel):
    """Liquidate one unresolved instrument mention; the holding sets the size."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_mention: str = Field(min_length=1, max_length=80)
    side: Literal[OrderSide.SELL] = OrderSide.SELL
    order_type: Literal[OrderType.MARKET] = OrderType.MARKET
    time_in_force: Literal["DAY"] = "DAY"
    reduce_only: Literal[True] = True


class CanonicalBasketOrderItem(BaseModel):
    """One unresolved member of a strict PAPER market basket."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_mention: str = Field(min_length=1, max_length=80)
    notional_krw: str | None = Field(
        default=None, pattern=r"^[1-9]\d*$", max_length=30
    )
    quantity: str | None = Field(
        default=None, pattern=r"^[1-9]\d*$", max_length=30
    )
    side: OrderSide
    order_type: Literal[OrderType.MARKET] = OrderType.MARKET
    time_in_force: Literal["DAY"] = "DAY"

    @model_validator(mode="after")
    def _sizing_policy(self) -> "CanonicalBasketOrderItem":
        if (self.notional_krw is None) == (self.quantity is None):
            raise ValueError("basket member requires exactly one sizing policy")
        # A KRW-sized SELL is bounded by the live bid and stays reduce_only
        # downstream, so it can undershoot but never oversell.
        return self


class CanonicalPlaceBasketPayload(BaseModel):
    """A list of unresolved catalog mentions and explicit sizing policies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    orders: tuple[CanonicalBasketOrderItem, ...] = Field(min_length=2, max_length=20)

    @model_validator(mode="after")
    def _unique_mentions(self) -> "CanonicalPlaceBasketPayload":
        mentions = [order.instrument_mention for order in self.orders]
        if len(set(mentions)) != len(mentions):
            raise ValueError("basket cannot contain duplicate instrument mentions")
        if len({order.side for order in self.orders}) != 1:
            raise ValueError("basket members must have one shared side")
        return self


class VerifiedPaperDirective(BaseModel):
    """A verified interpretation that still requires authenticated admission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal[CandidateDecision.EXECUTE] = CandidateDecision.EXECUTE
    mode: Literal["PAPER"] = PAPER_MODE
    binding: Literal[False] = False
    requires_authenticated_admission: Literal[True] = True
    raw_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: DirectiveAction
    payload: (
        CanonicalPlaceOrderPayload
        | CanonicalPlaceBasketPayload
        | CanonicalSellPositionPayload
        | None
    ) = None
    evidence: tuple[TextEvidence, ...]

    @model_validator(mode="after")
    def _payload_matches_action(self) -> "VerifiedPaperDirective":
        if self.action is DirectiveAction.PLACE_ORDER and self.payload is None:
            raise ValueError("PLACE_ORDER verified directive requires payload")
        if self.action is DirectiveAction.PLACE_BASKET and self.payload is None:
            raise ValueError("PLACE_BASKET verified directive requires payload")
        if self.action is DirectiveAction.SELL_POSITION and not isinstance(
            self.payload, CanonicalSellPositionPayload
        ):
            raise ValueError("SELL_POSITION verified directive requires its payload")
        if self.action not in {
            DirectiveAction.PLACE_ORDER,
            DirectiveAction.PLACE_BASKET,
            DirectiveAction.SELL_POSITION,
        } and self.payload is not None:
            raise ValueError("aggregate verified directive must not carry payload")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        """Return the unresolved payload expected by the authenticated caller."""

        if self.payload is None:
            return {}
        return self.payload.model_dump(mode="json", exclude_none=False)


class OrderClarification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal[CandidateDecision.CLARIFY] = CandidateDecision.CLARIFY
    mode: Literal["PAPER"] = PAPER_MODE
    binding: Literal[False] = False
    raw_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_codes: tuple[OrderReasonCode, ...] = Field(min_length=1)


class NotOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal[CandidateDecision.NOT_ORDER] = CandidateDecision.NOT_ORDER
    mode: Literal["PAPER"] = PAPER_MODE
    binding: Literal[False] = False
    raw_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_codes: tuple[OrderReasonCode, ...] = Field(min_length=1)


OrderLanguageResult = VerifiedPaperDirective | OrderClarification | NotOrder


__all__ = [
    "CandidateDecision",
    "CanonicalBasketOrderItem",
    "CanonicalPlaceBasketPayload",
    "CanonicalPlaceOrderPayload",
    "CanonicalSellPositionPayload",
    "DirectiveAction",
    "EvidenceField",
    "HermesOrderCandidate",
    "INTERPRETATION_SCHEMA_VERSION",
    "NotOrder",
    "OrderClarification",
    "OrderLanguageResult",
    "OrderReasonCode",
    "OrderSide",
    "OrderType",
    "PAPER_MODE",
    "TextEvidence",
    "VerifiedPaperDirective",
]
