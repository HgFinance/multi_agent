from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from orchestration.maintenance_retention import (
    HealthLedger,
    MaintenanceJob,
    _job_loop,
    build_jobs,
    healthcheck,
)


def test_health_ledger_keeps_independent_job_results(tmp_path: Path) -> None:
    now = [100.0]
    path = tmp_path / "health.json"
    ledger = HealthLedger(path, wall_clock=lambda: now[0])
    first = MaintenanceJob("kanban", 10, lambda: None, 30)
    second = MaintenanceJob("notion", 20, lambda: None, 40)

    ledger.started(first)
    now[0] = 101.0
    ledger.finished(first)
    ledger.started(second)
    now[0] = 102.0
    ledger.finished(second, error=RuntimeError("unavailable"))

    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["jobs"]["kanban"]["status"] == "ok"
    assert state["jobs"]["notion"]["status"] == "failed"
    assert state["jobs"]["notion"]["error"] == "RuntimeError"
    assert not healthcheck(path, now=103.0)


def test_healthcheck_rejects_stale_scheduler_and_overdue_job(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    path.write_text(
        json.dumps(
            {
                "heartbeat": 100.0,
                "jobs": {
                    "kanban": {
                        "status": "running",
                        "started_at": 90.0,
                        "max_run_seconds": 5.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert not healthcheck(path, now=101.0)

    path.write_text(json.dumps({"heartbeat": 100.0, "jobs": {}}), encoding="utf-8")
    assert not healthcheck(path, now=116.0)


def test_healthcheck_rejects_missing_or_invalid_state(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    assert not healthcheck(path)
    path.write_text("not-json", encoding="utf-8")
    assert not healthcheck(path)


def test_scheduler_keeps_existing_retention_domains(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_RETENTION_INTERVAL_SECONDS", "123")
    jobs = build_jobs(dry_run=True)

    assert [job.name for job in jobs] == [
        "kanban",
        "memo-harness",
        "notion",
        "discord",
        "langsmith",
    ]
    assert jobs[-1].interval_seconds == 123


def test_scheduler_projects_worker_error_code_into_health(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    ledger = HealthLedger(path)
    job = MaintenanceJob(
        "langsmith",
        10,
        lambda: SimpleNamespace(error_code="TRACE_DELETE_HOURLY_LIMIT"),
        30,
    )

    _job_loop(job, threading.Event(), ledger, once=True)

    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["jobs"]["langsmith"]["status"] == "failed"
    assert state["jobs"]["langsmith"]["error"] == "MaintenanceResultError"
    assert state["jobs"]["langsmith"]["error_code"] == "TRACE_DELETE_HOURLY_LIMIT"


def test_scheduler_treats_explicit_langsmith_egress_disable_as_a_healthy_skip(
    tmp_path: Path,
) -> None:
    path = tmp_path / "health.json"
    ledger = HealthLedger(path)
    job = MaintenanceJob(
        "langsmith",
        10,
        lambda: SimpleNamespace(error_code="LANGSMITH_EGRESS_DISABLED"),
        30,
    )

    _job_loop(job, threading.Event(), ledger, once=True)

    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["jobs"]["langsmith"]["status"] == "ok"
    assert state["jobs"]["langsmith"]["error_code"] is None
    assert healthcheck(path)


def test_scheduler_exposes_async_langsmith_delete_backlog_without_failing_health(
    tmp_path: Path,
) -> None:
    path = tmp_path / "health.json"
    ledger = HealthLedger(path)
    job = MaintenanceJob(
        "langsmith",
        10,
        lambda: SimpleNamespace(
            scanned=10,
            eligible=2,
            deleted=2,
            queued=2,
            pending_visible=2,
            visible_overflow=2,
            skipped=0,
            error_code=None,
        ),
        30,
    )

    _job_loop(job, threading.Event(), ledger, once=True)

    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["jobs"]["langsmith"]["status"] == "ok"
    assert state["jobs"]["langsmith"]["warning"] == "LANGSMITH_DELETE_PENDING"
    assert state["jobs"]["langsmith"]["result"]["pending_visible"] == 2
    assert healthcheck(path)
