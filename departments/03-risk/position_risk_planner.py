"""Deterministic PAPER position stop/take-profit risk planner.

Research and Quant values are read-only observations.  The planner never asks
an LLM to produce a price and never activates an order.  Its invariant is:

    quantity_cap * abs(entry_reference - stop_price) <= position_risk_amount

Wider stops therefore reduce quantity.  Missing, stale, or non-authoritative
snapshots produce ``DEFER`` without fabricated prices.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from risk_observability import risk_span

CALCULATION_VERSION = "dynamic-position-risk-planner.v1"
SCHEMA_VERSION = "risk.position-risk-plan.v1"
_PLAN_NAMESPACE = UUID("c975a2ad-963d-408b-9f68-97253029974e")


class MarketRegime(StrEnum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    DOWNTREND = "DOWNTREND"
    STRESS = "STRESS"


class PlanAction(StrEnum):
    PROPOSE = "PROPOSE"
    DEFER = "DEFER"
    REDUCE_ONLY = "REDUCE_ONLY"


class DataQuality(StrEnum):
    VALID = "VALID"
    STALE = "STALE"
    NON_AUTHORITATIVE = "NON_AUTHORITATIVE"
    MISSING = "MISSING"


class DynamicRiskMandate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_capital: Decimal = Field(gt=0)
    trade_risk_budget_pct: Decimal = Field(gt=0, le=1)
    max_instrument_weight: Decimal = Field(gt=0, le=1)
    min_reward_risk_ratio: Decimal = Field(default=Decimal("1.20"), gt=0)


class DynamicMarketSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market_snapshot_id: str = Field(min_length=1, max_length=200)
    observed_at: datetime
    authoritative: bool
    last_price: Decimal = Field(gt=0)
    atr: Decimal = Field(gt=0)
    realized_vol_annualized: Decimal = Field(ge=0)
    trend_score: Decimal = Field(ge=-1, le=1)
    spread_bps: Decimal = Field(ge=0)
    gap_risk_pct: Decimal = Field(ge=0, le=1)
    tradable: bool = True


class ExistingPositionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stop_price: Decimal = Field(gt=0)
    position_risk_amount: Decimal = Field(gt=0)


class PositionRiskPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_id: UUID
    instrument_id: str = Field(min_length=1, max_length=200)
    mandate_version_id: UUID | Literal["unversioned"]
    portfolio_snapshot_id: str = Field(min_length=1, max_length=200)
    portfolio_snapshot_observed_at: datetime
    portfolio_snapshot_authoritative: bool
    market: DynamicMarketSnapshot
    mandate: DynamicRiskMandate
    as_of: datetime
    side: Literal["LONG"] = "LONG"
    execution_mode: Literal["PAPER"] = "PAPER"
    task_id: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(min_length=1, max_length=200)
    current_quantity: Decimal = Field(default=Decimal(0), ge=0)
    existing_plan: ExistingPositionPlan | None = None
    max_snapshot_age_seconds: int = Field(default=300, ge=1, le=3600)

    @model_validator(mode="after")
    def timezone_required(self) -> PositionRiskPlanRequest:
        for name, value in (
            ("as_of", self.as_of),
            ("market.observed_at", self.market.observed_at),
            ("portfolio_snapshot_observed_at", self.portfolio_snapshot_observed_at),
        ):
            if value.tzinfo is None:
                raise ValueError(f"{name} must include a timezone")
        return self


class LiquidationStage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    trigger_price: Decimal = Field(gt=0)
    quantity_fraction: Decimal = Field(gt=0, le=1)
    action: Literal["TAKE_PROFIT", "TRAIL_REMAINDER"]


class PositionRiskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["risk.position-risk-plan.v1"] = SCHEMA_VERSION
    risk_plan_id: UUID
    fund_id: UUID
    instrument_id: str
    mandate_version_id: str
    portfolio_snapshot_id: str
    market_snapshot_id: str
    as_of: datetime
    expires_at: datetime
    regime: MarketRegime | None = None
    action: PlanAction
    entry_reference: Decimal | None = None
    stop_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    trailing_activation_price: Decimal | None = None
    trailing_distance: Decimal | None = None
    position_risk_amount: Decimal | None = None
    quantity_cap: Decimal | None = None
    current_quantity: Decimal = Decimal(0)
    reward_risk_ratio: Decimal | None = None
    liquidation_stages: list[LiquidationStage] = Field(default_factory=list)
    calculation_version: str = CALCULATION_VERSION
    input_hash: str
    data_quality: DataQuality
    reason_codes: list[str] = Field(default_factory=list)
    review_triggers: list[str] = Field(default_factory=list)
    execution_mode: Literal["PAPER"] = "PAPER"
    task_id: str
    trace_id: str


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, UUID, StrEnum)):
        return str(value)
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_jsonable(child) for child in value]
    return value


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def classify_regime(market: DynamicMarketSnapshot) -> MarketRegime:
    if (
        not market.tradable
        or market.realized_vol_annualized >= Decimal("0.60")
        or market.gap_risk_pct >= Decimal("0.08")
        or market.spread_bps >= Decimal(120)
    ):
        return MarketRegime.STRESS
    if market.trend_score <= Decimal("-0.35"):
        return MarketRegime.DOWNTREND
    if (
        market.realized_vol_annualized >= Decimal("0.35")
        or market.spread_bps >= Decimal(50)
        or market.gap_risk_pct >= Decimal("0.035")
    ):
        return MarketRegime.CAUTION
    return MarketRegime.NORMAL


def position_risk_identity(
    request: PositionRiskPlanRequest | dict[str, Any],
) -> tuple[PositionRiskPlanRequest, str, UUID]:
    """Normalize once and expose the deterministic trace identity."""

    item = (
        request
        if isinstance(request, PositionRiskPlanRequest)
        else PositionRiskPlanRequest.model_validate(request)
    )
    input_hash = _hash(item)
    return item, input_hash, uuid5(_PLAN_NAMESPACE, input_hash)


def _defer(
    request: PositionRiskPlanRequest,
    input_hash: str,
    quality: DataQuality,
    reasons: list[str],
    *,
    regime: MarketRegime | None = None,
    action: PlanAction = PlanAction.DEFER,
) -> PositionRiskPlan:
    plan_id = uuid5(_PLAN_NAMESPACE, input_hash)
    return PositionRiskPlan(
        risk_plan_id=plan_id,
        fund_id=request.fund_id,
        instrument_id=request.instrument_id,
        mandate_version_id=str(request.mandate_version_id),
        portfolio_snapshot_id=request.portfolio_snapshot_id,
        market_snapshot_id=request.market.market_snapshot_id,
        as_of=request.as_of,
        expires_at=request.as_of,
        regime=regime,
        action=action,
        input_hash=input_hash,
        data_quality=quality,
        reason_codes=reasons,
        review_triggers=["AUTHORITATIVE_SNAPSHOT_REQUIRED"],
        current_quantity=request.current_quantity,
        task_id=request.task_id,
        trace_id=request.trace_id,
    )


def plan_position_risk(
    request: PositionRiskPlanRequest | dict[str, Any],
) -> PositionRiskPlan:
    item, input_hash, plan_id = position_risk_identity(request)
    trace_metadata = {
        "task_id": item.task_id,
        "trace_id": item.trace_id,
        "risk_plan_id": str(plan_id),
        "mandate_version_id": str(item.mandate_version_id),
        "input_hash": input_hash,
        "algorithm_version": CALCULATION_VERSION,
    }
    with risk_span("risk.mandate-load", trace_metadata):
        if item.mandate_version_id == "unversioned":
            return _defer(item, input_hash, DataQuality.NON_AUTHORITATIVE, ["UNVERSIONED_MANDATE"])
    with risk_span("risk.portfolio-snapshot", trace_metadata):
        if not item.portfolio_snapshot_authoritative:
            return _defer(item, input_hash, DataQuality.NON_AUTHORITATIVE, ["NON_AUTHORITATIVE_PORTFOLIO_SNAPSHOT"])
    with risk_span("risk.market-snapshot", trace_metadata):
        if not item.market.authoritative:
            return _defer(item, input_hash, DataQuality.NON_AUTHORITATIVE, ["NON_AUTHORITATIVE_MARKET_SNAPSHOT"])
    ages = (
        (item.as_of - item.market.observed_at).total_seconds(),
        (item.as_of - item.portfolio_snapshot_observed_at).total_seconds(),
    )
    if any(age < 0 or age > item.max_snapshot_age_seconds for age in ages):
        return _defer(item, input_hash, DataQuality.STALE, ["STALE_SNAPSHOT"])

    with risk_span("risk.regime-classification", trace_metadata):
        regime = classify_regime(item.market)
    if regime == MarketRegime.STRESS:
        return _defer(
            item,
            input_hash,
            DataQuality.VALID,
            ["MARKET_STRESS", "ENTRY_BLOCKED", "REDUCE_ONLY_PRIORITY"],
            regime=regime,
            action=PlanAction.REDUCE_ONLY,
        )

    entry = item.market.last_price
    atr_factor = {
        MarketRegime.NORMAL: Decimal("1.50"),
        MarketRegime.CAUTION: Decimal("1.80"),
        MarketRegime.DOWNTREND: Decimal("2.00"),
    }[regime]
    take_factor = {
        MarketRegime.NORMAL: Decimal("3.00"),
        MarketRegime.CAUTION: Decimal("2.50"),
        MarketRegime.DOWNTREND: Decimal("1.50"),
    }[regime]
    daily_sigma = item.market.realized_vol_annualized / Decimal("15.874507866")
    volatility_distance = entry * daily_sigma * Decimal("1.25")
    gap_distance = entry * item.market.gap_risk_pct
    spread_distance = entry * item.market.spread_bps / Decimal(10000) * Decimal(2)
    with risk_span("risk.stop-calculation", trace_metadata):
        stop_distance = max(
            item.market.atr * atr_factor,
            volatility_distance,
            gap_distance,
            spread_distance,
        )
        stop_price = (entry - stop_distance).quantize(Decimal("0.0001"))
    if stop_price <= 0:
        return _defer(item, input_hash, DataQuality.VALID, ["INVALID_STOP_PRICE"], regime=regime)

    # A held losing position may tighten its stop but may not move it farther
    # away without explicit user approval.  This planner has no approval input,
    # so the unsafe proposal is rejected.
    if item.existing_plan is not None and stop_price < item.existing_plan.stop_price:
        return _defer(
            item,
            input_hash,
            DataQuality.VALID,
            ["ADVERSE_STOP_RELAXATION_FORBIDDEN"],
            regime=regime,
        )

    with risk_span("risk.take-profit-calculation", trace_metadata):
        take_distance = item.market.atr * take_factor
        take_profit = (entry + take_distance).quantize(Decimal("0.0001"))
        reward_risk = (take_distance / stop_distance).quantize(Decimal("0.0001"))
    if reward_risk < item.mandate.min_reward_risk_ratio:
        return _defer(
            item,
            input_hash,
            DataQuality.VALID,
            ["MIN_REWARD_RISK_NOT_MET", f"CALCULATED_RR:{reward_risk}"],
            regime=regime,
        )

    risk_amount = (
        item.mandate.base_capital * item.mandate.trade_risk_budget_pct
    ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if item.existing_plan is not None:
        risk_amount = min(risk_amount, item.existing_plan.position_risk_amount)
    risk_quantity = (risk_amount / stop_distance).quantize(Decimal(1), rounding=ROUND_DOWN)
    concentration_quantity = (
        item.mandate.base_capital * item.mandate.max_instrument_weight / entry
    ).quantize(Decimal(1), rounding=ROUND_DOWN)
    quantity_cap = min(risk_quantity, concentration_quantity)
    if quantity_cap <= 0:
        return _defer(item, input_hash, DataQuality.VALID, ["QUANTITY_CAP_ZERO"], regime=regime)
    with risk_span("risk.constraint-validation", trace_metadata):
        if quantity_cap * stop_distance > risk_amount:
            raise AssertionError("position loss budget invariant violated")

    expiry_delta = {
        MarketRegime.NORMAL: timedelta(hours=24),
        MarketRegime.CAUTION: timedelta(hours=4),
        MarketRegime.DOWNTREND: timedelta(hours=1),
    }[regime]
    trailing_distance = (stop_distance * Decimal("0.75")).quantize(Decimal("0.0001"))
    trailing_activation = (entry + take_distance * Decimal("0.50")).quantize(
        Decimal("0.0001")
    )
    return PositionRiskPlan(
        risk_plan_id=plan_id,
        fund_id=item.fund_id,
        instrument_id=item.instrument_id,
        mandate_version_id=str(item.mandate_version_id),
        portfolio_snapshot_id=item.portfolio_snapshot_id,
        market_snapshot_id=item.market.market_snapshot_id,
        as_of=item.as_of,
        expires_at=item.as_of + expiry_delta,
        regime=regime,
        action=PlanAction.PROPOSE,
        entry_reference=entry,
        stop_price=stop_price,
        take_profit_price=take_profit,
        trailing_activation_price=trailing_activation,
        trailing_distance=trailing_distance,
        position_risk_amount=risk_amount,
        quantity_cap=quantity_cap,
        current_quantity=item.current_quantity,
        reward_risk_ratio=reward_risk,
        liquidation_stages=[
            LiquidationStage(sequence=1, trigger_price=take_profit, quantity_fraction=Decimal("0.50"), action="TAKE_PROFIT"),
            LiquidationStage(sequence=2, trigger_price=trailing_activation, quantity_fraction=Decimal("0.50"), action="TRAIL_REMAINDER"),
        ],
        input_hash=input_hash,
        data_quality=DataQuality.VALID,
        reason_codes=[f"REGIME:{regime}"],
        review_triggers=[
            "PLAN_EXPIRED",
            "REGIME_CHANGED",
            "ATR_CHANGED_20PCT",
            "POSITION_QUANTITY_CHANGED",
            "MANDATE_VERSION_CHANGED",
            "TRADING_HALT_OR_GAP",
        ],
        task_id=item.task_id,
        trace_id=item.trace_id,
    )


__all__ = [
    "CALCULATION_VERSION",
    "DataQuality",
    "DynamicMarketSnapshot",
    "DynamicRiskMandate",
    "ExistingPositionPlan",
    "MarketRegime",
    "PlanAction",
    "PositionRiskPlan",
    "PositionRiskPlanRequest",
    "classify_regime",
    "plan_position_risk",
    "position_risk_identity",
]
