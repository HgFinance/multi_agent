"""CEO Office query boundary for the operator UI."""

from __future__ import annotations

try:
    from . import hermes_cli
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    import hermes_cli  # type: ignore[no-redef]

from fastapi import APIRouter


router = APIRouter(prefix="/ui/ceo", tags=["ceo-office"])


@router.post("/ask", operation_id="ceo_query")
def ceo_query(req: hermes_cli.AgentAsk) -> dict[str, object]:
    """Send a non-binding natural-language query to the CEO Hermes Head."""

    task = hermes_cli.create_kanban_task(
        assignee="ceo-agent",
        title=f"사용자 질의: {req.query[:120]}",
        body=req.query,
        idempotency_key=req.request_id,
    )
    result = hermes_cli.ask(
        department="ceo-agent",
        config="departments/00-ceo-office/hermes/config.yaml",
        query=req.query,
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
