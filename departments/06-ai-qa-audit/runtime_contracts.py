"""QA-owned canonical runtime contracts and compatibility adapters.

This module deliberately has no imports from a worker, API, event bus, model,
or persistence package.  It is the wire boundary for the QA runner only.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal, Mapping, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_HASH_RE = r"^sha256:[0-9a-f]{64}$"


class ContractModel(BaseModel):
    """Strict models: unknown fields are rejected rather than silently dropped."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    DEGRADED = "DEGRADED"
    HOLD = "HOLD"
    ESCALATED = "ESCALATED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class WorkerStatus(StrEnum):
    COMPLETED = "COMPLETED"
    DEGRADED = "DEGRADED"
    HOLD = "HOLD"
    ESCALATED = "ESCALATED"
    REJECTED = "REJECTED"
    NOT_EXECUTED = "NOT_EXECUTED"


class ErrorCode(StrEnum):
    MISSING_INPUT = "MISSING_INPUT"
    INVALID_INPUT = "INVALID_INPUT"
    TOOLCALL_DENIED = "TOOLCALL_DENIED"
    TOOL_FAILURE = "TOOL_FAILURE"
    EVIDENCE_FAILURE = "EVIDENCE_FAILURE"
    TIMEOUT = "TIMEOUT"
    OOM = "OOM"
    CRASHED = "CRASHED"
    SCHEMA_FAILURE = "SCHEMA_FAILURE"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    EVAL_SET_MISMATCH = "EVAL_SET_MISMATCH"
    UNSUPPORTED_ENVIRONMENT = "UNSUPPORTED_ENVIRONMENT"
    CANDIDATE_FAILURE = "CANDIDATE_FAILURE"


class ArtifactRef(ContractModel):
    type: str = Field(min_length=1, max_length=64)
    id: str = Field(min_length=1, max_length=128, pattern=_ID_RE)
    content_hash: str = Field(pattern=_HASH_RE)
    as_of: datetime | None = None
    provenance_ref: str | None = Field(default=None, max_length=128, pattern=_ID_RE)
    acl_scope: list[str] = Field(default_factory=list)

    @field_validator("acl_scope")
    @classmethod
    def unique_acl_scope(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("acl_scope must contain unique values")
        if any(not item or len(item) > 128 for item in value):
            raise ValueError("acl_scope entries must be non-empty and <= 128 chars")
        return value


class AgentTaskContext(ContractModel):
    """The checked-in ``agent-task-context.v1`` contract."""

    schema_version: Literal["agent-task-context.v1"]
    case_id: str = Field(min_length=1, max_length=128, pattern=_ID_RE)
    task_id: str = Field(min_length=1, max_length=128, pattern=_ID_RE)
    parent_task_id: str | None = Field(default=None, max_length=128, pattern=_ID_RE)
    department_handoff_id: str | None = Field(default=None, max_length=128, pattern=_ID_RE)
    department: Literal[
        "ceo-agent",
        "research-department",
        "trading-department",
        "risk-management",
        "quant-backtest-department",
        "accounting-portfolio-department",
        "qa-department",
        "hr-department",
    ]
    worker: str = Field(min_length=1, max_length=128)
    route: str = Field(min_length=1, max_length=64)
    input_refs: list[ArtifactRef] = Field(min_length=1)
    trace_id: str = Field(min_length=1, max_length=128, pattern=_ID_RE)
    status: TaskStatus
    attempt: int = Field(ge=1, le=3)
    priority: int = Field(default=50, ge=0, le=100)
    idempotency_key: str = Field(min_length=1, max_length=256)
    created_at: datetime
    updated_at: datetime
    @field_validator("input_refs")
    @classmethod
    def unique_input_refs(cls, value: list[ArtifactRef]) -> list[ArtifactRef]:
        keys = [(item.type, item.id) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("input_refs must contain unique artifacts")
        return value

    @model_validator(mode="after")
    def timestamps_are_aware(self) -> "AgentTaskContext":
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("created_at and updated_at must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class WorkerContext(ContractModel):
    """The checked-in ``worker-context.v1`` contract."""

    context_id: str = Field(min_length=1, max_length=128, pattern=_ID_RE)
    schema_version: Literal["worker-context.v1", "qa.worker-context.v1"]
    case_id: str = Field(min_length=1, max_length=128, pattern=_ID_RE)
    task_id: str = Field(min_length=1, max_length=128, pattern=_ID_RE)
    department_handoff_id: str | None = Field(default=None, max_length=128, pattern=_ID_RE)
    input_contract: str = Field(min_length=1, max_length=128)
    department: Literal[
        "ceo-agent",
        "research-department",
        "trading-department",
        "risk-management",
        "quant-backtest-department",
        "accounting-portfolio-department",
        "qa-department",
        "hr-department",
    ]
    trace_id: str = Field(min_length=1, max_length=128, pattern=_ID_RE)
    producer_worker: str = Field(min_length=1, max_length=128)
    consumer_worker: str = Field(min_length=1, max_length=128)
    status: WorkerStatus
    advisory: dict[str, str] = Field(min_length=1)
    reason_codes: list[str] = Field(default_factory=list)
    input_refs: list[ArtifactRef] = Field(min_length=1)
    output_refs: list[ArtifactRef] = Field(default_factory=list)
    profile_version: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=128)
    adapter_version: str = Field(min_length=1, max_length=128)
    input_hash: str = Field(pattern=_HASH_RE)
    output_hash: str | None = Field(default=None, pattern=_HASH_RE)
    calculation_version: str | None = Field(default=None, max_length=128)
    attempt: int = Field(ge=1, le=3)
    timeout_ms: int = Field(ge=1, le=120000)
    replay_manifest_ref: str | None = Field(default=None, max_length=128, pattern=_ID_RE)
    created_at: datetime
    completed_at: datetime | None = None

    @field_validator("reason_codes")
    @classmethod
    def unique_reason_codes(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("reason_codes must contain unique values")
        return value
    @field_validator("advisory")
    @classmethod
    def strict_advisory(cls, value: dict[str, str]) -> dict[str, str]:
        if "summary" not in value or not value["summary"]:
            raise ValueError("advisory.summary is required")
        if set(value) - {"summary", "suggested_verdict"}:
            raise ValueError("advisory has unknown fields")
        if any(not isinstance(item, str) or not item for item in value.values()):
            raise ValueError("advisory values must be non-empty strings")
        return value

    @field_validator("input_refs")
    @classmethod
    def unique_input_refs(cls, value: list[ArtifactRef]) -> list[ArtifactRef]:
        keys = [(item.type, item.id) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("input_refs must contain unique artifacts")
        return value

    @field_validator("output_refs")
    @classmethod
    def unique_output_refs(cls, value: list[ArtifactRef]) -> list[ArtifactRef]:
        keys = [(item.type, item.id) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("output_refs must contain unique artifacts")
        return value

    @model_validator(mode="after")
    def timestamps_are_aware(self) -> "WorkerContext":
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at cannot precede created_at")
        failure_codes = {code.value for code in ErrorCode}
        if self.status is WorkerStatus.COMPLETED and failure_codes.intersection(self.reason_codes):
            raise ValueError("a worker failure reason cannot be projected as COMPLETED")
        if self.department == "qa-department" and self.schema_version != "qa.worker-context.v1":
            raise ValueError("QA worker contexts must use qa.worker-context.v1")
        if self.input_contract != input_contract_for_department(self.department):
            raise ValueError("worker input_contract does not match department")
        return self


# Public aliases keep names used by callers that refer to the wire version.
AgentTaskContextV1 = AgentTaskContext
RuntimeAgentTaskContext = AgentTaskContext
RuntimeWorkerContext = WorkerContext
WorkerContextV1 = WorkerContext


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_hash(value: Any) -> str:
    """Hash canonical JSON as the contract's ``sha256:<hex>`` value."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_payload_hash(value: Any) -> str:
    """Return the single payload digest shared by every QA runtime projection."""

    return sha256_hash(value)


def normalize_status(value: str | TaskStatus | WorkerStatus) -> WorkerStatus:
    """Map internal/legacy status vocabulary to the wire worker vocabulary."""

    raw = value.value if isinstance(value, (TaskStatus, WorkerStatus)) else str(value)
    if raw in {"ESCALATE", "ESCALATED"}:
        return WorkerStatus.ESCALATED
    if raw == "SKIPPED_SAFE":
        return WorkerStatus.NOT_EXECUTED
    if raw in {item.value for item in WorkerStatus}:
        return WorkerStatus(raw)
    if raw in {"PASS", "COMPLETED"}:
        return WorkerStatus.COMPLETED
    if raw in {"WARN", "DEGRADED"}:
        return WorkerStatus.DEGRADED
    if raw in {"FAIL", "REJECTED"}:
        return WorkerStatus.REJECTED
    if raw in {"HOLD", "NOT_EXECUTED"}:
        return WorkerStatus(raw)
    # Unknown statuses are not successes.  Escalation is the safe projection.
    return WorkerStatus.ESCALATED


_STATUS_ORDER = {
    WorkerStatus.REJECTED: 6,
    WorkerStatus.ESCALATED: 5,
    WorkerStatus.HOLD: 4,
    WorkerStatus.DEGRADED: 3,
    WorkerStatus.NOT_EXECUTED: 2,
    WorkerStatus.COMPLETED: 1,
}


def reduce_status(statuses: Sequence[str | TaskStatus | WorkerStatus]) -> WorkerStatus:
    """Fail-closed fan-in reduction; no failure branch can become COMPLETED."""

    if not statuses:
        return WorkerStatus.NOT_EXECUTED
    return max((normalize_status(item) for item in statuses), key=lambda item: _STATUS_ORDER[item])


# Commonly used explicit name in older QA callers.
reduce_qa_status = reduce_status
def input_contract_for_department(department: str) -> str:
    contracts = {
        "ceo-agent": "ceo.department-input.v1",
        "research-department": "research.department-input.v1",
        "trading-department": "trading.department-input.v1",
        "risk-management": "risk.department-input.v1",
        "quant-backtest-department": "quant.department-input.v1",
        "accounting-portfolio-department": "accounting.department-input.v1",
        "qa-department": "qa.department-input.v1",
        "hr-department": "hr.department-input.v1",
    }
    try:
        return contracts[department]
    except KeyError as exc:
        raise ValueError(f"unsupported department: {department}") from exc



def to_worker_context(
    task: AgentTaskContext | Mapping[str, Any],
    *,
    producer_worker: str,
    profile_version: str,
    model_version: str,
    adapter_version: str,
    status: str | TaskStatus | WorkerStatus,
    advisory: Mapping[str, str] | None = None,
    decision: str | None = None,
    reason: str | None = None,
    error_code: str | ErrorCode | None = None,
    reason_codes: Sequence[str] = (),
    output_refs: Sequence[ArtifactRef | Mapping[str, Any]] = (),
    input_hash: str | None = None,
    output_hash: str | None = None,
    calculation_version: str | None = None,
    timeout_ms: int = 30000,
    attempt: int | None = None,
    context_id: str | None = None,
    replay_manifest_ref: str | None = None,
    clock: Any = utc_now,
) -> WorkerContext:
    """Adapt a task envelope to a strict worker envelope without inventing refs."""

    task_model = task if isinstance(task, AgentTaskContext) else AgentTaskContext.model_validate(task)
    refs = [item if isinstance(item, ArtifactRef) else ArtifactRef.model_validate(item) for item in output_refs]
    now = clock()
    advisory_data = dict(advisory or {})
    if reason and "summary" not in advisory_data:
        advisory_data["summary"] = reason
    if "summary" not in advisory_data:
        advisory_data["summary"] = "QA worker completed"
    if decision is not None:
        advisory_data.setdefault("suggested_verdict", decision)
    codes = list(dict.fromkeys(str(code) for code in reason_codes))
    if error_code is not None:
        code_text = str(error_code)
        if code_text not in codes:
            codes.append(code_text)
    normalized_status = normalize_status(status)
    if error_code is not None and normalized_status is WorkerStatus.COMPLETED:
        # Error-bearing contexts are never allowed to look like a PASS.
        normalized_status = WorkerStatus.ESCALATED
    return WorkerContext(
        schema_version="qa.worker-context.v1",
        context_id=context_id or f"{task_model.task_id}:{uuid4().hex}",
        case_id=task_model.case_id,
        task_id=task_model.task_id,
        department_handoff_id=task_model.department_handoff_id,
        input_contract=input_contract_for_department(task_model.department),
        department=task_model.department,
        trace_id=task_model.trace_id,
        producer_worker=producer_worker,
        consumer_worker=task_model.worker,
        status=normalized_status,
        advisory=advisory_data,
        reason_codes=codes,
        input_refs=list(task_model.input_refs),
        output_refs=refs,
        profile_version=profile_version,
        model_version=model_version,
        adapter_version=adapter_version,
        input_hash=input_hash or sha256_hash(task_model.input_refs),
        output_hash=output_hash,
        calculation_version=calculation_version,
        attempt=attempt or task_model.attempt,
        timeout_ms=timeout_ms,
        replay_manifest_ref=replay_manifest_ref,
        created_at=now,
        completed_at=now,
    )
adapt_task_to_worker = to_worker_context


def legacy_worker_projection(context: WorkerContext) -> dict[str, Any]:
    """Return the stable subset expected by the pre-runtime QA façades."""

    return {
        "worker_id": context.consumer_worker,
        "department": "QA",
        "status": context.status.value,
        "reason_codes": list(context.reason_codes),
        "trace_id": context.trace_id,
        "case_id": context.case_id,
        "task_id": context.task_id,
        "input_refs": [ref.model_dump(mode="json") for ref in context.input_refs],
        "output_refs": [ref.model_dump(mode="json") for ref in context.output_refs],
        "profile_version": context.profile_version,
        "model_version": context.model_version,
        "adapter_version": context.adapter_version,
        "input_hash": context.input_hash,
        "output_hash": context.output_hash,
        "binding": False,
        "advisory": dict(context.advisory),
    }


__all__ = [
    "AgentTaskContext",
    "AgentTaskContextV1",
    "ArtifactRef",
    "ContractModel",
    "ErrorCode",
    "TaskStatus",
    "RuntimeAgentTaskContext",
    "RuntimeWorkerContext",
    "WorkerContext",
    "WorkerContextV1",
    "WorkerStatus",
    "input_contract_for_department",
    "adapt_task_to_worker",
    "legacy_worker_projection",
    "normalize_status",
    "reduce_qa_status",
    "reduce_status",
    "sha256_hash",
    "canonical_payload_hash",
    "to_worker_context",
    "utc_now",
]
