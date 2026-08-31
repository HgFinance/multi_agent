"""Langfuse 관측을 workforce Scorecard Snapshot 으로 옮기는 producer (2026-08-27).

왜 이게 필요한가
================
HR 은 같은 성격의 수치를 **두 출처**에서 본다.

    Langfuse 실행 이벤트  ─(관측)→  GET /workforce/v1/departments/observability
    workforce.*_snapshots ─(DB)  →  GET /workforce/v1/departments/scorecard(-brief)

`POST /workforce/v1/capacity-snapshots` 와 `POST .../cost-snapshots` 는 2026-08-25
에 생겼고, 이 writer가 10분 주기로 Langfuse 집계와 Scorecard Snapshot을 연결한다.
따라서 읽기 경로는 매번 외부 관측 API를 호출하지 않고 DB의 정시 버킷 Snapshot을
사용한다. 다만 원천에 토큰 사용량이 없거나 가격표가 없는 경우에는 비용을 0으로
만들지 않고 `UNKNOWN`으로 남긴다.

부수 효과가 하나 더 있다. Langfuse Public API 는 **분당 15 요청** 상한이므로
(실측: `x-ratelimit-limit: 15`, 초과 시 429 + `Retry-After` 최대 60초), 읽는 쪽이
Worker마다 API를 반복 호출하면 폴링이 쉽게 제한에 걸린다. 이 writer는 한 창에서
Worker당 실행 이벤트와 미발화 이벤트를 각각 최대 한 번만 읽어 Snapshot으로 남기고,
읽는 쪽(HR 과제·Operator 화면)은 DB를 우선 사용한다.

적지 않는 것
============
**관측 못 한 것을 0 으로 적지 않는다.** UNAVAILABLE(조회 실패)과
NO_WORKERS_REGISTERED(잴 대상 없음)는 건너뛴다 - 그 자리에 0 을 적는 순간
"측정했더니 한가하다"가 되고, 그 값으로 인원 조치가 결정된다.

**토큰이 안 잡힌 Worker 는 cost 를 적지 않는다.** arrivals > 0 이어도
prompt/completion 토큰이 전부 None 일 수 있다(begin_worker_metric 컨텍스트 밖
실행). 그때 0 토큰을 적으면 `assess_budget` 이 "사용량 0 = 예산 여유"로 읽는다.
안 적으면 그 함수가 `BudgetStatus.UNKNOWN` + `INVESTIGATE_MISSING_DATA` 로
정직하게 떨어진다(cost.py 불변식 3).

**값을 못 매기는 모델은 비용을 적지 않는다.** 아래 MODEL_PRICING 참고.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # api/app.py 와 같은 sys.path 규약
    sys.path.insert(0, str(_HERE))

from cost import CapacitySnapshot, CostSnapshot
from observability import (
    CapacityObservationStatus,
    DEPARTMENT_PROFILE_BY_KEY,
    DepartmentCapacityReport,
    WorkerUsageObservationStatus,
    WorkerUsageReport,
    WorkforceObservability,
    collect_workforce_observability,
)

# 이 producer 가 적은 행이라는 표식. 사람이 POST 로 직접 넣은 행과 구분된다 -
# 재보고 멱등성은 (subject, 창) 키가 보장하므로 이 값은 출처 표기 전용이다.
RECORDED_BY = "workforce-snapshot-writer/langfuse"


# ── 부서 키 다리 ──────────────────────────────────────────────────────────────
#
# 두 이름 공간이 실제로 다르다. 관측은 Langfuse stage 키(`research`)를 쓰고
# Scorecard 는 workforce.departments.department_code(`research-department`)를
# 쓴다. 이름 규칙(`+ "-department"`)으로 유도하지 않는다 - risk 는
# `risk-management`, qa 는 `qa-department` 라 규칙이 이미 두 번 깨진다. 표로 적고
# 어긋나면 즉시 실패하는 쪽이 조용히 빈 Snapshot 을 내는 것보다 낫다
# (app.py get_department_scorecard_brief 가 기본값을 거부하는 것과 같은 이유).
DEPARTMENT_CODE_BY_STAGE_KEY = DEPARTMENT_PROFILE_BY_KEY


class UnknownDepartmentKey(RuntimeError):
    """관측 부서 키를 DB department_code 로 못 옮겼다."""


def department_code_for(observation_key: str) -> str:
    try:
        return DEPARTMENT_CODE_BY_STAGE_KEY[observation_key]
    except KeyError as exc:
        raise UnknownDepartmentKey(
            f"unknown_department_key:{observation_key} - "
            "DEPARTMENT_CODE_BY_STAGE_KEY 에 추가해야 한다"
        ) from exc


# ── 모델 단가 ─────────────────────────────────────────────────────────────────
#
# 1K 토큰당 USD. **자체 호스팅 Qwen 은 0 이다** - 토큰당 과금이 있는 API 가 아니라
# 우리가 띄운 vLLM/Ollama 라, 토큰이 늘어도 청구서가 늘지 않는다. GPU 시간 비용은
# 실재하지만 그건 토큰이 아니라 가동 시간에 붙는 값이고, cost_snapshots 의
# model_cost(토큰 단가) 축이 아니라 infra_cost 축이다. 여기서 그 값을 지어내지
# 않는다(플랫폼이 보고하면 그때 별도 경로로 들어온다).
#
# ⚠ 모르는 모델은 **0 으로 접지 않는다**. 0 을 기본값으로 두면 나중에 누가 Worker
#   하나를 과금 모델로 바꿨을 때 그 비용이 조용히 0 으로 적히고, 예산 판정이
#   "여유"로 뒤집힌다(개발 원칙 9 - 실패는 확대가 아니라 차단 방향으로).
ZERO_COST_REASON = "self_hosted_no_per_token_billing"
MODEL_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    # model_name: (input USD/1K, output USD/1K)
    "qwen2.5-14b-instruct-awq": (Decimal(0), Decimal(0)),  # 운영 Qwen AWQ v1
    "qwen3:1.7b": (Decimal(0), Decimal(0)),                # 로컬 Ollama 개발 fallback
}


class UnpricedModel(RuntimeError):
    """단가표에 없는 모델이 관측됐다 - 비용을 0 으로 접지 않고 멈춘다."""


def model_cost_usd(
    *, model_names: Iterable[str], input_tokens: int, output_tokens: int
) -> Decimal:
    """관측된 모델들의 토큰 비용. 하나라도 단가를 모르면 UnpricedModel.

    같은 Worker 가 창 안에서 모델을 갈아탔을 수 있는데(운영 AWQ ↔ 개발 fallback)
    토큰은 모델별로 안 쪼개져 온다. 지금은 후보 전부가 0 단가라 어느 쪽에 붙여도
    같은 값이고, 그래서 **전부 0 일 때만** 합산이 성립한다. 0 이 아닌 단가가 표에
    들어오는 순간 이 함수는 모델별 토큰 분해를 요구해야 한다 - 그때 조용히 틀리지
    않도록 아래에서 명시적으로 막는다.
    """

    names = tuple(model_names)
    if not names:
        raise UnpricedModel("no_model_observed - 모델을 못 읽었으면 비용을 매기지 않는다")
    unknown = [n for n in names if n not in MODEL_PRICING]
    if unknown:
        raise UnpricedModel(f"unpriced_model:{','.join(sorted(unknown))}")
    rates = [MODEL_PRICING[n] for n in names]
    if any(rate != (Decimal(0), Decimal(0)) for rate in rates):
        if len(set(names)) > 1:
            raise UnpricedModel(
                f"mixed_models_with_nonzero_price:{','.join(sorted(set(names)))} - "
                "모델별 토큰 분해 없이 합산할 수 없다"
            )
        rate_in, rate_out = rates[0]
        return (
            Decimal(input_tokens) / Decimal(1000) * rate_in
            + Decimal(output_tokens) / Decimal(1000) * rate_out
        )
    return Decimal(0)


# ── 저장소 계약 ───────────────────────────────────────────────────────────────


class ScorecardWriteRepository(Protocol):
    """이 모듈이 쓰는 저장소 표면만 좁게 선언한다(테스트가 대역을 만들 수 있게)."""

    def get_department_id(self, department_code: str) -> str | None: ...

    def get_agent_cost_subject(self, employee_code: str) -> tuple[str, str] | None: ...

    def append_capacity_snapshot(self, snapshot: CapacitySnapshot) -> tuple[str, bool]: ...

    def append_cost_snapshot(self, snapshot: CostSnapshot) -> tuple[str, bool]: ...


@dataclass
class WriteOutcome:
    """무엇을 적었고 무엇을 왜 건너뛰었는지. 건너뜀은 실패가 아니라 관측 사실이다."""

    capacity_written: int = 0
    cost_written: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)
    # 어느 버킷을 적었는지. 로그만 보고 "지금 것"으로 오해하지 않게 같이 낸다.
    window_start: datetime | None = None
    window_end: datetime | None = None
    # HR Langfuse review is deliberately separate from Snapshot writes. A
    # Discord outage must not make the authoritative workforce snapshot fail.
    hr_langfuse_review: str = "NOT_ATTEMPTED"

    def skip(self, *, kind: str, subject: str, reason: str) -> None:
        self.skipped.append({"kind": kind, "subject": subject, "reason": reason})

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "capacity_written": self.capacity_written,
            "cost_written": self.cost_written,
            "skipped_count": len(self.skipped),
            "skipped": self.skipped,
            "hr_langfuse_review": self.hr_langfuse_review,
        }


def _decimal_or_none(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def build_capacity_snapshot(
    report: DepartmentCapacityReport, *, department_id: str
) -> CapacitySnapshot:
    """MEASURED 한 건을 그대로 옮긴다 - 여기서 재집계하지 않는다."""

    if report.status is not CapacityObservationStatus.MEASURED:
        raise ValueError(f"{report.status.value} 는 Snapshot 으로 적을 수 없다")
    return CapacitySnapshot(
        department_id=department_id,
        agent_id=None,
        window_start=report.window_start,
        window_end=report.window_end,
        # MEASURED 이면 arrivals 는 반드시 정수다(observability.py 계약).
        arrivals=int(report.arrivals or 0),
        # 영구 부재다 - publish_worker_activity 가 "작업 끝" 시점만 남기고 대기열
        # 진입 시점을 안 남긴다. 0 으로 채우면 "대기 없음"으로 읽힌다.
        queue_p95_ms=None,
        duration_p95_ms=_decimal_or_none(report.duration_p95_ms),
        retry_rate=_decimal_or_none(report.retry_rate),
        error_rate=_decimal_or_none(report.error_rate),
        utilization=_decimal_or_none(report.utilization),
        recorded_by=RECORDED_BY,
    )


def build_cost_snapshot(
    report: WorkerUsageReport, *, agent_id: str, profile_version_id: str
) -> CostSnapshot:
    """Worker 한 명의 토큰 관측을 cost_snapshots 한 행으로 옮긴다."""

    if report.status is not WorkerUsageObservationStatus.MEASURED:
        raise ValueError(f"{report.status.value} 는 Snapshot 으로 적을 수 없다")
    if report.prompt_tokens is None and report.completion_tokens is None:
        raise ValueError("토큰이 하나도 안 잡힌 창은 0 으로 적지 않는다")
    input_tokens = int(report.prompt_tokens or 0)
    output_tokens = int(report.completion_tokens or 0)
    return CostSnapshot(
        agent_id=agent_id,
        profile_version_id=profile_version_id,
        window_start=report.window_start,
        window_end=report.window_end,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model_cost=model_cost_usd(
            model_names=report.model_names,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
        # 관측에 없는 축이다. 도구·인프라 비용을 여기서 지어내지 않는다 -
        # 플랫폼이 보고하면 POST .../cost-snapshots 로 따로 들어온다.
        tool_cost=Decimal(0),
        infra_cost=Decimal(0),
        # 실행 건수. llm_calls(모델 호출 수)와 다르다 - 한 실행이 모델을 여러 번
        # 부를 수 있어서, 예산의 분모는 실행 건수 쪽이다.
        case_count=int(report.arrivals or 0),
        currency="USD",
        recorded_by=RECORDED_BY,
    )


def write_observability_snapshots(
    observability: WorkforceObservability,
    repository: ScorecardWriteRepository,
    *,
    dry_run: bool = False,
) -> WriteOutcome:
    """관측 한 창을 Snapshot 두 종류로 옮긴다. 관측 못 한 것은 건너뛴다."""

    outcome = WriteOutcome()

    for report in observability.capacity:
        subject = report.department
        if report.status is not CapacityObservationStatus.MEASURED:
            outcome.skip(
                kind="capacity", subject=subject,
                reason=report.reason or report.status.value,
            )
            continue
        department_id = repository.get_department_id(department_code_for(subject))
        if department_id is None:
            outcome.skip(
                kind="capacity", subject=subject,
                reason=f"department_not_registered:{department_code_for(subject)}",
            )
            continue
        if not dry_run:
            repository.append_capacity_snapshot(
                build_capacity_snapshot(report, department_id=department_id)
            )
        outcome.capacity_written += 1

    for usage in observability.worker_usage:
        subject = f"{usage.department}/{usage.worker_id}"
        if usage.status is not WorkerUsageObservationStatus.MEASURED:
            outcome.skip(
                kind="cost", subject=subject,
                reason=usage.reason or usage.status.value,
            )
            continue
        if usage.prompt_tokens is None and usage.completion_tokens is None:
            # 0 으로 적으면 "예산 여유"로 읽힌다 - 없는 채로 두면 assess_budget 이
            # UNKNOWN + INVESTIGATE_MISSING_DATA 로 정직하게 떨어진다.
            outcome.skip(kind="cost", subject=subject, reason="no_token_measurement")
            continue
        cost_subject = repository.get_agent_cost_subject(usage.worker_id)
        if cost_subject is None:
            outcome.skip(
                kind="cost", subject=subject,
                reason="agent_profile_or_live_version_missing",
            )
            continue
        agent_id, profile_version_id = cost_subject
        try:
            snapshot = build_cost_snapshot(
                usage, agent_id=agent_id, profile_version_id=profile_version_id
            )
        except UnpricedModel as exc:
            outcome.skip(kind="cost", subject=subject, reason=str(exc))
            continue
        if not dry_run:
            repository.append_cost_snapshot(snapshot)
        outcome.cost_written += 1

    return outcome


# ── 관측 창 ───────────────────────────────────────────────────────────────────
#
# ⚠ 창은 **겹치면 안 된다.** 두 리더의 집계 방식이 다르기 때문이다:
#
#   get_capacity_snapshot()            창 안에서 window_end 가 가장 늦은 행 **1개**
#   list_cost_snapshots_by_department() 창 안의 행을 **전부 합산** (assess_budget)
#
# 그래서 "지금부터 24시간 전" 같은 **이동 창**으로 매번 적으면, 실행할 때마다
# window 가 달라 새 행이 되고, 24시간 Scorecard 질의가 그 행들을 전부 더한다 -
# 사용량이 실행 횟수만큼 부풀어 예산 판정이 뒤집힌다(append_cost_snapshot 머리말의
# "재보고가 새 행이 되면 사용량이 조용히 두 배가 된다"와 같은 사고를, 재보고가
# 아니라 창 설계로 일으키는 경우다).
#
# 그래서 **정시에 정렬된 고정 길이 버킷**만 적는다. 같은 시간대에 몇 번을 다시
# 돌려도 창이 글자 그대로 같아서 행이 늘지 않고 갱신되고(unique index), 서로 다른
# 버킷은 겹치지 않아 합산이 정확하다.
DEFAULT_WINDOW_HOURS = 1


def aligned_window(*, now: datetime, window_hours: int = DEFAULT_WINDOW_HOURS) -> tuple[datetime, datetime]:
    """`now` 직전의 **완료된** 버킷 하나. 부분 버킷은 절대 적지 않는다.

    진행 중인 시간대를 적으면 그 값이 "그 시간대의 전부"로 읽히고, 다음 실행이
    같은 창에 더 큰 값을 덮어쓴다 - 중간에 그 값을 인용한 판단은 과소 집계를
    본 것이 된다. 끝난 버킷만 적으면 한 번 적힌 값이 변하지 않는다.
    """

    if window_hours <= 0:
        raise ValueError("window_hours 는 양수여야 한다")
    if now.tzinfo is None:
        raise ValueError("now 는 timezone-aware 여야 한다 - 창 경계가 흔들린다")
    floored = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    # window_hours 배수 경계로 내린다(1이면 매시 정각).
    floored = floored.replace(hour=(floored.hour // window_hours) * window_hours)
    return floored - timedelta(hours=window_hours), floored


def run_once(
    *,
    repository: ScorecardWriteRepository,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    now: datetime | None = None,
    dry_run: bool = False,
    observability: WorkforceObservability | None = None,
) -> WriteOutcome:
    """완료된 버킷 하나를 관측해 Snapshot 으로 적는다. 스케줄러가 부르는 진입점.

    같은 버킷을 다시 돌리는 것은 안전하다(멱등 갱신). 그래서 실행 주기를 버킷
    길이보다 짧게 잡아도 되고, 그 편이 컨테이너 재시작에 강하다.
    """

    window_start, window_end = aligned_window(
        now=now or datetime.now(timezone.utc), window_hours=window_hours
    )
    observed = observability or collect_workforce_observability(
        # now/lookback 조합으로 창을 **정확히 그 버킷**에 맞춘다.
        now=window_end,
        lookback_hours=float(window_hours),
        # 유휴 임계는 이 경로와 무관하다(Snapshot 두 종류는 유휴 판정을 안 쓴다).
        idle_threshold_hours=4.0,
    )
    outcome = write_observability_snapshots(observed, repository, dry_run=dry_run)
    outcome.window_start = window_start
    outcome.window_end = window_end
    try:
        from orchestration.hr_langfuse_feedback import publish_hr_langfuse_review

        outcome.hr_langfuse_review = publish_hr_langfuse_review(
            observed,
            latency_warn_ms=int(
                os.getenv("LANGSMITH_FEEDBACK_LATENCY_WARN_MS", "60000")
            ),
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - Discord review is fail-open
        LOGGER.warning(
            "HR Langfuse 검토 카드 연결 실패 - Snapshot 기록은 유지한다: %s",
            type(exc).__name__,
        )
        outcome.hr_langfuse_review = "FAILED"
    return outcome


def run_backfill(
    *,
    repository: ScorecardWriteRepository,
    buckets: int,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    now: datetime | None = None,
    dry_run: bool = False,
    pace_seconds: float = 10.0,
    sleep: Any = time.sleep,
) -> list[WriteOutcome]:
    """직전 N 개 버킷을 순서대로 메운다. 컨테이너가 죽어 있던 구간용.

    ▶ 버킷 하나당 Langfuse 왕복 2회인데 Public API 는 분당 15 요청 상한이라
      (429 의 x-ratelimit-limit), 연달아 쏘면 스스로 한도를 넘긴다. 버킷 사이에
      간격을 둔다 - 백필은 급한 작업이 아니고, 여기서 429 를 맞으면 SDK 가
      최대 60초를 자서 오히려 더 느려진다.
    """

    if buckets <= 0:
        raise ValueError("buckets 는 양수여야 한다")
    anchor = now or datetime.now(timezone.utc)
    outcomes: list[WriteOutcome] = []
    for index in range(buckets):
        # 오래된 버킷부터 채운다 - 중간에 멈춰도 최신 구간이 비는 편이 낫다
        # (최신은 다음 정기 실행이 곧 다시 적는다).
        offset = timedelta(hours=window_hours * (buckets - 1 - index))
        outcomes.append(
            run_once(
                repository=repository, window_hours=window_hours,
                now=anchor - offset, dry_run=dry_run,
            )
        )
        if index < buckets - 1 and pace_seconds > 0:
            sleep(pace_seconds)
    return outcomes


# ── 스케줄러 ─────────────────────────────────────────────────────────────────

LOGGER = logging.getLogger("workforce.snapshot_writer")
DEFAULT_HEALTH_PATH = "/tmp/workforce-snapshot-writer.health"
# 버킷 길이(1시간)보다 짧게 돈다. 같은 버킷 재보고는 멱등 갱신이라 손해가 없고,
# 컨테이너가 잠깐 죽었다 살아나도 그 버킷을 놓치지 않는다.
DEFAULT_INTERVAL_SECONDS = 600.0
# 이 파일이 이보다 오래 안 바뀌면 healthcheck 가 실패한다. 주기의 3배 - 한 번
# 걸러 뛰는 것으로 컨테이너를 재시작시키지 않는다.
HEALTH_STALE_MULTIPLIER = 3


def _heartbeat(path: Path) -> None:
    """살아 있다는 사실만 남긴다 - 성공/실패는 로그가 들고 있다.

    ▶ 기록 0건도 heartbeat 는 찍는다. Worker 가 한 시간 동안 아무것도 안 한
      정상 상태(arrivals=0)와 writer 가 죽은 상태를 healthcheck 가 구분해야
      한다 - 전자로 컨테이너를 재시작하면 진짜 장애만 가려진다.
    """

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("heartbeat 기록 실패: %s", exc)


def healthcheck(path: Path, *, interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> bool:
    try:
        stamp = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if stamp.tzinfo is None:
        return False
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    return age <= interval_seconds * HEALTH_STALE_MULTIPLIER


def run_scheduler(
    *,
    repository: ScorecardWriteRepository,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    health_path: Path = Path(DEFAULT_HEALTH_PATH),
    dry_run: bool = False,
    once: bool = False,
    sleep: Any = time.sleep,
) -> int:
    """완료된 버킷을 주기적으로 적는다.

    ▶ 한 번의 실패로 루프를 끝내지 않는다. Langfuse 가 잠깐 죽거나 DB 가 끊겨도
      다음 주기에 같은 버킷을 다시 적으면 그만이다(멱등 갱신) - 여기서 예외를
      올려 컨테이너가 재시작하면, 재시작 루프가 오히려 관측 공백을 만든다.
    """

    while True:
        try:
            outcome = run_once(
                repository=repository, window_hours=window_hours, dry_run=dry_run
            )
            LOGGER.info("snapshot %s", json.dumps(outcome.as_dict(), ensure_ascii=False))
        except Exception:
            LOGGER.exception("snapshot 기록 실패 - 다음 주기에 다시 시도한다")
        _heartbeat(health_path)
        if once:
            return 0
        sleep(interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Langfuse 관측을 workforce capacity/cost Snapshot 으로 기록한다.",
    )
    parser.add_argument(
        "--window-hours", type=int, default=DEFAULT_WINDOW_HOURS,
        help="정시 정렬 버킷 길이(시간). 겹치지 않는 창만 적는다",
    )
    parser.add_argument(
        "--interval-seconds", type=float,
        default=_env_float("WORKFORCE_SNAPSHOT_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS),
    )
    parser.add_argument(
        "--once", action="store_true",
        help="한 버킷만 적고 끝낸다(기본은 주기 실행)",
    )
    parser.add_argument(
        "--backfill-hours", type=int, default=0,
        help="직전 N 개 버킷을 메우고 끝낸다. 컨테이너가 죽어 있던 구간용",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="무엇을 적을지만 출력하고 DB 에 쓰지 않는다",
    )
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument(
        "--health-path",
        default=os.getenv("WORKFORCE_SNAPSHOT_HEALTH_PATH", DEFAULT_HEALTH_PATH),
    )
    args = parser.parse_args(argv)

    health_path = Path(args.health_path)
    if args.healthcheck:
        return 0 if healthcheck(health_path, interval_seconds=args.interval_seconds) else 1

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    dsn = (os.getenv("GOVERNANCE_WORKFORCE_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        print("DATABASE_URL 미설정 - Snapshot 기록 불가", file=sys.stderr)
        return 2

    from postgres_scorecard_repository import (
        PostgresScorecardRepository,
    )

    repository = PostgresScorecardRepository.connect(dsn)
    try:
        if args.backfill_hours:
            outcomes = run_backfill(
                repository=repository, buckets=args.backfill_hours,
                window_hours=args.window_hours, dry_run=args.dry_run,
            )
            print(json.dumps([o.as_dict() for o in outcomes], ensure_ascii=False, indent=2))
            return 0 if any(o.capacity_written or o.cost_written for o in outcomes) else 1
        if args.once:
            outcome = run_once(
                repository=repository, window_hours=args.window_hours, dry_run=args.dry_run,
            )
            print(json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2))
            # 한 버킷도 못 적었으면 성공으로 끝내지 않는다 - cron 이 조용히 도는 것을 막는다.
            return 0 if (outcome.capacity_written or outcome.cost_written) else 1
        return run_scheduler(
            repository=repository, window_hours=args.window_hours,
            interval_seconds=args.interval_seconds, health_path=health_path,
            dry_run=args.dry_run,
        )
    finally:
        repository.close()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


__all__ = [
    "DEPARTMENT_CODE_BY_STAGE_KEY",
    "MODEL_PRICING",
    "RECORDED_BY",
    "ScorecardWriteRepository",
    "UnknownDepartmentKey",
    "UnpricedModel",
    "WriteOutcome",
    "aligned_window",
    "build_capacity_snapshot",
    "build_cost_snapshot",
    "department_code_for",
    "healthcheck",
    "main",
    "model_cost_usd",
    "run_backfill",
    "run_once",
    "run_scheduler",
    "write_observability_snapshots",
]


if __name__ == "__main__":
    raise SystemExit(main())
