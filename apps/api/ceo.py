"""CEO Office query boundary for closed-loop Kanban workflows."""

from __future__ import annotations

try:
    from . import hermes_boundary
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    import hermes_boundary  # type: ignore[no-redef]

from fastapi import APIRouter, HTTPException

from orchestration.canonical_profiles import canonical_profile_for_department
from orchestration.ceo_workflow_scope import build_root_body


router = APIRouter(prefix="/ui/ceo", tags=["ceo-office"])


@router.post("/ask", operation_id="ceo_query", status_code=202)
def ceo_query(req: hermes_boundary.AgentAsk) -> dict[str, object]:
    """Enqueue a CEO Kanban workflow without running a second CEO turn."""

    task = hermes_boundary.create_kanban_task(
        assignee=canonical_profile_for_department("ceo"),
        title=f"사용자 질의: {req.query[:120]}",
        body=build_root_body(req.query, req.request_id),
        idempotency_key=req.request_id,
    )

    if not task or not task.get("task_id"):
        # The root task is the durable anchor for the closed-loop workflow.
        # Never claim success when the Kanban graph was not created.
        raise HTTPException(
            status_code=503,
            detail="CEO root Kanban task를 생성하지 못했습니다. Hermes Kanban runtime을 확인하세요.",
        )

    if not hermes_boundary.comment_root_scope(
        task_id=str(task["task_id"]), request_id=req.request_id
    ):
        # Fail closed: a ready root without its concrete scope binding could
        # be dispatched with no durable proof of which root owns its children.
        raise HTTPException(
            status_code=503,
            detail="CEO root Kanban scope를 기록하지 못했습니다. 재시도하세요.",
        )

    return {
        "schema_version": "ceo.query-accepted.v1",
        "department": "ceo-agent",
        "binding": False,
        "task_id": task["task_id"],
        "task": task,
        "answer": "CEO Kanban workflow accepted. Final synthesis will be produced by the closed-loop supervisor.",
        "session_id": None,
    }


__all__ = ["router"]
