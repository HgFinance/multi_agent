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
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

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
from orchestration.discord_delivery import DiscordFinalDelivery
from orchestration.discord_idempotency import DiscordIdempotencyStore

logger = logging.getLogger(__name__)


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
        "error": child.error,
        "block_reason": child.block_reason,
    }
    if child.terminal:
        # 답변 품질 등급을 함께 싣는다 - 차단이 아니라 신호다(answer_contract).
        # QA 는 "무엇을 의심해야 하는지" 를 알고 시작해야 검증이 성립한다.
        grade = grade_answer(child.result, summary=child.summary)
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
        run_metadata: Mapping[str, Any] = {}
        runs = payload.get("runs")
        if isinstance(runs, Sequence) and not isinstance(runs, (str, bytes)):
            for run in reversed(runs):
                if not isinstance(run, Mapping):
                    continue
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
        )
        block_reason = _text(
            payload.get("block_reason")
            or payload.get("blocked_reason")
            or payload.get("reason")
            or run_metadata.get("block_reason")
        )
        block_kind = str(payload.get("block_kind") or payload.get("kind") or "").casefold()
        outcome = str(payload.get("outcome") or "").casefold()
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
    return SupervisorDecision(
        SupervisorAction.SYNTHESIZE,
        state.parent_task_id,
        assignee=canonical_profile_for_department("ceo"),
        title="CEO final synthesis",
        body=(
            f"{SUPERVISOR_MARKER} action=SYNTHESIZE\n"
            "workflow_plane=response\n"
            "governance_plane=async_qa\n"
            "Synthesize available primary department work, including terminal "
            "blocked results. QA runs independently "
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
            "Synthesize only after existing QA/Risk/approval gate.\n"
            + json.dumps(
                [
                    {
                        "task_id": child.task_id,
                        "profile": child.profile,
                        "status": child.status,
                        "summary": child.summary,
                        "error": child.error,
                        "block_reason": child.block_reason,
                    }
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


class HermesKanbanClient:
    """Small CLI adapter; no direct shared Kanban DB access is allowed."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        environment: Mapping[str, str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        timeout: float | None = None,
    ) -> None:
        self.executable = executable or os.environ.get("HERMES_BIN", "hermes")
        self.environment = dict(environment or os.environ)
        self.runner = runner or subprocess.run
        self.timeout = timeout or float(os.environ.get("CEO_SUPERVISOR_CLI_TIMEOUT_SECONDS", "15"))

    def _run(self, args: Sequence[str]) -> str:
        try:
            process = self.runner(
                [self.executable, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env=self.environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HermesKanbanCommandError(f"hermes kanban command failed: {type(exc).__name__}") from exc
        if process.returncode != 0:
            raise HermesKanbanCommandError(f"hermes kanban command exited {process.returncode}")
        return process.stdout

    def show(self, task_id: str) -> dict[str, Any]:
        try:
            payload = json.loads(self._run(("kanban", "show", task_id, "--json")))
        except (json.JSONDecodeError, TypeError) as exc:
            raise HermesKanbanCommandError(
                "hermes kanban show returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise HermesKanbanCommandError("hermes kanban show returned a non-object")
        # Hermes exposes the task row under ``task`` and graph/run projections
        # beside it. Flatten that supported JSON shape for the policy layer.
        task = payload.get("task", payload)
        if not isinstance(task, dict):
            raise HermesKanbanCommandError("hermes kanban show returned no task object")
        normalized = dict(task)
        for key in ("latest_summary", "parents", "children", "comments", "events", "runs"):
            if key in payload:
                normalized[key] = payload[key]
        return normalized

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
        try:
            payload = json.loads(self._run(args))
        except (json.JSONDecodeError, TypeError) as exc:
            raise HermesKanbanCommandError(
                "hermes kanban create returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise HermesKanbanCommandError("hermes kanban create returned a non-object")
        return payload

    def unblock_task(self, task_id: str) -> None:
        self._run(("kanban", "unblock", task_id))

    def comment_task(self, task_id: str, text: str) -> None:
        self._run(("kanban", "comment", task_id, text, "--author", "ceo-supervisor"))

    def block_task(self, task_id: str, reason: str) -> None:
        self._run(("kanban", "block", task_id, reason, "--kind", "needs_input"))

    def list_tasks(self) -> tuple[dict[str, Any], ...]:
        """List current-board tasks through the supported Hermes JSON API."""

        try:
            payload = json.loads(self._run(("kanban", "list", "--json")))
        except (json.JSONDecodeError, TypeError) as exc:
            raise HermesKanbanCommandError(
                "hermes kanban list returned invalid JSON"
            ) from exc
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise HermesKanbanCommandError(
                "hermes kanban list returned a non-task array"
            )
        return tuple(dict(item) for item in payload)

    def workflow(self, task_id: str) -> tuple[str, tuple[dict[str, Any], ...]]:
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
        starting_body = str(starting_payload.get("body") or "")
        starting_role = workflow_role_from_body(starting_body)
        # hgfinance-canonical-root-scope-v1
        #
        # Current direct CEO ingress roots are canonical user-query roots but
        # may not carry the legacy hgfinance.ceo-workflow-scope.v1 marker.
        # Parentless primaries still declare workflow_root_task_id, so these
        # roots must enter marker-based scope discovery rather than ancestry
        # fallback.
        canonical_user_root = (
            starting_role == "root"
            and is_user_query_body(starting_body)
            and workflow_mode_from_body(starting_body)
            in {"analysis", "binding"}
        )
        legacy_scoped_root = (
            starting_role == "root"
            and CEO_WORKFLOW_SCOPE_MARKER in starting_body
        )
        is_scoped_root = canonical_user_root or legacy_scoped_root

        if scoped_root_ids or is_scoped_root:
            root_id = scoped_root_ids[0] if scoped_root_ids else task_id
            fetch(root_id)

            scoped_ids = {root_id}
            for row in self.list_tasks():
                row_id = str(row.get("id") or row.get("task_id") or "")
                if not row_id:
                    continue
                if root_id in extract_scope_references(row).root_ids:
                    scoped_ids.add(row_id)

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


def _department_progress_text(
    profile: str,
    kind: str,
    *,
    summary: str = "",
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

    if normalized == "blocked":
        return f"{icon} **{label}**\n현재 필요한 입력 또는 의존성이 부족해 작업이 지연되고 있습니다."

    if normalized in {"failed", "error"}:
        return f"{icon} **{label}**\n작업 중 오류가 발생했습니다. CEO가 가능한 결과와 누락 범위를 확인합니다."

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
        self._seen_events: set[str] = set()
        self._wakeups: dict[str, int] = {}
        self._replans: dict[str, int] = {}
        self._executed_actions: set[str] = set()
        self._seen_events_lock = threading.Lock()

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
    ) -> None:
        """Run a terminal observer without changing the supervisor decision."""

        task = next(
            (
                payload
                for payload in task_payloads
                if str(payload.get("id") or payload.get("task_id") or "") == task_id
            ),
            None,
        )
        if task is None:
            return
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
                    extra={"root_task_id": root_task_id, "task_id": task_id, "error": str(exc)},
        )
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

                # hgfinance-synthesis-parent-delivery-v1
                #
                # Multi-primary synthesis has two projections:
                #   1. the canonical CEO final answer in the parent channel
                #   2. the complete synthesis in the existing request thread
                #
                # The thread is supplementary detail. It must never replace the
                # user-facing final CEO response in the channel where the request
                # originated.
                parent_status = self.discord_delivery.deliver(
                    root_task_id=root_task_id,
                    synthesis_task=delivery_task,
                    content=content,
                    store=delivery_store,
                    profile=ceo_profile,
                )

                logger.info(
                    "synthesis-discord-parent root=%s task=%s status=%s",
                    root_task_id,
                    task_id,
                    parent_status,
                )

                # Also preserve the complete CEO synthesis in the SAME
                # request thread as the department detailed outputs.
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

                logger.info(
                    "synthesis-discord-thread root=%s task=%s status=%s",
                    root_task_id,
                    task_id,
                    thread_status,
                )

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

        for row in list_tasks():
            task_id = str(row.get("id") or row.get("task_id") or "")
            body = str(row.get("body") or "")
            status = str(row.get("status") or "").casefold()
            created_at = int(row.get("created_at") or 0)

            if (
                not task_id
                or status not in {"ready", "running", "done"}
                or (
                    status == "done"
                    and (
                        created_at <= 0
                        or now - created_at > done_recovery_window_seconds
                    )
                )
                or workflow_role_from_body(body) != "root"
                or not is_user_query_body(body)
                or workflow_mode_from_body(body) != "analysis"
                or "selected_primary_profiles=" not in body
                or "delegation_instruction." not in body
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
                    workflow_role_from_body(root_body) != "root"
                    or not is_user_query_body(root_body)
                    or workflow_mode_from_body(root_body) != "analysis"
                ):
                    continue

                selected_profiles = (
                    selected_primary_profiles_from_task(root_payload)
                )

                if not selected_profiles:
                    continue

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
                    root_body,
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
                            pool.submit(self._execute, decision, state)
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

    def handle_terminal_event(self, event: Mapping[str, Any]) -> SupervisorDecision | None:
        task_id = str(event.get("task_id") or event.get("id") or "")
        kind = str(event.get("kind") or event.get("event_type") or event.get("status") or "").casefold()
        if not task_id:
            return None

        if kind in {"claimed", "spawned", "started", "running"}:
            # Fast rejection before workflow()/show().  A successful initial
            # Discord "started" projection is enough for every equivalent
            # active lifecycle event for this task.
            with self._department_started_progress_lock:
                if task_id in self._department_started_progress:
                    return None

            try:
                root_id, payloads = self.client.workflow(task_id)
                show = getattr(self.client, "show", None)

                if callable(show):
                    root_payload = show(root_id)

                    task_payload = show(task_id)
                    self._deliver_department_progress(
                        root_task_id=root_id,
                        root_payload=root_payload,
                        task_payload=task_payload,
                        event=event,
                    )

                    # Recover sibling starts that the CLI watch may have
                    # coalesced or missed in the same polling interval.
                    self._reconcile_department_start_progress(
                        root_task_id=root_id,
                        root_payload=root_payload,
                        task_payloads=payloads,
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
        with self._seen_events_lock:
            if event_key in self._seen_events:
                return None
            self._seen_events.add(event_key)
        try:
            root_id, _ = self.client.workflow(task_id)
            with self._parent_lock(root_id):
                # Re-read after acquiring the workflow lock. A sibling event may
                # have completed while this event was waiting for the lock.
                root_id, payloads = self.client.workflow(task_id)
                # The production Hermes client exposes ``show`` for durable
                # wakeup comments. Keep the policy service compatible with small
                # workflow-only fakes and adapters used by the supervisor tests.
                show = getattr(self.client, "show", None)
                root_payload = show(root_id) if callable(show) else {}
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
                        # Preserve compatibility for legacy planning roots whose
                        # execution-parent linkage may not yet be durable when
                        # the root completion event arrives.
                        logger.info(
                            "root-planning-complete-ignored root=%s event=%s",
                            root_id,
                            event_key,
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
                self._project_terminal_task(
                    root_task_id=root_id,
                    task_id=task_id,
                    task_payloads=(root_payload, *payloads),
                    event=event,
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
                if terminal_task_payload is not None:
                    try:
                        show_task = getattr(self.client, "show", None)
                        if callable(show_task):
                            terminal_task_payload = show_task(task_id)

                        self._deliver_department_progress(
                            root_task_id=root_id,
                            root_payload=root_payload,
                            task_payload=terminal_task_payload,
                            event=event,
                        )
                    except Exception as exc:
                        logger.warning(
                            "department-discord-progress-failed "
                            "task=%s kind=%s error=%s",
                            task_id,
                            kind,
                            type(exc).__name__,
                        )

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
                # (especially final_answer). workflow() may carry only a shallow
                # task projection, so hydrate exactly one selected primary with
                # show() before building SupervisorState.
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
                        self._execute(initial_primary_decision, state)

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

                    delivery_status = self.discord_delivery.deliver(
                        root_task_id=root_id,
                        synthesis_task=delivery_task,
                        content=passthrough.final_answer,
                        store=DiscordIdempotencyStore(delivery_home),
                        profile=canonical_profile_for_department("ceo"),
                    )
                    logger.info(
                        "single-primary-passthrough root=%s task=%s "
                        "profile=%s status=%s",
                        root_id,
                        passthrough.task_id,
                        passthrough.profile,
                        delivery_status,
                    )

                decision = self.decider(state)
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
                    if action_key in self._executed_actions:
                        return None
                    self._executed_actions.add(action_key)
                self._execute(decision, state)
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
                    # markers. Small test/fake clients without list_tasks retain
                    # the previous full-workflow fallback.
                    synthesis_exists = False
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
                            self._execute(synthesis, state)
                            action = f"{action},SYNTHESIZE"
                if decision.action == SupervisorAction.CREATE_TASK:
                    self._replans[root_id] = self._replans.get(root_id, 0) + 1
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
