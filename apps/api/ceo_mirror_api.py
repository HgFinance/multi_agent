"""FastAPI adapter for the shared Web/Discord CEO execution timeline."""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

try:
    from .ceo_mirror import (
        CanonicalIngress,
        MirrorEvent,
        MirrorEventListResponse,
        MirrorIngressResponse,
        MirrorRequestConflict,
        MirrorStore,
        MirrorStoreUnavailable,
        build_default_mirror_store,
        execute_once,
        publish_mirror_event,
        stable_event_id,
    )
    from .ceo_schemas import CeoQueryAcceptedResponse
    from .hermes_boundary import AgentAsk
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    from ceo_mirror import (  # type: ignore[no-redef]
        CanonicalIngress,
        MirrorEvent,
        MirrorEventListResponse,
        MirrorIngressResponse,
        MirrorRequestConflict,
        MirrorStore,
        MirrorStoreUnavailable,
        build_default_mirror_store,
        execute_once,
        publish_mirror_event,
        stable_event_id,
    )
    from ceo_schemas import CeoQueryAcceptedResponse  # type: ignore[no-redef]
    from hermes_boundary import AgentAsk  # type: ignore[no-redef]


router = APIRouter(prefix="/ui/ceo", tags=["ceo-mirror"])
MIRROR_STORE: MirrorStore = build_default_mirror_store()


def _ceo_query(request: CanonicalIngress) -> dict[str, Any]:
    # Lazy import avoids making apps.api.ceo import the mirror adapter and
    # keeps the existing CEO module's public contract unchanged.
    try:
        from . import ceo
    except ImportError:  # pragma: no cover
        import ceo  # type: ignore[no-redef]

    return ceo.ceo_query(AgentAsk(query=request.query, request_id=request.request_id))


def _execute(request: CanonicalIngress):
    try:
        return execute_once(
            request,
            store=MIRROR_STORE,
            execute=lambda: _ceo_query(request),
        )
    except MirrorRequestConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MirrorStoreUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="CEO mirror dedup store is unavailable"
        ) from exc


def _publish_workflow_projection(request_id: str) -> None:
    """Project sanitized Kanban states into the shared mirror stream.

    Kanban remains the execution source of truth. This projection never stores
    prompts, tool transcripts, or hidden reasoning and is idempotent by event
    id, so repeated SSE polling cannot duplicate events.
    """

    record = MIRROR_STORE.get_request(request_id)
    if record is None or not record.response:
        return
    task_id = str(record.response.get("task_id") or "").strip()
    if not task_id:
        return
    try:
        try:
            from .ceo_kanban_read import load_workflow
        except ImportError:  # pragma: no cover
            from ceo_kanban_read import load_workflow  # type: ignore[no-redef]

        workflow = load_workflow(task_id)
    except Exception:  # noqa: BLE001 - read projection must not break SSE.
        return

    request = record.request
    for node in workflow.nodes:
        status = str(node.status or "unknown").casefold()
        if node.is_qa:
            event_type = (
                "QA_RESULT"
                if status in {"done", "completed", "failed", "blocked"}
                else "QA_STARTED"
            )
            lane = "evaluation"
        elif node.role(root_task_id=workflow.root_task_id) == "synthesis":
            event_type = (
                "CEO_FINAL"
                if status in {"done", "completed"}
                else "CEO_SYNTHESIS_STARTED"
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
            MIRROR_STORE,
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


@router.post(
    "/ask",
    status_code=202,
    operation_id="ceo_query_mirror_compat",
    response_model=CeoQueryAcceptedResponse,
)
def mirror_ask(
    request: AgentAsk,
    x_source_message_id: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Existing Web contract, wrapped with source/request deduplication."""

    canonical = CanonicalIngress(
        query=request.query,
        request_id=request.request_id,
        source="web",
        source_message_id=x_source_message_id or request.request_id,
        actor_id=x_actor_id or "web-user",
        actor_type="user",
    )
    execution = _execute(canonical)
    if execution.response is None:
        raise HTTPException(status_code=202, detail="request_in_progress")
    return execution.response


@router.post("/ingress", status_code=202, response_model=MirrorIngressResponse)
def mirror_ingress(request: CanonicalIngress) -> MirrorIngressResponse:
    """Canonical ingress for a human Web or Discord message."""

    execution = _execute(request)
    response = execution.response
    return MirrorIngressResponse(
        accepted=execution.accepted,
        duplicate=execution.duplicate,
        ignored=execution.ignored,
        reason=execution.reason,
        request_id=request.request_id,
        source=request.source,
        task_id=str(response.get("task_id"))
        if response and response.get("task_id")
        else None,
        execution_count=0 if execution.duplicate or execution.ignored else 1,
        ceo=response,
    )


@router.get("/events", response_model=MirrorEventListResponse)
def mirror_events(
    request_id: str = Query(min_length=8, max_length=128),
    after: str | None = Query(default=None, min_length=8, max_length=128),
) -> MirrorEventListResponse:
    _publish_workflow_projection(request_id)
    return MirrorEventListResponse(
        request_id=request_id, events=MIRROR_STORE.read_events(request_id, after)
    )


@router.get("/events/stream")
def mirror_event_stream(
    request_id: str = Query(min_length=8, max_length=128),
    after: str | None = Query(default=None, min_length=8, max_length=128),
) -> StreamingResponse:
    """Short-lived SSE; clients reconnect with the last event_id cursor."""

    def generate():
        cursor = after
        deadline = time.monotonic() + max(
            1.0, float(os.getenv("UI_MIRROR_SSE_SECONDS", "25"))
        )
        while time.monotonic() < deadline:
            _publish_workflow_projection(request_id)
            events = MIRROR_STORE.read_events(request_id, cursor)
            for event in events:
                cursor = event.event_id
                yield f"id: {event.event_id}\nevent: {event.event_type}\ndata: {event.model_dump_json()}\n\n"
            if not events:
                yield ": heartbeat\n\n"
                time.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/events", response_model=MirrorEvent, status_code=202)
def publish_event(event: MirrorEvent) -> MirrorEvent:
    """Sanitized adapter for supervisor, department, QA, and HR events."""

    MIRROR_STORE.publish_event(event)
    return event


__all__ = ["MIRROR_STORE", "router"]
