"""In-process ``agent.status.v1`` projector for the operator BFF.

The production target is Hermes Kanban -> event bus -> projector -> read model.
This adapter keeps the same event envelope and sequence rules for the current
prototype runtime, without exposing Hermes storage or credentials to the UI.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

AGENT_STATUS_EVENT_TYPE = "agent.status.v1"
AgentRuntimeStatus = Literal[
    "OFFLINE",
    "IDLE",
    "QUEUED",
    "RUNNING",
    "WAITING_APPROVAL",
    "BLOCKED",
    "DEGRADED",
    "ERROR",
]


class AgentStatusEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"evt-{uuid4().hex}")
    event_type: Literal["agent.status.v1"] = AGENT_STATUS_EVENT_TYPE
    schema_version: Literal[1] = 1
    sequence: int = Field(ge=1)
    occurred_at: str
    observed_at: str
    department_code: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=256)
    worker_id: str | None = Field(default=None, max_length=256)
    status: AgentRuntimeStatus
    role: str | None = Field(default=None, max_length=512)
    case_id: str | None = Field(default=None, max_length=256)
    trace_id: str | None = Field(default=None, max_length=256)
    reason: str | None = Field(default=None, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    producer: str = "portfolio-runtime-adapter"


class AgentStatusProjector:
    """Apply ordered status events and expose a browser-safe read projection."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._sequence = 0
        self._states: dict[str, dict[str, Any]] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=200)

    def publish(
        self,
        *,
        event_id: str | None = None,
        department_code: str,
        agent_id: str,
        status: AgentRuntimeStatus,
        worker_id: str | None = None,
        role: str | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        case_id: str | None = None,
        trace_id: str | None = None,
        producer: str = "portfolio-runtime-adapter",
    ) -> AgentStatusEvent:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._sequence += 1
            event = AgentStatusEvent(
                event_id=event_id or f"evt-{uuid4().hex}",
                sequence=self._sequence,
                occurred_at=now,
                observed_at=now,
                department_code=department_code,
                agent_id=agent_id,
                worker_id=worker_id,
                status=status,
                role=role,
                reason=reason,
                case_id=case_id,
                trace_id=trace_id,
            producer=producer,
            metadata=metadata or {},
        )
            payload = event.model_dump(mode="json")
            self._states[agent_id] = payload
            self._events.append(payload)
            return event

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "event_type": AGENT_STATUS_EVENT_TYPE,
                "schema_version": 1,
                "sequence": self._sequence,
                "agents": [dict(item) for item in self._states.values()],
                "events": [dict(item) for item in self._events],
            }

    def reset(self) -> None:
        with self._lock:
            self._sequence = 0
            self._states.clear()
            self._events.clear()


AGENT_STATUS_PROJECTOR = AgentStatusProjector()


def publish_agent_status(**kwargs: Any) -> AgentStatusEvent:
    return AGENT_STATUS_PROJECTOR.publish(**kwargs)


def agent_status_snapshot() -> dict[str, Any]:
    return AGENT_STATUS_PROJECTOR.snapshot()


__all__ = [
    "AGENT_STATUS_EVENT_TYPE",
    "AGENT_STATUS_PROJECTOR",
    "AgentStatusEvent",
    "AgentStatusProjector",
    "agent_status_snapshot",
    "publish_agent_status",
]
