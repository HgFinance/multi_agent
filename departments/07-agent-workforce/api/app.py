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
구현으로 전환한다. Scorecard/Budget은 원래 저장소 자체가 없었어서(In-Memory 대체가
없다) - DATABASE_URL이 없으면 아래 GET 엔드포인트가 501로 막고, 기존 POST 데모
엔드포인트(호출자가 Snapshot을 직접 실어 보냄)만 동작한다.

스펙과 의도적으로 다른 부분(투명하게 남긴다):
  - POST /workforce/v1/departments/{code}/scorecard, POST /workforce/v1/budget-assessments는
    스펙 3.4가 GET인데 POST로 뒀다 - Snapshot 저장소가 없던 시절 데모용으로, 호출자가
    Snapshot 자체를 요청 본문에 실어 보낸다. 지금은 GET /workforce/v1/departments/{code}/
    scorecard, GET /workforce/v1/agents/{agent_id}/budget-assessment가 스펙이 원래
    의도한 형태(postgres_scorecard_repository.py가 실제 Snapshot을 조회) - POST는
    호환을 위해 남겨뒀다.
  - GET /workforce/v1/roster, GET /workforce/v1/skill-gap(스펙 3.1/3.4)은 없다 -
    workforce.agent_profiles를 조회하는 Repository가 아직 없다(HR-01 선행 작업).

실행: uvicorn app:app --app-dir departments/07-agent-workforce/api
자체 점검: python departments/07-agent-workforce/api/app.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

_BASE = Path(__file__).resolve().parent.parent
_LIFECYCLE_DIR = _BASE / "lifecycle"
_IMPROVEMENTS_DIR = _BASE / "improvements"
_SCORECARD_DIR = _BASE / "scorecard"
for _p in (_LIFECYCLE_DIR, _IMPROVEMENTS_DIR, _SCORECARD_DIR):
    sys.path.insert(0, str(_p))

from access import (  # noqa: E402
    AccessAssignment,
    AccessRequest,
    Environment,
    IllegalTransition,
    InMemoryAccessRepository,
    MissingProvisioningError,
    MissingRevocationEvidenceError,
    ResourceKind,
    SelfApprovalError as AccessSelfApprovalError,
    approve_request,
    provision,
    revoke,
)
from candidate import ImprovementCandidate  # noqa: E402
from cost import (  # noqa: E402
    CapacitySnapshot,
    CostSnapshot,
    TokenBudget,
    assess_budget,
    build_department_scorecard,
)
from workflow import (  # noqa: E402
    Approval,
    CandidateStatus,
    ImprovementWorkflow,
    InMemoryImprovementRepository,
    IllegalTransition as CandidateIllegalTransition,
    MissingEvidenceError,
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

# Scorecard/Budget용 Snapshot 조회 Repository - In-Memory 대체가 없다(원래 저장소가
# 아예 없었다, config.yaml not_started). DATABASE_URL 없으면 None - 아래 GET 엔드포인트가
# 501로 막고, 기존 POST 데모 엔드포인트(호출자가 Snapshot을 직접 보냄)는 그대로 동작한다.
if os.environ.get("DATABASE_URL") and PostgresScorecardRepository is not None:
    _scorecard_repo = PostgresScorecardRepository.connect(os.environ["DATABASE_URL"])
else:
    _scorecard_repo = None


@app.exception_handler(ValueError)
def _on_value_error(request, exc: ValueError):
    return JSONResponse(status_code=400, content={
        "error_code": type(exc).__name__, "message": str(exc), "detail": {}, "trace_id": None,
    })


for _exc_type in (
    AccessSelfApprovalError, MissingProvisioningError, MissingRevocationEvidenceError,
    IllegalTransition, CandidateSelfApprovalError, MissingEvidenceError, CandidateIllegalTransition,
):
    @app.exception_handler(_exc_type)
    def _on_domain_error(request, exc, _status={  # noqa: B006 - 클로저 캡처용 기본값 트릭
        AccessSelfApprovalError: 403, MissingProvisioningError: 409,
        MissingRevocationEvidenceError: 409, IllegalTransition: 409,
        CandidateSelfApprovalError: 403, MissingEvidenceError: 409, CandidateIllegalTransition: 409,
    }):
        return JSONResponse(status_code=_status[type(exc)], content={
            "error_code": type(exc).__name__, "message": str(exc), "detail": {}, "trace_id": None,
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

    print("ok - Workforce Domain API 4개 영역(access/improvements/scorecard/budget) 점검 통과")
