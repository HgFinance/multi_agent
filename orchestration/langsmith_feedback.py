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

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

LOGGER = logging.getLogger(__name__)

WORKFLOW_PROJECT_DEFAULT = "First"
EVALS_PROJECT_DEFAULT = "HgFinance-Evals"
FEEDBACK_SCHEMA = "hgfinance.observability.feedback.v1"
FEEDBACK_MODES = frozenset({"off", "shadow", "active"})
TERMINAL_STATUSES = frozenset({"success", "completed", "complete", "error", "failed", "blocked", "degraded"})
ERROR_STATUSES = frozenset({"error", "failed", "blocked", "degraded", "gave_up", "timed_out"})
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
        "error_class",
        "error_count",
        "latency_ms",
        "eval_score",
        "provider",
        "model_name",
        "raw_payloads_sent",
        "source",
        "metric_count",
        "window_start",
        "window_end",
        "p95_latency_ms",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: Any, limit: int = 160) -> str:
    return " ".join(str(value or "").split())[:limit]


def canonical_department(value: Any) -> str:
    """Normalize a UI department code and a trace stage into one key."""

    candidate = _bounded_text(value, 64).lower()
    return _DEPARTMENT_CANONICAL.get(candidate, candidate)


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
    if score != score:  # NaN
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

    @classmethod
    def from_env(cls) -> "FeedbackConfig":
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
            # LangSmith's /runs/query endpoint accepts at most 100 rows per
            # request. Keep the bound below that server-side limit so a bad
            # tuning value cannot turn the background, fail-open poller into
            # a repeated 400 loop.
            metrics_max_runs=_int("LANGSMITH_FEEDBACK_METRICS_MAX_RUNS", 100, 1, 100),
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
    metadata: dict[str, Any] = {}
    for key in _SAFE_METADATA_KEYS:
        if key not in raw_metadata:
            continue
        value = raw_metadata[key]
        if key in {"request_id", "root_id", "task_id", "workflow_mode", "workflow_role", "department", "stage", "worker_id", "role", "status", "error_class", "provider", "model_name", "source"}:
            metadata[key] = _bounded_text(value, 160)
        elif key in {"error_count", "latency_ms", "metric_count", "p95_latency_ms"}:
            metadata[key] = _bounded_int(value)
        elif key in {"window_start", "window_end"}:
            metadata[key] = _bounded_text(value, 64)
        elif key == "eval_score":
            score = _bounded_score(value)
            if score is not None:
                metadata[key] = score
        elif key == "raw_payloads_sent":
            metadata[key] = bool(value)
    metadata.setdefault("raw_payloads_sent", False)
    return TraceObservation(
        source_run_id=_bounded_text(getattr(run, "id", ""), 128),
        name=_bounded_text(getattr(run, "name", ""), 160),
        status=_bounded_text(getattr(run, "status", ""), 32).lower(),
        started_at=getattr(run, "start_time", None).isoformat() if getattr(run, "start_time", None) else None,
        ended_at=getattr(run, "end_time", None).isoformat() if getattr(run, "end_time", None) else None,
        metadata=metadata,
    )


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
    latency_ms = _bounded_int(metadata.get("latency_ms"))
    if latency_ms > latency_warn_ms:
        findings.append("LATENCY_ABOVE_THRESHOLD")
        summaries.append("worker latency exceeded the configured observation threshold")
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
        "request_id": metadata.get("request_id"),
        "root_id": metadata.get("root_id"),
        "task_id": metadata.get("task_id"),
        "workflow_mode": metadata.get("workflow_mode"),
        "workflow_role": observation.workflow_role,
        "department": observation.department,
        "status": status,
        "latency_ms": latency_ms or None,
        "p95_latency_ms": _bounded_int(metadata.get("p95_latency_ms")) or None,
        "metric_count": _bounded_int(metadata.get("metric_count")) or None,
        "window_start": metadata.get("window_start"),
        "window_end": metadata.get("window_end"),
        "error_count": error_count,
        "eval_score": score,
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
        connection = sqlite3.connect(self.path, timeout=0.2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=200")
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
                CREATE TABLE IF NOT EXISTS langsmith_feedback_reviews (
                    review_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    target_department TEXT NOT NULL,
                    reviewer_department TEXT NOT NULL,
                    reviewer_user_id TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_reviews_artifact
                    ON langsmith_feedback_reviews(artifact_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_feedback_reviews_department
                    ON langsmith_feedback_reviews(target_department, created_at);
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
        with self._connect() as db:
            existing = db.execute(
                "SELECT artifact_id FROM langsmith_feedback_artifacts WHERE source_run_id=?",
                (source_run_id,),
            ).fetchone()
            if existing is not None:
                db.execute(
                    """UPDATE langsmith_feedback_jobs
                    SET status='COMPLETED', eval_run_id=?, last_error=NULL,
                        lease_until=NULL, updated_at=?
                    WHERE source_run_id=?""",
                    (eval_run_id, now, source_run_id),
                )
                return str(existing["artifact_id"])
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
                """SELECT a.*, d.decision AS approval_decision, d.approved_by, d.reason AS approval_reason
                FROM langsmith_feedback_artifacts a
                LEFT JOIN langsmith_feedback_decisions d ON d.artifact_id=a.artifact_id
                WHERE d.artifact_id IS NULL ORDER BY a.created_at LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._artifact(row) for row in rows]

    def department_feedback(self, department: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return redacted findings and append-only reviews for one department.

        The department key is matched against the artifact's server-derived
        metadata. Callers cannot attach a department comment to another
        department's trace by changing the UI label.
        """

        department_key = canonical_department(department)
        limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            rows = db.execute(
                """SELECT a.*, d.decision AS approval_decision, d.approved_by,
                    d.reason AS approval_reason,
                    COUNT(r.review_id) AS review_count
                FROM langsmith_feedback_artifacts a
                LEFT JOIN langsmith_feedback_decisions d ON d.artifact_id=a.artifact_id
                LEFT JOIN langsmith_feedback_reviews r ON r.artifact_id=a.artifact_id
                WHERE a.department_key=?
                GROUP BY a.artifact_id
                ORDER BY a.created_at DESC LIMIT ?""",
                (department_key, limit),
            ).fetchall()
            reviews_by_artifact: dict[str, list[dict[str, Any]]] = {}
            if rows:
                placeholders = ",".join("?" for _ in rows)
                review_rows = db.execute(
                    f"""SELECT review_id, artifact_id, target_department,
                        reviewer_department, reviewer_user_id, comment, created_at
                    FROM langsmith_feedback_reviews
                    WHERE artifact_id IN ({placeholders})
                    ORDER BY created_at ASC""",
                    tuple(row["artifact_id"] for row in rows),
                ).fetchall()
                for review in review_rows:
                    reviews_by_artifact.setdefault(str(review["artifact_id"]), []).append(
                        {
                            "review_id": review["review_id"],
                            "target_department": review["target_department"],
                            "reviewer_department": review["reviewer_department"],
                            "reviewer_user_id": review["reviewer_user_id"],
                            "comment": review["comment"],
                            "created_at": review["created_at"],
                        }
                    )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._artifact(row)
            item["review_count"] = int(row["review_count"] or 0)
            item["reviews"] = reviews_by_artifact.get(str(row["artifact_id"]), [])
            result.append(item)
        return result

    def add_department_review(
        self,
        artifact_id: str,
        *,
        target_department: str,
        reviewer_department: str,
        reviewer_user_id: str,
        comment: str,
    ) -> dict[str, Any] | None:
        """Append one self-department review without changing QA authority."""

        target_key = canonical_department(target_department)
        reviewer_key = canonical_department(reviewer_department)
        bounded_comment = _bounded_text(comment, 1_200)
        bounded_user = _bounded_text(reviewer_user_id, 128)
        if not target_key or target_key != reviewer_key or not bounded_user or not bounded_comment:
            return None
        try:
            with self._connect() as db:
                artifact = db.execute(
                    "SELECT artifact_id, department, department_key FROM langsmith_feedback_artifacts WHERE artifact_id=?",
                    (artifact_id,),
                ).fetchone()
                if artifact is None or artifact["department_key"] != target_key:
                    return None
                review_id = f"review-{uuid4().hex}"
                now = _now()
                db.execute(
                    """INSERT INTO langsmith_feedback_reviews
                    (review_id, artifact_id, target_department, reviewer_department,
                     reviewer_user_id, comment, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        review_id,
                        artifact_id,
                        target_key,
                        reviewer_key,
                        bounded_user,
                        bounded_comment,
                        now,
                    ),
                )
                return {
                    "review_id": review_id,
                    "artifact_id": artifact_id,
                    "target_department": target_key,
                    "reviewer_department": reviewer_key,
                    "reviewer_user_id": bounded_user,
                    "comment": bounded_comment,
                    "created_at": now,
                }
        except sqlite3.Error:
            LOGGER.exception("langsmith_department_review_failed")
            return None

    def approve(self, artifact_id: str, decision: str, approved_by: str, reason: str) -> bool:
        if decision not in {"APPROVED", "REJECTED"}:
            return False
        try:
            with self._connect() as db:
                artifact = db.execute(
                    "SELECT artifact_id FROM langsmith_feedback_artifacts WHERE artifact_id=?",
                    (artifact_id,),
                ).fetchone()
                if artifact is None:
                    return False
                now = _now()
                cursor = db.execute(
                    "INSERT OR IGNORE INTO langsmith_feedback_decisions (artifact_id, decision, approved_by, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                    (artifact_id, decision, _bounded_text(approved_by, 128), _bounded_text(reason, 240), now),
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

    def approved_hints(self, department: str | None, limit: int, max_chars: int) -> dict[str, Any] | None:
        limit = max(1, min(int(limit), 10))
        with self._connect() as db:
            if department:
                rows = db.execute(
                    """SELECT a.*, d.decision AS approval_decision, b.status AS benchmark_status
                    FROM langsmith_feedback_artifacts a
                    JOIN langsmith_feedback_decisions d ON d.artifact_id=a.artifact_id
                    JOIN langsmith_feedback_benchmarks b ON b.artifact_id=a.artifact_id
                    WHERE d.decision='APPROVED' AND b.status='PASSED' AND a.department=?
                    ORDER BY a.created_at DESC LIMIT ?""",
                    (_bounded_text(department, 64).lower(), limit),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT a.*, d.decision AS approval_decision, b.status AS benchmark_status
                    FROM langsmith_feedback_artifacts a
                    JOIN langsmith_feedback_decisions d ON d.artifact_id=a.artifact_id
                    JOIN langsmith_feedback_benchmarks b ON b.artifact_id=a.artifact_id
                    WHERE d.decision='APPROVED' AND b.status='PASSED'
                    ORDER BY a.created_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        if not rows:
            return None
        items: list[dict[str, Any]] = []
        for row in rows:
            item = {
                "department": row["department"],
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
                    b.status AS benchmark_status, b.benchmark_id,
                    b.score AS benchmark_score, b.report_ref,
                    b.result_summary AS benchmark_result_summary
                FROM langsmith_feedback_artifacts a
                JOIN langsmith_feedback_decisions d ON d.artifact_id=a.artifact_id
                LEFT JOIN langsmith_feedback_benchmarks b ON b.artifact_id=a.artifact_id
                WHERE d.decision='APPROVED'
                  AND (b.status IS NULL OR b.status IN ('PENDING', 'FAILED'))
                ORDER BY a.created_at LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._artifact(row) for row in rows]

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
            "approved_by": row["approved_by"] if "approved_by" in row.keys() else None,
            "approval_reason": row["approval_reason"] if "approval_reason" in row.keys() else None,
            "benchmark_status": row["benchmark_status"] if "benchmark_status" in row.keys() else None,
            "benchmark_id": row["benchmark_id"] if "benchmark_id" in row.keys() else None,
            "benchmark_score": row["benchmark_score"] if "benchmark_score" in row.keys() else None,
            "benchmark_report_ref": row["report_ref"] if "report_ref" in row.keys() else None,
            "benchmark_result_summary": (
                row["benchmark_result_summary"]
                if "benchmark_result_summary" in row.keys()
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
                """DELETE FROM langsmith_feedback_reviews
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
    return {
        **result.metadata,
        "schema_version": FEEDBACK_SCHEMA,
        "evaluation_type": "metadata_contract",
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
        from orchestration.llm_observability import _safe_langsmith_client, langsmith_enabled

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

            client = Client(hide_inputs=True, hide_outputs=True, hide_metadata=False)
            now = datetime.now(timezone.utc)
            since = now - timedelta(seconds=self.config.lookback_seconds)
            # A root may start before the observation window and finish inside
            # it (the common case for a multi-worker advisory).  Filtering by
            # start_time alone silently loses that root.  Use the server-side
            # end_time filter and keep a bounded page; the SQLite source-run
            # key makes repeated pages idempotent and the next poll catches
            # older rows when the page is full.
            root_filter = f'gt(end_time, "{since.isoformat()}")'
            root_limit = min(100, max(self.config.batch_size * 4, self.config.batch_size))
            try:
                root_runs = client.list_runs(
                    project_name=self.config.workflow_project,
                    is_root=True,
                    filter=root_filter,
                    end_time=now,
                    limit=root_limit,
                    select=["id", "name", "status", "start_time", "end_time", "extra"],
                )
                root_runs = list(root_runs)
            except Exception as exc:  # noqa: BLE001 - preserve SDK compatibility
                LOGGER.warning(
                    "langsmith_feedback_end_time_query_fallback error=%s",
                    type(exc).__name__,
                )
                root_runs = list(
                    client.list_runs(
                        project_name=self.config.workflow_project,
                        is_root=True,
                        start_time=since,
                        limit=root_limit,
                        select=["id", "name", "status", "start_time", "end_time", "extra"],
                    )
                )
            for run in root_runs:
                observation = observation_from_run(run)
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
                client.list_runs(
                    project_name=self.config.metrics_project,
                    is_root=True,
                    start_time=metrics_start,
                    end_time=metrics_end,
                    limit=self.config.metrics_max_runs,
                    select=["id", "name", "status", "start_time", "end_time", "extra"],
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
                    self.ledger.complete(job["source_run_id"], eval_run_id, result)
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
    "FeedbackConfig",
    "FeedbackLedger",
    "EvaluationResult",
    "LangSmithFeedbackService",
    "TraceObservation",
    "approved_feedback_hint",
    "evaluate_observation",
    "evaluation_run_id",
    "feedback_mode",
    "observation_from_run",
    "publish_evaluation",
]
