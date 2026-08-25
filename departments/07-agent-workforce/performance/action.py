#!/usr/bin/env python3
"""HR-03 성과 조치(Performance Action) 상태 머신 — review.py의 자매 모듈.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/04-organization/AGENT_EMPLOYEE_PROFILES.md HR-03(공식 산출물: Learning
      Plan, Performance Improvement Plan, Promotion/Exit Proposal)
      대응 테이블: supabase/migrations/20260731000800_workforce_plan_quality_probation.sql
      (workforce.performance_actions)

DDL 주석이 분리 이유를 적어뒀다: "performance_reviews(평가 자체)와 분리한다 —
리뷰는 평가, Action은 그 뒤의 조치." 이 모듈은 그 조치의 생명주기를 맡는다.

불변식:
  1. **VERIFIED 는 검증 근거(verification) 없이 통과하지 않는다.** DDL 의
     `check (status <> 'VERIFIED' or verification is not null)` 와 같은 규칙을 앱
     계층에서도 강제한다 - "계획만 세우고 검증 없이 닫지 않는다"(DDL 주석).
  2. **계획(plan) 없는 조치는 만들 수 없다.** DDL 은 not null 만 강제하므로 `{}` 가
     통과한다 - 무엇을 할지 없는 조치는 조치가 아니다.
  3. 허용되지 않은 상태 전이와 종료 상태 재전이는 막는다.
  4. **review 에 연결된 조치는 그 평가의 decision 과 종류가 같아야 한다.** 평가는
     PIP 를 제안했는데 조치가 DEACTIVATION 으로 나가는 조합을 막는다. 이 모듈은 DB 를
     모르므로 호출자가 performance_reviews 를 조회해 얻은 decision 문자열만 받는다
     (planning/workforce_plan.py 의 approval_decision 과 같은 조회-판정 분리).
  5. 이 모듈도 비활성화를 **집행하지 않는다**. DEACTIVATION 조치가 VERIFIED 가 돼도
     Agent 의 employment status 는 바뀌지 않는다 - roster 전이는 CEO 승인 게이트를
     따로 거친다(review.py 불변식 3과 같다).

자체 점검: python departments/07-agent-workforce/performance/action.py
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum


class ActionType(str, Enum):
    """DDL 의 action_type check 제약과 같은 4개. review.py 의 ACTIONABLE_DECISIONS 와
    같은 어휘다(불변식 4)."""

    LEARNING = "LEARNING"
    PIP = "PIP"
    ROLE_CHANGE = "ROLE_CHANGE"
    DEACTIVATION = "DEACTIVATION"


class ActionStatus(str, Enum):
    """DDL 의 status check 제약과 같은 5개.

    OPEN         조치 등록, 아직 착수 전
    IN_PROGRESS  이행 중
    VERIFIED     (종료) 이행 확인 - verification 필수
    CANCELLED    (종료) 취소
    OVERDUE      기한 초과 - 종료가 아니다(아래 참고)
    """

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFIED = "VERIFIED"
    CANCELLED = "CANCELLED"
    OVERDUE = "OVERDUE"


# OVERDUE 는 종료 상태가 아니다 - 기한을 넘겼다고 조치가 사라지지 않는다. 늦게라도
# 이행하거나(IN_PROGRESS/VERIFIED) 명시적으로 취소해야 닫힌다. 이걸 종료로 두면
# "기한 넘김"이 조용한 면제가 된다(개발 원칙 9: 실패는 차단 방향으로).
TERMINAL_STATUSES: frozenset[ActionStatus] = frozenset(
    {ActionStatus.VERIFIED, ActionStatus.CANCELLED}
)

ALLOWED_TRANSITIONS: dict[ActionStatus, frozenset[ActionStatus]] = {
    ActionStatus.OPEN: frozenset(
        {ActionStatus.IN_PROGRESS, ActionStatus.CANCELLED, ActionStatus.OVERDUE}
    ),
    ActionStatus.IN_PROGRESS: frozenset(
        {ActionStatus.VERIFIED, ActionStatus.CANCELLED, ActionStatus.OVERDUE}
    ),
    ActionStatus.OVERDUE: frozenset(
        {ActionStatus.IN_PROGRESS, ActionStatus.VERIFIED, ActionStatus.CANCELLED}
    ),
}


class IllegalTransition(Exception):
    """허용되지 않은 상태 전이 (불변식 3)."""


class MissingVerificationError(Exception):
    """검증 근거 없이 VERIFIED 로 닫으려 함 (불변식 1)."""


class ActionReviewMismatchError(Exception):
    """조치 종류가 연결된 평가의 decision 과 다르다 (불변식 4)."""


@dataclass(frozen=True)
class PerformanceAction:
    """workforce.performance_actions 한 행. 컬럼과 1:1."""

    action_id: str
    agent_id: str
    action_type: ActionType
    due_at: datetime
    plan: dict
    review_id: str | None = None
    verification: dict | None = None
    status: ActionStatus = ActionStatus.OPEN
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.plan:
            raise ValueError("plan 이 비어 있으면 조치를 만들 수 없다 - 무엇을 할지가 조치다")
        # DDL check 를 앱에서도 지킨다 - DB 를 안 거치는 경로(In-Memory)에서도 같은 규칙.
        if self.status is ActionStatus.VERIFIED and not self.verification:
            raise MissingVerificationError(
                "VERIFIED 는 verification 없이 나올 수 없다 - 계획만 세우고 검증 없이 닫지 않는다"
            )
        if self.status in TERMINAL_STATUSES and self.completed_at is None:
            raise ValueError(f"{self.status.value} 는 completed_at 이 있어야 한다")
        if self.status not in TERMINAL_STATUSES and self.completed_at is not None:
            raise ValueError(f"{self.status.value} 는 아직 종료가 아니라 completed_at 이 없어야 한다")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def open_action(
    *,
    action_id: str,
    agent_id: str,
    action_type: ActionType,
    due_at: datetime,
    plan: dict,
    review_id: str | None = None,
    review_decision: str | None = None,
) -> PerformanceAction:
    """조치를 등록한다.

    review_id 를 붙이면 review_decision 도 함께 줘야 한다 - 호출자가
    performance_reviews 를 조회해 얻은 값이고, 조치 종류와 다르면 거절한다
    (불변식 4). 평가에 연결하지 않는 단독 조치는 둘 다 없이 만든다.
    """
    if review_id is not None:
        if review_decision is None:
            raise ActionReviewMismatchError(
                "review_id 를 붙이려면 그 평가의 decision 을 함께 확인해야 한다"
            )
        if review_decision != action_type.value:
            raise ActionReviewMismatchError(
                f"평가는 {review_decision!r} 를 제안했는데 조치는 {action_type.value!r} 다"
            )
    return PerformanceAction(
        action_id=action_id, agent_id=agent_id, action_type=action_type,
        due_at=due_at, plan=plan, review_id=review_id,
    )


def transition(
    action: PerformanceAction,
    to_status: ActionStatus,
    *,
    at: datetime,
    verification: dict | None = None,
) -> PerformanceAction:
    """조치 상태를 옮긴다. VERIFIED 에는 verification 이 필요하다 (불변식 1)."""
    if action.is_terminal:
        raise IllegalTransition(f"종료 상태에서 전이 불가: {action.status.value}")

    allowed = ALLOWED_TRANSITIONS.get(action.status, frozenset())
    if to_status not in allowed:
        raise IllegalTransition(
            f"{action.status.value} -> {to_status.value} 는 허용되지 않는다 "
            f"(허용: {sorted(s.value for s in allowed)})"
        )

    if to_status is ActionStatus.VERIFIED:
        # 기존 verification 을 재사용하지 않는다 - 이번 종료의 근거를 요구한다.
        if not verification:
            raise MissingVerificationError(
                "VERIFIED 로 닫으려면 verification 이 필요하다"
            )

    return replace(
        action,
        status=to_status,
        verification=verification if verification is not None else action.verification,
        completed_at=at if to_status in TERMINAL_STATUSES else None,
    )


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/performance/action.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta, timezone

    now = datetime(2026, 8, 25, tzinfo=timezone.utc)
    due = now + timedelta(days=14)

    def _action(**over) -> PerformanceAction:
        base = {
            "action_id": "ac-1", "agent_id": "a1", "action_type": ActionType.PIP,
            "due_at": due, "plan": {"goal": "SLA 0.95 회복", "steps": ["재시도 상한 조정"]},
        }
        base.update(over)
        return PerformanceAction(**base)

    # 1) 계획 없는 조치는 만들 수 없다 (불변식 2).
    try:
        _action(plan={})
        raise AssertionError("빈 plan 이 통과함")
    except ValueError:
        pass

    # 2) 정상 흐름: OPEN -> IN_PROGRESS -> VERIFIED(근거 있음).
    a = _action()
    assert a.status is ActionStatus.OPEN and a.completed_at is None
    a = transition(a, ActionStatus.IN_PROGRESS, at=now)
    assert a.status is ActionStatus.IN_PROGRESS and a.completed_at is None
    a = transition(
        a, ActionStatus.VERIFIED, at=due, verification={"eval_run_id": "eval-9", "sla": 0.96},
    )
    assert a.status is ActionStatus.VERIFIED and a.completed_at == due
    assert a.is_terminal and a.verification["sla"] == 0.96

    # 3) 검증 근거 없이 VERIFIED 로 닫을 수 없다 (불변식 1).
    b = transition(_action(action_id="ac-2"), ActionStatus.IN_PROGRESS, at=now)
    try:
        transition(b, ActionStatus.VERIFIED, at=due)
        raise AssertionError("근거 없는 VERIFIED 가 통과함")
    except MissingVerificationError:
        pass

    # 4) 종료 상태 재전이 불가 (불변식 3).
    try:
        transition(a, ActionStatus.IN_PROGRESS, at=due)
        raise AssertionError("종료 상태에서 전이됨")
    except IllegalTransition:
        pass

    # 5) OVERDUE 는 종료가 아니다 - 늦게라도 이행할 수 있어야 하고, 조용히 면제되지
    #    않는다(기한 넘김을 종료로 두면 미이행이 닫힌 것처럼 보인다).
    c = transition(_action(action_id="ac-3"), ActionStatus.OVERDUE, at=due)
    assert c.is_terminal is False and c.completed_at is None
    c = transition(c, ActionStatus.VERIFIED, at=due, verification={"late": True})
    assert c.status is ActionStatus.VERIFIED

    # 6) 불변식 4 - 평가의 decision 과 조치 종류가 다르면 거절.
    try:
        open_action(
            action_id="ac-4", agent_id="a1", action_type=ActionType.DEACTIVATION,
            due_at=due, plan={"goal": "x"}, review_id="rv-1", review_decision="PIP",
        )
        raise AssertionError("평가와 어긋난 조치가 통과함")
    except ActionReviewMismatchError:
        pass
    # decision 을 확인하지 않고 review 에 붙이는 것도 막는다.
    try:
        open_action(
            action_id="ac-5", agent_id="a1", action_type=ActionType.PIP,
            due_at=due, plan={"goal": "x"}, review_id="rv-1",
        )
        raise AssertionError("decision 확인 없이 review 연결이 통과함")
    except ActionReviewMismatchError:
        pass
    # 일치하면 통과한다.
    linked = open_action(
        action_id="ac-6", agent_id="a1", action_type=ActionType.PIP,
        due_at=due, plan={"goal": "x"}, review_id="rv-1", review_decision="PIP",
    )
    assert linked.review_id == "rv-1"

    # 7) 어휘가 review.py 와 같아야 한다 - 다르면 평가와 조치를 이을 수 없다.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from review import ACTIONABLE_DECISIONS

    assert {a.value for a in ActionType} == {d.value for d in ACTIONABLE_DECISIONS}

    # 8) 불변식 5 - DEACTIVATION 이 VERIFIED 가 돼도 고용 상태를 들고 있지 않다.
    d = open_action(
        action_id="ac-7", agent_id="a1", action_type=ActionType.DEACTIVATION,
        due_at=due, plan={"goal": "비활성화 제안"},
    )
    d = transition(d, ActionStatus.IN_PROGRESS, at=now)
    d = transition(d, ActionStatus.VERIFIED, at=due, verification={"ceo_approval_id": "ap-9"})
    assert not hasattr(d, "employment_status"), "조치가 고용 상태를 들고 있으면 집행에 가깝다"
    _body = Path(__file__).read_text(encoding="utf-8").split('if __name__ ==')[0]
    assert not any(
        line.startswith(("import roster", "from roster"))
        for line in (ln.strip() for ln in _body.splitlines())
    ), "이 모듈이 roster 를 import 하면 제안과 집행의 경계가 무너진다"

    print("ok - Performance Action 상태머신 8개 점검 통과")
