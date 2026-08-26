from __future__ import annotations

import json
from pathlib import Path

from orchestration.maintenance_retention import (
    HealthLedger,
    MaintenanceJob,
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
    assert healthcheck(path, now=103.0)


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
