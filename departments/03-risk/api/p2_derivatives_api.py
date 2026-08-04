"""FastAPI boundary for deterministic P2 derivatives analytics.

The request must contain canonical instrument identifiers and an explicit
margin/volatility snapshot.  Hermes and LangGraph may explain this result,
but neither is allowed to override the gate.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID

from analytics.derivatives_gate import (
    DerivativePosition,
    calculate_derivative_snapshot,
    evaluate_derivative_gate,
)
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/risk/v1/p2", tags=["risk-p2"])


def _canonical_database_url() -> str:
    """Select a writable Risk/QA DSN without using the portfolio read-only DSN."""

    return (
        os.environ.get("RISK_QA_DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )


class DerivativePositionIn(BaseModel):
    instrument_id: str = Field(min_length=1)
    option_type: str = Field(min_length=1)
    quantity: float
    spot: float
    strike: float
    time_to_expiry_years: float
    risk_free_rate: float
    volatility: float
    dividend_yield: float = 0.0
    multiplier: float = 1.0


class DerivativeGateRequest(BaseModel):
    trace_id: UUID
    fund_id: UUID
    as_of: datetime
    positions: list[DerivativePositionIn] = Field(min_length=1)
    stress_shocks: dict[str, float]
    margin_rates: dict[str, float]
    vol_surface: dict[str, float]
    max_abs_delta: float = Field(ge=0)
    max_abs_gamma: float = Field(ge=0)
    max_stress_loss: float = Field(ge=0)
    max_margin_requirement: float = Field(ge=0)


@router.post("/derivatives-check")
def derivatives_check(body: DerivativeGateRequest) -> dict[str, Any]:
    """Calculate and gate an explicit point-in-time derivatives snapshot."""

    if body.as_of.tzinfo is None or body.as_of.utcoffset() is None:
        raise HTTPException(status_code=422, detail="as_of must be timezone-aware")
    positions = tuple(DerivativePosition(**item.model_dump()) for item in body.positions)
    snapshot = calculate_derivative_snapshot(
        positions,
        stress_shocks=body.stress_shocks,
        margin_rates=body.margin_rates,
        vol_surface=body.vol_surface,
    )
    decision = evaluate_derivative_gate(
        snapshot,
        max_abs_delta=body.max_abs_delta,
        max_abs_gamma=body.max_abs_gamma,
        max_stress_loss=body.max_stress_loss,
        max_margin_requirement=body.max_margin_requirement,
    )
    if os.environ.get("RISK_P2_PERSIST", "false").strip().lower() == "true":
        _persist_snapshot(body, snapshot, decision.value)
    return {
        "trace_id": str(body.trace_id),
        "as_of": body.as_of.isoformat(),
        "gate": decision.value,
        "snapshot": {
            **asdict(snapshot),
            "greeks": {key: asdict(value) for key, value in snapshot.greeks.items()},
        },
    }


def _persist_snapshot(body: DerivativeGateRequest, snapshot: Any, decision: str) -> None:
    database_url = _canonical_database_url()
    if not database_url:
        raise HTTPException(status_code=503, detail="DATABASE_URL required for P2 persistence")
    try:
        import psycopg2
        from psycopg2.extras import Json

        with psycopg2.connect(database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                insert into risk.derivative_snapshots (
                  fund_id, trace_id, as_of, calculation_version, input_hash,
                  aggregate_delta, aggregate_gamma, aggregate_vega_per_1pct,
                  stress_loss, margin_requirement, vol_surface_hash,
                  quality_status, reason_codes, greeks, gate_decision
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (fund_id, trace_id, as_of, calculation_version) do nothing
                """,
                (
                    body.fund_id,
                    body.trace_id,
                    body.as_of,
                    snapshot.calculation_version,
                    snapshot.input_hash,
                    snapshot.aggregate_delta,
                    snapshot.aggregate_gamma,
                    snapshot.aggregate_vega_per_1pct,
                    snapshot.stress_loss,
                    snapshot.margin_requirement,
                    snapshot.vol_surface_hash,
                    snapshot.quality_status,
                    Json(list(snapshot.reason_codes)),
                    Json({key: asdict(value) for key, value in snapshot.greeks.items()}),
                    decision,
                ),
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="P2 snapshot persistence failed") from exc
