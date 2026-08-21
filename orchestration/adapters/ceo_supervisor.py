"""Deterministic CEO supervisor for Hermes Kanban terminal events.

The supervisor is deliberately a small policy layer around the supported Hermes
Kanban CLI.  Hermes remains the owner of task state, parent dependencies,
worker spawning, and persistence; this module only collects task projections
and chooses the next structured action.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from collections.abc import Callable, Collection, Mapping, Sequence
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any
from pathlib import Path

from orchestration.adapters.terminal_projection_utils import (
    action as terminal_action,
)
from orchestration.adapters.terminal_projection_utils import (
    is_background_research,
)
from orchestration.adapters.terminal_projection_utils import (
    workflow_role as terminal_workflow_role,
)
from orchestration.adapters.terminal_projection_utils import (
    workflow_root as terminal_workflow_root,
)
from orchestration.answer_contract import grade_answer
from orchestration.canonical_profiles import (
    USER_QUERY_PRIORITY,
    CanonicalKanbanTaskRequest,
    CanonicalProfileError,
    canonical_profile_for_department,
    department_for_canonical_profile,
    validate_canonical_profile,
)
from orchestration.ceo_workflow_scope import (
    CEO_WORKFLOW_SCOPE_MARKER,
    WorkflowScopeViolation,
    build_scoped_task_body,
    extract_scope_references,
    is_user_query_body,
    mandate_snapshot_present,
    primary_idempotency_key,
    selected_primary_profiles_from_task,
    user_paper_order_scope_from_body,
    validate_workflow_scope,
    workflow_mode_from_body,
    workflow_role_from_body,
)
from orchestration.discord_delivery import (
    DiscordFinalDelivery,
    correlation_from_task,
)
from orchestration.discord_idempotency import DiscordIdempotencyStore
from orchestration.failure_taxonomy import FailureKind, classify_failure
from orchestration.adapters.department_notion_projection import (
    DepartmentNotionProjection,
)
from orchestration.kanban_retention_lock import workflow_mutation_lock
from orchestration.kanban_root_index import (
    RootScopedIndexUnavailable,
    SQLiteRootScopedIndex,
    kanban_db_path,
)

logger = logging.getLogger(__name__)

_CLI_LANE: ContextVar[str] = ContextVar("ceo_cli_lane", default="unknown")


def _record_full_board_fallback(*, lane: str, reason: str, root_id: str = "") -> None:
    """Record fallback ownership without logging task bodies or prompts."""

    logger.warning(
        "kanban-full-board-fallback lane=%s reason=%s root=%s",
        lane or "unknown",
        reason,
        root_id or "unknown",
    )


@contextmanager
def cli_lane(lane: str):
    """Attach a bounded, non-secret lane label to Hermes CLI diagnostics."""

    normalized = str(lane or "unknown").strip() or "unknown"
    token = _CLI_LANE.set(normalized)
    try:
        yield
    finally:
        _CLI_LANE.reset(token)


def current_cli_lane() -> str:
    return _CLI_LANE.get()


class SupervisorValidationError(ValueError):
    """Raised when an event or structured supervisor action is invalid."""


class HermesKanbanCommandError(RuntimeError):
    """Raised when the Hermes CLI cannot perform a supervisor operation."""


class SupervisorWorkflowError(RuntimeError):
    """A single workflow could not be evaluated; the daemon may continue."""


class SupervisorAction(str, Enum):
    SYNTHESIZE = "SYNTHESIZE"
    CREATE_TASK = "CREATE_TASK"
    RETRY_TASK = "RETRY_TASK"
    REQUEST_USER_INPUT = "REQUEST_USER_INPUT"
    RUN_QA = "RUN_QA"
    BLOCK_ABORT = "BLOCK/ABORT"


TERMINAL_EVENT_KINDS = frozenset(
    {
        "completed",
        "blocked",
        "gave_up",
        "crashed",
        "timed_out",
        "spawn_failed",
    }
)
NON_TERMINAL_EVENT_KINDS = frozenset({"reclaimed", "claim_extended"})
TERMINAL_STATUSES = frozenset(
    {
        "done",
        "completed",
        "archived",
        "blocked",
        "failed",
        "gave_up",
        "crashed",
        "timed_out",
        "spawn_failed",
    }
)
FAILURE_OUTCOMES = frozenset(
    {"gave_up", "crashed", "timed_out", "spawn_failed", "failed"}
)
PRIMARY_DEPARTMENTS = frozenset(
    {"research", "quant", "trading", "risk", "accounting"}
)
SUPERVISOR_MARKER = "hgfinance.ceo-supervisor.v1"
SUPERVISOR_WAKE_MARKER = "hgfinance.ceo-supervisor.wakeup.v1"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("summary", "result", "error", "reason", "message"):
            if value.get(key):
                return str(value[key])
    return str(value)


def _child_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping):
        task_id = value.get("id") or value.get("task_id")
        return str(task_id) if task_id else None
    return None


def _ids(values: Any) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    return tuple(task_id for item in values if (task_id := _child_id(item)))


def child_handoff_payload(child: ChildTaskState, **extra: Any) -> dict[str, Any]:
    """Hand a finished child to QA/synthesis **with its answer body**.

    요약만 넘기면 뒤 단계가 원문을 못 본다 - QA 는 인용을 검증할 대상이 없고
    (본 적 없는 문장을 통과시키게 된다), 종합은 표·수치를 다시 만들 수 없어
    사용자 응답이 요약 한 줄로 쪼그라든다. 실측 2026-08-14 t_79e42ca4.

    본문이 비어 있으면 그 사실을 명시한다 - 없는 것을 요약으로 때우면
    "답이 있었는데 사라진 것"과 "애초에 못 만든 것"이 구분되지 않는다.
    """

    payload: dict[str, Any] = {
        "task_id": child.task_id,
        "summary": child.summary,
        "result": child.result,
        "final_answer": child.final_answer,
        "error": child.error,
        "block_reason": child.block_reason,
        "missing_dependencies": list(child.missing_dependencies),
        "failure_kind": child.failure_kind,
    }
    if child.terminal:
        # 답변 품질 등급을 함께 싣는다 - 차단이 아니라 신호다(answer_contract).
        # QA 는 "무엇을 의심해야 하는지" 를 알고 시작해야 검증이 성립한다.
        grade = grade_answer(
            child.result or child.final_answer,
            summary=child.summary,
        )
        payload.update(grade.as_payload())
        if not grade.has_body:
            payload["answer_body_missing"] = True
            payload["answer_body_missing_note"] = (
                "이 부서 카드는 result(답변 본문) 없이 종료됐다. 요약만으로 본문을 "
                "복원하지 말고, 근거가 없는 수치·목록은 만들지 마라."
            )
    payload.update(extra)
    return payload


def _is_direct_ceo_response_synthesis(*, role: str, body: str) -> bool:
    """Return True for a CEO-authored response synthesis in the current workflow."""

    if str(role or "").casefold() != "synthesis":
        return False

    return any(
        line.strip().casefold() == "producer=ceo-hermes-direct"
        for line in str(body or "").splitlines()
    )


@dataclass(frozen=True)
class ChildTaskState:
    """Relevant, read-only task projection used by the supervisor."""

    task_id: str
    profile: str
    status: str
    summary: str = ""
    # 부서가 낸 **답변 본문**. summary 와 따로 든다 - 실측 2026-08-14: 창구가
    # 외국인 순매수 상위 10 표를 만들어 놓고 kanban_complete 에는 요약 한 줄만
    # 넣어, QA 도 종합도 표를 못 보고 사용자 응답이 result:null 로 나갔다.
    result: str = ""
    # A concise, user-ready answer produced by the primary department.
    # This is deliberately separate from the structured/internal result.
    final_answer: str = ""
    error: str = ""
    block_reason: str = ""
    block_kind: str = ""
    outcome: str = ""
    missing_dependencies: tuple[str, ...] = ()
    failure_kind: str = ""
    retry_count: int = 0
    body: str = ""
    workflow_root_task_id: str = ""

    @classmethod
    def from_hermes(cls, payload: Mapping[str, Any]) -> ChildTaskState:
        task_id = str(payload.get("id") or payload.get("task_id") or "")
        raw_profile = str(payload.get("assignee") or payload.get("profile") or "")
        status = str(payload.get("status") or "unknown").casefold()
        latest = payload.get("latest_summary")

        # Hermes stores structured terminal handoff fields in the latest run
        # metadata even when the task-level result remains null. Prefer explicit
        # task-level fields, then fall back to the newest run metadata.
        run_payload: Mapping[str, Any] = {}
        run_metadata: Mapping[str, Any] = {}
        runs = payload.get("runs")
        if isinstance(runs, Sequence) and not isinstance(runs, (str, bytes)):
            for run in reversed(runs):
                if not isinstance(run, Mapping):
                    continue
                run_payload = run
                metadata = run.get("metadata")
                if isinstance(metadata, Mapping):
                    run_metadata = metadata
                break

        summary = _text(
            payload.get("summary")
            or latest
            or payload.get("result")
            or run_metadata.get("summary")
            or run_metadata.get("result")
        )
        result = _text(
            payload.get("result")
            or run_metadata.get("result")
        )
        final_answer = _text(
            payload.get("final_answer")
            or run_metadata.get("final_answer")
        )
        error = _text(
            payload.get("error")
            or payload.get("last_error")
            or run_metadata.get("error")
            or run_payload.get("error")
        )
        block_reason = _text(
            payload.get("block_reason")
            or payload.get("blocked_reason")
            or payload.get("reason")
            or run_metadata.get("block_reason")
        )
        block_kind = str(payload.get("block_kind") or payload.get("kind") or "").casefold()
        outcome = str(
            payload.get("outcome")
            or run_payload.get("outcome")
            or ""
        ).casefold()
        raw_missing_dependencies = (
            payload.get("missing_dependencies")
            or run_metadata.get("missing_dependencies")
            or ()
        )
        if isinstance(raw_missing_dependencies, str):
            missing_dependencies = tuple(
                item.strip()
                for item in raw_missing_dependencies.split(",")
                if item.strip()
            )
        elif isinstance(raw_missing_dependencies, Sequence):
            missing_dependencies = tuple(
                str(item).strip()
                for item in raw_missing_dependencies
                if str(item).strip()
            )
        else:
            missing_dependencies = ()
        failure_verdict = classify_failure(error, block_reason)
        failure_kind = (
            failure_verdict.kind.value
            if failure_verdict.kind is not FailureKind.UNKNOWN
            else ""
        )
        body = _text(payload.get("body"))
        workflow_root_task_id = terminal_workflow_root(payload) or ""
        # Background research is outside the CEO task plane.  Its profile may
        # be a future dedicated runtime identity, so do not force it through
        # the request-scoped canonical department allowlist before excluding it.
        profile = (
            raw_profile
            if is_background_research({"body": body})
            else validate_canonical_profile(raw_profile)
        )
        runs = payload.get("runs")
        retry_count = 0
        if isinstance(runs, Sequence) and not isinstance(runs, (str, bytes)):
            retry_count = sum(
                1
                for run in runs
                if isinstance(run, Mapping)
                and str(run.get("outcome") or run.get("status") or "").casefold()
                in FAILURE_OUTCOMES
            )
        return cls(
            task_id=task_id,
            profile=profile,
            status=status,
            summary=summary,
            result=result,
            final_answer=final_answer,
            error=error,
            block_reason=block_reason,
            block_kind=block_kind,
            outcome=outcome,
            missing_dependencies=missing_dependencies,
            failure_kind=failure_kind,
            retry_count=retry_count,
            body=body,
            workflow_root_task_id=workflow_root_task_id,
        )

    @property
    def department(self) -> str:
        if self.is_background_research:
            return self.profile
        return department_for_canonical_profile(self.profile)

    @property
    def workflow_role(self) -> str:
        """Return the durable workflow role marker, when present."""

        for line in self.body.splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip().casefold() == "workflow_role":
                return value.strip().casefold()
        return ""

    @property
    def is_supervisor(self) -> bool:
        # QA and replan tasks are supervisor-created work, but they remain
        # workflow children. Only CEO-assigned control tasks are excluded
        # from the analysis/QA dependency graph.
        if self.is_background_research:
            return False
        return self.workflow_role in {"control", "synthesis"} or (
            self.profile == canonical_profile_for_department("ceo")
            and SUPERVISOR_MARKER in self.body
        )

    @property
    def is_qa(self) -> bool:
        return self.workflow_role == "qa" or (
            not self.workflow_role
            and self.profile == canonical_profile_for_department("qa")
        )

    @property
    def is_analysis(self) -> bool:
        """Whether this task is a current workflow's primary analysis child."""

        if self.is_background_research or self.is_supervisor or self.is_qa:
            return False
        return self.workflow_role == "primary"

    @property
    def is_background_research(self) -> bool:
        return is_background_research({"body": self.body})

    def is_in_workflow(self, root_task_id: str) -> bool:
        declared_root = self.workflow_root_task_id or terminal_workflow_root(
            {"body": self.body}
        )
        return declared_root == root_task_id

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES or self.outcome in TERMINAL_STATUSES

    @property
    def done(self) -> bool:
        return self.status in {"done", "completed", "archived"} or self.outcome == "completed"

    @property
    def blocked(self) -> bool:
        return self.status == "blocked" or self.outcome == "blocked"

    @property
    def failed(self) -> bool:
        return self.status in FAILURE_OUTCOMES or self.outcome in FAILURE_OUTCOMES


@dataclass(frozen=True)
class SupervisorState:
    parent_task_id: str
    children: tuple[ChildTaskState, ...]
    wakeups: int = 0
    replan_count: int = 0
    max_retries: int = 2
    max_wakeups: int = 8
    qa_required: bool = True
    workflow_mode: str = "analysis"
    # The root mandate is the single source of truth.  This flag only controls
    # whether supervisor-created children receive a reference to that snapshot.
    has_mandate: bool = False
    # Durable planner selection.  An empty tuple preserves compatibility for
    # legacy roots whose body predates the machine-readable field.
    selected_primary_profiles: tuple[str, ...] = ()
    # 이 루트가 **사람이 발원한 질의**인가 (origin=user-query 도장, RFC 3834 동형).
    # 공장 자동 생성 카드는 CEO 워크플로가 아니다 - 사용자에게 물어볼 것이 없다.
    root_is_user_query: bool = False
    # Enabled only by the production service when final Discord delivery exists.
    allow_primary_passthrough: bool = False

    @property
    def analysis_children(self) -> tuple[ChildTaskState, ...]:
        candidates = tuple(
            child
            for child in self.children
            if child.is_in_workflow(self.parent_task_id) and child.is_analysis
        )
        if not self.selected_primary_profiles:
            return candidates
        selected = set(self.selected_primary_profiles)
        return tuple(child for child in candidates if child.profile in selected)

    @property
    def missing_primary_profiles(self) -> tuple[str, ...]:
        if not self.selected_primary_profiles:
            return ()
        present = {child.profile for child in self.analysis_children}
        return tuple(profile for profile in self.selected_primary_profiles if profile not in present)

    @property
    def primary_by_profile(self) -> dict[str, ChildTaskState]:
        """Return one canonical primary per selected profile when valid."""

        if self.duplicate_primary_profiles:
            return {}
        candidates = self.analysis_children
        profiles = self.selected_primary_profiles or tuple(
            dict.fromkeys(child.profile for child in candidates)
        )
        return {
            profile: next(child for child in candidates if child.profile == profile)
            for profile in profiles
            if any(child.profile == profile for child in candidates)
        }

    @property
    def ready_profiles(self) -> tuple[str, ...]:
        """Unique selected profiles whose canonical primary is terminal."""

        if self.duplicate_primary_profiles:
            return ()
        return tuple(
            profile
            for profile, child in self.primary_by_profile.items()
            if child.terminal
        )

    @property
    def ready_count(self) -> int:
        return len(self.ready_profiles)

    @property
    def primary_ready(self) -> bool:
        selected_count = len(self.selected_primary_profiles) or len(
            self.primary_by_profile
        )
        return bool(selected_count) and not self.missing_primary_profiles and not (
            self.duplicate_primary_profiles
        ) and self.ready_count == selected_count

    @property
    def usable_analysis_children(self) -> tuple[ChildTaskState, ...]:
        """Terminal primary answers that contain a real user-facing body."""

        return tuple(
            child
            for child in self.analysis_children
            if grade_answer(
                child.result or child.final_answer,
                summary=child.summary,
            ).usable
        )

    @property
    def duplicate_primary_profiles(self) -> tuple[str, ...]:
        counts: dict[str, int] = {}
        for child in self.analysis_children:
            counts[child.profile] = counts.get(child.profile, 0) + 1
        return tuple(profile for profile in self.selected_primary_profiles or counts if counts.get(profile, 0) > 1)

    @property
    def qa_children(self) -> tuple[ChildTaskState, ...]:
        return tuple(
            child
            for child in self.children
            if child.is_in_workflow(self.parent_task_id)
            and child.is_qa
            and not child.is_supervisor
        )

    @property
    def supervisor_children(self) -> tuple[ChildTaskState, ...]:
        return tuple(child for child in self.children if child.is_supervisor)

    def has_action(self, action: SupervisorAction) -> bool:
        if action == SupervisorAction.SYNTHESIZE:
            return any(
                child.is_in_workflow(self.parent_task_id)
                and child.workflow_role == "synthesis"
                and (
                    (
                        SUPERVISOR_MARKER in child.body
                        and action.value in child.body
                    )
                    or _is_direct_ceo_response_synthesis(
                        role=child.workflow_role,
                        body=child.body,
                    )
                )
                for child in self.children
            )

        return any(
            child.is_in_workflow(self.parent_task_id)
            and SUPERVISOR_MARKER in child.body
            and action.value in child.body
            for child in self.children
        )


@dataclass(frozen=True)
class SupervisorDecision:
    action: SupervisorAction
    parent_task_id: str
    target_task_id: str | None = None
    assignee: str | None = None
    title: str | None = None
    body: str | None = None
    parent_task_ids: tuple[str, ...] = ()
    reason: str = ""
    retry_count: int = 0


def _blocked_decision(
    state: SupervisorState, child: ChildTaskState
) -> SupervisorDecision | None:
    if child.block_kind in {"needs_input", "user_input", "clarification"} or any(
        token in child.block_reason.casefold()
        for token in ("user input", "clarification", "missing input", "credentials")
    ):
        return SupervisorDecision(
            SupervisorAction.REQUEST_USER_INPUT,
            state.parent_task_id,
            target_task_id=child.task_id,
            assignee=canonical_profile_for_department("ceo"),
            title=f"User input required for {child.department}",
            body=(
                f"{SUPERVISOR_MARKER} action=REQUEST_USER_INPUT\n"
                f"Blocked task: {child.task_id}\nReason: {child.block_reason or child.error}"
            ),
            parent_task_ids=(),
            reason="blocked_needs_user_input",
        )
    if child.retry_count < state.max_retries and child.block_kind in {"transient", "retryable"}:
        return SupervisorDecision(
            SupervisorAction.RETRY_TASK,
            state.parent_task_id,
            target_task_id=child.task_id,
            retry_count=child.retry_count,
            reason="blocked_transient_retry",
        )
    if state.replan_count < state.max_retries:
        # Do not fan out another replan while the previous replan child is
        # still active. A later terminal event re-evaluates the full phase
        # and may legitimately permit the next bounded replan.
        if any(
            not sibling.terminal
            for sibling in state.analysis_children
            if sibling.task_id != child.task_id
        ):
            return None
        assignee = validate_canonical_profile(child.profile)
        return SupervisorDecision(
            SupervisorAction.CREATE_TASK,
            state.parent_task_id,
            assignee=assignee,
            title=f"Replan {child.department} after blocked task",
            body=(
                f"{SUPERVISOR_MARKER} action=CREATE_TASK mode=replan\n"
                f"Original task: {child.task_id}\n"
                f"Blocked reason: {child.block_reason or child.error or 'unspecified'}"
            ),
            parent_task_ids=(),
            reason="blocked_replan",
        )
    return SupervisorDecision(
        SupervisorAction.BLOCK_ABORT,
        state.parent_task_id,
        target_task_id=child.task_id,
        reason="blocked_replan_limit_reached",
    )


# hgfinance-batch-delegation-materializer-v1
_DELEGATION_INSTRUCTION_PREFIX = "delegation_instruction."
_ANALYSIS_EXECUTION_MODES = frozenset(
    {"fast_advisory", "standard_analysis", "full_experiment"}
)


def _analysis_execution_mode_from_root_body(body: str) -> str | None:
    """Read the CEO-selected non-binding analysis execution mode."""

    for raw_line in str(body or "").splitlines():
        key, separator, value = raw_line.partition("=")
        if separator and key.strip().casefold() == "analysis_mode":
            mode = value.strip().casefold()
            return mode if mode in _ANALYSIS_EXECUTION_MODES else None
    return None


def _delegation_plan_from_root_body(body: str) -> dict[str, str]:
    """Read the CEO-authored one-pass department delegation plan.

    The CEO remains the planner.  This parser only validates and exposes the
    already-selected department instructions to the deterministic supervisor.
    """

    plan: dict[str, str] = {}

    for raw_line in str(body or "").splitlines():
        key, separator, value = raw_line.partition("=")
        if not separator:
            continue

        normalized_key = key.strip()
        if not normalized_key.startswith(_DELEGATION_INSTRUCTION_PREFIX):
            continue

        raw_profile = normalized_key[len(_DELEGATION_INSTRUCTION_PREFIX):].strip()
        instruction = value.strip()

        if not raw_profile or not instruction:
            return {}

        try:
            profile = validate_canonical_profile(raw_profile)
        except CanonicalProfileError:
            return {}

        if profile in plan:
            # Duplicate plan entries are ambiguous. Fail closed rather than
            # silently choosing one instruction.
            return {}

        plan[profile] = instruction

    return plan


def _materialization_plan_body(
    root_payload: Mapping[str, Any],
) -> str:
    """Return the durable CEO-authored delegation projection for materialization.

    BFF-created roots keep immutable request/scope data in the task body. The
    direct CEO planner may persist its semantic routing plan as a root-local
    ceo-agent comment. Only a complete CEO-authored planning comment is allowed
    to augment the root body; user or department comments are never consulted.
    """

    root_body = str(root_payload.get("body") or "")

    body_has_complete_plan = (
        "selected_primary_profiles=" in root_body
        and "delegation_instruction." in root_body
        and _analysis_execution_mode_from_root_body(root_body) is not None
    )
    if body_has_complete_plan:
        return root_body

    comments = root_payload.get("comments")
    if not isinstance(comments, Sequence) or isinstance(comments, (str, bytes)):
        return root_body

    for comment in reversed(comments):
        if not isinstance(comment, Mapping):
            continue
        if str(comment.get("author") or "").strip().casefold() != "ceo-agent":
            continue

        comment_body = str(comment.get("body") or "")
        if (
            "selected_primary_profiles=" not in comment_body
            or "delegation_instruction." not in comment_body
            or _analysis_execution_mode_from_root_body(comment_body) is None
        ):
            continue

        return root_body + "\n" + comment_body

    return root_body


def _initial_primary_materialization_decisions(
    state: SupervisorState,
    root_body: str,
) -> tuple[SupervisorDecision, ...]:
    """Materialize only the CEO's already-authored initial analysis plan.

    This is deliberately not a planner:
    - non-binding analysis only
    - human-originated workflow only
    - exact selected-profile/plan equality required
    - duplicates suppress creation
    - only missing primaries are emitted
    """

    if state.workflow_mode != "analysis" or not state.root_is_user_query:
        return ()

    if (
        not state.selected_primary_profiles
        or not state.missing_primary_profiles
        or state.duplicate_primary_profiles
    ):
        return ()

    plan = _delegation_plan_from_root_body(root_body)
    selected = tuple(state.selected_primary_profiles)

    if set(plan) != set(selected):
        logger.warning(
            "initial-primary-plan-invalid root=%s selected=%s plan=%s",
            state.parent_task_id,
            ",".join(selected),
            ",".join(plan),
        )
        return ()

    analysis_mode = _analysis_execution_mode_from_root_body(root_body)
    if analysis_mode is None:
        logger.warning(
            "initial-primary-plan-invalid root=%s reason=missing_analysis_mode",
            state.parent_task_id,
        )
        return ()

    decisions: list[SupervisorDecision] = []

    for profile in state.missing_primary_profiles:
        department = department_for_canonical_profile(profile)

        decisions.append(
            SupervisorDecision(
                SupervisorAction.CREATE_TASK,
                state.parent_task_id,
                assignee=profile,
                title=f"CEO delegated {department} analysis",
                body=(
                    f"producer=ceo-supervisor-materializer\n"
                    f"analysis_mode={analysis_mode}\n\n"
                    f"{plan[profile]}"
                ),
                parent_task_ids=(),
                reason=f"initial_primary_materialize:{profile}",
            )
        )

    return tuple(decisions)



def _single_primary_passthrough_child(
    state: SupervisorState,
) -> ChildTaskState | None:
    """Return the one user-ready primary that may bypass CEO LLM synthesis.

    This optimization is intentionally narrow:
    - analysis workflow only
    - user-originated root only
    - exactly one explicitly selected primary
    - complete/unique primary set
    - primary completed successfully
    - a dedicated user-ready final_answer exists
    - final Discord delivery is configured

    Multi-primary, blocked/failed, binding, legacy, or incomplete work keeps the
    existing CEO synthesis path.
    """

    if not state.allow_primary_passthrough:
        return None
    if state.workflow_mode != "analysis" or not state.root_is_user_query:
        return None
    if len(state.selected_primary_profiles) != 1:
        return None
    if (
        state.missing_primary_profiles
        or state.duplicate_primary_profiles
        or not state.primary_ready
    ):
        return None

    children = state.analysis_children
    if len(children) != 1:
        return None

    child = children[0]
    if not child.done or child.blocked or child.failed:
        return None
    if child.error or child.block_reason:
        return None
    if not child.final_answer.strip():
        return None

    return child


def _analysis_synthesis_decision(
    state: SupervisorState,
) -> SupervisorDecision | None:
    """Build synthesis from primary state without consulting QA state."""

    if state.workflow_mode != "analysis" or state.has_action(SupervisorAction.SYNTHESIZE):
        return None
    if state.selected_primary_profiles and (
        state.missing_primary_profiles or state.duplicate_primary_profiles
    ):
        logger.warning(
            "synthesis-primary-set-incomplete root=%s missing=%s duplicates=%s",
            state.parent_task_id,
            ",".join(state.missing_primary_profiles),
            ",".join(state.duplicate_primary_profiles),
        )
        return None
    if not state.primary_ready:
        return None

    # A successful single-primary read/analysis that already produced a
    # user-ready answer does not need a second CEO LLM rewrite.
    if _single_primary_passthrough_child(state) is not None:
        return None

    # Execution dependencies must contain only successful primaries.
    # Blocked/failed terminal primaries remain in the synthesis payload so the
    # CEO can disclose missing evidence, but they must not gate dispatch.
    primary_ids = tuple(
        child.task_id for child in state.analysis_children if child.done
    )
    usable_children = state.usable_analysis_children
    usable_count = len(usable_children)
    selected_count = len(state.selected_primary_profiles) or len(
        state.analysis_children
    )
    if usable_count == selected_count:
        availability = "complete"
    elif usable_count >= 2:
        availability = "partial"
    elif usable_count == 1:
        availability = "limited_confidence"
    else:
        availability = "blocked"
    unavailable_profiles = tuple(
        child.profile
        for child in state.analysis_children
        if child not in usable_children
    )
    return SupervisorDecision(
        SupervisorAction.SYNTHESIZE,
        state.parent_task_id,
        assignee=canonical_profile_for_department("ceo"),
        title="CEO final synthesis",
        body=(
            f"{SUPERVISOR_MARKER} action=SYNTHESIZE\n"
            "workflow_plane=response\n"
            "governance_plane=async_qa\n"
            f"synthesis_availability={availability}\n"
            f"usable_primary_count={usable_count}\n"
            f"selected_primary_count={selected_count}\n"
            f"unavailable_primary_profiles={','.join(unavailable_profiles)}\n"
            "Synthesize available primary department work, including terminal "
            "blocked results. Preserve every usable department answer. If the "
            "availability is partial or limited_confidence, state which "
            "departments are unavailable and lower confidence accordingly. If "
            "availability is blocked, do not invent an investment conclusion; "
            "report the failure or missing-dependency scope. QA runs independently "
            "in an async governance lane and is not a synthesis prerequisite.\n"
            + json.dumps(
                [
                    child_handoff_payload(
                        child, profile=child.profile, status=child.status
                    )
                    for child in state.analysis_children
                ],
                ensure_ascii=False,
            )
        ),
        parent_task_ids=primary_ids,
        reason="primary_results_ready_fast_path",
    )


def decide_supervisor(state: SupervisorState) -> SupervisorDecision | None:
    """Choose one bounded action, or ``None`` while another child is running."""

    if state.wakeups >= state.max_wakeups:
        return SupervisorDecision(
            SupervisorAction.BLOCK_ABORT,
            state.parent_task_id,
            reason="supervisor_wakeup_limit_reached",
        )
    if not state.analysis_children:
        if state.selected_primary_profiles:
            logger.info(
                "primary-profile-state root=%s selected=%d ready=%d missing=%s",
                state.parent_task_id,
                len(state.selected_primary_profiles),
                0,
                ",".join(state.missing_primary_profiles),
            )
            return None
        # 사람이 발원한 질의일 때만 사용자에게 되묻는다. 공장 자동 생성 카드
        # (공장 주기·공장 개선 등)는 자식 없이 혼자 끝나는 게 정상인데, 그것까지
        # 워크플로로 보고 REQUEST_USER_INPUT 카드를 찍어내면 **아무도 답할 수 없는
        # 카드**가 쌓인다 - CEO 에이전트가 "무엇을 물어야 하는지 지시에 없다"며
        # blocked 로 보내고, 그게 43 장 쌓여 있었다(2026-08-14 실측, 전부 같은 제목
        # "CEO planner produced no executable child task").
        if not state.root_is_user_query:
            logger.info(
                "no-analysis-children on non-user root=%s - skipping user-input card",
                state.parent_task_id,
            )
            return None
        return SupervisorDecision(
            SupervisorAction.REQUEST_USER_INPUT,
            state.parent_task_id,
            assignee=canonical_profile_for_department("ceo"),
            title="CEO planner produced no executable child task",
            body=f"{SUPERVISOR_MARKER} action=REQUEST_USER_INPUT no_analysis_children",
            parent_task_ids=(),
            reason="no_analysis_children",
        )

    if state.missing_primary_profiles:
        logger.info(
            "primary-profile-state root=%s selected=%d ready=%d missing=%s",
            state.parent_task_id,
            len(state.selected_primary_profiles),
            state.ready_count,
            ",".join(state.missing_primary_profiles),
        )
        return None

    if state.duplicate_primary_profiles:
        logger.warning(
            "primary-duplicate-detected primary-integrity root=%s selected=%d "
            "duplicate_profiles=%s ready=false",
            state.parent_task_id,
            len(state.selected_primary_profiles),
            ",".join(state.duplicate_primary_profiles),
        )
        # Do not hide an already-created duplicate by selecting the newest or
        # fastest task.  A fresh workflow is prevented from reaching this
        # state by the stable create key in the CEO producer contract.
        return None

    policy_children = state.analysis_children
    if state.workflow_mode == "binding":
        policy_children = policy_children + state.qa_children
    for child in policy_children:
        if not child.terminal:
            continue
        if child.blocked:
            if state.workflow_mode == "analysis" and state.selected_primary_profiles:
                # A blocked selected primary is terminal for ordinary analysis.
                # Preserve its block_reason in the synthesis payload instead of
                # waiting forever or turning an advisory workflow into a gate.
                continue
            return _blocked_decision(state, child)
        if child.failed:
            if child.retry_count < state.max_retries:
                return SupervisorDecision(
                    SupervisorAction.RETRY_TASK,
                    state.parent_task_id,
                    target_task_id=child.task_id,
                    retry_count=child.retry_count,
                    reason="failed_child_retry",
                )
            return SupervisorDecision(
                SupervisorAction.BLOCK_ABORT,
                state.parent_task_id,
                target_task_id=child.task_id,
                reason="failed_retry_limit_reached",
            )

    if any(not child.terminal for child in state.analysis_children):
        return None
    # Async QA receives every primary in its payload, but only successful
    # primaries are execution dependencies. A blocked advisory primary must
    # not prevent QA or CEO synthesis from running.
    primary_ids = tuple(
        child.task_id for child in state.analysis_children if child.done
    )

    if state.workflow_mode == "analysis":
        if state.qa_required and not state.qa_children:
            return SupervisorDecision(
                SupervisorAction.RUN_QA,
                state.parent_task_id,
                assignee=canonical_profile_for_department("qa"),
                title="QA audit completed primary analysis",
                body=(
                    f"{SUPERVISOR_MARKER} action=RUN_QA\n"
                    "workflow_plane=governance\n"
                    "evaluation_sink=audit.eval_runs\n"
                    "feedback_consumer=hr-department\n"
                    "store_reasoning_trace=false\n"
                    + json.dumps(
                        [
                            child_handoff_payload(
                                child,
                                department=child.department,
                                actor_type="department_head",
                            )
                            for child in state.analysis_children
                        ],
                        ensure_ascii=False,
                    )
                ),
                parent_task_ids=primary_ids,
                reason="primary_results_ready_async_audit",
            )
        synthesis = _analysis_synthesis_decision(state)
        if synthesis is not None:
            return synthesis
        return None
    # Binding/high-risk workflows retain the existing fail-closed QA path.
    if state.qa_required:
        if not state.qa_children:
            parent_ids = tuple(child.task_id for child in state.analysis_children)
            return SupervisorDecision(
                SupervisorAction.RUN_QA,
                state.parent_task_id,
                assignee=canonical_profile_for_department("qa"),
                title="QA and audit completed primary analysis",
                body=(
                    f"{SUPERVISOR_MARKER} action=RUN_QA\n"
                    "Audit the completed primary analysis. Preserve citations,"
                    " reject unsupported claims, and report blocked findings.\n"
                    + json.dumps(
                        [
                            child_handoff_payload(child)
                            for child in state.analysis_children
                        ],
                        ensure_ascii=False,
                    )
                ),
                parent_task_ids=parent_ids,
                reason="primary_analysis_terminal",
            )
        if any(not child.terminal for child in state.qa_children):
            return None
        for child in state.qa_children:
            if child.blocked or child.failed:
                if child.blocked:
                    return _blocked_decision(state, child)
                if child.retry_count < state.max_retries:
                    return SupervisorDecision(
                        SupervisorAction.RETRY_TASK,
                        state.parent_task_id,
                        target_task_id=child.task_id,
                        retry_count=child.retry_count,
                        reason="qa_failed_retry",
                    )
                return SupervisorDecision(
                    SupervisorAction.BLOCK_ABORT,
                    state.parent_task_id,
                    target_task_id=child.task_id,
                    reason="qa_retry_limit_reached",
                )
        if not all(child.done for child in state.qa_children):
            return SupervisorDecision(
                SupervisorAction.BLOCK_ABORT,
                state.parent_task_id,
                reason="qa_terminal_without_success",
            )

    if state.has_action(SupervisorAction.SYNTHESIZE):
        return None
    qa_ids = tuple(
        child.task_id for child in state.qa_children if child.done
    )
    return SupervisorDecision(
        SupervisorAction.SYNTHESIZE,
        state.parent_task_id,
        assignee=canonical_profile_for_department("ceo"),
        title="CEO final synthesis",
        body=(
            f"{SUPERVISOR_MARKER} action=SYNTHESIZE\n"
            "workflow_plane=response\nworkflow_mode=binding\n"
            "Synthesize only after existing QA/Risk/approval gate. For a marked "
            "user PAPER-order result, preserve the primary final_answer verbatim; "
            "a non-binding or rejected result must explicitly say no order was "
            "submitted and must never be described as pending review.\n"
            + json.dumps(
                [
                    child_handoff_payload(
                        child,
                        profile=child.profile,
                        status=child.status,
                    )
                    for child in state.analysis_children + state.qa_children
                ],
                ensure_ascii=False,
            )
        ),
        parent_task_ids=qa_ids,
        reason="binding_qa_completed_final_synthesis",
    )


def parse_supervisor_output(payload: str | Mapping[str, Any]) -> SupervisorDecision:
    """Validate a structured action before it can reach the Hermes CLI."""

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SupervisorValidationError("supervisor output is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise SupervisorValidationError("supervisor output must be an object")
    allowed = {
        "action",
        "parent_task_id",
        "target_task_id",
        "assignee",
        "title",
        "body",
        "parent_task_ids",
        "reason",
        "retry_count",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise SupervisorValidationError(f"unknown supervisor fields: {sorted(unknown)}")
    try:
        action = SupervisorAction(str(payload["action"]))
        parent_task_id = str(payload["parent_task_id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise SupervisorValidationError("invalid supervisor action or parent_task_id") from exc
    assignee = payload.get("assignee")
    if assignee is not None:
        try:
            assignee = validate_canonical_profile(str(assignee))
        except CanonicalProfileError as exc:
            raise SupervisorValidationError(str(exc)) from exc
    parent_ids = payload.get("parent_task_ids", ())
    if not isinstance(parent_ids, Sequence) or isinstance(parent_ids, (str, bytes)):
        raise SupervisorValidationError("parent_task_ids must be an array")
    if action in {
        SupervisorAction.CREATE_TASK,
        SupervisorAction.RUN_QA,
        SupervisorAction.SYNTHESIZE,
    } and (not assignee or not payload.get("title") or not payload.get("body")):
        raise SupervisorValidationError(f"{action.value} requires canonical assignee, title, and body")
    if action == SupervisorAction.RETRY_TASK and not payload.get("target_task_id"):
        raise SupervisorValidationError("RETRY_TASK requires target_task_id")
    return SupervisorDecision(
        action=action,
        parent_task_id=parent_task_id,
        target_task_id=str(payload["target_task_id"]) if payload.get("target_task_id") else None,
        assignee=assignee,
        title=str(payload["title"]) if payload.get("title") else None,
        body=str(payload["body"]) if payload.get("body") else None,
        parent_task_ids=tuple(str(item) for item in parent_ids),
        reason=str(payload.get("reason") or ""),
        retry_count=int(payload.get("retry_count") or 0),
    )


class _DirectKanbanShowUnavailable(RuntimeError):
    """The optional Hermes-native read path cannot safely answer a show."""


class _HermesDirectKanbanReader:
    """Read the exact Hermes ``show --json`` projection without a subprocess.

    This is deliberately an adapter around Hermes' own ``kanban_db`` helpers,
    not a second workflow reader.  The connection is SQLite read-only and the
    response is assembled with Hermes' own ``_task_to_dict`` serializer and
    the same helper calls used by ``hermes kanban show --json``.  Import,
    schema, or read failures are surfaced to the caller so the CLI remains the
    authoritative fallback.
    """

    def __init__(self, environment: Mapping[str, str]) -> None:
        self.environment = dict(environment)
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._kanban_db: Any | None = None
        self._task_to_dict: Callable[[Any], dict[str, Any]] | None = None
        self._db_uri: str | None = None
        self._unavailable_reason: str | None = None
        try:
            from hermes_cli import kanban_db
            from hermes_cli.kanban import _task_to_dict

            required = (
                "get_task",
                "list_comments",
                "list_events",
                "parent_ids",
                "child_ids",
                "list_runs",
                "latest_summary",
            )
            if any(not callable(getattr(kanban_db, name, None)) for name in required):
                raise ImportError("Hermes kanban_db read helper is incomplete")
            db_path = Path(kanban_db_path(self.environment)).expanduser().resolve()
            if not db_path.is_file():
                raise FileNotFoundError(str(db_path))
            self._kanban_db = kanban_db
            self._task_to_dict = _task_to_dict
            self._db_uri = db_path.as_uri() + "?mode=ro"
        except Exception as exc:
            self._unavailable_reason = type(exc).__name__

    @property
    def available(self) -> bool:
        return self._db_uri is not None and self._kanban_db is not None

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def _connection_for_read(self) -> sqlite3.Connection:
        if self._connection is None:
            if self._db_uri is None:
                raise _DirectKanbanShowUnavailable(
                    self._unavailable_reason or "direct Hermes API unavailable"
                )
            conn = sqlite3.connect(
                self._db_uri,
                uri=True,
                timeout=1.0,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=1000")
            self._connection = conn
        return self._connection

    def _drop_connection(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def show(self, task_id: str) -> dict[str, Any]:
        """Return the same structured projection as Hermes ``show --json``."""

        with self._lock:
            try:
                assert self._kanban_db is not None
                assert self._task_to_dict is not None
                conn = self._connection_for_read()
                task = self._kanban_db.get_task(conn, task_id)
                if task is None:
                    raise _DirectKanbanShowUnavailable("task not found")
                comments = self._kanban_db.list_comments(conn, task_id)
                events = self._kanban_db.list_events(conn, task_id)
                parents = self._kanban_db.parent_ids(conn, task_id)
                children = self._kanban_db.child_ids(conn, task_id)
                runs = self._kanban_db.list_runs(conn, task_id)
                latest_summary = self._kanban_db.latest_summary(conn, task_id)
                return {
                    "task": self._task_to_dict(task),
                    "latest_summary": latest_summary,
                    "parents": parents,
                    "children": children,
                    "comments": [
                        {
                            "author": comment.author,
                            "body": comment.body,
                            "created_at": comment.created_at,
                        }
                        for comment in comments
                    ],
                    "events": [
                        {
                            "kind": event.kind,
                            "payload": event.payload,
                            "created_at": event.created_at,
                            "run_id": event.run_id,
                        }
                        for event in events
                    ],
                    "runs": [
                        {
                            "id": run.id,
                            "profile": run.profile,
                            "step_key": run.step_key,
                            "status": run.status,
                            "outcome": run.outcome,
                            "summary": run.summary,
                            "error": run.error,
                            "metadata": run.metadata,
                            "worker_pid": run.worker_pid,
                            "started_at": run.started_at,
                            "ended_at": run.ended_at,
                        }
                        for run in runs
                    ],
                }
            except _DirectKanbanShowUnavailable:
                raise
            except Exception as exc:
                self._drop_connection()
                raise _DirectKanbanShowUnavailable(type(exc).__name__) from exc


class HermesKanbanClient:
    """Hermes Kanban adapter with a fail-closed native read fast path."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        environment: Mapping[str, str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        timeout: float | None = None,
        root_index: SQLiteRootScopedIndex | None = None,
    ) -> None:
        self.executable = executable or os.environ.get("HERMES_BIN", "hermes")
        self.environment = dict(environment or os.environ)
        using_default_runner = runner is None
        self.runner = runner or subprocess.run
        self.timeout = timeout or float(os.environ.get("CEO_SUPERVISOR_CLI_TIMEOUT_SECONDS", "15"))
        self.root_index = root_index
        if self.root_index is None and using_default_runner:
            self.root_index = SQLiteRootScopedIndex(self.environment)
            # Index setup is a one-time board migration.  Do it before the
            # watch loop starts so a normal terminal event does not pay the
            # schema setup cost.  Any failure is deliberately non-fatal: the
            # authoritative full-board path remains available.
            try:
                self.root_index.prepare()
            except RootScopedIndexUnavailable as exc:
                logger.warning(
                    "kanban-root-index-unavailable error=%s",
                    type(exc).__name__,
                )
        self._retrieval_metrics_lock = threading.Lock()
        self._retrieval_metrics: dict[str, list[int]] = {
            "full_board_list_latency_ms": [],
            "root_lookup_latency_ms": [],
            "root_query_latency_ms": [],
            "candidate_discovery_latency_ms": [],
        }
        self._full_board_list_count = 0
        self._root_lookup_count = 0
        self._root_query_count = 0
        self._candidate_discovery_count = 0
        self._direct_show_reader = (
            _HermesDirectKanbanReader(self.environment)
            if using_default_runner
            else None
        )
        self._show_transport_metrics_lock = threading.Lock()
        self._direct_show_count = 0
        self._direct_show_fallback_count = 0
        self._direct_show_latency_ms: list[int] = []
        if self._direct_show_reader is not None:
            if self._direct_show_reader.available:
                logger.info("hermes-kanban-show-transport mode=direct-read-only")
            else:
                logger.info(
                    "hermes-kanban-show-transport mode=cli-fallback reason=%s",
                    self._direct_show_reader.unavailable_reason or "unknown",
                )
        self._cli_metrics_lock = threading.Lock()
        self._active_cli_calls = 0
        self._max_active_cli_calls = 0
        self._cli_operation_durations: dict[tuple[str, str], list[int]] = {}

    @staticmethod
    def _operation_for_args(args: Sequence[str]) -> str:
        if len(args) < 2 or args[0] != "kanban":
            return "unknown"
        command = str(args[1] or "").casefold()
        if command in {"comment", "block", "unblock", "complete", "archive"}:
            return "update"
        if command in {"runs", "log", "context", "stats"}:
            return "archive/debug"
        if command in {"show", "list", "create"}:
            return command
        return command or "unknown"

    @staticmethod
    def _stderr_category(
        stderr: object,
        *,
        timeout: bool = False,
        process_error: bool = False,
        return_code: int | None = None,
    ) -> str:
        if timeout:
            return "TIMEOUT"
        if process_error:
            return "PROCESS_ERROR"
        text = str(stderr or "").casefold()
        if "database is locked" in text or "database is busy" in text:
            return "SQLITE_LOCK"
        if "sqlite" in text and ("lock" in text or "busy" in text):
            return "SQLITE_LOCK"
        if return_code is not None and return_code != 0:
            return "PROCESS_ERROR"
        return "NONE"

    def _record_cli_diagnostic(
        self,
        *,
        operation: str,
        lane: str,
        elapsed_ms: int,
        timeout_ms: int,
        return_code: int | None,
        stderr_category: str,
        success: bool,
        active_cli_calls: int,
        max_active_cli_calls: int,
    ) -> None:
        logger.info(
            "hermes-cli operation=%s lane=%s elapsed_ms=%d timeout_ms=%d "
            "return_code=%s stderr_category=%s success=%s active_cli_calls=%d "
            "max_active_cli_calls=%d",
            operation,
            lane,
            elapsed_ms,
            timeout_ms,
            return_code if return_code is not None else "none",
            stderr_category,
            str(bool(success)).lower(),
            active_cli_calls,
            max_active_cli_calls,
        )

    def _record_json_failure(self, operation: str) -> None:
        with self._cli_metrics_lock:
            active = self._active_cli_calls
            maximum = self._max_active_cli_calls
        self._record_cli_diagnostic(
            operation=operation,
            lane=current_cli_lane(),
            elapsed_ms=0,
            timeout_ms=max(0, int(self.timeout * 1000)),
            return_code=0,
            stderr_category="JSON_ERROR",
            success=False,
            active_cli_calls=active,
            max_active_cli_calls=maximum,
        )

    def cli_metrics_snapshot(self) -> dict[str, Any]:
        """Return bounded CLI overlap/duration metrics without command data."""

        with self._cli_metrics_lock:
            by_operation = {
                f"{lane}:{operation}": {
                    "count": len(values),
                    "durations_ms": tuple(values),
                }
                for (lane, operation), values in sorted(
                    self._cli_operation_durations.items()
                )
            }
            return {
                "active_cli_calls": self._active_cli_calls,
                "max_active_cli_calls": self._max_active_cli_calls,
                "by_operation": by_operation,
            }

    def _run(self, args: Sequence[str], *, operation: str | None = None) -> str:
        operation_name = operation or self._operation_for_args(args)
        lane = current_cli_lane()
        timeout_ms = max(0, int(self.timeout * 1000))
        started_ns = time.perf_counter_ns()
        process: subprocess.CompletedProcess[str] | None = None
        failure: HermesKanbanCommandError | None = None
        stderr_category = "NONE"
        return_code: int | None = None
        stdout = ""
        with self._cli_metrics_lock:
            self._active_cli_calls += 1
            self._max_active_cli_calls = max(
                self._max_active_cli_calls,
                self._active_cli_calls,
            )
        try:
            process = self.runner(
                [self.executable, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env=self.environment,
            )
            return_code = process.returncode
            stderr_category = self._stderr_category(
                getattr(process, "stderr", ""),
                return_code=return_code,
            )
            if return_code != 0:
                failure = HermesKanbanCommandError(
                    f"hermes kanban command exited {return_code}"
                )
            else:
                stdout = process.stdout
        except subprocess.TimeoutExpired as exc:
            stderr_category = "TIMEOUT"
            failure = HermesKanbanCommandError(
                "hermes kanban command failed: TimeoutExpired"
            )
            failure.__cause__ = exc
        except OSError as exc:
            stderr_category = "PROCESS_ERROR"
            failure = HermesKanbanCommandError(
                f"hermes kanban command failed: {type(exc).__name__}"
            )
            failure.__cause__ = exc
        finally:
            elapsed_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
            with self._cli_metrics_lock:
                self._active_cli_calls = max(0, self._active_cli_calls - 1)
                active = self._active_cli_calls
                maximum = self._max_active_cli_calls
                self._cli_operation_durations.setdefault(
                    (lane, operation_name), []
                ).append(elapsed_ms)
            self._record_cli_diagnostic(
                operation=operation_name,
                lane=lane,
                elapsed_ms=elapsed_ms,
                timeout_ms=timeout_ms,
                return_code=return_code,
                stderr_category=stderr_category,
                success=failure is None,
                active_cli_calls=active,
                max_active_cli_calls=maximum,
            )
        if failure is not None:
            raise failure
        return stdout

    @staticmethod
    def _normalize_show_payload(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise HermesKanbanCommandError("hermes kanban show returned a non-object")
        # Hermes exposes the task row under ``task`` and graph/run projections
        # beside it. Keep this normalization shared by the CLI and native read
        # transports so the policy layer cannot observe two payload shapes.
        task = payload.get("task", payload)
        if not isinstance(task, dict):
            raise HermesKanbanCommandError(
                "hermes kanban show returned no task object"
            )
        normalized = dict(task)
        for key in (
            "latest_summary",
            "parents",
            "children",
            "comments",
            "events",
            "runs",
        ):
            if key in payload:
                normalized[key] = payload[key]
        return normalized

    def show(self, task_id: str) -> dict[str, Any]:
        direct_reader = self._direct_show_reader
        if direct_reader is not None and direct_reader.available:
            started_ns = time.perf_counter_ns()
            try:
                payload = direct_reader.show(task_id)
                normalized = self._normalize_show_payload(payload)
            except (_DirectKanbanShowUnavailable, HermesKanbanCommandError) as exc:
                with self._show_transport_metrics_lock:
                    self._direct_show_fallback_count += 1
                logger.warning(
                    "hermes-kanban-show-direct-fallback task=%s reason=%s",
                    task_id,
                    type(exc).__name__,
                )
            else:
                elapsed_ms = max(
                    0,
                    (time.perf_counter_ns() - started_ns) // 1_000_000,
                )
                with self._show_transport_metrics_lock:
                    self._direct_show_count += 1
                    self._direct_show_latency_ms.append(elapsed_ms)
                return normalized

        try:
            payload = json.loads(
                self._run(("kanban", "show", task_id, "--json"), operation="show")
            )
        except (json.JSONDecodeError, TypeError) as exc:
            self._record_json_failure("show")
            raise HermesKanbanCommandError(
                "hermes kanban show returned invalid JSON"
            ) from exc
        try:
            return self._normalize_show_payload(payload)
        except HermesKanbanCommandError:
            self._record_json_failure("show")
            raise

    def show_transport_metrics_snapshot(self) -> dict[str, Any]:
        """Return native-show usage and fallback counts without payload data."""

        with self._show_transport_metrics_lock:
            return {
                "direct_available": bool(
                    self._direct_show_reader is not None
                    and self._direct_show_reader.available
                ),
                "direct_show_count": self._direct_show_count,
                "direct_show_fallback_count": self._direct_show_fallback_count,
                "direct_show_latency_ms": tuple(self._direct_show_latency_ms),
            }

    def create_task(
        self,
        *,
        title: str,
        body: str,
        assignee: str,
        parent_task_ids: Sequence[str],
        idempotency_key: str,
        initial_status: str | None = None,
    ) -> dict[str, Any]:
        # 사용자 발원(origin=user-query) 워크플로의 자식은 대기열에서 공장 카드보다
        # 앞선다. 루트만 앞세우면 소용이 없다 - 실제로 답을 만드는 것은 자식이고,
        # 자식이 공장 뒤에 서면 사용자 지연은 그대로다(2026-08-14 실측).
        priority = USER_QUERY_PRIORITY if is_user_query_body(body) else 0
        request = CanonicalKanbanTaskRequest(
            assignee, title, body, idempotency_key, priority=priority
        )
        args: list[str] = ["kanban", "create", request.title, "--body", request.body]
        args.extend(("--assignee", request.assignee))
        for parent_task_id in parent_task_ids:
            args.extend(("--parent", str(parent_task_id)))
        args.extend(
            (
                "--idempotency-key",
                request.idempotency_key,
                "--created-by",
                "ceo-supervisor",
                "--priority",
                str(request.priority),
                "--json",
            )
        )
        if initial_status:
            if initial_status not in {"blocked", "running"}:
                raise SupervisorValidationError("invalid initial status")
            args.extend(("--initial-status", initial_status))
        # Retention runs in a separate process. Keep recovery/create mutations
        # out of the short final archive transaction's critical section.
        with workflow_mutation_lock(
            parent_task_ids[0] if parent_task_ids else None,
            environment=self.environment,
        ):
            try:
                payload = json.loads(self._run(args, operation="create"))
            except (json.JSONDecodeError, TypeError) as exc:
                self._record_json_failure("create")
                raise HermesKanbanCommandError(
                    "hermes kanban create returned invalid JSON"
                ) from exc
        if not isinstance(payload, dict):
            self._record_json_failure("create")
            raise HermesKanbanCommandError("hermes kanban create returned a non-object")
        return payload

    def unblock_task(self, task_id: str) -> None:
        with workflow_mutation_lock(environment=self.environment):
            self._run(("kanban", "unblock", task_id), operation="update")

    def comment_task(self, task_id: str, text: str) -> None:
        with workflow_mutation_lock(environment=self.environment):
            self._run(
                ("kanban", "comment", task_id, text, "--author", "ceo-supervisor"),
                operation="update",
            )

    def block_task(self, task_id: str, reason: str) -> None:
        with workflow_mutation_lock(environment=self.environment):
            self._run(
                ("kanban", "block", task_id, reason, "--kind", "needs_input"),
                operation="update",
            )

    def list_tasks(self) -> tuple[dict[str, Any], ...]:
        """List current-board tasks through the supported Hermes JSON API."""

        started_ns = time.perf_counter_ns()
        try:
            payload = json.loads(
                self._run(("kanban", "list", "--json"), operation="list")
            )
        except (json.JSONDecodeError, TypeError) as exc:
            self._record_json_failure("list")
            raise HermesKanbanCommandError(
                "hermes kanban list returned invalid JSON"
            ) from exc
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            self._record_json_failure("list")
            raise HermesKanbanCommandError(
                "hermes kanban list returned a non-task array"
            )
        elapsed_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
        with self._retrieval_metrics_lock:
            self._full_board_list_count += 1
            self._retrieval_metrics["full_board_list_latency_ms"].append(elapsed_ms)
        return tuple(dict(item) for item in payload)

    def recovery_candidate_rows(self) -> tuple[dict[str, Any], ...]:
        """Return SQLite discovery candidates, never authoritative task state."""

        index = self.root_index
        if index is None:
            raise RootScopedIndexUnavailable("root index is not configured")
        discovery = getattr(index, "recovery_candidate_rows", None)
        if not callable(discovery):
            raise RootScopedIndexUnavailable(
                "root index does not support recovery candidate discovery"
            )
        started_ns = time.perf_counter_ns()
        try:
            rows = discovery()
        finally:
            elapsed_ms = max(
                0,
                (time.perf_counter_ns() - started_ns) // 1_000_000,
            )
            with self._retrieval_metrics_lock:
                self._candidate_discovery_count += 1
                self._retrieval_metrics["candidate_discovery_latency_ms"].append(
                    elapsed_ms
                )
        logger.info(
            "kanban-candidate-discovery source=sqlite candidates=%d elapsed_ms=%d",
            len(rows),
            elapsed_ms,
        )
        return tuple(dict(row) for row in rows)

    def root_scoped_task_ids(
        self,
        root_id: str,
        *,
        include_archived: bool = False,
    ) -> tuple[str, ...]:
        """Find candidate task IDs using the local SQLite root index only.

        The returned IDs are a discovery hint.  The caller must still call
        :meth:`show` for every ID before making a workflow decision.
        """

        if self.root_index is None:
            raise RootScopedIndexUnavailable("root index is not configured")
        started_ns = time.perf_counter_ns()
        try:
            if include_archived:
                task_ids = self.root_index.task_ids(
                    root_id,
                    include_archived=True,
                )
            else:
                task_ids = self.root_index.task_ids(root_id)
        finally:
            elapsed_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
            with self._retrieval_metrics_lock:
                self._root_query_count += 1
                self._retrieval_metrics["root_query_latency_ms"].append(elapsed_ms)
        return tuple(task_ids)

    def authoritative_synthesis_exists(self, root_id: str) -> bool:
        """Check synthesis existence from indexed IDs plus authoritative shows."""

        root = str(root_id or "").strip()
        if not root:
            raise RootScopedIndexUnavailable("root_id is missing")
        candidate_ids = self.root_scoped_task_ids(
            root,
            include_archived=True,
        )
        for candidate_id in candidate_ids:
            payload = self.show(candidate_id)
            payload_id = str(payload.get("id") or payload.get("task_id") or "")
            if payload_id != candidate_id:
                raise RootScopedIndexUnavailable(
                    "indexed synthesis candidate returned another task"
                )
            refs = extract_scope_references(payload).root_ids
            if refs != (root,):
                raise RootScopedIndexUnavailable(
                    "indexed synthesis candidate has inconsistent root correlation"
                )
            body = str(payload.get("body") or "")
            role = terminal_workflow_role(payload) or ""
            action = terminal_action(payload) or terminal_action({"body": body}) or ""
            if role == "synthesis" and (
                action == "SYNTHESIZE"
                or _is_direct_ceo_response_synthesis(role=role, body=body)
            ):
                return True
        return False

    @staticmethod
    def _scoped_ids_from_rows(
        rows: Sequence[Mapping[str, Any]], root_id: str
    ) -> list[str]:
        if not str(root_id or "").strip():
            return []
        scoped_ids: list[str] = []
        for row in rows:
            row_id = str(row.get("id") or row.get("task_id") or "")
            if row_id and root_id in extract_scope_references(row).root_ids:
                scoped_ids.append(row_id)
        return list(dict.fromkeys(scoped_ids))

    @staticmethod
    def _is_canonical_scoped_root(
        payload: Mapping[str, Any], task_id: str
    ) -> bool:
        body = str(payload.get("body") or "")
        role = workflow_role_from_body(body)
        return (
            (
                role == "root"
                and is_user_query_body(body)
                and workflow_mode_from_body(body) in {"analysis", "binding"}
            )
            or (
                CEO_WORKFLOW_SCOPE_MARKER in body
                and (
                    role == "root"
                    or "root_task_role=scope_and_planning" in body
                )
            )
        )

    def _hydrate_ids(
        self,
        hydrate_ids: Sequence[str],
        *,
        known_payloads: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Hydrate independent IDs through authoritative ``show`` calls."""

        hydrated: dict[str, dict[str, Any]] = {
            str(task_id): dict(payload)
            for task_id, payload in (known_payloads or {}).items()
        }
        pending = [
            task_id
            for task_id in dict.fromkeys(str(item) for item in hydrate_ids)
            if task_id not in hydrated
        ]
        if not pending:
            return hydrated
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(
            max_workers=min(4, len(pending)),
            thread_name_prefix="ceo-workflow-show",
        ) as pool:
            futures = [
                pool.submit(copy_context().run, self.show, task_id)
                for task_id in pending
            ]
            hydrated_payloads = tuple(future.result() for future in futures)
        hydrated.update(
            dict(zip(pending, hydrated_payloads, strict=True))
        )
        return hydrated

    def _full_board_authoritative_snapshot(
        self,
        root_id: str,
        task_id: str,
        *,
        fallback_reason: str = "legacy-or-index-uncertain",
    ) -> tuple[str, tuple[dict[str, Any], ...], dict[str, Any]]:
        """Compatibility discovery for legacy and failed-index workflows."""

        _record_full_board_fallback(
            lane=current_cli_lane(),
            reason=fallback_reason,
            root_id=root_id,
        )
        rows = self.list_tasks()
        listed_by_id = {
            str(row.get("id") or row.get("task_id") or ""): row for row in rows
        }
        task_row = listed_by_id.get(task_id)
        task_refs = (
            extract_scope_references(task_row).root_ids
            if task_row is not None
            else ()
        )
        effective_root_id = str(root_id or "").strip() or (
            task_refs[0] if task_refs else task_id
        )
        scoped_ids = self._scoped_ids_from_rows(rows, effective_root_id)
        # A malformed direct marker must not be smuggled back into the
        # fallback merely because it was the event task.  Legacy tasks with no
        # marker are still retained for the parent/child compatibility path.
        fallback_task_ids = (
            ()
            if task_id != effective_root_id
            and task_refs
            and effective_root_id not in task_refs
            else (task_id,)
        )
        hydrate_ids = list(
            dict.fromkeys((effective_root_id, *fallback_task_ids, *scoped_ids))
        )
        hydrated = self._hydrate_ids(hydrate_ids)
        return (
            effective_root_id,
            tuple(
                hydrated[current_id]
                for current_id in hydrate_ids
                if current_id != effective_root_id
            ),
            hydrated[effective_root_id],
        )

    def workflow_root(self, task_id: str) -> str:
        """Resolve only the immutable workflow root for one task.

        Terminal wakeups need the root id before taking the per-root lock, but
        they do not need a complete workflow snapshot until after that lock is
        held.  Calling :meth:`workflow` for root discovery scans the board and
        hydrates every sibling, only to discard that snapshot and repeat the
        same work authoritatively under the lock.

        Current scoped tasks carry ``workflow_root_task_id`` directly.  The
        ancestry walk remains as a compatibility path for legacy parent-linked
        workflows; neither path scans descendants or the whole board.
        """

        # The generated-column index can identify the root candidate for a
        # canonical scoped child without hydrating that child before the root
        # lock. This is discovery metadata only; the authoritative snapshot
        # still shows the task, root, and every candidate after locking.
        if self.root_index is not None:
            lookup_started_ns = time.perf_counter_ns()
            try:
                root_for_task = getattr(self.root_index, "root_id_for_task", None)
                indexed_root = (
                    root_for_task(task_id)
                    if callable(root_for_task)
                    else None
                )
            except RootScopedIndexUnavailable:
                indexed_root = None
            finally:
                elapsed_ms = max(
                    0,
                    (time.perf_counter_ns() - lookup_started_ns) // 1_000_000,
                )
                with self._retrieval_metrics_lock:
                    self._root_lookup_count += 1
                    self._retrieval_metrics["root_lookup_latency_ms"].append(
                        elapsed_ms
                    )
            if indexed_root:
                return indexed_root

        cache: dict[str, dict[str, Any]] = {}

        def fetch(current_id: str) -> dict[str, Any]:
            if current_id not in cache:
                cache[current_id] = self.show(current_id)
            return cache[current_id]

        starting_payload = fetch(task_id)
        scoped_root_ids = extract_scope_references(starting_payload).root_ids
        if scoped_root_ids:
            return scoped_root_ids[0]

        if self._is_canonical_scoped_root(starting_payload, task_id):
            return task_id

        root_id = task_id
        visited: set[str] = set()
        while root_id not in visited:
            visited.add(root_id)
            parents = _ids(fetch(root_id).get("parents"))
            if not parents:
                break
            root_id = parents[0]
        return root_id

    def authoritative_workflow_snapshot(
        self,
        root_id: str,
        task_id: str,
    ) -> tuple[str, tuple[dict[str, Any], ...], dict[str, Any]]:
        return self._with_cli_operation_span(
            "workflow-reconstruction",
            lambda: self._authoritative_workflow_snapshot_impl(root_id, task_id),
        )

    def _authoritative_workflow_snapshot_impl(
        self,
        root_id: str,
        task_id: str,
    ) -> tuple[str, tuple[dict[str, Any], ...], dict[str, Any]]:
        """Hydrate one known-root workflow using indexed discovery when safe.

        The SQLite result supplies IDs only.  Status, runs, summaries,
        revisions, parents, and children still come from authoritative
        ``kanban show`` responses.  Any inability to prove that the indexed
        candidate set is safe falls back to the pre-existing full-board path.
        """

        # A fake/legacy adapter without the optional index keeps the exact old
        # contract.  This also makes the fallback explicit for migrations where
        # the shared board cannot be altered safely.
        if self.root_index is None:
            return self._full_board_authoritative_snapshot(
                root_id,
                task_id,
                fallback_reason="missing-root-index",
            )

        try:
            task_payload = self.show(task_id)
            task_scope = extract_scope_references(task_payload).root_ids
            task_is_scoped = (
                task_id != root_id
                and task_scope == (root_id,)
            )
            task_is_root = task_id == root_id and self._is_canonical_scoped_root(
                task_payload, task_id
            )
            if not (task_is_scoped or task_is_root):
                logger.info(
                    "kanban-root-retrieval-fallback root=%s task=%s reason=legacy-task",
                    root_id,
                    task_id,
                )
                return self._full_board_authoritative_snapshot(
                    root_id,
                    task_id,
                    fallback_reason="legacy-task",
                )

            scoped_ids = list(self.root_scoped_task_ids(root_id))
            # A non-root terminal event must be discoverable in its own index
            # scope.  Missing it means the index is stale or the correlation is
            # malformed; do not make a decision from an incomplete set.
            if task_id != root_id and task_id not in scoped_ids:
                raise RootScopedIndexUnavailable("indexed candidate set omitted event task")

            hydrate_ids = list(dict.fromkeys((root_id, task_id, *scoped_ids)))
            hydrated = self._hydrate_ids(
                hydrate_ids,
                known_payloads={task_id: task_payload},
            )

            root_payload = hydrated[root_id]
            if str(root_payload.get("id") or root_payload.get("task_id") or "") != root_id:
                raise RootScopedIndexUnavailable("indexed root show returned another task")
            for candidate_id in scoped_ids:
                payload = hydrated[candidate_id]
                payload_id = str(payload.get("id") or payload.get("task_id") or "")
                if payload_id != candidate_id:
                    raise RootScopedIndexUnavailable("indexed show returned another task")
                refs = extract_scope_references(payload).root_ids
                if refs != (root_id,):
                    raise RootScopedIndexUnavailable("indexed candidate has inconsistent root correlation")

            logger.info(
                "kanban-root-retrieval mode=indexed root=%s candidates=%d "
                "root_lookup_count=%d root_query_count=%d full_board_list_count=%d",
                root_id,
                len(scoped_ids),
                self._root_lookup_count,
                self._root_query_count,
                self._full_board_list_count,
            )
            return (
                root_id,
                tuple(
                    hydrated[current_id]
                    for current_id in hydrate_ids
                    if current_id != root_id
                ),
                root_payload,
            )
        except (RootScopedIndexUnavailable, HermesKanbanCommandError, KeyError) as exc:
            logger.warning(
                "kanban-root-retrieval-fallback root=%s task=%s reason=%s",
                root_id,
                task_id,
                type(exc).__name__,
            )
            return self._full_board_authoritative_snapshot(
                root_id,
                task_id,
                fallback_reason=f"indexed-validation-{type(exc).__name__}",
            )

    def retrieval_metrics_snapshot(self) -> dict[str, Any]:
        """Return retrieval counters used by before/after production probes."""

        with self._retrieval_metrics_lock:
            return {
                "full_board_list_count": self._full_board_list_count,
                "root_lookup_count": self._root_lookup_count,
                "root_query_count": self._root_query_count,
                "candidate_discovery_count": self._candidate_discovery_count,
                "full_board_list_latency_ms": tuple(
                    self._retrieval_metrics["full_board_list_latency_ms"]
                ),
                "root_lookup_latency_ms": tuple(
                    self._retrieval_metrics["root_lookup_latency_ms"]
                ),
                "root_query_latency_ms": tuple(
                    self._retrieval_metrics["root_query_latency_ms"]
                ),
                "candidate_discovery_latency_ms": tuple(
                    self._retrieval_metrics["candidate_discovery_latency_ms"]
                ),
            }

    def workflow(self, task_id: str) -> tuple[str, tuple[dict[str, Any], ...]]:
        return self._with_cli_operation_span(
            "workflow-reconstruction",
            lambda: self._workflow_impl(task_id),
        )

    def _with_cli_operation_span(
        self,
        operation: str,
        callback: Callable[[], Any],
    ) -> Any:
        started_ns = time.perf_counter_ns()
        success = True
        category = "NONE"
        try:
            return callback()
        except Exception as exc:
            success = False
            message = str(exc).casefold()
            if "timeout" in message:
                category = "TIMEOUT"
            elif "sqlite" in message and ("lock" in message or "busy" in message):
                category = "SQLITE_LOCK"
            else:
                category = "UNKNOWN"
            raise
        finally:
            elapsed_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
            with self._cli_metrics_lock:
                active = self._active_cli_calls
                maximum = self._max_active_cli_calls
            self._record_cli_diagnostic(
                operation=operation,
                lane=current_cli_lane(),
                elapsed_ms=elapsed_ms,
                timeout_ms=max(0, int(self.timeout * 1000)),
                return_code=None,
                stderr_category=category,
                success=success,
                active_cli_calls=active,
                max_active_cli_calls=maximum,
            )

    def _workflow_impl(self, task_id: str) -> tuple[str, tuple[dict[str, Any], ...]]:
        """Collect one workflow using execution edges or the durable scope marker.

        Current CEO primary tasks deliberately have no parent edge to the
        planning root: Hermes treats ``--parent`` as a blocking dependency.
        Those tasks carry ``workflow_root_task_id`` in their body and are
        discovered through ``kanban list --json``. Parent-linked workflows from
        before this contract remain supported through the ancestry fallback.
        """

        cache: dict[str, dict[str, Any]] = {}

        def fetch(current_id: str) -> dict[str, Any]:
            if current_id not in cache:
                cache[current_id] = self.show(current_id)
            return cache[current_id]

        starting_payload = fetch(task_id)
        scoped_root_ids = extract_scope_references(starting_payload).root_ids

        # A scoped primary/QA/synthesis declares workflow_root_task_id directly.
        # The root itself deliberately does not point to itself; identify it by
        # the durable workflow marker + workflow_role=root and perform the same
        # marker-based discovery. This keeps parentless primaries inside the
        # workflow scope without turning the root into an execution dependency.
        # hgfinance-canonical-root-scope-v1
        #
        # Current direct CEO ingress roots are canonical user-query roots but
        # may not carry the legacy hgfinance.ceo-workflow-scope.v1 marker.
        # Parentless primaries still declare workflow_root_task_id, so these
        # roots must enter marker-based scope discovery rather than ancestry
        # fallback.
        canonical_user_root = self._is_canonical_scoped_root(
            starting_payload, task_id
        )
        is_scoped_root = canonical_user_root

        if scoped_root_ids or is_scoped_root:
            root_id = scoped_root_ids[0] if scoped_root_ids else task_id
            fetch(root_id)

            scoped_ids = {root_id}
            try:
                scoped_ids.update(self.root_scoped_task_ids(root_id))
            except RootScopedIndexUnavailable:
                # Compatibility path for legacy Hermes versions, unavailable
                # SQLite, and migration windows.  The subsequent show() calls
                # remain authoritative exactly as before.
                _record_full_board_fallback(
                    lane=current_cli_lane(),
                    reason="scoped-index-unavailable",
                    root_id=root_id,
                )
                scoped_ids.update(
                    self._scoped_ids_from_rows(self.list_tasks(), root_id)
                )

            for scoped_id in scoped_ids:
                fetch(scoped_id)

            return root_id, tuple(
                payload
                for current_id, payload in cache.items()
                if current_id != root_id
            )

        root_id = task_id
        visited: set[str] = set()
        while root_id not in visited:
            visited.add(root_id)
            parents = _ids(fetch(root_id).get("parents"))
            if not parents:
                break
            root_id = parents[0]

        descendant_ids: set[str] = set()

        def descendants(current_id: str) -> None:
            task = fetch(current_id)
            for child_id in _ids(task.get("children")):
                if child_id in descendant_ids:
                    continue
                descendant_ids.add(child_id)
                if child_id not in cache:
                    fetch(child_id)
                descendants(child_id)

        descendants(root_id)
        return root_id, tuple(task for current, task in cache.items() if current != root_id)


DEPARTMENT_DISCORD_LABELS: dict[str, tuple[str, str]] = {
    "research-department": ("🔬", "Research 부서"),
    "quant-backtest-department": ("📊", "Quant / Backtest 부서"),
    "risk-management": ("🛡️", "Risk 부서"),
    "accounting-portfolio-department": ("📒", "Accounting / Portfolio 부서"),
    "trading-department": ("💹", "Trading 부서"),
    "hr-department": ("👥", "HR 부서"),
    "qa-department": ("✅", "QA 부서"),
}


def _task_timestamp_ms(task: Mapping[str, Any], field: str) -> int:
    try:
        value = int(task.get(field) or 0)
    except (TypeError, ValueError):
        return 0
    return value * 1000 if value > 0 else 0


def _elapsed_ms(started_at_ms: int, completed_at_ms: int) -> int:
    if started_at_ms <= 0 or completed_at_ms < started_at_ms:
        return -1
    return completed_at_ms - started_at_ms


def _department_progress_text(
    profile: str,
    kind: str,
    *,
    summary: str = "",
    missing_dependencies: Sequence[str] = (),
    failure_kind: str = "",
) -> str | None:
    icon, label = DEPARTMENT_DISCORD_LABELS.get(
        profile,
        ("🏢", profile),
    )

    normalized = str(kind or "").casefold()

    if normalized in {"claimed", "spawned", "started", "running"}:
        return f"{icon} **{label}**\n분석을 시작했습니다."

    if normalized in {"done", "completed"}:
        tail = str(summary or "").strip()

        # Keep department completion messages useful in Discord without
        # dumping an entire artifact or report into the channel.
        has_detail_thread = len(tail) > 600

        if len(tail) > 450:
            tail = tail[:447].rstrip() + "..."

        if tail:
            quoted = "\n".join(
                f"> {line}" if line.strip() else ">"
                for line in tail.splitlines()
            )
            detail_hint = (
                "\n\n🧵 **전체 상세 분석은 이 요청의 스레드에서 확인할 수 있습니다.**"
                if has_detail_thread
                else ""
            )

            return (
                f"{icon} **{label}**\n"
                f"분석을 완료했습니다.\n\n"
                f"**핵심 결과**\n"
                f"{quoted}"
                f"{detail_hint}"
            )

        return f"{icon} **{label}**\n분석을 완료했습니다."

    if (
        normalized == "blocked"
        and missing_dependencies
        and failure_kind != FailureKind.PROTOCOL.value
    ):
        waiting_text = {
            "research-department": "시장 데이터 확인 대기 중입니다.",
            "research-liaison": "시장 데이터 확인 대기 중입니다.",
            "quant-backtest-department": "가격·변동성 데이터 확인 대기 중입니다.",
            "quant-liaison": "가격·변동성 데이터 확인 대기 중입니다.",
            "risk-management": "현재 포트폴리오 노출 확인 대기 중입니다.",
        }.get(profile, "필요한 데이터 확인 대기 중입니다.")
        return f"{icon} **{label}**\n⏳ {waiting_text}"

    if normalized in {
        "blocked",
        "failed",
        "error",
        "gave_up",
        "crashed",
        "timed_out",
        "spawn_failed",
    }:
        if failure_kind == FailureKind.PROTOCOL.value:
            return (
                f"{icon} **{label}**\n"
                "❌ 실행 결과가 정상적으로 인계되지 않았습니다. "
                "CEO가 가용 결과와 실패 범위를 확인합니다."
            )
        if str(summary or "").strip():
            return (
                f"{icon} **{label}**\n"
                "⚠️ 제한된 결과만 확보됐습니다. CEO 종합에서 누락 범위를 밝힙니다."
            )
        return (
            f"{icon} **{label}**\n"
            "❌ 작업 중 오류가 발생했습니다. CEO가 가능한 결과와 누락 범위를 확인합니다."
        )

    return None


class CeoSupervisorService:
    """Wake once per terminal event and execute at most one bounded action."""

    def __init__(
        self,
        client: HermesKanbanClient,
        *,
        max_retries: int = 2,
        max_wakeups: int = 8,
        qa_required: bool = True,
        decider: Callable[[SupervisorState], SupervisorDecision | None] = decide_supervisor,
        synthesis_projection: Any | None = None,
        qa_projection: Any | None = None,
        discord_delivery: DiscordFinalDelivery | None = None,
        department_notion_projection: Any | None = None,
    ) -> None:
        self.client = client
        self.max_retries = max_retries
        self.max_wakeups = max_wakeups
        self.qa_required = qa_required
        self.decider = decider
        # Terminal projections observe outcomes; they never participate in policy.
        self.synthesis_projection = synthesis_projection
        self.qa_projection = qa_projection
        self.discord_delivery = discord_delivery
        self._department_notion_projection = (
            department_notion_projection
            if department_notion_projection is not None
            else DepartmentNotionProjection(
                env=getattr(client, "environment", os.environ),
            )
        )
        self._seen_events: set[str] = set()
        # Hermes watch and recovery can describe the same terminal transition
        # with different event ids. Coalesce those wakeups before they contend
        # on the root lock or re-enter response delivery.
        self._seen_terminal_transitions: set[str] = set()
        self._wakeups: dict[str, int] = {}
        self._replans: dict[str, int] = {}
        self._executed_actions: set[str] = set()
        self._seen_events_lock = threading.Lock()

        # Hot-path cache for Discord/supervisor lifecycle events.
        # workflow(task_id) is comparatively expensive because the Hermes
        # adapter reconstructs workflow state. The root relation is immutable
        # for the lifetime of a task, so cache only that relation; authoritative
        # workflow payloads are still re-read after acquiring the parent lock.
        self._task_root_cache: dict[str, str] = {}
        self._task_root_cache_lock = threading.Lock()
        # Active Discord progress may reuse a root payload that was already
        # hydrated by a terminal/recovery path.  It is deliberately a cache
        # of metadata only; active events never rebuild a workflow snapshot.
        self._task_root_payload_cache: dict[str, dict[str, Any]] = {}
        self._active_task_payload_cache: dict[str, dict[str, Any]] = {}

        # hgfinance-department-progress-dedupe-v1
        # Active lifecycle events (claimed/spawned/started/running) are
        # semantically one Discord state.  Remember successful projections so
        # duplicate Hermes watch chatter can be rejected before expensive
        # workflow()/show() calls.
        self._department_started_progress: set[str] = set()
        self._department_started_progress_lock = threading.Lock()

        self._parent_locks: dict[str, threading.Lock] = {}
        self._parent_locks_lock = threading.Lock()

    def _parent_lock(self, parent_task_id: str) -> threading.Lock:
        with self._parent_locks_lock:
            return self._parent_locks.setdefault(parent_task_id, threading.Lock())

    @staticmethod
    def _wakeup_comments(root_payload: Mapping[str, Any]) -> dict[str, str]:
        comments = root_payload.get("comments")
        if not isinstance(comments, Sequence) or isinstance(comments, (str, bytes)):
            return {}
        entries: dict[str, str] = {}
        for comment in comments:
            if not isinstance(comment, Mapping):
                continue
            body = str(comment.get("body") or "")
            if not body.startswith(SUPERVISOR_WAKE_MARKER):
                continue
            for field in body.split()[1:]:
                if field.startswith("event="):
                    entries[field[6:]] = body
                    break
        return entries

    @staticmethod
    def _wakeup_budget(comments: Mapping[str, str]) -> int:
        """Count only safety-loop actions, not ordinary terminal events.

        Older wakeup comments did not carry the budget flag.  They are treated
        conservatively: retries, replans, user-input requests, and aborts count;
        normal QA/synthesis phase transitions do not.
        """

        safety_actions = {
            SupervisorAction.CREATE_TASK.value,
            SupervisorAction.RETRY_TASK.value,
            SupervisorAction.REQUEST_USER_INPUT.value,
            SupervisorAction.BLOCK_ABORT.value,
        }
        total = 0
        for body in comments.values():
            if "budget_consumed=false" in body:
                continue
            if "budget_consumed=true" in body:
                total += 1
                continue
            action = next(
                (
                    token.split("=", 1)[1]
                    for token in body.split()
                    if token.startswith("action=")
                ),
                "",
            )
            total += action in safety_actions
        return total

    @staticmethod
    def _consumes_wakeup_budget(action: SupervisorAction | None) -> bool:
        return action in {
            SupervisorAction.CREATE_TASK,
            SupervisorAction.RETRY_TASK,
            SupervisorAction.REQUEST_USER_INPUT,
            SupervisorAction.BLOCK_ABORT,
        }

    def _record_wakeup(
        self,
        *,
        root_task_id: str,
        event_id: str,
        kind: str,
        action: str,
        existing: Mapping[str, str],
        state: str,
        budget_consumed: bool,
    ) -> None:
        comment_task = getattr(self.client, "comment_task", None)
        if not callable(comment_task):
            return
        comment_task(
            root_task_id,
            f"{SUPERVISOR_WAKE_MARKER} event={event_id} kind={kind} "
            f"state={state} action={action} budget_consumed={str(budget_consumed).lower()}",
        )

    def _safe_abort(self, task_id: str, reason: str) -> None:
        try:
            self.client.block_task(task_id, reason)
        except HermesKanbanCommandError as exc:
            raise SupervisorWorkflowError(
                f"workflow {task_id} canonical abort failed: {exc}"
            ) from exc

    def _project_terminal_task(
        self,
        *,
        root_task_id: str,
        task_id: str,
        task_payloads: Sequence[Mapping[str, Any]],
        event: Mapping[str, Any],
    ) -> str | None:
        """Run a terminal observer without changing the supervisor decision."""

        delivery_status: str | None = None

        task = next(
            (
                payload
                for payload in task_payloads
                if str(payload.get("id") or payload.get("task_id") or "") == task_id
            ),
            None,
        )
        if task is None:
            # workflow() may return the root and workflow descendants without
            # the terminal synthesis task that triggered reconciliation.
            # Recover that durable task explicitly so terminal projection
            # (especially Discord CEO-final delivery) is not silently skipped.
            show = getattr(self.client, "show", None)
            if not callable(show):
                return None
            try:
                task = show(task_id)
            except Exception as exc:
                logger.warning(
                    "terminal-projection-show-failed "
                    "root=%s task=%s error=%s",
                    root_task_id,
                    task_id,
                    type(exc).__name__,
                )
                return None

        body = str(task.get("body") or "")
        role = terminal_workflow_role(task) or ""
        task_action = terminal_action(task) or terminal_action({"body": body})
        supervisor_synthesis = (
            role == "synthesis" and task_action == "SYNTHESIZE"
        )
        direct_ceo_synthesis = _is_direct_ceo_response_synthesis(
            role=role,
            body=body,
        )
        response_synthesis = supervisor_synthesis or direct_ceo_synthesis

        if response_synthesis:
            logger.info(
                "synthesis-complete root=%s task=%s producer=%s",
                root_task_id,
                task_id,
                "ceo-hermes-direct" if direct_ceo_synthesis else "ceo-supervisor",
            )
        if response_synthesis and self.discord_delivery:
            synthesized = ChildTaskState.from_hermes(task)
            content = _text(
                synthesized.final_answer
                or task.get("latest_summary")
                or task.get("summary")
                or task.get("result")
            )
            if content:
                root_payload = next(
                    (
                        payload
                        for payload in task_payloads
                        if str(payload.get("id") or payload.get("task_id") or "")
                        == root_task_id
                    ),
                    {},
                )
                delivery_task = dict(task)
                delivery_task["root_task"] = root_payload
                delivery_environment = getattr(self.client, "environment", os.environ)
                hermes_home = delivery_environment.get("HERMES_HOME", "/opt/data")
                ceo_profile_home = os.path.join(
                    hermes_home,
                    "profiles",
                    canonical_profile_for_department("ceo"),
                )
                delivery_home = (
                    ceo_profile_home
                    if os.path.isdir(ceo_profile_home)
                    else hermes_home
                )
                delivery_store = DiscordIdempotencyStore(delivery_home)
                ceo_profile = canonical_profile_for_department("ceo")

                # hgfinance-synthesis-thread-only-v1
                #
                # A Discord request that owns a request thread keeps its entire
                # workflow output in that exact thread.  The originating parent
                # channel contains only the user's root request.
                #
                # Legacy/web/no-thread flows retain the historical parent reply
                # as a compatibility fallback.
                correlation = correlation_from_task(delivery_task)

                # Always let DiscordFinalDelivery resolve the existing thread
                # first. It can recover the thread from explicit correlation,
                # the inbound ledger, or the Discord starter message id.
                delivery_started_ms = time.time_ns() // 1_000_000
                thread_status = self.discord_delivery.deliver_to_existing_thread(
                    root_task_id=root_task_id,
                    source_task=delivery_task,
                    root_task=root_payload,
                    content=content,
                    title="🧠 CEO 종합",
                    store=delivery_store,
                    profile=ceo_profile,
                    response_key_suffix=f"synthesis-detail:{task_id}",
                )
                delivery_status = thread_status

                logger.info(
                    "synthesis-discord-thread root=%s task=%s status=%s",
                    root_task_id,
                    task_id,
                    thread_status,
                )

                # Parent-channel delivery is compatibility fallback only when
                # the request truly has no resolvable Discord thread.
                if thread_status == "missing_thread":
                    parent_status = self.discord_delivery.deliver(
                        root_task_id=root_task_id,
                        synthesis_task=delivery_task,
                        content=content,
                        store=delivery_store,
                        profile=ceo_profile,
                    )
                    delivery_status = parent_status

                    logger.info(
                        "synthesis-discord-parent-fallback "
                        "root=%s task=%s status=%s",
                        root_task_id,
                        task_id,
                        parent_status,
                    )

                delivery_completed_ms = time.time_ns() // 1_000_000
                logger.info(
                    "supervisor-action-timing root=%s task=%s event=%s "
                    "action=DISCORD_DELIVERY action_started=%d "
                    "action_completed=%d action_duration_ms=%d",
                    root_task_id,
                    task_id,
                    event.get("event_id") or "",
                    delivery_started_ms,
                    delivery_completed_ms,
                    _elapsed_ms(delivery_started_ms, delivery_completed_ms),
                )

        # Non-binding observers run after the user response lane. Their
        # filesystem/HTTP work must not delay completed synthesis delivery.
        projection = (
            self.synthesis_projection
            if supervisor_synthesis
            else self.qa_projection
            if role == "qa" and task_action == "RUN_QA"
            else None
        )
        if projection is not None:
            try:
                projection.project(
                    root_task_id=root_task_id,
                    task=task,
                    workflow_tasks=task_payloads,
                    event=event,
                )
            except Exception as exc:
                logger.exception(
                    "terminal projection observer failed",
                    extra={
                        "root_task_id": root_task_id,
                        "task_id": task_id,
                        "error": str(exc),
                    },
                )

        # Trading/Quant terminal results are projected to their existing
        # Notion databases as a non-binding observer. Research/Risk/
        # Accounting/QA/HR retain their native reporters, so this projector
        # deliberately skips them.
        try:
            department_projection = self._department_notion_projection.project(
                root_task_id=root_task_id,
                task=task,
                workflow_tasks=task_payloads,
                event=event,
            )
            if department_projection.status not in {"skipped", "duplicate"}:
                logger.info(
                    "department-notion-projection "
                    "task=%s department=%s status=%s",
                    task_id,
                    department_projection.department,
                    department_projection.status,
                )
        except Exception as exc:
            logger.exception(
                "department notion projection observer failed",
                extra={
                    "root_task_id": root_task_id,
                    "task_id": task_id,
                    "error": str(exc),
                },
            )

        return delivery_status

    def _bridge_root_completion_to_discord(
        self,
        *,
        root_task_id: str,
        root_payload: Mapping[str, Any],
    ) -> str | None:
        """Bridge a completed CEO planning root to existing Discord delivery.

        No semantic routing happens here.

        Existing CEO-authored durable state decides the UX:
        - no selected primary + root final_answer -> direct CEO reply
        - selected primaries -> one CEO delegation card

        Existing Discord delivery methods own correlation, idempotency,
        message creation/update, and thread targeting.
        """

        if self.discord_delivery is None:
            return None

        root_body = str(root_payload.get("body") or "")

        if (
            workflow_mode_from_body(root_body) != "analysis"
            or not is_user_query_body(root_body)
        ):
            return None

        environment = getattr(self.client, "environment", os.environ)
        hermes_home = environment.get("HERMES_HOME", "/opt/data")

        ceo_profile = canonical_profile_for_department("ceo")
        ceo_profile_home = os.path.join(
            hermes_home,
            "profiles",
            ceo_profile,
        )
        delivery_home = (
            ceo_profile_home
            if os.path.isdir(ceo_profile_home)
            else hermes_home
        )
        store = DiscordIdempotencyStore(delivery_home)

        selected = selected_primary_profiles_from_task(root_payload)

        # Delegated workflow:
        # reuse the CEO's existing durable selection + delegation instructions.
        if selected:
            plan_body = _materialization_plan_body(root_payload)
            plan = _delegation_plan_from_root_body(plan_body)

            lines = [
                "🧠 **CEO 업무 분배**",
                "",
            ]

            for profile in selected:
                try:
                    department = department_for_canonical_profile(profile)
                except Exception:
                    department = profile

                instruction = str(plan.get(profile) or "").strip()

                lines.append(f"**{department}**")
                if instruction:
                    lines.append(f"└ {instruction}")

            content = "\n".join(lines).strip()

            status = self.discord_delivery.upsert_thread_card(
                root_task_id=root_task_id,
                source_task=root_payload,
                root_task=root_payload,
                content=content,
                store=store,
                profile=ceo_profile,
                response_key_suffix=f"ceo-delegation:{root_task_id}",
                update_existing=True,
            )

            logger.info(
                "ceo-root-discord-bridge root=%s mode=delegated "
                "selected=%s status=%s",
                root_task_id,
                ",".join(selected),
                status,
            )
            return status

        # Direct CEO answer:
        # reuse ChildTaskState's existing run-metadata final_answer fallback.
        root_state = ChildTaskState.from_hermes(root_payload)

        content = _text(
            root_state.final_answer
            or root_state.result
            or root_state.summary
            or root_payload.get("latest_summary")
            or root_payload.get("summary")
            or root_payload.get("result")
        )

        if not content:
            logger.info(
                "ceo-root-discord-bridge root=%s mode=direct status=empty",
                root_task_id,
            )
            return "empty"

        status = self.discord_delivery.deliver_to_existing_thread(
            root_task_id=root_task_id,
            source_task=root_payload,
            root_task=root_payload,
            content=content,
            title="🧠 CEO 답변",
            store=store,
            profile=ceo_profile,
            response_key_suffix=f"ceo-direct:{root_task_id}",
        )

        logger.info(
            "ceo-root-discord-bridge root=%s mode=direct status=%s",
            root_task_id,
            status,
        )
        return status


    def _reconcile_department_start_progress(
        self,
        *,
        root_task_id: str,
        root_payload: Mapping[str, Any],
        task_payloads: Sequence[Mapping[str, Any]],
    ) -> None:
        """Recover department start messages from durable task state.

        Hermes kanban watch can coalesce or miss sibling claimed/spawned
        transitions that happen in the same polling window. Whenever any
        execution activity is observed, inspect all selected primary tasks and
        project a synthetic started event for every task that is already
        running. Discord idempotency keeps the projection exactly-once.
        """
        if self.discord_delivery is None:
            return

        root_body = str(root_payload.get("body") or "")
        if workflow_mode_from_body(root_body) != "analysis":
            return
        if not is_user_query_body(root_body):
            return

        selected = selected_primary_profiles_from_task(root_payload)
        if len(selected) < 2:
            return

        show = getattr(self.client, "show", None)

        for payload in task_payloads:
            child = ChildTaskState.from_hermes(payload)

            if (
                child.workflow_role != "primary"
                or child.profile not in selected
                or not child.is_in_workflow(root_task_id)
            ):
                continue

            candidate = payload
            if callable(show):
                try:
                    candidate = show(child.task_id)
                    child = ChildTaskState.from_hermes(candidate)
                except HermesKanbanCommandError:
                    candidate = payload

            status = str(
                candidate.get("status")
                or child.status
                or ""
            ).casefold()

            started_at = candidate.get("started_at")

            if (
                status not in {"running", "claimed", "in_progress"}
                and started_at is None
            ):
                continue

            try:
                self._deliver_department_progress(
                    root_task_id=root_task_id,
                    root_payload=root_payload,
                    task_payload=candidate,
                    event={
                        "event_id": f"state-start:{child.task_id}",
                        "task_id": child.task_id,
                        "kind": "started",
                    },
                )
            except Exception as exc:
                logger.warning(
                    "department-start-reconcile-failed "
                    "root=%s task=%s profile=%s error=%s",
                    root_task_id,
                    child.task_id,
                    child.profile,
                    type(exc).__name__,
                )



    def _reconcile_department_terminal_progress(
        self,
        *,
        root_task_id: str,
        root_payload: Mapping[str, Any],
        task_payloads: Sequence[Mapping[str, Any]],
        payloads_are_authoritative: bool = False,
        skip_task_ids: Collection[str] = (),
    ) -> None:
        """Recover terminal department cards from durable task state.

        Hermes kanban watch can coalesce or miss sibling terminal transitions
        in the same polling window. Re-read all selected primary tasks and
        idempotently project every terminal state into the existing Discord
        request thread.
        """
        if self.discord_delivery is None:
            return

        root_body = str(root_payload.get("body") or "")
        if workflow_mode_from_body(root_body) != "analysis":
            return
        if not is_user_query_body(root_body):
            return

        selected = selected_primary_profiles_from_task(root_payload)
        if len(selected) < 2:
            return

        show = getattr(self.client, "show", None)

        for payload in task_payloads:
            child = ChildTaskState.from_hermes(payload)

            if (
                child.workflow_role != "primary"
                or child.profile not in selected
                or not child.is_in_workflow(root_task_id)
            ):
                continue
            # The terminal handler may have just projected this exact child
            # successfully before entering reconciliation.  Recovery still
            # checks every other terminal sibling, but must not issue a second
            # Discord card update for the already completed projection.  A
            # failed/missing delivery is intentionally not added to this set,
            # so the existing same-handler retry and restart recovery remain.
            if child.task_id in skip_task_ids:
                continue

            candidate = payload
            if callable(show) and not payloads_are_authoritative:
                try:
                    candidate = show(child.task_id)
                    child = ChildTaskState.from_hermes(candidate)
                except HermesKanbanCommandError:
                    candidate = payload

            if not child.terminal:
                continue

            kind = (
                "blocked"
                if child.blocked
                else "failed"
                if child.failed
                else "completed"
            )

            try:
                self._deliver_department_progress(
                    root_task_id=root_task_id,
                    root_payload=root_payload,
                    task_payload=candidate,
                    event={
                        "event_id": f"state-terminal:{child.task_id}:{kind}",
                        "task_id": child.task_id,
                        "kind": kind,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "department-terminal-reconcile-failed "
                    "root=%s task=%s profile=%s error=%s",
                    root_task_id,
                    child.task_id,
                    child.profile,
                    type(exc).__name__,
                )

    def _deliver_department_progress(
        self,
        *,
        root_task_id: str,
        root_payload: Mapping[str, Any],
        task_payload: Mapping[str, Any],
        event: Mapping[str, Any],
    ) -> str | None:
        if self.discord_delivery is None:
            return None

        root_body = str(root_payload.get("body") or "")
        if workflow_mode_from_body(root_body) != "analysis":
            return None
        if not is_user_query_body(root_body):
            return None

        selected = selected_primary_profiles_from_task(root_payload)
        if len(selected) < 2:
            # Keep the existing single-primary fast path quiet.
            return None

        child = ChildTaskState.from_hermes(task_payload)
        if (
            child.workflow_role != "primary"
            or child.profile not in selected
            or not child.is_in_workflow(root_task_id)
        ):
            return None

        kind = str(
            event.get("kind")
            or event.get("event_type")
            or event.get("status")
            or ""
        ).casefold()

        department_result = (
            child.final_answer
            or child.result
            or child.summary
        )

        content = _department_progress_text(
            child.profile,
            kind,
            summary=department_result,
            missing_dependencies=child.missing_dependencies,
            failure_kind=child.failure_kind,
        )
        if not content:
            return None

        logical_kind = (
            "started"
            if kind in {"claimed", "spawned", "started", "running"}
            else kind
        )

        delivery_task = dict(task_payload)
        delivery_task["root_task"] = root_payload

        delivery_environment = getattr(
            self.client,
            "environment",
            os.environ,
        )
        hermes_home = delivery_environment.get(
            "HERMES_HOME",
            "/opt/data",
        )
        ceo_profile_home = os.path.join(
            hermes_home,
            "profiles",
            canonical_profile_for_department("ceo"),
        )
        delivery_home = (
            ceo_profile_home
            if os.path.isdir(ceo_profile_home)
            else hermes_home
        )

        icon, detail_label = DEPARTMENT_DISCORD_LABELS.get(
            child.profile,
            ("🏢", child.profile),
        )

        if logical_kind == "started":
            card_content = (
                f"{icon} **{detail_label}**\n"
                "⏳ 분석 중입니다..."
            )
        elif kind in {"done", "completed"}:
            result_text = str(department_result or "").strip()

            card_content = (
                f"{icon} **{detail_label}**\n"
                "✅ 분석을 완료했습니다."
            )

            if result_text:
                card_content += f"\n\n{result_text}"
        else:
            card_content = content

        try:
            status = self.discord_delivery.upsert_thread_card(
                root_task_id=root_task_id,
                source_task=delivery_task,
                root_task=root_payload,
                content=card_content,
                store=DiscordIdempotencyStore(delivery_home),
                profile=child.profile,
                response_key_suffix=(
                    f"department-card:{child.task_id}"
                ),
                update_existing=(logical_kind != "started"),
            )
        except Exception as exc:
            logger.warning(
                "department-thread-card-failed "
                "root=%s task=%s profile=%s error=%s",
                root_task_id,
                child.task_id,
                child.profile,
                type(exc).__name__,
            )
            return "failed"

        if logical_kind == "started" and status not in {None, "failed"}:
            # Mark only after the Discord projection succeeds.  A failed
            # delivery remains retryable on the next Hermes lifecycle event.
            with self._department_started_progress_lock:
                self._department_started_progress.add(child.task_id)

        logger.info(
            "department-thread-card root=%s task=%s "
            "profile=%s kind=%s status=%s",
            root_task_id,
            child.task_id,
            child.profile,
            kind,
            status,
        )

        return status

    # hgfinance-ready-plan-materializer-v2
    def materialize_ready_primary_plans(
        self,
        *,
        listed_rows: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[SupervisorDecision, ...]:
        """Materialize complete CEO-authored analysis plans before root completion.

        This method does not perform semantic routing. The CEO has already
        selected profiles and written one delegation_instruction per profile.
        The supervisor only validates and materializes that complete plan.

        The root lock serializes this fast path with terminal-event processing.
        Workflow state is rebuilt under the lock before any creation decision.
        """

        list_tasks = getattr(self.client, "list_tasks", None)
        show = getattr(self.client, "show", None)
        workflow = getattr(self.client, "workflow", None)

        if (
            not callable(list_tasks)
            or not callable(show)
            or not callable(workflow)
        ):
            return ()

        # hgfinance-recent-done-root-recovery-v1
        #
        # ready/running planning roots are always eligible.  done roots are a
        # narrow race-recovery path only: scanning historical done roots causes
        # repeated show/workflow/full-list CLI calls and can starve the newest
        # user request.
        import time

        now = int(time.time())
        done_recovery_window_seconds = 120
        candidates: list[tuple[int, str]] = []

        # The recovery lane may provide one board snapshot shared with the
        # synthesis reconciler.  The watch/terminal path remains authoritative;
        # this optional snapshot only removes duplicate read-only list scans
        # inside one recovery cycle.
        if listed_rows is None:
            _record_full_board_fallback(
                lane=current_cli_lane(),
                reason="ready-recovery-discovery-missing",
                root_id="",
            )
            board_rows = list_tasks()
        else:
            board_rows = listed_rows
        for row in board_rows:
            task_id = str(row.get("id") or row.get("task_id") or "")
            body = str(row.get("body") or "")
            status = str(row.get("status") or "").casefold()
            created_at = int(row.get("created_at") or 0)
            completed_at = int(row.get("completed_at") or 0)

            if (
                not task_id
                or status not in {"ready", "running", "done"}
                or (
                    status == "done"
                    and (
                        completed_at <= 0
                        or now - completed_at > done_recovery_window_seconds
                    )
                )
                or (
                    workflow_role_from_body(body) != "root"
                    and not (
                        "root_task_role=scope_and_planning" in body
                        and "planning_terminal_state=done_after_child_creation" in body
                    )
                )
                or not is_user_query_body(body)
                or workflow_mode_from_body(body) != "analysis"
            ):
                continue

            candidates.append((created_at, task_id))

        materialized: list[SupervisorDecision] = []

        # Newest planning roots first.  A fresh user query must never queue
        # behind historical recovery work.
        ordered_root_ids = [
            task_id
            for _, task_id in sorted(
                set(candidates),
                key=lambda item: (item[0], item[1]),
                reverse=True,
            )
        ]

        for root_id in ordered_root_ids:
            with self._parent_lock(root_id):
                root_payload = show(root_id)
                self._remember_workflow_root(root_id, root_id, (root_payload,))
                root_status = str(
                    root_payload.get("status") or ""
                ).casefold()

                # hgfinance-ready-plan-done-recovery-v1
                #
                # A direct CEO planning root can move from ready/running to done
                # faster than one full `kanban list --json` scan. Treat done as
                # recoverable here as long as the root still has a complete,
                # validated analysis delegation plan. Existing primaries are
                # rediscovered under the root lock, so this remains idempotent.
                if root_status not in {"ready", "running", "done"}:
                    continue

                root_body = str(root_payload.get("body") or "")

                if (
                    (
                        workflow_role_from_body(root_body) != "root"
                        and not (
                            "root_task_role=scope_and_planning" in root_body
                            and "planning_terminal_state=done_after_child_creation"
                            in root_body
                        )
                    )
                    or not is_user_query_body(root_body)
                    or workflow_mode_from_body(root_body) != "analysis"
                ):
                    continue

                selected_profiles = (
                    selected_primary_profiles_from_task(root_payload)
                )

                if not selected_profiles:
                    continue

                materialization_body = _materialization_plan_body(root_payload)

                _, payloads = workflow(root_id)

                children = tuple(
                    ChildTaskState.from_hermes(payload)
                    for payload in payloads
                    if payload.get("assignee") is not None
                )

                state = SupervisorState(
                    parent_task_id=root_id,
                    children=children,
                    wakeups=0,
                    replan_count=0,
                    max_retries=self.max_retries,
                    max_wakeups=self.max_wakeups,
                    qa_required=False,
                    workflow_mode="analysis",
                    has_mandate=mandate_snapshot_present(root_body),
                    selected_primary_profiles=selected_profiles,
                    root_is_user_query=True,
                    allow_primary_passthrough=(
                        self.discord_delivery is not None
                    ),
                )

                decisions = _initial_primary_materialization_decisions(
                    state,
                    materialization_body,
                )

                if not decisions:
                    continue

                # hgfinance-parallel-primary-fanout-v1
                #
                # Initial analysis primaries are independent: they deliberately
                # have no execution-parent edges and each has a distinct
                # canonical profile/idempotency key. Run only this initial
                # ready-plan fan-out concurrently. QA, synthesis, binding, and
                # terminal fallback remain sequential.
                if len(decisions) == 1:
                    self._execute(decisions[0], state)
                else:
                    from concurrent.futures import ThreadPoolExecutor

                    with ThreadPoolExecutor(
                        max_workers=min(3, len(decisions)),
                        thread_name_prefix="ceo-primary-create",
                    ) as pool:
                        futures = [
                            pool.submit(
                                copy_context().run,
                                self._execute,
                                decision,
                                state,
                            )
                            for decision in decisions
                        ]

                        # Surface command failures to the outer ready-plan loop.
                        # Successful siblings remain idempotent; a later poll
                        # rebuilds workflow state and retries only missing
                        # profiles.
                        for future in futures:
                            future.result()

                logger.info(
                    "ready-primary-materialized "
                    "root=%s count=%d profiles=%s",
                    root_id,
                    len(decisions),
                    ",".join(
                        decision.assignee or ""
                        for decision in decisions
                    ),
                )

                materialized.extend(decisions)

        return tuple(materialized)

    def reconcile_existing_workflows(self) -> tuple[SupervisorDecision, ...]:
        """Reconcile terminal roots whose watch event was missed.

        The supervisor is normally event-driven, but a restart cannot replay
        terminal events that happened before ``kanban watch`` subscribed. A
        narrow startup reconciliation covers only completed planning roots
        with a durable primary selection and at least one terminal primary.
        It reuses ``handle_terminal_event`` so the normal scope validation,
        idempotency comments, and action guards remain authoritative.
        """

        list_tasks = getattr(self.client, "list_tasks", None)
        show = getattr(self.client, "show", None)
        if not callable(list_tasks) or not callable(show):
            return ()

        roots: dict[str, Mapping[str, Any]] = {}
        _record_full_board_fallback(
            lane=current_cli_lane(),
            reason="startup-reconciliation",
            root_id="",
        )
        for row in list_tasks():
            task_id = str(row.get("id") or row.get("task_id") or "")
            body = str(row.get("body") or "")
            role = terminal_workflow_role(row) or ""
            explicit_legacy_planning_root = (
                role in {"planning", "scope_and_planning"}
                and "root_task_role=scope_and_planning" in body
                and "planning_terminal_state=done_after_child_creation" in body
            )
            if (
                not task_id
                or CEO_WORKFLOW_SCOPE_MARKER not in body
                or not explicit_legacy_planning_root
            ):
                continue
            roots[task_id] = row

        decisions: list[SupervisorDecision] = []
        for root_id in sorted(roots):
            root_payload = show(root_id)
            self._remember_workflow_root(root_id, root_id, (root_payload,))
            root_status = str(root_payload.get("status") or "").casefold()
            if root_status not in {"done", "completed", "archived"}:
                continue
            if not selected_primary_profiles_from_task(root_payload):
                continue

            _, payloads = self.client.workflow(root_id)
            children = tuple(
                ChildTaskState.from_hermes(payload)
                for payload in payloads
                if payload.get("assignee") is not None
            )
            terminal_primary = tuple(
                child
                for child in children
                if child.is_in_workflow(root_id)
                and child.is_analysis
                and child.terminal
            )
            if not terminal_primary:
                continue

            wake_child = next(
                (child for child in terminal_primary if child.done),
                terminal_primary[0],
            )
            event = {
                "event_id": (
                    f"reconcile:{root_id}:{wake_child.task_id}:{wake_child.status}"
                ),
                "task_id": wake_child.task_id,
                "kind": "blocked" if wake_child.blocked else "completed",
            }
            decision = self.handle_terminal_event(event)
            if decision is not None:
                decisions.append(decision)
        return tuple(decisions)

    def reconcile_completed_syntheses(
        self,
        *,
        listed_rows: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[str, ...]:
        """Recover recent completed syntheses whose watch event was missed.

        ``hermes kanban watch`` remains the low-latency path.  This reconciler
        is only a narrow race-recovery lane for recently completed synthesis
        tasks.  ``kanban list --json`` already contains body/status/timestamps,
        so expensive ``kanban show`` calls are reserved for matching candidates.
        """

        list_tasks = getattr(self.client, "list_tasks", None)
        show = getattr(self.client, "show", None)

        if not callable(list_tasks) or not callable(show):
            return ()

        import time

        now = int(time.time())
        done_recovery_window_seconds = 120

        candidates: list[tuple[int, str]] = []

        # See materialize_ready_primary_plans(): both recovery checks can
        # safely consume the same immutable-in-memory list projection. Each
        # candidate is still revalidated through show() before acting.
        if listed_rows is None:
            _record_full_board_fallback(
                lane=current_cli_lane(),
                reason="synthesis-recovery-discovery-missing",
                root_id="",
            )
            board_rows = list_tasks()
        else:
            board_rows = listed_rows
        for row in board_rows:
            task_id = str(row.get("id") or row.get("task_id") or "")
            if not task_id:
                continue

            status = str(row.get("status") or "").casefold()
            completed_at = int(row.get("completed_at") or 0)

            if status not in {"done", "completed", "archived"}:
                continue

            if (
                completed_at <= 0
                or now - completed_at > done_recovery_window_seconds
            ):
                continue

            body = str(row.get("body") or "")
            role = terminal_workflow_role(row) or ""

            if role != "synthesis":
                continue

            action = (
                terminal_action(row)
                or terminal_action({"body": body})
                or ""
            )

            if (
                action != "SYNTHESIZE"
                and not _is_direct_ceo_response_synthesis(
                    role=role,
                    body=body,
                )
            ):
                continue

            if not extract_scope_references(row).root_ids:
                continue

            candidates.append((completed_at, task_id))

        recovered: list[str] = []

        # Newest first so a fresh user request is never starved by older work.
        for _completed_at, task_id in sorted(candidates, reverse=True):
            event_id = f"reconcile-synthesis:{task_id}:done"

            # Avoid repeating the same reconciliation every polling cycle.
            with self._seen_events_lock:
                if event_id in self._seen_events:
                    continue

            try:
                payload = show(task_id)
            except Exception as exc:
                logger.warning(
                    "synthesis-reconcile-show-failed task=%s error=%s",
                    task_id,
                    type(exc).__name__,
                )
                continue

            # Revalidate after the targeted show() in case state changed between
            # list and show.
            status = str(payload.get("status") or "").casefold()
            body = str(payload.get("body") or "")
            role = terminal_workflow_role(payload) or ""
            action = (
                terminal_action(payload)
                or terminal_action({"body": body})
                or ""
            )
            roots = extract_scope_references(payload).root_ids

            if (
                status not in {"done", "completed", "archived"}
                or role != "synthesis"
                or not roots
                or (
                    action != "SYNTHESIZE"
                    and not _is_direct_ceo_response_synthesis(
                        role=role,
                        body=body,
                    )
                )
            ):
                continue

            self.handle_terminal_event(
                {
                    "event_id": event_id,
                    "task_id": task_id,
                    "kind": "completed",
                }
            )
            recovered.append(task_id)

        return tuple(recovered)

    def _materialize_completed_analysis_root_fast(
        self,
        *,
        task_id: str,
        kind: str,
    ) -> tuple[bool, SupervisorDecision | None]:
        """Fast-path a completed CEO analysis root without workflow() reconstruction.

        Returns ``(handled, decision)``.  The normal workflow path remains the
        fallback for child tasks, incomplete plans, and legacy/ambiguous roots.
        Durable create idempotency keeps this safe against the recovery poller.
        """

        if kind not in {"done", "completed"}:
            return False, None

        show = getattr(self.client, "show", None)
        if not callable(show):
            return False, None

        root_payload = show(task_id)
        self._remember_workflow_root(task_id, task_id, (root_payload,))
        root_body = str(root_payload.get("body") or "")

        is_planning_root = (
            workflow_role_from_body(root_body) == "root"
            or (
                "root_task_role=scope_and_planning" in root_body
                and "planning_terminal_state=done_after_child_creation" in root_body
            )
        )

        if (
            not is_planning_root
            or not is_user_query_body(root_body)
            or workflow_mode_from_body(root_body) != "analysis"
        ):
            return False, None

        selected_profiles = selected_primary_profiles_from_task(root_payload)
        materialization_body = _materialization_plan_body(root_payload)

        # A direct CEO answer has no selected primary plan.  It is still a root
        # completion and can be projected immediately without workflow().
        if not selected_profiles:
            try:
                bridge_status = self._bridge_root_completion_to_discord(
                    root_task_id=task_id,
                    root_payload=root_payload,
                )
            except Exception as exc:
                logger.warning(
                    "ceo-root-discord-bridge-failed root=%s error=%s",
                    task_id,
                    type(exc).__name__,
                )
                bridge_status = "failed"

            logger.info(
                "root-planning-complete-fast-projected root=%s status=%s",
                task_id,
                bridge_status,
            )
            return True, None

        state = SupervisorState(
            parent_task_id=task_id,
            children=(),
            wakeups=0,
            replan_count=0,
            max_retries=self.max_retries,
            max_wakeups=self.max_wakeups,
            qa_required=False,
            workflow_mode="analysis",
            has_mandate=mandate_snapshot_present(root_body),
            selected_primary_profiles=selected_profiles,
            root_is_user_query=True,
            allow_primary_passthrough=self.discord_delivery is not None,
        )

        decisions = _initial_primary_materialization_decisions(
            state,
            materialization_body,
        )

        if not decisions:
            # A selected plan that cannot be validated should fall back to the
            # authoritative recovery/workflow path rather than being swallowed.
            return False, None

        if len(decisions) == 1:
            self._execute(decisions[0], state)
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(
                max_workers=min(3, len(decisions)),
                thread_name_prefix="ceo-primary-fast-create",
            ) as pool:
                futures = [
                    pool.submit(
                        copy_context().run,
                        self._execute,
                        decision,
                        state,
                    )
                    for decision in decisions
                ]
                for future in futures:
                    future.result()

        logger.info(
            "ready-primary-fast-materialized root=%s count=%d profiles=%s",
            task_id,
            len(decisions),
            ",".join(decision.assignee or "" for decision in decisions),
        )

        try:
            bridge_status = self._bridge_root_completion_to_discord(
                root_task_id=task_id,
                root_payload=root_payload,
            )
        except Exception as exc:
            logger.warning(
                "ceo-root-discord-bridge-failed root=%s error=%s",
                task_id,
                type(exc).__name__,
            )
            bridge_status = "failed"

        logger.info(
            "root-planning-complete-fast-projected root=%s status=%s",
            task_id,
            bridge_status,
        )
        return True, decisions[0]

    def _cached_workflow_root(self, task_id: str) -> str | None:
        with self._task_root_cache_lock:
            return self._task_root_cache.get(task_id)

    def _remember_workflow_root(
        self,
        task_id: str,
        root_task_id: str,
        payloads: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        if not task_id or not root_task_id:
            return

        with self._task_root_cache_lock:
            self._task_root_cache[task_id] = root_task_id
            self._task_root_cache[root_task_id] = root_task_id

            for payload in payloads:
                child_id = str(
                    payload.get("id")
                    or payload.get("task_id")
                    or ""
                )
                if child_id:
                    self._task_root_cache[child_id] = root_task_id
                    if child_id == root_task_id:
                        self._task_root_payload_cache[root_task_id] = dict(payload)

    def _active_progress_payloads(
        self,
        *,
        task_id: str,
        event: Mapping[str, Any],
    ) -> tuple[str | None, Mapping[str, Any], Mapping[str, Any]]:
        """Return the smallest payload set needed for a progress card.

        Active lifecycle events are UX hints, not reconciliation boundaries.
        The only live read permitted here is ``show(task_id)`` once per task;
        in particular this method must never call ``workflow()``, ``list()``,
        or ``show(root_id)``.  A root payload is reused from a terminal path or
        an event envelope when available.  Without it, the caller simply
        skips the optional Discord projection and leaves terminal recovery as
        the authoritative path.
        """

        task_payload: Mapping[str, Any] | None = None
        for key in ("task_payload", "task", "payload"):
            candidate = event.get(key)
            if isinstance(candidate, Mapping):
                task_payload = candidate
                break
        if task_payload is None and "body" in event:
            task_payload = event

        with self._task_root_cache_lock:
            cached_task = self._active_task_payload_cache.get(task_id)
            cached_root_id = self._task_root_cache.get(task_id)
            cached_root_payload = self._task_root_payload_cache.get(
                cached_root_id or "",
                {},
            )
        if task_payload is None and cached_task:
            task_payload = cached_task

        if task_payload is None:
            show = getattr(self.client, "show", None)
            if not callable(show):
                return cached_root_id, cached_root_payload, {}
            # This is the one and only authoritative active-event read.  Do
            # not replace it with workflow_root(): that method may walk
            # ancestry and would reintroduce the expensive hot path.
            task_payload = show(task_id)
            if not isinstance(task_payload, Mapping):
                return cached_root_id, cached_root_payload, {}
            with self._task_root_cache_lock:
                self._active_task_payload_cache[task_id] = dict(task_payload)

        task_payload = dict(task_payload)
        if not task_payload.get("id") and not task_payload.get("task_id"):
            task_payload["id"] = task_id

        root_payload: Mapping[str, Any] = {}
        for key in ("root_payload", "root_task"):
            candidate = event.get(key)
            if isinstance(candidate, Mapping):
                root_payload = candidate
                break
        if not root_payload:
            for key in ("root_payload", "root_task"):
                candidate = task_payload.get(key)
                if isinstance(candidate, Mapping):
                    root_payload = candidate
                    break

        root_id = cached_root_id
        if not root_id:
            explicit_root = event.get("root_task_id") or task_payload.get(
                "workflow_root_task_id"
            )
            root_id = str(explicit_root or "").strip() or None
        if not root_id:
            root_id = terminal_workflow_root(task_payload) or None
        if not root_id:
            scope = extract_scope_references(task_payload)
            root_id = scope.root_ids[0] if scope.root_ids else None
        if not root_id:
            root_id = str(
                task_payload.get("id") or task_payload.get("task_id") or ""
            ).strip() or None

        if root_id and not root_payload:
            with self._task_root_cache_lock:
                root_payload = self._task_root_payload_cache.get(root_id, {})
        if root_id and not root_payload and root_id == task_id:
            root_payload = task_payload

        if root_id:
            self._remember_workflow_root(task_id, root_id)
        return root_id, root_payload, task_payload

    def _publish_head_card_activity(
        self, *, task_id: str, kind: str, event: Mapping[str, Any]
    ) -> None:
        """카드를 끝낸 부서장 1턴을 HR 관측으로 내보낸다. 실패는 삼킨다.

        신원(head_persona)과 stage 는 orchestration/llm_observability 의 공용
        해석기를 쓴다 - BFF 의 직접 호출 경로와 **같은 이름**이어야 같은 부서장의
        활동으로 합쳐진다.
        """

        try:
            from orchestration.llm_observability import (
                head_persona_for_profile,
                publish_head_activity,
                stage_for_profile,
            )

            profile = str(event.get("assignee") or "").strip()
            if not profile:
                return
            stage = stage_for_profile(profile)
            persona = head_persona_for_profile(profile)
            if not stage or not persona:
                # 모르는 프로필은 stage 를 지어내지 않는다 - 틀린 stage 로 나간
                # 이벤트는 조회되지 않으면서 있는 것처럼 보인다.
                return
            if kind in {"done", "completed", "archived"}:
                status, errors = "COMPLETED", 0
            elif kind in {"blocked"}:
                status, errors = "BLOCKED", 0
            else:
                status, errors = "DEGRADED", 1
            publish_head_activity(
                stage=stage,
                head_persona=persona,
                status=status,
                error_count=errors,
                trace_id=task_id,
                source="kanban_card",
            )
        except Exception:  # noqa: BLE001 - 계측이 워크플로를 멈추지 못한다
            logger.debug("head-card-activity-publish-skipped task=%s", task_id)

    def handle_terminal_event(self, event: Mapping[str, Any]) -> SupervisorDecision | None:
        handler_started_ms = time.time_ns() // 1_000_000
        event_consumed_ms = int(
            event.get("_event_consumed_ms") or handler_started_ms
        )
        event_created_ms = int(event.get("_event_created_ms") or 0)
        event_persisted_ms = int(
            event.get("_event_persisted_ms") or event_created_ms
        )
        task_id = str(event.get("task_id") or event.get("id") or "")
        kind = str(event.get("kind") or event.get("event_type") or event.get("status") or "").casefold()
        if not task_id:
            return None

        if kind in {"claimed", "spawned", "started", "running"}:
            event_assignee = str(event.get("assignee") or "").strip().casefold()
            progress_profiles = set(DEPARTMENT_DISCORD_LABELS) | {
                "research-liaison",
                "quant-liaison",
            }
            if event_assignee and event_assignee not in progress_profiles:
                return None

            # Fast rejection before workflow()/show().  A successful initial
            # Discord "started" projection is enough for every equivalent
            # active lifecycle event for this task.
            with self._department_started_progress_lock:
                if task_id in self._department_started_progress:
                    return None

            try:
                root_id, root_payload, task_payload = self._active_progress_payloads(
                    task_id=task_id,
                    event=event,
                )
                if root_id and task_payload:
                    self._deliver_department_progress(
                        root_task_id=root_id,
                        root_payload=root_payload,
                        task_payload=task_payload,
                        event=event,
                    )
            except Exception as exc:
                # Progress projection must never interfere with execution.
                logger.warning(
                    "department-discord-progress-failed task=%s kind=%s error=%s",
                    task_id,
                    kind,
                    type(exc).__name__,
                )
            return None

        if kind in NON_TERMINAL_EVENT_KINDS:
            return None
        if kind not in TERMINAL_EVENT_KINDS and kind not in TERMINAL_STATUSES:
            return None

        event_key = str(event.get("event_id") or f"{task_id}:{kind}")
        transition_key = (
            f"{task_id}:completed"
            if kind in {"done", "completed", "archived"}
            else ""
        )
        with self._seen_events_lock:
            if (
                event_key in self._seen_events
                or (
                    transition_key
                    and transition_key in self._seen_terminal_transitions
                )
            ):
                logger.info(
                    "supervisor-terminal-duplicate-suppressed "
                    "task=%s kind=%s event=%s",
                    task_id,
                    kind,
                    event_key,
                )
                return None
            self._seen_events.add(event_key)
            if transition_key:
                self._seen_terminal_transitions.add(transition_key)

        # ── HR 관측: 이 카드를 끝낸 부서장 1턴 (2026-08-20) ──────────────────
        #
        # Discord/웹에서 들어온 사용자 질의는 BFF 가 부서장을 직접 부르지 않는다 -
        # 카드를 만들고 Hermes 게이트웨이가 자기 컨테이너 안에서 실행한다. 그래서
        # apps/api/hermes_boundary.ask() 에 붙인 계측이 이 경로를 못 본다. 이
        # 지점이 우리 코드가 "그 부서장이 실제로 일을 끝냈다"를 아는 유일한 자리다.
        #
        # 여기 두는 이유: 바로 위 중복 억제(_seen_events/_seen_terminal_transitions)를
        # 통과한 뒤라 **전이 1건당 정확히 한 번** 실행된다. 아래 워크플로 재구성은
        # 예외·재시도가 있어 같은 카드가 여러 번 지날 수 있다.
        #
        # 지속시간은 보내지 않는다 - 이 이벤트가 아는 것은 "끝났다"는 사실뿐이고,
        # 카드 생성 시각은 여기서 신뢰할 수 있게 얻지 못한다(안 잰 값을 0 으로
        # 채우지 않는다).
        self._publish_head_card_activity(task_id=task_id, kind=kind, event=event)

        # Root fast-path is relevant only to CEO-authored terminal tasks.
        # Department primary events must not pay an extra show() call merely to
        # discover that they are not roots.
        event_assignee = str(event.get("assignee") or "").strip().casefold()
        ceo_assignee = canonical_profile_for_department("ceo").casefold()

        if event_assignee == ceo_assignee:
            try:
                handled, fast_decision = self._materialize_completed_analysis_root_fast(
                    task_id=task_id,
                    kind=kind,
                )
                if handled:
                    return fast_decision
            except (SupervisorValidationError, HermesKanbanCommandError):
                with self._seen_events_lock:
                    self._seen_events.discard(event_key)
                    self._seen_terminal_transitions.discard(transition_key)
                raise
            except Exception as exc:
                # Fast path is an optimization only. Any ambiguity falls through
                # to the existing authoritative workflow reconstruction.
                logger.warning(
                    "root-fast-materialization-fallback task=%s error=%s",
                    task_id,
                    type(exc).__name__,
                )

        try:
            root_id = self._cached_workflow_root(task_id)

            if root_id is None:
                # Root discovery is immutable and does not require the full
                # workflow snapshot.  Production clients use one targeted
                # show() here; small/legacy adapters retain the old workflow()
                # fallback.  Fresh workflow state is still read exactly once
                # after taking the root lock below.
                workflow_root = getattr(self.client, "workflow_root", None)
                if callable(workflow_root):
                    root_id = workflow_root(task_id)
                    self._remember_workflow_root(task_id, root_id)
                else:
                    root_id, initial_payloads = self.client.workflow(task_id)
                    self._remember_workflow_root(
                        task_id,
                        root_id,
                        initial_payloads,
                    )

            root_resolved_ms = time.time_ns() // 1_000_000
            with self._parent_lock(root_id):
                lock_acquired_ms = time.time_ns() // 1_000_000
                # Keep one authoritative read after acquiring the workflow lock.
                # The cache removes only redundant root discovery; it never
                # replaces freshness-sensitive workflow reconstruction.
                authoritative_snapshot = getattr(
                    self.client,
                    "authoritative_workflow_snapshot",
                    None,
                )
                payloads_are_authoritative = callable(authoritative_snapshot)
                if callable(authoritative_snapshot):
                    root_id, payloads, root_payload = authoritative_snapshot(
                        root_id,
                        task_id,
                    )
                else:
                    root_id, payloads = self.client.workflow(task_id)
                    root_payload = {}
                self._remember_workflow_root(
                    task_id,
                    root_id,
                    (root_payload, *payloads),
                )
                # The production Hermes client exposes ``show`` for durable
                # wakeup comments. Keep the policy service compatible with small
                # workflow-only fakes and adapters used by the supervisor tests.
                show = getattr(self.client, "show", None)
                if not root_payload:
                    root_payload = show(root_id) if callable(show) else {}
                workflow_ready_ms = time.time_ns() // 1_000_000
                timing_task = next(
                    (
                        payload
                        for payload in payloads
                        if str(
                            payload.get("id")
                            or payload.get("task_id")
                            or ""
                        ) == task_id
                    ),
                    {},
                )
                task_completed_ms = _task_timestamp_ms(
                    timing_task, "completed_at"
                )
                logger.info(
                    "supervisor-terminal-timing root=%s task=%s kind=%s "
                    "task_completed=%d event_created=%d event_persisted=%d "
                    "event_consumed=%d "
                    "handler_started=%d root_resolved=%d "
                    "lock_acquired=%d workflow_ready=%d "
                    "created_to_consumed_ms=%d completion_to_consumed_ms=%d "
                    "queue_wait_ms=%d consumed_to_lock_ms=%d "
                    "root_resolution_ms=%d "
                    "lock_wait_ms=%d locked_workflow_ms=%d",
                    root_id,
                    task_id,
                    kind,
                    task_completed_ms,
                    event_created_ms,
                    event_persisted_ms,
                    event_consumed_ms,
                    handler_started_ms,
                    root_resolved_ms,
                    lock_acquired_ms,
                    workflow_ready_ms,
                    _elapsed_ms(event_created_ms, event_consumed_ms),
                    _elapsed_ms(task_completed_ms, event_consumed_ms),
                    _elapsed_ms(event_consumed_ms, handler_started_ms),
                    _elapsed_ms(event_consumed_ms, lock_acquired_ms),
                    _elapsed_ms(handler_started_ms, root_resolved_ms),
                    _elapsed_ms(root_resolved_ms, lock_acquired_ms),
                    _elapsed_ms(lock_acquired_ms, workflow_ready_ms),
                )

                def execute_timed(
                    action_decision: SupervisorDecision,
                    action_state: SupervisorState,
                ) -> None:
                    action_started_ms = time.time_ns() // 1_000_000
                    try:
                        self._execute(action_decision, action_state)
                    finally:
                        action_completed_ms = time.time_ns() // 1_000_000
                        logger.info(
                            "supervisor-action-timing root=%s task=%s event=%s "
                            "action=%s workflow_ready=%d action_started=%d "
                            "action_completed=%d workflow_to_action_ms=%d "
                            "action_duration_ms=%d",
                            root_id,
                            task_id,
                            event_key,
                            action_decision.action.value,
                            workflow_ready_ms,
                            action_started_ms,
                            action_completed_ms,
                            _elapsed_ms(workflow_ready_ms, action_started_ms),
                            _elapsed_ms(action_started_ms, action_completed_ms),
                        )
                # The root is a planning/scope task in the current contract. Its
                # terminal transition means planning finished, not that the
                # workflow is ready for synthesis. Primary child events are the
                # wake-up boundary.
                root_body = str(root_payload.get("body") or "")
                if root_id == task_id and kind in {"done", "completed"}:
                    legacy_planning_root = (
                        "root_task_role=scope_and_planning" in root_body
                        and "planning_terminal_state=done_after_child_creation" in root_body
                    )
                    if legacy_planning_root:
                        # Root completion remains a planning boundary, never a
                        # synthesis-ready signal.  Project only the CEO-authored
                        # durable outcome into the already-existing Discord thread.
                        try:
                            bridge_status = self._bridge_root_completion_to_discord(
                                root_task_id=root_id,
                                root_payload=root_payload,
                            )
                        except Exception as exc:
                            # UI projection must never mutate or block workflow
                            # execution. Existing primary events remain the
                            # deterministic wake-up boundary.
                            logger.warning(
                                "ceo-root-discord-bridge-failed "
                                "root=%s error=%s",
                                root_id,
                                type(exc).__name__,
                            )
                            bridge_status = "failed"

                        logger.info(
                            "root-planning-complete-projected "
                            "root=%s event=%s status=%s",
                            root_id,
                            event_key,
                            bridge_status,
                        )
                        return None
                try:
                    validate_workflow_scope(
                        root_task_id=root_id,
                        root_payload=root_payload,
                        descendants=payloads,
                    )
                    workflow_mode = workflow_mode_from_body(root_body)
                    user_paper_order_scope = user_paper_order_scope_from_body(
                        root_body
                    )
                    validation_completed_ms = time.time_ns() // 1_000_000
                except WorkflowScopeViolation as exc:
                    reason = f"workflow_scope_validation: {exc}"
                    comment_task = getattr(self.client, "comment_task", None)
                    if callable(comment_task):
                        comment_task(
                            root_id,
                            f"hgfinance.ceo-workflow-scope-error.v1 "
                            f"event={event_key} reason={reason}",
                        )
                    self._safe_abort(root_id, reason)
                    return SupervisorDecision(
                        SupervisorAction.BLOCK_ABORT,
                        root_id,
                        reason="workflow_scope_validation",
                    )
                terminal_task_payload = next(
                    (
                        payload
                        for payload in payloads
                        if str(
                            payload.get("id")
                            or payload.get("task_id")
                            or ""
                        ) == task_id
                    ),
                    None,
                )
                terminal_observers_projected = False
                terminal_progress_status: str | None = None
                observer_started_ms = 0
                observer_completed_ms = 0

                def project_terminal_observers() -> None:
                    nonlocal terminal_observers_projected, terminal_task_payload
                    nonlocal terminal_progress_status
                    nonlocal observer_started_ms, observer_completed_ms
                    if terminal_observers_projected:
                        return
                    terminal_observers_projected = True
                    observer_started_ms = time.time_ns() // 1_000_000
                    try:
                        terminal_projection_status = self._project_terminal_task(
                            root_task_id=root_id,
                            task_id=task_id,
                            task_payloads=(root_payload, *payloads),
                            event=event,
                        )
                        if (
                            terminal_role == "synthesis"
                            and terminal_projection_status not in {"sent", "deduped"}
                        ):
                            # A later recovery wakeup may retry a failed or
                            # not-yet-correlatable delivery. Concurrent duplicates
                            # remain coalesced because this runs after the response
                            # attempt has completed.
                            with self._seen_events_lock:
                                self._seen_terminal_transitions.discard(
                                    transition_key
                                )

                        if terminal_task_payload is not None:
                            try:
                                terminal_progress_status = (
                                    self._deliver_department_progress(
                                        root_task_id=root_id,
                                        root_payload=root_payload,
                                        task_payload=terminal_task_payload,
                                        event=event,
                                    )
                                )
                            except Exception as exc:
                                logger.warning(
                                    "department-discord-progress-failed "
                                    "task=%s kind=%s error=%s",
                                    task_id,
                                    kind,
                                    type(exc).__name__,
                                )

                        self._reconcile_department_terminal_progress(
                            root_task_id=root_id,
                            root_payload=root_payload,
                            task_payloads=payloads,
                            payloads_are_authoritative=payloads_are_authoritative,
                            skip_task_ids=(
                                (task_id,)
                                if terminal_progress_status
                                in {"created", "updated", "unchanged", "deduped", "sent"}
                                else ()
                            ),
                        )
                    finally:
                        observer_completed_ms = time.time_ns() // 1_000_000
                        logger.info(
                            "supervisor-observer-timing root=%s task=%s event=%s "
                            "observer_started=%d observer_completed=%d "
                            "observer_duration_ms=%d",
                            root_id,
                            task_id,
                            event_key,
                            observer_started_ms,
                            observer_completed_ms,
                            _elapsed_ms(observer_started_ms, observer_completed_ms),
                        )

                terminal_role = (
                    terminal_workflow_role(terminal_task_payload)
                    if terminal_task_payload is not None
                    else ""
                )
                if terminal_role == "synthesis":
                    # Discord delivery is the response action for this event.
                    project_terminal_observers()

                children = tuple(
                    ChildTaskState.from_hermes(payload)
                    for payload in payloads
                    if payload.get("assignee") is not None
                )
                unmarked_primary_ids = tuple(
                    child.task_id
                    for child in children
                    if child.is_in_workflow(root_id)
                    and not child.workflow_role
                    and not child.is_background_research
                    and child.profile
                    in {
                        canonical_profile_for_department(name)
                        for name in PRIMARY_DEPARTMENTS
                    }
                )
                if unmarked_primary_ids:
                    logger.warning(
                        "primary-unmarked-excluded root=%s task_ids=%s",
                        root_id,
                        ",".join(unmarked_primary_ids),
                    )
                existing_wakeups = self._wakeup_comments(root_payload)
                if event_key in existing_wakeups and "state=done" in existing_wakeups[event_key]:
                    return None
                wakeups = self._wakeup_budget(existing_wakeups)
                durable_replans = sum(
                    1
                    for child in children
                    if SUPERVISOR_MARKER in child.body
                    and "action=CREATE_TASK" in child.body
                )
                # Single-primary passthrough needs the terminal run metadata
                # (especially final_answer). Legacy workflow() may carry only a
                # shallow task projection, so hydrate exactly one selected
                # primary in that compatibility path. The authoritative client
                # already returned the same show() payloads above; re-reading
                # them only adds latency and API load.
                selected_profiles = selected_primary_profiles_from_task(root_payload)
                if (
                    workflow_mode == "analysis"
                    and is_user_query_body(root_body)
                    and len(selected_profiles) == 1
                ):
                    selected_profile = selected_profiles[0]
                    hydrated_children = []
                    for child in children:
                        if (
                            child.profile == selected_profile
                            and child.is_in_workflow(root_id)
                            and child.workflow_role == "primary"
                            and (child.done or child.blocked or child.failed)
                            and not payloads_are_authoritative
                        ):
                            try:
                                hydrated_payload = self.client.show(child.task_id)
                                child = ChildTaskState.from_hermes(hydrated_payload)
                                logger.info(
                                    "single-primary-hydrated root=%s task=%s "
                                    "profile=%s final_answer=%s",
                                    root_id,
                                    child.task_id,
                                    child.profile,
                                    str(bool(child.final_answer)).lower(),
                                )
                            except HermesKanbanCommandError as exc:
                                # Safe fallback: if hydration fails, keep the
                                # shallow child. The existing CEO synthesis path
                                # remains available because final_answer will be
                                # empty.
                                logger.warning(
                                    "single-primary-hydration-failed root=%s "
                                    "task=%s error=%s",
                                    root_id,
                                    child.task_id,
                                    type(exc).__name__,
                                )
                        hydrated_children.append(child)
                    children = tuple(hydrated_children)

                state = SupervisorState(
                    parent_task_id=root_id,
                    children=children,
                    # Evaluate ordinary workflow phase transitions without
                    # spending the safety budget.  If the candidate action is
                    # a retry/replan/abort, the bounded second evaluation
                    # below applies the persisted budget.
                    wakeups=0,
                    replan_count=max(self._replans.get(root_id, 0), durable_replans),
                    max_retries=self.max_retries,
                    max_wakeups=self.max_wakeups,
                    # The precreated Trading primary owns strict PAPER-order
                    # interpretation/tool execution; strategy QA is not an
                    # execution gate for this direct-user lane.
                    qa_required=(
                        False
                        if user_paper_order_scope is not None
                        else self._qa_required_from_event(event)
                    ),
                    workflow_mode=workflow_mode,
                    has_mandate=mandate_snapshot_present(root_body),
                    selected_primary_profiles=selected_profiles,
                    root_is_user_query=is_user_query_body(root_body),
                    allow_primary_passthrough=self.discord_delivery is not None,
                )

                initial_primary_decisions = (
                    _initial_primary_materialization_decisions(
                        state,
                        root_body,
                    )
                )
                if initial_primary_decisions:
                    for initial_primary_decision in initial_primary_decisions:
                        execute_timed(initial_primary_decision, state)

                    logger.info(
                        "initial-primary-materialized root=%s count=%d profiles=%s",
                        root_id,
                        len(initial_primary_decisions),
                        ",".join(
                            decision.assignee or ""
                            for decision in initial_primary_decisions
                        ),
                    )


                passthrough = _single_primary_passthrough_child(state)
                if (
                    passthrough is not None
                    and passthrough.task_id == task_id
                    and self.discord_delivery is not None
                ):
                    primary_payload = next(
                        (
                            payload
                            for payload in payloads
                            if str(payload.get("id") or payload.get("task_id") or "")
                            == passthrough.task_id
                        ),
                        {},
                    )
                    delivery_task = dict(primary_payload)
                    delivery_task["root_task"] = root_payload

                    delivery_environment = getattr(
                        self.client, "environment", os.environ
                    )
                    hermes_home = delivery_environment.get(
                        "HERMES_HOME", "/opt/data"
                    )
                    ceo_profile_home = os.path.join(
                        hermes_home,
                        "profiles",
                        canonical_profile_for_department("ceo"),
                    )
                    delivery_home = (
                        ceo_profile_home
                        if os.path.isdir(ceo_profile_home)
                        else hermes_home
                    )

                    delivery_store = DiscordIdempotencyStore(delivery_home)
                    ceo_profile = canonical_profile_for_department("ceo")

                    # Single-primary/PAPER fast paths must follow the same
                    # thread-first policy as normal CEO synthesis.
                    delivery_status = (
                        self.discord_delivery.deliver_to_existing_thread(
                            root_task_id=root_id,
                            source_task=delivery_task,
                            root_task=root_payload,
                            content=passthrough.final_answer,
                            title="🧠 CEO 답변",
                            store=delivery_store,
                            profile=ceo_profile,
                            response_key_suffix=(
                                f"single-primary-detail:{passthrough.task_id}"
                            ),
                        )
                    )

                    if delivery_status == "missing_thread":
                        delivery_status = self.discord_delivery.deliver(
                            root_task_id=root_id,
                            synthesis_task=delivery_task,
                            content=passthrough.final_answer,
                            store=delivery_store,
                            profile=ceo_profile,
                        )

                    logger.info(
                        "single-primary-passthrough root=%s task=%s "
                        "profile=%s status=%s",
                        root_id,
                        passthrough.task_id,
                        passthrough.profile,
                        delivery_status,
                    )

                decision_started_ms = time.time_ns() // 1_000_000
                decision = self.decider(state)
                decision_completed_ms = time.time_ns() // 1_000_000
                if (
                    wakeups >= self.max_wakeups
                    and self._consumes_wakeup_budget(
                        decision.action if decision is not None else None
                    )
                ):
                    decision = self.decider(replace(state, wakeups=wakeups))
                if state.primary_ready:
                    logger.info(
                        "primary-ready root=%s selected=%d ready=%d",
                        root_id,
                        len(state.selected_primary_profiles) or len(state.primary_by_profile),
                        state.ready_count,
                    )
                elif state.duplicate_primary_profiles:
                    logger.warning(
                        "primary-duplicate-detected primary-integrity root=%s "
                        "selected=%d duplicate_profiles=%s ready=false",
                        root_id,
                        len(state.selected_primary_profiles),
                        ",".join(state.duplicate_primary_profiles),
                    )
                action = decision.action.value if decision is not None else "NONE"
                if decision is not None and state.has_action(decision.action):
                    # A durable supervisor child is the idempotency record for
                    # actions that create work.  This also covers a daemon
                    # restart after the Hermes CLI succeeded but before the
                    # watch loop acknowledged the event.
                    decision = None
                    action = "NONE"
                budget_consumed = self._consumes_wakeup_budget(
                    decision.action if decision is not None else None
                )
                if event_key not in existing_wakeups:
                    self._record_wakeup(
                        root_task_id=root_id,
                        event_id=event_key,
                        kind=kind,
                        action=action,
                        existing=existing_wakeups,
                        state="started",
                        budget_consumed=budget_consumed,
                    )
                if decision is None:
                    project_terminal_observers()
                    logger.info(
                        "supervisor-handler-stage-timing root=%s task=%s event=%s "
                        "workflow_ready=%d validation_completed=%d decision_started=%d "
                        "decision_completed=%d observer_started=%d observer_completed=%d "
                        "decision_duration_ms=%d",
                        root_id,
                        task_id,
                        event_key,
                        workflow_ready_ms,
                        validation_completed_ms,
                        decision_started_ms,
                        decision_completed_ms,
                        observer_started_ms,
                        observer_completed_ms,
                        _elapsed_ms(decision_started_ms, decision_completed_ms),
                    )
                    self._record_wakeup(
                        root_task_id=root_id,
                        event_id=event_key,
                        kind=kind,
                        action=action,
                        existing=existing_wakeups,
                        state="done",
                        budget_consumed=budget_consumed,
                    )
                    logger.info(
                        "supervisor-wakeup root=%s event=%s action=%s budget_consumed=%s",
                        root_id,
                        event_key,
                        action,
                        str(budget_consumed).lower(),
                    )
                    return None
                action_key = ":".join(
                    (
                        root_id,
                        decision.action.value,
                        decision.target_task_id or decision.reason,
                        str(
                            state.replan_count
                            if decision.action == SupervisorAction.CREATE_TASK
                            else decision.retry_count
                        ),
                    )
                )
                with self._seen_events_lock:
                    action_already_executed = action_key in self._executed_actions
                    if not action_already_executed:
                        self._executed_actions.add(action_key)
                if action_already_executed:
                    project_terminal_observers()
                    return None
                execute_timed(decision, state)
                if (
                    decision.action == SupervisorAction.RUN_QA
                    and state.workflow_mode == "analysis"
                ):
                    # FAST path with race protection.
                    #
                    # Synthesis depends only on the already-terminal primary
                    # results, not QA. We still need to observe a synthesis that
                    # may have been created concurrently while RUN_QA was being
                    # created, but rebuilding the complete workflow is expensive
                    # because it launches multiple Hermes CLI subprocesses.
                    #
                    # Prefer one board-list read and inspect only durable workflow
                    # markers. Production clients use indexed candidate IDs and
                    # authoritative targeted show() calls. Small test/fake
                    # clients and uncertain indexes retain the old fallback.
                    synthesis_exists = False
                    indexed_synthesis = getattr(
                        self.client,
                        "authoritative_synthesis_exists",
                        None,
                    )

                    if callable(indexed_synthesis):
                        try:
                            synthesis_exists = indexed_synthesis(root_id)
                        except (
                            RootScopedIndexUnavailable,
                            HermesKanbanCommandError,
                            KeyError,
                        ) as exc:
                            _record_full_board_fallback(
                                lane=current_cli_lane(),
                                reason=(
                                    "synthesis-index-uncertain-"
                                    f"{type(exc).__name__}"
                                ),
                                root_id=root_id,
                            )
                            indexed_synthesis = None

                    if indexed_synthesis is None:
                        list_tasks = getattr(self.client, "list_tasks", None)
                        if callable(list_tasks):
                            for row in list_tasks():
                                body = str(row.get("body") or "")
                                row_role = terminal_workflow_role(row) or ""
                                row_action = (
                                    terminal_action(row)
                                    or terminal_action({"body": body})
                                    or ""
                                )
                                row_roots = extract_scope_references(row).root_ids

                                if (
                                    root_id in row_roots
                                    and row_role == "synthesis"
                                    and (
                                        row_action == "SYNTHESIZE"
                                        or _is_direct_ceo_response_synthesis(
                                            role=row_role,
                                            body=body,
                                        )
                                    )
                                ):
                                    synthesis_exists = True
                                    break
                        else:
                            # Small/fake clients without list_tasks retain the
                            # previous full-workflow fallback.
                            _, refreshed_payloads = self.client.workflow(root_id)
                            synthesis_exists = any(
                                child.is_in_workflow(root_id)
                                and child.workflow_role == "synthesis"
                                and (
                                    (
                                        SUPERVISOR_MARKER in child.body
                                        and "action=SYNTHESIZE" in child.body
                                    )
                                    or _is_direct_ceo_response_synthesis(
                                        role=child.workflow_role,
                                        body=child.body,
                                    )
                                )
                                for child in (
                                    ChildTaskState.from_hermes(payload)
                                    for payload in refreshed_payloads
                                    if payload.get("assignee") is not None
                                )
                            )

                    if not synthesis_exists:
                        synthesis = _analysis_synthesis_decision(state)
                        if (
                            synthesis is not None
                            and synthesis.action == SupervisorAction.SYNTHESIZE
                        ):
                            execute_timed(synthesis, state)
                            action = f"{action},SYNTHESIZE"
                if decision.action == SupervisorAction.CREATE_TASK:
                    self._replans[root_id] = self._replans.get(root_id, 0) + 1
                project_terminal_observers()
                logger.info(
                    "supervisor-handler-stage-timing root=%s task=%s event=%s "
                    "workflow_ready=%d validation_completed=%d decision_started=%d "
                    "decision_completed=%d observer_started=%d observer_completed=%d "
                    "decision_duration_ms=%d",
                    root_id,
                    task_id,
                    event_key,
                    workflow_ready_ms,
                    validation_completed_ms,
                    decision_started_ms,
                    decision_completed_ms,
                    observer_started_ms,
                    observer_completed_ms,
                    _elapsed_ms(decision_started_ms, decision_completed_ms),
                )
                self._record_wakeup(
                    root_task_id=root_id,
                    event_id=event_key,
                    kind=kind,
                    action=action,
                    existing=existing_wakeups,
                    state="done",
                    budget_consumed=budget_consumed,
                )
                logger.info(
                    "supervisor-wakeup root=%s event=%s action=%s budget_consumed=%s",
                    root_id,
                    event_key,
                    action,
                    str(budget_consumed).lower(),
                )
                return decision
        except CanonicalProfileError as exc:
            root_for_abort = locals().get("root_id", task_id)
            self._safe_abort(str(root_for_abort), f"canonical profile validation: {exc}")
            return SupervisorDecision(
                SupervisorAction.BLOCK_ABORT,
                str(root_for_abort),
                reason="canonical_profile_validation",
            )
        except (SupervisorValidationError, HermesKanbanCommandError) as exc:
            with self._seen_events_lock:
                # A failed workflow operation is retryable on the next
                # delivery.  Canonical failures are handled and blocked
                # above, so they intentionally remain acknowledged.
                self._seen_events.discard(event_key)
                self._seen_terminal_transitions.discard(transition_key)
            raise SupervisorWorkflowError(
                f"workflow {task_id} evaluation failed: {exc}"
            ) from exc

    def _qa_required_from_event(self, event: Mapping[str, Any]) -> bool:
        """Read an explicit CEO completion decision; default remains QA on."""

        value: Any = event.get("qa_required")
        metadata = event.get("metadata")
        if value is None and isinstance(metadata, Mapping):
            value = metadata.get("qa_required")
        if value is None:
            return self.qa_required
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.casefold() in {"true", "false"}:
            return value.casefold() == "true"
        raise SupervisorValidationError("qa_required must be a boolean")

    def _execute(self, decision: SupervisorDecision, state: SupervisorState) -> None:
        allowed_parent_ids = {state.parent_task_id} | {
            child.task_id for child in state.children
        }
        requested_parent_ids = set(decision.parent_task_ids)
        outside_parent_ids = requested_parent_ids - allowed_parent_ids
        if outside_parent_ids:
            raise SupervisorValidationError(
                "supervisor action references task IDs outside current root: "
                f"{sorted(outside_parent_ids)}"
            )

        if decision.action == SupervisorAction.RETRY_TASK:
            if not decision.target_task_id:
                raise SupervisorValidationError("RETRY_TASK has no target")
            if decision.target_task_id not in {
                child.task_id for child in state.children
            }:
                raise SupervisorValidationError(
                    "RETRY_TASK target is outside current root workflow"
                )
            self.client.unblock_task(decision.target_task_id)
            return
        if decision.action == SupervisorAction.BLOCK_ABORT:
            self.client.block_task(decision.parent_task_id, decision.reason or "supervisor aborted")
            return
        if decision.action == SupervisorAction.REQUEST_USER_INPUT:
            self.client.create_task(
                title=decision.title or "CEO requires user input",
                body=build_scoped_task_body(
                    decision.body or f"{SUPERVISOR_MARKER} action=REQUEST_USER_INPUT",
                    state.parent_task_id,
                    role="control",
                    workflow_mode=state.workflow_mode,
                    has_mandate=state.has_mandate,
                ),
                assignee=decision.assignee or canonical_profile_for_department("ceo"),
                parent_task_ids=decision.parent_task_ids,
                idempotency_key=f"{state.parent_task_id}:supervisor:user-input",
                initial_status="blocked",
            )
            return
        if decision.action in {
            SupervisorAction.CREATE_TASK,
            SupervisorAction.RUN_QA,
            SupervisorAction.SYNTHESIZE,
        }:
            if not decision.assignee or not decision.title or not decision.body:
                raise SupervisorValidationError(f"{decision.action.value} lacks create fields")
            if decision.action == SupervisorAction.RUN_QA:
                expected = {
                    child.task_id
                    for child in state.analysis_children
                    if child.done
                }
                if requested_parent_ids != expected:
                    raise SupervisorValidationError(
                        "RUN_QA dependencies must be the current root's "
                        f"primary children: expected {sorted(expected)}, "
                        f"got {sorted(requested_parent_ids)}"
                    )
            elif decision.action == SupervisorAction.SYNTHESIZE:
                expected = (
                    {
                        child.task_id
                        for child in state.analysis_children
                        if child.done
                    }
                    if state.workflow_mode == "analysis"
                    else {child.task_id for child in state.qa_children if child.done}
                )
                if requested_parent_ids != expected:
                    raise SupervisorValidationError(
                        "SYNTHESIZE dependencies do not match workflow mode: "
                        f"expected {sorted(expected)}, "
                        f"got {sorted(requested_parent_ids)}"
                    )
            role = {
                SupervisorAction.CREATE_TASK: "primary",
                SupervisorAction.RUN_QA: "qa",
                SupervisorAction.SYNTHESIZE: "synthesis",
            }[decision.action]
            if role == "primary":
                existing = tuple(
                    child
                    for child in state.analysis_children
                    if child.profile == decision.assignee
                )
                if len(existing) > 1:
                    logger.error(
                        "primary-integrity root=%s profile=%s duplicate_count=%d create_suppressed=true",
                        state.parent_task_id,
                        decision.assignee,
                        len(existing),
                    )
                    return
                if existing:
                    # Replan must reuse the logical primary identity. The
                    # supported Hermes boundary exposes unblock as the
                    # bounded retry/reopen operation; never create a second
                    # canonical task for the same root/profile.
                    self.client.unblock_task(existing[0].task_id)
                    logger.info(
                        "retry-primary root=%s profile=%s task=%s attempt=%d",
                        state.parent_task_id,
                        decision.assignee,
                        existing[0].task_id,
                        state.replan_count + 1,
                    )
                    return
            idempotency_key = (
                primary_idempotency_key(state.parent_task_id, decision.assignee)
                if role == "primary"
                else f"{state.parent_task_id}:supervisor:{decision.action.value}:{decision.target_task_id or 'root'}"
                + (
                    f":replan-{state.replan_count}"
                    if decision.action == SupervisorAction.CREATE_TASK
                    else ""
                )
            )
            created = self.client.create_task(
                title=decision.title,
                body=build_scoped_task_body(
                    decision.body,
                    state.parent_task_id,
                    role=role,
                    workflow_mode=state.workflow_mode,
                    has_mandate=state.has_mandate,
                ),
                assignee=decision.assignee,
                parent_task_ids=decision.parent_task_ids,
                idempotency_key=idempotency_key,
            )
            if role == "primary":
                logger.info(
                    "primary-create root=%s assignee=%s producer=ceo-supervisor dedup_key=%s created=%s",
                    state.parent_task_id,
                    decision.assignee,
                    idempotency_key,
                    bool(created),
                )
            elif role == "synthesis":
                logger.info(
                    "synthesis-create root=%s parents=%d",
                    state.parent_task_id,
                    len(decision.parent_task_ids),
                )


__all__ = [
    "FAILURE_OUTCOMES",
    "PRIMARY_DEPARTMENTS",
    "CeoSupervisorService",
    "ChildTaskState",
    "HermesKanbanClient",
    "HermesKanbanCommandError",
    "SupervisorAction",
    "SupervisorDecision",
    "SupervisorState",
    "SupervisorValidationError",
    "SupervisorWorkflowError",
    "decide_supervisor",
    "parse_supervisor_output",
]
