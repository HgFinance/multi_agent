"""Langfuse 관측을 workforce Scorecard Snapshot 으로 옮기는 producer (2026-08-27).

왜 이게 필요한가
================
HR 은 같은 성격의 수치를 **두 출처**에서 본다.

    Langfuse 실행 이벤트  ─(관측)→  GET /workforce/v1/departments/observability
    workforce.*_snapshots ─(DB)  →  GET /workforce/v1/departments/scorecard(-brief)

`POST /workforce/v1/capacity-snapshots` 와 `POST .../cost-snapshots` 는 2026-08-25
에 생겼지만 **그 엔드포인트를 부르는 쪽이 없었다.** 그래서 두 테이블이 계속 비어
있었고, Scorecard 브리프의 처리량·비용은 영구히 `NO_SNAPSHOT` 이었다. 정작 같은
수치(arrivals/duration_p95/error_rate/retry_rate/tokens)는 Langfuse 쪽에 이미
있었다 - 두 출처가 안 이어져 있었을 뿐이다. 이 모듈이 그 다리다.

부수 효과가 하나 더 있다. Langfuse Public API 는 **분당 15 요청** 상한이고
(실측: `x-ratelimit-limit: 15`, 초과 시 429 + `Retry-After` 최대 60초), 관측
1회는 Worker 8명 × 2 = **16 요청**이라 매번 정확히 한 건이 429 를 맞는다. 그래서
읽는 쪽(HR 과제·Operator 화면)이 실시간으로 Langfuse 를 때리는 구조 자체가 지속
불가능하다. 이 writer 가 주기적으로 한 번만 관측해 DB 에 남기면, 읽는 쪽은 DB 만
본다.

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
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Protocol

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # api/app.py 와 같은 sys.path 규약
    sys.path.insert(0, str(_HERE))

from cost import CapacitySnapshot, CostSnapshot  # noqa: E402
from observability import (  # noqa: E402
    CapacityObservationStatus,
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
DEPARTMENT_CODE_BY_STAGE_KEY: dict[str, str] = {
    "research": "research-department",
    "trading": "trading-department",
    "risk": "risk-management",
    "quant-backtest": "quant-backtest-department",
    "accounting-portfolio": "accounting-portfolio-department",
    "qa": "qa-department",
}


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

    def skip(self, *, kind: str, subject: str, reason: str) -> None:
        self.skipped.append({"kind": kind, "subject": subject, "reason": reason})

    def as_dict(self) -> dict[str, Any]:
        return {
            "capacity_written": self.capacity_written,
            "cost_written": self.cost_written,
            "skipped_count": len(self.skipped),
            "skipped": self.skipped,
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


def run_once(
    *,
    repository: ScorecardWriteRepository,
    lookback_hours: float = 24.0,
    dry_run: bool = False,
    observability: WorkforceObservability | None = None,
) -> WriteOutcome:
    """관측 1회 → Snapshot 기록 1회. cron/스케줄러가 부르는 진입점."""

    observed = observability or collect_workforce_observability(
        lookback_hours=lookback_hours,
        # 유휴 임계는 이 경로와 무관하다(Snapshot 두 종류는 유휴 판정을 안 쓴다).
        # 기본값을 그대로 두어 관측 창이 화면 쪽과 같게 유지된다.
        idle_threshold_hours=4.0,
    )
    return write_observability_snapshots(observed, repository, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Langfuse 관측을 workforce capacity/cost Snapshot 으로 기록한다.",
    )
    parser.add_argument("--lookback-hours", type=float, default=24.0)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="무엇을 적을지만 출력하고 DB 에 쓰지 않는다",
    )
    args = parser.parse_args(argv)

    dsn = (os.getenv("GOVERNANCE_WORKFORCE_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        print("DATABASE_URL 미설정 - Snapshot 기록 불가", file=sys.stderr)
        return 2

    from postgres_scorecard_repository import PostgresScorecardRepository  # noqa: PLC0415

    repository = PostgresScorecardRepository.connect(dsn)
    try:
        outcome = run_once(
            repository=repository,
            lookback_hours=args.lookback_hours,
            dry_run=args.dry_run,
        )
    finally:
        repository.close()
    print(json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2))
    # 하나도 못 적었으면 성공으로 끝내지 않는다 - cron 이 조용히 도는 것을 막는다.
    return 0 if (outcome.capacity_written or outcome.cost_written) else 1


__all__ = [
    "DEPARTMENT_CODE_BY_STAGE_KEY",
    "MODEL_PRICING",
    "RECORDED_BY",
    "ScorecardWriteRepository",
    "UnknownDepartmentKey",
    "UnpricedModel",
    "WriteOutcome",
    "build_capacity_snapshot",
    "build_cost_snapshot",
    "department_code_for",
    "main",
    "model_cost_usd",
    "run_once",
    "write_observability_snapshots",
]


if __name__ == "__main__":
    raise SystemExit(main())
