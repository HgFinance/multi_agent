"""CEO Office query boundary and closed-loop Kanban workflow APIs.

`/ui/ceo/ask` creates only the CEO root task.  The CEO Supervisor owns
planning, department-task creation, QA, and final synthesis.  All read paths
use the normalized Kanban reader; the BFF never opens Hermes' database.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor

try:
    from . import hermes_boundary
    from .ceo_kanban_read import (
        KanbanTaskNotFound,
        KanbanUnavailable,
        Workflow,
        archive_tasks,
        extract_user_query,
        list_ceo_roots,
        load_workflow,
    )
    from .current_user import optional_current_user
    from .governance_client import fetch_current_mandate_by_fund
    from .ceo_schemas import (
        CeoPlanning,
        CeoQueryAcceptedResponse,
        GraphNode,
        TaskArchiveResponse,
        TaskGraphResponse,
        TaskListItem,
        TaskListResponse,
        TaskProgress,
        TaskResult,
        TaskResultResponse,
        TaskStatusResponse,
        TaskWorkflow,
    )
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    import hermes_boundary  # type: ignore[no-redef]
    from ceo_kanban_read import (  # type: ignore[no-redef]
        KanbanTaskNotFound,
        KanbanUnavailable,
        Workflow,
        archive_tasks,
        extract_user_query,
        list_ceo_roots,
        load_workflow,
    )
    from current_user import optional_current_user  # type: ignore[no-redef]
    from governance_client import fetch_current_mandate_by_fund  # type: ignore[no-redef]
    from ceo_schemas import (  # type: ignore[no-redef]
        CeoPlanning,
        CeoQueryAcceptedResponse,
        GraphNode,
        TaskArchiveResponse,
        TaskGraphResponse,
        TaskListItem,
        TaskListResponse,
        TaskProgress,
        TaskResult,
        TaskResultResponse,
        TaskStatusResponse,
        TaskWorkflow,
    )

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from orchestration.canonical_profiles import (
    CANONICAL_PROFILES,
    canonical_profile_for_department,
)
from orchestration.ceo_workflow_scope import (
    build_root_body,
    infer_workflow_mode,
    selected_primary_profiles_from_body,
)


router = APIRouter(prefix="/ui/ceo", tags=["ceo-office"])
logger = logging.getLogger(__name__)


class CeoAsk(hermes_boundary.AgentAsk):
    """`/ui/ceo/ask` 전용 Body. `AgentAsk` + `fund_id`.

    `fund_id`를 `AgentAsk`에 넣지 않은 이유: 그 모델은 부서 Agent 질의 6개가
    함께 쓰는 계약이고, 거기에 CEO 전용 필드를 넣으면 트레이딩·회계 질의에도
    쓰지 않는 필드가 노출된다.

    **왜 서버가 user_id로 fund를 찾지 않고 화면이 보내나**: `governance.fund_memberships`
    (user<->fund 연결 테이블)가 아직 비어 있어 `user_id -> fund_id` 역참조 경로가
    없다. 프론트엔드가 계정을 하드코딩하는 단계이므로 `fund_id`도 그 쌍으로 함께
    보내는 편이 조회 경로를 새로 만드는 것보다 단순하다. 진짜 로그인이 붙으면
    그때 `fund_memberships`로 옮긴다.
    """

    fund_id: str | None = None


# Hermes Task ID 형식(`t_` + hex). 경로 파라미터가 CLI 인자로 들어가므로
# 서버에서 먼저 모양을 고정한다. shell=False라 주입 경로는 아니지만, 형식이
# 틀린 값을 CLI까지 보내 404를 만들 이유가 없다.
_TASK_ID_PATTERN = r"^t_[A-Za-z0-9]{4,64}$"
_TASK_ID_PATH = Path(
    description="Kanban Task ID. Root ID를 권장하지만 자식 ID도 Root로 해석한다.",
    pattern=_TASK_ID_PATTERN,
    examples=["t_c2f6fe62"],
)
_LIST_WORKERS = max(1, int(os.getenv("CEO_TASK_LIST_WORKERS", "4")))
_LIST_GRAPH_WORKERS = max(1, int(os.getenv("CEO_TASK_LIST_GRAPH_WORKERS", "3")))

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
    "risk-management": ("risk-management", "risk", "리스크"),
    "hr-department": ("hr-department", "hr", "인사", "워크포스"),
}


def _load(task_id: str, *, max_workers: int | None = None) -> Workflow:
    """Load a root workflow and translate CLI failures to HTTP errors."""

    try:
        return load_workflow(task_id, max_workers=max_workers)
    except KanbanTaskNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Task를 찾을 수 없습니다: {task_id}") from exc
    except KanbanUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Hermes Kanban을 읽지 못했습니다: {exc}") from exc


def _child_records(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _planning_profiles(task: Mapping[str, object]) -> tuple[list[str], bool, bool]:
    """Read planning only from the current root scope; never infer a fixed pipeline."""

    selected: list[str] = []
    qa_required = False
    synthesis_present = False
    children = _child_records(task.get("children"))
    declared_primary = selected_primary_profiles_from_body(str(task.get("body") or ""))
    if declared_primary:
        selected.extend(declared_primary)
    metadata = task.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    for key in ("task_run_metadata", "run_metadata"):
        extra = task.get(key)
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except (TypeError, ValueError):
                extra = {}
        if isinstance(extra, Mapping):
            metadata = {**metadata, **extra}
    task_run = task.get("task_run")
    if isinstance(task_run, Mapping):
        task_run_metadata = task_run.get("metadata", task_run)
        if isinstance(task_run_metadata, Mapping):
            metadata = {**metadata, **task_run_metadata}
    workflow_metadata = metadata.get("workflow_metadata")
    if isinstance(workflow_metadata, Mapping):
        metadata = {**metadata, **workflow_metadata}
    runs = task.get("runs")
    if isinstance(runs, Sequence) and not isinstance(runs, (str, bytes, bytearray)):
        for run in runs:
            if not isinstance(run, Mapping):
                continue
            run_metadata = run.get("metadata")
            if isinstance(run_metadata, str):
                try:
                    run_metadata = json.loads(run_metadata)
                except (TypeError, ValueError):
                    run_metadata = {}
            if isinstance(run_metadata, Mapping):
                metadata = {**metadata, **run_metadata}
                nested = run_metadata.get("workflow_metadata")
                if isinstance(nested, Mapping):
                    metadata = {**metadata, **nested}

    for child in children:
        assignee = str(child.get("assignee") or child.get("profile") or "").strip()
        if assignee not in CANONICAL_PROFILES:
            continue
        body = str(child.get("body") or "").casefold()
        role_match = re.search(r"(?:^|\n)workflow_role=(\w+)", body)
        role = role_match.group(1) if role_match else ""
        if assignee == "qa-department" and role == "qa":
            qa_required = True
        elif assignee == "ceo-agent" and role == "synthesis":
            synthesis_present = True
        elif assignee in _PRIMARY_PROFILE_ORDER and role in {"", "primary"}:
            if assignee not in selected and not declared_primary:
                selected.append(assignee)

    declared_departments = metadata.get("selected_departments")
    if isinstance(declared_departments, str):
        try:
            declared_departments = json.loads(declared_departments)
        except (TypeError, ValueError):
            declared_departments = ()
    if isinstance(declared_departments, Sequence) and not isinstance(
        declared_departments, (str, bytes, bytearray)
    ):
        for profile in declared_departments:
            profile = str(profile).strip()
            if (
                not declared_primary
                and profile in _PRIMARY_PROFILE_ORDER
                and profile not in selected
            ):
                selected.append(profile)
    declared_qa = metadata.get("qa_required")
    if isinstance(declared_qa, str):
        declared_qa = declared_qa.strip().casefold() == "true"
    if isinstance(declared_qa, bool):
        qa_required = qa_required or declared_qa

    summary = str(task.get("latest_summary") or "").casefold()
    if not selected and summary:
        for profile in _PRIMARY_PROFILE_ORDER:
            if any(alias.casefold() in summary for alias in _PROFILE_ALIASES[profile]):
                selected.append(profile)
    if not qa_required and re.search(r"\bqa\b|quality|검증|감사", summary):
        qa_required = True
    if not synthesis_present and re.search(r"synth|합성|최종 의견", summary):
        synthesis_present = True
    return selected, qa_required, synthesis_present


def _planning_summary(
    task: Mapping[str, object],
    selected: Sequence[str],
    qa_required: bool,
) -> str | None:
    """Return user-facing planning prose consistent with the workflow lane."""
    existing = str(task.get("latest_summary") or "").strip() or None
    body = str(task.get("body") or "").casefold()
    binding = bool(re.search(r"(?:^|\n)workflow_mode=binding(?:\n|$)", body))
    if not binding:
        labels = [_PROFILE_LABEL[profile] for profile in selected if profile in _PROFILE_LABEL]
        if labels:
            subject = "와 ".join(labels)
            return (
                f"관련 부서({subject})의 근거 기반 분석 결과가 준비되는 대로 "
                "CEO가 종합 분석을 전달하겠습니다."
            )
        return "관련 부서의 근거 기반 분석 결과가 준비되는 대로 CEO가 종합 분석을 전달하겠습니다."
    return existing


def _scoped_planning_projection(
    root: Mapping[str, object], *, timeout: float
) -> dict[str, object]:
    """Merge parentless primary tasks using the durable root scope marker."""

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
        if isinstance(row, Mapping)
        and marker in str(row.get("body") or "").splitlines()
    ]
    if not scoped:
        return dict(root)
    by_id: dict[str, Mapping[str, object]] = {}
    for child in (*_child_records(root.get("children")), *scoped):
        child_id = str(child.get("id") or child.get("task_id") or "").strip()
        if child_id:
            by_id[child_id] = child
    projection = dict(root)
    projection["children"] = list(by_id.values())
    return projection


def _planning_acknowledgement(task: Mapping[str, object]) -> dict[str, object]:
    selected, qa_required, synthesis_present = _planning_profiles(task)
    actions = [f"{_PROFILE_LABEL[p]}에서 {_PROFILE_COPY[p]}" for p in selected]
    if actions:
        answer = f"{'· '.join(actions)}하겠습니다."
    else:
        answer = "CEO workflow를 접수했습니다. 실제 planning 결과가 준비되면 선택된 부서와 다음 단계를 표시하겠습니다."
    if synthesis_present:
        answer += " CEO가 최종 종합합니다."
    steps = [_PROFILE_LABEL[p] for p in selected]
    if qa_required:
        steps.append("QA")
    if synthesis_present:
        steps.append("CEO Synthesis")
    planned = bool(selected or qa_required or synthesis_present)
    return {
        "status": "planned" if planned else "accepted",
        "planning": {
            "selected_departments": selected,
            "steps": steps,
            "qa_required": qa_required,
            "summary": _planning_summary(task, selected, qa_required),
        },
        "answer": answer,
    }


def _accepted_fallback() -> dict[str, object]:
    return {
        "status": "accepted",
        "planning": {
            "selected_departments": [],
            "steps": [],
            "qa_required": False,
            "summary": None,
        },
        "answer": "CEO workflow를 접수했습니다. 실제 planning 결과가 준비되면 선택된 부서와 다음 단계를 표시하겠습니다.",
    }


def _planning_read_timeout() -> float:
    try:
        return max(0.1, float(os.getenv("CEO_PLANNING_READ_TIMEOUT_SECONDS", "2")))
    except ValueError:
        return 2.0


def _wait_for_planning(task_id: str) -> dict[str, object]:
    """Poll briefly for an already-created supervisor projection."""

    try:
        wait_seconds = max(0.0, float(os.getenv("CEO_PLANNING_WAIT_SECONDS", "4")))
    except ValueError:
        wait_seconds = 4.0
    deadline = time.monotonic() + wait_seconds
    read_timeout = _planning_read_timeout()
    while True:
        remaining = deadline - time.monotonic()
        if remaining < 0:
            return _accepted_fallback()
        payload = hermes_boundary.show_kanban_task(
            task_id, timeout=min(max(0.1, remaining), read_timeout)
        )
        if payload is None:
            return _accepted_fallback()
        acknowledgement = _planning_acknowledgement(
            _scoped_planning_projection(
                payload, timeout=min(max(0.1, remaining), read_timeout)
            )
        )
        if acknowledgement["status"] == "planned" or remaining <= 0:
            return acknowledgement
        time.sleep(min(0.2, remaining))


def _accepted_response(task: Mapping[str, object], planning: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": _PLANNING_SCHEMA_VERSION,
        "department": "ceo-agent",
        "binding": False,
        "task_id": str(task.get("task_id") or task.get("id") or ""),
        "task": dict(task),
        "status": planning["status"],
        "answer": planning["answer"],
        "planning": planning["planning"],
        "session_id": None,
    }


@router.post(
    "/ask",
    operation_id="ceo_query",
    status_code=202,
    response_model=CeoQueryAcceptedResponse,
    summary="CEO에게 새 분석을 요청한다",
)
def ceo_query(
    req: CeoAsk,
    owner_id: str | None = Depends(optional_current_user),
) -> dict[str, object]:
    """Create the CEO root task; supervisor execution remains asynchronous.

    `owner_id`(`X-User-Id`)는 2026-08-12에 추가됐다. 그 전까지 이 경로는 요청자를
    **아예 몰랐다** - `AgentAsk`에 `query`와 `request_id`만 있어서, CEO는 누가
    물었는지도 그 사람의 Mandate가 무엇인지도 알 수 없었다.
    """

    # Mandate 스냅샷. 못 읽으면 None이고 그때는 블록 없이 진행한다 - 이것 때문에
    # 질의 접수가 실패하면 Mandate가 없는 사용자는 아무 질문도 못 한다.
    # `getattr`로 읽는 이유: 이 라우트는 `CeoAsk`를 받지만, 부서 질의와 같은
    # `AgentAsk`를 직접 넘기는 호출부(테스트·내부 유틸)가 남아 있을 수 있고 그쪽엔
    # `fund_id`가 없다. 속성 부재로 500을 내는 대신 "Mandate 없음"으로 떨어진다.
    fund_id = getattr(req, "fund_id", None)
    mandate = fetch_current_mandate_by_fund(fund_id) if fund_id else None

    task = hermes_boundary.create_kanban_task(
        assignee=canonical_profile_for_department("ceo"),
        title=f"사용자 질의: {req.query[:120]}",
        body=build_root_body(
            req.query,
            req.request_id,
            workflow_mode=infer_workflow_mode(req.query),
            mandate=mandate,
        ),
        idempotency_key=req.request_id,
    )
    if not task or not task.get("task_id"):
        raise HTTPException(
            status_code=503,
            detail="CEO root Kanban task를 생성하지 못했습니다. Hermes Kanban runtime을 확인하세요.",
        )
    logger.info(
        "ceo-planning root=%s request_id=%s producer=portfolio-bff",
        task["task_id"],
        req.request_id,
    )

    if not hermes_boundary.comment_root_scope(
        task_id=str(task["task_id"]), request_id=req.request_id
    ):
        raise HTTPException(
            status_code=503,
            detail="CEO root Kanban scope를 기록하지 못했습니다. 재시도하세요.",
        )
    return _accepted_response(task, _wait_for_planning(str(task["task_id"])))


def _planning_status_payload(task_id: str) -> dict[str, object]:
    """Compatibility projection used by the qa-department client/tests.

    The canonical read API remains the PR #224 workflow model.  This helper
    only reads the root plus the explicit scope marker and never creates or
    mutates a task.
    """

    raw = hermes_boundary.show_kanban_task(task_id, timeout=_planning_read_timeout())
    if not raw:
        raise HTTPException(status_code=404, detail="CEO Kanban task를 찾을 수 없습니다.")
    projection = _scoped_planning_projection(raw, timeout=_planning_read_timeout())
    return _accepted_response(
        {"task_id": str(projection.get("id") or task_id), **projection},
        _planning_acknowledgement(projection),
    )


def _status_payload(workflow: Workflow) -> dict[str, object]:
    payload = TaskStatusResponse(
        task_id=workflow.root_task_id,
        root_task_id=workflow.root_task_id,
        status=workflow.status,
        assignee=workflow.root.profile,
        query=workflow.query,
        created_at=workflow.root.created_at,
        completed_at=workflow.root.completed_at,
        workflow=TaskWorkflow(
            selected_departments=list(workflow.selected_departments),
            qa_required=workflow.qa_required,
        ),
        progress=TaskProgress(
            primary_total=len(workflow.primary_nodes),
            primary_done=sum(1 for node in workflow.primary_nodes if node.done),
            qa=workflow.qa_stage,
            synthesis=workflow.synthesis_stage,
        ),
    )
    return payload.model_dump()


@router.get("/tasks", operation_id="ceo_task_list", response_model=TaskListResponse)
def ceo_task_list(
    limit: int = Query(default=20, ge=1, le=100),
    include_archived: bool = Query(default=False),
) -> TaskListResponse:
    try:
        rows = list_ceo_roots(limit=limit, include_archived=include_archived)
    except KanbanUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Hermes Kanban을 읽지 못했습니다: {exc}") from exc
    identified = [
        (str(row.get("id") or row.get("task_id") or ""), str(row.get("body") or ""))
        for row in rows
    ]
    identified = [(task_id, body) for task_id, body in identified if task_id]
    if not identified:
        return TaskListResponse(items=[])
    with ThreadPoolExecutor(max_workers=_LIST_WORKERS) as pool:
        workflows = list(
            pool.map(
                lambda item: _load(item[0], max_workers=_LIST_GRAPH_WORKERS),
                identified,
            )
        )
    return TaskListResponse(
        items=[
            TaskListItem(
                task_id=workflow.root_task_id,
                query=extract_user_query(body),
                status=workflow.status,
                created_at=workflow.root.created_at,
                selected_departments=list(workflow.selected_departments),
            )
            for (task_id, body), workflow in zip(identified, workflows)
        ]
    )


@router.get("/tasks/{task_id}", operation_id="ceo_task_status", response_model=TaskStatusResponse)
def ceo_task_status(task_id: str = _TASK_ID_PATH) -> dict[str, object]:
    """Return the canonical PR #224 status with an additive planning field."""

    try:
        workflow = _load(task_id)
    except HTTPException as exc:
        # Keep the qa-department direct-call contract as a read-only fallback
        # for runtimes where the normalized reader is not installed yet.
        if exc.status_code != 503:
            raise
        raw = hermes_boundary.show_kanban_task(task_id, timeout=_planning_read_timeout())
        if not raw:
            raise
        projection = _scoped_planning_projection(raw, timeout=_planning_read_timeout())
        acknowledgement = _planning_acknowledgement(projection)
        selected = acknowledgement["planning"]["selected_departments"]
        raw_status = str(raw.get("status") or "queued").casefold()
        status = {"ready": "queued", "todo": "queued", "done": "completed"}.get(
            raw_status, raw_status
        )
        return {
            "schema_version": "ceo.task-status.v1",
            "task_id": str(raw.get("id") or raw.get("task_id") or task_id),
            "root_task_id": str(raw.get("id") or raw.get("task_id") or task_id),
            "status": status,
            "assignee": str(raw.get("assignee") or "ceo-agent"),
            "query": extract_user_query(str(raw.get("body") or "")),
            "created_at": None,
            "completed_at": None,
            "workflow": {
                "selected_departments": selected,
                "qa_required": acknowledgement["planning"]["qa_required"],
            },
            "progress": {
                "primary_total": len(selected),
                "primary_done": 0,
                "qa": "todo",
                "synthesis": "todo",
            },
            "planning": acknowledgement["planning"],
        }
    payload = _status_payload(workflow)
    try:
        raw = hermes_boundary.show_kanban_task(task_id, timeout=_planning_read_timeout())
        if raw:
            payload["planning"] = _planning_acknowledgement(
                _scoped_planning_projection(raw, timeout=_planning_read_timeout())
            )["planning"]
    except (KanbanTaskNotFound, KanbanUnavailable):
        pass
    return payload


@router.get("/tasks/{task_id}/graph", operation_id="ceo_task_graph", response_model=TaskGraphResponse)
def ceo_task_graph(task_id: str = _TASK_ID_PATH) -> TaskGraphResponse:
    workflow = _load(task_id)
    return TaskGraphResponse(
        root=workflow.root_task_id,
        nodes=[
            GraphNode(
                id=node.task_id,
                department=node.profile,
                status=node.status,
                role=node.role(root_task_id=workflow.root_task_id),
                title=node.title,
            )
            for node in workflow.nodes
        ],
        edges=list(workflow.edges),
    )


@router.get("/tasks/{task_id}/result", response_model=TaskResultResponse)
def ceo_task_result(task_id: str = _TASK_ID_PATH) -> TaskResultResponse:
    workflow = _load(task_id)
    synthesis = workflow.synthesis_node
    terminal = workflow.status in {"completed", "blocked", "failed", "archived"}
    result = None
    if synthesis is not None and synthesis.done and synthesis.summary:
        result = TaskResult(
            summary=synthesis.summary,
            decision=workflow.decision,
            qa_verdict=workflow.qa_verdict,
        )
    elif not workflow.descendants and workflow.root.done and workflow.root.summary:
        # CEO가 부서에 위임하지 않고 root Task 안에서 직접 답한 경우(동적 라우팅 -
        # 이벤트에 맞는 페르소나가 없으면 CEO 혼자 처리한다). synthesis_node가
        # 없다는 이유로 결과를 계속 비워두면, 실제로 완료된 답이 있는데도 화면에
        # 영원히 안 뜬다 - 2026-08-13 "지금 막혀 있는 업무와 이유를 알려줘"에서
        # 실사용 중 확인. `not descendants`로 좁힌 이유: 부서가 있는데 아직
        # synthesis만 안 끝난 진행 중 상태(root의 "접수했다" 문구)를 답으로
        # 잘못 노출하면 안 된다 - 자식이 하나도 없을 때만 root가 곧 답이다.
        result = TaskResult(
            summary=workflow.root.summary,
            decision=workflow.decision,
            qa_verdict=workflow.qa_verdict,
        )
    return TaskResultResponse(
        task_id=task_id,
        status="completed" if terminal else "processing",
        result=result,
        departments=workflow.department_summaries,
        qa_verdict=workflow.qa_verdict,
        block_reason=workflow.block_reason,
    )


@router.post("/tasks/{task_id}/archive", response_model=TaskArchiveResponse)
def ceo_task_archive(task_id: str = _TASK_ID_PATH) -> TaskArchiveResponse:
    workflow = _load(task_id)
    target_ids = [node.task_id for node in workflow.descendants]
    target_ids.append(workflow.root_task_id)
    try:
        archive_tasks(target_ids)
    except KanbanTaskNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Task를 찾을 수 없습니다: {task_id}") from exc
    except KanbanUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Archive에 실패했습니다: {exc}") from exc
    return TaskArchiveResponse(task_id=task_id, archived_task_ids=target_ids)


__all__ = ["router"]
