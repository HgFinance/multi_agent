from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "departments/07-agent-workforce/scorecard"))

from kanban_latency import collect_kanban_department_latency


def test_kanban_latency_reads_lifecycle_metadata_once_per_task(tmp_path: Path) -> None:
    database_path = tmp_path / "kanban.db"
    with sqlite3.connect(database_path) as database:
        database.executescript(
            """
            create table tasks (
              id text primary key, assignee text, created_at integer,
              started_at integer, completed_at integer
            );
            create table task_runs (
              id integer primary key, task_id text, started_at integer, ended_at integer, status text
            );
            """
        )
        database.executemany(
            "insert into tasks values (?, ?, ?, ?, ?)",
            [
                ("r1", "research-department", 100, 102, 112),
                ("r2", "research-department", 120, 125, 145),
                ("q1", "qa-department", 130, 131, 191),
            ],
        )
        database.executemany(
            "insert into task_runs values (?, ?, ?, ?, ?)",
            [
                (1, "r1", 102, 112, "done"),
                (2, "r2", 125, 145, "timed_out"),
                (3, "q1", 131, 191, "crashed"),
            ],
        )

    start = datetime.fromtimestamp(90, timezone.utc)
    reports = collect_kanban_department_latency(
        department_profiles={"research": "research-department", "qa": "qa-department"},
        window_start=start,
        window_end=start + timedelta(seconds=120),
        database_path=database_path,
    )
    research, qa = reports
    assert research.completed_tasks == 2
    # Existing Workforce percentile semantics choose the lower middle sample
    # for an even count, so this must stay aligned with Langfuse capacity.
    assert research.queue_p50_ms == 2_000
    assert research.workflow_p95_ms == 25_000
    assert research.execution_p95_ms == 20_000
    assert research.failure_count == 1
    assert research.timeout_count == 1
    assert qa.completed_tasks == 1
    assert qa.crash_count == 1


def test_kanban_latency_separates_blocked_wall_time_from_active_execution(tmp_path: Path) -> None:
    database_path = tmp_path / "kanban.db"
    with sqlite3.connect(database_path) as database:
        database.executescript(
            """
            create table tasks (
              id text primary key, assignee text, created_at integer,
              started_at integer, completed_at integer
            );
            create table task_runs (
              id integer primary key, task_id text, started_at integer, ended_at integer, status text
            );
            insert into tasks values ('qa-1', 'qa-department', 100, 101, 20501);
            insert into task_runs values (1, 'qa-1', 101, 131, 'crashed');
            insert into task_runs values (2, 'qa-1', 131, 265, 'gave_up');
            insert into task_runs values (3, 'qa-1', 20479, 20501, 'done');
            """
        )

    start = datetime.fromtimestamp(90, timezone.utc)
    report = collect_kanban_department_latency(
        department_profiles={"qa": "qa-department"},
        window_start=start,
        window_end=start + timedelta(seconds=30_000),
        database_path=database_path,
    )[0]
    assert report.workflow_p95_ms == 20_401_000
    assert report.execution_p95_ms == 186_000
    assert report.timeout_count == 0
    assert report.gave_up_count == 1
    assert report.crash_count == 1
