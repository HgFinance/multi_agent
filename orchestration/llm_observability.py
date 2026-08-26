"""Credential-free LLM performance metrics and redacted LangSmith/Langfuse tracing."""

from __future__ import annotations

import contextlib
import contextvars
import os
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4


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

    def as_dict(
        self, *, status: str, attempts: int, eval_score: float | None
    ) -> dict[str, Any]:
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
            "latency_ms": self.latency_ms
            or int((time.perf_counter() - self.started_at) * 1000),
            "eval_score": eval_score,
            "error_count": self.errors,
            "raw_payloads_sent": False,
        }


_CURRENT_METRIC: contextvars.ContextVar[WorkerMetric | None] = contextvars.ContextVar(
    "current_worker_metric", default=None
)


def begin_worker_metric(
    *, worker_id: str, role: str, stage: str, model_name: str
) -> contextvars.Token:
    return _CURRENT_METRIC.set(WorkerMetric(worker_id, role, stage, model_name))


def end_worker_metric(
    token: contextvars.Token, *, status: str, attempts: int, eval_score: float | None
) -> dict[str, Any]:
    metric = _CURRENT_METRIC.get()
    try:
        return (
            metric.as_dict(status=status, attempts=attempts, eval_score=eval_score)
            if metric
            else {
                "schema_version": "llm.performance.v1",
                "status": status,
                "attempts": attempts,
                "raw_payloads_sent": False,
            }
        )
    finally:
        _CURRENT_METRIC.reset(token)


def record_llm_call(
    *, usage: Any = None, latency_ms: int = 0, error: bool = False
) -> None:
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
        value = next(
            (
                getattr(usage, name, None)
                for name in names
                if getattr(usage, name, None) is not None
            ),
            None,
        )
        if value is not None:
            setattr(metric, target, int(value) + int(getattr(metric, target) or 0))


def langsmith_enabled() -> bool:
    tracing = os.getenv("LANGSMITH_TRACING", "")
    return tracing.casefold() in {"1", "true", "yes", "on"} and bool(
        os.getenv("LANGSMITH_API_KEY", "").strip()
    )


def langsmith_project(
    scope: Literal["workflow", "metrics", "evals"] = "workflow",
) -> str | None:
    """Return the single configured LangSmith project for an observability scope.

    Workflow/root/worker traces stay together in ``LANGSMITH_PROJECT`` so QA can
    inspect one request tree.  High-frequency empty-payload performance events
    use a separate project and never change the existing producer call sites.
    Evaluation producers may opt into the reserved evals project without
    reusing the production project.
    """

    if scope == "metrics":
        return os.getenv("LANGSMITH_METRICS_PROJECT", "").strip() or "HgFinance-Metrics"
    if scope == "evals":
        return os.getenv("LANGSMITH_EVALS_PROJECT", "").strip() or "HgFinance-Evals"
    return os.getenv("LANGSMITH_PROJECT", "").strip() or None


def _metric_metadata(
    metric: dict[str, Any], *, trace_id: str | None = None
) -> dict[str, Any]:
    allowed = {
        "schema_version",
        "worker_id",
        "role",
        "stage",
        "model_name",
        "status",
        "attempts",
        "llm_calls",
        "retries",
        "prompt_tokens",
        "completion_tokens",
        # source: 같은 Agent 의 활동이라도 **어느 경로로 관측됐는지**가 다르다
        # (2026-08-20). bff_ask=BFF 가 직접 CLI 를 띄운 턴, kanban_card=CEO
        # 워크플로 카드가 끝난 것. 원인 추적이 안 되면 "왜 이 이벤트가 났지"에
        # 답할 수 없다. 값은 우리가 정한 고정 문자열이라 payload 유출이 아니다.
        "source",
        "request_id",
        "root_id",
        "task_id",
        "department",
        "workflow_role",
        "workflow_mode",
        "provider",
        "trace_kind",
        "latency_scope",
        "latency_ms",
        "eval_score",
        "error_count",
        "raw_payloads_sent",
        "trace_id",
        "semantic_qa_version",
        "semantic_qa_evaluator",
        "semantic_qa_verdict",
        "semantic_qa_score",
        "semantic_qa_completeness",
        "semantic_qa_groundedness",
        "semantic_qa_temporal_consistency",
        "semantic_qa_uncertainty_honesty",
        "semantic_qa_relevance",
        "semantic_qa_finding_count",
        "semantic_qa_finding_codes",
    }
    result = {key: metric[key] for key in allowed if key in metric}
    if trace_id:
        result["trace_id"] = trace_id
    return result


def trace_correlation_metadata(
    payload: Mapping[str, Any] | None = None,
    *,
    input_hash: str | None = None,
    request_id: str | None = None,
    root_id: str | None = None,
    task_id: str | None = None,
    trace_id: str | None = None,
    case_id: str | None = None,
) -> dict[str, str]:
    """Build one bounded correlation envelope for every worker trace.

    Department runtimes historically each derived ``case_id``/``task_id`` /
    ``trace_id`` for their worker-context output, while the LangGraph run was
    created with none of those values.  Keep the derivation in one place and
    use deterministic local fallbacks for legacy callers.  The fallback IDs
    are hashes or existing opaque IDs; business payloads are never copied.
    """

    source = payload if isinstance(payload, Mapping) else {}

    def _text(value: Any, limit: int = 160) -> str:
        return " ".join(str(value or "").split())[:limit]

    artifact = source.get("artifact")
    artifact_trace = (
        artifact.get("trace_id")
        if isinstance(artifact, Mapping)
        else getattr(artifact, "trace_id", None)
    )
    resolved_trace = _text(
        trace_id
        or artifact_trace
        or source.get("trace_id")
        or source.get("event_id")
        or source.get("correlation_id")
        or request_id
        or root_id
        or case_id
    )
    if not resolved_trace:
        stable_hash = _text(input_hash, 32)[:24]
        resolved_trace = (
            f"local:{stable_hash}" if stable_hash else f"local:{uuid4().hex}"
        )

    resolved_request = _text(
        request_id
        or source.get("request_id")
        or source.get("client_request_id")
        or source.get("correlation_id")
        or resolved_trace
    )
    resolved_root = _text(
        root_id
        or source.get("root_id")
        or source.get("root_task_id")
        or source.get("workflow_root_task_id")
        or case_id
        or source.get("case_id")
        or source.get("mandate_id")
        or resolved_request
    )
    resolved_task = _text(
        task_id
        or source.get("task_id")
        or source.get("worker_task_id")
        or f"{resolved_root}-task"
    )
    return {
        "request_id": resolved_request,
        "root_id": resolved_root,
        "task_id": resolved_task,
        "trace_id": resolved_trace,
    }


def worker_graph_trace_config(
    *,
    stage: str,
    worker_id: str,
    role: str = "",
    correlation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """LangGraph ``invoke()``/``ainvoke()`` config that tags a Worker's own root run.

    `redacted_trace()`의 ambient `tracing_context()`는 같은 코루틴 안에서 바로
    이어지는 `ainvoke()`에만 닿는다 - `asyncio.gather()`로 fan-out된 Worker별
    독립 그래프(각 부서 `_execute_one`)나 Node 함수 안에서 새로 빌드해 부르는
    그래프에는 전파되지 않는다(실측, 2026-08-24: LangSmith 프로젝트 root run의
    91%가 이름 `LangGraph`·tags 없음·metadata 없음으로 찍혀 있었다 - QA 카드
    Trace Count가 실제 트래픽의 1%도 못 세는 원인이었다). 이 함수가 만드는
    `config=`를 invoke 호출에 직접 넘기면 LangGraph의 `RunnableConfig` → tracer
    배선이 ambient context를 거치지 않고 그 run에 확실히 붙는다 - 읽는 쪽
    (`apps/api/langsmith_traces.py`)도 `extra.metadata.stage`만 본다.
    """

    metadata: dict[str, Any] = {
        "observability_schema": "llm.performance.v1",
        "trace_kind": "worker_graph",
        "latency_scope": "worker_execution",
        "stage": stage,
        "worker_id": worker_id,
        "role": role,
        "raw_payloads_sent": False,
    }
    metadata.update(trace_correlation_metadata(correlation))
    return {
        "run_name": f"worker.{worker_id}",
        "tags": ["hgfinance", f"stage:{stage}", "redacted"],
        "metadata": metadata,
    }


@lru_cache(maxsize=1)
def _safe_langsmith_client() -> Any:
    from langsmith import Client

    return Client(hide_inputs=True, hide_outputs=True, hide_metadata=False)


@contextlib.contextmanager
def redacted_trace(
    *,
    trace_id: str,
    model_name: str,
    stage: str,
    correlation: Mapping[str, Any] | None = None,
) -> Iterator[None]:
    """Make every nested LangChain/LangGraph run redacted for this pipeline."""

    if not langsmith_enabled():
        yield
        return
    try:
        from langsmith import tracing_context

        metadata = {
            "observability_schema": "llm.performance.v1",
            "raw_payloads_sent": False,
            "model_name": model_name,
            "stage": stage,
        }
        metadata.update(
            trace_correlation_metadata(correlation, trace_id=trace_id)
        )
        observer = tracing_context(
            client=_safe_langsmith_client(),
            project_name=langsmith_project("workflow"),
            tags=["hgfinance", "redacted", f"stage:{stage}"],
            metadata=metadata,
            enabled=True,
        )
        observer.__enter__()
    except Exception:
        # Observability setup is optional; business execution must continue.
        yield
        return

    try:
        yield
    except BaseException:
        # Preserve the business exception.  A tracing context cleanup failure
        # must never replace it or turn it into an observability failure.
        try:
            observer.__exit__(*sys.exc_info())
        except Exception:
            pass
        raise
    else:
        try:
            observer.__exit__(None, None, None)
        except Exception:
            pass


def publish_metric(
    metric: dict[str, Any],
    *,
    trace_id: str | None = None,
    project_name: str | None = None,
) -> bool:
    """Send one empty-payload performance metric to the metrics project.

    This is the legacy single-event metric producer.  Only its destination is
    separated from the workflow trace project; no second metric/run is made.
    New feedback evaluation aggregates these events in bounded windows rather
    than creating one QA artifact per event. ``project_name`` is retained for
    the compatibility root publisher below.
    """

    if not langsmith_enabled():
        return False
    try:
        client = _safe_langsmith_client()
        safe = _metric_metadata(metric, trace_id=trace_id)
        safe.update(
            {
                key: value
                for key, value in trace_correlation_metadata(
                    safe, trace_id=trace_id
                ).items()
                if not safe.get(key)
            }
        )
        client.create_run(
            name="llm.performance.metric",
            run_type="chain",
            inputs={},
            outputs={},
            project_name=project_name or langsmith_project("metrics"),
            tags=[
                "hgfinance",
                "metric",
                "redacted",
                f"worker:{metric.get('worker_id', 'unknown')}",
            ],
            extra={"metadata": safe},
            end_time=datetime.now(timezone.utc),
            hide_inputs=True,
            hide_outputs=True,
        )
        return True
    except Exception:
        return False


def publish_root_trace(
    *,
    request_id: str,
    root_id: str | None = None,
    workflow_mode: str | None = None,
    source: str | None = None,
    status: str = "accepted",
    semantic_qa: Mapping[str, Any] | None = None,
) -> bool:
    """Publish one legacy redacted user-query observation.

    Compatibility only: current BFF user-query lifecycle tracing must use
    :func:`start_root_trace` and its terminal close path. This function writes
    a standalone metadata metric and does not create a lifecycle-aware root or
    a distributed child span. LangSmith is an optional observer: missing SDK,
    credentials, network, serialization, or client errors are all swallowed
    by :func:`publish_metric` and cannot change the CEO workflow result.
    """

    normalized_status = str(status or "accepted")
    terminal = normalized_status.casefold() in {
        "completed",
        "failed",
        "error",
        "blocked",
        "degraded",
    }
    correlation = trace_correlation_metadata(
        request_id=request_id,
        root_id=root_id,
        task_id=root_id,
        trace_id=request_id,
    )
    metadata: dict[str, Any] = {
        "schema_version": "llm.workflow-root.v1",
        "worker_id": "ceo-root",
        "role": "workflow_root",
        "stage": "ceo-terminal" if terminal else "ceo-ingress",
        "status": normalized_status,
        "trace_kind": "workflow_root",
        "latency_scope": "standalone_observation",
        **correlation,
        "workflow_mode": str(workflow_mode) if workflow_mode else None,
        "source": str(source) if source else None,
        "raw_payloads_sent": False,
    }
    if isinstance(semantic_qa, Mapping):
        for key in (
            "semantic_qa_version",
            "semantic_qa_evaluator",
            "semantic_qa_verdict",
            "semantic_qa_score",
            "semantic_qa_completeness",
            "semantic_qa_groundedness",
            "semantic_qa_temporal_consistency",
            "semantic_qa_uncertainty_honesty",
            "semantic_qa_relevance",
            "semantic_qa_finding_count",
            "semantic_qa_finding_codes",
        ):
            if key in semantic_qa and semantic_qa[key] not in (None, ""):
                metadata[key] = semantic_qa[key]
    return publish_metric(
        metadata,
        trace_id=str(request_id),
        project_name=langsmith_project("workflow"),
    )


@dataclass
class RootTraceHandle:
    """Best-effort handle for one metadata-only workflow root run."""

    context: str
    request_id: str
    workflow_mode: str
    source: str | None = None
    run_id: str | None = None


def start_root_trace(
    *,
    request_id: str,
    workflow_mode: str,
    source: str | None = None,
) -> RootTraceHandle | None:
    """Create and post a redacted LangSmith root run, fail-open."""

    if not langsmith_enabled():
        return None
    try:
        from langsmith import RunTree

        correlation = trace_correlation_metadata(
            request_id=request_id,
            root_id=request_id,
            task_id=f"{request_id}-task",
            trace_id=request_id,
        )
        metadata = _metric_metadata(
            {
                "schema_version": "llm.workflow-root.v2",
                "worker_id": "ceo-root",
                "role": "workflow_root",
                "stage": "ceo-ingress",
                "department": "ceo-workflow",
                "observation_point": "ceo-ingress",
                "joint_improvement_targets": "ceo-workflow / observability",
                "status": "accepted",
                "trace_kind": "workflow_root",
                "latency_scope": "end_to_end",
                **correlation,
                "workflow_mode": str(workflow_mode),
                "source": str(source) if source else None,
                "raw_payloads_sent": False,
            }
        )
        run = RunTree(
            name="hgfinance.user-query",
            run_type="chain",
            inputs={},
            outputs={},
            project_name=langsmith_project("workflow"),
            tags=["hgfinance", "workflow-root", "redacted"],
            extra={"metadata": metadata},
            ls_client=_safe_langsmith_client(),
        )
        run.post()
        context = str(run.to_headers().get("langsmith-trace") or "")
        if not context:
            return None
        return RootTraceHandle(
            context=context,
            request_id=str(request_id),
            workflow_mode=str(workflow_mode),
            source=str(source) if source else None,
            run_id=str(getattr(run, "id", "") or "") or None,
        )
    except Exception:
        # Observability setup/post/serialization errors never become workflow
        # errors and leave the caller without a propagation marker.
        return None


def close_root_trace(
    trace_context: str | None,
    *,
    run_id: str | None = None,
    request_id: str | None = None,
    root_id: str | None = None,
    workflow_mode: str | None = None,
    source: str | None = None,
    task_id: str | None = None,
    department: str | None = None,
    status: str,
    error_class: str | None = None,
    semantic_qa: Mapping[str, Any] | None = None,
) -> bool:
    """Close a previously posted root run through its distributed context."""

    if not trace_context or not langsmith_enabled():
        return False
    try:
        from langsmith import RunTree

        client = _safe_langsmith_client()
        resolved_run_id = str(run_id or "").strip()
        if resolved_run_id:
            # ``RunTree.post`` may use the SDK background batcher. An
            # immediate system answer can otherwise update the run before the
            # ingress POST is flushed, and that delayed POST restores the old
            # ``accepted`` metadata over the terminal QA result.
            flush = getattr(client, "flush", None)
            if callable(flush):
                flush()
        if not resolved_run_id:
            run = RunTree.from_headers(
                {"langsmith-trace": str(trace_context)},
                project_name=langsmith_project("workflow"),
                ls_client=client,
            )
            if run is None:
                return False
            resolved_run_id = str(run.id)
        correlation = trace_correlation_metadata(
            request_id=request_id,
            root_id=root_id,
            task_id=task_id or root_id,
            trace_id=str(trace_context),
        )
        metadata = _metric_metadata(
            {
                "schema_version": "llm.workflow-root.v2",
                "worker_id": "ceo-root",
                "role": "workflow_root",
                "stage": "ceo-terminal",
                "observation_point": "ceo-ingress",
                "joint_improvement_targets": "ceo-workflow / observability",
                "status": str(status),
                "trace_kind": "workflow_root",
                "latency_scope": "end_to_end",
                **correlation,
                "department": str(department) if department else "ceo-workflow",
                "workflow_mode": str(workflow_mode) if workflow_mode else None,
                "source": str(source) if source else None,
                "raw_payloads_sent": False,
            }
        )
        if isinstance(semantic_qa, Mapping):
            for key in (
                "semantic_qa_version",
                "semantic_qa_evaluator",
                "semantic_qa_verdict",
                "semantic_qa_score",
                "semantic_qa_completeness",
                "semantic_qa_groundedness",
                "semantic_qa_temporal_consistency",
                "semantic_qa_uncertainty_honesty",
                "semantic_qa_relevance",
                "semantic_qa_finding_count",
                "semantic_qa_finding_codes",
            ):
                if key in semantic_qa and semantic_qa[key] not in (None, ""):
                    metadata[key] = semantic_qa[key]
        # ``RunTree.from_headers()`` constructs a local placeholder named
        # ``parent`` with a new start_time. Patching that object would overwrite
        # the already-posted root's real name/timestamp. Update terminal fields
        # only, preserving ``hgfinance.user-query`` and its original start.
        client.update_run(
            run_id=resolved_run_id,
            end_time=datetime.now(timezone.utc),
            error=str(error_class) if error_class else None,
            extra={"metadata": metadata},
        )
        return True
    except Exception:
        # LangSmith must not delay or fail a durable finalization.
        return False


def langfuse_worker_event_name(*, stage: str, worker_id: str) -> str:
    """Single source of truth for the event name both sides key off of.

    Write 측(publish_langfuse_metric)과 read 측(HR observability.py)이 각자
    문자열을 조립하면 접두어가 드리프트해도 아무도 모른다 - 여기 하나로 강제한다.
    """

    return f"llm.performance.metric:{stage}:{worker_id}"


def langfuse_worker_opportunity_event_name(*, stage: str, worker_id: str) -> str:
    """미발화(trigger 조건 불성립) 1건의 이벤트 이름 (2026-08-20 신규).

    실행 이벤트(langfuse_worker_event_name)와 **반드시 다른 이름 공간**이어야 한다.
    같은 이름으로 보내면 HR 의 유휴 판정이 "이 워커가 돌았다"로 읽어 한 번도 실행된
    적 없는 워커가 ACTIVE 로 뜬다 - 관측을 고치려다 관측을 망가뜨리는 셈이다.

    이 이벤트가 만드는 값은 점유율의 **분모**다:
        발화율 = 실행 / (실행 + 미발화)
    조건부 Worker 의 낮은 발화율은 결함이 아니다. 구분해야 하는 것은 "기회가
    없었다"(분모 0)와 "기회가 있었는데 한 번도 안 켜졌다"(분모 N, 분자 0)이며,
    지금까지 둘 다 똑같이 UNOBSERVED 로 보였다.
    """

    return f"llm.performance.opportunity:{stage}:{worker_id}"


def langfuse_enabled() -> bool:
    """2026-08-10: HR(07-agent-workforce) 유휴 Agent 관측용으로 신규 도입.

    LangSmith(langsmith_enabled 위)와 나란히 켜는 이중 계측이다 - 기존 LangSmith
    파이프라인은 그대로 두고, 여기 추가한 것만으로 HR 이 읽을 계측을 늘린다.
    """

    tracing = os.getenv("LANGFUSE_TRACING", "")
    return (
        tracing.casefold() in {"1", "true", "yes", "on"}
        and bool(os.getenv("LANGFUSE_PUBLIC_KEY", "").strip())
        and bool(os.getenv("LANGFUSE_SECRET_KEY", "").strip())
    )


@lru_cache(maxsize=1)
def _safe_langfuse_client() -> Any:
    from langfuse import Langfuse

    return Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        host=os.getenv("LANGFUSE_HOST") or "https://cloud.langfuse.com",
    )


def _native_observation_metadata(
    *, worker_id: str, role: str, stage: str, trace_id: str | None = None
) -> dict[str, str | bool]:
    """Metadata safe for native Langfuse span/generation observations.

    Native observation fields own latency and token usage.  Do not duplicate
    them in metadata: the Langfuse UI does not aggregate arbitrary metadata
    into its built-in latency/token dashboards.  Prompt and completion text
    stay absent by policy; these identifiers are fixed operational labels.
    """

    metadata: dict[str, str | bool] = {
        "observability_schema": "llm.native.v1",
        "worker_id": worker_id,
        "role": role,
        "stage": stage,
        "raw_payloads_sent": False,
    }
    if trace_id:
        # This application ID is not guaranteed to satisfy Langfuse trace-ID
        # syntax, so it remains a correlation label rather than being forced
        # into the SDK's trace context.
        metadata["application_trace_id"] = trace_id
    return metadata


def _usage_details(usage: Any) -> dict[str, int]:
    """Translate OpenAI-compatible usage without retaining provider payloads."""

    if usage is None:
        return {}
    input_tokens = next(
        (
            getattr(usage, name, None)
            for name in ("prompt_tokens", "input_tokens")
            if getattr(usage, name, None) is not None
        ),
        None,
    )
    output_tokens = next(
        (
            getattr(usage, name, None)
            for name in ("completion_tokens", "output_tokens")
            if getattr(usage, name, None) is not None
        ),
        None,
    )
    details: dict[str, int] = {}
    if input_tokens is not None:
        details["input"] = int(input_tokens)
    if output_tokens is not None:
        details["output"] = int(output_tokens)
    if details:
        details["total"] = sum(details.values())
    return details


@dataclass
class _NativeLangfuseObservation:
    """Best-effort handle exposed by the redacted native-observation scopes."""

    observation: Any | None = None

    def set_usage(self, usage: Any) -> None:
        details = _usage_details(usage)
        if not details or self.observation is None:
            return
        with contextlib.suppress(Exception):
            self.observation.update(usage_details=details)


@contextlib.contextmanager
def _redacted_native_observation(
    *,
    as_type: Literal["span", "generation"],
    name: str,
    model_name: str | None = None,
    metadata: dict[str, str | bool],
) -> Iterator[_NativeLangfuseObservation]:
    """Create a Langfuse native observation without allowing SDK failures to break work.

    ``start_as_current_observation`` is deliberately used instead of an
    independent event so a generation becomes a child of its Worker span.
    Inputs and outputs are omitted altogether: native timing/token fields do
    not require business prompt or response text.
    """

    manager: Any | None = None
    handle = _NativeLangfuseObservation()
    if langfuse_enabled():
        try:
            kwargs: dict[str, Any] = {
                "as_type": as_type,
                "name": name,
                "metadata": metadata,
            }
            if model_name:
                kwargs["model"] = model_name
            manager = _safe_langfuse_client().start_as_current_observation(**kwargs)
            handle.observation = manager.__enter__()
        except Exception:
            # Observability remains non-binding.  An unavailable SDK/client
            # must not change a trading-adjacent Worker result.
            if manager is not None:
                with contextlib.suppress(Exception):
                    manager.__exit__(None, None, None)
            manager = None
            handle.observation = None

    try:
        yield handle
    except BaseException as exc:
        if manager is not None:
            with contextlib.suppress(Exception):
                manager.__exit__(type(exc), exc, exc.__traceback__)
            manager = None
        raise
    finally:
        if manager is not None:
            with contextlib.suppress(Exception):
                manager.__exit__(None, None, None)


@contextlib.contextmanager
def redacted_langfuse_worker_span(
    *, worker_id: str, role: str, stage: str | None, trace_id: str | None = None
) -> Iterator[None]:
    """Native Worker-duration span for the Langfuse latency dashboard."""

    # Legacy callers without a canonical stage intentionally remain
    # uninstrumented instead of creating a misleading ``unknown`` series.
    if not stage:
        yield
        return
    with _redacted_native_observation(
        as_type="span",
        name="worker.run",
        metadata=_native_observation_metadata(
            worker_id=worker_id,
            role=role,
            stage=stage,
            trace_id=trace_id,
        ),
    ):
        yield


@contextlib.contextmanager
def redacted_langfuse_generation(
    *,
    worker_id: str,
    role: str,
    stage: str,
    model_name: str,
    trace_id: str | None = None,
) -> Iterator[_NativeLangfuseObservation]:
    """Native LLM generation with automatic duration and explicit token usage."""

    with _redacted_native_observation(
        as_type="generation",
        name="ollama.chat.completions",
        model_name=model_name,
        metadata=_native_observation_metadata(
            worker_id=worker_id,
            role=role,
            stage=stage,
            trace_id=trace_id,
        ),
    ) as generation:
        yield generation


@contextlib.contextmanager
def redacted_current_worker_generation() -> Iterator[_NativeLangfuseObservation]:
    """Attach an LLM call to the Worker span held in the current context."""

    metric = _CURRENT_METRIC.get()
    if metric is None:
        yield _NativeLangfuseObservation()
        return
    with redacted_langfuse_generation(
        worker_id=metric.worker_id,
        role=metric.role,
        stage=metric.stage,
        model_name=metric.model_name,
    ) as generation:
        yield generation


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
    request_id: str | None = None,
    root_id: str | None = None,
    task_id: str | None = None,
    measured: dict[str, Any] | None = None,
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

    # ▶ 모르는 값을 0/"" 으로 채우지 않는다(2026-08-20). llm_calls: 0 은 "모델을
    #   한 번도 안 불렀다"는 **관측 사실**로 읽히는데, 안 센 것과 0 은 다르다
    #   (quality.py aggregate_quality() 의 None/0 구분과 같은 원칙).
    #
    #   `measured` 는 end_worker_metric() 이 돌려준 실측치다. 있으면 토큰·모델·
    #   호출수가 함께 나가고, 없으면 그 필드들은 아예 빠진다. record_llm_call() 이
    #   네 모델 경로 전부(공용 런타임·Risk·QA·Worker Model Gateway)에서 이미
    #   불리고 있었는데 begin_worker_metric() 컨텍스트가 열려 있지 않아 그 값이
    #   버려지고 있었다 - 이 인자가 그 값을 살리는 통로다.
    measured = measured or {}
    optional = {
        key: measured[key]
        for key in (
            "llm_calls",
            "model_name",
            "prompt_tokens",
            "completion_tokens",
            "retries",
        )
        if measured.get(key) not in (None, "")
    }
    correlation = {
        key: value
        for key, value in (
            ("request_id", request_id),
            ("root_id", root_id),
            ("task_id", task_id),
        )
        if value not in (None, "")
    }
    return publish_langfuse_metric(
        {
            **optional,
            **correlation,
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


# 부서 Hermes Profile 디렉터리. 이름 규칙으로 짓지 않고 표로 적는다 - 하나라도
# 어긋나면 그 부서장만 조용히 계측에서 빠진다(hermes_boundary PROFILE_CONTAINERS 와
# 같은 이유). 키는 canonical profile 이름이다(orchestration/canonical_profiles.py).
PROFILE_DIRECTORY: dict[str, str] = {
    "ceo-agent": "00-ceo-office",
    "research-department": "01-research",
    "research-liaison": "01-research",
    "trading-department": "02-trading",
    "risk-management": "03-risk",
    "quant-backtest-department": "04-quant-backtest",
    "quant-liaison": "04-quant-backtest",
    "accounting-portfolio-department": "05-accounting-portfolio",
    "qa-department": "06-ai-qa-audit",
    "hr-department": "07-agent-workforce",
}

_REPO_ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=32)
def head_persona_for_profile(profile: str, repo_root: str | None = None) -> str:
    """canonical profile -> 그 부서장의 `agent.head_persona`.

    write 측이 둘이다(BFF 의 hermes_boundary.ask 와 ceo-supervisor 의 카드 종료
    관측). 둘이 각자 신원을 만들면 같은 부서장이 두 이름으로 쪼개져 유휴 판정이
    양쪽 다 놓친다 - 그래서 여기 하나로 모으고, read 측(HR observability)도 같은
    키를 읽는다.

    못 읽으면 빈 문자열이다. 계측 때문에 질의·워크플로가 실패하면 안 된다.
    """

    directory = PROFILE_DIRECTORY.get(profile)
    if not directory:
        return ""
    root = Path(repo_root) if repo_root else _REPO_ROOT
    try:
        import yaml

        config = (
            yaml.safe_load(
                (root / "departments" / directory / "hermes" / "config.yaml").read_text(
                    encoding="utf-8"
                )
            )
            or {}
        )
        return str((config.get("agent") or {}).get("head_persona") or "").strip()
    except Exception:  # noqa: BLE001 - 계측 부재가 실행을 죽이지 않는다
        return ""


def stage_for_profile(profile: str) -> str:
    """canonical profile -> event stage. 모르는 프로필이면 빈 문자열(계측 포기).

    stage 이름 공간은 orchestration/canonical_profiles.py 의 부서 코드와 같다 -
    research/quant/trading/accounting/risk/qa/ceo/hr. 여기서 새 표를 만들지 않는
    이유는 그 표가 이미 정본이고, 복제하면 어긋나도 아무도 모르기 때문이다.
    """

    try:
        from orchestration.canonical_profiles import department_for_canonical_profile

        return department_for_canonical_profile(profile)
    except Exception:  # noqa: BLE001 - 규칙으로 지어내지 않는다
        return ""


def publish_worker_opportunity(
    *,
    stage: str,
    worker_id: str,
    role: str = "",
    reason: str = "trigger_not_fired",
    trace_id: str | None = None,
) -> bool:
    """Worker 가 후보였지만 trigger 가 안 켜진 1건을 기록한다(2026-08-20).

    세 실행기가 이미 not_executed 를 계산하고 있었는데 발행만 안 하고 있었다 -
    새로 재는 값이 아니라 버려지던 값이다.
    """

    if not langfuse_enabled():
        return False
    try:
        client = _safe_langfuse_client()
        client.create_event(
            name=langfuse_worker_opportunity_event_name(
                stage=stage, worker_id=worker_id
            ),
            input=None,
            output=None,
            metadata={
                "schema_version": "llm.opportunity.v1",
                "worker_id": worker_id,
                "role": role,
                "stage": stage,
                "reason": reason,
                "trace_id": trace_id or "",
                "raw_payloads_sent": False,
            },
            level="DEFAULT",
        )
        return True
    except Exception:
        return False


def publish_head_activity(
    *,
    stage: str,
    head_persona: str,
    status: str = "COMPLETED",
    latency_ms: int = 0,
    error_count: int = 0,
    trace_id: str | None = None,
    source: str = "bff_ask",
) -> bool:
    """부서장(Hermes Profile) 턴 1건을 기록한다(2026-08-20).

    ▶ 왜 부서장을 재는가: 일반 질문 트래픽은 부서장 턴에서 끝나고, Worker 를 부를지는
      부서장의 판단이다. 부서장이 몇 번 불렸는지를 모르면 Worker 의 실행 0 회가
      "일이 없었다"인지 "일이 있었는데 위임하지 않았다"인지 영원히 구분할 수 없다 -
      Department Scorecard 의 arrivals(도착 건수)가 여기서 나온다.

    신원은 부서 Profile 의 `agent.head_persona` 를 그대로 쓴다. Worker id 와 이름
    공간이 겹치지 않으므로 실행 이벤트와 같은 name 규칙을 쓴다 - HR 의 유휴 조회는
    등록된 id 만 묻기 때문에 이 이벤트가 기존 판정을 오염시키지 않는다.
    """

    return publish_langfuse_metric(
        {
            "schema_version": "llm.performance.v1",
            "worker_id": head_persona,
            "role": "department_head",
            "stage": stage,
            "status": status,
            "source": source,
            "error_count": int(error_count),
            "raw_payloads_sent": False,
            # 안 잰 값은 넣지 않는다 - 카드 종료 관측에는 지속시간이 없다.
            **({"latency_ms": int(latency_ms)} if latency_ms else {}),
        },
        trace_id=trace_id,
    )


def metric_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
