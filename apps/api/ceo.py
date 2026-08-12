"""CEO Office query boundary for closed-loop Kanban workflows.

`/ui/ceo/ask`가 Root Task를 만들고, 나머지 경로는 그 Root 그래프를 읽는다.
읽기 경로는 전부 `ceo_kanban_read`를 통과한다 - BFF는 `kanban.db`를 직접 열지
않고, Task 생성·QA 판정·Synthesis는 CEO Supervisor 컨테이너가 소유한다.

의도적으로 만들지 않은 것: `DELETE /ui/ceo/tasks/{task_id}`. 누가 언제 무엇을
요청했고 어느 부서가 실패했는지는 감사 추적이며, 정리는 Archive로만 한다.
"""

from __future__ import annotations

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

import os
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from orchestration.canonical_profiles import canonical_profile_for_department
from orchestration.ceo_workflow_scope import build_root_body


router = APIRouter(prefix="/ui/ceo", tags=["ceo-office"])


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

# 목록 경로의 동시 CLI 프로세스 상한은 두 값의 곱이다. Root 20건을 읽어도
# 컨테이너에 뜨는 hermes 프로세스가 12개를 넘지 않게 잡았다.
_LIST_WORKERS = max(1, int(os.getenv("CEO_TASK_LIST_WORKERS", "4")))
_LIST_GRAPH_WORKERS = max(1, int(os.getenv("CEO_TASK_LIST_GRAPH_WORKERS", "3")))

_TASK_ID_PATH = Path(
    description="Kanban Task ID. Root ID를 권장하지만 자식 ID도 Root로 해석한다.",
    pattern=_TASK_ID_PATTERN,
    examples=["t_c2f6fe62"],
)


def _load(task_id: str, *, max_workers: int | None = None) -> Workflow:
    """Root 그래프를 읽는다. CLI 실패를 화면이 읽을 수 있는 오류로 옮긴다."""

    try:
        return load_workflow(task_id, max_workers=max_workers)
    except KanbanTaskNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Task를 찾을 수 없습니다: {task_id}") from exc
    except KanbanUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Hermes Kanban을 읽지 못했습니다: {exc}") from exc


@router.post(
    "/ask",
    operation_id="ceo_query",
    status_code=202,
    response_model=CeoQueryAcceptedResponse,
    summary="CEO에게 새 분석을 요청한다",
    description=(
        "Root Kanban Task만 만들고 즉시 202로 돌려준다. 부서 선택·QA·최종 종합은"
        " CEO Supervisor가 비동기로 진행하므로, 결과는 `/ui/ceo/tasks/{task_id}`"
        " polling 후 `/result`로 가져간다."
    ),
)
def ceo_query(
    req: CeoAsk,
    owner_id: str | None = Depends(optional_current_user),
) -> dict[str, object]:
    """Enqueue a CEO Kanban workflow without running a second CEO turn.

    `owner_id`(`X-User-Id`)는 2026-08-12에 추가됐다. 그 전까지 이 경로는 요청자를
    **아예 몰랐다** - `AgentAsk`에 `query`와 `request_id`만 있어서, CEO는 누가
    물었는지도 그 사람의 Mandate가 무엇인지도 알 수 없었다.

    지금은 요청자를 받아 감사 추적에만 쓴다. Mandate 스냅샷을 root body에 싣는
    작업은 별도이며, 그것이 붙기 전까지 이 값은 "누구의 질의였나"를 기록하는
    용도다 - 값이 판정에 쓰이지는 않는다(USER_INPUT_SPEC 5: 자연어·요청자 맥락은
    판정 근거가 아니다).
    """

    # Mandate 스냅샷. 못 읽으면 None이고 그때는 블록 없이 진행한다 - 이것 때문에
    # 질의 접수가 실패하면 Mandate가 없는 사용자는 아무 질문도 못 한다.
    mandate = fetch_current_mandate_by_fund(req.fund_id) if req.fund_id else None

    task = hermes_boundary.create_kanban_task(
        assignee=canonical_profile_for_department("ceo"),
        title=f"사용자 질의: {req.query[:120]}",
        body=build_root_body(req.query, req.request_id, mandate=mandate),
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


@router.get(
    "/tasks",
    operation_id="ceo_task_list",
    response_model=TaskListResponse,
    summary="최근 CEO 요청 목록",
    description=(
        "사용자가 `/ui/ceo/ask`로 만든 Root만 최신순으로 준다. Supervisor가 만든"
        " QA·Synthesis 제어 Task와 Archive된 작업은 빠진다."
    ),
)
def ceo_task_list(
    limit: int = Query(default=20, ge=1, le=100, description="최대 항목 수"),
    include_archived: bool = Query(default=False, description="Archive된 작업 포함 여부"),
) -> TaskListResponse:
    try:
        rows = list_ceo_roots(limit=limit, include_archived=include_archived)
    except KanbanUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Hermes Kanban을 읽지 못했습니다: {exc}") from exc

    # 목록 Row에는 그래프(parents/children)가 없다. 상태와 부서 구성은 Root마다
    # 그래프를 읽어야 나온다. Root 간에는 병렬로, 대신 그래프 내부 병렬도는
    # 낮춰서 전체 동시 CLI 프로세스 수가 곱으로 늘지 않게 한다.
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
                lambda task_id: _load(task_id, max_workers=_LIST_GRAPH_WORKERS),
                [task_id for task_id, _ in identified],
            )
        )

    return TaskListResponse(
        items=[
            TaskListItem(
                task_id=task_id,
                query=extract_user_query(body),
                status=workflow.status,
                created_at=workflow.root.created_at,
                selected_departments=list(workflow.selected_departments),
            )
            for (task_id, body), workflow in zip(identified, workflows)
        ]
    )


@router.get(
    "/tasks/{task_id}",
    operation_id="ceo_task_status",
    response_model=TaskStatusResponse,
    summary="Task 진행 상태 조회",
    description="프론트엔드가 2~5초 주기로 polling하는 경로다.",
    responses={404: {"description": "판에 없는 Task"}},
)
def ceo_task_status(task_id: str = _TASK_ID_PATH) -> TaskStatusResponse:
    workflow = _load(task_id)
    return TaskStatusResponse(
        task_id=task_id,
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


@router.get(
    "/tasks/{task_id}/graph",
    operation_id="ceo_task_graph",
    response_model=TaskGraphResponse,
    summary="Workflow Graph 조회",
    description=(
        "CEO -> 부서 -> QA -> Synthesis 의존 그래프. `role`로 노드를 구분해서"
        " 화면 레이아웃을 만들 수 있다."
    ),
    responses={404: {"description": "판에 없는 Task"}},
)
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


@router.get(
    "/tasks/{task_id}/result",
    operation_id="ceo_task_result",
    response_model=TaskResultResponse,
    summary="최종 결과 조회",
    description=(
        "Synthesis Task의 요약을 정규화해서 준다. 진행 중이면 `result`는 null이다."
        " CEO 산출물은 `binding: false` - 주문·리스크 승인·원장 확정 근거가 아니다."
    ),
    responses={404: {"description": "판에 없는 Task"}},
)
def ceo_task_result(task_id: str = _TASK_ID_PATH) -> TaskResultResponse:
    workflow = _load(task_id)
    synthesis = workflow.synthesis_node
    terminal = workflow.status in {"completed", "blocked", "failed", "archived"}

    result: TaskResult | None = None
    if synthesis is not None and synthesis.done and synthesis.summary:
        result = TaskResult(
            summary=synthesis.summary,
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


@router.post(
    "/tasks/{task_id}/archive",
    operation_id="ceo_task_archive",
    response_model=TaskArchiveResponse,
    summary="Task Archive",
    description=(
        "Root와 하위 Task 전체를 Archive한다. Kanban 기록과 감사 추적은 그대로"
        " 남고, 기본 목록과 Dispatcher 실행 대상에서만 빠진다. 자식을 남기면"
        " Dispatcher가 계속 실행하므로 그래프 전체를 함께 처리한다."
    ),
    responses={404: {"description": "판에 없는 Task"}},
)
def ceo_task_archive(task_id: str = _TASK_ID_PATH) -> TaskArchiveResponse:
    workflow = _load(task_id)
    # 자식부터 Archive한다. Root를 먼저 닫아도 자식은 ready로 남는다.
    target_ids = [node.task_id for node in workflow.descendants if node.task_id]
    target_ids.append(workflow.root_task_id)
    try:
        archive_tasks(target_ids)
    except KanbanTaskNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Task를 찾을 수 없습니다: {task_id}") from exc
    except KanbanUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Archive에 실패했습니다: {exc}") from exc
    return TaskArchiveResponse(task_id=task_id, archived_task_ids=target_ids)


__all__ = ["router"]
