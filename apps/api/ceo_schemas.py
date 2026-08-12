#!/usr/bin/env python3
"""`/ui/ceo/*` 응답 계약. Swagger 스키마는 이 파일 하나에서만 나온다.

프론트엔드가 Hermes Kanban 내부 JSON(`task`/`runs[].metadata`/`latest_summary`)
구조를 알 필요가 없게 BFF에서 정규화한 형태다. 필드를 늘리거나 이름을 바꾸면
`schema_version`을 같이 올린다 - 화면은 이 값으로 계약 위반을 잡는다.

원칙 하나를 계약에 박아둔다: **CEO 산출물은 구속력이 없다.** `binding: false`는
선택 필드가 아니라 항상 내려간다(마스터플랜 5.6 - CEO는 주문 제출·리스크 승인·
원장 수정·NAV 확정 권한이 없다).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

WorkflowStatus = Literal["queued", "running", "blocked", "failed", "completed", "archived"]
StageStatus = Literal["todo", "running", "done", "blocked", "failed"]
NodeRole = Literal["root", "primary", "qa", "synthesis", "user_input"]


class TaskWorkflow(BaseModel):
    """CEO Planner가 실제로 고른 실행 구성."""

    selected_departments: list[str] = Field(
        default_factory=list,
        description="CEO가 선택한 부서 Hermes Profile. 생성 순서를 유지한다.",
        examples=[["research-department", "risk-management"]],
    )
    qa_required: bool = Field(
        description=(
            "QA 단계 필요 여부. QA Task가 생기면 확정 true. Synthesis까지 갔는데 "
            "QA Task가 없으면 false. 그 전에는 Supervisor 기본값 true."
        )
    )


class TaskProgress(BaseModel):
    """단계별 진행. 프론트가 2~5초 주기로 polling하는 값이다."""

    primary_total: int = Field(description="CEO가 만든 부서 분석 Task 수", ge=0)
    primary_done: int = Field(description="그중 완료된 수", ge=0)
    qa: StageStatus = Field(description="QA 단계 상태. Task가 아직 없으면 todo")
    synthesis: StageStatus = Field(description="CEO 최종 종합 단계 상태")


class TaskStatusResponse(BaseModel):
    """GET /ui/ceo/tasks/{task_id}"""

    schema_version: Literal["ceo.task-status.v1"] = "ceo.task-status.v1"
    task_id: str = Field(description="요청한 Task ID")
    root_task_id: str = Field(
        description="이 워크플로의 Root ID. 자식 ID로 물어봐도 Root 기준으로 답한다."
    )
    status: WorkflowStatus = Field(
        description="워크플로 전체 상태. Kanban Task 하나의 status가 아니다."
    )
    assignee: str = Field(description="Root Task의 Hermes Profile", examples=["ceo-agent"])
    query: str | None = Field(
        default=None, description="사용자가 처음 보낸 질의. Scope 헤더는 제거된 값이다."
    )
    created_at: str | None = Field(default=None, description="ISO 8601 UTC")
    completed_at: str | None = Field(default=None, description="ISO 8601 UTC")
    workflow: TaskWorkflow
    progress: TaskProgress


class GraphNode(BaseModel):
    id: str
    department: str = Field(
        description="Hermes Profile 이름", examples=["research-department"]
    )
    status: str = Field(description="Hermes Kanban Task status 원값", examples=["running"])
    role: NodeRole = Field(description="그래프에서의 역할. 화면 레이아웃에 쓴다.")
    title: str = Field(default="")


class TaskGraphResponse(BaseModel):
    """GET /ui/ceo/tasks/{task_id}/graph"""

    schema_version: Literal["ceo.task-graph.v1"] = "ceo.task-graph.v1"
    root: str
    nodes: list[GraphNode]
    edges: list[tuple[str, str]] = Field(
        description="(parent, child) 쌍. 그래프 안에 있는 부모만 남긴다.",
        examples=[[["t_root", "t_research"], ["t_research", "t_qa"]]],
    )


class TaskResult(BaseModel):
    """CEO 최종 종합. 참고 자료이며 구속력 있는 결정이 아니다."""

    summary: str = Field(description="Synthesis Task의 최종 요약")
    decision: str | None = Field(
        default=None,
        description=(
            "Synthesis 요약에 `decision:`/`recommendation:` 라벨로 명시된 값만 인정한다"
            " (BUY/RESIZE/HOLD/REJECT/ESCALATE/DEFER). 서술만 있으면 null."
        ),
    )
    qa_verdict: str | None = Field(default=None, description="QA 판정. 아래 최상위 값과 같다.")


class TaskResultResponse(BaseModel):
    """GET /ui/ceo/tasks/{task_id}/result"""

    schema_version: Literal["ceo.task-result.v1"] = "ceo.task-result.v1"
    task_id: str
    status: Literal["processing", "completed"] = Field(
        description="Synthesis가 끝났거나 워크플로가 종료됐으면 completed"
    )
    binding: Literal[False] = Field(
        default=False,
        description="CEO 산출물은 주문·리스크 승인·원장 확정 근거가 될 수 없다.",
    )
    result: TaskResult | None = Field(default=None, description="분석 중이면 null")
    departments: dict[str, str] = Field(
        default_factory=dict,
        description="부서 코드 -> 요약",
        examples=[{"research": "...", "risk": "...", "qa": "..."}],
    )
    qa_verdict: str | None = Field(
        default=None,
        description=(
            "PASS/WARN/FAIL 또는 QA가 결정을 막았으면 FAIL_BLOCKED_FOR_DECISION."
            " QA Task가 아직 없거나 진행 중이면 null."
        ),
    )
    block_reason: str | None = Field(default=None, description="막힌 경우의 사유")


class TaskListItem(BaseModel):
    task_id: str
    query: str | None = None
    status: WorkflowStatus
    created_at: str | None = None
    selected_departments: list[str] = Field(default_factory=list)


class TaskListResponse(BaseModel):
    """GET /ui/ceo/tasks"""

    schema_version: Literal["ceo.task-list.v1"] = "ceo.task-list.v1"
    items: list[TaskListItem]


class TaskArchiveResponse(BaseModel):
    """POST /ui/ceo/tasks/{task_id}/archive"""

    schema_version: Literal["ceo.task-archived.v1"] = "ceo.task-archived.v1"
    task_id: str
    status: Literal["archived"] = "archived"
    archived_task_ids: list[str] = Field(
        description="Root와 하위 Task 전체. 자식을 남기면 Dispatcher가 계속 실행한다."
    )


class CeoQueryAcceptedTask(BaseModel):
    task_id: str | None = None
    status: str = "ready"
    source: Literal["hermes-kanban"] = "hermes-kanban"


class CeoQueryAcceptedResponse(BaseModel):
    """POST /ui/ceo/ask"""

    schema_version: Literal["ceo.query-accepted.v1"] = "ceo.query-accepted.v1"
    department: Literal["ceo-agent"] = "ceo-agent"
    binding: Literal[False] = False
    task_id: str
    task: CeoQueryAcceptedTask
    answer: str
    session_id: str | None = None


__all__ = [
    "CeoQueryAcceptedResponse",
    "CeoQueryAcceptedTask",
    "GraphNode",
    "NodeRole",
    "StageStatus",
    "TaskArchiveResponse",
    "TaskGraphResponse",
    "TaskListItem",
    "TaskListResponse",
    "TaskProgress",
    "TaskResult",
    "TaskResultResponse",
    "TaskStatusResponse",
    "TaskWorkflow",
    "WorkflowStatus",
]
