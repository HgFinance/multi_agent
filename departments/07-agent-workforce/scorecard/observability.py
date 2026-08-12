#!/usr/bin/env python3
"""2026-08-10 신규: Langfuse 원격 관측을 읽어 6개 투자본부 Worker의 유휴 여부를 판정.

소유: 영주 (Agent Workforce 인사팀)
근거: .env.example 3-2절("Langfuse Tracing — HR 유휴 Agent 관측용"), 2026-08-02 방향
      결정("HR 유휴 Agent 리포팅 파이프라인" — 이 모듈이 그 실제 파이프라인이다)

quality.py/cost.py와 같은 이유로 여기에도 LLM이 없다. "이 Worker가 최근에 실행됐는가"는
타임스탬프 비교이지 판단이 아니다 — LLM을 쓰면 정확한 시각 비교를 부정확한 서술로
바꾸는 꼴이다(CLAUDE.md: 결정론 함수가 정답을 만들 수 있는 태스크면 LLM을 쓰지 않는다).

## 원문을 읽지 않는다 — 이 모듈의 가장 중요한 제약

.env.example 3-2절이 이미 못 박아뒀다: "HR 은 이 Trace 를 원문으로 받지 않는다.
compliance-policy-agent Trace 에는 Mandate/제한종목 질의응답 같은 Risk/Compliance
내용이 그대로 담긴다." 이 모듈은 Langfuse 조회 시 절대 input/output 필드를 읽지
않고, timestamp 하나만 본다. TraceWithDetails.metadata 도 읽지 않는다 — metadata
안의 eval_score 등은 QA 소유 판정이라 여기서 복제하면 원본과 어긋날 수 있다.

## 부서 키가 두 개인 이유

- orchestration/employee_dispatch.py 의 EMPLOYEE_MODULE_BY_DEPARTMENT 키:
  research/trading/risk/quant-backtest/accounting-portfolio/qa (Worker 코드 로드용)
- orchestration/workflows/portfolio_recommendation.py 가 실제로 Langfuse 이벤트에
  써넣는 stage 값: research/quant/trading/risk/qa/accounting (DEPARTMENTS 튜플)
이 둘이 다른 이름 공간이라 INVESTMENT_DEPARTMENT_STAGE 로 명시 매핑한다. 같다고
가정하고 문자열을 그대로 재사용하면 quant-backtest/accounting-portfolio 부서가
조용히 매 조회에서 0건으로 빠진다.

불변식:
  1. idle_threshold_hours 는 양수여야 한다.
  2. Langfuse 비활성/조회 실패는 IDLE 이 아니라 UNAVAILABLE 이다 — "쉬고 있다"와
     "우리가 모른다"를 구분한다(quality.py aggregate_quality() 의 None/0 구분과 동일 원칙).
  3. trigger 가 always 가 아닌 Worker가 이 창(lookback_hours) 안에 한 번도 안 잡히면
     IDLE 이 아니라 UNOBSERVED 다 — 조건이 안 켜졌을 뿐 결함이 아닐 수 있어서다.

자체 점검: python departments/07-agent-workforce/scorecard/observability.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

try:
    from orchestration.employee_dispatch import load_worker_specs
    from orchestration.llm_observability import langfuse_worker_event_name
except ModuleNotFoundError:  # direct department-local execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from orchestration.employee_dispatch import load_worker_specs
    from orchestration.llm_observability import langfuse_worker_event_name

ROOT = Path(__file__).resolve().parents[3]

# employee_dispatch 의 로더 키 -> portfolio_recommendation 이 이벤트 name 에 쓰는 stage 값.
# 위 모듈 docstring "부서 키가 두 개인 이유" 참고.
INVESTMENT_DEPARTMENT_STAGE: dict[str, str] = {
    "research": "research",
    "trading": "trading",
    "risk": "risk",
    "quant-backtest": "quant",
    "accounting-portfolio": "accounting",
    "qa": "qa",
}


class IdleStatus(str, Enum):
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    # lookback_hours 안에 한 번도 관측되지 않음 - conditional Worker의 trigger가
    # 아직 안 켜졌을 수 있어 "결함"으로 단정하지 않는다.
    UNOBSERVED = "UNOBSERVED"
    # Langfuse 가 꺼져 있거나 조회 자체가 실패함 - "쉬고 있다"가 아니라 "모른다".
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class WorkerIdleReport:
    """한 Worker의 유휴 판정 한 건. HR API 응답의 원소 하나에 대응."""

    department: str
    worker_id: str
    trigger: str
    status: IdleStatus
    last_seen_at: datetime | None
    idle_hours: float | None

    def __post_init__(self) -> None:
        if self.status is IdleStatus.ACTIVE and self.last_seen_at is None:
            raise ValueError("ACTIVE 판정은 last_seen_at 없이 나올 수 없다")
        if self.status in (IdleStatus.UNOBSERVED, IdleStatus.UNAVAILABLE) and self.last_seen_at is not None:
            raise ValueError(f"{self.status.value} 판정은 last_seen_at 이 있으면 안 된다")

    def as_dict(self) -> dict[str, Any]:
        return {
            "department": self.department,
            "worker_id": self.worker_id,
            "trigger": self.trigger,
            "status": self.status.value,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "idle_hours": float(self.idle_hours) if self.idle_hours is not None else None,
        }


class LangfuseQueryError(RuntimeError):
    """Langfuse 조회 자체가 실패함 (자격증명·네트워크·API 응답 이상)."""


class LangfuseTraceReader:
    """조회 전용 인터페이스. read 측이라 create_event 계열은 갖지 않는다."""

    def latest_event_timestamp(self, *, event_name: str, since: datetime) -> datetime | None:
        """event_name 을 가진 가장 최근 이벤트의 timestamp. 없으면 None."""

        raise NotImplementedError


class LangfuseApiTraceReader(LangfuseTraceReader):
    """실제 Langfuse API 조회 구현. LANGFUSE_* 자격증명이 있을 때만 만든다."""

    def __init__(self) -> None:
        try:
            from langfuse import Langfuse
        except ModuleNotFoundError as exc:
            # requirements.txt 의 langfuse 는 선택적 의존성이다(orchestration/
            # llm_observability.py 와 동일 lazy-import 원칙) - 미설치도 UNAVAILABLE
            # 로 접히는 실패이지, ImportError 로 파이프라인을 죽이는 실패가 아니다.
            raise LangfuseQueryError("langfuse_not_installed") from exc

        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
        if not public_key or not secret_key:
            raise LangfuseQueryError("langfuse_credentials_missing")
        self._client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com",
        )

    def latest_event_timestamp(self, *, event_name: str, since: datetime) -> datetime | None:
        try:
            # order_by 문자열 문법이 langfuse 버전별로 갈릴 수 있어 서버 정렬에
            # 기대지 않는다 - limit 으로 조회량을 lookback 창 안으로 제한한 뒤
            # 클라이언트에서 max() 로 가장 최근 것만 뽑는다.
            page = self._client.api.trace.list(
                name=event_name,
                from_timestamp=since,
                limit=50,
            )
        except Exception as exc:  # noqa: BLE001 - 조회 실패는 항상 UNAVAILABLE 로 접힌다.
            raise LangfuseQueryError(f"langfuse_trace_list_failed:{type(exc).__name__}") from exc
        timestamps = [item.timestamp for item in page.data if item.timestamp is not None]
        return max(timestamps) if timestamps else None


def check_idle_agents(
    *,
    reader: LangfuseTraceReader | None = None,
    departments: tuple[str, ...] = tuple(INVESTMENT_DEPARTMENT_STAGE),
    lookback_hours: float = 24.0,
    idle_threshold_hours: float = 4.0,
    now: datetime | None = None,
    repo_root: Path = ROOT,
) -> list[WorkerIdleReport]:
    """6개 투자본부(기본값)의 등록된 Worker 전원에 대해 유휴 여부를 판정한다.

    reader 가 None 이면 실제 LangfuseApiTraceReader 생성을 시도한다 - 자격증명이
    없거나 langfuse 미설치면 그 시점에 LangfuseQueryError 가 나고, 이 함수는 그걸
    잡아 전원 UNAVAILABLE 로 접는다(개발 원칙 9: 실패는 확대가 아니라 차단 방향).
    """

    if idle_threshold_hours <= 0:
        raise ValueError("idle_threshold_hours 는 양수여야 한다")

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=lookback_hours)

    if reader is None:
        try:
            reader = LangfuseApiTraceReader()
        except LangfuseQueryError:
            reader = None

    reports: list[WorkerIdleReport] = []
    for department in departments:
        stage = INVESTMENT_DEPARTMENT_STAGE.get(department)
        if stage is None:
            raise ValueError(f"unknown_investment_department:{department}")
        specs = load_worker_specs(repo_root, department)
        for spec in specs:
            if reader is None:
                reports.append(
                    WorkerIdleReport(
                        department=department,
                        worker_id=spec.worker_id,
                        trigger=spec.trigger,
                        status=IdleStatus.UNAVAILABLE,
                        last_seen_at=None,
                        idle_hours=None,
                    )
                )
                continue
            event_name = langfuse_worker_event_name(stage=stage, worker_id=spec.worker_id)
            try:
                last_seen = reader.latest_event_timestamp(event_name=event_name, since=since)
            except LangfuseQueryError:
                reports.append(
                    WorkerIdleReport(
                        department=department,
                        worker_id=spec.worker_id,
                        trigger=spec.trigger,
                        status=IdleStatus.UNAVAILABLE,
                        last_seen_at=None,
                        idle_hours=None,
                    )
                )
                continue
            if last_seen is None:
                reports.append(
                    WorkerIdleReport(
                        department=department,
                        worker_id=spec.worker_id,
                        trigger=spec.trigger,
                        status=IdleStatus.UNOBSERVED,
                        last_seen_at=None,
                        idle_hours=None,
                    )
                )
                continue
            idle_hours = (now - last_seen).total_seconds() / 3600.0
            status = IdleStatus.ACTIVE if idle_hours <= idle_threshold_hours else IdleStatus.IDLE
            reports.append(
                WorkerIdleReport(
                    department=department,
                    worker_id=spec.worker_id,
                    trigger=spec.trigger,
                    status=status,
                    last_seen_at=last_seen,
                    idle_hours=idle_hours,
                )
            )
    return reports


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/scorecard/observability.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("ok - import 확인 (langfuse lazy load)")

    class _FakeReader(LangfuseTraceReader):
        """왕복 없이 판정 로직만 검증하는 대역."""

        def __init__(self, fixed: dict[str, datetime]) -> None:
            self._fixed = fixed

        def latest_event_timestamp(self, *, event_name: str, since: datetime) -> datetime | None:
            return self._fixed.get(event_name)

    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    active_name = langfuse_worker_event_name(stage="research", worker_id="research-data-worker")
    idle_name = langfuse_worker_event_name(stage="research", worker_id="microstructure-worker")
    reader = _FakeReader(
        {
            active_name: now - timedelta(hours=1),
            idle_name: now - timedelta(hours=48),
        }
    )
    reports = check_idle_agents(
        reader=reader,
        departments=("research",),
        idle_threshold_hours=4.0,
        now=now,
    )
    by_id = {r.worker_id: r for r in reports}
    assert by_id["research-data-worker"].status is IdleStatus.ACTIVE, by_id["research-data-worker"]
    assert by_id["microstructure-worker"].status is IdleStatus.IDLE, by_id["microstructure-worker"]
    unobserved = [r for r in reports if r.worker_id not in ("research-data-worker", "microstructure-worker")]
    assert unobserved and all(r.status is IdleStatus.UNOBSERVED for r in unobserved), unobserved
    print(f"  ACTIVE/IDLE/UNOBSERVED 판정 - OK ({len(reports)}개 Worker)")

    # reader=None 이고 자격증명도 없으면 전원 UNAVAILABLE - "쉬고 있다"로 오판하지 않는다.
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    unavailable_reports = check_idle_agents(departments=("qa",), now=now)
    assert unavailable_reports and all(r.status is IdleStatus.UNAVAILABLE for r in unavailable_reports)
    print(f"  자격증명 없음 -> 전원 UNAVAILABLE - OK ({len(unavailable_reports)}개 Worker)")

    print("본부 6개 유휴 판정 자체 점검 통과.")
