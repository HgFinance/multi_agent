"""CEO Office query boundary for closed-loop Kanban workflows."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping, Sequence

try:
    from . import hermes_boundary
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    import hermes_boundary  # type: ignore[no-redef]

from fastapi import APIRouter, HTTPException

from orchestration.canonical_profiles import (
    CANONICAL_PROFILES,
    canonical_profile_for_department,
)
from orchestration.ceo_workflow_scope import build_root_body


router = APIRouter(prefix="/ui/ceo", tags=["ceo-office"])


_PLANNING_SCHEMA_VERSION = "ceo.query-accepted.v2"
_PRIMARY_PROFILE_ORDER = (
    "research-department",
    "quant-backtest-department",
    "trading-department",
    "accounting-portfolio-department",
    "risk-management",
    "hr-department",
)
_PROFILE_COPY = {
    "research-department": "최신 공시·뉴스·산업 근거를 수집",
    "quant-backtest-department": "정량 검증과 전략 후보를 평가",
    "trading-department": "실행 가능성과 주문 경로를 검토",
    "accounting-portfolio-department": "포트폴리오·NAV 영향을 검토",
    "risk-management": "사업·규제·시장 리스크를 검토",
    "hr-department": "인력·역할·역량을 검토",
}
_PROFILE_LABEL = {
    "research-department": "Research",
    "quant-backtest-department": "Quant",
    "trading-department": "Trading",
    "accounting-portfolio-department": "Accounting/Portfolio",
    "risk-management": "Risk",
    "hr-department": "HR",
}
_PROFILE_ALIASES = {
    "research-department": ("research-department", "research", "리서치"),
    "quant-backtest-department": ("quant-backtest-department", "quant", "퀀트"),
    "trading-department": ("trading-department", "trading", "트레이딩"),
    "accounting-portfolio-department": (
        "accounting-portfolio-department",
        "accounting",
        "portfolio",
        "회계",
        "포트폴리오",
    ),
    "risk-management": ("risk-management", "risk management", "risk", "리스크"),
    "hr-department": ("hr-department", "workforce", "human resources", "인사"),
}


def _child_records(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _planning_profiles(task: Mapping[str, object]) -> tuple[tuple[str, ...], bool, bool]:
    """Read selected profiles only from the current root's planner projection."""
    selected: list[str] = []
    qa_required = False
    synthesis_present = False
    children = _child_records(task.get("children"))
    for child in children:
        assignee = str(child.get("assignee") or "").strip()
        if assignee not in CANONICAL_PROFILES:
            continue
        body = str(child.get("body") or "").casefold()
        role = re.search(r"(?:^|\n)workflow_role=(\w+)", body)
        role_name = role.group(1) if role else ""
        if assignee == "qa-department" or role_name == "qa":
            qa_required = True
        if assignee == "ceo-agent" and role_name == "synthesis":
            synthesis_present = True
        if (
            assignee in _PRIMARY_PROFILE_ORDER
            and role_name in {"", "primary"}
            and assignee not in selected
        ):
            selected.append(assignee)

    # Some Hermes versions expose child IDs rather than child rows on ``show``.
    # In that shape, latest_summary is the planner's durable projection.  It is
    # used only when no child row supplied a department; no fixed pipeline is
    # inferred here.
    summary = str(task.get("latest_summary") or "")
    if summary:
        folded = summary.casefold()
        if not selected:
            for profile in _PRIMARY_PROFILE_ORDER:
                if any(alias.casefold() in folded for alias in _PROFILE_ALIASES[profile]):
                    selected.append(profile)
        if not qa_required and re.search(r"\bqa\b|quality|검증|감사", folded):
            qa_required = True
        if not synthesis_present and re.search(r"synth|합성|최종 의견", folded):
            synthesis_present = True

    return tuple(selected), qa_required, synthesis_present


def _scoped_planning_projection(
    root: Mapping[str, object],
    *,
    timeout: float,
) -> dict[str, object]:
    """Add parentless primary tasks from the current root's scope marker."""
    root_id = str(root.get("id") or root.get("task_id") or "").strip()
    if not root_id:
        return dict(root)
    rows = hermes_boundary.list_kanban_tasks(timeout=timeout)
    if rows is None:
        return dict(root)
    marker = f"workflow_root_task_id={root_id}"
    scoped = [
        row
        for row in rows
        if marker in str(row.get("body") or "").splitlines()
    ]
    if not scoped:
        return dict(root)

    existing = _child_records(root.get("children"))
    by_id: dict[str, Mapping[str, object]] = {}
    for child in (*existing, *scoped):
        child_id = str(child.get("id") or child.get("task_id") or "").strip()
        if child_id:
            by_id[child_id] = child
    projection = dict(root)
    projection["children"] = list(by_id.values())
    return projection


def _planning_acknowledgement(task: Mapping[str, object]) -> dict[str, object]:
    selected, qa_required, synthesis_present = _planning_profiles(task)
    summary = str(task.get("latest_summary") or "").strip() or None
    planned = bool(selected or qa_required)
    # The existing supervisor contract always ends a planned workflow with
    # CEO synthesis, whether QA is required or explicitly skipped.
    synthesis_present = synthesis_present or planned
    actions = [
        f"{_PROFILE_LABEL[profile]}에서 {_PROFILE_COPY[profile]}"
        for profile in selected
    ]
    if actions:
        answer = f"{'· '.join(actions)}하겠습니다."
    else:
        answer = (
            "CEO workflow를 접수했습니다. 실제 planning 결과가 준비되면 "
            "선택된 부서와 다음 단계를 표시하겠습니다."
        )
    if qa_required:
        answer += " QA 검증을 거치겠습니다."
    if synthesis_present:
        if qa_required:
            answer += " 검증 후 CEO가 최종 합성하겠습니다."
        else:
            answer += " 분석 후 CEO가 최종 합성하겠습니다."

    steps = [_PROFILE_LABEL[profile] for profile in selected]
    if qa_required:
        steps.append("QA")
    if synthesis_present:
        steps.append("CEO final synthesis")
    return {
        "status": "planned" if planned else "accepted",
        "answer": answer,
        "planning": {
            "selected_departments": list(selected),
            "steps": steps,
            "qa_required": qa_required,
            "summary": summary,
        },
    }


def _accepted_fallback() -> dict[str, object]:
    return {
        "status": "accepted",
        "answer": (
            "CEO Kanban workflow accepted. Planning summary will appear when "
            "available; final synthesis will be produced by the closed-loop supervisor."
        ),
        "planning": {
            "selected_departments": [],
            "steps": [],
            "qa_required": False,
            "summary": None,
        },
    }


def _planning_read_timeout() -> float:
    try:
        return max(0.1, float(os.getenv("CEO_PLANNING_READ_TIMEOUT_SECONDS", "2")))
    except ValueError:
        return 2.0


def _wait_for_planning(task_id: str) -> dict[str, object]:
    """Poll the existing root briefly without blocking on CEO inference."""
    try:
        wait_seconds = max(0.0, float(os.getenv("CEO_PLANNING_WAIT_SECONDS", "4")))
    except ValueError:
        wait_seconds = 4.0
    read_timeout = _planning_read_timeout()
    deadline = time.monotonic() + wait_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining < 0:
            return _accepted_fallback()
        payload = hermes_boundary.show_kanban_task(
            task_id,
            timeout=min(
                max(0.1, remaining),
                read_timeout,
            ),
        )
        if payload is None:
            return _accepted_fallback()
        payload = _scoped_planning_projection(
            payload,
            timeout=min(max(0.1, remaining), read_timeout),
        )
        acknowledgement = _planning_acknowledgement(payload)
        if acknowledgement["status"] == "planned":
            return acknowledgement
        if remaining <= 0:
            return acknowledgement
        time.sleep(min(0.2, remaining))


def _response(task: Mapping[str, object], acknowledgement: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": _PLANNING_SCHEMA_VERSION,
        "department": "ceo-agent",
        "binding": False,
        "task_id": task.get("task_id") or task.get("id"),
        "task": dict(task),
        "status": acknowledgement["status"],
        "answer": acknowledgement["answer"],
        "planning": acknowledgement["planning"],
        "session_id": None,
    }


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

    return _response(task, _wait_for_planning(str(task["task_id"])))


@router.get("/tasks/{task_id}", operation_id="ceo_task_status")
def ceo_task_status(task_id: str) -> dict[str, object]:
    """Expose the current root/planning projection for frontend polling."""
    task = hermes_boundary.show_kanban_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="CEO Kanban task를 찾을 수 없습니다.")
    task = _scoped_planning_projection(
        task,
        timeout=_planning_read_timeout(),
    )
    return _response(
        {"task_id": task.get("id") or task_id, **task},
        _planning_acknowledgement(task),
    )


__all__ = ["router"]
