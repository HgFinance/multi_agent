"""Credential-free LLM performance metrics and redacted LangSmith/Langfuse tracing."""

from __future__ import annotations

import contextlib
import contextvars
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any


@dataclass
class WorkerMetric:
    worker_id: str
    role: str
    stage: str
    model_name: str
    started_at: float = field(default_factory=time.perf_counter)
    llm_calls: int = 0
    retries: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    errors: int = 0

    def as_dict(self, *, status: str, attempts: int, eval_score: float | None) -> dict[str, Any]:
        return {
            "schema_version": "llm.performance.v1",
            "worker_id": self.worker_id,
            "role": self.role,
            "stage": self.stage,
            "model_name": self.model_name,
            "status": status,
            "attempts": attempts,
            "llm_calls": self.llm_calls,
            "retries": max(attempts - 1, 0),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_ms": self.latency_ms or int((time.perf_counter() - self.started_at) * 1000),
            "eval_score": eval_score,
            "error_count": self.errors,
            "raw_payloads_sent": False,
        }


_CURRENT_METRIC: contextvars.ContextVar[WorkerMetric | None] = contextvars.ContextVar(
    "current_worker_metric", default=None
)


def begin_worker_metric(*, worker_id: str, role: str, stage: str, model_name: str) -> contextvars.Token:
    return _CURRENT_METRIC.set(WorkerMetric(worker_id, role, stage, model_name))


def end_worker_metric(token: contextvars.Token, *, status: str, attempts: int, eval_score: float | None) -> dict[str, Any]:
    metric = _CURRENT_METRIC.get()
    try:
        return metric.as_dict(status=status, attempts=attempts, eval_score=eval_score) if metric else {
            "schema_version": "llm.performance.v1",
            "status": status,
            "attempts": attempts,
            "raw_payloads_sent": False,
        }
    finally:
        _CURRENT_METRIC.reset(token)


def record_llm_call(*, usage: Any = None, latency_ms: int = 0, error: bool = False) -> None:
    metric = _CURRENT_METRIC.get()
    if metric is None:
        return
    metric.llm_calls += 1
    metric.latency_ms += max(int(latency_ms), 0)
    metric.errors += int(error)
    if usage is None:
        return
    for target, names in (
        ("prompt_tokens", ("prompt_tokens", "input_tokens")),
        ("completion_tokens", ("completion_tokens", "output_tokens")),
    ):
        value = next((getattr(usage, name, None) for name in names if getattr(usage, name, None) is not None), None)
        if value is not None:
            setattr(metric, target, int(value) + int(getattr(metric, target) or 0))


def langsmith_enabled() -> bool:
    tracing = os.getenv("LANGCHAIN_TRACING_V2", os.getenv("LANGSMITH_TRACING", ""))
    return tracing.casefold() in {"1", "true", "yes", "on"} and bool(os.getenv("LANGSMITH_API_KEY", "").strip())


def _metric_metadata(metric: dict[str, Any], *, trace_id: str | None = None) -> dict[str, Any]:
    allowed = {
        "schema_version", "worker_id", "role", "stage", "model_name", "status",
        "attempts", "llm_calls", "retries", "prompt_tokens", "completion_tokens",
        "latency_ms", "eval_score", "error_count", "raw_payloads_sent",
    }
    result = {key: metric[key] for key in allowed if key in metric}
    if trace_id:
        result["trace_id"] = trace_id
    return result


@lru_cache(maxsize=1)
def _safe_langsmith_client() -> Any:
    from langsmith import Client

    return Client(hide_inputs=True, hide_outputs=True, hide_metadata=False)


@contextlib.contextmanager
def redacted_trace(*, trace_id: str, model_name: str, stage: str) -> Iterator[None]:
    """Make every nested LangChain/LangGraph run redacted for this pipeline."""

    if not langsmith_enabled():
        yield
        return
    from langsmith import tracing_context

    metadata = {
        "observability_schema": "llm.performance.v1",
        "raw_payloads_sent": False,
        "model_name": model_name,
        "stage": stage,
    }
    with tracing_context(
        client=_safe_langsmith_client(),
        project_name=os.getenv("LANGCHAIN_PROJECT") or os.getenv("LANGSMITH_PROJECT"),
        tags=["hgfinance", "redacted", f"stage:{stage}"],
        metadata=metadata,
        enabled=True,
    ):
        yield


def publish_metric(metric: dict[str, Any], *, trace_id: str | None = None) -> bool:
    """Send an empty-payload metric run; never send prompt or completion text."""

    if not langsmith_enabled():
        return False
    try:
        client = _safe_langsmith_client()
        safe = _metric_metadata(metric, trace_id=trace_id)
        client.create_run(
            name="llm.performance.metric",
            run_type="chain",
            inputs={},
            outputs={},
            project_name=os.getenv("LANGCHAIN_PROJECT") or os.getenv("LANGSMITH_PROJECT"),
            tags=["hgfinance", "metric", "redacted", f"worker:{metric.get('worker_id', 'unknown')}"],
            extra={"metadata": safe},
            hide_inputs=True,
            hide_outputs=True,
        )
        return True
    except Exception:
        return False


def langfuse_worker_event_name(*, stage: str, worker_id: str) -> str:
    """Single source of truth for the event name both sides key off of.

    Write 측(publish_langfuse_metric)과 read 측(HR observability.py)이 각자
    문자열을 조립하면 접두어가 드리프트해도 아무도 모른다 - 여기 하나로 강제한다.
    """

    return f"llm.performance.metric:{stage}:{worker_id}"


def langfuse_enabled() -> bool:
    """2026-08-10: HR(07-agent-workforce) 유휴 Agent 관측용으로 신규 도입.

    LangSmith(langsmith_enabled 위)와 나란히 켜는 이중 계측이다 - 기존 LangSmith
    파이프라인은 그대로 두고, 여기 추가한 것만으로 HR 이 읽을 계측을 늘린다.
    """

    tracing = os.getenv("LANGFUSE_TRACING", "")
    return tracing.casefold() in {"1", "true", "yes", "on"} and bool(
        os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    ) and bool(os.getenv("LANGFUSE_SECRET_KEY", "").strip())


@lru_cache(maxsize=1)
def _safe_langfuse_client() -> Any:
    from langfuse import Langfuse

    return Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        host=os.getenv("LANGFUSE_HOST") or "https://cloud.langfuse.com",
    )


def publish_langfuse_metric(
    metric: dict[str, Any], *, trace_id: str | None = None, flush: bool = False
) -> bool:
    """Send a redacted metric event; never send prompt or completion text.

    LangSmith 의 publish_metric() 과 같은 계약이다 - _metric_metadata() 로 허용된
    필드만 내보내고 input/output 은 항상 비운다. HR 이 이 이벤트를 읽을 때도 timestamp/
    tags 만 보고 원문을 보지 않는다는 전제가 이 redaction 에 달려 있다(.env.example
    3-2절 - compliance-policy-agent Trace 에 Mandate/제한종목 내용이 그대로 담긴다).

    우리 쪽 trace_id 를 Langfuse trace_context 에 강제하지 않는다 - 우리 trace_id 는
    Langfuse 가 기대하는 hex 형식이 아닐 수 있어 API 오류를 유발할 수 있다. LangSmith
    와 동일하게 metadata 안에 값으로만 넣는다(_metric_metadata 가 이미 처리).

    반환값의 의미가 LangSmith 쪽과 다르다 - 실측 확인(2026-08-10, 도달 불가능한
    host 로 재현): langfuse 4.x 는 OTel 배치 Exporter 라 create_event() 가 큐잉만
    하고 즉시 반환한다. 네트워크 실패는 백그라운드 스레드에서 나중에 일어나고
    flush() 도 예외를 던지지 않는다(stderr 에 경고만 찍힘). 즉 이 함수의 True 는
    "서버가 받았다"가 아니라 "로컬 큐잉에 성공했다"는 뜻이다 - LangSmith 의
    create_run() 처럼 동기 왕복을 확인한 게 아니다. HR 의 관측이 최종적으로
    맞는지는 read 측(scorecard/observability.py)이 실제 조회로 다시 검증한다.
    """

    if not langfuse_enabled():
        return False
    try:
        client = _safe_langfuse_client()
        safe = _metric_metadata(metric, trace_id=trace_id)
        stage = str(metric.get("stage", "unknown"))
        worker_id = str(metric.get("worker_id", "unknown"))
        # create_event() 에는 LangSmith create_run() 의 tags= 에 대응하는 파라미터가
        # 없다(langfuse 4.14.3 실측 확인). worker 별 최근 활동 시각을 서버에서 걸러
        # 조회하려면 event.name 자체를 부서·worker 단위로 유일하게 만드는 수밖에 없다.
        client.create_event(
            name=langfuse_worker_event_name(stage=stage, worker_id=worker_id),
            input=None,
            output=None,
            metadata=safe,
            level="ERROR" if metric.get("error_count", 0) else "DEFAULT",
        )
        # ▶ 2026-08-20: 매 호출 flush 를 걷어냈다(기본 flush=False).
        #   실측: flush 포함 중앙값 85.8ms / 최대 233.8ms (JP 리전 왕복), 큐잉만
        #   0.117ms. Worker 하나당 85ms 는 8명 파이프라인에서 0.7초이고, 공용
        #   런타임의 async fan-out 안에서 부르면 blocking flush 가 이벤트 루프를
        #   막아 병렬 Worker 를 직렬화한다 - 계측이 로직 성능을 바꾸는 순간이다.
        #
        #   유실 우려는 SDK 가 이미 처리한다(실측: langfuse 4.14.3
        #   _client/resource_manager.py 278행 `atexit.register(self.shutdown)`).
        #   프로세스가 정상 종료하면 종료 훅이 큐를 비운다. 강제 종료(SIGKILL)에서
        #   마지막 몇 초가 유실될 수 있는데, 유휴 판정은 "마지막 관측 시각"이라
        #   그 손실이 판정을 뒤집지 않는다(다음 실행이 다시 최신 시각을 쓴다).
        #
        #   그래도 동기 확인이 필요한 호출자(단발 스크립트 등)는 flush=True 를 준다.
        if flush:
            with contextlib.suppress(Exception):
                client.flush()
        return True
    except Exception:
        return False


def publish_worker_activity(
    *,
    stage: str,
    worker_id: str,
    role: str = "",
    status: str = "COMPLETED",
    attempts: int = 0,
    latency_ms: int = 0,
    error_count: int = 0,
    trace_id: str | None = None,
) -> bool:
    """Worker 실행 한 건을 HR 유휴 관측용으로 기록한다(2026-08-20).

    publish_langfuse_metric() 이 요구하는 metric dict 를 실행기마다 손으로 조립하면
    필드 이름이 갈린다 - 여기 하나로 모은다. 보내는 값은 _metric_metadata() 의
    허용 목록을 그대로 따르므로 input/output 은 여전히 나가지 않는다.

    ▶ 왜 실행기마다 불러야 하는가: Worker 실행 경로가 셋이다 -
      (1) orchestration/workflows/portfolio_recommendation.py 자체 실행기,
      (2) departments/employee_worker_runtime.py 공용 registry 실행기,
      (3) Risk/QA 자체 실행기(risk_employee_workers.py, qa_employee_workers.py).
      2026-08-10 도입 당시엔 (1) 하나가 전부라고 봤지만 2026-08-13 에 본부장이
      자기 Worker 를 직접 돌리는 MCP 간선이 생기면서 (2)(3) 이 계측 밖으로
      빠졌다. 그러면 **실제로 일한 Worker 가 HR 리포트에 IDLE 로 뜬다** -
      유휴 리포트에서 가장 위험한 종류의 오차다(정리 대상으로 오판된다).
    """

    # ▶ 모르는 값을 0/"" 으로 채우지 않는다(2026-08-20 수정). 이 경로에는
    #   begin_worker_metric() 컨텍스트가 없어 llm_calls·model_name·토큰수를 셀
    #   방법이 자체가 없다 - 그런데 llm_calls: 0 을 보내면 "모델을 한 번도 안
    #   불렀다"는 **관측 사실**로 읽힌다. 실측(2026-08-20): 모델 엔드포인트가 없는
    #   컨테이너에서 DEGRADED 로 끝난 Worker 가 llm_calls 0 을 달고 나갔는데,
    #   그 0 은 실패의 증거가 아니라 우리가 안 센 결과였다. quality.py
    #   aggregate_quality() 의 None/0 구분과 같은 원칙 - 필드를 아예 뺀다.
    return publish_langfuse_metric(
        {
            "schema_version": "llm.performance.v1",
            "worker_id": worker_id,
            "role": role,
            "stage": stage,
            "status": status,
            "attempts": int(attempts),
            "latency_ms": int(latency_ms),
            "error_count": int(error_count),
            "raw_payloads_sent": False,
        },
        trace_id=trace_id,
    )


def metric_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
