"""HR 유휴 Agent 판정 계약 테스트 (2026-08-10 Langfuse 도입).

departments/07-agent-workforce/scorecard/observability.py 의 __main__ 자체 점검이
기본 판정 로직을 이미 검증하지만, 여기서는 CI 에서 항상 돌아야 하는 경계 조건
(자격증명 없음, 조회 실패, 부서 키 불일치)을 pytest 로 고정한다.
"""

from __future__ import annotations

import builtins
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


class _EmptyReader(LangfuseTraceReader):
    def latest_event_timestamp(self, *, event_name: str, since: datetime) -> datetime | None:
        return None


def a_worker_of(department: str) -> str:
    """그 부서에 **실제로 등록된** 워커 id 하나.

    ▶ 워커 id 를 테스트에 박아두지 않는다 (2026-08-11 실측).
      `research-data-worker`·`strategy-hypothesis-worker` 를 박아뒀는데 그 사이
      워커가 개편돼(`holdings-analyst-worker` 등) 세 테스트가 KeyError 로 죽었다.
      판정 로직은 멀쩡했고 이름만 낡은 것인데, 그 실패가 스위트에 섞여 **진짜
      회귀와 구분되지 않았다.** 이름은 레지스트리에서 받아온다.
    """
    reports = check_idle_agents(reader=_EmptyReader(), departments=(department,), now=_NOW)
    assert reports, f"{department} 에 등록된 워커가 없다 - 이 테스트가 무의미해진다"
    return sorted(r.worker_id for r in reports)[0]


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
    worker_id = a_worker_of("research")
    name = langfuse_worker_event_name(stage="research", worker_id=worker_id)
    reader = _FixedReader({name: _NOW - timedelta(hours=1)})
    reports = check_idle_agents(reader=reader, departments=("research",), idle_threshold_hours=4.0, now=_NOW)
    by_id = {r.worker_id: r for r in reports}
    assert by_id[worker_id].status is IdleStatus.ACTIVE
    assert by_id[worker_id].idle_hours == pytest.approx(1.0)


def test_stale_trace_is_idle_not_unobserved() -> None:
    worker_id = a_worker_of("research")
    name = langfuse_worker_event_name(stage="research", worker_id=worker_id)
    reader = _FixedReader({name: _NOW - timedelta(hours=48)})
    reports = check_idle_agents(reader=reader, departments=("research",), idle_threshold_hours=4.0, now=_NOW)
    by_id = {r.worker_id: r for r in reports}
    assert by_id[worker_id].status is IdleStatus.IDLE


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

    worker_id = a_worker_of("quant-backtest")
    correct_name = langfuse_worker_event_name(stage="quant", worker_id=worker_id)
    wrong_name = langfuse_worker_event_name(stage="quant-backtest", worker_id=worker_id)
    assert correct_name != wrong_name

    reader = _FixedReader({correct_name: _NOW - timedelta(hours=1)})
    reports = check_idle_agents(
        reader=reader, departments=("quant-backtest",), idle_threshold_hours=4.0, now=_NOW
    )
    by_id = {r.worker_id: r for r in reports}
    assert by_id[worker_id].status is IdleStatus.ACTIVE


# ---------------------------------------------------------------------------
# 2026-08-20: Profile(데이터) 레지스트리 전환분 계약 테스트
#
# HR 은 더 이상 남의 본부 employee_workers.py 를 import 하지 않고 hermes/config.yaml
# 의 workers 만 읽는다. 그 전환이 성립하려면 두 가지가 계속 참이어야 하고, 둘 다
# 깨져도 **예외 없이 조용히** 틀린 답이 나오는 종류라 여기서 고정한다.
# ---------------------------------------------------------------------------


def test_profile_registry_matches_python_worker_specs() -> None:
    """YAML(정본)과 실행 모듈 WORKER_SPECS 가 같은 편제를 말해야 한다.

    어긋나면 HR 은 존재하지 않는 워커를 영원히 UNOBSERVED 로 보고하거나(YAML 에만
    있음), 실제로 도는 워커를 아예 못 본다(모듈에만 있음). 어느 쪽도 조회 실패가
    아니라서 UNAVAILABLE 로도 안 잡힌다.
    """

    from observability import DEPARTMENT_PROFILE_DIR, load_worker_profile_specs
    from orchestration.employee_dispatch import load_worker_specs

    for department in DEPARTMENT_PROFILE_DIR:
        from_profile = {(s.worker_id, s.trigger) for s in load_worker_profile_specs(ROOT, department)}
        from_module = {(s.worker_id, s.trigger) for s in load_worker_specs(ROOT, department)}
        assert from_profile == from_module, f"{department} 편제가 Profile 과 코드에서 다르다"


def test_fallback_event_name_matches_canonical() -> None:
    """orchestration 이 없는 컨테이너용 복제 구현이 정본과 같은 문자열을 만들어야 한다.

    이전 fallback 은 `worker.{stage}.{worker_id}` 라는 다른 포맷이었다 - 조회가
    예외 없이 0건이 되므로 UNOBSERVED 로 위장된다(2026-08-20 수정).
    """

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_hr_observability_isolated",
        ROOT / "departments" / "07-agent-workforce" / "scorecard" / "observability.py",
    )
    module = importlib.util.module_from_spec(spec)
    real_import = builtins.__import__

    def _no_orchestration(name, *args, **kwargs):
        if name.startswith("orchestration"):
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _no_orchestration
    # @dataclass 가 실행 중 sys.modules 에서 자기 모듈을 되찾으므로 먼저 등록한다.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        builtins.__import__ = real_import
        sys.modules.pop(spec.name, None)

    for stage, worker_id in (("research", "holdings-analyst-worker"), ("quant", "x-worker")):
        assert module.langfuse_worker_event_name(
            stage=stage, worker_id=worker_id
        ) == langfuse_worker_event_name(stage=stage, worker_id=worker_id)


def test_mounted_profile_root_is_used_when_repo_tree_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """컨테이너 배선 검증 - 저장소 트리 없이 마운트 경로만으로 읽혀야 한다."""

    from observability import PROFILE_MOUNT_ROOT_ENV, load_worker_profile_specs

    mount = tmp_path / "profiles"
    (mount / "risk").mkdir(parents=True)
    (mount / "risk" / "config.yaml").write_text(
        "workers:\n  compliance-policy-worker:\n    trigger: when_compliance_evidence_exists\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(PROFILE_MOUNT_ROOT_ENV, str(mount))
    specs = load_worker_profile_specs(tmp_path / "no-such-repo", "risk")
    assert [(s.worker_id, s.trigger) for s in specs] == [
        ("compliance-policy-worker", "when_compliance_evidence_exists")
    ]


def test_missing_profile_is_unavailable_not_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Profile 을 못 읽으면 빈 목록(=유휴 없음)이 아니라 명시적 실패여야 한다."""

    from observability import PROFILE_MOUNT_ROOT_ENV, WorkerRegistryUnavailable, load_worker_profile_specs

    monkeypatch.setenv(PROFILE_MOUNT_ROOT_ENV, str(tmp_path / "empty"))
    with pytest.raises(WorkerRegistryUnavailable):
        load_worker_profile_specs(tmp_path / "no-such-repo", "research")


def test_department_without_llm_workers_is_empty_not_broken() -> None:
    """트레이딩은 LLM 직원 0명이 정상이다 - 결함(키 없음)과 구분돼야 한다."""

    from observability import load_worker_profile_specs

    assert load_worker_profile_specs(ROOT, "trading") == ()
