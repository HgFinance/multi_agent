"""Deterministic Stress, historical VaR, and Black-Scholes Greeks.

This module never fetches data and never decides an order. Callers must pass a
time-bounded snapshot from an approved Portfolio/Market adapter. Invalid or
missing inputs raise instead of producing a permissive zero-risk result.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import exp, isfinite, log, pi, sqrt
from statistics import NormalDist


class RiskAnalyticsError(ValueError):
    """Raised when a risk calculation cannot be performed safely."""


@dataclass(frozen=True)
class Position:
    instrument_id: str
    quantity: float
    price: float
    multiplier: float = 1.0

    def market_value(self) -> float:
        value = self.quantity * self.price * self.multiplier
        if not all(
            map(_is_finite, (self.quantity, self.price, self.multiplier, value))
        ):
            raise RiskAnalyticsError("position contains a non-finite value")
        return value


@dataclass(frozen=True)
class StressResult:
    scenario: str
    pnl: float
    base_value: float
    stressed_value: float
    shocks: Mapping[str, float]


@dataclass(frozen=True)
class GreekResult:
    delta: float
    gamma: float
    theta_per_year: float
    vega_per_1pct: float
    rho_per_1pct: float


def _is_finite(value: float) -> bool:
    return isfinite(value)


def _require_probability(value: float, name: str) -> None:
    if not _is_finite(value) or not 0.0 < value < 1.0:
        raise RiskAnalyticsError(f"{name} must be between 0 and 1")


def historical_var(
    returns: Sequence[float],
    *,
    confidence: float = 0.99,
    capital: float = 1.0,
) -> float:
    """Return a positive loss amount using a deterministic nearest-rank VaR.

    Returns are decimal returns (``-0.02`` means -2%). Nearest-rank avoids
    version-dependent interpolation differences in a pre-trade gate.
    """

    _require_probability(confidence, "confidence")
    if not returns or not _is_finite(capital) or capital <= 0:
        raise RiskAnalyticsError("returns and positive capital are required")
    if any(not _is_finite(float(item)) for item in returns):
        raise RiskAnalyticsError("returns contain a non-finite value")

    losses = sorted(-float(item) for item in returns)
    rank = max(1, int((confidence * len(losses)) + 0.999999))
    loss_fraction = max(0.0, losses[min(rank - 1, len(losses) - 1)])
    return loss_fraction * capital


def stress_test(
    positions: Iterable[Position],
    shocks: Mapping[str, float],
    *,
    scenario: str = "unnamed",
) -> StressResult:
    """Apply explicit instrument-level percentage shocks to a snapshot."""

    if not scenario.strip():
        raise RiskAnalyticsError("scenario is required")

    position_list = tuple(positions)
    if not position_list:
        raise RiskAnalyticsError("at least one position is required")

    base_value = 0.0
    stressed_value = 0.0
    normalized_shocks: dict[str, float] = {}
    for position in position_list:
        shock = float(shocks.get(position.instrument_id, 0.0))
        if not _is_finite(shock):
            raise RiskAnalyticsError("shock contains a non-finite value")
        if shock < -1.0:
            raise RiskAnalyticsError("a price shock cannot be below -100%")
        base = position.market_value()
        base_value += base
        stressed_value += base * (1.0 + shock)
        normalized_shocks[position.instrument_id] = shock

    return StressResult(
        scenario=scenario,
        pnl=stressed_value - base_value,
        base_value=base_value,
        stressed_value=stressed_value,
        shocks=normalized_shocks,
    )


def black_scholes_greeks(
    *,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    risk_free_rate: float,
    volatility: float,
    option_type: str,
    dividend_yield: float = 0.0,
) -> GreekResult:
    """Calculate vanilla European option Greeks without a mutable model state."""

    values = (
        spot,
        strike,
        time_to_expiry_years,
        risk_free_rate,
        volatility,
        dividend_yield,
    )
    if any(not _is_finite(float(value)) for value in values):
        raise RiskAnalyticsError("option inputs must be finite")
    if spot <= 0 or strike <= 0 or time_to_expiry_years <= 0 or volatility <= 0:
        raise RiskAnalyticsError(
            "spot, strike, expiry, and volatility must be positive"
        )
    normalized_type = option_type.lower()
    if normalized_type not in {"call", "put"}:
        raise RiskAnalyticsError("option_type must be call or put")

    sigma_sqrt_t = volatility * sqrt(time_to_expiry_years)
    d1 = (
        log(spot / strike)
        + (risk_free_rate - dividend_yield + 0.5 * volatility**2) * time_to_expiry_years
    ) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    normal = NormalDist()
    pdf_d1 = exp(-(d1**2) / 2.0) / sqrt(2.0 * pi)
    nd1 = normal.cdf(d1)
    nd2 = normal.cdf(d2)
    nmd1 = normal.cdf(-d1)
    nmd2 = normal.cdf(-d2)
    discounted_spot = exp(-dividend_yield * time_to_expiry_years)
    discounted_strike = exp(-risk_free_rate * time_to_expiry_years)

    if normalized_type == "call":
        delta = discounted_spot * nd1
        theta = (
            -(spot * discounted_spot * pdf_d1 * volatility)
            / (2.0 * sqrt(time_to_expiry_years))
            - risk_free_rate * strike * discounted_strike * nd2
            + dividend_yield * spot * discounted_spot * nd1
        )
        rho = strike * time_to_expiry_years * discounted_strike * nd2
    else:
        delta = discounted_spot * (nd1 - 1.0)
        theta = (
            -(spot * discounted_spot * pdf_d1 * volatility)
            / (2.0 * sqrt(time_to_expiry_years))
            + risk_free_rate * strike * discounted_strike * nmd2
            - dividend_yield * spot * discounted_spot * nmd1
        )
        rho = -strike * time_to_expiry_years * discounted_strike * nmd2

    gamma = discounted_spot * pdf_d1 / (spot * sigma_sqrt_t)
    vega_per_1pct = spot * discounted_spot * pdf_d1 * sqrt(time_to_expiry_years) * 0.01
    rho_per_1pct = rho * 0.01
    result = GreekResult(delta, gamma, theta, vega_per_1pct, rho_per_1pct)
    if any(not _is_finite(float(value)) for value in result.__dict__.values()):
        raise RiskAnalyticsError("calculation produced a non-finite result")
    return result
