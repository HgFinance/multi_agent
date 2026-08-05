#!/usr/bin/env python3
"""Workforce Domain API — lifecycle/access.py, improvements/{candidate,workflow}.py,
scorecard/cost.py를 감싸는 FastAPI 래퍼.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/02-engineering/GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 3절(workforce-api),
      departments/03-risk/api/app.py·departments/06-ai-qa-audit/api/app.py 패턴.

여기엔 새 판정 로직이 없다. access.py의 승인/부여/회수 상태 전이, workflow.py의
자기승인 차단·QA 근거 게이트, cost.py의 Scorecard 집계가 이미 하는 일을 얇게 감싈 뿐이다.

저장소는 기본 In-Memory다. DATABASE_URL이 설정돼 있으면 Access(lifecycle/
postgres_access_repository.py)·Improvements(improvements/repository.py)가 Postgres
구현으로 전환한다. Scorecard/Budget/Roster는 원래 저장소 자체가 없었어서(In-Memory
대체가 의미 없다 - 실제로 등록된 Agent를 보는 게 목적이라) - DATABASE_URL이 없으면
관련 GET/POST 엔드포인트가 501로 막고, Scorecard의 기존 POST 데모 엔드포인트(호출자가
Snapshot을 직접 실어 보냄)만 그대로 동작한다.

스펙과 의도적으로 다른 부분(투명하게 남긴다):
  - POST /workforce/v1/departments/{code}/scorecard, POST /workforce/v1/budget-assessments는
    스펙 3.4가 GET인데 POST로 뒀다 - Snapshot 저장소가 없던 시절 데모용으로, 호출자가
    Snapshot 자체를 요청 본문에 실어 보낸다. 지금은 GET /workforce/v1/departments/{code}/
    scorecard, GET /workforce/v1/agents/{agent_id}/budget-assessment가 스펙이 원래
    의도한 형태(postgres_scorecard_repository.py가 실제 Snapshot을 조회) - POST는
    호환을 위해 남겨뒀다.
  - GET /workforce/v1/skill-gap(스펙 3.4)은 아직 없다 - Roster(3.1)만 이번(HR-02)에
    구현했다.
  - POST /workforce/v1/agents/{agent_id}/status의 idempotency_key는 스펙 필드를
    그대로 받지만 실제 중복 실행 방지에는 쓰지 않는다(workforce 스키마에 이 값을 저장할
    테이블이 없다 - governance.case_events만 idempotency_key 컬럼을 갖는데 그건 GOV-02
    영역이라 여기서 빌려 쓰지 않는다).

실행: uvicorn app:app --app-dir departments/07-agent-workforce/api
자체 점검: python departments/07-agent-workforce/api/app.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# 저장소 루트의 .env를 읽는다 - 이미 설정된 프로세스 환경변수는 덮어쓰지 않는다
# (override=False 기본값). CEO Office api/app.py와 동일한 이유(2026-08-05) - 이게
# 없으면 .env에 DATABASE_URL이 있어도 이 프로세스는 그 값을 못 보고 조용히 In-Memory/
# 501 경로로 빠진다.
load_dotenv()

_BASE = Path(__file__).resolve().parent.parent
_LIFECYCLE_DIR = _BASE / "lifecycle"
_IMPROVEMENTS_DIR = _BASE / "improvements"
_SCORECARD_DIR = _BASE / "scorecard"
_ROSTER_DIR = _BASE / "roster"
for _p in (_LIFECYCLE_DIR, _IMPROVEMENTS_DIR, _SCORECARD_DIR, _ROSTER_DIR):
    sys.path.insert(0, str(_p))

from access import (
    AccessAssignment,
    AccessRequest,
    Environment,
    IllegalTransition,
    InMemoryAccessRepository,
    MissingProvisioningError,
    MissingRevocationEvidenceError,
    ResourceKind,
    approve_request,
    provision,
    revoke,
)
from access import (
    SelfApprovalError as AccessSelfApprovalError,
)
from candidate import ImprovementCandidate
from cost import (
    CapacitySnapshot,
    CostSnapshot,
    TokenBudget,
    assess_budget,
    build_department_scorecard,
)
from roster import (
    AgentNotFoundError,
    AgentSummary,
    EmploymentStatus,
    MissingActivationEvidenceError,
    ProfileVersionRow,
    ProfileVersionSubmission,
    StatusChangeRequest,
    ToolAllowlistMissingError,
    UnverifiedActivationEvidenceError,
    validate_status_change,
    verify_activation_evidence,
)
from activation_evidence import InMemoryActivationEvidenceRepository
from workflow import (
    Approval,
    CandidateStatus,
    ImprovementWorkflow,
    InMemoryImprovementRepository,
    MissingEvidenceError,
)
from workflow import (
    IllegalTransition as CandidateIllegalTransition,
)
from workflow import (
    SelfApprovalError as CandidateSelfApprovalError,
)

try:
    from postgres_access_repository import PostgresAccessRepository
except ImportError:
    PostgresAccessRepository = None  # type: ignore[assignment,misc]

try:
    from repository import PostgresImprovementRepository
except ImportError:
    PostgresImprovementRepository = None  # type: ignore[assignment,misc]

try:
    from postgres_scorecard_repository import PostgresScorecardRepository
except ImportError:
    PostgresScorecardRepository = None  # type: ignore[assignment,misc]

try:
    from postgres_roster_repository import PostgresRosterRepository
except ImportError:
    PostgresRosterRepository = None  # type: ignore[assignment,misc]

try:
    from activation_evidence import PostgresActivationEvidenceRepository
except ImportError:
    PostgresActivationEvidenceRepository = None  # type: ignore[assignment,misc]

_WORKFORCE_EVENTS_DIR = _BASE / "workforce_events"
sys.path.insert(0, str(_WORKFORCE_EVENTS_DIR))

from redis_event_bus import (
    DEFAULT_GROUP as _WORKFORCE_EVENT_GROUP,
)
from redis_event_bus import (
    DEFAULT_STREAM as _WORKFORCE_EVENT_STREAM,
)
from redis_event_bus import (
    RedisEventBus,
    WorkforceEventBusError,
)

# --- Request 모델 --------------------------------------------------------------


class AccessRequestIn(BaseModel):
    agent_id: str
    resource_kind: ResourceKind
    resource_ref: str
    environment: Environment
    justification: str = Field(min_length=1)
    requested_by: str
    expires_at: datetime
    requested_at: datetime
    tool_id: str | None = None
    profile_version_id: str | None = None
    scope: dict = {}
    trace_id: str = ""


class ApproveRequestIn(BaseModel):
    approver: str
    approval_id: str
    at: datetime


class ProvisionRequestIn(BaseModel):
    provisioning_ref: str
    provisioned_by: str
    effective_from: datetime
    tool_permission_id: str | None = None


class RevokeRequestIn(BaseModel):
    at: datetime
    evidence: dict = Field(min_length=1)


class TransitionRequestIn(BaseModel):
    to_status: CandidateStatus
    actor: str
    reason: str
    at: datetime
    approver: str | None = None
    qa_eval_run_id: str | None = None


class CostSnapshotIn(BaseModel):
    agent_id: str
    profile_version_id: str
    window_start: datetime
    window_end: datetime
    input_tokens: int = 0
    output_tokens: int = 0
    model_cost: str = "0"
    tool_cost: str = "0"
    infra_cost: str = "0"
    case_count: int = 0
    currency: str = "USD"


class CapacitySnapshotIn(BaseModel):
    window_start: datetime
    window_end: datetime
    arrivals: int = 0
    queue_p95_ms: str | None = None
    duration_p95_ms: str | None = None
    retry_rate: str | None = None
    error_rate: str | None = None
    utilization: str | None = None
    department_id: str | None = None
    agent_id: str | None = None


class ScorecardRequest(BaseModel):
    window_start: datetime
    window_end: datetime
    capacity: CapacitySnapshotIn | None = None
    cost_snapshots: list[CostSnapshotIn] = []
    finding_count: int | None = None
    rework_rate: str | None = None


class ProfileVersionSubmissionIn(BaseModel):
    """POST /workforce/v1/agents/{agent_id}/profile-versions Request.

    agent_profile_versions 컬럼과 1:1 (GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 3.1).
    """

    model_id: str
    prompt_artifact_path: str
    skill_manifest: dict
    tool_allowlist: dict
    data_scopes: dict
    memory_namespace: str
    token_budget: dict
    sla: dict
    eval_requirements: dict
    forbidden_actions: list
    effective_from: datetime
    effective_to: datetime | None = None


class StatusChangeRequestIn(BaseModel):
    """POST /workforce/v1/agents/{agent_id}/status Request (스펙 3.1 change_status).

    idempotency_key는 스펙 필드 그대로 받지만, 이를 저장·조회할 별도 테이블이
    workforce 스키마에 없어(governance.case_events만 idempotency_key 컬럼을 가진다 -
    GOV-02 영역) 아직 실제 중복 실행 방지에는 쓰지 않는다 - 투명하게 남겨둔다.
    """

    to_status: EmploymentStatus
    profile_version_id: str
    reason: str
    idempotency_key: str
    qa_eval_run_id: str | None = None
    ceo_approval_id: str | None = None


class BudgetAssessmentRequest(BaseModel):
    agent_id: str
    employee_code: str
    department_code: str
    per_case_tokens: int
    daily_tokens: int
    cost_snapshots: list[CostSnapshotIn] = []


def _dec(v: str | None) -> Decimal | None:
    return None if v is None else Decimal(v)


def _cost_snapshot(d: CostSnapshotIn) -> CostSnapshot:
    return CostSnapshot(
        agent_id=d.agent_id, profile_version_id=d.profile_version_id,
        window_start=d.window_start, window_end=d.window_end,
        input_tokens=d.input_tokens, output_tokens=d.output_tokens,
        model_cost=Decimal(d.model_cost), tool_cost=Decimal(d.tool_cost),
        infra_cost=Decimal(d.infra_cost), case_count=d.case_count, currency=d.currency,
    )


def _capacity_snapshot(d: CapacitySnapshotIn) -> CapacitySnapshot:
    return CapacitySnapshot(
        window_start=d.window_start, window_end=d.window_end, arrivals=d.arrivals,
        queue_p95_ms=_dec(d.queue_p95_ms), duration_p95_ms=_dec(d.duration_p95_ms),
        retry_rate=_dec(d.retry_rate), error_rate=_dec(d.error_rate),
        utilization=_dec(d.utilization), department_id=d.department_id, agent_id=d.agent_id,
    )


def _access_request_dict(r: AccessRequest) -> dict:
    return {
        "request_id": r.request_id, "agent_id": r.agent_id, "resource_kind": r.resource_kind.value,
        "resource_ref": r.resource_ref, "environment": r.environment.value, "status": r.status.value,
        "expires_at": r.expires_at.isoformat(), "approval_id": r.approval_id,
    }


def _agent_summary_dict(a: AgentSummary) -> dict:
    current_profile_version = None
    if a.current_profile_version is not None:
        pv = a.current_profile_version
        current_profile_version = {
            "profile_version_id": pv.profile_version_id, "version": pv.version,
            "model": {"provider": pv.model.provider, "model_name": pv.model.model_name,
                      "model_version": pv.model.model_version},
            "memory_namespace": pv.memory_namespace, "status": pv.status.value,
        }
    return {
        "agent_id": a.agent_id, "employee_code": a.employee_code, "display_name": a.display_name,
        "department_code": a.department_code, "role_code": a.role_code,
        "employment_status": a.employment_status.value, "current_version": a.current_version,
        "current_profile_version": current_profile_version,
        "owner_user_id": a.owner_user_id, "backup_owner_user_id": a.backup_owner_user_id,
    }


def _profile_version_row_dict(row: ProfileVersionRow) -> dict:
    return {
        "profile_version_id": row.profile_version_id, "agent_id": row.agent_id,
        "version": row.version, "artifact_hash": row.artifact_hash, "status": row.status.value,
    }


def _access_assignment_dict(a: AccessAssignment) -> dict:
    return {
        "assignment_id": a.assignment_id, "request_id": a.request_id, "agent_id": a.agent_id,
        "resource_kind": a.resource_kind.value, "resource_ref": a.resource_ref, "status": a.status.value,
        "provisioning_ref": a.provisioning_ref, "effective_from": a.effective_from.isoformat(),
        "effective_to": a.effective_to.isoformat(),
    }


# --- App ----------------------------------------------------------------------


app = FastAPI(title="Workforce Domain API", version="v1")

if os.environ.get("DATABASE_URL") and PostgresAccessRepository is not None:
    _access_repo = PostgresAccessRepository.connect(os.environ["DATABASE_URL"])
else:
    _access_repo = InMemoryAccessRepository()

if os.environ.get("DATABASE_URL") and PostgresImprovementRepository is not None:
    _improvement_repo = PostgresImprovementRepository.connect(os.environ["DATABASE_URL"])
else:
    _improvement_repo = InMemoryImprovementRepository()
_workflow = ImprovementWorkflow(_improvement_repo)


# --- F19 improvement-worker Event Consumer (workforce_events/worker.py가 이 함수들을 쓴다) ---
#
# workforce.eval.v1(QA Eval 결과)을 아직 QA/감사본부가 발행하지 않는다(2026-08-03 확인) -
# 그래도 배관(Redis Streams + Consumer Group + dedupe + ACK)은 governance_events와 같은
# 방식으로 실제로 구현하고 검증한다. Payload Contract(candidate_id, result=PASS|FAIL)는
# QA의 실제 Eval Runner가 정해지기 전까지의 잠정안이다 - 최종 확정본과 다를 수 있다.
_KNOWN_NON_EVAL_EVENTS: frozenset[str] = frozenset({
    "workforce.hiring_request.v1",
    "workforce.profile_candidate.v1",
    "workforce.lifecycle_changed.v1",
    "workforce.access_request.v1",
})

_workforce_event_bus_instance: RedisEventBus | None = None


def _workforce_event_bus() -> RedisEventBus | None:
    """hf:workforce Redis Stream을 실제 호출 시점에만 연결한다."""

    global _workforce_event_bus_instance
    redis_url = os.environ.get("WORKFORCE_EVENT_REDIS_URL") or os.environ.get("REDIS_URL")
    if not redis_url:
        return None
    if _workforce_event_bus_instance is None:
        import redis

        try:
            dedupe_ttl_seconds = int(
                os.environ.get("WORKFORCE_EVENT_DEDUPE_TTL_SECONDS", "604800")
            )
        except ValueError as exc:
            raise WorkforceEventBusError(
                "WORKFORCE_EVENT_DEDUPE_TTL_SECONDS must be an integer"
            ) from exc

        _workforce_event_bus_instance = RedisEventBus(
            redis.Redis.from_url(redis_url),
            stream=os.environ.get("WORKFORCE_EVENT_STREAM", _WORKFORCE_EVENT_STREAM),
            group=os.environ.get("WORKFORCE_EVENT_GROUP", _WORKFORCE_EVENT_GROUP),
            consumer=os.environ.get("WORKFORCE_EVENT_CONSUMER", "workforce-api"),
            dedupe_ttl_seconds=dedupe_ttl_seconds,
        )
    return _workforce_event_bus_instance


def _handle_workforce_event(event: dict) -> None:
    """workforce.eval.v1을 EVALUATING -> SHADOW/REJECTED 전이로 변환한다.

    여기엔 판정 로직이 없다 - Eval 결과를 다음 상태로 매핑만 하고, 자기승인 차단·전이
    허용 여부 같은 실제 규칙은 workflow.py의 ImprovementWorkflow가 그대로 한다(candidate가
    EVALUATING이 아니면 IllegalTransition을 내고, 그 예외는 여기서 삼키지 않는다 - 처리
    중 예외면 ACK하지 않고 재시도 대상으로 남는다).
    """

    event_type = event.get("event_type")
    if event_type in _KNOWN_NON_EVAL_EVENTS:
        return  # 다른 Consumer Group을 위한 Event - 조용히 넘긴다(ACK, 전이 없음).
    if event_type != "workforce.eval.v1":
        raise WorkforceEventBusError(
            f"improvement-worker가 모르는 Event입니다: {event_type}"
        )

    payload = event.get("payload") or {}
    candidate_id = payload.get("candidate_id")
    result = payload.get("result")
    if not candidate_id or result not in ("PASS", "FAIL"):
        raise WorkforceEventBusError(
            f"workforce.eval.v1 payload에 candidate_id/result(PASS|FAIL)가 없습니다 "
            f"(event_id={event.get('event_id')})"
        )

    candidate = _improvement_repo.get_candidate(candidate_id)
    if candidate is None:
        raise WorkforceEventBusError(f"candidate_id={candidate_id}를 찾을 수 없습니다")

    to_status = CandidateStatus.SHADOW if result == "PASS" else CandidateStatus.REJECTED
    occurred_at = event.get("occurred_at")
    at = datetime.fromisoformat(occurred_at) if occurred_at else datetime.now(timezone.utc)
    updated = _workflow.transition(
        candidate, to_status, actor="qa-eval-consumer",
        reason=payload.get("reason", f"QA Eval 결과: {result}"), at=at,
    )
    _improvement_repo.save_candidate(updated)


# Scorecard/Budget용 Snapshot 조회 Repository - In-Memory 대체가 없다(원래 저장소가
# 아예 없었다, config.yaml not_started). DATABASE_URL 없으면 None - 아래 GET 엔드포인트가
# 501로 막고, 기존 POST 데모 엔드포인트(호출자가 Snapshot을 직접 보냄)는 그대로 동작한다.
if os.environ.get("DATABASE_URL") and PostgresScorecardRepository is not None:
    _scorecard_repo = PostgresScorecardRepository.connect(os.environ["DATABASE_URL"])
else:
    _scorecard_repo = None

# Roster/Profile - workforce.agent_profiles는 seed 데이터 이전 In-Memory 대체가 의미
# 없다(QA/Model Gateway가 보려는 건 실제로 등록된 Agent다) - Scorecard와 같은 이유로
# DATABASE_URL 없으면 None -> 아래 엔드포인트가 501로 막는다.
if os.environ.get("DATABASE_URL") and PostgresRosterRepository is not None:
    _roster_repo = PostgresRosterRepository.connect(os.environ["DATABASE_URL"])
else:
    _roster_repo = None

# P0-3(2026-08-05, TEAM_YOUNGJU_CEO_HR_GUIDE.md) - ACTIVE 전이 증거(qa_eval_run_id/
# ceo_approval_id) 실재성 검증. Roster와 같은 이유로 DATABASE_URL 없으면 In-Memory -
# 개발 환경에서도 이 게이트 자체는 계약대로 동작해야 self-check가 의미 있다.
if os.environ.get("DATABASE_URL") and PostgresActivationEvidenceRepository is not None:
    _activation_evidence_repo = PostgresActivationEvidenceRepository.connect(os.environ["DATABASE_URL"])
else:
    _activation_evidence_repo = InMemoryActivationEvidenceRepository()


@app.exception_handler(ValueError)
def _on_value_error(request, exc: ValueError):
    return JSONResponse(status_code=400, content={
        "error_code": type(exc).__name__, "message": str(exc), "detail": {}, "trace_id": None,
    })


for _exc_type in (
    AccessSelfApprovalError, MissingProvisioningError, MissingRevocationEvidenceError,
    IllegalTransition, CandidateSelfApprovalError, MissingEvidenceError, CandidateIllegalTransition,
    MissingActivationEvidenceError, UnverifiedActivationEvidenceError, ToolAllowlistMissingError,
):
    @app.exception_handler(_exc_type)
    def _on_domain_error(request, exc, _status={  # noqa: B006 - 클로저 캡처용 기본값 트릭
        AccessSelfApprovalError: 403, MissingProvisioningError: 409,
        MissingRevocationEvidenceError: 409, IllegalTransition: 409,
        CandidateSelfApprovalError: 403, MissingEvidenceError: 409, CandidateIllegalTransition: 409,
        MissingActivationEvidenceError: 409,
        # P0-3 - 칸은 채워졌지만 실재하지 않는/조건 미충족 증거는 403(신원 위조에 준하는
        # 취급, CEO 쪽 UnverifiedActorUserError와 같은 방향). tool_allowlist 누락은
        # 증거 위조가 아니라 설정 미비라 기존 MissingActivationEvidenceError와 같은 409.
        UnverifiedActivationEvidenceError: 403, ToolAllowlistMissingError: 409,
    }):
        return JSONResponse(status_code=_status[type(exc)], content={
            "error_code": type(exc).__name__, "message": str(exc), "detail": {}, "trace_id": None,
        })


@app.exception_handler(AgentNotFoundError)
def _on_agent_not_found(request, exc: AgentNotFoundError):
    return JSONResponse(status_code=404, content={
        "error_code": "AgentNotFoundError", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(WorkforceEventBusError)
def _on_workforce_event_bus_error(request, exc: WorkforceEventBusError):
    return JSONResponse(status_code=503, content={
        "error_code": "WORKFORCE_EVENT_BUS_ERROR", "message": str(exc), "detail": {}, "trace_id": None,
    })


# --- 3.5 Access ------------------------------------------------------------------


@app.post("/workforce/v1/access-requests")
def request_access(body: AccessRequestIn):
    request_id = str(uuid4())
    req = AccessRequest(
        request_id=request_id, agent_id=body.agent_id, resource_kind=body.resource_kind,
        resource_ref=body.resource_ref, environment=body.environment, justification=body.justification,
        requested_by=body.requested_by, expires_at=body.expires_at, requested_at=body.requested_at,
        tool_id=body.tool_id, profile_version_id=body.profile_version_id, scope=body.scope,
        trace_id=body.trace_id,
    )
    _access_repo.save_request(req)
    return _access_request_dict(req)


@app.post("/workforce/v1/access-requests/{request_id}/approve")
def approve_access_request(request_id: str, body: ApproveRequestIn):
    req = _access_repo.get_request(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"access_request {request_id} 없음")
    updated = approve_request(req, approver=body.approver, approval_id=body.approval_id, at=body.at)
    _access_repo.save_request(updated)
    return _access_request_dict(updated)


@app.post("/workforce/v1/access-requests/{request_id}/provision")
def provision_access(request_id: str, body: ProvisionRequestIn):
    req = _access_repo.get_request(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"access_request {request_id} 없음")
    assignment_id = str(uuid4())
    updated_req, assignment = provision(
        req, assignment_id=assignment_id, provisioning_ref=body.provisioning_ref,
        provisioned_by=body.provisioned_by, effective_from=body.effective_from,
        tool_permission_id=body.tool_permission_id,
    )
    _access_repo.save_request(updated_req)
    _access_repo.save_assignment(assignment)
    return _access_assignment_dict(assignment)


@app.post("/workforce/v1/access-assignments/{assignment_id}/revoke")
def revoke_access(assignment_id: str, body: RevokeRequestIn):
    assignment = _access_repo.get_assignment(assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail=f"access_assignment {assignment_id} 없음")
    updated = revoke(assignment, at=body.at, evidence=body.evidence)
    _access_repo.save_assignment(updated)
    return _access_assignment_dict(updated)


@app.get("/workforce/v1/agents/{agent_id}/access")
def get_agent_access(agent_id: str):
    return {"assignments": [
        _access_assignment_dict(a) for a in _access_repo.list_assignments_by_agent(agent_id)
    ]}


# --- 3.1 Roster / Profile (HR-02) ------------------------------------------------
#
# QA·Model Gateway가 같은 Profile Version을 조회할 공식 Read API(+ 발급/전이 Write).
# 판정 로직은 없다 - roster.py의 순수 함수(compute_artifact_hash, validate_status_change)와
# postgres_roster_repository.py의 SQL 왕복이 전부다.


def _require_roster_repo() -> None:
    if _roster_repo is None:
        raise HTTPException(
            status_code=501,
            detail="DATABASE_URL 미설정 - workforce.agent_profiles 조회 불가",
        )


@app.get("/workforce/v1/roster")
def get_roster():
    _require_roster_repo()
    return {"agents": [_agent_summary_dict(a) for a in _roster_repo.list_roster()]}


@app.get("/workforce/v1/agents/{agent_id}")
def get_agent(agent_id: str):
    _require_roster_repo()
    agent = _roster_repo.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent_id={agent_id} 없음")
    return _agent_summary_dict(agent)


@app.post("/workforce/v1/agents/{agent_id}/profile-versions")
def submit_profile_version(agent_id: str, body: ProfileVersionSubmissionIn):
    """항상 새 Version을 insert한다 - 기존 Version을 수정하는 엔드포인트는 없다."""
    _require_roster_repo()
    submission = ProfileVersionSubmission(
        model_id=body.model_id, prompt_artifact_path=body.prompt_artifact_path,
        skill_manifest=body.skill_manifest, tool_allowlist=body.tool_allowlist,
        data_scopes=body.data_scopes, memory_namespace=body.memory_namespace,
        token_budget=body.token_budget, sla=body.sla, eval_requirements=body.eval_requirements,
        forbidden_actions=body.forbidden_actions, effective_from=body.effective_from,
        effective_to=body.effective_to,
    )
    row = _roster_repo.submit_profile(agent_id, submission)
    return _profile_version_row_dict(row)


@app.post("/workforce/v1/agents/{agent_id}/status")
def change_agent_status(agent_id: str, body: StatusChangeRequestIn):
    """to_status=ACTIVE는 qa_eval_run_id와 ceo_approval_id가 둘 다 있어야 한다.

    P0-3(2026-08-05): 그 둘이 실재하는 증거인지도 검증한다 - qa_eval_run_id는
    audit.eval_runs에서 이 profile_version_id를 candidate로 하는 COMPLETED 행을,
    ceo_approval_id는 governance.approvals에서 이 profile_version_id를 대상으로 한
    APPROVED CEO 결정을 가리켜야 한다(UnverifiedActivationEvidenceError -> 403).
    tool_allowlist가 비어있는 Persona도 ACTIVE로 못 간다(ToolAllowlistMissingError -> 409).
    """
    request = StatusChangeRequest(
        to_status=body.to_status, profile_version_id=body.profile_version_id, reason=body.reason,
        idempotency_key=body.idempotency_key, qa_eval_run_id=body.qa_eval_run_id,
        ceo_approval_id=body.ceo_approval_id,
    )
    validate_status_change(request)
    _require_roster_repo()
    if body.to_status is EmploymentStatus.ACTIVE:
        tool_allowlist = _roster_repo.get_profile_version_tool_allowlist(body.profile_version_id) or {}
        eval_run_status = _activation_evidence_repo.get_eval_run_status(
            body.qa_eval_run_id, body.profile_version_id
        )
        ceo_approval_decision = _activation_evidence_repo.get_ceo_approval_decision(
            body.ceo_approval_id, body.profile_version_id
        )
        verify_activation_evidence(
            eval_run_status=eval_run_status, approval_decision=ceo_approval_decision,
            tool_allowlist=tool_allowlist,
        )
    _roster_repo.change_status(agent_id, to_status=body.to_status, at=datetime.now(timezone.utc))
    return _agent_summary_dict(_roster_repo.get_agent(agent_id))


# --- 3.3 Improvement Candidate (F19) ----------------------------------------------


@app.post("/workforce/v1/improvements")
def create_improvement(body: dict[str, Any]):
    candidate = ImprovementCandidate(**body)
    _improvement_repo.save_candidate(candidate)
    return candidate.model_dump(mode="json")


@app.get("/workforce/v1/improvements/{candidate_id}")
def get_improvement(candidate_id: str):
    candidate = _improvement_repo.get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"improvement {candidate_id} 없음")
    return candidate.model_dump(mode="json")


@app.post("/workforce/v1/improvements/{candidate_id}/transitions")
def transition_improvement(candidate_id: str, body: TransitionRequestIn):
    candidate = _improvement_repo.get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"improvement {candidate_id} 없음")
    approval = None
    if body.approver is not None:
        approval = Approval(approver=body.approver, qa_eval_run_id=body.qa_eval_run_id or "",
                             reason=body.reason)
    updated = _workflow.transition(
        candidate, body.to_status, actor=body.actor, reason=body.reason, at=body.at, approval=approval,
    )
    _improvement_repo.save_candidate(updated)
    return updated.model_dump(mode="json")


@app.get("/workforce/v1/improvements/{candidate_id}/events")
def get_improvement_events(candidate_id: str):
    return {"events": [
        {"sequence": e.sequence, "from_status": e.from_status.value, "to_status": e.to_status.value,
         "actor": e.actor, "reason": e.reason, "occurred_at": e.occurred_at.isoformat(),
         "qa_eval_run_id": e.qa_eval_run_id}
        for e in _workflow.events_for(candidate_id)
    ]}


# --- 3.4 Scorecard / Budget ----------------------------------------------------------


@app.post("/workforce/v1/departments/{department_code}/scorecard")
def get_department_scorecard(department_code: str, body: ScorecardRequest):
    if body.window_end <= body.window_start:
        raise HTTPException(status_code=400, detail="window_end는 window_start 이후여야 한다")
    scorecard = build_department_scorecard(
        department_code=department_code, window_start=body.window_start, window_end=body.window_end,
        capacity=_capacity_snapshot(body.capacity) if body.capacity else None,
        cost_snapshots=[_cost_snapshot(c) for c in body.cost_snapshots],
        finding_count=body.finding_count, rework_rate=_dec(body.rework_rate),
    )
    return scorecard


@app.post("/workforce/v1/budget-assessments")
def create_budget_assessment(body: BudgetAssessmentRequest):
    budget = TokenBudget(per_case_tokens=body.per_case_tokens, daily_tokens=body.daily_tokens)
    assessment = assess_budget(
        agent_id=body.agent_id, employee_code=body.employee_code, department_code=body.department_code,
        budget=budget, snapshots=[_cost_snapshot(c) for c in body.cost_snapshots],
    )
    return _budget_assessment_dict(assessment)


def _budget_assessment_dict(assessment) -> dict:
    return {
        "agent_id": assessment.agent_id, "status": assessment.status.value,
        "recommended_action": assessment.recommended_action.value, "tokens_used": assessment.tokens_used,
        "usage_ratio": str(assessment.usage_ratio) if assessment.usage_ratio is not None else None,
        "is_control_role": assessment.is_control_role, "note": assessment.note,
    }


# --- 3.4 스펙 형태 GET 엔드포인트 (Postgres Scorecard Repository 배선) -------------------
#
# 위 POST 데모 엔드포인트는 호출자가 Snapshot을 직접 실어 보낸다(저장소 없던 시절 설계).
# 아래는 스펙 3.4가 원래 의도한 GET 형태 - workforce.cost_snapshots/capacity_snapshots를
# 실제로 조회한다. DATABASE_URL이 없으면(_scorecard_repo가 None) 501로 막는다 - 조회할
# 저장소가 없는데 빈 결과를 성공처럼 돌려주지 않는다.


@app.get("/workforce/v1/departments/{department_code}/scorecard")
def get_department_scorecard_real(department_code: str, window_start: datetime, window_end: datetime):
    if _scorecard_repo is None:
        raise HTTPException(
            status_code=501,
            detail="DATABASE_URL 미설정 - 실 Snapshot 조회 불가. POST .../scorecard로 직접 보내라",
        )
    if window_end <= window_start:
        raise HTTPException(status_code=400, detail="window_end는 window_start 이후여야 한다")
    department_id = _scorecard_repo.get_department_id(department_code)
    if department_id is None:
        raise HTTPException(status_code=404, detail=f"department_code={department_code} 없음")
    capacity = _scorecard_repo.get_capacity_snapshot(
        department_id=department_id, window_start=window_start, window_end=window_end,
    )
    cost_snapshots = _scorecard_repo.list_cost_snapshots_by_department(
        department_id, window_start=window_start, window_end=window_end,
    )
    return build_department_scorecard(
        department_code=department_code, window_start=window_start, window_end=window_end,
        capacity=capacity, cost_snapshots=cost_snapshots,
    )


@app.get("/workforce/v1/agents/{agent_id}/budget-assessment")
def get_budget_assessment_real(
    agent_id: str, employee_code: str, department_code: str,
    per_case_tokens: int, daily_tokens: int, window_start: datetime, window_end: datetime,
):
    if _scorecard_repo is None:
        raise HTTPException(
            status_code=501,
            detail="DATABASE_URL 미설정 - 실 Snapshot 조회 불가. POST .../budget-assessments로 직접 보내라",
        )
    budget = TokenBudget(per_case_tokens=per_case_tokens, daily_tokens=daily_tokens)
    snapshots = _scorecard_repo.list_cost_snapshots_by_agent(
        agent_id, window_start=window_start, window_end=window_end,
    )
    assessment = assess_budget(
        agent_id=agent_id, employee_code=employee_code, department_code=department_code,
        budget=budget, snapshots=snapshots,
    )
    return _budget_assessment_dict(assessment)


if __name__ == "__main__":
    from fastapi.testclient import TestClient

    # 이 자체 점검은 "a1"/"pv1" 같은 합성 ID로 계약을 검증한다 - DATABASE_URL이 .env에
    # 있어도(위 load_dotenv()) 실 DB로 새면 대부분의 조회가 빈 결과나 FK 에러로 깨진다.
    # CEO Office api/app.py와 같은 이유로 In-Memory로 강제한다.
    if not isinstance(_access_repo, InMemoryAccessRepository):
        _access_repo = InMemoryAccessRepository()
    if not isinstance(_improvement_repo, InMemoryImprovementRepository):
        _improvement_repo = InMemoryImprovementRepository()
        _workflow = ImprovementWorkflow(_improvement_repo)  # _workflow는 모듈 로드 시점
        # 원래 _improvement_repo를 이미 캡처했다 - 재배선 안 하면 실 DB를 계속 쓴다.
    _scorecard_repo = None
    _roster_repo = None  # 아래 5번 블록까지는 "미설정시 501"을 그대로 검증한다.
    if not isinstance(_activation_evidence_repo, InMemoryActivationEvidenceRepository):
        _activation_evidence_repo = InMemoryActivationEvidenceRepository()

    client = TestClient(app)
    t0 = "2026-08-02T00:00:00+00:00"
    t_exp = "2026-09-01T00:00:00+00:00"

    # 1. Access 요청 -> 자기승인 차단 -> 독립 승인 -> 부여 -> 회수.
    r1 = client.post("/workforce/v1/access-requests", json={
        "agent_id": "a1", "resource_kind": "DATA", "resource_ref": "market-api:read",
        "environment": "SHADOW", "justification": "Shadow 관찰", "requested_by": "hr-04",
        "expires_at": t_exp, "requested_at": t0,
    })
    assert r1.status_code == 200, r1.text
    request_id = r1.json()["request_id"]

    r2 = client.post(f"/workforce/v1/access-requests/{request_id}/approve", json={
        "approver": "hr-04", "approval_id": "ap-1", "at": t0,
    })
    assert r2.status_code == 403, r2.text  # 자기승인 차단

    r3 = client.post(f"/workforce/v1/access-requests/{request_id}/approve", json={
        "approver": "ceo-office", "approval_id": "ap-1", "at": t0,
    })
    assert r3.status_code == 200 and r3.json()["status"] == "APPROVED", r3.text

    r4 = client.post(f"/workforce/v1/access-requests/{request_id}/provision", json={
        "provisioning_ref": "iam-1", "provisioned_by": "platform-iam", "effective_from": t0,
    })
    assert r4.status_code == 200, r4.text
    assignment_id = r4.json()["assignment_id"]

    r5 = client.get("/workforce/v1/agents/a1/access")
    assert len(r5.json()["assignments"]) == 1

    r6 = client.post(f"/workforce/v1/access-assignments/{assignment_id}/revoke", json={
        "at": t0, "evidence": {"ticket": "IAM-1"},
    })
    assert r6.status_code == 200 and r6.json()["status"] == "REVOKED", r6.text

    # 2. Improvement Candidate -> 자기승인 차단 -> 독립 승인.
    r7 = client.post("/workforce/v1/improvements", json={
        "candidate_id": "ic-1", "author": "qa-department-hermes", "target_type": "PROFILE",
        "target_ref": "agent-citation-checker", "target_current_version": 3,
        "evidence_ids": ["finding-101"], "expected_effect": "인용 누락 오탐 감소",
        "risk_class": "MEDIUM", "rollback_target_version": 3,
    })
    assert r7.status_code == 200 and r7.json()["status"] == "PROPOSED", r7.text

    r8 = client.post("/workforce/v1/improvements/ic-1/transitions", json={
        "to_status": "EVALUATING", "actor": "hr", "reason": "Eval 시작", "at": t0,
    })
    assert r8.status_code == 200 and r8.json()["status"] == "EVALUATING", r8.text
    client.post("/workforce/v1/improvements/ic-1/transitions", json={
        "to_status": "SHADOW", "actor": "hr", "reason": "Shadow", "at": t0,
    })
    client.post("/workforce/v1/improvements/ic-1/transitions", json={
        "to_status": "PENDING_APPROVAL", "actor": "hr", "reason": "검토", "at": t0,
    })

    r9 = client.post("/workforce/v1/improvements/ic-1/transitions", json={
        "to_status": "APPROVED", "actor": "x", "reason": "x", "at": t0,
        "approver": "qa-department-hermes", "qa_eval_run_id": "eval-1",
    })
    assert r9.status_code == 403, r9.text  # author와 approver가 같다 -> 자기승인 차단

    r10 = client.post("/workforce/v1/improvements/ic-1/transitions", json={
        "to_status": "APPROVED", "actor": "x", "reason": "x", "at": t0,
        "approver": "ceo-office-hermes", "qa_eval_run_id": "eval-1",
    })
    assert r10.status_code == 200 and r10.json()["status"] == "APPROVED", r10.text

    r11 = client.get("/workforce/v1/improvements/ic-1/events")
    assert len(r11.json()["events"]) == 4

    # 3. Scorecard - Snapshot 없으면 0이 아니라 None.
    r12 = client.post("/workforce/v1/departments/07-agent-workforce/scorecard", json={
        "window_start": t0, "window_end": t_exp, "cost_snapshots": [],
    })
    assert r12.status_code == 200 and r12.json()["cost"] is None, r12.text

    r13 = client.post("/workforce/v1/departments/07-agent-workforce/scorecard", json={
        "window_start": t0, "window_end": t_exp,
        "cost_snapshots": [{"agent_id": "a1", "profile_version_id": "pv1",
                            "window_start": t0, "window_end": t_exp, "input_tokens": 100,
                            "output_tokens": 100, "model_cost": "1", "case_count": 1}],
    })
    assert r13.status_code == 200 and r13.json()["cost"]["case_count"] == 1, r13.text

    # 4. Budget Assessment - 예산 초과 시 강등 제안.
    r14 = client.post("/workforce/v1/budget-assessments", json={
        "agent_id": "a1", "employee_code": "HR-01", "department_code": "07-agent-workforce",
        "per_case_tokens": 1000, "daily_tokens": 1000,
        "cost_snapshots": [{"agent_id": "a1", "profile_version_id": "pv1",
                            "window_start": t0, "window_end": t_exp, "input_tokens": 800,
                            "output_tokens": 800, "case_count": 1}],
    })
    assert r14.status_code == 200 and r14.json()["status"] == "EXCEEDED", r14.text
    assert r14.json()["recommended_action"] == "PROPOSE_MODEL_DOWNGRADE"

    # 5. Roster (HR-02) - DATABASE_URL 없는 이 self-check 환경에서는 501로 막혀야 한다.
    # 실제 DB 왕복 검증은 roster/postgres_roster_repository.py 자체 점검이 담당한다.
    r15 = client.get("/workforce/v1/roster")
    assert r15.status_code == 501, r15.text
    r16 = client.get("/workforce/v1/agents/a1")
    assert r16.status_code == 501, r16.text
    r17 = client.post("/workforce/v1/agents/a1/profile-versions", json={
        "model_id": "m1", "prompt_artifact_path": "x", "skill_manifest": {}, "tool_allowlist": {},
        "data_scopes": {}, "memory_namespace": "x", "token_budget": {}, "sla": {},
        "eval_requirements": {}, "forbidden_actions": [], "effective_from": t0,
    })
    assert r17.status_code == 501, r17.text
    r18 = client.post("/workforce/v1/agents/a1/status", json={
        "to_status": "ACTIVE", "profile_version_id": "pv1", "reason": "x",
        "idempotency_key": "idem-1",
    })
    assert r18.status_code == 409, r18.text  # validate_status_change가 501 확인보다 먼저 막는다

    # 6. P0-3 ACTIVE 전이 증거 실재성 게이트 - In-Memory Roster/Evidence Repository를
    # 채운 뒤에만 의미가 있으므로 여기서부터 module 전역을 재배선한다.
    from roster import InMemoryRosterRepository, ProfileVersionStatus

    _roster_repo = InMemoryRosterRepository()
    _roster_repo.seed_agent(AgentSummary(
        agent_id="a-p03", employee_code="HR-P03-SELFCHECK", display_name="p0-3-selfcheck-agent",
        department_code="hr-department", role_code="HR-01",
        employment_status=EmploymentStatus.CANDIDATE, current_version=0,
        current_profile_version=None, owner_user_id=None,
    ))
    submitted = _roster_repo.submit_profile("a-p03", ProfileVersionSubmission(
        model_id="m1", prompt_artifact_path="x", skill_manifest={},
        tool_allowlist={"read": ["capacity_snapshots"]}, data_scopes={}, memory_namespace="x",
        token_budget={}, sla={}, eval_requirements={}, forbidden_actions=[], effective_from=t0,
    ))
    pv_id = submitted.profile_version_id

    # 6a. qa_eval_run_id/ceo_approval_id가 채워져 있어도 실재하지 않으면 403.
    r19 = client.post("/workforce/v1/agents/a-p03/status", json={
        "to_status": "ACTIVE", "profile_version_id": pv_id, "reason": "x",
        "idempotency_key": "idem-p03-1", "qa_eval_run_id": "eval-ghost",
        "ceo_approval_id": "appr-ghost",
    })
    assert r19.status_code == 403 and r19.json()["error_code"] == "UnverifiedActivationEvidenceError", \
        r19.text

    # 6b. 실재하지만 아직 안 끝난 Eval(RUNNING)/미승인(PENDING)도 403.
    _activation_evidence_repo.seed_eval_run("eval-running", pv_id, "RUNNING")
    _activation_evidence_repo.seed_ceo_approval("appr-pending", pv_id, "PENDING")
    r20 = client.post("/workforce/v1/agents/a-p03/status", json={
        "to_status": "ACTIVE", "profile_version_id": pv_id, "reason": "x",
        "idempotency_key": "idem-p03-2", "qa_eval_run_id": "eval-running",
        "ceo_approval_id": "appr-pending",
    })
    assert r20.status_code == 403, r20.text

    # 6c. 다른 Profile Version을 대상으로 한 증거는 재사용할 수 없다(매칭 조건).
    _activation_evidence_repo.seed_eval_run("eval-other-pv", "다른-pv", "COMPLETED")
    r21 = client.post("/workforce/v1/agents/a-p03/status", json={
        "to_status": "ACTIVE", "profile_version_id": pv_id, "reason": "x",
        "idempotency_key": "idem-p03-3", "qa_eval_run_id": "eval-other-pv",
        "ceo_approval_id": "appr-pending",
    })
    assert r21.status_code == 403, r21.text

    # 6d. tool_allowlist가 빈 Version은 QA/CEO 증거가 완벽해도 409(실행 권한 없음).
    empty_allowlist_agent = _roster_repo.submit_profile(
        "a-p03",
        ProfileVersionSubmission(
            model_id="m1", prompt_artifact_path="x", skill_manifest={}, tool_allowlist={},
            data_scopes={}, memory_namespace="x", token_budget={}, sla={}, eval_requirements={},
            forbidden_actions=[], effective_from=t0,
        ),
    )
    _activation_evidence_repo.seed_eval_run("eval-empty-tools", empty_allowlist_agent.profile_version_id, "COMPLETED")
    _activation_evidence_repo.seed_ceo_approval("appr-empty-tools", empty_allowlist_agent.profile_version_id, "APPROVED")
    r22 = client.post("/workforce/v1/agents/a-p03/status", json={
        "to_status": "ACTIVE", "profile_version_id": empty_allowlist_agent.profile_version_id,
        "reason": "x", "idempotency_key": "idem-p03-4", "qa_eval_run_id": "eval-empty-tools",
        "ceo_approval_id": "appr-empty-tools",
    })
    assert r22.status_code == 409 and r22.json()["error_code"] == "ToolAllowlistMissingError", r22.text

    # 6e. 정상 증거(완료된 Eval + 승인된 CEO 결정 + 채워진 tool_allowlist) -> ACTIVE 성공.
    _activation_evidence_repo.seed_eval_run("eval-ok", pv_id, "COMPLETED")
    _activation_evidence_repo.seed_ceo_approval("appr-ok", pv_id, "APPROVED")
    r23 = client.post("/workforce/v1/agents/a-p03/status", json={
        "to_status": "ACTIVE", "profile_version_id": pv_id, "reason": "x",
        "idempotency_key": "idem-p03-5", "qa_eval_run_id": "eval-ok", "ceo_approval_id": "appr-ok",
    })
    assert r23.status_code == 200 and r23.json()["employment_status"] == "ACTIVE", r23.text
    print("ok - P0-3 ACTIVE 전이 증거 실재성 게이트 5개 시나리오 통과 "
          "(위조 증거 403, 미완료/미승인 403, 증거 재사용 차단 403, tool_allowlist 없음 409, 정상 승인 200)")

    print("ok - Workforce Domain API 6개 영역(access/improvements/scorecard/budget/roster/P0-3 게이트) 점검 통과")
