#!/usr/bin/env python3
"""신규 채용 요청 — `workforce.hiring_request.propose` 도구가 실제로 도달하는 곳.

소유: 영주 (Agent Workforce 인사팀)
근거: supabase/migrations/20260729000200_governance_workforce.sql
      (workforce.hiring_requests), orchestration/workflows/workforce-management.yaml
      1단계(hr-profile, input_contract: hiring_request)

## 왜 이 모듈이 필요했나 (2026-08-10)

`agent-workforce-supervisor`(HR-00)의 tool_allowlist에 `workforce.hiring_request.propose`가
있었지만 실제로 호출할 곳이 없었다 — `workforce.hiring_requests` 테이블 DDL은
있는데 그 위에 도메인 객체도, Repository도, API도 없었다. `app.py`의
`_KNOWN_NON_EVAL_EVENTS`에 `"workforce.hiring_request.v1"`이라는 이벤트 이름만
"안다"고 등록돼 있었을 뿐 발행하는 코드가 없었다(2026-08-10 실측). 이 모듈이
그 빈 자리를 채운다 - "제안"이 실제로 DB 행 하나를 만드는 행위가 된다.

## 상태기계가 access.py의 AccessRequest와 다른 이유

AccessRequest는 이미 존재하는 Agent의 자원 접근을 다루지만, HiringRequest는
**아직 존재하지 않는 Agent를 만들자는 제안**이다. DDL의 상태값
(DRAFT/OPEN/EVALUATING/APPROVED/REJECTED/CLOSED)을 그대로 쓴다.

  DRAFT      -> OPEN               (제출)
  OPEN       -> EVALUATING         (workforce-management.yaml 1단계로 진입 -
                                     profile-architecture-worker 가 Job Profile 초안을 만듦)
  EVALUATING -> APPROVED/REJECTED  (CEO 승인 - 요청자와 승인자가 달라야 한다)
  APPROVED/REJECTED -> CLOSED      (종결)

`propose` 도구는 **OPEN으로 직접 만든다** - DRAFT는 사람이 손으로 다듬는
중간 상태를 남기고 싶을 때를 위해 상태기계에는 두되, 이 API가 기본으로
제공하는 생성 경로는 아니다(propose=제안 자체가 제출 행위이므로).

불변식:
  1. business_problem 이 비어 있으면 요청을 만들 수 없다.
  2. evidence/required_capabilities/budget 은 dict 여야 한다(DDL not null과 동일 - 빈
     dict는 허용하되 타입은 강제한다. "증거 없음"과 "증거 필드 자체가 없음"을 구분).
  3. EVALUATING -> APPROVED/REJECTED 전이는 요청자와 승인자가 달라야 한다
     (마스터플랜 4.3절 - HR이 자기 제안을 스스로 승인할 수 없다. improvements/workflow.py의
     SelfApprovalError와 같은 원칙).

자체 점검: python departments/07-agent-workforce/hiring/hiring_request.py
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class HiringRequestStatus(str, Enum):
    DRAFT = "DRAFT"
    OPEN = "OPEN"
    EVALUATING = "EVALUATING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


class IllegalHiringTransition(Exception):
    """허용되지 않은 상태 전이."""


class HiringSelfApprovalError(Exception):
    """제안자가 자기 채용 제안을 스스로 승인하려 함 (마스터플랜 4.3절 금지)."""


@dataclass(frozen=True)
class HiringRequest:
    """workforce.hiring_requests 한 행. 컬럼과 1:1."""

    request_id: str
    department_id: str
    business_problem: str
    evidence: dict[str, Any]
    required_capabilities: dict[str, Any]
    budget: dict[str, Any]
    requested_by: str
    trace_id: str
    created_at: datetime
    status: HiringRequestStatus = HiringRequestStatus.OPEN
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.business_problem.strip():
            raise ValueError("business_problem이 비어 있으면 채용 요청을 만들 수 없다")
        for name, value in (
            ("evidence", self.evidence),
            ("required_capabilities", self.required_capabilities),
            ("budget", self.budget),
        ):
            if not isinstance(value, dict):
                raise ValueError(f"{name}은 dict여야 한다 (DDL not null과 동일)")
        if not self.requested_by.strip():
            raise ValueError("requested_by가 비어 있으면 제안 주체를 알 수 없다")


ALLOWED_HIRING_TRANSITIONS: dict[HiringRequestStatus, frozenset[HiringRequestStatus]] = {
    HiringRequestStatus.DRAFT: frozenset({HiringRequestStatus.OPEN, HiringRequestStatus.CLOSED}),
    HiringRequestStatus.OPEN: frozenset(
        {HiringRequestStatus.EVALUATING, HiringRequestStatus.REJECTED, HiringRequestStatus.CLOSED}
    ),
    HiringRequestStatus.EVALUATING: frozenset(
        {HiringRequestStatus.APPROVED, HiringRequestStatus.REJECTED}
    ),
    HiringRequestStatus.APPROVED: frozenset({HiringRequestStatus.CLOSED}),
    HiringRequestStatus.REJECTED: frozenset({HiringRequestStatus.CLOSED}),
}

# 요청자와 다른 승인자를 요구하는 전이 - 이 전이에서만 자기승인 검사를 한다.
_DECISION_TRANSITIONS = frozenset({HiringRequestStatus.APPROVED, HiringRequestStatus.REJECTED})


def transition(
    request: HiringRequest,
    *,
    to_status: HiringRequestStatus,
    actor: str,
    at: datetime,
    reason: str | None = None,
) -> HiringRequest:
    """상태를 전이한다. APPROVED/REJECTED는 requested_by와 actor가 달라야 한다."""

    allowed = ALLOWED_HIRING_TRANSITIONS.get(request.status, frozenset())
    if to_status not in allowed:
        raise IllegalHiringTransition(
            f"{request.status.value}에서 {to_status.value}로 전이할 수 없다"
        )
    if to_status in _DECISION_TRANSITIONS and actor == request.requested_by:
        raise HiringSelfApprovalError(
            f"제안자({request.requested_by})는 자기 채용 제안을 스스로 승인/거절할 수 없다"
        )

    updates: dict[str, Any] = {"status": to_status}
    if to_status in _DECISION_TRANSITIONS:
        updates.update(decided_by=actor, decided_at=at, decision_reason=reason)
    return HiringRequest(**{**request.__dict__, **updates})


# ---------------------------------------------------------------------------
# Repository 인터페이스 + In-Memory 구현
# ---------------------------------------------------------------------------


class HiringRequestRepository:
    """조회·저장 인터페이스. 실제 구현은 workforce.hiring_requests에 반영한다."""

    def get_request(self, request_id: str) -> HiringRequest | None:
        raise NotImplementedError

    def save_request(self, request: HiringRequest) -> None:
        """새 요청이면 insert, 이미 있으면 전체 행을 갱신한다."""
        raise NotImplementedError

    def list_requests_by_status(self, status: HiringRequestStatus) -> list[HiringRequest]:
        raise NotImplementedError

    def list_requests_by_department(self, department_id: str) -> list[HiringRequest]:
        raise NotImplementedError


class InMemoryHiringRequestRepository(HiringRequestRepository):
    def __init__(self) -> None:
        self._requests: dict[str, HiringRequest] = {}

    def get_request(self, request_id: str) -> HiringRequest | None:
        return self._requests.get(request_id)

    def save_request(self, request: HiringRequest) -> None:
        self._requests[request.request_id] = request

    def list_requests_by_status(self, status: HiringRequestStatus) -> list[HiringRequest]:
        return [r for r in self._requests.values() if r.status is status]

    def list_requests_by_department(self, department_id: str) -> list[HiringRequest]:
        return [r for r in self._requests.values() if r.department_id == department_id]


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timezone
    from uuid import uuid4

    t0 = datetime(2026, 8, 10, tzinfo=timezone.utc)

    # 1) 불변식 - business_problem 비면 거부.
    try:
        HiringRequest(
            request_id="r0", department_id="dept-1", business_problem="  ",
            evidence={}, required_capabilities={}, budget={}, requested_by="hr-department",
            trace_id="t0", created_at=t0,
        )
        raise AssertionError("빈 business_problem이 통과했다")
    except ValueError:
        pass
    print("ok - business_problem 불변식 통과")

    # 2) 정상 생성 - 기본 상태는 OPEN(propose=제출 행위).
    req = HiringRequest(
        request_id=str(uuid4()), department_id="research-department",
        business_problem="Queue 깊이 12, SLA 위반 3%", evidence={"queue_depth": 12},
        required_capabilities={"skills": ["python"]}, budget={"usd": 500},
        requested_by="hr-department", trace_id=str(uuid4()), created_at=t0,
    )
    assert req.status is HiringRequestStatus.OPEN
    print("ok - 기본 상태 OPEN 확인")

    # 3) OPEN -> EVALUATING (승인자 검사 없음 - 결정이 아니라 진입).
    evaluating = transition(req, to_status=HiringRequestStatus.EVALUATING, actor="qa-department", at=t0)
    assert evaluating.status is HiringRequestStatus.EVALUATING
    print("ok - OPEN -> EVALUATING 전이")

    # 4) 자기승인 차단 - requested_by와 같은 actor가 APPROVED로 못 바꾼다.
    try:
        transition(evaluating, to_status=HiringRequestStatus.APPROVED, actor="hr-department", at=t0)
        raise AssertionError("자기승인이 통과했다")
    except HiringSelfApprovalError:
        pass
    print("ok - 자기승인 차단 확인")

    # 5) 정상 승인 - 다른 actor(CEO).
    approved = transition(
        evaluating, to_status=HiringRequestStatus.APPROVED, actor="ceo-agent", at=t0,
        reason="Queue 근거 충분",
    )
    assert approved.status is HiringRequestStatus.APPROVED
    assert approved.decided_by == "ceo-agent"
    print("ok - CEO 승인 전이 확인")

    # 6) 허용 안 된 전이 - CLOSED에서 다시 OPEN으로는 못 간다.
    closed = transition(approved, to_status=HiringRequestStatus.CLOSED, actor="ceo-agent", at=t0)
    try:
        transition(closed, to_status=HiringRequestStatus.OPEN, actor="ceo-agent", at=t0)
        raise AssertionError("CLOSED에서 재전이가 통과했다")
    except IllegalHiringTransition:
        pass
    print("ok - 종결 상태 재전이 차단 확인")

    # 7) In-Memory Repository 왕복.
    repo = InMemoryHiringRequestRepository()
    repo.save_request(req)
    assert repo.get_request(req.request_id) is not None
    assert len(repo.list_requests_by_status(HiringRequestStatus.OPEN)) == 1
    assert len(repo.list_requests_by_department("research-department")) == 1
    print("ok - In-Memory Repository 왕복 확인")

    print("HiringRequest 상태기계·불변식 7개 영역 통과")
