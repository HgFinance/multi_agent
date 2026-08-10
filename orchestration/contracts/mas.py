"""Deterministic MAS contracts for analysis, handoff, validation and replay.

LLM output is advisory text only.  Every cross-department boundary receives a
validated object, and a validation failure becomes a safe downstream state
instead of an implicit approval.  This module intentionally has no network,
database, broker or ledger dependency.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TASK_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"

TASK_DEPARTMENTS = frozenset(
    {
        "ceo-agent",
        "research-department",
        "trading-department",
        "risk-management",
        "quant-backtest-department",
        "accounting-portfolio-department",
        "qa-department",
        "hr-department",
    }
)

TASK_RESULT_STATUSES = frozenset(
    {"COMPLETED", "DEGRADED", "HOLD", "ESCALATED", "REJECTED", "NOT_EXECUTED"}
)


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
    def validate_as_of(self) -> EvidenceRef:
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
    def validate_as_of(self) -> AnalysisOutput:
        if self.as_of.tzinfo is None:
            raise ValueError("analysis.as_of must be timezone-aware")
        return self


class ProbabilityDistribution(_Contract):
    up: float = Field(ge=0.0, le=1.0)
    down: float = Field(ge=0.0, le=1.0)
    side: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def sums_to_one(self) -> ProbabilityDistribution:
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
    def validate_horizons(self) -> PredictionOutput:
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
    def validate_action(self) -> DecisionOutput:
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
    def heads_only_and_safe(self) -> DepartmentHandoff:
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
    def require_evidence_or_escalation(self) -> WorkerContextOutput:
        if not self.evidence_refs and not self.escalate:
            raise ValueError("worker output without evidence must escalate")
        if not self.schema_valid:
            raise ValueError("worker output marked schema_valid=false")
        return self


class TaskArtifactRef(_Contract):
    """Mirrors agent-task-result.v1's $defs/artifact_ref. Never carries a raw
    prompt or secret — only a content-addressed pointer."""

    type: str = Field(min_length=1, max_length=64)
    id: str = Field(min_length=1, max_length=128, pattern=TASK_ID_PATTERN)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    as_of: datetime | None = None
    provenance_ref: str | None = Field(default=None, min_length=1, max_length=128, pattern=TASK_ID_PATTERN)
    acl_scope: tuple[str, ...] = ()


class AgentTaskResult(_Contract):
    """docs/02-engineering/contracts/agent-task-result.v1.json.

    The non-binding domain result a Department Head/Runner hands back to
    CEO/Kanban. Only the deterministic Risk Engine/OMS/Ledger/Approval
    Service can bind financial state — this object cannot.
    """

    schema_version: Literal["agent-task-result.v1"] = "agent-task-result.v1"
    case_id: str = Field(min_length=1, max_length=128, pattern=TASK_ID_PATTERN)
    task_id: str = Field(min_length=1, max_length=128, pattern=TASK_ID_PATTERN)
    department: str
    worker: str = Field(min_length=1, max_length=128)
    input_refs: tuple[TaskArtifactRef, ...] = ()
    evidence_refs: tuple[TaskArtifactRef, ...] = ()
    decision: str = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0.0, le=1.0)
    escalate: bool
    model_version: str = Field(min_length=1, max_length=128)
    adapter_version: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128, pattern=TASK_ID_PATTERN)
    status: str
    summary: str = Field(min_length=1, max_length=4000)
    reason_codes: tuple[str, ...] = ()
    output_refs: tuple[TaskArtifactRef, ...] = ()
    input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    calculation_version: str | None = Field(default=None, min_length=1, max_length=128)
    replay_manifest_ref: str | None = Field(default=None, min_length=1, max_length=128, pattern=TASK_ID_PATTERN)
    created_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_enums_and_time(self) -> AgentTaskResult:
        if self.department not in TASK_DEPARTMENTS:
            raise ValueError(f"unknown department: {self.department}")
        if self.status not in TASK_RESULT_STATUSES:
            raise ValueError(f"unknown task status: {self.status}")
        if self.created_at.tzinfo is None:
            raise ValueError("agent-task-result.created_at must be timezone-aware")
        if self.completed_at is not None and self.completed_at.tzinfo is None:
            raise ValueError("agent-task-result.completed_at must be timezone-aware")
        return self


WORKER_CONTEXT_SCHEMA_VERSIONS = frozenset(
    {
        "worker-context.v1",
        "research.worker-context.v1",
        "quant.worker-context.v1",
        "trading.worker-context.v1",
        "risk.worker-context.v1",
        "qa.worker-context.v1",
        "governance.worker-context.v1",
        "workforce.worker-context.v1",
        "accounting.worker-context.v1",
    }
)


class WorkerAdvisory(_Contract):
    summary: str = Field(min_length=1, max_length=4000)
    suggested_verdict: str | None = Field(default=None, min_length=1, max_length=64)


class WorkerContextResult(_Contract):
    """docs/02-engineering/contracts/worker-context.v1.json.

    The non-binding execution context a Runner hands to its Worker and the
    Worker hands back — never a confidence/escalate field (§5.1.1: those are
    Task-level aggregates the Department Head computes across every
    worker-context result, not something a single Worker call carries).
    """

    context_id: str = Field(min_length=1, max_length=128, pattern=TASK_ID_PATTERN)
    schema_version: str
    case_id: str = Field(min_length=1, max_length=128, pattern=TASK_ID_PATTERN)
    task_id: str = Field(min_length=1, max_length=128, pattern=TASK_ID_PATTERN)
    department_handoff_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=TASK_ID_PATTERN)
    input_contract: str = Field(min_length=1, max_length=128)
    department: str
    trace_id: str = Field(min_length=1, max_length=128, pattern=TASK_ID_PATTERN)
    producer_worker: str = Field(min_length=1, max_length=128)
    consumer_worker: str = Field(min_length=1, max_length=128)
    status: str
    advisory: WorkerAdvisory
    reason_codes: tuple[str, ...] = ()
    input_refs: tuple[TaskArtifactRef, ...] = Field(min_length=1)
    output_refs: tuple[TaskArtifactRef, ...] = ()
    profile_version: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=128)
    adapter_version: str = Field(min_length=1, max_length=128)
    input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    calculation_version: str | None = Field(default=None, min_length=1, max_length=128)
    attempt: int = Field(ge=1, le=3)
    timeout_ms: int = Field(ge=1, le=120_000)
    replay_manifest_ref: str | None = Field(default=None, min_length=1, max_length=128, pattern=TASK_ID_PATTERN)
    created_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_enums_and_time(self) -> WorkerContextResult:
        if self.schema_version not in WORKER_CONTEXT_SCHEMA_VERSIONS:
            raise ValueError(f"unknown worker-context schema_version: {self.schema_version}")
        if self.department not in TASK_DEPARTMENTS:
            raise ValueError(f"unknown department: {self.department}")
        if self.status not in TASK_RESULT_STATUSES:
            raise ValueError(f"unknown worker-context status: {self.status}")
        if self.created_at.tzinfo is None:
            raise ValueError("worker-context.created_at must be timezone-aware")
        if self.completed_at is not None and self.completed_at.tzinfo is None:
            raise ValueError("worker-context.completed_at must be timezone-aware")
        return self


def build_worker_context_result(
    *,
    schema_version: str,
    case_id: str,
    task_id: str,
    input_contract: str,
    department: str,
    trace_id: str,
    producer_worker: str,
    consumer_worker: str,
    status: str,
    summary: str,
    input_refs: Sequence[TaskArtifactRef],
    profile_version: str,
    model_version: str,
    input_hash: str,
    context_id: str | None = None,
    output_refs: Sequence[TaskArtifactRef] = (),
    adapter_version: str = "none",
    suggested_verdict: str | None = None,
    reason_codes: Sequence[str] = (),
    attempt: int = 1,
    timeout_ms: int = 8_000,
    output_hash: str | None = None,
    created_at: datetime | None = None,
) -> WorkerContextResult:
    """Build a single Runner→Worker call's worker-context.v1 record.

    `context_id` defaults to a deterministic id (a Task can fan out to many
    Worker calls, so it must differ per case_id/task_id/consumer_worker/trace_id
    combination — §5.1.1). Caller resolves `status` beforehand; this builder
    never infers a decision from Worker text.
    """

    return WorkerContextResult(
        context_id=context_id
        or f"ctx:{stable_hash({'case_id': case_id, 'task_id': task_id, 'consumer_worker': consumer_worker, 'trace_id': trace_id})[:32]}",
        schema_version=schema_version,
        case_id=case_id,
        task_id=task_id,
        input_contract=input_contract,
        department=department,
        trace_id=trace_id,
        producer_worker=producer_worker,
        consumer_worker=consumer_worker,
        status=status,
        advisory=WorkerAdvisory(summary=summary[:4000], suggested_verdict=suggested_verdict),
        reason_codes=tuple(reason_codes),
        input_refs=tuple(input_refs),
        output_refs=tuple(output_refs),
        profile_version=profile_version,
        model_version=model_version,
        adapter_version=adapter_version,
        input_hash=f"sha256:{input_hash}",
        output_hash=output_hash,
        attempt=attempt,
        timeout_ms=timeout_ms,
        created_at=created_at or datetime.now(timezone.utc),
    )


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
    # Keep the validated head-to-head boundary in the audit envelope so the
    # runtime projection cannot represent a peer/worker call as a department
    # handoff.
    handoff: DepartmentHandoff | None = None

    @model_validator(mode="after")
    def validate_time(self) -> PipelineEvent:
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
        handoff=(
            DepartmentHandoff.model_validate(event["handoff"])
            if isinstance(event.get("handoff"), Mapping)
            else None
        ),
    )


# Decision -> Task Status. A department head's own decision vocabulary
# (Risk: APPROVE/ESCALATE/REJECT, QA: PASS/WARN/FAIL/ESCALATE, ...) already
# matches FINAL_RUNTIME_ARCHITECTURE.md's §5.3 vocabulary, so only the Task
# Status projection needs mapping. Anything outside this table escalates
# rather than completing (fail-closed, never widen into a success state).
TASK_DECISION_STATUS: dict[str, str] = {
    "APPROVE": "COMPLETED",
    "PASS": "COMPLETED",
    "NO_ACTION": "COMPLETED",
    "RESIZE": "COMPLETED",
    "RECOMMEND": "COMPLETED",
    "HOLD": "HOLD",
    "WARN": "DEGRADED",
    "ESCALATE": "ESCALATED",
    "REJECT": "REJECTED",
    "FAIL": "REJECTED",
    "NOT_EXECUTED": "NOT_EXECUTED",
}


def task_status_for_decision(decision: str) -> str:
    return TASK_DECISION_STATUS.get(decision, "ESCALATED")


def build_agent_task_result(
    *,
    department: str,
    worker: str,
    case_id: str,
    task_id: str,
    trace_id: str,
    decision: str,
    escalate: bool,
    summary: str,
    reports: Sequence[Mapping[str, Any]],
    model_version: str,
    adapter_version: str = "none",
    reason_codes: Sequence[str] = (),
    created_at: datetime | None = None,
) -> AgentTaskResult:
    """Build a Department Head's non-binding agent-task-result.v1 from its own
    deterministic worker-fan-in reports (each report already carries a plain
    hex input_hash from the department's skills/contracts.py helpers)."""

    refs = tuple(
        TaskArtifactRef(
            type="worker-report",
            id=f"{report.get('worker_id', 'worker')}:{trace_id}",
            content_hash=f"sha256:{report['input_hash']}",
        )
        for report in reports
        if report.get("input_hash")
    )
    input_hash = reports[0]["input_hash"] if reports and reports[0].get("input_hash") else stable_hash(case_id)
    # ponytail: confidence is a binary proxy from the head's own escalate
    # flag (these fan-ins are deterministic, not model-scored). Replace with
    # real evidence-coverage scoring once Kanban ranks on this value.
    confidence = 0.5 if escalate else 1.0
    return AgentTaskResult(
        case_id=case_id,
        task_id=task_id,
        department=department,
        worker=worker,
        input_refs=refs,
        evidence_refs=refs,
        decision=decision,
        confidence=confidence,
        escalate=escalate,
        model_version=model_version,
        adapter_version=adapter_version,
        trace_id=trace_id,
        status=task_status_for_decision(decision),
        summary=summary[:4000],
        reason_codes=tuple(reason_codes),
        output_refs=refs,
        input_hash=f"sha256:{input_hash}",
        output_hash=f"sha256:{stable_hash({'decision': decision, 'reports': list(reports)})}",
        created_at=created_at or datetime.now(timezone.utc),
    )


__all__ = [
    "ACTIONS",
    "TASK_DECISION_STATUS",
    "TASK_DEPARTMENTS",
    "TASK_RESULT_STATUSES",
    "WORKER_CONTEXT_SCHEMA_VERSIONS",
    "AgentTaskResult",
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
    "TaskArtifactRef",
    "WorkerAdvisory",
    "WorkerContextOutput",
    "WorkerContextResult",
    "build_agent_task_result",
    "build_replay_metadata",
    "build_worker_context_result",
    "make_pipeline_event",
    "resolve_signal_conflict",
    "stable_hash",
    "task_status_for_decision",
    "validate_worker_context",
]
