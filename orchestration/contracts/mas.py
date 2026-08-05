"""Deterministic MAS contracts for analysis, handoff, validation and replay.

LLM output is advisory text only.  Every cross-department boundary receives a
validated object, and a validation failure becomes a safe downstream state
instead of an implicit approval.  This module intentionally has no network,
database, broker or ledger dependency.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Direction(str, Enum):
    UP = "up"
    DOWN = "down"
    SIDE = "side"


class Horizon(str, Enum):
    T1 = "T1"
    T5 = "T5"
    T20 = "T20"


class EvidenceRef(_Contract):
    source: str = Field(min_length=1, max_length=32)
    ref: str = Field(min_length=1, max_length=256)
    as_of: datetime | None = None

    @model_validator(mode="after")
    def validate_as_of(self) -> "EvidenceRef":
        if self.as_of is not None and self.as_of.tzinfo is None:
            raise ValueError("evidence.as_of must be timezone-aware")
        return self


class Signal(_Contract):
    signal_type: str = Field(min_length=1, max_length=64)
    direction: Direction
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)
    risk_flags: tuple[str, ...] = ()
    horizon: Horizon = Horizon.T5


class AnalysisOutput(_Contract):
    schema_id: str = "mas.analysis.v1"
    run_id: str = Field(min_length=1)
    as_of: datetime
    asset_code: str | None = Field(default=None, min_length=1, max_length=32)
    signals: tuple[Signal, ...] = Field(min_length=1)
    assumptions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_as_of(self) -> "AnalysisOutput":
        if self.as_of.tzinfo is None:
            raise ValueError("analysis.as_of must be timezone-aware")
        return self


class ProbabilityDistribution(_Contract):
    up: float = Field(ge=0.0, le=1.0)
    down: float = Field(ge=0.0, le=1.0)
    side: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def sums_to_one(self) -> "ProbabilityDistribution":
        if abs((self.up + self.down + self.side) - 1.0) > 0.001:
            raise ValueError("prediction probabilities must sum to 1 ± 0.001")
        return self


class PredictionOutput(_Contract):
    schema_id: str = "mas.prediction.v1"
    run_id: str = Field(min_length=1)
    as_of: datetime
    horizons: dict[Horizon, ProbabilityDistribution]
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_horizons(self) -> "PredictionOutput":
        required = set(Horizon)
        if set(self.horizons) != required:
            raise ValueError("prediction requires exactly T1, T5 and T20 horizons")
        if self.as_of.tzinfo is None:
            raise ValueError("prediction.as_of must be timezone-aware")
        return self


ACTIONS: frozenset[str] = frozenset(
    {
        "close",
        "reduce_40",
        "reduce_20",
        "hold",
        "increase_20",
        "increase_40",
        "increase_upper_limit",
    }
)


class DecisionOutput(_Contract):
    schema_id: str = "mas.decision.v1"
    run_id: str = Field(min_length=1)
    as_of: datetime
    asset_code: str = Field(min_length=1, max_length=32)
    action: str
    rationale: tuple[str, ...] = Field(min_length=1)
    constraints_applied: tuple[str, ...] = ()
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_action(self) -> "DecisionOutput":
        if self.action not in ACTIONS:
            raise ValueError(f"action must be one of {sorted(ACTIONS)}")
        if self.as_of.tzinfo is None:
            raise ValueError("decision.as_of must be timezone-aware")
        return self


class DepartmentHandoff(_Contract):
    schema_id: str = "mas.department-handoff.v1"
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    from_department: str = Field(min_length=1, max_length=64)
    to_department: str = Field(min_length=1, max_length=64)
    from_role: str = Field(min_length=1, max_length=96)
    to_role: str = Field(min_length=1, max_length=96)
    input_contract: str = Field(min_length=1, max_length=128)
    output_contract: str = Field(min_length=1, max_length=128)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: str = Field(min_length=1, max_length=256)
    read_only: bool = True
    binding: bool = False
    as_of: datetime

    @model_validator(mode="after")
    def heads_only_and_safe(self) -> "DepartmentHandoff":
        if not self.from_role.endswith(":head"):
            raise ValueError("cross-department handoff sender must be a department head")
        if not self.to_role.endswith(":head"):
            raise ValueError("cross-department handoff receiver must be a department head")
        if self.binding:
            raise ValueError("advisory MAS handoffs cannot be binding")
        if not self.read_only:
            raise ValueError("advisory MAS handoffs must be read-only")
        if self.as_of.tzinfo is None:
            raise ValueError("handoff.as_of must be timezone-aware")
        return self


class WorkerContextOutput(_Contract):
    """Compatibility contract for every independent Worker graph."""

    worker_id: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_refs: tuple[str, ...] = ()
    escalate: bool = False
    schema_valid: bool = True

    @model_validator(mode="after")
    def require_evidence_or_escalation(self) -> "WorkerContextOutput":
        if not self.evidence_refs and not self.escalate:
            raise ValueError("worker output without evidence must escalate")
        if not self.schema_valid:
            raise ValueError("worker output marked schema_valid=false")
        return self


class PipelineEvent(_Contract):
    schema_id: str = "mas.pipeline-event.v1"
    event_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1, max_length=64)
    stage: str = Field(min_length=1, max_length=64)
    department: str = Field(min_length=1, max_length=96)
    worker_id: str | None = None
    status: str = Field(min_length=1, max_length=32)
    input_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_contract: str | None = None
    retry_count: int = Field(default=0, ge=0)
    safe_action: str | None = None
    occurred_at: datetime
    summary: str = Field(default="", max_length=4000)
    payload_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_time(self) -> "PipelineEvent":
        if self.occurred_at.tzinfo is None:
            raise ValueError("pipeline event timestamp must be timezone-aware")
        return self


class ConflictResolution(_Contract):
    schema_id: str = "mas.signal-conflict.v1"
    final_direction: Direction
    final_score: float = Field(ge=-1.0, le=1.0)
    score_breakdown: dict[str, float]
    stop_rule_triggered: bool
    stop_rule: str | None = None
    reason: str = Field(min_length=1)


class ReplayMetadata(_Contract):
    schema_id: str = "mas.replay.v1"
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    replayable: bool
    replay_scope: str = Field(min_length=1)
    excludes: tuple[str, ...] = ()


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def stable_hash(value: Any) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def validate_worker_context(value: Mapping[str, Any]) -> WorkerContextOutput:
    """Validate generic Worker output at the department boundary."""

    return WorkerContextOutput.model_validate(value)


def resolve_signal_conflict(signals: Sequence[Signal]) -> ConflictResolution:
    """Apply deterministic priority, weighted consensus and a stop rule.

    The weights are intentionally explicit and easy to replace with a versioned
    configuration.  Disclosure evidence has priority over lower-quality news;
    score disagreement or weak evidence results in HOLD semantics.
    """

    weights = {
        "disclosure": 0.35,
        "announcement": 0.35,
        "macro": 0.25,
        "market": 0.25,
        "news": 0.15,
        "event": 0.15,
        "technical": 0.25,
        "price_momentum": 0.25,
    }
    score_breakdown: dict[str, float] = {}
    total_weight = 0.0
    for signal in signals:
        direction_score = {Direction.UP: 1.0, Direction.DOWN: -1.0, Direction.SIDE: 0.0}[signal.direction]
        weight = weights.get(signal.signal_type.lower(), 0.20)
        contribution = direction_score * signal.confidence * weight
        score_breakdown[signal.signal_type] = score_breakdown.get(signal.signal_type, 0.0) + contribution
        total_weight += weight
    final_score = sum(score_breakdown.values()) / total_weight if total_weight else 0.0
    evidence_missing = any(not signal.evidence for signal in signals)
    low_confidence = bool(signals) and sum(signal.confidence for signal in signals) / len(signals) < 0.45
    weak_consensus = abs(final_score) < 0.15
    stop = not signals or evidence_missing or low_confidence or weak_consensus
    if stop:
        direction = Direction.SIDE
        reason = "signal_conflict_or_insufficient_evidence"
        stop_rule = "HOLD_ON_WEAK_CONSENSUS"
    else:
        direction = Direction.UP if final_score > 0 else Direction.DOWN
        reason = "weighted_consensus"
        stop_rule = None
    return ConflictResolution(
        final_direction=direction,
        final_score=round(final_score, 8),
        score_breakdown={key: round(value, 8) for key, value in score_breakdown.items()},
        stop_rule_triggered=stop,
        stop_rule=stop_rule,
        reason=reason,
    )


def build_replay_metadata(
    profile: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    result: Mapping[str, Any],
) -> ReplayMetadata:
    """Create a credential-free replay pointer for one advisory run."""

    input_hash = stable_hash({"profile": profile, "candidates": candidates})
    replay_result = {
        key: value
        for key, value in result.items()
        if key not in {"pipeline_events", "replay", "generated_at"}
    }
    data_context = result.get("data_context", {})
    source = result.get("data_source")
    if source is None and isinstance(data_context, Mapping):
        source = data_context.get("source", "TEST")
    return ReplayMetadata(
        input_hash=input_hash,
        output_hash=stable_hash(replay_result),
        replayable=str(source or "TEST").upper() == "TEST",
        replay_scope="contract_and_deterministic_fixture",
        excludes=("credentials", "broker_state", "live_market_data", "pipeline_events"),
    )


def make_pipeline_event(
    *,
    event_id: str,
    run_id: str,
    event: Mapping[str, Any],
    occurred_at: datetime | None = None,
) -> PipelineEvent:
    """Normalize an internal callback event into the audit envelope."""

    stage = str(event.get("stage", "unknown"))
    department = str(event.get("department", stage))
    return PipelineEvent(
        event_id=event_id,
        run_id=run_id,
        event_type=str(event.get("kind", "pipeline_event")),
        stage=stage,
        department=department,
        worker_id=str(event["worker_id"]) if event.get("worker_id") else None,
        status=str(event.get("status", "RUNNING")),
        input_hash=str(event["input_hash"]) if event.get("input_hash") else None,
        output_contract=str(event["output_contract"]) if event.get("output_contract") else None,
        retry_count=int(event.get("attempts", event.get("retry_count", 0)) or 0),
        safe_action=str(event["safe_action"]) if event.get("safe_action") else None,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        summary=str(event.get("summary", event.get("message", "")))[:4000],
        payload_hash=stable_hash(event),
    )


__all__ = [
    "ACTIONS",
    "AnalysisOutput",
    "ConflictResolution",
    "DecisionOutput",
    "DepartmentHandoff",
    "Direction",
    "EvidenceRef",
    "Horizon",
    "PipelineEvent",
    "PredictionOutput",
    "ProbabilityDistribution",
    "ReplayMetadata",
    "Signal",
    "WorkerContextOutput",
    "build_replay_metadata",
    "make_pipeline_event",
    "resolve_signal_conflict",
    "stable_hash",
    "validate_worker_context",
]
