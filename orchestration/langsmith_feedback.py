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
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar
from urllib.error import HTTPError, URLError
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from orchestration.langsmith_queries import close_query_client, query_runs
from orchestration.qa_feedback_contract import (
    ACTIONABLE_FEEDBACK_CODES,
    IMPROVEMENT_TYPES,
    OBSERVABILITY_FINDINGS,
    PERFORMANCE_FINDINGS,
    REVIEW_DECISIONS,
    REVIEW_REQUIRED_FINDINGS,
    qa_approver_is_allowed,
    is_actionable_feedback,
)

LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")
_SQLITE_LOCK_RETRY_DELAYS = (0.0, 0.5, 1.0, 2.0)
_LEDGER_DERIVED_INDEX_MAINTENANCE = "derived_indexes_v1"

WORKFLOW_PROJECT_DEFAULT = "First"
EVALS_PROJECT_DEFAULT = "HgFinance-Evals"
FEEDBACK_SCHEMA = "hgfinance.observability.feedback.v1"
FEEDBACK_MODES = frozenset({"off", "shadow", "active"})
# Compatibility aliases for callers that imported the old private names.
_ACTIONABLE_FEEDBACK_CODES = ACTIONABLE_FEEDBACK_CODES

CORRELATION_AGGREGATION_WINDOW_SECONDS = 60 * 60

# Keep every human-facing review surface on the same bounded priority order.
# Required/privacy work and explicit improvement proposals deserve attention
# before routine performance noise; creation time remains the stable tie-break.
_PENDING_REVIEW_ORDER_SQL = """
    CASE a.decision
        WHEN 'REVIEW_REQUIRED' THEN 0
        WHEN 'EVOLUTION_PROPOSAL' THEN 1
        WHEN 'REVIEW_WORTHY' THEN 2
        WHEN 'IMPROVEMENT_CANDIDATE' THEN 3
        ELSE 4
    END,
    CASE COALESCE(json_extract(a.metadata, '$.review_class'), '')
        WHEN 'QUALITY_OR_WORKFLOW_REVIEW' THEN 0
        WHEN 'OBSERVABILITY_GAP' THEN 1
        WHEN 'PERFORMANCE_EVENT' THEN 2
        ELSE 3
    END,
    a.created_at,
    a.artifact_id
"""
_EXCLUDE_SYNTHETIC_CANARY_SQL = (
    "AND COALESCE(json_extract(a.metadata, '$.canary'), 0) != 1"
)


def evaluation_is_worthy(result: "EvaluationResult") -> bool:
    """Return true only for a bounded operational or quality review item."""

    return result.decision in REVIEW_DECISIONS and bool(
        ACTIONABLE_FEEDBACK_CODES.intersection(result.finding_codes)
    )


TERMINAL_STATUSES = frozenset(
    {"success", "completed", "complete", "error", "failed", "blocked", "degraded"}
)
ERROR_STATUSES = frozenset(
    {"error", "failed", "blocked", "degraded", "gave_up", "timed_out"}
)
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
        "analysis_mode",
        "workflow_role",
        "department",
        "stage",
        "worker_id",
        "role",
        "status",
        "terminal_status",
        "terminal_reason",
        "terminal_task_id",
        "terminal_department",
        "error_code",
        "error_class",
        "http_status",
        "error_count",
        "latency_ms",
        "attempts",
        "retries",
        "llm_calls",
        "prompt_tokens",
        "completion_tokens",
        "max_tokens",
        "tool_calls",
        "tool_error_count",
        "length_termination_count",
        "length_termination_rate",
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
        "configured_max_turns",
        "actual_turns",
        "observation_category",
        "department_key",
        "stage_status",
        "source",
        "metric_count",
        "worker_count",
        "token_observation_count",
        "cost_observation_count",
        "failed_count",
        "error_rate",
        "latency_sum_ms",
        "latency_min_ms",
        "latency_max_ms",
        "latency_avg_ms",
        "window_start",
        "window_end",
        "p95_latency_ms",
        "cost_usd",
        "cost_status",
        "cost_source",
        "hallucination_verdict",
        "hallucination_score",
        "harmfulness_verdict",
        "harmfulness_score",
        "relevance_score",
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
        "improvement_candidate",
        "review_class",
        "sample_count",
        "aggregation_window",
        "observation_started_at",
        "observation_ended_at",
    }
)

_TEXT_METADATA_KEYS = frozenset(
    {
        "request_id",
        "root_id",
        "task_id",
        "trace_id",
        "workflow_mode",
        "analysis_mode",
        "workflow_role",
        "department",
        "stage",
        "worker_id",
        "role",
        "status",
        "terminal_status",
        "terminal_reason",
        "terminal_task_id",
        "terminal_department",
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
        "review_class",
        "aggregation_window",
        "observation_started_at",
        "observation_ended_at",
        "cost_status",
        "cost_source",
        "hallucination_verdict",
        "harmfulness_verdict",
        "profile",
        "observation_unit",
        "workflow_root_task_id",
        "kanban_run_id",
        "telemetry_completeness",
        "observability_source",
        "output_verdict",
        "observation_category",
        "department_key",
        "stage_status",
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
        "prompt_tokens",
        "completion_tokens",
        "max_tokens",
        "tool_calls",
        "tool_error_count",
        "length_termination_count",
        "metric_count",
        "worker_count",
        "token_observation_count",
        "cost_observation_count",
        "failed_count",
        "latency_sum_ms",
        "latency_min_ms",
        "latency_max_ms",
        "latency_avg_ms",
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
        "configured_max_turns",
        "actual_turns",
        "sample_count",
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
        "hallucination_score",
        "harmfulness_score",
        "relevance_score",
    }
)


_FLOAT_METADATA_KEYS = frozenset(
    {"cost_usd", "error_rate", "length_termination_rate"}
)


def _normalized_metadata_value(key: str, value: Any) -> Any:
    """Copy one safe scalar from a run without ever retaining payload text."""

    if key == "tool_names":
        if not isinstance(value, (list, tuple)):
            return None
        return [
            _bounded_text(item, 80) for item in value[:32] if _bounded_text(item, 80)
        ]
    if key in _TEXT_METADATA_KEYS:
        return _bounded_text(value, 160)
    if key in _INT_METADATA_KEYS:
        return _bounded_int(value, maximum=3_600_000)
    if key in {"window_start", "window_end"}:
        return _bounded_text(value, 64)
    if key in _SCORE_METADATA_KEYS:
        return _bounded_score(value)
    if key in _FLOAT_METADATA_KEYS:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed >= 0 else None
    if key in {"raw_payloads_sent", "improvement_candidate"}:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized == "true":
                return True
            if normalized == "false":
                return False
        return None
    if key in {"latency_available", "tool_latency_available"}:
        return bool(value)
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _artifact_fingerprint(db: sqlite3.Connection) -> str:
    """Return a cheap revision for the artifact table used by maintenance."""

    row = db.execute(
        "SELECT count(*) AS count, COALESCE(max(created_at), '') AS latest "
        "FROM langsmith_feedback_artifacts"
    ).fetchone()
    return f"{int(row['count'] if row else 0)}:{str(row['latest'] if row else '')}"


def _mark_artifact_fingerprint(db: sqlite3.Connection, completed_at: str) -> None:
    """Advance the maintenance marker after a normal artifact transaction."""

    db.execute(
        "UPDATE langsmith_feedback_maintenance SET completed_at=?, state=? "
        "WHERE name=?",
        (completed_at, _artifact_fingerprint(db), _LEDGER_DERIVED_INDEX_MAINTENANCE),
    )


def _with_sqlite_lock_retry(operation: Callable[[], _T]) -> _T:
    """Retry only transient SQLite lock contention around one transaction."""

    for attempt, delay in enumerate(_SQLITE_LOCK_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            message = str(exc).casefold()
            if "locked" not in message and "busy" not in message:
                raise
            if attempt == len(_SQLITE_LOCK_RETRY_DELAYS) - 1:
                raise
    raise AssertionError("sqlite lock retry exhausted without a result")


def _bounded_text(value: Any, limit: int = 160) -> str:
    return " ".join(str(value or "").split())[:limit]


def canonical_department(value: Any) -> str:
    """Normalize a UI department code and a trace stage into one key."""

    candidate = _bounded_text(value, 64).lower()
    return _DEPARTMENT_CANONICAL.get(candidate, candidate)


def _observation_category(metadata: Mapping[str, Any]) -> str:
    """Classify one bounded observation for QA filtering."""

    source = _bounded_text(metadata.get("source"), 80)
    return {
        "metrics-window": "metrics",
        "conditional-execution-consumer": "conditional",
        "langfuse-workforce-observability": "workforce",
    }.get(source, "workflow")


def _aggregation_window(metadata: Mapping[str, Any]) -> str:
    """Return a deterministic UTC bucket for uncorrelated observations.

    A missing request/root ID is an observability defect, not permission to
    create one Discord card per run.  Prefer the producer's explicit window,
    then the bounded observation timestamps.  ``unknown`` is intentionally a
    stable last resort for old records that contain neither.
    """

    raw_value = (
        metadata.get("window_start")
        or metadata.get("observation_ended_at")
        or metadata.get("observation_started_at")
    )
    if not raw_value:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    bucket = int(parsed.timestamp()) // CORRELATION_AGGREGATION_WINDOW_SECONDS
    return str(bucket)


def _feedback_semantic_key(
    *,
    department: Any,
    decision: Any,
    finding_codes: Any,
    metadata: Mapping[str, Any],
) -> str | None:
    """Identify one finding without turning missing correlation into a flood.

    Correlated traces use the request/root identity.  Legacy or broken
    producers use a bounded aggregation identity made from source, role,
    department, finding set, and one UTC time bucket.  The latter intentionally
    groups the observability defect so its sample count can be reviewed once.
    """

    request_id = _bounded_text(metadata.get("request_id"), 160)
    root_id = _bounded_text(metadata.get("root_id"), 160)
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
    if not findings or normalized_decision == "OBSERVED_PASS":
        return None
    correlation_id = request_id or root_id
    source = _bounded_text(
        metadata.get("source")
        or metadata.get("source_project")
        or metadata.get("source_name"),
        120,
    )
    role = _bounded_text(
        metadata.get("workflow_role") or metadata.get("role"), 96
    ).lower()
    identity = {
        "schema": "hgfinance.qa-finding-identity.v1",
        "correlation_id": correlation_id,
        "department": canonical_department(department),
        "decision": normalized_decision,
        "finding_codes": findings,
        "latency_scope": _bounded_text(metadata.get("latency_scope"), 64).lower(),
    }
    if not correlation_id:
        identity["aggregation_scope"] = {
            "source": source,
            "workflow_role": role,
            "department": canonical_department(department),
            "window": _aggregation_window(metadata),
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
    candidate = (
        str(value if value is not None else os.getenv("LANGSMITH_FEEDBACK_MODE", "off"))
        .strip()
        .lower()
    )
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
    discord_retention_interval_seconds: float = 86_400.0

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
            workflow_project=os.getenv(
                "LANGSMITH_PROJECT", WORKFLOW_PROJECT_DEFAULT
            ).strip()
            or WORKFLOW_PROJECT_DEFAULT,
            metrics_project=os.getenv(
                "LANGSMITH_METRICS_PROJECT", "HgFinance-Metrics"
            ).strip()
            or "HgFinance-Metrics",
            evals_project=os.getenv(
                "LANGSMITH_EVALS_PROJECT", EVALS_PROJECT_DEFAULT
            ).strip()
            or EVALS_PROJECT_DEFAULT,
            state_path=os.getenv(
                "LANGSMITH_FEEDBACK_STATE_PATH",
                "/var/lib/portfolio/langsmith-feedback.sqlite3",
            ).strip()
            or "/var/lib/portfolio/langsmith-feedback.sqlite3",
            poll_seconds=_float("LANGSMITH_FEEDBACK_POLL_SECONDS", 30.0, 5.0, 300.0),
            # Discovery is based on completed roots' end_time, not only their
            # start_time.  Keep a bounded completion window so a long-running
            # workflow can still be found after it finishes without scanning
            # the whole project.
            lookback_seconds=_int(
                "LANGSMITH_FEEDBACK_LOOKBACK_SECONDS", 900, 30, 86_400
            ),
            batch_size=_int("LANGSMITH_FEEDBACK_BATCH_SIZE", 25, 1, 100),
            max_pending=_int("LANGSMITH_FEEDBACK_MAX_PENDING", 500, 10, 10_000),
            retention_days=_int("LANGSMITH_FEEDBACK_RETENTION_DAYS", 30, 1, 365),
            latency_warn_ms=_int(
                "LANGSMITH_FEEDBACK_LATENCY_WARN_MS", 60_000, 1_000, 3_600_000
            ),
            max_feedback_items=_int("LANGSMITH_FEEDBACK_MAX_ITEMS", 3, 1, 10),
            max_feedback_chars=_int("LANGSMITH_FEEDBACK_MAX_CHARS", 1_200, 200, 4_000),
            metrics_window_seconds=_int(
                "LANGSMITH_FEEDBACK_METRICS_WINDOW_SECONDS", 300, 60, 3_600
            ),
            # SmithDB v2 accepts at most 100 rows per page. Keep the bound
            # below that server-side limit so a bad tuning value cannot turn
            # the background, fail-open poller into a repeated 400 loop.
            metrics_max_runs=_int("LANGSMITH_FEEDBACK_METRICS_MAX_RUNS", 100, 1, 100),
            kanban_db_path=os.getenv("LANGSMITH_FEEDBACK_KANBAN_DB_PATH", "").strip()
            or None,
            discord_retention_interval_seconds=_float(
                "LANGSMITH_FEEDBACK_DISCORD_RETENTION_INTERVAL_SECONDS",
                86_400.0,
                60.0,
                604_800.0,
            ),
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
        value = (
            self.metadata.get("department") or self.metadata.get("stage") or "unknown"
        )
        return _bounded_text(value, 64).lower()

    @property
    def department_key(self) -> str:
        """Return the canonical QA routing key without losing raw evidence."""

        return canonical_department(self.department)

    @property
    def workflow_role(self) -> str:
        return _bounded_text(
            self.metadata.get("workflow_role") or self.metadata.get("role"), 64
        ).lower()


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


@dataclass(frozen=True)
class FeedbackDiscordRetentionSummary:
    """Bounded cleanup result for QA feedback cards only."""

    enabled: bool
    available: bool
    attempted: int = 0
    deleted: int = 0
    skipped_pending: int = 0
    skipped_malformed: int = 0
    failed: int = 0
    error_code: str | None = None


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
        with suppress(AttributeError, TypeError, ValueError):
            metadata["latency_ms"] = _bounded_int(
                max(0.0, (end_time - start_time).total_seconds()) * 1_000,
                maximum=3_600_000,
            )
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


def _review_class(findings: set[str]) -> str:
    """Map findings to an operational review lane, not an auto-fix claim."""

    if findings and findings <= PERFORMANCE_FINDINGS:
        return "PERFORMANCE_EVENT"
    if findings and findings <= OBSERVABILITY_FINDINGS:
        return "OBSERVABILITY_GAP"
    if findings & PERFORMANCE_FINDINGS and not findings & {
        "SEMANTIC_QA_FAILED",
        "SEMANTIC_QA_SCORE_LOW",
        "SEMANTIC_QA_RELEVANCE_LOW",
        "RELEVANCE_LOW",
        "HALLUCINATION_DETECTED",
        "HARMFUL_CONTENT_DETECTED",
    }:
        return "PERFORMANCE_EVENT"
    return "QUALITY_OR_WORKFLOW_REVIEW"


def _classify_decision(findings: set[str], metadata: Mapping[str, Any]) -> str:
    """Classify one observation conservatively.

    A single deterministic signal proves that a human should review it.  It
    does not prove that a Skill, prompt, or code change is the right remedy.
    ``IMPROVEMENT_CANDIDATE`` is therefore reserved for an explicit verified
    candidate lane (currently the persisted D5 QA lane) or a producer that
    supplies its own bounded candidate evidence.
    """

    if not findings:
        return "OBSERVED_PASS"
    if REVIEW_REQUIRED_FINDINGS.intersection(findings):
        return "REVIEW_REQUIRED"
    if metadata.get("evolution_proposal_id"):
        return "EVOLUTION_PROPOSAL"
    if metadata.get("improvement_candidate") is True:
        return "IMPROVEMENT_CANDIDATE"
    if (
        metadata.get("source") == "memo_harness_d5"
        and _bounded_text(metadata.get("candidate_type"), 80)
    ):
        return "IMPROVEMENT_CANDIDATE"
    return "REVIEW_WORTHY"


def _deduplicate_legacy_uncorrelated_artifacts(db: sqlite3.Connection) -> bool:
    """Merge old per-run correlation findings into one bounded artifact.

    This migration only touches artifacts with a missing request/root ID and
    only merges groups whose approval, benchmark, and Discord records do not
    conflict.  Any ambiguous group is left intact for manual audit.
    """

    rows = db.execute(
        """SELECT artifact_id, source_run_id, department, department_key,
            decision, finding_codes, summaries, metadata, created_at
        FROM langsmith_feedback_artifacts ORDER BY created_at, artifact_id"""
    ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
            findings = json.loads(row["finding_codes"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, Mapping) or not isinstance(findings, list):
            continue
        if not {
            str(code).strip().upper() for code in findings
        }.intersection({"CORRELATION_METADATA_MISSING"}):
            continue
        if _bounded_text(metadata.get("request_id"), 160) or _bounded_text(
            metadata.get("root_id"), 160
        ):
            continue
        semantic_key = _feedback_semantic_key(
            department=row["department_key"] or row["department"],
            decision=row["decision"],
            finding_codes=findings,
            metadata=metadata,
        )
        if semantic_key:
            groups.setdefault(semantic_key, []).append(row)

    merged = False
    related_tables = (
        "langsmith_feedback_decisions",
        "langsmith_feedback_benchmarks",
        "langsmith_feedback_discord_deliveries",
    )
    for group in groups.values():
        if len(group) < 2:
            continue
        keeper = group[0]
        duplicate_ids = [str(row["artifact_id"]) for row in group[1:]]
        can_merge = True
        for duplicate_id in duplicate_ids:
            for table in related_tables:
                keeper_has = db.execute(
                    f"SELECT 1 FROM {table} WHERE artifact_id=?",  # noqa: S608
                    (keeper["artifact_id"],),
                ).fetchone()
                duplicate_has = db.execute(
                    f"SELECT 1 FROM {table} WHERE artifact_id=?",  # noqa: S608
                    (duplicate_id,),
                ).fetchone()
                if keeper_has is not None and duplicate_has is not None:
                    can_merge = False
                    break
            if not can_merge:
                break
        if not can_merge:
            continue

        keeper_id = str(keeper["artifact_id"])
        try:
            keeper_codes = json.loads(keeper["finding_codes"] or "[]")
            keeper_summaries = json.loads(keeper["summaries"] or "[]")
            keeper_metadata = json.loads(keeper["metadata"] or "{}")
        except (TypeError, json.JSONDecodeError):
            keeper_codes, keeper_summaries, keeper_metadata = [], [], {}
        if not isinstance(keeper_codes, list):
            keeper_codes = []
        if not isinstance(keeper_summaries, list):
            keeper_summaries = []
        if not isinstance(keeper_metadata, Mapping):
            keeper_metadata = {}
        merged_codes = list(dict.fromkeys(str(code) for code in keeper_codes))
        merged_summaries = list(
            dict.fromkeys(str(summary) for summary in keeper_summaries)
        )
        for duplicate_id in duplicate_ids:
            duplicate = next(
                row for row in group if str(row["artifact_id"]) == duplicate_id
            )
            try:
                duplicate_codes = json.loads(duplicate["finding_codes"] or "[]")
                duplicate_summaries = json.loads(duplicate["summaries"] or "[]")
            except (TypeError, json.JSONDecodeError):
                duplicate_codes, duplicate_summaries = [], []
            if isinstance(duplicate_codes, list):
                merged_codes.extend(str(code) for code in duplicate_codes)
            if isinstance(duplicate_summaries, list):
                merged_summaries.extend(str(summary) for summary in duplicate_summaries)
            for table in related_tables:
                db.execute(
                    f"UPDATE {table} SET artifact_id=? WHERE artifact_id=?",  # noqa: S608
                    (keeper_id, duplicate_id),
                )
            db.execute(
                "UPDATE langsmith_feedback_artifact_sources SET artifact_id=? "
                "WHERE artifact_id=?",
                (keeper_id, duplicate_id),
            )
            db.execute(
                "DELETE FROM langsmith_feedback_semantic_keys WHERE artifact_id=?",
                (duplicate_id,),
            )
            db.execute(
                "DELETE FROM langsmith_feedback_artifacts WHERE artifact_id=?",
                (duplicate_id,),
            )

        source_count = db.execute(
            "SELECT count(*) FROM langsmith_feedback_artifact_sources "
            "WHERE artifact_id=?",
            (keeper_id,),
        ).fetchone()[0]
        keeper_metadata = dict(keeper_metadata)
        keeper_metadata["sample_count"] = max(1, int(source_count or 1))
        keeper_metadata.setdefault(
            "review_class", _review_class(set(merged_codes))
        )
        db.execute(
            """UPDATE langsmith_feedback_artifacts
            SET finding_codes=?, summaries=?, metadata=? WHERE artifact_id=?""",
            (
                json.dumps(list(dict.fromkeys(merged_codes))[:12], separators=(",", ":")),
                json.dumps(
                    list(dict.fromkeys(merged_summaries))[:8],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                json.dumps(keeper_metadata, ensure_ascii=False, separators=(",", ":")),
                keeper_id,
            ),
        )
        merged = True
    return merged


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
    raw_payloads_sent = metadata.get("raw_payloads_sent")
    if raw_payloads_sent is True:
        findings.append("PRIVACY_PAYLOAD_PRESENT")
        summaries.append("trace payload privacy contract requires review")
    redaction_marker_missing = (
        raw_payloads_sent is not False and raw_payloads_sent is not True
    )
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
            summaries.append(
                "end-to-end latency exceeded the configured observation threshold"
            )
        elif latency_scope == "worker_execution":
            summaries.append(
                "worker execution latency exceeded the configured observation threshold"
            )
        else:
            summaries.append(
                "observed latency exceeded the configured observation threshold"
            )
    is_metrics_window = metadata.get("source") == "metrics-window"
    observation_category = _observation_category(metadata)
    if (
        not is_metrics_window
        and not metadata.get("request_id")
        and not metadata.get("root_id")
    ):
        findings.append("CORRELATION_METADATA_MISSING")
        summaries.append("request/root correlation metadata is missing")
    if not metadata.get("stage") and not metadata.get("department"):
        findings.append("DEPARTMENT_METADATA_MISSING")
        summaries.append("department or stage metadata is missing")
    score = _bounded_score(metadata.get("eval_score"))
    if score is not None and score < 0.8:
        findings.append("STRUCTURED_EVAL_SCORE_LOW")
        summaries.append(
            "structured worker evaluation score is below the review threshold"
        )
    semantic_score = _bounded_score(metadata.get("semantic_qa_score"))
    semantic_verdict = _bounded_text(metadata.get("semantic_qa_verdict"), 32).upper()
    if semantic_verdict == "FAIL":
        findings.append("SEMANTIC_QA_FAILED")
        summaries.append("answer contract semantic QA failed")
    elif semantic_score is not None and semantic_score < 0.8:
        findings.append("SEMANTIC_QA_SCORE_LOW")
        summaries.append(
            "answer contract semantic QA score is below the review threshold"
        )
    semantic_relevance = _bounded_score(metadata.get("semantic_qa_relevance"))
    if semantic_relevance is not None and semantic_relevance < 0.8:
        findings.append("SEMANTIC_QA_RELEVANCE_LOW")
        summaries.append("answer relevance is below the review threshold")
    relevance_score = _bounded_score(metadata.get("relevance_score"))
    if relevance_score is not None and relevance_score < 0.8:
        findings.append("RELEVANCE_LOW")
        summaries.append("answer relevance score is below the review threshold")
    length_termination_rate = _bounded_score(
        metadata.get("length_termination_rate")
    )
    if length_termination_rate is not None and length_termination_rate >= 0.05:
        findings.append("LENGTH_TERMINATION_HIGH")
        summaries.append("model output hit its token limit too often")
    hallucination_verdict = _bounded_text(
        metadata.get("hallucination_verdict"), 32
    ).upper()
    if hallucination_verdict in {
        "FAIL",
        "UNSUPPORTED",
        "CONTRADICTED",
        "DETECTED",
    }:
        findings.append("HALLUCINATION_DETECTED")
        summaries.append("hallucination critic flagged the answer")
    harmfulness_verdict = _bounded_text(
        metadata.get("harmfulness_verdict"), 32
    ).upper()
    if harmfulness_verdict in {"FAIL", "UNSAFE", "HARMFUL", "DETECTED"}:
        findings.append("HARMFUL_CONTENT_DETECTED")
        summaries.append("harmfulness evaluator flagged the answer")
    if redaction_marker_missing:
        # Keep the established summary order for existing findings while
        # making an absent marker an explicit, reviewable finding.
        findings.append("REDACTION_MARKER_MISSING")
        summaries.append("trace payload redaction status is unverified")
    if score is None:
        score = semantic_score
    finding_set = set(findings)
    decision = _classify_decision(finding_set, metadata)
    if not findings:
        summaries.append("metadata-only trace passed operational checks")
    review_class = _review_class(finding_set) if findings else "NORMAL"
    observation_started_at = getattr(observation, "started_at", None)
    observation_ended_at = getattr(observation, "ended_at", None)
    department_key = getattr(
        observation,
        "department_key",
        canonical_department(observation.department),
    )
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
        "analysis_mode": metadata.get("analysis_mode"),
        "workflow_role": observation.workflow_role,
        "department": observation.department,
        "department_key": department_key,
        "stage": metadata.get("stage"),
        "stage_status": "PRESENT" if metadata.get("stage") else "MISSING",
        "observation_category": observation_category,
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
        "prompt_tokens": _bounded_int(metadata.get("prompt_tokens")) or None,
        "completion_tokens": _bounded_int(metadata.get("completion_tokens")) or None,
        "max_tokens": _bounded_int(metadata.get("max_tokens")) or None,
        "worker_count": _bounded_int(metadata.get("worker_count")) or None,
        "token_observation_count": _bounded_int(
            metadata.get("token_observation_count")
        )
        or None,
        "cost_observation_count": _bounded_int(
            metadata.get("cost_observation_count")
        )
        or None,
        "failed_count": _bounded_int(metadata.get("failed_count")) or None,
        "error_rate": (
            metadata.get("error_rate")
            if isinstance(metadata.get("error_rate"), (int, float))
            else None
        ),
        "latency_sum_ms": _bounded_int(metadata.get("latency_sum_ms")) or None,
        "latency_min_ms": _bounded_int(metadata.get("latency_min_ms")) or None,
        "latency_max_ms": _bounded_int(metadata.get("latency_max_ms")) or None,
        "latency_avg_ms": _bounded_int(metadata.get("latency_avg_ms")) or None,
        "error_count": error_count,
        "attempts": _bounded_int(metadata.get("attempts")) or None,
        "retries": _bounded_int(metadata.get("retries")) or None,
        "llm_calls": _bounded_int(metadata.get("llm_calls")) or None,
        "tool_calls": _bounded_int(metadata.get("tool_calls")) or None,
        "tool_error_count": _bounded_int(metadata.get("tool_error_count")) or None,
        "length_termination_count": _bounded_int(
            metadata.get("length_termination_count")
        )
        or None,
        "length_termination_rate": (
            metadata.get("length_termination_rate")
            if isinstance(metadata.get("length_termination_rate"), (int, float))
            else None
        ),
        "telemetry_completeness": metadata.get("telemetry_completeness"),
        "observability_source": metadata.get("observability_source"),
        "observation_unit": metadata.get("observation_unit"),
        "profile": metadata.get("profile"),
        "configured_max_turns": (
            _bounded_int(metadata.get("configured_max_turns"))
            if metadata.get("configured_max_turns") is not None
            else None
        ),
        "actual_turns": (
            _bounded_int(metadata.get("actual_turns"))
            if metadata.get("actual_turns") is not None
            else None
        ),
        "llm_turn_count_observed": (
            _bounded_int(metadata.get("llm_turn_count_observed"))
            if metadata.get("llm_turn_count_observed") is not None
            else None
        ),
        "output_verdict": metadata.get("output_verdict"),
        "finding_count": _bounded_int(metadata.get("finding_count")) or None,
        "eval_score": score,
        "semantic_qa_version": metadata.get("semantic_qa_version"),
        "semantic_qa_evaluator": metadata.get("semantic_qa_evaluator"),
        "semantic_qa_verdict": semantic_verdict or None,
        "semantic_qa_score": semantic_score,
        "semantic_qa_completeness": _bounded_score(
            metadata.get("semantic_qa_completeness")
        ),
        "semantic_qa_groundedness": _bounded_score(
            metadata.get("semantic_qa_groundedness")
        ),
        "semantic_qa_temporal_consistency": _bounded_score(
            metadata.get("semantic_qa_temporal_consistency")
        ),
        "semantic_qa_uncertainty_honesty": _bounded_score(
            metadata.get("semantic_qa_uncertainty_honesty")
        ),
        "semantic_qa_relevance": _bounded_score(metadata.get("semantic_qa_relevance")),
        "semantic_qa_finding_count": _bounded_int(
            metadata.get("semantic_qa_finding_count")
        )
        or None,
        "semantic_qa_finding_codes": metadata.get("semantic_qa_finding_codes"),
        "review_class": review_class,
        "sample_count": 1,
        "aggregation_window": _aggregation_window(
            {
                **metadata,
                "observation_started_at": observation_started_at,
                "observation_ended_at": observation_ended_at,
            }
        ),
        "observation_started_at": observation_started_at,
        "observation_ended_at": observation_ended_at,
        "improvement_candidate": (
            decision == "IMPROVEMENT_CANDIDATE"
            if metadata.get("improvement_candidate") is True
            else None
        ),
        "cost_usd": (
            metadata.get("cost_usd")
            if isinstance(metadata.get("cost_usd"), (int, float))
            else None
        ),
        "cost_status": metadata.get("cost_status"),
        "cost_source": metadata.get("cost_source"),
        "hallucination_verdict": hallucination_verdict or None,
        "hallucination_score": _bounded_score(metadata.get("hallucination_score")),
        "harmfulness_verdict": harmfulness_verdict or None,
        "harmfulness_score": _bounded_score(metadata.get("harmfulness_score")),
        "relevance_score": relevance_score,
        "raw_payloads_sent": (
            raw_payloads_sent if isinstance(raw_payloads_sent, bool) else None
        ),
        "redaction_status": (
            "PAYLOAD_PRESENT"
            if raw_payloads_sent is True
            else "VERIFIED_SAFE"
            if raw_payloads_sent is False
            else "UNVERIFIED"
        ),
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
        # briefly contend on the same small WAL database.  This plane is
        # isolated from business execution, so a bounded five-second wait is
        # safer and faster than re-running an evaluation job.
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _init_schema(self) -> None:
        _with_sqlite_lock_retry(self._init_schema_once)

    def _init_schema_once(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            legacy_state_migrated = False
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
                    decision TEXT NOT NULL CHECK(
                        decision IN ('APPROVED', 'REJECTED', 'CLOSED_NO_ACTION')
                    ),
                    approved_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    improvement_type TEXT NOT NULL DEFAULT 'NO_ACTION',
                    target_skill_slug TEXT,
                    task_activation TEXT NOT NULL DEFAULT '',
                    mandatory_controls TEXT NOT NULL DEFAULT '[]',
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
                    updated_at TEXT NOT NULL,
                    discord_deleted_at TEXT
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
                CREATE TABLE IF NOT EXISTS langsmith_feedback_maintenance (
                    name TEXT PRIMARY KEY,
                    completed_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS langsmith_feedback_manual_labels (
                    artifact_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL CHECK(
                        label IN ('REVIEW', 'NO_ACTION', 'INSUFFICIENT_EVIDENCE')
                    ),
                    labeled_by TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            maintenance_columns = {
                str(row["name"])
                for row in db.execute(
                    "PRAGMA table_info(langsmith_feedback_maintenance)"
                ).fetchall()
            }
            if "state" not in maintenance_columns:
                db.execute(
                    "ALTER TABLE langsmith_feedback_maintenance "
                    "ADD COLUMN state TEXT NOT NULL DEFAULT ''"
                )
            current_artifact_fingerprint = _artifact_fingerprint(db)
            maintenance_row = db.execute(
                "SELECT completed_at, state FROM langsmith_feedback_maintenance "
                "WHERE name=?",
                (_LEDGER_DERIVED_INDEX_MAINTENANCE,),
            ).fetchone()
            derived_indexes_ready = bool(
                maintenance_row is not None
                and str(maintenance_row["state"] or "")
                == current_artifact_fingerprint
            )
            artifact_columns = {
                str(row["name"])
                for row in db.execute(
                    "PRAGMA table_info(langsmith_feedback_artifacts)"
                ).fetchall()
            }
            if "department_key" not in artifact_columns:
                db.execute(
                    "ALTER TABLE langsmith_feedback_artifacts ADD COLUMN department_key TEXT NOT NULL DEFAULT ''"
                )
            # ``APPROVED + NO_ACTION`` was historically possible in the
            # persisted ledger even though the current API rejected it.  Move
            # that legacy combination to an explicit terminal decision while
            # retaining the original actor, reason, and timestamp.
            decision_schema_row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='langsmith_feedback_decisions'"
            ).fetchone()
            decision_schema = str(decision_schema_row[0] or "") if decision_schema_row else ""
            if "CLOSED_NO_ACTION" not in decision_schema:
                legacy_state_migrated = True
                db.execute(
                    "ALTER TABLE langsmith_feedback_decisions "
                    "RENAME TO langsmith_feedback_decisions_legacy"
                )
                legacy_columns = {
                    str(row["name"])
                    for row in db.execute(
                        "PRAGMA table_info(langsmith_feedback_decisions_legacy)"
                    ).fetchall()
                }
                if "improvement_type" not in legacy_columns:
                    db.execute(
                        "ALTER TABLE langsmith_feedback_decisions_legacy "
                        "ADD COLUMN improvement_type TEXT NOT NULL DEFAULT 'NO_ACTION'"
                    )
                if "target_skill_slug" not in legacy_columns:
                    db.execute(
                        "ALTER TABLE langsmith_feedback_decisions_legacy "
                        "ADD COLUMN target_skill_slug TEXT"
                    )
                if "task_activation" not in legacy_columns:
                    db.execute(
                        "ALTER TABLE langsmith_feedback_decisions_legacy "
                        "ADD COLUMN task_activation TEXT NOT NULL DEFAULT ''"
                    )
                if "mandatory_controls" not in legacy_columns:
                    db.execute(
                        "ALTER TABLE langsmith_feedback_decisions_legacy "
                        "ADD COLUMN mandatory_controls TEXT NOT NULL DEFAULT '[]'"
                    )
                db.execute(
                    """
                    CREATE TABLE langsmith_feedback_decisions (
                        artifact_id TEXT PRIMARY KEY,
                        decision TEXT NOT NULL CHECK(
                            decision IN ('APPROVED', 'REJECTED', 'CLOSED_NO_ACTION')
                        ),
                        approved_by TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        improvement_type TEXT NOT NULL DEFAULT 'NO_ACTION',
                        target_skill_slug TEXT,
                        task_activation TEXT NOT NULL DEFAULT '',
                        mandatory_controls TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL
                    )
                    """
                )
                db.execute(
                    """
                    INSERT INTO langsmith_feedback_decisions
                    (artifact_id, decision, approved_by, reason, improvement_type,
                     target_skill_slug, task_activation, mandatory_controls, created_at)
                    SELECT artifact_id,
                           CASE
                               WHEN decision='APPROVED' AND improvement_type='NO_ACTION'
                               THEN 'CLOSED_NO_ACTION'
                               ELSE decision
                           END,
                           approved_by, reason, improvement_type,
                           target_skill_slug, task_activation, mandatory_controls, created_at
                    FROM langsmith_feedback_decisions_legacy
                    """
                )
                db.execute("DROP TABLE langsmith_feedback_decisions_legacy")

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
            if "task_activation" not in decision_columns:
                db.execute(
                    "ALTER TABLE langsmith_feedback_decisions ADD COLUMN "
                    "task_activation TEXT NOT NULL DEFAULT ''"
                )
            if "mandatory_controls" not in decision_columns:
                db.execute(
                    "ALTER TABLE langsmith_feedback_decisions ADD COLUMN "
                    "mandatory_controls TEXT NOT NULL DEFAULT '[]'"
                )
            delivery_columns = {
                str(row["name"])
                for row in db.execute(
                    "PRAGMA table_info(langsmith_feedback_discord_deliveries)"
                ).fetchall()
            }
            if "discord_deleted_at" not in delivery_columns:
                db.execute(
                    "ALTER TABLE langsmith_feedback_discord_deliveries "
                    "ADD COLUMN discord_deleted_at TEXT"
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_feedback_discord_retention "
                "ON langsmith_feedback_discord_deliveries(discord_deleted_at, created_at)"
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
                for row in db.execute(
                    "PRAGMA table_info(langsmith_feedback_jobs)"
                ).fetchall()
            }
            if "observation" not in columns:
                db.execute(
                    "ALTER TABLE langsmith_feedback_jobs ADD COLUMN observation TEXT NOT NULL DEFAULT '{}'"
                )
            if "lease_until" not in columns:
                db.execute(
                    "ALTER TABLE langsmith_feedback_jobs ADD COLUMN lease_until TEXT"
                )
            if not derived_indexes_ready:
                # Before the state split, every non-pass finding was labelled as
                # ``IMPROVEMENT_CANDIDATE``.  That was a review queue label, not
                # evidence of a proposed change.  Migrate historical automatic
                # labels to the honest state while retaining explicit D5/producer
                # candidates.  Derived semantic keys are rebuilt if any state
                # changed so future completions still deduplicate correctly.
                for row in db.execute(
                    "SELECT artifact_id, decision, finding_codes, metadata "
                    "FROM langsmith_feedback_artifacts"
                ).fetchall():
                    if row["decision"] != "IMPROVEMENT_CANDIDATE":
                        continue
                    try:
                        metadata = json.loads(row["metadata"] or "{}")
                        findings = json.loads(row["finding_codes"] or "[]")
                    except (TypeError, json.JSONDecodeError):
                        metadata, findings = {}, []
                    explicit_candidate = (
                        isinstance(metadata, Mapping)
                        and (
                            metadata.get("improvement_candidate") is True
                            or (
                                metadata.get("source") == "memo_harness_d5"
                                and _bounded_text(metadata.get("candidate_type"), 80)
                            )
                        )
                    )
                    if explicit_candidate:
                        continue
                    if not isinstance(findings, list):
                        findings = []
                    migrated_metadata = (
                        dict(metadata) if isinstance(metadata, Mapping) else {}
                    )
                    source_count = db.execute(
                        "SELECT count(*) FROM langsmith_feedback_artifact_sources "
                        "WHERE artifact_id=?",
                        (row["artifact_id"],),
                    ).fetchone()[0]
                    migrated_metadata.setdefault(
                        "review_class", _review_class(set(str(code) for code in findings))
                    )
                    migrated_metadata["sample_count"] = max(1, int(source_count or 1))
                    db.execute(
                        "UPDATE langsmith_feedback_artifacts SET decision=?, metadata=? "
                        "WHERE artifact_id=?",
                        (
                            "REVIEW_WORTHY",
                            json.dumps(
                                migrated_metadata,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            row["artifact_id"],
                        ),
                    )
                    legacy_state_migrated = True
            def seed_semantic_index() -> None:
                """Seed derived indexes after any legacy artifact migration."""

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

            # Seed the semantic index from existing artifacts only once per
            # ledger version. New completions write both derived rows in the
            # same transaction, so repeating this full JSON scan on every
            # FeedbackLedger construction only adds startup contention.
            if not derived_indexes_ready or legacy_state_migrated:
                if legacy_state_migrated:
                    db.execute("DELETE FROM langsmith_feedback_semantic_keys")
                seed_semantic_index()
                if _deduplicate_legacy_uncorrelated_artifacts(db):
                    db.execute("DELETE FROM langsmith_feedback_semantic_keys")
                    seed_semantic_index()
                db.execute(
                    "INSERT INTO langsmith_feedback_maintenance"
                    "(name, completed_at, state) VALUES (?, ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET completed_at=excluded.completed_at, "
                    "state=excluded.state",
                    (
                        _LEDGER_DERIVED_INDEX_MAINTENANCE,
                        _now(),
                        _artifact_fingerprint(db),
                    ),
                )
            # Discord 429 means the request was rate-limited before message
            # creation. Those claims are safe to retry; ambiguous timeouts and
            # readback failures remain FAILED_FINAL to avoid duplicates.
            db.execute(
                """DELETE FROM langsmith_feedback_discord_deliveries
                WHERE status='FAILED_FINAL' AND error_code='discord_http_429'"""
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

    def complete(
        self, source_run_id: str, eval_run_id: str, result: EvaluationResult
    ) -> str:
        return _with_sqlite_lock_retry(
            lambda: self._complete_once(source_run_id, eval_run_id, result)
        )

    def _complete_once(
        self, source_run_id: str, eval_run_id: str, result: EvaluationResult
    ) -> str:
        artifact_id = f"feedback-{uuid4().hex}"
        now = _now()
        artifact_metadata = dict(result.metadata)
        artifact_metadata.setdefault("sample_count", 1)
        artifact_metadata.setdefault("last_observed_at", now)
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
                source_cursor = db.execute(
                    """INSERT OR IGNORE INTO langsmith_feedback_artifact_sources
                    (source_run_id, artifact_id, created_at) VALUES (?, ?, ?)""",
                    (source_run_id, existing_artifact_id, now),
                )
                source_count_row = db.execute(
                    """SELECT count(*) AS count
                    FROM langsmith_feedback_artifact_sources
                    WHERE artifact_id=?""",
                    (existing_artifact_id,),
                ).fetchone()
                sample_count = int(source_count_row["count"] if source_count_row else 1)
                if source_cursor.rowcount == 1:
                    current = db.execute(
                        """SELECT finding_codes, summaries, metadata
                        FROM langsmith_feedback_artifacts WHERE artifact_id=?""",
                        (existing_artifact_id,),
                    ).fetchone()
                    if current is not None:
                        try:
                            existing_codes = json.loads(current["finding_codes"] or "[]")
                            existing_summaries = json.loads(current["summaries"] or "[]")
                            existing_metadata = json.loads(current["metadata"] or "{}")
                        except (TypeError, json.JSONDecodeError):
                            existing_codes, existing_summaries, existing_metadata = (
                                [],
                                [],
                                {},
                            )
                        if not isinstance(existing_codes, list):
                            existing_codes = []
                        if not isinstance(existing_summaries, list):
                            existing_summaries = []
                        merged_codes = list(
                            dict.fromkeys(
                                [
                                    *(item for item in existing_codes if isinstance(item, str)),
                                    *result.finding_codes,
                                ]
                            )
                        )[:12]
                        merged_summaries = list(
                            dict.fromkeys(
                                [
                                    *(
                                        item
                                        for item in existing_summaries
                                        if isinstance(item, str)
                                    ),
                                    *result.summaries,
                                ]
                            )
                        )[:8]
                        merged_metadata = (
                            dict(existing_metadata)
                            if isinstance(existing_metadata, Mapping)
                            else {}
                        )
                        merged_metadata.update(
                            {
                                "sample_count": sample_count,
                                "last_observed_at": now,
                            }
                        )
                        db.execute(
                            """UPDATE langsmith_feedback_artifacts
                            SET finding_codes=?, summaries=?, metadata=?
                            WHERE artifact_id=?""",
                            (
                                json.dumps(merged_codes, separators=(",", ":")),
                                json.dumps(
                                    merged_summaries,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                json.dumps(
                                    merged_metadata,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                existing_artifact_id,
                            ),
                        )
                db.execute(
                    """UPDATE langsmith_feedback_jobs
                    SET status='COMPLETED', eval_run_id=?, last_error=NULL,
                        lease_until=NULL, updated_at=?
                    WHERE source_run_id=?""",
                    (eval_run_id, now, source_run_id),
                )
                _mark_artifact_fingerprint(db, now)
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
                    json.dumps(
                        list(result.summaries),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        artifact_metadata, ensure_ascii=False, separators=(",", ":")
                    ),
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
            _mark_artifact_fingerprint(db, now)
        return artifact_id

    def skip(self, source_run_id: str) -> None:
        """Mark a normal observation handled without creating a QA artifact."""

        now = _now()
        with self._connect() as db:
            db.execute(
                """UPDATE langsmith_feedback_jobs
                SET status='COMPLETED', eval_run_id=NULL, last_error=NULL,
                    lease_until=NULL, updated_at=?
                WHERE source_run_id=?""",
                (now, source_run_id),
            )

    def retry(self, source_run_id: str, error_code: str, attempts: int) -> None:
        now = datetime.now(timezone.utc)
        delay = min(300, 2 ** min(max(attempts, 0), 8))
        next_at = (now + timedelta(seconds=delay)).isoformat()
        status = "FAILED" if attempts >= 5 else "RETRY"
        with self._connect() as db:
            db.execute(
                "UPDATE langsmith_feedback_jobs SET status=?, last_error=?, next_attempt_at=?, lease_until=NULL, updated_at=? WHERE source_run_id=?",
                (
                    status,
                    _bounded_text(error_code, 120),
                    next_at,
                    now.isoformat(),
                    source_run_id,
                ),
            )

    def pending(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT a.*, d.decision AS approval_decision, d.approved_by,
                    d.reason AS approval_reason, d.improvement_type,
                    d.target_skill_slug, d.task_activation, d.mandatory_controls
                FROM langsmith_feedback_artifacts a
                LEFT JOIN langsmith_feedback_decisions d ON d.artifact_id=a.artifact_id
                WHERE d.artifact_id IS NULL
                  AND a.decision != 'OBSERVED_PASS'
                  {_EXCLUDE_SYNTHETIC_CANARY_SQL}
                ORDER BY {_PENDING_REVIEW_ORDER_SQL}
                LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._artifact(row) for row in rows]

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        """Read one redacted artifact with its current aggregate count."""

        with self._connect() as db:
            row = db.execute(
                """SELECT a.*, d.decision AS approval_decision, d.approved_by,
                    d.reason AS approval_reason, d.improvement_type,
                    d.target_skill_slug, d.task_activation, d.mandatory_controls,
                    b.status AS benchmark_status,
                    b.benchmark_id, b.score AS benchmark_score,
                    b.report_ref, b.result_summary AS benchmark_result_summary
                FROM langsmith_feedback_artifacts a
                LEFT JOIN langsmith_feedback_decisions d ON d.artifact_id=a.artifact_id
                LEFT JOIN langsmith_feedback_benchmarks b ON b.artifact_id=a.artifact_id
                WHERE a.artifact_id=?""",
                (artifact_id,),
            ).fetchone()
        return self._artifact(row) if row is not None else None

    def record_manual_label(
        self,
        artifact_id: str,
        *,
        label: str,
        labeled_by: str,
        rationale: str,
    ) -> bool:
        """Persist one redacted adjudication without changing QA lifecycle."""

        normalized_label = _bounded_text(label, 32).upper()
        if normalized_label not in {
            "REVIEW",
            "NO_ACTION",
            "INSUFFICIENT_EVIDENCE",
        }:
            return False
        if not _bounded_text(labeled_by, 128) or not _bounded_text(rationale, 240):
            return False
        try:
            with self._connect() as db:
                artifact = db.execute(
                    "SELECT 1 FROM langsmith_feedback_artifacts WHERE artifact_id=?",
                    (artifact_id,),
                ).fetchone()
                if artifact is None:
                    return False
                cursor = db.execute(
                    """INSERT OR IGNORE INTO langsmith_feedback_manual_labels
                    (artifact_id, label, labeled_by, rationale, created_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        artifact_id,
                        normalized_label,
                        _bounded_text(labeled_by, 128),
                        _bounded_text(rationale, 240),
                        _now(),
                    ),
                )
                return cursor.rowcount == 1
        except sqlite3.Error:
            LOGGER.exception("langsmith_feedback_manual_label_failed")
            return False

    def manual_labels(
        self, artifact_ids: list[str] | tuple[str, ...] | None = None
    ) -> dict[str, dict[str, str]]:
        """Return measurement labels only; never expose artifact payloads."""

        try:
            with self._connect() as db:
                if artifact_ids is None:
                    rows = db.execute(
                        "SELECT artifact_id, label, labeled_by, rationale, created_at "
                        "FROM langsmith_feedback_manual_labels ORDER BY artifact_id"
                    ).fetchall()
                else:
                    ids = tuple(str(value) for value in artifact_ids if str(value))
                    if not ids:
                        return {}
                    placeholders = ",".join("?" for _ in ids)
                    rows = db.execute(
                        "SELECT artifact_id, label, labeled_by, rationale, created_at "
                        "FROM langsmith_feedback_manual_labels "
                        f"WHERE artifact_id IN ({placeholders}) ORDER BY artifact_id",
                        ids,
                    ).fetchall()
                return {
                    str(row["artifact_id"]): {
                        "label": str(row["label"]),
                        "labeled_by": str(row["labeled_by"]),
                        "rationale": str(row["rationale"]),
                        "created_at": str(row["created_at"]),
                    }
                    for row in rows
                }
        except sqlite3.Error:
            LOGGER.exception("langsmith_feedback_manual_labels_read_failed")
            return {}

    def discord_delivery(self, artifact_id: str) -> dict[str, Any] | None:
        """Return the bounded Discord delivery state for one artifact."""

        with self._connect() as db:
            row = db.execute(
                """SELECT status, discord_message_id, error_code, updated_at
                FROM langsmith_feedback_discord_deliveries WHERE artifact_id=?""",
                (artifact_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def pending_discord_reviews(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return actionable artifacts that never received a QA card.

        Review delivery is deliberately separate from LangSmith polling.  A
        provider outage or a deployment that was previously in ``shadow``
        mode must not make already-persisted, metadata-only evidence
        permanently invisible to the named QA reviewer.  The delivery table
        remains the idempotency fence; this method only reads the backlog.
        """

        limit = max(1, min(int(limit), 100))
        actionable_codes = tuple(sorted(ACTIONABLE_FEEDBACK_CODES))
        placeholders = ",".join("?" for _ in actionable_codes)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT a.*
                FROM langsmith_feedback_artifacts a
                LEFT JOIN langsmith_feedback_discord_deliveries d
                  ON d.artifact_id=a.artifact_id
                WHERE d.artifact_id IS NULL
                  AND a.decision != 'OBSERVED_PASS'
                  {_EXCLUDE_SYNTHETIC_CANARY_SQL}
                  AND EXISTS (
                      SELECT 1
                      FROM json_each(a.finding_codes) AS finding
                      WHERE upper(finding.value) IN ({placeholders})
                  )
                ORDER BY {_PENDING_REVIEW_ORDER_SQL}
                LIMIT ?""",
                (*actionable_codes, limit),
            ).fetchall()
        return [
            self._artifact(row)
            for row in rows
            if is_actionable_feedback(json.loads(row["finding_codes"] or "[]"))
        ]

    def d5_finding_codes(self, limit: int = 400) -> tuple[str, ...]:
        """Return only structured D5 finding identities for CEO self-review.

        This is intentionally narrower than :meth:`pending`: the CEO self-
        improvement lane may consume the existence of a verified D5 finding,
        but never its request, answer, summaries, raw metadata, failure
        department set, or skill target.  The returned values are stable
        identifiers only and are filtered again by the D5 allow-list before
        they can reach a CEO prompt.
        """

        limit = max(1, min(int(limit), 1_000))
        try:
            with self._connect() as db:
                rows = db.execute(
                    """SELECT finding_codes, metadata
                    FROM langsmith_feedback_artifacts
                    ORDER BY created_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        except sqlite3.Error:
            LOGGER.exception("d5_finding_codes_read_failed")
            return ()

        codes: list[str] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata"] or "{}")
                findings = json.loads(row["finding_codes"] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                not isinstance(metadata, Mapping)
                or metadata.get("source") != "memo_harness_d5"
            ):
                continue
            if not isinstance(findings, list):
                continue
            for value in findings[:12]:
                code = str(value or "").strip().upper()
                if code.startswith("D5_") and code not in codes:
                    codes.append(code[:96])
        return tuple(codes[:64])

    def approve(
        self,
        artifact_id: str,
        decision: str,
        approved_by: str,
        reason: str,
        *,
        improvement_type: str = "NO_ACTION",
        target_skill_slug: str = "",
        task_activation: str = "",
        mandatory_controls: Iterable[str] = (),
    ) -> bool:
        if not qa_approver_is_allowed(approved_by):
            LOGGER.warning("langsmith_feedback_decision_rejected_invalid_approver")
            return False
        decision = _bounded_text(decision, 32).upper()
        if decision not in {"APPROVED", "REJECTED", "CLOSED_NO_ACTION"}:
            return False
        normalized_type = _bounded_text(improvement_type, 32).upper()
        if normalized_type not in IMPROVEMENT_TYPES:
            return False
        if decision == "APPROVED" and normalized_type == "NO_ACTION":
            return False
        if decision in {"REJECTED", "CLOSED_NO_ACTION"}:
            normalized_type = "NO_ACTION"
        normalized_slug = _bounded_text(target_skill_slug, 64).lower()
        if normalized_type == "SKILL_EVOLVE" and not normalized_slug:
            return False
        if normalized_type != "SKILL_EVOLVE":
            normalized_slug = ""
        normalized_activation = _bounded_text(task_activation, 32).lower()
        if normalized_activation not in {"", "owner-task"}:
            return False
        if normalized_activation and (
            decision != "APPROVED"
            or normalized_type not in {"SKILL_CREATE", "SKILL_EVOLVE"}
        ):
            return False
        if isinstance(mandatory_controls, (str, bytes)):
            return False
        normalized_controls = tuple(
            dict.fromkeys(
                _bounded_text(value, 240)
                for value in mandatory_controls
                if _bounded_text(value, 240)
            )
        )
        if len(normalized_controls) > 8:
            return False
        if normalized_controls and (
            decision != "APPROVED"
            or normalized_type not in {"SKILL_CREATE", "SKILL_EVOLVE"}
        ):
            return False
        try:
            with self._connect() as db:
                artifact = db.execute(
                    """SELECT artifact_id, decision, finding_codes, metadata
                    FROM langsmith_feedback_artifacts WHERE artifact_id=?""",
                    (artifact_id,),
                ).fetchone()
                if artifact is None:
                    return False
                if decision == "APPROVED":
                    finding_codes = set(json.loads(artifact["finding_codes"]))
                    try:
                        artifact_metadata = json.loads(artifact["metadata"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        artifact_metadata = {}
                    d5_candidate = (
                        isinstance(artifact_metadata, Mapping)
                        and artifact_metadata.get("source") == "memo_harness_d5"
                        and any(
                            str(code).upper().startswith("D5_")
                            for code in finding_codes
                        )
                    )
                    if artifact["decision"] == "OBSERVED_PASS" or not (
                        _ACTIONABLE_FEEDBACK_CODES.intersection(finding_codes)
                        or d5_candidate
                    ):
                        return False
                now = _now()
                cursor = db.execute(
                    """INSERT OR IGNORE INTO langsmith_feedback_decisions
                    (artifact_id, decision, approved_by, reason, improvement_type,
                     target_skill_slug, task_activation, mandatory_controls, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        artifact_id,
                        decision,
                        _bounded_text(approved_by, 128),
                        _bounded_text(reason, 240),
                        normalized_type,
                        normalized_slug or None,
                        normalized_activation,
                        json.dumps(normalized_controls, ensure_ascii=False),
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

    def requeue_discord_delivery(self, artifact_id: str) -> None:
        """Release a claimed card only when Discord explicitly rate-limited it."""

        try:
            with self._connect() as db:
                db.execute(
                    """DELETE FROM langsmith_feedback_discord_deliveries
                    WHERE artifact_id=? AND status='CLAIMED'""",
                    (artifact_id,),
                )
        except sqlite3.Error:
            LOGGER.exception("langsmith_feedback_discord_requeue_failed")

    def cleanup_qa_discord_messages(
        self,
        *,
        retention_days: int | None = None,
        max_messages: int | None = None,
        token: str | None = None,
        channel_id: str | None = None,
        dry_run: bool = False,
        now: datetime | None = None,
        opener: Any | None = None,
    ) -> FeedbackDiscordRetentionSummary:
        """Delete old, resolved QA feedback cards from the QA channel only.

        The SQLite ledger remains the audit index. Pending cards are never
        selected, and deletion is fenced by message IDs recorded when the QA
        bot posted the card. Proposal-review cards have a separate lifecycle
        and are intentionally not selected here.
        """

        enabled = os.getenv(
            "LANGSMITH_FEEDBACK_DISCORD_RETENTION_ENABLED", "true"
        ).strip().lower() not in {"0", "false", "no", "off"}
        if not enabled:
            return FeedbackDiscordRetentionSummary(enabled=False, available=False)

        configured_days = retention_days
        if configured_days is None:
            try:
                configured_days = int(
                    os.getenv("LANGSMITH_FEEDBACK_DISCORD_RETENTION_DAYS", "7")
                )
            except (TypeError, ValueError):
                configured_days = 7
        configured_days = max(1, min(int(configured_days), 3650))

        configured_max = max_messages
        if configured_max is None:
            try:
                configured_max = int(
                    os.getenv(
                        "LANGSMITH_FEEDBACK_DISCORD_RETENTION_MAX_MESSAGES", "100"
                    )
                )
            except (TypeError, ValueError):
                configured_max = 100
        configured_max = max(1, min(int(configured_max), 1000))

        effective_token = (
            token if token is not None else os.getenv("DISCORD_BOT_TOKEN_QA", "")
        ).strip()
        if channel_id is None:
            from orchestration.qa_discord_feedback import QA_FEEDBACK_CHANNEL_DEFAULT

            effective_channel = (
                os.getenv("QA_DISCORD_CHANNEL_ID", "").strip()
                or QA_FEEDBACK_CHANNEL_DEFAULT
            )
        else:
            effective_channel = channel_id.strip()
        if not effective_token or not effective_channel:
            return FeedbackDiscordRetentionSummary(enabled=False, available=False)

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff = current - timedelta(days=configured_days)

        try:
            with self._connect() as db:
                rows = db.execute(
                    """SELECT d.artifact_id, d.discord_message_id,
                              dec.created_at AS decision_at,
                              b.status AS benchmark_status,
                              b.updated_at AS benchmark_at
                         FROM langsmith_feedback_discord_deliveries d
                         JOIN langsmith_feedback_artifacts a
                           ON a.artifact_id=d.artifact_id
                    LEFT JOIN langsmith_feedback_decisions dec
                           ON dec.artifact_id=d.artifact_id
                    LEFT JOIN langsmith_feedback_benchmarks b
                           ON b.artifact_id=d.artifact_id
                        WHERE d.status='DELIVERED'
                          AND d.discord_message_id IS NOT NULL
                          AND d.discord_deleted_at IS NULL
                          AND (
                                dec.artifact_id IS NOT NULL
                                OR b.status IN ('PASSED', 'FAILED')
                              )
                     ORDER BY d.created_at ASC
                        LIMIT ?""",
                    (configured_max,),
                ).fetchall()
        except sqlite3.Error as exc:
            LOGGER.warning(
                "langsmith-feedback-discord-retention-query-failed error=%s",
                type(exc).__name__,
            )
            return FeedbackDiscordRetentionSummary(
                enabled=True,
                available=False,
                error_code=type(exc).__name__,
            )

        def _parse(value: Any) -> datetime | None:
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

        eligible: list[sqlite3.Row] = []
        skipped_malformed = 0
        for row in rows:
            decision_at = _parse(row["decision_at"])
            benchmark_at = (
                _parse(row["benchmark_at"])
                if row["benchmark_status"] in {"PASSED", "FAILED"}
                else None
            )
            terminal_times = [
                value for value in (decision_at, benchmark_at) if value is not None
            ]
            terminal_at = max(terminal_times, default=None)
            if terminal_at is None:
                skipped_malformed += 1
                continue
            # If a benchmark completed after approval, retain the card for
            # seven days after the latest terminal transition.
            if terminal_at >= cutoff:
                continue
            if not str(row["discord_message_id"] or "").strip():
                skipped_malformed += 1
                continue
            eligible.append(row)

        if dry_run:
            return FeedbackDiscordRetentionSummary(
                enabled=True,
                available=True,
                attempted=len(eligible),
                deleted=len(eligible),
                skipped_malformed=skipped_malformed,
            )

        from orchestration.discord_retention import DiscordRetentionWorker

        discord = DiscordRetentionWorker(
            token=effective_token,
            channel_ids=[effective_channel],
            opener=opener,
        )
        attempted = deleted = failed = 0
        for row in eligible:
            attempted += 1
            message_id = str(row["discord_message_id"])
            try:
                discord.delete_message(effective_channel, message_id)
                deleted_at = _now()
                with self._connect() as db:
                    db.execute(
                        """UPDATE langsmith_feedback_discord_deliveries
                              SET discord_deleted_at=?, updated_at=?
                            WHERE artifact_id=? AND discord_deleted_at IS NULL""",
                        (deleted_at, deleted_at, row["artifact_id"]),
                    )
                deleted += 1
            except Exception as exc:  # noqa: BLE001 - one card cannot block the pass
                failed += 1
                LOGGER.warning(
                    "langsmith-feedback-discord-delete-failed error=%s",
                    type(exc).__name__,
                )

        return FeedbackDiscordRetentionSummary(
            enabled=True,
            available=True,
            attempted=attempted,
            deleted=deleted,
            skipped_malformed=skipped_malformed,
            failed=failed,
            error_code="DISCORD_QA_DELETE_FAILED" if failed else None,
        )

    def approved_hints(
        self, department: str | None, limit: int, max_chars: int
    ) -> dict[str, Any] | None:
        limit = max(1, min(int(limit), 10))
        query_limit = min(40, limit * 4)
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
                    (canonical_department(department), query_limit),
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
                    (query_limit,),
                ).fetchall()
        if not rows:
            return None
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            # A D5 candidate is a review/regression work item, not a routing
            # hint. An approved candidate must never alter the next plan
            # before the central router regression gate has run.
            if (
                isinstance(metadata, Mapping)
                and metadata.get("source") == "memo_harness_d5"
            ):
                continue
            item = {
                "department": canonical_department(row["department"]),
                "decision": row["decision"],
                "finding_codes": json.loads(row["finding_codes"]),
                "summaries": json.loads(row["summaries"]),
                "source": "qa-approved-langsmith-feedback",
            }
            items.append(item)
            if len(items) >= limit:
                break
        if not items:
            return None
        payload = {"schema_version": FEEDBACK_SCHEMA, "items": items}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > max_chars:
            payload["items"] = items[:1]
        return payload

    def approved_benchmark_candidates(
        self, *, source: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return benchmark-passed candidates for an explicit downstream gate.

        This is intentionally separate from ``approved_hints``. Passing the
        redaction/admission benchmark never grants authority to change the CEO
        router; callers still need the central regression and promotion
        controls owned by that subsystem.
        """

        limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            rows = db.execute(
                """SELECT a.*, d.decision AS approval_decision,
                    d.approved_by, d.reason AS approval_reason,
                    d.improvement_type, d.target_skill_slug, d.task_activation,
                    d.mandatory_controls,
                    b.status AS benchmark_status, b.benchmark_id,
                    b.score AS benchmark_score, b.report_ref,
                    b.result_summary AS benchmark_result_summary
                FROM langsmith_feedback_artifacts a
                JOIN langsmith_feedback_decisions d ON d.artifact_id=a.artifact_id
                JOIN langsmith_feedback_benchmarks b ON b.artifact_id=a.artifact_id
                WHERE d.decision='APPROVED' AND b.status='PASSED'
                  AND d.improvement_type != 'NO_ACTION'
                  AND a.decision != 'OBSERVED_PASS'
                ORDER BY b.updated_at DESC LIMIT ?""",
                (min(400, limit * 4),),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = self._artifact(row)
            metadata = item.get("metadata")
            if source is not None and (
                not isinstance(metadata, Mapping) or metadata.get("source") != source
            ):
                continue
            items.append(item)
            if len(items) >= limit:
                break
        return items

    def benchmark_candidates(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return QA-approved, redacted candidates waiting for offline replay."""

        limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            rows = db.execute(
                """SELECT a.*, d.decision AS approval_decision,
                    d.approved_by, d.reason AS approval_reason,
                    d.improvement_type, d.target_skill_slug, d.task_activation,
                    d.mandatory_controls,
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

    def evolution_benchmark_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return first-approved skill findings awaiting admission benchmark."""

        limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute(
                """SELECT a.*, d.decision AS approval_decision,
                    d.approved_by, d.reason AS approval_reason,
                    d.improvement_type, d.target_skill_slug, d.task_activation,
                    d.mandatory_controls,
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
                    d.improvement_type, d.target_skill_slug, d.task_activation,
                    d.mandatory_controls,
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

        if status not in {"RUNNING", "PASSED", "FAILED"} or not _bounded_text(
            benchmark_id, 160
        ):
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
            "approval_decision": (
                row["approval_decision"] if "approval_decision" in keys else None
            ),
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
            "task_activation": (
                str(row["task_activation"] or "")
                if "task_activation" in keys
                else ""
            ),
            "mandatory_controls": (
                json.loads(row["mandatory_controls"] or "[]")
                if "mandatory_controls" in keys
                else []
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
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))
        ).isoformat()
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
    observation_category = result.metadata.get(
        "observation_category"
    ) or _observation_category(result.metadata)
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
        "observation_category": observation_category,
        "department_key": result.metadata.get("department_key")
        or canonical_department(result.department),
        "stage_status": result.metadata.get("stage_status")
        or ("PRESENT" if result.metadata.get("stage") else "MISSING"),
        "raw_payloads_sent": result.metadata.get("raw_payloads_sent"),
    }


def evaluation_run_id(source_run_id: str, project_name: str) -> UUID:
    """Stable Evals run identity for crash/retry idempotency."""

    return uuid5(NAMESPACE_URL, f"hgfinance:{project_name}:{source_run_id}")


def publish_evaluation(result: EvaluationResult, project_name: str) -> str | None:
    """Publish one closed metadata-only evaluation run; failures are swallowed."""

    if not evaluation_is_worthy(result):
        return None
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
    except Exception:
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
        count += max(1, _bounded_int(observation.metadata.get("metric_count")))
        status = (
            observation.metadata.get("status") or observation.status or ""
        ).lower()
        saw_error = saw_error or status in ERROR_STATUSES
        error_count += _bounded_int(observation.metadata.get("error_count"))
        latency = _bounded_int(observation.metadata.get("latency_ms"))
        if latency:
            latencies.append(latency)
    if count == 0:
        return None
    latencies.sort()
    p95 = (
        latencies[
            min(
                len(latencies) - 1,
                max(0, int(math.ceil(len(latencies) * 0.95)) - 1),
            )
        ]
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
        edit_qa_discord_message,
        format_qa_feedback_request,
        is_actionable_feedback,
        post_qa_discord_message,
        qa_feedback_channel_id,
        verify_discord_message_delivery,
    )

    artifact = ledger.get_artifact(artifact_id)
    if artifact is None:
        return False
    finding_codes = artifact["finding_codes"]
    if not is_actionable_feedback(finding_codes):
        return False
    channel_id = qa_feedback_channel_id()
    token = os.getenv("DISCORD_BOT_TOKEN_QA", "").strip()
    if not channel_id or not token:
        return False
    content = format_qa_feedback_request(
        artifact_id=artifact_id,
        department=str(artifact["department"]),
        decision=str(artifact["decision"]),
        finding_codes=finding_codes,
        summaries=artifact["summaries"],
        metadata=artifact["metadata"],
    )
    if not ledger.claim_discord_delivery(artifact_id):
        delivery = ledger.discord_delivery(artifact_id)
        if (
            delivery
            and delivery.get("status") == "DELIVERED"
            and delivery.get("discord_message_id")
        ):
            try:
                edit_qa_discord_message(
                    content,
                    token=token,
                    channel_id=channel_id,
                    message_id=str(delivery["discord_message_id"]),
                )
                return True
            except (HTTPError, URLError, TimeoutError, ValueError, OSError):
                LOGGER.warning(
                    "qa_discord_feedback_refresh_failed artifact_id=%s",
                    artifact_id,
                )
        return False
    try:
        message_id = post_qa_discord_message(
            content, token=token, channel_id=channel_id
        )
        if not verify_discord_message_delivery(
            content,
            token=token,
            channel_id=channel_id,
            message_id=message_id,
        ):
            raise RuntimeError("discord_message_readback_failed")
        ledger.finish_discord_delivery(
            artifact_id,
            delivered=True,
            discord_message_id=message_id,
        )
        return True
    except HTTPError as exc:
        if exc.code == 429:
            ledger.requeue_discord_delivery(artifact_id)
            LOGGER.warning(
                "qa_discord_feedback_rate_limited artifact_id=%s", artifact_id
            )
            return False
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


def _evaluation_result_from_artifact(
    artifact: Mapping[str, Any],
) -> EvaluationResult:
    """Rebuild the bounded card payload from the local artifact projection."""

    metadata = artifact.get("metadata")
    safe_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    findings = tuple(
        str(value).strip().upper()
        for value in (artifact.get("finding_codes") or ())
        if str(value).strip()
    )
    summaries = tuple(
        str(value).strip()
        for value in (artifact.get("summaries") or ())
        if str(value).strip()
    )
    return EvaluationResult(
        source_run_id=str(artifact.get("source_run_id") or ""),
        department=str(artifact.get("department") or "unknown"),
        workflow_role=str(safe_metadata.get("workflow_role") or ""),
        decision=str(artifact.get("decision") or "REVIEW_REQUIRED"),
        score=artifact.get("score")
        if isinstance(artifact.get("score"), (int, float))
        else None,
        finding_codes=findings,
        summaries=summaries,
        metadata=safe_metadata,
    )


class LangSmithFeedbackService:
    """Bounded, asynchronous evaluator owned by the portfolio worker process."""

    def __init__(
        self, config: FeedbackConfig | None = None, ledger: FeedbackLedger | None = None
    ) -> None:
        self.config = config or FeedbackConfig.from_env()
        try:
            self.ledger = ledger or FeedbackLedger(self.config.state_path)
        except Exception:
            LOGGER.exception("langsmith_feedback_ledger_unavailable")
            self.ledger = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_discord_retention_attempt = 0.0
        self._poll_failure_streak = 0
        self._last_poll_error_code: str | None = None

    @staticmethod
    def _poll_error_code(error: BaseException) -> str:
        message = _bounded_text(str(error), 96)
        if message.startswith("langsmith_"):
            return message
        return type(error).__name__

    def _record_poll_failure(self, error: BaseException) -> None:
        self._poll_failure_streak = min(self._poll_failure_streak + 1, 8)
        self._last_poll_error_code = self._poll_error_code(error)
        LOGGER.warning(
            "langsmith_feedback_poll_failed error_code=%s failure_streak=%d",
            self._last_poll_error_code,
            self._poll_failure_streak,
        )

    def _record_poll_success(self) -> None:
        self._poll_failure_streak = 0
        self._last_poll_error_code = None

    def _poll_wait_seconds(self, elapsed: float) -> float:
        """Back off only after a provider poll failure, then recover on success."""

        base = max(0.1, float(self.config.poll_seconds))
        if self._poll_failure_streak <= 0:
            return max(0.1, base - max(0.0, elapsed))
        backoff = min(300.0, base * (2 ** min(self._poll_failure_streak, 5)))
        return max(0.1, backoff - max(0.0, elapsed))

    def _publish_pending_qa_reviews(self) -> int:
        """Drain a bounded local review backlog during an explicit active window."""

        if self.config.mode != "active" or self.ledger is None:
            return 0

        delivered = 0
        for artifact in self.ledger.pending_discord_reviews(
            limit=self.config.batch_size
        ):
            findings = artifact.get("finding_codes") or ()
            if not is_actionable_feedback(findings):
                continue
            result = _evaluation_result_from_artifact(artifact)
            if _publish_qa_discord_request(
                self.ledger, str(artifact["artifact_id"]), result
            ):
                delivered += 1
        if delivered:
            LOGGER.info("langsmith_feedback_review_backfill delivered=%d", delivered)
        return delivered

    def start(self) -> None:
        if self.config.mode == "off" or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="langsmith-feedback", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def run_once(self) -> dict[str, int]:
        if self.config.mode == "off" or self.ledger is None:
            self._record_poll_success()
            return {"discovered": 0, "completed": 0, "failed": 0, "dropped": 0}
        discovered = completed = failed = dropped = 0
        client: Any | None = None
        try:
            # This runs before the provider query so a temporary LangSmith 5xx
            # cannot strand artifacts that were already evaluated locally.
            # ``claim_discord_delivery`` makes the one-shot transport attempt
            # idempotent, so switching from shadow to active does not duplicate
            # cards that were already delivered.
            self._publish_pending_qa_reviews()
            from orchestration.llm_observability import langsmith_enabled

            if not langsmith_enabled():
                self._record_poll_success()
                return {"discovered": 0, "completed": 0, "failed": 0, "dropped": 0}
            from langsmith import Client
            from orchestration.llm_observability import langsmith_multipart_ingest_info

            client = Client(
                info=langsmith_multipart_ingest_info(),
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
            root_limit = min(
                100, max(self.config.batch_size * 4, self.config.batch_size)
            )
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
            pending_slots = max(
                0, self.config.max_pending - self.ledger.pending_count()
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
                if not observation.metadata.get(
                    "stage"
                ) and not observation.metadata.get("department"):
                    continue
                discovered += 1
                if pending_slots <= 0:
                    dropped += 1
                    continue
                if self.ledger.enqueue(
                    observation.source_run_id,
                    self.config.workflow_project,
                    observation=observation,
                ):
                    pending_slots -= 1
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
                if pending_slots > 0 and self.ledger.enqueue(
                    metrics_observation.source_run_id,
                    self.config.metrics_project,
                    observation=metrics_observation,
                ):
                    pending_slots -= 1
                else:
                    dropped += 1
            for _ in range(self.config.batch_size):
                job = self.ledger.claim()
                if job is None:
                    break
                try:
                    raw_observation = json.loads(job["observation"] or "{}")
                    observation = TraceObservation(
                        source_run_id=_bounded_text(
                            raw_observation.get("source_run_id"), 128
                        ),
                        name=_bounded_text(raw_observation.get("name"), 160),
                        status=_bounded_text(raw_observation.get("status"), 32).lower(),
                        started_at=raw_observation.get("started_at"),
                        ended_at=raw_observation.get("ended_at"),
                        metadata=dict(raw_observation.get("metadata") or {}),
                    )
                    result = evaluate_observation(
                        observation,
                        latency_warn_ms=self.config.latency_warn_ms,
                        source_project=str(
                            job["project_name"] or self.config.workflow_project
                        ),
                    )
                    eval_run_id = publish_evaluation(result, self.config.evals_project)
                    if not eval_run_id:
                        if not evaluation_is_worthy(result):
                            self.ledger.skip(job["source_run_id"])
                            dropped += 1
                            continue
                        raise RuntimeError("eval_publish_unavailable")
                    artifact_id = self.ledger.complete(
                        job["source_run_id"], eval_run_id, result
                    )
                    if self.config.mode == "active":
                        _publish_qa_discord_request(self.ledger, artifact_id, result)
                    completed += 1
                except Exception as exc:  # noqa: BLE001 - retry outside business path
                    failed += 1
                    self.ledger.retry(
                        job["source_run_id"],
                        type(exc).__name__,
                        int(job["attempts"] or 0),
                    )
        except Exception as exc:  # noqa: BLE001 - LangSmith outage is fail-open
            self._record_poll_failure(exc)
        else:
            self._record_poll_success()
        finally:
            if client is not None:
                close_query_client(client)
        return {
            "discovered": discovered,
            "completed": completed,
            "failed": failed,
            "dropped": dropped,
        }

    def run_discord_retention_once(self) -> dict[str, int]:
        """Delete only resolved QA/review cards while retaining local history."""

        if self.ledger is None:
            return {"feedback_deleted": 0, "proposal_deleted": 0, "failed": 0}

        feedback_result = self.ledger.cleanup_qa_discord_messages()
        from orchestration.evolution_skills import (
            EvolutionSkillStore,
            cleanup_discord_review_cards,
        )

        evolution_root = Path(
            os.getenv("EVOLUTION_SKILLS_HOME", "/var/lib/evolution-skills").strip()
            or "/var/lib/evolution-skills"
        )
        proposal_result = cleanup_discord_review_cards(
            EvolutionSkillStore(evolution_root)
        )
        failed = feedback_result.failed + proposal_result.failed
        LOGGER.info(
            "qa-discord-retention feedback_deleted=%d proposal_deleted=%d "
            "feedback_failed=%d proposal_failed=%d",
            feedback_result.deleted,
            proposal_result.deleted,
            feedback_result.failed,
            proposal_result.failed,
        )
        return {
            "feedback_deleted": feedback_result.deleted,
            "proposal_deleted": proposal_result.deleted,
            "failed": failed,
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.run_once()
                if self.ledger is not None:
                    self.ledger.cleanup(self.config.retention_days)
            except Exception as exc:
                self._record_poll_failure(exc)
                LOGGER.exception("langsmith_feedback_loop_failed")
            now = time.monotonic()
            if (
                now - self._last_discord_retention_attempt
                >= self.config.discord_retention_interval_seconds
            ):
                # A Discord/API failure is isolated from feedback polling, and
                # the attempt gate prevents a bad credential from causing a
                # tight retry loop. The next daily pass retries it.
                self._last_discord_retention_attempt = now
                try:
                    self.run_discord_retention_once()
                except Exception:
                    LOGGER.exception("langsmith_feedback_discord_retention_failed")
            self._stop.wait(self._poll_wait_seconds(time.monotonic() - started))


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
    "FeedbackDiscordRetentionSummary",
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
