from __future__ import annotations

import sqlite3

from apps.api.ceo_mirror_projection_worker import (
    _changed_request_ids,
    _kanban_event_changes,
    _kanban_event_watermark,
)
from apps.api.ceo_mirror import CanonicalIngress, InMemoryMirrorStore


def test_kanban_event_watermark_tracks_task_event_changes(tmp_path, monkeypatch) -> None:
    database = tmp_path / "kanban.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "create table task_events (id integer primary key autoincrement, task_id text)"
        )
        connection.execute("insert into task_events(task_id) values ('task-1')")

    monkeypatch.setenv("HERMES_KANBAN_DB", str(database))
    assert _kanban_event_watermark() == 1

    with sqlite3.connect(database) as connection:
        connection.execute("insert into task_events(task_id) values ('task-2')")

    assert _kanban_event_watermark() == 2
    assert _kanban_event_changes(1) == (2, {"task-2"}, False)


def test_kanban_event_watermark_fails_open_when_database_is_unavailable(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "missing.db"))
    assert _kanban_event_watermark() is None


def test_changed_request_ids_maps_child_event_to_existing_root_request() -> None:
    store = InMemoryMirrorStore()
    request = CanonicalIngress(
        query="status",
        request_id="request-1",
        source="web",
        source_message_id="web:1",
        actor_id="user-1",
    )
    store.claim_request(request)
    store.save_response(request.request_id, {"task_id": "root-1"})

    assert _changed_request_ids(
        store,
        {"child-1"},
        [
            {"id": "root-1", "body": "workflow_role=root"},
            {
                "id": "child-1",
                "body": "workflow_root_task_id=root-1\nworkflow_role=primary",
            },
        ],
    ) == ["request-1"]
