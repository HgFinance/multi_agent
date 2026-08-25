#!/usr/bin/env python3
"""F27: LLM Budget — 인사팀 담당분 (에이전트별 예산·비용 귀속·Scorecard).

소유: 영주 (Agent Workforce 인사팀)
근거: docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F27,
      docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md 3.2, 10.3(비용과 품질),
      docs/02-engineering/GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 3.4(get_department_scorecard)

F27은 두 부서가 나눠 맡는다. 이 모듈은 **인사팀 절반**이다.
  플랫폼/인프라 : 토큰 측정, 과금, 성능 저하 차단 (집행)
  인사팀(여기)  : 에이전트별 예산 설정, 비용 귀속, Scorecard, 조치 **권고**

여기에 LLM은 없다. 집계와 초과 판정은 결정론적 코드만 한다.

불변식:
  1. 인사팀은 권고만 한다. 실제 차단·모델 강등 집행은 플랫폼이 한다.
  2. 비용을 줄이려고 Risk/QA 독립성을 제거하지 않는다 (10.3). 통제 부서는
     예산을 초과해도 기능 축소를 권고하지 않고 CEO Escalation 으로 보낸다.
  3. Snapshot 이 없으면 0으로 채우지 않는다. 데이터 없음(None)과 사용량 0을 구분한다 —
     0으로 채우면 "예산 여유 있음"으로 잘못 보인다.
  4. 금액·토큰은 Decimal/int 로 다루고 float 로 계산하지 않는다.
  5. 단위는 DDL 을 따른다 — capacity 는 _ms(밀리초)와 _rate(비율)다.

자체 점검: python departments/07-agent-workforce/scorecard/cost.py
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

# 통제 부서 — 비용을 이유로 축소·강등을 권고하지 않는다 (10.3).
CONTROL_DEPARTMENTS: frozenset[str] = frozenset({"03-risk", "06-ai-qa-audit"})

# 예산 사용률 경고선.
WARNING_RATIO = Decimal("0.8")


class BudgetStatus(str, Enum):
    OK = "OK"
    WARNING = "WARNING"          # 경고선 초과, 아직 예산 내
    EXCEEDED = "EXCEEDED"        # 예산 초과
    UNKNOWN = "UNKNOWN"          # Snapshot 없음 — 여유 있다고 단정하지 않는다


class RecommendedAction(str, Enum):
    CONTINUE = "CONTINUE"
    REVIEW_BUDGET = "REVIEW_BUDGET"          # 경고 구간
    PROPOSE_MODEL_DOWNGRADE = "PROPOSE_MODEL_DOWNGRADE"   # Deep -> Quick 검토 제안
    ESCALATE_TO_CEO = "ESCALATE_TO_CEO"      # 통제 부서 초과 — 축소 대신 CEO 판단
    INVESTIGATE_MISSING_DATA = "INVESTIGATE_MISSING_DATA"


@dataclass(frozen=True)
class TokenBudget:
    """agent_profile_versions.token_budget 의 내부 계약."""

    per_case_tokens: int
    daily_tokens: int

    def __post_init__(self) -> None:
        if self.per_case_tokens <= 0 or self.daily_tokens <= 0:
            raise ValueError("token_budget 값은 0보다 커야 한다")


@dataclass(frozen=True)
class CostSnapshot:
    """workforce.cost_snapshots 한 행. 컬럼과 1:1.

    recorded_by 는 DB 컬럼상 not null 이지만 여기서는 선택값이다 - 이 dataclass 가
    저장된 행과 **계산용으로만 만든 값** 둘 다를 나른다. POST .../scorecard 나
    POST /budget-assessments 는 호출자가 실어 보낸 수치로 판정만 하고 저장하지
    않으므로 보고자가 없다(None). 저장 경로(append_cost_snapshot)에서만 값을 요구한다 -
    그쪽에서 빈 값을 거부한다.
    """

    agent_id: str
    profile_version_id: str
    window_start: datetime
    window_end: datetime
    input_tokens: int
    output_tokens: int
    model_cost: Decimal
    tool_cost: Decimal
    infra_cost: Decimal
    case_count: int
    currency: str = "USD"
    recorded_by: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def total_cost(self) -> Decimal:
        return self.model_cost + self.tool_cost + self.infra_cost


@dataclass(frozen=True)
class CapacitySnapshot:
    """workforce.capacity_snapshots 한 행. 지연은 _ms, 재시도·오류는 _rate.

    recorded_by 는 CostSnapshot 과 같은 이유로 선택값이다 - 저장된 행과 계산용으로만
    만든 값 둘 다 이 dataclass 로 나른다. 저장 경로(append_capacity_snapshot)에서만
    값을 요구한다.
    """

    window_start: datetime
    window_end: datetime
    arrivals: int
    queue_p95_ms: Decimal | None
    duration_p95_ms: Decimal | None
    retry_rate: Decimal | None
    error_rate: Decimal | None
    utilization: Decimal | None
    department_id: str | None = None
    agent_id: str | None = None
    recorded_by: str | None = None


@dataclass(frozen=True)
class BudgetAssessment:
    """에이전트 한 명의 예산 대비 사용 평가."""

    agent_id: str
    employee_code: str
    department_code: str
    tokens_used: int | None          # None = Snapshot 없음
    daily_budget: int
    usage_ratio: Decimal | None      # None = 산정 불가
    cost: Decimal | None
    status: BudgetStatus
    recommended_action: RecommendedAction
    is_control_role: bool
    note: str = ""


def assess_budget(
    *,
    agent_id: str,
    employee_code: str,
    department_code: str,
    budget: TokenBudget,
    snapshots: list[CostSnapshot],
) -> BudgetAssessment:
    """예산 대비 사용량을 평가하고 조치를 **권고**한다 (집행하지 않는다)."""
    is_control = department_code in CONTROL_DEPARTMENTS

    if not snapshots:
        # 불변식 3 — 데이터 없음을 0으로 채우지 않는다.
        return BudgetAssessment(
            agent_id=agent_id,
            employee_code=employee_code,
            department_code=department_code,
            tokens_used=None,
            daily_budget=budget.daily_tokens,
            usage_ratio=None,
            cost=None,
            status=BudgetStatus.UNKNOWN,
            recommended_action=RecommendedAction.INVESTIGATE_MISSING_DATA,
            is_control_role=is_control,
            note="cost_snapshots 없음 — 사용량 0이 아니라 측정 누락일 수 있다",
        )

    tokens_used = sum(s.total_tokens for s in snapshots)
    cost = sum((s.total_cost for s in snapshots), Decimal(0))
    ratio = Decimal(tokens_used) / Decimal(budget.daily_tokens)

    if ratio > 1:
        status = BudgetStatus.EXCEEDED
        # 불변식 2 — 통제 부서는 비용을 이유로 축소하지 않는다.
        if is_control:
            action = RecommendedAction.ESCALATE_TO_CEO
            note = "통제 부서 예산 초과 — 기능 축소 대신 CEO 판단으로 보낸다 (10.3)"
        else:
            action = RecommendedAction.PROPOSE_MODEL_DOWNGRADE
            note = "예산 초과 — Deep -> Quick 검토를 제안한다 (집행은 플랫폼)"
    elif ratio >= WARNING_RATIO:
        status = BudgetStatus.WARNING
        action = RecommendedAction.REVIEW_BUDGET
        note = f"예산 {WARNING_RATIO * 100:.0f}% 초과 사용"
    else:
        status = BudgetStatus.OK
        action = RecommendedAction.CONTINUE
        note = ""

    return BudgetAssessment(
        agent_id=agent_id,
        employee_code=employee_code,
        department_code=department_code,
        tokens_used=tokens_used,
        daily_budget=budget.daily_tokens,
        usage_ratio=ratio,
        cost=cost,
        status=status,
        recommended_action=action,
        is_control_role=is_control,
        note=note,
    )


def _num(value: Decimal | None) -> str | None:
    """numeric 은 부동소수점 오차를 피하려고 문자열로 직렬화한다 (API 설계서 3.4)."""
    return None if value is None else format(value, "f")


def build_department_scorecard(
    *,
    department_code: str,
    window_start: datetime,
    window_end: datetime,
    capacity: CapacitySnapshot | None,
    cost_snapshots: list[CostSnapshot],
    finding_count: int | None = None,
    rework_rate: Decimal | None = None,
    quality_references: dict | None = None,
) -> dict:
    """GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC 3.4 응답 형태로 조립한다.

    quality 의 Eval 원본은 QA/감사본부 소유(audit.eval_runs)다. 인사팀은 Reference 만
    싣고 값을 만들지 않는다 — eval_score 는 항상 None 으로 두고 audit-api 가 채운다.
    그 Reference 를 실제로 싣는 자리가 quality_references 다(quality.py
    QualityReferences) — eval_run_ids 가 없으면 소비자는 `eval_score: null` 만 보고
    어느 Eval 을 열어야 할지 알 수 없다. role_kpi 도 여기로 함께 온다(집계하지 않고
    출처별로 그대로).
    """
    if window_end <= window_start:
        raise ValueError("window_end 는 window_start 이후여야 한다")

    cost_block: dict = {
        "input_tokens": sum(s.input_tokens for s in cost_snapshots),
        "output_tokens": sum(s.output_tokens for s in cost_snapshots),
        "model_cost": _num(sum((s.model_cost for s in cost_snapshots), Decimal(0))),
        "tool_cost": _num(sum((s.tool_cost for s in cost_snapshots), Decimal(0))),
        "infra_cost": _num(sum((s.infra_cost for s in cost_snapshots), Decimal(0))),
        "case_count": sum(s.case_count for s in cost_snapshots),
        "currency": cost_snapshots[0].currency if cost_snapshots else None,
    }

    # 통화가 섞이면 합산이 무의미하다 — 합치지 않고 막는다.
    currencies = {s.currency for s in cost_snapshots}
    if len(currencies) > 1:
        raise ValueError(f"Snapshot 통화가 섞여 있어 합산할 수 없다: {sorted(currencies)}")

    return {
        "department_code": department_code,
        "window": {
            "window_start": window_start.isoformat().replace("+00:00", "Z"),
            "window_end": window_end.isoformat().replace("+00:00", "Z"),
        },
        "capacity": None
        if capacity is None
        else {
            "arrivals": capacity.arrivals,
            "queue_p95_ms": _num(capacity.queue_p95_ms),
            "duration_p95_ms": _num(capacity.duration_p95_ms),
            "retry_rate": _num(capacity.retry_rate),
            "error_rate": _num(capacity.error_rate),
            "utilization": _num(capacity.utilization),
        },
        "cost": cost_block if cost_snapshots else None,
        "quality": {
            "eval_score": None,  # audit-api 소유. 인사팀이 만들지 않는다.
            "finding_count": finding_count,
            "rework_rate": None if rework_rate is None else float(rework_rate),
            # 참조는 전달만 한다 - 없으면 빈 목록이지 None 이 아니다(집계 실패가
            # 아니라 "참조가 없었다"는 관측 사실이라서다). finding_count/rework_rate
            # 와 마찬가지로 quality.py 타입이 아니라 이미 풀어진 값으로 받는다.
            **(quality_references or {"eval_run_ids": [], "role_kpi": []}),
        },
    }


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timezone

    t0 = datetime(2026, 7, 24, tzinfo=timezone.utc)
    t1 = datetime(2026, 7, 31, tzinfo=timezone.utc)

    def snap(agent="a1", inp=100, out=100, cost="1", cases=1, currency="USD") -> CostSnapshot:
        return CostSnapshot(
            agent_id=agent, profile_version_id="pv1",
            window_start=t0, window_end=t1,
            input_tokens=inp, output_tokens=out,
            model_cost=Decimal(cost), tool_cost=Decimal(0), infra_cost=Decimal(0),
            case_count=cases, currency=currency,
        )

    budget = TokenBudget(per_case_tokens=1000, daily_tokens=1000)

    # 1) 정상 구간.
    a = assess_budget(agent_id="a1", employee_code="HR-01", department_code="07-agent-workforce",
                      budget=budget, snapshots=[snap(inp=200, out=200)])
    assert a.status is BudgetStatus.OK and a.recommended_action is RecommendedAction.CONTINUE
    assert a.tokens_used == 400 and a.usage_ratio == Decimal("0.4")

    # 2) 경고 구간 (80% 이상).
    a = assess_budget(agent_id="a1", employee_code="HR-01", department_code="07-agent-workforce",
                      budget=budget, snapshots=[snap(inp=450, out=400)])
    assert a.status is BudgetStatus.WARNING and a.recommended_action is RecommendedAction.REVIEW_BUDGET

    # 3) 초과 — 일반 부서는 모델 강등 검토 제안.
    a = assess_budget(agent_id="a1", employee_code="HR-00", department_code="07-agent-workforce",
                      budget=budget, snapshots=[snap(inp=800, out=800)])
    assert a.status is BudgetStatus.EXCEEDED
    assert a.recommended_action is RecommendedAction.PROPOSE_MODEL_DOWNGRADE

    # 4) 불변식 2 — 통제 부서(Risk/QA)는 초과해도 축소를 권고하지 않는다.
    for dept in ("03-risk", "06-ai-qa-audit"):
        a = assess_budget(agent_id="r1", employee_code="RSK-01", department_code=dept,
                          budget=budget, snapshots=[snap(inp=5000, out=5000)])
        assert a.status is BudgetStatus.EXCEEDED, dept
        assert a.is_control_role is True, dept
        assert a.recommended_action is RecommendedAction.ESCALATE_TO_CEO, dept
        assert a.recommended_action is not RecommendedAction.PROPOSE_MODEL_DOWNGRADE, dept

    # 5) 불변식 3 — Snapshot 없음을 사용량 0으로 채우지 않는다.
    a = assess_budget(agent_id="a1", employee_code="HR-01", department_code="07-agent-workforce",
                      budget=budget, snapshots=[])
    assert a.status is BudgetStatus.UNKNOWN
    assert a.tokens_used is None and a.usage_ratio is None
    assert a.recommended_action is RecommendedAction.INVESTIGATE_MISSING_DATA
    assert a.status is not BudgetStatus.OK, "데이터 없음을 정상으로 판정했다"

    # 6) 예산 값 검증.
    try:
        TokenBudget(per_case_tokens=0, daily_tokens=100)
        raise AssertionError("0 예산이 통과함")
    except ValueError:
        pass

    # 7) Scorecard 응답이 API 설계서 3.4 형태와 맞는지.
    cap = CapacitySnapshot(
        window_start=t0, window_end=t1, arrivals=120,
        queue_p95_ms=Decimal("45000.0000"), duration_p95_ms=Decimal("300000.0000"),
        retry_rate=Decimal("0.02500000"), error_rate=Decimal("0.00833333"),
        utilization=Decimal("0.72000000"), department_id="d1",
    )
    card = build_department_scorecard(
        department_code="03-risk", window_start=t0, window_end=t1,
        capacity=cap, cost_snapshots=[snap(cost="2", cases=120)], finding_count=2,
    )
    assert set(card) == {"department_code", "window", "capacity", "cost", "quality"}
    assert card["capacity"]["queue_p95_ms"] == "45000.0000", "numeric 을 문자열로 유지"
    assert isinstance(card["cost"]["input_tokens"], int)
    assert card["cost"]["model_cost"] == "2"
    assert card["quality"]["eval_score"] is None, "Eval 원본은 audit 소유 — 인사팀이 만들지 않는다"
    # 참조를 안 넘기면 빈 목록이다(None 이 아니다 - "참조가 없었다"는 관측 사실).
    assert card["quality"]["eval_run_ids"] == [] and card["quality"]["role_kpi"] == []

    # 7-1) 참조를 넘기면 eval_score 가 None 이어도 어느 Eval 을 볼지 알 수 있다.
    referenced = build_department_scorecard(
        department_code="03-risk", window_start=t0, window_end=t1,
        capacity=cap, cost_snapshots=[snap(cost="2", cases=120)], finding_count=2,
        quality_references={
            "eval_run_ids": ["eval-1"],
            "role_kpi": [{"agent_id": "a1", "profile_version_id": "pv1",
                          "role_kpi": {"citation_coverage": 0.97}}],
        },
    )
    assert referenced["quality"]["eval_score"] is None, "참조를 실어도 값은 여전히 audit 소유"
    assert referenced["quality"]["eval_run_ids"] == ["eval-1"]
    assert referenced["quality"]["role_kpi"][0]["role_kpi"]["citation_coverage"] == 0.97

    # 8) Snapshot 없으면 0이 아니라 None (불변식 3).
    empty = build_department_scorecard(
        department_code="03-risk", window_start=t0, window_end=t1,
        capacity=None, cost_snapshots=[],
    )
    assert empty["capacity"] is None and empty["cost"] is None

    # 9) 통화가 섞이면 합산하지 않는다.
    try:
        build_department_scorecard(
            department_code="03-risk", window_start=t0, window_end=t1,
            capacity=None, cost_snapshots=[snap(currency="USD"), snap(currency="KRW")],
        )
        raise AssertionError("통화 혼합인데 합산됨")
    except ValueError:
        pass

    # 10) window 역전 거부.
    try:
        build_department_scorecard(
            department_code="03-risk", window_start=t1, window_end=t0,
            capacity=None, cost_snapshots=[],
        )
        raise AssertionError("window 역전이 통과함")
    except ValueError:
        pass

    print("ok - F27 예산·비용 Scorecard 점검 10개 통과")
