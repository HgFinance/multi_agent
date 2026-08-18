"""Optional operational trace bridge for QA LangGraph workers.

The bridge is disabled unless ``QA_TRACE_PERSIST=true`` and a database DSN is
available.  It resolves only ACTIVE LangGraph worker profiles, writes an
``audit.agent_runs`` row, records the worker tool calls, and closes the run.
Missing profiles or persistence failures never become a PASS; callers receive
the error so the QA pipeline can escalate safely.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from audit.repository import PostgresAuditRepository
from audit.trace_recorder import TraceRecorder


class WorkerTraceBridgeError(RuntimeError):
    """Raised when an operational worker trace cannot be recorded."""


class WorkerTraceBridge:
    def __init__(self, repository: PostgresAuditRepository) -> None:
        self._repository = repository

    def _resolve_profile(self, worker_id: str) -> tuple[UUID, UUID, UUID]:
        connection = self._repository._get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select p.agent_id, v.profile_version_id, v.model_id
                    from workforce.agent_profiles p
                    join workforce.role_templates r on r.role_id = p.role_id
                    join workforce.agent_profile_versions v
                      on v.agent_id = p.agent_id and v.version = p.current_version
                    where p.employee_code = %s
                      and p.runtime = 'LANGGRAPH'
                      and p.employment_status = 'ACTIVE'
                      and v.status = 'ACTIVE'
                    """,
                    (worker_id,),
                )
                row = cursor.fetchone()
        finally:
            connection.rollback()
            self._repository._pool.putconn(connection)
        if not row:
            raise WorkerTraceBridgeError(
                f"active worker profile not found: {worker_id}"
            )
        return UUID(str(row[0])), UUID(str(row[1])), UUID(str(row[2]))

    def record(
        self,
        *,
        worker_id: str,
        trace_id: str,
        input_hash: str,
        tools: Sequence[str],
        payload: dict[str, Any],
        output: dict[str, Any],
        started_at: datetime | None = None,
    ) -> None:
        try:
            trace_uuid = UUID(str(trace_id))
        except (AttributeError, ValueError) as exc:
            raise WorkerTraceBridgeError("worker trace_id must be UUID") from exc
        agent_id, profile_version_id, model_id = self._resolve_profile(worker_id)
        recorder = TraceRecorder(self._repository)
        run = recorder.start_run(
            trace_uuid,
            agent_id,
            profile_version_id,
            input_hash,
            model_id=model_id,
            started_at=started_at or datetime.now(timezone.utc),
        )
        output_hash = hashlib.sha256(_compact(output).encode("utf-8")).hexdigest()
        for tool_name in tools:
            call = recorder.record_tool_call(
                run.agent_run_id,
                tool_name,
                {"department": "qa", "worker_id": worker_id},
                hashlib.sha256(_compact(payload).encode("utf-8")).hexdigest(),
                policy_version="qa-worker-tools-v1",
            )
            recorder.allow_tool_call(call.tool_call_id)
            recorder.complete_tool_call(call.tool_call_id, output_hash)
        recorder.complete_run(run.agent_run_id, trace_uri=f"qa-worker:{worker_id}")


def build_default_trace_bridge() -> WorkerTraceBridge | None:
    if os.environ.get("QA_TRACE_PERSIST", "false").strip().lower() != "true":
        return None
    dsn = (
        os.environ.get("RISK_QA_DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )
    if not dsn:
        raise WorkerTraceBridgeError("QA_TRACE_PERSIST requires DATABASE_URL")
    return WorkerTraceBridge(PostgresAuditRepository.connect(dsn))


def _compact(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
