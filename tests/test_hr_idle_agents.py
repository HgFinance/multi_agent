"""HR 유휴 Agent 판정 계약 테스트 (2026-08-10 Langfuse 도입).

departments/07-agent-workforce/scorecard/observability.py 의 __main__ 자체 점검이
기본 판정 로직을 이미 검증하지만, 여기서는 CI 에서 항상 돌아야 하는 경계 조건
(자격증명 없음, 조회 실패, 부서 키 불일치)을 pytest 로 고정한다.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "departments" / "07-agent-workforce" / "scorecard"))

from observability import (  # noqa: E402
    INVESTMENT_DEPARTMENT_STAGE,
    IdleStatus,
    LangfuseQueryError,
    LangfuseTraceReader,
    WorkerIdleReport,
    check_idle_agents,
)
from orchestration.llm_observability import langfuse_worker_event_name  # noqa: E402

_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


class _FixedReader(LangfuseTraceReader):
    def __init__(self, fixed: dict[str, datetime]) -> None:
        self._fixed = fixed

    def latest_event_timestamp(self, *, event_name: str, since: datetime) -> datetime | None:
        return self._fixed.get(event_name)


class _FailingReader(LangfuseTraceReader):
    def latest_event_timestamp(self, *, event_name: str, since: datetime) -> datetime | None:
        raise LangfuseQueryError("simulated_query_failure")


def test_worker_covers_all_six_investment_departments() -> None:
    """직접 dispatch 키 스펠링을 몰라도 이 상수만 보면 범위를 알 수 있어야 한다."""

    assert set(INVESTMENT_DEPARTMENT_STAGE) == {
        "research",
        "trading",
        "risk",
        "quant-backtest",
        "accounting-portfolio",
        "qa",
    }


def test_recent_trace_is_active() -> None:
    name = langfuse_worker_event_name(stage="research", worker_id="research-data-worker")
    reader = _FixedReader({name: _NOW - timedelta(hours=1)})
    reports = check_idle_agents(reader=reader, departments=("research",), idle_threshold_hours=4.0, now=_NOW)
    by_id = {r.worker_id: r for r in reports}
    assert by_id["research-data-worker"].status is IdleStatus.ACTIVE
    assert by_id["research-data-worker"].idle_hours == pytest.approx(1.0)


def test_stale_trace_is_idle_not_unobserved() -> None:
    name = langfuse_worker_event_name(stage="research", worker_id="research-data-worker")
    reader = _FixedReader({name: _NOW - timedelta(hours=48)})
    reports = check_idle_agents(reader=reader, departments=("research",), idle_threshold_hours=4.0, now=_NOW)
    by_id = {r.worker_id: r for r in reports}
    assert by_id["research-data-worker"].status is IdleStatus.IDLE


def test_never_seen_worker_is_unobserved_not_idle() -> None:
    """UNOBSERVED != IDLE - conditional Worker의 trigger 가 안 켜졌을 뿐일 수 있다."""

    reader = _FixedReader({})
    reports = check_idle_agents(reader=reader, departments=("research",), now=_NOW)
    assert all(r.status is IdleStatus.UNOBSERVED for r in reports)
    assert all(r.last_seen_at is None and r.idle_hours is None for r in reports)


def test_query_failure_degrades_to_unavailable_not_idle() -> None:
    """조회 자체가 실패하면 '쉬고 있다'가 아니라 '모른다'로 접혀야 한다."""

    reports = check_idle_agents(reader=_FailingReader(), departments=("qa",), now=_NOW)
    assert reports
    assert all(r.status is IdleStatus.UNAVAILABLE for r in reports)
    assert all(r.last_seen_at is None for r in reports)


def test_no_reader_and_no_credentials_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    reports = check_idle_agents(departments=("qa",), now=_NOW)
    assert reports
    assert all(r.status is IdleStatus.UNAVAILABLE for r in reports)


def test_idle_threshold_must_be_positive() -> None:
    with pytest.raises(ValueError):
        check_idle_agents(reader=_FixedReader({}), idle_threshold_hours=0, now=_NOW)
    with pytest.raises(ValueError):
        check_idle_agents(reader=_FixedReader({}), idle_threshold_hours=-1, now=_NOW)


def test_unknown_department_key_raises_instead_of_silently_skipping() -> None:
    with pytest.raises(ValueError):
        check_idle_agents(reader=_FixedReader({}), departments=("not-a-real-department",), now=_NOW)


def test_active_report_requires_last_seen_at() -> None:
    with pytest.raises(ValueError):
        WorkerIdleReport(
            department="research",
            worker_id="w",
            trigger="always",
            status=IdleStatus.ACTIVE,
            last_seen_at=None,
            idle_hours=None,
        )


def test_unobserved_report_rejects_last_seen_at() -> None:
    with pytest.raises(ValueError):
        WorkerIdleReport(
            department="research",
            worker_id="w",
            trigger="always",
            status=IdleStatus.UNOBSERVED,
            last_seen_at=_NOW,
            idle_hours=0.0,
        )


def test_department_stage_mapping_disambiguates_dispatch_key_from_event_name() -> None:
    """quant-backtest(dispatch 키) 워커가 quant(event stage) 이름으로 조회돼야 한다 -
    두 이름 공간이 같다고 착각하면 이 부서가 매 조회에서 조용히 0건이 된다."""

    worker_id = "strategy-hypothesis-worker"
    correct_name = langfuse_worker_event_name(stage="quant", worker_id=worker_id)
    wrong_name = langfuse_worker_event_name(stage="quant-backtest", worker_id=worker_id)
    assert correct_name != wrong_name

    reader = _FixedReader({correct_name: _NOW - timedelta(hours=1)})
    reports = check_idle_agents(
        reader=reader, departments=("quant-backtest",), idle_threshold_hours=4.0, now=_NOW
    )
    by_id = {r.worker_id: r for r in reports}
    assert by_id[worker_id].status is IdleStatus.ACTIVE
