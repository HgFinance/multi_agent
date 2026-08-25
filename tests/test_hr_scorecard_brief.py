"""HR 부서 Scorecard 브리프 렌더러 계약 테스트 (2026-08-26 신규).

departments/07-agent-workforce/scorecard/scorecard_brief.py 의 __main__ 자체 점검이
기본 렌더링을 이미 확인하지만, 여기서는 CI 에서 항상 돌아야 하는 **오독 방지 계약**을
pytest 로 고정한다. 표 모양이 아니라 아래 셋이 이 렌더러의 존재 이유다.

  1. Snapshot 없음(`—`/NO_SNAPSHOT)이 0 으로 둔갑하지 않는다
  2. 판정을 렌더러가 다시 만들지 않는다 (cost.assess_budget 값을 그대로 옮긴다)
  3. 관측 창이 다른 부서를 조용히 한 표에 세우지 않는다

test_hr_idle_agents.py 와 같은 이유로 pytest 로 못 박는다 - 상태를 뭉개는 회귀는
표가 여전히 그럴듯해 보이기 때문에 사람 눈으로는 안 잡힌다.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_DEPARTMENT = ROOT / "departments" / "07-agent-workforce"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(_DEPARTMENT / "scorecard"))
# 부서 루트(reporting.py)는 **뒤에** 붙인다 - 같은 곳에 scripts.py 가 있어서 앞에
# 끼우면 같은 pytest 세션의 다른 테스트가 쓰는 저장소 루트 scripts/ 패키지를 가린다.
sys.path.append(str(_DEPARTMENT))

from cost import (  # noqa: E402
    BudgetAssessment,
    BudgetStatus,
    CapacitySnapshot,
    CostSnapshot,
    RecommendedAction,
    build_department_scorecard,
)
from scorecard_brief import (  # noqa: E402
    MISSING,
    SCHEMA_VERSION,
    build_scorecard_brief,
    render_budget_table,
    render_reference_table,
)

_T0 = datetime(2026, 8, 19, tzinfo=timezone.utc)
_T1 = datetime(2026, 8, 26, tzinfo=timezone.utc)


def _capacity() -> CapacitySnapshot:
    return CapacitySnapshot(
        window_start=_T0, window_end=_T1, arrivals=120,
        queue_p95_ms=Decimal("840"), duration_p95_ms=Decimal("2100"),
        retry_rate=Decimal("0.02"),
        # 관측된 0 이다 - 결측과 구분되는지 보는 값.
        error_rate=Decimal("0"),
        utilization=Decimal("0.61"),
    )


def _cost() -> CostSnapshot:
    return CostSnapshot(
        agent_id="a1", profile_version_id="pv1", window_start=_T0, window_end=_T1,
        input_tokens=1200, output_tokens=800, model_cost=Decimal("3.5"),
        tool_cost=Decimal("0"), infra_cost=Decimal("0"), case_count=12,
    )


def _observed(department_code: str = "research-department", **overrides) -> dict:
    payload = {
        "department_code": department_code,
        "window_start": _T0, "window_end": _T1,
        "capacity": _capacity(), "cost_snapshots": [_cost()],
        "finding_count": 0, "rework_rate": Decimal("0.05"),
        "quality_references": {"eval_run_ids": ["eval-77"], "role_kpi": []},
    }
    payload.update(overrides)
    return build_department_scorecard(**payload)


def _unobserved(department_code: str = "risk-management") -> dict:
    """capacity·cost Snapshot 이 하나도 없는 부서."""

    return build_department_scorecard(
        department_code=department_code, window_start=_T0, window_end=_T1,
        capacity=None, cost_snapshots=[], finding_count=None, rework_rate=None,
    )


def _row(brief: str, prefix: str) -> str:
    return next(line for line in brief.splitlines() if line.startswith(prefix))


# ---------------------------------------------------------------------------
# 1. 결측을 0 으로 렌더링하지 않는다 (cost.py 불변식 3 의 렌더링 쪽 절반)
# ---------------------------------------------------------------------------


def test_missing_snapshot_is_never_rendered_as_zero() -> None:
    brief = build_scorecard_brief([_observed(), _unobserved()])
    row = _row(brief, "| risk-management | NO_SNAPSHOT")
    assert "| 0 |" not in row, row
    assert MISSING in row
    # 블록 자체가 없다는 사실이 별도 컬럼으로 남아야 한다 - 셀만 비면 "필드 하나가
    # 빈 것"과 "Snapshot 이 아예 없는 것"이 같아 보인다.
    assert "NO_SNAPSHOT" in row


def test_observed_zero_survives_as_zero() -> None:
    """error_rate 0 은 관측된 사실이다 - 결측 기호로 바뀌면 오류가 사라진 것처럼 보인다."""

    brief = build_scorecard_brief([_observed()])
    row = _row(brief, "| research-department | OBSERVED")
    assert "| 0 |" in row, row
    assert MISSING not in row, row


def test_legend_states_that_missing_and_zero_differ() -> None:
    brief = build_scorecard_brief([_unobserved()])
    assert SCHEMA_VERSION in brief
    assert "둘을 같은 뜻으로 읽지 않는다" in brief
    assert "사용량이 0이라는 뜻이 아니다" in brief


# ---------------------------------------------------------------------------
# 2. 판정을 렌더러가 다시 만들지 않는다
# ---------------------------------------------------------------------------


def test_budget_verdict_is_transported_not_recomputed() -> None:
    """사용률 2.0 인데 status 가 OK 인 (일부러 어긋난) 판정을 그대로 실어야 한다.

    렌더러가 임계값을 알고 있으면 여기서 EXCEEDED 로 '고쳐' 쓴다. 그 순간 판정
    소유자가 cost.py 에서 이 표로 옮겨간다 - 이 테스트가 막는 것이 그 이동이다.
    """

    inconsistent = BudgetAssessment(
        agent_id="agent-1", employee_code="RES-01", department_code="research-department",
        tokens_used=2000, daily_budget=1000, usage_ratio=Decimal("2.0"),
        cost=Decimal("3.5"), status=BudgetStatus.OK,
        recommended_action=RecommendedAction.CONTINUE, is_control_role=False,
    )
    rendered = "\n".join(render_budget_table([inconsistent]))
    assert "| OK | CONTINUE |" in rendered, rendered
    assert "EXCEEDED" not in rendered


def test_brief_tells_the_head_not_to_rejudge() -> None:
    assessment = BudgetAssessment(
        agent_id="agent-2", employee_code="RISK-01", department_code="risk-management",
        tokens_used=2000, daily_budget=1000, usage_ratio=Decimal("2.0"),
        cost=Decimal("3.5"), status=BudgetStatus.EXCEEDED,
        recommended_action=RecommendedAction.ESCALATE_TO_CEO, is_control_role=True,
        note="통제 부서 예산 초과",
    )
    brief = build_scorecard_brief([_observed()], assessments=[assessment])
    assert "재판정 금지" in brief
    assert "다시 판정하지 말고" in brief
    assert "| ESCALATE_TO_CEO | Y |" in brief


def test_eval_score_is_flagged_as_qa_owned_not_as_a_quality_gap() -> None:
    """eval_score 는 항상 비어 있다 - 그 공백을 품질 문제로 읽으면 안 된다."""

    brief = build_scorecard_brief([_observed()])
    assert "audit.eval_runs" in brief
    assert "품질 문제로 읽지 말고" in brief


# ---------------------------------------------------------------------------
# 3. 창이 다른 부서를 한 표에 세우지 않는다
# ---------------------------------------------------------------------------


def test_mismatched_windows_are_surfaced() -> None:
    shifted = _observed(
        "qa-department", window_end=datetime(2026, 8, 25, tzinfo=timezone.utc)
    )
    brief = build_scorecard_brief([_observed(), shifted])
    assert "부서마다 다르다" in brief
    assert "## 관측 창" in brief
    assert "같은 기준으로 비교하지 않는다" in brief


def test_uniform_windows_are_stated_once() -> None:
    brief = build_scorecard_brief([_observed(), _unobserved()])
    assert "전 부서 동일" in brief
    assert "## 관측 창" not in brief


# ---------------------------------------------------------------------------
# 표가 깨지는 경로
# ---------------------------------------------------------------------------


def test_reference_arrays_go_to_their_own_table() -> None:
    """배열을 셀에 밀어 넣으면 LLM 이 부서와 참조를 잘못 짝짓는다."""

    brief = build_scorecard_brief([_observed(), _unobserved()])
    assert "| research-department | eval_run_ids | eval-77 |" in brief
    assert "['eval-77']" not in brief and '["eval-77"]' not in brief
    # 참조가 하나도 없으면 빈 표를 만들지 않는다.
    assert render_reference_table([_unobserved()]) == []


def test_pipe_in_a_value_does_not_break_the_table() -> None:
    piped = _unobserved("trading-department | 임시")
    assert "trading-department \\| 임시" in build_scorecard_brief([piped])


def test_empty_input_is_rejected_rather_than_rendered_as_an_empty_brief() -> None:
    """빈 브리프는 '지표 없음'으로 읽힌다 - 그건 관측 결과가 아니다."""

    with pytest.raises(ValueError):
        build_scorecard_brief([])
    with pytest.raises(ValueError):
        build_scorecard_brief([{"department_code": "research-department"}])
