"""Durable scope contract for one CEO Kanban workflow.

Hermes supplies recent work by the assignee in a worker's context.  That is
useful background, but those task IDs are not part of the current workflow
graph.  This module defines the small machine-readable contract used by the
BFF and supervisor to keep a fresh CEO request isolated from that history.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


CEO_WORKFLOW_SCOPE_MARKER = "hgfinance.ceo-workflow-scope.v1"
CEO_WORKFLOW_SCOPE_POLICY = "fresh"
CEO_WORKFLOW_REUSE_POLICY = "disabled"

_TASK_ID_RE = re.compile(r"\bt_[a-f0-9]{8,}\b")
_REFERENCE_KEYS = frozenset(
    {
        "department_tasks",
        "qa_task",
        "qa_task_id",
        "primary_task_ids",
        "analysis_task_ids",
        "qa_dependency_ids",
        "synthesis_task_ids",
    }
)
_ROOT_KEYS = frozenset(
    {"current_root_task_id", "workflow_root_task_id", "root_task_id"}
)


class WorkflowScopeViolation(ValueError):
    """A task reference or parent edge escapes the active root graph."""


@dataclass(frozen=True)
class WorkflowScopeReferences:
    """Task IDs declared by machine-readable workflow metadata/comments."""

    root_ids: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()


def build_root_body(query: str, request_id: str) -> str:
    """Build a root body that is unambiguous before the root ID exists."""

    return (
        f"{CEO_WORKFLOW_SCOPE_MARKER}\n"
        f"workflow_scope={CEO_WORKFLOW_SCOPE_POLICY}\n"
        f"reuse_policy={CEO_WORKFLOW_REUSE_POLICY}\n"
        f"request_id={request_id}\n"
        "root_task_role=scope_and_planning\n"
        "primary_execution_parent=none\n"
        "primary_scope_field=workflow_root_task_id\n"
        "planning_terminal_state=done_after_child_creation\n"
        "Primary tasks must bind workflow_root_task_id to this task ID in their body;\n"
        "do not pass this root as a Hermes execution parent for primary tasks.\n"
        "Only task IDs carrying this workflow root marker may be used.\n"
        "Do not reuse IDs from recent work, memory, or another root.\n\n"
        "## User request\n"
        f"{query}"
    )


def build_root_comment(root_task_id: str, request_id: str) -> str:
    """Build the post-create comment that binds the concrete root ID."""

    return (
        f"{CEO_WORKFLOW_SCOPE_MARKER} "
        f"root_task_id={root_task_id} "
        f"request_id={request_id} "
        f"workflow_scope={CEO_WORKFLOW_SCOPE_POLICY} "
        f"reuse_policy={CEO_WORKFLOW_REUSE_POLICY}"
    )


def build_scoped_task_body(
    body: str,
    root_task_id: str,
    *,
    role: str,
    request_id: str | None = None,
) -> str:
    """Bind a task to a workflow without creating a dependency edge."""

    root_task_id = str(root_task_id).strip()
    if not root_task_id:
        raise ValueError("root_task_id must not be empty")
    role = str(role).strip().casefold()
    if role not in {"primary", "qa", "synthesis", "control"}:
        raise ValueError("workflow task role must be primary, qa, synthesis, or control")

    metadata = [
        CEO_WORKFLOW_SCOPE_MARKER,
        f"workflow_root_task_id={root_task_id}",
        f"workflow_role={role}",
    ]
    if request_id:
        metadata.append(f"request_id={request_id}")
    prefix = "\n".join(metadata)
    body = str(body or "").strip()
    return f"{prefix}\n\n{body}" if body else prefix


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _task_ids(value: Any) -> tuple[str, ...]:
    """Extract IDs from the supported structured metadata shapes."""

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return tuple(_TASK_ID_RE.findall(value))
        return _task_ids(decoded)
    if isinstance(value, Mapping):
        direct = value.get("task_id") or value.get("id")
        if direct:
            return (str(direct),)
        return _ordered_unique(
            tuple(task_id for item in value.values() for task_id in _task_ids(item))
        )
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return _ordered_unique(
            tuple(task_id for item in value for task_id in _task_ids(item))
        )
    return ()


def _labelled_ids(text: str, labels: frozenset[str]) -> tuple[str, ...]:
    """Read IDs from a human-readable comment only when it names a field."""

    ids: list[str] = []
    for label in labels:
        match = re.search(
            rf"\b{re.escape(label)}\s*[:=]\s*([^\n]+)", text,
            flags=re.IGNORECASE,
        )
        if match:
            ids.extend(_TASK_ID_RE.findall(match.group(1)))
    return _ordered_unique(ids)


def extract_scope_references(payload: Mapping[str, Any]) -> WorkflowScopeReferences:
    """Collect only declared workflow references, never arbitrary prose IDs."""

    root_ids: list[str] = []
    task_ids: list[str] = []

    def visit(value: Any, key: str | None = None) -> None:
        if key in _ROOT_KEYS:
            root_ids.extend(_task_ids(value))
        elif key in _REFERENCE_KEYS:
            task_ids.extend(_task_ids(value))

        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key).casefold())
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                visit(item, key)
        elif isinstance(value, str) and key in {"body", "text", "comment"}:
            root_ids.extend(_labelled_ids(value, _ROOT_KEYS))
            task_ids.extend(_labelled_ids(value, _REFERENCE_KEYS))

    visit(payload)
    return WorkflowScopeReferences(
        root_ids=_ordered_unique(root_ids),
        task_ids=_ordered_unique(task_ids),
    )


def validate_workflow_scope(
    *,
    root_task_id: str,
    root_payload: Mapping[str, Any],
    descendants: Sequence[Mapping[str, Any]],
) -> None:
    """Fail closed when metadata or graph edges leave ``root_task_id``."""

    graph_ids = {root_task_id}
    graph_ids.update(
        str(payload.get("id") or payload.get("task_id"))
        for payload in descendants
        if payload.get("id") or payload.get("task_id")
    )

    refs = extract_scope_references(root_payload)
    wrong_roots = set(refs.root_ids) - {root_task_id}
    if wrong_roots:
        raise WorkflowScopeViolation(
            f"declared root IDs outside active root {root_task_id}: "
            f"{sorted(wrong_roots)}"
        )

    outside = set(refs.task_ids) - graph_ids
    if outside:
        raise WorkflowScopeViolation(
            f"workflow metadata references non-descendant task IDs under "
            f"{root_task_id}: {sorted(outside)}"
        )

    for payload in descendants:
        task_refs = extract_scope_references(payload)
        wrong_task_roots = set(task_refs.root_ids) - {root_task_id}
        if wrong_task_roots:
            task_id = payload.get("id") or payload.get("task_id") or "unknown"
            raise WorkflowScopeViolation(
                f"task {task_id} declares root IDs outside active root "
                f"{root_task_id}: {sorted(wrong_task_roots)}"
            )

    # A descendant discovered through root.children must not secretly point
    # at a different root.  QA and synthesis legitimately have multiple
    # parents, but every parent must still be inside this graph.
    for payload in descendants:
        parent_ids = _task_ids(payload.get("parents"))
        outside_parents = set(parent_ids) - graph_ids
        if outside_parents:
            task_id = payload.get("id") or payload.get("task_id") or "unknown"
            raise WorkflowScopeViolation(
                f"task {task_id} has parent IDs outside active root "
                f"{root_task_id}: {sorted(outside_parents)}"
            )
        if extract_scope_references(payload).root_ids and root_task_id in parent_ids:
            task_id = payload.get("id") or payload.get("task_id") or "unknown"
            raise WorkflowScopeViolation(
                f"scoped task {task_id} must not use workflow root "
                f"{root_task_id} as an execution parent"
            )


__all__ = [
    "CEO_WORKFLOW_REUSE_POLICY",
    "CEO_WORKFLOW_SCOPE_MARKER",
    "CEO_WORKFLOW_SCOPE_POLICY",
    "WorkflowScopeReferences",
    "WorkflowScopeViolation",
    "build_root_body",
    "build_root_comment",
    "extract_scope_references",
    "validate_workflow_scope",
]
