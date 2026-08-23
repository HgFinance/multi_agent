"""Persist completed Kanban QA tasks in the existing canonical audit schema."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestration.adapters.terminal_projection_utils import (
    ids_from,
    is_request_scoped_role,
    iso_timestamp,
    merged_run_metadata,
    safe_json,
    summary,
    task_id,
    terminal_success,
)
from orchestration.ceo_workflow_scope import selected_primary_profiles_from_task
from orchestration.qa_contract import split_planner_selection

logger = logging.getLogger(__name__)
PROJECTION_MARKER = "hgfinance.qa-audit-projection.v1"
_UUID_NAMESPACE = uuid.UUID("b8a25c03-2d9d-5f4e-b542-9dcb36db3e91")
_ALLOWED_DECISIONS = {"PASS", "WARN", "FAIL", "CONDITIONAL"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid(name: str) -> str:
    return str(uuid.uuid5(_UUID_NAMESPACE, name))


_KANBAN_QA_EVAL_SET_ID = _uuid("kanban-qa-eval-set:v1")


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _verdict(metadata: Mapping[str, Any], task: Mapping[str, Any]) -> str:
    value = metadata.get("verdict") or metadata.get("qa_verdict") or task.get("verdict")
    return str(value or "UNKNOWN").strip().upper()


def _canonical_decision(original: str) -> str:
    if original in _ALLOWED_DECISIONS:
        return original
    if original in {"CONDITIONAL PASS", "CONDITIONAL_PASS", "WARN", "ESCALATE"}:
        return "WARN"
    if original in {"FAIL", "REJECT", "BLOCK"}:
        return "FAIL"
    return "WARN"


@dataclass(frozen=True)
class QaAuditProjectionRecord:
    eval_run_id: str
    eval_set_id: str
    trace_id: str
    root_task_id: str
    qa_task_id: str
    evaluated_primary_task_ids: tuple[str, ...]
    original_verdict: str
    canonical_decision: str
    highest_severity: str
    findings: Any
    checks: Any
    sources_http: Any
    artifacts: Any
    tests_run: Any
    worker_session_id: str
    started_at: str | None
    completed_at: str | None
    evidence: Mapping[str, Any]

    @property
    def projection_key(self) -> str:
        return f"kanban-qa:{self.root_task_id}:{self.qa_task_id}"


class _DefaultAuditRepository:
    """Bridge to PostgresAuditRepository without importing it at module import time."""

    def __init__(self, dsn: str) -> None:
        audit_root = Path(__file__).resolve().parents[2] / "departments" / "06-ai-qa-audit"
        if str(audit_root) not in sys.path:
            sys.path.insert(0, str(audit_root))
        from audit.repository import (
            PostgresAuditRepository,  # type: ignore[import-not-found]
        )

        self.repo = PostgresAuditRepository.connect(dsn)

    def persist_kanban_qa(self, record: QaAuditProjectionRecord) -> dict[str, Any]:
        # The canonical eval tables require an EvalSet FK and a QUEUED -> RUNNING
        # -> COMPLETED lifecycle. The deterministic identities make replay safe.
        existing = self.repo.get_eval_run(record.eval_run_id)
        existing_status = str(
            getattr(existing, "status", None)
            if existing is not None
            else existing.get("status")
            if isinstance(existing, Mapping)
            else ""
        ).upper()
        if existing is not None and existing_status == "COMPLETED":
            insert_findings = getattr(self.repo, "insert_kanban_qa_findings", None)
            if callable(insert_findings):
                insert_findings(record)
            return {"duplicate": True, "eval_run_id": record.eval_run_id}
        eval_set = {
            "eval_set_id": record.eval_set_id,
            "role_code": "kanban-qa-terminal",
            "version": 1,
            "content_hash": _sha256({"role_code": "kanban-qa-terminal", "version": 1}),
        }
        self.repo.ensure_eval_set(eval_set)
        created = _now()
        run = {
            "eval_run_id": record.eval_run_id,
            "eval_set_id": record.eval_set_id,
            "candidate_id": f"kanban-qa:{record.qa_task_id}",
            "candidate_profile_version": "qa-department:kanban-terminal-projection.v1",
            "eval_set_version": 1,
            "eval_set_hash": eval_set["content_hash"],
            "champion_ref": None,
            "config": {
                "root_task_id": record.root_task_id,
                "qa_task_id": record.qa_task_id,
                "evaluated_primary_task_ids": list(record.evaluated_primary_task_ids),
                "original_verdict": record.original_verdict,
                "canonical_decision": record.canonical_decision,
                "highest_severity": record.highest_severity,
            },
            "status": "QUEUED",
            "trace_id": record.trace_id,
            "environment": "SHADOW",
            "mock_tool_manifest": {"source": "kanban", "worker_session_id": record.worker_session_id},
            "model_version": "hermes-qa",
            "adapter_version": "kanban-qa-terminal-projection.v1",
            "evidence_hash": _sha256(record.evidence),
            "started_at": created,
            "ended_at": None,
            "created_at": created,
        }
        if existing is None:
            self.repo.insert_eval_run(run)
        if existing_status != "RUNNING":
            self.repo.transition_eval_run(record.eval_run_id, "RUNNING")
        result = {
            "eval_result_id": _uuid(f"{record.projection_key}:result"),
            "eval_run_id": record.eval_run_id,
            "case_key": record.projection_key,
            "metric": "citation_precision",
            "score": 1 if record.canonical_decision == "PASS" else 0,
            "passed": record.canonical_decision == "PASS",
            "evidence": dict(record.evidence),
            "error_code": None if record.canonical_decision == "PASS" else record.original_verdict,
            "created_at": created,
        }
        self.repo.insert_eval_result(result)
        insert_findings = getattr(self.repo, "insert_kanban_qa_findings", None)
        if callable(insert_findings):
            insert_findings(record)
        self.repo.transition_eval_run(record.eval_run_id, "COMPLETED", ended_at=created)
        return {"duplicate": False, "eval_run_id": record.eval_run_id}


class QaAuditProjection:
    """Observe QA terminal events; persistence failure never changes workflow state."""

    def __init__(
        self,
        *,
        repository: Any | None = None,
        kanban_client: Any | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.env = env if env is not None else os.environ
        self.repository = repository
        self.kanban_client = kanban_client

    def _record(
        self,
        root_task_id: str,
        task: Mapping[str, Any],
        workflow_tasks: Sequence[Mapping[str, Any]],
    ) -> QaAuditProjectionRecord:
        metadata = merged_run_metadata(task)
        root_task = next(
            (item for item in workflow_tasks if task_id(item) == root_task_id),
            {},
        )
        selected_profiles, _ = split_planner_selection(
            selected_primary_profiles_from_task(root_task)
        )
        scoped_primary = tuple(
            task_id(item)
            for item in workflow_tasks
            if is_request_scoped_role(item, root_task_id, "primary")
            and (not selected_profiles or str(item.get("assignee") or "") in selected_profiles)
            and terminal_success(item)
        )
        logger.info(
            "qa-primary-selector root=%s selected=%d accepted=%d",
            root_task_id,
            len(selected_profiles),
            len(scoped_primary),
        )
        declared_primary = ids_from(metadata.get("evaluated_primary_task_ids")) or ids_from(
            metadata.get("primary_task_ids")
        )
        # The task graph is authoritative.  Never trust a worker-provided
        # primary_task_ids list to widen the current request scope.
        if declared_primary and any(item not in scoped_primary for item in declared_primary):
            logger.warning(
                "qa_audit_foreign_primary_ids_ignored",
                extra={"root_task_id": root_task_id},
            )
        primary = scoped_primary
        original = _verdict(metadata, task)
        qa_task_id = task_id(task)
        evidence = safe_json(
            {
                "root_task_id": root_task_id,
                "qa_task_id": qa_task_id,
                "evaluated_primary_task_ids": list(primary),
                "original_verdict": original,
                "highest_severity": metadata.get("highest_severity") or task.get("highest_severity"),
                "findings": metadata.get("findings") or task.get("findings") or [],
                "checks": metadata.get("checks") or task.get("checks") or [],
                "sources_http": metadata.get("sources_http") or task.get("sources_http") or [],
                "artifacts": metadata.get("artifacts") or task.get("artifacts") or [],
                "tests_run": metadata.get("tests_run") or task.get("tests_run") or [],
                "worker_session_id": metadata.get("worker_session_id") or task.get("worker_session_id") or "",
                "summary": summary(task, metadata),
            }
        )
        return QaAuditProjectionRecord(
            eval_run_id=_uuid(f"kanban-qa:{root_task_id}:{qa_task_id}"),
            eval_set_id=_KANBAN_QA_EVAL_SET_ID,
            trace_id=_uuid(f"kanban-qa-trace:{root_task_id}:{qa_task_id}"),
            root_task_id=root_task_id,
            qa_task_id=qa_task_id,
            evaluated_primary_task_ids=tuple(primary),
            original_verdict=original,
            canonical_decision=_canonical_decision(original),
            highest_severity=str(evidence.get("highest_severity") or "UNKNOWN"),
            findings=evidence.get("findings", []),
            checks=evidence.get("checks", []),
            sources_http=evidence.get("sources_http", []),
            artifacts=evidence.get("artifacts", []),
            tests_run=evidence.get("tests_run", []),
            worker_session_id=str(evidence.get("worker_session_id") or ""),
            started_at=iso_timestamp(task.get("started_at")),
            completed_at=iso_timestamp(task.get("completed_at") or task.get("finished_at")),
            evidence=evidence if isinstance(evidence, Mapping) else {},
        )

    def _comment(self, record: QaAuditProjectionRecord) -> None:
        if self.kanban_client is not None:
            self.kanban_client.comment_task(
                record.qa_task_id,
                f"{PROJECTION_MARKER} qa_task_id={record.qa_task_id} "
                f"eval_run_id={record.eval_run_id} status=persisted",
            )

    def project(
        self,
        *,
        root_task_id: str,
        task: Mapping[str, Any],
        workflow_tasks: Sequence[Mapping[str, Any]],
        event: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not is_request_scoped_role(task, root_task_id, "qa"):
            return {"status": "skipped"}
        if not terminal_success(task):
            return {"status": "skipped"}
        record = self._record(root_task_id, task, workflow_tasks)
        repository = self.repository
        if repository is None:
            dsn = str(self.env.get("RISK_QA_DATABASE_URL") or self.env.get("DATABASE_URL") or "")
            if not dsn:
                return {
                    "status": "failed",
                    "eval_run_id": record.eval_run_id,
                    "projection_key": record.projection_key,
                    "retryable": True,
                    "error": "RISK_QA_DATABASE_URL/DATABASE_URL missing",
                }
            repository = _DefaultAuditRepository(dsn)
        try:
            result = repository.persist_kanban_qa(record)
            if not isinstance(result, Mapping):
                result = {}
            comment_error = None
            if not result.get("duplicate"):
                try:
                    self._comment(record)
                except Exception as exc:  # noqa: BLE001 - audit row is already durable
                    comment_error = str(exc)
                    logger.warning(
                        "qa_audit_projection_marker_failed",
                        extra={"error": comment_error},
                    )
            return {
                "status": "duplicate" if result.get("duplicate") else "persisted",
                "eval_run_id": record.eval_run_id,
                "projection_key": record.projection_key,
                "original_verdict": record.original_verdict,
                "canonical_decision": record.canonical_decision,
                "duplicate": bool(result.get("duplicate")),
                "comment_error": comment_error,
            }
        except Exception as exc:  # noqa: BLE001 - async governance observer
            logger.warning("qa_audit_projection_failed", extra={"error": str(exc)})
            return {
                "status": "failed",
                "eval_run_id": record.eval_run_id,
                "projection_key": record.projection_key,
                "original_verdict": record.original_verdict,
                "canonical_decision": record.canonical_decision,
                "retryable": True,
                "error": str(exc),
            }


__all__ = ["PROJECTION_MARKER", "QaAuditProjection", "QaAuditProjectionRecord"]
