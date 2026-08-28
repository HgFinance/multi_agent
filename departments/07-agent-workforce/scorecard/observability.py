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

⚠ 경계 확장(2026-08-24 Capacity, 2026-08-25 LLM 사용량): `list_worker_activity()`는
metadata 중 `latency_ms`/`error_count`/`retries`/`attempts`/`status`/`llm_calls`/
`model_name`/`prompt_tokens`/`completion_tokens`만 추가로 읽는다. 전부 QA 판정이
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

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

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

try:
    from orchestration.contracts.runtime_service_registry import (
        RuntimeServiceRegistryError,
        load_runtime_service_registry,
    )
except ModuleNotFoundError as _exc:
    load_runtime_service_registry = None  # type: ignore[assignment]
    _RUNTIME_SERVICE_REGISTRY_IMPORT_ERROR: str | None = f"{type(_exc).__name__}:{_exc}"
    class RuntimeServiceRegistryError(RuntimeError):
        """Fallback type used when the common runtime registry is not packaged."""
else:
    _RUNTIME_SERVICE_REGISTRY_IMPORT_ERROR = None



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


def _safe_str(value: Any) -> str | None:
    """metadata 값을 str 로 바꾼다 - 빈 값(None/"")은 None."""

    if value in (None, ""):
        return None
    return str(value)


def _percentile(values: list[float], fraction: float) -> float | None:
    """values 의 fraction 분위수(예: 0.95 -> p95). values 가 비면 None."""

    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _require_reason(status: Any, unavailable: Any, reason: str | None) -> None:
    """UNAVAILABLE 에는 사유가 반드시 있고, 관측된 상태에는 없어야 한다.

    네 리포트가 같은 규약을 쓰도록 한 자리에 모은다. "빈 응답은 ok=False + 사유"
    라는 계약을 dataclass 생성 시점에 강제하는 것이 요점이다 - 나중에 리포트를
    한 종류 더 만들 때 이 검사를 빼먹으면 그 종류만 조용히 사유 없이 나간다.
    """

    if status is unavailable and not reason:
        raise ValueError(f"{status.value} 는 사유(reason) 없이 나올 수 없다")
    if status is not unavailable and reason:
        raise ValueError(f"{status.value} 는 관측된 상태라 사유(reason)를 가질 수 없다")


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
    # UNAVAILABLE 일 때 **왜 관측을 못 했는지**. 관측된 판정에는 없다.
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is IdleStatus.ACTIVE and self.last_seen_at is None:
            raise ValueError("ACTIVE 판정은 last_seen_at 없이 나올 수 없다")
        if self.status in (IdleStatus.UNOBSERVED, IdleStatus.UNAVAILABLE) and self.last_seen_at is not None:
            raise ValueError(f"{self.status.value} 판정은 last_seen_at 이 있으면 안 된다")
        # 사유 없는 실패는 만들 수 없게 계약으로 막는다 - UNAVAILABLE 이 사유 없이
        # 나가면 읽는 쪽(사람·HR Agent)이 "관측 실패"와 "설정 미비"와 "API 오류"를
        # 구분할 수 없고, 그 상태로 몇 주가 지나간다(2026-08-27 limit 상수 사고).
        _require_reason(self.status, IdleStatus.UNAVAILABLE, self.reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "department": self.department,
            "worker_id": self.worker_id,
            "trigger": self.trigger,
            "status": self.status.value,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "idle_hours": float(self.idle_hours) if self.idle_hours is not None else None,
            "reason": self.reason,
        }


class LangfuseQueryError(RuntimeError):
    """Langfuse 조회 자체가 실패함 (자격증명·네트워크·API 응답 이상)."""


# reader 를 아예 만들지 못한 경우의 기본 사유. 호출부가 생성 시점 예외 메시지를
# 넘겨주면(langfuse_credentials_missing / langfuse_not_installed) 그쪽이 이긴다 -
# 이 값은 reader=None 이 외부에서 주입된 경우에만 남는다.
_READER_UNAVAILABLE_REASON = "langfuse_reader_unavailable"

# 사유 문자열에 실을 최대 길이. 관측 리포트는 사람이 읽는 표라 본문 전체를
# 옮기면 셀이 무너진다 - 원인을 식별할 만큼만 남긴다.
_REASON_MAX_CHARS = 200


def _query_failure_reason(exc: Exception) -> str:
    """조회 예외를 **행에 실을 수 있는 사유 문자열**로 바꾼다(2026-08-27).

    이전에는 `langfuse_trace_list_failed:{type(exc).__name__}` 이었다. langfuse
    SDK 의 4xx 는 클래스 이름이 전부 `Error` 라서 그 값이 늘 `...failed:Error`
    였고, 정작 원인(HTTP 400 / 'limit must be <=100')은 예외 안에만 남고 리포트에
    닿지 않았다. 그 결과 HR Agent 가 "관측 실패 사유가 핸드오프에 없다"고만
    적었고, 사람은 상수 하나 때문이라는 걸 알 길이 없었다 - 진단 가능한 실패를
    진단 불가능하게 만드는 자리였다.

    body 원문은 Trace 내용이 아니라 **요청 검증 오류 메시지**다(우리가 보낸
    쿼리 파라미터에 대한 서버 응답) - "Trace 원문을 읽지 않는다" 규약과 무관하다.
    """

    parts = [f"langfuse_trace_list_failed:{type(exc).__name__}"]
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        parts.append(f"http_{status_code}")
    body = getattr(exc, "body", None)
    detail = ""
    if isinstance(body, dict):
        messages = [str(body.get("message"))] if body.get("message") else []
        errors = body.get("error")
        if isinstance(errors, list):
            messages.extend(
                str(item.get("message")) for item in errors
                if isinstance(item, dict) and item.get("message")
            )
        detail = "; ".join(m for m in messages if m)
    elif body not in (None, ""):
        detail = str(body)
    if not detail:
        detail = str(exc)
    detail = " ".join(detail.split())
    if detail:
        parts.append(detail[:_REASON_MAX_CHARS])
    return ":".join(parts)


@dataclass(frozen=True)
class WorkerActivityRecord:
    """실행 이벤트 한 건에서 Capacity/LLM 사용량 계산에 필요한 필드만 뽑는다.

    전부 이 이벤트를 쓴 실행기 자신의 값이다(publish_worker_activity() 참고) -
    QA 소유 eval_score 나 input/output 원문은 여기 없다(위 "원문을 읽지 않는다"
    절 참고). attempts/status는 매 이벤트에 항상 있고, llm_calls/model_name/
    prompt_tokens/completion_tokens는 begin_worker_metric() 컨텍스트가 열려
    있었을 때만 있다(둘 다 없으면 None) - retries와 같은 조건이다.
    """

    timestamp: datetime
    latency_ms: int | None
    error_count: int | None
    retries: int | None
    attempts: int | None
    status: str | None
    llm_calls: int | None
    model_name: str | None
    prompt_tokens: int | None
    completion_tokens: int | None


# Langfuse `GET /api/public/traces` 가 받는 limit 의 **서버 상한**이다. 넘기면
# 200 이 아니라 400 을 돌려준다:
#   {'message': 'Invalid request data', 'error': [{'code': 'too_big',
#     'maximum': 100, 'path': ['limit'], 'message': 'Too big: expected number to be <=100'}]}
#
# ▶ 2026-08-27 실측. 이 값이 200 이던 동안 **HR 의 Langfuse 관측 질의가 단 한 번도
#   성공한 적이 없다.** 400 이 LangfuseQueryError 로 접히고 그게 다시 UNAVAILABLE
#   로 접혀서, 유휴·Capacity·LLM 사용량·발화율 네 리포트가 전부 "관측 불가"로만
#   나왔다 - 예외도 로그도 없이 조용히 죽는 종류다. 재현: limit=100 OK,
#   limit=101 부터 400. 테스트 대역(_FakeTraceApi)은 limit 을 검사하지 않아
#   이 400 을 영원히 못 본다 - 그래서 아래 _assert_page_limit_within_api_max() 로
#   상수 자체를 못 박는다.
LANGFUSE_MAX_PAGE_LIMIT = 100
DEFAULT_ACTIVITY_PAGE_LIMIT = LANGFUSE_MAX_PAGE_LIMIT
# 한 Worker·한 창에서 실측으로 모을 최대 페이지 수(100 x 20 = 2000건). 페이지가
# 작아진 만큼 페이지 수를 늘려 창당 수집 상한을 그대로 유지한다 - 상한을 같이
# 줄이면 이번 수정이 조용한 표본 축소가 된다. 이 상한을 넘긴 창은 records 가
# 표본이 되지만 total_items 는 서버 meta 에서 오므로 건수는 계속 정확하다 -
# 잘린 것을 "그만큼만 있었다"로 바꾸지 않는 것이 요점이다.
MAX_ACTIVITY_PAGES = 20
LANGFUSE_OBSERVATIONS_PAGE_LIMIT = 1_000
MAX_OBSERVATIONS_PAGES = 20


@dataclass(frozen=True)
class WorkerActivityPage:
    """한 (event_name, 창) 조합에서 읽어온 실행 이벤트 묶음.

    total_items 가 records 길이와 따로 있는 이유: 이전 count_events() 는
    len(page.data) 를 돌려줬고, 그래서 창 안에 limit(한 페이지 상한) 이상이 쌓이면
    실행·미발화 둘 다 그 상한으로 포화돼 fire_rate 가 실제와 무관하게 0.5 로
    수렴했다 - 예외 없이 조용히 틀리는 종류의 실패다. total_items 는 서버 meta 에서
    받아오므로 records 가 잘려도 건수는 정확하다.
    """

    records: tuple[WorkerActivityRecord, ...]
    total_items: int
    truncated: bool


class LangfuseTraceReader:
    """조회 전용 인터페이스. read 측이라 create_event 계열은 갖지 않는다."""

    def fetch_worker_activity(
        self, *, event_name: str, since: datetime, max_pages: int = MAX_ACTIVITY_PAGES
    ) -> WorkerActivityPage:
        """이 모듈의 단일 조회 원시함수 - 창 안의 실행 이벤트를 모아 돌려준다.

        기본 구현은 기존 list_worker_activity() 로 접는다. 이 인터페이스를 직접
        구현한 테스트 대역(tests/test_hr_idle_agents.py 등)이 새 메서드를 몰라도
        계속 동작해야 해서다 - 대역을 다 고치게 만들면 이번 변경의 회귀 위험이
        판정 로직이 아니라 대역 쪽으로 옮겨간다.
        """

        records = tuple(self.list_worker_activity(event_name=event_name, since=since))
        return WorkerActivityPage(records=records, total_items=len(records), truncated=False)

    def count_worker_activity(self, *, event_name: str, since: datetime) -> int:
        """건수만 필요한 조회(미발화 이벤트). 기본 구현은 count_events() 로 접는다.

        fetch_worker_activity() 와 나눠 두는 이유: 미발화(not_executed) 이벤트는
        조건부 Worker 라면 하루에도 수백 건이 쌓이는데 발화율은 그 **개수**만
        쓴다. 레코드를 다 끌어오면 쓰지도 않을 페이지를 도는 꼴이다.
        """

        return self.count_events(event_name=event_name, since=since)


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

    # ── 배치 조회 (2026-08-27) ────────────────────────────────────────────────
    #
    # Langfuse Public API 는 **분당 15 요청** 상한이다(실측: 429 응답의
    # `x-ratelimit-limit: 15`). 도입 전에는 Worker별 실행/미발화 조회가
    # 이 한도를 넘었고, SDK가 `Retry-After`만큼 대기해 collect가 41~62초로
    # 늘어나는 사례가 있었다.
    #
    # 아래 둘은 현재 실행/미발화 조회를 각각 한 번의 배치로 접는다. 기본 구현은
    # 이름마다 기존 단건 메서드를 부르는 것이라, 이 메서드를 모르는 테스트 대역도 그대로 동작한다
    # (fetch_worker_activity 가 list_worker_activity 로 접히는 것과 같은 이유).

    def fetch_many_worker_activity(
        self, *, event_names: tuple[str, ...], since: datetime,
        max_pages: int = MAX_ACTIVITY_PAGES,
    ) -> dict[str, WorkerActivityPage]:
        """여러 이벤트 이름의 실행 레코드를 한 번에 모은다. 요청한 이름은 전부 키로 온다.

        ▶ 조회에 성공했는데 레코드가 0건인 이름도 **빈 페이지로 돌려준다.**
          키를 빼면 호출부가 "관측했더니 없었다"(UNOBSERVED)와 "조회 못 했다"
          (UNAVAILABLE)를 구분할 수 없다.
        """

        return {
            name: self.fetch_worker_activity(
                event_name=name, since=since, max_pages=max_pages
            )
            for name in event_names
        }

    def count_many_worker_activity(
        self, *, event_names: tuple[str, ...], since: datetime,
        until: datetime | None = None,
    ) -> dict[str, int]:
        """여러 이벤트 이름의 건수를 한 번에 센다. 요청한 이름은 전부 키로 온다.

        ▶ 건수 0 인 이름도 **0 으로 돌려준다.** 발화율의 분모가 여기서 나오는데,
          키가 빠지면 "기회 0건"(fire_rate=None)이 "조회 실패"와 섞인다.
        """

        return {
            name: self.count_worker_activity(event_name=name, since=since)
            for name in event_names
        }


def _bounded_langfuse_call(method: Any, **kwargs: Any) -> Any:
    """Call an SDK endpoint with bounded retries, preserving old test doubles."""

    try:
        return method(
            **kwargs,
            request_options={"timeout_in_seconds": 5, "max_retries": 0},
        )
    except TypeError as exc:
        # Older/local fakes implement the pre-request_options signature.  Keep
        # those doubles usable without weakening the real SDK call above.
        if "request_options" not in str(exc):
            raise
        return method(**kwargs)


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

    def _list_page(self, *, event_name: str, since: datetime, limit: int, page: int):
        try:
            return _bounded_langfuse_call(
                self._client.api.trace.list,
                name=event_name, from_timestamp=since, limit=limit, page=page,
            )
        except Exception as exc:
            raise LangfuseQueryError(_query_failure_reason(exc)) from exc

    def fetch_worker_activity(
        self, *, event_name: str, since: datetime, max_pages: int = MAX_ACTIVITY_PAGES
    ) -> WorkerActivityPage:
        """창 안의 실행 이벤트를 페이지 끝까지 모은다 - Langfuse 를 만지는 유일한 경로.

        ▶ 서버 정렬에 기대지 않는다. order_by 문자열 문법이 langfuse 버전별로
          갈리기 때문인데, 이전 구현은 그 대신 `limit=50` 한 장만 받아 클라이언트
          max() 를 썼다. 창 안에 50건이 넘으면 그 한 장에 최신 건이 없을 수 있고,
          그러면 멀쩡히 도는 Worker 가 IDLE/UNOBSERVED 로 뒤집힌다. 페이지를 끝까지
          도는 쪽이 그 가정 자체를 없앤다.
        """

        records: list[WorkerActivityRecord] = []
        total_items: int | None = None
        truncated = False
        page_number = 1
        while True:
            page = self._list_page(
                event_name=event_name, since=since,
                limit=DEFAULT_ACTIVITY_PAGE_LIMIT, page=page_number,
            )
            if total_items is None:
                total_items = _meta_int(page, "total_items")
            records.extend(_activity_records(page))
            if not page.data:
                break
            total_pages = _meta_int(page, "total_pages")
            if total_pages is not None and page_number >= total_pages:
                break
            if total_pages is None and len(page.data) < DEFAULT_ACTIVITY_PAGE_LIMIT:
                # meta 를 못 읽는 서버·대역에서는 "덜 찬 페이지"가 끝의 신호다.
                break
            if page_number >= max_pages:
                truncated = True
                break
            page_number += 1
        return WorkerActivityPage(
            records=tuple(records),
            total_items=total_items if total_items is not None else len(records),
            truncated=truncated,
        )

    def count_worker_activity(self, *, event_name: str, since: datetime) -> int:
        """meta.total_items 만 읽는다 - limit=1 이라 페이로드가 거의 없다.

        레코드를 모으지 않으므로 왕복 한 번으로 끝나고, 예전 count_events() 처럼
        limit 에서 포화되지도 않는다.
        """

        page = self._list_page(event_name=event_name, since=since, limit=1, page=1)
        total_items = _meta_int(page, "total_items")
        return total_items if total_items is not None else len(page.data)

    # ── 배치 조회 구현 ────────────────────────────────────────────────────────

    def _list_page_many(self, *, event_names: tuple[str, ...], since: datetime, page: int):
        """이름 여러 개를 `any of` 필터 하나로 묶어 한 페이지 받는다.

        `name=` 파라미터는 값을 하나만 받지만, `filter=` 는 Metrics API 와 같은
        필터 문법을 받아 `stringOptions`/`any of` 를 쓸 수 있다(langfuse
        4.14.3 실측). 그래서 Worker 8명이 요청 하나가 된다.
        """

        payload = json.dumps(
            [{"column": "name", "operator": "any of",
              "value": list(event_names), "type": "stringOptions"}]
        )
        try:
            return _bounded_langfuse_call(
                self._client.api.trace.list,
                from_timestamp=since, limit=DEFAULT_ACTIVITY_PAGE_LIMIT,
                page=page, filter=payload,
            )
        except Exception as exc:
            raise LangfuseQueryError(_query_failure_reason(exc)) from exc

    def fetch_many_worker_activity(
        self, *, event_names: tuple[str, ...], since: datetime,
        max_pages: int = MAX_ACTIVITY_PAGES,
    ) -> dict[str, WorkerActivityPage]:
        names = tuple(dict.fromkeys(event_names))
        if not names:
            return {}

        grouped: dict[str, list[WorkerActivityRecord]] = {name: [] for name in names}
        truncated = False
        page_number = 1
        while True:
            page = self._list_page_many(event_names=names, since=since, page=page_number)
            for item in page.data:
                record = _activity_record(item)
                if record is None:
                    continue
                bucket = grouped.get(getattr(item, "name", None))
                if bucket is not None:
                    bucket.append(record)
            if not page.data:
                break
            total_pages = _meta_int(page, "total_pages")
            if total_pages is not None and page_number >= total_pages:
                break
            if total_pages is None and len(page.data) < DEFAULT_ACTIVITY_PAGE_LIMIT:
                break
            if page_number >= max_pages:
                truncated = True
                break
            page_number += 1

        # ▶ 배치에서는 서버 meta.total_items 가 **묶음 전체**의 건수라 이름별
        #   건수로 쓸 수 없다. 잘리지 않았으면 모은 레코드 수가 곧 정확한 건수다.
        #   잘렸으면 그건 표본이므로 truncated 를 세워 호출부가 알게 한다
        #   (정확한 이름별 건수는 count_many_worker_activity 가 따로 준다).
        return {
            name: WorkerActivityPage(
                records=tuple(records), total_items=len(records), truncated=truncated,
            )
            for name, records in grouped.items()
        }

    def count_many_worker_activity(
        self, *, event_names: tuple[str, ...], since: datetime,
        until: datetime | None = None,
    ) -> dict[str, int]:
        """Metrics API 로 이름별 건수를 **요청 한 번**에 받는다.

        레코드를 안 끌어오므로 건수가 아무리 커도 왕복 하나다 - 미발화 이벤트는
        조건부 Worker 하나가 하루 수백 건을 쌓을 수 있어서 이 차이가 크다.

        우리 실행·미발화 이벤트는 `create_event()` 산물이라 observations view 의
        EVENT 로 잡힌다(실측 확인).
        """

        names = tuple(dict.fromkeys(event_names))
        if not names:
            return {}
        observations_api = getattr(getattr(self._client, "api", None), "observations", None)
        observations_get_many = getattr(observations_api, "get_many", None)
        if callable(observations_get_many):
            # Langfuse recommends the v2 observations endpoint for high-volume
            # reads. It is cursor-paginated and avoids the separately
            # rate-limited Metrics endpoint that was producing 429s for HR.
            filters = json.dumps([
                {
                    "column": "name",
                    "operator": "any of",
                    "value": list(names),
                    "type": "stringOptions",
                },
                {
                    "column": "type",
                    "operator": "=",
                    "value": "EVENT",
                    "type": "string",
                },
                {
                    "column": "startTime",
                    "operator": ">=",
                    "value": since.isoformat(),
                    "type": "datetime",
                },
                {
                    "column": "startTime",
                    "operator": "<",
                    "value": (until or datetime.now(timezone.utc)).isoformat(),
                    "type": "datetime",
                },
            ])
            counts = dict.fromkeys(names, 0)
            cursor: str | None = None
            for _ in range(MAX_OBSERVATIONS_PAGES):
                kwargs: dict[str, Any] = {
                    "fields": "core,basic",
                    "limit": LANGFUSE_OBSERVATIONS_PAGE_LIMIT,
                    "filter": filters,
                }
                if cursor:
                    kwargs["cursor"] = cursor
                try:
                    response = _bounded_langfuse_call(observations_get_many, **kwargs)
                except Exception as exc:
                    raise LangfuseQueryError(_query_failure_reason(exc)) from exc
                for item in (getattr(response, "data", None) or []):
                    name = getattr(item, "name", None)
                    if name in counts:
                        counts[name] += 1
                next_cursor = getattr(getattr(response, "meta", None), "cursor", None)
                if not next_cursor or next_cursor == cursor:
                    return counts
                cursor = str(next_cursor)
            raise LangfuseQueryError("langfuse_observations_count_truncated")

        query = {
            "view": "observations",
            "dimensions": [{"field": "name"}],
            "metrics": [{"measure": "count", "aggregation": "count"}],
            "filters": [{"column": "name", "operator": "any of",
                         "value": list(names), "type": "stringOptions"}],
            "fromTimestamp": since.isoformat(),
            "toTimestamp": (until or datetime.now(timezone.utc)).isoformat(),
            # 기본값 100 을 넘길 일은 없지만(이름 수만큼만 나온다) 명시해 둔다.
            "config": {"row_limit": max(100, len(names) * 2)},
        }
        try:
            response = _bounded_langfuse_call(
                self._client.api.metrics.metrics,
                query=json.dumps(query),
            )
        except Exception as exc:
            # The Metrics endpoint is independently rate-limited in Langfuse.
            # On 429, use the same bounded Trace batch already used for
            # execution activity and count only event names/timestamps.  This
            # keeps the HR read contract metadata-only while avoiding a
            # second unbounded retry loop.  Test doubles without a Trace API
            # retain the original error behavior.
            if getattr(exc, "status_code", None) != 429:
                raise LangfuseQueryError(_query_failure_reason(exc)) from exc
            trace_api = getattr(getattr(self._client, "api", None), "trace", None)
            if trace_api is None:
                raise LangfuseQueryError(_query_failure_reason(exc)) from exc
            counts = dict.fromkeys(names, 0)
            page_number = 1
            while True:
                page = self._list_page_many(
                    event_names=names, since=since, page=page_number,
                )
                for item in page.data:
                    name = getattr(item, "name", None)
                    if name in counts:
                        counts[name] += 1
                if not page.data:
                    break
                total_pages = _meta_int(page, "total_pages")
                if total_pages is not None and page_number >= total_pages:
                    break
                if total_pages is None and len(page.data) < DEFAULT_ACTIVITY_PAGE_LIMIT:
                    break
                if page_number >= MAX_ACTIVITY_PAGES:
                    raise LangfuseQueryError("langfuse_trace_count_truncated")
                page_number += 1
            return counts

        # ▶ 건수 0 인 이름은 응답에 **행이 아예 없다.** 0 으로 채워 둬야
        #   "기회 0건"과 "조회 실패"가 안 섞인다.
        counts = dict.fromkeys(names, 0)
        for row in (getattr(response, "data", None) or []):
            name = row.get("name") if isinstance(row, dict) else getattr(row, "name", None)
            if name not in counts:
                continue
            raw = row.get("count_count") if isinstance(row, dict) else getattr(row, "count_count", None)
            value = _safe_int(raw)
            counts[name] = value if value is not None else 0
        return counts

    # 아래 셋은 기존 호출부·테스트 대역과의 계약을 위해 남긴다. 전부 위의 두
    # 원시함수로 접히므로 Langfuse 를 실제로 부르는 자리는 _list_page 하나다.

    def latest_event_timestamp(self, *, event_name: str, since: datetime) -> datetime | None:
        page = self.fetch_worker_activity(event_name=event_name, since=since)
        timestamps = [r.timestamp for r in page.records]
        return max(timestamps) if timestamps else None

    def list_worker_activity(
        self, *, event_name: str, since: datetime, limit: int = DEFAULT_ACTIVITY_PAGE_LIMIT
    ) -> list[WorkerActivityRecord]:
        return list(self.fetch_worker_activity(event_name=event_name, since=since).records)

    def count_events(
        self, *, event_name: str, since: datetime, limit: int = DEFAULT_ACTIVITY_PAGE_LIMIT
    ) -> int:
        return self.count_worker_activity(event_name=event_name, since=since)


def _meta_int(page: Any, field: str) -> int | None:
    """Traces.meta 의 정수 필드(total_items/total_pages). 없으면 None.

    meta 를 못 읽는 경우를 0 으로 바꾸지 않는다 - 0 은 "창이 비었다"는 관측이고
    None 은 "서버가 안 알려줬다"라서, 호출부가 len(records) 로 접을 수 있어야 한다.
    """

    meta = getattr(page, "meta", None)
    if meta is None:
        return None
    return _safe_int(getattr(meta, field, None))


def _activity_record(item: Any) -> WorkerActivityRecord | None:
    """Trace 한 건에서 metadata 허용 목록만 뽑는다(input/output 은 안 읽는다).

    timestamp 가 없으면 None - 유휴 판정이 timestamp 위에 서 있어서, 시각 없는
    이벤트를 "관측됨"으로 세면 안 된다. 단건·배치 경로가 같은 변환을 쓰도록
    여기 하나로 모은다(이름별로 갈리면 배치에서만 조용히 다른 값이 나온다).
    """

    if getattr(item, "timestamp", None) is None:
        return None
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    return WorkerActivityRecord(
        timestamp=item.timestamp,
        latency_ms=_safe_int(metadata.get("latency_ms")),
        error_count=_safe_int(metadata.get("error_count")),
        retries=_safe_int(metadata.get("retries")),
        attempts=_safe_int(metadata.get("attempts")),
        status=_safe_str(metadata.get("status")),
        llm_calls=_safe_int(metadata.get("llm_calls")),
        model_name=_safe_str(metadata.get("model_name")),
        prompt_tokens=_safe_int(metadata.get("prompt_tokens")),
        completion_tokens=_safe_int(metadata.get("completion_tokens")),
    )


def _activity_records(page: Any) -> list[WorkerActivityRecord]:
    """Traces 한 페이지에서 metadata 허용 목록만 뽑는다(input/output 은 안 읽는다)."""

    records = (_activity_record(item) for item in page.data)
    return [record for record in records if record is not None]


def _worker_event_names(
    *, departments: tuple[str, ...], repo_root: Path
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """프리페치가 담을 (실행 이벤트 이름, 미발화 이벤트 이름).

    이름을 여기서 새로 짓지 않는다 - 네 집계가 부르는 것과 **같은 함수**로
    만든다. 규칙을 한 벌 더 만들면 프리페치만 조용히 다른 이름을 채우고, 캐시가
    빗나가 왕복이 원래대로 돌아간다(그러면 이 최적화가 무효인 채로 초록불이다).
    """

    if load_worker_registry is None or workers_for_department is None:
        raise WorkerRegistryUnavailable(
            f"worker_registry_unavailable:{_WORKER_REGISTRY_IMPORT_ERROR}"
        )
    try:
        registry = load_worker_registry(repo_root)
    except WorkerRegistryError as exc:
        raise WorkerRegistryUnavailable(f"worker_registry_unavailable:{exc}") from exc

    executions: list[str] = []
    opportunities: list[str] = []
    for department in departments:
        stage = INVESTMENT_DEPARTMENT_STAGE.get(department)
        if stage is None:
            raise ValueError(f"unknown_investment_department:{department}")
        for spec in workers_for_department(registry, department):
            executions.append(
                langfuse_worker_event_name(stage=stage, worker_id=spec.worker_id)
            )
            opportunities.append(
                langfuse_worker_opportunity_event_name(stage=stage, worker_id=spec.worker_id)
            )
    return tuple(executions), tuple(opportunities)


def _resolve_reader(
    reader: LangfuseTraceReader | None,
) -> tuple[LangfuseTraceReader | None, str | None]:
    """reader 를 확정하고, 못 만들었으면 **그 이유를 같이** 돌려준다(2026-08-27).

    네 집계가 각자 `except LangfuseQueryError: reader = None` 을 쓰고 있었고, 그
    except 절이 예외 메시지를 그 자리에서 버렸다. 그래서 자격증명 미설정
    (langfuse_credentials_missing)과 SDK 미설치(langfuse_not_installed)가 리포트
    상에서 똑같은 UNAVAILABLE 로 보였다 - 조치가 완전히 다른 두 상태인데도.
    """

    if reader is not None:
        return reader, None
    try:
        return LangfuseApiTraceReader(), None
    except LangfuseQueryError as exc:
        return None, str(exc) or _READER_UNAVAILABLE_REASON


class WindowedActivityReader(LangfuseTraceReader):
    """공용 fetch 층 - (event_name, 창) 하나당 Langfuse 왕복을 한 번만 낸다.

    2026-08-26 신설. 그 전까지 유휴·Capacity·LLM 사용량·발화율 네 집계가 각자
    reader 를 만들어 **같은 이벤트를 네 번** 읽었다. Worker 8명 기준 화면 1회당
    왕복 40회였고, 그중 Capacity 와 LLM 사용량은 event_name·since·limit 이 글자
    그대로 같은 질의였다(집계 축만 달랐다). 60초 폴링이라 그 값이 그대로 분당
    부하가 된다.

    이 클래스는 판정을 하지 않는다 - 네 집계 함수의 로직은 그대로 두고, 그들이
    부르는 reader 만 이걸로 바꾸면 중복 질의가 캐시에서 접힌다. 그래서 집계 로직과
    왕복 절약이 서로를 망가뜨리지 않는다.

    실패도 캐시한다: 죽은 Worker 하나를 네 집계가 각각 다시 물어보면 장애 때
    왕복이 원래대로 돌아간다.

    ⚠ 요청 수명(request-scoped)이다. 창(since)이 키에 들어가 있어 다음 폴링은 다른
      키가 되지만, 그렇다고 프로세스 수명으로 들고 있으면 관측값이 낡는다 -
      collect_workforce_observability() 가 호출마다 새로 만든다.
    """

    def __init__(self, inner: LangfuseTraceReader) -> None:
        self._inner = inner
        self._pages: dict[tuple[str, str], WorkerActivityPage] = {}
        self._counts: dict[tuple[str, str], int] = {}
        self._failures: dict[tuple[str, str], str] = {}
        # 실제로 나간 논리 질의 수. 테스트가 중복 제거를 관측하는 자리다
        # (tests/test_hr_shared_activity_reader.py). 배치 프리페치는 이름 수와
        # 무관하게 묶음당 1 로 센다 - Langfuse 왕복 수와 같은 뜻을 유지한다.
        self.queries = 0

    def prefetch(
        self,
        *,
        execution_names: tuple[str, ...],
        opportunity_names: tuple[str, ...],
        since: datetime,
        until: datetime | None = None,
    ) -> None:
        """창 하나에 필요한 것을 **묶음 2회**로 미리 채운다 (2026-08-27).

        도입 전에는 Langfuse Public API 분당 15 요청 상한(429 의
        `x-ratelimit-limit`)보다 많은 Worker별 조회가 나갔다. SDK가
        `Retry-After`만큼 대기하며 collect가 41~62초로 늘어난 사례가 있어,
        현재는 실행/미발화 조회를 각각 한 번의 배치로 줄여 한도 아래로 내린다.

        판정은 아무것도 안 한다 - 아래 네 집계 함수는 그대로 단건 메서드를 부르고,
        그 호출이 여기서 채운 캐시에 맞으면 왕복이 없다. 그래서 집계 로직과 왕복
        절약이 서로를 망가뜨리지 않는다(이 클래스 머리말과 같은 원칙).

        ▶ 실패는 삼키지 않고 **이름별 캐시에 사유로 남긴다.** 그래야 뒤이은 단건
          호출이 같은 LangfuseQueryError 로 떨어지고, 리포트가 UNAVAILABLE + 사유가
          된다. 여기서 예외를 올리면 배치 도입이 실패 모양 자체를 바꾼다.

        ▶ 실패했다고 단건 조회로 되돌아가지 않는다. 그 폴백은 정확히 우리가 없애려던
          16 요청이고, 429 상황에서 되살리면 상태가 더 나빠진다.
        """

        execution_names = tuple(dict.fromkeys(execution_names))
        opportunity_names = tuple(dict.fromkeys(opportunity_names))

        # ① 미발화 건수 - 건수만 쓰는 축이라 레코드를 안 끌어온다. 조건부 Worker
        #    하나가 하루 수백 건을 쌓을 수 있어서 이 차이가 크다.
        #
        #    실행 건수는 여기 안 담는다 - ②가 레코드를 끝까지 모으므로 그 길이가
        #    곧 정확한 건수다. 굳이 같이 물으면 배치를 못 하는 reader(테스트 대역)
        #    에서 왕복이 오히려 늘어난다.
        # ①과 ②는 서로 다른 read-only 요청이고 결과를 공유하지 않는다. 같은
        # Langfuse client의 네트워크 호출만 병렬화해 전체 GET 시간을 두 요청의
        # 합이 아니라 느린 요청 하나에 가깝게 제한한다. 결과를 캐시에 쓰는 일과
        # queries 카운트는 메인 스레드에서 수행해 reader 대역의 thread-safety
        # 가정을 넓히지 않는다.
        def fetch_opportunity_counts() -> dict[str, int]:
            batch_counter = getattr(
                self._inner, "count_many_worker_activity", None
            )
            if callable(batch_counter):
                return batch_counter(
                    event_names=opportunity_names, since=since, until=until,
                )
            # Compatibility path for older test/local readers that only
            # implement the original single-name interface.
            return {
                name: self._inner.count_worker_activity(
                    event_name=name, since=since,
                )
                for name in opportunity_names
            }

        def fetch_execution_pages() -> dict[str, WorkerActivityPage]:
            return self._inner.fetch_many_worker_activity(
                event_names=execution_names, since=since,
            )

        opportunity_future = None
        execution_future = None
        with ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="hr-observability"
        ) as executor:
            if opportunity_names:
                opportunity_future = executor.submit(fetch_opportunity_counts)
            if execution_names:
                execution_future = executor.submit(fetch_execution_pages)

            if opportunity_future is not None:
                try:
                    counts = opportunity_future.result()
                    self.queries += 1
                    for name, count in counts.items():
                        self._counts.setdefault(self._key(name, since), count)
                except LangfuseQueryError as exc:
                    for name in opportunity_names:
                        self._failures.setdefault(self._key(name, since), str(exc))

            # ② 실행 레코드 - 유휴·Capacity·LLM 사용량·Worker 사용량·발화율
            #    분자가 전부 이 한 묶음에서 나온다.
            if execution_future is None:
                return
            try:
                pages = execution_future.result()
                self.queries += 1
            except LangfuseQueryError as exc:
                for name in execution_names:
                    self._failures.setdefault(self._key(name, since), str(exc))
                return

        # 잘린 묶음은 이름별 건수를 못 믿는다 - 서버 meta 는 묶음 전체 합이고
        # len(records) 는 표본이다. 그때만 Metrics 로 정확한 건수를 따로 받는다
        # (잘린 것을 "그만큼만 있었다"로 바꾸지 않는다). 흔한 경로가 아니라
        # 왕복 하나를 더 쓰는 값이 있다.
        truncated_names = tuple(n for n, p in pages.items() if p.truncated)
        exact_counts: dict[str, int] = {}
        if truncated_names:
            try:
                exact_counts = self._inner.count_many_worker_activity(
                    event_names=truncated_names, since=since, until=until,
                )
                self.queries += 1
            except LangfuseQueryError:
                # 정확한 건수를 못 구했다 - 표본 길이를 건수로 승격시키지 않고
                # 그대로 둔다(truncated 플래그가 이미 그 사실을 들고 있다).
                exact_counts = {}

        for name, page in pages.items():
            key = self._key(name, since)
            exact = exact_counts.get(name)
            if exact is not None and exact != page.total_items:
                page = WorkerActivityPage(
                    records=page.records, total_items=exact, truncated=page.truncated,
                )
            self._pages.setdefault(key, page)
            self._counts.setdefault(key, page.total_items)

    @staticmethod
    def _key(event_name: str, since: datetime) -> tuple[str, str]:
        return (event_name, since.isoformat())

    def _raise_cached_failure(self, key: tuple[str, str]) -> None:
        cached = self._failures.get(key)
        if cached is not None:
            raise LangfuseQueryError(cached)

    def fetch_worker_activity(
        self, *, event_name: str, since: datetime, max_pages: int = MAX_ACTIVITY_PAGES
    ) -> WorkerActivityPage:
        key = self._key(event_name, since)
        self._raise_cached_failure(key)
        cached_page = self._pages.get(key)
        if cached_page is not None:
            return cached_page
        try:
            page = self._inner.fetch_worker_activity(
                event_name=event_name, since=since, max_pages=max_pages
            )
        except LangfuseQueryError as exc:
            self._failures[key] = str(exc)
            raise
        self.queries += 1
        self._pages[key] = page
        self._counts[key] = page.total_items
        return page

    def count_worker_activity(self, *, event_name: str, since: datetime) -> int:
        key = self._key(event_name, since)
        self._raise_cached_failure(key)
        cached_page = self._pages.get(key)
        if cached_page is not None:
            # 이미 레코드를 받아온 창이면 건수는 공짜다 - 발화율의 분자(실행 건수)가
            # 유휴·Capacity 와 같은 이벤트라서 여기서 왕복 하나가 통째로 사라진다.
            return cached_page.total_items
        cached_count = self._counts.get(key)
        if cached_count is not None:
            return cached_count
        try:
            count = self._inner.count_worker_activity(event_name=event_name, since=since)
        except LangfuseQueryError as exc:
            self._failures[key] = str(exc)
            raise
        self.queries += 1
        self._counts[key] = count
        return count

    def latest_event_timestamp(self, *, event_name: str, since: datetime) -> datetime | None:
        page = self.fetch_worker_activity(event_name=event_name, since=since)
        timestamps = [r.timestamp for r in page.records]
        return max(timestamps) if timestamps else None

    def list_worker_activity(
        self, *, event_name: str, since: datetime, limit: int = DEFAULT_ACTIVITY_PAGE_LIMIT
    ) -> list[WorkerActivityRecord]:
        return list(self.fetch_worker_activity(event_name=event_name, since=since).records)

    def count_events(
        self, *, event_name: str, since: datetime, limit: int = DEFAULT_ACTIVITY_PAGE_LIMIT
    ) -> int:
        return self.count_worker_activity(event_name=event_name, since=since)


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
        except Exception as exc:
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
    reader_unavailable_reason: str | None = None,
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

    # An explicitly supplied reason means the caller already attempted reader
    # construction and failed. Do not create a second live reader here: that
    # would turn a deterministic unavailable result into an unrelated query.
    if reader is None and reader_unavailable_reason:
        reader_reason = reader_unavailable_reason
    else:
        reader, reader_reason = _resolve_reader(reader)
        reader_reason = reader_unavailable_reason or reader_reason or _READER_UNAVAILABLE_REASON

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
                        reason=reader_reason,
                    )
                )
                continue
            event_name = langfuse_worker_event_name(stage=stage, worker_id=spec.worker_id)
            try:
                last_seen = reader.latest_event_timestamp(event_name=event_name, since=since)
            except LangfuseQueryError as exc:
                reports.append(
                    WorkerIdleReport(
                        department=department,
                        worker_id=spec.worker_id,
                        trigger=spec.trigger,
                        status=IdleStatus.UNAVAILABLE,
                        last_seen_at=None,
                        idle_hours=None,
                        reason=str(exc) or _READER_UNAVAILABLE_REASON,
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
    # 이 부서에 등록된 Worker 가 0명이다 - 질의를 낼 대상 자체가 없다(2026-08-27).
    #
    # ▶ 전에는 이것도 MEASURED/arrivals=0 이었다. specs 가 빈 튜플이면 조회 루프가
    #   한 번도 안 돌아 records 가 비고, 그대로 `arrivals == 0` 분기에 떨어졌기
    #   때문이다. 실측(2026-08-27): trading 은 등록 Worker 0명인데 응답에서 혼자
    #   MEASURED/0 으로 나왔고, 나머지 5개 부서가 UNAVAILABLE 인 화면에서 그 행만
    #   "관측됐고 부하가 없다"로 읽혔다. **"측정했더니 0"과 "잴 대상이 없다"는
    #   다른 사실이다** - 인원 조치 판단이 이 둘을 뒤집어 읽으면 없는 부서를
    #   한가하다고 결론짓는다.
    NO_WORKERS_REGISTERED = "NO_WORKERS_REGISTERED"


@dataclass(frozen=True)
class DepartmentCapacityReport:
    """부서 하나의 Langfuse 기반 Capacity 관측 한 건.

    `GET .../scorecard`는 DB Snapshot을 읽고, 이 리포트는 최신 진단용으로
    Langfuse를 직접 집계한다. 두 경로의 목적과 스키마가 다르므로 `cost.py`의
    `CapacitySnapshot`으로 강제하지 않는다 - 출처가 다른 두 값을 같은 타입으로
    섞으면 어느 쪽 계약을 따르는지 흐려진다.

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
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_reason(self.status, CapacityObservationStatus.UNAVAILABLE, self.reason)

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
            "reason": self.reason,
        }


def compute_department_capacity(
    *,
    department: str,
    reader: LangfuseTraceReader | None,
    since: datetime,
    now: datetime,
    repo_root: Path,
    reader_unavailable_reason: str | None = None,
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

    def _unavailable(reason: str) -> DepartmentCapacityReport:
        return DepartmentCapacityReport(
            department=department, window_start=since, window_end=now,
            status=CapacityObservationStatus.UNAVAILABLE,
            arrivals=None, duration_p95_ms=None, retry_rate=None, error_rate=None,
            utilization=None, reason=reason,
        )

    if reader is None:
        return _unavailable(reader_unavailable_reason or _READER_UNAVAILABLE_REASON)

    specs = tuple(workers_for_department(registry, department))
    # 등록 Worker 0명은 MEASURED/0 이 아니다 - 위 enum 주석 참고. 조회를 내기
    # **전에** 갈라야 한다(뒤에서 arrivals==0 으로 만나면 "측정했더니 0"과
    # 구분이 안 된다).
    if not specs:
        return DepartmentCapacityReport(
            department=department, window_start=since, window_end=now,
            status=CapacityObservationStatus.NO_WORKERS_REGISTERED,
            arrivals=None, duration_p95_ms=None, retry_rate=None, error_rate=None,
            utilization=None,
        )
    records: list[WorkerActivityRecord] = []
    try:
        for spec in specs:
            event_name = langfuse_worker_event_name(stage=stage, worker_id=spec.worker_id)
            records.extend(reader.list_worker_activity(event_name=event_name, since=since))
    except LangfuseQueryError as exc:
        return _unavailable(str(exc))

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
    reader_unavailable_reason: str | None = None,
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

    reader, resolved_reason = _resolve_reader(reader)
    reader_unavailable_reason = reader_unavailable_reason or resolved_reason

    return [
        compute_department_capacity(
            department=department, reader=reader, since=since, now=now, repo_root=repo_root,
            reader_unavailable_reason=reader_unavailable_reason,
        )
        for department in departments
    ]


# ── LLM 사용량 ─────────────────────────────────────────────────────────────────
#
# compute_department_capacity와 별도 함수로 두는 이유: capacity는 지연·재시도·
# 오류(latency_ms/retries/error_count)를 다루고, 여기는 모델·토큰·상태
# (llm_calls/model_name/prompt_tokens/completion_tokens/attempts/status)를
# 다룬다 - 같은 이벤트를 다시 읽지만 집계 목적이 달라 idle/capacity/fire-rate가
# 나뉜 것과 같은 이유로 분리한다.


class LlmUsageObservationStatus(str, Enum):
    MEASURED = "MEASURED"
    UNAVAILABLE = "UNAVAILABLE"
    # CapacityObservationStatus.NO_WORKERS_REGISTERED 와 같은 이유·같은 규약이다.
    NO_WORKERS_REGISTERED = "NO_WORKERS_REGISTERED"


@dataclass(frozen=True)
class DepartmentLlmUsageReport:
    """부서 하나의 Langfuse 기반 LLM 사용량 관측 한 건.

    llm_calls/prompt_tokens/completion_tokens는 begin_worker_metric() 컨텍스트가
    열려 있었던 이벤트에서만 나온다 - arrivals(관측된 이벤트 수) > 0이어도 이
    셋은 None일 수 있다(그 창의 실행이 전부 컨텍스트 밖이었을 경우). 0과 None을
    섞지 않는다(cost.py 불변식 3과 동일 원칙) - 그래서 "합산할 값이 하나도
    없었다"와 "합산했더니 0이었다"를 구분해 값이 있는 레코드가 하나도 없으면
    None을 돌려준다.
    """

    department: str
    window_start: datetime
    window_end: datetime
    status: LlmUsageObservationStatus
    arrivals: int | None
    llm_calls: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    avg_attempts: float | None
    status_counts: dict[str, int] | None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_reason(self.status, LlmUsageObservationStatus.UNAVAILABLE, self.reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "department": self.department,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "status": self.status.value,
            "arrivals": self.arrivals,
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "avg_attempts": self.avg_attempts,
            "status_counts": self.status_counts,
            "reason": self.reason,
        }


def compute_department_llm_usage(
    *,
    department: str,
    reader: LangfuseTraceReader | None,
    since: datetime,
    now: datetime,
    repo_root: Path,
    reader_unavailable_reason: str | None = None,
) -> DepartmentLlmUsageReport:
    """department 등록 Worker 전원의 실행 이벤트를 모아 LLM 사용량 하나로 합친다."""

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

    def _unavailable(reason: str) -> DepartmentLlmUsageReport:
        return DepartmentLlmUsageReport(
            department=department, window_start=since, window_end=now,
            status=LlmUsageObservationStatus.UNAVAILABLE,
            arrivals=None, llm_calls=None, prompt_tokens=None, completion_tokens=None,
            avg_attempts=None, status_counts=None, reason=reason,
        )

    if reader is None:
        return _unavailable(reader_unavailable_reason or _READER_UNAVAILABLE_REASON)

    specs = tuple(workers_for_department(registry, department))
    if not specs:
        # compute_department_capacity 와 같은 규약 - 잴 대상이 없는 것을 "재 봤더니
        # 0" 으로 바꾸지 않는다.
        return DepartmentLlmUsageReport(
            department=department, window_start=since, window_end=now,
            status=LlmUsageObservationStatus.NO_WORKERS_REGISTERED,
            arrivals=None, llm_calls=None, prompt_tokens=None, completion_tokens=None,
            avg_attempts=None, status_counts=None,
        )
    records: list[WorkerActivityRecord] = []
    try:
        for spec in specs:
            event_name = langfuse_worker_event_name(stage=stage, worker_id=spec.worker_id)
            records.extend(reader.list_worker_activity(event_name=event_name, since=since))
    except LangfuseQueryError as exc:
        return _unavailable(str(exc))

    arrivals = len(records)
    if arrivals == 0:
        return DepartmentLlmUsageReport(
            department=department, window_start=since, window_end=now,
            status=LlmUsageObservationStatus.MEASURED,
            arrivals=0, llm_calls=None, prompt_tokens=None, completion_tokens=None,
            avg_attempts=None, status_counts=None,
        )

    llm_calls = [r.llm_calls for r in records if r.llm_calls is not None]
    prompt_tokens = [r.prompt_tokens for r in records if r.prompt_tokens is not None]
    completion_tokens = [r.completion_tokens for r in records if r.completion_tokens is not None]
    attempts = [r.attempts for r in records if r.attempts is not None]
    statuses = [r.status for r in records if r.status is not None]

    status_counts: dict[str, int] | None = None
    if statuses:
        status_counts = {}
        for s in statuses:
            status_counts[s] = status_counts.get(s, 0) + 1

    return DepartmentLlmUsageReport(
        department=department, window_start=since, window_end=now,
        status=LlmUsageObservationStatus.MEASURED,
        arrivals=arrivals,
        llm_calls=sum(llm_calls) if llm_calls else None,
        prompt_tokens=sum(prompt_tokens) if prompt_tokens else None,
        completion_tokens=sum(completion_tokens) if completion_tokens else None,
        avg_attempts=(sum(attempts) / len(attempts)) if attempts else None,
        status_counts=status_counts,
    )


def check_department_llm_usage(
    *,
    reader: LangfuseTraceReader | None = None,
    departments: tuple[str, ...] = tuple(INVESTMENT_DEPARTMENT_STAGE),
    lookback_hours: float = 24.0,
    now: datetime | None = None,
    repo_root: Path = ROOT,
    reader_unavailable_reason: str | None = None,
) -> list[DepartmentLlmUsageReport]:
    """6개 투자본부(기본값) 전체의 LLM 사용량을 부서 단위로 하나씩 돌려준다.

    check_department_capacity()와 같은 실패 모드다 - reader를 못 만들거나 조회가
    실패하면 UNAVAILABLE로 접는다.
    """

    if lookback_hours <= 0:
        raise ValueError("lookback_hours 는 양수여야 한다")
    for department in departments:
        if department not in INVESTMENT_DEPARTMENT_STAGE:
            raise ValueError(f"unknown_investment_department:{department}")

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=lookback_hours)

    reader, resolved_reason = _resolve_reader(reader)
    reader_unavailable_reason = reader_unavailable_reason or resolved_reason

    return [
        compute_department_llm_usage(
            department=department, reader=reader, since=since, now=now, repo_root=repo_root,
            reader_unavailable_reason=reader_unavailable_reason,
        )
        for department in departments
    ]


# ── Worker 단위 사용량 ────────────────────────────────────────────────────────
#
# check_department_llm_usage 와 **같은 이벤트를 같은 캐시에서** 읽고 집계 축만
# 바꾼다(부서 합산 -> Worker 개별). 그래서 Langfuse 왕복이 늘지 않는다 -
# WindowedActivityReader 가 (event_name, 창) 단위로 이미 페이지를 들고 있다.
#
# 왜 Worker 단위가 따로 필요한가: workforce.cost_snapshots 는 agent_id 가 NOT NULL
# 이다(부서 단위로 적을 수 없다). 부서 합산만 있으면 그 테이블을 채울 수 없어서,
# Langfuse 관측과 DB Scorecard 가 영원히 이어지지 않는다.


class WorkerUsageObservationStatus(str, Enum):
    MEASURED = "MEASURED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class WorkerUsageReport:
    """Worker 한 명의 실행 건수·토큰·모델 관측 한 건.

    model_names 는 **비용 산정의 근거**다. 이 창에서 실제로 관측된 모델 이름만
    담는다 - 비어 있으면 모델을 못 읽은 것이지 "모델을 안 썼다"가 아니다.
    같은 Worker 가 창 안에서 모델을 갈아탈 수 있어(운영 AWQ / 개발 fallback)
    단수가 아니라 목록이다.

    prompt_tokens/completion_tokens/llm_calls 는 begin_worker_metric() 컨텍스트가
    열려 있었던 실행에서만 나온다 - arrivals > 0 이어도 None 일 수 있고, 그건
    "0 토큰"이 아니라 "그 창의 실행이 전부 계측 컨텍스트 밖이었다"는 뜻이다
    (cost.py 불변식 3).
    """

    department: str
    worker_id: str
    window_start: datetime
    window_end: datetime
    status: WorkerUsageObservationStatus
    arrivals: int | None
    llm_calls: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    model_names: tuple[str, ...]
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_reason(self.status, WorkerUsageObservationStatus.UNAVAILABLE, self.reason)
        if self.status is WorkerUsageObservationStatus.UNAVAILABLE and self.model_names:
            raise ValueError("UNAVAILABLE 인데 모델 이름이 관측됐다 - 모순")

    def as_dict(self) -> dict[str, Any]:
        return {
            "department": self.department,
            "worker_id": self.worker_id,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "status": self.status.value,
            "arrivals": self.arrivals,
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "model_names": list(self.model_names),
            "reason": self.reason,
        }


def compute_worker_usage(
    *, department: str, stage: str, spec: Any, reader: LangfuseTraceReader | None,
    since: datetime, now: datetime, reader_unavailable_reason: str | None = None,
) -> WorkerUsageReport:
    """Worker 한 명의 실행 이벤트를 토큰·모델 축으로 합친다."""

    def _unavailable(reason: str) -> WorkerUsageReport:
        return WorkerUsageReport(
            department=department, worker_id=spec.worker_id,
            window_start=since, window_end=now,
            status=WorkerUsageObservationStatus.UNAVAILABLE,
            arrivals=None, llm_calls=None, prompt_tokens=None, completion_tokens=None,
            model_names=(), reason=reason,
        )

    if reader is None:
        return _unavailable(reader_unavailable_reason or _READER_UNAVAILABLE_REASON)

    event_name = langfuse_worker_event_name(stage=stage, worker_id=spec.worker_id)
    try:
        records = reader.list_worker_activity(event_name=event_name, since=since)
    except LangfuseQueryError as exc:
        return _unavailable(str(exc))

    llm_calls = [r.llm_calls for r in records if r.llm_calls is not None]
    prompt_tokens = [r.prompt_tokens for r in records if r.prompt_tokens is not None]
    completion_tokens = [r.completion_tokens for r in records if r.completion_tokens is not None]
    # 이름 순 고정 - 같은 창을 다시 읽었을 때 목록 순서가 흔들리면 재보고가 변경으로
    # 보인다(멱등 재보고가 이 값을 metadata 로 싣는다).
    models = tuple(sorted({r.model_name for r in records if r.model_name}))

    return WorkerUsageReport(
        department=department, worker_id=spec.worker_id,
        window_start=since, window_end=now,
        status=WorkerUsageObservationStatus.MEASURED,
        arrivals=len(records),
        llm_calls=sum(llm_calls) if llm_calls else None,
        prompt_tokens=sum(prompt_tokens) if prompt_tokens else None,
        completion_tokens=sum(completion_tokens) if completion_tokens else None,
        model_names=models,
    )


def check_worker_usage(
    *,
    reader: LangfuseTraceReader | None = None,
    departments: tuple[str, ...] = tuple(INVESTMENT_DEPARTMENT_STAGE),
    lookback_hours: float = 24.0,
    now: datetime | None = None,
    repo_root: Path = ROOT,
    reader_unavailable_reason: str | None = None,
) -> list[WorkerUsageReport]:
    """6개 투자본부(기본값) 등록 Worker 전원의 토큰·모델 사용량을 개별로 돌려준다."""

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
    reader, resolved_reason = _resolve_reader(reader)
    reader_unavailable_reason = reader_unavailable_reason or resolved_reason

    reports: list[WorkerUsageReport] = []
    for department in departments:
        stage = INVESTMENT_DEPARTMENT_STAGE[department]
        for spec in workers_for_department(registry, department):
            reports.append(
                compute_worker_usage(
                    department=department, stage=stage, spec=spec, reader=reader,
                    since=since, now=now,
                    reader_unavailable_reason=reader_unavailable_reason,
                )
            )
    return reports


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
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_reason(self.status, TriggerRateObservationStatus.UNAVAILABLE, self.reason)

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
            "reason": self.reason,
        }


def compute_worker_trigger_rate(
    *, department: str, stage: str, spec: Any, reader: LangfuseTraceReader | None,
    since: datetime, now: datetime, reader_unavailable_reason: str | None = None,
) -> WorkerTriggerRateReport:
    """Worker 한 명의 실행/미발화 이벤트를 세어 발화율 하나로 합친다."""

    def _unavailable(reason: str) -> WorkerTriggerRateReport:
        return WorkerTriggerRateReport(
            department=department, worker_id=spec.worker_id, trigger=spec.trigger,
            window_start=since, window_end=now,
            status=TriggerRateObservationStatus.UNAVAILABLE,
            execution_count=None, opportunity_count=None, fire_rate=None, reason=reason,
        )

    if reader is None:
        return _unavailable(reader_unavailable_reason or _READER_UNAVAILABLE_REASON)

    execution_name = langfuse_worker_event_name(stage=stage, worker_id=spec.worker_id)
    opportunity_name = langfuse_worker_opportunity_event_name(stage=stage, worker_id=spec.worker_id)
    try:
        execution_count = reader.count_events(event_name=execution_name, since=since)
        opportunity_count = reader.count_events(event_name=opportunity_name, since=since)
    except LangfuseQueryError as exc:
        return _unavailable(str(exc))

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
    reader_unavailable_reason: str | None = None,
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

    reader, resolved_reason = _resolve_reader(reader)
    reader_unavailable_reason = reader_unavailable_reason or resolved_reason

    reports: list[WorkerTriggerRateReport] = []
    for department in departments:
        stage = INVESTMENT_DEPARTMENT_STAGE[department]
        for spec in workers_for_department(registry, department):
            reports.append(
                compute_worker_trigger_rate(
                    department=department, stage=stage, spec=spec, reader=reader,
                    since=since, now=now,
                    reader_unavailable_reason=reader_unavailable_reason,
                )
            )
    return reports


# ── 통합 관측 ─────────────────────────────────────────────────────────────────
#
# 네 집계(유휴·Capacity·LLM 사용량·발화율)를 **한 번의 호출로** 돌려주는 자리다.
# 각각을 따로 부르면 같은 Langfuse 이벤트를 네 번 읽는다 - WindowedActivityReader
# 머리말 참고. 판정 로직은 위 네 함수 그대로고, 여기서는 reader 하나를 공유시키는
# 것과 창(window)을 하나로 고정하는 일만 한다.


@dataclass(frozen=True)
class WorkforceObservability:
    """한 창에서 관측한 네 리포트 묶음. HR 통합 엔드포인트 응답 하나에 대응."""

    window_start: datetime
    window_end: datetime
    idle_agents: tuple[WorkerIdleReport, ...]
    capacity: tuple[DepartmentCapacityReport, ...]
    llm_usage: tuple[DepartmentLlmUsageReport, ...]
    # Worker 개별 토큰·모델. 부서 합산(llm_usage)과 같은 이벤트를 같은 캐시에서
    # 읽으므로 왕복이 늘지 않는다 - workforce.cost_snapshots 가 agent_id 를
    # NOT NULL 로 요구해서 부서 합산만으로는 그 테이블을 채울 수 없다.
    worker_usage: tuple[WorkerUsageReport, ...]
    trigger_rates: tuple[WorkerTriggerRateReport, ...]
    # 부서장 신원만 못 읽은 경우의 사유(Worker 판정은 정상) - 조용히 빼면 부서장이
    # "전부 정상"으로 읽힌다(list_idle_agents 머리말과 같은 이유).
    head_profiles_unavailable: str | None
    # 이 호출이 Langfuse 에 실제로 낸 논리 질의 수. 관측 자체를 관측한다 - 중복
    # 제거가 조용히 풀리면(예: 창이 어긋나 캐시 키가 갈라지면) 이 값이 먼저 는다.
    langfuse_queries: int
    # LLM Worker Registry와 분리된 deterministic services. In particular this
    # makes Trading's always-on desk runner visible without inventing token cost.
    runtime_services: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "idle_agents": [r.as_dict() for r in self.idle_agents],
            "capacity": [r.as_dict() for r in self.capacity],
            "llm_usage": [r.as_dict() for r in self.llm_usage],
            "worker_usage": [r.as_dict() for r in self.worker_usage],
            "trigger_rates": [r.as_dict() for r in self.trigger_rates],
            "langfuse_queries": self.langfuse_queries,
            "runtime_services": [dict(item) for item in self.runtime_services],
        }
        if self.head_profiles_unavailable:
            payload["head_profiles_unavailable"] = self.head_profiles_unavailable
        return payload


def collect_workforce_observability(
    *,
    reader: LangfuseTraceReader | None = None,
    departments: tuple[str, ...] = tuple(INVESTMENT_DEPARTMENT_STAGE),
    lookback_hours: float = 24.0,
    idle_threshold_hours: float = 4.0,
    now: datetime | None = None,
    repo_root: Path = ROOT,
    include_heads: bool = False,
) -> WorkforceObservability:
    """네 관측을 한 창·한 reader 로 모아 돌려준다.

    Worker 한 명당 Langfuse 왕복은 최대 2회다 - 실행 이벤트 1회(유휴·Capacity·
    LLM 사용량·발화율 분자가 전부 여기서 나온다) + 미발화 이벤트 건수 1회. 네
    엔드포인트를 따로 부르던 이전 구조는 같은 Worker 를 5회 물었다.

    reader 가 None 이면 check_idle_agents() 와 같은 규칙이다 - 자격증명이 없거나
    langfuse 가 없으면 네 리포트 전부 UNAVAILABLE 로 접힌다(개발 원칙 9).
    """

    if idle_threshold_hours <= 0:
        raise ValueError("idle_threshold_hours 는 양수여야 한다")
    if lookback_hours <= 0:
        raise ValueError("lookback_hours 는 양수여야 한다")

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=lookback_hours)

    reader, reader_unavailable_reason = _resolve_reader(reader)

    runtime_services: tuple[dict[str, str], ...] = ()
    if load_runtime_service_registry is not None:
        try:
            runtime_registry = load_runtime_service_registry(repo_root)
        except RuntimeServiceRegistryError:
            # The LLM registry remains the hard dependency for HR metrics. A
            # missing optional projection must not turn measured LLM data into
            # UNAVAILABLE; the image contract test catches missing packaging.
            runtime_services = ()
        else:
            runtime_services = tuple(
                {
                    "department": item.department,
                    "service_id": item.service_id,
                    "worker_id": item.worker_id,
                    "kind": item.kind,
                    "trigger": item.trigger,
                }
                for item in runtime_registry
            )

    # 창이 같아야 캐시 키가 같다. 그래서 now 를 여기서 한 번 고정해 네 집계에
    # 그대로 넘긴다 - 각자 datetime.now() 를 부르게 두면 since 가 미세하게 어긋나
    # 캐시가 통째로 빗나가고, 왕복이 조용히 원래대로 돌아간다.
    shared: LangfuseTraceReader | None = (
        WindowedActivityReader(reader) if reader is not None else None
    )
    if isinstance(shared, WindowedActivityReader):
        # 네 집계가 부를 이름을 미리 알아내 묶음 2회로 채운다. 실패해도 여기서
        # 예외가 나지 않는다 - 이름별 캐시에 사유가 남고 각 집계가 평소대로
        # UNAVAILABLE + 사유로 떨어진다(prefetch 머리말 참고).
        execution_names, opportunity_names = _worker_event_names(
            departments=departments, repo_root=repo_root
        )
        shared.prefetch(
            execution_names=execution_names,
            opportunity_names=opportunity_names,
            since=since,
            until=now,
        )
    common = {
        "reader": shared,
        "departments": departments,
        "lookback_hours": lookback_hours,
        "now": now,
        "repo_root": repo_root,
        # reader 를 못 만든 이유를 네 집계가 각자 다시 추측하지 않게 여기서 한 번
        # 정해 내려보낸다 - shared 가 None 이면 네 리포트가 같은 사유를 단다.
        "reader_unavailable_reason": reader_unavailable_reason,
    }

    head_profiles_unavailable: str | None = None
    try:
        idle = check_idle_agents(
            idle_threshold_hours=idle_threshold_hours, include_heads=include_heads, **common
        )
    except HeadProfilesUnavailable as exc:
        head_profiles_unavailable = str(exc)
        idle = check_idle_agents(
            idle_threshold_hours=idle_threshold_hours, include_heads=False, **common
        )

    capacity = check_department_capacity(**common)
    llm_usage = check_department_llm_usage(**common)
    worker_usage = check_worker_usage(**common)
    trigger_rates = check_worker_trigger_rates(**common)

    return WorkforceObservability(
        window_start=since,
        window_end=now,
        idle_agents=tuple(idle),
        capacity=tuple(capacity),
        llm_usage=tuple(llm_usage),
        worker_usage=tuple(worker_usage),
        trigger_rates=tuple(trigger_rates),
        head_profiles_unavailable=head_profiles_unavailable,
        langfuse_queries=shared.queries if isinstance(shared, WindowedActivityReader) else 0,
        runtime_services=runtime_services,
    )


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
        retry 1건, llm_calls 2/3/1, tokens 100+50/200+80/50+20, attempts 1/2/1,
        status COMPLETED/DEGRADED/COMPLETED)을 돌려주는 대역 - Worker 수와
        무관하게 집계값을 손으로 검산할 수 있게 고정한다."""

        def list_worker_activity(self, *, event_name: str, since: datetime, limit: int = 200):
            return [
                WorkerActivityRecord(
                    timestamp=now, latency_ms=100, error_count=0, retries=0,
                    attempts=1, status="COMPLETED", llm_calls=2,
                    model_name="qwen2.5-14b-instruct-awq", prompt_tokens=100, completion_tokens=50,
                ),
                WorkerActivityRecord(
                    timestamp=now, latency_ms=200, error_count=1, retries=1,
                    attempts=2, status="DEGRADED", llm_calls=3,
                    model_name="qwen2.5-14b-instruct-awq", prompt_tokens=200, completion_tokens=80,
                ),
                WorkerActivityRecord(
                    timestamp=now, latency_ms=900, error_count=0, retries=0,
                    attempts=1, status="COMPLETED", llm_calls=1,
                    model_name="qwen2.5-14b-instruct-awq", prompt_tokens=50, completion_tokens=20,
                ),
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

    # ── LLM 사용량(2026-08-25) ──────────────────────────────────────────────

    usage_reports = check_department_llm_usage(
        reader=_FixedActivityReader(), departments=("research",), lookback_hours=1.0, now=now,
    )
    assert len(usage_reports) == 1
    usage = usage_reports[0]
    assert usage.status is LlmUsageObservationStatus.MEASURED, usage
    assert usage.arrivals == 3 * n_research_workers, (usage.arrivals, n_research_workers)
    assert usage.llm_calls == 6 * n_research_workers, usage.llm_calls  # 2+3+1
    assert usage.prompt_tokens == 350 * n_research_workers, usage.prompt_tokens  # 100+200+50
    assert usage.completion_tokens == 150 * n_research_workers, usage.completion_tokens  # 50+80+20
    assert abs(usage.avg_attempts - (4 / 3)) < 1e-9, usage.avg_attempts  # (1+2+1)/3
    assert usage.status_counts == {"COMPLETED": 2 * n_research_workers, "DEGRADED": n_research_workers}, (
        usage.status_counts
    )
    print(f"  LLM 사용량 집계(llm_calls/tokens/avg_attempts/status_counts) - OK ({usage.arrivals}건)")

    # arrivals=0이면 MEASURED이되 나머지는 전부 None - "측정했더니 0"과 "잴 값이 없었다"를 구분.
    empty_usage = check_department_llm_usage(
        reader=_EmptyActivityReader(), departments=("qa",), lookback_hours=1.0, now=now,
    )
    assert empty_usage[0].status is LlmUsageObservationStatus.MEASURED
    assert empty_usage[0].arrivals == 0
    assert empty_usage[0].llm_calls is None and empty_usage[0].status_counts is None
    print("  arrivals=0 -> MEASURED(0건), 나머지 None - OK")

    # reader=None(자격증명 없음)이면 전부 UNAVAILABLE.
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    unavailable_usage = check_department_llm_usage(departments=("qa",), now=now)
    assert unavailable_usage[0].status is LlmUsageObservationStatus.UNAVAILABLE
    assert unavailable_usage[0].llm_calls is None
    print("  자격증명 없음 -> LLM 사용량 전부 UNAVAILABLE - OK")

    print("LLM 사용량(Langfuse 기반) 자체 점검 통과.")

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

    # ── 통합 관측(2026-08-26) ────────────────────────────────────────────────
    #
    # 여기서 세는 것은 판정이 아니라 **왕복 수**다. 네 집계가 다시 각자 조회하게
    # 회귀해도 값은 전부 맞게 나오고 화면도 정상이라, 왕복을 직접 세지 않으면
    # 아무도 모른다.
    class _CountingReader(LangfuseTraceReader):
        def __init__(self) -> None:
            self.fetches: list[str] = []
            self.counts: list[str] = []

        def fetch_worker_activity(
            self, *, event_name: str, since: datetime, max_pages: int = MAX_ACTIVITY_PAGES
        ) -> WorkerActivityPage:
            self.fetches.append(event_name)
            record = WorkerActivityRecord(
                timestamp=since + timedelta(hours=1),
                latency_ms=900, error_count=0, retries=0, attempts=1, status="SUCCESS",
                llm_calls=1, model_name="qwen2.5-14b-instruct-awq",
                prompt_tokens=500, completion_tokens=80,
            )
            return WorkerActivityPage(records=(record,), total_items=1, truncated=False)

        def count_worker_activity(self, *, event_name: str, since: datetime) -> int:
            self.counts.append(event_name)
            return 2

    counting = _CountingReader()
    merged = collect_workforce_observability(reader=counting, departments=("research",), now=now)
    research_workers = len(
        [r for r in check_idle_agents(reader=_NoneReader(), departments=("research",), now=now)]
    )
    assert len(counting.fetches) == research_workers, counting.fetches
    assert len(set(counting.fetches)) == research_workers, "같은 실행 이벤트를 두 번 읽었다"
    assert len(counting.counts) == research_workers, counting.counts
    # ▶ 2026-08-27: 왕복이 Worker 수에 비례하지 않는다. 프리페치가 실행 레코드
    #   묶음 1회 + 미발화 건수 묶음 1회로 창 하나를 통째로 채우기 때문이다.
    #   Langfuse 는 분당 15 요청 상한이라, Worker 가 늘 때마다 왕복이 같이 늘면
    #   8명에서 이미 한도를 넘는다(16 > 15) - 그게 실측 41~62초 스톨의 원인이었다.
    assert merged.langfuse_queries == 2, merged.langfuse_queries
    print(f"  왕복이 Worker 수와 무관하게 2회 - OK ({research_workers}명)")

    assert merged.idle_agents and merged.capacity and merged.llm_usage and merged.trigger_rates
    assert merged.window_start < merged.window_end
    print("  네 리포트가 한 응답에 - OK")

    # 캐시가 창을 무시하면 낡은 값이 섞인다.
    windowed = WindowedActivityReader(_CountingReader())
    _name = langfuse_worker_event_name(stage="research", worker_id="w")
    windowed.fetch_worker_activity(event_name=_name, since=now - timedelta(hours=24))
    windowed.fetch_worker_activity(event_name=_name, since=now - timedelta(hours=24))
    assert windowed.queries == 1, "같은 창을 두 번 조회했다"
    windowed.fetch_worker_activity(event_name=_name, since=now - timedelta(hours=168))
    assert windowed.queries == 2, "다른 창인데 캐시가 재사용됐다"
    print("  같은 창은 1회, 다른 창은 별도 조회 - OK")

    print("통합 관측(공용 fetch 층) 자체 점검 통과.")

    # ── 2026-08-27 배선 사고 회귀 방지 ────────────────────────────────────────
    #
    # 아래 셋은 전부 "조용히 틀린" 상태를 붙잡는 검사다. 세 결함 모두 예외도 로그도
    # 남기지 않았고, 리포트는 매번 200 OK 로 그럴듯하게 응답했다.

    # ① limit 상한. 이 값이 100 을 넘으면 Langfuse 가 400 을 돌려주고 네 리포트가
    #   전부 UNAVAILABLE 이 된다 - 대역(_FakeTraceApi)은 limit 을 검사하지 않으므로
    #   상수 자체를 여기서 못 박는다.
    assert DEFAULT_ACTIVITY_PAGE_LIMIT <= LANGFUSE_MAX_PAGE_LIMIT, (
        f"limit {DEFAULT_ACTIVITY_PAGE_LIMIT} > Langfuse 상한 {LANGFUSE_MAX_PAGE_LIMIT} "
        "- 서버가 400(too_big)을 돌려주고 관측이 전부 UNAVAILABLE 로 접힌다"
    )
    assert DEFAULT_ACTIVITY_PAGE_LIMIT * MAX_ACTIVITY_PAGES >= 2000, (
        "창당 수집 상한이 줄었다 - 페이지를 작게 만들었으면 페이지 수로 보전해야 한다"
    )
    print("  limit 이 Langfuse 상한(100) 이내 - OK")

    # ② 사유 없는 UNAVAILABLE 은 만들 수 없다.
    try:
        WorkerIdleReport(
            department="research", worker_id="w", trigger="t",
            status=IdleStatus.UNAVAILABLE, last_seen_at=None, idle_hours=None,
        )
        raise AssertionError("사유 없는 UNAVAILABLE 이 통과했다")
    except ValueError:
        pass
    # 관측된 상태는 반대로 사유를 못 가진다(사유가 붙으면 실패로 오독된다).
    try:
        WorkerIdleReport(
            department="research", worker_id="w", trigger="t",
            status=IdleStatus.UNOBSERVED, last_seen_at=None, idle_hours=None,
            reason="아무거나",
        )
        raise AssertionError("관측된 상태에 사유가 붙었는데 통과했다")
    except ValueError:
        pass
    print("  UNAVAILABLE 은 사유 필수, 관측된 상태는 사유 금지 - OK")

    # ③ HTTP 400 본문이 사유 문자열까지 살아 온다. 실제 사고 응답을 픽스처로 쓴다 -
    #   가짜 문구로 만들면 이 검사가 자기가 막아야 할 사고를 못 잡는다.
    class _ApiError(Exception):
        status_code = 400
        body: ClassVar[dict[str, Any]] = {
            "message": "Invalid request data",
            "error": [{"code": "too_big", "maximum": 100, "path": ["limit"],
                       "message": "Too big: expected number to be <=100"}],
        }

    _reason = _query_failure_reason(_ApiError())
    assert "http_400" in _reason, _reason
    assert "<=100" in _reason, _reason
    assert len(_reason) < 400, "사유가 길어 표를 무너뜨린다"
    print(f"  조회 실패 사유에 HTTP 상태·본문이 실린다 - OK ({_reason[:70]}…)")

    # ④ 등록 Worker 0명은 MEASURED/0 이 아니다. trading 은 실제로 0명이다.
    _cap = compute_department_capacity(
        department="trading", reader=_NoneReader(), since=now - timedelta(hours=24),
        now=now, repo_root=ROOT,
    )
    assert _cap.status is CapacityObservationStatus.NO_WORKERS_REGISTERED, _cap.status
    assert _cap.arrivals is None, "잴 대상이 없는데 arrivals 에 숫자가 들어갔다"
    _usage = compute_department_llm_usage(
        department="trading", reader=_NoneReader(), since=now - timedelta(hours=24),
        now=now, repo_root=ROOT,
    )
    assert _usage.status is LlmUsageObservationStatus.NO_WORKERS_REGISTERED, _usage.status
    print("  Worker 0명 부서는 NO_WORKERS_REGISTERED - OK")

    # ⑤ reader 를 못 만든 사유가 리포트까지 내려온다.
    _idle = check_idle_agents(
        reader=None, departments=("research",), now=now,
        reader_unavailable_reason="langfuse_credentials_missing",
    )
    assert _idle and all(r.status is IdleStatus.UNAVAILABLE for r in _idle)
    assert all(r.reason == "langfuse_credentials_missing" for r in _idle), [r.reason for r in _idle]
    print("  reader 생성 실패 사유가 행마다 실린다 - OK")

    print("배선 사고 회귀 방지 자체 점검 통과.")
