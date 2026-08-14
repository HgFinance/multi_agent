"""Deterministic idempotency for request-scoped CEO primary tasks.

This module is imported by the build-time Hermes Kanban tool patch and is also
usable by repository-owned callers.  The identity intentionally depends only
on durable workflow scope, role, and canonical assignee; task wording and
producer metadata are not part of it.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCOPE_MARKER = "hgfinance.ceo-workflow-scope.v1"
PRIMARY_ROLE = "primary"


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


def scoped_primary_identity(body: Any, assignee: Any) -> tuple[str, str] | None:
    """Return ``(root_task_id, canonical_assignee)`` for a primary body."""

    if SCOPE_MARKER not in {line.strip() for line in str(body or "").splitlines()}:
        return None
    if _marker_value(body, "workflow_role") != PRIMARY_ROLE:
        return None
    root_task_id = _marker_value(body, "workflow_root_task_id")
    canonical_assignee = str(assignee or "").strip().casefold()
    if not root_task_id or not canonical_assignee:
        return None
    return root_task_id, canonical_assignee


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
    "PRIMARY_ROLE",
    "SCOPE_MARKER",
    "find_existing_scoped_primary",
    "scoped_primary_create_lock",
    "scoped_primary_identity",
]
