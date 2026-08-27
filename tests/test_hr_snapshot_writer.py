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

from observability import (
    CapacityObservationStatus,
    DepartmentCapacityReport,
    WorkerUsageObservationStatus,
    WorkerUsageReport,
)
from snapshot_writer import (
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
    base = {
        "department": department,
        "window_start": _START,
        "window_end": _END,
        "status": status,
        "arrivals": arrivals,
        "duration_p95_ms": 1200.0,
        "retry_rate": 0.0,
        "error_rate": 0.02,
        "utilization": 0.13,
    }
    base.update(kw)
    return DepartmentCapacityReport(**base)


def _usage(
    worker_id="competing-explanation-worker", department="research",
    status=WorkerUsageObservationStatus.MEASURED, prompt=1732, completion=183,
    models=("qwen2.5-14b-instruct-awq",), arrivals=67, **kw
) -> WorkerUsageReport:
    base = {
        "department": department,
        "worker_id": worker_id,
        "window_start": _START,
        "window_end": _END,
        "status": status,
        "arrivals": arrivals,
        "llm_calls": 70,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "model_names": tuple(models),
    }
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
    from observability import INVESTMENT_DEPARTMENT_STAGE

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

    kw = {
        "arrivals": None,
        "duration_p95_ms": None,
        "retry_rate": None,
        "error_rate": None,
        "utilization": None,
    }
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


# ── 관측 창: 겹치면 비용이 부풀어 오른다 ──────────────────────────────────────
#
# get_capacity_snapshot 은 창 안의 **1행**을 고르고, list_cost_snapshots_by_department
# 는 창 안의 행을 **전부 합산**한다(assess_budget). 그래서 "지금부터 24시간 전"
# 같은 이동 창으로 매번 적으면 24시간 Scorecard 질의가 그 행들을 다 더해 사용량이
# 실행 횟수만큼 부풀고 예산 판정이 뒤집힌다.


def test_window_is_aligned_and_covers_only_completed_buckets() -> None:
    from snapshot_writer import aligned_window

    start, end = aligned_window(now=datetime(2026, 8, 27, 12, 34, 56, tzinfo=timezone.utc))
    assert end == datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    assert start == datetime(2026, 8, 27, 11, 0, tzinfo=timezone.utc)

    # 진행 중인 시간대는 절대 안 적는다 - 부분 값이 "그 시간대 전부"로 읽히고
    # 다음 실행이 더 큰 값으로 덮어써서, 중간에 인용한 판단이 과소 집계가 된다.
    assert end <= datetime(2026, 8, 27, 12, 34, 56, tzinfo=timezone.utc)


def test_reruns_within_the_same_hour_target_the_identical_window() -> None:
    """멱등 갱신의 전제 - 창이 조금이라도 다르면 unique index 를 비껴가 새 행이 된다."""

    from snapshot_writer import aligned_window

    base = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    windows = {
        aligned_window(now=base + timedelta(minutes=m)) for m in (0, 7, 31, 59)
    }
    assert len(windows) == 1, windows


def test_consecutive_buckets_do_not_overlap() -> None:
    """비용은 창 안의 행을 합산한다 - 겹치면 그만큼 이중 계상된다."""

    from snapshot_writer import aligned_window

    base = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    first = aligned_window(now=base)
    second = aligned_window(now=base + timedelta(hours=1))
    assert first[1] == second[0], "버킷 사이에 틈이나 겹침이 있다"


def test_naive_datetime_is_rejected() -> None:
    from snapshot_writer import aligned_window

    with pytest.raises(ValueError, match="timezone-aware"):
        aligned_window(now=datetime.fromisoformat("2026-08-27T12:00:00"))


def test_run_once_observes_exactly_the_bucket_it_writes() -> None:
    """관측 창과 기록 창이 어긋나면 한 시간짜리 행에 다른 시간의 수치가 들어간다."""

    import snapshot_writer as sw

    captured: dict = {}

    def _fake_collect(*, now, lookback_hours, idle_threshold_hours):
        captured.update(now=now, lookback_hours=lookback_hours)
        return _Observability()

    original = sw.collect_workforce_observability
    sw.collect_workforce_observability = _fake_collect
    try:
        outcome = sw.run_once(
            repository=_FakeRepo(), now=datetime(2026, 8, 27, 12, 40, tzinfo=timezone.utc),
        )
    finally:
        sw.collect_workforce_observability = original

    assert captured["now"] == datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    assert captured["lookback_hours"] == 1.0
    assert outcome.window_start == datetime(2026, 8, 27, 11, tzinfo=timezone.utc)
    assert outcome.window_end == datetime(2026, 8, 27, 12, tzinfo=timezone.utc)


# ── 스케줄러 ─────────────────────────────────────────────────────────────────


def test_scheduler_survives_a_failing_cycle() -> None:
    """한 번 실패했다고 루프를 끝내면, 재시작 루프가 오히려 관측 공백을 만든다."""

    import snapshot_writer as sw

    calls = {"n": 0}

    def _boom(*, repository, window_hours, dry_run):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("langfuse down")
        return sw.WriteOutcome(capacity_written=1)

    original, sw.run_once = sw.run_once, _boom
    slept: list[float] = []
    try:
        # 두 번째 주기에서 멈추도록 sleep 이 한 번 불린 뒤 once 처럼 빠져나온다.
        def _sleep(seconds):
            slept.append(seconds)
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            sw.run_scheduler(
                repository=_FakeRepo(), health_path=Path(_tmp_health()),
                interval_seconds=5.0, sleep=_sleep,
            )
    finally:
        sw.run_once = original

    assert calls["n"] == 1, "첫 주기가 예외를 올려 루프가 끊겼다"
    assert slept == [5.0]


def test_heartbeat_is_written_even_when_nothing_was_recorded() -> None:
    """기록 0건(정상)과 writer 사망을 healthcheck 가 구분해야 한다."""

    import snapshot_writer as sw

    path = Path(_tmp_health())
    original, sw.run_once = sw.run_once, (
        lambda *, repository, window_hours, dry_run: sw.WriteOutcome()
    )
    try:
        assert sw.run_scheduler(repository=_FakeRepo(), health_path=path, once=True) == 0
    finally:
        sw.run_once = original

    assert path.exists()
    assert sw.healthcheck(path, interval_seconds=60.0) is True


def test_healthcheck_fails_when_the_heartbeat_goes_stale_or_missing() -> None:
    import snapshot_writer as sw

    missing = Path(_tmp_health())
    assert sw.healthcheck(missing) is False

    stale = Path(_tmp_health())
    stale.write_text(
        (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), encoding="utf-8"
    )
    assert sw.healthcheck(stale, interval_seconds=60.0) is False


def test_backfill_fills_oldest_first_and_paces_between_buckets() -> None:
    """버킷당 Langfuse 왕복 2회 - 분당 15 상한이라 연달아 쏘면 스스로 429 를 만든다."""

    import snapshot_writer as sw

    seen: list[datetime] = []
    original = sw.run_once

    def _record(*, repository, window_hours, now, dry_run):
        outcome = sw.WriteOutcome(capacity_written=1)
        outcome.window_end = sw.aligned_window(now=now, window_hours=window_hours)[1]
        seen.append(outcome.window_end)
        return outcome

    sw.run_once = _record
    slept: list[float] = []
    try:
        sw.run_backfill(
            repository=_FakeRepo(), buckets=3,
            now=datetime(2026, 8, 27, 12, 30, tzinfo=timezone.utc),
            pace_seconds=10.0, sleep=slept.append,
        )
    finally:
        sw.run_once = original

    assert seen == sorted(seen), "최신부터 채우면 중간에 멈췄을 때 과거가 빈다"
    assert seen == [
        datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 11, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 12, tzinfo=timezone.utc),
    ]
    assert slept == [10.0, 10.0], "버킷 사이 간격이 없다"


def _tmp_health() -> str:
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False, suffix=".health") as handle:
        path = handle.name
    Path(path).unlink(missing_ok=True)
    return path
