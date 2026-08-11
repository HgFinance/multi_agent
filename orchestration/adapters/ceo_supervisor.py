"""Deterministic CEO supervisor for Hermes Kanban terminal events.

The supervisor is deliberately a small policy layer around the supported Hermes
Kanban CLI.  Hermes remains the owner of task state, parent dependencies,
worker spawning, and persistence; this module only collects task projections
and chooses the next structured action.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from orchestration.canonical_profiles import (
    CanonicalKanbanTaskRequest,
    CanonicalProfileError,
    canonical_profile_for_department,
    department_for_canonical_profile,
    validate_canonical_profile,
)
from orchestration.ceo_workflow_scope import (
    WorkflowScopeViolation,
    validate_workflow_scope,
)


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


@dataclass(frozen=True)
class ChildTaskState:
    """Relevant, read-only task projection used by the supervisor."""

    task_id: str
    profile: str
    status: str
    summary: str = ""
    error: str = ""
    block_reason: str = ""
    block_kind: str = ""
    outcome: str = ""
    retry_count: int = 0
    body: str = ""

    @classmethod
    def from_hermes(cls, payload: Mapping[str, Any]) -> "ChildTaskState":
        task_id = str(payload.get("id") or payload.get("task_id") or "")
        profile = validate_canonical_profile(str(payload.get("assignee") or ""))
        status = str(payload.get("status") or "unknown").casefold()
        latest = payload.get("latest_summary")
        summary = _text(payload.get("summary") or latest or payload.get("result"))
        error = _text(payload.get("error") or payload.get("last_error"))
        block_reason = _text(
            payload.get("block_reason")
            or payload.get("blocked_reason")
            or payload.get("reason")
        )
        block_kind = str(payload.get("block_kind") or payload.get("kind") or "").casefold()
        outcome = str(payload.get("outcome") or "").casefold()
        body = _text(payload.get("body"))
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
            error=error,
            block_reason=block_reason,
            block_kind=block_kind,
            outcome=outcome,
            retry_count=retry_count,
            body=body,
        )

    @property
    def department(self) -> str:
        return department_for_canonical_profile(self.profile)

    @property
    def is_supervisor(self) -> bool:
        # QA and replan tasks are supervisor-created work, but they remain
        # workflow children. Only CEO-assigned control tasks are excluded
        # from the analysis/QA dependency graph.
        return (
            self.profile == canonical_profile_for_department("ceo")
            and SUPERVISOR_MARKER in self.body
        )

    @property
    def is_qa(self) -> bool:
        return self.profile == canonical_profile_for_department("qa")

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

    @property
    def analysis_children(self) -> tuple[ChildTaskState, ...]:
        return tuple(
            child
            for child in self.children
            if not child.is_supervisor and not child.is_qa
        )

    @property
    def qa_children(self) -> tuple[ChildTaskState, ...]:
        return tuple(child for child in self.children if child.is_qa and not child.is_supervisor)

    @property
    def supervisor_children(self) -> tuple[ChildTaskState, ...]:
        return tuple(child for child in self.children if child.is_supervisor)

    def has_action(self, action: SupervisorAction) -> bool:
        return any(
            SUPERVISOR_MARKER in child.body and action.value in child.body
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
            parent_task_ids=(state.parent_task_id,),
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
            parent_task_ids=(state.parent_task_id,),
            reason="blocked_replan",
        )
    return SupervisorDecision(
        SupervisorAction.BLOCK_ABORT,
        state.parent_task_id,
        target_task_id=child.task_id,
        reason="blocked_replan_limit_reached",
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
        return SupervisorDecision(
            SupervisorAction.REQUEST_USER_INPUT,
            state.parent_task_id,
            assignee=canonical_profile_for_department("ceo"),
            title="CEO planner produced no executable child task",
            body=f"{SUPERVISOR_MARKER} action=REQUEST_USER_INPUT no_analysis_children",
            parent_task_ids=(state.parent_task_id,),
            reason="no_analysis_children",
        )

    for child in state.children:
        if child.is_supervisor or not child.terminal:
            continue
        if child.blocked:
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
                            {"task_id": child.task_id, "summary": child.summary}
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
    parent_ids = tuple(
        child.task_id for child in state.analysis_children + state.qa_children if child.done
    )
    return SupervisorDecision(
        SupervisorAction.SYNTHESIZE,
        state.parent_task_id,
        assignee=canonical_profile_for_department("ceo"),
        title="CEO final synthesis",
        body=(
            f"{SUPERVISOR_MARKER} action=SYNTHESIZE\n"
            "Synthesize the completed dynamic department work and QA result.\n"
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
        parent_task_ids=parent_ids,
        reason="qa_completed_final_synthesis",
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
    if action in {SupervisorAction.CREATE_TASK, SupervisorAction.RUN_QA, SupervisorAction.SYNTHESIZE}:
        if not assignee or not payload.get("title") or not payload.get("body"):
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
        request = CanonicalKanbanTaskRequest(assignee, title, body, idempotency_key)
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

    def workflow(self, task_id: str) -> tuple[str, tuple[dict[str, Any], ...]]:
        """Collect ancestors and descendants through Hermes ``show`` only."""

        cache: dict[str, dict[str, Any]] = {}

        def fetch(current_id: str) -> dict[str, Any]:
            if current_id not in cache:
                cache[current_id] = self.show(current_id)
            return cache[current_id]

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
    ) -> None:
        self.client = client
        self.max_retries = max_retries
        self.max_wakeups = max_wakeups
        self.qa_required = qa_required
        self.decider = decider
        self._seen_events: set[str] = set()
        self._wakeups: dict[str, int] = {}
        self._replans: dict[str, int] = {}
        self._executed_actions: set[str] = set()
        self._seen_events_lock = threading.Lock()
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

    def _record_wakeup(
        self,
        *,
        root_task_id: str,
        event_id: str,
        kind: str,
        action: str,
        existing: Mapping[str, str],
        state: str,
    ) -> None:
        comment_task = getattr(self.client, "comment_task", None)
        if not callable(comment_task):
            return
        comment_task(
            root_task_id,
            f"{SUPERVISOR_WAKE_MARKER} event={event_id} kind={kind} "
            f"state={state} action={action}",
        )

    def _safe_abort(self, task_id: str, reason: str) -> None:
        try:
            self.client.block_task(task_id, reason)
        except HermesKanbanCommandError as exc:
            raise SupervisorWorkflowError(
                f"workflow {task_id} canonical abort failed: {exc}"
            ) from exc

    def handle_terminal_event(self, event: Mapping[str, Any]) -> SupervisorDecision | None:
        task_id = str(event.get("task_id") or event.get("id") or "")
        kind = str(event.get("kind") or event.get("event_type") or event.get("status") or "").casefold()
        if not task_id or kind in NON_TERMINAL_EVENT_KINDS:
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
                try:
                    validate_workflow_scope(
                        root_task_id=root_id,
                        root_payload=root_payload,
                        descendants=payloads,
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
                children = tuple(
                    ChildTaskState.from_hermes(payload)
                    for payload in payloads
                    if payload.get("assignee") is not None
                )
                existing_wakeups = self._wakeup_comments(root_payload)
                if event_key in existing_wakeups and "state=done" in existing_wakeups[event_key]:
                    return None
                wakeups = len(existing_wakeups) + (0 if event_key in existing_wakeups else 1)
                durable_replans = sum(
                    1
                    for child in children
                    if SUPERVISOR_MARKER in child.body
                    and "action=CREATE_TASK" in child.body
                )
                state = SupervisorState(
                    parent_task_id=root_id,
                    children=children,
                    wakeups=wakeups,
                    replan_count=max(self._replans.get(root_id, 0), durable_replans),
                    max_retries=self.max_retries,
                    max_wakeups=self.max_wakeups,
                    qa_required=self._qa_required_from_event(event),
                )
                decision = self.decider(state)
                action = decision.action.value if decision is not None else "NONE"
                if decision is not None and decision.action in {
                    SupervisorAction.REQUEST_USER_INPUT,
                    SupervisorAction.SYNTHESIZE,
                } and state.has_action(decision.action):
                    # A durable supervisor child is the idempotency record for
                    # actions that create work.  This also covers a daemon
                    # restart after the Hermes CLI succeeded but before the
                    # watch loop acknowledged the event.
                    decision = None
                    action = "NONE"
                if event_key not in existing_wakeups:
                    self._record_wakeup(
                        root_task_id=root_id,
                        event_id=event_key,
                        kind=kind,
                        action=action,
                        existing=existing_wakeups,
                        state="started",
                )
                if decision is None:
                    self._record_wakeup(
                        root_task_id=root_id,
                        event_id=event_key,
                        kind=kind,
                        action=action,
                        existing=existing_wakeups,
                        state="done",
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
                if action_key in self._executed_actions:
                    return None
                self._executed_actions.add(action_key)
                self._execute(decision, state)
                if decision.action == SupervisorAction.CREATE_TASK:
                    self._replans[root_id] = self._replans.get(root_id, 0) + 1
                self._record_wakeup(
                    root_task_id=root_id,
                    event_id=event_key,
                    kind=kind,
                    action=action,
                    existing=existing_wakeups,
                    state="done",
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
        requested_parent_ids = set(decision.parent_task_ids) or {state.parent_task_id}
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
                body=decision.body or f"{SUPERVISOR_MARKER} action=REQUEST_USER_INPUT",
                assignee=decision.assignee or canonical_profile_for_department("ceo"),
                parent_task_ids=decision.parent_task_ids or (state.parent_task_id,),
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
                expected = {child.task_id for child in state.analysis_children}
                if requested_parent_ids != expected:
                    raise SupervisorValidationError(
                        "RUN_QA dependencies must be the current root's "
                        f"primary children: expected {sorted(expected)}, "
                        f"got {sorted(requested_parent_ids)}"
                    )
            elif decision.action == SupervisorAction.SYNTHESIZE:
                expected = {
                    child.task_id
                    for child in state.analysis_children + state.qa_children
                    if child.done
                }
                if requested_parent_ids != expected:
                    raise SupervisorValidationError(
                        "SYNTHESIZE dependencies must be current root terminal "
                        f"children: expected {sorted(expected)}, "
                        f"got {sorted(requested_parent_ids)}"
                    )
            elif state.parent_task_id not in requested_parent_ids:
                raise SupervisorValidationError(
                    f"{decision.action.value} must remain parented to current root"
                )
            self.client.create_task(
                title=decision.title,
                body=decision.body,
                assignee=decision.assignee,
                parent_task_ids=decision.parent_task_ids or (state.parent_task_id,),
                idempotency_key=(
                    f"{state.parent_task_id}:supervisor:{decision.action.value}:{decision.target_task_id or 'root'}"
                    + (
                        f":replan-{state.replan_count}"
                        if decision.action == SupervisorAction.CREATE_TASK
                        else ""
                    )
                ),
            )


__all__ = [
    "ChildTaskState",
    "CeoSupervisorService",
    "FAILURE_OUTCOMES",
    "HermesKanbanClient",
    "HermesKanbanCommandError",
    "PRIMARY_DEPARTMENTS",
    "SupervisorWorkflowError",
    "SupervisorAction",
    "SupervisorDecision",
    "SupervisorState",
    "SupervisorValidationError",
    "decide_supervisor",
    "parse_supervisor_output",
]
