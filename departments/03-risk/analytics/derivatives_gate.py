"""Deterministic P2 derivatives analytics and gate.

The gate is intentionally separate from the LLM worker.  A worker can explain
the result, but only this module can return a binding P2 pass/reject.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .risk_metrics import (
    GreekResult,
    Position,
    RiskAnalyticsError,
    black_scholes_greeks,
    stress_test,
)

CALCULATION_VERSION = "risk-p2-derivatives-v1"


class DerivativeGateDecision(StrEnum):
    PASS = "PASS"
    REJECT = "REJECT"


@dataclass(frozen=True)
class DerivativePosition:
    instrument_id: str
    option_type: str
    quantity: float
    spot: float
    strike: float
    time_to_expiry_years: float
    risk_free_rate: float
    volatility: float
    dividend_yield: float = 0.0
    multiplier: float = 1.0


@dataclass(frozen=True)
class DerivativeRiskSnapshot:
    calculation_version: str
    input_hash: str
    greeks: dict[str, GreekResult]
    aggregate_delta: float
    aggregate_gamma: float
    aggregate_vega_per_1pct: float
    stress_loss: float
    margin_requirement: float
    vol_surface_hash: str
    quality_status: str
    reason_codes: tuple[str, ...]


def calculate_derivative_snapshot(
    positions: tuple[DerivativePosition, ...],
    *,
    stress_shocks: dict[str, float],
    margin_rates: dict[str, float] | None = None,
    vol_surface: dict[str, float] | None = None,
) -> DerivativeRiskSnapshot:
    if not positions:
        raise RiskAnalyticsError("derivative positions are required")
    greeks: dict[str, GreekResult] = {}
    aggregate_delta = 0.0
    aggregate_gamma = 0.0
    aggregate_vega = 0.0
    margin_requirement = 0.0
    stress_positions = []
    payload: list[dict[str, Any]] = []
    reasons: list[str] = []
    margin_rates = margin_rates or {}
    vol_surface = vol_surface or {}
    for position in positions:
        values = (
            position.quantity,
            position.spot,
            position.strike,
            position.time_to_expiry_years,
            position.volatility,
            position.multiplier,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise RiskAnalyticsError(f"non-finite derivatives input: {position.instrument_id}")
        greek = black_scholes_greeks(
            spot=position.spot,
            strike=position.strike,
            time_to_expiry_years=position.time_to_expiry_years,
            risk_free_rate=position.risk_free_rate,
            volatility=position.volatility,
            option_type=position.option_type,
            dividend_yield=position.dividend_yield,
        )
        greeks[position.instrument_id] = greek
        scale = position.quantity * position.multiplier
        aggregate_delta += greek.delta * scale
        aggregate_gamma += greek.gamma * scale
        aggregate_vega += greek.vega_per_1pct * scale
        margin_rate = margin_rates.get(position.instrument_id)
        surface_volatility = vol_surface.get(position.instrument_id)
        if margin_rate is None:
            reasons.append(f"margin_input_missing:{position.instrument_id}")
        elif not math.isfinite(float(margin_rate)) or not 0 <= float(margin_rate) <= 1:
            reasons.append(f"margin_input_invalid:{position.instrument_id}")
        else:
            margin_requirement += abs(position.quantity * position.spot * position.multiplier) * float(margin_rate)
        if surface_volatility is None:
            reasons.append(f"vol_surface_input_missing:{position.instrument_id}")
        elif not math.isfinite(float(surface_volatility)) or float(surface_volatility) <= 0:
            reasons.append(f"vol_surface_input_invalid:{position.instrument_id}")
        # A derivative's stressed notional is explicit; no implicit market value.
        stress_positions.append(
            Position(
                position.instrument_id,
                position.quantity,
                position.spot,
                position.multiplier,
            )
        )
        payload.append(position.__dict__)

    stress = stress_test(stress_positions, stress_shocks, scenario="derivatives-shock")
    reason_codes: list[str] = list(reasons)
    if not math.isfinite(-stress.pnl):
        reason_codes.append("stress_result_non_finite")
    quality_status = "PASS" if not reason_codes else "FAIL"
    vol_surface_hash = hashlib.sha256(
        json.dumps(vol_surface, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    encoded = json.dumps(
        {
            "positions": payload,
            "stress_shocks": stress_shocks,
            "margin_rates": margin_rates,
            "vol_surface": vol_surface,
            "version": CALCULATION_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return DerivativeRiskSnapshot(
        calculation_version=CALCULATION_VERSION,
        input_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        greeks=greeks,
        aggregate_delta=aggregate_delta,
        aggregate_gamma=aggregate_gamma,
        aggregate_vega_per_1pct=aggregate_vega,
        stress_loss=max(0.0, -stress.pnl),
        margin_requirement=margin_requirement,
        vol_surface_hash=vol_surface_hash,
        quality_status=quality_status,
        reason_codes=tuple(reason_codes),
    )


def evaluate_derivative_gate(
    snapshot: DerivativeRiskSnapshot,
    *,
    max_abs_delta: float,
    max_abs_gamma: float,
    max_stress_loss: float,
    max_margin_requirement: float | None = None,
) -> DerivativeGateDecision:
    """Reject missing/over-limit P2 metrics; never resize silently."""

    limits = (max_abs_delta, max_abs_gamma, max_stress_loss)
    if not all(math.isfinite(float(value)) and value >= 0 for value in limits):
        raise RiskAnalyticsError("derivative gate limits must be finite and non-negative")
    if snapshot.quality_status != "PASS":
        return DerivativeGateDecision.REJECT
    if abs(snapshot.aggregate_delta) > max_abs_delta:
        return DerivativeGateDecision.REJECT
    if abs(snapshot.aggregate_gamma) > max_abs_gamma:
        return DerivativeGateDecision.REJECT
    if snapshot.stress_loss > max_stress_loss:
        return DerivativeGateDecision.REJECT
    if max_margin_requirement is not None:
        if not math.isfinite(float(max_margin_requirement)) or max_margin_requirement < 0:
            raise RiskAnalyticsError("max_margin_requirement must be finite and non-negative")
        if snapshot.margin_requirement > max_margin_requirement:
            return DerivativeGateDecision.REJECT
    return DerivativeGateDecision.PASS
