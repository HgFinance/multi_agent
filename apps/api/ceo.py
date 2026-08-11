"""CEO Office query boundary for the operator UI."""

from __future__ import annotations

try:
    from . import hermes_cli
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    import hermes_cli  # type: ignore[no-redef]

from fastapi import APIRouter, HTTPException

from orchestration.canonical_profiles import canonical_profile_for_department

from apps.api.ceo_hermes_client import ask_ceo


router = APIRouter(prefix="/ui/ceo", tags=["ceo-office"])


@router.post("/ask", operation_id="ceo_query")
def ceo_query(req: hermes_cli.AgentAsk) -> dict[str, object]:
    """Send a non-binding natural-language query to the CEO Hermes Head."""

    task = hermes_cli.create_kanban_task(
        assignee=canonical_profile_for_department("ceo"),
        title=f"사용자 질의: {req.query[:120]}",
        body=req.query,
        idempotency_key=req.request_id,
    )
    if not task or not task.get("task_id"):
        # The root task is the durable anchor for the closed-loop workflow.
        # Never call the CEO after this boundary failed: otherwise the user
        # receives an answer with no Kanban graph to supervise.
        raise HTTPException(
            status_code=503,
            detail="CEO root Kanban task를 생성하지 못했습니다. Hermes Kanban runtime을 확인하세요.",
        )

    result = ask_ceo(
        query=(
            "Closed-loop Kanban context: the durable CEO root task is "
            f"{task['task_id']}. Use this task as the parent for every "
            "dynamic child task and keep the workflow closed-loop.\n\n"
            f"Original user request:\n{req.query}"
        ),
        timeout=hermes_cli.timeout_of(
            "departments/00-ceo-office/hermes/config.yaml"
        ),
    )
    return {
        "schema_version": "ceo.query-result.v1",
        "department": "ceo-agent",
        "binding": False,
        "task": task,
        "answer": result["answer"],
        "session_id": result.get("session_id"),
    }


__all__ = ["router"]
