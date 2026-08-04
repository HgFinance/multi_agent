"""FastAPI routes for the external Risk P1 snapshot boundary."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from integrations.ls_openapi import LSOpenAPIClient
from p1.analytics import InstrumentMapping, KillSwitchState
from p1.instrument_repository import PostgresInstrumentMappingRepository
from p1.repository import RiskP1Repository
from p1.runtime import (
    ExternalRiskRuntimeConfig,
    RiskExternalRuntimeError,
    collect_external_assessment,
)
from pydantic import BaseModel, Field

router = APIRouter(prefix="/risk/v1/p1", tags=["risk-p1"])


class InstrumentMappingIn(BaseModel):
    broker_symbol: str = Field(min_length=1)
    instrument_id: UUID
    instrument_type: str = "EQUITY"


class ExternalP1SnapshotRequest(BaseModel):
    trace_id: UUID
    fund_id: UUID
    book_id: UUID | None = None
    strategy_version_id: UUID | None = None
    as_of: datetime
    broker_symbols: list[str] = Field(min_length=1)
    mappings: list[InstrumentMappingIn] | None = None
    stress_scenarios: dict[str, dict[str, float]] = Field(default_factory=dict)
    returns_by_symbol: dict[str, list[float]] = Field(default_factory=dict)
    confidence: float = Field(default=0.99, gt=0, lt=1)
    kill_switch_state: KillSwitchState = KillSwitchState.ENABLED


def _resolve_mappings(body: ExternalP1SnapshotRequest) -> tuple[InstrumentMapping, ...]:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        import psycopg2

        with psycopg2.connect(database_url) as connection:
            return PostgresInstrumentMappingRepository(connection).resolve(
                body.broker_symbols,
                as_of=body.as_of,
            )
    if os.environ.get("LS_ENV", "PAPER").strip().upper() != "PAPER":
        raise RiskExternalRuntimeError("DATABASE_URL is required for non-PAPER instrument resolution")
    if not body.mappings:
        raise RiskExternalRuntimeError("mappings are required in PAPER without DATABASE_URL")
    mappings = tuple(
        InstrumentMapping(
            broker_symbol=item.broker_symbol,
            instrument_id=item.instrument_id,
            instrument_type=item.instrument_type,
        )
        for item in body.mappings
    )
    requested = {symbol.strip().upper() for symbol in body.broker_symbols}
    provided = {mapping.broker_symbol.strip().upper() for mapping in mappings}
    if requested != provided:
        raise RiskExternalRuntimeError("broker_symbols and mappings must match exactly")
    return mappings


@router.post("/external-snapshot")
def external_p1_snapshot(body: ExternalP1SnapshotRequest) -> dict[str, Any]:
    """Fetch read-only broker inputs and return a deterministic P1 assessment."""

    try:
        config = ExternalRiskRuntimeConfig(
            fund_id=body.fund_id,
            book_id=body.book_id,
            strategy_version_id=body.strategy_version_id,
            as_of=body.as_of,
            mappings=_resolve_mappings(body),
            stress_scenarios=body.stress_scenarios,
            confidence=body.confidence,
            kill_switch_state=body.kill_switch_state,
        )
        with LSOpenAPIClient.from_env() as client:
            assessment = collect_external_assessment(
                client,
                config,
                returns_by_symbol=body.returns_by_symbol,
            )
        if os.environ.get("RISK_P1_PERSIST", "false").strip().lower() == "true":
            _persist_snapshot(assessment.snapshot, trace_id=body.trace_id)
        return assessment.as_dict()
    except RiskExternalRuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="external Risk snapshot unavailable") from exc


def _persist_snapshot(snapshot: Any, *, trace_id: UUID) -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    raw_scenarios = os.environ.get("RISK_STRESS_SCENARIO_IDS_JSON", "")
    if not database_url or not raw_scenarios:
        raise RiskExternalRuntimeError(
            "DATABASE_URL and RISK_STRESS_SCENARIO_IDS_JSON are required for P1 persistence"
        )
    import json

    import psycopg2

    try:
        scenario_ids = {name: UUID(value) for name, value in json.loads(raw_scenarios).items()}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RiskExternalRuntimeError("RISK_STRESS_SCENARIO_IDS_JSON is invalid") from exc
    with psycopg2.connect(database_url) as connection:
        RiskP1Repository(connection).save_snapshot(
            snapshot,
            stress_scenario_ids=scenario_ids,
        trace_id=trace_id,
        )
