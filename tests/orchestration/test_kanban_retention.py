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
    AUDIT_CAPSULE_SCHEMA_VERSION,
    AuditStore,
    DeliveryState,
    FilesystemArtifactCleaner,
    RetentionWorker,
    SQLiteKanbanMaintenance,
    _archive_scan_root_ids,
    build_qa_hr_capsule,
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
    def __init__(self, *, workflow_exists: bool = True) -> None:
        self.archived: list[tuple[str, tuple[str, ...]]] = []
        self.purged: list[tuple[str, tuple[str, ...]]] = []
        self._workflow_exists = workflow_exists

    def archive_workflow(self, root_id: str, task_ids: list[str]) -> bool:
        self.archived.append((root_id, tuple(task_ids)))
        return True

    def purge_workflow(self, root_id: str, task_ids: list[str]) -> bool:
        self.purged.append((root_id, tuple(task_ids)))
        return True

    def workflow_exists(self, _root_id: str) -> bool:
        return self._workflow_exists


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


def test_claimed_terminal_status_is_still_not_archivable() -> None:
    workflow = _workflow()
    claimed_raw = dict(workflow.nodes[1].raw, claim_lock="worker-lock")
    claimed = ceo_kanban_read.Workflow(
        root_task_id=ROOT,
        nodes=(
            workflow.nodes[0],
            ceo_kanban_read.WorkflowNode.from_hermes(claimed_raw),
            workflow.nodes[2],
        ),
        root_payload=workflow.root_payload,
    )
    result = _decision(claimed)
    assert not result.eligible
    assert result.reason == f"active_execution:{PRIMARY}"


def test_recovery_pending_archive_zero() -> None:
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


def test_stale_non_input_block_is_archivable_after_seven_days() -> None:
    workflow = _workflow(child_status="blocked")
    blocked_raw = dict(
        workflow.nodes[1].raw,
        block_kind="capability",
        blocked_at=NOW - 8 * 24 * 3600,
        completed_at=None,
    )
    workflow = ceo_kanban_read.Workflow(
        root_task_id=ROOT,
        nodes=(
            workflow.nodes[0],
            ceo_kanban_read.WorkflowNode.from_hermes(blocked_raw),
            workflow.nodes[2],
        ),
        metadata={},
        root_payload=workflow.root_payload,
    )

    result = _decision(workflow)

    assert result.eligible
    assert result.reason == "safe"
    assert result.terminal_at == NOW - 8 * 24 * 3600


def test_recent_block_and_user_input_block_are_retained() -> None:
    recent = _workflow(child_status="blocked")
    recent_raw = dict(
        recent.nodes[1].raw,
        block_kind="capability",
        blocked_at=NOW - 6 * 24 * 3600,
        completed_at=None,
    )
    recent = ceo_kanban_read.Workflow(
        root_task_id=ROOT,
        nodes=(
            recent.nodes[0],
            ceo_kanban_read.WorkflowNode.from_hermes(recent_raw),
            recent.nodes[2],
        ),
        metadata={},
        root_payload=recent.root_payload,
    )
    recent_result = _decision(recent)
    assert not recent_result.eligible
    assert recent_result.reason == "blocked_under_7d"

    waiting = _workflow(child_status="blocked")
    waiting_raw = dict(
        waiting.nodes[1].raw,
        block_kind="needs_input",
        blocked_at=NOW - 30 * 24 * 3600,
        completed_at=None,
    )
    waiting = ceo_kanban_read.Workflow(
        root_task_id=ROOT,
        nodes=(
            waiting.nodes[0],
            ceo_kanban_read.WorkflowNode.from_hermes(waiting_raw),
            waiting.nodes[2],
        ),
        metadata={},
        root_payload=waiting.root_payload,
    )
    waiting_result = _decision(waiting)
    assert not waiting_result.eligible
    assert waiting_result.reason == f"blocked_needs_input:{PRIMARY}"


def test_scan_includes_legacy_standalone_cards_without_stealing_children() -> None:
    rows = [
        {"id": "marked-root", "status": "done", "created_at": 30, "body": "root_task_role=scope_and_planning\nworkflow_root_task_id=marked-root\n"},
        {"id": "marked-child", "status": "done", "created_at": 1, "body": "workflow_root_task_id=marked-root\n"},
        {"id": "legacy-done", "status": "done", "created_at": 10, "body": "diagnostic only"},
        {"id": "legacy-blocked", "status": "blocked", "created_at": 20, "body": "factory diagnostic"},
        {"id": "triage-card", "status": "triage", "created_at": 0, "body": "not terminal"},
    ]

    assert _archive_scan_root_ids(rows) == (
        "legacy-done",
        "legacy-blocked",
        "marked-root",
    )


def test_scan_excludes_markerless_linked_children_when_graph_roots_are_known() -> None:
    rows = [
        {"id": "legacy-root", "status": "done", "created_at": 10, "body": "root request"},
        {"id": "legacy-child", "status": "done", "created_at": 1, "body": "department result"},
        {"id": "legacy-standalone", "status": "done", "created_at": 5, "body": "diagnostic only"},
    ]

    assert _archive_scan_root_ids(
        rows,
        linked_root_ids=("legacy-root",),
        linked_task_ids=("legacy-root", "legacy-child"),
    ) == ("legacy-standalone", "legacy-root")


def test_required_synthesis_missing_blocks_multi_stage_workflow() -> None:
    workflow = _workflow()
    second_primary = ceo_kanban_read.WorkflowNode.from_hermes(
        _node("primary-retention-2")
    )
    without_synthesis = ceo_kanban_read.Workflow(
        root_task_id=ROOT,
        nodes=(workflow.nodes[0], workflow.nodes[1], second_primary),
        root_payload=workflow.root_payload,
    )
    result = _decision(without_synthesis)
    assert not result.eligible
    assert result.reason == "required_synthesis_missing"


def test_single_primary_final_processing_can_replace_synthesis() -> None:
    workflow = _workflow()
    single_primary = ceo_kanban_read.Workflow(
        root_task_id=ROOT,
        nodes=workflow.nodes[:2],
        root_payload=workflow.root_payload,
    )
    result = _decision(single_primary)
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


def test_active_root_reconstruction_is_bounded_parallel(tmp_path: Path) -> None:
    started = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    max_active = 0

    def loader(_root_id: str, **_):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            started.wait(timeout=2)
            return _workflow()
        finally:
            with lock:
                active -= 1

    worker = RetentionWorker(
        maintenance=FakeMaintenance(),
        audit=AuditStore(tmp_path / "audit.db"),
        workflow_loader=loader,
        row_lister=lambda **_: [],
        root_workers=2,
    )

    results = list(
        worker._inspect_active_roots(
            ("root-one", "root-two"),
            active_rows=(),
            now=NOW,
        )
    )

    assert len(results) == 2
    assert max_active == 2


def test_legacy_root_pending_marker_resolves_to_existing_root() -> None:
    payload = _node(ROOT)
    payload["body"] = str(payload["body"]).replace(
        f"workflow_root_task_id={ROOT}",
        "workflow_root_task_id=ROOT_PENDING",
    )
    payload["body"] = f"{payload['body']}## User request\nlegacy request\n"
    fetched: list[str] = []

    def fetch(task_id: str):
        fetched.append(task_id)
        if task_id != ROOT:
            raise ceo_kanban_read.KanbanTaskNotFound(task_id)
        return payload

    assert ceo_kanban_read.resolve_root_id(ROOT, fetch=fetch) == ROOT
    assert fetched == [ROOT]


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
    assert "body" not in audit.get(ROOT).keys()

    state["archived"] = True
    under_7_days = RetentionWorker(
        maintenance=maintenance,
        audit=audit,
        workflow_loader=loader,
        row_lister=rows,
        clock=lambda: NOW + 4 * 24 * 3600,
    ).run_once()
    assert under_7_days.purged_count == 0

    before_purge = RetentionWorker(
        maintenance=maintenance,
        audit=audit,
        workflow_loader=loader,
        row_lister=rows,
        clock=lambda: NOW + 5 * 24 * 3600,
    ).run_once()
    assert before_purge.purged_count == 0

    with audit._connect() as conn:
        conn.execute(
            "UPDATE workflow_retention_audit SET qa_hr_capsule_json = ? WHERE root_id = ?",
            (
                json.dumps(
                    {
                        "schema_version": AUDIT_CAPSULE_SCHEMA_VERSION,
                        "workflow": {"task_ids": ["stale-task-id"]},
                    }
                ),
                ROOT,
            ),
        )
        conn.commit()

    after_purge = RetentionWorker(
        maintenance=maintenance,
        audit=audit,
        workflow_loader=loader,
        row_lister=rows,
        clock=lambda: NOW + 5 * 24 * 3600 + 1,
    ).run_once()
    assert after_purge.purged_count == 1
    assert audit.get(ROOT) is not None
    assert audit.get(ROOT)["purged_at"] == NOW + 5 * 24 * 3600 + 1
    refreshed_capsule = json.loads(audit.get(ROOT)["qa_hr_capsule_json"])
    assert refreshed_capsule["workflow"]["task_ids"] == [ROOT, PRIMARY, SYNTHESIS]


def test_bounded_production_scan_stops_after_oldest_safe_batch(tmp_path: Path) -> None:
    rows = [
        {
            "id": "newer-root",
            "body": (
                "hgfinance.ceo-workflow-scope.v1\n"
                "workflow_root_task_id=newer-root\n"
                "## User request\nnewer\n"
            ),
            "status": "done",
            "created_at": NOW - 2 * 24 * 3600,
            "completed_at": NOW - 25 * 3600,
        },
        {
            "id": "oldest-root",
            "body": (
                "hgfinance.ceo-workflow-scope.v1\n"
                "workflow_root_task_id=oldest-root\n"
                "## User request\noldest\n"
            ),
            "status": "done",
            "created_at": NOW - 5 * 24 * 3600,
            "completed_at": NOW - 3 * 24 * 3600,
        },
    ]
    loaded: list[str] = []

    def loader(root_id: str, **_):
        loaded.append(root_id)
        return _workflow()

    result = RetentionWorker(
        maintenance=FakeMaintenance(),
        audit=AuditStore(tmp_path / "retention-audit.db"),
        workflow_loader=loader,
        row_lister=lambda *, include_archived=False: rows if not include_archived else [],
        clock=lambda: NOW,
        max_archive_roots=1,
        root_workers=1,
    ).run_once()

    assert loaded == ["oldest-root"]
    assert result.active_root_count == 2
    assert result.archived_count == 1


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


def test_missing_descendant_does_not_mark_existing_root_purged(tmp_path: Path) -> None:
    maintenance = FakeMaintenance(workflow_exists=True)
    audit = AuditStore(tmp_path / "retention-audit.db")
    active = [{"id": ROOT, "body": _workflow().root.body, "status": "done"}]
    state = {"archived": False}

    def rows(*, include_archived: bool = False):
        if include_archived and state["archived"]:
            return [{"id": ROOT, "body": _workflow().root.body, "status": "archived"}]
        return [] if state["archived"] or include_archived else active

    def loader(_root_id: str, **_):
        if state["archived"]:
            raise ceo_kanban_read.KanbanTaskNotFound("missing legacy descendant")
        return _workflow()

    RetentionWorker(
        maintenance=maintenance,
        audit=audit,
        workflow_loader=loader,
        row_lister=rows,
        clock=lambda: NOW,
    ).run_once()
    state["archived"] = True

    result = RetentionWorker(
        maintenance=maintenance,
        audit=audit,
        workflow_loader=loader,
        row_lister=rows,
        clock=lambda: NOW + 8 * 24 * 3600,
    ).run_once()

    assert result.purged_count == 0
    assert audit.get(ROOT)["purged_at"] is None
    assert (ROOT, "purge_graph_missing_root_still_exists") in result.skipped


def test_audit_store_migrates_legacy_schema_with_qa_hr_capsule(tmp_path: Path) -> None:
    audit_path = tmp_path / "retention-audit.db"
    with sqlite3.connect(audit_path) as conn:
        conn.execute(
            """CREATE TABLE workflow_retention_audit (
                root_id TEXT PRIMARY KEY,
                request_id TEXT,
                final_status TEXT NOT NULL,
                created_at INTEGER,
                terminal_at INTEGER,
                completed_at INTEGER,
                departments TEXT NOT NULL,
                total_latency_ms INTEGER,
                final_result_ref TEXT,
                discord_thread_id TEXT,
                discord_message_id TEXT,
                archived_at INTEGER NOT NULL,
                purged_at INTEGER
            )"""
        )

    with AuditStore(audit_path)._connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_retention_audit)")}
    assert "qa_hr_capsule_json" in columns


def test_qa_hr_capsule_contains_only_compact_review_fields() -> None:
    qa = _node(
        "qa-retention",
        role="qa",
        status="blocked",
        runs=[
            {
                "status": "failed",
                "outcome": "protocol_violation",
                "ended_at": NOW,
                "error": "raw provider detail must not be retained",
            }
        ],
    )
    qa["assignee"] = "qa-department"
    hr = _node("hr-retention", role="primary")
    hr["assignee"] = "hr-department"
    workflow = ceo_kanban_read.Workflow(
        root_task_id=ROOT,
        nodes=tuple(
            ceo_kanban_read.WorkflowNode.from_hermes(payload)
            for payload in (_node(ROOT), qa, hr)
        ),
        metadata={},
        root_payload=_node(ROOT),
    )

    capsule = json.loads(build_qa_hr_capsule(workflow))
    assert capsule["schema_version"] == AUDIT_CAPSULE_SCHEMA_VERSION
    assert capsule["qa"]["task_count"] == 1
    assert capsule["qa"]["protocol_violation_count"] == 1
    assert capsule["hr"]["task_count"] == 1
    encoded = json.dumps(capsule, ensure_ascii=False)
    assert "raw provider detail" not in encoded
    assert "body" not in encoded
    assert "summary" not in encoded


def test_filesystem_artifact_cleanup_is_task_scoped(tmp_path: Path) -> None:
    home = tmp_path / "shared-kanban"
    for relative in (
        "kanban/logs/root-retention.log",
        "kanban/workspaces/root-retention/output.txt",
        "kanban/attachments/root-retention/evidence.md",
        "kanban/logs/unrelated.log",
        "kanban/workspaces/unrelated/keep.txt",
        "kanban/attachments/unrelated/keep.md",
    ):
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    removed, skipped = FilesystemArtifactCleaner(
        {"HERMES_KANBAN_HOME": str(home)}
    ).cleanup([ROOT, "root-retention"])

    assert removed == 3
    assert skipped == 0
    assert not (home / "kanban/logs/root-retention.log").exists()
    assert not (home / "kanban/workspaces/root-retention").exists()
    assert not (home / "kanban/attachments/root-retention").exists()
    assert (home / "kanban/logs/unrelated.log").exists()
    assert (home / "kanban/workspaces/unrelated/keep.txt").exists()
    assert (home / "kanban/attachments/unrelated/keep.md").exists()


def test_legacy_archived_workflow_is_audited_before_old_purge(tmp_path: Path) -> None:
    maintenance = FakeMaintenance()
    audit = AuditStore(tmp_path / "retention-audit.db")
    current = _workflow()
    legacy = ceo_kanban_read.Workflow(
        root_task_id=ROOT,
        nodes=tuple(
            ceo_kanban_read.WorkflowNode.from_hermes(
                dict(node.raw, completed_at=None)
            )
            for node in current.nodes
        ),
        root_payload=dict(current.root_payload, completed_at=None),
    )
    fully_archived = ceo_kanban_read.Workflow(
        root_task_id=ROOT,
        nodes=tuple(
            ceo_kanban_read.WorkflowNode.from_hermes(
                dict(
                    node.raw,
                    status="archived",
                    completed_at=None,
                    events=[{"kind": "archived", "created_at": NOW - 8 * 24 * 3600}],
                )
            )
            for node in legacy.nodes
        ),
        root_payload=dict(legacy.root_payload, status="archived"),
    )
    partial_legacy = ceo_kanban_read.Workflow(
        root_task_id=ROOT,
        nodes=(fully_archived.nodes[0], *legacy.nodes[1:]),
        root_payload=fully_archived.root_payload,
    )

    def rows(*, include_archived: bool = False):
        return [{"id": ROOT, "body": legacy.root.body, "status": "archived"}] if include_archived else []

    def loader(_root_id: str, **_):
        return fully_archived if maintenance.archived else partial_legacy

    worker = RetentionWorker(
        maintenance=maintenance,
        audit=audit,
        workflow_loader=loader,
        row_lister=rows,
        clock=lambda: NOW,
    )
    result = worker.run_once()
    assert result.purged_count == 1
    assert maintenance.archived == [(ROOT, (PRIMARY, SYNTHESIS))]
    assert maintenance.purged == [(ROOT, (PRIMARY, SYNTHESIS))]
    assert audit.get(ROOT)["archived_at"] == NOW - 8 * 24 * 3600
    assert audit.get(ROOT)["purged_at"] == NOW


def test_dry_run_reports_candidates_without_audit_or_board_mutation(tmp_path: Path) -> None:
    maintenance = FakeMaintenance()
    audit_path = tmp_path / "preview-audit.db"
    active = [{"id": ROOT, "body": _workflow().root.body, "status": "done"}]

    worker = RetentionWorker(
        maintenance=maintenance,
        audit=AuditStore(audit_path),
        dry_run=True,
        max_archive_roots=1,
        workflow_loader=lambda _root_id, **_: _workflow(),
        row_lister=lambda *, include_archived=False: active if not include_archived else [],
        clock=lambda: NOW,
    )
    result = worker.run_once()
    assert result.eligible_root_ids == (ROOT,)
    assert result.would_archive_root_ids == (ROOT,)
    assert result.would_archive_task_count == 3
    assert result.archived_count == 0
    assert result.purged_count == 0
    assert maintenance.archived == []
    assert maintenance.purged == []
    assert not audit_path.exists()


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
                body=f"workflow_root_task_id={ROOT}\nworkflow_role=primary\n",
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
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 2
    assert maintenance.archive_workflow("root", ["child"])
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 2
    assert maintenance.purge_workflow("root", ["child"])
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
