"""External Risk P1 runtime boundary.

This module connects the read-only market/portfolio adapter to the deterministic
P1 calculator.  It deliberately does not create orders and does not let an LLM
change a gate result.  Missing credentials, mappings, or stale data fail closed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from integrations.ls_openapi import LSOpenAPIClient

from .analytics import (
    InstrumentMapping,
    KillSwitchState,
    P1GateDecision,
    P1RiskSnapshot,
    RiskP1Engine,
    RiskP1Error,
    evaluate_p1_gate,
)
from .ls_adapter import collect_ls_inputs


class RiskExternalRuntimeError(RiskP1Error):
    """Raised when an external P1 run cannot be safely configured."""


@dataclass(frozen=True)
class ExternalRiskRuntimeConfig:
    """Immutable, auditable inputs required for one external P1 run."""

    fund_id: UUID
    book_id: UUID | None
    strategy_version_id: UUID | None
    as_of: datetime
    mappings: tuple[InstrumentMapping, ...]
    stress_scenarios: Mapping[str, Mapping[str, float]]
    confidence: float = 0.99
    kill_switch_state: KillSwitchState = KillSwitchState.ENABLED

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise RiskExternalRuntimeError("as_of must be timezone-aware")
        if not self.mappings:
            raise RiskExternalRuntimeError("at least one canonical instrument mapping is required")

    @classmethod
    def from_env(cls, *, as_of: datetime | None = None) -> ExternalRiskRuntimeConfig:
        """Load non-secret runtime configuration from environment variables.

        ``RISK_INSTRUMENT_MAPPINGS_JSON`` is intentionally explicit.  Guessing a
        broker symbol's identity is unsafe and would make replay non-deterministic.
        Format: ``[{"broker_symbol":"AAPL","instrument_id":"uuid"}]``.
        """

        def required_uuid(name: str) -> UUID:
            value = os.environ.get(name, "").strip()
            if not value:
                raise RiskExternalRuntimeError(f"{name} is required")
            try:
                return UUID(value)
            except ValueError as exc:
                raise RiskExternalRuntimeError(f"{name} is not a UUID") from exc

        raw_mappings = os.environ.get("RISK_INSTRUMENT_MAPPINGS_JSON", "").strip()
        if not raw_mappings:
            raise RiskExternalRuntimeError("RISK_INSTRUMENT_MAPPINGS_JSON is required")
        try:
            items = json.loads(raw_mappings)
        except json.JSONDecodeError as exc:
            raise RiskExternalRuntimeError("RISK_INSTRUMENT_MAPPINGS_JSON is invalid JSON") from exc
        if not isinstance(items, list):
            raise RiskExternalRuntimeError("RISK_INSTRUMENT_MAPPINGS_JSON must be a list")

        mappings: list[InstrumentMapping] = []
        for item in items:
            if not isinstance(item, dict):
                raise RiskExternalRuntimeError("instrument mapping entries must be objects")
            try:
                mappings.append(
                    InstrumentMapping(
                        broker_symbol=str(item["broker_symbol"]),
                        instrument_id=UUID(str(item["instrument_id"])),
                        instrument_type=str(item.get("instrument_type", "EQUITY")),
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                raise RiskExternalRuntimeError("invalid instrument mapping entry") from exc

        raw_stress = os.environ.get("RISK_STRESS_SCENARIOS_JSON", "{}")
        try:
            stress_scenarios = json.loads(raw_stress)
        except json.JSONDecodeError as exc:
            raise RiskExternalRuntimeError("RISK_STRESS_SCENARIOS_JSON is invalid JSON") from exc
        if not isinstance(stress_scenarios, dict):
            raise RiskExternalRuntimeError("RISK_STRESS_SCENARIOS_JSON must be an object")

        confidence = float(os.environ.get("RISK_VAR_CONFIDENCE", "0.99"))
        return cls(
            fund_id=required_uuid("RISK_FUND_ID"),
            book_id=UUID(os.environ["RISK_BOOK_ID"]) if os.environ.get("RISK_BOOK_ID") else None,
            strategy_version_id=(
                UUID(os.environ["RISK_STRATEGY_VERSION_ID"])
                if os.environ.get("RISK_STRATEGY_VERSION_ID")
                else None
            ),
            as_of=as_of or datetime.now(timezone.utc),
            mappings=tuple(mappings),
            stress_scenarios=stress_scenarios,
            confidence=confidence,
            kill_switch_state=KillSwitchState(
                os.environ.get("RISK_KILL_SWITCH_STATE", KillSwitchState.ENABLED)
            ),
        )


@dataclass(frozen=True)
class ExternalRiskAssessment:
    snapshot: P1RiskSnapshot
    gate: P1GateDecision
    source: str = "ls-openapi"

    @property
    def binding(self) -> bool:
        """Whether the deterministic gate has enough healthy data to bind."""

        return self.gate is P1GateDecision.PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "binding": self.binding,
            "gate": self.gate.value,
            "snapshot": {
                "fund_id": str(self.snapshot.fund_id),
                "book_id": str(self.snapshot.book_id) if self.snapshot.book_id else None,
                "as_of": self.snapshot.as_of.isoformat(),
                "gross_exposure": self.snapshot.gross_exposure,
                "net_exposure": self.snapshot.net_exposure,
                "value_at_risk": self.snapshot.value_at_risk,
                "expected_shortfall": self.snapshot.expected_shortfall,
                "stress_losses": dict(self.snapshot.stress_losses),
                "correlation_shock_loss": self.snapshot.correlation_shock_loss,
                "correlation_max": self.snapshot.correlation_max,
                "quality_status": self.snapshot.quality_status,
                "input_hash": self.snapshot.input_hash,
                "calculation_version": self.snapshot.calculation_version,
                "kill_switch_state": self.snapshot.kill_switch_state.value,
                "breaches": list(self.snapshot.breaches),
            },
        }


def collect_external_assessment(
    client: LSOpenAPIClient,
    config: ExternalRiskRuntimeConfig,
    *,
    returns_by_symbol: Mapping[str, Sequence[float]] | None = None,
) -> ExternalRiskAssessment:
    """Collect point-in-time LS inputs and run the deterministic P1 gate."""

    collected = collect_ls_inputs(
        client,
        mappings=config.mappings,
        returns_by_symbol=returns_by_symbol,
    )
    engine = RiskP1Engine(config.mappings)
    snapshot = engine.build_snapshot(
        fund_id=config.fund_id,
        book_id=config.book_id,
        strategy_version_id=config.strategy_version_id,
        as_of=config.as_of,
        equity=collected.equity,
        positions=collected.positions,
        market=collected.market,
        stress_scenarios=config.stress_scenarios,
        confidence=config.confidence,
        kill_switch_state=config.kill_switch_state,
    )
    return ExternalRiskAssessment(snapshot=snapshot, gate=evaluate_p1_gate(snapshot))


def collect_external_assessment_from_env(
    *,
    config: ExternalRiskRuntimeConfig | None = None,
    returns_by_symbol: Mapping[str, Sequence[float]] | None = None,
) -> ExternalRiskAssessment:
    """Run the production adapter with explicit environment configuration."""

    runtime_config = config or ExternalRiskRuntimeConfig.from_env()
    with LSOpenAPIClient.from_env() as client:
        return collect_external_assessment(
            client,
            runtime_config,
            returns_by_symbol=returns_by_symbol,
        )
