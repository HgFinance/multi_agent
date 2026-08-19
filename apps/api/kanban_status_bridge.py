"""Hermes Kanban -> ``agent.status.v1`` normalization boundary.

The bridge accepts a sanitized Kanban task event; it never opens or exposes
the Kanban SQLite database to the browser and never creates or assigns tasks.
The current prototype runtime uses this same adapter, so replacing the input
with ``hermes kanban watch`` or a Redis consumer does not change the projector
contract.
"""

from __future__ import annotations

from threading import RLock
from typing import Any, Mapping

try:  # script execution path
    from agent_status import (
        AGENT_STATUS_PROJECTOR,
        AgentRuntimeStatus,
        AgentStatusEvent,
    )
except ModuleNotFoundError:  # pragma: no cover - package import path
    from apps.api.agent_status import (
        AGENT_STATUS_PROJECTOR,
        AgentRuntimeStatus,
        AgentStatusEvent,
    )


KANBAN_TO_AGENT_STATUS: dict[str, AgentRuntimeStatus] = {
    "triage": "QUEUED",
    "todo": "QUEUED",
    "scheduled": "QUEUED",
    "queued": "QUEUED",
    "in_progress": "RUNNING",
    "running": "RUNNING",
    "waiting_approval": "WAITING_APPROVAL",
    "blocked": "BLOCKED",
    "degraded": "DEGRADED",
    "error": "ERROR",
    "done": "IDLE",
    "completed": "IDLE",
    "cancelled": "IDLE",
    "idle": "IDLE",
}


class KanbanStatusBridge:
    """Normalize sanitized Kanban task changes and publish them idempotently."""

    def __init__(self, projector=AGENT_STATUS_PROJECTOR) -> None:
        self.projector = projector
        self._lock = RLock()
        self._seen_source_events: dict[str, AgentStatusEvent] = {}

    def publish_task_event(self, event: Mapping[str, Any]) -> AgentStatusEvent:
        source_event_id = str(event.get("event_id") or event.get("task_event_id") or "")
        if not source_event_id:
            raise ValueError("kanban task event requires event_id")
        with self._lock:
            previous = self._seen_source_events.get(source_event_id)
            if previous is not None:
                return previous

            source_status = str(event.get("status", "")).strip().lower()
            try:
                status = KANBAN_TO_AGENT_STATUS[source_status]
            except KeyError as exc:
                raise ValueError(f"unsupported kanban status: {source_status}") from exc
            department_code = str(event.get("department_code") or event.get("department_id") or "")
            agent_id = str(event.get("agent_id") or event.get("profile_id") or event.get("task_id") or "")
            if not department_code or not agent_id:
                raise ValueError("kanban task event requires department_code and agent/profile/task id")
            projected = self.projector.publish(
                event_id=f"kanban-{source_event_id}",
                department_code=department_code,
                agent_id=agent_id,
                worker_id=event.get("worker_id") or event.get("agent_id") or event.get("profile_id"),
                status=status,
                role=event.get("role"),
                case_id=event.get("case_id"),
                trace_id=event.get("trace_id"),
                reason=event.get("reason") or event.get("title"),
                producer="hermes-kanban-status-bridge",
            )
            self._seen_source_events[source_event_id] = projected
            return projected


KANBAN_STATUS_BRIDGE = KanbanStatusBridge()


__all__ = [
    "KANBAN_STATUS_BRIDGE",
    "KANBAN_TO_AGENT_STATUS",
    "KanbanStatusBridge",
]
