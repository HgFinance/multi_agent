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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

# ▶ 2026-08-20: 남의 본부 **파이썬 모듈 import** 를 걷어냈다(영주 결정).
#   이전에는 orchestration.employee_dispatch.load_worker_specs() 로 각 본부의
#   employee_workers.py 를 실행해 WORKER_SPECS 를 읽었다. 유휴 판정에 필요한 값은
#   worker_id 와 trigger 두 개뿐인데, 그걸 얻으려고 프롬프트·툴 정의까지 든 실행
#   모듈을 HR 프로세스 안에서 돌린 셈이다. 배포 이미지에는 다른 본부 코드가 없어
#   (계획서 11.1) 그 import 가 실패했고, 2026-08-12 에는 HR API 전체가 크래시
#   루프에 빠졌다.
#
#   지금은 부서 Hermes Profile(`hermes/config.yaml`)의 `workers:` 만 읽는다 -
#   CLAUDE.md 가 "정본은 각 부서 hermes/config.yaml 의 workers" 라고 못 박은 바로
#   그 파일이고, tests/test_worker_architecture.py 도 이미 그 YAML 을 진실로 삼아
#   편제를 검증한다. 코드가 아니라 **데이터**를 읽으므로 남의 본부 코드는 이
#   프로세스에 들어오지 않는다.
#
#   ⚠ 이건 "선언된 편제"이지 "지금 배포된 런타임"이 아니다. 선언돼 있는데 실제로
#     안 도는 워커는 UNOBSERVED 로 드러난다 - 이 리포트가 잡아야 할 종류의 불일치다.
#     편제가 런타임에 동적으로 바뀌게 되면 그때 Versioned API 로 승격한다(계획서 3.2).

try:
    from orchestration.llm_observability import langfuse_worker_event_name
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


class WorkerRegistryUnavailable(RuntimeError):
    """부서 Worker registry 를 이 런타임에서 읽을 수 없다(유휴 판정 불가)."""

ROOT = Path(__file__).resolve().parents[3]

# 부서 dispatch 키 -> Hermes Profile 디렉터리명. 이 매핑이 INVESTMENT_DEPARTMENT_STAGE
# 와 별개인 이유는 모듈 docstring "부서 키가 두 개인 이유" 와 같다 - 폴더명(번호 접두어)
# 은 또 다른 이름 공간이다.
DEPARTMENT_PROFILE_DIR: dict[str, str] = {
    "research": "01-research",
    "trading": "02-trading",
    "risk": "03-risk",
    "quant-backtest": "04-quant-backtest",
    "accounting-portfolio": "05-accounting-portfolio",
    "qa": "06-ai-qa-audit",
}

# 컨테이너에서는 저장소 트리가 통째로 있지 않고 config.yaml 6 개만 read-only 로
# 마운트된다(departments/07-agent-workforce/compose.yaml). 경로는 환경변수로 바꿀 수
# 있게 두되 기본값을 박아둬 compose 와 코드가 따로 놀지 않게 한다.
PROFILE_MOUNT_ROOT_ENV = "WORKFORCE_PROFILE_ROOT"
DEFAULT_PROFILE_MOUNT_ROOT = Path("/app/profiles")


@dataclass(frozen=True)
class WorkerProfileSpec:
    """Profile 에 선언된 Worker 한 명. 유휴 판정에 필요한 두 필드만 갖는다."""

    worker_id: str
    trigger: str


def _profile_candidates(department: str, repo_root: Path) -> tuple[Path, ...]:
    """저장소 직접 실행(개발)과 마운트(컨테이너) 두 경로를 다 본다."""

    directory = DEPARTMENT_PROFILE_DIR.get(department)
    if directory is None:
        raise ValueError(f"unknown_investment_department:{department}")
    mount_root = Path(os.environ.get(PROFILE_MOUNT_ROOT_ENV) or DEFAULT_PROFILE_MOUNT_ROOT)
    return (
        repo_root / "departments" / directory / "hermes" / "config.yaml",
        mount_root / department / "config.yaml",
    )


def load_head_profile_spec(repo_root: Path, department: str) -> WorkerProfileSpec | None:
    """부서장(Hermes Profile) 1명. Profile 의 `agent.head_persona` 가 정본이다.

    2026-08-20 신규. 부서장은 `workers:` 에 없다 - 직원이 아니라 본부장이라서다.
    그래서 편제표(LLM Worker 10명)와도 별개이고, 기본 리포트에는 포함하지 않는다
    (include_heads=True 로 명시할 때만 나온다). 관측 대상 인원이 조용히 늘면
    "8명 중 3명 유휴" 같은 문장이 말없이 다른 뜻이 된다.

    write 측은 apps/api/hermes_boundary.py 가 **같은 파일의 같은 키**를 읽어
    이벤트 이름을 만든다 - 두 쪽이 다른 출처를 보면 조용히 어긋난다.
    """

    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - 이미지 빌드 결함
        raise WorkerRegistryUnavailable(f"pyyaml_not_installed:{exc}") from exc

    for path in _profile_candidates(department, repo_root):
        if not path.is_file():
            continue
        try:
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            raise WorkerRegistryUnavailable(
                f"profile_unreadable:{department}:{type(exc).__name__}"
            ) from exc
        persona = ((config.get("agent") or {}).get("head_persona") or "").strip()
        if not persona:
            return None
        # 부서장은 "요청이 올 때" 돈다 - conditional Worker 의 trigger 와 같은 자리에
        # 그 사실을 적어 리포트가 그대로 읽히게 한다.
        return WorkerProfileSpec(str(persona), "on_request")
    raise WorkerRegistryUnavailable(f"profile_not_found:{department}")


def load_worker_profile_specs(repo_root: Path, department: str) -> tuple[WorkerProfileSpec, ...]:
    """부서 Profile 의 `workers:` 를 읽어 (worker_id, trigger) 목록을 만든다.

    실패 구분이 이 함수의 요점이다:
      - 파일을 못 찾거나 못 읽음 / `workers:` 키가 없음  -> WorkerRegistryUnavailable
        ("우리가 모른다". 빈 목록으로 돌려주면 "유휴 워커 없음" 으로 오독된다)
      - `workers: {}` (트레이딩)                        -> 빈 tuple, 정상
        LLM 직원을 두지 않는 부서가 실제로 있다(CLAUDE.md 편제표: trading 0 명).
    """

    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - 이미지 빌드 결함
        raise WorkerRegistryUnavailable(f"pyyaml_not_installed:{exc}") from exc

    candidates = _profile_candidates(department, repo_root)
    for path in candidates:
        if not path.is_file():
            continue
        try:
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001 - 깨진 Profile 도 "모른다" 로 접힌다
            raise WorkerRegistryUnavailable(
                f"profile_unreadable:{department}:{type(exc).__name__}"
            ) from exc
        workers = config.get("workers")
        if workers is None:
            # `workers: {}` 는 None 이 아니라 빈 dict 다 - 선언된 0 명과 키 자체가
            # 없는 결함을 구분한다.
            raise WorkerRegistryUnavailable(f"profile_workers_key_missing:{department}")
        if not isinstance(workers, dict):
            raise WorkerRegistryUnavailable(f"profile_workers_not_mapping:{department}")
        specs: list[WorkerProfileSpec] = []
        for worker_id, item in workers.items():
            trigger = (item or {}).get("trigger") if isinstance(item, dict) else None
            if not worker_id or not trigger:
                raise WorkerRegistryUnavailable(f"profile_worker_invalid:{department}:{worker_id}")
            specs.append(WorkerProfileSpec(str(worker_id), str(trigger)))
        return tuple(specs)
    raise WorkerRegistryUnavailable(
        "profile_not_found:" + department + ":" + os.pathsep.join(str(c) for c in candidates)
    )


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
    include_heads: bool = False,
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
        # Profile(데이터)만 읽는다 - 워커 목록 자체를 못 얻으면 여기서 던지는
        # WorkerRegistryUnavailable 가 app.py 에서 503 이 된다. 빈 목록으로 돌려주면
        # "유휴 워커 없음" 으로 오독되기 때문이다.
        specs = load_worker_profile_specs(repo_root, department)
        if include_heads:
            # 부서장은 편제표 밖이라 기본값에서 빠져 있다 - 명시적으로 요청할 때만
            # 합친다(load_head_profile_spec docstring 참고).
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
