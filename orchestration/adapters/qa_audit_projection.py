"""Persist completed Kanban QA tasks in the existing canonical audit schema."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
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
    qa_projection_checks,
    qa_projection_findings,
    safe_json,
    summary,
    task_id,
    terminal_success,
)
from orchestration.ceo_workflow_scope import selected_primary_profiles_from_task
from orchestration.discord_delivery import _token_from_env
from orchestration.discord_idempotency import (
    DiscordIdempotencyStore,
    IdempotencyStoreUnavailable,
    canonical_discord_dedup_key,
)
from orchestration.qa_contract import split_planner_selection
from orchestration.qa_discord_feedback import (
    QA_FEEDBACK_CHANNEL_DEFAULT,
    edit_qa_discord_message,
    format_qa_terminal_report,
    post_qa_discord_message,
)

logger = logging.getLogger(__name__)
PROJECTION_VERSION = "v2"
EVAL_SET_VERSION = 2
PROJECTION_MARKER = f"hgfinance.qa-audit-projection.{PROJECTION_VERSION}"
LANGSMITH_MARKER = "hgfinance.qa-langsmith-terminal.v1"
DISCORD_MARKER = "hgfinance.qa-terminal-discord.v1"
_UUID_NAMESPACE = uuid.UUID("b8a25c03-2d9d-5f4e-b542-9dcb36db3e91")
_ALLOWED_DECISIONS = {"PASS", "WARN", "FAIL", "CONDITIONAL"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid(name: str) -> str:
    return str(uuid.uuid5(_UUID_NAMESPACE, name))


_KANBAN_QA_EVAL_SET_ID = _uuid(f"kanban-qa-eval-set:{PROJECTION_VERSION}")


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _verdict(metadata: Mapping[str, Any], task: Mapping[str, Any]) -> str:
    value = (
        metadata.get("verdict")
        or metadata.get("qa_verdict")
        or metadata.get("overall")
        or metadata.get("qa_status")
        or task.get("verdict")
        or task.get("overall")
        or metadata.get("audit_result")
    )
    if value:
        return str(value).strip().upper()

    # The QA Hermes terminal contract historically persisted a human-readable
    # verdict in the completion summary while leaving result/metadata empty.
    # Preserve that compatibility path, but only accept an explicit verdict;
    # never infer PASS from a missing field.
    terminal_text = " ".join(
        str(task.get(key) or "")
        for key in ("result", "latest_summary", "summary")
    )
    match = re.search(
        r"\bQA\s+overall\s*=\s*(PASS|WARN|FAIL|CONDITIONAL(?:\s+PASS)?)\b",
        terminal_text,
        flags=re.IGNORECASE,
    )
    return str(match.group(1) if match else "UNKNOWN").strip().upper()


def _audit_input(task: Mapping[str, Any]) -> Mapping[str, Any]:
    """Decode the bounded JSON audit envelope appended to a QA task body."""

    body = str(task.get("body") or "")
    marker = body.find('{"root_task_id"')
    if marker < 0:
        return {}
    try:
        value = json.loads(body[marker:])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _canonical_decision(original: str) -> str:
    if original in _ALLOWED_DECISIONS:
        return original
    if original in {"CONDITIONAL PASS", "CONDITIONAL_PASS", "WARN", "ESCALATE"}:
        return "WARN"
    if original in {"FAIL", "REJECT", "BLOCK"}:
        return "FAIL"
    return "WARN"


def _profile_model(env: Mapping[str, str]) -> tuple[str | None, str | None, str | None]:
    """Read the QA profile's declared model without reading credentials."""

    configured_home = Path(str(env.get("HERMES_HOME") or "/opt/data"))
    candidates = (
        configured_home / "profiles" / "qa-department" / "config.yaml",
        configured_home / "config.yaml",
    )
    try:
        lines = next(
            path.read_text(encoding="utf-8").splitlines()
            for path in candidates
            if path.is_file()
        )
    except (OSError, UnicodeError, StopIteration):
        return None, None, None

    in_model = False
    provider = model = source = None
    for line in lines:
        if line.strip() == "model:":
            in_model = True
            continue
        if in_model and line and not line[0].isspace():
            break
        if not in_model:
            continue
        for key in ("provider", "default"):
            prefix = f"{key}:"
            if line.strip().startswith(prefix):
                value = line.split(":", 1)[1].strip().strip("\"'")
                if key == "provider":
                    provider = value
                else:
                    model = value
    if provider or model:
        source = "qa-profile-config"
    return provider, model, source


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _count_observed(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value)
    return None


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
        return (
            f"kanban-qa:{PROJECTION_VERSION}:"
            f"{self.root_task_id}:{self.qa_task_id}"
        )


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
            "version": EVAL_SET_VERSION,
            "content_hash": _sha256(
                {
                    "role_code": "kanban-qa-terminal",
                    "version": EVAL_SET_VERSION,
                }
            ),
        }
        self.repo.ensure_eval_set(eval_set)
        created = _now()
        run = {
            "eval_run_id": record.eval_run_id,
            "eval_set_id": record.eval_set_id,
            "candidate_id": f"kanban-qa:{record.qa_task_id}",
            "candidate_profile_version": (
                f"qa-department:kanban-terminal-projection.{PROJECTION_VERSION}"
            ),
            "eval_set_version": EVAL_SET_VERSION,
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
            "adapter_version": f"kanban-qa-terminal-projection.{PROJECTION_VERSION}",
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
        self._published_langsmith: set[str] = set()

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
        audit_envelope = _audit_input(task)
        workflow_observations = audit_envelope.get("workflow_observations")
        evidence = safe_json(
            {
                "root_task_id": root_task_id,
                "qa_task_id": qa_task_id,
                "evaluated_primary_task_ids": list(primary),
                "original_verdict": original,
                "highest_severity": metadata.get("highest_severity") or task.get("highest_severity"),
                "findings": qa_projection_findings(task, metadata),
                "checks": qa_projection_checks(task, metadata),
                "sources_http": metadata.get("sources_http") or task.get("sources_http") or [],
                "artifacts": metadata.get("artifacts") or task.get("artifacts") or [],
                "tests_run": metadata.get("tests_run") or task.get("tests_run") or [],
                "worker_session_id": metadata.get("worker_session_id") or task.get("worker_session_id") or "",
                # Keep the bounded, worker-declared facts available to the
                # manager-facing projections.  These are not sent as raw
                # LangSmith payloads; the projections humanize and cap them.
                "verified_facts": metadata.get("verified_facts") or task.get("verified_facts") or [],
                "unknowns": metadata.get("unknowns") or task.get("unknowns") or [],
                "limitations": metadata.get("limitations") or task.get("limitations") or [],
                "safety": metadata.get("safety") or task.get("safety") or {},
                "summary": summary(task, metadata),
                "numerical_posture": (
                    metadata.get("numerical_posture")
                    or metadata.get("numeric_posture")
                    or metadata.get("decision")
                ),
                "workflow_observations": (
                    workflow_observations
                    if isinstance(workflow_observations, Mapping)
                    else {}
                ),
            }
        )
        started_at = iso_timestamp(task.get("started_at"))
        completed_at = iso_timestamp(task.get("completed_at") or task.get("finished_at"))
        started_dt = _timestamp(started_at)
        completed_dt = _timestamp(completed_at)
        if started_dt is not None and completed_dt is not None:
            evidence["latency_ms"] = max(
                0, int((completed_dt - started_dt).total_seconds() * 1000)
            )
        return QaAuditProjectionRecord(
            eval_run_id=_uuid(
                f"kanban-qa:{PROJECTION_VERSION}:{root_task_id}:{qa_task_id}"
            ),
            eval_set_id=_KANBAN_QA_EVAL_SET_ID,
            trace_id=_uuid(
                f"kanban-qa-trace:{PROJECTION_VERSION}:{root_task_id}:{qa_task_id}"
            ),
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
            started_at=started_at,
            completed_at=completed_at,
            evidence=evidence if isinstance(evidence, Mapping) else {},
        )

    def _comment(self, record: QaAuditProjectionRecord) -> None:
        if self.kanban_client is not None:
            self.kanban_client.comment_task(
                record.qa_task_id,
                f"{PROJECTION_MARKER} qa_task_id={record.qa_task_id} "
                f"eval_run_id={record.eval_run_id} status=persisted",
            )

    @staticmethod
    def _has_marker(task: Mapping[str, Any], marker: str) -> bool:
        comments = task.get("comments")
        if isinstance(comments, Sequence) and not isinstance(
            comments, (str, bytes, bytearray)
        ):
            return any(marker in str(item) for item in comments)
        return marker in str(comments or "")

    def _publish_langsmith(
        self,
        record: QaAuditProjectionRecord,
        task: Mapping[str, Any],
    ) -> str:
        """Publish one correlated QA terminal envelope, never report text."""

        if record.projection_key in self._published_langsmith or self._has_marker(
            task, LANGSMITH_MARKER
        ):
            return "deduped"
        try:
            from orchestration.llm_observability import (
                langsmith_enabled,
                langsmith_project,
                publish_metric,
            )

            if not langsmith_enabled():
                return "disabled"
            metadata = merged_run_metadata(task)
            provider, model, model_source = _profile_model(self.env)
            log_metrics: dict[str, Any] = {}
            try:
                from scripts.hermes_worker_observability import worker_log_metrics

                log_metrics = worker_log_metrics(
                    task_id=record.qa_task_id,
                    env=self.env,
                )
            except Exception:  # noqa: BLE001 - log enrichment is fail-open
                logger.debug("qa_worker_log_metrics_unavailable", exc_info=True)
            observed_llm_calls = _count_observed(
                metadata.get("llm_calls")
                or metadata.get("llm_call_count")
                or log_metrics.get("llm_calls")
            )
            observed_tool_calls = _count_observed(
                metadata.get("tool_calls")
                or metadata.get("tool_call_count")
                or log_metrics.get("tool_calls")
            )
            observed_tool_errors = _count_observed(
                metadata.get("tool_error_count")
                or metadata.get("tool_errors")
                or log_metrics.get("tool_error_count")
            )
            runs = task.get("runs")
            run_count = (
                len(runs)
                if isinstance(runs, Sequence)
                and not isinstance(runs, (str, bytes, bytearray))
                else None
            )
            try:
                attempts = int(metadata.get("attempts") or run_count or 1)
            except (TypeError, ValueError):
                attempts = 1
            attempts = max(1, attempts)
            started_at = _timestamp(record.started_at)
            ended_at = _timestamp(record.completed_at)
            latency_ms = (
                max(0, int((ended_at - started_at).total_seconds() * 1000))
                if started_at is not None and ended_at is not None
                else None
            )
            metric: dict[str, Any] = {
                "schema_version": "llm.qa-terminal.v1",
                "worker_id": "qa-department",
                "role": "qa",
                "stage": "qa-terminal",
                "model_name": model,
                "provider": provider,
                "model_source": model_source,
                "status": "COMPLETED",
                "terminal_status": "COMPLETED",
                "terminal_reason": "qa_audit_persisted",
                "terminal_task_id": record.qa_task_id,
                "terminal_department": "qa",
                "request_id": metadata.get("request_id") or record.root_task_id,
                "root_id": record.root_task_id,
                "task_id": record.qa_task_id,
                "workflow_role": "qa",
                "workflow_mode": metadata.get("workflow_mode") or "analysis",
                "trace_kind": "qa_worker_terminal",
                "latency_scope": "worker_execution",
                "latency_ms": latency_ms,
                "attempts": attempts,
                "retries": max(attempts - 1, 0),
                "llm_calls": observed_llm_calls,
                "tool_calls": observed_tool_calls,
                "tool_error_count": observed_tool_errors,
                "tool_duration_total_ms": _count_observed(
                    metadata.get("tool_duration_total_ms")
                    or log_metrics.get("tool_duration_total_ms")
                ),
                "tool_latency_available": bool(
                    metadata.get("tool_latency_available")
                    or log_metrics.get("tool_latency_available")
                ),
                "tool_timing_source": (
                    metadata.get("tool_timing_source")
                    or log_metrics.get("tool_timing_source")
                    or "unavailable"
                ),
                "error_count": 0,
                "error_class": None,
                "output_verdict": record.original_verdict,
                "finding_count": len(record.findings) if isinstance(record.findings, Sequence) else None,
                "telemetry_completeness": (
                    "runtime-and-terminal"
                    if observed_llm_calls is not None or observed_tool_calls is not None
                    else "terminal-handoff"
                ),
                "observability_source": "kanban_terminal_projection",
                "raw_payloads_sent": False,
                "workflow_observations": record.evidence.get(
                    "workflow_observations", {}
                ),
            }
            if (
                latency_ms is not None
                and metric["tool_duration_total_ms"] is not None
                and metric["tool_latency_available"]
            ):
                metric["model_latency_ms"] = max(
                    0,
                    latency_ms - int(metric["tool_duration_total_ms"]),
                )
            if metadata.get("input_hash"):
                metric["input_hash"] = metadata["input_hash"]
            published = publish_metric(
                metric,
                trace_id=record.trace_id,
                project_name=langsmith_project("workflow"),
                name="qa.hermes.terminal",
                start_time=started_at,
                end_time=ended_at,
            )
            if not published:
                return "failed"
            self._published_langsmith.add(record.projection_key)
            if self.kanban_client is not None:
                self.kanban_client.comment_task(
                    record.qa_task_id,
                    f"{LANGSMITH_MARKER} eval_run_id={record.eval_run_id} status=published",
                )
            return "published"
        except Exception as exc:  # noqa: BLE001 - observer is fail-open
            logger.warning(
                "qa_langsmith_terminal_projection_failed",
                extra={"error": type(exc).__name__},
            )
            return "failed"

    def _publish_discord(
        self,
        record: QaAuditProjectionRecord,
    ) -> str:
        """Post one manager-facing QA card through the profile's own identity."""

        channel_id = str(
            self.env.get("QA_DISCORD_CHANNEL_ID") or QA_FEEDBACK_CHANNEL_DEFAULT
        ).strip()
        token = _token_from_env(self.env, "qa-department")
        if not channel_id or not token:
            return "not_configured"
        home = Path(str(self.env.get("HERMES_HOME") or "/opt/data"))
        profile_home = home / "profiles" / "qa-department"
        delivery_home = profile_home if profile_home.is_dir() else home
        store = DiscordIdempotencyStore(delivery_home)
        profile = "qa-department"
        response_key = f"qa-terminal:{record.eval_run_id}"
        dedup_key = canonical_discord_dedup_key(
            "qa", channel_id, record.eval_run_id
        )
        existing_message_id: str | None = None
        try:
            content = format_qa_terminal_report(record)
            existing_message_id = store.outbound_message_id(response_key, profile)
            if existing_message_id:
                edit_qa_discord_message(
                    content,
                    token=token,
                    channel_id=channel_id,
                    message_id=existing_message_id,
                )
                return "deduped"
            claim = store.claim_outbound(
                response_key=response_key,
                dedup_key=dedup_key,
                profile=profile,
            )
            if not claim.admitted:
                return "deduped"
            message_id = post_qa_discord_message(
                content,
                token=token,
                channel_id=channel_id,
            )
            store.mark_outbound(
                response_key,
                "COMPLETED",
                profile,
                response_message_id=message_id,
            )
            if self.kanban_client is not None:
                self.kanban_client.comment_task(
                    record.qa_task_id,
                    f"{DISCORD_MARKER} eval_run_id={record.eval_run_id} "
                    f"channel_id={channel_id} status=sent",
                )
            return "sent"
        except IdempotencyStoreUnavailable:
            return "failed"
        except Exception as exc:  # noqa: BLE001 - observer is fail-open
            if not existing_message_id:
                try:
                    store.mark_outbound(response_key, "FAILED", profile)
                except Exception:  # noqa: BLE001 - cleanup must not mask the failure
                    logger.debug("qa_discord_outbound_failure_mark_failed", exc_info=True)
            logger.warning(
                "qa_discord_terminal_projection_failed",
                extra={"error": type(exc).__name__},
            )
            return "failed"

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
            langsmith_status = self._publish_langsmith(record, task)
            discord_status = self._publish_discord(record)
            return {
                "status": "duplicate" if result.get("duplicate") else "persisted",
                "eval_run_id": record.eval_run_id,
                "projection_key": record.projection_key,
                "original_verdict": record.original_verdict,
                "canonical_decision": record.canonical_decision,
                "duplicate": bool(result.get("duplicate")),
                "comment_error": comment_error,
                "langsmith_status": langsmith_status,
                "discord_status": discord_status,
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


__all__ = [
    "EVAL_SET_VERSION",
    "PROJECTION_MARKER",
    "PROJECTION_VERSION",
    "QaAuditProjection",
    "QaAuditProjectionRecord",
]
