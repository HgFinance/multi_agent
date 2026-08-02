"""Risk P1 analytics.

This module deliberately has no network, database, or order side effects.  A
trusted Portfolio/Market adapter supplies point-in-time inputs; this module
normalises instrument identifiers, calculates deterministic metrics, and
returns a fail-closed gate result for the binding Risk Engine.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from statistics import NormalDist
from typing import Any
from uuid import UUID

CALCULATION_VERSION = "risk-p1-analytics-v1"


class RiskP1Error(ValueError):
    """Raised when P1 inputs cannot be used safely."""


class KillSwitchState(StrEnum):
    ENABLED = "ENABLED"
    REDUCE_ONLY = "REDUCE_ONLY"
    ENTRY_BLOCKED = "ENTRY_BLOCKED"
    HALTED = "HALTED"


class P1GateDecision(StrEnum):
    PASS = "PASS"
    REJECT = "REJECT"


@dataclass(frozen=True)
class InstrumentMapping:
    """Broker symbol to canonical ``reference.instruments`` identity."""

    broker_symbol: str
    instrument_id: UUID
    instrument_type: str = "EQUITY"

    def __post_init__(self) -> None:
        if not self.broker_symbol.strip():
            raise RiskP1Error("broker_symbol is required")
        if not self.instrument_type.strip():
            raise RiskP1Error("instrument_type is required")


@dataclass(frozen=True)
class PortfolioPosition:
    broker_symbol: str
    quantity: float
    multiplier: float = 1.0
    instrument_id: UUID | None = None


@dataclass(frozen=True)
class MarketPoint:
    broker_symbol: str
    price: float
    observed_at: datetime
    returns: tuple[float, ...] = ()


@dataclass(frozen=True)
class P1RiskSnapshot:
    fund_id: UUID
    book_id: UUID | None
    strategy_version_id: UUID | None
    as_of: datetime
    gross_exposure: float
    net_exposure: float
    value_at_risk: float | None
    expected_shortfall: float | None
    stress_losses: Mapping[str, float]
    correlation_shock_loss: float | None
    correlation_max: float | None
    quality_status: str
    input_hash: str
    calculation_version: str
    kill_switch_state: KillSwitchState
    breaches: tuple[str, ...]
    exposure_components: tuple[Mapping[str, Any], ...]


def _finite(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise RiskP1Error(f"{name} must be finite")
    return value


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise RiskP1Error(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    count = min(len(left), len(right))
    if count < 2:
        return None
    x = [float(value) for value in left[:count]]
    y = [float(value) for value in right[:count]]
    mean_x = sum(x) / count
    mean_y = sum(y) / count
    numerator = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y))
    denom_x = math.sqrt(sum((a - mean_x) ** 2 for a in x))
    denom_y = math.sqrt(sum((b - mean_y) ** 2 for b in y))
    if denom_x == 0 or denom_y == 0:
        return None
    return max(-1.0, min(1.0, numerator / (denom_x * denom_y)))


def _sample_volatility(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(float(value) for value in values) / len(values)
    return math.sqrt(sum((float(value) - mean) ** 2 for value in values) / (len(values) - 1))


class RiskP1Engine:
    """Build an auditable exposure and market-risk snapshot."""

    def __init__(self, mappings: Sequence[InstrumentMapping], *, max_age: timedelta = timedelta(minutes=5)) -> None:
        if not mappings:
            raise RiskP1Error("at least one instrument mapping is required")
        self._mappings = {item.broker_symbol.strip().upper(): item for item in mappings}
        if len(self._mappings) != len(mappings):
            raise RiskP1Error("duplicate broker_symbol mapping")
        self._max_age = max_age

    def _resolve(self, position: PortfolioPosition) -> InstrumentMapping:
        mapping = self._mappings.get(position.broker_symbol.strip().upper())
        if mapping is None:
            raise RiskP1Error(f"instrument mapping missing for {position.broker_symbol}")
        if position.instrument_id is not None and position.instrument_id != mapping.instrument_id:
            raise RiskP1Error(f"instrument mapping mismatch for {position.broker_symbol}")
        return mapping

    def build_snapshot(
        self,
        *,
        fund_id: UUID,
        book_id: UUID | None,
        strategy_version_id: UUID | None,
        as_of: datetime,
        equity: float,
        positions: Sequence[PortfolioPosition],
        market: Sequence[MarketPoint],
        stress_scenarios: Mapping[str, Mapping[str, float]],
        confidence: float = 0.99,
        kill_switch_state: KillSwitchState = KillSwitchState.ENABLED,
    ) -> P1RiskSnapshot:
        as_of = _utc(as_of, "as_of")
        equity = _finite(equity, "equity")
        if equity <= 0:
            raise RiskP1Error("equity must be positive")
        if not positions:
            raise RiskP1Error("portfolio positions are required")
        if not market:
            raise RiskP1Error("market points are required")
        if not 0.0 < confidence < 1.0:
            raise RiskP1Error("confidence must be between 0 and 1")

        market_by_symbol = {point.broker_symbol.strip().upper(): point for point in market}
        if len(market_by_symbol) != len(market):
            raise RiskP1Error("duplicate market point")
        resolved: list[tuple[InstrumentMapping, PortfolioPosition, MarketPoint, float]] = []
        for position in positions:
            mapping = self._resolve(position)
            point = market_by_symbol.get(mapping.broker_symbol.strip().upper())
            if point is None:
                raise RiskP1Error(f"market point missing for {mapping.broker_symbol}")
            observed_at = _utc(point.observed_at, f"{mapping.broker_symbol}.observed_at")
            if observed_at > as_of or as_of - observed_at > self._max_age:
                raise RiskP1Error(f"market point stale or outside PIT window for {mapping.broker_symbol}")
            quantity = _finite(position.quantity, "quantity")
            multiplier = _finite(position.multiplier, "multiplier")
            price = _finite(point.price, "price")
            if multiplier <= 0 or price <= 0:
                raise RiskP1Error("price and multiplier must be positive")
            resolved.append((mapping, position, point, quantity * price * multiplier))

        gross_value = sum(abs(value) for _, _, _, value in resolved)
        net_value = sum(value for _, _, _, value in resolved)
        gross_exposure = gross_value / equity
        net_exposure = net_value / equity

        exposure_components = tuple(
            {
                "dimension": "INSTRUMENT",
                "dimension_id": str(mapping.instrument_id),
                "value": value / equity,
                "unit": "EQUITY_FRACTION",
                "metadata": {"broker_symbol": mapping.broker_symbol, "instrument_type": mapping.instrument_type},
            }
            for mapping, _, _, value in resolved
        )

        return_series: dict[str, tuple[float, ...]] = {}
        for mapping, _, point, value in resolved:
            returns = tuple(_finite(item, f"{mapping.broker_symbol}.return") for item in point.returns)
            return_series[mapping.broker_symbol] = returns
        history_lengths = [len(values) for values in return_series.values() if values]
        portfolio_returns: tuple[float, ...] = ()
        if history_lengths and len(history_lengths) == len(resolved):
            history_size = min(history_lengths)
            portfolio_returns = tuple(
                sum(
                    (value / equity) * return_series[mapping.broker_symbol][index]
                    for mapping, _, _, value in resolved
                )
                for index in range(history_size)
            )
        value_at_risk: float | None = None
        expected_shortfall: float | None = None
        if len(portfolio_returns) >= 2:
            losses = sorted(-item * equity for item in portfolio_returns)
            rank = max(1, math.ceil(confidence * len(losses))) - 1
            value_at_risk = max(0.0, losses[min(rank, len(losses) - 1)])
            tail = [loss for loss in losses if loss >= value_at_risk]
            expected_shortfall = sum(tail) / len(tail) if tail else value_at_risk

        stress_losses: dict[str, float] = {}
        for scenario, shocks in stress_scenarios.items():
            if not scenario.strip():
                raise RiskP1Error("stress scenario name is required")
            stressed = 0.0
            for mapping, _, _, value in resolved:
                shock = _finite(float(shocks.get(mapping.broker_symbol, shocks.get(mapping.broker_symbol.upper(), 0.0))), "shock")
                if shock < -1.0:
                    raise RiskP1Error("price shock cannot be below -100 percent")
                stressed += value * (1.0 + shock)
            stress_losses[scenario] = max(0.0, net_value - stressed)

        correlations = [
            correlation
            for index, left in enumerate(resolved)
            for right in resolved[index + 1 :]
            for correlation in [_correlation(return_series[left[0].broker_symbol], return_series[right[0].broker_symbol])]
            if correlation is not None
        ]
        correlation_max = max(correlations) if correlations else None
        correlation_shock_loss: float | None = None
        if len(portfolio_returns) >= 2:
            volatilities = [
                abs(value / equity) * volatility
                for mapping, _, _, value in resolved
                if (volatility := _sample_volatility(return_series[mapping.broker_symbol])) is not None
            ]
            perfect_corr_vol = sum(volatilities)
            observed_vol = _sample_volatility(portfolio_returns) or 0.0
            correlation_shock_loss = max(0.0, (perfect_corr_vol - observed_vol) * equity * NormalDist().inv_cdf(confidence))

        quality_status = "PASS"
        breaches: list[str] = []
        if value_at_risk is None or expected_shortfall is None:
            quality_status = "WARN"
            breaches.append("insufficient_return_history")
        if not stress_losses:
            quality_status = "WARN"
            breaches.append("stress_scenario_missing")
        if correlation_shock_loss is None:
            quality_status = "WARN"
            breaches.append("insufficient_correlation_history")

        payload = {
            "fund_id": str(fund_id),
            "book_id": str(book_id) if book_id else None,
            "strategy_version_id": str(strategy_version_id) if strategy_version_id else None,
            "as_of": as_of.isoformat(),
            "positions": [
                {"instrument_id": str(mapping.instrument_id), "quantity": position.quantity, "value": value}
                for mapping, position, _, value in resolved
            ],
            "market": [
                {"symbol": point.broker_symbol, "price": point.price, "observed_at": _utc(point.observed_at, "observed_at").isoformat(), "returns": point.returns}
                for _, _, point, _ in resolved
            ],
            "stress_scenarios": stress_scenarios,
            "confidence": confidence,
            "calculation_version": CALCULATION_VERSION,
        }
        input_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        return P1RiskSnapshot(
            fund_id=fund_id,
            book_id=book_id,
            strategy_version_id=strategy_version_id,
            as_of=as_of,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            value_at_risk=value_at_risk,
            expected_shortfall=expected_shortfall,
            stress_losses=stress_losses,
            correlation_shock_loss=correlation_shock_loss,
            correlation_max=correlation_max,
            quality_status=quality_status,
            input_hash=input_hash,
            calculation_version=CALCULATION_VERSION,
            kill_switch_state=kill_switch_state,
            breaches=tuple(breaches),
            exposure_components=exposure_components,
        )


def evaluate_p1_gate(snapshot: P1RiskSnapshot, *, entry_requested: bool = True) -> P1GateDecision:
    """Allow only a complete, healthy snapshot; never allow uncertainty."""

    if not entry_requested:
        return P1GateDecision.PASS
    if snapshot.kill_switch_state is not KillSwitchState.ENABLED:
        return P1GateDecision.REJECT
    if snapshot.quality_status != "PASS":
        return P1GateDecision.REJECT
    if snapshot.value_at_risk is None or snapshot.expected_shortfall is None:
        return P1GateDecision.REJECT
    if any(value < 0 or not math.isfinite(value) for value in snapshot.stress_losses.values()):
        return P1GateDecision.REJECT
    return P1GateDecision.PASS
