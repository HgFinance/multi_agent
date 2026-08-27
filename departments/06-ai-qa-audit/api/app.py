#!/usr/bin/env python3
"""QA Domain API — evidence_qa_engine.py/ops_health_monitor.py/trace_recorder.py/
tool_permission_check.py/incident_timeline.py/agentic-rag를 감싸는 FastAPI 래퍼.

소유: 동규 (AI QA/감사본부)
근거: docs/02-engineering/RISK_QA_DOMAIN_API_SPEC.md 3절, 8절 "다음 작업 제안 순서" (2)
      docs/02-engineering/TECH_STACK_DECISIONS.md 7절(Hermes는 Domain 서비스를 API/MCP
      경계로만 부른다)

여기엔 새 판정 로직이 없다 - 5개 결정론적 모듈이 이미 하는 일을 얇게 감싼다.

**`POST /investment-cases/{case_id}/qa-check`(3.1)는 스펙 문서에 "제안 — 팀 승인 필요"로
명시돼 있다.** `MINIMUM_SERVICE_UNIT_SPEC.md` §8/§11에 아직 `EvaluateEvidence` Command와
`qa_passed`/`qa_warned`/`qa_blocked` Event가 등록돼 있지 않다 - 여기 구현은 이미 완성된
`EvidenceQaEngine.check_artifact`를 감싸는 코드일 뿐이지만, 상위 문서(CLAUDE.md 문서 규칙
4번) 자체는 팀 승인 전까지 건드리지 않는다.

`context.evidence_store`는 스펙 3.1대로 요청에 안 받는다 - 이 프로세스 안의 `_evidence_store`
스텁을 쓴다(rag-librarian-evidence-curator 실연동 전까지).

`/qa/v1/evidence/check`는 OPENAI_API_KEY와 네트워크가 필요해 이 파일의 __main__ 점검에서는
뺐다(skills/agentic-rag 자체 점검이 이미 그 경로를 검증한다).

실행: uvicorn app:app --app-dir departments/06-ai-qa-audit/api
자체 점검: python departments/06-ai-qa-audit/api/app.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json

# Current contract status: qa-check is approved as Evidence QA Gate v1. The
# production flag and fail-closed corpus/worker-profile checks below are the
# authoritative runtime boundary; older proposal wording in this module docstring
# is retained only as historical context.
import os
import sys
from datetime import datetime
from decimal import Decimal
from functools import wraps
from pathlib import Path
from threading import Lock
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

_QA_DIR = Path(__file__).resolve().parent.parent
_EVIDENCE_DIR = _QA_DIR / "evidence"
_AUDIT_DIR = Path(__file__).resolve().parent.parent / "audit"
_AGENTIC_RAG_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent / "skills" / "agentic-rag"
)
# 저장소 루트. 아래 import 사슬이 `orchestration.contracts.mas`(qa_mandate_workers)
# 와 `apps.observability`를 쓰므로 **여기서** 넣어야 한다. 예전에는 415행에서 넣었는데
# 그 사슬을 처음 타는 import 는 99행이라 316줄 늦었다. risk-api 와 같은 고장으로,
# 컨테이너에서 `uvicorn` 콘솔 스크립트로 뜨면 audit-api·qa-worker 가 둘 다
# `ModuleNotFoundError: orchestration` 크래시 루프였다 - 2026-08-12 실측.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _configured_evidence_corpus() -> Path:
    """Resolve the operator-provided QA corpus without exposing its contents."""

    configured = os.environ.get("QA_EVIDENCE_CORPUS_DIR", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else _AGENTIC_RAG_DIR / "corpus" / "evidence"
    )


for _p in (_QA_DIR, _EVIDENCE_DIR, _AUDIT_DIR, _AGENTIC_RAG_DIR, _REPO_ROOT):
    sys.path.insert(0, str(_p))


def _load_qa_repository():
    """Load QA's repository without claiming the process-wide ``repository`` name.

    Workforce and accounting prototypes historically expose a top-level
    ``repository`` module.  Which implementation wins then depends on pytest
    collection order.  The QA API only needs this module's exported classes,
    so load its file under a stable, domain-owned namespace instead.
    """

    module_name = "hgfinance_qa_audit.repository"
    spec = importlib.util.spec_from_file_location(
        module_name, _QA_DIR / "repository.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - packaging error
        raise ImportError("cannot load the QA repository module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_qa_repository = _load_qa_repository()
from corpus_registry import inspect_policy_corpus
from eval_runner import (
    CandidateSpec,
    ChampionComparison,
    EvalCase,
    EvalRunner,
    EvalSet,
    InMemoryEvalAuditRepository,
)
from evidence_qa_engine import (
    Artifact,
    EvidenceQaEngine,
    EvidenceStore,
    QaContext,
)
from incident_timeline import (
    IncidentEntryType,
    IncidentTimeline,
    IncidentTimelineError,
)
from internal_audit import InternalAuditEngine
from model_risk import ModelRiskEngine, ModelRiskInput
from ops_health_monitor import (
    AgentHealthMetrics,
    OpsHealthMonitor,
    OpsThresholds,
)
from qa_events.redis_event_bus import (
    DEFAULT_GROUP,
    DEFAULT_STREAM,
    FORWARD_QA_DLQ_STREAM,
    FORWARD_QA_GROUP,
    FORWARD_QA_STREAM,
    INTRADAY_FORWARD_QA_REQUESTED_EVENT,
    QA_DECISION_EVENT,
    RISK_DECISION_EVENT,
    RISK_QA_DLQ_STREAM,
    QaEventBusError,
    QaEventPoisonError,
    RedisEventBus,
)
from qa_mandate_workers import QaVerificationRequest, assess_qa_verification
from tool_permission_check import (
    AgentToolPolicy,
    check_tool_permission,
    count_unauthorized_calls,
    record_and_check_tool_call,
)
from trace_recorder import TraceRecorder, TraceRecorderError

from orchestration.evolution_skills import (
    EvolutionSkillError,
    EvolutionSkillStore,
    build_resolution_report,
)
from orchestration.d5_improvement_pipeline import d5_regression_candidates
from orchestration.langsmith_feedback import FeedbackConfig, FeedbackLedger

# DATABASE_URL이 있을 때만 audit 및 QA Eval write-through을 활성화한다.
# Shadow import에서 DATABASE_URL이 없으면 어떤 PostgreSQL pool도 만들지 않는다.
_DATABASE_URL = (
    os.environ.get("RISK_QA_DATABASE_URL", "").strip()
    or os.environ.get("DATABASE_URL", "").strip()
)
_QA_RUNTIME = os.environ.get("RISK_QA_RUNTIME", "").strip().lower()
DomainEventConflict = _qa_repository.DomainEventConflict
ForwardQaRequestConflict = _qa_repository.ForwardQaRequestConflict
PostgresAuditRepository = _qa_repository.PostgresAuditRepository
QaDecisionPersistenceError = _qa_repository.QaDecisionPersistenceError

if _QA_RUNTIME == "test" or not _DATABASE_URL:
    _audit_repository = None
else:
    _audit_repository = PostgresAuditRepository.connect(_DATABASE_URL)
_event_bus: RedisEventBus | None = None
_forward_event_bus: RedisEventBus | None = None


def _qa_event_bus() -> RedisEventBus | None:
    """Risk↔QA Redis Stream을 실제 호출 시점에만 연결한다."""

    global _event_bus
    redis_url = os.environ.get("RISK_QA_EVENT_REDIS_URL") or os.environ.get("REDIS_URL")
    if not redis_url:
        return None
    if _event_bus is None:
        import redis

        try:
            dedupe_ttl_seconds = int(
                os.environ.get("RISK_QA_EVENT_DEDUPE_TTL_SECONDS", "604800")
            )
            max_delivery_attempts = int(
                os.environ.get("RISK_QA_EVENT_MAX_DELIVERIES", "5")
            )
        except ValueError as exc:
            raise QaEventBusError(
                "risk QA Redis numeric settings must be integers"
            ) from exc

        _event_bus = RedisEventBus(
            redis.Redis.from_url(redis_url),
            stream=os.environ.get("RISK_QA_EVENT_STREAM", DEFAULT_STREAM),
            group=os.environ.get("QA_EVENT_GROUP", DEFAULT_GROUP),
            consumer=os.environ.get("QA_EVENT_CONSUMER", "qa-api"),
            dedupe_ttl_seconds=dedupe_ttl_seconds,
            dead_letter_stream=(
                os.environ.get("RISK_QA_EVENT_DLQ_STREAM") or RISK_QA_DLQ_STREAM
            ),
            max_delivery_attempts=max_delivery_attempts,
            failure_prefix="risk-qa:event-failures:",
        )
    return _event_bus


def _forward_qa_event_bus() -> RedisEventBus | None:
    """Return the isolated quant-to-QA stream with poison-message DLQ."""

    global _forward_event_bus
    redis_url = os.environ.get("RISK_QA_EVENT_REDIS_URL") or os.environ.get(
        "REDIS_URL"
    )
    if not redis_url:
        return None
    if _forward_event_bus is None:
        import redis

        try:
            dedupe_ttl_seconds = int(
                os.environ.get("RISK_QA_EVENT_DEDUPE_TTL_SECONDS", "604800")
            )
            max_delivery_attempts = int(
                os.environ.get("QA_FORWARD_EVENT_MAX_DELIVERIES", "5")
            )
        except ValueError as exc:
            raise QaEventBusError(
                "forward QA Redis numeric settings must be integers"
            ) from exc
        _forward_event_bus = RedisEventBus(
            redis.Redis.from_url(redis_url),
            stream=os.environ.get("QUANT_QA_EVENT_STREAM", FORWARD_QA_STREAM),
            group=os.environ.get("QUANT_QA_EVENT_GROUP", FORWARD_QA_GROUP),
            consumer=os.environ.get(
                "QUANT_QA_EVENT_CONSUMER", "qa-forward-worker"
            ),
            dedupe_prefix="quant-qa:event-processed:",
            dedupe_ttl_seconds=dedupe_ttl_seconds,
            dead_letter_stream=os.environ.get(
                "QUANT_QA_EVENT_DLQ_STREAM", FORWARD_QA_DLQ_STREAM
            ),
            max_delivery_attempts=max_delivery_attempts,
            failure_prefix="quant-qa:event-failures:",
            # Forward requests are rare, durable governance records.  Never
            # evict an unconsumed request merely because an unrelated burst
            # reached an approximate stream length.
            stream_maxlen=None,
        )
    return _forward_event_bus


def _persist_qa_decision(assessment) -> None:
    """QA 결과를 DB에 기록하고 같은 trace의 Event를 발행한다."""

    if _audit_repository is None:
        return
    if os.environ.get("RISK_QA_RUNTIME", "").lower() == "test":
        return
    _audit_repository.save_qa_assessment(assessment)
    bus = _qa_event_bus()
    if bus is None:
        raise QaEventBusError("Canonical QA DB는 연결됐지만 QA Event Bus가 없습니다")
    bus.publish(
        event_id=assessment.qa_decision_id,
        event_type=QA_DECISION_EVENT,
        trace_id=assessment.trace_id,
        payload={
            "qa_decision_id": str(assessment.qa_decision_id),
            "artifact_version_id": str(assessment.artifact_version_id),
            "gate": assessment.gate,
            "decision": assessment.decision.value,
            "calculation_version": assessment.calculation_version,
            "input_hash": assessment.input_hash,
            "trace_id": str(assessment.trace_id),
        },
    )


_QA_CONSUMED_EVENT_SOURCES = {
    RISK_DECISION_EVENT: "risk-management",
    INTRADAY_FORWARD_QA_REQUESTED_EVENT: "quant-backtest-department",
}


def _record_risk_event(event: dict) -> None:
    """Accepted QA-bound events를 canonical audit ledger에 멱등 기록한다.

    The historical function name remains because the HTTP endpoint and worker
    import it. Routing is explicit by event type so a quant forward QA request
    cannot be mislabeled as a Risk event.
    """

    event_type = event.get("event_type")
    source_department = _QA_CONSUMED_EVENT_SOURCES.get(event_type)
    if source_department is None:
        raise QaEventPoisonError(
            f"QA consumer received an unsupported event: {event_type}"
        )
    if _audit_repository is None:
        raise QaEventBusError("QA-bound event를 기록할 DATABASE_URL이 없습니다")
    try:
        payload = event["payload"]
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        if event_type == INTRADAY_FORWARD_QA_REQUESTED_EVENT:
            _audit_repository.accept_intraday_forward_qa_request(event)
        else:
            _audit_repository.record_domain_event(
                event_id=UUID(event["event_id"]),
                event_type=event_type,
                source_department=source_department,
                trace_id=UUID(event["trace_id"]),
                payload=payload,
                occurred_at=datetime.fromisoformat(event["occurred_at"]),
            )
    except (DomainEventConflict, ForwardQaRequestConflict) as exc:
        raise QaEventPoisonError(
            f"QA immutable event conflicts with canonical state: {exc}"
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise QaEventPoisonError(
            f"QA event envelope is invalid: {exc}"
        ) from exc


# --- Request 모델 ------------------------------------------------------------------


class QaContextIn(BaseModel):
    decision_time: datetime


class ModelRiskCheckRequest(BaseModel):
    model_id: UUID
    model_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)
    evaluation_count: int = Field(ge=0)
    accuracy: float = Field(ge=0, le=1)
    calibration_error: float = Field(ge=0, le=1)
    drift_score: float = Field(ge=0, le=1)
    protected_failure_rate: float = Field(ge=0, le=1)


class InternalAuditCheckRequest(BaseModel):
    events: list[dict]
    expected_department: str = Field(default="qa", min_length=1)


class QaCheckRequest(BaseModel):
    qa_decision_id: UUID | None = None
    artifact: Artifact
    context: QaContextIn


class EventConsumeRequest(BaseModel):
    count: int = Field(default=10, ge=1, le=100)
    min_idle_ms: int = Field(default=0, ge=0)


class QaVerificationResponse(BaseModel):
    verification_id: str
    pipeline_status: str
    dispatch: dict[str, object]
    employees: dict[str, dict[str, object]]
    qa_head: dict[str, object]


class AgentHealthMetricsIn(BaseModel):
    scope: str
    window_start: datetime
    window_end: datetime
    request_count: int
    error_count: int
    p95_latency_ms: Decimal
    cost_usd: Decimal


class OpsThresholdsIn(BaseModel):
    max_error_rate: Decimal
    critical_error_rate: Decimal
    max_p95_latency_ms: Decimal
    critical_p95_latency_ms: Decimal
    max_cost_usd_per_window: Decimal


class OpsEvaluateRequest(BaseModel):
    metrics: AgentHealthMetricsIn
    thresholds: OpsThresholdsIn
    trace_id: UUID | None = None


class StartRunRequest(BaseModel):
    trace_id: UUID
    agent_id: UUID
    profile_version_id: UUID
    input_hash: str
    case_id: UUID | None = None
    fund_id: UUID | None = None
    model_id: UUID | None = None


class CompleteRunRequest(BaseModel):
    output_artifact_version_id: UUID | None = None
    token_usage: dict = {}
    cost: dict = {}
    trace_uri: str | None = None


class FailRunRequest(BaseModel):
    error_code: str


class RecordToolCallRequest(BaseModel):
    tool_name: str
    scope: dict
    input_hash: str
    policy_version: str | None = None


class DenyToolCallRequest(BaseModel):
    reason: str


class CompleteToolCallRequest(BaseModel):
    output_hash: str


class FailToolCallRequest(BaseModel):
    error_code: str


class AgentToolPolicyIn(BaseModel):
    agent_id: UUID
    profile_version_id: UUID
    allowed_tools: list[str]

    def to_policy(self) -> AgentToolPolicy:
        return AgentToolPolicy(
            agent_id=self.agent_id,
            profile_version_id=self.profile_version_id,
            allowed_tools=frozenset(self.allowed_tools),
        )


class ToolPermissionCheckRequest(BaseModel):
    policy: AgentToolPolicyIn
    tool_name: str


class RecordAndCheckToolCallRequest(BaseModel):
    policy: AgentToolPolicyIn
    tool_name: str
    scope: dict
    input_hash: str


class AddEventRequest(BaseModel):
    source: str
    entry_type: IncidentEntryType
    summary: str
    occurred_at: datetime
    recorded_by: str
    evidence: dict = {}


class OpenCorrectiveActionRequest(BaseModel):
    owner: str
    action_plan: dict
    due_at: datetime
    incident_id: UUID | None = None
    finding_id: UUID | None = None


class VerifyAndCloseRequest(BaseModel):
    verifier: str
    verification: dict


class CancelActionRequest(BaseModel):
    reason: str


class ComplianceCheckRequest(BaseModel):
    query: str = Field(min_length=1)
    as_of: str


# --- App --------------------------------------------------------------------------


class EvalRunRequest(BaseModel):
    eval_set_id: str = Field(min_length=1)
    role_code: str = Field(min_length=1)
    version: int = Field(gt=0)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cases: list[dict[str, object]] = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    candidate_profile_version: str = Field(min_length=1)
    champion_id: str | None = None
    champion_profile_version: str | None = None
    trace_id: str | None = None
    config: dict = Field(default_factory=dict)


class EvalRunResponse(BaseModel):
    eval_run_id: str
    status: str
    trace_id: str
    result_count: int
    comparison: ChampionComparison | None = None


class EvalResultResponse(BaseModel):
    eval_run_id: str
    case_key: str
    metric: str
    score: float | None
    passed: bool
    evidence: dict
    error_code: str | None


class ObservabilityFeedbackDecisionRequest(BaseModel):
    decision: str = Field(pattern=r"^(APPROVED|REJECTED)$")
    approved_by: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=240)
    improvement_type: str = Field(
        default="NO_ACTION",
        pattern=(
            r"^(SKILL_CREATE|SKILL_EVOLVE|CODE_FIX|PROMPT_POLICY|"
            r"RUNTIME_CONFIG|DATA_QUALITY|NO_ACTION)$"
        ),
    )
    target_skill_slug: str = Field(default="", max_length=64)


class ObservabilityBenchmarkStatusRequest(BaseModel):
    status: str = Field(pattern=r"^(RUNNING|PASSED|FAILED)$")
    benchmark_id: str = Field(min_length=1, max_length=160)
    score: float | None = Field(default=None, ge=0, le=1)
    report_ref: str = Field(default="", max_length=240)
    result_summary: str = Field(default="", max_length=240)


class EvolutionProposalDecisionRequest(BaseModel):
    decision: str = Field(pattern=r"^(APPROVED|REJECTED)$")
    approved_by: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=240)


# --- App --------------------------------------------------------------------------
app = FastAPI(title="QA Domain API", version="v1")
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi.responses import Response

from apps.observability.risk_qa import get_runtime_telemetry

QA_TELEMETRY = get_runtime_telemetry("qa")
evidence_engine = EvidenceQaEngine()
ops_monitor = OpsHealthMonitor()
recorder = TraceRecorder(repository=_audit_repository)
timeline = IncidentTimeline(repository=_audit_repository)
evidence_store = EvidenceStore()  # rag-librarian-evidence-curator 실연동 전까지 스텁
_eval_repository = (
    InMemoryEvalAuditRepository()
    if _QA_RUNTIME == "test"
    else _audit_repository
)
_eval_candidates: dict[str, object] = {}
_eval_execution_lock = Lock()

def _serialize_eval_execution(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _eval_execution_lock:
            return function(*args, **kwargs)
    return wrapped
_eval_comparisons: dict[str, ChampionComparison | None] = {}
_eval_idempotency: dict[str, tuple[str, EvalRunResponse]] = {}
_feedback_ledger: FeedbackLedger | None = None
_evolution_store_instance: EvolutionSkillStore | None = None


def _observability_feedback_ledger() -> FeedbackLedger:
    global _feedback_ledger
    if _feedback_ledger is None:
        _feedback_ledger = FeedbackLedger(FeedbackConfig.from_env().state_path)
    return _feedback_ledger


def _evolution_skill_store() -> EvolutionSkillStore:
    global _evolution_store_instance
    if _evolution_store_instance is None:
        _evolution_store_instance = EvolutionSkillStore(
            Path(os.environ.get("EVOLUTION_SKILLS_HOME", "/var/lib/evolution-skills"))
        )
    return _evolution_store_instance

def _eval_request_fingerprint(body: EvalRunRequest) -> str:
    payload = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def set_eval_repository(repository: object) -> None:
    """Inject a repository for focused tests without changing production defaults."""
    global _eval_repository
    _eval_repository = repository


def register_eval_candidate(candidate_id: str, runner: object) -> None:
    """Register an injected QA Candidate for the internal TEST/Shadow boundary."""
    _eval_candidates[candidate_id] = runner


def _require_eval_repository():
    global _eval_repository
    if _eval_repository is None and os.environ.get("RISK_QA_RUNTIME", "").strip().lower() == "test":
        _eval_repository = InMemoryEvalAuditRepository()
    if _eval_repository is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "EVAL_REPOSITORY_NOT_CONFIGURED",
                "message": "QA Eval persistence requires DATABASE_URL",
            },
        )
    return _eval_repository


def _eval_run_for(run_id: str):
    repository = _require_eval_repository()
    getter = (
        getattr(repository, "run_for_id", None)
        or getattr(repository, "get_eval_run", None)
        or getattr(repository, "get_run", None)
    )
    if getter is not None:
        return getter(run_id)
    return next(
        (item for item in getattr(repository, "runs", ()) if item.eval_run_id == run_id),
        None,
    )


def _eval_comparison_for(run_id: str):
    repository = _require_eval_repository()
    getter = getattr(repository, "comparison_for_run", None)
    if getter is not None:
        return getter(run_id)
    return _eval_comparisons.get(run_id)

def _eval_results_for(run_id: str) -> list[EvalResultResponse]:
    repository = _require_eval_repository()
    getter = getattr(repository, "results_for_run", None)
    rows = (
        list(getter(run_id))
        if getter is not None
        else [
            item for item in getattr(repository, "results", ()) if item.eval_run_id == run_id
        ]
    )
    return [
        EvalResultResponse(
            eval_run_id=result.eval_run_id,
            case_key=result.case_key,
            metric=result.metric.value,
            score=result.score,
            passed=result.passed,
            evidence=result.evidence,
            error_code=result.error_code,
        )
        for result in rows
    ]


@app.exception_handler(TraceRecorderError)
def _on_trace_recorder_error(request, exc: TraceRecorderError):
    return JSONResponse(
        status_code=409,
        content={
            "error_code": type(exc).__name__,
            "message": str(exc),
            "detail": {},
            "trace_id": None,
        },
    )


@app.exception_handler(IncidentTimelineError)
def _on_incident_timeline_error(request, exc: IncidentTimelineError):
    return JSONResponse(
        status_code=409,
        content={
            "error_code": type(exc).__name__,
            "message": str(exc),
            "detail": {},
            "trace_id": None,
        },
    )


@app.exception_handler(RequestValidationError)
def _on_validation_error(request, exc: RequestValidationError):
    # 스펙 1.4 에러 봉투를 요청 스키마 검증 실패에도 그대로 적용 (Risk API와 동일 패턴)
    return JSONResponse(
        status_code=422,
        content={
            "error_code": "RequestValidationError",
            "message": "요청 스키마 검증 실패",
            "detail": {"errors": jsonable_encoder(exc.errors())},
            "trace_id": None,
        },
    )


@app.exception_handler(QaDecisionPersistenceError)
def _on_qa_persistence_error(request, exc: QaDecisionPersistenceError):
    return JSONResponse(
        status_code=503,
        content={
            "error_code": type(exc).__name__,
            "message": str(exc),
            "detail": {},
            "trace_id": None,
        },
    )


@app.exception_handler(QaEventBusError)
def _on_qa_event_bus_error(request, exc: QaEventBusError):
    return JSONResponse(
        status_code=503,
        content={
            "error_code": type(exc).__name__,
            "message": str(exc),
            "detail": {},
            "trace_id": None,
        },
    )


def _qa_check_contract_is_approved() -> bool:
    """Keep the proposed gate inactive until the owning service approves v1."""
    runtime = os.environ.get("RISK_QA_RUNTIME", "").strip().lower()
    if runtime == "test":
        return True
    if runtime != "production":
        return False
    return (
        os.environ.get("QA_CHECK_CONTRACT_APPROVED", "false").strip().lower() == "true"
    )


def _require_service_token(
    authorization: str | None,
    *,
    required_scope: str,
    expected_subject: str | None = None,
):
    from apps.security.service_auth import ServiceAuthError, authenticate_service_token

    try:
        return authenticate_service_token(
            authorization,
            required_scope=required_scope,
            expected_department="qa-department",
            expected_service="qa-api",
            expected_subject=expected_subject,
            secret_env="QA_SERVICE_AUTH_SECRET",
            issuer_env="QA_SERVICE_AUTH_ISSUER",
            audience_env="QA_SERVICE_AUTH_AUDIENCE",
        )
    except ServiceAuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.code, "message": str(exc)},
        ) from exc
def _require_eval_service_token(
    authorization: str | None,
    *,
    required_scope: str,
):
    """Protect Eval audit routes while keeping direct test fixtures deterministic."""
    if _QA_RUNTIME == "test":
        return None
    return _require_service_token(
        authorization,
        required_scope=required_scope,
    )


# 3.1 승인된 QA Evidence Gate v1 — Case에 종속된 판정 -------------------------------


@app.post("/qa/v1/model-risk/evaluate")
def model_risk_evaluate(body: ModelRiskCheckRequest):
    result = ModelRiskEngine().evaluate(ModelRiskInput(**body.model_dump()))
    return {
        "decision": result.decision.value,
        "reason_codes": list(result.reason_codes),
        "calculation_version": result.calculation_version,
        "input_hash": result.input_hash,
    }


@app.get("/qa/v1/evolution/proposals/{proposal_id}")
def get_evolution_skill_proposal(
    proposal_id: str,
    authorization: str | None = Header(default=None),
):
    """Return the immutable lineage and current outcome-verification status."""

    _require_eval_service_token(
        authorization, required_scope="qa.observability.read"
    )
    try:
        return build_resolution_report(_evolution_skill_store(), proposal_id)
    except (EvolutionSkillError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "EVOLUTION_PROPOSAL_NOT_FOUND"},
        ) from exc


@app.post("/qa/v1/internal-audit/evaluate")
def internal_audit_evaluate(body: InternalAuditCheckRequest):
    result = InternalAuditEngine().evaluate(
        events=body.events, expected_department=body.expected_department
    )
    return {
        "decision": result.decision.value,
        "findings": list(result.findings),
        "calculation_version": result.calculation_version,
        "input_hash": result.input_hash,
    }


@app.post("/investment-cases/{case_id}/qa-check")
def qa_check(case_id: str, body: QaCheckRequest):
    if not _qa_check_contract_is_approved():
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "QA_CHECK_CONTRACT_NOT_APPROVED",
                "message": "상위 서비스 계약 승인 전에는 production qa-check를 활성화할 수 없습니다",
            },
        )
    ctx = QaContext(
        evidence_store=evidence_store, decision_time=body.context.decision_time
    )
    assessment = evidence_engine.check_artifact(body.artifact, ctx, body.qa_decision_id)
    _persist_qa_decision(assessment)
    return assessment


@app.post("/qa/v1/eval-runs", response_model=EvalRunResponse)
@_serialize_eval_execution
def create_eval_run(
    body: EvalRunRequest,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Run an injected QA Candidate in Shadow/Mock and persist audit records only."""
    _require_eval_service_token(authorization, required_scope="qa.eval.execute")
    if _QA_RUNTIME != "test" and not idempotency_key:
        raise HTTPException(
            status_code=428,
            detail={"error_code": "IDEMPOTENCY_KEY_REQUIRED"},
        )
    request_key = idempotency_key or f"test:{_eval_request_fingerprint(body)}"
    fingerprint = _eval_request_fingerprint(body)
    cached = _eval_idempotency.get(request_key)
    if cached is not None:
        if cached[0] != fingerprint:
            raise HTTPException(
                status_code=409,
                detail={"error_code": "IDEMPOTENCY_KEY_CONFLICT"},
            )
        return cached[1]
    if _QA_RUNTIME != "test":
        for field_name, value in (
            ("eval_set_id", body.eval_set_id),
            ("trace_id", body.trace_id),
        ):
            if value is None:
                continue
            try:
                UUID(value)
            except (ValueError, AttributeError):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error_code": "SCHEMA_FAILURE",
                        "message": f"{field_name} must be a UUID in production runtime",
                    },
                ) from None
    try:
        eval_set = EvalSet(
            eval_set_id=body.eval_set_id,
            role_code=body.role_code,
            version=body.version,
            content_hash=body.content_hash,
            cases=[EvalCase.from_mapping(case) for case in body.cases],
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "SCHEMA_FAILURE", "message": str(exc)},
        ) from exc
    if bool(body.champion_id) != bool(body.champion_profile_version):
        raise HTTPException(
            status_code=422,
            detail={"error_code": "CHAMPION_IDENTITY_INCOMPLETE"},
        )
    repository = _require_eval_repository()
    candidate_runner = _eval_candidates.get(body.candidate_id)
    if candidate_runner is None:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "CANDIDATE_NOT_REGISTERED"},
        )
    candidate = CandidateSpec(
        candidate_id=body.candidate_id,
        profile_version=body.candidate_profile_version,
        model_version=str(body.config.get("model_version", "injected")),
        adapter_version=str(body.config.get("adapter_version", "none")),
        runner=candidate_runner,
    )
    champion = None
    champion_set = None
    if body.champion_id and body.champion_profile_version:
        champion_runner = _eval_candidates.get(body.champion_id)
        if champion_runner is None:
            raise HTTPException(status_code=409, detail={"error_code": "CHAMPION_NOT_REGISTERED"})
        champion = CandidateSpec(
            candidate_id=body.champion_id,
            profile_version=body.champion_profile_version,
            model_version=str(body.config.get("champion_model_version", "injected")),
            adapter_version=str(body.config.get("champion_adapter_version", "none")),
            runner=champion_runner,
        )
        champion_set = eval_set
    report = EvalRunner(repository=repository).evaluate(
        eval_set,
        candidate,
        champion=champion,
        champion_eval_set=champion_set,
        trace_id=body.trace_id,
        config={**body.config, "idempotency_key": request_key},
    )
    run = report.candidate_run
    _eval_comparisons[run.eval_run_id] = report.comparison
    comparison = _eval_comparison_for(run.eval_run_id)
    response = EvalRunResponse(
        eval_run_id=run.eval_run_id,
        status=run.status,
        trace_id=run.trace_id,
        result_count=len(_eval_results_for(run.eval_run_id)),
        comparison=comparison,
    )
    _eval_idempotency[request_key] = (fingerprint, response)
    return response


@app.get("/qa/v1/eval-runs/{eval_run_id}", response_model=EvalRunResponse)
def get_eval_run_status(
    eval_run_id: str,
    authorization: str | None = Header(default=None),
):
    _require_eval_service_token(authorization, required_scope="qa.eval.read")
    try:
        run = _eval_run_for(eval_run_id)
    except (TypeError, ValueError):
        run = None
    if run is None:
        raise HTTPException(status_code=404, detail={"error_code": "EVAL_RUN_NOT_FOUND"})
    return EvalRunResponse(
        eval_run_id=run.eval_run_id,
        status=run.status,
        trace_id=run.trace_id,
        result_count=len(_eval_results_for(run.eval_run_id)),
        comparison=_eval_comparison_for(run.eval_run_id),
    )


@app.get("/qa/v1/eval-runs/{eval_run_id}/results", response_model=list[EvalResultResponse])
def get_eval_results(
    eval_run_id: str,
    authorization: str | None = Header(default=None),
):
    _require_eval_service_token(authorization, required_scope="qa.eval.read")
    try:
        run = _eval_run_for(eval_run_id)
    except (TypeError, ValueError):
        run = None
    if run is None:
        raise HTTPException(status_code=404, detail={"error_code": "EVAL_RUN_NOT_FOUND"})
    return _eval_results_for(eval_run_id)

@app.post(
    "/qa/v1/verifications/{verification_id}/assess",
    response_model=QaVerificationResponse,
)
def assess_qa_verification_for_head(verification_id: str, body: QaVerificationRequest):
    """Dispatch one immutable QA verification to the three QA employees.

    The QA Head receives the independent qa-runner/hallucination-critic-worker/
    incident-postmortem-worker reports only after each has run. This endpoint
    is advisory and never closes a Finding, changes a QA verdict, alters a
    Risk verdict, or approves/submits an order.
    """

    if verification_id != body.verification_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "VERIFICATION_ID_MISMATCH",
                "path_verification_id": verification_id,
                "body_verification_id": body.verification_id,
            },
        )
    return assess_qa_verification(body)


@app.post("/qa/v1/events/consume")
def consume_events(body: EventConsumeRequest):
    """QA Worker가 Risk Decision Stream을 한 배치 소비한다."""

    bus = _qa_event_bus()
    if bus is None:
        raise QaEventBusError("QA Event Bus 연결 설정이 없습니다")
    return {
        "consumed": bus.consume_once(
            _record_risk_event,
            count=body.count,
            min_idle_ms=body.min_idle_ms,
        )
    }


@app.get("/qa/v1/observability/rag")
def rag_observability():
    from src.resilience import latency_summary

    return {
        "nodes": {
            node: latency_summary(node)
            for node in ("retrieve", "grade", "generate", "hallucination_check")
        }
    }


@app.get("/qa/v1/observability/feedback/pending")
def pending_observability_feedback(
    limit: int = 50,
    authorization: str | None = Header(default=None),
):
    """Return redacted, unevaluated findings for QA review.

    This endpoint reads the local coordination ledger only.  It never calls
    LangSmith on the request path and never returns model inputs/outputs.
    """

    _require_eval_service_token(authorization, required_scope="qa.observability.read")
    bounded_limit = max(1, min(int(limit), 100))
    return {
        "status": "READY",
        "items": _observability_feedback_ledger().pending(bounded_limit),
    }


@app.post("/qa/v1/observability/feedback/{artifact_id}/decision")
def decide_observability_feedback(
    artifact_id: str,
    body: ObservabilityFeedbackDecisionRequest,
    authorization: str | None = Header(default=None),
):
    """Append one QA approval/rejection decision for a feedback artifact."""

    _require_eval_service_token(authorization, required_scope="qa.observability.approve")
    if not _observability_feedback_ledger().approve(
        artifact_id,
        body.decision,
        body.approved_by,
        body.reason,
        improvement_type=body.improvement_type,
        target_skill_slug=body.target_skill_slug,
    ):
        raise HTTPException(status_code=409, detail={"error_code": "FEEDBACK_DECISION_EXISTS_OR_INVALID"})
    return {
        "status": body.decision,
        "artifact_id": artifact_id,
        "approved_by": body.approved_by,
        "improvement_type": body.improvement_type,
        "target_skill_slug": body.target_skill_slug or None,
        "benchmark_status": "PENDING" if body.decision == "APPROVED" else None,
    }


@app.post("/qa/v1/evolution/proposals/{proposal_id}/decision")
def decide_evolution_skill_proposal(
    proposal_id: str,
    body: EvolutionProposalDecisionRequest,
    authorization: str | None = Header(default=None),
    discord_message_id: str | None = Header(
        default=None, alias="X-HgFinance-Discord-Message-Id"
    ),
):
    """Record the second human decision against the exact proposal hashes."""

    _require_eval_service_token(
        authorization, required_scope="qa.observability.approve"
    )
    try:
        state = _evolution_skill_store().approve(
            proposal_id,
            approved_by=body.approved_by,
            qa_verdict="PASS" if body.decision == "APPROVED" else "FAIL",
            reason=body.reason,
            decision_ref=(
                f"discord:{discord_message_id}" if discord_message_id else ""
            ),
        )
    except (EvolutionSkillError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "EVOLUTION_PROPOSAL_DECISION_REJECTED",
                "reason": str(exc),
            },
        ) from exc
    return {
        "status": state["status"],
        "proposal_id": proposal_id,
        "content_hash": state.get("content_hash"),
        "provenance_hash": state.get("provenance_hash"),
        "next_step": "CONTROL_PLANE_PROMOTION"
        if state["status"] == "APPROVED"
        else "CLOSED",
    }


@app.get("/qa/v1/observability/feedback/approved")
def approved_observability_feedback(
    department: str | None = None,
    limit: int = 3,
    authorization: str | None = Header(default=None),
):
    """Return only bounded QA-approved hints for an internal consumer."""

    _require_eval_service_token(authorization, required_scope="qa.observability.read")
    config = FeedbackConfig.from_env()
    return _observability_feedback_ledger().approved_hints(
        department=department,
        limit=max(1, min(int(limit), config.max_feedback_items)),
        max_chars=config.max_feedback_chars,
    ) or {"schema_version": "hgfinance.observability.feedback.v1", "items": []}


@app.get("/qa/v1/observability/feedback/benchmark/candidates")
def observability_benchmark_candidates(
    limit: int = 50,
    authorization: str | None = Header(default=None),
):
    """Return redacted QA-approved candidates for an offline benchmark runner."""

    _require_eval_service_token(authorization, required_scope="qa.observability.benchmark")
    return {
        "status": "READY",
        "candidates": _observability_feedback_ledger().benchmark_candidates(
            max(1, min(int(limit), 100))
        ),
    }


@app.post("/qa/v1/observability/feedback/{artifact_id}/benchmark")
def update_observability_benchmark(
    artifact_id: str,
    body: ObservabilityBenchmarkStatusRequest,
    authorization: str | None = Header(default=None),
):
    """Record only an offline benchmark gate result; raw benchmark data is excluded."""

    _require_eval_service_token(authorization, required_scope="qa.observability.benchmark")
    if not _observability_feedback_ledger().update_benchmark(
        artifact_id,
        status=body.status,
        benchmark_id=body.benchmark_id,
        score=body.score,
        report_ref=body.report_ref,
        result_summary=body.result_summary,
    ):
        raise HTTPException(status_code=409, detail={"error_code": "BENCHMARK_UPDATE_INVALID"})
    return {
        "status": body.status,
        "artifact_id": artifact_id,
        "benchmark_id": body.benchmark_id,
    }


@app.get("/qa/v1/d5/improvement/regression-candidates")
def d5_regression_improvement_candidates(
    limit: int = 50,
    authorization: str | None = Header(default=None),
):
    """Return admitted D5 candidates awaiting central-router regression work.

    The response is metadata-only. This endpoint does not apply a route,
    modify code, or turn an approved candidate into a CEO hint.
    """

    _require_eval_service_token(authorization, required_scope="qa.observability.benchmark")
    return {
        "status": "READY",
        "candidates": d5_regression_candidates(
            _observability_feedback_ledger(),
            limit=max(1, min(int(limit), 100)),
        ),
    }


# 3.2 Ops Health ---------------------------------------------------------------------


@app.post("/qa/v1/ops/evaluate")
def ops_evaluate(body: OpsEvaluateRequest):
    metrics = AgentHealthMetrics(**body.metrics.model_dump())
    thresholds = OpsThresholds(**body.thresholds.model_dump())
    return ops_monitor.evaluate(metrics, thresholds, body.trace_id)


# 3.3 Agent/Tool Trace -----------------------------------------------------------------


@app.post("/qa/v1/runs")
def start_run(body: StartRunRequest):
    return recorder.start_run(
        body.trace_id,
        body.agent_id,
        body.profile_version_id,
        body.input_hash,
        case_id=body.case_id,
        fund_id=body.fund_id,
        model_id=body.model_id,
    )


@app.post("/qa/v1/runs/{agent_run_id}/complete")
def complete_run(agent_run_id: UUID, body: CompleteRunRequest | None = None):
    body = body or CompleteRunRequest()
    return recorder.complete_run(
        agent_run_id,
        output_artifact_version_id=body.output_artifact_version_id,
        token_usage=body.token_usage,
        cost=body.cost,
        trace_uri=body.trace_uri,
    )


@app.post("/qa/v1/runs/{agent_run_id}/fail")
def fail_run(agent_run_id: UUID, body: FailRunRequest):
    return recorder.fail_run(agent_run_id, body.error_code)


@app.post("/qa/v1/runs/{agent_run_id}/timeout")
def timeout_run(agent_run_id: UUID):
    return recorder.timeout_run(agent_run_id)


@app.post("/qa/v1/runs/{agent_run_id}/cancel")
def cancel_run(agent_run_id: UUID):
    return recorder.cancel_run(agent_run_id)


@app.post("/qa/v1/runs/{agent_run_id}/tool-calls")
def record_tool_call(agent_run_id: UUID, body: RecordToolCallRequest):
    return recorder.record_tool_call(
        agent_run_id,
        body.tool_name,
        body.scope,
        body.input_hash,
        policy_version=body.policy_version,
    )


@app.post("/qa/v1/tool-calls/{tool_call_id}/allow")
def allow_tool_call(tool_call_id: UUID):
    return recorder.allow_tool_call(tool_call_id)


@app.post("/qa/v1/tool-calls/{tool_call_id}/deny")
def deny_tool_call(tool_call_id: UUID, body: DenyToolCallRequest):
    return recorder.deny_tool_call(tool_call_id, body.reason)


@app.post("/qa/v1/tool-calls/{tool_call_id}/complete")
def complete_tool_call(tool_call_id: UUID, body: CompleteToolCallRequest):
    return recorder.complete_tool_call(tool_call_id, body.output_hash)


@app.post("/qa/v1/tool-calls/{tool_call_id}/fail")
def fail_tool_call(tool_call_id: UUID, body: FailToolCallRequest):
    return recorder.fail_tool_call(tool_call_id, body.error_code)


# 3.4 Tool Permission ------------------------------------------------------------------


@app.post("/qa/v1/tool-permission/check")
def tool_permission_check(body: ToolPermissionCheckRequest):
    return check_tool_permission(body.policy.to_policy(), body.tool_name)


@app.post("/qa/v1/runs/{agent_run_id}/tool-calls:checked")
def record_and_check(agent_run_id: UUID, body: RecordAndCheckToolCallRequest):
    return record_and_check_tool_call(
        recorder,
        agent_run_id,
        body.policy.to_policy(),
        body.tool_name,
        body.scope,
        body.input_hash,
    )


@app.get("/qa/v1/tool-calls/unauthorized-count")
def unauthorized_count():
    return {"count": count_unauthorized_calls(list(recorder.tool_calls.values()))}


# 3.5 Incident/Corrective Action ---------------------------------------------------------


@app.post("/qa/v1/incidents/{incident_id}/events")
def add_event(incident_id: UUID, body: AddEventRequest):
    return timeline.add_event(
        incident_id,
        body.source,
        body.entry_type,
        body.summary,
        body.occurred_at,
        body.recorded_by,
        evidence=body.evidence,
    )


@app.get("/qa/v1/incidents/{incident_id}/timeline")
def get_timeline(incident_id: UUID):
    return timeline.timeline_for(incident_id)


@app.post("/qa/v1/corrective-actions")
def open_corrective_action(body: OpenCorrectiveActionRequest):
    return timeline.open_corrective_action(
        body.owner,
        body.action_plan,
        body.due_at,
        incident_id=body.incident_id,
        finding_id=body.finding_id,
    )


@app.post("/qa/v1/corrective-actions/{corrective_action_id}/start")
def start_action(corrective_action_id: UUID):
    return timeline.start_action(corrective_action_id)


@app.post("/qa/v1/corrective-actions/{corrective_action_id}/submit-for-verification")
def submit_for_verification(corrective_action_id: UUID):
    return timeline.submit_for_verification(corrective_action_id)


@app.post("/qa/v1/corrective-actions/{corrective_action_id}/verify-and-close")
def verify_and_close(
    corrective_action_id: UUID,
    body: VerifyAndCloseRequest,
    authorization: str | None = Header(default=None),
):
    # API 레이어에서 서명된 Service Token의 sub와 verifier를 일치시킨다.
    _require_service_token(
        authorization,
        required_scope="qa.corrective_action.close",
        expected_subject=body.verifier,
    )
    return timeline.verify_and_close(
        corrective_action_id, body.verifier, body.verification
    )


@app.post("/qa/v1/corrective-actions/{corrective_action_id}/cancel")
def cancel_action(corrective_action_id: UUID, body: CancelActionRequest):
    return timeline.cancel_action(corrective_action_id, body.reason)


# 3.6 Evidence QA (Agentic RAG baseline) ------------------------------------------------


@app.post("/qa/v1/evidence/check")
def evidence_check(body: ComplianceCheckRequest):
    from src.graph import (
        run_compliance_check,  # 지연 import - langgraph/OpenAI는 호출 시점에만 필요
    )

    corpus_dir = _configured_evidence_corpus()
    if os.environ.get("RISK_QA_RUNTIME", "").strip().lower() == "production":
        status = inspect_policy_corpus(corpus_dir)
        if not status.ready:
            raise HTTPException(
                status_code=503,
                detail={
                    "error_code": "QA_EVIDENCE_CORPUS_NOT_READY",
                    "reason": status.reason,
                },
            )
    return run_compliance_check(
        body.query,
        body.as_of,
        corpus_dir=corpus_dir,
        persona="evidence-qa-agent",
    )


# ── Health 계약 ───────────────────────────────────────────────────────────────
# 전 부서 공통 규격이다(통합계획 8.1). risk-api 와 같은 규약 - `/health` 는
# 프로세스 생존만 보고 외부를 만지지 않으며, 저장소 판단은 `/health/ready` 가 한다.


@app.get("/health")
def health() -> dict:
    """Liveness. **저장소가 죽어도 200 이다.** 외부 호출을 하지 않는다."""
    return {
        "status": "ok",
        "service": "qa-api",
        "api_version": "v1",
        "canonical_db_configured": bool(_DATABASE_URL),
        "runtime": _QA_RUNTIME or "default",
    }


@app.get("/health/ready")
def health_ready() -> dict:
    """Readiness. audit 저장소가 붙어 있어야 ready 다."""
    if not _DATABASE_URL:
        raise HTTPException(
            status_code=503,
            detail={"status": "degraded", "canonical_db": "NOT_CONFIGURED"},
        )
    if _audit_repository is None:
        # DSN 은 있는데 저장소가 없다 = TEST 런타임으로 뜬 것이다. 숨기지 않는다.
        raise HTTPException(
            status_code=503,
            detail={
                "status": "degraded",
                "canonical_db": "DISABLED_BY_RUNTIME",
                "runtime": _QA_RUNTIME or "default",
            },
        )
    try:
        database = _audit_repository.runtime_database_status()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "degraded",
                "canonical_db": "ROLE_OR_WRITE_CHECK_FAILED",
                "error_type": type(exc).__name__,
            },
        ) from exc
    return {
        "status": "ready",
        "service": "qa-api",
        "canonical_db": "READY",
        "database_role": database["current_user"],
        "transaction_read_only": database["transaction_read_only"],
    }


@app.get("/qa/v1/observability/runtime")
def runtime_observability():
    return QA_TELEMETRY.snapshot()


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics():
    return Response(
        content=QA_TELEMETRY.prometheus_text(),
        media_type="text/plain; version=0.0.4",
    )


@app.get("/qa/v1/evidence/corpus/status")
def evidence_corpus_status():
    """Expose readiness only; document contents never leave the QA service."""

    corpus_dir = _configured_evidence_corpus()
    status = inspect_policy_corpus(corpus_dir)
    return {
        "directory": status.directory,
        "document_count": status.document_count,
        "placeholder_count": status.placeholder_count,
        "corpus_hash": status.corpus_hash,
        "ready": status.ready,
        "reason": status.reason,
    }


if __name__ == "__main__":
    from datetime import timedelta, timezone
    from uuid import uuid4

    from evidence_qa_engine import EvidenceChunk
    from fastapi.testclient import TestClient

    now = datetime.now(timezone.utc)
    client = TestClient(app)

    # --- 3.1 qa-check: evidence_store에 근거를 미리 심어두고 두 시나리오 확인 ---------------
    ev_id = uuid4()
    evidence_store.chunks[ev_id] = EvidenceChunk(
        evidence_id=ev_id,
        source="research-api",
        published_at=now - timedelta(hours=1),
        observed_at=now - timedelta(hours=1),
        excerpt="근거 원문",
        numeric_value=Decimal(70000),
        unit="KRW",
    )
    fund, trace, artifact_id = uuid4(), uuid4(), uuid4()

    r1 = client.post(
        "/investment-cases/case-1/qa-check",
        json={
            "artifact": {
                "artifact_version_id": str(artifact_id),
                "artifact_type": "research_packet",
                "producer": "research-supervisor",
                "fund_id": str(fund),
                "trace_id": str(trace),
                "claims": [
                    {
                        "claim_index": 0,
                        "text": "AAPL 종가는 70000원",
                        "kind": "fact",
                        "subject": "AAPL",
                        "numeric_value": "70000",
                        "unit": "KRW",
                        "evidence_ids": [str(ev_id)],
                    }
                ],
            },
            "context": {"decision_time": now.isoformat()},
        },
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["decision"] == "PASS", r1.json()

    r2 = client.post(
        "/investment-cases/case-1/qa-check",
        json={
            "artifact": {
                "artifact_version_id": str(uuid4()),
                "artifact_type": "research_packet",
                "producer": "research-supervisor",
                "fund_id": str(fund),
                "trace_id": str(trace),
                "claims": [
                    {
                        "claim_index": 0,
                        "text": "AAPL은 반등한다",
                        "kind": "fact",
                        "subject": "AAPL",
                    }
                ],
            },
            "context": {"decision_time": now.isoformat()},
        },
    )
    assert r2.json()["decision"] == "FAIL", r2.json()
    assert len(r2.json()["findings"]) == 1

    # --- 3.2 ops/evaluate --------------------------------------------------------------
    ops_body = {
        "metrics": {
            "scope": "research-department",
            "window_start": (now - timedelta(minutes=5)).isoformat(),
            "window_end": now.isoformat(),
            "request_count": 1000,
            "error_count": 150,
            "p95_latency_ms": "800",
            "cost_usd": "2.5",
        },
        "thresholds": {
            "max_error_rate": "0.02",
            "critical_error_rate": "0.10",
            "max_p95_latency_ms": "2000",
            "critical_p95_latency_ms": "5000",
            "max_cost_usd_per_window": "10",
        },
    }
    r3 = client.post("/qa/v1/ops/evaluate", json=ops_body)
    assert r3.json()["status"] == "critical", r3.json()
    assert r3.json()["incident"]["severity"] == "SEV2"

    # 필수 필드 누락 -> 422, 스펙 1.4 에러 봉투 확인
    r3_bad = client.post("/qa/v1/ops/evaluate", json={"metrics": ops_body["metrics"]})
    assert r3_bad.status_code == 422, r3_bad.text
    assert r3_bad.json()["error_code"] == "RequestValidationError", r3_bad.json()

    # --- 3.3 Agent/Tool Trace ------------------------------------------------------------
    agent_id, profile_id, trace_id = uuid4(), uuid4(), uuid4()
    run = client.post(
        "/qa/v1/runs",
        json={
            "trace_id": str(trace_id),
            "agent_id": str(agent_id),
            "profile_version_id": str(profile_id),
            "input_hash": "hash_1",
        },
    ).json()
    call = client.post(
        f"/qa/v1/runs/{run['agent_run_id']}/tool-calls",
        json={
            "tool_name": "market-api",
            "scope": {"symbol": "AAPL"},
            "input_hash": "call_hash_1",
        },
    ).json()
    client.post(f"/qa/v1/tool-calls/{call['tool_call_id']}/allow")
    client.post(
        f"/qa/v1/tool-calls/{call['tool_call_id']}/complete",
        json={"output_hash": "out_1"},
    )
    finished = client.post(f"/qa/v1/runs/{run['agent_run_id']}/complete").json()
    assert finished["status"] == "COMPLETED", finished

    # --- 3.4 Tool Permission ---------------------------------------------------------------
    policy = {
        "agent_id": str(agent_id),
        "profile_version_id": str(profile_id),
        "allowed_tools": ["market-api"],
    }
    ok_check = client.post(
        "/qa/v1/tool-permission/check",
        json={"policy": policy, "tool_name": "market-api"},
    ).json()
    assert ok_check["result"] == "ALLOWED"
    bad_check = client.post(
        "/qa/v1/tool-permission/check",
        json={"policy": policy, "tool_name": "broker-adapter-submit"},
    ).json()
    assert bad_check["result"] == "DENIED"
    run2 = client.post(
        "/qa/v1/runs",
        json={
            "trace_id": str(trace_id),
            "agent_id": str(agent_id),
            "profile_version_id": str(profile_id),
            "input_hash": "hash_2",
        },
    ).json()
    denied_call = client.post(
        f"/qa/v1/runs/{run2['agent_run_id']}/tool-calls:checked",
        json={
            "policy": policy,
            "tool_name": "broker-adapter-submit",
            "scope": {},
            "input_hash": "call_hash_2",
        },
    ).json()
    assert denied_call["status"] == "DENIED"
    count = client.get("/qa/v1/tool-calls/unauthorized-count").json()["count"]
    assert count == 1, count

    # --- 3.5 Incident/Corrective Action -----------------------------------------------------
    incident_id, finding_id = uuid4(), uuid4()
    client.post(
        f"/qa/v1/incidents/{incident_id}/events",
        json={
            "source": "agent-ops-monitor",
            "entry_type": "FACT",
            "summary": "에러율 15% 관측",
            "occurred_at": now.isoformat(),
            "recorded_by": "svc_audit_collector",
        },
    )
    client.post(
        f"/qa/v1/incidents/{incident_id}/events",
        json={
            "source": "incident-postmortem-agent",
            "entry_type": "INFERENCE",
            "summary": "market-api 지연이 원인으로 추정",
            "occurred_at": now.isoformat(),
            "recorded_by": "svc_audit_collector",
        },
    )
    tl = client.get(f"/qa/v1/incidents/{incident_id}/timeline").json()
    assert (
        len(tl) == 2
        and tl[0]["entry_type"] == "FACT"
        and tl[1]["entry_type"] == "INFERENCE"
    )

    action = client.post(
        "/qa/v1/corrective-actions",
        json={
            "owner": "research-department",
            "action_plan": {"plan": "타임아웃 값 상향"},
            "due_at": (now + timedelta(days=3)).isoformat(),
            "incident_id": str(incident_id),
        },
    ).json()
    action_id = action["corrective_action_id"]
    client.post(f"/qa/v1/corrective-actions/{action_id}/start")
    client.post(f"/qa/v1/corrective-actions/{action_id}/submit-for-verification")

    # 인증 주체와 verifier가 다르면 403
    mismatched = client.post(
        f"/qa/v1/corrective-actions/{action_id}/verify-and-close",
        json={"verifier": "qa-audit-supervisor", "verification": {}},
        headers={"x-auth-subject": "someone-else"},
    )
    assert mismatched.status_code == 403, mismatched.text

    # 조치를 만든 사람 본인이 검증자면 engine이 409로 막는다
    self_verify = client.post(
        f"/qa/v1/corrective-actions/{action_id}/verify-and-close",
        json={"verifier": "research-department", "verification": {}},
    )
    assert self_verify.status_code == 409, self_verify.text

    closed = client.post(
        f"/qa/v1/corrective-actions/{action_id}/verify-and-close",
        json={"verifier": "qa-audit-supervisor", "verification": {"checked": "확인함"}},
        headers={"x-auth-subject": "qa-audit-supervisor"},
    ).json()
    assert closed["status"] == "COMPLETED", closed

    # --- 3.6 QA verification mandate (qa-runner/hallucination-critic/incident-postmortem) --
    v_artifact_id, v_fund, v_trace = uuid4(), uuid4(), uuid4()
    unsupported_verification = client.post(
        "/qa/v1/verifications/ver-1/assess",
        json={
            "verification_id": "ver-1",
            "artifact": {
                "artifact_version_id": str(v_artifact_id),
                "artifact_type": "research_packet",
                "producer": "research-supervisor",
                "fund_id": str(v_fund),
                "trace_id": str(v_trace),
                "claims": [
                    {
                        "claim_index": 0,
                        "text": "AAPL은 반등한다",
                        "kind": "fact",
                        "subject": "AAPL",
                    }
                ],
            },
            "decision_time": now.isoformat(),
        },
    )
    assert unsupported_verification.status_code == 200, unsupported_verification.text
    v_body = unsupported_verification.json()
    assert v_body["employees"]["qa-runner"]["decision"] == "FAIL", v_body
    assert v_body["employees"]["hallucination-critic-worker"]["status"] == "DEGRADED", v_body
    assert v_body["employees"]["hallucination-critic-worker"]["namespace"] == "qa-hallucination-reference"
    assert v_body["qa_head"]["binding"] is False
    assert v_body["dispatch"]["mutation_allowed"] is False

    mismatched_verification = client.post(
        "/qa/v1/verifications/ver-1/assess",
        json={
            "verification_id": "ver-mismatch",
            "artifact": {
                "artifact_version_id": str(uuid4()),
                "artifact_type": "research_packet",
                "producer": "research-supervisor",
                "fund_id": str(v_fund),
                "trace_id": str(v_trace),
                "claims": [{"claim_index": 0, "text": "무관", "kind": "inference"}],
            },
            "decision_time": now.isoformat(),
        },
    )
    assert mismatched_verification.status_code == 409, mismatched_verification.text
    assert mismatched_verification.json()["detail"]["error_code"] == "VERIFICATION_ID_MISMATCH"

    print(
        "ok - QA Domain API 6개 영역(qa-check/ops/trace/tool-permission/incident/verification-mandate) "
        "점검 통과 (evidence/check는 OPENAI_API_KEY 필요 - 제외)"
    )
