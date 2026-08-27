"""Read-only root-scoped discovery for the shared Hermes Kanban board.

The CEO workflow relation is intentionally stored in the task body as a
line-level marker (``workflow_root_task_id=<id>``), not as a Hermes graph edge.
This module makes that existing relation searchable without treating the
search result as task state.  Callers must still hydrate every returned id via
the normal authoritative ``kanban show`` path.

The index is a SQLite virtual generated column over ``tasks.body``.  Hermes
continues to own task writes; inserting or editing a body automatically changes
the generated value, and the B-tree index follows it.  Older/unsupported
SQLite databases fail closed and let the caller use its full-board fallback.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

from orchestration.primary_task_idempotency import REQUEST_USER_INPUT_SUFFIX


ROOT_COLUMN = "workflow_root_task_id"
ROOT_INDEX_NAME = "idx_tasks_workflow_root_task_id"
ROOT_MARKER = "workflow_root_task_id="


class RootScopedIndexUnavailable(RuntimeError):
    """The board cannot safely serve an indexed root query."""


# SQLite has no regexp function in the stock build.  The expression mirrors
# the repository's line-level marker reader: prepend a newline so a marker at
# byte zero is also a line, locate the exact marker prefix, then take the value
# up to the next newline.  CRLF is normalised before parsing.  Valid task IDs
# are non-whitespace, so a malformed value is rejected by the authoritative
# validation step in the caller and falls back to the old path.
_ROOT_EXPRESSION = """CASE
 WHEN instr(char(10) || replace(coalesce(body, ''), char(13), ''),
            char(10) || 'workflow_root_task_id=') = 0 THEN NULL
 ELSE trim(substr(
   substr(
     char(10) || replace(coalesce(body, ''), char(13), ''),
     instr(char(10) || replace(coalesce(body, ''), char(13), ''),
          char(10) || 'workflow_root_task_id=')
       + length(char(10) || 'workflow_root_task_id=')
   ),
   1,
   instr(
     substr(
       char(10) || replace(coalesce(body, ''), char(13), ''),
       instr(char(10) || replace(coalesce(body, ''), char(13), ''),
            char(10) || 'workflow_root_task_id=')
         + length(char(10) || 'workflow_root_task_id=')
     ) || char(10),
     char(10)
   ) - 1
 ))
END"""


def kanban_db_path(environment: Mapping[str, str] | None = None) -> Path:
    """Resolve the exact board database used by the Hermes CLI."""

    env = environment or os.environ
    configured = str(env.get("HERMES_KANBAN_DB") or "").strip()
    if configured:
        return Path(configured).expanduser()
    home = str(env.get("HERMES_KANBAN_HOME") or "").strip()
    if home:
        return Path(home).expanduser() / "kanban.db"
    return Path.home() / ".hermes" / "kanban.db"


class SQLiteRootScopedIndex:
    """Maintain and query the generated-column root index for one board."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self.environment = dict(environment or os.environ)
        self.db_path = kanban_db_path(self.environment)
        self._prepare_lock = threading.Lock()
        self._prepared = False

    @property
    def prepared(self) -> bool:
        return self._prepared

    def prepare(self) -> None:
        """Install the virtual column and B-tree index once, atomically."""

        if self._prepared:
            return
        with self._prepare_lock:
            if self._prepared:
                return
            if not self.db_path.is_file():
                raise RootScopedIndexUnavailable(
                    f"kanban database does not exist: {self.db_path}"
                )
            conn: sqlite3.Connection | None = None
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=1.0)
                conn.execute("PRAGMA busy_timeout=1000")
                columns = {
                    str(row[1]): row
                    for row in conn.execute("PRAGMA table_xinfo(tasks)")
                }
                if not columns:
                    raise RootScopedIndexUnavailable("kanban tasks table is missing")
                existing = columns.get(ROOT_COLUMN)
                if existing is None:
                    conn.execute(
                        "ALTER TABLE tasks ADD COLUMN "
                        f"{ROOT_COLUMN} TEXT GENERATED ALWAYS AS "
                        f"({_ROOT_EXPRESSION}) VIRTUAL"
                    )
                elif int(existing[6] or 0) not in {2, 3}:
                    raise RootScopedIndexUnavailable(
                        f"{ROOT_COLUMN} exists but is not a generated column"
                    )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {ROOT_INDEX_NAME} "
                    f"ON tasks({ROOT_COLUMN})"
                )
                conn.commit()
            except RootScopedIndexUnavailable:
                try:
                    if conn is not None:
                        conn.rollback()
                except Exception:
                    pass
                raise
            except (OSError, sqlite3.Error) as exc:
                try:
                    if conn is not None:
                        conn.rollback()
                except Exception:
                    pass
                raise RootScopedIndexUnavailable(
                    f"cannot prepare root index for {self.db_path}: "
                    f"{type(exc).__name__}"
                ) from exc
            finally:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
            self._prepared = True

    def task_ids(
        self,
        root_task_id: str,
        *,
        include_archived: bool = False,
    ) -> tuple[str, ...]:
        """Return candidate IDs only; never return task state."""

        root = str(root_task_id or "").strip()
        if not root or any(char.isspace() for char in root):
            raise RootScopedIndexUnavailable("root_task_id is malformed")
        self.prepare()
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=1.0)
            conn.execute("PRAGMA busy_timeout=1000")
            archived_filter = "" if include_archived else "AND status != 'archived' "
            rows = conn.execute(
                f"SELECT id FROM tasks WHERE {ROOT_COLUMN} = ? "
                f"{archived_filter}ORDER BY created_at ASC, id ASC",
                (root,),
            ).fetchall()
            return tuple(str(row[0]) for row in rows if row[0])
        except (OSError, sqlite3.Error) as exc:
            raise RootScopedIndexUnavailable(
                f"root index query failed for {self.db_path}: "
                f"{type(exc).__name__}"
            ) from exc
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def root_id_for_task(self, task_id: str) -> str | None:
        """Return a scoped root candidate for one task, or ``None``.

        This is discovery metadata only.  The returned root id is never used
        as workflow state: callers must still hydrate the task, root, and all
        indexed candidates through authoritative ``show`` calls after taking
        the workflow lock.  Legacy parent-linked tasks intentionally return
        ``None`` and retain the existing CLI ancestry fallback.
        """

        task = str(task_id or "").strip()
        if not task or any(char.isspace() for char in task):
            raise RootScopedIndexUnavailable("task_id is malformed")
        self.prepare()
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=1.0)
            conn.execute("PRAGMA busy_timeout=1000")
            row = conn.execute(
                f"SELECT {ROOT_COLUMN} FROM tasks "
                "WHERE id = ? AND status != 'archived'",
                (task,),
            ).fetchone()
            root = str(row[0] or "").strip() if row else ""
            if not root or any(char.isspace() for char in root):
                return None
            return root
        except (OSError, sqlite3.Error) as exc:
            raise RootScopedIndexUnavailable(
                f"root candidate lookup failed for {self.db_path}: "
                f"{type(exc).__name__}"
            ) from exc
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def recovery_candidate_rows(self) -> tuple[dict[str, object], ...]:
        """Return discovery-only rows for active recovery candidates.

        This query intentionally returns only the small set of rows whose
        existing workflow markers can make them recovery candidates.  The
        returned body/status/timestamps are hints for candidate selection;
        callers must revalidate the selected task through authoritative
        ``show`` before taking any action.
        """

        self.prepare()
        conn: sqlite3.Connection | None = None
        try:
            db_uri = f"file:{quote(str(self.db_path.resolve()), safe='/')}?mode=ro"
            conn = sqlite3.connect(db_uri, uri=True, timeout=1.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=1000")
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_xinfo(tasks)")
            }
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            completed_at_expression = (
                "completed_at" if "completed_at" in columns else "NULL"
            )
            # The timeout reconciler runs on a short polling interval.  A
            # completed planning root is only an expiry candidate when it
            # still has a non-terminal primary child.  Keep this as discovery
            # metadata (the supervisor still revalidates with show/workflow),
            # so old completed roots do not trigger repeated authoritative
            # reads on every recovery cycle.
            active_primary_expression = (
                "CASE WHEN EXISTS ("
                "SELECT 1 FROM tasks AS active_child "
                "WHERE active_child.workflow_root_task_id = tasks.id "
                "AND instr(char(10) || replace(coalesce(active_child.body, ''), "
                "char(13), '') || char(10), "
                "char(10) || 'workflow_role=primary' || char(10)) > 0 "
                "AND lower(coalesce(active_child.status, '')) NOT IN ("
                "'done','completed','archived','blocked','failed','gave_up',"
                "'crashed','timed_out','spawn_failed','triage','cancelled'"
                ")"
                ") THEN 1 ELSE 0 END"
                if ROOT_COLUMN in columns
                else "1"
            )
            analysis_child_expression = (
                "CASE WHEN EXISTS ("
                "SELECT 1 FROM tasks AS analysis_child "
                "WHERE analysis_child.workflow_root_task_id = tasks.id "
                "AND instr(char(10) || replace(coalesce(analysis_child.body, ''), "
                "char(13), '') || char(10), "
                "char(10) || 'workflow_role=primary' || char(10)) > 0"
                ") THEN 1 ELSE 0 END"
                if ROOT_COLUMN in columns
                else "0"
            )
            terminal_primary_expression = (
                "CASE WHEN EXISTS ("
                "SELECT 1 FROM tasks AS terminal_primary "
                "WHERE terminal_primary.workflow_root_task_id = tasks.id "
                "AND instr(char(10) || replace(coalesce(terminal_primary.body, ''), "
                "char(13), '') || char(10), "
                "char(10) || 'workflow_role=primary' || char(10)) > 0 "
                "AND lower(coalesce(terminal_primary.status, '')) IN ("
                "'done','completed','archived','blocked','failed','gave_up',"
                "'crashed','timed_out','spawn_failed','triage','cancelled'"
                ")"
                ") THEN 1 ELSE 0 END"
                if ROOT_COLUMN in columns
                else "0"
            )
            synthesis_expression = (
                "CASE WHEN EXISTS ("
                "SELECT 1 FROM tasks AS synthesis_child "
                "WHERE synthesis_child.workflow_root_task_id = tasks.id "
                "AND instr(char(10) || replace(coalesce(synthesis_child.body, ''), "
                "char(13), '') || char(10), "
                "char(10) || 'workflow_role=synthesis' || char(10)) > 0"
                ") THEN 1 ELSE 0 END"
                if ROOT_COLUMN in columns
                else "0"
            )
            selection_comment_expression = (
                "CASE WHEN EXISTS ("
                "SELECT 1 FROM task_comments AS selection_comment "
                "WHERE selection_comment.task_id = tasks.id "
                "AND instr(replace(coalesce(selection_comment.body, ''), "
                "char(13), ''), 'selected_primary_profiles=') > 0"
                ") THEN 1 ELSE 0 END"
                if "task_comments" in tables
                else "0"
            )
            # An empty-primary clarification is a durable terminal handling
            # marker for the invalid plan.  Exclude only that exact control
            # child from recovery discovery; other REQUEST_USER_INPUT tasks
            # remain eligible for the existing recovery paths.
            control_key_clause = ""
            query_parameters: tuple[object, ...] = ()
            if "idempotency_key" in columns:
                control_key_clause = (
                    "AND control.idempotency_key = tasks.id || ? "
                )
                query_parameters = (REQUEST_USER_INPUT_SUFFIX,)
            rows = conn.execute(
                "SELECT id, body, status, created_at, "
                f"{completed_at_expression} AS completed_at, "
                f"{active_primary_expression} AS has_active_primary, "
                f"{analysis_child_expression} AS has_analysis_child, "
                f"{terminal_primary_expression} AS has_terminal_primary, "
                f"{synthesis_expression} AS has_synthesis, "
                f"{selection_comment_expression} AS has_selection_comment "
                "FROM tasks "
                "WHERE body IS NOT NULL AND ("
                "instr(body, 'workflow_role=root') > 0 OR "
                "instr(body, 'root_task_role=scope_and_planning') > 0 OR "
                "instr(body, 'workflow_role=synthesis') > 0"
                ") AND NOT EXISTS ("
                "SELECT 1 FROM tasks AS control "
                "WHERE control.workflow_root_task_id = tasks.id "
                "AND control.status != 'archived' "
                "AND instr("
                "char(10) || replace(coalesce(control.body, ''), char(13), '') "
                "|| char(10), char(10) || 'workflow_role=control' || char(10)"
                ") > 0 "
                "AND instr(control.body, 'action=REQUEST_USER_INPUT') > 0 "
                "AND instr(control.body, 'no_analysis_children') > 0 "
                f"{control_key_clause}"
                ") ORDER BY created_at ASC, id ASC",
                query_parameters,
            ).fetchall()
            return tuple(
                {
                    "id": str(row["id"]),
                    "body": row["body"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "completed_at": row["completed_at"],
                    "has_active_primary": bool(row["has_active_primary"]),
                    "has_analysis_child": bool(row["has_analysis_child"]),
                    "has_terminal_primary": bool(row["has_terminal_primary"]),
                    "has_synthesis": bool(row["has_synthesis"]),
                    "has_selection_comment": bool(row["has_selection_comment"]),
                }
                for row in rows
                if row["id"]
            )
        except (OSError, sqlite3.Error) as exc:
            raise RootScopedIndexUnavailable(
                f"recovery candidate query failed for {self.db_path}: "
                f"{type(exc).__name__}"
            ) from exc
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass


__all__ = [
    "ROOT_COLUMN",
    "ROOT_INDEX_NAME",
    "ROOT_MARKER",
    "RootScopedIndexUnavailable",
    "SQLiteRootScopedIndex",
    "kanban_db_path",
]
