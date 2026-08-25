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

⚠ 경계 확장(2026-08-24, Capacity): `list_worker_activity()`는 metadata 중
`latency_ms`/`error_count`/`retries` **셋만** 추가로 읽는다. 이 셋은 QA 판정이
아니라 이 이벤트를 쓴 실행기 자신의 값이다(`_metric_metadata()` 허용 목록 —
`orchestration/llm_observability.py`). eval_score·input·output은 여전히 절대
읽지 않는다 — 그 경계는 그대로다.

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

# The HR observer consumes only the versioned, metadata-only Worker Registry.
# It never imports another department runtime Python module.
# The manifest contains only department, worker_id, and trigger metadata.
# A missing or invalid manifest raises WorkerRegistryUnavailable
# rather than returning an empty list.
# Langfuse payloads remain timestamp-only and never include trace contents.

try:
    from orchestration.llm_observability import (
        langfuse_worker_event_name,
        langfuse_worker_opportunity_event_name,
    )
except ModuleNotFoundError:  # 배포된 workforce-api 이미지에는 orchestration 이 없다.

    def langfuse_worker_event_name(*, stage: str, worker_id: str) -> str:
        """write 측과 **같은** 문자열을 만들어야 한다.

        이벤트 이름은 부서 코드가 아니라 write/read 사이의 wire contract 라서,
        import 할 수 없는 런타임에서는 복제하고 계약 테스트로 묶는다
        (tests/test_hr_idle_agents.py::test_fallback_event_name_matches_canonical).
        포맷이 어긋나면 조회가 예외 없이 **조용히 0건**이 되므로 - 이전 fallback 이
        `worker.{stage}.{worker_id}` 라는 다른 포맷을 만들고 있었다(2026-08-20 수정) -
        이 대조를 테스트로 고정하는 것이 이 복제의 전제다.
        """

        return f"llm.performance.metric:{stage}:{worker_id}"

    def langfuse_worker_opportunity_event_name(*, stage: str, worker_id: str) -> str:
        """langfuse_worker_event_name 의 fallback 과 같은 이유 - 발화율 조회가
        import 할 수 없는 런타임에서도 wire contract 를 놓치지 않게 복제한다."""

        return f"llm.performance.opportunity:{stage}:{worker_id}"


class WorkerRegistryUnavailable(RuntimeError):
    """부서 Worker registry 를 이 런타임에서 읽을 수 없다(유휴 판정 불가)."""


class HeadProfilesUnavailable(WorkerRegistryUnavailable):
    """부서장 신원만 못 읽었다 - Worker 목록 자체는 멀쩡하다(2026-08-20).

    둘을 같은 예외로 던지면 호출부가 구분할 방법이 문자열 매칭뿐이라, 부서장을
    못 읽었다는 이유로 **Worker 리포트까지 통째로 실패**한다(실측: 매니페스트
    전환으로 Profile 이 이 컨테이너에서 사라지자 --include-heads 가 그렇게 됐다).

    WorkerRegistryUnavailable 를 상속하는 이유: 부서장을 필수로 요구하는 호출부는
    기존처럼 잡으면 되고, Worker 만으로 진행할 수 있는 호출부만 이 타입을 따로
    잡으면 된다. 새로 생긴 실패가 조용히 무시되지는 않는다.
    """

ROOT = Path(__file__).resolve().parents[3]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from orchestration.contracts.worker_registry import (
        WorkerRegistryError,
        load_worker_registry,
        workers_for_department,
    )
except ModuleNotFoundError as _exc:
    load_worker_registry = None  # type: ignore[assignment]
    workers_for_department = None  # type: ignore[assignment]
    _WORKER_REGISTRY_IMPORT_ERROR: str | None = f"{type(_exc).__name__}:{_exc}"
    class WorkerRegistryError(RuntimeError):
        """Fallback type used when the common contract was not packaged."""
else:
    _WORKER_REGISTRY_IMPORT_ERROR = None



# Worker Registry department key -> portfolio_recommendation event stage value.
# 위 모듈 docstring "부서 키가 두 개인 이유" 참고.
INVESTMENT_DEPARTMENT_STAGE: dict[str, str] = {
    "research": "research",
    "trading": "trading",
    "risk": "risk",
    "quant-backtest": "quant",
    "accounting-portfolio": "accounting",
    "qa": "qa",
}


def _safe_int(value: Any) -> int | None:
    """metadata 값을 int 로 바꾼다 - 형이 안 맞으면(None 포함) None."""

    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _percentile(values: list[float], fraction: float) -> float | None:
    """values 의 fraction 분위수(예: 0.95 -> p95). values 가 비면 None."""

    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


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


@dataclass(frozen=True)
class WorkerActivityRecord:
    """실행 이벤트 한 건에서 Capacity 계산에 필요한 필드만 뽑는다.

    latency_ms/error_count/retries는 이 이벤트를 쓴 실행기 자신의 값이다
    (publish_worker_activity() 참고) - QA 소유 eval_score 나 input/output 원문은
    여기 없다(위 "원문을 읽지 않는다" 절 참고).
    """

    timestamp: datetime
    latency_ms: int | None
    error_count: int | None
    retries: int | None


class LangfuseTraceReader:
    """조회 전용 인터페이스. read 측이라 create_event 계열은 갖지 않는다."""

    def latest_event_timestamp(self, *, event_name: str, since: datetime) -> datetime | None:
        """event_name 을 가진 가장 최근 이벤트의 timestamp. 없으면 None."""

        raise NotImplementedError

    def list_worker_activity(
        self, *, event_name: str, since: datetime, limit: int = 200
    ) -> list[WorkerActivityRecord]:
        """event_name 을 가진 최근 이벤트들의 latency_ms/error_count/retries.

        Capacity(용량) 집계 전용이다 - 유휴 판정(latest_event_timestamp)과 달리
        여러 건을 모아 arrivals/p95/rate를 계산해야 해서 별도 메서드로 둔다.
        """

        raise NotImplementedError

    def count_events(self, *, event_name: str, since: datetime, limit: int = 200) -> int:
        """event_name 을 가진 이벤트 개수 (limit 초과분은 세지 않는다).

        발화율(fire_rate) 집계 전용이다 - 실행/미발화 이벤트 각각의 건수만
        필요하고 timestamp・metadata 는 안 쓴다. list_worker_activity 처럼 원문을
        읽지 않는다(위 "원문을 읽지 않는다" 절과 동일).
        """

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

    def list_worker_activity(
        self, *, event_name: str, since: datetime, limit: int = 200
    ) -> list[WorkerActivityRecord]:
        try:
            page = self._client.api.trace.list(name=event_name, from_timestamp=since, limit=limit)
        except Exception as exc:  # noqa: BLE001 - 조회 실패는 항상 UNAVAILABLE 로 접힌다.
            raise LangfuseQueryError(f"langfuse_trace_list_failed:{type(exc).__name__}") from exc
        records: list[WorkerActivityRecord] = []
        for item in page.data:
            if item.timestamp is None:
                continue
            metadata = item.metadata if isinstance(item.metadata, dict) else {}
            records.append(
                WorkerActivityRecord(
                    timestamp=item.timestamp,
                    latency_ms=_safe_int(metadata.get("latency_ms")),
                    error_count=_safe_int(metadata.get("error_count")),
                    retries=_safe_int(metadata.get("retries")),
                )
            )
        return records

    def count_events(self, *, event_name: str, since: datetime, limit: int = 200) -> int:
        try:
            page = self._client.api.trace.list(name=event_name, from_timestamp=since, limit=limit)
        except Exception as exc:  # noqa: BLE001 - 조회 실패는 항상 UNAVAILABLE 로 접힌다.
            raise LangfuseQueryError(f"langfuse_trace_list_failed:{type(exc).__name__}") from exc
        return len(page.data)


# ── 부서장(Hermes Profile) ────────────────────────────────────────────────────
#
# ▶ Worker Registry 매니페스트(orchestration/contracts/worker_registry.v1.json)는
#   **Worker 만** 담는다(schema 가 department/worker_id/trigger 세 키로 고정).
#   부서장은 직원이 아니라 본부장이라 그 목록에 없고, 편제표(LLM Worker 10명)와도
#   별개다. 그래서 신원은 부서 Profile 의 `agent.head_persona` 에서 읽는다 -
#   write 측(apps/api/hermes_boundary.py)이 이벤트 이름을 만들 때 읽는 **같은
#   파일의 같은 키**다. 두 쪽이 다른 출처를 보면 조용히 어긋난다.
#
#   ⚠ 이것만 매니페스트 경계 밖이다. 부서장을 매니페스트 v2 에 넣을지는 미결이고
#     (그 계약은 리서치 소유), 그때까지 include_heads 는 opt-in 으로 둔다.
DEPARTMENT_PROFILE_DIR: dict[str, str] = {
    "research": "01-research",
    "trading": "02-trading",
    "risk": "03-risk",
    "quant-backtest": "04-quant-backtest",
    "accounting-portfolio": "05-accounting-portfolio",
    "qa": "06-ai-qa-audit",
}

# 컨테이너에는 저장소 트리가 없고 Profile 만 read-only 로 마운트된다
# (departments/07-agent-workforce/compose.yaml).
PROFILE_MOUNT_ROOT_ENV = "WORKFORCE_PROFILE_ROOT"
DEFAULT_PROFILE_MOUNT_ROOT = Path("/app/profiles")


@dataclass(frozen=True)
class HeadProfileSpec:
    """부서장 1명. WorkerMetadata 와 같은 속성 이름을 쓴다 - 판정 루프가 둘을
    구분하지 않고 그대로 돌 수 있어야 한다."""

    worker_id: str
    # 부서장은 "요청이 올 때" 돈다. conditional Worker 의 trigger 자리에 그 사실을
    # 적어 리포트가 그대로 읽히게 한다.
    trigger: str = "on_request"


def load_head_profile_spec(repo_root: Path, department: str) -> HeadProfileSpec | None:
    """부서 Profile 의 `agent.head_persona`. 없으면 None, 못 읽으면 예외."""

    directory = DEPARTMENT_PROFILE_DIR.get(department)
    if directory is None:
        raise ValueError(f"unknown_investment_department:{department}")
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - 이미지 빌드 결함
        raise HeadProfilesUnavailable(f"pyyaml_not_installed:{exc}") from exc

    mount_root = Path(os.environ.get(PROFILE_MOUNT_ROOT_ENV) or DEFAULT_PROFILE_MOUNT_ROOT)
    candidates = (
        repo_root / "departments" / directory / "hermes" / "config.yaml",
        mount_root / department / "config.yaml",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001 - 깨진 Profile 도 "모른다" 로 접힌다
            raise HeadProfilesUnavailable(
                f"profile_unreadable:{department}:{type(exc).__name__}"
            ) from exc
        persona = str((config.get("agent") or {}).get("head_persona") or "").strip()
        return HeadProfileSpec(persona) if persona else None
    # Worker 목록은 매니페스트에서 이미 받았다 - 부서장만 못 읽은 것을 빈 목록으로
    # 위장하지 않는다("유휴 없음"이 아니라 "모른다").
    raise HeadProfilesUnavailable(f"head_profile_not_found:{department}")


def check_idle_agents(
    *,
    reader: LangfuseTraceReader | None = None,
    departments: tuple[str, ...] = tuple(INVESTMENT_DEPARTMENT_STAGE),
    lookback_hours: float = 24.0,
    idle_threshold_hours: float = 4.0,
    now: datetime | None = None,
    repo_root: Path = ROOT,
    include_heads: bool = False,
) -> list[WorkerIdleReport]:
    """6개 투자본부(기본값)의 등록된 Worker 전원에 대해 유휴 여부를 판정한다.

    reader 가 None 이면 실제 LangfuseApiTraceReader 생성을 시도한다 - 자격증명이
    없거나 langfuse 미설치면 그 시점에 LangfuseQueryError 가 나고, 이 함수는 그걸
    잡아 전원 UNAVAILABLE 로 접는다(개발 원칙 9: 실패는 확대가 아니라 차단 방향).
    """

    if idle_threshold_hours <= 0:
        raise ValueError("idle_threshold_hours 는 양수여야 한다")

    for department in departments:
        if department not in INVESTMENT_DEPARTMENT_STAGE:
            raise ValueError(f"unknown_investment_department:{department}")
    if load_worker_registry is None or workers_for_department is None:
        raise WorkerRegistryUnavailable(
            f"worker_registry_unavailable:{_WORKER_REGISTRY_IMPORT_ERROR}"
        )
    try:
        registry = load_worker_registry(repo_root)
    except WorkerRegistryError as exc:
        raise WorkerRegistryUnavailable(f"worker_registry_unavailable:{exc}") from exc

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
        specs: tuple[Any, ...] = tuple(workers_for_department(registry, department))
        if include_heads:
            # 기본값에서 빠져 있다 - 기본 응답 인원이 말없이 늘면 이 리포트를 인용한
            # 과거 문장의 뜻이 바뀐다(load_head_profile_spec 머리말 참고).
            head = load_head_profile_spec(repo_root, department)
            if head is not None:
                specs = (head, *specs)
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


class CapacityObservationStatus(str, Enum):
    MEASURED = "MEASURED"
    # Langfuse 가 꺼져 있거나 조회 자체가 실패함 - IdleStatus.UNAVAILABLE 과 같은
    # 이유. "부하가 0이다"(측정됨)와 "모른다"(측정 실패)를 섞지 않는다.
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class DepartmentCapacityReport:
    """부서 하나의 Langfuse 기반 Capacity 관측 한 건.

    `workforce.capacity_snapshots` writer가 아직 없어(P1-2 미착수) DB 기반
    `GET .../scorecard`의 capacity 필드가 항상 null이다 - 이 리포트가 그 빈
    자리를 Langfuse 직접 집계로 메운다(idle-agents 와 같은 원리). DB Snapshot과
    스키마가 다르므로 `cost.py`의 `CapacitySnapshot`으로 강제하지 않는다 -
    출처가 다른 두 값을 같은 타입으로 섞으면 어느 쪽 계약을 따르는지 흐려진다.

    department 등록 Worker 전원을 합산한 값이라 여러 Worker가 겹쳐 돌면
    utilization 이 1.0을 넘을 수 있다 - 단일 서버 가동률이 아니라 "부서 총
    작업시간 / 관측 시간" 비율이라서다. queue_p95_ms 는 영구적으로 없다 -
    지금 계측(publish_worker_activity)은 "작업이 끝났다" 시점 이벤트 하나뿐이고
    "작업이 도착했다"(대기열 진입) 시점을 별도로 남기지 않는다.
    """

    department: str
    window_start: datetime
    window_end: datetime
    status: CapacityObservationStatus
    arrivals: int | None
    duration_p95_ms: float | None
    retry_rate: float | None
    error_rate: float | None
    utilization: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "department": self.department,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "status": self.status.value,
            "arrivals": self.arrivals,
            "duration_p95_ms": self.duration_p95_ms,
            "retry_rate": self.retry_rate,
            "error_rate": self.error_rate,
            "utilization": self.utilization,
            "queue_p95_ms": None,
        }


def compute_department_capacity(
    *,
    department: str,
    reader: LangfuseTraceReader | None,
    since: datetime,
    now: datetime,
    repo_root: Path,
) -> DepartmentCapacityReport:
    """department 등록 Worker 전원의 실행 이벤트를 모아 Capacity 하나로 합친다."""

    stage = INVESTMENT_DEPARTMENT_STAGE.get(department)
    if stage is None:
        raise ValueError(f"unknown_investment_department:{department}")
    if load_worker_registry is None or workers_for_department is None:
        raise WorkerRegistryUnavailable(
            f"worker_registry_unavailable:{_WORKER_REGISTRY_IMPORT_ERROR}"
        )
    try:
        registry = load_worker_registry(repo_root)
    except WorkerRegistryError as exc:
        raise WorkerRegistryUnavailable(f"worker_registry_unavailable:{exc}") from exc

    def _unavailable() -> DepartmentCapacityReport:
        return DepartmentCapacityReport(
            department=department, window_start=since, window_end=now,
            status=CapacityObservationStatus.UNAVAILABLE,
            arrivals=None, duration_p95_ms=None, retry_rate=None, error_rate=None,
            utilization=None,
        )

    if reader is None:
        return _unavailable()

    specs = tuple(workers_for_department(registry, department))
    records: list[WorkerActivityRecord] = []
    try:
        for spec in specs:
            event_name = langfuse_worker_event_name(stage=stage, worker_id=spec.worker_id)
            records.extend(reader.list_worker_activity(event_name=event_name, since=since))
    except LangfuseQueryError:
        return _unavailable()

    arrivals = len(records)
    if arrivals == 0:
        return DepartmentCapacityReport(
            department=department, window_start=since, window_end=now,
            status=CapacityObservationStatus.MEASURED,
            arrivals=0, duration_p95_ms=None, retry_rate=None, error_rate=None,
            utilization=None,
        )

    latencies = [float(r.latency_ms) for r in records if r.latency_ms is not None]
    errors = [r.error_count for r in records if r.error_count is not None]
    retries = [r.retries for r in records if r.retries is not None]
    window_ms = max((now - since).total_seconds() * 1000.0, 1.0)

    return DepartmentCapacityReport(
        department=department, window_start=since, window_end=now,
        status=CapacityObservationStatus.MEASURED,
        arrivals=arrivals,
        duration_p95_ms=_percentile(latencies, 0.95) if latencies else None,
        error_rate=(sum(errors) / arrivals) if errors else None,
        retry_rate=(sum(retries) / arrivals) if retries else None,
        utilization=(sum(latencies) / window_ms) if latencies else None,
    )


def check_department_capacity(
    *,
    reader: LangfuseTraceReader | None = None,
    departments: tuple[str, ...] = tuple(INVESTMENT_DEPARTMENT_STAGE),
    lookback_hours: float = 24.0,
    now: datetime | None = None,
    repo_root: Path = ROOT,
) -> list[DepartmentCapacityReport]:
    """6개 투자본부(기본값) 전체의 Capacity 를 부서 단위로 하나씩 돌려준다.

    check_idle_agents() 와 같은 실패 모드다 - reader 를 못 만들거나 조회가 실패하면
    UNAVAILABLE 로 접고, arrivals=0(측정됐지만 실행이 없었다)과 구분한다.
    """

    if lookback_hours <= 0:
        raise ValueError("lookback_hours 는 양수여야 한다")
    for department in departments:
        if department not in INVESTMENT_DEPARTMENT_STAGE:
            raise ValueError(f"unknown_investment_department:{department}")

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=lookback_hours)

    if reader is None:
        try:
            reader = LangfuseApiTraceReader()
        except LangfuseQueryError:
            reader = None

    return [
        compute_department_capacity(
            department=department, reader=reader, since=since, now=now, repo_root=repo_root,
        )
        for department in departments
    ]


# ── 발화율(fire rate) ─────────────────────────────────────────────────────────
#
# check_idle_agents()에 합치지 않는 이유: idle-agents 는 "가장 최근 실행이
# 언제였나"(단일 timestamp)만 본다. 발화율은 "이 창 안에서 실행/미발화가 몇
# 건씩이었나"(카운트 둘)가 필요해 조회 모양이 다르다 - compute_department_capacity
# 가 idle 판정과 별도 함수로 분리된 것과 같은 이유.


class TriggerRateObservationStatus(str, Enum):
    MEASURED = "MEASURED"
    # Langfuse 가 꺼져 있거나 조회 자체가 실패함 - CapacityObservationStatus.
    # UNAVAILABLE 과 같은 이유.
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class WorkerTriggerRateReport:
    """Worker 한 명의 발화율 관측 한 건.

    fire_rate 가 None 인 것과 0.0 인 것은 다른 사실이다 - None 은 "이 창 안에
    기회 자체가 없었다"(분모 0, cost.py 불변식 3과 같은 원칙), 0.0 은 "기회가
    있었는데 한 번도 안 켜졌다"(분모 > 0, 분자 0)다. 지금까지 이 둘은 idle-agents
    쪽에서 똑같이 UNOBSERVED 로 보였다 - 여기서 분리해 낸다.
    """

    department: str
    worker_id: str
    trigger: str
    window_start: datetime
    window_end: datetime
    status: TriggerRateObservationStatus
    execution_count: int | None
    opportunity_count: int | None
    fire_rate: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "department": self.department,
            "worker_id": self.worker_id,
            "trigger": self.trigger,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "status": self.status.value,
            "execution_count": self.execution_count,
            "opportunity_count": self.opportunity_count,
            "fire_rate": self.fire_rate,
        }


def compute_worker_trigger_rate(
    *, department: str, stage: str, spec: Any, reader: LangfuseTraceReader | None,
    since: datetime, now: datetime,
) -> WorkerTriggerRateReport:
    """Worker 한 명의 실행/미발화 이벤트를 세어 발화율 하나로 합친다."""

    if reader is None:
        return WorkerTriggerRateReport(
            department=department, worker_id=spec.worker_id, trigger=spec.trigger,
            window_start=since, window_end=now,
            status=TriggerRateObservationStatus.UNAVAILABLE,
            execution_count=None, opportunity_count=None, fire_rate=None,
        )

    execution_name = langfuse_worker_event_name(stage=stage, worker_id=spec.worker_id)
    opportunity_name = langfuse_worker_opportunity_event_name(stage=stage, worker_id=spec.worker_id)
    try:
        execution_count = reader.count_events(event_name=execution_name, since=since)
        opportunity_count = reader.count_events(event_name=opportunity_name, since=since)
    except LangfuseQueryError:
        return WorkerTriggerRateReport(
            department=department, worker_id=spec.worker_id, trigger=spec.trigger,
            window_start=since, window_end=now,
            status=TriggerRateObservationStatus.UNAVAILABLE,
            execution_count=None, opportunity_count=None, fire_rate=None,
        )

    denominator = execution_count + opportunity_count
    return WorkerTriggerRateReport(
        department=department, worker_id=spec.worker_id, trigger=spec.trigger,
        window_start=since, window_end=now,
        status=TriggerRateObservationStatus.MEASURED,
        execution_count=execution_count, opportunity_count=opportunity_count,
        # 불변식 - 분모 0(이 창에 기회가 전혀 없었다)은 0.0이 아니라 None이다.
        fire_rate=(execution_count / denominator) if denominator > 0 else None,
    )


def check_worker_trigger_rates(
    *,
    reader: LangfuseTraceReader | None = None,
    departments: tuple[str, ...] = tuple(INVESTMENT_DEPARTMENT_STAGE),
    lookback_hours: float = 24.0,
    now: datetime | None = None,
    repo_root: Path = ROOT,
) -> list[WorkerTriggerRateReport]:
    """6개 투자본부(기본값)의 등록된 Worker 전원에 대해 발화율을 계산한다.

    check_idle_agents()와 같은 실패 모드다 - reader를 못 만들거나 조회가
    실패하면 UNAVAILABLE로 접는다.
    """

    if lookback_hours <= 0:
        raise ValueError("lookback_hours 는 양수여야 한다")
    for department in departments:
        if department not in INVESTMENT_DEPARTMENT_STAGE:
            raise ValueError(f"unknown_investment_department:{department}")
    if load_worker_registry is None or workers_for_department is None:
        raise WorkerRegistryUnavailable(
            f"worker_registry_unavailable:{_WORKER_REGISTRY_IMPORT_ERROR}"
        )
    try:
        registry = load_worker_registry(repo_root)
    except WorkerRegistryError as exc:
        raise WorkerRegistryUnavailable(f"worker_registry_unavailable:{exc}") from exc

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=lookback_hours)

    if reader is None:
        try:
            reader = LangfuseApiTraceReader()
        except LangfuseQueryError:
            reader = None

    reports: list[WorkerTriggerRateReport] = []
    for department in departments:
        stage = INVESTMENT_DEPARTMENT_STAGE[department]
        for spec in workers_for_department(registry, department):
            reports.append(
                compute_worker_trigger_rate(
                    department=department, stage=stage, spec=spec, reader=reader,
                    since=since, now=now,
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

    # ▶ 워커 id 를 박아두지 않는다 (2026-08-11 실측). `research-data-worker` 등
    #   개편 전 이름이 박혀 있어 판정 로직은 멀쩡한데 자체 점검이 KeyError 로
    #   죽었다 - 이름 변경이 회귀처럼 보이면 진짜 회귀를 못 알아본다.
    class _NoneReader(LangfuseTraceReader):
        def latest_event_timestamp(self, *, event_name: str, since: datetime) -> datetime | None:
            return None

    _known = sorted(r.worker_id for r in
                    check_idle_agents(reader=_NoneReader(), departments=("research",), now=now))
    assert len(_known) >= 2, f"리서치 워커가 2명 미만이라 이 점검이 성립하지 않는다: {_known}"
    active_worker, idle_worker = _known[0], _known[1]
    active_name = langfuse_worker_event_name(stage="research", worker_id=active_worker)
    idle_name = langfuse_worker_event_name(stage="research", worker_id=idle_worker)
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
    assert by_id[active_worker].status is IdleStatus.ACTIVE, by_id[active_worker]
    assert by_id[idle_worker].status is IdleStatus.IDLE, by_id[idle_worker]
    unobserved = [r for r in reports if r.worker_id not in (active_worker, idle_worker)]
    # 워커가 딱 2명이면 나머지가 없다 - 없는 것을 있다고 요구하지 않는다
    assert all(r.status is IdleStatus.UNOBSERVED for r in unobserved), unobserved
    print(f"  ACTIVE/IDLE/UNOBSERVED 판정 - OK ({len(reports)}개 Worker)")

    # reader=None 이고 자격증명도 없으면 전원 UNAVAILABLE - "쉬고 있다"로 오판하지 않는다.
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    unavailable_reports = check_idle_agents(departments=("qa",), now=now)
    assert unavailable_reports and all(r.status is IdleStatus.UNAVAILABLE for r in unavailable_reports)
    print(f"  자격증명 없음 -> 전원 UNAVAILABLE - OK ({len(unavailable_reports)}개 Worker)")

    print("본부 6개 유휴 판정 자체 점검 통과.")

    # ── Capacity(2026-08-24) ────────────────────────────────────────────────

    class _FixedActivityReader(LangfuseTraceReader):
        """모든 event_name 에 같은 레코드 3건(latency 100/200/900ms, error 1건,
        retry 1건)을 돌려주는 대역 - Worker 수와 무관하게 집계값을 손으로
        검산할 수 있게 고정한다."""

        def list_worker_activity(self, *, event_name: str, since: datetime, limit: int = 200):
            return [
                WorkerActivityRecord(timestamp=now, latency_ms=100, error_count=0, retries=0),
                WorkerActivityRecord(timestamp=now, latency_ms=200, error_count=1, retries=1),
                WorkerActivityRecord(timestamp=now, latency_ms=900, error_count=0, retries=0),
            ]

    research_workers = check_idle_agents(reader=_NoneReader(), departments=("research",), now=now)
    n_research_workers = len({r.worker_id for r in research_workers})
    assert n_research_workers >= 1, "리서치 워커가 0명이라 이 점검이 성립하지 않는다"

    cap_reports = check_department_capacity(
        reader=_FixedActivityReader(), departments=("research",), lookback_hours=1.0, now=now,
    )
    assert len(cap_reports) == 1
    cap = cap_reports[0]
    assert cap.status is CapacityObservationStatus.MEASURED, cap
    assert cap.arrivals == 3 * n_research_workers, (cap.arrivals, n_research_workers)
    assert cap.duration_p95_ms == 900.0, cap.duration_p95_ms  # 3건 중 p95 -> 최댓값
    assert abs(cap.error_rate - (1 / 3)) < 1e-9, cap.error_rate
    assert abs(cap.retry_rate - (1 / 3)) < 1e-9, cap.retry_rate
    assert cap.utilization is not None and cap.utilization > 0, cap.utilization
    assert cap.as_dict()["queue_p95_ms"] is None  # 이 계측 경로에서 영구적으로 None
    print(f"  Capacity 집계(arrivals/p95/error_rate/retry_rate/utilization) - OK ({cap.arrivals}건)")

    # arrivals=0 인 부서(레코드 없음)는 MEASURED 이되 나머지가 전부 None이다 -
    # "측정했더니 0건"과 "측정을 못 했다"를 구분한다.
    class _EmptyActivityReader(LangfuseTraceReader):
        def list_worker_activity(self, *, event_name: str, since: datetime, limit: int = 200):
            return []

    empty_reports = check_department_capacity(
        reader=_EmptyActivityReader(), departments=("qa",), lookback_hours=1.0, now=now,
    )
    assert empty_reports[0].status is CapacityObservationStatus.MEASURED
    assert empty_reports[0].arrivals == 0
    assert empty_reports[0].duration_p95_ms is None and empty_reports[0].utilization is None
    print("  arrivals=0 -> MEASURED(0건), 나머지 None - OK")

    # reader=None(자격증명 없음)이면 전부 UNAVAILABLE - "부하 없음"으로 위장하지 않는다.
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    unavailable_cap = check_department_capacity(departments=("qa",), now=now)
    assert unavailable_cap[0].status is CapacityObservationStatus.UNAVAILABLE
    assert unavailable_cap[0].arrivals is None
    print("  자격증명 없음 -> Capacity 전부 UNAVAILABLE - OK")

    print("Capacity(Langfuse 기반) 자체 점검 통과.")

    # ── 발화율(2026-08-25) ──────────────────────────────────────────────────

    class _FixedCountReader(LangfuseTraceReader):
        """실행 이벤트는 2건, 미발화 이벤트는 3건으로 고정 - fire_rate = 2/5 = 0.4를
        손으로 검산할 수 있게 한다."""

        def count_events(self, *, event_name: str, since: datetime, limit: int = 200) -> int:
            return 2 if event_name.startswith("llm.performance.metric:") else 3

    rate_reports = check_worker_trigger_rates(
        reader=_FixedCountReader(), departments=("research",), lookback_hours=1.0, now=now,
    )
    assert len(rate_reports) == n_research_workers
    for r in rate_reports:
        assert r.status is TriggerRateObservationStatus.MEASURED, r
        assert r.execution_count == 2 and r.opportunity_count == 3, r
        assert abs(r.fire_rate - 0.4) < 1e-9, r.fire_rate
    print(f"  발화율 = 실행/(실행+미발화) 계산 - OK ({len(rate_reports)}개 Worker, 0.4)")

    # 분모 0(이 창에 기회 자체가 없었다)은 fire_rate 0.0이 아니라 None이어야 한다 -
    # "발화율이 0%다"와 "잴 기회가 없었다"를 섞으면 조건부 Worker가 전부 저성과로
    # 보인다.
    class _ZeroCountReader(LangfuseTraceReader):
        def count_events(self, *, event_name: str, since: datetime, limit: int = 200) -> int:
            return 0

    zero_reports = check_worker_trigger_rates(
        reader=_ZeroCountReader(), departments=("qa",), lookback_hours=1.0, now=now,
    )
    assert all(r.status is TriggerRateObservationStatus.MEASURED for r in zero_reports)
    assert all(r.execution_count == 0 and r.opportunity_count == 0 for r in zero_reports)
    assert all(r.fire_rate is None for r in zero_reports), [r.fire_rate for r in zero_reports]
    print("  분모 0 -> fire_rate None(0.0 아님) - OK")

    # reader=None(자격증명 없음)이면 전부 UNAVAILABLE.
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    unavailable_rate = check_worker_trigger_rates(departments=("qa",), now=now)
    assert unavailable_rate and all(
        r.status is TriggerRateObservationStatus.UNAVAILABLE for r in unavailable_rate
    )
    assert all(r.fire_rate is None for r in unavailable_rate)
    print("  자격증명 없음 -> 발화율 전부 UNAVAILABLE - OK")

    print("발화율(Langfuse 기반) 자체 점검 통과.")
