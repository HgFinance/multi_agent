"""CEO Office query boundary and closed-loop Kanban workflow APIs.

`/ui/ceo/ask` creates only the CEO root task.  The BFF may attach the shared
deterministic department route to that root; the CEO Supervisor then
materializes the already-selected department tasks, owns QA, and performs
final synthesis.  All read paths use the normalized Kanban reader; the BFF
never opens Hermes' database.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256

from orchestration.discord_delivery import humanize_user_facing_text
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
    from .conditional_rule_orchestrator import process_user_conditional_paper_rule
    from .conditional_rules import build_delayed_order_candidate
    from .current_user import (
        current_user,
        optional_current_user,
        require_trading_book_access,
    )
    from .governance_client import fetch_current_mandate_by_fund
    from .user_order_orchestrator import process_deterministic_user_paper_order
    from .user_order_workflow import (
        UserOrderRequestConflict,
        UserOrderRequestRecord,
        UserOrderWorkflowUnavailable,
        user_order_repository,
    )
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    import hermes_boundary  # type: ignore[no-redef]
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
    from conditional_rule_orchestrator import (  # type: ignore[no-redef]
        process_user_conditional_paper_rule,
    )
    from conditional_rules import (  # type: ignore[no-redef]
        build_delayed_order_candidate,
    )
    from current_user import (  # type: ignore[no-redef]
        current_user,
        optional_current_user,
        require_trading_book_access,
    )
    from governance_client import (
        fetch_current_mandate_by_fund,  # type: ignore[no-redef]
    )
    from user_order_orchestrator import (  # type: ignore[no-redef]
        process_deterministic_user_paper_order,
    )
    from user_order_workflow import (  # type: ignore[no-redef]
        UserOrderRequestConflict,
        UserOrderRequestRecord,
        UserOrderWorkflowUnavailable,
        user_order_repository,
    )

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from orchestration.canonical_profiles import (
    CANONICAL_PROFILES,
    canonical_profile_for_department,
)
from orchestration.ceo_query_routing import is_read_only_hr_e2e_query
from orchestration.ceo_request_classifier import classify_ceo_request
from orchestration.ceo_workflow_scope import (
    UserPaperOrderScope,
    build_root_body,
    build_scoped_task_body,
    build_user_paper_order_scope,
    primary_idempotency_key,
    read_marker,
    requested_by_from_body,
    selected_primary_profiles_from_task,
)
from orchestration.dynamic_universe_orders import (
    expand_to_basket_instruction,
    parse_dynamic_universe_order,
)
from orchestration.compound_paper_orders import (
    AnalysisThenConditionalPaperOrderPlan,
    build_compound_conditional_candidate,
    parse_compound_paper_order,
)
from orchestration.experience_bank import (
    ExperienceBank,
    bounded_failure_memory_hint,
    discord_experience_case_type,
)
from orchestration.qa_contract import (
    canonical_qa_contract,
    split_planner_selection,
)
from orchestration.user_order_language import (
    deterministic_delayed_order_plan,
    deterministic_order_candidate,
)

router = APIRouter(prefix="/ui/ceo", tags=["ceo-office"])
logger = logging.getLogger(__name__)


def _trace_error_metadata(exc: BaseException) -> dict[str, object]:
    """Return bounded HTTP/error labels without copying request payloads."""

    metadata: dict[str, object] = {"error_code": type(exc).__name__}
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        metadata["http_status"] = status_code
    detail = getattr(exc, "detail", None)
    if isinstance(detail, Mapping):
        detail = detail.get("code")
    if isinstance(detail, str):
        candidate = detail.strip()
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", candidate):
            metadata["error_code"] = candidate
    return metadata


def _paper_order_terminal_trace(
    response: Mapping[str, object],
    *,
    request_id: str,
) -> tuple[str, dict[str, object], dict[str, object]] | None:
    """Build one safe terminal envelope for the existing PAPER root trace."""

    execution = response.get("execution")
    if not isinstance(execution, Mapping):
        # The asynchronous Hermes lane has not produced a terminal result yet.
        return None
    state = (
        str(execution.get("request_state") or execution.get("decision") or "UNKNOWN")
        .strip()
        .upper()
    )
    terminal = state in {"COMPLETED", "FAILED", "UNKNOWN", "ACCOUNTING_PENDING"}
    status = "completed" if state == "COMPLETED" else "degraded"
    metadata: dict[str, object] = {
        "schema_version": "paper-order-observability.v1",
        "worker_id": "trading-deterministic",
        "role": "primary",
        "stage": "trading",
        "department": "trading",
        "workflow_role": "primary",
        "workflow_mode": "binding",
        "trace_kind": "deterministic_paper_order",
        "latency_scope": "paper_order_execution",
        "status": status if terminal else "running",
        "terminal_status": state,
        "tool_calls": 1,
        "tool_error_count": int(state in {"FAILED", "UNKNOWN"}),
        "error_count": int(state in {"FAILED", "UNKNOWN"}),
        "raw_payloads_sent": False,
        "request_id": request_id,
        "root_id": str(response.get("task_id") or ""),
        "task_id": str(response.get("trading_task_id") or ""),
    }
    output_summary: dict[str, object] = {
        "execution_path": "deterministic_paper",
        "mode": "PAPER",
        "request_state": state,
        "decision": str(execution.get("decision") or ""),
        "order_submitted": bool(execution.get("order_submitted")),
    }
    return status, metadata, output_summary


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
    # Bounded, explicit Discord thread context for follow-ups such as "위 질문".
    # It is stored in the root body, never used as authorization, and is not
    # copied into LangSmith inputs.
    previous_question_context: str | None = None
    previous_question_context_source_message_id: str | None = None
    # Explicit user action from the preview card. It only bypasses the
    # confirmation prompt; the same parser, authorization, and PAPER gates
    # still run on the confirmed request.
    confirm_order: bool = False


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


def _expand_dynamic_universe_request(req: CeoAsk) -> CeoAsk:
    """Rewrite a market-cap ranking request into the explicit basket sentence.

    Returns the request unchanged whenever the phrase is not recognised, the
    snapshot is missing or stale, or the ranking is shorter than the requested
    member count. Failing closed keeps a half-filled basket - a different order
    from the one the user asked for - from ever being admitted (개발 원칙 9).
    """

    plan = parse_dynamic_universe_order(req.query)
    if plan is None:
        return req
    # `ls_account_stream`은 이 모듈의 저장소 접근자를 되읽으므로 최상위에서
    # 들여오면 순환이 된다. 순위를 실제로 볼 때만 들여온다.
    try:
        from .ls_account_stream import market_cap_universe_rows
    except ImportError:  # pragma: no cover - 스크립트 실행 경로
        try:
            from ls_account_stream import market_cap_universe_rows  # type: ignore[no-redef]
        except ImportError:
            return req
    snapshot = market_cap_universe_rows()
    if snapshot is None:
        return req
    rows, _as_of = snapshot
    expanded = expand_to_basket_instruction(plan, rows)
    if not expanded:
        return req
    return req.model_copy(update={"query": expanded})


def _deterministic_paper_order_fast_path_enabled() -> bool:
    """Use the code-built interpreter for one exact, unambiguous order.

    Production defaults on because the path still traverses the same durable
    authority and OMS gates. Development and tests retain the Hermes lane
    unless explicitly enabled, which keeps local workflows inspectable.
    """

    configured = (
        os.getenv("USER_PAPER_ORDER_DETERMINISTIC_FAST_PATH_ENABLED", "")
        .strip()
        .casefold()
    )
    if configured:
        return configured in {"1", "true", "yes", "on"}
    return os.getenv("APP_ENV", "development").strip().casefold() in {
        "prod",
        "production",
    }


def _is_read_only_hr_e2e_request(raw_query: str) -> bool:
    """Compatibility alias for the canonical CEO routing predicate."""

    return is_read_only_hr_e2e_query(raw_query)


def _is_read_only_risk_e2e_request(raw_query: str) -> bool:
    """Keep an explicit Risk E2E review out of the high-recall order router.

    Legal examples often contain words such as ``매도`` or ``6개월 이내``.
    Those are evidence for the Risk/Legal review, not an instruction to create
    a PAPER order.  Require an explicit Risk scope plus both an E2E/read-only
    marker and a no-execution marker before bypassing the order detectors.
    """

    text = str(raw_query or "").casefold()
    has_risk_scope = "리스크" in text or "risk" in text
    has_e2e_scope = any(
        marker in text
        for marker in (
            "e2e",
            "읽기 전용",
            "read-only",
            "비주문",
            "검증",
        )
    )
    has_no_execution = any(
        marker in text
        for marker in (
            "주문 금지",
            "주문·매매·승인",
            "주문/매매/승인",
            "실제 주문",
            "실행하지",
            "실행 금지",
            "no order",
        )
    )
    return has_risk_scope and has_e2e_scope and has_no_execution


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


def _load(
    task_id: str,
    *,
    max_workers: int | None = None,
    listed_rows: Sequence[Mapping[str, object]] | None = None,
    known_root: bool = False,
) -> Workflow:
    """Load a root workflow and translate CLI failures to HTTP errors."""

    try:
        return load_workflow(
            task_id,
            max_workers=max_workers,
            listed_rows=listed_rows,
            known_root=known_root,
        )
    except KanbanTaskNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"Task를 찾을 수 없습니다: {task_id}"
        ) from exc
    except KanbanUnavailable as exc:
        raise HTTPException(
            status_code=503, detail=f"Hermes Kanban을 읽지 못했습니다: {exc}"
        ) from exc


_TASK_LIST_STATUS = {
    "done": "completed",
    "completed": "completed",
    "archived": "archived",
    "blocked": "blocked",
    "failed": "failed",
    "error": "failed",
    "running": "running",
    "claimed": "running",
    "processing": "running",
    "review": "running",
    "ready": "queued",
    "queued": "queued",
    "todo": "queued",
    "triage": "queued",
}


def _task_list_item_from_row(row: Mapping[str, object]) -> TaskListItem | None:
    """Build the bounded history projection without hydrating a full graph.

    The detail/graph endpoints remain authoritative for descendant progress. A
    list page only needs the root's durable status, query, owner, and declared
    planner selection. Hydrating every root with repeated Hermes ``show``
    subprocesses made a large history page time out before it could render.
    Rows without a durable status (legacy/unit fixtures) deliberately fall
    back to the full workflow loader below.
    """

    task_id = str(row.get("id") or row.get("task_id") or "").strip()
    body = str(row.get("body") or "")
    status = _TASK_LIST_STATUS.get(str(row.get("status") or "").casefold().strip())
    if not task_id or not status:
        return None
    raw_selected = selected_primary_profiles_from_task(row)
    selected, _planner_qa_requested = split_planner_selection(raw_selected)
    created_at = row.get("created_at")
    created_iso: str | None = None
    if isinstance(created_at, (int, float)) and not isinstance(created_at, bool):
        try:
            created_iso = (
                datetime.fromtimestamp(float(created_at), tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            created_iso = None
    elif isinstance(created_at, str) and created_at.strip():
        created_iso = created_at.strip()
    return TaskListItem(
        task_id=task_id,
        query=extract_user_query(body),
        status=status,
        created_at=created_iso,
        selected_departments=list(selected),
        owner_id=requested_by_from_body(body),
    )


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
        elif (
            assignee in _PRIMARY_PROFILE_ORDER
            and role in {"", "primary"}
            and assignee not in selected
            and not declared_primary
        ):
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
        elif (
            assignee in _PRIMARY_PROFILE_ORDER
            and role in {"", "primary"}
            and assignee not in selected
        ):
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
        labels = [
            _PROFILE_LABEL[profile] for profile in selected if profile in _PROFILE_LABEL
        ]
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
    # Current BFF roots carry explicit parent/child links.  Their child rows
    # are already in ``kanban show``; rediscovering the same scope through a
    # full-board ``kanban list`` only adds a subprocess and can dominate a
    # status/result poll.  Keep the board scan for legacy marker-only roots,
    # whose parent links are precisely what this projection repairs.
    if (
        read_marker(str(root.get("body") or ""), "producer")
        == "portfolio-bff-deterministic"
        and _child_records(root.get("children"))
    ):
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


def _hydrated_planning_projection(workflow: Workflow) -> dict[str, object]:
    """Project planning from the canonical workflow already loaded for this request.

    ``load_workflow`` has resolved both linked and legacy marker-only members.  A
    second full-board scan here would rediscover the same rows and used to add a
    Hermes subprocess to every status poll.  Preserve any embedded child rows,
    then fill the projection from the normalized descendants without opening a
    second read path or inferring tasks that were not actually materialized.
    """

    projection = dict(workflow.root_payload)
    children: list[Mapping[str, object]] = []
    positions: dict[str, int] = {}
    for child in _child_records(projection.get("children")):
        child_id = str(child.get("id") or child.get("task_id") or "").strip()
        if child_id:
            positions[child_id] = len(children)
        children.append(child)
    for node in workflow.descendants:
        child_id = str(node.task_id).strip()
        if child_id and isinstance(node.raw, Mapping):
            if child_id in positions:
                children[positions[child_id]] = node.raw
            else:
                positions[child_id] = len(children)
                children.append(node.raw)
    projection["children"] = children
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
        metadata=task.get("metadata")
        if isinstance(task.get("metadata"), Mapping)
        else None,
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
    # QA audits the exact CEO input and response after the response boundary
    # in every mode. Binding execution safety remains owned by deterministic
    # Risk/OMS admission; the UI must never project QA as CEO's prerequisite.
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


def _clarification_required_response(
    req: CeoAsk,
    *,
    owner_id: str | None,
    mandate: Mapping[str, object] | None,
    discord_channel_id: str | None,
    discord_message_id: str | None,
    discord_guild_id: str | None,
    discord_thread_id: str | None,
    routing_plan: Mapping[str, object],
    failure_memory: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Record an input clarification without creating executable children."""

    bounded_failure_memory = bounded_failure_memory_hint(failure_memory)
    answer = "요청 대상이나 원하는 작업이 불명확합니다. 종목·계좌·전략 등 대상과 분석, 조회, 위험 검토 같은 목적을 함께 적어 주세요."
    if bounded_failure_memory:
        answer += (
            " 유사 요청의 이전 실패 기록도 확인했지만, 현재 대상이 없어 임의로 "
            "부서를 재호출하지 않고 추가 입력을 기다립니다."
        )
    body = build_root_body(
        req.query,
        req.request_id,
        workflow_mode="analysis",
        source=getattr(req, "source", None),
        mandate=mandate,
        requested_by=owner_id,
        discord_channel_id=discord_channel_id,
        discord_message_id=discord_message_id,
        discord_guild_id=discord_guild_id,
        discord_thread_id=discord_thread_id,
        selected_primary_profiles=(),
        routing_basis="insufficient_query_intent",
        routing_category=str(
            routing_plan.get("category") or "PORTFOLIO_RECOMMENDATION"
        ),
        producer="portfolio-bff-deterministic",
    )
    task = hermes_boundary.create_kanban_task(
        assignee=canonical_profile_for_department("ceo"),
        title=f"입력 확인 필요: {req.query[:120]}",
        body=body,
        idempotency_key=req.request_id,
        initial_status="blocked",
    )
    if not task or not task.get("task_id"):
        raise HTTPException(
            status_code=503, detail="ceo_clarification_task_unavailable"
        )
    task_id = str(task["task_id"])
    scope_recorded = hermes_boundary.comment_root_scope(
        task_id=task_id, request_id=req.request_id
    )
    if not scope_recorded:
        # A clarification root has no executable children and therefore does
        # not need a scope comment to be safely dispatched.  The old behavior
        # leaked this non-critical projection failure as HTTP 503 even though
        # the durable root already carried block_reason=clarification_required.
        logger.warning(
            "ceo-clarification scope_deferred task_id=%s request_id=%s",
            task_id,
            req.request_id,
        )
    if bounded_failure_memory:
        logger.warning(
            "event=memo_harness_d5_failure_memory root_id=%s matched_failures=%s "
            "failure_code_count=%s failed_department_set_count=%s",
            task_id,
            bounded_failure_memory.get("matched_failures", 0),
            len(bounded_failure_memory.get("failure_codes", ()))
            if isinstance(bounded_failure_memory.get("failure_codes"), list)
            else 0,
            len(bounded_failure_memory.get("failed_department_sets", ()))
            if isinstance(bounded_failure_memory.get("failed_department_sets"), list)
            else 0,
        )
    completed = hermes_boundary.complete_kanban_task(task_id=task_id, result=answer)
    if not completed:
        logger.warning(
            "ceo-clarification status=blocked task_id=%s request_id=%s",
            task_id,
            req.request_id,
        )
    task_payload = {
        **task,
        "task_id": task_id,
        "status": "completed" if completed else "blocked",
        "latest_summary": answer,
    }
    response = _accepted_response(
        task_payload,
        {
            "status": "accepted",
            "answer": answer,
            "planning": {
                "selected_departments": [],
                "planned_departments": [],
                "materialized_departments": [],
                "steps": [],
                "qa_required": False,
                "planned_qa_required": False,
                "qa_enabled": True,
                "qa_blocks_response": False,
                "qa_materialized": False,
                "qa_legacy_primary_present": False,
                "planned_synthesis": False,
                "summary": "추가 입력이 필요해 부서 위임을 보류했습니다.",
            },
        },
    )
    if not scope_recorded:
        response["warning"] = "clarification_scope_deferred"
    return response


def _no_action_response() -> dict[str, object]:
    """Return an explicit, terminal non-execution response without a task.

    A standalone "do not buy/sell" instruction has no missing fact to ask
    for and no analysis objective to delegate.  Reuse the ordinary accepted
    response envelope so Web and Discord render it exactly once, while making
    no Kanban, order, conditional rule, or ledger mutation.
    """

    answer = "실행하지 말라는 요청으로 처리했습니다. PAPER 주문·조건주문·부서 작업은 만들지 않았습니다."
    return _accepted_response(
        {
            "task_id": "",
            "status": "completed",
            "source": "deterministic-no-action",
        },
        {
            "status": "accepted",
            "answer": answer,
            "planning": {
                "selected_departments": [],
                "planned_departments": [],
                "materialized_departments": [],
                "steps": [],
                "qa_required": False,
                "planned_qa_required": False,
                "qa_enabled": True,
                "qa_blocks_response": False,
                "qa_materialized": False,
                "qa_legacy_primary_present": False,
                "planned_synthesis": False,
                "summary": "명시적 비실행 요청으로 작업을 만들지 않았습니다.",
            },
        },
    )


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


def _accepted_response(
    task: Mapping[str, object], planning: Mapping[str, object]
) -> dict[str, object]:
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
        "order_state": "RULE_INTERPRETATION_QUEUED"
        if conditional_rule
        else "KANBAN_QUEUED",
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
    conditional_rule = result.get("rule_active") is not None
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
            "steps": (
                ["Deterministic source verification", "Durable PAPER rule activation"]
                if conditional_rule
                else [
                    "Deterministic source verification",
                    "PAPER OMS submission and tracking",
                ]
            ),
            "qa_required": False,
            "summary": answer,
        },
        "session_id": None,
        "order_request_id": str(result.get("order_request_id") or ""),
        "order_state": str(result.get("request_state") or "UNKNOWN"),
        "order_mode": "PAPER",
        "conditional_rule": conditional_rule,
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
            "For one otherwise complete PAPER PLACE_ORDER (BUY or SELL) with no price and no explicit",
            "market/limit marker, apply the managed omission default: order_type=MARKET,",
            "limit_price=null, and no ORDER_TYPE evidence. A limit marker without exactly",
            "one valid price, or conflicting market/limit language, must CLARIFY.",
            "For strict PAPER baskets, support only '종목A, 종목B N만원씩 매수',",
            "'종목A N주, 종목B M주 [시장가] 매수/매도', or",
            "'종목A N만원, 종목B M만원 [시장가] 매수'. Preserve every listed",
            "instrument mention in order. The equal-notional form is BUY only with",
            "notional_krw=N*10000 and empty basket arrays; the quantity form has one",
            "exact side and aligned positive integer basket_quantities; the member",
            "notional BUY form has aligned positive basket_notionals_krw. Both set",
            "notional_krw=null. All basket forms are MARKET only and non-atomic;",
            "A single explicit BUY amount such as '삼성전자 100만원어치 매수' uses",
            "the same PLACE_BASKET allocation schema with exactly one member; preserve",
            "the exact mention and notional_krw, and never calculate shares yourself.",
            "members are catalog-resolved and tracked individually. Never infer a",
            "missing member, mixed side, or price/limit basket.",
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


def _deterministic_delayed_order_child_body(
    *,
    query: str,
    scope: UserPaperOrderScope,
    root_task_id: str,
    request_id: str,
    has_mandate: bool,
) -> str:
    """Describe a deterministic time rule on the existing Trading card."""

    body = "\n".join(
        (
            "hgfinance.user-conditional-paper-rule.v1",
            build_user_paper_order_scope(scope),
            "authority=server_verified_paper_only",
            "execution_mode=PAPER_ONLY",
            "interpreter=DETERMINISTIC_RELATIVE_TIME",
            "runtime=EXISTING_CONDITIONAL_RULE_WORKER",
            "repeat_policy=ONCE",
            "No Hermes interpretation or additional department task is required.",
            "At the trigger window, the existing deterministic guard rechecks fresh",
            "market data, market session, mandate, funds/position, and idempotency.",
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
    """Build the prompt vocabulary from the registry, never from a static list.

    Names alone left the interpreter guessing parameter spellings, so Korean
    HTS notation such as ``bollingerband(종가,2,0,20)`` produced invented keys
    that the deterministic validator could only answer with
    ``UNSUPPORTED_INDICATOR_PARAMETER``.  Emitting each indicator's exact
    parameter names, defaults, and outputs removes the guess.
    """

    from orchestration.conditional_rules import list_supported_indicators

    entries = []
    for item in list_supported_indicators():
        parameters = [
            f"{key}={value}" for key, value in item["defaults"].items()
        ] + [
            f"{key}=required"
            for key in item["required_parameters"]
            if key not in item["defaults"]
        ]
        entries.append(
            f"{item['name']}({','.join(parameters)})"
            f"->{'|'.join(item['outputs'])}"
        )
    return ", ".join(entries)


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
            "If an 상승/하락/오름/내림 phrase has no numeric threshold, return",
            "candidate=null and clarification_reason=CONDITION_THRESHOLD_REQUIRED.",
            "If a condition expression or indicator name appears misspelled or cannot",
            "be matched exactly, do not normalize it: return candidate=null and",
            "clarification_reason=CONDITION_EXPRESSION_CLARIFICATION_REQUIRED.",
            "clarification_reason carries only registered codes; join several with",
            "'; ' and never invent a new code name. Chaining entry, partial",
            "take-profit, and a remainder exit as linked stages of one position is",
            "UNSUPPORTED_MULTI_STAGE_POSITION_MANAGEMENT; a trailing stop OR'd with a",
            "moving-average exit is UNSUPPORTED_COMBINED_TRAILING_OR_MOVING_AVERAGE_EXIT.",
            "For one action, pass candidate. For 2-10 independent conditional actions,",
            "pass candidates in source-text order and leave candidate=null. A leading",
            "symbol shared by coordinated clauses applies to each clause; never invent",
            "a different symbol. Each candidate must preserve its own comparator,",
            "threshold, side, and sizing. Never collapse branches with different actions",
            "into one LOGICAL OR rule.",
            "The Korean percent spellings %, 퍼, and 퍼센트 mean only a relative move",
            "amount; they do not provide a baseline. For a two-sided sentence such as",
            "'삼성전자 평균 매입가 대비 1퍼 오르면 1주 매도하고 1퍼 내리면 1주 매수',",
            "emit two candidates in source-text order (SELL then BUY), preserving the",
            "same explicit baseline in both conditions. If the baseline is omitted,",
            "return candidate=null and clarification_reason=AMBIGUOUS_RETURN_BASELINE.",
            "Every fixed-share action must have its own explicit 주/주식/개 quantity;",
            "if a BUY quantity is omitted, return QUANTITY_REQUIRED and never assume 1주.",
            "For an existing-position take-profit/stop-loss request that explicitly says",
            "OCO or that one execution cancels the other, pass exactly two candidates in",
            "source-text order and set oco_mode=EXIT_BRACKET on both. Both candidates must",
            "be SELL for the exact same symbol, with identical sizing and expiry. Do not set",
            "oco_group_id: the trusted boundary derives it from the admitted request. If the",
            "user did not explicitly request OCO/cancellation, leave oco_mode unset.",
            "Supported expression node types are LITERAL, TIME, MARKET, PORTFOLIO, INDICATOR,",
            "TRAILING_STOP, TEMPORAL_SEQUENCE, ARITHMETIC, COMPARISON, LOGICAL, NOT, and CROSS.",
            "The executable contract is one instrument and one ONCE action per candidate.",
            "Never approximate unbounded repetition, event-count windows, dynamic universes,",
            "cross-instrument FIRST_OF, consecutive-hold duration, post-order cancel/replace",
            "timers, partial-fill resubmission, or a relative one-tick limit price. Return",
            "candidate=null with a concise unsupported capability reason. Multiple independent",
            "candidates are not a substitute for atomic FIRST_OF or a shared total budget.",
            "A quote comparison is a current state predicate. CROSS is a completed-bar edge",
            "predicate and must never be rewritten as a quote comparison or vice versa.",
            "If an AND condition places mutually exclusive bounds on the same value, preserve",
            "the values; the deterministic validator rejects it as CONTRADICTORY_CONDITION.",
            "User text can never disable risk, freshness, session, authority, audit, version,",
            "or idempotency checks. Do not encode or claim any such override.",
            "Supported indicators follow, each as NAME(PARAMETER=default,...)->OUTPUT|OUTPUT.",
            "Use only these exact parameter names and outputs; never invent, rename,",
            "or translate one, and omit a parameter whose value equals the default.",
            f"{_conditional_rule_indicator_catalog_prompt()}.",
            "Korean HTS notation lists arguments positionally, as in",
            "볼린저밴드(종가,2,0,20) = price source 종가, STDDEV 2, OFFSET 0, PERIOD 20.",
            "Map each argument onto the named parameter it means; OFFSET is the bar",
            "shift back from the latest completed bar and is 0 unless stated. Every",
            "local indicator reads the 종가/CLOSE series, so a 종가 price-source",
            "argument is already the default and adds no parameter. For any other",
            "price source (시가/고가/저가/중간값), return candidate=null with",
            "clarification_reason=UNSUPPORTED_INDICATOR_PRICE_SOURCE.",
            "중심선/중간선 is output=MIDDLE, 상단선 is UPPER, and 하단선 is LOWER.",
            "터치/닿으면 against a band or line means the completed bar spans it, so",
            "build LOGICAL AND of MARKET LOW LTE <line> and MARKET HIGH GTE <line>",
            "on BAR_CLOSE. Never express a touch as COMPARISON EQ: an exact tick",
            "match against a computed band would essentially never trigger.",
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
            "Use comparison operators GT/GTE/LT/LTE/EQ, cross ABOVE/BELOW, and logical",
            "AND/OR. Executable completed-bar timeframes are 1M/3M/5M/10M/15M/30M/1H/1D.",
            "Never rewrite an explicit timeframe. In a multi-timeframe BAR_CLOSE rule,",
            "primary_timeframe is the fastest trigger cadence and every indicator carries",
            "its own explicit timeframe. The worker uses only each timeframe's latest",
            "completed candle whose close is at or before the primary candle close.",
            "CROSS operands must have the same timeframe; use LOGICAL AND for a faster",
            "entry cross plus a slower trend/momentum confirmation. For 3분봉 60일선",
            "or N선/N일선, use PERIOD=N on timeframe=3M; CROSS ABOVE compares completed",
            "MARKET CLOSE against INDICATOR SMA. If the buy rule omits a quantity,",
            "return candidate=null with clarification_reason=QUANTITY_REQUIRED;",
            "never assume 1주.",
            "If an indicator timeframe is omitted, use 1D completed bars and BAR_CLOSE; never",
            "default an explicit intraday phrase to another timeframe. Portfolio/last-price-only rules use",
            "QUOTE. POSITION_PERCENT is a ratio in (0,1], FIXED_SHARES is an integer,",
            "and ALL is sell-only. NOTIONAL_KRW is a positive whole-KRW maximum order",
            "amount for MARKET only: use it only when the user explicitly binds an",
            "amount to the order verb, such as '100만원 시장가 매수', '100만원어치',",
            "or '50만원만큼'. At trigger time the server floors that amount by the",
            "fresh price and lot size; it must never invent a share",
            "quantity. BUY supports FIXED_SHARES, NOTIONAL_KRW, or the explicitly bounded",
            "AVAILABLE_CASH_PERCENT_CAPPED policy described below. Omit expires_at to use",
            "the trusted KRX regular-session close default; do not claim the rule lasts until cancelled.",
            "Use the trusted max_data_age_seconds=30 default; never reduce it unless the user explicitly asks.",
            "CROSS always requires BAR_CLOSE and an explicit primary_timeframe; when the",
            "instruction gives no timeframe for a price-only cross, return candidate=null",
            "with clarification_reason=TIMEFRAME_REQUIRED_FOR_CROSS instead of guessing.",
            "Example: '하이닉스 3분봉 5선이 20선 상향 돌파하고 15분봉 RSI(14)가 70",
            "미만이면 2주 시장가 매수' is one LOGICAL AND: CROSS ABOVE of SMA(5,3M)",
            "and SMA(20,3M), plus RSI(14,15M) LT 70; evaluation={clock:BAR_CLOSE,",
            "primary_timeframe:3M}. Do not use CROSS between a 3M value and a 15M value.",
            "The exact Korean cadence 60분봉 maps to canonical timeframe=1H; this is the",
            "same sixty-minute completed candle, not a fallback to a different cadence.",
            "For '60분봉 20이평 상향 돌파 + RSI(14) 50 이상 + 거래량이 20봉",
            "평균의 1.5배 이상', use one 1H LOGICAL AND: MARKET CLOSE CROSS ABOVE",
            "SMA(PERIOD=20), RSI(PERIOD=14) GTE 50, and MARKET VOLUME GTE",
            "VOLUME_AVERAGE(PERIOD=20) MUL 1.5. Preserve the exact period and factor.",
            "For A + B 또는 C, preserve Korean grouping as LOGICAL OR(LOGICAL AND(A,B),C);",
            "do not flatten it into AND(A,OR(B,C)) or discard a branch. A bare '거래량 2배'",
            "in this indicator context means current completed-bar VOLUME GTE the same",
            "timeframe's VOLUME_AVERAGE(PERIOD=20) MUL 2.",
            "When a faster primary rule needs a slower completed candle's close, never use",
            "MARKET CLOSE for the slower frame because MARKET belongs to primary_timeframe.",
            "Represent an explicit daily close as local SMA(PERIOD=1,timeframe=1D). Thus",
            "'일봉이 20일 이평 위 + 5분봉 RSI가 30 재돌파' is LOGICAL AND of",
            "SMA(1,1D) GT SMA(20,1D) and RSI(14,5M) CROSS ABOVE literal 30, with",
            "primary_timeframe=5M. '재돌파' is an edge CROSS, not a level comparison.",
            "For '포트폴리오 비중 20% 초과 ... 초과 비중 매도', use SELL sizing",
            "{type:TARGET_POSITION_WEIGHT,value:0.20}. The worker computes only the",
            "whole-share excess above NAV*0.20 from the same fresh portfolio snapshot.",
            "For '가용 현금의 10%를 매수하되 최대 주문금액 100만원', use BUY sizing",
            "{type:AVAILABLE_CASH_PERCENT_CAPPED,value:0.10,cap_krw:1000000} and MARKET.",
            "The executable notional is min(fresh available cash*0.10,1000000), floored",
            "to whole shares; preserve both source values exactly and never convert this",
            "into a fixed NOTIONAL_KRW or a guessed share count.",
            "For a bounded sequence such as 'RSI가 30 하회한 이후 20봉 이내 20이평을",
            "상향 돌파하면 5주 매수, 그 전에 RSI 70 돌파 시 조건 취소', use one root",
            "TEMPORAL_SEQUENCE with parameters={WINDOW_BARS:20} and exactly three children",
            "in order: ARM=RSI(14) CROSS BELOW 30, TRIGGER=MARKET CLOSE CROSS ABOVE",
            "SMA(20), CANCEL=RSI(14) CROSS ABOVE 70. Use one explicit completed-bar",
            "timeframe for all three and BAR_CLOSE at that timeframe. The server persists",
            "armed progress by rule version; cancellation wins if cancel and trigger are true",
            "on the same completed bar. Never flatten this sequence into LOGICAL AND/OR.",
            "An explicit KST intraday time window may be combined with either BAR_CLOSE",
            "or QUOTE conditions. For a clear 24-hour window such as '10:00~14:30에만',",
            "add LOGICAL AND children {type:COMPARISON,operator:GTE,left:{type:TIME,",
            "field:KST_SECONDS_SINCE_MIDNIGHT},right:{type:LITERAL,value:36000,unit:NUMBER}}",
            "and the same TIME node with operator=LTE, value=52200. The time field may",
            "only be directly compared to an integer seconds literal; never use arithmetic",
            "or CROSS. '오전 10시부터 오후 2시 30분까지' is equivalent. Do not infer AM/PM",
            "from an ambiguous '2시'; return candidate=null with TIME_WINDOW_AM_PM_REQUIRED.",
            "The worker still rejects a closed/unavailable market even if the window is true.",
            "For an explicit existing-position trailing exit such as '평균 매입가 대비 2%",
            "수익이 난 뒤 고점 대비 1% 하락하면 전량 매도', use the complete root condition",
            "{type:TRAILING_STOP,parameters:{DRAWDOWN:0.01,ACTIVATION_RETURN:0.02}},",
            "action SELL, and evaluation={clock:QUOTE}. DRAWDOWN is required; ratios are",
            "decimal fractions. The server, not Hermes, persists the highest fresh quote after",
            "ACTIVE and ignores late quotes. Do not combine TRAILING_STOP with AND/OR, a time",
            "window, or a BAR_CLOSE condition in this version. Do not infer a trailing stop",
            "from an ordinary fixed-price stop-loss request.",
            "For '수익률이 15%를 넘은 뒤 최고 수익률 대비 5%p 하락' use "
            "DRAWDOWN:0.05, ACTIVATION_RETURN:0.15, and DRAWDOWN_MODE:RETURN_POINTS. "
            "RETURN_POINTS fixes the cost basis when this rule first observes a quote, so "
            "later portfolio cost-basis changes cannot change the promised %p threshold.",
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
    except Exception:
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
        raise HTTPException(
            status_code=503, detail="paper_order_hermes_runtime_unavailable"
        )

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
        raise HTTPException(
            status_code=409, detail="paper_order_request_id_conflict"
        ) from exc
    except UserOrderWorkflowUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="paper_order_workflow_unavailable"
        ) from exc

    if record.ceo_root_task_id:
        existing_root = hermes_boundary.show_kanban_task(record.ceo_root_task_id)
        if existing_root is None:
            raise HTTPException(
                status_code=503, detail="paper_order_kanban_unavailable"
            )
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
        source=getattr(req, "source", None),
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
        previous_question_context=getattr(req, "previous_question_context", None),
        previous_question_context_source_message_id=getattr(
            req, "previous_question_context_source_message_id", None
        ),
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
            raise HTTPException(
                status_code=503, detail="paper_order_kanban_unavailable"
            )
        root_task_id = str(root["task_id"])
        if not hermes_boundary.comment_root_scope(
            task_id=root_task_id, request_id=req.request_id
        ):
            raise HTTPException(
                status_code=503, detail="paper_order_kanban_unavailable"
            )
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
        raise HTTPException(
            status_code=503, detail="paper_order_workflow_unavailable"
        ) from exc
    except Exception as exc:
        _mark_paper_order_failed(
            repository,
            record.order_request_id,
            error_code="ANALYSIS_ROOT_CREATE_FAILED",
            error_message=type(exc).__name__,
        )
        raise HTTPException(
            status_code=503, detail="paper_order_kanban_unavailable"
        ) from exc

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
    discord_channel_id: str | None = None,
    discord_message_id: str | None = None,
    discord_guild_id: str | None = None,
    discord_thread_id: str | None = None,
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
        raise HTTPException(
            status_code=503, detail="paper_order_hermes_runtime_unavailable"
        )

    # These imports stay local so the normal CEO analysis path does not acquire
    # conditional-rule dependencies when it is not an order request.
    from orchestration.conditional_rules import RuleState

    from .conditional_rule_workflow import (
        ConditionalRuleConflict,
        ConditionalRuleUnavailable,
        conditional_rule_repository,
    )
    from .conditional_rules import (
        ConditionalRuleCandidate,
        ConditionalRulePreviewRequest,
        _build_preview,
    )
    from .paper_order_bundle import (
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
            except Exception:
                logger.exception("compound PAPER admission cleanup failed")
        if rule is not None:
            try:
                conditional_rule_repository().transition(
                    rule.rule_id,
                    user_id=access["user_id"],
                    target=RuleState.CANCELLED,
                )
            except Exception:
                logger.exception("compound PAPER rule cleanup failed")
        if "immediate_record" in locals():
            try:
                orders.mark_outcome(
                    immediate_record.order_request_id,
                    state="FAILED",
                    error_code="COMPOUND_ADMISSION_FAILED",
                    error_message="compound PAPER bundle was not fully admitted",
                )
            except Exception:
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
            discord_channel_id=discord_channel_id,
            discord_message_id=discord_message_id,
            discord_guild_id=discord_guild_id,
            discord_thread_id=discord_thread_id,
        )
    except Exception as exc:
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
        except Exception:
            logger.exception(
                "compound PAPER cleanup failed bundle=%s", bundle.bundle_id
            )
        raise

    return {
        "schema_version": _PLANNING_SCHEMA_VERSION,
        "department": "ceo-agent",
        "binding": False,
        "task_id": str(immediate_response.get("task_id") or ""),
        "task": immediate_response.get("task") or {},
        "status": "planned",
        # The trigger was hard-coded to one example price, so every compound
        # order was reported back as "265,000원 초과" no matter what the user
        # actually asked for (2026-08-27).  Describe the rule that was stored.
        "answer": (
            "PAPER 매수 주문을 기존 Trading 경로로 접수했습니다. 매수 수량이 전량 "
            f"체결된 뒤 기존 조건주문 worker가 {plan.conditional_instruction} 규칙을 "
            "자동 활성화합니다. 부분체결·실패 시 조건주문은 활성화하지 않습니다."
            + (
                " 전량 체결 시점부터 공식 KRX 정규장 캘린더 기준 "
                f"{plan.exit_lifetime_trading_days}거래일째 마감까지 추적합니다."
                if plan.exit_lifetime_trading_days is not None
                else ""
            )
            + (
                " 익절·손절은 두 개의 독립 매도 주문이 아니라 하나의 OR 청산 규칙이므로, "
                "둘 중 먼저 충족한 조건에서만 1회 PAPER 시장가 청산을 시도합니다. "
                "기존 같은 종목 보유분과 섞여 보유수량이 이번 매수 수량과 다르면 "
                "기준가 왜곡을 막기 위해 청산하지 않습니다."
                if plan.is_entry_exit_bracket
                else (
                    " 수익 활성 구간에 도달한 뒤의 신선한 현재가 최고가만 DB에 보존하고, "
                    "그 고점 대비 지정한 비율만큼 하락할 때 1회 PAPER 시장가 청산을 "
                    "시도합니다. 기존 같은 종목 보유분과 섞여 보유수량이 이번 매수 수량과 "
                    "다르면 고점 추적과 청산을 시작하지 않습니다."
                    if plan.is_entry_trailing_stop
                    else ""
                )
            )
        ),
        "planning": {
            "schema_version": "ceo.planning.v1",
            "selected_departments": ["trading-department"],
            "steps": [
                "Existing PAPER buy directive",
                "Wait for full fill",
                (
                    "Activate one atomic take-profit/stop-loss exit rule"
                    if plan.is_entry_exit_bracket
                    else (
                        "Activate one durable entry-relative trailing exit rule"
                        if plan.is_entry_trailing_stop
                        else "Activate existing conditional rule"
                    )
                ),
            ],
            "qa_required": False,
            "summary": "기존 PAPER 주문과 조건주문 경로를 하나의 durable bundle로 연결했습니다.",
        },
        "session_id": None,
        "order_request_id": immediate_record.order_request_id,
        "order_state": bundle.state,
        "order_mode": "PAPER",
        "compound_paper_order": True,
        "entry_exit_bracket": plan.is_entry_exit_bracket,
        "entry_trailing_stop": plan.is_entry_trailing_stop,
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
    langsmith_trace_run_id: str | None = None,
    discord_channel_id: str | None = None,
    discord_message_id: str | None = None,
    discord_guild_id: str | None = None,
    discord_thread_id: str | None = None,
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
    delayed_plan = None
    if not conditional_rule and _deterministic_paper_order_fast_path_enabled():
        deterministic_candidate = deterministic_order_candidate(req.query)
    elif conditional_rule and _deterministic_paper_order_fast_path_enabled():
        delayed_plan = deterministic_delayed_order_plan(req.query)

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
            raise HTTPException(
                status_code=409, detail="paper_order_admission_mismatch"
            )
    admitted_at = time.monotonic()
    delayed_candidate = (
        build_delayed_order_candidate(delayed_plan, admitted_at=record.created_at)
        if delayed_plan is not None
        else None
    )

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
            source=getattr(req, "source", None),
            mandate=mandate,
            requested_by=access["user_id"],
            user_paper_order_scope=scope,
            discord_channel_id=discord_channel_id,
            discord_message_id=discord_message_id,
            discord_guild_id=discord_guild_id,
            discord_thread_id=discord_thread_id,
            langsmith_trace_context=langsmith_trace_context,
            langsmith_trace_run_id=langsmith_trace_run_id,
            previous_question_context=getattr(req, "previous_question_context", None),
            previous_question_context_source_message_id=getattr(
                req, "previous_question_context_source_message_id", None
            ),
        ),
        idempotency_key=req.request_id,
        # Hermes' ``--initial-status running`` is a historical CLI spelling
        # that creates a *ready* card.  A dispatcher can therefore claim it
        # before this function finishes the SQL/Kanban bindings.  The root is
        # only an immutable scope container, so park it blocked and complete
        # it in place after the bindings are durable.
        initial_status="blocked",
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
            "사용자 PAPER 예약주문 결정론적 활성화"
            if delayed_candidate is not None
            else "사용자 PAPER 조건주문 AST 해석 및 즉시 활성화"
            if conditional_rule
            else (
                "사용자 PAPER 주문 결정론적 검증·제출"
                if deterministic_candidate is not None
                else "사용자 PAPER 주문 원문 해석 및 검증 제출"
            )
        ),
        body=(
            _deterministic_delayed_order_child_body(
                query=req.query,
                scope=scope,
                root_task_id=root_task_id,
                request_id=req.request_id,
                has_mandate=bool(mandate),
            )
            if delayed_candidate is not None
            else _conditional_rule_child_body(
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
        idempotency_key=primary_idempotency_key(root_task_id, "trading-department"),
        # Keep every primary blocked while its authority binding is assembled.
        # The asynchronous interpreter is explicitly released below. A future
        # synchronous deterministic lane must also execute from this parked
        # state rather than racing the dispatcher.
        initial_status="blocked",
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

    # Both cards were created blocked. The root is only an immutable
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

    if deterministic_candidate is not None or delayed_candidate is not None:
        execution_started_at = time.monotonic()
        try:
            if delayed_candidate is not None:
                execution = process_user_conditional_paper_rule(
                    root_task_id=root_task_id,
                    trading_task_id=trading_task_id,
                    candidate=delayed_candidate,
                    interpretation_source="DETERMINISTIC",
                )
                execution = {
                    **execution,
                    "order_request_id": record.order_request_id,
                    "request_state": "COMPLETED",
                }
            else:
                execution = process_deterministic_user_paper_order(
                    root_task_id=root_task_id,
                    trading_task_id=trading_task_id,
                    interpretation=deterministic_candidate.model_dump(mode="json"),
                )
        except Exception as exc:  # noqa: BLE001 - unknown commit must not be retried.
            logger.error(
                "paper-order-synchronous-failed request=%s root=%s trading=%s "
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
                    error_code="SYNCHRONOUS_EXECUTION_UNAVAILABLE",
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
                "reason_codes": ["SYNCHRONOUS_EXECUTION_UNAVAILABLE"],
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
            "paper-order-synchronous-complete request=%s root=%s trading=%s "
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
    discord_channel_id: str | None = None,
    discord_message_id: str | None = None,
    discord_guild_id: str | None = None,
    discord_thread_id: str | None = None,
) -> dict[str, object]:
    """Route the direct PAPER lane with the same redacted root trace as analysis."""

    root_trace = None
    try:
        from orchestration.llm_observability import start_root_trace

        root_trace = start_root_trace(
            request_id=req.request_id,
            workflow_mode="binding",
            source=getattr(req, "source", None),
            query=req.query,
        )
    except Exception:  # noqa: BLE001 - observability remains fail-open.
        root_trace = None
    try:
        response = _route_user_paper_order(
            req,
            owner_id=owner_id,
            mandate=mandate,
            conditional_rule=conditional_rule,
            langsmith_trace_context=(
                root_trace.context if root_trace is not None else None
            ),
            langsmith_trace_run_id=(
                getattr(root_trace, "run_id", None) if root_trace is not None else None
            ),
            discord_channel_id=discord_channel_id,
            discord_message_id=discord_message_id,
            discord_guild_id=discord_guild_id,
            discord_thread_id=discord_thread_id,
        )
        if root_trace is not None:
            terminal_trace = _paper_order_terminal_trace(
                response,
                request_id=req.request_id,
            )
            if terminal_trace is not None:
                status, metadata, output_summary = terminal_trace
                try:
                    from orchestration.llm_observability import close_root_trace

                    close_root_trace(
                        root_trace.context,
                        run_id=getattr(root_trace, "run_id", None),
                        request_id=req.request_id,
                        root_id=str(response.get("task_id") or "") or None,
                        task_id=str(response.get("trading_task_id") or "") or None,
                        department="trading",
                        workflow_mode="binding",
                        source=getattr(req, "source", None),
                        status=status,
                        terminal_metadata=metadata,
                        output_summary=output_summary,
                    )
                except Exception as exc:  # noqa: BLE001 - tracing remains fail-open.
                    logger.debug(
                        "paper_root_trace_close_failed error=%s",
                        type(exc).__name__,
                    )
        return response
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
                    run_id=getattr(root_trace, "run_id", None),
                    terminal_metadata=_trace_error_metadata(exc),
                )
            except Exception:  # noqa: BLE001, S110 - tracing cannot mask route failure.
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
    deterministic_routing_plan: Mapping[str, object] | None = None,
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

    # 레인 판정은 `classify_ceo_request()` 한 곳이 소유한다.
    #
    # 예전에는 이 자리에서 순차 if 체인으로 여섯 가지 검사를 직접 했다.
    # 순서가 곧 우선순위인데 그 순서가 어디에도 적혀 있지 않았고, 부정 가드가
    # 즉시 주문 레인에만 걸려 있었다 - `"이평 깨지면 매도하지 마"`가 조건주문
    # 레인으로 들어갔던 이유다(2026-08-31 실측). 판정이 한 함수에 모여야
    # 레인 사이의 비대칭이 눈에 보인다.
    #
    # 읽기 전용 E2E 스코프는 CEO가 소유한 판정이므로 여기서 계산해 넘긴다.
    # 이 요청들은 합법적으로 `매도`·`6개월 이내` 같은 어휘를 담고 있어서
    # 주문 문법이 먼저 집어가면 안 된다.
    read_only_hr_e2e = _is_read_only_hr_e2e_request(req.query)
    read_only_risk_e2e = _is_read_only_risk_e2e_request(req.query)
    # "시가총액 상위 10종목 300만원씩 매수"에는 조건이 없다 - "현재 기준"은
    # 지금이다. 그런데 종목이 열거되지 않아 주문 문법이 집지 못했고, 남은
    # 레인이 조건주문이라 실행 시점 동적 유니버스로 읽혀 거부됐다(2026-09-01).
    # 여기서 순위를 한 번 읽어 평범한 열거 문장으로 바꾸면, 라우팅·검증·admission이
    # 손으로 적은 목록과 완전히 같은 경로를 탄다. 확장에 실패하면 문장을 그대로
    # 두어 기존 거부 사유가 그대로 남는다.
    req = _expand_dynamic_universe_request(req)
    route = classify_ceo_request(
        req.query,
        previous_question_context=getattr(req, "previous_question_context", None),
        routing_plan=deterministic_routing_plan,
        read_only_hr_e2e=read_only_hr_e2e,
        read_only_risk_e2e=read_only_risk_e2e,
    )
    deterministic_routing_plan = dict(route.routing_plan)
    workflow_mode = route.workflow_mode

    if route.lane == "no_action":
        return _no_action_response()

    # Consult D5 before the clarification branch as well.  Ambiguity is a
    # terminal, safe outcome, but its structured failure history is still
    # useful to explain why no department fan-out is being guessed.  This is
    # read-only and bounded; it must never turn a filler message into a risky
    # route on its own.
    d5_bank = ExperienceBank.from_env()
    d5_lookup = None
    if d5_bank.enabled and not route.order_grammar_detected:
        d5_lookup = d5_bank.lookup(
            case_type=discord_experience_case_type(
                deterministic_routing_plan.get("category")
            ),
            binding=workflow_mode == "binding",
            correlation_id=req.request_id,
        )
    d5_failure_memory = (
        d5_lookup.failure_memory
        if d5_lookup is not None and d5_bank.mode == "active"
        else None
    )

    if route.lane == "clarification":
        return _clarification_required_response(
            req,
            owner_id=owner_id if isinstance(owner_id, str) else None,
            mandate=mandate,
            discord_channel_id=discord_channel_id,
            discord_message_id=discord_message_id,
            discord_guild_id=discord_guild_id,
            discord_thread_id=discord_thread_id,
            routing_plan=deterministic_routing_plan,
            failure_memory=d5_failure_memory,
        )

    if route.lane == "analysis_then_order":
        return _route_analysis_then_conditional_paper_order(
            req,
            plan=route.order_plan,
            owner_id=owner_id if isinstance(owner_id, str) else None,
            mandate=mandate,
            discord_channel_id=discord_channel_id,
            discord_message_id=discord_message_id,
            discord_guild_id=discord_guild_id,
            discord_thread_id=discord_thread_id,
        )

    if route.lane == "compound_order":
        return _route_compound_user_paper_order(
            req,
            owner_id=owner_id if isinstance(owner_id, str) else None,
            mandate=mandate,
            discord_channel_id=discord_channel_id,
            discord_message_id=discord_message_id,
            discord_guild_id=discord_guild_id,
            discord_thread_id=discord_thread_id,
        )

    if route.lane in {"conditional_order", "immediate_order"}:
        return _route_traced_user_paper_order(
            req,
            owner_id=owner_id if isinstance(owner_id, str) else None,
            mandate=mandate,
            conditional_rule=route.lane == "conditional_order",
            discord_channel_id=discord_channel_id,
            discord_message_id=discord_message_id,
            discord_guild_id=discord_guild_id,
            discord_thread_id=discord_thread_id,
        )

    # 결정론 플랜이 그대로 응답 평면이 되는 경우에만 root body에 싣는다.
    # 읽기 전용 E2E 레인은 CEO가 소유한 처리를 유지하며, 자유 문장에서 온
    # research/risk 기본값을 물려받지 않는다.
    bff_routing_plan = (
        deterministic_routing_plan
        if route.lane in {"department_analysis", "operational_status"}
        else None
    )
    deterministic_operational_status = route.lane == "operational_status"
    selected_bff_profiles = {
        str(profile).strip()
        for profile in (
            bff_routing_plan.get("selected_primary_profiles", ())
            if isinstance(bff_routing_plan, Mapping)
            else ()
        )
        if str(profile).strip()
    }
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
    ceo_self_improvement = None
    if d5_bank.mode == "active":
        try:
            # CEO self-improvement is a local, payload-free read of the
            # verified D5 ledger. It does not call LangSmith and cannot mutate
            # routing, skills, mandates, or authority gates on the hot path.
            from orchestration.d5_improvement_pipeline import (
                build_ceo_self_improvement_hint,
                d5_feedback_ledger_from_env,
            )

            ceo_self_improvement = build_ceo_self_improvement_hint(
                d5_feedback_ledger_from_env()
            )
        except Exception:  # noqa: BLE001 - self-improvement is advisory/fail-open.
            ceo_self_improvement = None
    root_trace = None
    try:
        from orchestration.llm_observability import start_root_trace

        root_trace = start_root_trace(
            request_id=req.request_id,
            workflow_mode=workflow_mode,
            source=getattr(req, "source", None),
            query=req.query,
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
                source=getattr(req, "source", None),
                mandate=mandate,
                requested_by=owner_id,
                discord_channel_id=discord_channel_id,
                discord_message_id=discord_message_id,
                discord_guild_id=discord_guild_id,
                discord_thread_id=discord_thread_id,
                langsmith_trace_context=(
                    root_trace.context if root_trace is not None else None
                ),
                langsmith_trace_run_id=(
                    getattr(root_trace, "run_id", None)
                    if root_trace is not None
                    else None
                ),
                # HR E2E is an explicit Workforce read-only lane. Do not
                # attach the unrelated default Accounting snapshot to its
                # root: it needlessly enlarges every CEO/QA prompt and can
                # expose irrelevant portfolio detail to an HR worker.
                advisory_fund_id=(
                    None if read_only_hr_e2e else getattr(req, "fund_id", None)
                ),
                advisory_book_id=(
                    None if read_only_hr_e2e else getattr(req, "book_id", None)
                ),
                previous_question_context=getattr(
                    req, "previous_question_context", None
                ),
                previous_question_context_source_message_id=getattr(
                    req, "previous_question_context_source_message_id", None
                ),
                experience_hint=d5_hint,
                approved_feedback_hint=approved_feedback,
                ceo_self_improvement_hint=ceo_self_improvement,
                include_accounting_advisory=(
                    not read_only_hr_e2e
                    and not read_only_risk_e2e
                    and (
                        not selected_bff_profiles
                        or canonical_profile_for_department("accounting")
                        in selected_bff_profiles
                    )
                ),
                producer=(
                    str(bff_routing_plan.get("producer") or "")
                    if bff_routing_plan
                    else None
                ),
                selected_primary_profiles=(
                    bff_routing_plan.get("selected_primary_profiles")
                    if bff_routing_plan
                    else None
                ),
                delegation_instructions=(
                    bff_routing_plan.get("delegation_instructions")
                    if bff_routing_plan
                    else None
                ),
                analysis_mode=(
                    str(bff_routing_plan.get("analysis_mode") or "")
                    if bff_routing_plan
                    else None
                ),
                routing_basis=(
                    str(bff_routing_plan.get("routing_basis") or "")
                    if bff_routing_plan
                    else None
                ),
                routing_category=(
                    str(bff_routing_plan.get("category") or "")
                    if bff_routing_plan
                    else None
                ),
            ),
            idempotency_key=req.request_id,
            # The operational status lane has no LLM primary. Keep its root
            # blocked until the existing deterministic completion boundary
            # records the planning result, so the dispatcher cannot claim it
            # and spend a CEO model turn rediscovering the same route. Omit
            # the optional argument entirely for normal user-query roots.
            **(
                {"initial_status": "blocked"}
                if deterministic_operational_status
                else {}
            ),
        )
    except Exception as exc:
        if root_trace is not None:
            from orchestration.llm_observability import close_root_trace

            close_root_trace(
                root_trace.context,
                run_id=getattr(root_trace, "run_id", None),
                request_id=req.request_id,
                workflow_mode=workflow_mode,
                source=getattr(req, "source", None),
                status="error",
                error_class=type(exc).__name__,
                terminal_metadata=_trace_error_metadata(exc),
            )
        raise
    if not task or not task.get("task_id"):
        if root_trace is not None:
            from orchestration.llm_observability import close_root_trace

            close_root_trace(
                root_trace.context,
                run_id=getattr(root_trace, "run_id", None),
                request_id=req.request_id,
                workflow_mode=workflow_mode,
                source=getattr(req, "source", None),
                status="error",
                error_class="root_create_failed",
                terminal_metadata={"error_code": "root_create_failed"},
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
        failure_metadata = bounded_failure_memory_hint(d5_lookup.failure_memory)
        if failure_metadata:
            logger.warning(
                "event=memo_harness_d5_failure_memory root_id=%s matched_failures=%s "
                "failure_code_count=%s failed_department_set_count=%s",
                root_id,
                failure_metadata.get("matched_failures", 0),
                len(failure_metadata.get("failure_codes", ()))
                if isinstance(failure_metadata.get("failure_codes"), list)
                else 0,
                len(failure_metadata.get("failed_department_sets", ()))
                if isinstance(failure_metadata.get("failed_department_sets"), list)
                else 0,
            )
        logger.info(
            "ceo-planning root=%s request_id=%s producer=portfolio-bff",
            str(task["task_id"]),
            req.request_id,
        )
        guardrails = (
            ceo_self_improvement.get("guardrails")
            if isinstance(ceo_self_improvement, Mapping)
            else ()
        )
        logger.info(
            "event=ceo_self_review_guardrails root_id=%s present=%s count=%d "
            "source=verified_d5 raw_payloads_sent=false",
            str(task["task_id"]),
            str(bool(guardrails)).lower(),
            min(len(guardrails), 8) if isinstance(guardrails, list) else 0,
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
                run_id=getattr(root_trace, "run_id", None),
                request_id=req.request_id,
                root_id=str(task["task_id"]),
                workflow_mode=workflow_mode,
                source=getattr(req, "source", None),
                status="error",
                error_class="root_scope_failed",
                terminal_metadata={"error_code": "root_scope_failed"},
            )
        raise HTTPException(
            status_code=503,
            detail="CEO root Kanban scope를 기록하지 못했습니다. 재시도하세요.",
        )
    if deterministic_operational_status:
        root_id = str(task["task_id"])
        completed = hermes_boundary.complete_kanban_task(
            task_id=root_id,
            result=(
                "운영 상태 조회를 결정론적 read-only 경로로 접수했습니다. "
                "시장 Research/Risk LLM primary는 호출하지 않습니다."
            ),
        )
        if completed:
            logger.info(
                "ceo-operational-status-deterministic-completed root=%s",
                root_id,
            )
        else:
            # A CLI timeout has unknown commit status. The completion helper
            # already verifies terminal state; only reopen a positively
            # observed blocked root so a transient CLI failure cannot strand
            # the user request forever.
            current = hermes_boundary.show_kanban_task(
                root_id, timeout=_planning_read_timeout()
            )
            current_status = (
                str(current.get("status") or "").casefold()
                if isinstance(current, Mapping)
                else ""
            )
            if current_status not in {"done", "completed", "archived"}:
                if not hermes_boundary.unblock_kanban_task(task_id=root_id):
                    logger.warning(
                        "ceo-operational-status-root-release-failed root=%s",
                        root_id,
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
        raise HTTPException(
            status_code=404, detail="CEO Kanban task를 찾을 수 없습니다."
        )
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
        completed_at=workflow.completed_at,
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
        listing = list_ceo_roots(
            limit=limit,
            include_archived=include_archived,
            owner_id=normalized_owner_id,
            with_board_rows=True,
        )
    except KanbanUnavailable as exc:
        raise HTTPException(
            status_code=503, detail=f"Hermes Kanban을 읽지 못했습니다: {exc}"
        ) from exc
    if isinstance(listing, tuple):
        rows, board_rows = listing
    else:
        # Keep direct/unit-test fixtures and older integrations that patch or
        # call the root-list function returning only roots compatible.
        rows = listing
        board_rows = rows
    identified = [
        (str(row.get("id") or row.get("task_id") or ""), str(row.get("body") or ""))
        for row in rows
    ]
    identified = [(task_id, body) for task_id, body in identified if task_id]
    if not identified:
        return TaskListResponse(items=[])

    # Normal Hermes list rows already contain everything needed by the bounded
    # history projection. Only legacy/minimal rows go through the expensive
    # graph reader, which keeps `/ui/ceo/tasks?limit=100` usable while the
    # detailed task/graph endpoints retain their full workflow semantics.
    projected: dict[str, TaskListItem] = {}
    fallback: list[tuple[str, str]] = []
    row_by_id = {str(row.get("id") or row.get("task_id") or ""): row for row in rows}
    for task_id, body in identified:
        item = _task_list_item_from_row(row_by_id.get(task_id, {}))
        if item is None:
            fallback.append((task_id, body))
        else:
            projected[task_id] = item

    if not fallback:
        return TaskListResponse(
            items=[projected[task_id] for task_id, _body in identified]
        )

    with ThreadPoolExecutor(max_workers=_LIST_WORKERS) as pool:
        workflows = list(
            pool.map(
                lambda item: _load(
                    item[0],
                    max_workers=_LIST_GRAPH_WORKERS,
                    listed_rows=board_rows,
                    known_root=True,
                ),
                fallback,
            )
        )
    for (task_id, body), workflow in zip(fallback, workflows, strict=True):
        projected[task_id] = TaskListItem(
            task_id=workflow.root_task_id,
            query=extract_user_query(body),
            status=workflow.status,
            created_at=workflow.root.created_at,
            selected_departments=list(workflow.selected_departments),
            owner_id=requested_by_from_body(body),
        )
    return TaskListResponse(items=[projected[task_id] for task_id, _body in identified])


@router.get(
    "/kanban", operation_id="ceo_kanban_board", response_model=KanbanBoardResponse
)
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


@router.get(
    "/tasks/{task_id}",
    operation_id="ceo_task_status",
    response_model=TaskStatusResponse,
)
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
        raw = hermes_boundary.show_kanban_task(
            task_id, timeout=_planning_read_timeout()
        )
        if not raw:
            raise
        _require_ceo_task_owner(str(raw.get("body") or ""), authenticated_owner_id)
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
    # ``load_workflow`` already hydrated the root and keeps its authoritative
    # raw projection. Reusing it avoids a second identical ``kanban show`` on
    # every status poll. Older test/fixture readers may not expose it, so keep
    # the bounded CLI fallback for that compatibility boundary only.
    raw = workflow.root_payload
    if isinstance(raw, Mapping):
        payload["planning"] = _planning_acknowledgement(
            _hydrated_planning_projection(workflow)
        )["planning"]
    else:
        try:
            raw = hermes_boundary.show_kanban_task(
                task_id, timeout=_planning_read_timeout()
            )
            if raw:
                payload["planning"] = _planning_acknowledgement(
                    _scoped_planning_projection(
                        raw, timeout=_planning_read_timeout()
                    )
                )["planning"]
        except (KanbanTaskNotFound, KanbanUnavailable):
            pass
    return payload


@router.get(
    "/tasks/{task_id}/graph",
    operation_id="ceo_task_graph",
    response_model=TaskGraphResponse,
)
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
            summary=humanize_user_facing_text(synthesis.summary),
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
            summary=humanize_user_facing_text(workflow.root.summary),
            decision=workflow.decision,
            qa_verdict=workflow.qa_verdict,
        )
    return TaskResultResponse(
        task_id=task_id,
        status="completed" if terminal else "processing",
        result=result,
        departments={
            department: humanize_user_facing_text(summary)
            for department, summary in workflow.department_summaries.items()
        },
        qa_verdict=workflow.qa_verdict,
        block_reason=(
            humanize_user_facing_text(workflow.block_reason)
            if workflow.block_reason
            else None
        ),
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
        raise HTTPException(
            status_code=503, detail="Retention 상태를 확인하지 못했습니다."
        ) from exc
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
        raise HTTPException(
            status_code=404, detail=f"Task를 찾을 수 없습니다: {task_id}"
        ) from exc
    except KanbanUnavailable as exc:
        raise HTTPException(
            status_code=503, detail=f"Archive에 실패했습니다: {exc}"
        ) from exc
    return TaskArchiveResponse(task_id=task_id, archived_task_ids=target_ids)


__all__ = ["router"]
