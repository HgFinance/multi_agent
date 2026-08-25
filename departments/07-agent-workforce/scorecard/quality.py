#!/usr/bin/env python3
"""P1-2 HR-04: Quality Snapshot — cost.py의 자매 모듈 (get_department_scorecard의 quality 블록).

소유: 영주 (Agent Workforce 인사팀)
근거: docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md P1-2("Quality Snapshot과 Workforce
      Plan을 실제 데이터에서 집계·저장한다. 빈 집계를 정상 운영 상태로 표시하지 않는다"),
      UNIFIED_DOMAIN_API_SPEC.md 5.4(Workforce), 9(구현 상태)
      대응 테이블: supabase/migrations/20260731000800_workforce_plan_quality_probation.sql
      (workforce.quality_snapshots)

cost.py와 같은 이유로 여기에 LLM은 없다. eval_score 원본은 QA/감사본부 소유
(audit.eval_runs)이므로 값을 복제하지 않고 eval_run_id로만 참조한다 - 이 모듈이
직접 만드는 값은 finding_count/rework_rate 뿐이다(테이블 주석과 동일).

불변식:
  1. window_end 는 window_start 이후여야 한다.
  2. department_id 또는 agent_id 중 하나는 있어야 한다 (DDL check 와 동일).
  3. Snapshot 이 없으면 0 으로 채우지 않는다 - cost.py 불변식 3 과 동일한 원칙.
     aggregate_quality([]) 는 (None, None) 이다.
  4. eval_run_id 와 role_kpi 는 **집계하지 않고 출처를 붙여 그대로 싣는다**
     (collect_quality_references). 이유는 그 함수 docstring 참고.

자체 점검: python departments/07-agent-workforce/scorecard/quality.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class QualitySnapshot:
    """workforce.quality_snapshots 한 행. 컬럼과 1:1."""

    window_start: datetime
    window_end: datetime
    recorded_by: str
    department_id: str | None = None
    agent_id: str | None = None
    profile_version_id: str | None = None
    eval_run_id: str | None = None
    finding_count: int | None = None
    rework_rate: Decimal | None = None
    role_kpi: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.window_end <= self.window_start:
            raise ValueError("window_end 는 window_start 이후여야 한다")
        if self.department_id is None and self.agent_id is None:
            raise ValueError("department_id 또는 agent_id 중 하나는 있어야 한다 (DDL check 와 동일)")
        if not self.recorded_by.strip():
            raise ValueError("recorded_by 가 비어 있으면 Snapshot 을 남길 수 없다")
        if self.finding_count is not None and self.finding_count < 0:
            raise ValueError("finding_count 는 음수일 수 없다")
        if self.rework_rate is not None and self.rework_rate < 0:
            raise ValueError("rework_rate 는 음수일 수 없다")


def aggregate_quality(
    snapshots: list[QualitySnapshot],
) -> tuple[int | None, Decimal | None]:
    """department Scorecard 의 quality 블록에 실어 보낼 (finding_count, rework_rate).

    불변식 3 - Snapshot 이 없으면 (None, None) 이다. 0건으로 채우면 "결함 없음"으로
    잘못 보인다 - "집계할 데이터 자체가 없음"과 구분해야 한다(cost.py 의 UNKNOWN 과 동일한
    이유). finding_count 는 합산하고, rework_rate 는 값이 있는 Snapshot 만으로 평균한다.
    """
    if not snapshots:
        return None, None

    counted = [s.finding_count for s in snapshots if s.finding_count is not None]
    finding_count = sum(counted) if counted else None

    rated = [s.rework_rate for s in snapshots if s.rework_rate is not None]
    rework_rate = (sum(rated, Decimal(0)) / len(rated)) if rated else None

    return finding_count, rework_rate


@dataclass(frozen=True)
class RoleKpiEntry:
    """역할 KPI 한 묶음과 그 출처.

    KPI 이름은 역할마다 다르다(docs/04-organization/AGENT_EMPLOYEE_PROFILES.md 의
    각 직원 프로필 `KPI:` 줄). 그래서 어느 Agent/Profile Version 의 값인지 없이
    dict 만 들고 다니면 "이 숫자가 누구 것인지"를 잃는다.
    """

    agent_id: str | None
    profile_version_id: str | None
    role_kpi: dict


@dataclass(frozen=True)
class QualityReferences:
    """Scorecard quality 블록에 **집계 없이** 실어 보내는 값.

    eval_run_ids
        audit.eval_runs 참조. 인사팀은 eval_score 를 복제하지 않고 Reference 만
        보관한다(테이블 주석) - 그런데 Scorecard 가 이 참조를 안 실으면 소비자는
        `eval_score: null` 만 보고 **어느 Eval 을 열어봐야 하는지** 알 수 없다.
        값을 만들지 않는다는 원칙과, 참조를 전달한다는 원칙은 서로 배타적이지 않다.

    role_kpi
        역할별 KPI. **평균·합산하지 않는다** - 역할마다 KPI 이름이 다르고
        (AGENT_EMPLOYEE_PROFILES.md), 같은 이름이라도 비율·건수·SLA 가 섞여 있어
        부서 단위로 합치는 규칙이 어디에도 정의돼 있지 않다. 없는 규칙을 여기서
        지어내면 숫자는 나오지만 뜻이 없다 - 출처를 붙여 그대로 넘기고, 해석은
        그 KPI 정의를 아는 쪽(HR-03 성과 평가)이 한다.
    """

    eval_run_ids: list[str]
    role_kpi: list[RoleKpiEntry]

    def as_dict(self) -> dict[str, Any]:
        return {
            "eval_run_ids": list(self.eval_run_ids),
            "role_kpi": [
                {
                    "agent_id": e.agent_id,
                    "profile_version_id": e.profile_version_id,
                    "role_kpi": dict(e.role_kpi),
                }
                for e in self.role_kpi
            ],
        }


def collect_quality_references(snapshots: list[QualitySnapshot]) -> QualityReferences:
    """Snapshot 목록에서 eval_run_id 참조와 역할 KPI 를 출처와 함께 모은다.

    aggregate_quality 와 나누는 이유: 저쪽은 **집계**(합산·평균)이고 이쪽은
    **전달**이다. 둘을 한 함수에 두면 "이 값은 계산된 것인가 실린 것인가"가
    호출부에서 흐려진다.

    비어 있는 role_kpi(`{}`)는 싣지 않는다 - DDL 기본값이라 "KPI 를 안 쟀다"와
    구별되지 않는 빈 칸이고, 그대로 실으면 출처만 있고 내용 없는 항목이 쌓인다.
    eval_run_id 는 중복을 제거하되 처음 나온 순서를 유지한다(같은 Eval 을 여러
    Snapshot 이 참조할 수 있다).
    """
    eval_run_ids: list[str] = []
    for s in snapshots:
        if s.eval_run_id and s.eval_run_id not in eval_run_ids:
            eval_run_ids.append(s.eval_run_id)

    role_kpi = [
        RoleKpiEntry(
            agent_id=s.agent_id, profile_version_id=s.profile_version_id, role_kpi=dict(s.role_kpi),
        )
        for s in snapshots
        if s.role_kpi
    ]
    return QualityReferences(eval_run_ids=eval_run_ids, role_kpi=role_kpi)


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta, timezone

    t0 = datetime(2026, 8, 6, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=7)

    # 1) 정상 Snapshot.
    snap = QualitySnapshot(
        window_start=t0, window_end=t1, recorded_by="hr-01", department_id="d1",
        eval_run_id="eval-1", finding_count=2, rework_rate=Decimal("0.05"),
    )
    assert snap.finding_count == 2

    # 2) window 역전 거부.
    try:
        QualitySnapshot(window_start=t1, window_end=t0, recorded_by="hr-01", department_id="d1")
        raise AssertionError("역전된 window 이 통과함")
    except ValueError:
        pass

    # 3) 불변식 2 - department_id/agent_id 둘 다 없으면 거부.
    try:
        QualitySnapshot(window_start=t0, window_end=t1, recorded_by="hr-01")
        raise AssertionError("department_id/agent_id 없이 통과함")
    except ValueError:
        pass

    # 4) recorded_by 없으면 거부.
    try:
        QualitySnapshot(window_start=t0, window_end=t1, recorded_by="  ", department_id="d1")
        raise AssertionError("recorded_by 없이 통과함")
    except ValueError:
        pass

    # 5) 음수 finding_count/rework_rate 거부.
    for bad in ({"finding_count": -1}, {"rework_rate": Decimal("-0.1")}):
        try:
            QualitySnapshot(window_start=t0, window_end=t1, recorded_by="hr-01", department_id="d1", **bad)
            raise AssertionError(f"음수 {bad} 가 통과함")
        except ValueError:
            pass

    # 6) 불변식 3 - Snapshot 없으면 (None, None), 0건으로 채우지 않는다.
    assert aggregate_quality([]) == (None, None)

    # 7) 합산·평균.
    snap2 = QualitySnapshot(
        window_start=t0, window_end=t1, recorded_by="hr-01", department_id="d1",
        finding_count=4, rework_rate=Decimal("0.15"),
    )
    finding_count, rework_rate = aggregate_quality([snap, snap2])
    assert finding_count == 6
    assert rework_rate == Decimal("0.10")

    # 8) 일부만 값이 있어도 있는 값만으로 집계한다(0으로 채우지 않는다).
    snap3 = QualitySnapshot(window_start=t0, window_end=t1, recorded_by="hr-01", department_id="d1")
    finding_count, rework_rate = aggregate_quality([snap, snap3])
    assert finding_count == 2, "값 없는 Snapshot 을 0 으로 채워 합산을 왜곡시키면 안 된다"
    assert rework_rate == Decimal("0.05")

    # 9) 참조값 - eval_run_id 는 중복 제거하되 순서 유지, 빈 role_kpi 는 싣지 않는다.
    kpi_a = QualitySnapshot(
        window_start=t0, window_end=t1, recorded_by="hr-03", agent_id="a1",
        profile_version_id="pv1", eval_run_id="eval-1",
        role_kpi={"citation_coverage": 0.97, "retraction_rate": 0.01},
    )
    kpi_b = QualitySnapshot(
        window_start=t0, window_end=t1, recorded_by="hr-03", agent_id="a2",
        profile_version_id="pv2", eval_run_id="eval-2",
        role_kpi={"orphan_task": 0, "sla_compliance": 0.99},
    )
    # eval-1 을 다시 참조하고 role_kpi 는 비어 있는 Snapshot.
    dup = QualitySnapshot(
        window_start=t0, window_end=t1, recorded_by="hr-03", agent_id="a3", eval_run_id="eval-1",
    )
    refs = collect_quality_references([kpi_a, kpi_b, dup])
    assert refs.eval_run_ids == ["eval-1", "eval-2"], refs.eval_run_ids
    assert len(refs.role_kpi) == 2, "빈 role_kpi 가 실렸다"
    assert refs.role_kpi[0].agent_id == "a1"
    assert refs.role_kpi[0].role_kpi["citation_coverage"] == 0.97
    # 역할이 다르면 KPI 이름도 다르다 - 합치지 않고 출처별로 남는다.
    assert refs.role_kpi[1].role_kpi.keys() != refs.role_kpi[0].role_kpi.keys()

    # 10) Snapshot 이 없으면 참조도 빈 목록이다(없는 것을 지어내지 않는다).
    empty_refs = collect_quality_references([])
    assert empty_refs.eval_run_ids == [] and empty_refs.role_kpi == []
    assert empty_refs.as_dict() == {"eval_run_ids": [], "role_kpi": []}

    print("ok - Quality Snapshot 계약 10개 점검 통과")
