#!/usr/bin/env python3
"""HR-00/HR-03 수습 기간(Probation) 추적 — review.py/action.py의 자매 모듈.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/04-organization/AGENT_EMPLOYEE_PROFILES.md
      HR-00("활성화 후 수습 기간의 KPI와 종료 조건을 추적한다", KPI: 수습 통과 후 성과),
      HR-03("**채용 전에 Pass/Fail과 비용 한도를 고정하고** Historical Replay,
      Adversarial Case, Tool Failure와 Shadow Test를 실행한다", 필수 Skill: Shadow
      Probation, 공식 산출물: Probation Review, KPI: 수습 실패율)
      대응 테이블: supabase/migrations/20260731000800_workforce_plan_quality_probation.sql
      (workforce.probation_periods)

performance/ 아래 두는 이유: DDL 주석은 이 표를 "채용 Workflow의 Shadow/Paper 수습
단계"로 소개하지만, 같은 주석이 "selection_reviews(채용 결정 자체)와는 다른 대상이다 —
이건 **채용된 이후의 관찰 기간** 자체를 추적한다"고 못박는다. 관찰 기간을 열고 →
지표를 모으고 → 판정하는 모양이 review.py 와 같고, Probation Review 는 HR-03 의 공식
산출물이다. 채용 결정 자체(hiring/)와는 분리돼 있다.

불변식:
  1. **종료 조건 없이 수습을 시작할 수 없다.** success_metrics 는 DDL 기본값이 `{}` 라
     빈 채로 열 수 있지만, 그러면 관찰이 끝난 뒤에 기준을 만들게 된다 - HR-03 이
     "채용 **전에** Pass/Fail 을 고정하고"라고 못박은 것이 정확히 이걸 막으려는 것이다.
  2. **판정할 때 기준을 바꿀 수 없다.** 1번의 이빨이다 - 끝나고 기준을 옮길 수 있으면
     미리 고정하는 의미가 없다. close_probation 은 success_metrics 를 손대지 않는다.
  3. 종료된 수습은 결과가 있어야 하고(DDL check 와 같은 규칙), 종료 상태에서 다시
     판정하지 않는다.
  4. **수습 결과는 고용 상태를 바꾸지 않는다.** PASSED 여도 Agent 가 ACTIVE 가 되지
     않는다 - roster 전이는 QA Eval 실재성과 CEO 승인 게이트(P0-3)를 따로 거친다.
     review.py 불변식 3과 같고, 이 모듈도 roster 를 import 하지 않는다.

stage(SHADOW/PAPER) 순서는 제약하지 않는다 - DDL 도 문서도 "SHADOW 를 통과해야 PAPER"
라고 정한 곳이 없어서다. 정하려면 그 규칙을 먼저 문서에 세운다.

자체 점검: python departments/07-agent-workforce/performance/probation.py
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum


class ProbationStage(str, Enum):
    """DDL 의 stage check 제약과 같은 2개."""

    SHADOW = "SHADOW"
    PAPER = "PAPER"


class ProbationResult(str, Enum):
    """DDL 의 result check 제약과 같은 3개.

    EXTENDED 는 "이 기간은 닫되 수습 자체는 계속"이다 - 이 행은 ended_at 이 찍혀
    종료되고, 이어지는 관찰은 **새 행**으로 연다(open_probation). 한 행을 계속
    늘리면 어느 기준으로 얼마나 관찰했는지가 뭉개진다.
    """

    PASSED = "PASSED"
    FAILED = "FAILED"
    EXTENDED = "EXTENDED"


class MissingSuccessMetricsError(Exception):
    """종료 조건(success_metrics) 없이 수습을 시작하려 함 (불변식 1)."""


class ProbationAlreadyClosedError(Exception):
    """이미 종료된 수습을 다시 판정하려 함 (불변식 3)."""


@dataclass(frozen=True)
class ProbationPeriod:
    """workforce.probation_periods 한 행. 컬럼과 1:1."""

    probation_id: str
    agent_id: str
    profile_version_id: str
    stage: ProbationStage
    started_at: datetime
    # 시작 시점에 고정하는 Pass/Fail 기준과 종료 조건. 비어 있으면 열 수 없다(불변식 1).
    success_metrics: dict = field(default_factory=dict)
    ended_at: datetime | None = None
    result: ProbationResult | None = None

    def __post_init__(self) -> None:
        if not self.success_metrics:
            raise MissingSuccessMetricsError(
                "success_metrics 없이 수습을 열 수 없다 - Pass/Fail 기준은 관찰 전에 고정한다"
            )
        if self.ended_at is not None and self.ended_at <= self.started_at:
            raise ValueError("ended_at 은 started_at 이후여야 한다")
        # DDL check 를 앱에서도 지킨다 - 관찰만 하고 판정을 미루지 않는다.
        if self.ended_at is not None and self.result is None:
            raise ValueError("종료된 수습은 result 가 있어야 한다")
        if self.ended_at is None and self.result is not None:
            raise ValueError("끝나지 않은 수습에 result 가 있을 수 없다")

    @property
    def is_closed(self) -> bool:
        return self.ended_at is not None


def open_probation(
    *,
    probation_id: str,
    agent_id: str,
    profile_version_id: str,
    stage: ProbationStage,
    started_at: datetime,
    success_metrics: dict,
) -> ProbationPeriod:
    """수습을 연다. 기준(success_metrics)을 여기서 고정한다 (불변식 1)."""
    return ProbationPeriod(
        probation_id=probation_id, agent_id=agent_id,
        profile_version_id=profile_version_id, stage=stage,
        started_at=started_at, success_metrics=success_metrics,
    )


def close_probation(
    probation: ProbationPeriod, *, result: ProbationResult, at: datetime,
) -> ProbationPeriod:
    """수습을 판정하고 닫는다.

    success_metrics 를 인자로 받지 않는다 - 판정 시점에 기준을 못 바꾼다는 것이
    불변식 2이고, 인자로 열어두면 그 불변식이 호출자 선의에 맡겨진다.
    """
    if probation.is_closed:
        raise ProbationAlreadyClosedError(
            f"이미 {probation.result.value} 로 종료된 수습이다"
        )
    return replace(probation, ended_at=at, result=result)


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/performance/probation.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta, timezone
    from pathlib import Path

    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=14)

    _metrics = {"pass_if": {"sla_compliance": ">=0.95"}, "max_cost_usd": 20}

    def _open(**over) -> ProbationPeriod:
        base = {
            "probation_id": "pb-1", "agent_id": "a1", "profile_version_id": "pv1",
            "stage": ProbationStage.SHADOW, "started_at": t0,
            "success_metrics": _metrics,
        }
        base.update(over)
        return open_probation(**base)

    # 1) 기준 없이 수습을 열 수 없다 (불변식 1) - 관찰이 끝난 뒤 기준을 만들지 않는다.
    try:
        _open(success_metrics={})
        raise AssertionError("기준 없는 수습이 열렸다")
    except MissingSuccessMetricsError:
        pass

    # 2) 정상 흐름: 열고 -> 판정.
    p = _open()
    assert p.is_closed is False and p.result is None
    passed = close_probation(p, result=ProbationResult.PASSED, at=t1)
    assert passed.is_closed and passed.result is ProbationResult.PASSED
    assert passed.ended_at == t1

    # 3) 불변식 2 - 판정이 기준을 바꾸지 않는다. close_probation 에 기준을 넘길
    #    자리가 아예 없어야 한다(있으면 호출자 선의에 맡겨진다).
    assert passed.success_metrics == _metrics, "판정 후 기준이 바뀌었다"
    import inspect
    assert "success_metrics" not in inspect.signature(close_probation).parameters, (
        "close_probation 이 기준을 인자로 받으면 불변식 2가 호출자 선의에 맡겨진다"
    )

    # 4) 종료된 수습 재판정 불가 (불변식 3).
    try:
        close_probation(passed, result=ProbationResult.FAILED, at=t1)
        raise AssertionError("종료된 수습이 다시 판정됐다")
    except ProbationAlreadyClosedError:
        pass

    # 5) DDL check 를 앱에서도 지킨다 - 종료엔 결과가 필요하고, 미종료엔 결과가 없다.
    for bad in ({"ended_at": t1}, {"result": ProbationResult.PASSED}):
        try:
            ProbationPeriod(
                probation_id="pb-x", agent_id="a1", profile_version_id="pv1",
                stage=ProbationStage.PAPER, started_at=t0, success_metrics=_metrics, **bad,
            )
            raise AssertionError(f"{bad} 가 통과함")
        except ValueError:
            pass
    # 역전된 기간도 거부.
    try:
        ProbationPeriod(
            probation_id="pb-y", agent_id="a1", profile_version_id="pv1",
            stage=ProbationStage.PAPER, started_at=t1, success_metrics=_metrics,
            ended_at=t0, result=ProbationResult.PASSED,
        )
        raise AssertionError("역전된 기간이 통과함")
    except ValueError:
        pass

    # 6) EXTENDED 는 이 행을 닫고, 이어지는 관찰은 새 행이다 - 한 행을 늘리지 않는다.
    ext = close_probation(_open(probation_id="pb-2"), result=ProbationResult.EXTENDED, at=t1)
    assert ext.is_closed and ext.ended_at == t1
    nxt = _open(probation_id="pb-3", started_at=t1)
    assert nxt.is_closed is False and nxt.probation_id != ext.probation_id

    # 7) 불변식 4 - 수습 결과가 고용 상태를 바꾸지 않는다.
    assert not hasattr(passed, "employment_status"), "수습이 고용 상태를 들고 있으면 집행에 가깝다"
    _body = Path(__file__).read_text(encoding="utf-8").split('if __name__ ==')[0]
    assert not any(
        line.startswith(("import roster", "from roster"))
        for line in (ln.strip() for ln in _body.splitlines())
    ), "이 모듈이 roster 를 import 하면 수습 통과가 곧 활성화가 된다"

    print("ok - Probation 계약 7개 점검 통과")
