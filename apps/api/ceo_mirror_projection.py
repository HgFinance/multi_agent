"""Single canonical Kanban-to-CEO-mirror projection.

The BFF endpoint and the reconciliation worker must use the same projection
function.  The Kanban board remains the execution source of truth; Redis is
only the durable, sanitized UI/event journal.  Event ids are deterministic,
so polling and worker retries are safe.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

try:
    from .ceo_kanban_read import load_workflow
    from .ceo_mirror import (
        MirrorStore,
        publish_mirror_event,
        stable_event_id,
    )
except ImportError:  # pragma: no cover - direct module execution compatibility
    from ceo_kanban_read import load_workflow  # type: ignore[no-redef]
    from ceo_mirror import (  # type: ignore[no-redef]
        MirrorStore,
        publish_mirror_event,
        stable_event_id,
    )


LOG = logging.getLogger("ceo-mirror-projection")
_TERMINAL_STATUSES = frozenset({"done", "completed", "failed", "blocked"})


def publish_workflow_projection(
    store: MirrorStore,
    request_id: str,
    *,
    listed_rows: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    """Project one request's current Kanban state and return node count.

    Any read-side Kanban failure is logged and isolated. The request remains
    discoverable by the next reconciliation cycle; a transient read failure
    must not turn the CEO ingress or SSE endpoint into a 500.
    """

    record = store.get_request(request_id)
    if record is None or not record.response:
        return 0
    task_id = str(record.response.get("task_id") or "").strip()
    if not task_id:
        return 0
    try:
        workflow = load_workflow(task_id, listed_rows=listed_rows)
    except Exception:
        LOG.warning(
            "kanban workflow projection read failed",
            extra={"request_id": request_id, "task_id": task_id},
            exc_info=True,
        )
        return 0

    request = record.request
    published = 0
    for node in workflow.nodes:
        status = str(node.status or "unknown").casefold()
        if node.is_qa:
            event_type = "QA_RESULT" if status in _TERMINAL_STATUSES else "QA_STARTED"
            lane = "evaluation"
        elif node.role(root_task_id=workflow.root_task_id) == "synthesis":
            event_type = (
                "CEO_FINAL" if status in {"done", "completed"} else "CEO_SYNTHESIS_STARTED"
            )
            lane = "execution"
        else:
            event_type = {
                "done": "TASK_COMPLETED",
                "completed": "TASK_COMPLETED",
                "failed": "TASK_FAILED",
                "blocked": "TASK_FAILED",
            }.get(status, "TASK_STARTED")
            lane = "execution"
        parent_task_id = node.parents[0] if node.parents else None
        summary = (node.summary or node.error or node.block_reason or "").strip()
        publish_mirror_event(
            store,
            request=request,
            event_type=event_type,
            status=status,
            actor_id=node.profile or "hermes-kanban",
            actor_type="agent",
            lane=lane,
            task_id=node.task_id,
            parent_task_id=parent_task_id,
            summary=summary,
            payload={
                "department_id": node.profile,
                "role": node.role(root_task_id=workflow.root_task_id),
                "run_outcome": node.run_outcome,
            },
            event_id=stable_event_id(
                "workflow", request_id, node.task_id, event_type, status, summary
            ),
        )
        published += 1
    return published


def reconcile_workflow_projections(
    store: MirrorStore,
    *,
    limit: int = 250,
    listed_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, int]:
    """Reconcile accepted CEO requests without requiring a connected browser."""

    counts = {"scanned": 0, "projected": 0, "failed": 0}
    for request_id in store.list_request_ids(limit=limit):
        counts["scanned"] += 1
        try:
            counts["projected"] += publish_workflow_projection(
                store, request_id, listed_rows=listed_rows
            )
        except Exception:
            counts["failed"] += 1
            LOG.warning(
                "workflow projection reconciliation failed",
                extra={"request_id": request_id},
                exc_info=True,
            )
    return counts


__all__ = [
    "publish_workflow_projection",
    "reconcile_workflow_projections",
]
