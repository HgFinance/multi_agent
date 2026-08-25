"""Single canonical Kanban-to-CEO-mirror projection.

The BFF endpoint and the reconciliation worker must use the same projection
function.  The Kanban board remains the execution source of truth; Redis is
only the durable, sanitized UI/event journal.  Event ids are deterministic,
so polling and worker retries are safe.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
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
_WORKFLOW_ROOT_RE = re.compile(r"(?m)^workflow_root_task_id=(\S+)\s*$")


def workflow_projection_fingerprint(
    root_task_id: str,
    listed_rows: Sequence[Mapping[str, Any]],
) -> str:
    """Hash the list-row fields that can change the sanitized projection.

    The board list is the discovery snapshot; authoritative hydration still
    uses the existing ``load_workflow``/``kanban show`` path.  This fingerprint
    only prevents repeatedly hydrating terminal workflows whose list rows did
    not change.
    """

    root = str(root_task_id or "").strip()
    projected_rows: list[dict[str, Any]] = []
    for row in listed_rows:
        if not isinstance(row, Mapping):
            continue
        row_id = str(row.get("id") or row.get("task_id") or "").strip()
        body = str(row.get("body") or "")
        marker = _WORKFLOW_ROOT_RE.search(body)
        if row_id != root and (marker is None or marker.group(1) != root):
            continue
        projected_rows.append(
            {
                "id": row_id,
                "assignee": row.get("assignee"),
                "status": row.get("status"),
                "started_at": row.get("started_at"),
                "completed_at": row.get("completed_at"),
                "result": row.get("result"),
                # Role/action/root markers affect event classification.
                "body": body,
            }
        )
    projected_rows.sort(key=lambda item: str(item["id"]))
    payload = json.dumps(
        projected_rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    request_ids: Sequence[str] | None = None,
) -> dict[str, int]:
    """Reconcile accepted CEO requests without requiring a connected browser."""

    counts = {"scanned": 0, "projected": 0, "failed": 0}
    candidates = (
        list(dict.fromkeys(str(item) for item in request_ids if str(item).strip()))[:limit]
        if request_ids is not None
        else store.list_request_ids(limit=limit)
    )
    for request_id in candidates:
        counts["scanned"] += 1
        try:
            fingerprint: str | None = None
            record = store.get_request(request_id)
            root_task_id = (
                str(record.response.get("task_id") or "").strip()
                if record is not None and record.response
                else ""
            )
            if listed_rows is not None and root_task_id:
                fingerprint = workflow_projection_fingerprint(
                    root_task_id,
                    listed_rows,
                )
                if store.get_projection_state(request_id) == fingerprint:
                    continue
            projected = publish_workflow_projection(
                store, request_id, listed_rows=listed_rows
            )
            counts["projected"] += projected
            if fingerprint is not None and projected > 0:
                store.save_projection_state(request_id, fingerprint)
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
    "workflow_projection_fingerprint",
]
