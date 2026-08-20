"""Small shared lock for workflow mutations and retention maintenance.

The supervisor already serializes decisions inside one process. Retention runs
in a separate maintenance process, so that in-memory lock is not enough for
the final archive/purge transaction. This lock is intentionally acquired only
around short mutation sections; expensive workflow reconstruction stays
outside it.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def kanban_db_path(environment: dict[str, str] | None = None) -> Path:
    env = environment or os.environ
    configured = env.get("HERMES_KANBAN_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    home = env.get("HERMES_KANBAN_HOME", "").strip()
    if home:
        return Path(home).expanduser() / "kanban.db"
    return Path.home() / ".hermes-shared-kanban" / "kanban.db"


def lock_path(environment: dict[str, str] | None = None) -> Path:
    env = environment or os.environ
    configured = env.get("HERMES_KANBAN_RETENTION_LOCK", "").strip()
    if configured:
        return Path(configured).expanduser()
    return kanban_db_path(env).with_name("retention.lock")


@contextmanager
def workflow_mutation_lock(
    root_id: str | None = None,
    *,
    environment: dict[str, str] | None = None,
) -> Iterator[None]:
    """Serialize short workflow mutations with the retention lane.

    ``root_id`` is accepted for call-site clarity and future per-root lock
    sharding. A single board lock is safer for legacy parent-linked tasks and
    keeps the change compatible with the existing Hermes SQLite ownership.
    """

    del root_id
    path = lock_path(environment)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows fallback
            pass
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover - Windows fallback
                pass


__all__ = ["kanban_db_path", "lock_path", "workflow_mutation_lock"]
