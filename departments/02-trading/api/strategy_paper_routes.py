"""Internal PAPER order boundary for the autonomous strategy runtime.

The strategy container never receives an LS credential and never calls the
broker.  It calls the runtime-control sidecar, which mints a short-lived
least-privilege service proof and reaches this route.  This route then uses
the existing authenticated PAPER directive service, so quote/session,
cash/position, lot-size, idempotency, and LS PAPER adapter rules remain in one
place.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from directives.auth import DirectiveProof
from directives.contracts import DirectiveAction, UserDirectiveRequest
from directives.service import DirectiveServiceError
from internal_service_auth import (
    InternalServiceAuthError,
    STRATEGY_PAPER_EXECUTE_POLICY,
    InternalServiceIdentity,
)

try:
    from .internal_service_auth import authenticate_internal_service
except ImportError:  # pragma: no cover - direct module execution compatibility
    from internal_service_auth import authenticate_internal_service  # type: ignore[no-redef]


router = APIRouter(
    prefix="/trading/v1/internal/strategy-paper",
    tags=["internal-strategy-paper"],
)

_DEPLOYMENT_ID_PATTERN = r"^deployment-[0-9a-f]{24}$"
_SYMBOL_PATTERN = r"^[0-9A-Z]{6}$"
_SIGNAL_KEY_PATTERN = r"^[A-Za-z0-9|._:+-]{8,256}$"


class StrategyPaperOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment_id: str = Field(pattern=_DEPLOYMENT_ID_PATTERN)
    symbol: str = Field(pattern=_SYMBOL_PATTERN)
    side: Literal["BUY", "SELL"]
    quantity: Decimal = Field(gt=0, max_digits=30, decimal_places=10)
    signal_key: str = Field(pattern=_SIGNAL_KEY_PATTERN)


def _configured_uuid(name: str) -> UUID:
    value = os.environ.get(name, "").strip()
    try:
        return UUID(value)
    except ValueError as exc:
        raise DirectiveServiceError(
            "TRADING_STRATEGY_PAPER_SCOPE_UNCONFIGURED",
            f"{name} is not a valid UUID",
            503,
        ) from exc


def _strategy_paper_scope() -> tuple[UUID, UUID, UUID]:
    """Return the one account scope assigned to autonomous PAPER strategies."""

    return (
        _configured_uuid("STRATEGY_PAPER_USER_ID"),
        _configured_uuid("STRATEGY_PAPER_FUND_ID"),
        _configured_uuid("STRATEGY_PAPER_BOOK_ID"),
    )


def _idempotency_key(body: StrategyPaperOrderRequest) -> str:
    digest = hashlib.sha256(body.signal_key.encode("utf-8")).hexdigest()[:40]
    return f"strategy-paper:{body.deployment_id}:{body.symbol}:{body.side.lower()}:{digest}"


def _authenticate(
    authorization: str | None,
) -> InternalServiceIdentity:
    try:
        return authenticate_internal_service(
            authorization,
            policy=STRATEGY_PAPER_EXECUTE_POLICY,
        )
    except InternalServiceAuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.code, "message": str(exc), "action": "HOLD"},
        ) from exc


@router.post("/orders", status_code=202)
def submit_strategy_paper_order(
    body: StrategyPaperOrderRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    _authenticate(authorization)
    user_id, fund_id, book_id = _strategy_paper_scope()
    now = datetime.now(timezone.utc)
    idempotency_key = _idempotency_key(body)
    instruction_ref = f"strategy:{body.deployment_id}:{body.symbol}:{body.side}:{hashlib.sha256(body.signal_key.encode()).hexdigest()[:24]}"
    payload = {
        "instrument_id": None,
        "symbol": body.symbol,
        "side": body.side,
        "quantity": body.quantity,
        "order_type": "MARKET",
        "limit_price": None,
        "time_in_force": "DAY",
    }
    request = UserDirectiveRequest(
        fund_id=fund_id,
        book_id=book_id,
        action=DirectiveAction.PLACE_ORDER,
        instruction_ref=instruction_ref,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    issued_at = now.timestamp()
    proof = DirectiveProof(
        subject=user_id,
        fund_id=fund_id,
        book_id=book_id,
        action=request.action,
        instruction_ref=request.instruction_ref,
        idempotency_key=request.idempotency_key,
        payload_sha256=request.payload_sha256(),
        jti=f"strategy-paper-proof:{idempotency_key}",
        issued_at=issued_at,
        not_before=issued_at,
        expires_at=issued_at + 300,
        scope=frozenset({"trading.user-directive.execute", "trading.strategy-paper.execute"}),
    )
    # Imported lazily to keep this route's module importable in contract tests
    # before the trading lifespan has configured its repository.
    try:
        from directive_routes import required_directive_service
    except ImportError:  # pragma: no cover - package import compatibility
        from .directive_routes import required_directive_service
    record = required_directive_service().submit_trusted_rule(request, proof, now=now)
    return {
        "deployment_id": body.deployment_id,
        "signal_key": body.signal_key,
        "execution_status": "PAPER_ORDER_SUBMITTED",
        "directive": record.view(),
    }


__all__ = ["StrategyPaperOrderRequest", "router", "submit_strategy_paper_order"]
