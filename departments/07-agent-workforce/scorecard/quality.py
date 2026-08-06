#!/usr/bin/env python3
"""P1-2 HR-04: Quality Snapshot — cost.py의 자매 모듈 (get_department_scorecard의 quality 블록).

소유: 영주 (Agent Workforce 인사팀)
근거: docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md P1-2("Quality Snapshot과 Workforce
      Plan을 실제 데이터에서 집계·저장한다. 빈 집계를 정상 운영 상태로 표시하지 않는다"),
      GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 3.4/7절
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

자체 점검: python departments/07-agent-workforce/scorecard/quality.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


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

    print("ok - Quality Snapshot 계약 8개 점검 통과")
