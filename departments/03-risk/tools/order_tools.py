"""Deterministic order-compliance tool for the Risk worker boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OrderComplianceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mandate_id: str = Field(min_length=1, max_length=100)
    symbol: str = Field(min_length=1, max_length=100)
    side: Literal["BUY", "SELL"]
    notional: Decimal = Field(gt=0)
    current_position_notional: Decimal
    resulting_position_notional: Decimal
    current_exposure_pct: Decimal = Field(ge=0)
    resulting_exposure_pct: Decimal = Field(ge=0)
    max_exposure_pct: Decimal = Field(ge=0)
    portfolio_var: Decimal | None = None
    portfolio_var_limit: Decimal | None = None
    as_of: datetime


class OrderComplianceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal[
        "APPROVE",
        "RESIZE",
        "REJECT",
        "HOLD",
        "HALTED",
        "ESCALATE",
    ]
    authoritative: Literal[True] = True
    reasons: list[str] = Field(default_factory=list)
    limit_breaches: list[str] = Field(default_factory=list)
    max_allowed_notional: Decimal | None = None
    calculation_version: str = "risk-order-compliance-v1"
    evaluated_at: datetime
    tool_name: Literal["evaluate_order_compliance"] = "evaluate_order_compliance"


def evaluate_order_compliance(request: OrderComplianceInput) -> OrderComplianceOutput:
    """Evaluate order state deterministically; never submits an order."""

    breaches: list[str] = []
    reasons: list[str] = []

    if request.resulting_exposure_pct > request.max_exposure_pct:
        breaches.append("TOTAL_EXPOSURE_LIMIT_BREACH")
        reasons.append("Resulting exposure exceeds the mandate exposure limit.")

    if (
        request.portfolio_var is not None
        and request.portfolio_var_limit is not None
        and request.portfolio_var > request.portfolio_var_limit
    ):
        breaches.append("VAR_LIMIT_BREACH")
        reasons.append("Portfolio VaR exceeds the configured limit.")

    if request.resulting_position_notional < 0:
        breaches.append("NEGATIVE_RESULTING_POSITION")
        reasons.append("Resulting position cannot be negative.")

    if "VAR_LIMIT_BREACH" in breaches:
        verdict: Literal[
            "APPROVE", "RESIZE", "REJECT", "HOLD", "HALTED", "ESCALATE"
        ] = "REJECT"
    elif breaches:
        verdict = "RESIZE"
    else:
        verdict = "APPROVE"

    return OrderComplianceOutput(
        verdict=verdict,
        reasons=reasons,
        limit_breaches=breaches,
        evaluated_at=datetime.now(timezone.utc),
    )


__all__ = [
    "OrderComplianceInput",
    "OrderComplianceOutput",
    "evaluate_order_compliance",
]
