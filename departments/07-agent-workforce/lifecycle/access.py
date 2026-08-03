#!/usr/bin/env python3
"""Y4: Access Lifecycle — 권한 요청·부여·회수 (HR-04 Lifecycle Coordinator).

소유: 영주 (Agent Workforce 인사팀)
근거: docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md 4.3, 6.4(Joiner/Mover/Leaver), 10.1(Version/Effective Time),
      docs/02-engineering/GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 3.5(request_access)
      대응 테이블: supabase/migrations/20260731000700_workforce_access_lifecycle.sql

인사팀은 **요청까지만** 한다. 실제 Identity·권한 생성은 Platform/IAM Service 만 하고,
그 결과를 provisioning_ref 로 되받아 기록한다. 여기에 LLM 은 없다.

세 테이블의 역할이 다르다 — 중복 저장하지 않는다.
  agent_tool_permissions : Profile Version 이 가질 수 있는 도구 권한 선언 (설계)
  access_requests        : 권한 요청과 승인 워크플로 (절차)
  access_assignments     : Platform/IAM 이 실제로 부여·회수한 사실 (증거)

불변식:
  1. 만료 없는 권한 요청을 만들 수 없다 (10.1). expires_at 은 요청 시점 이후여야 한다.
  2. 요청자는 자기 요청을 승인할 수 없다 (HR-04 금지: assign_self_as_approver).
  3. 인사팀은 부여를 수행하지 않는다. provisioning_ref 없이 ACTIVE 부여를 만들 수 없다.
  4. 회수는 증거 없이 완료되지 않는다 (6.4 Leaver: Revocation Evidence 와 종료 시각).
  5. 승인되지 않은 요청은 부여로 넘어갈 수 없다.

자체 점검: python departments/07-agent-workforce/lifecycle/access.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ResourceKind(str, Enum):
    TOOL = "TOOL"
    DATA = "DATA"
    ENVIRONMENT = "ENVIRONMENT"


class Environment(str, Enum):
    DEVELOPMENT = "DEVELOPMENT"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    PRODUCTION = "PRODUCTION"


class RequestStatus(str, Enum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PROVISIONED = "PROVISIONED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class AssignmentStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class SelfApprovalError(Exception):
    """요청자가 자기 권한 요청을 승인하려 함 (HR-04 금지 행위)."""


class MissingProvisioningError(Exception):
    """Platform/IAM 의 provisioning_ref 없이 부여를 만들려 함."""


class MissingRevocationEvidenceError(Exception):
    """회수 증거 없이 권한을 회수 완료로 처리하려 함."""


class IllegalTransition(Exception):
    """허용되지 않은 상태 전이."""


@dataclass(frozen=True)
class AccessRequest:
    """workforce.access_requests 한 행. 컬럼과 1:1."""

    request_id: str
    agent_id: str
    resource_kind: ResourceKind
    resource_ref: str
    environment: Environment
    justification: str
    requested_by: str
    expires_at: datetime
    requested_at: datetime
    tool_id: str | None = None
    profile_version_id: str | None = None
    scope: dict = field(default_factory=dict)
    approval_id: str | None = None
    approvals: list = field(default_factory=list)
    status: RequestStatus = RequestStatus.REQUESTED
    trace_id: str = ""

    def __post_init__(self) -> None:
        # 불변식 1 — 만료 없는(또는 이미 만료된) 권한 요청 금지.
        if self.expires_at <= self.requested_at:
            raise ValueError("expires_at 은 요청 시각 이후여야 한다 (만료 없는 권한 금지)")
        if not self.justification.strip():
            raise ValueError("justification 이 비어 있으면 권한을 요청할 수 없다")
        # DDL check 와 동일: TOOL 이면 tool_id 필수.
        if self.resource_kind is ResourceKind.TOOL and not self.tool_id:
            raise ValueError("resource_kind=TOOL 이면 tool_id 가 필요하다")


@dataclass(frozen=True)
class AccessAssignment:
    """workforce.access_assignments 한 행. Platform/IAM 이 부여한 사실 기록."""

    assignment_id: str
    request_id: str
    agent_id: str
    resource_kind: ResourceKind
    resource_ref: str
    environment: Environment
    provisioning_ref: str
    provisioned_by: str
    effective_from: datetime
    effective_to: datetime
    tool_permission_id: str | None = None
    scope: dict = field(default_factory=dict)
    revoked_at: datetime | None = None
    revocation_evidence: dict | None = None
    status: AssignmentStatus = AssignmentStatus.ACTIVE

    def __post_init__(self) -> None:
        if self.effective_to <= self.effective_from:
            raise ValueError("effective_to 는 effective_from 이후여야 한다")
        # 불변식 3 — provisioning_ref 없이 부여를 만들 수 없다.
        if not self.provisioning_ref.strip():
            raise ValueError("provisioning_ref 없이 부여를 만들 수 없다 (Platform/IAM 이 발급)")
        if self.resource_kind is ResourceKind.TOOL and not self.tool_permission_id:
            raise ValueError("resource_kind=TOOL 이면 tool_permission_id 가 필요하다")
        # 불변식 4 — 회수는 증거를 동반한다.
        if self.status is AssignmentStatus.REVOKED and (
            self.revoked_at is None or self.revocation_evidence is None
        ):
            raise ValueError("REVOKED 는 revoked_at 과 revocation_evidence 가 필요하다")


ALLOWED_REQUEST_TRANSITIONS: dict[RequestStatus, frozenset[RequestStatus]] = {
    RequestStatus.REQUESTED: frozenset(
        {RequestStatus.APPROVED, RequestStatus.REJECTED, RequestStatus.CANCELLED, RequestStatus.EXPIRED}
    ),
    RequestStatus.APPROVED: frozenset(
        {RequestStatus.PROVISIONED, RequestStatus.CANCELLED, RequestStatus.EXPIRED}
    ),
}


def approve_request(
    request: AccessRequest, *, approver: str, approval_id: str, at: datetime
) -> AccessRequest:
    """권한 요청 승인. 요청자는 자기 요청을 승인할 수 없다 (불변식 2)."""
    if RequestStatus.APPROVED not in ALLOWED_REQUEST_TRANSITIONS.get(request.status, frozenset()):
        raise IllegalTransition(f"{request.status.value} 에서 승인할 수 없다")
    if approver == request.requested_by:
        raise SelfApprovalError(
            f"요청자({request.requested_by})는 자기 권한 요청을 승인할 수 없다"
        )
    if at >= request.expires_at:
        raise IllegalTransition("이미 만료된 요청은 승인할 수 없다")

    return AccessRequest(
        **{
            **request.__dict__,
            "status": RequestStatus.APPROVED,
            "approval_id": approval_id,
            "approvals": [*request.approvals, {"approver": approver, "at": at.isoformat()}],
        }
    )


def provision(
    request: AccessRequest,
    *,
    assignment_id: str,
    provisioning_ref: str,
    provisioned_by: str,
    effective_from: datetime,
    tool_permission_id: str | None = None,
) -> tuple[AccessRequest, AccessAssignment]:
    """승인된 요청을 부여 기록으로 남긴다.

    이 함수는 권한을 **생성하지 않는다.** Platform/IAM 이 이미 부여하고 돌려준
    provisioning_ref 를 기록할 뿐이다 (불변식 3).
    """
    if request.status is not RequestStatus.APPROVED:
        raise IllegalTransition(
            f"승인되지 않은 요청은 부여할 수 없다 (현재 {request.status.value})"
        )
    if not provisioning_ref.strip():
        raise MissingProvisioningError("Platform/IAM 의 provisioning_ref 가 필요하다")

    assignment = AccessAssignment(
        assignment_id=assignment_id,
        request_id=request.request_id,
        agent_id=request.agent_id,
        resource_kind=request.resource_kind,
        resource_ref=request.resource_ref,
        environment=request.environment,
        scope=request.scope,
        tool_permission_id=tool_permission_id,
        provisioning_ref=provisioning_ref,
        provisioned_by=provisioned_by,
        effective_from=effective_from,
        # 만료는 요청의 expires_at 을 넘길 수 없다 — 요청보다 오래 사는 권한 금지.
        effective_to=request.expires_at,
    )
    updated = AccessRequest(**{**request.__dict__, "status": RequestStatus.PROVISIONED})
    return updated, assignment


def revoke(
    assignment: AccessAssignment, *, at: datetime, evidence: dict
) -> AccessAssignment:
    """권한 회수. 증거 없이 완료되지 않는다 (불변식 4 — 6.4 Leaver)."""
    if assignment.status is AssignmentStatus.REVOKED:
        raise IllegalTransition("이미 회수된 부여다")
    if not evidence:
        raise MissingRevocationEvidenceError("회수 증거(revocation_evidence)가 필요하다")

    return AccessAssignment(
        **{
            **assignment.__dict__,
            "status": AssignmentStatus.REVOKED,
            "revoked_at": at,
            "revocation_evidence": evidence,
        }
    )


def find_expired(
    assignments: list[AccessAssignment], *, now: datetime
) -> list[AccessAssignment]:
    """만료됐는데 아직 ACTIVE 인 부여. Dormant Identity 0 KPI 의 입력."""
    return [
        a for a in assignments if a.status is AssignmentStatus.ACTIVE and a.effective_to <= now
    ]


# ---------------------------------------------------------------------------
# Repository 인터페이스 + In-Memory 구현 (api/app.py가 dict 대신 이걸 쓴다)
# ---------------------------------------------------------------------------


class AccessRepository:
    """조회·저장 인터페이스. 실제 구현은 workforce.access_requests/access_assignments에 반영한다."""

    def get_request(self, request_id: str) -> AccessRequest | None:
        raise NotImplementedError

    def save_request(self, request: AccessRequest) -> None:
        """새 요청이면 insert, 이미 있으면(같은 request_id) 전체 행을 갱신한다."""
        raise NotImplementedError

    def get_assignment(self, assignment_id: str) -> AccessAssignment | None:
        raise NotImplementedError

    def save_assignment(self, assignment: AccessAssignment) -> None:
        """새 부여면 insert, 이미 있으면(같은 assignment_id) 전체 행을 갱신한다."""
        raise NotImplementedError

    def list_assignments_by_agent(self, agent_id: str) -> list[AccessAssignment]:
        raise NotImplementedError


class InMemoryAccessRepository(AccessRepository):
    def __init__(self) -> None:
        self._requests: dict[str, AccessRequest] = {}
        self._assignments: dict[str, AccessAssignment] = {}

    def get_request(self, request_id: str) -> AccessRequest | None:
        return self._requests.get(request_id)

    def save_request(self, request: AccessRequest) -> None:
        self._requests[request.request_id] = request

    def get_assignment(self, assignment_id: str) -> AccessAssignment | None:
        return self._assignments.get(assignment_id)

    def save_assignment(self, assignment: AccessAssignment) -> None:
        self._assignments[assignment.assignment_id] = assignment

    def list_assignments_by_agent(self, agent_id: str) -> list[AccessAssignment]:
        return [a for a in self._assignments.values() if a.agent_id == agent_id]


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta, timezone

    t0 = datetime(2026, 7, 31, tzinfo=timezone.utc)
    t_exp = t0 + timedelta(days=30)

    def req(**over) -> AccessRequest:
        base = dict(
            request_id="req-1", agent_id="a1",
            resource_kind=ResourceKind.DATA, resource_ref="market-api:read",
            environment=Environment.SHADOW, justification="Shadow 관찰에 필요",
            requested_by="hr-04", expires_at=t_exp, requested_at=t0, trace_id="t1",
        )
        base.update(over)
        return AccessRequest(**base)

    # 1) 정상 요청.
    r = req()
    assert r.status is RequestStatus.REQUESTED

    # 2) 불변식 1 — 만료 없는/지난 요청 거부.
    for bad in (t0, t0 - timedelta(days=1)):
        try:
            req(expires_at=bad)
            raise AssertionError("만료 없는 요청이 통과함")
        except ValueError:
            pass

    # 3) 사유 없는 요청 거부.
    try:
        req(justification="   ")
        raise AssertionError("사유 없는 요청이 통과함")
    except ValueError:
        pass

    # 4) TOOL 인데 tool_id 없으면 거부 (DDL check 와 동일).
    try:
        req(resource_kind=ResourceKind.TOOL)
        raise AssertionError("tool_id 없는 TOOL 요청이 통과함")
    except ValueError:
        pass

    # 5) 불변식 2 — 요청자 자기승인 차단.
    try:
        approve_request(r, approver="hr-04", approval_id="ap-1", at=t0)
        raise AssertionError("자기승인이 통과함")
    except SelfApprovalError:
        pass

    # 6) 독립 승인자면 통과.
    approved = approve_request(r, approver="ceo-office", approval_id="ap-1", at=t0)
    assert approved.status is RequestStatus.APPROVED
    assert approved.approval_id == "ap-1" and len(approved.approvals) == 1

    # 7) 불변식 5 — 승인 안 된 요청은 부여 불가.
    try:
        provision(r, assignment_id="as-1", provisioning_ref="iam-1",
                  provisioned_by="platform", effective_from=t0)
        raise AssertionError("미승인 요청이 부여됨")
    except IllegalTransition:
        pass

    # 8) 불변식 3 — provisioning_ref 없이 부여 불가.
    try:
        provision(approved, assignment_id="as-1", provisioning_ref="  ",
                  provisioned_by="platform", effective_from=t0)
        raise AssertionError("provisioning_ref 없이 부여됨")
    except MissingProvisioningError:
        pass

    # 9) 정상 부여 — 만료는 요청의 expires_at 을 그대로 따른다.
    updated, asg = provision(approved, assignment_id="as-1", provisioning_ref="iam-1",
                             provisioned_by="platform-iam", effective_from=t0)
    assert updated.status is RequestStatus.PROVISIONED
    assert asg.status is AssignmentStatus.ACTIVE
    assert asg.effective_to == t_exp, "부여가 요청보다 오래 살면 안 된다"
    assert asg.provisioning_ref == "iam-1"

    # 10) 불변식 4 — 증거 없는 회수 차단.
    try:
        revoke(asg, at=t0 + timedelta(days=1), evidence={})
        raise AssertionError("증거 없는 회수가 통과함")
    except MissingRevocationEvidenceError:
        pass

    # 11) 정상 회수.
    revoked = revoke(asg, at=t0 + timedelta(days=1),
                     evidence={"ticket": "IAM-77", "verified_by": "platform"})
    assert revoked.status is AssignmentStatus.REVOKED
    assert revoked.revoked_at is not None and revoked.revocation_evidence

    # 12) 이미 회수된 것은 재회수 불가.
    try:
        revoke(revoked, at=t0 + timedelta(days=2), evidence={"x": 1})
        raise AssertionError("재회수가 통과함")
    except IllegalTransition:
        pass

    # 13) 만료된 요청은 승인 불가.
    try:
        approve_request(r, approver="ceo-office", approval_id="ap-2", at=t_exp)
        raise AssertionError("만료된 요청이 승인됨")
    except IllegalTransition:
        pass

    # 14) 만료됐는데 ACTIVE 로 남은 부여 탐지 (Dormant Identity 0).
    stale = find_expired([asg], now=t_exp + timedelta(days=1))
    assert len(stale) == 1
    assert find_expired([asg], now=t0) == []
    assert find_expired([revoked], now=t_exp + timedelta(days=1)) == [], "회수된 건 대상 아님"

    print("ok - Access Lifecycle 불변식 14개 점검 통과")
