from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics.risk_metrics import (
    Position,
    RiskAnalyticsError,
    black_scholes_greeks,
    historical_var,
    stress_test,
)


def test_historical_var_is_deterministic_and_positive() -> None:
    returns = (-0.10, 0.02, -0.03, 0.01, -0.05)
    assert historical_var(returns, confidence=0.8, capital=1000) == 50
    assert historical_var(returns, confidence=0.8, capital=1000) == 50


def test_stress_test_uses_explicit_position_shocks() -> None:
    result = stress_test(
        [Position("A", quantity=10, price=100), Position("B", quantity=2, price=50)],
        {"A": -0.20, "B": 0.10},
        scenario="equity-shock",
    )
    assert result.base_value == 1100
    assert result.stressed_value == 910
    assert result.pnl == -190


def test_black_scholes_greeks_and_invalid_input() -> None:
    result = black_scholes_greeks(
        spot=100,
        strike=100,
        time_to_expiry_years=1,
        risk_free_rate=0.02,
        volatility=0.2,
        option_type="call",
    )
    assert 0 < result.delta < 1
    assert result.gamma > 0
    assert result.vega_per_1pct > 0
    with pytest.raises(RiskAnalyticsError):
        black_scholes_greeks(
            spot=100,
            strike=100,
            time_to_expiry_years=0,
            risk_free_rate=0.02,
            volatility=0.2,
            option_type="call",
        )
