"""Optional OpenTelemetry + Prometheus instrumentation for Risk/QA.

The module is dependency-optional so deterministic gates and local tests keep
running when no Collector or Prometheus package is installed. Exporters are
enabled only with explicit environment variables.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from functools import wraps
from statistics import median
from threading import Lock
from time import perf_counter
from typing import Any

try:  # pragma: no cover - exercised only in an instrumented environment
    from prometheus_client import Counter, Gauge, Histogram, generate_latest
except ImportError:  # pragma: no cover - local tests intentionally allow this
    Counter = Gauge = Histogram = generate_latest = None

try:  # pragma: no cover - exercised only in an instrumented environment
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
except ImportError:  # pragma: no cover - local tests intentionally allow this
    trace = Status = StatusCode = None


_PROM_LOCK = Lock()
_PROM_METRICS: dict[str, Any] = {}


def _prom_metric(kind: str, name: str, documentation: str, labels: tuple[str, ...]):
    if Counter is None:
        return None
    with _PROM_LOCK:
        if name in _PROM_METRICS:
            return _PROM_METRICS[name]
        constructor = {"counter": Counter, "gauge": Gauge, "histogram": Histogram}[kind]
        metric = constructor(name, documentation, labels)
        _PROM_METRICS[name] = metric
        return metric


class RuntimeTelemetry:
    """Small testable facade around optional OTel and Prometheus exporters."""

    def __init__(self, department: str) -> None:
        self.department = department
        self._lock = Lock()
        self._pipeline_samples: list[float] = []
        self._pipeline_count = 0
        self._fallback_count = 0
        self._fallback_by_stage: dict[str, int] = {}
        self._circuit_breakers: dict[str, str] = {}
        self._tracer = self._build_tracer()
        self._duration = _prom_metric(
            "histogram",
            "risk_qa_pipeline_duration_seconds",
            "Risk/QA pipeline duration by stage and outcome",
            ("department", "stage", "outcome"),
        )
        self._fallbacks = _prom_metric(
            "counter",
            "risk_qa_fallback_total",
            "Risk/QA fail-closed fallback count",
            ("department", "stage"),
        )
        self._breaker = _prom_metric(
            "gauge",
            "risk_qa_circuit_breaker_state",
            "Risk/QA circuit breaker state: CLOSED=0, HALF_OPEN=1, OPEN=2",
            ("department", "component"),
        )

    def _build_tracer(self):
        if trace is None or os.getenv("RISK_QA_OTEL_ENABLED") != "1":
            return None
        try:  # pragma: no cover - requires SDK/exporter packages
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
            provider = TracerProvider(
                resource=Resource.create({"service.name": f"{self.department}-department"})
            )
            if endpoint:
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            trace.set_tracer_provider(provider)
            return trace.get_tracer(f"risk-qa.{self.department}")
        except Exception:  # noqa: BLE001 - telemetry setup must never change a safe decision
            return None

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
        """Create a trace span when enabled; otherwise yield a no-op context."""
        if self._tracer is None:
            with nullcontext(None) as span:
                yield span
            return

        try:  # pragma: no cover - requires SDK/exporter packages
            with self._tracer.start_as_current_span(name) as span:
                for key, value in (attributes or {}).items():
                    span.set_attribute(key, str(value))
                try:
                    yield span
                except Exception as exc:
                    if Status is not None and StatusCode is not None:
                        span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                    raise
        except Exception:  # noqa: BLE001 - telemetry backend must never change a safe decision
            # A telemetry backend is never allowed to alter the domain outcome.
            yield None

    def record_pipeline(
        self, *, stage: str, outcome: str, duration_seconds: float, fallback_count: int = 0
    ) -> None:
        with self._lock:
            self._pipeline_samples.append(max(0.0, duration_seconds))
            self._pipeline_samples = self._pipeline_samples[-1000:]
            self._pipeline_count += 1
            self._fallback_count += max(0, fallback_count)
            if fallback_count:
                self._fallback_by_stage[stage] = self._fallback_by_stage.get(stage, 0) + fallback_count
        if self._duration is not None:
            self._duration.labels(self.department, stage, outcome).observe(duration_seconds)
        if fallback_count and self._fallbacks is not None:
            self._fallbacks.labels(self.department, stage).inc(fallback_count)

    def record_circuit_breaker(self, component: str, state: str) -> None:
        normalized = state.upper()
        if normalized not in {"CLOSED", "HALF_OPEN", "OPEN"}:
            normalized = "OPEN"
        with self._lock:
            self._circuit_breakers[component] = normalized
        if self._breaker is not None:
            self._breaker.labels(self.department, component).set(
                {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}[normalized]
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            samples = sorted(self._pipeline_samples)
            count = self._pipeline_count
            fallback_count = self._fallback_count
            fallback_by_stage = dict(self._fallback_by_stage)
            breakers = dict(self._circuit_breakers)
        if not samples:
            p50 = p99 = 0.0
        else:
            p50 = median(samples)
            p99 = samples[min(len(samples) - 1, int((len(samples) - 1) * 0.99))]
        return {
            "department": self.department,
            "pipeline_count": count,
            "fallback_count": fallback_count,
            "fallback_rate": fallback_count / count if count else 0.0,
            "p50_seconds": p50,
            "p99_seconds": p99,
            "fallback_by_stage": fallback_by_stage,
            "circuit_breakers": breakers,
        }

    def prometheus_text(self) -> bytes:
        if generate_latest is None:
            return b"# prometheus-client is not installed\n"
        return generate_latest()


def observe_pipeline(telemetry: RuntimeTelemetry, stage: str) -> Callable:
    """Decorate a department runner without changing its return contract."""

    def decorator(function: Callable) -> Callable:
        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            started = perf_counter()
            outcome = "ERROR"
            fallback_count = 0
            try:
                with telemetry.span(f"{telemetry.department}.{stage}", {"stage": stage}):
                    result = function(*args, **kwargs)
                if isinstance(result, dict):
                    fallback_count = len(result.get("fallbacks") or [])
                    outcome = "ESCALATE" if result.get("escalate") else str(
                        result.get("verdict") or result.get("decision") or "UNKNOWN"
                    )
                return result
            finally:
                telemetry.record_pipeline(
                    stage=stage,
                    outcome=outcome,
                    duration_seconds=perf_counter() - started,
                    fallback_count=fallback_count,
                )

        return wrapped

    return decorator


_TELEMETRY_BY_DEPARTMENT: dict[str, RuntimeTelemetry] = {}


def get_runtime_telemetry(department: str) -> RuntimeTelemetry:
    """Return one process-local sink so API and pipeline share metrics in tests."""
    if department not in _TELEMETRY_BY_DEPARTMENT:
        _TELEMETRY_BY_DEPARTMENT[department] = RuntimeTelemetry(department)
    return _TELEMETRY_BY_DEPARTMENT[department]
