"""Internal-only submission route for durable conditional PAPER triggers."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Header

from directive_routes import required_directive_service
from directives.service import DirectiveServiceError
from internal_service_auth import (
    CONDITIONAL_RULE_EXECUTOR_POLICY,
    InternalServiceAuthError,
    authenticate_internal_service,
)
from rules.admission import (
    ConditionalRuleAdmissionError,
    admission_repository_from_env,
)
from rules.context import context_repository_from_env
from directives.repository import DirectiveRepositoryError
from directives.market_data import TrustedQuote
from orchestration.conditional_rules import SizingType


CONDITIONAL_LIMIT_QUOTE_MAX_AGE_SECONDS = 600.0


router = APIRouter(
    prefix="/trading/v1/conditional-rule-executions",
    tags=["conditional-paper-rules"],
)


def _authenticate(authorization: str | None) -> None:
    try:
        authenticate_internal_service(
            authorization,
            policy=CONDITIONAL_RULE_EXECUTOR_POLICY,
        )
    except InternalServiceAuthError as exc:
        raise DirectiveServiceError(exc.code, str(exc), exc.status_code) from exc


def _current_context(authority, repository) -> dict:
    """Read one book-locked canonical portfolio snapshot for evaluation."""

    spec = authority.spec
    with repository.book_guard(spec.authority.fund_id, spec.authority.book_id):
        instrument = repository.resolve_instrument(
            spec.authority.fund_id,
            spec.authority.book_id,
            spec.instrument_id,
            spec.symbol,
        )
        position_rows = repository.positions(
            spec.authority.fund_id, spec.authority.book_id
        )
        position_quantity = next(
            (
                quantity
                for held, quantity in position_rows
                if held.instrument_id == spec.instrument_id
            ),
            Decimal("0"),
        )
        holdings = [
            {
                "instrument_id": str(held.instrument_id),
                "symbol": held.symbol,
                "quantity": str(quantity),
                "average_cost": str(
                    repository.average_cost(
                        spec.authority.fund_id,
                        spec.authority.book_id,
                        held.instrument_id,
                    )
                ),
            }
            for held, quantity in position_rows
        ]
        now = datetime.now(timezone.utc)
        market_open = True
        market_session_available = True
        try:
            market_session_close = repository.market_session_close(now=now)
        except DirectiveRepositoryError as exc:
            if exc.code not in {
                "TRADING_MARKET_SESSION_CLOSED",
                "TRADING_MARKET_SESSION_UNAVAILABLE",
            }:
                raise
            if exc.code == "TRADING_MARKET_SESSION_UNAVAILABLE":
                market_session_available = False
            market_open = False
            market_session_close = None
        sellable_quantity = repository.sellable_quantity(
            spec.authority.fund_id,
            spec.authority.book_id,
            spec.instrument_id,
        )
        average_cost = repository.average_cost(
            spec.authority.fund_id,
            spec.authority.book_id,
            spec.instrument_id,
        )
        available_cash = repository.available_cash(
            spec.authority.fund_id,
            spec.authority.book_id,
            instrument.currency,
        )
    return {
        "rule_id": str(authority.rule_id),
        "rule_version": authority.rule_version,
        "spec_sha256": authority.spec_sha256,
        "rule_state": authority.rule_state.value,
        "membership_active": authority.membership_active,
        "fund_active": authority.fund_active,
        "book_active": authority.book_active,
        "market_session_available": market_session_available,
        "market_open": market_open,
        "market_session_close": (
            market_session_close.isoformat() if market_session_close else None
        ),
        "instrument": {
            "instrument_id": str(instrument.instrument_id),
            "symbol": instrument.symbol,
            "lot_size": str(instrument.lot_size),
            "currency": instrument.currency,
        },
        "portfolio": {
            "position_quantity": str(position_quantity),
            "sellable_quantity": str(sellable_quantity),
            "average_cost": str(average_cost),
            "available_cash": str(available_cash),
            "holdings": holdings,
        },
        "observed_at": now.isoformat(),
    }


def _assert_confirmed_rule_quantity(
    admission,
    repository,
    *,
    instrument=None,
    trusted_quote: TrustedQuote | None = None,
) -> None:
    """Bind the worker-proposed quantity back to the confirmed sizing policy."""

    spec = admission.spec
    payload = admission.request.place_order()
    sizing = spec.action.sizing
    if sizing.type in {
        SizingType.NOTIONAL_KRW,
        SizingType.AVAILABLE_CASH_PERCENT_CAPPED,
    }:
        # The worker calculates a preliminary whole-share quantity from a
        # fresh last price.  Trading repeats the cap check with its own
        # executable quote under the book lock.  A price decline may leave the
        # preliminary quantity below the current cap; that is intentional:
        # NOTIONAL_KRW is a maximum amount, never permission to increase a
        # confirmed order's shares after activation.
        if instrument is None or trusted_quote is None:
            return
        price = (
            trusted_quote.ask
            if spec.action.side.value == "BUY"
            else trusted_quote.bid
        )
        if sizing.type is SizingType.NOTIONAL_KRW:
            maximum_notional = sizing.value or Decimal("0")
        else:
            available = repository.available_cash(
                spec.authority.fund_id,
                spec.authority.book_id,
                instrument.currency,
            )
            maximum_notional = min(
                available * (sizing.value or Decimal("0")),
                sizing.cap_krw or Decimal("0"),
            )
        maximum = (maximum_notional / price // instrument.lot_size) * instrument.lot_size
        if maximum <= 0 or payload.quantity > maximum:
            raise DirectiveServiceError(
                "TRADING_CONDITIONAL_RULE_QUANTITY_MISMATCH",
                "conditional execution quantity exceeds the confirmed KRW notional cap",
                409,
            )
        return
    with repository.book_guard(spec.authority.fund_id, spec.authority.book_id):
        instrument = repository.resolve_instrument(
            spec.authority.fund_id,
            spec.authority.book_id,
            spec.instrument_id,
            spec.symbol,
        )
        sellable = repository.sellable_quantity(
            spec.authority.fund_id,
            spec.authority.book_id,
            spec.instrument_id,
        )
        if sizing.type is SizingType.FIXED_SHARES:
            expected = sizing.value or Decimal("0")
        elif sizing.type is SizingType.TARGET_POSITION_WEIGHT:
            if payload.quantity <= 0 or payload.quantity > sellable:
                raise DirectiveServiceError(
                    "TRADING_CONDITIONAL_RULE_QUANTITY_MISMATCH",
                    "target-weight execution exceeds the current sellable position",
                    409,
                )
            return
        else:
            requested = (
                sellable * (sizing.value or Decimal("0"))
                if sizing.type is SizingType.POSITION_PERCENT
                else sellable
            )
            expected = (requested // instrument.lot_size) * instrument.lot_size
    if expected <= 0 or payload.quantity != expected:
        raise DirectiveServiceError(
            "TRADING_CONDITIONAL_RULE_QUANTITY_MISMATCH",
            "conditional execution quantity does not match the confirmed sizing policy",
            409,
        )


@router.post("/{rule_execution_id}/submit", status_code=202)
def submit_conditional_rule_execution(
    rule_execution_id: UUID,
    authorization: str | None = Header(default=None),
) -> dict:
    _authenticate(authorization)
    try:
        admission = admission_repository_from_env().load(rule_execution_id)
    except ConditionalRuleAdmissionError as exc:
        raise DirectiveServiceError(exc.code, str(exc), exc.status_code) from exc
    service = required_directive_service()
    try:
        _assert_confirmed_rule_quantity(admission, service.repository)
    except DirectiveRepositoryError as exc:
        raise DirectiveServiceError(exc.code, str(exc), exc.status_code) from exc
    def _validate_quote_bound_notional(instrument, quote) -> None:
        _assert_confirmed_rule_quantity(
            admission,
            service.repository,
            instrument=instrument,
            trusted_quote=quote,
        )

    record = service.submit_trusted_rule(
        admission.request,
        admission.proof,
        market_quote_max_age_seconds=(
            CONDITIONAL_LIMIT_QUOTE_MAX_AGE_SECONDS
            if admission.spec.action.order_type == "LIMIT"
            else None
        ),
        conditional_quantity_validator=(
            _validate_quote_bound_notional
            if admission.spec.action.sizing.type
            in {
                SizingType.NOTIONAL_KRW,
                SizingType.AVAILABLE_CASH_PERCENT_CAPPED,
            }
            else None
        ),
    )
    return record.view()


@router.get("/rules/{rule_id}/context")
def get_conditional_rule_context(
    rule_id: UUID,
    authorization: str | None = Header(default=None),
) -> dict:
    _authenticate(authorization)
    try:
        authority = context_repository_from_env().load(rule_id)
        service = required_directive_service()
        return _current_context(authority, service.repository)
    except ConditionalRuleAdmissionError as exc:
        raise DirectiveServiceError(exc.code, str(exc), exc.status_code) from exc
    except DirectiveRepositoryError as exc:
        raise DirectiveServiceError(exc.code, str(exc), exc.status_code) from exc


__all__ = [
    "get_conditional_rule_context",
    "router",
    "submit_conditional_rule_execution",
]
