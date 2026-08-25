#!/usr/bin/env python3
"""HR-03 성과 평가(Performance Review) 계약.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/04-organization/AGENT_EMPLOYEE_PROFILES.md HR-03("재직 Agent는 역할 KPI로
      평가하고 반복 오류를 지식·Skill·Tool·Workflow 문제로 분류한다. 개선 후에도
      기준에 미달하면 역할 축소나 비활성화를 **제안**한다", 공식 산출물 Probation
      Review·Performance Improvement Plan·Promotion/Exit Proposal)
      대응 테이블: supabase/migrations/20260729000200_governance_workforce.sql
      (workforce.performance_reviews)

이 모듈이 scorecard/quality.py 의 종착지다. quality_snapshots 의 role_kpi 는
집계되지 않고 출처만 붙어 Scorecard 로 나가는데(collect_quality_references),
그 값을 실제로 **해석**해 평가로 만드는 쪽이 HR-03 이고 그 결과가 role_metrics 다.

cost.py/quality.py 와 같은 이유로 여기에 LLM 은 없다. 판정은 결정론적 코드만 한다.

불변식:
  1. period_end 는 period_start 이후여야 한다.
  2. **조치를 제안하는 평가는 역할 KPI 없이 내릴 수 없다.** decision 이 CONTINUE 가
     아니면 role_metrics 가 비어 있으면 안 된다 - 역할 축소·비활성화 제안은 사람의
     경력에 해당하는 결정이고, 근거 없이 내리면 되돌릴 방법이 없다(improvements/
     workflow.py 의 KEPT/ROLLED_BACK Scorecard 게이트와 같은 종류의 요구다).
  3. **이 모듈은 제안만 한다.** decision=DEACTIVATION 이어도 Agent 의 employment
     status 를 바꾸지 않는다 - 실제 비활성화는 CEO 승인과 roster 전이 게이트를 따로
     거친다(CLAUDE.md: "hr-department 는 자기 후보를 스스로 최종 승인할 수 없다").
     이 모듈은 roster 를 import 하지 않는다.

자체 점검: python departments/07-agent-workforce/performance/review.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ReviewDecision(str, Enum):
    """평가의 결론.

    DDL 의 `decision text not null` 에는 check 제약이 없다 - 값 어휘를 여기서 정한다.
    새로 지어내지 않고 **workforce.performance_actions.action_type 의 4개를 그대로
    쓰고**, "조치 없음" 한 칸(CONTINUE)만 더한다. 둘이 어긋나면 "평가는 PIP 를
    제안했는데 조치는 DEACTIVATION" 같은 조합이 조용히 생긴다(action.py 가 이
    일치를 강제한다).

    CONTINUE      기준 충족 - 후속 조치 없음
    LEARNING      교육·지식 보강 제안
    PIP           Performance Improvement Plan 제안
    ROLE_CHANGE   역할 축소·변경 제안
    DEACTIVATION  비활성화 제안 (제안일 뿐 집행이 아니다 - 불변식 3)
    """

    CONTINUE = "CONTINUE"
    LEARNING = "LEARNING"
    PIP = "PIP"
    ROLE_CHANGE = "ROLE_CHANGE"
    DEACTIVATION = "DEACTIVATION"


# 후속 조치를 동반하는 결정. CONTINUE 만 조치가 없다.
ACTIONABLE_DECISIONS: frozenset[ReviewDecision] = frozenset(
    {
        ReviewDecision.LEARNING,
        ReviewDecision.PIP,
        ReviewDecision.ROLE_CHANGE,
        ReviewDecision.DEACTIVATION,
    }
)


class MissingRoleMetricsError(Exception):
    """조치를 제안하는 평가인데 역할 KPI(role_metrics)가 비어 있다 (불변식 2)."""


@dataclass(frozen=True)
class PerformanceReview:
    """workforce.performance_reviews 한 행. 컬럼과 1:1.

    role_metrics/cost/findings 는 DDL 에서 not null 이지만 `{}` 는 통과한다 - 빈
    dict 와 "안 쟀다"를 DB 가 구분해주지 않으므로, 무엇을 비워도 되는지는 여기서
    결정한다(불변식 2).
    """

    review_id: str
    agent_id: str
    profile_version_id: str
    period_start: datetime
    period_end: datetime
    decision: ReviewDecision
    # 누가 평가했는가. 형제 테이블의 recorded_by/author 와 같은 자리다
    # (20260825000500 migration) - 역할 축소·비활성화 제안이 누구 것인지 없이 남으면
    # CEO 승인 단계에서 자기 평가와 독립 평가를 구별할 수 없다.
    reviewer: str = ""
    # 역할별 KPI 값. 이름은 역할마다 다르다(AGENT_EMPLOYEE_PROFILES.md 각 프로필의
    # `KPI:` 줄) - quality_snapshots.role_kpi 와 같은 이름 공간이다.
    role_metrics: dict = field(default_factory=dict)
    # 이 기간의 비용 귀속. 원천은 플랫폼 과금 계측이고 인사팀은 귀속만 한다(cost.py).
    cost: dict = field(default_factory=dict)
    # 반복 오류를 지식·Skill·Tool·Workflow 로 분류한 결과(HR-03 업무 수행).
    findings: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.period_end <= self.period_start:
            raise ValueError("period_end 는 period_start 이후여야 한다")
        if not self.reviewer.strip():
            raise ValueError("reviewer 가 없으면 평가를 남길 수 없다 - 누가 제안했는지가 승인의 전제다")
        if self.decision in ACTIONABLE_DECISIONS and not self.role_metrics:
            raise MissingRoleMetricsError(
                f"{self.decision.value} 제안에는 역할 KPI(role_metrics)가 필요하다 - "
                f"근거 없이 역할 축소·비활성화를 제안하지 않는다"
            )

    @property
    def proposes_action(self) -> bool:
        """후속 performance_action 이 필요한 평가인가."""
        return self.decision in ACTIONABLE_DECISIONS


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/performance/review.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta, timezone
    from pathlib import Path

    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=30)

    def _review(**over) -> PerformanceReview:
        base = {
            "review_id": "rv-1", "agent_id": "a1", "profile_version_id": "pv1",
            "period_start": t0, "period_end": t1, "decision": ReviewDecision.CONTINUE,
            "reviewer": "hr-03",
        }
        base.update(over)
        return PerformanceReview(**base)

    # 1) 조치 없는 평가는 role_metrics 가 비어도 된다 - "기준 충족"은 근거를 강제하지
    #    않는다(축소·비활성화와 달리 되돌릴 것이 없다).
    ok = _review()
    assert ok.decision is ReviewDecision.CONTINUE
    assert ok.proposes_action is False

    # 2) 조치를 제안하는 평가는 role_metrics 없이 못 만든다 (불변식 2).
    for decision in ACTIONABLE_DECISIONS:
        try:
            _review(decision=decision)
            raise AssertionError(f"{decision.value} 이 근거 없이 통과함")
        except MissingRoleMetricsError:
            pass

    # 3) 근거가 있으면 통과하고 proposes_action 이 True 다.
    pip = _review(
        decision=ReviewDecision.PIP,
        role_metrics={"sla_compliance": 0.71, "rework_rate": 0.22},
        findings=[{"category": "TOOL", "detail": "재시도 폭주"}],
    )
    assert pip.proposes_action is True
    assert pip.role_metrics["sla_compliance"] == 0.71

    # 4) period 역전·평가자 누락 거부.
    try:
        _review(period_start=t1, period_end=t0)
        raise AssertionError("역전된 period 가 통과함")
    except ValueError:
        pass
    for bad_reviewer in ("", "   "):
        try:
            _review(reviewer=bad_reviewer)
            raise AssertionError("평가자 없는 평가가 통과함")
        except ValueError:
            pass

    # 5) decision 어휘가 performance_actions.action_type 4개를 그대로 덮는다 -
    #    둘이 어긋나면 평가와 조치를 이을 수 없다(action.py 가 이 일치를 강제한다).
    assert {d.value for d in ACTIONABLE_DECISIONS} == {
        "LEARNING", "PIP", "ROLE_CHANGE", "DEACTIVATION"
    }

    # 6) 불변식 3 - 비활성화 "제안"은 고용 상태를 들고 있지 않다. 평가가 status 를
    #    갖고 있으면 그 자체로 집행 수단이 되므로, 이 모듈 소스에 roster 참조가
    #    없다는 것까지 같이 고정한다.
    deact = _review(decision=ReviewDecision.DEACTIVATION, role_metrics={"false_promotion": 3})
    assert not hasattr(deact, "employment_status"), "평가가 고용 상태를 들고 있으면 집행에 가깝다"
    # 자체 점검 블록 자신은 제외하고 모듈 본문만 본다(이 검사 문자열이 스스로에게
    # 걸리지 않도록).
    _body = Path(__file__).read_text(encoding="utf-8").split('if __name__ ==')[0]
    assert not any(
        line.startswith(("import roster", "from roster"))
        for line in (ln.strip() for ln in _body.splitlines())
    ), "이 모듈이 roster 를 import 하면 제안과 집행의 경계가 무너진다"

    print("ok - Performance Review 계약 6개 점검 통과")
