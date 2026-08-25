#!/usr/bin/env python3
"""P1-2 HR-04: Workforce Plan 상태 머신 — quality.py의 자매 모듈.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md P1-2("Quality Snapshot과 Workforce
      Plan을 실제 데이터에서 집계·저장한다. 빈 집계를 정상 운영 상태로 표시하지 않는다"),
      UNIFIED_DOMAIN_API_SPEC.md 5.4(Workforce) - workforce-planning-agent 산출물:
      Capacity Report·Staffing Scenario), 7절
      대응 테이블: supabase/migrations/20260731000800_workforce_plan_quality_probation.sql
      (workforce.workforce_plans)

workforce-planning-agent(HR-01)가 Capacity Report/Staffing Scenario를 DRAFT로 쓰지만,
실제 운영(ACTIVE)으로 올리는 결정은 인사팀이 스스로 못 한다 - governance.approvals에
이 plan_id를 object_id로 하는 실재 CEO 결정(object_type=WORKFORCE_PLAN,
decision=APPROVED)이 있어야 DRAFT -> APPROVED가 통과한다. roster.py의
verify_activation_evidence와 같은 조회-판정 분리 원칙: 이 모듈은 DB를 모르고, 호출자가
governance.approvals를 먼저 조회해 얻은 decision 문자열만 받는다.

불변식:
  1. period_end 는 period_start 이후여야 한다.
  2. DRAFT -> APPROVED 는 실재하는 CEO 승인 없이 통과하지 않는다. approval_id 칸이
     채워져 있어도 조회 결과가 APPROVED 가 아니면 거절한다 (값 재사용/위조 방지).
  3. 승인되지 않은 계획은 ACTIVE 가 될 수 없다 (DRAFT 에서 바로 ACTIVE 로 전이 불가).
  4. 종료 상태(RETIRED)에서는 재전이하지 않는다.

자체 점검: python departments/07-agent-workforce/planning/workforce_plan.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class WorkforcePlanStatus(str, Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class IllegalTransition(Exception):
    """허용되지 않은 상태 전이."""


class UnverifiedPlanApprovalError(Exception):
    """approval_id 가 실재하는 CEO 승인(WORKFORCE_PLAN, APPROVED)을 가리키지 않는다."""


ALLOWED_TRANSITIONS: dict[WorkforcePlanStatus, frozenset[WorkforcePlanStatus]] = {
    WorkforcePlanStatus.DRAFT: frozenset(
        {WorkforcePlanStatus.APPROVED, WorkforcePlanStatus.RETIRED}
    ),
    WorkforcePlanStatus.APPROVED: frozenset(
        {WorkforcePlanStatus.ACTIVE, WorkforcePlanStatus.RETIRED}
    ),
    WorkforcePlanStatus.ACTIVE: frozenset({WorkforcePlanStatus.RETIRED}),
}


@dataclass(frozen=True)
class WorkforcePlan:
    """workforce.workforce_plans 한 행. 컬럼과 1:1."""

    plan_id: str
    department_id: str
    period_start: datetime
    period_end: datetime
    skill_gaps: dict = field(default_factory=dict)
    actions: list = field(default_factory=list)
    budget: dict = field(default_factory=dict)
    assumptions: dict = field(default_factory=dict)
    status: WorkforcePlanStatus = WorkforcePlanStatus.DRAFT
    approval_id: str | None = None

    def __post_init__(self) -> None:
        if self.period_end <= self.period_start:
            raise ValueError("period_end 는 period_start 이후여야 한다")


def approve_plan(
    plan: WorkforcePlan, *, approval_id: str, approval_decision: str | None,
) -> WorkforcePlan:
    """DRAFT -> APPROVED. approval_decision 은 호출자가 governance.approvals 를 조회해
    얻은 값이다 (불변식 2)."""
    if WorkforcePlanStatus.APPROVED not in ALLOWED_TRANSITIONS.get(plan.status, frozenset()):
        raise IllegalTransition(f"{plan.status.value} 에서 승인할 수 없다")
    if approval_decision != "APPROVED":
        raise UnverifiedPlanApprovalError(
            f"approval_id 가 이 계획에 대한 승인된 CEO 결정을 가리키지 않는다 "
            f"(조회된 decision={approval_decision!r})"
        )
    return WorkforcePlan(
        **{**plan.__dict__, "status": WorkforcePlanStatus.APPROVED, "approval_id": approval_id}
    )


def activate_plan(plan: WorkforcePlan) -> WorkforcePlan:
    """APPROVED -> ACTIVE. 승인되지 않은 계획은 활성화할 수 없다 (불변식 3)."""
    if WorkforcePlanStatus.ACTIVE not in ALLOWED_TRANSITIONS.get(plan.status, frozenset()):
        raise IllegalTransition(f"{plan.status.value} 에서 활성화할 수 없다")
    return WorkforcePlan(**{**plan.__dict__, "status": WorkforcePlanStatus.ACTIVE})


def retire_plan(plan: WorkforcePlan) -> WorkforcePlan:
    if WorkforcePlanStatus.RETIRED not in ALLOWED_TRANSITIONS.get(plan.status, frozenset()):
        raise IllegalTransition(f"{plan.status.value} 에서 폐기할 수 없다")
    return WorkforcePlan(**{**plan.__dict__, "status": WorkforcePlanStatus.RETIRED})


# ---------------------------------------------------------------------------
# Repository 인터페이스 + In-Memory 구현
# ---------------------------------------------------------------------------


class PlanRepository:
    """조회·저장 인터페이스. 실제 구현은 workforce.workforce_plans 에 반영한다."""

    def get_plan(self, plan_id: str) -> WorkforcePlan | None:
        raise NotImplementedError

    def create_plan(self, plan: WorkforcePlan) -> WorkforcePlan:
        raise NotImplementedError

    def save_plan(self, plan: WorkforcePlan) -> None:
        raise NotImplementedError

    def list_plans_by_department(self, department_id: str) -> list[WorkforcePlan]:
        raise NotImplementedError


class InMemoryPlanRepository(PlanRepository):
    def __init__(self) -> None:
        self._plans: dict[str, WorkforcePlan] = {}

    def get_plan(self, plan_id: str) -> WorkforcePlan | None:
        return self._plans.get(plan_id)

    def create_plan(self, plan: WorkforcePlan) -> WorkforcePlan:
        self._plans[plan.plan_id] = plan
        return plan

    def save_plan(self, plan: WorkforcePlan) -> None:
        self._plans[plan.plan_id] = plan

    def list_plans_by_department(self, department_id: str) -> list[WorkforcePlan]:
        return [p for p in self._plans.values() if p.department_id == department_id]


# ---------------------------------------------------------------------------
# 승인 증거 조회 인터페이스 (roster/activation_evidence.py 와 같은 조회-판정 분리)
# ---------------------------------------------------------------------------


class PlanApprovalEvidenceRepository:
    """governance.approvals 조회 전용 인터페이스. 이 모듈은 판정하지 않는다."""

    def get_ceo_approval_decision(self, approval_id: str, plan_id: str) -> str | None:
        """approval_id 가 required_role=CEO, object_type=WORKFORCE_PLAN,
        object_id=plan_id 인 실제 governance.approvals 행을 가리키면 decision 을
        돌려준다. 없거나 다른 계획/역할이면 None - 증거 재사용을 막는다."""
        raise NotImplementedError


class InMemoryPlanApprovalEvidenceRepository(PlanApprovalEvidenceRepository):
    """테스트·개발용. seed_ceo_approval() 로 미리 등록해둔 것만 실재로 본다."""

    def __init__(self) -> None:
        self._approvals: dict[tuple[str, str], str] = {}

    def seed_ceo_approval(self, approval_id: str, plan_id: str, decision: str) -> None:
        self._approvals[(approval_id, plan_id)] = decision

    def get_ceo_approval_decision(self, approval_id: str, plan_id: str) -> str | None:
        return self._approvals.get((approval_id, plan_id))


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta, timezone

    t0 = datetime(2026, 8, 6, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=30)

    def plan(**over) -> WorkforcePlan:
        base = {
            "plan_id": "plan-1", "department_id": "d1",
            "period_start": t0, "period_end": t1,
            "skill_gaps": {"research": 1}, "actions": [{"type": "HIRE", "role": "HR-01"}],
            "budget": {"monthly_usd": "5000"},
        }
        base.update(over)
        return WorkforcePlan(**base)

    # 1) 정상 Draft.
    p = plan()
    assert p.status is WorkforcePlanStatus.DRAFT

    # 2) period 역전 거부.
    try:
        plan(period_start=t1, period_end=t0)
        raise AssertionError("역전된 period 가 통과함")
    except ValueError:
        pass

    # 3) 불변식 2 - 위조/미실재 승인 거부.
    try:
        approve_plan(p, approval_id="appr-ghost", approval_decision=None)
        raise AssertionError("미실재 승인이 통과함")
    except UnverifiedPlanApprovalError:
        pass

    # 4) 대기 중(PENDING) 결정도 거부.
    try:
        approve_plan(p, approval_id="appr-pending", approval_decision="PENDING")
        raise AssertionError("PENDING 결정이 통과함")
    except UnverifiedPlanApprovalError:
        pass

    # 5) 실재 승인 -> APPROVED.
    approved = approve_plan(p, approval_id="appr-1", approval_decision="APPROVED")
    assert approved.status is WorkforcePlanStatus.APPROVED
    assert approved.approval_id == "appr-1"

    # 6) 불변식 3 - 승인 없이 바로 ACTIVE 불가.
    try:
        activate_plan(p)
        raise AssertionError("DRAFT 에서 바로 ACTIVE 로 전이됨")
    except IllegalTransition:
        pass

    # 7) 승인된 계획은 활성화된다.
    active = activate_plan(approved)
    assert active.status is WorkforcePlanStatus.ACTIVE

    # 8) 종료 상태에서 재전이 불가.
    retired = retire_plan(active)
    assert retired.status is WorkforcePlanStatus.RETIRED
    try:
        activate_plan(retired)
        raise AssertionError("RETIRED 에서 전이됨")
    except IllegalTransition:
        pass

    # 9) Repository 왕복.
    repo = InMemoryPlanRepository()
    repo.create_plan(p)
    assert repo.get_plan("plan-1") is not None
    repo.save_plan(approved)
    assert repo.get_plan("plan-1").status is WorkforcePlanStatus.APPROVED
    assert len(repo.list_plans_by_department("d1")) == 1

    # 10) 승인 증거 Repository - seed 하지 않은 조합은 None, 다른 plan_id 재사용 불가.
    evidence = InMemoryPlanApprovalEvidenceRepository()
    assert evidence.get_ceo_approval_decision("appr-1", "plan-1") is None
    evidence.seed_ceo_approval("appr-1", "plan-1", "APPROVED")
    assert evidence.get_ceo_approval_decision("appr-1", "plan-1") == "APPROVED"
    assert evidence.get_ceo_approval_decision("appr-1", "plan-2") is None

    print("ok - Workforce Plan 상태 머신 10개 점검 통과")
