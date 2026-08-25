#!/usr/bin/env python3
"""Agent 생명주기 이벤트(lifecycle_events) 계약.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/04-organization/AGENT_EMPLOYEE_PROFILES.md HR-04(KPI: "승인 없는 활성화 0",
      Provisioning Lead Time, 권한 회수 SLA), supabase/seed.sql(HR 역할에
      `propose: ["lifecycle_events"]` 권한이 이미 선언돼 있다)
      대응 테이블: supabase/migrations/20260729000200_governance_workforce.sql
      (workforce.lifecycle_events)

**왜 필요한가**: `roster.change_status()`가 `agent_profiles.employment_status`를 바꾸는데
그 전이가 어디에도 기록되지 않았다. "승인 없는 활성화 0"을 KPI 로 두려면 활성화가
일어났다는 사실과 그때 무슨 승인을 근거로 삼았는지가 남아 있어야 한다 - 현재 상태만
보고는 "누가 언제 무슨 근거로 ACTIVE 로 올렸는지"를 사후에 확인할 수 없다.

⚠ `workforce.lifecycle_events` 에는 **append-only 트리거**가 걸려 있다
(`workforce_lifecycle_events_append_only` -> `governance.reject_append_only_change`).
update/delete 가 전부 예외로 거부된다 - improvement_candidate_events 와 같은 취급이고,
cost_snapshots/capacity_snapshots(트리거 없음)와는 다르다. 그래서 **한번 쓴 이벤트는
정정할 수 없다** - 자체 점검이나 개발 중 실수로 쓴 행도 지울 수 없으니, 실 DB 에
이벤트를 쓰는 코드는 그 사실을 알고 짜야 한다(실측 2026-08-25: roster 자체 점검이
남긴 행을 지우려다 정리 트랜잭션 전체가 롤백됐다).

불변식:
  1. **이벤트는 상태 변경과 같은 트랜잭션에서 쓰인다.** 나눠 쓰면 상태는 바뀌었는데
     이벤트가 없는 창이 생기고, 그게 정확히 이 표가 막으려는 감사 공백이다.
     (그 강제는 postgres_roster_repository.change_status 가 한다 - 이 모듈은 계약만.)
     append-only 라 사후 보정도 불가능하므로, 같은 트랜잭션이 유일한 방법이다.
  2. **ACTIVE 전이 이벤트는 근거(approvals) 없이 남길 수 없다.** roster 의
     validate_status_change/verify_activation_evidence 가 이미 qa_eval_run_id 와
     ceo_approval_id 를 요구하는데, 이벤트에 그 근거를 안 실으면 "승인 없는 활성화 0"을
     이벤트만 보고 확인할 수 없다.
  3. from_status 는 없을 수 있다(최초 등록). to_status 는 항상 있다(DDL not null).

event_type 어휘를 크게 만들지 않는다 - 지금 이 writer 가 내는 것은 상태 전이 하나뿐이고,
그 내용(무엇에서 무엇으로)은 from_status/to_status 가 이미 전부 담는다. to_status 를
ACTIVATED/SUSPENDED 같은 이름으로 한 번 더 옮겨 적으면 정보는 안 늘고 두 칸이 어긋날
자리만 생긴다. 다른 종류의 생명주기 이벤트(Provisioning 등)를 이 표에 같이 담을지는
정해진 바 없어 여기서 정하지 않는다.

자체 점검: python departments/07-agent-workforce/roster/lifecycle_event.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class LifecycleEventType(str, Enum):
    """지금 이 writer 가 내는 유일한 종류. 위 머리말 참고."""

    STATUS_CHANGE = "STATUS_CHANGE"


class MissingActivationApprovalsError(Exception):
    """ACTIVE 전이 이벤트에 근거(approvals)가 없다 (불변식 2)."""


@dataclass(frozen=True)
class LifecycleEvent:
    """workforce.lifecycle_events 한 행. 컬럼과 1:1."""

    agent_id: str
    to_status: str
    trace_id: str
    occurred_at: datetime
    event_type: LifecycleEventType = LifecycleEventType.STATUS_CHANGE
    from_status: str | None = None
    # 이 전이를 정당화한 근거. ACTIVE 전이면 qa_eval_run_id/ceo_approval_id 가 여기 온다.
    approvals: list = field(default_factory=list)
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.to_status:
            raise ValueError("to_status 는 비어 있을 수 없다 (DDL not null)")
        if not self.trace_id:
            # DDL 이 uuid not null 이다. 여기서 만들어 채우지 않는다 - 지어낸 trace_id 는
            # 아무것과도 이어지지 않으면서 상관관계가 있는 것처럼 보인다.
            raise ValueError("trace_id 없이 생명주기 이벤트를 남길 수 없다")
        if self.to_status == "ACTIVE" and not self.approvals:
            raise MissingActivationApprovalsError(
                "ACTIVE 전이 이벤트는 근거(approvals) 없이 남길 수 없다 - "
                "'승인 없는 활성화 0'을 이벤트만 보고 확인할 수 있어야 한다"
            )


def activation_approvals(
    *, qa_eval_run_id: str | None, ceo_approval_id: str | None,
) -> list[dict]:
    """ACTIVE 전이의 근거를 approvals 모양으로 만든다.

    roster.validate_status_change 가 이미 둘 다 있어야 통과시키므로, 여기서는 있는
    것만 담되 **빈 값을 채워 넣지 않는다** - 없는 근거를 빈 문자열로 적으면 칸은
    찼는데 실재하지 않는 상태가 된다(P0-3 UnverifiedActivationEvidenceError 가 막는
    것과 같은 종류의 위조다).
    """
    approvals: list[dict] = []
    if qa_eval_run_id:
        approvals.append({"kind": "QA_EVAL_RUN", "id": qa_eval_run_id})
    if ceo_approval_id:
        approvals.append({"kind": "CEO_APPROVAL", "id": ceo_approval_id})
    return approvals


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/roster/lifecycle_event.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timezone

    now = datetime(2026, 8, 25, tzinfo=timezone.utc)

    def _event(**over) -> LifecycleEvent:
        base = {
            "agent_id": "a1", "to_status": "SUSPENDED", "trace_id": "tr-1",
            "occurred_at": now, "from_status": "ACTIVE", "reason": "점검",
        }
        base.update(over)
        return LifecycleEvent(**base)

    # 1) 정상 이벤트. ACTIVE 가 아닌 전이는 근거를 요구하지 않는다.
    e = _event()
    assert e.event_type is LifecycleEventType.STATUS_CHANGE
    assert e.from_status == "ACTIVE" and e.to_status == "SUSPENDED"
    assert e.approvals == []

    # 2) 불변식 2 - ACTIVE 전이는 근거 없이 못 남긴다.
    try:
        _event(to_status="ACTIVE", from_status="PROBATION")
        raise AssertionError("근거 없는 ACTIVE 이벤트가 통과함")
    except MissingActivationApprovalsError:
        pass

    # 3) 근거가 있으면 통과.
    approvals = activation_approvals(qa_eval_run_id="eval-1", ceo_approval_id="ap-1")
    assert approvals == [
        {"kind": "QA_EVAL_RUN", "id": "eval-1"},
        {"kind": "CEO_APPROVAL", "id": "ap-1"},
    ]
    active = _event(to_status="ACTIVE", from_status="PROBATION", approvals=approvals)
    assert len(active.approvals) == 2

    # 4) 빈 근거를 칸 채우기로 넣지 않는다 - 없는 것은 없는 채로 둔다.
    assert activation_approvals(qa_eval_run_id="", ceo_approval_id=None) == []
    assert activation_approvals(qa_eval_run_id="eval-2", ceo_approval_id="") == [
        {"kind": "QA_EVAL_RUN", "id": "eval-2"},
    ]

    # 5) trace_id 를 지어내지 않는다 - 없으면 거절이다.
    try:
        _event(trace_id="")
        raise AssertionError("trace_id 없는 이벤트가 통과함")
    except ValueError:
        pass

    # 6) 불변식 3 - from_status 는 없을 수 있고(최초 등록), to_status 는 필수다.
    first = LifecycleEvent(
        agent_id="a1", to_status="CANDIDATE", trace_id="tr-2", occurred_at=now,
    )
    assert first.from_status is None
    try:
        LifecycleEvent(agent_id="a1", to_status="", trace_id="tr-3", occurred_at=now)
        raise AssertionError("빈 to_status 가 통과함")
    except ValueError:
        pass

    print("ok - Lifecycle Event 계약 6개 점검 통과")
