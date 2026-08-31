"""Read-only, department-level latency summaries from the shared Kanban board.

Langfuse measures model/worker events.  Kanban is the durable source for the
time a department task spent waiting and executing.  These are intentionally
reported as a separate metric family: merging the two would turn a missing
Langfuse event into a false execution measurement (or vice versa).

Only task lifecycle metadata is selected here.  Task bodies, results, run
summaries, errors, and other user content are never read.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class KanbanLatencyStatus(str, Enum):
    MEASURED = "MEASURED"
    UNAVAILABLE = "UNAVAILABLE"


_FAILURE_OUTCOMES = frozenset({"crashed", "reclaimed", "timed_out"})


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _epoch_seconds(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("kanban latency window must be timezone-aware")
    return int(value.astimezone(timezone.utc).timestamp())


def _database_path(path: str | Path | None) -> Path | None:
    raw = str(path or os.getenv("HERMES_KANBAN_DB", "")).strip()
    if not raw:
        return None
    candidate = Path(raw)
    return candidate if candidate.is_file() else None


@dataclass(frozen=True)
class KanbanDepartmentLatencyReport:
    """One department's completed-task latency in a fixed observation window."""

    department: str
    profile: str
    window_start: datetime
    window_end: datetime
    status: KanbanLatencyStatus
    completed_tasks: int | None
    queue_p50_ms: int | None
    queue_p95_ms: int | None
    execution_p50_ms: int | None
    execution_p95_ms: int | None
    failure_count: int | None
    timeout_count: int | None
    crash_count: int | None
    reclaim_count: int | None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "department": self.department,
            "profile": self.profile,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "status": self.status.value,
            "completed_tasks": self.completed_tasks,
            "queue_p50_ms": self.queue_p50_ms,
            "queue_p95_ms": self.queue_p95_ms,
            "execution_p50_ms": self.execution_p50_ms,
            "execution_p95_ms": self.execution_p95_ms,
            "failure_count": self.failure_count,
            "timeout_count": self.timeout_count,
            "crash_count": self.crash_count,
            "reclaim_count": self.reclaim_count,
            "reason": self.reason,
        }


def _unavailable_reports(
    *,
    department_profiles: Mapping[str, str],
    window_start: datetime,
    window_end: datetime,
    reason: str,
) -> tuple[KanbanDepartmentLatencyReport, ...]:
    return tuple(
        KanbanDepartmentLatencyReport(
            department=department,
            profile=profile,
            window_start=window_start,
            window_end=window_end,
            status=KanbanLatencyStatus.UNAVAILABLE,
            completed_tasks=None,
            queue_p50_ms=None,
            queue_p95_ms=None,
            execution_p50_ms=None,
            execution_p95_ms=None,
            failure_count=None,
            timeout_count=None,
            crash_count=None,
            reclaim_count=None,
            reason=reason,
        )
        for department, profile in department_profiles.items()
    )


def collect_kanban_department_latency(
    *,
    department_profiles: Mapping[str, str],
    window_start: datetime,
    window_end: datetime,
    database_path: str | Path | None = None,
) -> tuple[KanbanDepartmentLatencyReport, ...]:
    """Summarize completed department tasks from one read-only SQLite query.

    A task is counted once, using the run that started with its current task
    execution.  This protects retry history from inflating a department's
    arrival count.  Failure outcomes are retained as tail diagnostics instead
    of silently dropping them from p95.
    """

    if window_end <= window_start:
        raise ValueError("kanban latency window end must follow start")
    if not department_profiles:
        return ()
    path = _database_path(database_path)
    if path is None:
        return _unavailable_reports(
            department_profiles=department_profiles,
            window_start=window_start,
            window_end=window_end,
            reason="kanban_db_unavailable",
        )

    placeholders = ",".join("?" for _ in department_profiles)
    query = f"""
        select t.assignee, t.created_at, t.started_at, t.completed_at,
               coalesce(r.status, '')
        from tasks t
        left join task_runs r
          on r.id = (
              select max(candidate.id)
              from task_runs candidate
              where candidate.task_id = t.id
                and candidate.started_at = t.started_at
          )
        where t.assignee in ({placeholders})
          and t.completed_at >= ? and t.completed_at < ?
          and t.created_at is not null and t.started_at is not null
          and t.completed_at >= t.started_at
          and t.started_at >= t.created_at
    """
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.25) as database:
            database.execute("pragma query_only=on")
            rows = database.execute(
                query,
                (*department_profiles.values(), _epoch_seconds(window_start), _epoch_seconds(window_end)),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return _unavailable_reports(
            department_profiles=department_profiles,
            window_start=window_start,
            window_end=window_end,
            reason="kanban_db_read_failed",
        )

    samples: dict[str, list[tuple[int, int, str]]] = {
        profile: [] for profile in department_profiles.values()
    }
    for profile, created_at, started_at, completed_at, run_status in rows:
        samples[str(profile)].append(
            (
                max(0, (int(started_at) - int(created_at)) * 1_000),
                max(0, (int(completed_at) - int(started_at)) * 1_000),
                str(run_status).casefold(),
            )
        )

    reports: list[KanbanDepartmentLatencyReport] = []
    for department, profile in department_profiles.items():
        values = samples[profile]
        queues = [item[0] for item in values]
        executions = [item[1] for item in values]
        outcomes = [item[2] for item in values]
        reports.append(
            KanbanDepartmentLatencyReport(
                department=department,
                profile=profile,
                window_start=window_start,
                window_end=window_end,
                status=KanbanLatencyStatus.MEASURED,
                completed_tasks=len(values),
                queue_p50_ms=_percentile(queues, 0.50),
                queue_p95_ms=_percentile(queues, 0.95),
                execution_p50_ms=_percentile(executions, 0.50),
                execution_p95_ms=_percentile(executions, 0.95),
                failure_count=sum(item in _FAILURE_OUTCOMES for item in outcomes),
                timeout_count=outcomes.count("timed_out"),
                crash_count=outcomes.count("crashed"),
                reclaim_count=outcomes.count("reclaimed"),
            )
        )
    return tuple(reports)
