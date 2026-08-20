from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.api import ceo_kanban_read
from orchestration.adapters.ceo_supervisor import HermesKanbanClient
from orchestration.kanban_retention import (
    AuditStore,
    DeliveryState,
    RetentionWorker,
    SQLiteKanbanMaintenance,
    evaluate_workflow,
)
from orchestration.kanban_retention_lock import workflow_mutation_lock


NOW = 2_000_000_000
ROOT = "root-retention"
PRIMARY = "primary-retention"
SYNTHESIS = "synthesis-retention"


def _node(
    task_id: str,
    *,
    status: str = "done",
    role: str | None = None,
    completed_at: int | None = NOW - 2 * 24 * 3600,
    runs: list[dict[str, object]] | None = None,
    body_extra: str = "",
) -> dict[str, object]:
    role_line = f"workflow_role={role}\n" if role else ""
    body = (
        "hgfinance.ceo-workflow-scope.v1\n"
        f"workflow_root_task_id={ROOT}\n"
        f"{role_line}{body_extra}"
    )
    return {
        "id": task_id,
        "title": task_id,
        "body": body,
        "assignee": "ceo-agent" if task_id in {ROOT, SYNTHESIS} else "research-department",
        "status": status,
        "created_at": NOW - 3 * 24 * 3600,
        "completed_at": completed_at,
        "parents": [ROOT] if task_id == PRIMARY else [PRIMARY] if task_id == SYNTHESIS else [],
        "children": [PRIMARY] if task_id == ROOT else [SYNTHESIS] if task_id == PRIMARY else [],
        "runs": runs if runs is not None else [
            {"status": "done", "outcome": "completed", "ended_at": completed_at}
        ],
        "latest_summary": "decision: HOLD" if task_id == SYNTHESIS else "complete",
    }


def _workflow(*, root_status: str = "done", child_status: str = "done", **root_changes):
    root = _node(
        ROOT,
        status=root_status,
        completed_at=root_changes.pop("completed_at", NOW - 2 * 24 * 3600),
        body_extra=(
            "root_task_role=scope_and_planning\n"
            "request_id=req-retention\n"
            "## User request\nretention test\n"
        ),
    )
    root.update(root_changes)
    primary = _node(PRIMARY, status=child_status)
    synthesis = _node(SYNTHESIS, role="synthesis")
    return ceo_kanban_read.Workflow(
        root_task_id=ROOT,
        nodes=tuple(
            ceo_kanban_read.WorkflowNode.from_hermes(payload)
            for payload in (root, primary, synthesis)
        ),
        metadata={},
        root_payload=root,
    )


class FakeMaintenance:
    def __init__(self) -> None:
        self.archived: list[tuple[str, tuple[str, ...]]] = []
        self.purged: list[tuple[str, tuple[str, ...]]] = []

    def archive_workflow(self, root_id: str, task_ids: list[str]) -> bool:
        self.archived.append((root_id, tuple(task_ids)))
        return True

    def purge_workflow(self, root_id: str, task_ids: list[str]) -> bool:
        self.purged.append((root_id, tuple(task_ids)))
        return True


def _decision(workflow, *, delivery: DeliveryState = DeliveryState("not_required")):
    return evaluate_workflow(workflow, now=NOW, delivery=delivery)


def test_active_workflow_archive_zero() -> None:
    result = _decision(_workflow(root_status="running"))
    assert not result.eligible
    assert result.reason == "root_not_terminal"


def test_unfinished_descendant_archive_zero() -> None:
    result = _decision(_workflow(child_status="running"))
    assert not result.eligible
    assert result.reason == f"unfinished:{PRIMARY}"


def test_recovery_pending_archive_zero() -> None:
    result = _decision(_workflow(**{"children": []}))
    # Inject the marker on the authoritative child row, not on an LLM result.
    pending = _workflow()
    node = pending.nodes[1]
    raw = dict(node.raw)
    raw["recovery_pending"] = True
    replacement = ceo_kanban_read.WorkflowNode.from_hermes(raw)
    pending = ceo_kanban_read.Workflow(
        root_task_id=ROOT,
        nodes=(pending.nodes[0], replacement, pending.nodes[2]),
        root_payload=pending.root_payload,
    )
    result = _decision(pending)
    assert not result.eligible
    assert result.reason == f"recovery_pending:{PRIMARY}"


def test_terminal_under_24_hours_archive_zero() -> None:
    result = _decision(_workflow(completed_at=NOW - 23 * 3600))
    assert not result.eligible
    assert result.reason == "terminal_under_24h"


def test_terminal_over_24_hours_and_safe_archives() -> None:
    result = _decision(_workflow())
    assert result.eligible
    assert result.reason == "safe"


def test_delivery_failure_and_missing_thread_are_not_archivable() -> None:
    for state in ("failed", "missing_thread", "pending"):
        result = _decision(_workflow(), delivery=DeliveryState(state))
        assert not result.eligible
        assert result.reason == f"discord_delivery_{state}"


def test_repeated_policy_evaluation_is_idempotent() -> None:
    workflow = _workflow()
    first = _decision(workflow)
    second = _decision(workflow)
    assert first == second


def test_active_reconstruction_excludes_archived_rows() -> None:
    payloads = {
        ROOT: _node(ROOT, status="done"),
        PRIMARY: _node(PRIMARY),
        SYNTHESIS: _node(SYNTHESIS, role="synthesis"),
    }
    calls: list[bool] = []

    def fetch(task_id: str):
        return payloads[task_id]

    def listed(*, include_archived: bool = False):
        calls.append(include_archived)
        return list(payloads.values())

    with patch.object(ceo_kanban_read, "list_tasks", listed):
        ceo_kanban_read.load_workflow(ROOT, fetch=fetch)
    assert calls == [False]


def test_archive_then_purge_preserves_audit_and_forbids_under_7_days(tmp_path: Path) -> None:
    maintenance = FakeMaintenance()
    audit = AuditStore(tmp_path / "retention-audit.db")
    active = [{"id": ROOT, "body": _workflow().root.body, "status": "done"}]
    archived_workflow = _workflow()
    archived_workflow = ceo_kanban_read.Workflow(
        root_task_id=ROOT,
        nodes=tuple(
            ceo_kanban_read.WorkflowNode.from_hermes(
                dict(node.raw, status="archived")
            )
            for node in archived_workflow.nodes
        ),
        root_payload=dict(archived_workflow.root_payload, status="archived"),
    )
    state = {"archived": False}

    def rows(*, include_archived: bool = False):
        return [] if state["archived"] or include_archived else active

    def loader(_root_id: str, *, include_archived: bool = False):
        return archived_workflow if state["archived"] else _workflow()

    worker = RetentionWorker(
        maintenance=maintenance,
        audit=audit,
        workflow_loader=loader,
        row_lister=rows,
        clock=lambda: NOW,
    )
    first = worker.run_once()
    assert first.archived_count == 1
    assert maintenance.archived
    assert audit.get(ROOT) is not None

    state["archived"] = True
    before_purge = RetentionWorker(
        maintenance=maintenance,
        audit=audit,
        workflow_loader=loader,
        row_lister=rows,
        clock=lambda: NOW + 7 * 24 * 3600,
    ).run_once()
    assert before_purge.purged_count == 0

    after_purge = RetentionWorker(
        maintenance=maintenance,
        audit=audit,
        workflow_loader=loader,
        row_lister=rows,
        clock=lambda: NOW + 7 * 24 * 3600 + 1,
    ).run_once()
    assert after_purge.purged_count == 1
    assert audit.get(ROOT) is not None
    assert audit.get(ROOT)["purged_at"] == NOW + 7 * 24 * 3600 + 1


def test_purge_requires_audit_row(tmp_path: Path) -> None:
    maintenance = FakeMaintenance()
    worker = RetentionWorker(
        maintenance=maintenance,
        audit=AuditStore(tmp_path / "retention-audit.db"),
        workflow_loader=lambda _root_id, **_: _workflow(),
        row_lister=lambda **_: [],
        clock=lambda: NOW + 8 * 24 * 3600,
    )
    result = worker.run_once()
    assert result.purged_count == 0
    assert maintenance.purged == []


def test_supervisor_mutation_waits_for_retention_lock(tmp_path: Path) -> None:
    env = {
        "HERMES_KANBAN_DB": str(tmp_path / "kanban.db"),
        "HERMES_KANBAN_RETENTION_LOCK": str(tmp_path / "retention.lock"),
    }
    with workflow_mutation_lock(environment=env):
        started = threading.Event()
        finished = threading.Event()
        calls: list[tuple[str, ...]] = []

        def runner(command, **_kwargs):
            calls.append(tuple(command))
            return SimpleNamespace(returncode=0, stdout=json.dumps({"task_id": "new"}), stderr="")

        client = HermesKanbanClient(environment=env, runner=runner)

        def create():
            started.set()
            client.create_task(
                title="child",
                body=f"workflow_root_task_id={ROOT}",
                assignee="research-department",
                parent_task_ids=[ROOT],
                idempotency_key="req-lock",
            )
            finished.set()

        thread = threading.Thread(target=create)
        thread.start()
        assert started.wait(1)
        time.sleep(0.05)
        assert not finished.is_set()
    thread.join(timeout=1)
    assert finished.is_set()
    assert calls


def test_root_atomic_archive_and_purge(tmp_path: Path) -> None:
    db = tmp_path / "kanban.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, claim_lock TEXT,
              claim_expires INTEGER, worker_pid INTEGER);
            CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT,
              status TEXT, ended_at INTEGER, outcome TEXT);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER);
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
            CREATE TABLE task_comments (task_id TEXT);
            CREATE TABLE task_attachments (task_id TEXT);
            CREATE TABLE kanban_notify_subs (task_id TEXT);
            INSERT INTO tasks VALUES ('root','done',NULL,NULL,NULL);
            INSERT INTO tasks VALUES ('child','done',NULL,NULL,NULL);
            INSERT INTO task_links VALUES ('root','child');
            """
        )
    maintenance = SQLiteKanbanMaintenance({"HERMES_KANBAN_DB": str(db), "HERMES_KANBAN_RETENTION_LOCK": str(tmp_path / "lock")})
    assert maintenance.archive_workflow("root", ["child"])
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM tasks WHERE id='root'").fetchone()[0] == "archived"
        assert conn.execute("SELECT status FROM tasks WHERE id='child'").fetchone()[0] == "archived"
    assert maintenance.purge_workflow("root", ["child"])
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
