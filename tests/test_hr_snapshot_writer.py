"""Langfuse 관측 → workforce Snapshot writer 계약 (2026-08-27 신설).

이 producer 가 없던 동안 workforce.capacity_snapshots / cost_snapshots 는 계속
비어 있었고, Scorecard 브리프의 처리량·비용이 영구 NO_SNAPSHOT 이었다. 정작 같은
수치는 Langfuse 쪽에 있었다 - 두 출처가 안 이어져 있었을 뿐이다.

여기서 고정하는 것은 "옮겨진다"가 아니라 **무엇을 안 옮기는가**다. 관측 못 한
것을 0 으로 적는 순간 그 값으로 인원·예산 조치가 결정되기 때문이다.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "departments/07-agent-workforce/scorecard"))

from observability import (  # noqa: E402
    CapacityObservationStatus,
    DepartmentCapacityReport,
    WorkerUsageObservationStatus,
    WorkerUsageReport,
)
from snapshot_writer import (  # noqa: E402
    DEPARTMENT_CODE_BY_STAGE_KEY,
    MODEL_PRICING,
    RECORDED_BY,
    UnknownDepartmentKey,
    UnpricedModel,
    build_capacity_snapshot,
    build_cost_snapshot,
    department_code_for,
    model_cost_usd,
    write_observability_snapshots,
)

_END = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
_START = _END - timedelta(hours=24)


class _FakeRepo:
    """저장소 대역. 실제로 적힌 것만 들고 있는다."""

    def __init__(self, *, departments=None, agents=None) -> None:
        self._departments = departments if departments is not None else {
            code: f"dept-{code}" for code in DEPARTMENT_CODE_BY_STAGE_KEY.values()
        }
        self._agents = agents if agents is not None else {}
        self.capacity: list = []
        self.cost: list = []

    def get_department_id(self, department_code: str):
        return self._departments.get(department_code)

    def get_agent_cost_subject(self, employee_code: str):
        return self._agents.get(employee_code)

    def append_capacity_snapshot(self, snapshot):
        self.capacity.append(snapshot)
        return ("cap-1", True)

    def append_cost_snapshot(self, snapshot):
        self.cost.append(snapshot)
        return ("cost-1", True)


class _Observability:
    """WorkforceObservability 중 writer 가 읽는 두 필드만 가진 대역."""

    def __init__(self, capacity=(), worker_usage=()) -> None:
        self.capacity = tuple(capacity)
        self.worker_usage = tuple(worker_usage)


def _capacity(
    department="research", status=CapacityObservationStatus.MEASURED, arrivals=69, **kw
) -> DepartmentCapacityReport:
    base = dict(
        department=department, window_start=_START, window_end=_END, status=status,
        arrivals=arrivals, duration_p95_ms=1200.0, retry_rate=0.0, error_rate=0.02,
        utilization=0.13,
    )
    base.update(kw)
    return DepartmentCapacityReport(**base)


def _usage(
    worker_id="competing-explanation-worker", department="research",
    status=WorkerUsageObservationStatus.MEASURED, prompt=1732, completion=183,
    models=("qwen2.5-14b-instruct-awq",), arrivals=67, **kw
) -> WorkerUsageReport:
    base = dict(
        department=department, worker_id=worker_id, window_start=_START, window_end=_END,
        status=status, arrivals=arrivals, llm_calls=70, prompt_tokens=prompt,
        completion_tokens=completion, model_names=tuple(models),
    )
    base.update(kw)
    return WorkerUsageReport(**base)


# ── 부서 키 다리 ──────────────────────────────────────────────────────────────


def test_department_key_bridge_is_a_table_not_a_naming_rule() -> None:
    """이름 규칙으로 유도하면 risk/qa 에서 이미 깨진다."""

    assert department_code_for("research") == "research-department"
    assert department_code_for("risk") == "risk-management"
    assert department_code_for("qa") == "qa-department"
    # 규칙(`+ "-department"`)이었다면 risk-department 가 돼 조용히 404 가 난다.
    assert DEPARTMENT_CODE_BY_STAGE_KEY["risk"] != "risk-department"


def test_unknown_department_key_fails_loudly() -> None:
    with pytest.raises(UnknownDepartmentKey):
        department_code_for("hr")


def test_bridge_covers_every_observed_department() -> None:
    from observability import INVESTMENT_DEPARTMENT_STAGE  # noqa: PLC0415

    assert set(DEPARTMENT_CODE_BY_STAGE_KEY) == set(INVESTMENT_DEPARTMENT_STAGE), (
        "관측 부서와 다리 표가 갈렸다 - 빠진 부서는 Snapshot 이 조용히 안 적힌다"
    )


# ── 비용: qwen 자체 호스팅은 0달러 ────────────────────────────────────────────


def test_self_hosted_qwen_costs_zero() -> None:
    """토큰당 과금이 있는 API 가 아니라 우리가 띄운 모델이다."""

    for model in ("qwen2.5-14b-instruct-awq", "qwen3:1.7b"):
        assert MODEL_PRICING[model] == (Decimal(0), Decimal(0))
        assert model_cost_usd(
            model_names=(model,), input_tokens=1_000_000, output_tokens=500_000
        ) == Decimal(0)


def test_mixed_zero_cost_models_still_sum_to_zero() -> None:
    """한 Worker 가 창 안에서 운영 AWQ ↔ 개발 fallback 을 갈아탄 경우."""

    assert model_cost_usd(
        model_names=("qwen2.5-14b-instruct-awq", "qwen3:1.7b"),
        input_tokens=5000, output_tokens=800,
    ) == Decimal(0)


def test_unknown_model_is_not_folded_to_zero() -> None:
    """0 을 기본값으로 두면 과금 모델로 바꾼 날 비용이 조용히 0 으로 적힌다."""

    with pytest.raises(UnpricedModel, match="unpriced_model"):
        model_cost_usd(model_names=("claude-opus-5",), input_tokens=10, output_tokens=10)


def test_no_observed_model_is_not_priced() -> None:
    with pytest.raises(UnpricedModel, match="no_model_observed"):
        model_cost_usd(model_names=(), input_tokens=10, output_tokens=10)


def test_nonzero_priced_models_refuse_to_be_summed_without_token_split() -> None:
    """단가가 0 이 아닌 모델이 표에 들어오면 합산 가정이 깨진다 - 그때 조용히 틀리지 않게."""

    MODEL_PRICING["test-paid-model"] = (Decimal("0.003"), Decimal("0.015"))
    try:
        # 단일 모델이면 계산된다.
        assert model_cost_usd(
            model_names=("test-paid-model",), input_tokens=1000, output_tokens=1000
        ) == Decimal("0.018")
        # 섞이면 거부한다(토큰이 모델별로 안 쪼개져 오므로).
        with pytest.raises(UnpricedModel, match="mixed_models_with_nonzero_price"):
            model_cost_usd(
                model_names=("test-paid-model", "qwen3:1.7b"),
                input_tokens=1000, output_tokens=1000,
            )
    finally:
        del MODEL_PRICING["test-paid-model"]


# ── 적지 않는 것 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status",
    [CapacityObservationStatus.UNAVAILABLE, CapacityObservationStatus.NO_WORKERS_REGISTERED],
)
def test_unmeasured_capacity_is_never_written_as_zero(status) -> None:
    """관측 실패·대상 없음을 0 으로 적으면 "측정했더니 한가하다"가 된다."""

    kw = dict(arrivals=None, duration_p95_ms=None, retry_rate=None,
              error_rate=None, utilization=None)
    if status is CapacityObservationStatus.UNAVAILABLE:
        kw["reason"] = "langfuse_trace_list_failed:Error:http_400"
    repo = _FakeRepo()
    outcome = write_observability_snapshots(
        _Observability(capacity=[_capacity(status=status, **kw)]), repo
    )

    assert repo.capacity == []
    assert outcome.capacity_written == 0
    assert outcome.skipped[0]["kind"] == "capacity"
    assert outcome.skipped[0]["reason"]


def test_worker_without_token_measurement_gets_no_cost_row() -> None:
    """arrivals > 0 이어도 토큰이 안 잡혔으면 0 토큰으로 적지 않는다.

    0 을 적으면 assess_budget 이 "사용량 0 = 예산 여유"로 읽는다. 안 적으면 같은
    함수가 UNKNOWN + INVESTIGATE_MISSING_DATA 로 정직하게 떨어진다.
    """

    repo = _FakeRepo(agents={"competing-explanation-worker": ("a-1", "v-1")})
    outcome = write_observability_snapshots(
        _Observability(worker_usage=[_usage(prompt=None, completion=None)]), repo
    )

    assert repo.cost == []
    assert outcome.cost_written == 0
    assert outcome.skipped[0]["reason"] == "no_token_measurement"


def test_worker_without_live_profile_version_is_skipped_not_guessed() -> None:
    """은퇴한 버전에 비용을 붙이면 그 버전의 예산 판정이 사후에 바뀐다."""

    repo = _FakeRepo(agents={})
    outcome = write_observability_snapshots(_Observability(worker_usage=[_usage()]), repo)

    assert repo.cost == []
    assert outcome.skipped[0]["reason"] == "agent_profile_or_live_version_missing"


def test_unpriced_model_is_skipped_with_a_reason_not_written_as_zero() -> None:
    repo = _FakeRepo(agents={"competing-explanation-worker": ("a-1", "v-1")})
    outcome = write_observability_snapshots(
        _Observability(worker_usage=[_usage(models=("claude-opus-5",))]), repo
    )

    assert repo.cost == []
    assert "unpriced_model" in outcome.skipped[0]["reason"]


def test_department_missing_from_db_is_skipped_with_its_code() -> None:
    repo = _FakeRepo(departments={})
    outcome = write_observability_snapshots(_Observability(capacity=[_capacity()]), repo)

    assert repo.capacity == []
    assert "research-department" in outcome.skipped[0]["reason"]


# ── 적는 것 ───────────────────────────────────────────────────────────────────


def test_measured_capacity_is_carried_over_without_recomputation() -> None:
    repo = _FakeRepo()
    outcome = write_observability_snapshots(_Observability(capacity=[_capacity()]), repo)

    assert outcome.capacity_written == 1
    written = repo.capacity[0]
    assert written.department_id == "dept-research-department"
    assert written.agent_id is None, "부서 단위 행이라 agent_id 는 비어야 한다"
    assert written.arrivals == 69
    assert written.duration_p95_ms == Decimal("1200.0")
    assert written.error_rate == Decimal("0.02")
    assert written.recorded_by == RECORDED_BY
    # queue_p95_ms 는 영구 부재다 - 0 으로 채우면 "대기 없음"으로 읽힌다.
    assert written.queue_p95_ms is None


def test_measured_worker_usage_becomes_a_zero_cost_row() -> None:
    repo = _FakeRepo(agents={"competing-explanation-worker": ("a-1", "v-1")})
    outcome = write_observability_snapshots(_Observability(worker_usage=[_usage()]), repo)

    assert outcome.cost_written == 1
    written = repo.cost[0]
    assert (written.agent_id, written.profile_version_id) == ("a-1", "v-1")
    assert written.input_tokens == 1732
    assert written.output_tokens == 183
    assert written.model_cost == Decimal(0), "자체 호스팅 qwen 은 0달러"
    assert written.tool_cost == Decimal(0) and written.infra_cost == Decimal(0)
    # 예산의 분모는 실행 건수지 모델 호출 수가 아니다.
    assert written.case_count == 67
    assert written.currency == "USD"
    assert written.recorded_by == RECORDED_BY


def test_dry_run_reports_without_writing() -> None:
    repo = _FakeRepo(agents={"competing-explanation-worker": ("a-1", "v-1")})
    outcome = write_observability_snapshots(
        _Observability(capacity=[_capacity()], worker_usage=[_usage()]), repo, dry_run=True
    )

    assert (outcome.capacity_written, outcome.cost_written) == (1, 1)
    assert repo.capacity == [] and repo.cost == []


def test_builders_refuse_unmeasured_reports_directly() -> None:
    """write 경로를 우회해 부르는 호출자도 같은 계약을 받는다."""

    with pytest.raises(ValueError):
        build_capacity_snapshot(
            _capacity(status=CapacityObservationStatus.NO_WORKERS_REGISTERED,
                      arrivals=None, duration_p95_ms=None, retry_rate=None,
                      error_rate=None, utilization=None),
            department_id="d-1",
        )
    with pytest.raises(ValueError):
        build_cost_snapshot(
            _usage(prompt=None, completion=None), agent_id="a-1", profile_version_id="v-1",
        )
