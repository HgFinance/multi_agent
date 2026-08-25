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
from hashlib import sha256
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from orchestration.kanban_retention import (
    AuditStore,
    DiscordLedgerReader,
    SQLiteKanbanMaintenance,
    build_audit_metadata,
    default_audit_path,
    evaluate_workflow,
)

try:
    from . import hermes_boundary
    from .conditional_rule_language import looks_like_conditional_paper_rule
    from .ceo_kanban_read import (
        KanbanTaskNotFound,
        KanbanUnavailable,
        Workflow,
        extract_user_query,
        kanban_column_for_status,
        list_ceo_roots,
        list_tasks,
        load_workflow,
    )
    from .ceo_schemas import (
        GraphNode,
        KanbanBoardCard,
        KanbanBoardColumns,
        KanbanBoardResponse,
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
    from .current_user import (
        current_user,
        optional_current_user,
        require_trading_book_access,
    )
    from .governance_client import fetch_current_mandate_by_fund
    from .user_order_workflow import (
        UserOrderRequestConflict,
        UserOrderRequestRecord,
        UserOrderWorkflowUnavailable,
        user_order_repository,
    )
    from .user_order_orchestrator import process_deterministic_user_paper_order
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    import hermes_boundary  # type: ignore[no-redef]
    from conditional_rule_language import (  # type: ignore[no-redef]
        looks_like_conditional_paper_rule,
    )
    from ceo_kanban_read import (  # type: ignore[no-redef]
        KanbanTaskNotFound,
        KanbanUnavailable,
        Workflow,
        extract_user_query,
        kanban_column_for_status,
        list_ceo_roots,
        list_tasks,
        load_workflow,
    )
    from ceo_schemas import (  # type: ignore[no-redef]
        GraphNode,
        KanbanBoardCard,
        KanbanBoardColumns,
        KanbanBoardResponse,
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
    from current_user import (  # type: ignore[no-redef]
        current_user,
        optional_current_user,
        require_trading_book_access,
    )
    from governance_client import (
        fetch_current_mandate_by_fund,  # type: ignore[no-redef]
    )
    from user_order_workflow import (  # type: ignore[no-redef]
        UserOrderRequestConflict,
        UserOrderRequestRecord,
        UserOrderWorkflowUnavailable,
        user_order_repository,
    )
    from user_order_orchestrator import (  # type: ignore[no-redef]
        process_deterministic_user_paper_order,
    )

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from orchestration.canonical_profiles import (
    CANONICAL_PROFILES,
    canonical_profile_for_department,
)
from orchestration.ceo_workflow_scope import (
    UserPaperOrderScope,
    build_root_body,
    build_scoped_task_body,
    build_user_paper_order_scope,
    infer_workflow_mode,
    primary_idempotency_key,
    read_marker,
    requested_by_from_body,
    selected_primary_profiles_from_task,
)
from orchestration.user_order_language import (
    deterministic_order_candidate,
    is_clearly_non_executable_order_language,
    looks_like_user_order_request,
)
from orchestration.compound_paper_orders import (
    AnalysisThenConditionalPaperOrderPlan,
    build_compound_conditional_candidate,
    parse_analysis_then_conditional_paper_order,
    parse_compound_paper_order,
)
from orchestration.qa_contract import (
    canonical_qa_contract,
    split_planner_selection,
)
from orchestration.experience_bank import ExperienceBank

router = APIRouter(prefix="/ui/ceo", tags=["ceo-office"])
logger = logging.getLogger(__name__)


def _compound_leg_request_id(request_id: str, suffix: str) -> str:
    value = f"{request_id}:{suffix}"
    if len(value) <= 128:
        return value
    return f"compound-{sha256(value.encode('utf-8')).hexdigest()}:{suffix}"


class CeoAsk(hermes_boundary.AgentAsk):
    """`/ui/ceo/ask` 전용 Body. `AgentAsk` + `fund_id`.

    `fund_id`를 `AgentAsk`에 넣지 않은 이유: 그 모델은 부서 Agent 질의 6개가
    함께 쓰는 계약이고, 거기에 CEO 전용 필드를 넣으면 트레이딩·회계 질의에도
    쓰지 않는 필드가 노출된다.

    **왜 서버가 user_id로 fund를 찾지 않고 화면이 보내나**: `governance.fund_memberships`
    (user<->fund 연결 테이블)가 아직 비어 있어 `user_id -> fund_id` 역참조 경로가
    없다. 로컬 UI는 고정 데모 계정 단계이므로 `fund_id`도 그 쌍으로 함께 보내며,
    외부 로그인이나 계정 매핑은 이 모의투자 범위에 포함하지 않는다.
    """

    fund_id: str | None = None
    # Natural-language orders are always PAPER, but authority is still scoped
    # to one exact Book.  The server never guesses a Book when more than one is
    # available; the UI may preselect only a sole authorized Book.
    book_id: str | None = None
    # Ingress source is metadata-only observability context. It does not
    # participate in routing or execution semantics.
    source: str | None = None


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


def _user_paper_order_workflow_enabled() -> bool:
    """Require an explicit opt-in on shared production deployments.

    The legacy EB bundle intentionally has no dispatcher, Trading Hermes, or
    shared Kanban volume.  Accepting an order there would create an orphaned
    card that can never reach the verifier.  Local/test stays enabled for
    focused development; every production-capable Compose must opt in.
    """

    configured = os.getenv("USER_PAPER_ORDER_WORKFLOW_ENABLED", "").strip().casefold()
    if configured:
        return configured in {"1", "true", "yes", "on"}
    return os.getenv("APP_ENV", "development").strip().casefold() not in {
        "prod",
        "production",
        "staging",
    }


def _deterministic_paper_order_fast_path_enabled() -> bool:
    """Use the code-built interpreter for one exact, unambiguous order.

    Production defaults on because the path still traverses the same durable
    authority and OMS gates. Development and tests retain the Hermes lane
    unless explicitly enabled, which keeps local workflows inspectable.
    """

    configured = os.getenv(
        "USER_PAPER_ORDER_DETERMINISTIC_FAST_PATH_ENABLED", ""
    ).strip().casefold()
    if configured:
        return configured in {"1", "true", "yes", "on"}
    return os.getenv("APP_ENV", "development").strip().casefold() in {
        "prod",
        "production",
    }

_PLANNING_SCHEMA_VERSION = "ceo.query-accepted.v2"
_PRIMARY_PROFILE_ORDER = (
    "research-liaison",
    "quant-liaison",
    "research-department",
    "quant-backtest-department",
    "trading-department",
    "accounting-portfolio-department",
    "risk-management",
    "hr-department",
)
_PROFILE_COPY = {
    "research-liaison": "저장된 연구 결과와 수집 상태를 조회",
    "quant-liaison": "저장된 실험 결과와 승격 게이트 상태를 조회",
    "research-department": "최신 공시·뉴스·산업 근거를 수집",
    "quant-backtest-department": "정량 검증과 전략 후보를 평가",
    "trading-department": "실행 가능성과 주문 경로를 검토",
    "accounting-portfolio-department": "포트폴리오·NAV 영향을 검토",
    "risk-management": "사업·규제·시장 리스크를 검토",
    "hr-department": "인력·역할·역량을 검토",
}
_PROFILE_LABEL = {
    "research-liaison": "Research 조회",
    "quant-liaison": "Quant 조회",
    "research-department": "Research",
    "quant-backtest-department": "Quant",
    "trading-department": "Trading",
    "accounting-portfolio-department": "Accounting/Portfolio",
    "risk-management": "Risk",
    "hr-department": "HR",
}
_PROFILE_ALIASES = {
    "research-liaison": ("research-liaison", "리서치 조회", "research reference desk"),
    "quant-liaison": ("quant-liaison", "퀀트 조회", "quant reference desk"),
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


def _require_ceo_task_owner(body: str, authenticated_owner_id: str | None) -> None:
    """Prevent one local fixture identity from reading another task graph."""

    # The supported local mode may intentionally have no identity. There is no
    # browser JWT mode in this repository.
    # Direct unit/domain calls see FastAPI's ``Depends`` sentinel rather than a
    # resolved request identity.  Only a concrete string is an authenticated
    # subject; HTTP requests always pass the dependency-resolved value.
    if not isinstance(authenticated_owner_id, str):
        return
    if requested_by_from_body(body) != authenticated_owner_id:
        raise HTTPException(status_code=403, detail="ceo_task_forbidden")


def _require_ceo_workflow_owner(
    workflow: Workflow, authenticated_owner_id: str | None
) -> None:
    _require_ceo_task_owner(workflow.root.body, authenticated_owner_id)


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
    raw_declared_primary = selected_primary_profiles_from_task(task)
    declared_primary, planner_qa_requested = split_planner_selection(
        raw_declared_primary
    )
    if declared_primary:
        selected.extend(declared_primary)
    qa_required = planner_qa_requested
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

    # A run summary is an outcome/prose field, not a durable execution plan.
    # Inferring a department from it made a root with only
    # ``selected_primary_profiles`` in run metadata look delegated even when
    # materialization created no child (t_71e0df48).  Keep this function
    # limited to explicit planning metadata and materialized child markers.
    return selected, qa_required, synthesis_present


def _materialized_planning_profiles(
    task: Mapping[str, object],
) -> tuple[list[str], bool, bool]:
    """Return departments represented by actual root-scoped child tasks.

    Planned/selected metadata is intentionally not used here.  The response
    plane must not tell a user that a department was delegated until a durable
    child with the corresponding workflow role exists in this root projection.
    """

    selected: list[str] = []
    qa_present = False
    synthesis_present = False
    for child in _child_records(task.get("children")):
        assignee = str(child.get("assignee") or child.get("profile") or "").strip()
        if assignee not in CANONICAL_PROFILES:
            continue
        body = str(child.get("body") or "")
        role_match = re.search(r"(?:^|\n)workflow_role=(\w+)", body)
        role = role_match.group(1).casefold() if role_match else ""
        if assignee == "qa-department" and role == "qa":
            qa_present = True
        elif assignee == "ceo-agent" and role == "synthesis":
            synthesis_present = True
        elif assignee in _PRIMARY_PROFILE_ORDER and role in {"", "primary"}:
            if assignee not in selected:
                selected.append(assignee)
    return selected, qa_present, synthesis_present


def _qa_materialization_facts(
    task: Mapping[str, object],
) -> tuple[bool, bool]:
    """Return canonical QA-role presence and legacy QA-primary presence."""

    canonical = False
    legacy_primary = False
    for child in _child_records(task.get("children")):
        assignee = str(child.get("assignee") or child.get("profile") or "").strip()
        body = str(child.get("body") or "")
        role_match = re.search(r"(?:^|\n)workflow_role=(\w+)", body)
        role = role_match.group(1).casefold() if role_match else ""
        if assignee == "qa-department" and role == "qa":
            canonical = True
        elif assignee == "qa-department" and role == "primary":
            legacy_primary = True
    return canonical, legacy_primary


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
    planned, planned_qa, planned_synthesis = _planning_profiles(task)
    selected, qa_required, synthesis_present = _materialized_planning_profiles(task)
    qa_materialized, qa_legacy_primary_present = _qa_materialization_facts(task)
    planned_departments = list(planned)
    if planned_qa and "qa-department" not in planned_departments:
        # QA is a governance lane, not an analysis primary.  It still belongs
        # in the durable *planned* set; ``selected_departments`` remains the
        # materialized primary set so the UI never claims an uncreated child.
        planned_departments.append("qa-department")
    body = str(task.get("body") or "")
    workflow_mode = (
        "binding"
        if re.search(r"(?:^|\n)workflow_mode=binding(?:\n|$)", body.casefold())
        else "analysis"
    )
    qa_contract = canonical_qa_contract(
        workflow_mode=workflow_mode,
        body=body,
        metadata=task.get("metadata") if isinstance(task.get("metadata"), Mapping) else None,
        planner_qa_requested=planned_qa,
    )
    actions = [f"{_PROFILE_LABEL[p]}에서 {_PROFILE_COPY[p]}" for p in selected]
    if actions:
        answer = f"{'· '.join(actions)}하겠습니다."
    elif planned or planned_qa or planned_synthesis:
        answer = (
            "선택된 부서 작업이 아직 실행 가능한 상태로 생성되지 않아 "
            "CEO가 직접 확인 중입니다."
        )
    else:
        answer = "CEO workflow를 접수했습니다. 실제 planning 결과가 준비되면 선택된 부서와 다음 단계를 표시하겠습니다."
    if synthesis_present:
        answer += " CEO가 최종 종합합니다."
    steps = [_PROFILE_LABEL[p] for p in selected]
    binding = workflow_mode == "binding"
    if binding:
        if qa_required:
            steps.append("QA (blocking gate)")
        if synthesis_present:
            steps.append("CEO Synthesis")
    else:
        if synthesis_present:
            steps.append("CEO Synthesis")
        if qa_required:
            steps.append("QA (async evaluation)")
    materialized = bool(selected or qa_required or synthesis_present)
    return {
        "status": "planned" if materialized else "accepted",
        "planning": {
            "selected_departments": selected,
            "planned_departments": planned_departments,
            "materialized_departments": selected,
            "steps": steps,
            "qa_required": qa_required,
            "planned_qa_required": planned_qa,
            "qa_enabled": qa_contract.qa_enabled,
            "qa_blocks_response": qa_contract.qa_blocks_response,
            "qa_materialized": qa_materialized,
            "qa_legacy_primary_present": qa_legacy_primary_present,
            "planned_synthesis": planned_synthesis,
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


def _paper_order_accepted_response(
    *,
    root_task: Mapping[str, object],
    trading_task: Mapping[str, object],
    order_request_id: str,
    conditional_rule: bool = False,
) -> dict[str, object]:
    """Return an explicit asynchronous receipt without claiming an execution."""

    answer = (
        "조건주문 요청을 Trading Hermes에 직접 배정했습니다. Hermes가 원문을 "
        "AST로 구조화하고 deterministic 검증을 통과하면 PAPER 규칙이 즉시 "
        "활성화됩니다. 별도 Risk/QA/Research 부서 승인은 거치지 않습니다."
        if conditional_rule
        else (
            "주문 요청을 Trading Hermes에 배정했습니다. Hermes가 원문을 구조화한 뒤 "
            "서버 검증을 통과한 요청만 PAPER OMS로 제출합니다. 이 접수 응답 자체는 "
            "체결 완료를 의미하지 않습니다."
        )
    )

    return {
        "schema_version": _PLANNING_SCHEMA_VERSION,
        "department": "ceo-agent",
        "binding": False,
        "task_id": str(root_task.get("task_id") or root_task.get("id") or ""),
        "task": dict(root_task),
        "status": "planned",
        "answer": answer,
        "planning": {
            "schema_version": "ceo.planning.v1",
            "selected_departments": ["trading-department"],
            "steps": (
                ["Trading Hermes AST interpretation", "Immediate PAPER rule activation"]
                if conditional_rule
                else ["Trading Hermes interpretation", "PAPER OMS validation"]
            ),
            "qa_required": False,
            "summary": (
                "Trading Hermes가 조건주문 AST를 해석하고 있습니다."
                if conditional_rule
                else "Trading Hermes가 PAPER 주문 원문을 해석하고 있습니다."
            ),
        },
        "session_id": None,
        "order_request_id": order_request_id,
        "order_state": "RULE_INTERPRETATION_QUEUED" if conditional_rule else "KANBAN_QUEUED",
        "order_mode": "PAPER",
        "conditional_rule": conditional_rule,
        "trading_task_id": str(
            trading_task.get("task_id") or trading_task.get("id") or ""
        ),
    }


def _paper_order_execution_response(
    *,
    root_task: Mapping[str, object],
    trading_task: Mapping[str, object],
    result: Mapping[str, object],
) -> dict[str, object]:
    """Return the exact result produced by the trusted PAPER boundary."""

    answer = str(result.get("user_message") or "").strip()
    if not answer:
        reasons = result.get("reason_codes")
        rendered_reasons = (
            ", ".join(str(item) for item in reasons)
            if isinstance(reasons, Sequence) and not isinstance(reasons, str)
            else "PAPER_ORDER_NOT_SUBMITTED"
        )
        answer = (
            "PAPER 주문을 제출하지 않았습니다. "
            f"상태={result.get('request_state') or result.get('decision') or 'UNKNOWN'}, "
            f"사유={rendered_reasons}. 자동 재시도하지 않습니다."
        )
    return {
        "schema_version": _PLANNING_SCHEMA_VERSION,
        "department": "ceo-agent",
        # The CEO wrapper remains non-authoritative; the nested trusted
        # execution result carries its own binding flag and durable IDs.
        "binding": False,
        "task_id": str(root_task.get("task_id") or root_task.get("id") or ""),
        "task": dict(root_task),
        "status": "planned",
        "answer": answer,
        "planning": {
            "schema_version": "ceo.planning.v1",
            "selected_departments": ["trading-department"],
            "steps": ["Deterministic source verification", "PAPER OMS submission and tracking"],
            "qa_required": False,
            "summary": answer,
        },
        "session_id": None,
        "order_request_id": str(result.get("order_request_id") or ""),
        "order_state": str(result.get("request_state") or "UNKNOWN"),
        "order_mode": "PAPER",
        "trading_task_id": str(
            trading_task.get("task_id") or trading_task.get("id") or ""
        ),
        "execution": dict(result),
    }


def _paper_order_child_body(
    *,
    query: str,
    scope: UserPaperOrderScope,
    root_task_id: str,
    request_id: str,
    has_mandate: bool,
) -> str:
    interpretation_prompt = "\n".join(
        (
            "hgfinance.user-paper-order-interpretation.v1",
            build_user_paper_order_scope(scope),
            "authority=interpretation_only",
            "execution_mode=PAPER_ONLY",
            "mcp_tool=process_user_paper_order",
            "Interpret the exact user instruction below into the strict tool schema.",
            "Call process_user_paper_order exactly once with this root/task scope.",
            "Never invent a symbol, quantity, side, price, explicit order-type evidence, Fund, or Book.",
            "Every evidence item must include normalized. For INSTRUMENT evidence,",
            "normalized must exactly equal instrument_mention without guessing a symbol.",
            "If the tool result includes user_message, the user-facing final_answer",
            "must contain only that exact string, copied verbatim. Never describe a rejected or non-binding",
            "result as pending review, submitted, filled, or ledger-posted.",
            "For one otherwise complete PAPER PLACE_ORDER with no price and no explicit",
            "market/limit marker, apply the managed omission default: order_type=MARKET,",
            "limit_price=null, and no ORDER_TYPE evidence. A limit marker without exactly",
            "one valid price, or conflicting market/limit language, must CLARIFY.",
            "Questions, examples, negations, conditions, ambiguity, multiple commands,",
            "and any LIVE/real-account request must not execute.",
            "The tool result, not your interpretation, is the execution authority.",
            "",
            "## Exact user instruction",
            query,
        )
    )
    return build_scoped_task_body(
        interpretation_prompt,
        root_task_id,
        role="primary",
        request_id=request_id,
        workflow_mode="binding",
        has_mandate=has_mandate,
    )


def _deterministic_order_child_body(
    *,
    query: str,
    scope: UserPaperOrderScope,
    root_task_id: str,
    request_id: str,
    has_mandate: bool,
) -> str:
    """Describe the non-LLM fast lane on the same durable Trading card."""

    body = "\n".join(
        (
            "hgfinance.user-paper-order-deterministic.v1",
            build_user_paper_order_scope(scope),
            "authority=server_verified_paper_only",
            "execution_mode=PAPER_ONLY",
            "interpreter=DETERMINISTIC_EXACT_EVIDENCE",
            "No Risk/QA/Research approval is required for this direct user order.",
            "The trusted server still rechecks the exact text, fixed Fund/Book fixture,",
            "instrument resolution, idempotency, OMS state, and broker execution evidence.",
            "",
            "## Exact user instruction",
            query,
        )
    )
    return build_scoped_task_body(
        body,
        root_task_id,
        role="primary",
        request_id=request_id,
        workflow_mode="binding",
        has_mandate=has_mandate,
    )


def _conditional_rule_indicator_catalog_prompt() -> str:
    """Build the prompt vocabulary from the registry, never from a static list."""

    from orchestration.conditional_rules import list_supported_indicators

    return ", ".join(item["name"] for item in list_supported_indicators())


def _conditional_rule_child_body(
    *,
    query: str,
    scope: UserPaperOrderScope,
    root_task_id: str,
    request_id: str,
    has_mandate: bool,
) -> str:
    interpretation_prompt = "\n".join(
        (
            "hgfinance.user-conditional-paper-rule.v1",
            build_user_paper_order_scope(scope),
            "authority=interpretation_only",
            "execution_mode=PAPER_ONLY",
            "activation_policy=IMMEDIATE_AFTER_DETERMINISTIC_VALIDATION",
            "mcp_tool=process_user_conditional_paper_rule",
            "Interpret the exact user instruction below into one or more ConditionalRuleCandidate ASTs.",
            "Call process_user_conditional_paper_rule exactly once with this root/task scope.",
            "Do not create Risk, QA, Research, Accounting, or additional Trading tasks.",
            "Never invent a symbol, condition, threshold, timeframe, side, or sizing value.",
            "Questions, advice, negation, examples, ambiguity, and LIVE requests must",
            "use candidate=null and candidates=null with a concise clarification_reason.",
            "For one action, pass candidate. For 2-4 independent conditional actions,",
            "pass candidates in source-text order and leave candidate=null. A leading",
            "symbol shared by coordinated clauses applies to each clause; never invent",
            "a different symbol. Each candidate must preserve its own comparator,",
            "threshold, side, and sizing. Never collapse branches with different actions",
            "into one LOGICAL OR rule.",
            "Supported expression node types are LITERAL, MARKET, PORTFOLIO, INDICATOR,",
            "ARITHMETIC, COMPARISON, LOGICAL, NOT, and CROSS.",
            f"Supported indicators are {_conditional_rule_indicator_catalog_prompt()}.",
            "Node field ownership is strict: MARKET uses only type+field; LITERAL uses",
            "only type+value+unit; INDICATOR uses type+name+timeframe and optional",
            "output/parameters. Never put unit on MARKET or INDICATOR. Price literals",
            "use unit=PRICE, including values written in Korean as 원.",
            "Before the single tool call, check every recursive node and remove fields",
            "that do not belong to that node type. Do not use the tool call as validation.",
            "Canonical quote-price example: condition={type:COMPARISON,operator:GTE,",
            "left:{type:MARKET,field:LAST_PRICE},right:{type:LITERAL,value:70000,unit:PRICE}},",
            "evaluation={clock:QUOTE}.",
            "Canonical daily-SMA example: condition={type:COMPARISON,operator:GT,",
            "left:{type:MARKET,field:CLOSE},right:{type:INDICATOR,name:SMA,timeframe:1D,",
            "parameters:{PERIOD:5}}}, evaluation={clock:BAR_CLOSE,primary_timeframe:1D}.",
            "Canonical Bollinger upper example uses name=BOLLINGER, output=UPPER,",
            "timeframe=1D, parameters={PERIOD:20,STDDEV:2} and compares MARKET CLOSE.",
            "Use comparison operators GT/GTE/LT/LTE/EQ, cross ABOVE/BELOW, logical",
            "AND/OR, and only 1M/5M/15M/1H/1D executable timeframes. 3분봉",
            "is not a supported market-data capability: do not derive it from 1M",
            "candles. For an explicit 3분봉 request, preserve the condition and",
            "use the existing 5M feed; the trusted boundary records",
            "TIMEFRAME_FALLBACK_3M_TO_5M and tells the user that 5분봉 was used.",
            "For 3분봉 60일선 or N선/N일선, use PERIOD=N on the requested chart",
            "timeframe before the boundary fallback; CROSS ABOVE compares completed",
            "MARKET CLOSE against INDICATOR SMA. If the buy rule omits a quantity,",
            "return candidate=null with clarification_reason=QUANTITY_REQUIRED;",
            "never assume 1주.",
            "If an indicator timeframe is omitted, use 1D completed bars and BAR_CLOSE; never",
            "default an explicit intraday phrase to another timeframe. Portfolio/last-price-only rules use",
            "QUOTE. POSITION_PERCENT is a ratio in (0,1], FIXED_SHARES is an integer,",
            "and ALL is sell-only. Omit expires_at to use the trusted 10-minute default.",
            "Use the trusted max_data_age_seconds=30 default; never reduce it unless the user explicitly asks.",
            "CROSS always requires BAR_CLOSE and an explicit primary_timeframe; when the",
            "instruction gives no timeframe for a price-only cross, return candidate=null",
            "with clarification_reason=TIMEFRAME_REQUIRED_FOR_CROSS instead of guessing.",
            "For an explicit 지정가/limit order, preserve order_type=LIMIT and the exact user-provided limit_price; never calculate or invent a price. Without explicit limit evidence, use the default MARKET action.",
            "The trusted tool resolves the symbol, validates authority, units, semantics,",
            "idempotency, and activates the exact rule. Do not claim ACTIVE unless the",
            "tool result reports rule_active=true. Copy user_message verbatim.",
            "",
            "## Exact user instruction",
            query,
        )
    )
    return build_scoped_task_body(
        interpretation_prompt,
        root_task_id,
        role="primary",
        request_id=request_id,
        workflow_mode="binding",
        has_mandate=has_mandate,
    )


def _mark_paper_order_failed(
    repository: object,
    order_request_id: str,
    *,
    error_code: str,
    error_message: str,
) -> None:
    """Best-effort durable failure note; never hide the original boundary error."""

    try:
        repository.mark_outcome(  # type: ignore[attr-defined]
            order_request_id,
            state="FAILED",
            error_code=error_code,
            error_message=error_message[:1000],
        )
    except Exception:  # noqa: BLE001 - failure recording cannot grant authority.
        logger.exception(
            "paper-order failure record unavailable request=%s code=%s",
            order_request_id,
            error_code,
        )


def _route_analysis_then_conditional_paper_order(
    req: CeoAsk,
    *,
    plan: AnalysisThenConditionalPaperOrderPlan,
    owner_id: str | None,
    mandate: Mapping[str, object] | None,
    discord_channel_id: str | None,
    discord_message_id: str | None,
    discord_guild_id: str | None,
    discord_thread_id: str | None,
) -> dict[str, object]:
    """Run Research first, then hand the existing condition lane to Trading.

    This route is deliberately a composition of the existing user-order
    authority and CEO/Supervisor workflow.  It does not create a second order
    ledger or a conditional rule early: the supervisor binds the existing
    order request to a Trading card only after the Research primary completes.
    """

    if not isinstance(owner_id, str) or not owner_id.strip():
        raise HTTPException(status_code=401, detail="portfolio_authentication_required")
    if not req.fund_id:
        raise HTTPException(status_code=422, detail="portfolio_fund_id_required")
    if not req.book_id:
        raise HTTPException(status_code=422, detail="portfolio_book_id_required")
    if not _user_paper_order_workflow_enabled():
        raise HTTPException(status_code=503, detail="paper_order_hermes_runtime_unavailable")

    access = require_trading_book_access(owner_id, req.fund_id, req.book_id)
    repository = user_order_repository()
    try:
        record = repository.admit(
            user_id=access["user_id"],
            fund_id=access["fund_id"],
            book_id=access["book_id"],
            client_request_id=req.request_id,
            raw_instruction=req.query,
        )
    except UserOrderRequestConflict as exc:
        raise HTTPException(status_code=409, detail="paper_order_request_id_conflict") from exc
    except UserOrderWorkflowUnavailable as exc:
        raise HTTPException(status_code=503, detail="paper_order_workflow_unavailable") from exc

    if record.ceo_root_task_id:
        existing_root = hermes_boundary.show_kanban_task(record.ceo_root_task_id)
        if existing_root is None:
            raise HTTPException(status_code=503, detail="paper_order_kanban_unavailable")
        existing_body = str(existing_root.get("body") or "")
        if (
            read_marker(existing_body, "deferred_conditional") == "true"
            and read_marker(existing_body, "deferred_conditional_order_request_id")
            == record.order_request_id
        ):
            return _analysis_then_conditional_accepted_response(
                existing_root,
                _wait_for_planning(record.ceo_root_task_id),
                record=record,
                plan=plan,
            )
        # The request id is already bound to a different workflow shape. Never
        # relabel that historical root or create a second order workflow.
        raise HTTPException(status_code=409, detail="paper_order_request_already_bound")

    scope = UserPaperOrderScope(
        order_request_id=record.order_request_id,
        raw_instruction_sha256=record.raw_instruction_sha256,
        fund_id=record.fund_id,
        book_id=record.book_id,
    )
    root_body = build_root_body(
        plan.analysis_instruction,
        req.request_id,
        workflow_mode="analysis",
        mandate=mandate,
        requested_by=access["user_id"],
        user_paper_order_scope=scope,
        user_paper_order_include_primary_selection=False,
        deferred_conditional_analysis=True,
        discord_channel_id=discord_channel_id,
        discord_message_id=discord_message_id,
        discord_guild_id=discord_guild_id,
        discord_thread_id=discord_thread_id,
        advisory_fund_id=access["fund_id"],
        advisory_book_id=access["book_id"],
    )
    root_body = "\n".join(
        (
            root_body,
            "hgfinance.analysis-then-conditional-paper.v1",
            "deferred_conditional=true",
            f"deferred_conditional_order_request_id={record.order_request_id}",
            "deferred_conditional_required_profile=research-department",
            "deferred_conditional_policy=AFTER_RESEARCH_PRIMARY_COMPLETED",
            f"deferred_conditional_instruction_sha256={sha256(plan.conditional_instruction.encode('utf-8')).hexdigest()}",
        )
    )
    try:
        root = hermes_boundary.create_kanban_task(
            assignee=canonical_profile_for_department("ceo"),
            title=f"사용자 질의: {plan.analysis_instruction[:120]}",
            body=root_body,
            idempotency_key=req.request_id,
        )
        if not root or not root.get("task_id"):
            raise HTTPException(status_code=503, detail="paper_order_kanban_unavailable")
        root_task_id = str(root["task_id"])
        if not hermes_boundary.comment_root_scope(
            task_id=root_task_id, request_id=req.request_id
        ):
            raise HTTPException(status_code=503, detail="paper_order_kanban_unavailable")
        record = repository.bind_root(record.order_request_id, root_task_id)
    except HTTPException:
        _mark_paper_order_failed(
            repository,
            record.order_request_id,
            error_code="ANALYSIS_ROOT_CREATE_FAILED",
            error_message="analysis prerequisite root could not be created",
        )
        raise
    except (UserOrderRequestConflict, UserOrderWorkflowUnavailable) as exc:
        _mark_paper_order_failed(
            repository,
            record.order_request_id,
            error_code="ANALYSIS_ROOT_BIND_FAILED",
            error_message=str(exc),
        )
        raise HTTPException(status_code=503, detail="paper_order_workflow_unavailable") from exc
    except Exception as exc:  # noqa: BLE001 - preserve the API boundary contract.
        _mark_paper_order_failed(
            repository,
            record.order_request_id,
            error_code="ANALYSIS_ROOT_CREATE_FAILED",
            error_message=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="paper_order_kanban_unavailable") from exc

    return _analysis_then_conditional_accepted_response(
        root,
        _wait_for_planning(root_task_id),
        record=record,
        plan=plan,
    )


def _analysis_then_conditional_accepted_response(
    root: Mapping[str, object],
    planning: Mapping[str, object],
    *,
    record: UserOrderRequestRecord,
    plan: AnalysisThenConditionalPaperOrderPlan,
) -> dict[str, object]:
    response = _accepted_response(root, planning)
    response.update(
        {
            "answer": (
                "Research 분석을 먼저 진행합니다. Research가 정상 완료된 뒤에만 "
                "기존 Trading 조건주문 경로가 다음 조건을 해석·검증합니다: "
                f"{plan.conditional_instruction}. 분석 전에 조건주문을 활성화하지 않습니다."
            ),
            "order_request_id": record.order_request_id,
            "order_state": record.state,
            "order_mode": "PAPER",
            "conditional_rule": True,
            "analysis_then_conditional": True,
            "conditional_rule_activation": "AFTER_RESEARCH_PRIMARY_COMPLETED",
        }
    )
    return response


def _route_compound_user_paper_order(
    req: CeoAsk,
    *,
    owner_id: str | None,
    mandate: Mapping[str, object] | None,
) -> dict[str, object]:
    """Compose existing PAPER order and conditional-rule authorities.

    The rule is created pending and is activated only by the existing
    conditional-rule worker after the immediate request reaches COMPLETED.
    No second order executor is introduced here.
    """

    plan = parse_compound_paper_order(req.query)
    if plan is None:
        raise HTTPException(status_code=422, detail="compound_paper_order_unsupported")
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise HTTPException(status_code=401, detail="portfolio_authentication_required")
    if not req.fund_id:
        raise HTTPException(status_code=422, detail="portfolio_fund_id_required")
    if not req.book_id:
        raise HTTPException(status_code=422, detail="portfolio_book_id_required")
    if not _user_paper_order_workflow_enabled():
        raise HTTPException(status_code=503, detail="paper_order_hermes_runtime_unavailable")

    # These imports stay local so the normal CEO analysis path does not acquire
    # conditional-rule dependencies when it is not an order request.
    from .conditional_rules import (  # noqa: PLC0415
        ConditionalRuleCandidate,
        ConditionalRulePreviewRequest,
        _build_preview,
    )
    from .conditional_rule_workflow import (  # noqa: PLC0415
        ConditionalRuleConflict,
        ConditionalRuleUnavailable,
        conditional_rule_repository,
    )
    from orchestration.conditional_rules import RuleState  # noqa: PLC0415
    from .paper_order_bundle import (  # noqa: PLC0415
        PaperOrderBundleError,
        paper_order_bundle_repository,
    )

    access = require_trading_book_access(owner_id, req.fund_id, req.book_id)
    immediate_request_id = _compound_leg_request_id(req.request_id, "buy")
    conditional_request_id = _compound_leg_request_id(req.request_id, "sell-rule")
    orders = user_order_repository()
    bundles = None
    rule = None
    try:
        immediate_record = orders.admit(
            user_id=access["user_id"],
            fund_id=access["fund_id"],
            book_id=access["book_id"],
            client_request_id=immediate_request_id,
            raw_instruction=plan.immediate_instruction,
        )
        bundles = paper_order_bundle_repository()
        bundle = bundles.create(
            user_id=access["user_id"],
            fund_id=access["fund_id"],
            book_id=access["book_id"],
            client_request_id=req.request_id,
            raw_instruction=req.query,
            immediate_order_request_id=immediate_record.order_request_id,
            required_quantity=plan.immediate_quantity,
        )
        if bundle.conditional_rule_id is not None or bundle.state != "RECEIVED":
            # The parent request is the idempotency boundary.  A replay must
            # never cancel or rebuild an already-bound rule.
            return {
                "schema_version": _PLANNING_SCHEMA_VERSION,
                "department": "ceo-agent",
                "binding": False,
                "status": "planned",
                "answer": "동일한 compound PAPER 요청이 이미 접수되어 기존 상태를 반환합니다.",
                "planning": {
                    "schema_version": "ceo.planning.v1",
                    "selected_departments": ["trading-department"],
                    "steps": ["Replay existing compound PAPER bundle"],
                    "qa_required": False,
                    "summary": "기존 bundle을 재사용했습니다.",
                },
                "session_id": None,
                "order_request_id": immediate_record.order_request_id,
                "order_state": bundle.state,
                "order_mode": "PAPER",
                "compound_paper_order": True,
                "bundle_id": bundle.bundle_id,
                "conditional_rule_id": bundle.conditional_rule_id,
            }
        candidate = ConditionalRuleCandidate.model_validate(
            build_compound_conditional_candidate(plan)
        )
        preview = _build_preview(
            ConditionalRulePreviewRequest(
                fund_id=access["fund_id"],
                book_id=access["book_id"],
                raw_instruction=plan.conditional_instruction,
                candidate=candidate,
            ),
            subject=access["user_id"],
        )
        if not preview.activatable:
            raise ConditionalRuleConflict(
                "compound conditional rule requires clarification: "
                + ",".join(preview.clarification_codes)
            )
        rule = conditional_rule_repository().create_pending(
            spec=preview.spec,
            raw_instruction=plan.conditional_instruction,
            client_request_id=conditional_request_id,
            parser_source="DETERMINISTIC",
        )
        bundle = bundles.bind_conditional_rule(bundle.bundle_id, rule.rule_id)
    except (
        UserOrderRequestConflict,
        UserOrderWorkflowUnavailable,
        PaperOrderBundleError,
        ConditionalRuleConflict,
        ConditionalRuleUnavailable,
        HTTPException,
    ) as exc:
        if bundles is not None and "bundle" in locals():
            try:
                bundles.mark_failed(
                    bundle.bundle_id,
                    code="COMPOUND_ADMISSION_FAILED",
                    message=type(exc).__name__,
                )
            except Exception:  # noqa: BLE001 - preserve the admission error.
                logger.exception("compound PAPER admission cleanup failed")
        if rule is not None:
            try:
                conditional_rule_repository().transition(
                    rule.rule_id,
                    user_id=access["user_id"],
                    target=RuleState.CANCELLED,
                )
            except Exception:  # noqa: BLE001 - preserve the admission error.
                logger.exception("compound PAPER rule cleanup failed")
        if "immediate_record" in locals():
            try:
                orders.mark_outcome(
                    immediate_record.order_request_id,
                    state="FAILED",
                    error_code="COMPOUND_ADMISSION_FAILED",
                    error_message="compound PAPER bundle was not fully admitted",
                )
            except Exception:  # noqa: BLE001 - preserve the admission error.
                logger.exception("compound PAPER order cleanup failed")
        raise HTTPException(
            status_code=409 if isinstance(exc, UserOrderRequestConflict) else 503,
            detail="compound_paper_order_admission_failed",
        ) from exc

    immediate_request = req.model_copy(
        update={
            "query": plan.immediate_instruction,
            "request_id": immediate_request_id,
        }
    )
    try:
        immediate_response = _route_user_paper_order(
            immediate_request,
            owner_id=owner_id,
            mandate=mandate,
            pre_admitted_record=immediate_record,
        )
    except Exception as exc:  # noqa: BLE001 - close the pending composition.
        try:
            bundles.mark_failed(
                bundle.bundle_id,
                code="IMMEDIATE_ORDER_ROUTING_FAILED",
                message=type(exc).__name__,
            )
            conditional_rule_repository().transition(
                rule.rule_id,
                user_id=access["user_id"],
                target=RuleState.CANCELLED,
            )
        except Exception:  # noqa: BLE001 - original failure remains authoritative.
            logger.exception("compound PAPER cleanup failed bundle=%s", bundle.bundle_id)
        raise

    return {
        "schema_version": _PLANNING_SCHEMA_VERSION,
        "department": "ceo-agent",
        "binding": False,
        "task_id": str(immediate_response.get("task_id") or ""),
        "task": immediate_response.get("task") or {},
        "status": "planned",
        "answer": (
            "PAPER 매수 주문을 기존 Trading 경로로 접수했습니다. 매수 수량이 전량 "
            "체결된 뒤 기존 조건주문 worker가 265,000원 초과 시 매도 규칙을 "
            "자동 활성화합니다. 부분체결·실패 시 조건주문은 활성화하지 않습니다."
        ),
        "planning": {
            "schema_version": "ceo.planning.v1",
            "selected_departments": ["trading-department"],
            "steps": [
                "Existing PAPER buy directive",
                "Wait for full fill",
                "Activate existing conditional rule",
            ],
            "qa_required": False,
            "summary": "기존 PAPER 주문과 조건주문 경로를 하나의 durable bundle로 연결했습니다.",
        },
        "session_id": None,
        "order_request_id": immediate_record.order_request_id,
        "order_state": bundle.state,
        "order_mode": "PAPER",
        "compound_paper_order": True,
        "bundle_id": bundle.bundle_id,
        "conditional_rule_id": rule.rule_id,
        "trading_task_id": immediate_response.get("trading_task_id"),
    }


def _route_user_paper_order(
    req: CeoAsk,
    *,
    owner_id: str | None,
    mandate: Mapping[str, object] | None,
    conditional_rule: bool = False,
    pre_admitted_record: UserOrderRequestRecord | None = None,
    langsmith_trace_context: str | None = None,
) -> dict[str, object]:
    """Durably route one direct user workflow to Trading Hermes, always PAPER."""

    route_started_at = time.monotonic()

    if not isinstance(owner_id, str) or not owner_id.strip():
        raise HTTPException(status_code=401, detail="portfolio_authentication_required")
    if not req.fund_id:
        raise HTTPException(status_code=422, detail="portfolio_fund_id_required")
    if not req.book_id:
        raise HTTPException(status_code=422, detail="portfolio_book_id_required")
    if not _user_paper_order_workflow_enabled():
        raise HTTPException(
            status_code=503, detail="paper_order_hermes_runtime_unavailable"
        )

    deterministic_candidate = None
    if not conditional_rule and _deterministic_paper_order_fast_path_enabled():
        deterministic_candidate = deterministic_order_candidate(req.query)

    # This is admission, not execution. It canonicalizes the fixed local
    # user/Fund/Book tuple before anything is exposed to Hermes.
    access = require_trading_book_access(owner_id, req.fund_id, req.book_id)
    repository = user_order_repository()
    if pre_admitted_record is None:
        try:
            record = repository.admit(
                user_id=access["user_id"],
                fund_id=access["fund_id"],
                book_id=access["book_id"],
                client_request_id=req.request_id,
                raw_instruction=req.query,
            )
        except UserOrderRequestConflict as exc:
            raise HTTPException(
                status_code=409, detail="paper_order_request_id_conflict"
            ) from exc
        except UserOrderWorkflowUnavailable as exc:
            raise HTTPException(
                status_code=503, detail="paper_order_workflow_unavailable"
            ) from exc
    else:
        record = pre_admitted_record
        if (
            record.client_request_id != req.request_id
            or record.raw_instruction != req.query
            or record.user_id != str(access["user_id"])
            or record.fund_id != str(access["fund_id"])
            or record.book_id != str(access["book_id"])
        ):
            raise HTTPException(status_code=409, detail="paper_order_admission_mismatch")
    admitted_at = time.monotonic()

    scope = UserPaperOrderScope(
        order_request_id=record.order_request_id,
        raw_instruction_sha256=record.raw_instruction_sha256,
        fund_id=record.fund_id,
        book_id=record.book_id,
    )
    root = hermes_boundary.create_kanban_task(
        assignee=canonical_profile_for_department("ceo"),
        title=(
            f"사용자 PAPER 조건주문: {req.query[:100]}"
            if conditional_rule
            else f"사용자 PAPER 주문: {req.query[:100]}"
        ),
        body=build_root_body(
            req.query,
            req.request_id,
            workflow_mode="binding",
            mandate=mandate,
            requested_by=access["user_id"],
            user_paper_order_scope=scope,
            langsmith_trace_context=langsmith_trace_context,
        ),
        idempotency_key=req.request_id,
        # A running scope container cannot be claimed by the CEO dispatcher,
        # and Hermes supports completing it without a worker run. Trading
        # remains blocked until every SQL/Kanban binding is durable.
        initial_status="running",
    )
    if not root or not root.get("task_id"):
        _mark_paper_order_failed(
            repository,
            record.order_request_id,
            error_code="CEO_ROOT_CREATE_FAILED",
            error_message="CEO root Kanban task could not be created",
        )
        raise HTTPException(status_code=503, detail="paper_order_kanban_unavailable")
    root_task_id = str(root["task_id"])

    if not hermes_boundary.comment_root_scope(
        task_id=root_task_id, request_id=req.request_id
    ):
        _mark_paper_order_failed(
            repository,
            record.order_request_id,
            error_code="CEO_ROOT_SCOPE_FAILED",
            error_message="CEO root scope could not be persisted",
        )
        raise HTTPException(status_code=503, detail="paper_order_kanban_unavailable")

    try:
        repository.bind_root(record.order_request_id, root_task_id)
    except (UserOrderRequestConflict, UserOrderWorkflowUnavailable) as exc:
        _mark_paper_order_failed(
            repository,
            record.order_request_id,
            error_code="CEO_ROOT_BIND_FAILED",
            error_message=str(exc),
        )
        status = 409 if isinstance(exc, UserOrderRequestConflict) else 503
        raise HTTPException(
            status_code=status, detail="paper_order_workflow_unavailable"
        ) from exc

    trading = hermes_boundary.create_kanban_task(
        assignee=canonical_profile_for_department("trading"),
        title=(
            "사용자 PAPER 조건주문 AST 해석 및 즉시 활성화"
            if conditional_rule
            else (
                "사용자 PAPER 주문 결정론적 검증·제출"
                if deterministic_candidate is not None
                else "사용자 PAPER 주문 원문 해석 및 검증 제출"
            )
        ),
        body=(
            _conditional_rule_child_body(
                query=req.query,
                scope=scope,
                root_task_id=root_task_id,
                request_id=req.request_id,
                has_mandate=bool(mandate),
            )
            if conditional_rule
            else (
                _deterministic_order_child_body(
                    query=req.query,
                    scope=scope,
                    root_task_id=root_task_id,
                    request_id=req.request_id,
                    has_mandate=bool(mandate),
                )
                if deterministic_candidate is not None
                else _paper_order_child_body(
                    query=req.query,
                    scope=scope,
                    root_task_id=root_task_id,
                    request_id=req.request_id,
                    has_mandate=bool(mandate),
                )
            )
        ),
        idempotency_key=primary_idempotency_key(
            root_task_id, "trading-department"
        ),
        # A deterministic candidate is executed synchronously by this trusted
        # BFF and must never be claimed by Hermes. Ambiguous language retains
        # the durable blocked-then-release interpreter workflow.
        initial_status="running" if deterministic_candidate is not None else "blocked",
    )
    if not trading or not trading.get("task_id"):
        _mark_paper_order_failed(
            repository,
            record.order_request_id,
            error_code="TRADING_TASK_CREATE_FAILED",
            error_message="Trading Hermes task could not be created",
        )
        raise HTTPException(status_code=503, detail="paper_order_kanban_unavailable")
    trading_task_id = str(trading["task_id"])
    try:
        repository.bind_trading_task(record.order_request_id, trading_task_id)
    except (UserOrderRequestConflict, UserOrderWorkflowUnavailable) as exc:
        _mark_paper_order_failed(
            repository,
            record.order_request_id,
            error_code="TRADING_TASK_BIND_FAILED",
            error_message=str(exc),
        )
        status = 409 if isinstance(exc, UserOrderRequestConflict) else 503
        raise HTTPException(
            status_code=status, detail="paper_order_workflow_unavailable"
        ) from exc

    # The root was created running (and therefore unclaimable) while the
    # Trading card was created blocked. The root is only an immutable
    # authority/scope container in this lane, so complete it in place instead
    # of releasing it to a CEO worker.
    # Releasing both cards allowed the CEO worker to block the root while the
    # Trading interpreter was still preparing its trusted tool call.
    if not hermes_boundary.complete_kanban_task(
        task_id=root_task_id,
        result="PAPER order scope bound; Trading primary owns interpretation and execution",
    ):
        _mark_paper_order_failed(
            repository,
            record.order_request_id,
            error_code="CEO_ROOT_FINALIZE_FAILED",
            error_message="CEO root scope could not be finalized after durable binding",
        )
        raise HTTPException(status_code=503, detail="paper_order_kanban_unavailable")

    if deterministic_candidate is not None:
        execution_started_at = time.monotonic()
        try:
            execution = process_deterministic_user_paper_order(
                root_task_id=root_task_id,
                trading_task_id=trading_task_id,
                interpretation=deterministic_candidate.model_dump(mode="json"),
            )
        except Exception as exc:  # noqa: BLE001 - unknown commit must not be retried.
            logger.error(
                "paper-order-deterministic-failed request=%s root=%s trading=%s "
                "exception_type=%s",
                record.order_request_id,
                root_task_id,
                trading_task_id,
                type(exc).__name__,
            )
            try:
                repository.mark_outcome(
                    record.order_request_id,
                    state="UNKNOWN",
                    error_code="DETERMINISTIC_EXECUTION_UNAVAILABLE",
                    error_message=(
                        "Deterministic PAPER execution did not return a safe result"
                    ),
                )
            except Exception as record_exc:  # noqa: BLE001 - preserve UNKNOWN wording.
                logger.error(
                    "paper-order-deterministic-unknown-record-failed request=%s "
                    "exception_type=%s",
                    record.order_request_id,
                    type(record_exc).__name__,
                )
            execution = {
                "decision": "UNKNOWN",
                "mode": "PAPER",
                "binding": False,
                "order_submitted": False,
                "order_request_id": record.order_request_id,
                "request_state": "UNKNOWN",
                "reason_codes": ["DETERMINISTIC_EXECUTION_UNAVAILABLE"],
                "user_message": (
                    "PAPER 주문 상태를 확정하지 못했습니다. 중복 주문 방지를 위해 "
                    "자동 재시도하지 않습니다. 주문 요청 ID "
                    f"{record.order_request_id}."
                ),
            }
        execution_message = str(execution.get("user_message") or "").strip()
        if not execution_message:
            execution_message = (
                "PAPER 주문을 제출하지 않았습니다. "
                f"상태={execution.get('request_state') or 'UNKNOWN'}. "
                "자동 재시도하지 않습니다."
            )
        completed = hermes_boundary.complete_kanban_task(
            task_id=trading_task_id,
            result=execution_message,
        )
        if not completed:
            logger.error(
                "paper-order-deterministic-delivery-failed request=%s trading=%s",
                record.order_request_id,
                trading_task_id,
            )
        logger.info(
            "paper-order-deterministic-complete request=%s root=%s trading=%s "
            "state=%s delivery=%s admission_ms=%d kanban_ms=%d execution_ms=%d "
            "total_ms=%d",
            record.order_request_id,
            root_task_id,
            trading_task_id,
            execution.get("request_state"),
            completed,
            round((admitted_at - route_started_at) * 1000),
            round((execution_started_at - admitted_at) * 1000),
            round((time.monotonic() - execution_started_at) * 1000),
            round((time.monotonic() - route_started_at) * 1000),
        )
        released_root = {**root, "status": "done"}
        released_trading = {
            **trading,
            "status": "done" if completed else "running",
        }
        return _paper_order_execution_response(
            root_task=released_root,
            trading_task=released_trading,
            result=execution,
        )

    if not hermes_boundary.unblock_kanban_task(task_id=trading_task_id):
        _mark_paper_order_failed(
            repository,
            record.order_request_id,
            error_code="TRADING_TASK_RELEASE_FAILED",
            error_message="Trading task remained blocked after durable binding",
        )
        raise HTTPException(status_code=503, detail="paper_order_kanban_unavailable")

    released_root = {**root, "status": "done"}
    released_trading = {**trading, "status": "ready"}
    logger.info(
        "paper-order-routed request=%s root=%s trading=%s mode=PAPER conditional=%s "
        "admission_ms=%d kanban_ms=%d total_ms=%d",
        record.order_request_id,
        root_task_id,
        trading_task_id,
        conditional_rule,
        round((admitted_at - route_started_at) * 1000),
        round((time.monotonic() - admitted_at) * 1000),
        round((time.monotonic() - route_started_at) * 1000),
    )
    return _paper_order_accepted_response(
        root_task=released_root,
        trading_task=released_trading,
        order_request_id=record.order_request_id,
        conditional_rule=conditional_rule,
    )


def _route_traced_user_paper_order(
    req: CeoAsk,
    *,
    owner_id: str | None,
    mandate: Mapping[str, object] | None,
    conditional_rule: bool,
) -> dict[str, object]:
    """Route the direct PAPER lane with the same redacted root trace as analysis."""

    root_trace = None
    try:
        from orchestration.llm_observability import start_root_trace

        root_trace = start_root_trace(
            request_id=req.request_id,
            workflow_mode="binding",
            source=getattr(req, "source", None),
        )
    except Exception:  # noqa: BLE001 - observability remains fail-open.
        root_trace = None
    try:
        return _route_user_paper_order(
            req,
            owner_id=owner_id,
            mandate=mandate,
            conditional_rule=conditional_rule,
            langsmith_trace_context=(
                root_trace.context if root_trace is not None else None
            ),
        )
    except Exception as exc:
        if root_trace is not None:
            try:
                from orchestration.llm_observability import close_root_trace

                close_root_trace(
                    root_trace.context,
                    request_id=req.request_id,
                    workflow_mode="binding",
                    source=getattr(req, "source", None),
                    status="error",
                    error_class=type(exc).__name__,
                )
            except Exception:  # noqa: BLE001 - tracing cannot mask route failure.
                pass
        raise


def ceo_query(
    req: CeoAsk,
    owner_id: str | None = Depends(optional_current_user),
    *,
    discord_channel_id: str | None = None,
    discord_message_id: str | None = None,
    discord_guild_id: str | None = None,
    discord_thread_id: str | None = None,
) -> dict[str, object]:
    """Create the CEO root task; supervisor execution remains asynchronous.

    **이 함수는 `POST /ui/ceo/ask`의 유일한 구현이지만, 여기서는 route로
    등록하지 않는다.** `apps.api.ceo_mirror_api.mirror_ask`가 이 함수를 그대로
    감싸(dedup + Web/Discord 공용 event journal) 그 경로의 유일한 소유자로
    등록한다.

    ## 왜 여기서 `@router.post("/ask", ...)`를 안 붙이나

    예전에는 이 모듈도 같은 경로에 `@router.post("/ask", ...)`를 붙였고, 실제
    서비스에서는 `ceo_mirror_router`가 `main.py`에서 먼저 등록돼 그 라우트를
    항상 그림자로 덮었다(FastAPI는 같은 (path, method) 조합이 여러 라우터에
    있으면 등록 순서가 먼저인 쪽이 이긴다). 그 결과 실제 요청은 항상 mirror의
    자체 파싱 경로를 탔는데, mirror가 이 함수의 파라미터를 그대로 재사용하지
    않고 `fund_id` 없는 별도 모델을 새로 만들어 넘겨서, Mandate 스냅샷이
    항상 유실되는 사고가 났다(2026-08-14 AWS 실측) - 코드는 여기 있는데
    실행되는 코드는 따로 있었던 것이다.

    같은 경로를 두 라우터가 나눠 갖고 등록 순서로 승부하는 구조 자체가
    위험하므로, 이제는 이 함수 하나만 존재하고 mirror가 그 함수를 감싸는
    형태로 되돌린다 - "두 번째 구현"이 아예 존재할 수 없게 한다.
    `tests/api/test_main_routes.py`가 실제 앱에 같은 (path, method) 조합이
    두 번 이상 등록되면 실패하도록 고정한다.

    `owner_id`(`X-User-Id`)는 2026-08-12에 추가됐다. 그 전까지 이 경로는 요청자를
    **아예 몰랐다** - `AgentAsk`에 `query`와 `request_id`만 있어서, CEO는 누가
    물었는지도 그 사람의 Mandate가 무엇인지도 알 수 없었다.

    ## 왜 여기서 Discord에 게시하지 않나 (2026-08-18 이동)

    `discord_*` 좌표는 **받기만 한다.** 한때 이 함수가 직접
    `discord_mirror.post_question()`을 불렀는데, 그러면 두 가지가 깨진다:

    1. **출처를 모른다.** 이 함수는 요청이 웹에서 왔는지 Discord에서 왔는지
       알 수 없다. Discord에서 온 요청까지 게시하면 사용자가 쓴 원본 옆에 봇이
       같은 내용을 한 번 더 올린다.
    2. **이 함수를 부르는 모든 경로가 네트워크로 나간다.** 단위 테스트가 이
       함수를 부르는 것만으로 실제 팀 채널에 글이 올라갔다(2026-08-18 실측).

    그래서 게시 여부 판단은 출처를 아는 `apps/api/ceo_mirror_api._ceo_query`가
    하고, 이 함수는 그 결과 좌표를 root body에 적기만 한다.
    """

    # Mandate 스냅샷. 못 읽으면 None이고 그때는 블록 없이 진행한다 - 이것 때문에
    # 질의 접수가 실패하면 Mandate가 없는 사용자는 아무 질문도 못 한다.
    # `getattr`로 읽는 이유: 이 라우트는 `CeoAsk`를 받지만, 부서 질의와 같은
    # `AgentAsk`를 직접 넘기는 호출부(테스트·내부 유틸)가 남아 있을 수 있고 그쪽엔
    # `fund_id`가 없다. 속성 부재로 500을 내는 대신 "Mandate 없음"으로 떨어진다.
    fund_id = getattr(req, "fund_id", None)
    mandate = fetch_current_mandate_by_fund(fund_id) if fund_id else None

    analysis_then_conditional_plan = parse_analysis_then_conditional_paper_order(
        req.query
    )
    if analysis_then_conditional_plan is not None:
        return _route_analysis_then_conditional_paper_order(
            req,
            plan=analysis_then_conditional_plan,
            owner_id=owner_id if isinstance(owner_id, str) else None,
            mandate=mandate,
            discord_channel_id=discord_channel_id,
            discord_message_id=discord_message_id,
            discord_guild_id=discord_guild_id,
            discord_thread_id=discord_thread_id,
        )

    if parse_compound_paper_order(req.query) is not None:
        return _route_compound_user_paper_order(
            req,
            owner_id=owner_id if isinstance(owner_id, str) else None,
            mandate=mandate,
        )

    if looks_like_conditional_paper_rule(req.query):
        return _route_traced_user_paper_order(
            req,
            owner_id=owner_id if isinstance(owner_id, str) else None,
            mandate=mandate,
            conditional_rule=True,
        )

    if looks_like_user_order_request(
        req.query
    ) and not is_clearly_non_executable_order_language(req.query):
        return _route_traced_user_paper_order(
            req,
            owner_id=owner_id if isinstance(owner_id, str) else None,
            mandate=mandate,
            conditional_rule=False,
        )

    workflow_mode = infer_workflow_mode(req.query)
    d5_bank = ExperienceBank.from_env()
    d5_lookup = None
    if d5_bank.enabled:
        d5_lookup = d5_bank.lookup(
            case_type="discord_ceo",
            binding=workflow_mode == "binding",
            correlation_id=req.request_id,
        )
    d5_hint = (
        d5_lookup.planner_hint
        if d5_lookup is not None and d5_bank.mode == "active"
        else None
    )
    approved_feedback = None
    try:
        # This is a local, bounded SQLite read only when feedback mode is
        # active.  It never calls LangSmith on the CEO hot path; missing or
        # locked feedback state simply produces no advisory hint.
        from orchestration.langsmith_feedback import approved_feedback_hint

        approved_feedback = approved_feedback_hint()
    except Exception:  # noqa: BLE001 - advisory feedback is fail-open.
        approved_feedback = None
    root_trace = None
    try:
        from orchestration.llm_observability import start_root_trace

        root_trace = start_root_trace(
            request_id=req.request_id,
            workflow_mode=workflow_mode,
            source=getattr(req, "source", None),
        )
    except Exception:  # noqa: BLE001 - observability remains fail-open.
        root_trace = None

    try:
        task = hermes_boundary.create_kanban_task(
            assignee=canonical_profile_for_department("ceo"),
            title=f"사용자 질의: {req.query[:120]}",
            body=build_root_body(
                req.query,
                req.request_id,
                workflow_mode=workflow_mode,
                mandate=mandate,
                requested_by=owner_id,
                discord_channel_id=discord_channel_id,
                discord_message_id=discord_message_id,
                discord_guild_id=discord_guild_id,
                discord_thread_id=discord_thread_id,
                langsmith_trace_context=(
                    root_trace.context if root_trace is not None else None
                ),
                advisory_fund_id=getattr(req, "fund_id", None),
                advisory_book_id=getattr(req, "book_id", None),
                experience_hint=d5_hint,
                approved_feedback_hint=approved_feedback,
            ),
            idempotency_key=req.request_id,
        )
    except Exception as exc:
        if root_trace is not None:
            from orchestration.llm_observability import close_root_trace

            close_root_trace(
                root_trace.context,
                request_id=req.request_id,
                workflow_mode=workflow_mode,
                source=getattr(req, "source", None),
                status="error",
                error_class=type(exc).__name__,
            )
        raise
    if not task or not task.get("task_id"):
        if root_trace is not None:
            from orchestration.llm_observability import close_root_trace

            close_root_trace(
                root_trace.context,
                request_id=req.request_id,
                workflow_mode=workflow_mode,
                source=getattr(req, "source", None),
                status="error",
                error_class="root_create_failed",
            )
        raise HTTPException(
            status_code=503,
            detail="CEO root Kanban task를 생성하지 못했습니다. Hermes Kanban runtime을 확인하세요.",
        )
    logger.info(
        "ceo-planning root=%s request_id=%s producer=portfolio-bff",
        task["task_id"],
        req.request_id,
    )
    if d5_lookup is not None:
        hint_metadata = (
            d5_lookup.planner_hint
            if isinstance(d5_lookup.planner_hint, Mapping)
            else {}
        )

        def _hint_count(key: str) -> int:
            value = hint_metadata.get(key)
            return min(len(value), 20) if isinstance(value, (list, tuple)) else 0

        try:
            matched_successes = max(
                0, min(int(hint_metadata.get("successful_runs", 0)), 20)
            )
        except (TypeError, ValueError):
            matched_successes = 0
        matched_failures = max(d5_lookup.matched_count - matched_successes, 0)
        error_category = str(d5_lookup.error_code or "NONE")[:64]
        root_id = str(task["task_id"])

        # The application logger runs above INFO in the production BFF.  Keep
        # these two events payload-free and use the existing logger so the
        # already-computed timings are visible in the container log without a
        # second timer, DB write, network sink, or model call.
        logger.warning(
            "event=memo_harness_d5_lookup root_id=%s mode=%s success=%s "
            "matched_successes=%d matched_failures=%d matched_patterns=%d "
            "lookup_ms=%d error_category=%s",
            root_id,
            d5_lookup.mode,
            str(bool(d5_lookup.available)).lower(),
            matched_successes,
            matched_failures,
            _hint_count("lessons"),
            d5_lookup.lookup_ms,
            error_category,
        )
        logger.warning(
            "event=memo_harness_d5_hint root_id=%s mode=%s hint_present=%s "
            "hint_injected=%s support_count=%d preferred_policy_count=%d "
            "avoid_profile_count=%d avoid_pattern_count=%d hint_build_ms=%d",
            root_id,
            d5_lookup.mode,
            str(bool(d5_hint)).lower(),
            str(d5_lookup.mode == "active" and bool(d5_hint)).lower(),
            matched_successes,
            _hint_count("successful_policies"),
            _hint_count("avoid_profiles"),
            _hint_count("avoid_patterns"),
            d5_lookup.hint_build_ms,
        )
        logger.info(
            "ceo-planning root=%s request_id=%s producer=portfolio-bff",
            str(task["task_id"]),
            req.request_id,
        )
    else:
        logger.info(
            "ceo-planning root=%s request_id=%s producer=portfolio-bff",
            task["task_id"],
            req.request_id,
        )

    if not hermes_boundary.comment_root_scope(
        task_id=str(task["task_id"]), request_id=req.request_id
    ):
        if root_trace is not None:
            from orchestration.llm_observability import close_root_trace

            close_root_trace(
                root_trace.context,
                request_id=req.request_id,
                root_id=str(task["task_id"]),
                workflow_mode=workflow_mode,
                source=getattr(req, "source", None),
                status="error",
                error_class="root_scope_failed",
            )
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
            qa_enabled=workflow.qa_enabled,
            qa_blocks_response=workflow.qa_blocks_response,
            qa_materialized=workflow.qa_materialized,
            qa_legacy_primary_present=workflow.qa_legacy_primary_present,
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
    owner_id: str | None = Query(default=None),
    authenticated_owner_id: str | None = Depends(current_user),
) -> TaskListResponse:
    """계정별 이력 조회. `owner_id`는 반드시 서버가 걸러서 내려준다.

    `X-User-Id`는 인증이 아니지만, 그렇다고 프론트가 전체 목록을 받아 클라이언트
    에서 골라내면 다른 계정의 질문·답변 텍스트가 네트워크 응답에 그대로 실려
    나간다 - 계정 간 대화가 새는 것과 같다(2026-08-14). 그래서 필터는
    `list_ceo_roots(owner_id=...)`가 Root Body만 보고 조회 단계에서 처리한다.
    """

    normalized_owner_id = owner_id.strip() if owner_id and owner_id.strip() else None
    if (
        authenticated_owner_id is not None
        and normalized_owner_id is not None
        and normalized_owner_id != authenticated_owner_id
    ):
        raise HTTPException(status_code=403, detail="ceo_task_owner_mismatch")
    # A verified identity always controls the server-side filter. The query
    # value remains only for explicit identity-free local/test fixtures.
    normalized_owner_id = authenticated_owner_id or normalized_owner_id
    try:
        rows = list_ceo_roots(
            limit=limit, include_archived=include_archived, owner_id=normalized_owner_id
        )
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
                owner_id=requested_by_from_body(body),
            )
            for (task_id, body), workflow in zip(
                identified, workflows, strict=True
            )
        ]
    )


@router.get("/kanban", operation_id="ceo_kanban_board", response_model=KanbanBoardResponse)
def ceo_kanban_board(
    _authenticated_owner_id: str | None = Depends(current_user),
) -> KanbanBoardResponse:
    """Return a read-only four-column projection of the shared Hermes board.

    The BFF owns the Hermes CLI boundary. The browser receives only the small
    card projection needed by Agent Logs, never the Hermes dashboard, session
    cookie, database, or mutation commands.
    """

    try:
        rows = list_tasks(include_archived=False)
    except KanbanUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Hermes Kanban을 읽지 못했습니다: {exc}",
        ) from exc

    columns: dict[str, list[KanbanBoardCard]] = {
        "todo": [],
        "ready": [],
        "inprogress": [],
        "done": [],
    }
    for row in rows:
        task_id = str(row.get("id") or row.get("task_id") or "").strip()
        if not task_id:
            continue
        status = str(row.get("status") or "unknown").strip().casefold() or "unknown"
        card = KanbanBoardCard(
            task_id=task_id,
            title=str(row.get("title") or task_id).strip() or task_id,
            assignee=str(row.get("assignee") or "unassigned").strip() or "unassigned",
            status=status,
            created_at=row.get("created_at"),
        )
        columns[kanban_column_for_status(status)].append(card)

    return KanbanBoardResponse(
        observed_at=datetime.now(timezone.utc).isoformat(),
        columns=KanbanBoardColumns(**columns),
    )


@router.get("/tasks/{task_id}", operation_id="ceo_task_status", response_model=TaskStatusResponse)
def ceo_task_status(
    task_id: str = _TASK_ID_PATH,
    authenticated_owner_id: str | None = Depends(current_user),
) -> dict[str, object]:
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
        _require_ceo_task_owner(
            str(raw.get("body") or ""), authenticated_owner_id
        )
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
    _require_ceo_workflow_owner(workflow, authenticated_owner_id)
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
def ceo_task_graph(
    task_id: str = _TASK_ID_PATH,
    authenticated_owner_id: str | None = Depends(current_user),
) -> TaskGraphResponse:
    workflow = _load(task_id)
    _require_ceo_workflow_owner(workflow, authenticated_owner_id)
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
def ceo_task_result(
    task_id: str = _TASK_ID_PATH,
    authenticated_owner_id: str | None = Depends(current_user),
) -> TaskResultResponse:
    workflow = _load(task_id)
    _require_ceo_workflow_owner(workflow, authenticated_owner_id)
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
def ceo_task_archive(
    task_id: str = _TASK_ID_PATH,
    authenticated_owner_id: str | None = Depends(current_user),
) -> TaskArchiveResponse:
    workflow = _load(task_id)
    _require_ceo_workflow_owner(workflow, authenticated_owner_id)
    # Keep the legacy operator endpoint, but route it through the same
    # root-scoped retention policy. Direct per-task Hermes archive is unsafe:
    # it can archive a running child and it has no synthesis/recovery/Discord
    # guard. The periodic worker remains the normal path; this endpoint is a
    # safe explicit maintenance request only.
    try:
        delivery = DiscordLedgerReader().state(workflow)
        decision = evaluate_workflow(workflow, delivery=delivery)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Retention 상태를 확인하지 못했습니다.") from exc
    if not decision.eligible:
        raise HTTPException(
            status_code=409,
            detail=f"Workflow archive 조건을 충족하지 않습니다: {decision.reason}",
        )
    target_ids = [node.task_id for node in workflow.descendants]
    target_ids.append(workflow.root_task_id)
    try:
        metadata = build_audit_metadata(workflow, delivery)
        audit = AuditStore(default_audit_path())
        audit.save_archive(metadata, archived_at=int(time.time()))
        if not SQLiteKanbanMaintenance().archive_workflow(
            workflow.root_task_id,
            [node.task_id for node in workflow.descendants],
        ):
            raise KanbanUnavailable("root workflow archive CAS failed")
    except KanbanTaskNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Task를 찾을 수 없습니다: {task_id}") from exc
    except KanbanUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Archive에 실패했습니다: {exc}") from exc
    return TaskArchiveResponse(task_id=task_id, archived_task_ids=target_ids)


__all__ = ["router"]
