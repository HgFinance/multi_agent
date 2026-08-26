"""Root-scoped Kanban discovery and authoritative fallback tests."""

from __future__ import annotations

import json
import sqlite3
import subprocess

from orchestration.adapters.ceo_supervisor import HermesKanbanClient
from orchestration.ceo_workflow_scope import build_root_body, build_scoped_task_body
from orchestration.kanban_root_index import (
    SQLiteRootScopedIndex,
    RootScopedIndexUnavailable,
)

ROOT = "t_aaaaaaaa"
RESEARCH = "t_bbbbbbbb"
RISK = "t_cccccccc"
OTHER = "t_dddddddd"
ARCHIVED = "t_eeeeeeee"


def _make_board(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, body TEXT, status TEXT NOT NULL, created_at INTEGER NOT NULL"
        ")"
    )
    conn.executemany(
        "INSERT INTO tasks(id, body, status, created_at) VALUES (?, ?, ?, ?)",
        (
            (ROOT, build_root_body("query", "request"), "done", 1),
            (
                RESEARCH,
                build_scoped_task_body("research", ROOT, role="primary"),
                "done",
                2,
            ),
            (
                RISK,
                build_scoped_task_body("risk", ROOT, role="primary"),
                "blocked",
                3,
            ),
            (
                OTHER,
                build_scoped_task_body("other", "t_ffffffff", role="primary"),
                "done",
                4,
            ),
            (
                ARCHIVED,
                build_scoped_task_body("old", ROOT, role="primary"),
                "archived",
                5,
            ),
        ),
    )
    conn.commit()
    conn.close()


class FakeRootIndex:
    def __init__(self, ids=(), error: Exception | None = None) -> None:
        self.ids = tuple(ids)
        self.error = error
        self.calls: list[str] = []

    def task_ids(
        self,
        root_id: str,
        *,
        include_archived: bool = False,
    ) -> tuple[str, ...]:
        self.calls.append(root_id)
        if self.error is not None:
            raise self.error
        return self.ids


def _runner(payloads: dict[str, dict[str, object]], calls: list[tuple[str, ...]]):
    def run(args, **kwargs):
        command = tuple(args)
        calls.append(command)
        if command[1:3] == ("kanban", "list"):
            return subprocess.CompletedProcess(args, 0, json.dumps(list(payloads.values())), "")
        task_id = command[3]
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps({"task": payloads[task_id], "runs": []}),
            "",
        )

    return run


def _payloads() -> dict[str, dict[str, object]]:
    return {
        ROOT: {
            "id": ROOT,
            "status": "done",
            "body": build_root_body("query", "request"),
        },
        RESEARCH: {
            "id": RESEARCH,
            "status": "done",
            "body": build_scoped_task_body("research", ROOT, role="primary"),
        },
        RISK: {
            "id": RISK,
            "status": "blocked",
            "body": build_scoped_task_body("risk", ROOT, role="primary"),
        },
        OTHER: {
            "id": OTHER,
            "status": "done",
            "body": build_scoped_task_body("other", "t_ffffffff", role="primary"),
        },
    }


def test_sqlite_index_is_generated_and_uses_btree(tmp_path) -> None:
    path = tmp_path / "kanban.db"
    _make_board(path)

    index = SQLiteRootScopedIndex({"HERMES_KANBAN_DB": str(path)})
    index.prepare()

    assert index.task_ids(ROOT) == (RESEARCH, RISK)
    assert index.task_ids(ROOT, include_archived=True) == (
        RESEARCH,
        RISK,
        ARCHIVED,
    )
    plan = sqlite3.connect(path).execute(
        "EXPLAIN QUERY PLAN SELECT id FROM tasks "
        "WHERE workflow_root_task_id = ?",
        (ROOT,),
    ).fetchall()
    assert any("USING INDEX idx_tasks_workflow_root_task_id" in row[-1] for row in plan)


def test_sqlite_index_resolves_scoped_child_root_without_hydrating_state(tmp_path) -> None:
    path = tmp_path / "kanban.db"
    _make_board(path)

    index = SQLiteRootScopedIndex({"HERMES_KANBAN_DB": str(path)})
    index.prepare()

    assert index.root_id_for_task(RESEARCH) == ROOT
    assert index.root_id_for_task(ROOT) is None
    assert index.root_id_for_task(ARCHIVED) is None


def test_generated_index_tracks_hermes_task_insert_and_body_update(tmp_path) -> None:
    path = tmp_path / "kanban.db"
    _make_board(path)
    index = SQLiteRootScopedIndex({"HERMES_KANBAN_DB": str(path)})
    index.prepare()

    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = ?",
        (build_scoped_task_body("research", "t_ffffffff", role="primary"), RESEARCH),
    )
    conn.execute(
        "INSERT INTO tasks(id, body, status, created_at) VALUES (?, ?, ?, ?)",
        (
            "t_f0f0f0f0",
            build_scoped_task_body("new", ROOT, role="primary"),
            "ready",
            6,
        ),
    )
    conn.execute(
        "INSERT INTO tasks(id, body, status, created_at) VALUES (?, ?, ?, ?)",
        (
            "t_qa_after_index",
            build_scoped_task_body("qa", ROOT, role="qa"),
            "ready",
            7,
        ),
    )
    conn.execute(
        "INSERT INTO tasks(id, body, status, created_at) VALUES (?, ?, ?, ?)",
        (
            "t_synthesis_after_index",
            build_scoped_task_body("synthesis", ROOT, role="synthesis"),
            "ready",
            8,
        ),
    )
    conn.commit()
    conn.close()

    assert index.task_ids(ROOT) == (
        RISK,
        "t_f0f0f0f0",
        "t_qa_after_index",
        "t_synthesis_after_index",
    )


def test_recovery_candidate_rows_are_discovery_only(tmp_path) -> None:
    path = tmp_path / "kanban.db"
    _make_board(path)
    index = SQLiteRootScopedIndex({"HERMES_KANBAN_DB": str(path)})

    rows = index.recovery_candidate_rows()

    assert [row["id"] for row in rows] == [ROOT]
    assert rows[0]["status"] == "done"
    assert rows[0]["has_active_primary"] is False


def test_recovery_candidates_mark_only_roots_with_active_primary(tmp_path) -> None:
    path = tmp_path / "kanban.db"
    _make_board(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO tasks(id, body, status, created_at) VALUES (?, ?, ?, ?)",
        (
            "t_active_primary",
            build_scoped_task_body("risk", ROOT, role="primary"),
            "running",
            9,
        ),
    )
    conn.commit()
    conn.close()

    index = SQLiteRootScopedIndex({"HERMES_KANBAN_DB": str(path)})
    rows = index.recovery_candidate_rows()

    assert [row["id"] for row in rows] == [ROOT]
    assert rows[0]["has_active_primary"] is True


def test_recovery_candidates_exclude_handled_empty_primary_root(tmp_path) -> None:
    path = tmp_path / "kanban.db"
    _make_board(path)
    index = SQLiteRootScopedIndex({"HERMES_KANBAN_DB": str(path)})
    index.prepare()

    control_body = (
        "hgfinance.ceo-workflow-scope.v1\n"
        f"workflow_root_task_id={ROOT}\n"
        "workflow_role=control\n"
        "workflow_mode=analysis\n"
        "hgfinance.ceo-supervisor.v1 action=REQUEST_USER_INPUT "
        "no_analysis_children\n"
    )
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO tasks(id, body, status, created_at) VALUES (?, ?, ?, ?)",
        ("t_control", control_body, "blocked", 6),
    )
    conn.commit()
    conn.close()

    assert index.recovery_candidate_rows() == ()


def test_recovery_candidates_keep_other_request_user_input_root(tmp_path) -> None:
    path = tmp_path / "kanban.db"
    _make_board(path)
    index = SQLiteRootScopedIndex({"HERMES_KANBAN_DB": str(path)})
    index.prepare()

    control_body = (
        "hgfinance.ceo-workflow-scope.v1\n"
        f"workflow_root_task_id={ROOT}\n"
        "workflow_role=control\n"
        "workflow_mode=analysis\n"
        "hgfinance.ceo-supervisor.v1 action=REQUEST_USER_INPUT\n"
    )
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO tasks(id, body, status, created_at) VALUES (?, ?, ?, ?)",
        ("t_control", control_body, "blocked", 6),
    )
    conn.commit()
    conn.close()

    assert [row["id"] for row in index.recovery_candidate_rows()] == [ROOT]


def test_recovery_candidates_use_stable_control_idempotency_key(tmp_path) -> None:
    path = tmp_path / "kanban.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, body TEXT, status TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, idempotency_key TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO tasks(id, body, status, created_at, idempotency_key) "
        "VALUES (?, ?, ?, ?, ?)",
        (ROOT, build_root_body("query", "request"), "done", 1, "root-key"),
    )
    control_body = (
        "hgfinance.ceo-workflow-scope.v1\n"
        f"workflow_root_task_id={ROOT}\n"
        "workflow_role=control\n"
        "workflow_mode=analysis\n"
        "hgfinance.ceo-supervisor.v1 action=REQUEST_USER_INPUT "
        "no_analysis_children\n"
    )
    conn.execute(
        "INSERT INTO tasks(id, body, status, created_at, idempotency_key) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "t_control",
            control_body,
            "blocked",
            2,
            f"{ROOT}:supervisor:user-input",
        ),
    )
    conn.commit()
    conn.close()

    index = SQLiteRootScopedIndex({"HERMES_KANBAN_DB": str(path)})
    assert index.recovery_candidate_rows() == ()


def test_indexed_snapshot_omits_full_board_list_and_shows_every_candidate() -> None:
    payloads = _payloads()
    calls: list[tuple[str, ...]] = []
    index = FakeRootIndex((RESEARCH, RISK))
    client = HermesKanbanClient(
        runner=_runner(payloads, calls),
        root_index=index,  # type: ignore[arg-type]
    )

    root, children, root_payload = client.authoritative_workflow_snapshot(ROOT, RESEARCH)

    assert root == ROOT
    assert root_payload["id"] == ROOT
    assert {child["id"] for child in children} == {RESEARCH, RISK}
    assert {child["status"] for child in children} == {"done", "blocked"}
    assert index.calls == [ROOT]
    assert not any(command[1:3] == ("kanban", "list") for command in calls)
    shown = [command[3] for command in calls if command[1:3] == ("kanban", "show")]
    assert sorted(shown) == sorted([RESEARCH, RISK, ROOT])
    metrics = client.retrieval_metrics_snapshot()
    assert metrics["root_query_count"] == 1
    assert metrics["full_board_list_count"] == 0


def test_scoped_root_lookup_uses_index_without_cli_show() -> None:
    payloads = _payloads()
    calls: list[tuple[str, ...]] = []

    class ReverseFakeRootIndex(FakeRootIndex):
        def root_id_for_task(self, task_id: str) -> str | None:
            return ROOT if task_id == RESEARCH else None

    index = ReverseFakeRootIndex((RESEARCH, RISK))
    client = HermesKanbanClient(
        runner=_runner(payloads, calls),
        root_index=index,  # type: ignore[arg-type]
    )

    assert client.workflow_root(RESEARCH) == ROOT
    assert calls == []
    metrics = client.retrieval_metrics_snapshot()
    assert metrics["root_lookup_count"] == 1
    assert len(metrics["root_lookup_latency_ms"]) == 1


def test_scoped_root_task_can_use_indexed_snapshot() -> None:
    payloads = _payloads()
    calls: list[tuple[str, ...]] = []
    index = FakeRootIndex((RESEARCH, RISK))
    client = HermesKanbanClient(
        runner=_runner(payloads, calls),
        root_index=index,  # type: ignore[arg-type]
    )

    root, children, root_payload = client.authoritative_workflow_snapshot(ROOT, ROOT)

    assert root == ROOT
    assert root_payload["id"] == ROOT
    assert {child["id"] for child in children} == {RESEARCH, RISK}
    assert index.calls == [ROOT]
    assert not any(command[1:3] == ("kanban", "list") for command in calls)


def test_indexed_synthesis_existence_uses_targeted_show_only() -> None:
    payloads = _payloads()
    synthesis_id = "t_synthesis"
    payloads[synthesis_id] = {
        "id": synthesis_id,
        "status": "ready",
        "body": build_scoped_task_body(
            "synthesis\nhgfinance.ceo-supervisor.v1 action=SYNTHESIZE",
            ROOT,
            role="synthesis",
        ),
    }
    calls: list[tuple[str, ...]] = []
    index = FakeRootIndex((RESEARCH, RISK, synthesis_id))
    client = HermesKanbanClient(
        runner=_runner(payloads, calls),
        root_index=index,  # type: ignore[arg-type]
    )

    assert client.authoritative_synthesis_exists(ROOT) is True
    assert not any(command[1:3] == ("kanban", "list") for command in calls)
    shown = [command[3] for command in calls if command[1:3] == ("kanban", "show")]
    assert sorted(shown) == sorted([RESEARCH, RISK, synthesis_id])


def test_index_failure_falls_back_to_full_board_and_keeps_authoritative_ids() -> None:
    payloads = _payloads()
    calls: list[tuple[str, ...]] = []
    index = FakeRootIndex(error=RootScopedIndexUnavailable("sqlite unavailable"))
    client = HermesKanbanClient(
        runner=_runner(payloads, calls),
        root_index=index,  # type: ignore[arg-type]
    )

    _root, children, _root_payload = client.authoritative_workflow_snapshot(ROOT, RESEARCH)

    assert {child["id"] for child in children} == {RESEARCH, RISK}
    assert sum(command[1:3] == ("kanban", "list") for command in calls) == 1
    assert client.retrieval_metrics_snapshot()["full_board_list_count"] == 1


def test_missing_root_id_uses_full_board_recovery_from_authoritative_task() -> None:
    payloads = _payloads()
    calls: list[tuple[str, ...]] = []
    index = FakeRootIndex((RESEARCH, RISK))
    client = HermesKanbanClient(
        runner=_runner(payloads, calls),
        root_index=index,  # type: ignore[arg-type]
    )

    root, children, _root_payload = client.authoritative_workflow_snapshot("", RESEARCH)

    assert root == ROOT
    assert {child["id"] for child in children} == {RESEARCH, RISK}
    assert index.calls == []
    assert client.retrieval_metrics_snapshot()["full_board_list_count"] == 1


def test_legacy_task_uses_full_board_fallback() -> None:
    payloads = _payloads()
    payloads[RESEARCH]["body"] = "legacy task without a root marker"
    calls: list[tuple[str, ...]] = []
    index = FakeRootIndex((RESEARCH, RISK))
    client = HermesKanbanClient(
        runner=_runner(payloads, calls),
        root_index=index,  # type: ignore[arg-type]
    )

    _root, children, _root_payload = client.authoritative_workflow_snapshot(ROOT, RESEARCH)

    assert {child["id"] for child in children} == {RESEARCH, RISK}
    assert index.calls == []
    assert client.retrieval_metrics_snapshot()["full_board_list_count"] == 1


def test_malformed_root_correlation_uses_full_board_fallback() -> None:
    payloads = _payloads()
    payloads[RESEARCH]["body"] = build_scoped_task_body(
        "research", "t_ffffffff", role="primary"
    )
    calls: list[tuple[str, ...]] = []
    index = FakeRootIndex((RESEARCH, RISK))
    client = HermesKanbanClient(
        runner=_runner(payloads, calls),
        root_index=index,  # type: ignore[arg-type]
    )

    _root, children, _root_payload = client.authoritative_workflow_snapshot(ROOT, RESEARCH)

    # The full-board path does not silently attach a child that declares a
    # different root; the valid sibling remains discoverable.
    assert {child["id"] for child in children} == {RISK}
    assert index.calls == []
    assert client.retrieval_metrics_snapshot()["full_board_list_count"] == 1


def test_inconsistent_candidate_set_falls_back_without_trusting_index() -> None:
    payloads = _payloads()
    calls: list[tuple[str, ...]] = []
    # The index claims a foreign task belongs to root.  The show response is
    # authoritative and must force the old discovery path.
    index = FakeRootIndex((RESEARCH, OTHER))
    client = HermesKanbanClient(
        runner=_runner(payloads, calls),
        root_index=index,  # type: ignore[arg-type]
    )

    _root, children, _root_payload = client.authoritative_workflow_snapshot(ROOT, RESEARCH)

    assert {child["id"] for child in children} == {RESEARCH, RISK}
    assert client.retrieval_metrics_snapshot()["full_board_list_count"] == 1
