from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics.derivatives_gate import (
    DerivativeGateDecision,
    DerivativePosition,
    calculate_derivative_snapshot,
    evaluate_derivative_gate,
)
from analytics.risk_metrics import RiskAnalyticsError


def _position() -> DerivativePosition:
    return DerivativePosition(
        instrument_id="AAPL-C-200",
        option_type="call",
        quantity=1,
        spot=200,
        strike=200,
        time_to_expiry_years=0.5,
        risk_free_rate=0.03,
        volatility=0.25,
    )


def test_p2_snapshot_and_gate_are_deterministic() -> None:
    snapshot = calculate_derivative_snapshot(
        (_position(),),
        stress_shocks={"AAPL-C-200": -0.2},
        margin_rates={"AAPL-C-200": 0.25},
        vol_surface={"AAPL-C-200": 0.25},
    )

    assert snapshot.calculation_version == "risk-p2-derivatives-v1"
    assert len(snapshot.input_hash) == 64
    assert (
        evaluate_derivative_gate(
            snapshot,
            max_abs_delta=2,
            max_abs_gamma=2,
            max_stress_loss=10_000,
        )
        is DerivativeGateDecision.PASS
    )


def test_p2_gate_rejects_stress_limit() -> None:
    snapshot = calculate_derivative_snapshot(
        (_position(),),
        stress_shocks={"AAPL-C-200": -0.2},
        margin_rates={"AAPL-C-200": 0.25},
        vol_surface={"AAPL-C-200": 0.25},
    )

    assert (
        evaluate_derivative_gate(
            snapshot,
            max_abs_delta=2,
            max_abs_gamma=2,
            max_stress_loss=0,
        )
        is DerivativeGateDecision.REJECT
    )


def test_p2_rejects_empty_positions() -> None:
    with pytest.raises(RiskAnalyticsError, match="positions"):
        calculate_derivative_snapshot((), stress_shocks={})


def test_p2_requires_margin_and_vol_surface_inputs() -> None:
    snapshot = calculate_derivative_snapshot(
        (_position(),), stress_shocks={"AAPL-C-200": -0.2}
    )
    assert snapshot.quality_status == "FAIL"
    assert "margin_input_missing:AAPL-C-200" in snapshot.reason_codes
    assert "vol_surface_input_missing:AAPL-C-200" in snapshot.reason_codes
    assert (
        evaluate_derivative_gate(
            snapshot,
            max_abs_delta=2,
            max_abs_gamma=2,
            max_stress_loss=10_000,
            max_margin_requirement=10_000,
        )
        == DerivativeGateDecision.REJECT
    )
