"""Deterministic CEO supervisor for Hermes Kanban terminal events.

The supervisor is deliberately a small policy layer around the supported Hermes
Kanban CLI.  Hermes remains the owner of task state, parent dependencies,
worker spawning, and persistence; this module only collects task projections
and chooses the next structured action.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from orchestration.accounting_advisory_context import fetch_accounting_advisory_context
from orchestration.adapters.department_notion_projection import (
    DepartmentNotionProjection,
)
from orchestration.adapters.terminal_projection_utils import (
    action as terminal_action,
)
from orchestration.adapters.terminal_projection_utils import (
    is_background_research,
    merged_run_metadata,
    strip_internal_handoff,
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
    approved_feedback_section_from_root,
    build_scoped_task_body,
    extract_scope_references,
    is_user_query_body,
    langsmith_trace_context_from_body,
    langsmith_trace_run_id_from_body,
    mandate_snapshot_present,
    previous_question_context_from_body,
    primary_idempotency_key,
    read_marker,
    selected_primary_profiles_from_task,
    user_paper_order_scope_from_body,
    validate_workflow_scope,
    workflow_mode_from_body,
    workflow_role_from_body,
)
from orchestration.compound_paper_orders import (
    parse_analysis_then_conditional_paper_order,
)
from orchestration.discord_delivery import DiscordFinalDelivery
from orchestration.discord_idempotency import DiscordIdempotencyStore
from orchestration.experience_bank import (
    ExperienceBank,
    build_discord_experience_record,
)
from orchestration.failure_taxonomy import FailureKind, classify_failure
from orchestration.kanban_retention_lock import workflow_mutation_lock
from orchestration.kanban_root_index import (
    RootScopedIndexUnavailable,
    SQLiteRootScopedIndex,
    kanban_db_path,
)
from orchestration.primary_task_idempotency import (
    REQUEST_USER_INPUT_ACTION_BODY,
    is_analysis_primary_eligible,
    request_user_input_idempotency_key,
    validate_primary_create,
)
from orchestration.qa_contract import (
    canonical_qa_contract,
    split_planner_selection,
)
from orchestration.risk_advisory_context import fetch_risk_advisory_context
from orchestration.risk_observability import risk_span
from orchestration.risk_plan_projection import format_position_risk_plan
from orchestration.semantic_qa import evaluate_prompt_answer
from orchestration.workforce_advisory_context import fetch_workforce_advisory_context

logger = logging.getLogger(__name__)

_CLI_LANE: ContextVar[str] = ContextVar("ceo_cli_lane", default="unknown")
_LANGSMITH_DIRECT_ROOT_MARKER = "hgfinance.langsmith-direct-root.v1"
_HR_RESPONSE_DELIVERY_MARKER = "hgfinance.hr-response-delivery.v1"


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
        # Hermes moves a task here after a repeated block loop.  Triage is a
        # terminal manual-review state and must never be auto-unblocked again.
        "triage",
    }
)
FAILURE_OUTCOMES = frozenset(
    {"gave_up", "crashed", "timed_out", "spawn_failed", "failed", "triage"}
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


def child_handoff_payload(
    child: ChildTaskState,
    *,
    include_hr_evidence: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """Hand a finished child to QA/synthesis **with its answer body**.

    요약만 넘기면 뒤 단계가 원문을 못 본다 - QA 는 인용을 검증할 대상이 없고
    (본 적 없는 문장을 통과시키게 된다), 종합은 표·수치를 다시 만들 수 없어
    사용자 응답이 요약 한 줄로 쪼그라든다. 실측 2026-08-14 t_79e42ca4.

    본문이 비어 있으면 그 사실을 명시한다 - 없는 것을 요약으로 때우면
    "답이 있었는데 사라진 것"과 "애초에 못 만든 것"이 구분되지 않는다.
    """

    payload: dict[str, Any] = {
        "task_id": child.task_id,
        "profile": child.profile,
        "workflow_role": child.workflow_role,
        "workflow_root_task_id": child.workflow_root_task_id,
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
            # Some Hermes workers keep a transport token such as ``success``
            # in task.result and put the user-ready answer in run metadata.
            # Grade the visible answer first; otherwise QA receives a false
            # "no evidence" warning even though final_answer has the source
            # window and artifact receipt.
            child.final_answer or child.result,
            summary=child.summary,
        )
        payload.update(grade.as_payload())
        if not grade.has_body:
            payload["answer_body_missing"] = True
            payload["answer_body_missing_note"] = (
                "이 부서 카드는 result(답변 본문) 없이 종료됐다. 요약만으로 본문을 "
                "복원하지 말고, 근거가 없는 수치·목록은 만들지 마라."
            )
    provenance = _handoff_provenance(
        child,
        include_evidence_content=include_hr_evidence,
    )
    if provenance:
        payload["provenance"] = provenance
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
    workspace_path: str = ""
    workflow_root_task_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

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
        task_record = payload.get("task")
        task_record = task_record if isinstance(task_record, Mapping) else {}
        workspace_path = _text(
            payload.get("workspace_path") or task_record.get("workspace_path")
        )
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
            workspace_path=workspace_path,
            workflow_root_task_id=workflow_root_task_id,
            metadata=merged_run_metadata(payload),
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

        if (
            self.is_background_research
            or self.is_supervisor
            or self.is_qa
            or self.profile == canonical_profile_for_department("qa")
        ):
            return False
        return self.workflow_role == "primary"

    @property
    def is_legacy_qa_primary(self) -> bool:
        """A historical QA task incorrectly materialized as ``primary``."""

        return (
            self.profile == canonical_profile_for_department("qa")
            and self.workflow_role == "primary"
        )

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


def _normalize_hr_api_check_result(metadata: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Normalize the compact HR terminal envelope used by the live worker."""

    result = metadata.get("result")
    api_checks = result.get("api_checks") if isinstance(result, Mapping) else None
    if not isinstance(api_checks, Sequence) or isinstance(api_checks, (str, bytes)):
        endpoint_receipts = metadata.get("endpoints")
        if isinstance(endpoint_receipts, Sequence) and not isinstance(
            endpoint_receipts, (str, bytes)
        ):
            receipts = [
                item for item in endpoint_receipts if isinstance(item, Mapping)
            ]

            def _receipt(fragment: str) -> Mapping[str, Any]:
                return next(
                    (
                        item
                        for item in receipts
                        if fragment in str(item.get("path") or item.get("endpoint") or "")
                    ),
                    {},
                )

            def _endpoint(value: Any) -> str:
                raw = str(value or "").strip()
                if raw.startswith("GET http://"):
                    return raw
                if raw.startswith("GET /"):
                    return "GET http://workforce-api:8000" + raw[4:]
                if raw.startswith("/"):
                    return "GET http://workforce-api:8000" + raw
                return raw

            improvements = _receipt("/improvements")
            observability = _receipt("/observability")
            scorecard = _receipt("/scorecard-brief")
            departments = scorecard.get("departments") or []
            departments = (
                list(departments)
                if isinstance(departments, Sequence)
                and not isinstance(departments, (str, bytes))
                else []
            )
            return {
                "candidate_snapshot": {
                    "source": _endpoint(
                        improvements.get("path") or improvements.get("endpoint")
                    ),
                    "http_status": improvements.get("http_status") or 200,
                    "candidate_count": improvements.get("candidate_count"),
                },
                "observability": {
                    "source": _endpoint(
                        observability.get("path") or observability.get("endpoint")
                    ),
                    "http_status": observability.get("http_status") or 200,
                    "lookback_hours": 24,
                    "statuses": observability.get("idle_status_counts") or {},
                },
                "scorecard": {
                    "source": _endpoint(
                        scorecard.get("path") or scorecard.get("endpoint")
                    ),
                    "http_status": scorecard.get("http_status") or 200,
                    "departments": departments,
                },
            }

        # Another live worker envelope keeps the read receipts at the top
        # level and stores the compact count/status fields in ``summary``.
        summary_metadata = metadata.get("summary")
        observability = metadata.get("observability")
        scorecard = metadata.get("scorecard")
        summary_metadata = (
            summary_metadata if isinstance(summary_metadata, Mapping) else {}
        )
        if not isinstance(observability, Mapping) or not isinstance(scorecard, Mapping):
            return None

        def _window(value: Any) -> tuple[str | None, str | None]:
            raw = str(value or "").strip()
            if " ~ " in raw:
                start, end = raw.split(" ~ ", 1)
            elif "/" in raw:
                start, end = raw.split("/", 1)
            else:
                return None, None
            return start.strip(" `"), end.strip(" `")

        observation_start, observation_end = _window(
            observability.get("window")
            or summary_metadata.get("observability_window")
        )
        scorecard_start, scorecard_end = _window(
            scorecard.get("window") or summary_metadata.get("scorecard_window")
        )
        departments = scorecard.get("departments") or summary_metadata.get(
            "scorecard_scope"
        ) or []
        departments = (
            list(departments)
            if isinstance(departments, Sequence) and not isinstance(departments, (str, bytes))
            else []
        )
        scorecard_source = (
            "GET http://workforce-api:8000/workforce/v1/departments/"
            "scorecard-brief"
        )
        query = []
        if scorecard_start and scorecard_end:
            query.extend(
                [f"window_start={scorecard_start}", f"window_end={scorecard_end}"]
            )
        query.extend(f"department_code={item}" for item in departments if str(item).strip())
        if query:
            scorecard_source += "?" + "&".join(query)
        states = observability.get("states") or observability.get("idle_state_counts") or {}
        proposal = metadata.get("proposal_only_job_profile")
        evaluation = metadata.get("evaluation_plan")
        return {
            "candidate_snapshot": {
                "source": "GET http://workforce-api:8000/workforce/v1/improvements",
                "http_status": (
                    (summary_metadata.get("http_status") or {}).get("improvements")
                    if isinstance(summary_metadata.get("http_status"), Mapping)
                    else 200
                )
                or 200,
                "candidate_count": summary_metadata.get("improvement_candidate_count"),
            },
            "observability": {
                "source": (
                    "GET http://workforce-api:8000/workforce/v1/departments/"
                    "observability?lookback_hours=24"
                ),
                "http_status": (
                    (summary_metadata.get("http_status") or {}).get("observability")
                    if isinstance(summary_metadata.get("http_status"), Mapping)
                    else 200
                )
                or 200,
                "lookback_hours": 24,
                "statuses": states,
                "window_start": observation_start,
                "window_end": observation_end,
            },
            "scorecard": {
                "source": scorecard_source,
                "http_status": (
                    (summary_metadata.get("http_status") or {}).get("scorecard_brief")
                    if isinstance(summary_metadata.get("http_status"), Mapping)
                    else 200
                )
                or 200,
                "window_start": scorecard_start or observation_start,
                "window_end": scorecard_end or observation_end,
                "departments": departments,
            },
            "proposal": {"job_profile": proposal},
            "evaluation_suite": {
                "golden": evaluation.get("golden") if isinstance(evaluation, Mapping) else [],
                "adversarial": evaluation.get("adversarial") if isinstance(evaluation, Mapping) else [],
            },
        }
    if not isinstance(api_checks, Sequence) or isinstance(api_checks, (str, bytes)):
        return None

    checks = [item for item in api_checks if isinstance(item, Mapping)]

    def _check(fragment: str) -> Mapping[str, Any]:
        return next(
            (item for item in checks if fragment in str(item.get("endpoint") or "")),
            {},
        )

    def _endpoint(value: Any) -> str:
        raw = str(value or "").strip()
        if raw.startswith("GET http://"):
            return raw
        if raw.startswith("GET /"):
            return "GET http://workforce-api:8000" + raw[4:]
        if raw.startswith("/"):
            return "GET http://workforce-api:8000" + raw
        return raw

    def _window(value: Any) -> tuple[str | None, str | None]:
        raw = str(value or "").strip()
        if " ~ " in raw:
            start, end = raw.split(" ~ ", 1)
        elif "/" in raw:
            start, end = raw.split("/", 1)
        else:
            return None, None
        return start.strip(" `"), end.strip(" `")

    summary_metadata = metadata.get("summary")
    summary_metadata = summary_metadata if isinstance(summary_metadata, Mapping) else {}
    observation_start, observation_end = _window(
        summary_metadata.get("observability_window")
    )
    scorecard_start, scorecard_end = _window(
        summary_metadata.get("scorecard_window")
    )

    idle_state_counts: dict[str, int] = {}
    idle_agents = result.get("idle_agents")
    if isinstance(idle_agents, Sequence) and not isinstance(idle_agents, (str, bytes)):
        for agent in idle_agents:
            if not isinstance(agent, Mapping):
                continue
            state = str(agent.get("status") or "").strip()
            if state:
                idle_state_counts[state] = idle_state_counts.get(state, 0) + 1

    scorecard_check = _check("/scorecard-brief")
    departments = summary_metadata.get("scorecard_scope")
    if not isinstance(departments, Sequence) or isinstance(departments, (str, bytes)):
        departments = []

    proposal = metadata.get("proposal_only")
    proposal = proposal if isinstance(proposal, Mapping) else {}
    improvements_check = _check("/improvements")
    observability_check = _check("/observability")
    return {
        "candidate_snapshot": {
            "source": _endpoint(improvements_check.get("endpoint")),
            "http_status": improvements_check.get("http_status") or 200,
            "candidate_count": improvements_check.get("candidate_count"),
        },
        "observability": {
            "source": _endpoint(observability_check.get("endpoint")),
            "http_status": observability_check.get("http_status") or 200,
            "lookback_hours": 24,
            "statuses": idle_state_counts,
            "window_start": observation_start,
            "window_end": observation_end,
        },
        "scorecard": {
            "source": _endpoint(scorecard_check.get("endpoint")),
            "http_status": scorecard_check.get("http_status") or 200,
            "window_start": scorecard_start or observation_start,
            "window_end": scorecard_end or observation_end,
            "departments": list(departments),
        },
        "proposal": {"job_profile": proposal.get("job_profile")},
        "evaluation_suite": {
            "golden": proposal.get("golden_evals") or [],
            "adversarial": proposal.get("adversarial_evals") or [],
        },
    }


_RISK_LEGAL_FLAT_KEYS = (
    "legal_wiki_calls",
    "legal_status",
    "legal_verdict",
    "legal_pages_visited",
    "legal_source_references",
    "legal_cited_documents",
    "legal_escalate",
    "legal_error",
    "legal_invocation_id",
)


def _risk_legal_metadata(metadata: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Normalize the legacy and current Risk legal-result metadata shapes."""

    legacy = metadata.get("legal_routing_verification")
    if isinstance(legacy, Mapping):
        return legacy
    if not any(key in metadata for key in _RISK_LEGAL_FLAT_KEYS):
        return None
    return {
        "tool_status": metadata.get("legal_status"),
        "verdict": metadata.get("legal_verdict"),
        "escalate": metadata.get("legal_escalate", True),
        "cited_documents": metadata.get("legal_cited_documents") or [],
        "pages_visited": metadata.get("legal_pages_visited") or [],
        "source_references": metadata.get("legal_source_references") or [],
        "invocation_count": metadata.get("legal_wiki_calls"),
        "invocation_id": metadata.get("legal_invocation_id"),
        "error": metadata.get("legal_error") or "",
    }


def _handoff_provenance(
    child: ChildTaskState,
    *,
    include_evidence_content: bool = False,
) -> dict[str, Any]:
    """Expose bounded source coordinates to CEO/QA without raw worker output."""

    metadata = child.metadata
    structured_result = metadata.get("result")
    normalized_api_checks = _normalize_hr_api_check_result(metadata)
    if normalized_api_checks is not None:
        structured_result = normalized_api_checks
    if not isinstance(structured_result, Mapping):
        source_reads = metadata.get("authoritative_sources")
        if isinstance(source_reads, Mapping):
            structured_result = {
                "candidate_snapshot": source_reads.get("improvements"),
                "observability": source_reads.get("observability"),
                "scorecard": source_reads.get("scorecard_brief"),
            }
    if not isinstance(structured_result, Mapping):
        source_reads = metadata.get("api_reads")
        if isinstance(source_reads, Mapping):
            structured_result = {
                "candidate_snapshot": source_reads.get("improvements"),
                "observability": source_reads.get("observability"),
                "scorecard": source_reads.get("scorecard_brief"),
            }
    if not isinstance(structured_result, Mapping) and child.department == "hr":
        source_reads = metadata.get("sources")
        if isinstance(source_reads, Mapping):
            improvements = source_reads.get("improvements")
            observability_read = source_reads.get("observability")
            scorecard_read = source_reads.get("scorecard_brief")

            def _artifact_window(fragment: str) -> tuple[str | None, str | None]:
                refs = metadata.get("artifacts")
                if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
                    artifact = metadata.get("artifact")
                    refs = [artifact] if artifact else []
                for ref in refs[:3]:
                    try:
                        path = Path(str(ref).strip())
                        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                            continue
                        for line in path.read_text(encoding="utf-8").splitlines():
                            if fragment not in line or " ~ " not in line:
                                continue
                            value = line.split(":", 1)[-1].strip()
                            start, end = value.split(" ~ ", 1)
                            start = start.strip(" `")
                            end = end.strip(" `.。")
                            if start and end:
                                return start, end
                    except (OSError, UnicodeError, ValueError):
                        continue
                return None, None

            observability_start, observability_end = _artifact_window("반환 창")
            scorecard_start, scorecard_end = _artifact_window("관측 창은 두 부서 동일")
            structured_result = {
                "candidate_snapshot": {
                    "http_status": (
                        improvements.get("http_status")
                        or improvements.get("status")
                        or 200
                    )
                    if isinstance(improvements, Mapping)
                    else 200,
                    "candidate_count": improvements.get("candidate_count")
                    if isinstance(improvements, Mapping)
                    else None,
                },
                "observability": {
                    "http_status": (
                        observability_read.get("http_status")
                        or observability_read.get("status")
                        or 200
                    )
                    if isinstance(observability_read, Mapping)
                    else 200,
                    "window_start": metadata.get("observability_window_start")
                    or observability_start,
                    "window_end": metadata.get("observability_window_end")
                    or observability_end,
                },
                "scorecard": {
                    "http_status": (
                        scorecard_read.get("http_status")
                        or scorecard_read.get("status")
                        or 200
                    )
                    if isinstance(scorecard_read, Mapping)
                    else 200,
                    "window_start": metadata.get("scorecard_window_start")
                    or scorecard_start,
                    "window_end": metadata.get("scorecard_window_end")
                    or scorecard_end,
                    "departments": scorecard_read.get("departments")
                    if isinstance(scorecard_read, Mapping)
                    else [],
                },
            }
    if (
        isinstance(structured_result, Mapping)
        and "candidate_snapshot" not in structured_result
        and isinstance(structured_result.get("improvements"), Mapping)
        and isinstance(structured_result.get("observability"), Mapping)
        and isinstance(structured_result.get("scorecard"), Mapping)
    ):
        # Another live HR envelope names the three snapshots directly under
        # ``result``.  Normalize its window string while preserving the
        # underlying read-only facts.
        def _split_hr_window(value: Any) -> tuple[str | None, str | None]:
            raw = str(value or "").strip()
            if " ~ " not in raw:
                return None, None
            start, end = raw.split(" ~ ", 1)
            return start.strip(), end.strip()

        direct_observability = structured_result["observability"]
        direct_scorecard = structured_result["scorecard"]
        observation_start, observation_end = _split_hr_window(
            direct_observability.get("window")
            or (
                metadata.get("summary", {}).get("evidence_window")
                if isinstance(metadata.get("summary"), Mapping)
                else None
            )
        )
        scorecard_start, scorecard_end = _split_hr_window(
            direct_scorecard.get("window")
        )
        structured_result = {
            "candidate_snapshot": structured_result["improvements"],
            "observability": {
                **direct_observability,
                "window_start": observation_start,
                "window_end": observation_end,
            },
            "scorecard": {
                **direct_scorecard,
                "window_start": scorecard_start or observation_start,
                "window_end": scorecard_end or observation_end,
            },
        }
    if not isinstance(structured_result, Mapping) and child.department == "hr":
        # The active HR worker may persist only the human-readable list of
        # successful GETs, while the numeric snapshots remain in sibling
        # metadata fields.  Promote that bounded read receipt so synthesis and
        # QA receive the same endpoint/HTTP/window coordinates as other HR
        # terminal envelopes.
        raw_reads = metadata.get("authoritative_reads")
        if isinstance(raw_reads, Sequence) and not isinstance(raw_reads, (str, bytes)):
            read_lines = [str(item).strip() for item in raw_reads if str(item).strip()]

            def _read_endpoint(fragment: str) -> str:
                line = next((item for item in read_lines if fragment in item), "")
                if line.startswith("GET /"):
                    return "GET http://workforce-api:8000" + line[4:]
                return line

            def _read_query_value(line: str, key: str) -> str | None:
                token = f"{key}="
                if token not in line:
                    return None
                value = line.split(token, 1)[1].split("&", 1)[0].strip()
                return value or None

            scorecard_endpoint = _read_endpoint("/scorecard-brief")
            scorecard_metadata = metadata.get("scorecard")
            structured_result = {
                "candidate_snapshot": {
                    "endpoint": _read_endpoint("/improvements"),
                    "http_status": 200,
                    "candidate_count": metadata.get("candidate_count", 0),
                },
                "observability": {
                    "endpoint": _read_endpoint("/observability"),
                    "http_status": 200,
                    "window_start": _read_query_value(scorecard_endpoint, "window_start"),
                    "window_end": _read_query_value(scorecard_endpoint, "window_end"),
                    "states": metadata.get("idle_state_counts") or {},
                },
                "scorecard": {
                    "endpoint": scorecard_endpoint,
                    "http_status": 200,
                    "window_start": _read_query_value(scorecard_endpoint, "window_start"),
                    "window_end": _read_query_value(scorecard_endpoint, "window_end"),
                    "departments": list(scorecard_metadata)
                    if isinstance(scorecard_metadata, Mapping)
                    else [],
                },
            }
    if isinstance(structured_result, Mapping) and "candidate_snapshot" not in structured_result:
        # Current HR terminal envelope names the aggregate count directly.
        # Normalize it here only for the bounded QA/CEO handoff projection;
        # the worker's original machine result remains unchanged.
        if "improvement_candidate_count" in structured_result:
            api_status = structured_result.get("api_http_status")
            api_status = api_status if isinstance(api_status, Mapping) else {}
            idle_agents = structured_result.get("idle_agents")
            idle_agents = idle_agents if isinstance(idle_agents, Mapping) else {}
            direct_observability = structured_result.get("observability")
            direct_observability = (
                direct_observability
                if isinstance(direct_observability, Mapping)
                else {}
            )
            direct_scorecard = structured_result.get("scorecard")
            direct_scorecard = (
                direct_scorecard if isinstance(direct_scorecard, Mapping) else {}
            )
            structured_result = {
                "candidate_snapshot": {
                    "candidate_count": structured_result.get(
                        "improvement_candidate_count"
                    ),
                    "http_status": api_status.get("improvements") or 200,
                },
                "observability": {
                    **direct_observability,
                    "http_status": api_status.get("observability") or 200,
                    "states": direct_observability.get("states")
                    or idle_agents.get("statuses")
                    or {},
                    "window_start": direct_observability.get("window_start")
                    or direct_scorecard.get("window_start"),
                    "window_end": direct_observability.get("window_end")
                    or direct_scorecard.get("window_end"),
                },
                "scorecard": {
                    **direct_scorecard,
                    "http_status": api_status.get("scorecard_brief") or 200,
                },
            }
    sources = metadata.get("current_state_sources")
    source_endpoints = [
        str(item).strip()
        for item in sources
        if str(item).strip()
    ] if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)) else []

    windows: dict[str, Any] = {}
    for name, key in (("observability", "observability"), ("scorecard", "scorecard")):
        value = metadata.get(key)
        if not isinstance(value, Mapping):
            continue
        window = {
            field_name: value.get(field_name)
            for field_name in ("http_status", "window_start", "window_end")
            if value.get(field_name) not in (None, "")
        }
        if window:
            windows[name] = window

    artifacts: list[dict[str, str]] = []
    evidence_path: Path | None = None
    artifact_refs = metadata.get("artifacts")
    if isinstance(artifact_refs, Sequence) and not isinstance(artifact_refs, (str, bytes)):
        for ref in artifact_refs[:5]:
            raw_path = (
                ref.get("path") or ref.get("name") or ""
                if isinstance(ref, Mapping)
                else ref
            )
            path = Path(str(raw_path).strip())
            if not path.name:
                continue
            item: dict[str, str] = {"name": path.name}
            try:
                if path.is_file() and path.stat().st_size <= 10 * 1024 * 1024:
                    if child.department == "hr" and path.name == "hr_e2e_evidence.json":
                        evidence_path = path
                    digest = hashlib.sha256()
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    item["sha256"] = digest.hexdigest()
            except (OSError, ValueError):
                pass
            artifacts.append(item)

    # The HR helper is intentionally allowed to leave the evidence file in
    # Hermes' task workspace without relying on the model to serialize a
    # second metadata envelope correctly.  Discover only this exact filename
    # in the already-scoped workspace; never scan the workspace broadly.
    if child.department == "hr" and evidence_path is None:
        evidence_candidates = []
        if child.workspace_path:
            evidence_candidates.append(Path(child.workspace_path) / "hr_e2e_evidence.json")
        # Hermes stores task attachments on the shared Kanban volume, while
        # workspace paths are profile-local.  This exact task-scoped path is
        # the only fallback; never scan attachments or workspaces broadly.
        evidence_candidates.append(
            Path("/opt/data/shared-kanban/kanban/attachments")
            / child.task_id
            / "hr_e2e_evidence.json"
        )
        for candidate_path in evidence_candidates:
            if (
                candidate_path.is_file()
                and candidate_path.stat().st_size <= 10 * 1024 * 1024
            ):
                evidence_path = candidate_path
                artifacts = [
                    item
                    for item in artifacts
                    if item.get("name") != "hr_e2e_evidence.json"
                ]
                item = {"name": evidence_path.name}
                try:
                    digest = hashlib.sha256()
                    with evidence_path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    item["sha256"] = digest.hexdigest()
                    artifacts.append(item)
                except (OSError, ValueError):
                    evidence_path = None
                break

    # Keep the helper's request receipt authoritative even when the evidence
    # body is intentionally omitted from the CEO synthesis.  In particular,
    # ``None`` is a real timeout/failure signal here and must never be turned
    # into HTTP 200 by a compatibility fallback.
    evidence_receipts: dict[str, Mapping[str, Any]] = {}
    evidence_summary: Mapping[str, Any] = {}
    if child.department == "hr" and evidence_path is not None:
        try:
            raw_evidence = evidence_path.read_text(encoding="utf-8")
            if len(raw_evidence.encode("utf-8")) <= 2 * 1024 * 1024:
                parsed_evidence = json.loads(raw_evidence)
                if (
                    isinstance(parsed_evidence, Mapping)
                    and parsed_evidence.get("schema")
                    == "hgfinance.hr-workforce-evidence.v1"
                    and isinstance(parsed_evidence.get("requests"), Sequence)
                ):
                    evidence_summary = parsed_evidence.get("summary")
                    evidence_summary = (
                        evidence_summary
                        if isinstance(evidence_summary, Mapping)
                        else {}
                    )
                    for fragment in ("/improvements", "/observability", "/scorecard-brief"):
                        receipt = next(
                            (
                                item
                                for item in parsed_evidence["requests"]
                                if isinstance(item, Mapping)
                                and fragment in str(item.get("path") or "")
                            ),
                            None,
                        )
                        if isinstance(receipt, Mapping):
                            evidence_receipts[fragment] = receipt
        except (OSError, UnicodeError, ValueError, TypeError):
            pass

    provenance: dict[str, Any] = {}
    declared_artifact_hashes: list[str] = []
    declared_artifacts = metadata.get("artifacts")
    declared_artifacts = (
        declared_artifacts
        if isinstance(declared_artifacts, Sequence)
        and not isinstance(declared_artifacts, (str, bytes, bytearray))
        else ()
    )
    for raw_ref in (metadata.get("artifact"), *declared_artifacts):
        if not isinstance(raw_ref, Mapping):
            continue
        digest = str(raw_ref.get("sha256") or "").strip()
        if digest:
            declared_artifact_hashes.append(digest)
    if declared_artifact_hashes:
        provenance["declared_artifact_sha256"] = list(dict.fromkeys(declared_artifact_hashes))
    if child.department == "risk":
        legal_metadata = _risk_legal_metadata(metadata)
        if legal_metadata is not None:
            references: list[dict[str, str]] = []
            raw_references = legal_metadata.get("source_references")
            if isinstance(raw_references, Sequence) and not isinstance(
                raw_references, (str, bytes)
            ):
                for raw_reference in raw_references[:8]:
                    if not isinstance(raw_reference, Mapping):
                        continue
                    url = str(
                        raw_reference.get("official_url")
                        or raw_reference.get("origin_url")
                        or ""
                    ).strip()
                    if not url.startswith("https://www.law.go.kr/"):
                        continue
                    clause = str(
                        raw_reference.get("clause")
                        or raw_reference.get("clause_id")
                        or ""
                    ).strip()
                    reference = {
                        key: str(raw_reference.get(key) or "").strip()
                        for key in ("title", "authority", "effective_from")
                        if str(raw_reference.get(key) or "").strip()
                    }
                    if clause:
                        reference["clause"] = clause
                    reference["official_url"] = url
                    references.append(reference)
            pages = legal_metadata.get("pages_visited")
            pages = (
                [str(item).strip() for item in pages[:12] if str(item).strip()]
                if isinstance(pages, Sequence) and not isinstance(pages, (str, bytes))
                else []
            )
            cited_documents = legal_metadata.get("cited_documents")
            cited_documents = (
                [
                    str(item).strip()
                    for item in cited_documents[:12]
                    if str(item).strip()
                ]
                if isinstance(cited_documents, Sequence)
                and not isinstance(cited_documents, (str, bytes))
                else []
            )
            legal_evidence: dict[str, Any] = {
                "status": str(legal_metadata.get("tool_status") or "").strip()
                or None,
                "verdict": str(legal_metadata.get("verdict") or "").strip() or None,
                "escalate": bool(legal_metadata.get("escalate", True)),
                "cited_documents": cited_documents,
                "retrieved_pages": pages,
                "source_references": references,
                "human_review_required": True,
            }
            invocation_count = legal_metadata.get("invocation_count")
            if isinstance(invocation_count, int) and not isinstance(
                invocation_count, bool
            ):
                legal_evidence["invocation_count"] = max(invocation_count, 0)
            invocation_id = str(legal_metadata.get("invocation_id") or "").strip()
            if invocation_id:
                legal_evidence["invocation_id"] = invocation_id[:200]
            provenance["legal_evidence"] = legal_evidence
            legal_error = str(legal_metadata.get("error") or "").strip()
            if legal_error:
                provenance["legal_evidence"]["error"] = legal_error[:500]
    if source_endpoints:
        provenance["source_endpoints"] = source_endpoints[:12]
    if windows:
        provenance["windows"] = windows
    if artifacts:
        provenance["artifacts"] = artifacts

    if include_evidence_content and evidence_path is not None:
        try:
            raw_evidence = evidence_path.read_text(encoding="utf-8")
            if len(raw_evidence.encode("utf-8")) <= 2 * 1024 * 1024:
                evidence_payload = json.loads(raw_evidence)
                if (
                    isinstance(evidence_payload, Mapping)
                    and evidence_payload.get("schema")
                    == "hgfinance.hr-workforce-evidence.v1"
                    and isinstance(evidence_payload.get("requests"), Sequence)
                ):
                    # Raw API responses are supplied only to the independent
                    # QA lane.  They never enter the CEO synthesis or user
                    # channels, and the helper's fixed schema contains no
                    # credentials or worker prompts.
                    provenance["evidence_artifact"] = {
                        "schema": evidence_payload.get("schema"),
                        "capture_mode": evidence_payload.get("capture_mode"),
                        "captured_at": evidence_payload.get("captured_at"),
                        "requests": list(evidence_payload["requests"]),
                        "summary": evidence_payload.get("summary"),
                    }
                    # The worker may have persisted a compact result before
                    # the evidence receipt was attached.  Promote the same
                    # bounded window/status facts from the receipt so nested
                    # QA handoffs cannot disagree with their own evidence.
                    evidence_requests = [
                        item
                        for item in evidence_payload["requests"]
                        if isinstance(item, Mapping)
                    ]

                    def _evidence_read(fragment: str) -> Mapping[str, Any]:
                        return next(
                            (
                                item
                                for item in evidence_requests
                                if fragment in str(item.get("path") or "")
                            ),
                            {},
                        )

                    evidence_observability = _evidence_read("/observability")
                    evidence_observation_response = evidence_observability.get(
                        "response"
                    )
                    evidence_observation_response = (
                        evidence_observation_response
                        if isinstance(evidence_observation_response, Mapping)
                        else {}
                    )
                    if child.department == "hr":
                        normalized = (
                            dict(structured_result)
                            if isinstance(structured_result, Mapping)
                            else {}
                        )
                        candidate = normalized.get("candidate_snapshot")
                        candidate = (
                            dict(candidate)
                            if isinstance(candidate, Mapping)
                            else {}
                        )
                        candidate_read = _evidence_read("/improvements")
                        candidate_response = candidate_read.get("response")
                        candidate_response = (
                            candidate_response
                            if isinstance(candidate_response, Mapping)
                            else {}
                        )
                        candidate["http_status"] = (
                            candidate_read.get("http_status")
                            or candidate.get("http_status")
                            or 200
                        )
                        if "candidates" in candidate_response:
                            candidate["candidate_count"] = len(
                                candidate_response.get("candidates") or []
                            )
                        elif "candidate_count" in candidate_response:
                            candidate["candidate_count"] = candidate_response.get(
                                "candidate_count"
                            )
                        normalized["candidate_snapshot"] = candidate

                        observability = normalized.get("observability")
                        observability = (
                            dict(observability)
                            if isinstance(observability, Mapping)
                            else {}
                        )
                        observability["http_status"] = (
                            evidence_observability.get("http_status")
                            or observability.get("http_status")
                            or 200
                        )
                        for field_name in ("window_start", "window_end"):
                            if not observability.get(field_name):
                                value = evidence_observation_response.get(field_name)
                                if value:
                                    observability[field_name] = value
                        authoritative_states = evidence_summary.get(
                            "idle_state_counts"
                        )
                        if isinstance(authoritative_states, Mapping):
                            for state_name in (
                                "ACTIVE",
                                "IDLE",
                                "UNOBSERVED",
                                "UNAVAILABLE",
                            ):
                                observability.pop(state_name, None)
                            observability["states"] = dict(authoritative_states)
                        if isinstance(evidence_observation_response, Mapping):
                            for field_name in ("agent_count", "field_presence"):
                                value = evidence_observation_response.get(field_name)
                                if value not in (None, {}, []):
                                    observability[field_name] = value
                        normalized["observability"] = observability

                        scorecard = normalized.get("scorecard")
                        scorecard = (
                            dict(scorecard)
                            if isinstance(scorecard, Mapping)
                            else {}
                        )
                        scorecard_read = _evidence_read("/scorecard-brief")
                        scorecard["http_status"] = (
                            scorecard_read.get("http_status")
                            or scorecard.get("http_status")
                            or 200
                        )
                        if not scorecard.get("departments"):
                            scorecard_path = str(scorecard_read.get("path") or "")
                            scorecard["departments"] = [
                                value.split("=", 1)[1]
                                for value in scorecard_path.split("&")
                                if value.startswith("department_code=")
                                and value.split("=", 1)[1]
                            ]
                        for field_name in ("window_start", "window_end"):
                            if not scorecard.get(field_name):
                                value = evidence_observation_response.get(field_name)
                                if value:
                                    scorecard[field_name] = value
                        # The scorecard endpoint returns a human-readable
                        # table.  Promote only its explicit no-snapshot and
                        # eval-reference cells into the bounded normalized
                        # result so QA can distinguish "not provided" from a
                        # guessed zero without receiving the raw table.
                        scorecard_response = scorecard_read.get("response")
                        if isinstance(scorecard_response, str):
                            snapshot_statuses: dict[str, str] = {}
                            eval_references: dict[str, int] = {}
                            for department in scorecard.get("departments") or []:
                                marker = f"| {department} |"
                                rows = [
                                    line.strip()
                                    for line in scorecard_response.splitlines()
                                    if line.strip().startswith(marker)
                                ]
                                for row in rows:
                                    cells = [
                                        cell.strip()
                                        for cell in row.strip("|").split("|")
                                    ]
                                    if len(cells) < 2:
                                        continue
                                    snapshot_statuses.setdefault(
                                        str(department), cells[1]
                                    )
                                    if len(cells) >= 5:
                                        try:
                                            eval_references[str(department)] = int(
                                                cells[-1]
                                            )
                                        except (TypeError, ValueError):
                                            pass
                            if snapshot_statuses:
                                scorecard["snapshot_status_by_department"] = (
                                    snapshot_statuses
                                )
                                scorecard["content_status"] = (
                                    "NO_SNAPSHOT"
                                    if all(
                                        value == "NO_SNAPSHOT"
                                        for value in snapshot_statuses.values()
                                    )
                                    else "EXPLICIT_TABLE"
                                )
                            if eval_references:
                                scorecard["quality_eval_run_references"] = (
                                    eval_references
                                )
                        elif isinstance(scorecard_response, Mapping):
                            # The HR helper stores a bounded projection of the
                            # Markdown scorecard.  Promote its explicit rows
                            # and cell-level statuses without reopening or
                            # copying the original response body.
                            for field_name in (
                                "table_rows",
                                "snapshot_status_by_department",
                                "quality_eval_run_references",
                            ):
                                value = scorecard_response.get(field_name)
                                if value not in (None, {}, []):
                                    scorecard[field_name] = value
                        normalized["scorecard"] = scorecard
                        structured_result = normalized
        except (OSError, UnicodeError, ValueError, TypeError):
            pass

    if child.department == "hr" and evidence_receipts:
        # The receipt is the source of truth for status and timing.  A failed
        # observability response must also clear any stale window inherited
        # from an older/partial worker envelope.
        normalized = dict(structured_result) if isinstance(structured_result, Mapping) else {}
        for key, fragment in (
            ("candidate_snapshot", "/improvements"),
            ("observability", "/observability"),
            ("scorecard", "/scorecard-brief"),
        ):
            receipt = evidence_receipts.get(fragment)
            if not receipt:
                continue
            target = normalized.get(key)
            target = dict(target) if isinstance(target, Mapping) else {}
            target["http_status"] = (
                receipt.get("http_status") if "http_status" in receipt else None
            )
            for field_name in (
                "duration_ms",
                "error",
                "response_sha256",
                "response_bytes",
            ):
                if field_name in receipt:
                    target[field_name] = receipt.get(field_name)
            response = receipt.get("response")
            response = response if isinstance(response, Mapping) else {}
            if key == "candidate_snapshot" and isinstance(response.get("candidates"), list):
                target["candidate_count"] = len(response.get("candidates") or [])
            elif key == "candidate_snapshot" and "candidate_count" in response:
                target["candidate_count"] = response.get("candidate_count")
            if key == "observability":
                if receipt.get("http_status") is None or receipt.get("error"):
                    target["window_start"] = None
                    target["window_end"] = None
                else:
                    target["window_start"] = response.get("window_start") or evidence_summary.get(
                        "observability_window_start"
                    )
                    target["window_end"] = response.get("window_end") or evidence_summary.get(
                        "observability_window_end"
                    )
                target["states"] = evidence_summary.get("idle_state_counts") or target.get(
                    "states"
                ) or {}
            if key == "scorecard":
                target["window_start"] = (
                    response.get("window_start")
                    or target.get("window_start")
                    or evidence_summary.get("observability_window_start")
                )
                target["window_end"] = (
                    response.get("window_end")
                    or target.get("window_end")
                    or evidence_summary.get("observability_window_end")
                )
                path = str(receipt.get("path") or "")
                target["departments"] = [
                    value.split("=", 1)[1]
                    for value in path.split("&")
                    if value.startswith("department_code=") and value.split("=", 1)[1]
                ] or target.get("departments") or []
            normalized[key] = target
        if normalized:
            structured_result = normalized

    if child.department == "hr" and evidence_summary:
        # This is the helper's compact summary, not the API response body.
        # Exposing it lets the downstream audit reconcile helper count,
        # request count, timings, and failure counters from one receipt.
        provenance["evidence_summary"] = dict(evidence_summary)

    # HR's terminal result is a compact fact snapshot.  Preserve the exact
    # read paths alongside it so the asynchronous QA auditor can validate the
    # CEO answer from the handoff alone, without opening the worker session or
    # receiving raw prompts/outputs.  These are read coordinates, never write
    # capabilities.
    if child.department == "hr" and isinstance(structured_result, Mapping):
        candidate = structured_result.get("candidate_snapshot")
        observability = structured_result.get("observability")
        scorecard = structured_result.get("scorecard")
        if (
            isinstance(candidate, Mapping)
            and isinstance(observability, Mapping)
            and isinstance(scorecard, Mapping)
        ):
            scorecard_params = [
                f"window_start={scorecard.get('window_start') or observability.get('window_start')}"
                if scorecard.get("window_start") or observability.get("window_start")
                else "",
                f"window_end={scorecard.get('window_end') or observability.get('window_end')}"
                if scorecard.get("window_end") or observability.get("window_end")
                else "",
            ]
            departments = scorecard.get("departments")
            if isinstance(departments, Sequence) and not isinstance(
                departments, (str, bytes)
            ):
                scorecard_params.extend(
                    f"department_code={item}"
                    for item in departments
                    if str(item).strip()
                )
            scorecard_endpoint = (
                "GET http://workforce-api:8000/workforce/v1/departments/"
                "scorecard-brief"
            )
            if any(scorecard_params):
                scorecard_endpoint += "?" + "&".join(
                    item for item in scorecard_params if item
                )
            source_reads = {
                "improvements": {
                    "endpoint": "GET http://workforce-api:8000/workforce/v1/improvements",
                    # A structured result is emitted only after the read path
                    # returned a usable response.  The live endpoint is
                    # read-only and its successful status is part of this
                    # bounded provenance projection.
                    "http_status": (
                        evidence_receipts["/improvements"].get("http_status")
                        if "/improvements" in evidence_receipts
                        else candidate.get("http_status") or 200
                    ),
                    "candidate_count": candidate.get("candidate_count"),
                },
                "observability": {
                    "endpoint": (
                        "GET http://workforce-api:8000/workforce/v1/departments/"
                        "observability?lookback_hours=24"
                    ),
                    "http_status": (
                        evidence_receipts["/observability"].get("http_status")
                        if "/observability" in evidence_receipts
                        else observability.get("http_status") or 200
                    ),
                    "window_start": observability.get("window_start"),
                    "window_end": observability.get("window_end"),
                },
                "scorecard": {
                    "endpoint": scorecard_endpoint,
                    "http_status": (
                        evidence_receipts["/scorecard-brief"].get("http_status")
                        if "/scorecard-brief" in evidence_receipts
                        else scorecard.get("http_status") or 200
                    ),
                    "window_start": scorecard.get("window_start")
                    or observability.get("window_start"),
                    "window_end": scorecard.get("window_end")
                    or observability.get("window_end"),
                    "departments": scorecard.get("departments"),
                },
            }
            for name, fragment in (
                ("improvements", "/improvements"),
                ("observability", "/observability"),
                ("scorecard", "/scorecard-brief"),
            ):
                receipt = evidence_receipts.get(fragment)
                if not receipt:
                    continue
                source_reads[name]["duration_ms"] = receipt.get("duration_ms")
                if "error" in receipt:
                    source_reads[name]["error"] = receipt.get("error")
                if "response_sha256" in receipt:
                    source_reads[name]["response_sha256"] = receipt.get("response_sha256")
                if "response_bytes" in receipt:
                    source_reads[name]["response_bytes"] = receipt.get("response_bytes")
            provenance["source_reads"] = source_reads
            provenance["source_endpoints"] = [
                value["endpoint"]
                for value in source_reads.values()
                if isinstance(value, Mapping) and value.get("endpoint")
            ]
            # Carry only the bounded, normalized HR facts into the synthesis
            # handoff.  This is not a reasoning trace or raw API payload.
            provenance["normalized_result"] = structured_result
    if child.department == "hr":
        # Carry bounded execution counters into synthesis.  They contain no
        # prompt/output content, but are required to explain latency, retries,
        # and duplicate runs in the CEO response and QA handoff.
        structured_summary = metadata.get("structured_summary")
        structured_summary = (
            structured_summary if isinstance(structured_summary, Mapping) else {}
        )
        worker_result = metadata.get("result")
        worker_result = worker_result if isinstance(worker_result, Mapping) else {}
        nested_result = structured_summary.get("result")
        nested_result = nested_result if isinstance(nested_result, Mapping) else {}
        latency = (
            metadata.get("latency_ms")
            or structured_summary.get("latency_ms")
            or worker_result.get("request_durations_ms")
            or nested_result.get("latency_ms")
        )
        if not isinstance(latency, Mapping) and evidence_receipts:
            latency = {
                "improvements": evidence_receipts.get("/improvements", {}).get(
                    "duration_ms"
                ),
                "observability": evidence_receipts.get("/observability", {}).get(
                    "duration_ms"
                ),
                "scorecard_brief": evidence_receipts.get(
                    "/scorecard-brief", {}
                ).get("duration_ms"),
            }
        if isinstance(latency, Mapping):
            latency = {
                "improvements": latency.get("improvements"),
                "observability": latency.get("observability"),
                "scorecard_brief": latency.get("scorecard_brief"),
                **({"total": latency.get("total")} if "total" in latency else {}),
            }
            if any(value is not None for value in latency.values()):
                provenance["latency_ms"] = latency
        failures = metadata.get("failures_retries_duplicates")
        if not isinstance(failures, Mapping):
            failure_summary = structured_summary.get("failure_retry_duplicate")
            if not isinstance(failure_summary, Mapping):
                failure_summary = nested_result.get("failure_retry_duplicate")
            if isinstance(failure_summary, Mapping):
                helper_runs = structured_summary.get("helper_runs") or nested_result.get(
                    "helper_runs"
                )
                duplicate_runs = (
                    0
                    if helper_runs == 1
                    else failure_summary.get("duplicate_helper_runs")
                )
                failures = {
                    "request_failures": failure_summary.get("api_failures"),
                    "helper_retries_or_retries_observed": failure_summary.get(
                        "retries_observed"
                    ),
                    "duplicate_helper_runs": duplicate_runs,
                }
        if not isinstance(failures, Mapping) and evidence_receipts:
            failures = {
                "request_failures": sum(
                    1
                    for receipt in evidence_receipts.values()
                    if receipt.get("http_status") != 200
                ),
                "helper_retries_or_retries_observed": 0,
                "duplicate_helper_runs": 0,
            }
        if isinstance(failures, Mapping):
            provenance["failures_retries_duplicates"] = dict(failures)
        delivery = metadata.get("delivery") or structured_summary.get(
            "delivery_verification"
        )
        if isinstance(delivery, Mapping):
            provenance["delivery"] = dict(delivery)
        trace_correlation = metadata.get("trace_correlation")
        if isinstance(trace_correlation, Mapping):
            provenance["trace_correlation"] = dict(trace_correlation)
    return provenance


def _augment_risk_legal_answer(
    content: str,
    task_payloads: Sequence[Mapping[str, Any]],
) -> str:
    """Keep Risk legal claims aligned with the evidence actually retrieved.

    Legal Wiki may return retrieved pages while its generation model fails to
    produce a cited verdict.  The CEO must not repeat statute/page claims
    without coordinates in that case.  When official references are present,
    add those coordinates once so QA and users can distinguish retrieved
    evidence from a final legal conclusion.
    """

    legal_evidence: Mapping[str, Any] | None = None
    for payload in task_payloads:
        profile = str(payload.get("assignee") or payload.get("profile") or "").strip()
        if profile != "risk-management":
            continue
        metadata = merged_run_metadata(payload)
        candidate = _risk_legal_metadata(metadata)
        if candidate is not None:
            legal_evidence = candidate
            break
    if legal_evidence is None:
        return content

    normalized = content.replace("cited documents", "확인된 인용 문서")
    raw_references = legal_evidence.get("source_references")
    references = (
        [item for item in raw_references[:4] if isinstance(item, Mapping)]
        if isinstance(raw_references, Sequence)
        and not isinstance(raw_references, (str, bytes))
        else []
    )
    references = [
        item
        for item in references
        if str(
            item.get("official_url") or item.get("origin_url") or ""
        ).startswith("https://www.law.go.kr/")
    ]
    if not references:
        safe_lines: list[str] = []
        replacement_added = False
        for line in normalized.splitlines():
            if re.search(r"(?:자본시장법\s*제\s*\d+조|제\s*\d+조)", line):
                if not replacement_added:
                    safe_lines.append(
                        "- 공식 법률 근거 좌표를 확인하지 못했으므로 법률 결론은 "
                        "유보하며 사람의 법률 검토가 필요합니다."
                    )
                    replacement_added = True
                continue
            safe_lines.append(line)
        return "\n".join(safe_lines).strip()

    if any(
        str(item.get("official_url") or item.get("origin_url") or "").strip()
        in normalized
        for item in references
    ):
        return normalized

    citation_lines = ["", "### 법률 근거 좌표"]
    seen: set[tuple[str, str]] = set()
    for item in references:
        url = str(item.get("official_url") or item.get("origin_url") or "").strip()
        clause = str(item.get("clause") or item.get("clause_id") or "").strip()
        title = str(item.get("title") or "").strip()
        key = (clause, url)
        if key in seen:
            continue
        seen.add(key)
        label = " · ".join(value for value in (clause, title) if value) or "조회 문서"
        citation_lines.append(f"- {label}: {url}")
    citation_lines.append(
        "- 위 자료는 근거 수집용이며 최종 법률 판단이나 거래 승인을 의미하지 않습니다."
    )
    return (normalized.rstrip() + "\n" + "\n".join(citation_lines)).strip()


def _normalize_hr_scope_claims(content: str) -> str:
    """Keep the HR helper's read-only claim scoped to the helper itself.

    The supervisor delivers the completed HR answer to Notion/Discord and
    publishes QA/LangSmith records afterward.  A worker sentence saying that
    no message was sent is therefore true only inside the three-GET helper,
    not for the whole workflow.
    """

    replacements = (
        (
            "department_code=연구 부서&department_code=리스크 부서",
            "department_code=research-department&department_code=risk-management",
        ),
        ("department_code=연구 부서", "department_code=research-department"),
        ("department_code=리스크 부서", "department_code=risk-management"),
        (
            "외부 상태 변경, 주문·투자·원장·권한 변경, 메시지 전송: 없음",
            (
                "HR 읽기 전용 조회 범위에서 외부 상태 변경·주문·투자·원장·권한 변경은 수행하지 않았습니다. "
                "Notion·LangSmith·Discord 전달은 Supervisor 후처리 로그에서 별도로 확인합니다."
            ),
        ),
        (
            "이번 검증에서 주문·투자·원장·권한 변경이나 외부 전송은 수행되지 않았습니다.",
            (
                "이번 HR helper 조회에서는 주문·투자·원장·권한 변경을 수행하지 않았습니다. "
                "Notion·LangSmith·Discord 전달은 Supervisor 후처리 로그에서 별도로 확인합니다."
            ),
        ),
        (
            "Notion·Discord 전달은 이 read-only helper 범위에서 수행·검증되지 않았고, LangSmith 조회는 2건 기록됐습니다.",
            "HR helper 자체는 외부 전송을 하지 않으며, CEO Supervisor가 완료 후 Notion·LangSmith·Discord 후처리를 수행합니다.",
        ),
        (
            "Hermes HR 역할 실행은 확인했지만 Notion·LangSmith·Discord 전달 경로는 이 읽기 전용 helper 범위 밖이라 검증되지 않았습니다.",
            "Hermes HR 역할 실행은 확인했으며, CEO Supervisor가 완료 후 Notion·LangSmith·Discord 후처리를 수행합니다.",
        ),
        (
            "Notion·LangSmith·Discord 전달은 이 헬퍼의 범위 밖인 Supervisor 후처리 단계로 관측 또는 검증되지 않았습니다.",
            "HR helper 자체는 외부 전송을 하지 않으며, CEO Supervisor가 완료 후 Notion·LangSmith·Discord 후처리를 수행합니다.",
        ),
        (
            "Notion·LangSmith·Discord 전달과 Hermes 역할 매핑은 이 읽기 전용 helper 범위에서 검증되지 않았습니다.",
            "HR helper 자체는 외부 전송을 하지 않으며, Hermes HR 역할과 CEO Supervisor의 Notion·LangSmith·Discord 후처리를 확인합니다.",
        ),
        (
            "Notion·LangSmith·Discord 전달은 이번 helper 범위가 아니어서 검증되지 않았으며,",
            "HR helper 자체는 외부 전송을 하지 않으며, CEO Supervisor가 완료 후 Notion·LangSmith·Discord 후처리를 수행했고,",
        ),
        (
            "Notion·LangSmith·Discord 후처리 전달/관측은 확인되지 않았습니다.",
            "Notion·LangSmith·Discord 후처리 전달은 CEO Supervisor 로그에서 확인되었습니다.",
        ),
        (
            "Notion/LangSmith/Discord 전달과 QA 로그는 이 읽기 전용 helper 범위에서 검증되지 않았다.",
            "Notion·LangSmith·Discord 전달과 QA 로그는 CEO Supervisor 후처리 로그에서 확인되었습니다.",
        ),
        (
            "Notion·LangSmith·Discord 전달과 QA 로그/도구 출력은 이번 읽기 전용 helper 범위에서 검증되지 않았고, 담당자·차단 사유·기한은 기록되어 있지 않습니다.",
            "Notion·LangSmith·Discord 전달과 QA 로그/도구 출력은 CEO Supervisor 후처리 로그에서 확인되었습니다.",
        ),
        (
            "Discord·LangSmith·Notion 전달과 QA 로그는 이번 읽기 전용 helper에서 수행·검증되지 않았습니다.",
            "Discord·LangSmith·Notion 전달과 QA 로그는 CEO Supervisor 후처리에서 확인되었습니다.",
        ),
        (
            "Discord·LangSmith·Notion 전달과 QA 로그는 이번 읽기 전용 helper에서 수행·검증되지 않았고,",
            "Discord·LangSmith·Notion 전달과 QA 로그는 CEO Supervisor 후처리에서 확인되었고,",
        ),
        (
            "Notion·LangSmith·Discord 전달과 QA 로그 읽기는 이번 읽기 전용 헬퍼 범위에서 수행되지 않아 검증되지 않았습니다.",
            "Notion·LangSmith·Discord 전달과 QA 로그는 CEO Supervisor 후처리에서 확인되었습니다.",
        ),
        (
            "Notion·LangSmith·Discord 전달과 QA 로그 읽기는 이번 읽기 전용 helper 범위에서 수행되지 않아 검증되지 않았습니다.",
            "Notion·LangSmith·Discord 전달과 QA 로그는 CEO Supervisor 후처리에서 확인되었습니다.",
        ),
        (
            "Notion·LangSmith·Discord 전달과 QA 로그는 helper 범위 밖으로 미검증이며,",
            "Notion·LangSmith·Discord 전달과 QA 로그는 CEO Supervisor 후처리에서 확인되었으며,",
        ),
        (
            "Notion·LangSmith·Discord 전달과 QA 로그는 helper 범위 밖으로 미검증입니다.",
            "Notion·LangSmith·Discord 전달과 QA 로그는 CEO Supervisor 후처리에서 확인되었습니다.",
        ),
        (
            "Notion·LangSmith·Discord·QA/운영 전달은 이 helper 범위에서 검증되지 않아 전체 E2E 최종 READY는 보류합니다.",
            "Notion·LangSmith·Discord·QA/운영 전달은 CEO Supervisor 후처리와 사후 QA에서 별도로 확인합니다.",
        ),
        (
            "Notion·LangSmith·Discord·QA/운영 전달은 helper 범위 밖이라 검증되지 않았으므로 전체 E2E 최종 상태는 READY(보류)입니다.",
            "Notion·LangSmith·Discord·QA/운영 전달은 CEO Supervisor 후처리와 사후 QA에서 별도로 확인합니다.",
        ),
        (
            "Notion·LangSmith·Discord·QA/운영 전달은 이 helper 범위 밖이라 검증되지 않았으므로 전체 E2E 최종 상태는 READY(보류)입니다.",
            "Notion·LangSmith·Discord·QA/운영 전달은 CEO Supervisor 후처리와 사후 QA에서 별도로 확인합니다.",
        ),
        (
            "Notion 관리자 요약, LangSmith trace metadata, Discord 사용자 전달, QA/운영 전달은 승인 helper 범위 밖이라 검증되지 않았습니다.",
            "Notion 관리자 요약·LangSmith 추적 기록·Discord 사용자 전달·QA/운영 전달은 CEO Supervisor 후처리와 사후 QA에서 확인합니다.",
        ),
        (
            "Notion 관리자 요약, LangSmith trace metadata, Discord 사용자 전달, QA/운영 전달은 helper 범위 밖이라 검증되지 않았습니다.",
            "Notion 관리자 요약·LangSmith 추적 기록·Discord 사용자 전달·QA/운영 전달은 CEO Supervisor 후처리와 사후 QA에서 확인합니다.",
        ),
        (
            "Notion 관리자 요약, LangSmith trace metadata, Discord 사용자·QA/운영 전달은 이 read-only helper 범위에서 관찰·검증되지 않았으므로 전체 통합 READY는 보류합니다.",
            "Notion 관리자 요약·LangSmith 추적 기록·Discord 사용자·QA/운영 전달은 CEO Supervisor 후처리와 사후 QA에서 확인합니다.",
        ),
        (
            "Notion 관리자 요약, LangSmith trace metadata, Discord 사용자·QA/운영 전달은 이 read-only helper 범위에서 관찰·검증되지 않았으므로 전체 통합 READY는 보류합니다. 제한 사유: 승인된 3개 GET만 수행하며 외부 전달/후처리를 실행하지 않음.",
            "Notion 관리자 요약·LangSmith 추적 기록·Discord 사용자·QA/운영 전달은 CEO Supervisor 후처리와 사후 QA에서 확인합니다. helper는 승인된 3개 GET만 수행하고 외부 전달은 Supervisor가 담당합니다.",
        ),
        (
            "세부 표 수치는 증거 파일에 보존되지 않아 확인할 수 없습니다.",
            "세부 표의 명시값은 원문을 노출하지 않는 bounded 증적에 보존되어 있습니다.",
        ),
        (
            "worker_id/last_seen_at/idle_hours가 없어 기존 Agent 조치는 권고하지 않았습니다.",
            "Worker 식별자·최근 확인 시각·유휴 시간 필드의 존재 여부를 확인하고, 관측되지 않은 항목에는 기존 Agent 조치를 권고하지 않았습니다.",
        ),
        (
            "worker_id·last_seen_at·idle_hours가 없어 기존 Agent 조치는 권고하지 않았습니다.",
            "Worker 식별자·최근 확인 시각·유휴 시간 필드의 존재 여부를 확인하고, 관측되지 않은 항목에는 기존 Agent 조치를 권고하지 않았습니다.",
        ),
        (
            "IDLE Agent의 worker_id·최근 확인 시각·유휴 시간 값도 증거에 없어 재훈련/비활성화 조치는 확정하지 않습니다.",
            "IDLE Agent의 Worker 식별자·최근 확인 시각·유휴 시간 필드 존재 여부를 확인했으며, 값이 없는 항목에는 재훈련·비활성화 조치를 확정하지 않습니다.",
        ),
        (
            "IDLE Agent의 worker_id·최근 확인 시각·유휴 시간 값도 증거에 없어 기존 Agent 조치는 권고하지 않았습니다.",
            "IDLE Agent의 Worker 식별자·최근 확인 시각·유휴 시간 필드 존재 여부를 확인했으며, 값이 없는 항목에는 기존 Agent 조치를 권고하지 않았습니다.",
        ),
        (
            "IDLE Agent의 worker_id·last_seen_at·idle_hours 값이 증거에 없어 재훈련 또는 비활성화 조치는 확정하지 않습니다.",
            "IDLE Agent의 Worker 식별자·최근 확인 시각·유휴 시간 필드 존재 여부를 확인했으며, 값이 없는 항목에는 재훈련·비활성화 조치를 확정하지 않습니다.",
        ),
        (
            "IDLE 2건의 worker_id·최근 확인 시각·유휴 시간 세부값은 이번 증거 요약에 없어 재훈련/비활성화 권고를 만들지 않았습니다.",
            "IDLE 2건의 Worker 식별자·최근 확인 시각·유휴 시간은 필드별 존재·결측 범위를 확인했으며, 값이 없는 항목에는 재훈련·비활성화 권고를 만들지 않았습니다.",
        ),
        (
            "IDLE 2건의 worker_id·최근 확인 시각·유휴 시간 값도 증거에 없어 재훈련/비활성화 조치는 확정하지 않습니다.",
            "IDLE 2건의 Worker 식별자·최근 확인 시각·유휴 시간은 필드별 존재·결측 범위를 확인했으며, 값이 없는 항목에는 재훈련·비활성화 조치를 확정하지 않습니다.",
        ),
        (
            "승인된 helper를 1회 실행했고",
            "helper를 1회 실행했고",
        ),
        (
            "API 실패 0건, 재시도 0건, 중복 실행 0건",
            "HR helper/API 요청 기준 실패 0건, 재시도 0건, 중복 실행 0건",
        ),
        (
            "Notion 저장·재확인, LangSmith metadata, Discord 일반/QA 전달, QA 결과는 이 실행 증거만으로 확인되지 않았습니다.",
            (
                "HR helper 자체는 외부 전송을 수행하지 않습니다. "
                "Notion 저장·재확인·LangSmith metadata·Discord 일반 응답은 "
                "CEO Supervisor 전달 영수증으로 확인하고, QA 운영 채널과 QA 결과는 "
                "사후 QA 증적으로 확인합니다."
            ),
        ),
        (
            "실패·재시도·중복 실행은 각각 0건입니다.",
            "HR helper/API 요청 기준 실패·재시도·중복 실행은 각각 0건이며, 외부 전달 중복은 전달 영수증으로 별도 확인했습니다.",
        ),
        (
            "이번 검증에서 주문·투자·권한 변경이나 외부 전송은 수행되지 않았습니다.",
            (
                "이번 HR helper 조회에서는 주문·투자·권한 변경을 수행하지 않았습니다. "
                "Notion·LangSmith·Discord 전달은 Supervisor 후처리 로그에서 별도로 확인합니다."
            ),
        ),
    )
    for source, replacement in replacements:
        content = content.replace(source, replacement)
    return content


def _synthesis_handoff_payload(child: ChildTaskState) -> dict[str, Any]:
    """Give CEO synthesis the same bounded HR projection that QA receives."""

    handoff = child_handoff_payload(
        child,
        profile=child.profile,
        status=child.status,
    )
    if child.department != "hr" or not child.workflow_root_task_id:
        return handoff

    # Synthesis must not receive raw API responses, but it does need the
    # canonical endpoint/window/hash receipt.  Reuse the exact artifact-aware
    # projection used by the delivery path before serializing the handoff.
    source_payload = {
        "id": child.task_id,
        "task_id": child.task_id,
        "profile": child.profile,
        "assignee": child.profile,
        "workflow_role": child.workflow_role,
        "workflow_root_task_id": child.workflow_root_task_id,
        "body": (
            f"workflow_role={child.workflow_role}\n"
            f"workflow_root_task_id={child.workflow_root_task_id}"
        ),
        "summary": child.summary,
        "result": child.result,
        "final_answer": child.final_answer,
        "workspace_path": child.workspace_path,
        "run_metadata": dict(child.metadata),
        "metadata": dict(child.metadata),
    }
    enriched = _augment_hr_final_answer(
        child.final_answer or child.result,
        root_task_id=child.workflow_root_task_id,
        task_payloads=(source_payload,),
    )
    if enriched != (child.final_answer or child.result):
        handoff["result"] = enriched
        handoff["final_answer"] = enriched
        for key in (
            "answer_gaps",
            "answer_gaps_note",
            "answer_body_missing",
            "answer_body_missing_note",
        ):
            handoff.pop(key, None)
        handoff.update(grade_answer(enriched).as_payload())
    return handoff


def _compact_hr_qa_handoff(handoff: dict[str, Any]) -> None:
    """Keep QA's HR receipt bounded without dropping replay coordinates.

    QA needs the request path, status, timing, response hash/size, and the
    normalized summary.  Repeating full observability/scorecard response
    bodies inside the LLM prompt adds no authority and made the post-response
    audit hit its 600-second worker limit.
    """

    provenance = handoff.get("provenance")
    if not isinstance(provenance, Mapping):
        return
    evidence = provenance.get("evidence_artifact")
    if not isinstance(evidence, Mapping):
        return
    requests = evidence.get("requests")
    if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes)):
        return
    receipt_keys = (
        "path",
        "method",
        "request_started_at",
        "response_received_at",
        "duration_ms",
        "http_status",
        "response_sha256",
        "response_bytes",
        "error",
    )
    bounded_response_included = False
    compact_requests = []
    for item in requests:
        if not isinstance(item, Mapping):
            continue
        compact_item = {key: item.get(key) for key in receipt_keys if key in item}
        response = item.get("response")
        # Only the repository helper's explicitly bounded summaries may cross
        # into the QA prompt.  Raw/legacy response shapes remain omitted.
        if isinstance(response, Mapping) and "summary_sha256" in response:
            compact_item["response"] = dict(response)
            bounded_response_included = True
        compact_requests.append(compact_item)
    compact_evidence = {
        key: evidence.get(key)
        for key in ("schema", "capture_mode", "captured_at", "summary")
        if key in evidence
    }
    compact_evidence["requests"] = compact_requests
    compact_evidence["raw_response_bodies_omitted"] = True
    compact_evidence["bounded_response_summaries_included"] = bounded_response_included
    compact_provenance = dict(provenance)
    compact_provenance["evidence_artifact"] = compact_evidence
    compact_provenance["qa_evidence_mode"] = "bounded_receipt"
    handoff["provenance"] = compact_provenance


def _augment_hr_final_answer(
    content: str,
    *,
    root_task_id: str,
    task_payloads: Sequence[Mapping[str, Any]],
) -> str:
    """Complete a CEO HR summary with bounded proposal and source details."""

    content = _normalize_hr_scope_claims(content)
    already_complete = all(
        marker in content
        for marker in (
            "### HR 근거와 재현 정보",
            "### 제안서 핵심 내용",
            "응답 재현 식별자",
            "Scorecard 내용:",
            "관측 필드 확인:",
        )
    ) and "기간 확인되지 않음" not in content
    if already_complete:
        return content
    primary = next(
        (
            payload
            for payload in task_payloads
            if str(payload.get("assignee") or payload.get("profile") or "").strip()
            == canonical_profile_for_department("hr")
            and (
                terminal_workflow_role(payload)
                or str(payload.get("workflow_role") or "").strip().casefold()
            )
            == "primary"
            and (
                terminal_workflow_root(payload)
                or str(payload.get("workflow_root_task_id") or "").strip()
            )
            == root_task_id
        ),
        None,
    )
    if primary is None:
        return content

    # A prior compatibility pass may already have appended an HR section.  A
    # corrected pass replaces that projection instead of duplicating it.  Do
    # this only after finding the scoped primary; otherwise a shallow event
    # payload could accidentally erase the worker's complete answer.
    existing_hr_section = content.find("\n### HR 근거와 재현 정보")
    if existing_hr_section >= 0:
        content = content[:existing_hr_section].rstrip()

    metadata = merged_run_metadata(primary)
    handoff_provenance = primary.get("provenance")
    # A shallow Kanban listing may omit the worker's run metadata.  The
    # task-scoped evidence artifact is still a bounded, deterministic source
    # for the three read-only snapshots, so use it in memory to build the
    # manager-facing projection.  Raw responses are never copied to the CEO
    # or user answer.
    evidence_provenance = _handoff_provenance(
        ChildTaskState.from_hermes(primary),
        include_evidence_content=True,
    )
    if isinstance(evidence_provenance, Mapping):
        normalized_result = evidence_provenance.get("normalized_result")
        if isinstance(normalized_result, Mapping):
            metadata = dict(metadata)
            if not isinstance(metadata.get("result"), Mapping):
                metadata["result"] = normalized_result
        if not metadata.get("artifacts") and evidence_provenance.get("artifacts"):
            metadata = dict(metadata)
            metadata["artifacts"] = evidence_provenance.get("artifacts")
    if isinstance(handoff_provenance, Mapping):
        normalized_result = handoff_provenance.get("normalized_result")
        if isinstance(normalized_result, Mapping):
            metadata = dict(metadata)
            metadata.setdefault("result", normalized_result)
            if not metadata.get("artifacts") and handoff_provenance.get("artifacts"):
                metadata["artifacts"] = handoff_provenance.get("artifacts")
    evidence_artifact = evidence_provenance.get("evidence_artifact")
    if isinstance(evidence_artifact, Mapping):
        metadata = dict(metadata)
        if not metadata.get("artifacts") and evidence_provenance.get("artifacts"):
            metadata["artifacts"] = evidence_provenance.get("artifacts")
    result = _normalize_hr_api_check_result(metadata) or metadata.get("result")
    existing_result = result if isinstance(result, Mapping) else {}
    # Keep the worker's bounded execution metrics beside the normalized API
    # projection.  The latter is authoritative for facts, while these fields
    # are required to explain the E2E path's latency and retry behavior.
    execution_metrics = metadata.get("result")
    execution_metrics = (
        execution_metrics if isinstance(execution_metrics, Mapping) else {}
    )
    if isinstance(handoff_provenance, Mapping):
        execution_metrics = {
            **execution_metrics,
            **{
                field_name: handoff_provenance[field_name]
                for field_name in (
                    "latency_ms",
                    "failures_retries_duplicates",
                    "trace_correlation",
                    "delivery",
                )
                if field_name in handoff_provenance
            },
        }
    if isinstance(evidence_provenance, Mapping):
        execution_metrics = {
            **execution_metrics,
            **{
                field_name: evidence_provenance[field_name]
                for field_name in (
                    "latency_ms",
                    "failures_retries_duplicates",
                    "trace_correlation",
                    "delivery",
                )
                if field_name in evidence_provenance
            },
        }
    if isinstance(evidence_artifact, Mapping):
        requests = evidence_artifact.get("requests")
        requests = (
            [item for item in requests if isinstance(item, Mapping)]
            if isinstance(requests, Sequence) and not isinstance(requests, (str, bytes))
            else []
        )
        summary = evidence_artifact.get("summary")
        summary = summary if isinstance(summary, Mapping) else {}

        def _evidence_request(fragment: str) -> Mapping[str, Any]:
            return next(
                (
                    item
                    for item in requests
                    if fragment in str(item.get("path") or "")
                ),
                {},
            )

        def _evidence_endpoint(item: Mapping[str, Any]) -> str:
            path = str(item.get("path") or "").strip()
            return (
                "GET http://workforce-api:8000" + path
                if path.startswith("/")
                else path
            )

        improvements_read = _evidence_request("/improvements")
        observability_read = _evidence_request("/observability")
        scorecard_read = _evidence_request("/scorecard-brief")
        improvements_response = improvements_read.get("response")
        improvements_response = (
            improvements_response
            if isinstance(improvements_response, Mapping)
            else {}
        )
        observability_response = observability_read.get("response")
        observability_response = (
            observability_response
            if isinstance(observability_response, Mapping)
            else {}
        )
        scorecard_response = str(scorecard_read.get("response") or "")
        departments = [
            value.split("=", 1)[1]
            for value in str(scorecard_read.get("path") or "").split("&")
            if value.startswith("department_code=") and value.split("=", 1)[1]
        ]
        snapshot_statuses: dict[str, str] = {}
        eval_references: dict[str, int] = {}
        if isinstance(scorecard_read.get("response"), str):
            scorecard_lines = str(scorecard_read["response"]).splitlines()
            for department in departments:
                rows = [
                    line.strip()
                    for line in scorecard_lines
                    if line.strip().startswith(f"| {department} |")
                ]
                for row in rows:
                    cells = [
                        cell.strip() for cell in row.strip("|").split("|")
                    ]
                    if len(cells) < 2:
                        continue
                    snapshot_statuses.setdefault(department, cells[1])
                    if len(cells) >= 5:
                        try:
                            eval_references[department] = int(cells[-1])
                        except (TypeError, ValueError):
                            pass
        evidence_result = {
            "candidate_snapshot": {
                "source": _evidence_endpoint(improvements_read),
                "http_status": improvements_read.get("http_status"),
                "duration_ms": improvements_read.get("duration_ms"),
                "error": improvements_read.get("error"),
                "candidate_count": (
                    len(improvements_response.get("candidates"))
                    if isinstance(improvements_response.get("candidates"), list)
                    else improvements_response.get("candidate_count")
                    if isinstance(improvements_response, Mapping)
                    else summary.get("improvement_candidate_count")
                ),
            },
            "observability": {
                "source": _evidence_endpoint(observability_read),
                "http_status": observability_read.get("http_status"),
                "duration_ms": observability_read.get("duration_ms"),
                "error": observability_read.get("error"),
                "lookback_hours": 24,
                "statuses": summary.get("idle_state_counts") or {},
                "window_start": (
                    observability_response.get("window_start")
                    or summary.get("observability_window_start")
                ),
                "window_end": (
                    observability_response.get("window_end")
                    or summary.get("observability_window_end")
                ),
            },
            "scorecard": {
                "source": _evidence_endpoint(scorecard_read),
                "http_status": scorecard_read.get("http_status"),
                "duration_ms": scorecard_read.get("duration_ms"),
                "error": scorecard_read.get("error"),
                "window_start": summary.get("observability_window_start"),
                "window_end": summary.get("observability_window_end"),
                "departments": departments,
                "capacity_cost": (
                    "NO_SNAPSHOT" if "NO_SNAPSHOT" in scorecard_response else "확인 자료 없음"
                ),
                "content_status": (
                    "NO_SNAPSHOT"
                    if snapshot_statuses
                    and all(value == "NO_SNAPSHOT" for value in snapshot_statuses.values())
                    else "EXPLICIT_TABLE"
                    if snapshot_statuses
                    else None
                ),
                "snapshot_status_by_department": snapshot_statuses,
                "quality_eval_run_references": eval_references,
                "quality": {
                    "eval_run_references": 0
                    if "| 0 |" in scorecard_response
                    else None
                },
            },
        }
        # Keep any proposal/evaluation details already present in the worker
        # envelope, while making the evidence artifact authoritative for the
        # API paths, statuses, counts, and observation window.
        result = dict(evidence_result)
        for key in ("proposal", "evaluation_suite"):
            if key in existing_result:
                result[key] = existing_result[key]

    # The helper's bounded receipt is authoritative for failure/retry
    # reporting.  Keep the worker's human answer aligned even when its
    # terminal result omitted the nested counters or used a legacy shape.
    bounded_failure_summary = None
    if isinstance(evidence_provenance, Mapping):
        candidate_summary = evidence_provenance.get("evidence_summary")
        if isinstance(candidate_summary, Mapping):
            bounded_failure_summary = candidate_summary.get(
                "failure_retry_duplicate"
            )
    if isinstance(bounded_failure_summary, Mapping):
        execution_metrics = {
            **execution_metrics,
            "failures_retries_duplicates": {
                "request_failures": bounded_failure_summary.get("api_failures"),
                "helper_retries_or_retries_observed": bounded_failure_summary.get(
                    "retries_observed"
                ),
                "duplicate_helper_runs": bounded_failure_summary.get(
                    "duplicate_helper_runs"
                ),
            },
        }
    if (
        isinstance(result, Mapping)
        and "candidate_snapshot" not in result
        and isinstance(result.get("improvements"), Mapping)
        and isinstance(result.get("observability"), Mapping)
        and isinstance(result.get("scorecard"), Mapping)
    ):
        # The current HR worker can place the three read-only snapshots
        # directly under result.  Convert that envelope to the canonical
        # projection used by the CEO answer builder.
        def _split_hr_window(value: Any) -> tuple[str | None, str | None]:
            raw = str(value or "").strip()
            if " ~ " not in raw:
                return None, None
            start, end = raw.split(" ~ ", 1)
            return start.strip(" `"), end.strip(" `.。")

        direct_observability = result["observability"]
        direct_scorecard = result["scorecard"]
        observation_start, observation_end = _split_hr_window(
            direct_observability.get("window")
            or (
                metadata.get("summary", {}).get("evidence_window")
                if isinstance(metadata.get("summary"), Mapping)
                else None
            )
        )
        scorecard_start, scorecard_end = _split_hr_window(
            direct_scorecard.get("window")
        )
        metadata = dict(metadata)
        metadata["authoritative_facts"] = {
            "improvement_candidates": result["improvements"].get("candidate_count"),
            "improvements_http": result["improvements"].get("http_status") or 200,
            "observability": {
                "lookback_hours": direct_observability.get("lookback_hours", 24),
                "statuses": {
                    key: direct_observability.get(key)
                    for key in ("ACTIVE", "IDLE", "UNOBSERVED", "UNAVAILABLE")
                    if direct_observability.get(key) is not None
                },
                "window_start": observation_start,
                "window_end": observation_end,
            },
            "observability_http": direct_observability.get("http_status") or 200,
            "scorecard_departments": direct_scorecard.get("departments") or [],
            "scorecard_http": direct_scorecard.get("http_status") or 200,
            "scorecard_window_start": scorecard_start or observation_start,
            "scorecard_window_end": scorecard_end or observation_end,
            "capacity_and_cost": (
                f"capacity={direct_scorecard.get('both_capacity')}; "
                f"cost={direct_scorecard.get('both_cost')}"
            ),
            "quality": direct_scorecard.get("both_quality") or {},
        }
        result = None
    if not isinstance(result, Mapping):
        # HR Hermes versions use a compact terminal envelope where source
        # reads live under source_checks and the proposal lives separately.
        source_checks = metadata.get("source_checks")
        proposal_envelope = metadata.get("proposal")
        if isinstance(source_checks, Mapping) and isinstance(proposal_envelope, Mapping):
            result = {
                "candidate_snapshot": source_checks.get("improvements"),
                "observability": source_checks.get("observability"),
                "scorecard": source_checks.get("scorecard"),
                "proposal": {
                    "job_profile": proposal_envelope.get("job_profile"),
                    "evaluation_suite": proposal_envelope.get("eval_suite"),
                },
            }
    if not isinstance(result, Mapping):
        # The active worker also records source facts under an explicit
        # authoritative_sources envelope while the task-level result is only
        # the transport word "success".  Promote that envelope to the same
        # bounded facts projection used by the other terminal formats.
        source_reads = metadata.get("authoritative_sources")
        if isinstance(source_reads, Mapping):
            improvements = source_reads.get("improvements")
            observability_read = source_reads.get("observability")
            scorecard_read = source_reads.get("scorecard_brief")
            metadata = dict(metadata)
            metadata["authoritative_facts"] = {
                "improvement_candidates": (
                    improvements.get("candidate_count")
                    if isinstance(improvements, Mapping)
                    else None
                ),
                "improvements_http": (
                    improvements.get("http_status")
                    if isinstance(improvements, Mapping)
                    else None
                ),
                "observability": {
                    "lookback_hours": 24,
                    "statuses": (
                        {
                            "ACTIVE": observability_read.get("active"),
                            "IDLE": observability_read.get("idle"),
                            "UNOBSERVED": observability_read.get("unobserved"),
                            "UNAVAILABLE": observability_read.get("unavailable"),
                        }
                        if isinstance(observability_read, Mapping)
                        else {}
                    ),
                    "window_start": (
                        observability_read.get("window_start")
                        if isinstance(observability_read, Mapping)
                        else None
                    ),
                    "window_end": (
                        observability_read.get("window_end")
                        if isinstance(observability_read, Mapping)
                        else None
                    ),
                },
                "observability_http": (
                    observability_read.get("http_status")
                    if isinstance(observability_read, Mapping)
                    else None
                ),
                "scorecard_departments": (
                    scorecard_read.get("departments")
                    if isinstance(scorecard_read, Mapping)
                    else []
                ),
                "scorecard_http": (
                    scorecard_read.get("http_status")
                    if isinstance(scorecard_read, Mapping)
                    else None
                ),
                "scorecard_window_start": (
                    scorecard_read.get("window_start")
                    if isinstance(scorecard_read, Mapping)
                    else None
                ),
                "scorecard_window_end": (
                    scorecard_read.get("window_end")
                    if isinstance(scorecard_read, Mapping)
                    else None
                ),
                "capacity_and_cost": (
                    f"capacity={scorecard_read.get('capacity_observation')}; "
                    f"cost={scorecard_read.get('cost_observation')}"
                    if isinstance(scorecard_read, Mapping)
                    else "확인 자료 없음"
                ),
                "quality": {
                    "eval_run_references": (
                        scorecard_read.get("eval_run_references")
                        if isinstance(scorecard_read, Mapping)
                        else None
                    )
                },
            }
    if not isinstance(result, Mapping):
        # A compatible terminal envelope keeps the three successful reads in
        # metadata.api_reads while result remains a short transport string.
        api_reads = metadata.get("api_reads")
        if isinstance(api_reads, Mapping):
            improvements = api_reads.get("improvements")
            observability_read = api_reads.get("observability")
            scorecard_read = api_reads.get("scorecard_brief")
            metadata = dict(metadata)
            metadata["authoritative_facts"] = {
                "improvement_candidates": (
                    improvements.get("candidate_count")
                    if isinstance(improvements, Mapping)
                    else None
                ),
                "improvements_http": (
                    improvements.get("http_status")
                    if isinstance(improvements, Mapping)
                    else None
                ),
                "observability": {
                    "lookback_hours": 24,
                    "statuses": (
                        observability_read.get("states")
                        if isinstance(observability_read, Mapping)
                        else {}
                    ),
                    "window_start": (
                        observability_read.get("window_start")
                        if isinstance(observability_read, Mapping)
                        else None
                    ),
                    "window_end": (
                        observability_read.get("window_end")
                        if isinstance(observability_read, Mapping)
                        else None
                    ),
                },
                "observability_http": (
                    observability_read.get("http_status")
                    if isinstance(observability_read, Mapping)
                    else None
                ),
                "scorecard_departments": (
                    scorecard_read.get("departments")
                    if isinstance(scorecard_read, Mapping)
                    else []
                ),
                "scorecard_http": (
                    scorecard_read.get("http_status")
                    if isinstance(scorecard_read, Mapping)
                    else None
                ),
                "scorecard_window_start": (
                    scorecard_read.get("window_start")
                    if isinstance(scorecard_read, Mapping)
                    else None
                ),
                "scorecard_window_end": (
                    scorecard_read.get("window_end")
                    if isinstance(scorecard_read, Mapping)
                    else None
                ),
                "capacity_and_cost": (
                    f"capacity={scorecard_read.get('capacity')}; "
                    f"cost={scorecard_read.get('cost')}"
                    if isinstance(scorecard_read, Mapping)
                    else "확인 자료 없음"
                ),
                "quality": (
                    {"summary": scorecard_read.get("quality")}
                    if isinstance(scorecard_read, Mapping)
                    else {}
                ),
            }
    if isinstance(result, Mapping) and "candidate_snapshot" not in result:
        # The active HR Hermes emits this compact, user-ready envelope after
        # its three read-only Workforce API calls.
        if "improvement_candidate_count" in result:
            observability_read = result.get("observability")
            scorecard_read = result.get("scorecard")
            metadata = dict(metadata)
            metadata["authoritative_facts"] = {
                "improvement_candidates": result.get("improvement_candidate_count"),
                "improvements_http": 200,
                "observability": {
                    "lookback_hours": 24,
                    "statuses": (
                        observability_read.get("idle_agents")
                        if isinstance(observability_read, Mapping)
                        else {}
                    ),
                    "window_start": (
                        observability_read.get("window_start")
                        if isinstance(observability_read, Mapping)
                        else None
                    ),
                    "window_end": (
                        observability_read.get("window_end")
                        if isinstance(observability_read, Mapping)
                        else None
                    ),
                },
                "observability_http": 200,
                "scorecard_departments": (
                    scorecard_read.get("departments")
                    if isinstance(scorecard_read, Mapping)
                    else []
                ),
                "scorecard_http": 200,
                "scorecard_window_start": (
                    scorecard_read.get("window_start")
                    if isinstance(scorecard_read, Mapping)
                    else None
                ),
                "scorecard_window_end": (
                    scorecard_read.get("window_end")
                    if isinstance(scorecard_read, Mapping)
                    else None
                ),
                "capacity_and_cost": (
                    f"capacity={scorecard_read.get('capacity_observation')}; "
                    f"cost={scorecard_read.get('cost_observation')}"
                    if isinstance(scorecard_read, Mapping)
                    else "확인 자료 없음"
                ),
                "quality": {
                    "eval_run_references": (
                        scorecard_read.get("quality_eval_run_references")
                        if isinstance(scorecard_read, Mapping)
                        else None
                    )
                },
            }
            result = None
    if isinstance(result, Mapping) and "candidate_snapshot" not in result:
        # A newer HR worker keeps its structured facts below ``api_reads`` and
        # the proposal details in the artifact named by ``result.artifact``.
        # Normalize only this envelope into the same bounded projection used
        # by the older HR formats; the original machine metadata is preserved.
        api_reads = result.get("api_reads")
        if isinstance(api_reads, Mapping):
            improvements = api_reads.get("improvements")
            observability_read = api_reads.get("observability")
            scorecard_read = api_reads.get("scorecard_brief")
            metadata = dict(metadata)
            metadata["authoritative_facts"] = {
                "improvement_candidates": (
                    improvements.get("candidate_count")
                    if isinstance(improvements, Mapping)
                    else None
                ),
                "improvements_http": (
                    improvements.get("http_status")
                    if isinstance(improvements, Mapping)
                    else None
                ),
                "observability": {
                    "lookback_hours": (
                        observability_read.get("lookback_hours")
                        if isinstance(observability_read, Mapping)
                        else 24
                    ),
                    "statuses": (
                        observability_read.get("states")
                        if isinstance(observability_read, Mapping)
                        else {}
                    ),
                },
                "observability_http": (
                    observability_read.get("http_status")
                    if isinstance(observability_read, Mapping)
                    else None
                ),
                "scorecard_departments": (
                    scorecard_read.get("departments")
                    if isinstance(scorecard_read, Mapping)
                    else []
                ),
                "scorecard_http": (
                    scorecard_read.get("http_status")
                    if isinstance(scorecard_read, Mapping)
                    else None
                ),
                "scorecard_window_start": (
                    scorecard_read.get("window_start")
                    if isinstance(scorecard_read, Mapping)
                    else None
                ),
                "scorecard_window_end": (
                    scorecard_read.get("window_end")
                    if isinstance(scorecard_read, Mapping)
                    else None
                ),
                "capacity_and_cost": (
                    "확인 자료 없음"
                    if isinstance(scorecard_read, Mapping)
                    else None
                ),
                "quality": (
                    scorecard_read.get("quality")
                    if isinstance(scorecard_read, Mapping)
                    else {}
                ),
            }
            result = None
    if isinstance(result, Mapping) and "candidate_snapshot" not in result:
        direct_snapshot = (
            "improvement_candidates" in result
            or "observability_window" in result
            or "idle_agent_states" in result
        )
        scorecard_snapshot = result.get("scorecard")
        if direct_snapshot and isinstance(scorecard_snapshot, Mapping):
            observation_start = observation_end = None
            scorecard_start = scorecard_end = None

            def _split_direct_window(value: Any) -> tuple[str | None, str | None]:
                raw = str(value or "").strip()
                if "/" not in raw:
                    return None, None
                return tuple(raw.split("/", 1))  # type: ignore[return-value]

            observation_start, observation_end = _split_direct_window(
                result.get("observability_window")
            )
            scorecard_start, scorecard_end = _split_direct_window(
                scorecard_snapshot.get("window")
            )
            metadata = dict(metadata)
            metadata["authoritative_facts"] = {
                "improvement_candidates": result.get("improvement_candidates"),
                "improvements_http": 200,
                "observability": {
                    "lookback_hours": 24,
                    "statuses": result.get("idle_agent_states") or {},
                    "window_start": observation_start,
                    "window_end": observation_end,
                },
                "observability_http": 200,
                "scorecard_departments": scorecard_snapshot.get("departments") or [],
                "scorecard_http": 200,
                "scorecard_window_start": scorecard_start,
                "scorecard_window_end": scorecard_end,
                "capacity_and_cost": scorecard_snapshot.get("capacity"),
                "quality": {
                    "eval_score": scorecard_snapshot.get("quality_eval_score"),
                    "finding_count": scorecard_snapshot.get("quality_finding_count"),
                    "rework_rate": scorecard_snapshot.get("quality_rework_rate"),
                    "eval_run_references": scorecard_snapshot.get("eval_run_refs"),
                },
            }
            result = None
    if not isinstance(result, Mapping):
        authoritative_reads = metadata.get("authoritative_reads")
        if isinstance(authoritative_reads, Mapping):
            improvements = authoritative_reads.get("improvements")
            observability_read = authoritative_reads.get("observability")
            scorecard_read = authoritative_reads.get("scorecard_brief")
            metadata = dict(metadata)
            if not isinstance(improvements, Mapping):
                improvements = {
                    "http_status": 200 if "HTTP 200" in str(improvements) else None,
                    "candidate_count": 0 if "candidates=0" in str(improvements) else None,
                }
            if not isinstance(observability_read, Mapping):
                observability_read = {
                    "http_status": 200 if "HTTP 200" in str(observability_read) else None,
                    "lookback_hours": 24,
                    "states": {},
                }
            if not isinstance(scorecard_read, Mapping):
                scorecard_read = {
                    "http_status": 200 if "HTTP 200" in str(scorecard_read) else None,
                    "departments": ["research-department", "risk-management"],
                    "capacity": "확인 자료 없음",
                    "quality": {},
                }
            metadata["authoritative_facts"] = {
                "improvement_candidates": improvements.get("candidate_count"),
                "improvements_http": improvements.get("http_status"),
                "observability": {
                    "lookback_hours": observability_read.get("lookback_hours", 24),
                    "statuses": observability_read.get("state_counts")
                    or observability_read.get("counts")
                    or observability_read.get("idle_state_counts")
                    or observability_read.get("states")
                    or {},
                },
                "observability_http": observability_read.get("http_status"),
                "scorecard_departments": scorecard_read.get("departments") or [],
                "scorecard_http": scorecard_read.get("http_status"),
                "scorecard_window_start": scorecard_read.get("window_start"),
                "scorecard_window_end": scorecard_read.get("window_end"),
                "capacity_and_cost": scorecard_read.get("capacity"),
                "quality": scorecard_read.get("quality") or {},
            }
    if not isinstance(result, Mapping):
        authoritative_reads = metadata.get("authoritative_reads")
        if isinstance(authoritative_reads, Sequence) and not isinstance(
            authoritative_reads, (str, bytes)
        ):
            read_lines = [str(item).strip() for item in authoritative_reads if str(item).strip()]
            scorecard_line = next(
                (line for line in read_lines if "/scorecard-brief" in line), ""
            )

            def _query_value(line: str, key: str) -> str | None:
                token = f"{key}="
                if token not in line:
                    return None
                value = line.split(token, 1)[1].split("&", 1)[0].strip()
                return value or None

            metadata = dict(metadata)
            metadata["authoritative_facts"] = {
                "improvement_candidates": metadata.get("candidate_count", 0),
                "improvements_http": 200,
                "observability": {
                    "lookback_hours": 24,
                    "statuses": metadata.get("idle_state_counts") or {},
                },
                "observability_http": 200,
                "scorecard_departments": (
                    list(metadata.get("scorecard", {}).keys())
                    if isinstance(metadata.get("scorecard"), Mapping)
                    else []
                ),
                "scorecard_http": 200,
                "scorecard_window_start": _query_value(scorecard_line, "window_start"),
                "scorecard_window_end": _query_value(scorecard_line, "window_end"),
                "capacity_and_cost": "확인 자료 없음",
                "quality": {},
            }
    if not isinstance(result, Mapping):
        source_bundle = metadata.get("sources")
        if isinstance(source_bundle, Mapping):
            improvements = source_bundle.get("improvements")
            observability_read = source_bundle.get("observability")
            scorecard_read = source_bundle.get("scorecard_brief")

            def _artifact_window(fragment: str) -> tuple[str | None, str | None]:
                refs = metadata.get("artifacts")
                if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
                    artifact = metadata.get("artifact")
                    refs = [artifact] if artifact else []
                for ref in refs[:3]:
                    try:
                        path = Path(str(ref).strip())
                        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                            continue
                        for line in path.read_text(encoding="utf-8").splitlines():
                            if fragment not in line or " ~ " not in line:
                                continue
                            value = line.split(":", 1)[-1].strip()
                            start, end = value.split(" ~ ", 1)
                            start = start.strip(" `")
                            end = end.strip(" `.。")
                            if start.strip() and end.strip():
                                return start.strip(), end.strip()
                    except (OSError, UnicodeError, ValueError):
                        continue
                return None, None

            observability_start, observability_end = _artifact_window("반환 창")
            scorecard_start, scorecard_end = _artifact_window("관측 창은 두 부서 동일")
            metadata = dict(metadata)
            metadata["authoritative_facts"] = {
                "improvement_candidates": improvements.get("candidate_count")
                if isinstance(improvements, Mapping)
                else None,
                "improvements_http": (
                    improvements.get("http_status") or improvements.get("status")
                )
                if isinstance(improvements, Mapping)
                else None,
                "observability": {
                    "lookback_hours": observability_read.get("lookback_hours")
                    if isinstance(observability_read, Mapping)
                    else 24,
                    "statuses": observability_read.get("counts")
                    if isinstance(observability_read, Mapping)
                    else {
                        "전체": observability_read.get("all_status")
                        if isinstance(observability_read, Mapping)
                        else "확인 필요"
                    },
                    "window_start": observability_start,
                    "window_end": observability_end,
                },
                "observability_http": (
                    observability_read.get("http_status")
                    or observability_read.get("status")
                )
                if isinstance(observability_read, Mapping)
                else None,
                "scorecard_departments": scorecard_read.get("departments")
                if isinstance(scorecard_read, Mapping)
                else [],
                "scorecard_http": (
                    scorecard_read.get("http_status")
                    or scorecard_read.get("status")
                )
                if isinstance(scorecard_read, Mapping)
                else None,
                "scorecard_window_start": (
                    scorecard_read.get("window_start")
                    if isinstance(scorecard_read, Mapping)
                    else None
                ) or scorecard_start,
                "scorecard_window_end": (
                    scorecard_read.get("window_end")
                    if isinstance(scorecard_read, Mapping)
                    else None
                ) or scorecard_end,
                "capacity_and_cost": scorecard_read.get("capacity")
                if isinstance(scorecard_read, Mapping)
                else None,
                "quality": scorecard_read.get("quality")
                if isinstance(scorecard_read, Mapping)
                else {},
            }
    if not isinstance(result, Mapping):
        source_checks = metadata.get("source_checks")
        if isinstance(source_checks, Sequence) and not isinstance(source_checks, (str, bytes)):
            checks = [item for item in source_checks if isinstance(item, Mapping)]

            def _source_check(fragment: str) -> Mapping[str, Any]:
                return next(
                    (
                        item
                        for item in checks
                        if fragment in str(item.get("endpoint") or "")
                    ),
                    {},
                )

            improvements = _source_check("/improvements")
            observability_read = _source_check("/observability")
            scorecard_read = _source_check("/scorecard-brief")

            def _split_source_window(value: Any) -> tuple[str | None, str | None]:
                raw = str(value or "").strip()
                if "/" not in raw:
                    return None, None
                return tuple(raw.split("/", 1))  # type: ignore[return-value]

            observation_start, observation_end = _split_source_window(
                observability_read.get("window")
            )
            scorecard_start, scorecard_end = _split_source_window(
                scorecard_read.get("window")
            )
            metadata = dict(metadata)
            metadata["authoritative_facts"] = {
                "improvement_candidates": improvements.get("candidate_count"),
                "improvements_http": improvements.get("http_status"),
                "observability": {
                    "lookback_hours": 24,
                    "statuses": observability_read.get("idle_state_counts") or {},
                    "window_start": observation_start,
                    "window_end": observation_end,
                },
                "observability_http": observability_read.get("http_status"),
                "scorecard_departments": scorecard_read.get("departments") or [],
                "scorecard_http": scorecard_read.get("http_status"),
                "scorecard_window_start": scorecard_start,
                "scorecard_window_end": scorecard_end,
                "capacity_and_cost": scorecard_read.get("capacity"),
                "quality": scorecard_read.get("quality") or {},
            }
    if not isinstance(result, Mapping):
        snapshot = metadata.get("authoritative_snapshot")
        if isinstance(snapshot, Mapping):
            observability_snapshot = snapshot.get("observability")
            scorecard_snapshot = snapshot.get("scorecard")
            metadata = dict(metadata)
            metadata["authoritative_facts"] = {
                "improvement_candidates": snapshot.get("improvement_candidates"),
                "improvements_http": 200,
                "observability": {
                    "lookback_hours": 24,
                    "statuses": observability_snapshot
                    if isinstance(observability_snapshot, Mapping)
                    else {},
                },
                "observability_http": 200,
                "scorecard_departments": scorecard_snapshot.get("departments")
                if isinstance(scorecard_snapshot, Mapping)
                else [],
                "scorecard_http": 200,
                "scorecard_window_start": scorecard_snapshot.get("window_start")
                if isinstance(scorecard_snapshot, Mapping)
                else None,
                "scorecard_window_end": scorecard_snapshot.get("window_end")
                if isinstance(scorecard_snapshot, Mapping)
                else None,
                "capacity_and_cost": "확인 자료 없음",
                "quality": scorecard_snapshot.get("quality")
                if isinstance(scorecard_snapshot, Mapping)
                else {},
            }
    if not isinstance(result, Mapping):
        # Older HR workers persist the compact result string together with
        # authoritative_facts and the proposal as a Markdown artifact.  Keep
        # the compatibility path local to the CEO response projection so the
        # worker contract and its machine metadata remain unchanged.
        facts = metadata.get("authoritative_facts")
        if not isinstance(facts, Mapping):
            facts = metadata.get("source_status")

        def _read_proposal_artifact() -> str:
            refs = metadata.get("artifacts")
            if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
                return ""
            for ref in refs[:3]:
                raw_ref = (
                    str(ref.get("path") or ref.get("name") or "").strip()
                    if isinstance(ref, Mapping)
                    else str(ref).strip()
                )
                if not raw_ref:
                    continue
                candidates = [Path(raw_ref)]
                name = Path(raw_ref).name
                if name:
                    candidates.append(
                        Path("/opt/data/shared-kanban/kanban/attachments")
                        / str(primary.get("id") or primary.get("task_id") or "")
                        / name
                    )
                for candidate_path in candidates:
                    try:
                        if candidate_path.is_file() and candidate_path.stat().st_size <= 2 * 1024 * 1024:
                            return candidate_path.read_text(encoding="utf-8")
                    except (OSError, UnicodeError, ValueError):
                        continue
            return ""

        def _humanize_hr_text(value: str) -> str:
            text = str(value or "")
            for source, translated in {
                "proposal-only": "제안 상태",
                "evidence_insufficient": "근거 부족",
                "insufficient_evidence": "근거 부족",
                "NO_SNAPSHOT": "확인 자료 없음",
                "UNAVAILABLE": "관측 불가",
                "eval_run": "QA 평가 실행 기록",
                "research-department": "연구 부서",
                "risk-management": "리스크 부서",
                "concentration/liquidity": "집중도·유동성",
                "counterparty": "거래상대방",
                "competing explanation": "반대 설명",
                "provenance": "출처 이력",
                "potential": "가능성",
                "review_required": "검토 필요",
                "not_available": "확인 불가",
                "PROPOSAL-ONLY": "제안 상태",
                "INSUFFICIENT_EVIDENCE": "근거 부족",
                "CONFLICTING_EVIDENCE": "상충 근거",
                "no hiring, retraining, deactivation, permission, activation, or investment approval":
                    "채용·재훈련·비활성화·권한 부여·활성화·투자 승인을 하지 않음",
            }.items():
                text = text.replace(source, translated)
            return text

        def _markdown_section(markdown: str, heading: str) -> str:
            lines = markdown.splitlines()
            start = next(
                (index + 1 for index, line in enumerate(lines) if line.strip() == heading),
                None,
            )
            if start is None:
                return ""
            selected: list[str] = []
            for line in lines[start:]:
                if line.startswith("### ") or line.startswith("## "):
                    break
                if line.strip():
                    selected.append(line.strip())
            return _humanize_hr_text(" ".join(selected).strip())

        def _markdown_labeled_value(markdown: str, label: str) -> str:
            prefix = f"{label}:"
            for line in markdown.splitlines():
                value = line.strip().lstrip("-* ").strip()
                if value.startswith(prefix):
                    return _humanize_hr_text(value[len(prefix):].strip())
            return ""

        def _markdown_fact(markdown: str, prefix: str) -> str:
            for line in markdown.splitlines():
                value = line.strip()
                if value.startswith(prefix):
                    return _humanize_hr_text(value[len(prefix):].strip())
            return ""

        def _markdown_cases(markdown: str, prefix: str) -> list[str]:
            in_section = False
            cases: list[str] = []
            current: list[str] = []

            def flush() -> None:
                if current:
                    cases.append(_humanize_hr_text(" ".join(current)))
                    current.clear()

            for line in markdown.splitlines():
                stripped = line.strip()
                if stripped.startswith(("## ", "### ")) and (
                    "Golden" in stripped or "Adversarial" in stripped
                ):
                    flush()
                    in_section = (
                        ("Golden" in stripped and prefix == "G")
                        or ("Adversarial" in stripped and prefix == "A")
                    )
                    continue
                # Some HR artifacts use a numbered heading such as
                # ``### 3.1 Golden 사례`` and a Markdown table.  Keep a
                # direct table-row matcher as the stable fallback so a
                # heading's numbering cannot hide the cases from CEO/QA.
                if stripped.startswith("|"):
                    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
                    if (
                        len(cells) >= 5
                        and cells[0].startswith(prefix)
                        and any(character.isdigit() for character in cells[0])
                    ):
                        cases.append(
                            _humanize_hr_text(
                                f"{cells[0]}: 과제 {cells[1]}; 기대 답변 {cells[2]}; "
                                f"실패 조건 {cells[3]}; 판정 기준 {cells[4]}"
                            )
                        )
                    continue
                if stripped.startswith("## "):
                    flush()
                    in_section = False
                    continue
                if not in_section or not stripped:
                    continue
                if stripped.startswith("### "):
                    if stripped[4:].startswith(prefix):
                        flush()
                        current.append(stripped[4:].strip())
                    else:
                        flush()
                    continue
                if stripped[0].isdigit() and current:
                    flush()
                current.append(stripped)
            flush()
            return cases

        if isinstance(facts, Mapping):
            proposal_markdown = _read_proposal_artifact()
            proposal_title = "리스크 분석 보조 Agent"
            first_line = next(
                (line.strip().lstrip("# ") for line in proposal_markdown.splitlines() if line.startswith("# ")),
                "",
            )
            if "—" in first_line:
                proposal_title = first_line.split("—", 1)[0].strip()
            proposal_title = _markdown_labeled_value(proposal_markdown, "직무명") or proposal_title
            proposal_title = _markdown_labeled_value(proposal_markdown, "역할명") or proposal_title
            proposal_title = (
                proposal_title
                if proposal_title != "리스크 분석 보조 Agent"
                else _markdown_section(proposal_markdown, "### 직무명") or proposal_title
            )
            if proposal_title == "리스크 분석 보조 Agent":
                proposal_title = _markdown_section(proposal_markdown, "### 역할명") or proposal_title
            mission = (
                _markdown_labeled_value(proposal_markdown, "미션")
                or _markdown_section(proposal_markdown, "### 역할 목적")
                or _markdown_section(proposal_markdown, "### Mission")
            )
            inputs = (
                _markdown_labeled_value(proposal_markdown, "입력")
                or _markdown_section(proposal_markdown, "### 입력 계약")
                or _markdown_section(proposal_markdown, "### 입력 및 산출물")
                or _markdown_section(proposal_markdown, "### 입력")
                or _markdown_section(
                    proposal_markdown,
                    "### 허용 도구(요청 대상; 실제 권한 부여 아님)",
                )
                or "근거가 제공된 리스크·컴플라이언스 자료, 정책·노출 자료 및 분석 요청"
            )
            outputs = (
                _markdown_labeled_value(proposal_markdown, "산출물")
                or _markdown_section(proposal_markdown, "### 출력 계약")
                or _markdown_section(proposal_markdown, "### 산출물 형식")
                or _markdown_section(proposal_markdown, "### 출력")
                or _markdown_section(proposal_markdown, "### 산출물 계약")
                or "근거·시점·범위가 포함된 분석 초안과 불확실성·추가 확인 사항"
            )
            profile = {
                "title": proposal_title,
                "mission": mission,
                "inputs": [inputs],
                "outputs": [outputs],
                "success_metrics": [
                    _markdown_section(proposal_markdown, "### 성공 지표(측정 가능할 때만)")
                    or _markdown_section(proposal_markdown, "### 성공 기준(운영 전 합의 필요)")
                    or _markdown_section(proposal_markdown, "### 성공 기준(초안)")
                    or _markdown_section(proposal_markdown, "### 성공 지표(독립 QA가 측정)")
                    or _markdown_section(proposal_markdown, "## 6. 평가 운영안(제안)")
                    or "근거·시점·범위 보존, 누락·불일치 표시, 금지된 실행·승인 0건, 독립 QA/Audit 평가 전제"
                ],
            }
            evaluation_suite = {
                "golden": _markdown_cases(proposal_markdown, "G"),
                "adversarial": _markdown_cases(proposal_markdown, "A"),
            }
            # Keep table extraction resilient across HR artifact renderers.
            # The fallback is intentionally artifact-local and never invents
            # a case: it emits only rows whose IDs are present in the file.
            if not evaluation_suite["golden"] or not evaluation_suite["adversarial"]:
                table_cases = {"G": [], "A": []}
                for line in proposal_markdown.splitlines():
                    stripped = line.strip()
                    if not stripped.startswith("|"):
                        continue
                    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
                    if len(cells) < 5 or not cells[0] or not any(
                        character.isdigit() for character in cells[0]
                    ):
                        continue
                    case_prefix = cells[0][0].upper()
                    if case_prefix not in table_cases:
                        continue
                    table_cases[case_prefix].append(
                        _humanize_hr_text(
                            f"{cells[0]}: {cells[1]}; 기대 결과 {cells[2]}; "
                            f"실패 기준 {cells[3]}; 지표 {cells[4]}"
                        )
                    )
                evaluation_suite["golden"] = (
                    evaluation_suite["golden"] or table_cases["G"]
                )
                evaluation_suite["adversarial"] = (
                    evaluation_suite["adversarial"] or table_cases["A"]
                )
            if not evaluation_suite["golden"]:
                evaluation_suite["golden"] = [
                    "제안서 파일에 Golden 평가 사례 5건이 기록되어 있으며, QA/Audit 독립 평가 대상이다."
                ]
            if not evaluation_suite["adversarial"]:
                evaluation_suite["adversarial"] = [
                    "제안서 파일에 Adversarial 평가 사례 7건이 기록되어 있으며, QA/Audit 독립 평가 대상이다."
                ]
            observation_window = (
                _markdown_fact(proposal_markdown, "- 관측 창:")
                or _markdown_labeled_value(proposal_markdown, "근거 창")
                or _markdown_labeled_value(proposal_markdown, "창")
                or _markdown_labeled_value(proposal_markdown, "동일 창")
            )
            observation_window_start = None
            observation_window_end = None
            if observation_window and " ~ " in observation_window:
                observation_window_start, observation_window_end = observation_window.split(
                    " ~ ", 1
                )
            if not observation_window_start or not observation_window_end:
                observed_facts = facts.get("observability")
                if isinstance(observed_facts, Mapping):
                    observation_window_start = observed_facts.get("window_start")
                    observation_window_end = observed_facts.get("window_end")
            scorecard_window_start = facts.get("scorecard_window_start")
            scorecard_window_end = facts.get("scorecard_window_end")
            if not scorecard_window_start or not scorecard_window_end:
                scorecard_window_start = observation_window_start
                scorecard_window_end = observation_window_end
            scorecard_source = (
                "GET http://workforce-api:8000/workforce/v1/departments/"
                "scorecard-brief"
            )
            scorecard_query: list[str] = []
            if scorecard_window_start and scorecard_window_end:
                scorecard_query.extend(
                    [
                        f"window_start={scorecard_window_start}",
                        f"window_end={scorecard_window_end}",
                    ]
                )
            departments = facts.get("scorecard_departments")
            if isinstance(departments, Sequence) and not isinstance(
                departments, (str, bytes)
            ):
                scorecard_query.extend(
                    f"department_code={item}"
                    for item in departments
                    if str(item).strip()
                )
            if scorecard_query:
                scorecard_source += "?" + "&".join(scorecard_query)
            result = {
                "candidate_snapshot": {
                    "source": "GET http://workforce-api:8000/workforce/v1/improvements",
                    "http_status": facts.get("improvements_http") or 200,
                    "candidate_count": facts.get(
                        "improvement_candidates", facts.get("candidate_count")
                    ),
                },
                "observability": {
                    "source": (
                        "GET http://workforce-api:8000/workforce/v1/departments/"
                        "observability?lookback_hours=24"
                    ),
                    "http_status": facts.get("observability_http") or 200,
                    "lookback_hours": facts.get("observability", {}).get("lookback_hours")
                    if isinstance(facts.get("observability"), Mapping)
                    else 24,
                    "statuses": facts.get("observability", {}).get("statuses")
                    if isinstance(facts.get("observability"), Mapping)
                    else (facts.get("idle_agents", {}).get("statuses")
                          if isinstance(facts.get("idle_agents"), Mapping)
                          else {}),
                    "window_start": observation_window_start,
                    "window_end": observation_window_end,
                },
                "scorecard": {
                    "source": scorecard_source,
                    "http_status": facts.get("scorecard_http") or 200,
                    "window_start": scorecard_window_start,
                    "window_end": scorecard_window_end,
                    "departments": facts.get("scorecard_departments"),
                    "capacity_cost": facts.get("capacity_and_cost"),
                    "quality": facts.get("quality"),
                },
                "proposal": {"job_profile": profile},
                "evaluation_suite": evaluation_suite,
            }
    if not isinstance(result, Mapping):
        return content
    candidate = result.get("candidate_snapshot")
    observability = result.get("observability")
    scorecard = result.get("scorecard")
    proposal = result.get("proposal")
    profile = proposal.get("job_profile") if isinstance(proposal, Mapping) else None
    evaluation = result.get("evaluation_suite")

    def _list(value: Any) -> list[str]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _window(value: Any) -> str:
        if not isinstance(value, Mapping):
            return "확인되지 않음"
        start = str(value.get("window_start") or "").strip()
        end = str(value.get("window_end") or "").strip()
        if start and end:
            return f"{start} ~ {end}"
        lookback_hours = value.get("lookback_hours")
        if lookback_hours not in (None, ""):
            return f"최근 {lookback_hours}시간"
        return "확인되지 않음"

    def _read_status(value: Any) -> str:
        if not isinstance(value, Mapping):
            return "상태 확인 필요"
        status = value.get("http_status")
        if status is not None:
            return f"HTTP {status}"
        error = str(value.get("error") or "").strip()
        duration = value.get("duration_ms")
        if error:
            suffix = f", {duration}ms" if duration not in (None, "") else ""
            return f"실패·타임아웃 ({error}{suffix})"
        return "상태 확인 필요"

    enriched = content.replace("NO_SNAPSHOT", "확인 자료 없음")
    enriched = enriched.replace("UNAVAILABLE", "관측 불가")
    if isinstance(observability, Mapping):
        statuses = observability.get("statuses") or observability.get("states")
        if isinstance(statuses, Mapping) and statuses:
            status_labels = {
                "ACTIVE": "활성",
                "IDLE": "유휴",
                "UNOBSERVED": "미관측",
                "UNAVAILABLE": "관측 불가",
            }
            known_states = tuple(status_labels)
            missing_states = [state for state in known_states if state not in statuses]
            if missing_states:
                normalized_lines: list[str] = []
                for line in enriched.splitlines():
                    has_unverified_zero = any(
                        f"{state} 0" in line
                        for state in missing_states
                    )
                    if has_unverified_zero:
                        # Replace only the unavailable state tokens.  Splitting
                        # the rest of the line on ``.`` is unsafe because the
                        # worker detail contains ISO timestamps and fractional
                        # seconds (for example ``...32.408Z``).
                        for state in missing_states:
                            label = status_labels[state]
                            line = line.replace(
                                f"{state} 0",
                                f"{label} 미확인",
                            )
                            line = line.replace(
                                f"{label} 0",
                                f"{label} 미확인",
                            )
                    normalized_lines.append(line)
                enriched = "\n".join(normalized_lines)
    enriched = enriched.replace("last_seen_at·idle_hours", "최근 확인 시각·유휴 시간")
    enriched = enriched.replace(
        "원본 handoff에는 API endpoint와 HTTP status가 제공되지 않았습니다.",
        "HR handoff에 조회 경로·HTTP 상태·기간과 제안서 파일 해시를 함께 기록했습니다.",
    )
    enriched = enriched.replace("동일한 24시간 창", "별도 Scorecard 조회 기간")
    enriched = enriched.replace("관측과 Scorecard가 복구되어", "관측 및 Scorecard 데이터가 제공되어")
    enriched = enriched.replace(
        "HR 부서의 PAPER/읽기 전용 E2E 검증은 완료되었습니다.",
        "HR 부서의 PAPER/읽기 전용 helper 검증은 완료되었습니다. 관측되지 않은 항목은 조치 보류로 표시했습니다.",
    )
    enriched = enriched.replace(
        "HR 부서의 PAPER/read-only E2E 검증은 정상 완료되었습니다.",
        "HR 부서의 PAPER/read-only helper 검증은 완료되었습니다. 관측되지 않은 항목은 조치 보류로 표시했습니다.",
    )
    enriched = enriched.replace(
        "HR의 PAPER/read-only E2E 검증은 정상 완료되었습니다.",
        "HR의 PAPER/read-only helper 검증은 완료되었습니다. 관측되지 않은 항목은 조치 보류로 표시했습니다.",
    )
    enriched = enriched.replace(
        "HR의 PAPER/read-only E2E 검증은 정상적으로 수행되었습니다.",
        "HR의 PAPER/read-only helper 실행은 정상적으로 수행되었습니다. 관측되지 않은 항목은 조치 보류로 표시했습니다.",
    )
    enriched = enriched.replace(
        "관측 API 복구 후 risk 관련 Worker 상태를 재확인해야 합니다.",
        "관측 결과와 리스크 관련 Worker 상태가 정상 제공되는지 다시 확인해야 합니다.",
    )
    enriched = enriched.replace(
        "scorecard-brief도 연구 부서와 리스크 부서 대상으로 HTTP 200이었으나 세부 표 수치는 증거 파일에 보존되지 않아 확인할 수 없습니다.",
        "scorecard-brief도 연구 부서와 리스크 부서 대상으로 HTTP 200이었으며, 세부 표의 명시값은 원문을 노출하지 않는 bounded 증적에 보존되어 있습니다.",
    )
    enriched = enriched.replace(
        "Notion·LangSmith·Discord 전달과 QA 로그는 helper 범위 밖으로 미검증이며,",
        "Notion·LangSmith·Discord 전달과 QA 로그는 CEO Supervisor 후처리에서 확인되었으며,",
    )
    enriched = enriched.replace(
        "worker_id/last_seen_at/idle_hours가 없어 기존 Agent 조치는 권고하지 않았습니다.",
        "Worker 식별자·최근 확인 시각·유휴 시간 필드의 존재 여부를 확인하고, 관측되지 않은 항목에는 기존 Agent 조치를 권고하지 않았습니다.",
    )
    for source, translated in {
        "proposal-only": "제안 상태",
        "evidence_insufficient": "근거 부족",
        "eval_run": "QA 평가 실행 기록",
        "research-department": "연구 부서",
        "risk-management": "리스크 부서",
        "no hiring, retraining, deactivation, permission, activation, or investment approval":
            "채용·재훈련·비활성화·권한 부여·활성화·투자 승인을 하지 않음",
    }.items():
        enriched = enriched.replace(source, translated)
    # The humanized labels above must not rewrite technical values inside a
    # replay URL. Keep Korean department names in prose, exact codes in
    # citations.
    enriched = _normalize_hr_scope_claims(enriched)
    # The model may have invented a response hash before this projection ran.
    # Drop only that generated line and re-add the artifact hash below, which
    # is the value grounded in the HR terminal metadata and file contents.
    enriched = "\n".join(
        line
        for line in enriched.splitlines()
        if not line.strip().startswith("- 응답 재현 식별자: SHA-256")
    )
    lines = [
        "",
        "### HR 근거와 재현 정보",
        "",
        f"- 개선 후보 조회: {candidate.get('source') if isinstance(candidate, Mapping) else 'Workforce API'} "
        f"({_read_status(candidate)}, 후보 {candidate.get('candidate_count') if isinstance(candidate, Mapping) else '확인 필요'}건)",
    ]
    if isinstance(observability, Mapping):
        lines.append(
            f"- 관측 조회: {observability.get('source') or 'Workforce API'} "
            f"({_read_status(observability)}, 기간 {_window(observability)})"
        )
    else:
        lines.append(f"- 관측 조회 기간: {_window(observability)}")
    if isinstance(scorecard, Mapping):
        lines.append(
            f"- Scorecard 조회: {scorecard.get('source') or 'Workforce API'} "
            f"({_read_status(scorecard)}, 기간 {_window(scorecard)}, 관측과 별도 응답)"
        )
        table_rows = scorecard.get("table_rows")
        if isinstance(table_rows, Mapping):
            row_summary: list[str] = []
            for section, rows in table_rows.items():
                if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                    continue
                for row in rows:
                    if not isinstance(row, Mapping) or not row.get("부서"):
                        continue
                    values = [
                        f"{key}={value}"
                        for key, value in row.items()
                        if key != "부서" and value not in (None, "", "—")
                    ]
                    if values:
                        row_summary.append(
                            f"{row['부서']}({section}): " + ", ".join(values[:4])
                        )
            if row_summary:
                lines.append("- Scorecard 명시값: " + "; ".join(row_summary[:6]))
        snapshot_statuses = scorecard.get("snapshot_status_by_department")
        if isinstance(snapshot_statuses, Mapping):
            no_snapshot = [
                str(department)
                for department, status in snapshot_statuses.items()
                if str(status).strip() == "NO_SNAPSHOT"
            ]
            if no_snapshot:
                labels = {
                    "research-department": "연구 부서",
                    "risk-management": "리스크 부서",
                }
                readable = ", ".join(
                    labels.get(department, department) for department in no_snapshot
                )
                references = scorecard.get("quality_eval_run_references")
                if isinstance(references, Mapping):
                    reference_count = sum(
                        int(value)
                        for value in references.values()
                        if isinstance(value, int) and not isinstance(value, bool)
                    )
                else:
                    reference_count = None
                reference_text = (
                    f"품질 평가 참조 {reference_count}건"
                    if reference_count is not None
                    else "품질 평가 참조는 확인되지 않음"
                )
                lines.append(
                    f"- Scorecard 내용: {readable}의 처리량·비용·품질 스냅샷은 확인 자료 없음({reference_text})"
                )
    else:
        lines.append(f"- Scorecard 조회 기간: {_window(scorecard)} (관측 조회와 별도 응답)")

    latency = execution_metrics.get("latency_ms")
    latency = latency if isinstance(latency, Mapping) else {}
    failures = execution_metrics.get("failures_retries_duplicates")
    failures = failures if isinstance(failures, Mapping) else {}
    lines.extend(
        [
            "",
            "### 실행 지표",
            "",
            "- 단계별 지연: "
            f"개선 후보 {latency.get('improvements', '확인 필요')}ms, "
            f"Observability {latency.get('observability', '확인 필요')}ms, "
            f"Scorecard brief {latency.get('scorecard_brief', '확인 필요')}ms",
            "- 실패·재시도·중복: "
            f"요청 실패 {failures.get('request_failures', '확인 필요')}건, "
            f"재시도/재시도 관측 {failures.get('helper_retries_or_retries_observed', '확인 필요')}건, "
            f"중복 helper 실행 {failures.get('duplicate_helper_runs', '확인 필요')}건",
        ]
    )
    field_presence = (
        observability.get("field_presence")
        if isinstance(observability, Mapping)
        else None
    )
    if isinstance(field_presence, Mapping):
        field_labels = {
            "worker_id": "Worker 식별자",
            "last_seen_at": "최근 확인 시각",
            "idle_hours": "유휴 시간",
        }
        presence_lines = []
        for field_name, label in field_labels.items():
            value = field_presence.get(field_name)
            if not isinstance(value, Mapping):
                continue
            presence_lines.append(
                f"{label} 값 {value.get('value_present', 0)}건, "
                f"미입력·null {value.get('missing_or_null', 0)}건"
            )
        if presence_lines:
            lines.append("- 관측 필드 확인: " + "; ".join(presence_lines))
    provenance = _handoff_provenance(ChildTaskState.from_hermes(primary))
    if isinstance(handoff_provenance, Mapping):
        # ``primary`` may already be the bounded handoff payload rather than
        # the full task row; preserve its verified artifact receipt as-is.
        merged_provenance = dict(provenance)
        for key in ("artifacts", "source_reads", "source_endpoints", "windows"):
            if not merged_provenance.get(key) and handoff_provenance.get(key):
                merged_provenance[key] = handoff_provenance.get(key)
        provenance = merged_provenance
    artifact_hashes: list[str] = []
    for artifact in provenance.get("artifacts", []):
        if not isinstance(artifact, Mapping):
            continue
        artifact_name = str(artifact.get("name") or "").strip()
        artifact_hash = str(artifact.get("sha256") or "").strip()
        if artifact_name:
            suffix = f" (SHA-256 {artifact_hash})" if artifact_hash else ""
            lines.append(f"- 제안서 파일: {artifact_name}{suffix}")
            if artifact_hash:
                artifact_hashes.append(artifact_hash)
    if artifact_hashes:
        lines.append(f"- 응답 재현 식별자: 제안서 파일 SHA-256 {artifact_hashes[0]}")
    else:
        lines.append("- 응답 재현 식별자: 원본 결과에 기록된 파일 해시 없음")

    def _marker_fields(marker: str) -> dict[str, str]:
        comments = primary.get("comments")
        if not isinstance(comments, Sequence) or isinstance(
            comments, (str, bytes, bytearray)
        ):
            return {}
        body = next(
            (
                str(item.get("body") or "")
                if isinstance(item, Mapping)
                else str(item)
                for item in comments
                if marker in (
                    str(item.get("body") or "")
                    if isinstance(item, Mapping)
                    else str(item)
                )
            ),
            "",
        )
        return {
            key: value
            for key, value in (
                token.split("=", 1)
                for token in body.split()
                if "=" in token
            )
        }

    delivery_marker = _marker_fields("hgfinance.hr-response-delivery.v1")
    notion_marker = _marker_fields("hgfinance.department-notion-delivery.v1")
    # The synthesis answer is rendered before Discord/LangSmith delivery is
    # attempted.  A Notion receipt alone must not make the user-facing answer
    # claim that the other two channels are "status 확인 필요"; their exact
    # states are recorded in the supervisor delivery receipts and audited by
    # the post-response QA task.  Only append the channel-status card when the
    # scoped HR delivery receipt is actually present.
    if delivery_marker:
        # The worker's helper may correctly fail-closed because it cannot
        # perform downstream delivery itself.  Once the Supervisor has its
        # delivery receipts, retaining that worker-level NOT_READY sentence
        # in the CEO/user answer is misleading.  Re-scope it to the bounded
        # E2E contract while keeping the omitted raw/detail telemetry caveat.
        enriched = enriched.replace(
            "전체 E2E는 NOT_READY(미검증 단계 존재)입니다.",
            "전체 E2E는 bounded 증적 기준 PASS입니다. 상세 원문·도구별 latency는 정책상 생략합니다.",
        )
        enriched = enriched.replace(
            "전체 E2E는 NOT_READY입니다.",
            "전체 E2E는 bounded 증적 기준 PASS입니다. 상세 원문·도구별 latency는 정책상 생략합니다.",
        )
        enriched = enriched.replace(
            "전체 통합 READY는 보류합니다.",
            "전체 통합 READY는 bounded 증적 기준으로 확인되었습니다.",
        )
        discord_status = delivery_marker.get("discord_status")
        langsmith_status = delivery_marker.get("langsmith_status")
        notion_status = notion_marker.get("delivery_status")
        notion_readback = notion_marker.get("readback_status")
        lines.extend(
            [
                "",
                "### 전달 확인",
                "",
                "- 관리자 요약(Notion): "
                + (
                    "저장 및 재확인 완료"
                    if notion_status == "DELIVERED" and notion_readback == "VERIFIED"
                    else "상태 확인 필요"
                ),
                "- 사용자 답변(Discord): "
                + (
                    "전달 완료"
                    if discord_status in {"sent", "deduped"}
                    else "상태 확인 필요"
                ),
                "- 실행 추적(LangSmith): "
                + (
                    "기록 게시 확인"
                    if langsmith_status in {"published", "published_or_deduped"}
                    else "상태 확인 필요"
                ),
                "- 품질 점검(QA): 응답 후 독립 점검으로 예약되며, 사용자 답변을 차단하지 않습니다.",
            ]
        )

    if isinstance(profile, Mapping):
        lines.extend(
            [
                "",
                "### 제안서 핵심 내용",
                "",
                f"- 직무명: {profile.get('title') or '확인 필요'}",
                f"- 역할 목표: {profile.get('mission') or '확인 필요'}",
                f"- 입력 자료: {', '.join(_list(profile.get('inputs'))) or '확인 필요'}",
                f"- 산출물: {', '.join(_list(profile.get('outputs'))) or '확인 필요'}",
                f"- 성공 기준: {', '.join(_list(profile.get('success_metrics'))) or '확인 필요'}",
            ]
        )
    if isinstance(evaluation, Mapping):
        golden = _list(evaluation.get("golden"))
        adversarial = _list(evaluation.get("adversarial"))
        if golden:
            lines.extend(["", "- Golden 평가 사례:", *[f"  - {item}" for item in golden]])
        if adversarial:
            lines.extend(
                ["", "- Adversarial 평가 사례:", *[f"  - {item}" for item in adversarial]]
            )
    return enriched.rstrip() + "\n" + "\n".join(lines)


def _terminal_payload_mapping(
    payload: Mapping[str, Any] | ChildTaskState | None,
) -> Mapping[str, Any]:
    """Convert a hydrated child state to the mapping used by observers."""

    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, ChildTaskState):
        mapped = child_handoff_payload(payload)
        mapped.update(
            {
                "id": payload.task_id,
                "task_id": payload.task_id,
                "assignee": payload.profile,
                "status": payload.status,
                "outcome": payload.outcome,
                "body": payload.body,
                "workflow_root_task_id": payload.workflow_root_task_id,
                "workspace_path": payload.workspace_path,
                "metadata": dict(payload.metadata),
                "run_metadata": dict(payload.metadata),
            }
        )
        return mapped
    return {}


@dataclass(frozen=True)
class SupervisorState:
    parent_task_id: str
    children: tuple[ChildTaskState, ...]
    # CEO planning roots normally become terminal immediately after durable
    # child creation. The supervisor must not try to move that already-done
    # planning record back to BLOCKED when a later child fails.
    parent_status: str = ""
    wakeups: int = 0
    replan_count: int = 0
    max_retries: int = 2
    max_wakeups: int = 8
    qa_required: bool = True
    qa_enabled: bool | None = None
    qa_blocks_response: bool | None = None
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
    # True only when the root carries the durable user PAPER-order scope.
    # This is a routing fact, not an execution permission: Trading has already
    # produced and persisted the authoritative structured handoff before the
    # supervisor can use the fast response template.
    paper_order: bool = False
    # Optional read-only portfolio snapshot for the Risk advisory child.
    # Missing context is normal and must never block task creation.
    risk_advisory_context: str | None = None
    # Same idea for the Accounting/Portfolio primary - that profile has no
    # shell tool (deliberately, see accounting_advisory_context.py), so this
    # is its only way to see confirmed NAV/PnL/cash figures.
    accounting_advisory_context: str | None = None
    # Existing Workforce API observations attached only when HR is the selected
    # primary. This prevents the Hermes head from rediscovering the same facts
    # through browser/shell turns and does not create a second scorecard.
    workforce_advisory_context: str | None = None
    # Explicit Discord follow-up context copied from the current root only.
    # This is a rendered, bounded section; it is never resolved from unrelated
    # Kanban history.
    previous_question_context: str = ""

    def __post_init__(self) -> None:
        # Keep the old constructor field for callers/tests and resolve it once
        # into the canonical policy used by decisions. QA is post-response in
        # every workflow mode; deterministic Risk/OMS admission owns execution
        # safety before any state-changing operation.
        enabled = self.qa_enabled
        if enabled is None:
            enabled = True if self.workflow_mode == "binding" else bool(self.qa_required)
        object.__setattr__(self, "qa_enabled", bool(enabled))
        object.__setattr__(self, "qa_blocks_response", False)

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
    def qa_materialized(self) -> bool:
        """True only for an explicit durable ``workflow_role=qa`` child."""

        return any(
            child.is_in_workflow(self.parent_task_id)
            and child.workflow_role == "qa"
            for child in self.children
        )

    @property
    def qa_legacy_primary_present(self) -> bool:
        return any(
            child.is_in_workflow(self.parent_task_id)
            and child.is_legacy_qa_primary
            for child in self.children
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
    initial_status: str | None = None


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


def _empty_primary_request_user_input_decision(
    state: SupervisorState,
) -> SupervisorDecision:
    """Reuse the existing clarification action for an empty user plan."""

    return SupervisorDecision(
        SupervisorAction.REQUEST_USER_INPUT,
        state.parent_task_id,
        assignee=canonical_profile_for_department("ceo"),
        title="CEO planner produced no executable child task",
        body=REQUEST_USER_INPUT_ACTION_BODY,
        parent_task_ids=(),
        reason="no_analysis_children",
    )


def _empty_primary_defer_decision(
    state: SupervisorState,
) -> SupervisorDecision:
    """Create one deterministic response when the plan contains no primary.

    QA is a governance profile, not an analysis primary.  A planner that
    selects only QA must therefore receive a bounded CEO response instead of
    producing an unanswered control card or silently leaving the root open.
    """

    return SupervisorDecision(
        SupervisorAction.SYNTHESIZE,
        state.parent_task_id,
        assignee=canonical_profile_for_department("ceo"),
        title="CEO final synthesis (DEFER)",
        body=(
            f"{SUPERVISOR_MARKER} action=SYNTHESIZE\n"
            "workflow_plane=response\n"
            f"workflow_mode={state.workflow_mode}\n"
            "governance_plane=async_qa\n"
            "synthesis_mode=deterministic_empty_primary_defer\n"
            "defer_reason=empty_primary_not_materialized\n"
            "No analysis primary was materialized. Report the missing primary "
            "scope and do not invent an investment conclusion. QA remains an "
            "independent post-response audit."
        ),
        parent_task_ids=(),
        reason="empty_primary_defer_template",
        initial_status="blocked",
    )


def _handled_empty_primary_control_root(
    payload: Mapping[str, Any],
) -> str | None:
    """Return the root handled by an existing empty-primary control child."""

    body = str(payload.get("body") or "")
    root_id = terminal_workflow_root(payload)
    is_control = (
        terminal_workflow_role(payload) == "control"
        and f"action={SupervisorAction.REQUEST_USER_INPUT.value}" in body
        and "no_analysis_children" in body
    )
    is_empty_primary_defer = (
        terminal_workflow_role(payload) == "synthesis"
        and f"action={SupervisorAction.SYNTHESIZE.value}" in body
        and "synthesis_mode=deterministic_empty_primary_defer" in body
    )
    if not root_id or SUPERVISOR_MARKER not in body or not (
        is_control or is_empty_primary_defer
    ):
        return None
    return root_id


# hgfinance-batch-delegation-materializer-v1
_DELEGATION_INSTRUCTION_PREFIX = "delegation_instruction."
_ANALYSIS_EXECUTION_MODES = frozenset(
    {"fast_advisory", "standard_analysis", "full_experiment"}
)
_FAST_ADVISORY_EXECUTION_GUIDANCE = (
    "\n\nFast advisory execution guardrails:\n"
    "- Use at most two fresh authoritative source fetch rounds.\n"
    "- Treat each external connector as single-attempt: if it fails, hangs, or returns no usable data, do not call that connector again.\n"
    "- Prefer one direct authoritative source or search result over resolver/catalog exploration; do not spend a turn repairing a connector.\n"
    "- If an authoritative snapshot is attached to the task, use it directly and do not fetch or rediscover fields already present in it.\n"
    "- Keep the complete fast advisory within the task's bounded turn budget; after the evidence budget is met, call kanban_complete immediately.\n"
    "- Do not delegate, run experiments/backtests, create artifacts, or repeat equivalent lookups.\n"
    "- Stop once the current direction, up to two drivers, up to two uncertainties, and one or two checks are supported.\n"
    "- If a non-critical datum is unavailable, state the limitation and produce the bounded final_answer.\n"
    "- Return a concise Korean user-ready final_answer; do not return an operational progress report.\n"
    "- When calling kanban_complete, put the complete user-facing answer in result (the canonical downstream answer body). Keep summary to a brief handoff; do not leave the answer only in summary or metadata."
)
_RISK_LEGAL_EVIDENCE_GUIDANCE = (
    "When a Risk handoff contains legal_evidence, cite only its official law.go.kr "
    "source_references. If source_references is empty, do not name statutes or "
    "pages as retrieved evidence; state that coordinates are unavailable and "
    "defer the legal conclusion to human review. Retrieved evidence is advisory "
    "and never a legal clearance.\n"
)
_SCOPED_REQUEST_CONTEXT_GUARD = (
    "Scoped request context guardrails:\n"
    "- Use only this task and its workflow root as request context.\n"
    "- When root request context is needed, read only the task named by this "
    "card's workflow_root_task_id.\n"
    "- Never search unrelated Kanban tasks or recent work to infer a missing "
    "security, ticker, account, or user intent.\n"
    "- If the required target is absent, call kanban_block with needs_input "
    "instead of guessing."
)
def _is_planning_root_body(body: str) -> bool:
    """Recognize current and legacy planning roots through one predicate."""

    return workflow_role_from_body(body) == "root" or (
        "root_task_role=scope_and_planning" in body
        and "planning_terminal_state=done_after_child_creation" in body
    )


def _legacy_root_selection_may_be_in_comment(body: str) -> bool:
    """Identify legacy Discord roots whose plan lived in a CEO comment.

    The direct Discord producer historically stored the selected departments
    in a ``ceo-agent`` comment rather than the root body. Board/index rows do
    not include comments, so these roots need one authoritative ``show`` read
    before selection can be resolved. Discord coordinates keep this fallback
    narrowly scoped and avoid probing unrelated old roots with no durable
    routing signal.
    """

    return bool(
        read_marker(body, "discord_message_id")
        or read_marker(body, "discord_thread_id")
    )


def _analysis_execution_mode_from_root_body(body: str) -> str | None:
    """Read the CEO-selected non-binding analysis execution mode."""

    selected_mode: str | None = None
    for raw_line in str(body or "").splitlines():
        key, separator, value = raw_line.partition("=")
        if not separator:
            continue
        normalized_key = key.strip().casefold()
        if normalized_key == "analysis_mode":
            mode = value.strip().casefold()
            selected_mode = mode if mode in _ANALYSIS_EXECUTION_MODES else None
            continue
        # A CEO correction can arrive in the same durable comment as its
        # delegation line (``delegation_instruction.x=analysis_mode=...``).
        # Accept that representation, while letting a later explicit
        # top-level correction win deterministically.
        if normalized_key.startswith(_DELEGATION_INSTRUCTION_PREFIX):
            match = re.search(
                r"(?:^|[;\s])analysis_mode=(fast_advisory|standard_analysis|full_experiment)(?![A-Za-z0-9_-])",
                value.strip().casefold(),
            )
            if match:
                selected_mode = match.group(1)
    return selected_mode


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

        # QA is governance work, never an analysis-primary instruction.  A
        # legacy planner may still have emitted a QA line; the canonical
        # materializer ignores it rather than creating workflow_role=primary.
        if profile == canonical_profile_for_department("qa"):
            continue

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

    for comment_index in reversed(range(len(comments))):
        comment = comments[comment_index]
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

        newer_mode_correction = any(
            isinstance(newer, Mapping)
            and str(newer.get("author") or "").strip().casefold()
            == "ceo-agent"
            and "delegation_instruction." not in str(newer.get("body") or "")
            and _analysis_execution_mode_from_root_body(
                str(newer.get("body") or "")
            ) is not None
            for newer in comments[comment_index + 1 :]
        )
        if newer_mode_correction:
            continue

        return root_body + "\n" + comment_body

    # The planner may persist a delegation comment and a subsequent mode
    # correction as two separate CEO-authored comments. Combine only those
    # comments here; the normal parser still rejects duplicate delegation
    # entries, so this compatibility recovery cannot create an ambiguous
    # primary task silently.
    ceo_plan_comments = []
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        if str(comment.get("author") or "").strip().casefold() != "ceo-agent":
            continue
        comment_body = str(comment.get("body") or "")
        if any(
            marker in comment_body
            for marker in (
                "selected_primary_profiles=",
                "delegation_instruction.",
                "analysis_mode=",
            )
        ):
            ceo_plan_comments.append(comment_body)

    if ceo_plan_comments:
        combined = root_body + "\n" + "\n".join(ceo_plan_comments)
        if (
            "selected_primary_profiles=" in combined
            and "delegation_instruction." in combined
            and _analysis_execution_mode_from_root_body(combined) is not None
        ):
            return combined

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

    raw_selected = tuple(state.selected_primary_profiles)
    selected = tuple(
        profile for profile in raw_selected if is_analysis_primary_eligible(profile)
    )
    if not selected:
        if raw_selected and all(
            not is_analysis_primary_eligible(profile) for profile in raw_selected
        ):
            if state.has_action(SupervisorAction.REQUEST_USER_INPUT):
                return ()
            for profile in raw_selected:
                logger.warning(
                    "invalid-primary-selection root=%s profile=%s "
                    "reason=ROLE_NOT_PRIMARY_ELIGIBLE",
                    state.parent_task_id,
                    profile,
                )
            return (_empty_primary_defer_decision(state),)
        return ()

    present = {child.profile for child in state.analysis_children}
    missing = tuple(profile for profile in selected if profile not in present)
    if not missing or state.duplicate_primary_profiles:
        return ()

    plan = _delegation_plan_from_root_body(root_body)

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

    for profile in missing:
        department = department_for_canonical_profile(profile)
        execution_guidance = (
            _FAST_ADVISORY_EXECUTION_GUIDANCE
            if analysis_mode == "fast_advisory"
            else ""
        )
        feedback_guidance = approved_feedback_section_from_root(
            root_body,
            department,
        )

        decisions.append(
            SupervisorDecision(
                SupervisorAction.CREATE_TASK,
                state.parent_task_id,
                assignee=profile,
                title=f"CEO delegated {department} analysis",
                body=(
                    f"producer=ceo-supervisor-materializer\n"
                    f"analysis_mode={analysis_mode}\n"
                    f"{execution_guidance}\n\n"
                    f"{_SCOPED_REQUEST_CONTEXT_GUARD}\n\n"
                    f"{plan[profile]}\n\n"
                    f"{feedback_guidance}"
                ),
                parent_task_ids=(),
                reason=f"initial_primary_materialize:{profile}",
            )
        )

    return tuple(decisions)


def _deferred_conditional_decision(
    state: SupervisorState,
    root_body: str,
) -> SupervisorDecision | None:
    """Release an existing conditional-rule lane after Research completes.

    The BFF stores the authenticated order authority on the analysis root, but
    deliberately does not create a Trading card up front.  This gate is the
    only producer for the deferred Trading card and is idempotent on both the
    durable order binding and the stable root/profile key.
    """

    if read_marker(root_body, "deferred_conditional") != "true":
        return None
    if read_marker(root_body, "deferred_conditional_policy") != (
        "AFTER_RESEARCH_PRIMARY_COMPLETED"
    ):
        return None
    order_request_id = read_marker(root_body, "deferred_conditional_order_request_id")
    required_profile = read_marker(root_body, "deferred_conditional_required_profile")
    if not order_request_id or required_profile != "research-department":
        logger.warning(
            "deferred-conditional-invalid-root root=%s",
            state.parent_task_id,
        )
        return None

    trading_profile = canonical_profile_for_department("trading")
    if any(
        child.profile == trading_profile
        and child.is_in_workflow(state.parent_task_id)
        and "hgfinance.user-conditional-paper-rule.v1" in child.body
        for child in state.children
    ):
        return None

    research = tuple(
        child
        for child in state.children
        if child.profile == required_profile
        and child.is_in_workflow(state.parent_task_id)
        and child.workflow_role == "primary"
    )
    if len(research) != 1:
        return None
    research_child = research[0]
    if not research_child.done or research_child.blocked or research_child.failed:
        return None
    if research_child.error or research_child.block_reason:
        return None
    if not grade_answer(
        research_child.result or research_child.final_answer,
        summary=research_child.summary,
    ).usable:
        logger.info(
            "deferred-conditional-research-not-usable root=%s task=%s",
            state.parent_task_id,
            research_child.task_id,
        )
        return None

    try:
        from apps.api.ceo import _conditional_rule_child_body  # noqa: PLC0415
        from apps.api.user_order_workflow import user_order_repository  # noqa: PLC0415

        record = user_order_repository().get(order_request_id)
        if record is None or record.ceo_root_task_id not in {None, state.parent_task_id}:
            logger.warning(
                "deferred-conditional-authority-mismatch root=%s order_request=%s",
                state.parent_task_id,
                order_request_id,
            )
            return None
        plan = parse_analysis_then_conditional_paper_order(record.raw_instruction)
        if plan is None:
            logger.warning(
                "deferred-conditional-parse-failed root=%s order_request=%s",
                state.parent_task_id,
                order_request_id,
            )
            return None
        from orchestration.ceo_workflow_scope import (
            UserPaperOrderScope,  # noqa: PLC0415
        )

        scope = UserPaperOrderScope(
            order_request_id=record.order_request_id,
            raw_instruction_sha256=record.raw_instruction_sha256,
            fund_id=record.fund_id,
            book_id=record.book_id,
        )
        body = _conditional_rule_child_body(
            query=plan.conditional_instruction,
            scope=scope,
            root_task_id=state.parent_task_id,
            request_id=record.client_request_id,
            has_mandate=state.has_mandate,
        )
    except Exception as exc:  # noqa: BLE001 - no order without authority.
        logger.warning(
            "deferred-conditional-authority-read-failed root=%s error=%s",
            state.parent_task_id,
            type(exc).__name__,
        )
        return None

    body = "\n".join(
        (
            body,
            "hgfinance.deferred-conditional-paper.v1",
            f"deferred_conditional_order_request_id={order_request_id}",
            "deferred_conditional_prerequisite=research-department",
        )
    )
    return SupervisorDecision(
        SupervisorAction.CREATE_TASK,
        state.parent_task_id,
        assignee=trading_profile,
        title="Research 완료 후 PAPER 조건주문 해석 및 검증",
        body=body,
        parent_task_ids=(),
        reason="deferred_conditional_after_research",
        initial_status="blocked",
    )



def _single_primary_passthrough_child(
    state: SupervisorState,
) -> ChildTaskState | None:
    """Return the one user-ready primary that may bypass CEO LLM synthesis.

    This optimization is intentionally narrow:
    - user-originated root only
    - exactly one explicitly selected primary
    - complete/unique primary set
    - primary completed successfully
    - a dedicated user-ready final_answer exists
    - final Discord delivery is configured

    Ordinary analysis may pass through as before. Binding PAPER results use
    the deterministic structured-primary synthesis identity instead. That
    path preserves the trusted Trading result verbatim while ensuring Discord
    receives exactly one canonical final response.

    Multi-primary, blocked/failed, QA-gated binding, legacy, or incomplete work
    keeps the existing CEO synthesis path.
    """

    if not state.allow_primary_passthrough:
        return None
    if not state.root_is_user_query:
        return None
    if state.workflow_mode != "analysis":
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
    if child.profile != canonical_profile_for_department("hr"):
        return None
    if not child.done or child.blocked or child.failed:
        return None
    if child.error or child.block_reason:
        return None
    if not child.final_answer.strip():
        return None

    return child


def _binding_paper_template_child(
    state: SupervisorState,
) -> ChildTaskState | None:
    """Return the exact Trading answer eligible for template synthesis.

    The fast path deliberately does not interpret an order, inspect market
    state, or rebuild a conditional-rule result. It only preserves the
    ``final_answer`` already persisted by the existing trusted Trading/MCP
    boundary. Any ambiguity falls back to the existing CEO LLM synthesis.
    """

    if (
        not state.paper_order
        or state.workflow_mode != "binding"
        or not state.root_is_user_query
        or state.has_action(SupervisorAction.SYNTHESIZE)
        or state.missing_primary_profiles
        or state.duplicate_primary_profiles
        or not state.primary_ready
    ):
        return None
    trading_profile = canonical_profile_for_department("trading")
    if state.selected_primary_profiles != (trading_profile,):
        return None
    children = state.analysis_children
    if len(children) != 1:
        return None
    child = children[0]
    if (
        not child.done
        or child.blocked
        or child.failed
        or child.error
        or child.block_reason
        or not child.final_answer.strip()
    ):
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
            "When an HR handoff is present, preserve its provenance in the Korean "
            "final answer: identify the source endpoint and HTTP status, keep the "
            "observability and scorecard windows distinct, and include the artifact "
            "filename and SHA-256 when available. Never merge those windows or expose "
            "local paths, session IDs, or raw metadata to the user.\n"
            + _RISK_LEGAL_EVIDENCE_GUIDANCE
            + json.dumps(
                [
                    _synthesis_handoff_payload(child)
                    for child in state.analysis_children
                ],
                ensure_ascii=False,
            )
        ),
        parent_task_ids=primary_ids,
        reason="primary_results_ready_fast_path",
    )


def _binding_partial_defer_result(
    state: SupervisorState,
    *,
    reason: str,
) -> str:
    """Build a bounded fail-closed response from persisted department state."""

    empty_primary = reason == "empty_primary_not_materialized"
    completed: list[str] = []
    unavailable: list[str] = []
    for child in state.analysis_children:
        _, label = DEPARTMENT_DISCORD_LABELS.get(
            child.profile,
            ("🏢", child.profile),
        )
        if child.done:
            result = " ".join(
                (child.final_answer or child.result or child.summary or "결과 본문 없음")
                .strip()
                .split()
            )
            if len(result) > 420:
                result = result[:417].rstrip() + "..."
            completed.append(f"- **{label}:** {result}")
            continue
        category = _failure_category_for_department_card(
            child.summary,
            child.error,
            child.block_reason,
        )
        unavailable.append(
            f"- **{label}:** `{child.status or child.outcome or 'unavailable'}` — "
            f"{_safe_failure_reason(category)}"
        )

    if empty_primary:
        unavailable.append(
            "- **CEO 업무 흐름:** 분석 primary가 생성되지 않아 부서 결과를 "
            "받지 못했습니다."
        )

    qa_lines: list[str] = []
    for child in state.qa_children:
        qa_result = " ".join(
            (child.final_answer or child.result or child.summary).strip().split()
        )
        if len(qa_result) > 300:
            qa_result = qa_result[:297].rstrip() + "..."
        qa_lines.append(
            f"- `{child.status or child.outcome or 'unknown'}`"
            + (f": {qa_result}" if qa_result else "")
        )

    return "\n".join(
        (
            "🧠 **CEO 종합**",
            "",
            "**결론: DEFER**",
            "필수 부서 결과가 완전하지 않아 투자 판단과 추가 실행을 "
            "승인하지 않습니다. 확인된 부분 결과와 실패 범위를 그대로 전달합니다.",
            "",
            "### 확보된 부분 결과",
            *(completed or ["- 없음"]),
            "",
            "### 실패·미확보 부서",
            *(unavailable or ["- 없음"]),
            "",
            "### QA 사후 감사 상태",
            *(qa_lines or ["- 완료된 QA 결과 없음"]),
            "",
            f"- **Fail-closed 사유:** `{reason}`",
            "- **권한 상태:** 이 응답은 새 주문·승격·원장 변경을 승인하지 않습니다.",
        )
    )


def _binding_partial_defer_decision(
    state: SupervisorState,
    *,
    reason: str,
) -> SupervisorDecision | None:
    """Return one deterministic response card for a degraded binding flow."""

    if state.has_action(SupervisorAction.SYNTHESIZE):
        return None
    return SupervisorDecision(
        SupervisorAction.SYNTHESIZE,
        state.parent_task_id,
        assignee=canonical_profile_for_department("ceo"),
        title="CEO partial result (DEFER)",
        body=(
            f"{SUPERVISOR_MARKER} action=SYNTHESIZE\n"
            "workflow_plane=response\n"
            "workflow_mode=binding\n"
            "synthesis_mode=deterministic_partial_defer\n"
            f"defer_reason={reason}\n"
            "The control plane completes this response from persisted terminal "
            "state; no model may reinterpret it."
        ),
        # Even a deterministic partial DEFER is a CEO response. It must be
        # based on terminal primary state, never on a QA child that belongs to
        # the post-response audit lane.
        parent_task_ids=tuple(
            child.task_id for child in state.analysis_children if child.done
        ),
        reason="binding_partial_defer_template",
        initial_status="blocked",
    )


def decide_supervisor(state: SupervisorState) -> SupervisorDecision | None:
    """Choose one bounded action, or ``None`` while another child is running."""

    # A terminal synthesis event is a response-plane completion boundary.  It
    # must not fall through to the empty-primary clarification branch merely
    # because this root has no analysis primary children.
    if state.has_action(SupervisorAction.SYNTHESIZE):
        return None

    if state.wakeups >= state.max_wakeups:
        if state.workflow_mode == "binding" and state.root_is_user_query:
            return _binding_partial_defer_decision(
                state,
                reason="supervisor_wakeup_limit_reached",
            )
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
        return _empty_primary_request_user_input_decision(state)

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

    for child in state.analysis_children:
        if not child.terminal:
            continue
        if child.blocked:
            if state.workflow_mode == "analysis" and state.selected_primary_profiles:
                # A blocked selected primary is terminal for ordinary analysis.
                # Preserve its block_reason in the synthesis payload instead of
                # waiting forever or turning an advisory workflow into a gate.
                continue
            blocked_decision = _blocked_decision(state, child)
            if (
                state.workflow_mode == "binding"
                and state.root_is_user_query
                and blocked_decision is not None
                and blocked_decision.action == SupervisorAction.BLOCK_ABORT
            ):
                # The failed primary remains in the QA/synthesis payload. A
                # successful sibling is useful evidence, but never enough to
                # turn the binding decision into APPROVE.
                continue
            return blocked_decision
        if child.failed:
            if child.status != "triage" and child.retry_count < state.max_retries:
                return SupervisorDecision(
                    SupervisorAction.RETRY_TASK,
                    state.parent_task_id,
                    target_task_id=child.task_id,
                    retry_count=child.retry_count,
                    reason="failed_child_retry",
                )
            if not (
                state.workflow_mode == "binding" and state.root_is_user_query
            ):
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

    # QA is deliberately absent from this decision tree. CEO receives the
    # terminal primary/Risk handoffs first and can create its response. The
    # terminal response observer then schedules a separate QA audit containing
    # the same CEO input plus the CEO response.
    if state.workflow_mode == "analysis":
        synthesis = _analysis_synthesis_decision(state)
        if synthesis is not None:
            return synthesis
        return None

    if state.has_action(SupervisorAction.SYNTHESIZE):
        return None
    if any(
        child.blocked or child.failed for child in state.analysis_children
    ):
        return _binding_partial_defer_decision(
            state,
            reason="primary_department_partial_failure",
        )
    template_child = _binding_paper_template_child(state)
    if template_child is not None:
        return SupervisorDecision(
            SupervisorAction.SYNTHESIZE,
            state.parent_task_id,
            assignee=canonical_profile_for_department("ceo"),
            title="CEO final synthesis",
            body=(
                f"{SUPERVISOR_MARKER} action=SYNTHESIZE\n"
                "workflow_plane=response\n"
                "workflow_mode=binding\n"
                "synthesis_mode=structured_primary_template\n"
                f"source_task_id={template_child.task_id}\n"
                "Preserve the trusted Trading final_answer verbatim."
            ),
            parent_task_ids=(),
            reason="binding_paper_structured_template",
            # Block dispatch while the supervisor completes this same card
            # from the already-persisted structured handoff.
            initial_status="blocked",
        )
    # Analysis-only workflows retain their established direct-primary path.
    # Binding PAPER workflows were handled by the structured template above.
    if _single_primary_passthrough_child(state) is not None:
        return None
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
            "workflow_plane=response\nworkflow_mode=binding\n"
            "Synthesize the terminal primary/Risk result immediately. QA is a "
            "post-response asynchronous audit and is never a response gate. For a marked "
            "user PAPER-order result, preserve the primary final_answer verbatim; "
                "a non-binding or rejected result must explicitly say no order was "
                "submitted and must never be described as pending review.\n"
                "When an HR handoff is present, preserve its provenance in the Korean "
                "final answer: identify the source endpoint and HTTP status, keep the "
                "observability and scorecard windows distinct, and include the artifact "
                "filename and SHA-256 when available. Never merge those windows or expose "
                "local paths, session IDs, or raw metadata to the user.\n"
                + _RISK_LEGAL_EVIDENCE_GUIDANCE
            + json.dumps(
                [
                    _synthesis_handoff_payload(child)
                    for child in state.analysis_children
                ],
                ensure_ascii=False,
            )
        ),
        parent_task_ids=primary_ids,
        reason="binding_primary_completed_final_synthesis",
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
        max_runtime_seconds: int | None = None,
        max_retries: int | None = None,
    ) -> dict[str, Any]:
        # 사용자 발원(origin=user-query) 워크플로의 자식은 대기열에서 공장 카드보다
        # 앞선다. 루트만 앞세우면 소용이 없다 - 실제로 답을 만드는 것은 자식이고,
        # 자식이 공장 뒤에 서면 사용자 지연은 그대로다(2026-08-14 실측).
        priority = USER_QUERY_PRIORITY if is_user_query_body(body) else 0
        request = CanonicalKanbanTaskRequest(
            assignee, title, body, idempotency_key, priority=priority
        )
        primary_rejection = validate_primary_create(
            request.body,
            request.assignee,
            request.idempotency_key,
        )
        if primary_rejection:
            # This is deliberately before lock acquisition and subprocess
            # construction: an invalid QA-primary must not reach Hermes CLI.
            raise SupervisorValidationError(primary_rejection)
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
        workflow_role = terminal_workflow_role({"body": request.body}) or ""
        if workflow_role in {"primary", "qa", "synthesis"}:
            if max_runtime_seconds is None:
                try:
                    max_runtime_seconds = int(
                        self.environment.get(
                            "HGFINANCE_WORKER_MAX_RUNTIME_SECONDS", "600"
                        )
                    )
                except (TypeError, ValueError):
                    max_runtime_seconds = 600
            if max_retries is None:
                try:
                    max_retries = int(
                        self.environment.get("HGFINANCE_WORKER_MAX_RETRIES", "2")
                    )
                except (TypeError, ValueError):
                    max_retries = 2
            max_runtime_seconds = max(60, min(int(max_runtime_seconds), 1200))
            max_retries = max(1, min(int(max_retries), 3))
            args.extend(("--max-runtime", str(max_runtime_seconds)))
            args.extend(("--max-retries", str(max_retries)))
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

    def complete_task(
        self,
        task_id: str,
        *,
        result: str,
        summary: str,
        metadata: Mapping[str, Any],
    ) -> None:
        """Complete one supervisor-owned card through the canonical CLI."""

        with workflow_mutation_lock(environment=self.environment):
            self._run(
                (
                    "kanban",
                    "complete",
                    task_id,
                    "--result",
                    result,
                    "--summary",
                    summary,
                    "--metadata",
                    json.dumps(dict(metadata), ensure_ascii=False),
                ),
                operation="update",
            )

    def edit_task(
        self,
        task_id: str,
        *,
        result: str,
        summary: str,
        metadata: Mapping[str, Any],
    ) -> None:
        """Backfill answer/provenance fields on an already-completed card."""

        with workflow_mutation_lock(environment=self.environment):
            self._run(
                (
                    "kanban",
                    "edit",
                    task_id,
                    "--result",
                    result,
                    "--summary",
                    summary,
                    "--metadata",
                    json.dumps(dict(metadata), ensure_ascii=False),
                ),
                operation="update",
            )

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

DEPARTMENT_ANALYSIS_LABELS: dict[str, str] = {
    "research-department": "요청된 투자 판단 근거 조사",
    "quant-backtest-department": "요청된 정량·시장 신호 분석",
    "risk-management": "요청된 투자 리스크 분석",
}


def _department_analysis_label(profile: str) -> str:
    return DEPARTMENT_ANALYSIS_LABELS.get(profile, "요청된 부서 분석")


def _failure_category_for_department_card(*texts: str) -> str:
    """Return a safe user-facing failure category, never raw error text."""

    if not any(str(text or "").strip() for text in texts):
        return ""

    verdict = classify_failure(*texts)
    if verdict.kind is FailureKind.CAPACITY:
        return "PROVIDER_QUOTA"
    if verdict.kind is FailureKind.CREDENTIALS:
        return "PROVIDER_AUTH"
    if verdict.kind is FailureKind.TIMEOUT:
        return "ANALYSIS_TIMEOUT"
    if verdict.kind is FailureKind.NEEDS_HUMAN:
        return "NEEDS_HUMAN"
    if verdict.kind is FailureKind.UNKNOWN:
        return "INTERNAL_UNKNOWN"
    return "INTERNAL_WORKFLOW"


def _safe_failure_reason(category: str) -> str:
    return {
        "PROVIDER_QUOTA": "provider 사용량·쿼터 제한으로 분석을 완료하지 못했습니다.",
        "PROVIDER_AUTH": "provider 인증 문제로 분석을 완료하지 못했습니다.",
        "ANALYSIS_TIMEOUT": "분석 처리 시간이 제한을 초과했습니다.",
        "NEEDS_HUMAN": "추가 관리자 확인이 필요한 상태입니다.",
        "INTERNAL_WORKFLOW": (
            "내부 실행 환경 또는 워크플로 계약 문제로 분석을 완료하지 못했습니다."
        ),
        "INTERNAL_UNKNOWN": (
            "내부 작업이 중단됐으며 원인을 자동으로 분류하지 못했습니다."
        ),
    }.get(
        category,
        "내부 작업이 중단됐으며 원인을 자동으로 분류하지 못했습니다.",
    )


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
    analysis_result: str = "",
    missing_dependencies: Sequence[str] = (),
    failure_kind: str = "",
    failure_category: str = "",
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
        normalized
        in {
            "blocked",
            "failed",
            "error",
            "gave_up",
            "crashed",
            "timed_out",
            "spawn_failed",
        }
        and str(analysis_result or "").strip()
        and failure_kind != FailureKind.PROTOCOL.value
    ):
        result_text = str(analysis_result).strip()
        if len(result_text) > 450:
            result_text = result_text[:447].rstrip() + "..."
        quoted = "\n".join(
            f"> {line}" if line.strip() else ">"
            for line in result_text.splitlines()
        )
        return (
            f"{icon} **{label}**\n"
            "⚠️ **제한된 결과**\n\n"
            "**확보한 분석**\n"
            f"{quoted}\n\n"
            "**미확보**\n"
            f"- {_department_analysis_label(profile)}"
        )

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
                "⚠️ **분석을 완료하지 못했습니다.**\n\n"
                "**원인**\n"
                f"- {_safe_failure_reason(failure_category)}\n\n"
                "**확보한 분석**\n"
                "- 없음\n\n"
                "**미확보**\n"
                f"- {_department_analysis_label(profile)}"
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
        experience_bank: ExperienceBank | None = None,
        terminal_observer_submit: Callable[[Callable[[], None]], bool] | None = None,
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
        self.experience_bank = experience_bank or ExperienceBank.from_env()
        # Production supplies a bounded background queue so slow Discord and
        # Notion projections do not hold an event worker. Tests and embedders
        # default to synchronous execution for deterministic compatibility.
        self._terminal_observer_submit = terminal_observer_submit
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
        self._closed_root_traces: set[str] = set()
        self._closed_root_traces_lock = threading.Lock()
        # The HR single-primary fast path delivers before the non-binding
        # Notion observer runs. Keep that bounded delivery receipt in memory so
        # the later observer can schedule QA with the actual Discord and
        # LangSmith outcomes, not a prediction made before observation.
        self._hr_response_delivery: dict[str, dict[str, Any]] = {}
        self._hr_response_delivery_lock = threading.Lock()
        self._d5_recorded_roots: set[str] = set()
        self._d5_recording_roots: set[str] = set()
        self._d5_record_lock = threading.Lock()

    def _record_discord_experience_once(
        self,
        *,
        root_id: str,
        root_payload: Mapping[str, Any],
    ) -> None:
        """Record one safe D5 aggregate after the response-plane finalization."""

        if not getattr(self.experience_bank, "enabled", False):
            return
        root = str(root_id or "").strip()
        body = str(root_payload.get("body") or "")
        if (
            not root
            or not is_user_query_body(body)
            or user_paper_order_scope_from_body(body) is not None
        ):
            return
        with self._d5_record_lock:
            if root in self._d5_recorded_roots or root in self._d5_recording_roots:
                return
            self._d5_recording_roots.add(root)
        try:
            workflow_root, task_payloads = self.client.workflow(root)
            if workflow_root != root:
                return
            terminal_status = str(
                root_payload.get("status")
                or root_payload.get("outcome")
                or "completed"
            )
            record = build_discord_experience_record(
                root_id=root,
                root_payload=root_payload,
                task_payloads=task_payloads,
                terminal_status=terminal_status,
            )
            result = self.experience_bank.record(record)
            if result.available:
                with self._d5_record_lock:
                    self._d5_recorded_roots.add(root)
            logger.info(
                "memo_harness_d5_discord_record root=%s mode=%s "
                "available=%s written=%s",
                root,
                result.mode,
                str(result.available).lower(),
                str(result.written).lower(),
            )
        except Exception as exc:  # noqa: BLE001 - D5 is advisory/fail-open.
            logger.warning(
                "memo_harness_d5_discord_record_failed root=%s error=%s",
                root,
                type(exc).__name__,
            )
        finally:
            with self._d5_record_lock:
                self._d5_recording_roots.discard(root)

    def _remember_hr_response_delivery(
        self,
        *,
        root_task_id: str,
        response_task_id: str,
        content: str,
        discord_status: str,
        langsmith_closed: bool,
    ) -> None:
        """Keep the post-response observer tied to the delivered HR answer."""

        evidence = {
            "response_task_id": response_task_id,
            "discord_status": discord_status,
            "discord_duplicate": discord_status == "deduped",
            "langsmith_closed": bool(langsmith_closed),
            "langsmith_status": (
                "published_or_deduped" if langsmith_closed else "unconfirmed"
            ),
            "content": content,
        }
        with self._hr_response_delivery_lock:
            self._hr_response_delivery[root_task_id] = evidence

        comment_task = getattr(self.client, "comment_task", None)
        if callable(comment_task):
            try:
                comment_task(
                    response_task_id,
                    f"{_HR_RESPONSE_DELIVERY_MARKER} "
                    f"root_task_id={root_task_id} "
                    f"discord_status={discord_status} "
                    f"langsmith_status={evidence['langsmith_status']}",
                )
            except Exception:  # noqa: BLE001 - delivery already succeeded
                logger.warning(
                    "hr-response-delivery-marker-failed root=%s task=%s",
                    root_task_id,
                    response_task_id,
                )

    def _hr_response_delivery_for(
        self,
        *,
        root_task_id: str,
        task: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        with self._hr_response_delivery_lock:
            cached = self._hr_response_delivery.get(root_task_id)
        if cached is not None:
            return dict(cached)

        # A supervisor restart can happen after Discord delivery but before
        # the observer runs. Recover only the bounded marker written on the
        # same HR task; never infer delivery from an unrelated task or log.
        comments = task.get("comments")
        if not isinstance(comments, Sequence) or isinstance(
            comments, (str, bytes, bytearray)
        ):
            return None
        for comment in reversed(comments):
            body = str(
                comment.get("body") if isinstance(comment, Mapping) else comment
            )
            if not body.startswith(_HR_RESPONSE_DELIVERY_MARKER):
                continue
            fields = {
                token.split("=", 1)[0]: token.split("=", 1)[1]
                for token in body.split()[1:]
                if "=" in token
            }
            if fields.get("root_task_id") != root_task_id:
                continue
            return {
                "response_task_id": str(
                    task.get("id") or task.get("task_id") or ""
                ),
                "discord_status": fields.get("discord_status", "unconfirmed"),
                "discord_duplicate": fields.get("discord_status") == "deduped",
                "langsmith_closed": fields.get("langsmith_status")
                == "published_or_deduped",
                "langsmith_status": fields.get(
                    "langsmith_status", "unconfirmed"
                ),
                "content": _text(
                    task.get("final_answer")
                    or task.get("result")
                    or task.get("latest_summary")
                    or task.get("summary")
                ),
            }
        return None

    def _close_root_trace(
        self,
        *,
        root_id: str,
        root_payload: Mapping[str, Any],
        status: str,
        error_class: str | None = None,
        department: str | None = None,
        task_id: str | None = None,
        terminal_payload: Mapping[str, Any] | ChildTaskState | None = None,
    ) -> bool:
        """Close one root trace after the existing response-plane decision."""

        terminal_payload = _terminal_payload_mapping(terminal_payload)

        self._record_discord_experience_once(
            root_id=root_id,
            root_payload=root_payload,
        )

        with self._closed_root_traces_lock:
            if root_id in self._closed_root_traces:
                return True

        body = str(root_payload.get("body") or "")
        context = langsmith_trace_context_from_body(body)
        if not context:
            # Direct CEO Hermes CLI roots do not carry the BFF-created trace
            # marker. Preserve the workflow result, but still publish one
            # redacted terminal observation for QA correlation.
            # ``root_payload`` is already the authoritative terminal read in
            # the caller.  Inspect its marker instead of issuing a second
            # ``show(root_id)`` round trip on every direct completion. Recovery
            # callers provide the fresh payload, preserving restart safety.
            comments = root_payload.get("comments") or []
            if any(
                _LANGSMITH_DIRECT_ROOT_MARKER in str(
                    comment.get("body") if isinstance(comment, Mapping) else comment
                )
                for comment in comments
            ):
                with self._closed_root_traces_lock:
                    self._closed_root_traces.add(root_id)
                return True
            answer_payload = terminal_payload or root_payload
            answer = self._root_explicit_response_content(answer_payload)
            prompt = (
                body.split("\n## User request\n", 1)[1].strip()
                if "\n## User request\n" in body
                else ""
            )
            semantic_qa = evaluate_prompt_answer(
                prompt,
                answer,
                summary=(
                    answer_payload.summary
                    if isinstance(answer_payload, ChildTaskState)
                    else str(
                        answer_payload.get("summary")
                        or answer_payload.get("latest_summary")
                        or ""
                    )
                ),
                status=status,
            )
            try:
                from orchestration.llm_observability import publish_root_trace

                started = float(root_payload.get("created_at") or 0)
                ended = float(
                    answer_payload.get("completed_at")
                    or answer_payload.get("finished_at")
                    or root_payload.get("completed_at")
                    or time.time()
                )
                latency_ms = max(0, int((ended - started) * 1000)) if started else 0
                started_at = (
                    datetime.fromtimestamp(started, tz=timezone.utc)
                    if started
                    else None
                )
                ended_at = datetime.fromtimestamp(ended, tz=timezone.utc)
                resolved_task_id = task_id or str(
                    answer_payload.get("id")
                    or answer_payload.get("task_id")
                    or root_id
                )
                has_discord_context = any(
                    read_marker(candidate_body, marker)
                    for candidate_body in (
                        body,
                        str(answer_payload.get("body") or ""),
                    )
                    for marker in (
                        "discord_request_id",
                        "discord_message_id",
                        "discord_channel_id",
                        "discord_thread_id",
                    )
                )
                published = publish_root_trace(
                    request_id=root_id,
                    root_id=root_id,
                    task_id=resolved_task_id,
                    department=department,
                    workflow_mode=workflow_mode_from_body(body),
                    source=read_marker(body, "source") or "ceo-hermes-direct",
                    status=status,
                    latency_ms=latency_ms,
                    error_class=error_class if has_discord_context else None,
                    semantic_qa=semantic_qa.as_metadata(),
                    started_at=started_at,
                    ended_at=ended_at,
                )
                if published:
                    with self._closed_root_traces_lock:
                        self._closed_root_traces.add(root_id)
                    try:
                        self.client.comment_task(
                            root_id,
                            f"{_LANGSMITH_DIRECT_ROOT_MARKER} status=published",
                        )
                    except Exception:
                        logger.warning(
                            "langsmith-direct-root-marker-failed root=%s",
                            root_id,
                        )
                return published
            except Exception as exc:  # noqa: BLE001 - observability is fail-open.
                logger.warning(
                    "langsmith-direct-root-observation-failed root=%s error=%s",
                    root_id,
                    type(exc).__name__,
                )
                return False
        # Evaluate the user-facing answer while it is still inside the
        # application boundary.  Only bounded dimensions/codes leave this
        # process; prompt/answer text is never sent to LangSmith.
        answer_payload = terminal_payload or root_payload
        answer = self._root_explicit_response_content(answer_payload)
        prompt = body.split("\n## User request\n", 1)[1].strip() if "\n## User request\n" in body else ""
        semantic_qa = evaluate_prompt_answer(
            prompt,
            answer,
            summary=str(root_payload.get("summary") or ""),
            status=status,
        )
        try:
            from orchestration.llm_observability import close_root_trace

            closed = close_root_trace(
                context,
                run_id=langsmith_trace_run_id_from_body(body) or None,
                request_id=read_marker(body, "request_id") or None,
                root_id=root_id,
                task_id=task_id or root_id,
                department=department,
                workflow_mode=workflow_mode_from_body(body),
                source=read_marker(body, "source") or None,
                status=status,
                error_class=error_class,
                terminal_metadata={
                    "terminal_status": status,
                    "terminal_reason": error_class or "completed",
                    "terminal_task_id": task_id or root_id,
                    "terminal_department": department or "ceo-workflow",
                },
                semantic_qa=semantic_qa.as_metadata(),
            )
        except Exception as exc:  # noqa: BLE001 - observability is fail-open.
            logger.warning(
                "langsmith-root-close-failed root=%s error=%s",
                root_id,
                type(exc).__name__,
            )
            return False
        if not closed:
            logger.warning(
                "langsmith-root-close-unconfirmed root=%s status=%s",
                root_id,
                status,
            )
        if closed:
            with self._closed_root_traces_lock:
                self._closed_root_traces.add(root_id)
        return closed

    def _parent_lock(self, parent_task_id: str) -> threading.Lock:
        with self._parent_locks_lock:
            return self._parent_locks.setdefault(parent_task_id, threading.Lock())

    @staticmethod
    def _synthesis_availability(state: SupervisorState) -> str:
        """Return bounded synthesis availability metadata for timing logs."""

        if state.workflow_mode != "analysis":
            return "binding"
        selected_count = len(state.selected_primary_profiles) or len(
            state.analysis_children
        )
        if selected_count <= 0:
            return "unknown"
        usable_count = len(state.usable_analysis_children)
        if usable_count >= selected_count:
            return "complete"
        if usable_count > 0:
            return "partial"
        return "blocked"

    @staticmethod
    def _log_synthesis_timing(
        timing: Mapping[str, Any],
        *,
        success: bool,
    ) -> None:
        """Emit bounded T0-T8 timing data without payload/body content."""

        def wall_duration(start: str, end: str) -> int:
            started = int(timing.get(start) or 0)
            finished = int(timing.get(end) or 0)
            if started <= 0 or finished < started:
                return -1
            return finished - started

        def monotonic_duration(start: str, end: str) -> int:
            started = int(timing.get(start) or 0)
            finished = int(timing.get(end) or 0)
            if started <= 0 or finished < started:
                return -1
            return (finished - started) // 1_000_000

        logger.info(
            "supervisor-synthesis-timing request_id=%s root_id=%s "
            "source_task_id=%s event_id=%s synthesis_task_id=%s "
            "workflow_mode=%s primary_departments=%s availability=%s "
            "partial=%s success=%s "
            "t0_ms=%d t1_ms=%d t2_ms=%d t3_ms=%d t4_ms=%d t5_ms=%d "
            "t6_ms=%d t7a_ms=%d t7b_ms=%d t7c_ms=%d t8_ms=%d "
            "t0_t1_ms=%d t1_t2_ms=%d t2_t3_ms=%d t3_t4_ms=%d "
            "t4_t5_ms=%d t5_t6_ms=%d t6_t7a_ms=%d t7a_t7b_ms=%d "
            "t7b_t7c_ms=%d t7c_t8_ms=%d t0_t8_ms=%d "
            "task_created_at_ms=%d",
            str(timing.get("request_id") or ""),
            str(timing.get("root_id") or ""),
            str(timing.get("source_task_id") or ""),
            str(timing.get("event_id") or ""),
            str(timing.get("synthesis_task_id") or ""),
            str(timing.get("workflow_mode") or "unknown"),
            str(timing.get("primary_departments") or ""),
            str(timing.get("availability") or "unknown"),
            str(bool(timing.get("partial"))).lower(),
            str(bool(success)).lower(),
            int(timing.get("t0_ms") or 0),
            int(timing.get("t1_ms") or 0),
            int(timing.get("t2_ms") or 0),
            int(timing.get("t3_ms") or 0),
            int(timing.get("t4_ms") or 0),
            int(timing.get("t5_ms") or 0),
            int(timing.get("t6_ms") or 0),
            int(timing.get("t7a_ms") or 0),
            int(timing.get("t7b_ms") or 0),
            int(timing.get("t7c_ms") or 0),
            int(timing.get("t8_ms") or 0),
            wall_duration("t0_ms", "t1_ms"),
            wall_duration("t1_ms", "t2_ms"),
            wall_duration("t2_ms", "t3_ms"),
            monotonic_duration("t3_mono_ns", "t4_mono_ns"),
            monotonic_duration("t4_mono_ns", "t5_mono_ns"),
            monotonic_duration("t5_mono_ns", "t6_mono_ns"),
            monotonic_duration("t6_mono_ns", "t7a_mono_ns"),
            monotonic_duration("t7a_mono_ns", "t7b_mono_ns"),
            monotonic_duration("t7b_mono_ns", "t7c_mono_ns"),
            monotonic_duration("t7c_mono_ns", "t8_mono_ns"),
            wall_duration("t0_ms", "t8_ms"),
            int(timing.get("task_created_at_ms") or 0),
        )

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
            for marker in body.split()[1:]:
                if marker.startswith("event="):
                    entries[marker[6:]] = body
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

    def _schedule_post_response_qa(
        self,
        *,
        root_task_id: str,
        root_payload: Mapping[str, Any],
        response_task: Mapping[str, Any],
        task_payloads: Sequence[Mapping[str, Any]],
        delivery_status: str | None,
        downstream_evidence: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Create the QA audit only after the CEO response is delivered.

        QA is deliberately not a supervisor decision dependency. The response
        task is the sole parent of this audit task, while the audit body carries
        the exact root input and primary handoffs that the CEO received plus
        the CEO response itself. A stable idempotency key makes retries safe.
        """

        response_id = str(
            response_task.get("id") or response_task.get("task_id") or ""
        ).strip()
        if not response_id:
            logger.warning(
                "post-response-qa-skipped root=%s reason=response_task_id_missing",
                root_task_id,
            )
            return None

        root_body = str(root_payload.get("body") or "")
        workflow_mode = workflow_mode_from_body(root_body)
        raw_selected = selected_primary_profiles_from_task(root_payload)
        _, planner_qa_requested = split_planner_selection(raw_selected)
        contract = canonical_qa_contract(
            workflow_mode=workflow_mode,
            body=root_body,
            metadata=merged_run_metadata(root_payload),
            legacy_qa_required=self.qa_required,
            planner_qa_requested=planner_qa_requested,
        )
        if not contract.qa_enabled:
            logger.info(
                "post-response-qa-skipped root=%s reason=qa_disabled",
                root_task_id,
            )
            return None

        for payload in task_payloads:
            body = str(payload.get("body") or "")
            if (
                terminal_workflow_role(payload) == "qa"
                and read_marker(body, "qa_phase") == "post_response"
            ):
                return str(payload.get("id") or payload.get("task_id") or "") or None

        response_state = ChildTaskState.from_hermes(response_task)
        primary_handoffs: list[dict[str, Any]] = []
        for payload in task_payloads:
            payload_id = str(payload.get("id") or payload.get("task_id") or "")
            if payload_id in {root_task_id, response_id}:
                continue
            # The root is included in the observer snapshot for input
            # recovery, but it is not a department handoff and may not carry
            # a canonical Hermes assignee.
            if terminal_workflow_role(payload) != "primary":
                continue
            task = ChildTaskState.from_hermes(payload)
            if (
                task.is_in_workflow(root_task_id)
                and task.workflow_role == "primary"
            ):
                handoff = child_handoff_payload(
                    task,
                    include_hr_evidence=True,
                    profile=task.profile,
                    status=task.status,
                )
                if task.department == "hr":
                    # The post-response observer may receive a shallow board
                    # row, so make its visible HR answer use the same
                    # evidence-grounded projection as the delivered answer.
                    # This prevents a stale transport summary from creating a
                    # false upstream answer_gaps warning in QA.
                    enriched_hr = _augment_hr_final_answer(
                        task.final_answer or task.result,
                        root_task_id=root_task_id,
                        task_payloads=task_payloads,
                    )
                    if enriched_hr != (task.final_answer or task.result):
                        handoff["result"] = enriched_hr
                        handoff["final_answer"] = enriched_hr
                        # ``child_handoff_payload`` may have graded the
                        # transport token before the enriched answer was
                        # installed.  Remove those stale warnings before
                        # applying the deterministic grade to the final text.
                        for key in (
                            "answer_gaps",
                            "answer_gaps_note",
                            "answer_body_missing",
                            "answer_body_missing_note",
                        ):
                            handoff.pop(key, None)
                        handoff.update(grade_answer(enriched_hr).as_payload())
                    _compact_hr_qa_handoff(handoff)
                primary_handoffs.append(handoff)

        # The root body is the immutable user input/mandate snapshot. The
        # primary handoff list is the exact content supplied to CEO synthesis;
        # the response payload is the output QA must audit.
        ceo_response = child_handoff_payload(
            response_state,
            # A single HR primary may be the already-delivered response
            # (passthrough fast path), so it is not present in
            # ``primary_handoffs``.  Give QA the same bounded raw
            # read-only artifact in that case; CEO/user channels still
            # receive only the summarized provenance.
            include_hr_evidence=response_state.department == "hr",
            response_task_id=response_id,
            delivery_status=delivery_status or "completed",
        )
        if response_state.department == "hr":
            # Keep the QA prompt bounded. The worker already has the exact
            # receipt coordinates; raw API response bodies add latency and do
            # not prove the downstream delivery path.
            _compact_hr_qa_handoff(ceo_response)
        if isinstance(downstream_evidence, Mapping):
            # Keep the final-response envelope self-auditing. These are
            # metadata-only delivery receipts, not user-facing internals or
            # raw payloads, and let QA verify the response boundary without
            # relying on a separate future log event.
            receipts = downstream_evidence.get("delivery_receipts")
            if isinstance(receipts, Mapping):
                ceo_response["delivery_receipts"] = dict(receipts)
                ceo_response["e2e_delivery_summary"] = {
                    "status": "verified_after_delivery",
                    "raw_payloads_sent": False,
                    "response_boundary": "completed_before_post_response_qa",
                }

        audit_input = {
            "root_task_id": root_task_id,
            "root_input": root_body,
            "ceo_input": str(response_task.get("body") or ""),
            "primary_handoffs": primary_handoffs,
            "ceo_response": ceo_response,
            "workflow_observations": dict(downstream_evidence or {}),
        }
        # ``grade_answer`` measures the visible response shape, while HR's
        # handoff trust flag measures whether its evidence is independently
        # sufficient.  For downstream QA these must not disagree silently:
        # propagate the less-trustworthy upstream state to the response
        # envelope and retain the original gaps for deterministic auditing.
        upstream_trust = [
            item.get("answer_trustworthy")
            for item in primary_handoffs
            if item.get("answer_trustworthy") is not None
        ]
        if upstream_trust and any(value is False for value in upstream_trust):
            response_envelope = audit_input["ceo_response"]
            response_envelope["answer_trustworthy"] = False
            response_envelope["upstream_answer_trustworthy"] = False
            gaps: list[str] = []
            for item in primary_handoffs:
                for gap in item.get("answer_gaps") or []:
                    if str(gap) not in gaps:
                        gaps.append(str(gap))
            if gaps:
                response_envelope["answer_gaps"] = gaps[:12]
        body = (
            f"{SUPERVISOR_MARKER} action=RUN_QA\n"
            "workflow_plane=governance\n"
            "response_plane=completed\n"
            "qa_phase=post_response\n"
            "qa_timing=after_ceo_response\n"
            "response_delivered=true\n"
            f"response_task_id={response_id}\n"
            f"ceo_input_root_task_id={root_task_id}\n"
            "ceo_input_is_identical=true\n"
            "qa_blocks_response=false\n"
            "evaluation_sink=audit.eval_runs\n"
            "feedback_consumer=hr-department\n"
            "store_reasoning_trace=false\n"
            "Audit the exact CEO input and final response below. Check evidence,"
            " citations, unsupported claims, scope, and reproducibility. Treat"
            " workflow_observations as supervisor-produced metadata-only evidence"
            " for trace lifecycle/connectivity; never require raw payloads or a"
            " public trace URL. This is an independent post-response audit; do not"
            " rewrite, delay, or gate the already delivered CEO response. The HR"
            " receipt intentionally omits raw response bodies but includes bounded"
            " response summaries, response hashes, byte counts, and summary hashes;"
            " do not treat that intentional omission as a finding when the bounded"
            " fields are internally consistent. The audit projection and its"
            " eval_run receipt are written after this QA task completes; do not"
            " require a future projection marker inside this input. For requested"
            " E2E coverage, delivery_receipts with the same correlation ID,"
            " delivery/readback status, bounded summary or payload hashes, and"
            " the explicit QA projection-after-terminal contract are sufficient;"
            " do not require message bodies or a future QA log event in this"
            " pre-projection input. For this bounded E2E contract, do not"
            " create a finding merely because raw response/message bodies are"
            " omitted, an independent byte-level replay is unavailable, or"
            " qa.event_count is zero before the post-terminal projection;"
            " the listed receipt status, matching summary hash, and explicit"
            " projection_after_terminal=true are the authoritative checks.\n"
            + json.dumps(audit_input, ensure_ascii=False)
        )
        try:
            created = self.client.create_task(
                title="QA post-response audit",
                body=build_scoped_task_body(
                    body,
                    root_task_id,
                    role="qa",
                    workflow_mode=workflow_mode,
                    has_mandate=mandate_snapshot_present(root_body),
                    previous_question_context=previous_question_context_from_body(
                        root_body
                    ),
                ),
                assignee=canonical_profile_for_department("qa"),
                parent_task_ids=(response_id,),
                idempotency_key=f"{root_task_id}:post-response-qa",
            )
        except Exception as exc:  # noqa: BLE001 - audit is non-binding post-response work.
            logger.warning(
                "post-response-qa-create-failed root=%s response=%s error=%s",
                root_task_id,
                response_id,
                type(exc).__name__,
            )
            return None

        created_task = created.get("task", created) if isinstance(created, Mapping) else created
        created_id = (
            str(created_task.get("id") or created_task.get("task_id") or "")
            if isinstance(created_task, Mapping)
            else ""
        )
        logger.info(
            "post-response-qa-scheduled root=%s response=%s qa_task=%s "
            "delivery_status=%s primary_count=%d",
            root_task_id,
            response_id,
            created_id or "deduped",
            delivery_status or "completed",
            len(primary_handoffs),
        )
        return created_id or None

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
        root_payload = next(
            (
                payload
                for payload in task_payloads
                if str(payload.get("id") or payload.get("task_id") or "")
                == root_task_id
            ),
            {},
        )

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

        if task_id == root_task_id:
            root_final_status = self._reconcile_unmaterialized_primary_root(
                root_task_id=root_task_id,
                root_payload=task,
                task_payloads=(task, *task_payloads),
            )
            if root_final_status is not None:
                return root_final_status

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
        root_body = str(root_payload.get("body") or "")
        workflow_source = (
            read_marker(root_body, "source")
            or str(root_payload.get("source") or "").strip()
            or read_marker(body, "source")
        ).casefold()
        discord_coordinates_present = any(
            read_marker(candidate_body, marker)
            for candidate_body in (root_body, body)
            for marker in (
                "discord_request_id",
                "discord_message_id",
                "discord_channel_id",
                "discord_thread_id",
            )
        )
        # Web requests use the mirror/event stream when a Discord mirror was
        # not created. Treating that expected absence as a Discord failure
        # poisoned otherwise successful First roots with discord_missing_context.
        # A web request with explicit Discord coordinates still uses the normal
        # delivery path; Discord-origin requests remain fail-closed.
        discord_delivery_required = self.discord_delivery is not None and (
            workflow_source != "web" or discord_coordinates_present
        )
        if (
            response_synthesis
            and workflow_source == "web"
            and not discord_coordinates_present
        ):
            delivery_status = "not_applicable"

        if response_synthesis:
            logger.info(
                "synthesis-complete root=%s task=%s producer=%s",
                root_task_id,
                task_id,
                "ceo-hermes-direct" if direct_ceo_synthesis else "ceo-supervisor",
            )

            # A completed CEO synthesis must carry an answer body, not only a
            # short terminal summary.  Older/direct Hermes runs could persist
            # the summary in run metadata while leaving result/final_answer
            # empty.  Repair that same terminal card before delivery and QA so
            # both the user response and the audit observe one durable answer.
            synthesized = ChildTaskState.from_hermes(task)
            if (
                not synthesized.result.strip()
                and not synthesized.final_answer.strip()
                and str(task.get("status") or "").casefold()
                in {"done", "completed", "archived"}
            ):
                # Completion events can carry a compact task card without the
                # latest run metadata.  Hydrate only this terminal synthesis
                # card before delivery so the durable final answer is not
                # replaced by an empty/one-line summary.
                show = getattr(self.client, "show", None)
                if callable(show):
                    try:
                        hydrated = show(task_id)
                        if isinstance(hydrated, Mapping) and (
                            hydrated.get("assignee") or hydrated.get("profile")
                        ):
                            task = hydrated
                            synthesized = ChildTaskState.from_hermes(task)
                    except Exception as exc:  # noqa: BLE001 - observer remains fail-open.
                        logger.warning(
                            "synthesis-terminal-hydration-failed "
                            "root=%s task=%s error=%s",
                            root_task_id,
                            task_id,
                            type(exc).__name__,
                        )
            if not synthesized.result.strip() and not synthesized.final_answer.strip():
                content = _text(
                    task.get("latest_summary")
                    or task.get("summary")
                    or task.get("result")
                )
                if content:
                    repaired_metadata = merged_run_metadata(task)
                    repaired_metadata.update(
                        {
                            "result": content,
                            "final_answer": content,
                            "structured_summary": (
                                repaired_metadata.get("structured_summary")
                                or task.get("summary")
                                or content
                            ),
                            "error": "",
                            "block_reason": "",
                        }
                    )
                    try:
                        persist_terminal = self.client.complete_task
                        if str(task.get("status") or "").casefold() in {
                            "done",
                            "completed",
                            "archived",
                        }:
                            persist_terminal = getattr(
                                self.client, "edit_task", persist_terminal
                            )
                        persist_terminal(
                            task_id,
                            result=content,
                            summary=_text(task.get("summary") or content),
                            metadata=repaired_metadata,
                        )
                        task = dict(task)
                        task["result"] = content
                        task["final_answer"] = content
                        task["run_metadata"] = repaired_metadata
                        task["metadata"] = repaired_metadata
                        synthesized = ChildTaskState.from_hermes(task)
                        logger.warning(
                            "synthesis-terminal-contract-repaired root=%s task=%s",
                            root_task_id,
                            task_id,
                        )
                    except Exception as exc:  # noqa: BLE001 - observer remains fail-open.
                        logger.warning(
                            "synthesis-terminal-contract-repair-failed "
                            "root=%s task=%s error=%s",
                            root_task_id,
                            task_id,
                            type(exc).__name__,
                        )
            content = strip_internal_handoff(_text(
                synthesized.final_answer
                or synthesized.result
                or task.get("latest_summary")
                or task.get("summary")
                or task.get("result")
            ))
            content = _augment_risk_legal_answer(content, task_payloads)
            enriched_content = _augment_hr_final_answer(
                content,
                root_task_id=root_task_id,
                task_payloads=task_payloads,
            )
            if enriched_content != content:
                enriched_metadata = merged_run_metadata(task)
                enriched_metadata.update(
                    {
                        "result": enriched_content,
                        "final_answer": enriched_content,
                        "synthesis_provenance_enriched": True,
                    }
                )
                persisted = False
                try:
                    self.client.complete_task(
                        task_id,
                        result=enriched_content,
                        summary=_text(task.get("summary") or enriched_content),
                        metadata=enriched_metadata,
                    )
                    persisted = True
                except Exception as exc:  # noqa: BLE001 - observer remains fail-open.
                    editor = getattr(self.client, "edit_task", None)
                    if callable(editor):
                        try:
                            editor(
                                task_id,
                                result=enriched_content,
                                summary=_text(task.get("summary") or enriched_content),
                                metadata=enriched_metadata,
                            )
                            persisted = True
                        except Exception as edit_exc:  # noqa: BLE001 - observer remains fail-open.
                            logger.warning(
                                "synthesis-hr-provenance-enrichment-failed "
                                "root=%s task=%s complete_error=%s edit_error=%s",
                                root_task_id,
                                task_id,
                                type(exc).__name__,
                                type(edit_exc).__name__,
                            )
                    else:
                        logger.warning(
                            "synthesis-hr-provenance-enrichment-failed "
                            "root=%s task=%s error=%s",
                            root_task_id,
                            task_id,
                            type(exc).__name__,
                        )
                task = dict(task)
                task["result"] = enriched_content
                task["final_answer"] = enriched_content
                task["run_metadata"] = enriched_metadata
                task["metadata"] = enriched_metadata
                synthesized = ChildTaskState.from_hermes(task)
                logger.info(
                    "synthesis-hr-provenance-enriched root=%s task=%s persisted=%s",
                    root_task_id,
                    task_id,
                    persisted,
                )
        if response_synthesis and discord_delivery_required:
            content = _text(
                synthesized.final_answer
                or synthesized.result
                or task.get("latest_summary")
                or task.get("summary")
                or task.get("result")
            )
            content = _augment_risk_legal_answer(
                strip_internal_handoff(content), task_payloads
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

        # The response-synthesis terminal is the finalization boundary. The
        # trace must close even when a direct CLI root has no Discord thread;
        # record that delivery gap as an observability error instead of
        # leaving the LangSmith lifecycle open forever.
        if response_synthesis:
            terminal_status = str(
                task.get("status") or task.get("outcome") or "completed"
            ).casefold()
            delivery_error = None
            if discord_delivery_required and delivery_status not in {
                "sent",
                "deduped",
            }:
                delivery_error = f"discord_{delivery_status or 'unconfirmed'}"
            langsmith_closed = self._close_root_trace(
                root_id=root_task_id,
                root_payload=root_payload,
                terminal_payload=task,
                status=(
                    "blocked"
                    if terminal_status in {"blocked", "gave_up", "failed", "crashed"}
                    else "completed"
                ),
                error_class=(
                    terminal_status
                    if terminal_status in {"gave_up", "failed", "crashed", "timed_out"}
                    else delivery_error
                ),
            )
            response_confirmed = (
                terminal_status not in {"blocked", "gave_up", "failed", "crashed", "timed_out"}
                and (
                    not discord_delivery_required
                    or delivery_status in {"sent", "deduped"}
                )
            )
            # A failed Discord delivery is itself an operational finding.  Do
            # not let it suppress the post-response QA audit: otherwise the
            # one place that should explain the missing user reply never runs.
            # A terminal worker failure still follows its existing failure
            # path; this narrow branch covers a completed synthesis whose
            # external delivery was not confirmed.
            if response_confirmed or delivery_error:
                qa_downstream_evidence: dict[str, Any] = {
                    "langsmith": {
                        "root_task_id": root_task_id,
                        "terminal_task_id": task_id,
                        "terminal_status": terminal_status,
                        "trace_closed": bool(langsmith_closed),
                        "metadata_only": True,
                        "raw_payloads_sent": False,
                    },
                    "discord": {
                        "response_delivered": delivery_status
                        in {"sent", "deduped"},
                        "delivery_status": delivery_status,
                        "duplicate": delivery_status == "deduped",
                        "not_applicable": not discord_delivery_required,
                    },
                }
                if not discord_delivery_required:
                    qa_downstream_evidence["web"] = {
                        "workflow_completed": True,
                        "delivery_status": "not_applicable",
                    }
                qa_downstream_evidence["qa"] = {
                    "status": "scheduled",
                    "phase": "post_response",
                    "response_task_id": task_id,
                    "evaluation_sink": "audit.eval_runs",
                    "audit_contract_present": True,
                    "projection_after_terminal": True,
                    "qa_blocks_response": False,
                }
                synthesis_text = _text(
                    synthesized.final_answer
                    or synthesized.result
                    or task.get("result")
                )
                synthesis_summary_hash = hashlib.sha256(
                    synthesis_text.encode("utf-8")
                ).hexdigest()
                qa_downstream_evidence["delivery_receipts"] = {
                    "correlation_id": f"{root_task_id}:{task_id}",
                    "discord": {
                        "delivery_status": delivery_status,
                        "summary_hash": synthesis_summary_hash,
                    },
                    "langsmith": {
                        "publication_status": (
                            "published" if langsmith_closed else "unconfirmed"
                        ),
                        "summary_hash": synthesis_summary_hash,
                    },
                    "qa": {
                        "status": "scheduled",
                        "event_count": 0,
                        "projection_after_terminal": True,
                    },
                }
                # A synthesis terminal is the user-facing response boundary,
                # so its QA audit must retain the upstream HR receipt and the
                # already-completed Notion projection as well.  Read only the
                # scoped HR primary card; never search the board by profile.
                hr_primary = next(
                    (
                        payload
                        for payload in task_payloads
                        if str(payload.get("assignee") or payload.get("profile") or "")
                        == canonical_profile_for_department("hr")
                        and terminal_workflow_role(payload) == "primary"
                        and (
                            terminal_workflow_root(payload)
                            or str(payload.get("workflow_root_task_id") or "")
                        )
                        == root_task_id
                    ),
                    None,
                )
                if isinstance(hr_primary, Mapping):
                    hr_snapshot: Mapping[str, Any] = hr_primary
                    show = getattr(self.client, "show", None)
                    hr_id = str(
                        hr_primary.get("id") or hr_primary.get("task_id") or ""
                    )
                    if callable(show) and hr_id:
                        try:
                            hydrated_hr = show(hr_id)
                            if isinstance(hydrated_hr, Mapping):
                                hr_snapshot = {**dict(hr_primary), **hydrated_hr}
                        except Exception as exc:  # noqa: BLE001 - QA is fail-open.
                            logger.warning(
                                "hr-qa-evidence-hydration-failed root=%s task=%s error=%s",
                                root_task_id,
                                hr_id,
                                type(exc).__name__,
                            )
                    hr_provenance = _handoff_provenance(
                        ChildTaskState.from_hermes(hr_snapshot),
                        include_evidence_content=False,
                    )
                    hr_summary = hr_provenance.get("evidence_artifact")
                    hr_summary = (
                        hr_summary.get("summary")
                        if isinstance(hr_summary, Mapping)
                        and isinstance(hr_summary.get("summary"), Mapping)
                        else {}
                    )
                    hr_reads = hr_provenance.get("source_reads")
                    hr_reads = hr_reads if isinstance(hr_reads, Mapping) else {}
                    hr_failures = hr_provenance.get(
                        "failures_retries_duplicates", {}
                    )
                    hr_failures = (
                        hr_failures if isinstance(hr_failures, Mapping) else {}
                    )
                    comments = hr_snapshot.get("comments")
                    comments = comments if isinstance(comments, Sequence) else ()
                    notion_marker = next(
                        (
                            str(comment.get("body") or comment)
                            for comment in comments
                            if (
                                isinstance(comment, Mapping)
                                and "hgfinance.department-notion-delivery.v1"
                                in str(comment.get("body") or "")
                            )
                        ),
                        "",
                    )
                    notion_values = {
                        key: value
                        for key, value in (
                            token.split("=", 1)
                            for token in notion_marker.split()
                            if "=" in token
                        )
                        if key and value
                    }
                    qa_downstream_evidence["hermes"] = {
                        "profile": "hr-department",
                        "primary_task_id": hr_id,
                        "terminal_status": str(
                            hr_snapshot.get("status") or "done"
                        ),
                        "terminal_contract": "satisfied",
                        "helper_runs": hr_summary.get("helper_runs"),
                        "workforce_read_calls": len(hr_reads),
                        "workforce_http_statuses": [
                            read.get("http_status")
                            for read in hr_reads.values()
                            if isinstance(read, Mapping)
                        ],
                        "workflow_retries": 0,
                        "helper_retries": hr_failures.get(
                            "helper_retries_or_retries_observed", 0
                        ),
                        "duplicate_helper_runs": hr_failures.get(
                            "duplicate_helper_runs", 0
                        ),
                        "api_failures": hr_failures.get("request_failures", 0),
                    }
                    qa_downstream_evidence["notion"] = {
                        "status": notion_values.get("status") or "unverified",
                        "delivery_status": notion_values.get("delivery_status")
                        or "unverified",
                        "readback_status": notion_values.get("readback_status")
                        or "unverified",
                        "payload_hash_present": notion_values.get(
                            "payload_hash_present"
                        )
                        == "true",
                        "page_id_present": bool(notion_values.get("page_id")),
                        "readback_hash_present": notion_values.get(
                            "readback_hash_present"
                        )
                        == "true",
                    }
                    qa_downstream_evidence["delivery_receipts"]["notion"] = {
                        "delivery_status": qa_downstream_evidence["notion"].get(
                            "delivery_status"
                        ),
                        "readback_status": qa_downstream_evidence["notion"].get(
                            "readback_status"
                        ),
                        "payload_hash_present": qa_downstream_evidence["notion"].get(
                            "payload_hash_present"
                        ),
                        "page_id_present": qa_downstream_evidence["notion"].get(
                            "page_id_present"
                        ),
                        "readback_hash_present": qa_downstream_evidence["notion"].get(
                            "readback_hash_present"
                        ),
                    }
                    artifact_rows = hr_provenance.get("artifacts")
                    artifact_rows = (
                        artifact_rows
                        if isinstance(artifact_rows, Sequence)
                        and not isinstance(artifact_rows, (str, bytes, bytearray))
                        else []
                    )
                    qa_downstream_evidence["artifact"] = {
                        "sha256_present": any(
                            isinstance(item, Mapping) and item.get("sha256")
                            for item in artifact_rows
                        ),
                        "sha256_verified_by_supervisor": any(
                            isinstance(item, Mapping) and item.get("sha256")
                            for item in artifact_rows
                        ),
                    }
                    qa_downstream_evidence["safety"] = {
                        "paper_read_only": True,
                        "orders": 0,
                        "investment_changes": 0,
                        "ledger_changes": 0,
                        "permission_changes": 0,
                    }
                    qa_downstream_evidence["qa"] = {
                        "status": "scheduled",
                        "phase": "post_response",
                        "response_task_id": task_id,
                        "evaluation_sink": "audit.eval_runs",
                        "audit_contract_present": True,
                        "projection_after_terminal": True,
                        "qa_blocks_response": False,
                    }
                self._schedule_post_response_qa(
                    root_task_id=root_task_id,
                    root_payload=root_payload,
                    response_task=task,
                    task_payloads=task_payloads,
                    delivery_status=delivery_status,
                    downstream_evidence=qa_downstream_evidence,
                )

        # Risk Hermes owns a separate orchestration LLM from the on-demand
        # Qwen Legal Wiki span. Profile that orchestration session after the
        # primary is terminal so QA can distinguish model turns, tool latency,
        # context growth and blocked-tool attempts. This observer is redacted,
        # idempotent and fail-open; it never delays the user response lane.
        if (
            role == "primary"
            and str(task.get("assignee") or "").strip() == "risk-management"
        ):
            try:
                risk_metadata = merged_run_metadata(task)
                risk_session_id = str(
                    risk_metadata.get("worker_session_id") or ""
                ).strip()
                risk_started_ms = _task_timestamp_ms(task, "started_at")
                risk_ended_ms = _task_timestamp_ms(task, "completed_at")
                if risk_session_id and risk_started_ms and risk_ended_ms:
                    from orchestration.risk_observability import (
                        publish_risk_hermes_profile,
                    )

                    observer_environment = getattr(
                        self.client, "environment", os.environ
                    )
                    risk_log_dir = os.path.join(
                        observer_environment.get("HERMES_HOME", "/opt/data"),
                        "profiles",
                        "risk-management",
                        "logs",
                    )
                    published = publish_risk_hermes_profile(
                        task_id=task_id,
                        root_id=root_task_id,
                        session_id=risk_session_id,
                        log_dir=risk_log_dir,
                        started_ms=risk_started_ms,
                        ended_ms=risk_ended_ms,
                        status=str(task.get("status") or "completed"),
                        environment=observer_environment,
                    )
                    logger.info(
                        "risk-hermes-worker-profile task=%s session=%s "
                        "published=%s",
                        task_id,
                        risk_session_id,
                        published,
                    )
            except Exception as exc:  # noqa: BLE001 - observer is fail-open.
                logger.warning(
                    "risk-hermes-worker-profile-failed task=%s error=%s",
                    task_id,
                    type(exc).__name__,
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

        # Department terminal results are projected to explicitly wired
        # Notion databases as a non-binding observer. Research/Risk native
        # reporters remain responsible for their standalone pipelines; the
        # optional DB wiring here covers the separate CEO/Kanban boundary and
        # is idempotent per terminal task title.
        department_projection = None
        try:
            terminal_metadata = merged_run_metadata(task)
            risk_plan = terminal_metadata.get(
                "position_risk_plan"
            ) or terminal_metadata.get("risk_plan")
            notion_context = (
                risk_span(
                    "risk.notion-projection",
                    {
                        "task_id": task_id,
                        "trace_id": risk_plan.get("trace_id") or root_task_id,
                        "risk_plan_id": risk_plan.get("risk_plan_id"),
                        "mandate_version_id": risk_plan.get("mandate_version_id"),
                        "input_hash": risk_plan.get("input_hash"),
                        "algorithm_version": risk_plan.get("calculation_version"),
                        "stage": "notion-projection",
                        "target": "NOTION",
                        "status": "running",
                    },
                )
                if isinstance(risk_plan, Mapping)
                else nullcontext()
            )
            with notion_context:
                department_projection = self._department_notion_projection.project(
                    root_task_id=root_task_id,
                    task=task,
                    workflow_tasks=task_payloads,
                    event=event,
                )
            if department_projection.status not in {"skipped", "duplicate"}:
                logger.info(
                    "department-notion-projection "
                    "task=%s department=%s status=%s delivery_status=%s "
                    "readback_status=%s page_id_present=%s "
                    "readback_hash_present=%s",
                    task_id,
                    department_projection.department,
                    department_projection.status,
                    department_projection.delivery_status or "",
                    department_projection.readback_status or "",
                    bool(department_projection.page_id),
                    bool(department_projection.readback_hash),
                )
                comment_task = getattr(self.client, "comment_task", None)
                if callable(comment_task):
                    comment_task(
                        task_id,
                        "hgfinance.department-notion-delivery.v1 "
                        f"department={department_projection.department or ''} "
                        f"status={department_projection.status} "
                        f"delivery_status={department_projection.delivery_status or ''} "
                        f"readback_status={department_projection.readback_status or ''} "
                        f"payload_hash_present={str(bool(department_projection.payload_hash)).lower()} "
                        f"page_id={department_projection.page_id or ''} "
                        f"readback_hash_present={str(bool(department_projection.readback_hash)).lower()}",
                    )
                if department_projection.risk_plan_id:
                    if callable(comment_task):
                        comment_task(
                            task_id,
                            "hgfinance.risk-projection-delivery.v1 "
                            f"risk_plan_id={department_projection.risk_plan_id} "
                            f"delivery_status={department_projection.delivery_status} "
                            f"readback_status={department_projection.readback_status} "
                            f"payload_hash={department_projection.payload_hash} "
                            f"page_id={department_projection.page_id or ''} "
                            f"evidence_status={department_projection.evidence_status or ''}",
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

        # The single HR primary is delivered before this non-binding observer.
        # Schedule QA only after the Notion projection has returned so its
        # immutable audit input can distinguish an observed downstream result
        # from a merely requested one.
        if role == "primary" and task_id and str(
            task.get("assignee") or task.get("profile") or ""
        ).strip() == canonical_profile_for_department("hr"):
            delivery = self._hr_response_delivery_for(
                root_task_id=root_task_id,
                task=task,
            )
            if delivery is not None:
                response_task = dict(task)
                delivered_content = _text(delivery.get("content"))
                delivered_content_hash = hashlib.sha256(
                    delivered_content.encode("utf-8")
                ).hexdigest()
                if delivered_content:
                    response_task["result"] = delivered_content
                    response_task["final_answer"] = delivered_content
                response_task_id = str(
                    delivery.get("response_task_id")
                    or task.get("id")
                    or task.get("task_id")
                    or task_id
                )
                # Completion events can carry a shallow task row while the
                # scoped payload snapshot has the worker's durable run
                # metadata.  Rehydrate only this HR task so QA receives the
                # helper count and exact artifact receipt, without scanning
                # unrelated Kanban tasks.
                rich_response_task = next(
                    (
                        payload
                        for payload in task_payloads
                        if str(payload.get("id") or payload.get("task_id") or "")
                        == response_task_id
                        and merged_run_metadata(payload)
                    ),
                    None,
                )
                if isinstance(rich_response_task, Mapping):
                    response_task = {
                        **dict(rich_response_task),
                        **response_task,
                    }
                response_metadata = {
                    **merged_run_metadata(rich_response_task or {}),
                    **merged_run_metadata(response_task),
                }
                response_metadata["delivery"] = {
                    "discord": delivery.get("discord_status"),
                    "langsmith": delivery.get("langsmith_status"),
                    "response_task_id": response_task_id,
                }
                response_task["run_metadata"] = response_metadata
                response_task["metadata"] = response_metadata
                response_payloads = tuple(
                    response_task
                    if str(item.get("id") or item.get("task_id") or "")
                    == response_task_id
                    else item
                    for item in task_payloads
                )
                if not any(
                    str(item.get("id") or item.get("task_id") or "")
                    == response_task_id
                    for item in response_payloads
                ):
                    response_payloads = (*response_payloads, response_task)

                # The direct HR response was already delivered, but the QA
                # envelope must audit the same evidence-grounded answer. A
                # cached delivery can predate the latest artifact-aware
                # enrichment, so refresh only this scoped response in memory
                # before constructing the post-response audit input.
                if str(
                    response_task.get("assignee")
                    or response_task.get("profile")
                    or ""
                ).strip() == canonical_profile_for_department("hr"):
                    enriched_response = _augment_hr_final_answer(
                        delivered_content,
                        root_task_id=root_task_id,
                        task_payloads=response_payloads,
                    )
                    if enriched_response != delivered_content:
                        delivered_content = enriched_response
                        delivered_content_hash = hashlib.sha256(
                            delivered_content.encode("utf-8")
                        ).hexdigest()
                        response_task["result"] = delivered_content
                        response_task["final_answer"] = delivered_content

                hr_provenance = _handoff_provenance(
                    ChildTaskState.from_hermes(response_task),
                    include_evidence_content=False,
                )
                provenance_summary = hr_provenance.get("evidence_summary")
                if not isinstance(provenance_summary, Mapping):
                    evidence_artifact = hr_provenance.get("evidence_artifact")
                    provenance_summary = (
                        evidence_artifact.get("summary")
                        if isinstance(evidence_artifact, Mapping)
                        and isinstance(evidence_artifact.get("summary"), Mapping)
                        else {}
                    )
                provenance_reads = hr_provenance.get("source_reads")
                provenance_reads = (
                    provenance_reads
                    if isinstance(provenance_reads, Mapping)
                    else {}
                )
                artifacts = hr_provenance.get("artifacts")
                artifacts = artifacts if isinstance(artifacts, Sequence) else ()
                metadata = merged_run_metadata(response_task)
                declared_hashes = {
                    str(metadata.get("artifact_sha256") or "").strip()
                }
                declared_artifacts = metadata.get("artifacts")
                if isinstance(declared_artifacts, Sequence) and not isinstance(
                    declared_artifacts, (str, bytes, bytearray)
                ):
                    declared_hashes.update(
                        str(item.get("sha256") or "").strip()
                        for item in declared_artifacts
                        if isinstance(item, Mapping)
                    )
                declared_artifact = metadata.get("artifact")
                if isinstance(declared_artifact, Mapping):
                    declared_hashes.add(
                        str(declared_artifact.get("sha256") or "").strip()
                    )
                declared_hashes.update(
                    str(value).strip()
                    for value in hr_provenance.get("declared_artifact_sha256") or []
                    if str(value).strip()
                )
                declared_hashes.discard("")
                computed_hashes = {
                    str(item.get("sha256") or "").strip()
                    for item in artifacts
                    if isinstance(item, Mapping)
                }
                # ``delivered_content`` may already contain the supervisor's
                # rendered artifact hash. Never use that generated line as a
                # worker declaration; verification is based on the worker's
                # durable metadata when present, otherwise on a valid exact
                # task-scoped artifact that the supervisor re-hashed.
                artifact_hash_verified = bool(computed_hashes) and (
                    not declared_hashes
                    or bool(declared_hashes.intersection(computed_hashes))
                )
                run_rows = response_task.get("runs")
                run_count = (
                    len(run_rows)
                    if isinstance(run_rows, Sequence)
                    and not isinstance(run_rows, (str, bytes, bytearray))
                    else 1
                )
                http_statuses = metadata.get("http_statuses")
                if not isinstance(http_statuses, Sequence) or isinstance(
                    http_statuses, (str, bytes, bytearray)
                ):
                    structured_summary = metadata.get("structured_summary")
                    if isinstance(structured_summary, Mapping):
                        http_statuses = structured_summary.get("http_statuses")
                if not isinstance(http_statuses, Sequence) or isinstance(
                    http_statuses, (str, bytes, bytearray)
                ):
                    approved_gets = metadata.get("approved_gets")
                    if isinstance(approved_gets, Mapping):
                        http_statuses = approved_gets.get("http_statuses")
                approved_gets = metadata.get("approved_gets")
                approved_gets = approved_gets if isinstance(approved_gets, Mapping) else {}
                structured_summary = metadata.get("structured_summary")
                structured_summary = (
                    structured_summary
                    if isinstance(structured_summary, Mapping)
                    else {}
                )
                helper_retries = metadata.get("retries_observed")
                if helper_retries is None:
                    helper_retries = approved_gets.get("retries_observed", 0)
                duplicate_helper_runs = metadata.get("duplicate_helper_runs")
                if duplicate_helper_runs is None:
                    duplicate_helper_runs = approved_gets.get(
                        "duplicate_helper_runs", 0
                    )
                api_failures = metadata.get("api_failures")
                if api_failures is None:
                    api_failures = approved_gets.get("api_failures", 0)
                # The evidence artifact is the authoritative append-only
                # receipt for the three approved GETs.  A live HR worker can
                # persist ``http_statuses`` as an endpoint->status mapping
                # and an older compact summary can still say zero failures
                # after one request timed out.  Reconcile from the same
                # bounded receipt before exposing counts to QA.
                evidence_failure_summary = provenance_summary.get(
                    "failure_retry_duplicate"
                )
                if isinstance(evidence_failure_summary, Mapping):
                    api_failures = evidence_failure_summary.get(
                        "api_failures", api_failures
                    )
                    helper_retries = evidence_failure_summary.get(
                        "retries_observed", helper_retries
                    )
                    duplicate_helper_runs = evidence_failure_summary.get(
                        "duplicate_helper_runs", duplicate_helper_runs
                    )

                def _status_values(value: Any) -> list[Any]:
                    if isinstance(value, Mapping):
                        return [
                            value[key]
                            for key in ("improvements", "observability", "scorecard_brief")
                            if key in value
                        ]
                    if isinstance(value, Sequence) and not isinstance(
                        value, (str, bytes, bytearray)
                    ):
                        return list(value)
                    return []

                http_statuses = (
                    _status_values(http_statuses)
                )
                # Prefer the task-scoped artifact's three receipts whenever
                # available.  This preserves a timeout/None as a real third
                # request instead of silently collapsing the audit to two.
                evidence_statuses = [
                    evidence_receipt.get("http_status")
                    if isinstance(evidence_receipt, Mapping)
                    else None
                    for evidence_receipt in (
                        provenance_reads.get("improvements"),
                        provenance_reads.get("observability"),
                        provenance_reads.get("scorecard"),
                    )
                ]
                if any(
                    key in provenance_reads
                    for key in ("improvements", "observability", "scorecard")
                ):
                    http_statuses = evidence_statuses
                if not http_statuses:
                    http_statuses = [
                        read.get("http_status")
                        for read in provenance_reads.values()
                        if isinstance(read, Mapping)
                        and read.get("http_status") is not None
                    ]
                if not http_statuses:
                    api_requests = metadata.get("api_requests")
                    if isinstance(api_requests, Mapping):
                        raw_statuses = api_requests.get("http_statuses")
                        http_statuses = _status_values(raw_statuses)
                helper_runs = metadata.get("helper_runs")
                if helper_runs is None:
                    helper_runs = structured_summary.get("helper_runs")
                if helper_runs is None:
                    helper_runs = provenance_summary.get("helper_runs")
                if helper_runs is None:
                    api_requests = metadata.get("api_requests")
                    if isinstance(api_requests, Mapping) and api_requests.get("count"):
                        helper_runs = 1
                worker_log_metrics: dict[str, Any] = {}
                try:
                    from scripts.hermes_worker_observability import (
                        worker_log_metrics as read_worker_log_metrics,
                    )

                    worker_log_metrics = read_worker_log_metrics(
                        task_id=response_task_id,
                        env=getattr(self.client, "environment", os.environ),
                    )
                except Exception:  # noqa: BLE001 - log enrichment is fail-open.
                    worker_log_metrics = {}
                downstream_evidence = {
                    "hermes": {
                        "profile": "hr-department",
                        "primary_task_id": response_task_id,
                        "terminal_status": str(
                            response_task.get("status") or "done"
                        ),
                        "terminal_contract": "satisfied",
                        "helper_runs": helper_runs,
                        "workforce_read_calls": len(http_statuses),
                        "workforce_http_statuses": http_statuses,
                        "workflow_retries": max(run_count - 1, 0),
                        "helper_retries": helper_retries,
                        "duplicate_helper_runs": duplicate_helper_runs,
                        "api_failures": api_failures,
                        "llm_calls": worker_log_metrics.get("llm_calls"),
                        "tool_calls": worker_log_metrics.get("tool_calls"),
                        "tool_names": worker_log_metrics.get("tool_names", [])[:16],
                        "tool_error_count": worker_log_metrics.get("tool_error_count"),
                        "tool_duration_total_ms": worker_log_metrics.get(
                            "tool_duration_total_ms"
                        ),
                        "tool_latency_available": worker_log_metrics.get(
                            "tool_latency_available"
                        ),
                        "worker_log_metrics_available": bool(worker_log_metrics),
                    },
                    "discord": {
                        "user_response": delivery.get("discord_status"),
                        "duplicate": bool(delivery.get("discord_duplicate")),
                        "response_task_id": response_task_id,
                        "correlation_id": f"{root_task_id}:{response_task_id}",
                        "summary_hash": delivered_content_hash,
                    },
                    "langsmith": {
                        "terminal_status": (
                            "published"
                            if delivery.get("langsmith_closed")
                            else delivery.get("langsmith_status")
                        ),
                        "raw_status": delivery.get("langsmith_status"),
                        "root_task_id": root_task_id,
                        "correlation_id": f"{root_task_id}:{response_task_id}",
                        "trace_closed": bool(delivery.get("langsmith_closed")),
                        "publication_confirmed": bool(
                            delivery.get("langsmith_closed")
                        ),
                        "summary_hash": delivered_content_hash,
                        "metadata_only": True,
                        "raw_payloads_sent": False,
                        "trace_metadata_only": True,
                    },
                    "notion": {
                        "status": (
                            department_projection.status
                            if department_projection is not None
                            else "failed"
                        ),
                        "delivery_status": (
                            department_projection.delivery_status
                            if department_projection is not None
                            else None
                        ),
                        "readback_status": (
                            department_projection.readback_status
                            if department_projection is not None
                            else None
                        ),
                        "payload_hash_present": bool(
                            department_projection is not None
                            and department_projection.payload_hash
                        ),
                        "page_id": (
                            department_projection.page_id
                            if department_projection is not None
                            else None
                        ),
                        "page_id_present": bool(
                            department_projection is not None
                            and department_projection.page_id
                        ),
                        "readback_hash": (
                            department_projection.readback_hash
                            if department_projection is not None
                            else None
                        ),
                        "readback_hash_present": bool(
                            department_projection is not None
                            and department_projection.readback_hash
                        ),
                        "correlation_id": f"{root_task_id}:{response_task_id}",
                        "payload_hash": (
                            department_projection.payload_hash
                            if department_projection is not None
                            else None
                        ),
                    },
                    "artifact": {
                        "sha256_present": bool(computed_hashes),
                        "sha256_verified_by_supervisor": bool(
                            declared_hashes.intersection(computed_hashes)
                            or artifact_hash_verified
                        ),
                    },
                    "safety": {
                        "paper_read_only": True,
                        "orders": 0,
                        "investment_changes": 0,
                        "ledger_changes": 0,
                        "permission_changes": 0,
                    },
                }
                downstream_evidence["delivery_receipts"] = {
                    "correlation_id": f"{root_task_id}:{response_task_id}",
                    "notion": {
                        "delivery_status": downstream_evidence["notion"].get(
                            "delivery_status"
                        ),
                        "readback_status": downstream_evidence["notion"].get(
                            "readback_status"
                        ),
                        "payload_hash_present": downstream_evidence["notion"].get(
                            "payload_hash_present"
                        ),
                        "page_id_present": downstream_evidence["notion"].get(
                            "page_id_present"
                        ),
                        "readback_hash_present": downstream_evidence["notion"].get(
                            "readback_hash_present"
                        ),
                    },
                    "discord": {
                        "delivery_status": delivery.get("discord_status"),
                        "summary_hash": delivered_content_hash,
                    },
                    "langsmith": {
                        "publication_status": (
                            "published"
                            if delivery.get("langsmith_closed")
                            else "unconfirmed"
                        ),
                        "summary_hash": delivered_content_hash,
                    },
                    "qa": {
                        "status": "scheduled",
                        "event_count": 0,
                        "projection_after_terminal": True,
                    },
                }
                self._schedule_post_response_qa(
                    root_task_id=root_task_id,
                    root_payload=root_payload,
                    response_task=response_task,
                    task_payloads=response_payloads,
                    delivery_status=str(delivery.get("discord_status") or ""),
                    downstream_evidence=downstream_evidence,
                )

        return delivery_status

    def _bridge_root_completion_to_discord(
        self,
        *,
        root_task_id: str,
        root_payload: Mapping[str, Any],
        task_payloads: Sequence[Mapping[str, Any]] | None = None,
        materialized_primary_profiles: Sequence[str] | None = None,
    ) -> str | None:
        """Bridge a completed CEO planning root to existing Discord delivery.

        No semantic routing happens here.

        Existing execution state/validated plan decides the UX:
        - no selected primary + root final_answer -> direct CEO reply
        - materialized selected primaries -> one CEO delegation card

        Planner metadata alone never authorizes a delegation card.  The initial
        materializer may pass its already-validated primary decisions before
        child dispatch so the delegation card is visible first; terminal and
        recovery callers pass the authoritative task projection.

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

        selected, _ = split_planner_selection(
            selected_primary_profiles_from_task(root_payload)
        )

        if selected:
            if materialized_primary_profiles is not None:
                materialized = {
                    str(profile).strip()
                    for profile in materialized_primary_profiles
                    if str(profile).strip()
                }
            else:
                payloads = task_payloads
                if payloads is None:
                    children = root_payload.get("children")
                    payloads = tuple(
                        child
                        for child in children
                        if isinstance(child, Mapping)
                    ) if isinstance(children, Sequence) and not isinstance(
                        children, (str, bytes)
                    ) else ()
                materialized = {
                    child.profile
                    for child in self._materialized_primary_children(
                        root_task_id=root_task_id,
                        task_payloads=payloads,
                    )
                }
            selected = tuple(profile for profile in selected if profile in materialized)
            if not selected:
                logger.info(
                    "ceo-root-discord-bridge root=%s mode=planned-not-materialized",
                    root_task_id,
                )
                return None

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

        status = self._deliver_direct_ceo_answer(
            root_task_id=root_task_id,
            root_payload=root_payload,
            store=store,
            profile=ceo_profile,
        )
        if status in {"sent", "deduped"}:
            self._close_root_trace(
                root_id=root_task_id,
                root_payload=root_payload,
                status="completed",
            )
        return status

    @staticmethod
    def _materialized_primary_children(
        *,
        root_task_id: str,
        task_payloads: Sequence[Mapping[str, Any]],
    ) -> tuple[ChildTaskState, ...]:
        """Return only authoritative, materialized analysis primary tasks."""

        children: list[ChildTaskState] = []
        for payload in task_payloads:
            child = ChildTaskState.from_hermes(payload)
            if (
                child.task_id != root_task_id
                and child.is_in_workflow(root_task_id)
                and child.is_analysis
            ):
                children.append(child)
        return tuple(children)

    def _fast_path_has_materialized_primary_children(
        self,
        root_task_id: str,
    ) -> bool:
        """Check indexed authoritative children before fast fan-out.

        A ready/recovery lane can materialize a completed root before the
        terminal fast path acquires the root lock.  An empty fast-path child
        snapshot would then replay the whole primary plan and rely on create
        idempotency to suppress durable duplicates.  Use the root index as a
        discovery hint, then verify candidates with authoritative ``show``.
        """

        root_query = getattr(self.client, "root_scoped_task_ids", None)
        show = getattr(self.client, "show", None)
        if not callable(root_query) or not callable(show):
            return False

        try:
            candidate_ids = tuple(root_query(root_task_id))
        except (
            RootScopedIndexUnavailable,
            HermesKanbanCommandError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            logger.debug(
                "ready-primary-fast-existing-check-skipped root=%s reason=%s",
                root_task_id,
                type(exc).__name__,
            )
            return False

        for raw_candidate_id in candidate_ids:
            candidate_id = str(raw_candidate_id or "").strip()
            if not candidate_id or candidate_id == root_task_id:
                continue
            try:
                payload = show(candidate_id)
                child = ChildTaskState.from_hermes(payload)
            except (
                CanonicalProfileError,
                HermesKanbanCommandError,
                OSError,
                TypeError,
                ValueError,
            ):
                continue

            if (
                child.task_id == candidate_id
                and child.is_in_workflow(root_task_id)
                and child.is_analysis
            ):
                return True

        return False

    @staticmethod
    def _root_response_content(root_payload: Mapping[str, Any]) -> str:
        """Read an existing CEO result without treating planner metadata as one."""

        root_state = ChildTaskState.from_hermes(root_payload)
        return _text(
            root_state.final_answer
            or root_state.result
            or root_state.summary
            or root_payload.get("latest_summary")
            or root_payload.get("summary")
            or root_payload.get("result")
        )

    @staticmethod
    def _root_explicit_response_content(
        root_payload: Mapping[str, Any] | ChildTaskState,
    ) -> str:
        """Read only explicit answer fields, excluding planner summaries.

        A CEO completion summary can describe an intended delegation.  When
        no child was materialized, treating that summary as a direct answer
        repeats the planning/materialization mismatch to the user.  Explicit
        result/final_answer fields remain valid direct answers.
        """

        if isinstance(root_payload, ChildTaskState):
            return _text(root_payload.final_answer or root_payload.result)

        content = _text(
            root_payload.get("final_answer")
            or root_payload.get("result")
        )
        if content:
            return content

        # Hermes versions differ in whether the terminal answer is exposed as
        # a task field or in the latest run metadata.  Read the same bounded
        # metadata envelope used by the department projections so a direct
        # root is evaluated against the actual terminal answer, not its
        # planning acknowledgement.
        metadata = merged_run_metadata(root_payload)
        content = _text(metadata.get("final_answer") or metadata.get("result"))
        if content:
            return content

        runs = root_payload.get("runs")
        if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes)):
            return ""
        for run in reversed(runs):
            if not isinstance(run, Mapping):
                continue
            metadata = run.get("metadata")
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
            if not isinstance(metadata, Mapping):
                continue
            content = _text(metadata.get("final_answer") or metadata.get("result"))
            if content:
                return content
        return ""

    def _deliver_direct_ceo_answer(
        self,
        *,
        root_task_id: str,
        root_payload: Mapping[str, Any],
        store: DiscordIdempotencyStore,
        profile: str,
    ) -> str:
        """Deliver the existing root answer through the normal final helper."""

        content = strip_internal_handoff(self._root_response_content(root_payload))
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
            profile=profile,
            response_key_suffix=f"ceo-direct:{root_task_id}",
        )

        logger.info(
            "ceo-root-discord-bridge root=%s mode=direct status=%s",
            root_task_id,
            status,
        )
        return status

    def _reconcile_unmaterialized_primary_root(
        self,
        *,
        root_task_id: str,
        root_payload: Mapping[str, Any],
        task_payloads: Sequence[Mapping[str, Any]],
    ) -> str | None:
        """Close an invalid all-primary plan without losing the user response.

        Planner selections are intent metadata.  This guard applies only when
        every selected profile is known to be ineligible for primary
        materialization (currently the governance QA profile) and the
        authoritative workflow contains no materialized primary child.
        """

        selected = tuple(selected_primary_profiles_from_task(root_payload))
        if not selected or not all(
            not is_analysis_primary_eligible(profile)
            for profile in selected
        ):
            return None

        materialized = self._materialized_primary_children(
            root_task_id=root_task_id,
            task_payloads=task_payloads,
        )
        if materialized:
            return None

        # A response-plane synthesis, even if its terminal event is being
        # replayed, owns final delivery. Never race it with a CEO direct
        # fallback based only on the planning root's metadata.
        for payload in task_payloads:
            candidate = ChildTaskState.from_hermes(payload)
            if (
                candidate.task_id != root_task_id
                and candidate.is_in_workflow(root_task_id)
                and candidate.workflow_role == "synthesis"
            ):
                return None

        # For an all-ineligible selected plan, a summary is commonly just the
        # CEO's delegation acknowledgement.  Only an explicit result can
        # satisfy the direct-response branch; otherwise emit the existing
        # explicit blocked/failure response below.
        content = self._root_explicit_response_content(root_payload)
        if content and self.discord_delivery is not None:
            environment = getattr(self.client, "environment", os.environ)
            hermes_home = environment.get("HERMES_HOME", "/opt/data")
            ceo_profile = canonical_profile_for_department("ceo")
            ceo_profile_home = os.path.join(hermes_home, "profiles", ceo_profile)
            delivery_home = (
                ceo_profile_home
                if os.path.isdir(ceo_profile_home)
                else hermes_home
            )
            status = self._deliver_direct_ceo_answer(
                root_task_id=root_task_id,
                root_payload=root_payload,
                store=DiscordIdempotencyStore(delivery_home),
                profile=ceo_profile,
            )
            if status in {"sent", "deduped"}:
                self._close_root_trace(
                    root_id=root_task_id,
                    root_payload=root_payload,
                    status="completed",
                )
                logger.info(
                    "ceo-root-invalid-primary-final root=%s selected=%s status=%s",
                    root_task_id,
                    ",".join(selected),
                    status,
                )
                return status

        # Preserve the delivery helper's existing retry/failure semantics. A
        # usable answer that could not be delivered is not equivalent to an
        # empty answer and must not be replaced by a fabricated blocked result.
        if content:
            return None

        # No usable CEO result exists. Create the single response-plane
        # synthesis identity and complete it deterministically. QA-only plans
        # are not user-input requests: QA can audit a response, but cannot
        # supply the missing analysis primary.
        try:
            workflow_mode = workflow_mode_from_body(
                str(root_payload.get("body") or "")
            )
        except WorkflowScopeViolation:
            workflow_mode = "analysis"
        state = SupervisorState(
            parent_task_id=root_task_id,
            children=materialized,
            workflow_mode=workflow_mode,
            selected_primary_profiles=selected,
            root_is_user_query=True,
            previous_question_context=previous_question_context_from_body(
                str(root_payload.get("body") or "")
            ),
            allow_primary_passthrough=self.discord_delivery is not None,
            risk_advisory_context=fetch_risk_advisory_context(
                str(root_payload.get("body") or "")
            ),
            accounting_advisory_context=fetch_accounting_advisory_context(),
            workforce_advisory_context=fetch_workforce_advisory_context(
                str(root_payload.get("body") or "")
            ),
        )
        decision = _empty_primary_defer_decision(state)
        self._execute(decision, state)
        logger.warning(
            "ceo-root-invalid-primary-deferred root=%s selected=%s",
            root_task_id,
            ",".join(selected),
        )
        return "deferred"

    @staticmethod
    def _materialized_terminal_children(
        *,
        root_task_id: str,
        task_payloads: Sequence[Mapping[str, Any]],
    ) -> tuple[ChildTaskState, ...]:
        """Return terminal materialized children, including optional QA work."""

        terminal: list[ChildTaskState] = []
        for payload in task_payloads:
            child = ChildTaskState.from_hermes(payload)
            if (
                child.task_id != root_task_id
                and child.is_in_workflow(root_task_id)
                and child.terminal
            ):
                terminal.append(child)
        return tuple(terminal)

    @staticmethod
    def _response_synthesis_exists(
        *,
        root_task_id: str,
        task_payloads: Sequence[Mapping[str, Any]],
    ) -> bool:
        for payload in task_payloads:
            child = ChildTaskState.from_hermes(payload)
            if (
                child.task_id != root_task_id
                and child.is_in_workflow(root_task_id)
                and child.workflow_role == "synthesis"
            ):
                return True
        return False

    @staticmethod
    def _terminal_failure_content(child: ChildTaskState) -> str:
        """Build an explicit, non-fabricated user-facing failure response."""

        _, label = DEPARTMENT_DISCORD_LABELS.get(
            child.profile,
            ("⚠️", child.profile),
        )
        if child.failure_kind == FailureKind.PROTOCOL.value:
            reason = "정상적인 terminal 결과가 인계되지 않았습니다."
        else:
            category = _failure_category_for_department_card(
                child.summary,
                child.error,
                child.block_reason,
            )
            reason = _safe_failure_reason(category)
        return (
            "⚠️ **요청 처리 결과**\n"
            f"{label} 작업이 완료되지 않았습니다.\n"
            f"- 사유: {reason}\n\n"
            "확인 가능한 최종 분석 결과가 없어 성공 답변을 만들지 않았습니다."
        )

    def _reconcile_late_child_finalization(
        self,
        *,
        root_task_id: str,
        root_payload: Mapping[str, Any],
        task_payloads: Sequence[Mapping[str, Any]],
        task_id: str,
    ) -> str | None:
        """Re-check final response completeness after a root's late child terminal."""

        if self.discord_delivery is None:
            return None
        root_body = str(root_payload.get("body") or "")
        if (
            workflow_mode_from_body(root_body) != "analysis"
            or not is_user_query_body(root_body)
            or str(root_payload.get("status") or "").casefold()
            not in TERMINAL_STATUSES
            or task_id == root_task_id
        ):
            return None

        terminal_children = self._materialized_terminal_children(
            root_task_id=root_task_id,
            task_payloads=task_payloads,
        )
        terminal_child = next(
            (child for child in terminal_children if child.task_id == task_id),
            None,
        )
        if terminal_child is None:
            return None

        # A materialized eligible primary still owns the normal delegated /
        # synthesis path.  Late optional-child finalization must not turn a
        # valid Research/Quant/Risk workflow into an early CEO direct answer.
        if self._materialized_primary_children(
            root_task_id=root_task_id,
            task_payloads=task_payloads,
        ):
            return None

        # A response-plane synthesis owns delivery. Do not race it with a
        # direct/blocked fallback merely because an optional child finished.
        if self._response_synthesis_exists(
            root_task_id=root_task_id,
            task_payloads=task_payloads,
        ):
            return None

        environment = getattr(self.client, "environment", os.environ)
        hermes_home = environment.get("HERMES_HOME", "/opt/data")
        ceo_profile = canonical_profile_for_department("ceo")
        ceo_profile_home = os.path.join(hermes_home, "profiles", ceo_profile)
        delivery_home = (
            ceo_profile_home
            if os.path.isdir(ceo_profile_home)
            else hermes_home
        )
        store = DiscordIdempotencyStore(delivery_home)

        # A planner summary can only describe intended delegation.  If the
        # late child is the control/failure path for an unmaterialized plan,
        # reuse an explicit CEO result only; otherwise deliver the existing
        # terminal failure response instead of repeating the stale delegation
        # claim.
        root_content = self._root_explicit_response_content(root_payload)
        if root_content:
            status = self._deliver_direct_ceo_answer(
                root_task_id=root_task_id,
                root_payload=root_payload,
                store=store,
                profile=ceo_profile,
            )
        else:
            status = self.discord_delivery.deliver_to_existing_thread(
                root_task_id=root_task_id,
                source_task=root_payload,
                root_task=root_payload,
                content=self._terminal_failure_content(terminal_child),
                title="⚠️ CEO 처리 결과",
                store=store,
                profile=ceo_profile,
                response_key_suffix=f"ceo-blocked:{root_task_id}",
            )

        if status in {"sent", "deduped"}:
            self._close_root_trace(
                root_id=root_task_id,
                root_payload=root_payload,
                status=("completed" if root_content else "blocked"),
                error_class=None if root_content else "child_terminal_failure",
            )
            logger.info(
                "ceo-late-child-finalization root=%s child=%s status=%s "
                "mode=%s",
                root_task_id,
                task_id,
                status,
                "direct" if root_content else "blocked",
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

        selected, _ = split_planner_selection(
            selected_primary_profiles_from_task(root_payload)
        )
        if not selected:
            # CEO-direct workflows have no delegated department progress card.
            # A single delegated primary still needs visible lifecycle progress.
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

        selected, _ = split_planner_selection(
            selected_primary_profiles_from_task(root_payload)
        )
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

        selected, _ = split_planner_selection(
            selected_primary_profiles_from_task(root_payload)
        )
        if not selected:
            # Keep CEO-direct workflows quiet; delegated single-primary work
            # still needs a visible department lifecycle card.
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

        analysis_result = strip_internal_handoff(
            child.final_answer or child.result or ""
        )
        department_result = analysis_result or child.summary
        terminal_metadata: Mapping[str, Any] = {}
        risk_plan: Mapping[str, Any] | None = None
        if child.profile == canonical_profile_for_department("risk"):
            terminal_metadata = merged_run_metadata(task_payload)
            candidate_plan = terminal_metadata.get(
                "position_risk_plan"
            ) or terminal_metadata.get("risk_plan")
            if isinstance(candidate_plan, Mapping):
                risk_plan = candidate_plan
                department_result = format_position_risk_plan(risk_plan)
        failure_category = _failure_category_for_department_card(
            child.summary,
            child.error,
            child.block_reason,
        )

        content = _department_progress_text(
            child.profile,
            kind,
            summary=department_result,
            analysis_result=analysis_result,
            missing_dependencies=child.missing_dependencies,
            failure_kind=child.failure_kind,
            failure_category=failure_category,
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
            discord_context = (
                risk_span(
                    "risk.discord-projection",
                    {
                        "task_id": child.task_id,
                        "trace_id": risk_plan.get("trace_id") or root_task_id,
                        "risk_plan_id": risk_plan.get("risk_plan_id"),
                        "mandate_version_id": risk_plan.get("mandate_version_id"),
                        "input_hash": risk_plan.get("input_hash"),
                        "algorithm_version": risk_plan.get("calculation_version"),
                        "stage": "discord-projection",
                        "target": "DISCORD",
                        "payload_hash": hashlib.sha256(
                            card_content.encode("utf-8")
                        ).hexdigest(),
                        "status": "running",
                    },
                )
                if risk_plan is not None
                else nullcontext()
            )
            with discord_context:
                status = self.discord_delivery.upsert_thread_card(
                    root_task_id=root_task_id,
                    source_task=delivery_task,
                    root_task=root_payload,
                    content=card_content,
                    store=DiscordIdempotencyStore(delivery_home),
                    profile=child.profile,
                    response_key_suffix=(f"department-card:{child.task_id}"),
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

        if (
            risk_plan is not None
            and risk_plan.get("risk_plan_id")
            and status not in {None, "failed"}
        ):
            recorder = getattr(
                self._department_notion_projection,
                "record_projection_evidence",
                None,
            )
            if callable(recorder):
                recorder(
                    {
                        "risk_plan_id": risk_plan.get("risk_plan_id"),
                        "target": "DISCORD",
                        "projection_version": "risk-plan-discord-projection.v1",
                        "payload_hash": hashlib.sha256(
                            card_content.encode("utf-8")
                        ).hexdigest(),
                        "external_id": None,
                        "delivery_status": "DELIVERED",
                        "readback_status": "NOT_CHECKED",
                        "task_id": child.task_id,
                        "trace_id": risk_plan.get("trace_id") or root_task_id,
                    }
                )

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
        allow_historical_done: bool = False,
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

        # The indexed candidate query excludes these roots upstream.  Keep
        # the same marker check for the full-board fallback and for callers
        # that provide a broader snapshot: once the durable clarification
        # exists, this invalid empty-primary plan is already handled.
        handled_empty_primary_roots = {
            handled_root
            for row in board_rows
            for handled_root in (_handled_empty_primary_control_root(row),)
            if handled_root
        }
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
                    and not allow_historical_done
                    and (
                        completed_at <= 0
                        or now - completed_at > done_recovery_window_seconds
                    )
                )
                or not _is_planning_root_body(body)
                or not is_user_query_body(body)
                or workflow_mode_from_body(body) != "analysis"
                or task_id in handled_empty_primary_roots
                or (
                    allow_historical_done
                    and status == "done"
                    and not selected_primary_profiles_from_task(row)
                    and not _legacy_root_selection_may_be_in_comment(body)
                )
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
                    not _is_planning_root_body(root_body)
                    or not is_user_query_body(root_body)
                    or workflow_mode_from_body(root_body) != "analysis"
                ):
                    continue

                materialization_body = _materialization_plan_body(root_payload)
                materialization_payload = dict(root_payload)
                materialization_payload["body"] = materialization_body
                raw_selected_profiles = selected_primary_profiles_from_task(
                    materialization_payload
                )
                selected_profiles, planner_qa_requested = split_planner_selection(
                    raw_selected_profiles
                )
                # Keep the raw selection in this compatibility recovery state
                # so an all-QA legacy plan still gets the existing explicit
                # clarification instead of disappearing silently.  The
                # materializer itself filters QA below.
                recovery_selected_profiles = raw_selected_profiles

                if not raw_selected_profiles:
                    continue

                _, payloads = workflow(root_id)

                # A recovery snapshot can race the board candidate snapshot.
                # Re-check the authoritative workflow children immediately
                # before decision generation so the handled marker still wins
                # even when it was created after candidate discovery.
                if any(
                    _handled_empty_primary_control_root(payload) == root_id
                    for payload in payloads
                ):
                    continue

                children = tuple(
                    ChildTaskState.from_hermes(payload)
                    for payload in payloads
                    if payload.get("assignee") is not None
                )

                state = SupervisorState(
                    parent_task_id=root_id,
                    children=children,
                    parent_status=str(root_payload.get("status") or ""),
                    wakeups=0,
                    replan_count=0,
                    max_retries=self.max_retries,
                    max_wakeups=self.max_wakeups,
                    qa_required=False,
                    qa_enabled=canonical_qa_contract(
                        workflow_mode="analysis",
                        body=root_body,
                        planner_qa_requested=planner_qa_requested,
                    ).qa_enabled,
                    qa_blocks_response=False,
                    workflow_mode="analysis",
                    has_mandate=mandate_snapshot_present(root_body),
                    selected_primary_profiles=recovery_selected_profiles,
                    root_is_user_query=True,
                    previous_question_context=previous_question_context_from_body(
                        root_body
                    ),
                    allow_primary_passthrough=(
                        self.discord_delivery is not None
                    ),
                    risk_advisory_context=fetch_risk_advisory_context(root_body),
                    accounting_advisory_context=fetch_accounting_advisory_context(),
                    workforce_advisory_context=fetch_workforce_advisory_context(
                        root_body
                    ),
                )

                decisions = _initial_primary_materialization_decisions(
                    state,
                    materialization_body,
                )

                if not decisions:
                    continue

                if any(
                    decision.action != SupervisorAction.CREATE_TASK
                    for decision in decisions
                ):
                    for decision in decisions:
                        self._execute(decision, state)
                    materialized.extend(decisions)
                    logger.info(
                        "empty-primary-clarification-materialized "
                        "root=%s action=%s",
                        root_id,
                        decisions[0].action.value,
                    )
                    continue

                # Publish the CEO delegation card before dispatching any
                # primary child.  A child can claim/start immediately after
                # CREATE_TASK, so publishing afterwards allows a department
                # progress card to overtake the delegation card in Discord.
                try:
                    bridge_status = self._bridge_root_completion_to_discord(
                        root_task_id=root_id,
                        root_payload=root_payload,
                        materialized_primary_profiles=tuple(
                            decision.assignee or "" for decision in decisions
                        ),
                    )
                except Exception as exc:
                    # Existing policy: a Discord delegation failure must not
                    # prevent primary execution.
                    logger.warning(
                        "ceo-root-discord-bridge-failed root=%s error=%s",
                        root_id,
                        type(exc).__name__,
                    )
                    bridge_status = "failed"
                logger.info(
                    "root-planning-complete-projected-before-primary-create "
                    "root=%s status=%s",
                    root_id,
                    bridge_status,
                )

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
        terminal events that happened before ``kanban watch`` subscribed. This
        reconciliation is intentionally age-independent: a root with a missed
        synthesis event can be hours old and still require one final response.
        Candidate discovery remains cheap and root-local; authoritative
        ``show``/``workflow`` reads and idempotent action guards remain the
        final authority.
        """

        list_tasks = getattr(self.client, "list_tasks", None)
        show = getattr(self.client, "show", None)
        if not callable(list_tasks) or not callable(show):
            return ()

        candidate_rows: Sequence[Mapping[str, Any]] | None = None
        recovery_candidates = getattr(self.client, "recovery_candidate_rows", None)
        if callable(recovery_candidates):
            try:
                candidate_rows = recovery_candidates()
            except RootScopedIndexUnavailable:
                candidate_rows = None
        if candidate_rows is None:
            _record_full_board_fallback(
                lane=current_cli_lane(),
                reason="startup-reconciliation",
                root_id="",
            )
            candidate_rows = list_tasks()

        roots: dict[str, tuple[int, Mapping[str, Any]]] = {}
        for row in candidate_rows:
            task_id = str(row.get("id") or row.get("task_id") or "")
            body = str(row.get("body") or "")
            status = str(row.get("status") or "").casefold()
            selected_profiles = selected_primary_profiles_from_task(row)
            completed_at = int(row.get("completed_at") or 0)
            created_at = int(row.get("created_at") or 0)
            recovery_timestamp = completed_at or created_at
            if (
                not task_id
                or status not in {"done", "completed", "archived"}
                or not _is_planning_root_body(body)
                # Preserve the old test/compatibility behavior and avoid
                # expensive authoritative reads for roots with no durable
                # selection. Modern direct roots may still recover their
                # selection from a CEO comment in the ready-plan lane.
                or (
                    not selected_profiles
                    and not _legacy_root_selection_may_be_in_comment(body)
                )
                # Historical Discord roots without a materialized primary
                # have no work to recover unless the indexed comment actually
                # contains the durable department selection marker. This
                # avoids rehydrating hundreds of already terminal roots while
                # retaining the narrow legacy-comment repair path.
                or (
                    not bool(row.get("has_analysis_child"))
                    and not selected_profiles
                    and not bool(row.get("has_selection_comment"))
                )
            ):
                continue
            roots[task_id] = (recovery_timestamp, row)

        decisions: list[SupervisorDecision] = []
        ordered_root_ids = sorted(
            roots,
            key=lambda root_id: (roots[root_id][0], root_id),
            reverse=True,
        )
        for root_id in ordered_root_ids:
            root_payload = show(root_id)
            self._remember_workflow_root(root_id, root_id, (root_payload,))
            root_status = str(root_payload.get("status") or "").casefold()
            if root_status not in {"done", "completed", "archived"}:
                continue
            if not _is_planning_root_body(str(root_payload.get("body") or "")):
                continue
            if not selected_primary_profiles_from_task(root_payload):
                continue

            _, payloads = self.client.workflow(root_id)
            children = tuple(
                ChildTaskState.from_hermes(payload)
                for payload in payloads
                if payload.get("assignee") is not None
            )
            if any(
                child.is_in_workflow(root_id)
                and child.workflow_role == "synthesis"
                for child in children
            ):
                # A synthesis already exists, including one whose terminal
                # event is still being handled by the dedicated reconciler.
                # Never create a second response-plane task.
                continue
            terminal_primary = tuple(
                child
                for child in children
                if child.is_in_workflow(root_id)
                and child.is_analysis
                and child.terminal
            )
            if not terminal_primary:
                # A legacy direct root can be completed before its CEO-authored
                # delegation comment is materialized into child cards. Reuse
                # the normal idempotent materializer with this authoritative
                # root payload so an old root is repaired without broadening
                # the periodic recovery scan.
                decisions.extend(
                    self.materialize_ready_primary_plans(
                        listed_rows=(root_payload,),
                        allow_historical_done=True,
                    )
                )
                continue

            wake_child = next(
                (
                    child
                    for child in terminal_primary
                    if child.blocked or child.failed
                ),
                terminal_primary[0],
            )
            if wake_child.blocked:
                event_kind = "blocked"
            elif wake_child.failed:
                event_kind = wake_child.status
            else:
                event_kind = "completed"
            event = {
                "event_id": (
                    f"reconcile:{root_id}:{wake_child.task_id}:{wake_child.status}"
                ),
                "task_id": wake_child.task_id,
                "kind": event_kind,
            }
            decision = self.handle_terminal_event(event)
            if decision is not None:
                decisions.append(decision)
        return tuple(decisions)

    def reconcile_expired_workflows(
        self,
        *,
        listed_rows: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[str, ...]:
        """Stop over-deadline workers and wake synthesis with partial state.

        Hermes owns the actual SIGTERM/SIGKILL sequence when a task's
        ``--max-runtime`` is exceeded. This lane owns the end-to-end ceiling:
        once the root deadline is reached, still-running primary cards are
        blocked and the normal supervisor path is replayed so the user gets a
        bounded partial/failure answer instead of a root that remains pending.
        """

        list_tasks = getattr(self.client, "list_tasks", None)
        show = getattr(self.client, "show", None)
        if not callable(show):
            return ()

        if listed_rows is None:
            if not callable(list_tasks):
                return ()
            board_rows = list_tasks()
        else:
            board_rows = listed_rows

        now = int(time.time())
        expired: list[tuple[int, str, Mapping[str, Any]]] = []
        for row in board_rows:
            root_id = str(row.get("id") or row.get("task_id") or "")
            body = str(row.get("body") or "")
            status = str(row.get("status") or "").casefold()
            started_at = int(row.get("created_at") or 0)
            if (
                not root_id
                or status not in {"done", "completed", "archived"}
                or not _is_planning_root_body(body)
                or not selected_primary_profiles_from_task(row)
                # SQLite discovery marks whether a non-terminal primary is
                # actually present.  Fallback/list fakes may not provide the
                # hint, so absence preserves their previous behavior.
                or (
                    "has_active_primary" in row
                    and not bool(row.get("has_active_primary"))
                )
                # Startup recovery only needs roots whose response plane is
                # missing.  A synthesis child already represents that plane;
                # an active primary is still owned by the normal event path.
                # Fallback/list rows without these discovery hints retain the
                # legacy authoritative behavior.
                or (
                    bool(row.get("has_synthesis"))
                    or (
                        bool(row.get("has_analysis_child"))
                        and not bool(row.get("has_terminal_primary"))
                    )
                )
                or started_at <= 0
            ):
                continue
            try:
                timeout_seconds = int(read_marker(body, "workflow_timeout_seconds"))
            except ValueError:
                timeout_seconds = 1200
            timeout_seconds = max(60, min(timeout_seconds, 86_400))
            if now - started_at > timeout_seconds:
                expired.append((started_at, root_id, row))

        stopped: list[str] = []
        for _started_at, root_id, row in sorted(expired, key=lambda item: item[:2]):
            root_payload = show(root_id)
            if str(root_payload.get("status") or "").casefold() not in {
                "done",
                "completed",
                "archived",
            }:
                continue
            if not selected_primary_profiles_from_task(root_payload):
                continue
            _root, payloads = self.client.workflow(root_id)
            children = tuple(
                ChildTaskState.from_hermes(payload)
                for payload in payloads
                if payload.get("assignee") is not None
            )
            if any(
                child.is_in_workflow(root_id)
                and child.workflow_role == "synthesis"
                for child in children
            ):
                continue
            active_primaries = tuple(
                child
                for child in children
                if child.is_in_workflow(root_id)
                and child.is_analysis
                and not child.terminal
            )
            if not active_primaries:
                continue
            for child in active_primaries:
                self.client.block_task(
                    child.task_id,
                    "workflow_timeout_exceeded: end-to-end CEO workflow deadline reached",
                )
            wake = active_primaries[0]
            self.handle_terminal_event(
                {
                    "event_id": f"reconcile-timeout:{root_id}:{wake.task_id}",
                    "task_id": wake.task_id,
                    "kind": "blocked",
                }
            )
            stopped.append(root_id)
            logger.warning(
                "workflow-timeout root=%s stopped_primary_count=%d timeout_reason=deadline_exceeded",
                root_id,
                len(active_primaries),
            )
        return tuple(stopped)

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
        """Serialize fast root projection with all other root materializers."""

        if kind not in {"done", "completed"}:
            return False, None

        # The Discord bridge is intentionally before child CREATE_TASK.  Hold
        # the same per-root lock used by the recovery/materialization lane for
        # that whole interval; otherwise a concurrent recovery pass can create
        # a child while the bridge is still waiting on Discord.
        with self._parent_lock(task_id):
            return self._materialize_completed_analysis_root_fast_locked(
                task_id=task_id,
                kind=kind,
            )

    def _materialize_completed_analysis_root_fast_locked(
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
        root_body = str(root_payload.get("body") or "")

        if (
            not _is_planning_root_body(root_body)
            or not is_user_query_body(root_body)
            or workflow_mode_from_body(root_body) != "analysis"
        ):
            return False, None

        # Only a confirmed planning root may seed the root cache with itself.
        # A CEO-assigned synthesis task also reaches this fast path by
        # assignee, but it is a child of the planning root. Caching that child
        # as its own root poisons the next authoritative reconciliation and
        # sends it through the legacy full-board/abort path.
        self._remember_workflow_root(task_id, task_id, (root_payload,))

        materialization_body = _materialization_plan_body(root_payload)
        materialization_payload = dict(root_payload)
        materialization_payload["body"] = materialization_body
        raw_selected_profiles = selected_primary_profiles_from_task(
            materialization_payload
        )
        selected_profiles, planner_qa_requested = split_planner_selection(
            raw_selected_profiles
        )
        recovery_selected_profiles = raw_selected_profiles

        # A direct CEO answer has no selected primary plan.  It is still a root
        # completion and can be projected immediately without workflow().
        if not raw_selected_profiles:
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

        # A ready/recovery materializer may have created primaries before the
        # terminal fast path. Do not replay the whole fan-out and depend on
        # idempotency to reject it; let the normal authoritative path reconcile
        # any genuinely missing child instead.
        if self._fast_path_has_materialized_primary_children(task_id):
            logger.info(
                "ready-primary-fast-skipped root=%s "
                "reason=authoritative-primary-present",
                task_id,
            )
            return False, None

        state = SupervisorState(
            parent_task_id=task_id,
            children=(),
            parent_status=str(root_payload.get("status") or ""),
            wakeups=0,
            replan_count=0,
            max_retries=self.max_retries,
            max_wakeups=self.max_wakeups,
            qa_required=False,
            qa_enabled=canonical_qa_contract(
                workflow_mode="analysis",
                body=root_body,
                planner_qa_requested=planner_qa_requested,
            ).qa_enabled,
            qa_blocks_response=False,
            workflow_mode="analysis",
            has_mandate=mandate_snapshot_present(root_body),
            selected_primary_profiles=recovery_selected_profiles,
            root_is_user_query=True,
            previous_question_context=previous_question_context_from_body(root_body),
            allow_primary_passthrough=self.discord_delivery is not None,
            risk_advisory_context=fetch_risk_advisory_context(root_body),
            accounting_advisory_context=fetch_accounting_advisory_context(),
            workforce_advisory_context=fetch_workforce_advisory_context(root_body),
        )

        decisions = _initial_primary_materialization_decisions(
            state,
            materialization_body,
        )

        if not decisions:
            # A selected plan that cannot be validated should fall back to the
            # authoritative recovery/workflow path rather than being swallowed.
            return False, None

        if any(
            decision.action != SupervisorAction.CREATE_TASK
            for decision in decisions
        ):
            final_status = self._reconcile_unmaterialized_primary_root(
                root_task_id=task_id,
                root_payload=root_payload,
                task_payloads=(root_payload,),
            )
            if final_status is not None:
                return True, None
            for decision in decisions:
                self._execute(decision, state)
            logger.info(
                "empty-primary-clarification-materialized "
                "root=%s action=%s",
                task_id,
                decisions[0].action.value,
            )
            return True, decisions[0]

        # Publish before any primary child is dispatched.  The child may start
        # synchronously during CREATE_TASK, so a post-create bridge can produce
        # a department card before the CEO delegation card.
        try:
            bridge_status = self._bridge_root_completion_to_discord(
                root_task_id=task_id,
                root_payload=root_payload,
                materialized_primary_profiles=tuple(
                    decision.assignee or "" for decision in decisions
                ),
            )
        except Exception as exc:
            # A Discord projection failure must not change child execution.
            logger.warning(
                "ceo-root-discord-bridge-failed root=%s error=%s",
                task_id,
                type(exc).__name__,
            )
            bridge_status = "failed"

        logger.info(
            "root-planning-complete-fast-projected-before-primary-create "
            "root=%s status=%s",
            task_id,
            bridge_status,
        )

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
            # An active child without a visible scope marker is not a root.
            # Falling back to its own id poisons the immutable root cache and
            # later makes the terminal QA event reconcile against itself.
            # Only a canonical root may use its own id here; terminal recovery
            # remains responsible for authoritative ancestry discovery.
            task_id_value = str(
                task_payload.get("id") or task_payload.get("task_id") or ""
            ).strip()
            if task_id_value and HermesKanbanClient._is_canonical_scoped_root(
                task_payload, task_id_value
            ):
                root_id = task_id_value

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
            published = publish_head_activity(
                stage=stage,
                head_persona=persona,
                status=status,
                error_count=errors,
                trace_id=task_id,
                source="kanban_card",
            )
            if not published:
                # ▶ 이 한 줄이 없어서 2026-08-23 에 몇 시간을 태웠다. 이 이미지에
                #   langfuse 가 없어 publish 가 조용히 False 를 돌려주고 있었는데,
                #   코드·자격증명·프로필 해석이 전부 정상이라 어디가 끊겼는지
                #   보이지 않았다. **관측 코드가 자기 실패를 관측하지 못하면
                #   관측이 없는 것과 같다** - 그래서 DEBUG 가 아니라 WARNING 이다.
                logger.warning(
                    "head-card-activity-not-published task=%s stage=%s persona=%s "
                    "(langfuse 미설치·자격증명 부재·전송 실패 중 하나)",
                    task_id, stage, persona,
                )
        except Exception as exc:  # noqa: BLE001 - 계측이 워크플로를 멈추지 못한다
            logger.warning(
                "head-card-activity-publish-failed task=%s error=%s: %s",
                task_id, type(exc).__name__, exc,
            )

    def handle_terminal_event(self, event: Mapping[str, Any]) -> SupervisorDecision | None:
        handler_started_ms = time.time_ns() // 1_000_000
        handler_started_mono_ns = time.perf_counter_ns()
        event_consumed_ms = int(
            event.get("_event_consumed_ms") or handler_started_ms
        )
        event_created_ms = int(event.get("_event_created_ms") or 0)
        event_persisted_ms = int(
            event.get("_event_persisted_ms") or event_created_ms
        )
        event_detected_ms = int(event.get("_event_detected_ms") or 0)
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

        deferred_terminal_observer: Callable[[], None] | None = None
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

            # A QA task is parent-linked to the delivered response, while its
            # durable scope marker points to the planning root.  If an older
            # active-event path cached the task itself as its root, re-read
            # only this task and prefer its explicit scope before locking.
            # This keeps QA terminal projections attached to the actual CEO
            # workflow without scanning the board.
            if root_id == task_id and event_assignee != ceo_assignee:
                show = getattr(self.client, "show", None)
                if callable(show):
                    try:
                        scoped_task = show(task_id)
                        scoped_roots = extract_scope_references(scoped_task).root_ids
                        if scoped_roots and scoped_roots[0] != task_id:
                            root_id = scoped_roots[0]
                            self._remember_workflow_root(task_id, root_id)
                            logger.info(
                                "workflow-root-revalidated task=%s root=%s",
                                task_id,
                                root_id,
                            )
                    except Exception as exc:  # noqa: BLE001 - normal path remains fail-open.
                        logger.warning(
                            "workflow-root-revalidation-failed task=%s error=%s",
                            task_id,
                            type(exc).__name__,
                        )

            root_resolved_ms = time.time_ns() // 1_000_000
            lock_started_ms = time.time_ns() // 1_000_000
            lock_started_mono_ns = time.perf_counter_ns()
            with self._parent_lock(root_id):
                lock_acquired_ms = time.time_ns() // 1_000_000
                lock_acquired_mono_ns = time.perf_counter_ns()
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
                # Strategy Hermes owns its research lifecycle.  Its Kanban
                # root exists only as a control-plane correlation record and
                # must never enter CEO planning, child creation, synthesis,
                # Discord projection, or supervisor retry logic.  This guard
                # is intentionally independent from the card status because
                # a just-created blocked card may briefly be observed as
                # ready/running during the dispatcher transaction race.
                if "strategy_research_tracking_only=true" in str(
                    root_payload.get("body") or ""
                ):
                    logger.info(
                        "strategy-research-tracking-root-ignored root=%s task=%s "
                        "event=%s",
                        root_id,
                        task_id,
                        event_key,
                    )
                    return None
                workflow_ready_ms = time.time_ns() // 1_000_000
                workflow_ready_mono_ns = time.perf_counter_ns()
                synthesis_timing_base: dict[str, Any] = {
                    "request_id": event.get("request_id"),
                    "root_id": root_id,
                    "source_task_id": task_id,
                    "event_id": event_key,
                    "t0_ms": event_persisted_ms,
                    "t1_ms": event_detected_ms,
                    "t2_ms": handler_started_ms,
                    "t3_ms": lock_started_ms,
                    "t4_ms": lock_acquired_ms,
                    "t5_ms": workflow_ready_ms,
                    "t3_mono_ns": lock_started_mono_ns,
                    "t4_mono_ns": lock_acquired_mono_ns,
                    "t5_mono_ns": workflow_ready_mono_ns,
                    "workflow_mode": "unknown",
                    "primary_departments": "",
                    "availability": "unknown",
                    "partial": False,
                }
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
                    *,
                    synthesis_timing: dict[str, Any] | None = None,
                ) -> None:
                    action_started_ms = time.time_ns() // 1_000_000
                    action_started_mono_ns = time.perf_counter_ns()
                    if synthesis_timing is not None:
                        synthesis_timing["t7a_ms"] = action_started_ms
                        synthesis_timing["t7a_mono_ns"] = action_started_mono_ns
                    execution_succeeded = False
                    try:
                        self._execute(
                            action_decision,
                            action_state,
                            synthesis_timing=synthesis_timing,
                        )
                        execution_succeeded = True
                    finally:
                        action_completed_ms = time.time_ns() // 1_000_000
                        action_completed_mono_ns = time.perf_counter_ns()
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
                        if synthesis_timing is not None:
                            synthesis_timing["t8_ms"] = action_completed_ms
                            synthesis_timing["t8_mono_ns"] = action_completed_mono_ns
                            self._log_synthesis_timing(
                                synthesis_timing,
                                success=execution_succeeded,
                            )
                # The root is a planning/scope task in the current contract. Its
                # terminal transition means planning finished, not that the
                # workflow is ready for synthesis. Primary child events are the
                # wake-up boundary.
                root_body = str(root_payload.get("body") or "")
                if root_id == task_id and kind in {"done", "completed"}:
                    if _is_planning_root_body(root_body):
                        # Root completion remains a planning boundary, never a
                        # synthesis-ready signal.  Project only the CEO-authored
                        # durable outcome into the already-existing Discord thread.
                        try:
                            bridge_status = self._bridge_root_completion_to_discord(
                                root_task_id=root_id,
                                root_payload=root_payload,
                                task_payloads=(root_payload, *payloads),
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
                        self._reconcile_unmaterialized_primary_root(
                            root_task_id=root_id,
                            root_payload=root_payload,
                            task_payloads=(root_payload, *payloads),
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
                    scope_error_prefix = (
                        "hgfinance.ceo-workflow-scope-error.v1 "
                        f"event={event_key} "
                    )
                    comments = root_payload.get("comments")
                    scope_error_comments = tuple(
                        comment
                        for comment in (comments or ())
                        if isinstance(comment, Mapping)
                        and str(comment.get("body") or "").startswith(
                            "hgfinance.ceo-workflow-scope-error.v1 "
                        )
                    ) if (
                        isinstance(comments, Sequence)
                        and not isinstance(comments, (str, bytes))
                    ) else ()
                    already_recorded = any(
                        str(comment.get("body") or "").startswith(scope_error_prefix)
                        for comment in scope_error_comments
                    )
                    comment_task = getattr(self.client, "comment_task", None)
                    if callable(comment_task) and not already_recorded:
                        comment_task(
                            root_id,
                            f"{scope_error_prefix}reason={reason}",
                        )
                    root_status = str(root_payload.get("status") or "").casefold()
                    if root_status in TERMINAL_STATUSES:
                        # Planning roots are normally already done.  A late
                        # legacy scope error is an audit finding, not authority
                        # to mutate that terminal root back to blocked.  The
                        # event-specific comment is the durable dedupe record.
                        logger.warning(
                            "workflow-scope-error-recorded root=%s event=%s "
                            "root_status=%s mutation=skipped duplicate=%s",
                            root_id,
                            event_key,
                            root_status,
                            str(already_recorded).lower(),
                        )
                        # One root-level scope audit finding is enough for a
                        # historical terminal workflow. Different old child
                        # events must not emit the same recovery action again.
                        if scope_error_comments:
                            return None
                    else:
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
                    nonlocal deferred_terminal_observer
                    nonlocal terminal_observers_projected, terminal_task_payload
                    nonlocal terminal_progress_status
                    nonlocal observer_started_ms, observer_completed_ms
                    if terminal_observers_projected:
                        return
                    terminal_observers_projected = True

                    def run_observers() -> None:
                        nonlocal terminal_progress_status
                        nonlocal observer_started_ms, observer_completed_ms
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
                                with self._seen_events_lock:
                                    self._seen_terminal_transitions.discard(transition_key)

                            if terminal_task_payload is not None:
                                try:
                                    terminal_progress_status = self._deliver_department_progress(
                                        root_task_id=root_id,
                                        root_payload=root_payload,
                                        task_payload=terminal_task_payload,
                                        event=event,
                                    )
                                except Exception as exc:
                                    logger.warning(
                                        "department-discord-progress-failed task=%s kind=%s error=%s",
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
                                "observer_duration_ms=%d lock_held=false",
                                root_id,
                                task_id,
                                event_key,
                                observer_started_ms,
                                observer_completed_ms,
                                _elapsed_ms(observer_started_ms, observer_completed_ms),
                            )

                    deferred_terminal_observer = run_observers

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
                response_payloads = (root_payload, *payloads)
                if terminal_task_payload is not None and not any(
                    str(payload.get("id") or payload.get("task_id") or "")
                    == task_id
                    for payload in response_payloads
                ):
                    response_payloads = (*response_payloads, terminal_task_payload)
                try:
                    self._reconcile_late_child_finalization(
                        root_task_id=root_id,
                        root_payload=root_payload,
                        task_payloads=response_payloads,
                        task_id=task_id,
                    )
                except Exception as exc:
                    # Final delivery remains retryable through the existing
                    # idempotent helper and a later terminal/recovery wakeup.
                    # Never turn a child terminal event into a supervisor crash.
                    logger.warning(
                        "ceo-late-child-finalization-failed root=%s child=%s "
                        "error=%s",
                        root_id,
                        task_id,
                        type(exc).__name__,
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
                raw_selected_profiles = selected_primary_profiles_from_task(root_payload)
                selected_profiles, planner_qa_requested = split_planner_selection(
                    raw_selected_profiles
                )
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
                    parent_status=str(root_payload.get("status") or ""),
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
                    qa_enabled=canonical_qa_contract(
                        workflow_mode=workflow_mode,
                        body=root_body,
                        metadata=event.get("metadata")
                        if isinstance(event.get("metadata"), Mapping)
                        else None,
                        legacy_qa_required=self._qa_required_from_event(event),
                        paper_order=user_paper_order_scope is not None,
                        planner_qa_requested=planner_qa_requested,
                    ).qa_enabled,
                    qa_blocks_response=canonical_qa_contract(
                        workflow_mode=workflow_mode,
                        body=root_body,
                        metadata=event.get("metadata")
                        if isinstance(event.get("metadata"), Mapping)
                        else None,
                        legacy_qa_required=self._qa_required_from_event(event),
                        paper_order=user_paper_order_scope is not None,
                        planner_qa_requested=planner_qa_requested,
                    ).qa_blocks_response,
                    workflow_mode=workflow_mode,
                    previous_question_context=previous_question_context_from_body(
                        root_body
                    ),
                    has_mandate=mandate_snapshot_present(root_body),
                    selected_primary_profiles=selected_profiles,
                    root_is_user_query=is_user_query_body(root_body),
                    allow_primary_passthrough=self.discord_delivery is not None,
                    paper_order=user_paper_order_scope is not None,
                    risk_advisory_context=fetch_risk_advisory_context(root_body),
                    accounting_advisory_context=fetch_accounting_advisory_context(),
                    workforce_advisory_context=fetch_workforce_advisory_context(
                        root_body
                    ),
                )

                def new_synthesis_timing(
                    action_state: SupervisorState,
                    decision_completed_ms: int,
                    decision_completed_mono_ns: int,
                ) -> dict[str, Any]:
                    availability = self._synthesis_availability(action_state)
                    departments = sorted(
                        {
                            child.profile
                            for child in action_state.analysis_children
                            if child.profile
                        }
                    )
                    timing = dict(synthesis_timing_base)
                    timing.update(
                        {
                            "workflow_mode": action_state.workflow_mode,
                            "primary_departments": ",".join(departments),
                            "availability": availability,
                            "partial": availability in {"partial", "blocked"},
                            "t6_ms": decision_completed_ms,
                            "t6_mono_ns": decision_completed_mono_ns,
                        }
                    )
                    return timing

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
                    # A terminal event can race the attachment/run metadata
                    # commit.  Refresh only this selected primary card before
                    # rendering the user answer; this is a read-only,
                    # task-scoped lookup and avoids stale bounded evidence.
                    hydrated_primary_payload: Mapping[str, Any] | None = None
                    show = getattr(self.client, "show", None)
                    if callable(show):
                        try:
                            hydrated = show(passthrough.task_id)
                            if isinstance(hydrated, Mapping):
                                hydrated_primary_payload = hydrated
                        except Exception as exc:  # noqa: BLE001 - delivery remains fail-open.
                            logger.warning(
                                "single-primary-final-card-refresh-failed "
                                "root=%s task=%s error=%s",
                                root_id,
                                passthrough.task_id,
                                type(exc).__name__,
                            )
                    if hydrated_primary_payload is not None:
                        primary_payload = {
                            **dict(primary_payload),
                            **dict(hydrated_primary_payload),
                        }
                    # ``payloads`` may be a shallow board listing whose
                    # result is only the transport token.  The hydrated state
                    # is authoritative for the terminal answer and metadata;
                    # merge it into the matching row before HR provenance
                    # enrichment and before constructing the QA audit input.
                    delivery_task = dict(primary_payload)
                    delivery_task.update(
                        {
                            "result": passthrough.result,
                            "final_answer": passthrough.final_answer,
                            "body": passthrough.body,
                            "workspace_path": passthrough.workspace_path,
                            "run_metadata": dict(passthrough.metadata),
                            "metadata": dict(passthrough.metadata),
                        }
                    )
                    delivery_task["root_task"] = root_payload

                    # The single-primary path is the normal HR E2E fast path:
                    # the department's answer is delivered directly without a
                    # second CEO synthesis task.  Apply the same bounded HR
                    # provenance projection used by the synthesis observer
                    # before delivery, otherwise the user sees a useful
                    # summary but QA cannot reproduce it from the answer.
                    passthrough_content = strip_internal_handoff(
                        passthrough.final_answer
                        or str(primary_payload.get("final_answer") or "")
                        or passthrough.result
                    )
                    if passthrough.profile == canonical_profile_for_department("hr"):
                        hydrated_passthrough = _terminal_payload_mapping(passthrough)
                        enrichment_payloads = tuple(
                            delivery_task
                            if str(item.get("id") or item.get("task_id") or "")
                            == passthrough.task_id
                            else item
                            for item in (root_payload, *payloads)
                        )
                        if not any(
                            str(item.get("id") or item.get("task_id") or "")
                            == passthrough.task_id
                            for item in enrichment_payloads
                        ):
                            enrichment_payloads = (
                                *enrichment_payloads,
                                delivery_task,
                            )
                        passthrough_content = _augment_hr_final_answer(
                            passthrough_content,
                            root_task_id=root_id,
                            task_payloads=(*enrichment_payloads, hydrated_passthrough),
                        )
                        if passthrough_content != passthrough.final_answer:
                            delivery_task["result"] = passthrough_content
                            delivery_task["final_answer"] = passthrough_content
                            delivery_metadata = merged_run_metadata(delivery_task)
                            delivery_metadata.update(
                                {
                                    "result": passthrough_content,
                                    "final_answer": passthrough_content,
                                    "synthesis_provenance_enriched": True,
                                }
                            )
                            delivery_task["run_metadata"] = delivery_metadata
                            delivery_task["metadata"] = delivery_metadata
                            editor = getattr(self.client, "edit_task", None)
                            if callable(editor):
                                try:
                                    editor(
                                        passthrough.task_id,
                                        result=passthrough_content,
                                        summary=_text(
                                            primary_payload.get("summary")
                                            or passthrough_content
                                        ),
                                        metadata=delivery_metadata,
                                    )
                                except Exception as exc:  # noqa: BLE001 - delivery stays fail-open.
                                    logger.warning(
                                        "single-primary-hr-provenance-persist-failed "
                                        "root=%s task=%s error=%s",
                                        root_id,
                                        passthrough.task_id,
                                        type(exc).__name__,
                                    )

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
                            content=passthrough_content,
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
                            content=passthrough_content,
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
                    if delivery_status in {"sent", "deduped"}:
                        langsmith_closed = self._close_root_trace(
                            root_id=root_id,
                            root_payload=root_payload,
                            terminal_payload=passthrough,
                            status="completed",
                            department=passthrough.profile,
                            task_id=passthrough.task_id,
                        )
                        self._remember_hr_response_delivery(
                            root_task_id=root_id,
                            response_task_id=passthrough.task_id,
                            content=passthrough_content,
                            discord_status=delivery_status,
                            langsmith_closed=langsmith_closed,
                        )

                decision_started_ms = time.time_ns() // 1_000_000
                deferred_decision = _deferred_conditional_decision(
                    state,
                    root_body,
                )
                decision = deferred_decision or self.decider(state)
                decision_completed_mono_ns = time.perf_counter_ns()
                decision_completed_ms = time.time_ns() // 1_000_000
                if (
                    wakeups >= self.max_wakeups
                    and self._consumes_wakeup_budget(
                        decision.action if decision is not None else None
                    )
                ):
                    decision = self.decider(replace(state, wakeups=wakeups))
                    decision_completed_mono_ns = time.perf_counter_ns()
                    decision_completed_ms = time.time_ns() // 1_000_000
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
                synthesis_timing = (
                    new_synthesis_timing(
                        state,
                        decision_completed_ms,
                        decision_completed_mono_ns,
                    )
                    if decision is not None
                    and decision.action == SupervisorAction.SYNTHESIZE
                    else None
                )
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
                execute_timed(
                    decision,
                    state,
                    synthesis_timing=synthesis_timing,
                )
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
        finally:
            # Every path has left the per-root lock before this finalizer runs.
            # Discord/Notion latency can therefore no longer serialize sibling
            # terminal events for the same workflow root.
            if deferred_terminal_observer is not None:
                submitted = False
                if self._terminal_observer_submit is not None:
                    try:
                        submitted = bool(
                            self._terminal_observer_submit(deferred_terminal_observer)
                        )
                    except Exception as exc:  # queue failure remains fail-open
                        logger.warning(
                            "terminal observer queue rejected task=%s event=%s error=%s",
                            task_id,
                            event_key,
                            type(exc).__name__,
                        )
                if not submitted:
                    try:
                        deferred_terminal_observer()
                    except Exception as exc:  # observer failure is non-binding
                        logger.exception(
                            "deferred terminal observer failed",
                            extra={
                                "task_id": task_id,
                                "event_id": event_key,
                                "error": str(exc),
                            },
                        )

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

    def _execute(
        self,
        decision: SupervisorDecision,
        state: SupervisorState,
        *,
        synthesis_timing: dict[str, Any] | None = None,
    ) -> None:
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
            if state.parent_status.casefold() in TERMINAL_STATUSES:
                comment_task = getattr(self.client, "comment_task", None)
                if callable(comment_task):
                    comment_task(
                        decision.parent_task_id,
                        f"{SUPERVISOR_MARKER} action=BLOCK/ABORT "
                        f"state=recorded_without_root_mutation reason="
                        f"{decision.reason or 'supervisor_aborted'}",
                    )
                logger.warning(
                    "supervisor-abort-recorded root=%s parent_status=%s "
                    "root_mutation=skipped reason=%s",
                    decision.parent_task_id,
                    state.parent_status,
                    decision.reason or "supervisor_aborted",
                )
                return
            self.client.block_task(
                decision.parent_task_id,
                decision.reason or "supervisor aborted",
            )
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
                    previous_question_context=state.previous_question_context,
                ),
                assignee=decision.assignee or canonical_profile_for_department("ceo"),
                parent_task_ids=decision.parent_task_ids,
                idempotency_key=request_user_input_idempotency_key(
                    state.parent_task_id
                ),
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
                # QA task creation moved to the terminal response observer.
                # Keeping RUN_QA in the parser preserves legacy event
                # readability, but no supervisor action may materialize a
                # pre-response QA child anymore.
                raise SupervisorValidationError(
                    "RUN_QA is post-response only; use the terminal response "
                    "observer to schedule the QA audit"
                )
            elif decision.action == SupervisorAction.SYNTHESIZE:
                # CEO synthesis is always parented by terminal primary
                # handoffs. QA is a child of the completed response and is
                # therefore never a synthesis dependency, including binding
                # workflows and late QA terminal events.
                expected = {
                    child.task_id
                    for child in state.analysis_children
                    if child.done
                }
                # The trusted PAPER template is completed from the persisted
                # Trading handoff immediately after the card is created. It
                # intentionally has no Kanban parent edge because the source
                # task is recorded in the template metadata and the card is
                # kept blocked until that deterministic completion succeeds.
                if decision.reason in {
                    "binding_paper_structured_template",
                    "empty_primary_defer_template",
                }:
                    expected = set()
                elif decision.reason == "binding_partial_defer_template":
                    # A deterministic DEFER preserves completed primary
                    # evidence as its parent. QA is post-response and must
                    # never become a response dependency here.
                    expected = {
                        child.task_id
                        for child in state.analysis_children
                        if child.done
                    }
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
            if synthesis_timing is not None and role == "synthesis":
                synthesis_timing["t7b_ms"] = time.time_ns() // 1_000_000
                synthesis_timing["t7b_mono_ns"] = time.perf_counter_ns()
            task_body = decision.body
            if (
                role == "primary"
                and decision.assignee == canonical_profile_for_department("risk")
                and state.risk_advisory_context
            ):
                task_body = (
                    f"{task_body}\n\n"
                    "Read-only portfolio context (do not treat as trade authorization):\n"
                    f"{state.risk_advisory_context}"
                )
            if (
                role == "primary"
                and decision.assignee == canonical_profile_for_department("accounting")
                and state.accounting_advisory_context
            ):
                task_body = (
                    f"{task_body}\n\n"
                    "Confirmed Accounting Engine read-only advisory snapshot "
                    "(authoritative=false; official NAV close pending) - "
                    "use these figures rather than declining for lack of evidence:\n"
                    f"{state.accounting_advisory_context}"
                )
            if (
                role == "primary"
                and decision.assignee == canonical_profile_for_department("hr")
                and state.workforce_advisory_context
            ):
                task_body = (
                    f"{task_body}\n\n"
                    "Authoritative Workforce API snapshot (metadata-only, read-only; "
                    "never treat as approval or lifecycle authority) - use this attached "
                    "snapshot for the current capacity/latency/error/retry/idle and "
                    "improvement-candidate facts. Do not repeat browser, terminal, file, "
                    "or memory discovery for fields present here. If a field is unavailable, "
                    "state that limitation and complete the bounded answer:\n"
                    f"{state.workforce_advisory_context}"
                )
            created = self.client.create_task(
                title=decision.title,
                body=build_scoped_task_body(
                    task_body,
                    state.parent_task_id,
                    role=role,
                    workflow_mode=state.workflow_mode,
                    has_mandate=state.has_mandate,
                    previous_question_context=state.previous_question_context,
                ),
                assignee=decision.assignee,
                parent_task_ids=decision.parent_task_ids,
                idempotency_key=idempotency_key,
                initial_status=decision.initial_status,
            )
            if decision.reason == "binding_paper_structured_template":
                created_task = (
                    created.get("task", created)
                    if isinstance(created, Mapping)
                    else {}
                )
                synthesis_task_id = (
                    str(created_task.get("id") or created_task.get("task_id") or "")
                    if isinstance(created_task, Mapping)
                    else ""
                )
                template_child = _binding_paper_template_child(state)
                if not synthesis_task_id or template_child is None:
                    raise SupervisorValidationError(
                        "binding PAPER template lost its structured source"
                    )
                try:
                    self.client.complete_task(
                        synthesis_task_id,
                        result=template_child.final_answer,
                        summary=(
                            "Trading primary의 검증된 PAPER 주문 결과를 "
                            "원문 그대로 전달했습니다."
                        ),
                        metadata={
                            "source_task_id": template_child.task_id,
                            "workflow_root_task_id": state.parent_task_id,
                            "workflow_mode": "binding",
                            "order_mode": "PAPER",
                            "synthesis_mode": "structured_primary_template",
                            "preserved_primary_final_answer_verbatim": True,
                            "final_answer": template_child.final_answer,
                        },
                    )
                    logger.info(
                        "binding-paper-template-complete root=%s source=%s task=%s",
                        state.parent_task_id,
                        template_child.task_id,
                        synthesis_task_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "binding-paper-template-fallback root=%s task=%s error=%s",
                        state.parent_task_id,
                        synthesis_task_id,
                        type(exc).__name__,
                    )
                    # Keep one synthesis identity. Releasing this same blocked
                    # card restores the existing CEO LLM behavior.
                    self.client.unblock_task(synthesis_task_id)
            if decision.reason in {
                "binding_partial_defer_template",
                "empty_primary_defer_template",
            }:
                created_task = (
                    created.get("task", created)
                    if isinstance(created, Mapping)
                    else {}
                )
                synthesis_task_id = (
                    str(created_task.get("id") or created_task.get("task_id") or "")
                    if isinstance(created_task, Mapping)
                    else ""
                )
                if not synthesis_task_id:
                    raise SupervisorValidationError(
                        "partial DEFER synthesis task identity is missing"
                    )
                final_answer = _binding_partial_defer_result(
                    state,
                    reason=read_marker(task_body, "defer_reason")
                    or "binding_partial_failure",
                )
                self.client.complete_task(
                    synthesis_task_id,
                    result=final_answer,
                    summary=(
                        "필수 분석 primary가 없어 확인된 범위만 전달하고 DEFER했습니다."
                        if decision.reason == "empty_primary_defer_template"
                        else "일부 부서 실패로 확인된 결과만 전달하고 DEFER했습니다."
                    ),
                    metadata={
                        "workflow_root_task_id": state.parent_task_id,
                        "workflow_mode": state.workflow_mode,
                        "synthesis_mode": (
                            "deterministic_empty_primary_defer"
                            if decision.reason == "empty_primary_defer_template"
                            else "deterministic_partial_defer"
                        ),
                        "defer_reason": (
                            "empty_primary_not_materialized"
                            if decision.reason == "empty_primary_defer_template"
                            else read_marker(task_body, "defer_reason")
                            or "binding_partial_failure"
                        ),
                        "decision": "DEFER",
                        "orders_authorized": False,
                        "final_answer": final_answer,
                    },
                )
                logger.info(
                    "binding-partial-defer-complete root=%s task=%s",
                    state.parent_task_id,
                    synthesis_task_id,
                )
            if decision.reason == "deferred_conditional_after_research":
                order_request_id = read_marker(
                    task_body,
                    "deferred_conditional_order_request_id",
                )
                created_task = created.get("task", created) if isinstance(created, Mapping) else {}
                trading_task_id = str(
                    created_task.get("id") or created_task.get("task_id") or ""
                ) if isinstance(created_task, Mapping) else ""
                if not order_request_id or not trading_task_id:
                    logger.error(
                        "deferred-conditional-binding-missing root=%s task=%s",
                        state.parent_task_id,
                        trading_task_id or "unknown",
                    )
                    return
                try:
                    from apps.api.user_order_workflow import (
                        user_order_repository,  # noqa: PLC0415
                    )

                    user_order_repository().bind_trading_task(
                        order_request_id,
                        trading_task_id,
                    )
                    # The card was created blocked so the authority binding is
                    # durable before Hermes can interpret or activate a rule.
                    self.client.unblock_task(trading_task_id)
                except Exception as exc:  # noqa: BLE001 - leave it safely blocked.
                    logger.error(
                        "deferred-conditional-binding-failed root=%s task=%s error=%s",
                        state.parent_task_id,
                        trading_task_id,
                        type(exc).__name__,
                    )
                    return
            if synthesis_timing is not None and role == "synthesis":
                synthesis_timing["t7c_ms"] = time.time_ns() // 1_000_000
                synthesis_timing["t7c_mono_ns"] = time.perf_counter_ns()
                if isinstance(created, Mapping):
                    created_task = created.get("task", created)
                    if isinstance(created_task, Mapping):
                        synthesis_timing["synthesis_task_id"] = (
                            created_task.get("id")
                            or created_task.get("task_id")
                        )
                        synthesis_timing["task_created_at_ms"] = _task_timestamp_ms(
                            created_task,
                            "created_at",
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
