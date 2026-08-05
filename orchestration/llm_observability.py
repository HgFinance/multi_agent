"""Credential-free LLM performance metrics and redacted LangSmith tracing."""

from __future__ import annotations

import contextlib
import contextvars
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterator


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


def metric_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
