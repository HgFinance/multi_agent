"""Deterministic idempotency for request-scoped CEO primary tasks.

This module is imported by the build-time Hermes Kanban tool patch and is also
usable by repository-owned callers.  The identity intentionally depends only
on durable workflow scope, role, and canonical assignee; task wording and
producer metadata are not part of it.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCOPE_MARKER = "hgfinance.ceo-workflow-scope.v1"
PRIMARY_ROLE = "primary"
QA_ROLE = "qa"
CONTROL_ROLE = "control"
KNOWN_WORKFLOW_ROLES = frozenset(
    {PRIMARY_ROLE, QA_ROLE}
)
REQUEST_USER_INPUT_ACTION_BODY = (
    "hgfinance.ceo-supervisor.v1 action=REQUEST_USER_INPUT no_analysis_children"
)
REQUEST_USER_INPUT_TITLE = "CEO planner produced no executable child task"
REQUEST_USER_INPUT_SUFFIX = ":supervisor:user-input"
CANONICAL_PRIMARY_ASSIGNEES = frozenset(
    {
        "research-department",
        "research-liaison",
        "quant-backtest-department",
        "quant-liaison",
        "trading-department",
        "accounting-portfolio-department",
        "risk-management",
    }
)


def is_analysis_primary_eligible(profile: Any) -> bool:
    """Return whether ``profile`` may execute an analysis primary task.

    Governance QA deliberately is not in the primary allowlist.  The same
    helper is imported by the CEO-agent Kanban create boundary and by the
    supervisor so those two producers cannot drift.
    """

    return str(profile or "").strip().casefold() in CANONICAL_PRIMARY_ASSIGNEES


def request_user_input_idempotency_key(root_task_id: str) -> str:
    root = str(root_task_id).strip()
    if not root:
        raise ValueError("root_task_id is required")
    return f"{root}{REQUEST_USER_INPUT_SUFFIX}"


def request_user_input_task_body(root_task_id: str) -> str:
    root = str(root_task_id).strip()
    if not root:
        raise ValueError("root_task_id is required")
    return "\n".join(
        (
            SCOPE_MARKER,
            f"workflow_root_task_id={root}",
            f"workflow_role={CONTROL_ROLE}",
            "workflow_mode=analysis",
            "origin=user-query",
            "",
            REQUEST_USER_INPUT_ACTION_BODY,
        )
    )


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _marker_value(body: Any, key: str) -> str | None:
    for line in str(body or "").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == key:
            value = value.strip()
            return value or None
    return None


def _metadata_value(metadata: Any, key: str) -> Any:
    """Read role metadata from the small shapes used by Hermes."""

    if isinstance(metadata, Mapping):
        value = metadata.get(key)
        if value is not None:
            return value
        for nested_key in (
            "metadata",
            "workflow_metadata",
            "task_metadata",
            "run_metadata",
        ):
            value = _metadata_value(metadata.get(nested_key), key)
            if value is not None:
                return value
    return getattr(metadata, key, None)


def _normalize_role(value: Any) -> str | None:
    normalized = str(value or "").strip().casefold()
    return normalized or None


def _key_role_identity(
    idempotency_key: Any,
    assignee: str,
) -> tuple[str, str, str] | None:
    """Parse canonical and legacy scoped role keys.

    New producers use ``<root>:<role>:<assignee>``.  The legacy reader
    compatibility shape is ``<root>:<assignee>:<role>``.  Unknown non-scoped
    keys are ignored because root/control tasks have separate contracts.
    """

    key = str(idempotency_key or "").strip()
    if not key:
        return None
    left, separator, right = key.rpartition(":")
    if not separator:
        return None
    root, separator, middle = left.rpartition(":")
    if not separator or not root.strip():
        return None

    middle_role = _normalize_role(middle)
    right_role = _normalize_role(right)
    if middle_role in KNOWN_WORKFLOW_ROLES:
        return root.strip(), middle_role, right.strip().casefold()
    if right_role in KNOWN_WORKFLOW_ROLES:
        return root.strip(), right_role, middle.strip().casefold()
    return None


def _resolve_create_identity(
    body: Any,
    assignee: Any,
    idempotency_key: Any = None,
    *,
    workflow_role: Any = None,
    metadata: Any = None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve ``(root, role, error)`` for a durable create.

    Explicit role markers must agree with one another and with a scoped
    idempotency key.  Conflicts are returned as errors so every create path
    can fail closed before invoking Hermes or touching durable state.
    """

    body_role = _normalize_role(_marker_value(body, "workflow_role"))
    argument_role = _normalize_role(workflow_role)
    metadata_role = _normalize_role(_metadata_value(metadata, "workflow_role"))
    declared_roles = tuple(
        role for role in (argument_role, body_role, metadata_role) if role is not None
    )
    if len(set(declared_roles)) > 1:
        return None, None, "conflicting workflow_role declarations"
    declared_role = declared_roles[0] if declared_roles else None

    key_identity = _key_role_identity(
        idempotency_key,
        str(assignee or "").strip().casefold(),
    )
    key_root = key_role = key_assignee = None
    if key_identity is not None:
        key_root, key_role, key_assignee = key_identity
        canonical_assignee = str(assignee or "").strip().casefold()
        if key_assignee != canonical_assignee:
            return None, None, "idempotency_key assignee conflicts with create assignee"

    if declared_role and key_role and declared_role != key_role:
        return None, None, "workflow_role conflicts with idempotency_key role"

    body_root = _marker_value(body, "workflow_root_task_id")
    if body_root and key_root and body_root.strip() != key_root.strip():
        return None, None, "workflow_root_task_id conflicts with idempotency_key root"
    return (
        (body_root or key_root or "").strip() or None,
        declared_role or key_role,
        None,
    )


def scoped_primary_identity(
    body: Any,
    assignee: Any,
    idempotency_key: Any = None,
    *,
    workflow_role: Any = None,
    metadata: Any = None,
) -> tuple[str, str] | None:
    """Return ``(root_task_id, canonical_assignee)`` for a primary create.

    The structured body marker is authoritative when present.  The CEO-agent
    contract also carries the same role in an idempotency-key shape.  Accept
    the two production shapes seen across the tool and CLI boundaries as a
    narrow fallback for calls whose body has not yet been decorated.
    """

    root_task_id, resolved_role, error = _resolve_create_identity(
        body,
        assignee,
        idempotency_key,
        workflow_role=workflow_role,
        metadata=metadata,
    )
    if error or resolved_role != PRIMARY_ROLE:
        return None
    canonical_assignee = str(assignee or "").strip().casefold()
    if root_task_id and canonical_assignee:
        return root_task_id, canonical_assignee
    return None


def reject_invalid_primary_create(
    body: Any,
    assignee: Any,
    idempotency_key: Any = None,
    *,
    workflow_role: Any = None,
    metadata: Any = None,
) -> str | None:
    """Return a rejection before an invalid analysis-primary durable create."""

    _, resolved_role, error = _resolve_create_identity(
        body,
        assignee,
        idempotency_key,
        workflow_role=workflow_role,
        metadata=metadata,
    )
    if error:
        return error
    if resolved_role == PRIMARY_ROLE and not is_analysis_primary_eligible(assignee):
        return "CEO primary task assignee is not analysis-primary eligible"
    return None


def validate_primary_create(
    body: Any,
    assignee: Any,
    idempotency_key: Any = None,
    *,
    workflow_role: Any = None,
    metadata: Any = None,
) -> str | None:
    """Validate the complete primary-create contract before durable I/O."""

    rejection = reject_invalid_primary_create(
        body,
        assignee,
        idempotency_key,
        workflow_role=workflow_role,
        metadata=metadata,
    )
    if rejection:
        return rejection
    if requires_scoped_primary_contract(
        body,
        assignee,
        idempotency_key=idempotency_key,
        workflow_role=workflow_role,
        metadata=metadata,
    ):
        return "CEO primary task requires workflow_root_task_id and workflow_role=primary"
    return None


def requires_scoped_primary_contract(
    body: Any,
    assignee: Any,
    idempotency_key: Any = None,
    *,
    workflow_role: Any = None,
    metadata: Any = None,
) -> bool:
    """Whether a CEO create must carry the scoped-primary contract."""

    canonical_assignee = str(assignee or "").strip().casefold()
    return (
        is_analysis_primary_eligible(canonical_assignee)
        and scoped_primary_identity(
            body,
            canonical_assignee,
            idempotency_key=idempotency_key,
            workflow_role=workflow_role,
            metadata=metadata,
        )
        is None
    )


def find_existing_scoped_primary(
    tasks: Any,
    *,
    root_task_id: str,
    assignee: str,
) -> str | None:
    """Find the stable existing primary task, independent of status or prose."""

    expected_root = str(root_task_id).strip()
    expected_assignee = str(assignee).strip().casefold()
    matches: list[tuple[int, str]] = []
    for task in tasks or ():
        task_id = str(_field(task, "id", _field(task, "task_id", "")) or "").strip()
        task_assignee = str(_field(task, "assignee", _field(task, "profile", "")) or "")
        identity = scoped_primary_identity(_field(task, "body", ""), task_assignee)
        if not task_id or identity != (expected_root, expected_assignee):
            continue
        try:
            created_at = int(_field(task, "created_at", 0) or 0)
        except (TypeError, ValueError):
            created_at = 0
        matches.append((created_at, task_id))
    if not matches:
        return None
    # If legacy data already contains duplicates, always reuse the oldest
    # durable card.  This makes repeated calls stable without hiding history.
    return min(matches)[1]


def find_existing_request_user_input(tasks: Any, *, root_task_id: str) -> str | None:
    """Find the durable clarification child for one workflow root."""

    expected_key = request_user_input_idempotency_key(root_task_id)
    matches: list[tuple[int, str]] = []
    for task in tasks or ():
        task_id = str(_field(task, "id", _field(task, "task_id", "")) or "").strip()
        if not task_id or _field(task, "idempotency_key", "") != expected_key:
            continue
        try:
            created_at = int(_field(task, "created_at", 0) or 0)
        except (TypeError, ValueError):
            created_at = 0
        matches.append((created_at, task_id))
    if not matches:
        return None
    return min(matches)[1]


def ensure_request_user_input_task(
    kanban: Any,
    connection: Any,
    *,
    root_task_id: str,
    tenant: str | None = None,
    priority: int = 0,
    session_id: str | None = None,
    created_by: str = "worker",
) -> str:
    """Create or reuse the existing clarification control child exactly once."""

    with scoped_primary_create_lock():
        existing = find_existing_request_user_input(
            kanban.list_tasks(
                connection,
                assignee="ceo-agent",
                tenant=tenant,
                include_archived=True,
            ),
            root_task_id=root_task_id,
        )
        if existing:
            return existing
        return kanban.create_task(
            connection,
            title=REQUEST_USER_INPUT_TITLE,
            body=request_user_input_task_body(root_task_id),
            assignee="ceo-agent",
            created_by=created_by,
            workspace_kind="scratch",
            tenant=tenant,
            priority=int(priority),
            parents=(),
            idempotency_key=request_user_input_idempotency_key(root_task_id),
            initial_status="blocked",
            session_id=session_id,
        )


def _kanban_db_path() -> Path:
    explicit = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if explicit:
        return Path(explicit)
    home = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if home:
        return Path(home) / "kanban.db"
    return Path.home() / ".hermes" / "kanban.db"


@contextmanager
def scoped_primary_create_lock() -> Iterator[None]:
    """Serialize scoped lookup + create across native Hermes processes."""

    db_path = _kanban_db_path()
    lock_path = db_path.with_name(f"{db_path.name}.hgfinance-primary.lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        # Failing closed is required: creating without the lock would restore
        # the duplicate-primary failure mode.
        raise RuntimeError("primary task idempotency lock unavailable") from exc


__all__ = [
    "CANONICAL_PRIMARY_ASSIGNEES",
    "CONTROL_ROLE",
    "PRIMARY_ROLE",
    "REQUEST_USER_INPUT_ACTION_BODY",
    "REQUEST_USER_INPUT_SUFFIX",
    "REQUEST_USER_INPUT_TITLE",
    "SCOPE_MARKER",
    "ensure_request_user_input_task",
    "find_existing_request_user_input",
    "find_existing_scoped_primary",
    "is_analysis_primary_eligible",
    "requires_scoped_primary_contract",
    "request_user_input_idempotency_key",
    "request_user_input_task_body",
    "reject_invalid_primary_create",
    "validate_primary_create",
    "scoped_primary_create_lock",
    "scoped_primary_identity",
]
