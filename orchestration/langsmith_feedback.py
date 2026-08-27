"""Fail-open LangSmith -> QA feedback loop.

This module is deliberately separate from the business workflow.  It polls
completed metadata-only runs in the workflow project from a bounded background
worker, derives deterministic operational findings, and writes a redacted
evaluation run to ``HgFinance-Evals``.  No prompt, answer, provider output, or
credential is fetched or persisted.

The local SQLite ledger is only a small coordination/approval index.  It uses
WAL mode, short busy timeouts, idempotent source-run keys, and bounded result
sets so QA work cannot block CEO, Kanban, provider, or final-delivery paths.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from orchestration.langsmith_queries import query_runs

LOGGER = logging.getLogger(__name__)

WORKFLOW_PROJECT_DEFAULT = "First"
EVALS_PROJECT_DEFAULT = "HgFinance-Evals"
FEEDBACK_SCHEMA = "hgfinance.observability.feedback.v1"
FEEDBACK_MODES = frozenset({"off", "shadow", "active"})
IMPROVEMENT_TYPES = frozenset(
    {
        "SKILL_CREATE",
        "SKILL_EVOLVE",
        "CODE_FIX",
        "PROMPT_POLICY",
        "RUNTIME_CONFIG",
        "DATA_QUALITY",
        "NO_ACTION",
    }
)
_ACTIONABLE_FEEDBACK_CODES = frozenset(
    {
        "WORKER_OR_WORKFLOW_DEGRADED",
        "LATENCY_ABOVE_THRESHOLD",
        "STRUCTURED_EVAL_SCORE_LOW",
        "SEMANTIC_QA_FAILED",
        "SEMANTIC_QA_SCORE_LOW",
        "PRIVACY_PAYLOAD_PRESENT",
    }
)
TERMINAL_STATUSES = frozenset({"success", "completed", "complete", "error", "failed", "blocked", "degraded"})
ERROR_STATUSES = frozenset({"error", "failed", "blocked", "degraded", "gave_up", "timed_out"})
_NON_WORKFLOW_ROOT_NAMES = frozenset(
    {
        "llm.performance.metric",
        "qa.trace.evaluation",
        # This is the post-response terminal envelope, not a new workflow.
        # Feeding it back into the same QA loop creates self-referential
        # latency artifacts beside the real worker trace.
        "qa.hermes.terminal",
    }
)
_DEPARTMENT_CANONICAL = {
    "research": "research",
    "research-department": "research",
    "trading": "trading",
    "trading-department": "trading",
    "risk": "risk",
    "risk-management": "risk",
    "qa": "qa",
    "qa-department": "qa",
    "quant": "quant",
    "quant-backtest": "quant",
    "quant-backtest-department": "quant",
    "accounting": "accounting-portfolio",
    "portfolio": "accounting-portfolio",
    "portfolio-recommendation": "accounting-portfolio",
    "accounting-portfolio": "accounting-portfolio",
    "accounting-portfolio-department": "accounting-portfolio",
    "ceo": "ceo",
    "ceo-agent": "ceo",
    "ceo-workflow": "ceo",
    "ceo-ingress": "ceo",
    "ceo-terminal": "ceo",
    "hr": "hr",
    "hr-department": "hr",
}
_SAFE_METADATA_KEYS = frozenset(
    {
        "request_id",
        "root_id",
        "task_id",
        "workflow_mode",
        "workflow_role",
        "department",
        "stage",
        "worker_id",
        "role",
        "status",
        "error_code",
        "error_class",
        "http_status",
        "error_count",
        "latency_ms",
        "attempts",
        "retries",
        "llm_calls",
        "tool_calls",
        "tool_error_count",
        "profile",
        "observation_unit",
        "workflow_root_task_id",
        "kanban_run_id",
        "tool_call_count",
        "tool_names",
        "tool_duration_total_ms",
        "llm_turn_count_observed",
        "model_call_count_observed",
        "tool_latency_ms",
        "tool_timing_source",
        "model_latency_ms",
        "return_code",
        "latency_available",
        "tool_latency_available",
        "telemetry_completeness",
        "observability_source",
        "output_verdict",
        "finding_count",
        "eval_score",
        "provider",
        "model_name",
        "raw_payloads_sent",
        "source",
        "metric_count",
        "window_start",
        "window_end",
        "p95_latency_ms",
        "trace_kind",
        "latency_scope",
        "observation_point",
        "primary_bottleneck_department",
        "primary_bottleneck_duration_ms",
        "joint_improvement_targets",
        "latency_attribution_status",
        "latency_attribution_method",
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
)

_TEXT_METADATA_KEYS = frozenset(
    {
        "request_id",
        "root_id",
        "task_id",
        "trace_id",
        "workflow_mode",
        "workflow_role",
        "department",
        "stage",
        "worker_id",
        "role",
        "status",
        "error_code",
        "error_class",
        "provider",
        "model_name",
        "source",
        "trace_kind",
        "latency_scope",
        "observation_point",
        "primary_bottleneck_department",
        "joint_improvement_targets",
        "latency_attribution_status",
        "latency_attribution_method",
        "semantic_qa_version",
        "semantic_qa_evaluator",
        "semantic_qa_verdict",
        "semantic_qa_finding_codes",
        "profile",
        "observation_unit",
        "workflow_root_task_id",
        "kanban_run_id",
        "telemetry_completeness",
        "observability_source",
        "output_verdict",
    }
)
_INT_METADATA_KEYS = frozenset(
    {
        "http_status",
        "error_count",
        "latency_ms",
        "attempts",
        "retries",
        "llm_calls",
        "tool_calls",
        "tool_error_count",
        "metric_count",
        "p95_latency_ms",
        "primary_bottleneck_duration_ms",
        "semantic_qa_finding_count",
        "finding_count",
        "tool_call_count",
        "llm_turn_count_observed",
        "model_call_count_observed",
        "tool_latency_ms",
        "model_latency_ms",
        "tool_duration_total_ms",
        "return_code",
    }
)
_SCORE_METADATA_KEYS = frozenset(
    {
        "eval_score",
        "semantic_qa_score",
        "semantic_qa_completeness",
        "semantic_qa_groundedness",
        "semantic_qa_temporal_consistency",
        "semantic_qa_uncertainty_honesty",
        "semantic_qa_relevance",
    }
)


def _normalized_metadata_value(key: str, value: Any) -> Any:
    """Copy one safe scalar from a run without ever retaining payload text."""

    if key == "tool_names":
        if not isinstance(value, (list, tuple)):
            return None
        return [_bounded_text(item, 80) for item in value[:32] if _bounded_text(item, 80)]
    if key in _TEXT_METADATA_KEYS:
        return _bounded_text(value, 160)
    if key in _INT_METADATA_KEYS:
        return _bounded_int(value, maximum=3_600_000)
    if key in {"window_start", "window_end"}:
        return _bounded_text(value, 64)
    if key in _SCORE_METADATA_KEYS:
        return _bounded_score(value)
    if key == "raw_payloads_sent":
        return bool(value)
    if key in {"latency_available", "tool_latency_available"}:
        return bool(value)
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: Any, limit: int = 160) -> str:
    return " ".join(str(value or "").split())[:limit]


def canonical_department(value: Any) -> str:
    """Normalize a UI department code and a trace stage into one key."""

    candidate = _bounded_text(value, 64).lower()
    return _DEPARTMENT_CANONICAL.get(candidate, candidate)


def _feedback_semantic_key(
    *,
    department: Any,
    decision: Any,
    finding_codes: Any,
    metadata: Mapping[str, Any],
) -> str | None:
    """Identify one actionable finding without conflating unrelated traces."""

    request_id = _bounded_text(metadata.get("request_id"), 160)
    if not request_id and metadata.get("source") == "metrics-window":
        try:
            window_start = datetime.fromisoformat(
                str(metadata.get("window_start") or "").replace("Z", "+00:00")
            )
            six_hour_bucket = int(window_start.timestamp()) // (6 * 60 * 60)
            request_id = (
                "metrics-incident:"
                f"{_bounded_text(metadata.get('source_project'), 80)}:"
                f"{six_hour_bucket}"
            )
        except (TypeError, ValueError):
            request_id = ""
    findings = sorted(
        {
            _bounded_text(code, 96).upper()
            for code in (finding_codes or ())
            if _bounded_text(code, 96)
        }
    )
    normalized_decision = _bounded_text(decision, 48).upper()
    if not request_id or not findings or normalized_decision == "OBSERVED_PASS":
        return None
    identity = {
        "schema": "hgfinance.qa-finding-identity.v1",
        "request_id": request_id,
        "department": canonical_department(department),
        "decision": normalized_decision,
        "finding_codes": findings,
        "latency_scope": _bounded_text(metadata.get("latency_scope"), 64).lower(),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return "qa-finding:" + hashlib.sha256(encoded).hexdigest()


def _bounded_int(value: Any, default: int = 0, maximum: int = 2_147_483_647) -> int:
    try:
        return max(0, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _bounded_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(score):
        return None
    return max(0.0, min(score, 1.0))


def feedback_mode(value: str | None = None) -> str:
    candidate = str(value if value is not None else os.getenv("LANGSMITH_FEEDBACK_MODE", "shadow")).strip().lower()
    return candidate if candidate in FEEDBACK_MODES else "off"


@dataclass(frozen=True)
class FeedbackConfig:
    mode: str
    workflow_project: str
    metrics_project: str
    evals_project: str
    state_path: str
    poll_seconds: float
    lookback_seconds: int
    batch_size: int
    max_pending: int
    retention_days: int
    latency_warn_ms: int
    max_feedback_items: int
    max_feedback_chars: int
    metrics_window_seconds: int
    metrics_max_runs: int
    kanban_db_path: str | None = None

    @classmethod
    def from_env(cls) -> FeedbackConfig:
        def _float(name: str, default: float, minimum: float, maximum: float) -> float:
            try:
                return max(minimum, min(float(os.getenv(name, str(default))), maximum))
            except (TypeError, ValueError):
                return default

        def _int(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                return max(minimum, min(int(os.getenv(name, str(default))), maximum))
            except (TypeError, ValueError):
                return default

        return cls(
            mode=feedback_mode(),
            workflow_project=os.getenv("LANGSMITH_PROJECT", WORKFLOW_PROJECT_DEFAULT).strip() or WORKFLOW_PROJECT_DEFAULT,
            metrics_project=os.getenv("LANGSMITH_METRICS_PROJECT", "HgFinance-Metrics").strip() or "HgFinance-Metrics",
            evals_project=os.getenv("LANGSMITH_EVALS_PROJECT", EVALS_PROJECT_DEFAULT).strip() or EVALS_PROJECT_DEFAULT,
            state_path=os.getenv("LANGSMITH_FEEDBACK_STATE_PATH", "/var/lib/portfolio/langsmith-feedback.sqlite3").strip()
            or "/var/lib/portfolio/langsmith-feedback.sqlite3",
            poll_seconds=_float("LANGSMITH_FEEDBACK_POLL_SECONDS", 30.0, 5.0, 300.0),
            # Discovery is based on completed roots' end_time, not only their
            # start_time.  Keep a bounded completion window so a long-running
            # workflow can still be found after it finishes without scanning
            # the whole project.
            lookback_seconds=_int("LANGSMITH_FEEDBACK_LOOKBACK_SECONDS", 900, 30, 86_400),
            batch_size=_int("LANGSMITH_FEEDBACK_BATCH_SIZE", 25, 1, 100),
            max_pending=_int("LANGSMITH_FEEDBACK_MAX_PENDING", 500, 10, 10_000),
            retention_days=_int("LANGSMITH_FEEDBACK_RETENTION_DAYS", 30, 1, 365),
            latency_warn_ms=_int("LANGSMITH_FEEDBACK_LATENCY_WARN_MS", 60_000, 1_000, 3_600_000),
            max_feedback_items=_int("LANGSMITH_FEEDBACK_MAX_ITEMS", 3, 1, 10),
            max_feedback_chars=_int("LANGSMITH_FEEDBACK_MAX_CHARS", 1_200, 200, 4_000),
            metrics_window_seconds=_int("LANGSMITH_FEEDBACK_METRICS_WINDOW_SECONDS", 300, 60, 3_600),
            # SmithDB v2 accepts at most 100 rows per page. Keep the bound
            # below that server-side limit so a bad tuning value cannot turn
            # the background, fail-open poller into a repeated 400 loop.
            metrics_max_runs=_int("LANGSMITH_FEEDBACK_METRICS_MAX_RUNS", 100, 1, 100),
            kanban_db_path=os.getenv("LANGSMITH_FEEDBACK_KANBAN_DB_PATH", "").strip()
            or None,
        )


@dataclass(frozen=True)
class TraceObservation:
    source_run_id: str
    name: str
    status: str
    started_at: str | None
    ended_at: str | None
    metadata: dict[str, Any]

    @property
    def department(self) -> str:
        value = self.metadata.get("department") or self.metadata.get("stage") or "unknown"
        return _bounded_text(value, 64).lower()

    @property
    def workflow_role(self) -> str:
        return _bounded_text(self.metadata.get("workflow_role") or self.metadata.get("role"), 64).lower()


@dataclass(frozen=True)
class EvaluationResult:
    source_run_id: str
    department: str
    workflow_role: str
    decision: str
    score: float | None
    finding_codes: tuple[str, ...]
    summaries: tuple[str, ...]
    metadata: dict[str, Any]


def observation_from_run(run: Any) -> TraceObservation:
    """Extract only the allowlisted metadata fields from a LangSmith run."""

    extra = getattr(run, "extra", None) or {}
    raw_metadata = extra.get("metadata") if isinstance(extra, Mapping) else {}
    raw_metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    # LangSmith accepts the terminal ``outputs`` and ``error`` fields on an
    # existing run, but some deployments keep the original ``extra.metadata``
    # snapshot immutable.  Merge only the bounded terminal envelope so an
    # accepted root is not misclassified after it actually failed.  Unknown
    # output keys (answers, prompts, tool payloads) are intentionally ignored.
    raw_outputs = getattr(run, "outputs", None)
    raw_outputs = raw_outputs if isinstance(raw_outputs, Mapping) else {}
    metadata: dict[str, Any] = {}
    for key in _SAFE_METADATA_KEYS:
        value = raw_metadata.get(key, None)
        if key in raw_metadata:
            normalized = _normalized_metadata_value(key, value)
            if normalized is not None or key == "raw_payloads_sent":
                metadata[key] = normalized
        if key in raw_outputs and raw_outputs[key] not in (None, ""):
            normalized = _normalized_metadata_value(key, raw_outputs[key])
            if normalized is not None:
                # Terminal output is authoritative for status/error fields.
                # It is also useful for worker counters when metadata was
                # posted before the subprocess finished.
                metadata[key] = normalized

    run_status = _bounded_text(getattr(run, "status", ""), 32).lower()
    if run_status in TERMINAL_STATUSES:
        metadata["status"] = run_status
    run_error = _bounded_text(getattr(run, "error", ""), 120)
    if run_error:
        metadata.setdefault("error_class", run_error)
    start_time = getattr(run, "start_time", None)
    end_time = getattr(run, "end_time", None)
    if "latency_ms" not in metadata and start_time is not None and end_time is not None:
        try:
            metadata["latency_ms"] = _bounded_int(
                max(0.0, (end_time - start_time).total_seconds()) * 1_000,
                maximum=3_600_000,
            )
        except (AttributeError, TypeError, ValueError):
            pass
    metadata.setdefault("raw_payloads_sent", False)
    return TraceObservation(
        source_run_id=_bounded_text(getattr(run, "id", ""), 128),
        name=_bounded_text(getattr(run, "name", ""), 160),
        status=_bounded_text(getattr(run, "status", ""), 32).lower(),
        started_at=start_time.isoformat() if start_time else None,
        ended_at=end_time.isoformat() if end_time else None,
        metadata=metadata,
    )


def _is_workflow_feedback_source(observation: TraceObservation) -> bool:
    """Keep legacy/test roots out of the production workflow feedback loop."""

    name = observation.name.strip().casefold()
    return name not in _NON_WORKFLOW_ROOT_NAMES and not name.startswith(
        ("hgfinance.test.", "test.")
    )


def attribute_workflow_bottleneck(
    observation: TraceObservation,
    *,
    kanban_db_path: str | None,
) -> TraceObservation:
    """Attribute root latency from durable Kanban timings, never trace labels.

    ``ceo-ingress`` is where the end-to-end timer starts. It is not evidence
    that ingress caused the delay. For a completed workflow root, the longest
    measured primary department task is the bounded bottleneck attribution.
    If the Kanban evidence is unavailable, the observation remains unchanged
    rather than inventing an owner.
    """

    metadata = dict(observation.metadata)
    if (
        _bounded_text(metadata.get("trace_kind"), 40).lower() != "workflow_root"
        or _bounded_text(metadata.get("latency_scope"), 40).lower() != "end_to_end"
    ):
        return observation
    request_id = _bounded_text(metadata.get("request_id"), 160)
    path = Path(str(kanban_db_path or ""))
    if not request_id or not kanban_db_path or not path.is_file():
        return observation

    try:
        with sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=0.25,
        ) as database:
            database.execute("PRAGMA query_only=ON")
            root = database.execute(
                """
                SELECT id
                FROM tasks
                WHERE idempotency_key = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (request_id,),
            ).fetchone()
            if root is None:
                return observation
            root_id = _bounded_text(root[0], 80)
            marker = f"%workflow_root_task_id={root_id}%"
            rows = database.execute(
                """
                SELECT assignee, started_at, completed_at
                FROM tasks
                WHERE body LIKE ?
                  AND body LIKE '%workflow_role=primary%'
                  AND started_at IS NOT NULL
                  AND completed_at IS NOT NULL
                """,
                (marker,),
            ).fetchall()
    except (OSError, sqlite3.Error):
        LOGGER.warning(
            "langsmith_feedback_latency_attribution_unavailable request_id=%s",
            request_id,
        )
        return observation

    measured: list[tuple[int, str]] = []
    for assignee, started_at, completed_at in rows:
        duration_ms = _bounded_int(
            (int(completed_at) - int(started_at)) * 1_000,
            maximum=3_600_000,
        )
        department = _bounded_text(assignee, 64).lower()
        if department and duration_ms > 0:
            measured.append((duration_ms, department))
    if not measured:
        return observation

    duration_ms, department = max(measured, key=lambda item: item[0])
    metadata.update(
        {
            "root_id": metadata.get("root_id") or root_id,
            "department": department,
            "observation_point": metadata.get("observation_point") or "ceo-ingress",
            "primary_bottleneck_department": department,
            "primary_bottleneck_duration_ms": duration_ms,
            "joint_improvement_targets": "ceo-workflow / observability",
            "latency_attribution_status": "MEASURED",
            "latency_attribution_method": "kanban-primary-duration-v1",
        }
    )
    return replace(observation, metadata=metadata)


def evaluate_observation(
    observation: TraceObservation,
    *,
    latency_warn_ms: int = 60_000,
    source_project: str = WORKFLOW_PROJECT_DEFAULT,
) -> EvaluationResult:
    """Derive deterministic operational findings without reading model content."""

    metadata = observation.metadata
    findings: list[str] = []
    summaries: list[str] = []
    if metadata.get("raw_payloads_sent") is True:
        findings.append("PRIVACY_PAYLOAD_PRESENT")
        summaries.append("trace payload privacy contract requires review")
    status = _bounded_text(metadata.get("status") or observation.status, 32).lower()
    error_count = _bounded_int(metadata.get("error_count"))
    if status in ERROR_STATUSES or error_count > 0:
        findings.append("WORKER_OR_WORKFLOW_DEGRADED")
        summaries.append("worker or workflow reported a non-success status")
        error_code = _bounded_text(
            metadata.get("error_code") or metadata.get("error_class"), 96
        )
        http_status = _bounded_int(metadata.get("http_status"))
        if error_code or http_status:
            detail = f"error={error_code or 'unknown'}"
            if http_status:
                detail += f" http_status={http_status}"
            summaries.append(detail)
    latency_ms = _bounded_int(metadata.get("latency_ms"))
    if latency_ms > latency_warn_ms:
        findings.append("LATENCY_ABOVE_THRESHOLD")
        latency_scope = _bounded_text(metadata.get("latency_scope"), 40)
        if latency_scope == "end_to_end":
            summaries.append("end-to-end latency exceeded the configured observation threshold")
        elif latency_scope == "worker_execution":
            summaries.append("worker execution latency exceeded the configured observation threshold")
        else:
            summaries.append("observed latency exceeded the configured observation threshold")
    is_metrics_window = metadata.get("source") == "metrics-window"
    if not is_metrics_window and not metadata.get("request_id") and not metadata.get("root_id"):
        findings.append("CORRELATION_METADATA_MISSING")
        summaries.append("request/root correlation metadata is missing")
    if not metadata.get("stage") and not metadata.get("department"):
        findings.append("DEPARTMENT_METADATA_MISSING")
        summaries.append("department or stage metadata is missing")
    score = _bounded_score(metadata.get("eval_score"))
    if score is not None and score < 0.8:
        findings.append("STRUCTURED_EVAL_SCORE_LOW")
        summaries.append("structured worker evaluation score is below the review threshold")
    semantic_score = _bounded_score(metadata.get("semantic_qa_score"))
    semantic_verdict = _bounded_text(metadata.get("semantic_qa_verdict"), 32).upper()
    if semantic_verdict == "FAIL":
        findings.append("SEMANTIC_QA_FAILED")
        summaries.append("answer contract semantic QA failed")
    elif semantic_score is not None and semantic_score < 0.8:
        findings.append("SEMANTIC_QA_SCORE_LOW")
        summaries.append("answer contract semantic QA score is below the review threshold")
    if score is None:
        score = semantic_score
    if not findings:
        decision = "OBSERVED_PASS"
        summaries.append("metadata-only trace passed operational checks")
    elif "PRIVACY_PAYLOAD_PRESENT" in findings:
        decision = "REVIEW_REQUIRED"
    else:
        decision = "IMPROVEMENT_CANDIDATE"
    safe_metadata = {
        "schema_version": FEEDBACK_SCHEMA,
        "source_run_id": observation.source_run_id,
        "source_project": _bounded_text(source_project, 120),
        "source_name": observation.name,
        "source": metadata.get("source"),
        "trace_id": metadata.get("trace_id"),
        "request_id": metadata.get("request_id"),
        "root_id": metadata.get("root_id"),
        "task_id": metadata.get("task_id"),
        "workflow_mode": metadata.get("workflow_mode"),
        "workflow_role": observation.workflow_role,
        "department": observation.department,
        "status": status,
        "error_code": metadata.get("error_code"),
        "error_class": metadata.get("error_class"),
        "http_status": _bounded_int(metadata.get("http_status")) or None,
        "trace_kind": metadata.get("trace_kind"),
        "latency_scope": metadata.get("latency_scope"),
        "observation_point": metadata.get("observation_point"),
        "primary_bottleneck_department": metadata.get("primary_bottleneck_department"),
        "primary_bottleneck_duration_ms": _bounded_int(
            metadata.get("primary_bottleneck_duration_ms")
        )
        or None,
        "joint_improvement_targets": metadata.get("joint_improvement_targets"),
        "latency_attribution_status": metadata.get("latency_attribution_status"),
        "latency_attribution_method": metadata.get("latency_attribution_method"),
        "latency_ms": latency_ms or None,
        "latency_threshold_ms": max(0, int(latency_warn_ms)),
        "p95_latency_ms": _bounded_int(metadata.get("p95_latency_ms")) or None,
        "metric_count": _bounded_int(metadata.get("metric_count")) or None,
        "window_start": metadata.get("window_start"),
        "window_end": metadata.get("window_end"),
        "error_count": error_count,
        "attempts": _bounded_int(metadata.get("attempts")) or None,
        "retries": _bounded_int(metadata.get("retries")) or None,
        "llm_calls": _bounded_int(metadata.get("llm_calls")) or None,
        "tool_calls": _bounded_int(metadata.get("tool_calls")) or None,
        "tool_error_count": _bounded_int(metadata.get("tool_error_count")) or None,
        "telemetry_completeness": metadata.get("telemetry_completeness"),
        "observability_source": metadata.get("observability_source"),
        "observation_unit": metadata.get("observation_unit"),
        "profile": metadata.get("profile"),
        "output_verdict": metadata.get("output_verdict"),
        "finding_count": _bounded_int(metadata.get("finding_count")) or None,
        "eval_score": score,
        "semantic_qa_version": metadata.get("semantic_qa_version"),
        "semantic_qa_evaluator": metadata.get("semantic_qa_evaluator"),
        "semantic_qa_verdict": semantic_verdict or None,
        "semantic_qa_score": semantic_score,
        "semantic_qa_completeness": _bounded_score(metadata.get("semantic_qa_completeness")),
        "semantic_qa_groundedness": _bounded_score(metadata.get("semantic_qa_groundedness")),
        "semantic_qa_temporal_consistency": _bounded_score(metadata.get("semantic_qa_temporal_consistency")),
        "semantic_qa_uncertainty_honesty": _bounded_score(metadata.get("semantic_qa_uncertainty_honesty")),
        "semantic_qa_relevance": _bounded_score(metadata.get("semantic_qa_relevance")),
        "semantic_qa_finding_count": _bounded_int(metadata.get("semantic_qa_finding_count")) or None,
        "semantic_qa_finding_codes": metadata.get("semantic_qa_finding_codes"),
        "raw_payloads_sent": False,
    }
    return EvaluationResult(
        source_run_id=observation.source_run_id,
        department=observation.department,
        workflow_role=observation.workflow_role,
        decision=decision,
        score=score,
        finding_codes=tuple(dict.fromkeys(findings)),
        summaries=tuple(dict.fromkeys(summaries)),
        metadata=safe_metadata,
    )


class FeedbackLedger:
    """Small append-oriented coordination store shared by worker and QA API."""

    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # Evaluation completion, Discord review, and benchmark updates can
        # briefly contend on the same small WAL database.  A 200ms budget made
        # harmless bursts spill into the retry queue; this plane is isolated
        # from business execution, so waiting up to two seconds is both safer
        # and faster than re-running an evaluation job.
        connection = sqlite3.connect(self.path, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=2000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS langsmith_feedback_jobs (
                    source_run_id TEXT PRIMARY KEY,
                    project_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    observation TEXT NOT NULL DEFAULT '{}',
                    eval_run_id TEXT,
                    last_error TEXT,
                    lease_until TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_jobs_status
                    ON langsmith_feedback_jobs(status, next_attempt_at);
                CREATE TABLE IF NOT EXISTS langsmith_feedback_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    source_run_id TEXT NOT NULL UNIQUE,
                    eval_run_id TEXT NOT NULL,
                    department TEXT NOT NULL,
                    department_key TEXT NOT NULL DEFAULT '',
                    decision TEXT NOT NULL,
                    score REAL,
                    finding_codes TEXT NOT NULL,
                    summaries TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS langsmith_feedback_decisions (
                    artifact_id TEXT PRIMARY KEY,
                    decision TEXT NOT NULL CHECK(decision IN ('APPROVED', 'REJECTED')),
                    approved_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    improvement_type TEXT NOT NULL DEFAULT 'NO_ACTION',
                    target_skill_slug TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS langsmith_feedback_benchmarks (
                    benchmark_job_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('PENDING', 'RUNNING', 'PASSED', 'FAILED')),
                    benchmark_id TEXT,
                    score REAL,
                    report_ref TEXT,
                    result_summary TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_benchmarks_status
                    ON langsmith_feedback_benchmarks(status, updated_at);
                CREATE TABLE IF NOT EXISTS langsmith_feedback_discord_deliveries (
                    artifact_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('CLAIMED', 'DELIVERED', 'FAILED_FINAL')),
                    discord_message_id TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS langsmith_feedback_semantic_keys (
                    semantic_key TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS langsmith_feedback_artifact_sources (
                    source_run_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_artifact_sources_artifact
                    ON langsmith_feedback_artifact_sources(artifact_id);
                """
            )
            artifact_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(langsmith_feedback_artifacts)").fetchall()
            }
            if "department_key" not in artifact_columns:
                db.execute(
                    "ALTER TABLE langsmith_feedback_artifacts ADD COLUMN department_key TEXT NOT NULL DEFAULT ''"
                )
            decision_columns = {
                str(row["name"])
                for row in db.execute(
                    "PRAGMA table_info(langsmith_feedback_decisions)"
                ).fetchall()
            }
            if "improvement_type" not in decision_columns:
                db.execute(
                    "ALTER TABLE langsmith_feedback_decisions ADD COLUMN "
                    "improvement_type TEXT NOT NULL DEFAULT 'NO_ACTION'"
                )
            if "target_skill_slug" not in decision_columns:
                db.execute(
                    "ALTER TABLE langsmith_feedback_decisions ADD COLUMN "
                    "target_skill_slug TEXT"
                )
            for row in db.execute(
                "SELECT artifact_id, department FROM langsmith_feedback_artifacts WHERE department_key=''"
            ).fetchall():
                db.execute(
                    "UPDATE langsmith_feedback_artifacts SET department_key=? WHERE artifact_id=?",
                    (canonical_department(row["department"]), row["artifact_id"]),
                )
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(langsmith_feedback_jobs)").fetchall()
            }
            if "observation" not in columns:
                db.execute(
                    "ALTER TABLE langsmith_feedback_jobs ADD COLUMN observation TEXT NOT NULL DEFAULT '{}'"
                )
            if "lease_until" not in columns:
                db.execute(
                    "ALTER TABLE langsmith_feedback_jobs ADD COLUMN lease_until TEXT"
                )
            # Seed the semantic index from existing artifacts.  If historical
            # duplicates exist, the oldest artifact owns the key; no approval
            # or audit row is deleted during migration.
            for row in db.execute(
                "SELECT * FROM langsmith_feedback_artifacts ORDER BY created_at"
            ).fetchall():
                try:
                    metadata = json.loads(row["metadata"] or "{}")
                    findings = json.loads(row["finding_codes"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    metadata, findings = {}, ()
                semantic_key = _feedback_semantic_key(
                    department=row["department_key"] or row["department"],
                    decision=row["decision"],
                    finding_codes=findings,
                    metadata=metadata if isinstance(metadata, Mapping) else {},
                )
                db.execute(
                    """INSERT OR IGNORE INTO langsmith_feedback_artifact_sources
                    (source_run_id, artifact_id, created_at) VALUES (?, ?, ?)""",
                    (row["source_run_id"], row["artifact_id"], row["created_at"]),
                )
                if semantic_key:
                    db.execute(
                        """INSERT OR IGNORE INTO langsmith_feedback_semantic_keys
                        (semantic_key, artifact_id, created_at) VALUES (?, ?, ?)""",
                        (semantic_key, row["artifact_id"], row["created_at"]),
                    )

    def enqueue(
        self,
        source_run_id: str,
        project_name: str,
        observation: TraceObservation | None = None,
    ) -> bool:
        now = _now()
        safe_observation = observation or TraceObservation(
            source_run_id=source_run_id,
            name="",
            status="",
            started_at=None,
            ended_at=None,
            metadata={},
        )
        try:
            with self._connect() as db:
                cursor = db.execute(
                    """INSERT OR IGNORE INTO langsmith_feedback_jobs
                    (source_run_id, project_name, status, next_attempt_at, observation, created_at, updated_at)
                    VALUES (?, ?, 'QUEUED', ?, ?, ?, ?)""",
                    (
                        source_run_id,
                        project_name,
                        now,
                        json.dumps(
                            {
                                "source_run_id": safe_observation.source_run_id,
                                "name": safe_observation.name,
                                "status": safe_observation.status,
                                "started_at": safe_observation.started_at,
                                "ended_at": safe_observation.ended_at,
                                "metadata": safe_observation.metadata,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        now,
                        now,
                    ),
                )
                return cursor.rowcount == 1
        except sqlite3.Error:
            LOGGER.exception("langsmith_feedback_enqueue_failed")
            return False

    def pending_count(self) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT count(*) AS count FROM langsmith_feedback_jobs WHERE status IN ('QUEUED', 'RETRY', 'RUNNING')"
            ).fetchone()
        return int(row["count"] if row else 0)

    def claim(self) -> sqlite3.Row | None:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        stale_before = (now_dt - timedelta(minutes=10)).isoformat()
        lease_until = (now_dt + timedelta(minutes=10)).isoformat()
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    """SELECT * FROM langsmith_feedback_jobs
                    WHERE (
                        (status IN ('QUEUED', 'RETRY') AND next_attempt_at <= ?)
                        OR (status='RUNNING' AND updated_at <= ?)
                    )
                    ORDER BY created_at LIMIT 1""",
                    (now, stale_before),
                ).fetchone()
                if row is None:
                    return None
                db.execute(
                    """UPDATE langsmith_feedback_jobs
                    SET status='RUNNING', attempts=attempts+1, lease_until=?, updated_at=?
                    WHERE source_run_id=? AND (
                        status IN ('QUEUED', 'RETRY')
                        OR (status='RUNNING' AND updated_at <= ?)
                    )""",
                    (lease_until, now, row["source_run_id"], stale_before),
                )
                return row
        except sqlite3.Error:
            LOGGER.exception("langsmith_feedback_claim_failed")
            return None

    def complete(self, source_run_id: str, eval_run_id: str, result: EvaluationResult) -> str:
        artifact_id = f"feedback-{uuid4().hex}"
        now = _now()
        semantic_key = _feedback_semantic_key(
            department=result.department,
            decision=result.decision,
            finding_codes=result.finding_codes,
            metadata=result.metadata,
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT artifact_id FROM langsmith_feedback_artifacts WHERE source_run_id=?",
                (source_run_id,),
            ).fetchone()
            if existing is None and semantic_key:
                existing = db.execute(
                    """SELECT a.artifact_id
                    FROM langsmith_feedback_semantic_keys semantic
                    JOIN langsmith_feedback_artifacts a
                      ON a.artifact_id=semantic.artifact_id
                    WHERE semantic.semantic_key=?""",
                    (semantic_key,),
                ).fetchone()
            if existing is not None:
                existing_artifact_id = str(existing["artifact_id"])
                db.execute(
                    """INSERT OR IGNORE INTO langsmith_feedback_artifact_sources
                    (source_run_id, artifact_id, created_at) VALUES (?, ?, ?)""",
                    (source_run_id, existing_artifact_id, now),
                )
                db.execute(
                    """UPDATE langsmith_feedback_jobs
                    SET status='COMPLETED', eval_run_id=?, last_error=NULL,
                        lease_until=NULL, updated_at=?
                    WHERE source_run_id=?""",
                    (eval_run_id, now, source_run_id),
                )
                return existing_artifact_id
            db.execute(
                """INSERT OR IGNORE INTO langsmith_feedback_artifacts
                (artifact_id, source_run_id, eval_run_id, department, department_key,
                 decision, score, finding_codes, summaries, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    artifact_id,
                    source_run_id,
                    eval_run_id,
                    result.department,
                    canonical_department(result.department),
                    result.decision,
                    result.score,
                    json.dumps(list(result.finding_codes), separators=(",", ":")),
                    json.dumps(list(result.summaries), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(result.metadata, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
            db.execute(
                "UPDATE langsmith_feedback_jobs SET status='COMPLETED', eval_run_id=?, last_error=NULL, lease_until=NULL, updated_at=? WHERE source_run_id=?",
                (eval_run_id, now, source_run_id),
            )
            db.execute(
                """INSERT OR IGNORE INTO langsmith_feedback_artifact_sources
                (source_run_id, artifact_id, created_at) VALUES (?, ?, ?)""",
                (source_run_id, artifact_id, now),
            )
            if semantic_key:
                db.execute(
                    """INSERT INTO langsmith_feedback_semantic_keys
                    (semantic_key, artifact_id, created_at) VALUES (?, ?, ?)""",
                    (semantic_key, artifact_id, now),
                )
        return artifact_id

    def retry(self, source_run_id: str, error_code: str, attempts: int) -> None:
        now = datetime.now(timezone.utc)
        delay = min(300, 2 ** min(max(attempts, 0), 8))
        next_at = (now + timedelta(seconds=delay)).isoformat()
        status = "FAILED" if attempts >= 5 else "RETRY"
        with self._connect() as db:
            db.execute(
                "UPDATE langsmith_feedback_jobs SET status=?, last_error=?, next_attempt_at=?, lease_until=NULL, updated_at=? WHERE source_run_id=?",
                (status, _bounded_text(error_code, 120), next_at, now.isoformat(), source_run_id),
            )

    def pending(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            rows = db.execute(
                """SELECT a.*, d.decision AS approval_decision, d.approved_by,
                    d.reason AS approval_reason, d.improvement_type,
                    d.target_skill_slug
                FROM langsmith_feedback_artifacts a
                LEFT JOIN langsmith_feedback_decisions d ON d.artifact_id=a.artifact_id
                WHERE d.artifact_id IS NULL ORDER BY a.created_at LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._artifact(row) for row in rows]

    def approve(
        self,
        artifact_id: str,
        decision: str,
        approved_by: str,
        reason: str,
        *,
        improvement_type: str = "NO_ACTION",
        target_skill_slug: str = "",
    ) -> bool:
        if decision not in {"APPROVED", "REJECTED"}:
            return False
        normalized_type = _bounded_text(improvement_type, 32).upper()
        if normalized_type not in IMPROVEMENT_TYPES:
            return False
        if decision == "APPROVED" and normalized_type == "NO_ACTION":
            return False
        normalized_slug = _bounded_text(target_skill_slug, 64).lower()
        if normalized_type == "SKILL_EVOLVE" and not normalized_slug:
            return False
        if normalized_type != "SKILL_EVOLVE":
            normalized_slug = ""
        try:
            with self._connect() as db:
                artifact = db.execute(
                    """SELECT artifact_id, decision, finding_codes
                    FROM langsmith_feedback_artifacts WHERE artifact_id=?""",
                    (artifact_id,),
                ).fetchone()
                if artifact is None:
                    return False
                if decision == "APPROVED":
                    finding_codes = set(json.loads(artifact["finding_codes"]))
                    if (
                        artifact["decision"] == "OBSERVED_PASS"
                        or not _ACTIONABLE_FEEDBACK_CODES.intersection(finding_codes)
                    ):
                        return False
                now = _now()
                cursor = db.execute(
                    """INSERT OR IGNORE INTO langsmith_feedback_decisions
                    (artifact_id, decision, approved_by, reason, improvement_type,
                     target_skill_slug, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        artifact_id,
                        decision,
                        _bounded_text(approved_by, 128),
                        _bounded_text(reason, 240),
                        normalized_type,
                        normalized_slug or None,
                        now,
                    ),
                )
                if cursor.rowcount == 1 and decision == "APPROVED":
                    db.execute(
                        """INSERT OR IGNORE INTO langsmith_feedback_benchmarks
                        (benchmark_job_id, artifact_id, status, created_at, updated_at)
                        VALUES (?, ?, 'PENDING', ?, ?)""",
                        (f"benchmark-{uuid4().hex}", artifact_id, now, now),
                    )
                return cursor.rowcount == 1
        except sqlite3.Error:
            LOGGER.exception("langsmith_feedback_decision_failed")
            return False

    def claim_discord_delivery(self, artifact_id: str) -> bool:
        """Claim one permanent delivery attempt for an artifact.

        Discord has no idempotency-key header.  A transport timeout can be an
        ambiguous commit, so this claim is never automatically replayed.
        """

        now = _now()
        try:
            with self._connect() as db:
                cursor = db.execute(
                    """INSERT OR IGNORE INTO langsmith_feedback_discord_deliveries
                    (artifact_id, status, created_at, updated_at)
                    SELECT artifact_id, 'CLAIMED', ?, ?
                    FROM langsmith_feedback_artifacts WHERE artifact_id=?""",
                    (now, now, artifact_id),
                )
                return cursor.rowcount == 1
        except sqlite3.Error:
            LOGGER.exception("langsmith_feedback_discord_claim_failed")
            return False

    def finish_discord_delivery(
        self,
        artifact_id: str,
        *,
        delivered: bool,
        discord_message_id: str = "",
        error_code: str = "",
    ) -> None:
        try:
            with self._connect() as db:
                db.execute(
                    """UPDATE langsmith_feedback_discord_deliveries
                    SET status=?, discord_message_id=?, error_code=?, updated_at=?
                    WHERE artifact_id=? AND status='CLAIMED'""",
                    (
                        "DELIVERED" if delivered else "FAILED_FINAL",
                        _bounded_text(discord_message_id, 80) or None,
                        _bounded_text(error_code, 120) or None,
                        _now(),
                        artifact_id,
                    ),
                )
        except sqlite3.Error:
            LOGGER.exception("langsmith_feedback_discord_finish_failed")

    def approved_hints(self, department: str | None, limit: int, max_chars: int) -> dict[str, Any] | None:
        limit = max(1, min(int(limit), 10))
        with self._connect() as db:
            if department:
                rows = db.execute(
                    """SELECT a.*, d.decision AS approval_decision, b.status AS benchmark_status
                    FROM langsmith_feedback_artifacts a
                    JOIN langsmith_feedback_decisions d ON d.artifact_id=a.artifact_id
                    JOIN langsmith_feedback_benchmarks b ON b.artifact_id=a.artifact_id
                    WHERE d.decision='APPROVED' AND b.status='PASSED'
                      AND d.improvement_type != 'NO_ACTION'
                      AND a.decision != 'OBSERVED_PASS'
                      AND a.department_key=?
                    ORDER BY a.created_at DESC LIMIT ?""",
                    (canonical_department(department), limit),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT a.*, d.decision AS approval_decision, b.status AS benchmark_status
                    FROM langsmith_feedback_artifacts a
                    JOIN langsmith_feedback_decisions d ON d.artifact_id=a.artifact_id
                    JOIN langsmith_feedback_benchmarks b ON b.artifact_id=a.artifact_id
                    WHERE d.decision='APPROVED' AND b.status='PASSED'
                      AND d.improvement_type != 'NO_ACTION'
                      AND a.decision != 'OBSERVED_PASS'
                    ORDER BY a.created_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        if not rows:
            return None
        items: list[dict[str, Any]] = []
        for row in rows:
            item = {
                "department": canonical_department(row["department"]),
                "decision": row["decision"],
                "finding_codes": json.loads(row["finding_codes"]),
                "summaries": json.loads(row["summaries"]),
                "source": "qa-approved-langsmith-feedback",
            }
            items.append(item)
        payload = {"schema_version": FEEDBACK_SCHEMA, "items": items}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > max_chars:
            payload["items"] = items[:1]
        return payload

    def benchmark_candidates(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return QA-approved, redacted candidates waiting for offline replay."""

        limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            rows = db.execute(
                """SELECT a.*, d.decision AS approval_decision,
                    d.approved_by, d.reason AS approval_reason,
                    d.improvement_type, d.target_skill_slug,
                    b.status AS benchmark_status, b.benchmark_id,
                    b.score AS benchmark_score, b.report_ref,
                    b.result_summary AS benchmark_result_summary
                FROM langsmith_feedback_artifacts a
                JOIN langsmith_feedback_decisions d ON d.artifact_id=a.artifact_id
                LEFT JOIN langsmith_feedback_benchmarks b ON b.artifact_id=a.artifact_id
                WHERE d.decision='APPROVED'
                  AND d.improvement_type != 'NO_ACTION'
                  AND a.decision != 'OBSERVED_PASS'
                  AND (b.status IS NULL OR b.status IN ('PENDING', 'FAILED'))
                ORDER BY a.created_at LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._artifact(row) for row in rows]

    def evolution_benchmark_candidates(
        self, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return first-approved skill findings awaiting admission benchmark."""

        limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute(
                """SELECT a.*, d.decision AS approval_decision,
                    d.approved_by, d.reason AS approval_reason,
                    d.improvement_type, d.target_skill_slug,
                    b.status AS benchmark_status, b.benchmark_id,
                    b.score AS benchmark_score, b.report_ref,
                    b.result_summary AS benchmark_result_summary
                FROM langsmith_feedback_artifacts a
                JOIN langsmith_feedback_decisions d ON d.artifact_id=a.artifact_id
                JOIN langsmith_feedback_benchmarks b ON b.artifact_id=a.artifact_id
                WHERE d.decision='APPROVED' AND b.status='PENDING'
                  AND d.improvement_type IN ('SKILL_CREATE', 'SKILL_EVOLVE')
                ORDER BY b.updated_at LIMIT ?""",
                (limit,),
            ).fetchall()
            items = [self._artifact(row) for row in rows]
            self._attach_source_runs(db, items)
        return items

    def evolution_ready(self, limit: int = 500) -> list[dict[str, Any]]:
        """Return benchmark-passed skill findings for idempotent reconciliation."""

        limit = max(1, min(int(limit), 1_000))
        with self._connect() as db:
            rows = db.execute(
                """SELECT a.*, d.decision AS approval_decision,
                    d.approved_by, d.reason AS approval_reason,
                    d.improvement_type, d.target_skill_slug,
                    b.status AS benchmark_status, b.benchmark_id,
                    b.score AS benchmark_score, b.report_ref,
                    b.result_summary AS benchmark_result_summary
                FROM langsmith_feedback_artifacts a
                JOIN langsmith_feedback_decisions d ON d.artifact_id=a.artifact_id
                JOIN langsmith_feedback_benchmarks b ON b.artifact_id=a.artifact_id
                WHERE d.decision='APPROVED' AND b.status='PASSED'
                  AND d.improvement_type IN ('SKILL_CREATE', 'SKILL_EVOLVE')
                ORDER BY b.updated_at LIMIT ?""",
                (limit,),
            ).fetchall()
            items = [self._artifact(row) for row in rows]
            self._attach_source_runs(db, items)
        return items

    @staticmethod
    def _attach_source_runs(
        db: sqlite3.Connection, items: list[dict[str, Any]]
    ) -> None:
        for item in items:
            item["source_run_ids"] = [
                str(row["source_run_id"])
                for row in db.execute(
                    """SELECT source_run_id
                    FROM langsmith_feedback_artifact_sources
                    WHERE artifact_id=? ORDER BY source_run_id""",
                    (item["artifact_id"],),
                ).fetchall()
            ]

    def update_benchmark(
        self,
        artifact_id: str,
        *,
        status: str,
        benchmark_id: str,
        score: float | None = None,
        report_ref: str = "",
        result_summary: str = "",
    ) -> bool:
        """Record only the offline benchmark gate result, never raw benchmark data."""

        if status not in {"RUNNING", "PASSED", "FAILED"} or not _bounded_text(benchmark_id, 160):
            return False
        bounded_score = _bounded_score(score)
        now = _now()
        try:
            with self._connect() as db:
                approved = db.execute(
                    """SELECT 1 FROM langsmith_feedback_decisions
                    WHERE artifact_id=? AND decision='APPROVED'""",
                    (artifact_id,),
                ).fetchone()
                if approved is None:
                    return False
                existing = db.execute(
                    "SELECT benchmark_job_id FROM langsmith_feedback_benchmarks WHERE artifact_id=?",
                    (artifact_id,),
                ).fetchone()
                if existing is None:
                    db.execute(
                        """INSERT INTO langsmith_feedback_benchmarks
                        (benchmark_job_id, artifact_id, status, benchmark_id, score, report_ref,
                         result_summary, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            f"benchmark-{uuid4().hex}",
                            artifact_id,
                            status,
                            _bounded_text(benchmark_id, 160),
                            bounded_score,
                            _bounded_text(report_ref, 240),
                            _bounded_text(result_summary, 240),
                            now,
                            now,
                        ),
                    )
                else:
                    db.execute(
                        """UPDATE langsmith_feedback_benchmarks
                        SET status=?, benchmark_id=?, score=?, report_ref=?,
                            result_summary=?, updated_at=?
                        WHERE artifact_id=?""",
                        (
                            status,
                            _bounded_text(benchmark_id, 160),
                            bounded_score,
                            _bounded_text(report_ref, 240),
                            _bounded_text(result_summary, 240),
                            now,
                            artifact_id,
                        ),
                    )
                return True
        except sqlite3.Error:
            LOGGER.exception("langsmith_feedback_benchmark_update_failed")
            return False

    @staticmethod
    def _artifact(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "artifact_id": row["artifact_id"],
            "source_run_id": row["source_run_id"],
            "eval_run_id": row["eval_run_id"],
            "department": row["department"],
            "decision": row["decision"],
            "score": row["score"],
            "finding_codes": json.loads(row["finding_codes"]),
            "summaries": json.loads(row["summaries"]),
            "metadata": json.loads(row["metadata"]),
            "created_at": row["created_at"],
            "approval_decision": row["approval_decision"],
            "approved_by": row["approved_by"] if "approved_by" in keys else None,
            "approval_reason": (
                row["approval_reason"] if "approval_reason" in keys else None
            ),
            "improvement_type": (
                row["improvement_type"] if "improvement_type" in keys else None
            ),
            "target_skill_slug": (
                row["target_skill_slug"] if "target_skill_slug" in keys else None
            ),
            "benchmark_status": (
                row["benchmark_status"] if "benchmark_status" in keys else None
            ),
            "benchmark_id": row["benchmark_id"] if "benchmark_id" in keys else None,
            "benchmark_score": (
                row["benchmark_score"] if "benchmark_score" in keys else None
            ),
            "benchmark_report_ref": (
                row["report_ref"] if "report_ref" in keys else None
            ),
            "benchmark_result_summary": (
                row["benchmark_result_summary"]
                if "benchmark_result_summary" in keys
                else None
            ),
        }

    def cleanup(self, retention_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))).isoformat()
        with self._connect() as db:
            db.execute(
                """DELETE FROM langsmith_feedback_decisions
                WHERE artifact_id IN (
                    SELECT artifact_id FROM langsmith_feedback_artifacts WHERE created_at < ?
                )""",
                (cutoff,),
            )
            db.execute(
                """DELETE FROM langsmith_feedback_benchmarks
                WHERE artifact_id IN (
                    SELECT artifact_id FROM langsmith_feedback_artifacts WHERE created_at < ?
                )""",
                (cutoff,),
            )
            db.execute(
                """DELETE FROM langsmith_feedback_discord_deliveries
                WHERE artifact_id IN (
                    SELECT artifact_id FROM langsmith_feedback_artifacts WHERE created_at < ?
                )""",
                (cutoff,),
            )
            db.execute(
                """DELETE FROM langsmith_feedback_semantic_keys
                WHERE artifact_id IN (
                    SELECT artifact_id FROM langsmith_feedback_artifacts WHERE created_at < ?
                )""",
                (cutoff,),
            )
            db.execute(
                """DELETE FROM langsmith_feedback_artifact_sources
                WHERE artifact_id IN (
                    SELECT artifact_id FROM langsmith_feedback_artifacts WHERE created_at < ?
                )""",
                (cutoff,),
            )
            artifact_cursor = db.execute(
                "DELETE FROM langsmith_feedback_artifacts WHERE created_at < ?",
                (cutoff,),
            )
            job_cursor = db.execute(
                "DELETE FROM langsmith_feedback_jobs WHERE status IN ('COMPLETED', 'FAILED') AND updated_at < ?",
                (cutoff,),
            )
            return int(artifact_cursor.rowcount or 0) + int(job_cursor.rowcount or 0)


def _evaluation_metadata(result: EvaluationResult) -> dict[str, Any]:
    has_semantic = result.metadata.get("semantic_qa_version") is not None
    return {
        **result.metadata,
        "schema_version": FEEDBACK_SCHEMA,
        "evaluation_type": (
            "metadata_contract+semantic_answer_contract"
            if has_semantic
            else "metadata_contract"
        ),
        "decision": result.decision,
        "finding_codes": list(result.finding_codes)[:12],
        "finding_count": len(result.finding_codes),
        "summaries": list(result.summaries)[:8],
        "qa_approval": "PENDING",
        "raw_payloads_sent": False,
    }


def evaluation_run_id(source_run_id: str, project_name: str) -> UUID:
    """Stable Evals run identity for crash/retry idempotency."""

    return uuid5(NAMESPACE_URL, f"hgfinance:{project_name}:{source_run_id}")


def publish_evaluation(result: EvaluationResult, project_name: str) -> str | None:
    """Publish one closed metadata-only evaluation run; failures are swallowed."""

    try:
        from langsmith import RunTree

        from orchestration.llm_observability import (
            _safe_langsmith_client,
            langsmith_enabled,
        )

        if not langsmith_enabled():
            return None
        run = RunTree(
            id=evaluation_run_id(result.source_run_id, project_name),
            name="qa.trace.evaluation",
            run_type="chain",
            project_name=project_name,
            inputs={},
            outputs={},
            extra={"metadata": _evaluation_metadata(result)},
            tags=["hgfinance", "qa", "evaluation", "redacted"],
            ls_client=_safe_langsmith_client(),
        )
        run.post()
        run.end(outputs={})
        run.patch(exclude_inputs=True)
        return str(run.id)
    except Exception:  # noqa: BLE001 - observability must never affect workflow
        LOGGER.exception("langsmith_feedback_publish_failed")
        return None


def _aggregate_metric_window(
    runs: Any,
    *,
    project_name: str,
    window_start: datetime,
    window_end: datetime,
) -> TraceObservation | None:
    """Reduce high-frequency metric runs to one bounded QA observation."""

    count = 0
    error_count = 0
    latencies: list[int] = []
    saw_error = False
    for run in runs:
        observation = observation_from_run(run)
        if not observation.source_run_id or not observation.ended_at:
            continue
        count += 1
        status = (observation.metadata.get("status") or observation.status or "").lower()
        saw_error = saw_error or status in ERROR_STATUSES
        error_count += _bounded_int(observation.metadata.get("error_count"))
        latency = _bounded_int(observation.metadata.get("latency_ms"))
        if latency:
            latencies.append(latency)
    if count == 0:
        return None
    latencies.sort()
    p95 = (
        latencies[min(len(latencies) - 1, max(0, int(len(latencies) * 0.95) - 1))]
        if latencies
        else 0
    )
    bucket_id = f"metrics-window:{project_name}:{int(window_start.timestamp())}"
    status = "degraded" if saw_error or error_count else "success"
    return TraceObservation(
        source_run_id=bucket_id,
        name="metrics.window",
        status=status,
        started_at=window_start.isoformat(),
        ended_at=window_end.isoformat(),
        metadata={
            "source": "metrics-window",
            "stage": "metrics-window",
            "department": "metrics",
            "status": status,
            "error_count": error_count,
            "latency_ms": p95,
            "p95_latency_ms": p95,
            "metric_count": count,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "raw_payloads_sent": False,
        },
    )


def _publish_qa_discord_request(
    ledger: FeedbackLedger,
    artifact_id: str,
    result: EvaluationResult,
) -> bool:
    """Publish one redacted QA card through the existing QA bot identity."""

    from orchestration.qa_discord_feedback import (
        format_qa_feedback_request,
        is_actionable_feedback,
        post_qa_discord_message,
        qa_feedback_channel_id,
    )

    if not is_actionable_feedback(result.finding_codes):
        return False
    channel_id = qa_feedback_channel_id()
    token = os.getenv("DISCORD_BOT_TOKEN_QA", "").strip()
    if not channel_id or not token:
        return False
    if not ledger.claim_discord_delivery(artifact_id):
        return False
    content = format_qa_feedback_request(
        artifact_id=artifact_id,
        department=result.department,
        decision=result.decision,
        finding_codes=result.finding_codes,
        summaries=result.summaries,
        metadata=result.metadata,
    )
    try:
        message_id = post_qa_discord_message(
            content, token=token, channel_id=channel_id
        )
        ledger.finish_discord_delivery(
            artifact_id,
            delivered=True,
            discord_message_id=message_id,
        )
        return True
    except HTTPError as exc:
        ledger.finish_discord_delivery(
            artifact_id,
            delivered=False,
            error_code=f"discord_http_{exc.code}",
        )
    except (URLError, TimeoutError, ValueError, RuntimeError, OSError) as exc:
        # A timeout may mean Discord committed the message.  Never replay an
        # ambiguous attempt; the artifact id in the card supports manual audit.
        ledger.finish_discord_delivery(
            artifact_id,
            delivered=False,
            error_code=type(exc).__name__,
        )
    return False


class LangSmithFeedbackService:
    """Bounded, asynchronous evaluator owned by the portfolio worker process."""

    def __init__(self, config: FeedbackConfig | None = None, ledger: FeedbackLedger | None = None) -> None:
        self.config = config or FeedbackConfig.from_env()
        try:
            self.ledger = ledger or FeedbackLedger(self.config.state_path)
        except Exception:  # noqa: BLE001 - local coordination is fail-open
            LOGGER.exception("langsmith_feedback_ledger_unavailable")
            self.ledger = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.config.mode == "off" or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="langsmith-feedback", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def run_once(self) -> dict[str, int]:
        if self.config.mode == "off" or self.ledger is None:
            return {"discovered": 0, "completed": 0, "failed": 0, "dropped": 0}
        discovered = completed = failed = dropped = 0
        try:
            from orchestration.llm_observability import langsmith_enabled

            if not langsmith_enabled():
                return {"discovered": 0, "completed": 0, "failed": 0, "dropped": 0}
            from langsmith import Client

            client = Client(
                hide_inputs=True,
                hide_outputs=True,
                hide_metadata=False,
                omit_traced_runtime_info=True,
            )
            now = datetime.now(timezone.utc)
            since = now - timedelta(seconds=self.config.lookback_seconds)
            # A root may start before the observation window and finish inside
            # it (the common case for a multi-worker advisory). SmithDB v2
            # bounds by start time, so retain a one-hour bounded prefix and
            # keep the end_time filter for completed roots. The SQLite
            # source-run key makes repeated pages idempotent.
            root_filter = f'gt(end_time, "{since.isoformat()}")'
            root_limit = min(100, max(self.config.batch_size * 4, self.config.batch_size))
            root_runs = query_runs(
                client,
                project_name=self.config.workflow_project,
                min_start_time=since - timedelta(hours=1),
                max_start_time=now,
                is_root=True,
                filter_expression=root_filter,
                page_size=root_limit,
                max_results=root_limit,
                # Root terminal outputs contain only the bounded error/status
                # envelope written by close_root_trace.  Selecting OUTPUTS is
                # required because LangSmith may retain initial metadata as
                # immutable; observation_from_run ignores every other output
                # key and never stores prompts or answers.
                selects=[
                    "ID",
                    "NAME",
                    "STATUS",
                    "ERROR",
                    "START_TIME",
                    "END_TIME",
                    "EXTRA",
                    "OUTPUTS",
                ],
            )
            for run in root_runs:
                observation = observation_from_run(run)
                if not _is_workflow_feedback_source(observation):
                    continue
                observation = attribute_workflow_bottleneck(
                    observation,
                    kanban_db_path=self.config.kanban_db_path,
                )
                if not observation.source_run_id or not observation.ended_at:
                    continue
                if observation.status not in TERMINAL_STATUSES:
                    continue
                if not observation.metadata.get("stage") and not observation.metadata.get("department"):
                    continue
                discovered += 1
                if self.ledger.pending_count() >= self.config.max_pending:
                    dropped += 1
                    continue
                self.ledger.enqueue(
                    observation.source_run_id,
                    self.config.workflow_project,
                    observation=observation,
                )
            # High-frequency metrics are aggregated into one completed
            # window. Evaluating every metric run would amplify QA work and
            # create an unbounded Evals project without adding signal.
            metrics_end_epoch = (
                int(now.timestamp()) // self.config.metrics_window_seconds
            ) * self.config.metrics_window_seconds
            metrics_start = datetime.fromtimestamp(
                metrics_end_epoch - self.config.metrics_window_seconds, timezone.utc
            )
            metrics_end = datetime.fromtimestamp(metrics_end_epoch, timezone.utc)
            metrics_observation = _aggregate_metric_window(
                query_runs(
                    client,
                    project_name=self.config.metrics_project,
                    min_start_time=metrics_start,
                    max_start_time=metrics_end,
                    is_root=True,
                    page_size=self.config.metrics_max_runs,
                    max_results=self.config.metrics_max_runs,
                    selects=["ID", "NAME", "STATUS", "START_TIME", "END_TIME", "EXTRA"],
                ),
                project_name=self.config.metrics_project,
                window_start=metrics_start,
                window_end=metrics_end,
            )
            if metrics_observation is not None:
                discovered += 1
                if self.ledger.pending_count() < self.config.max_pending:
                    self.ledger.enqueue(
                        metrics_observation.source_run_id,
                        self.config.metrics_project,
                        observation=metrics_observation,
                    )
                else:
                    dropped += 1
            for _ in range(self.config.batch_size):
                job = self.ledger.claim()
                if job is None:
                    break
                try:
                    raw_observation = json.loads(job["observation"] or "{}")
                    observation = TraceObservation(
                        source_run_id=_bounded_text(raw_observation.get("source_run_id"), 128),
                        name=_bounded_text(raw_observation.get("name"), 160),
                        status=_bounded_text(raw_observation.get("status"), 32).lower(),
                        started_at=raw_observation.get("started_at"),
                        ended_at=raw_observation.get("ended_at"),
                        metadata=dict(raw_observation.get("metadata") or {}),
                    )
                    result = evaluate_observation(
                        observation,
                        latency_warn_ms=self.config.latency_warn_ms,
                        source_project=str(job["project_name"] or self.config.workflow_project),
                    )
                    eval_run_id = publish_evaluation(result, self.config.evals_project)
                    if not eval_run_id:
                        raise RuntimeError("eval_publish_unavailable")
                    artifact_id = self.ledger.complete(job["source_run_id"], eval_run_id, result)
                    if self.config.mode == "active":
                        _publish_qa_discord_request(self.ledger, artifact_id, result)
                    completed += 1
                except Exception as exc:  # noqa: BLE001 - retry outside business path
                    failed += 1
                    self.ledger.retry(job["source_run_id"], type(exc).__name__, int(job["attempts"] or 0))
        except Exception as exc:  # noqa: BLE001 - LangSmith outage is fail-open
            LOGGER.warning("langsmith_feedback_poll_failed error=%s", type(exc).__name__)
        return {"discovered": discovered, "completed": completed, "failed": failed, "dropped": dropped}

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.run_once()
                if self.ledger is not None:
                    self.ledger.cleanup(self.config.retention_days)
            except Exception:  # noqa: BLE001
                LOGGER.exception("langsmith_feedback_loop_failed")
            self._stop.wait(max(0.1, self.config.poll_seconds - (time.monotonic() - started)))


_HINT_LOCK = threading.Lock()
_HINT_CACHE: tuple[float, dict[str, Any] | None] | None = None


def approved_feedback_hint() -> dict[str, Any] | None:
    """Return a tiny QA-approved hint for Hermes; never performs network I/O."""

    if feedback_mode() != "active":
        return None
    global _HINT_CACHE
    now = time.monotonic()
    with _HINT_LOCK:
        if _HINT_CACHE is not None and now - _HINT_CACHE[0] < 30.0:
            return _HINT_CACHE[1]
        try:
            config = FeedbackConfig.from_env()
            if not Path(config.state_path).exists():
                _HINT_CACHE = (now, None)
                return None
            hint = FeedbackLedger(config.state_path).approved_hints(
                department=None,
                limit=config.max_feedback_items,
                max_chars=config.max_feedback_chars,
            )
            _HINT_CACHE = (now, hint)
            return hint
        except Exception:  # noqa: BLE001 - feedback is advisory only
            _HINT_CACHE = (now, None)
            return None


__all__ = [
    "FEEDBACK_SCHEMA",
    "EvaluationResult",
    "FeedbackConfig",
    "FeedbackLedger",
    "LangSmithFeedbackService",
    "TraceObservation",
    "approved_feedback_hint",
    "evaluate_observation",
    "evaluation_run_id",
    "feedback_mode",
    "observation_from_run",
    "publish_evaluation",
]
