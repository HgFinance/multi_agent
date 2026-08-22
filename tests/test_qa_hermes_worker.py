from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "qa_hermes_worker.py"
SPEC = importlib.util.spec_from_file_location("qa_hermes_worker", MODULE_PATH)
assert SPEC and SPEC.loader
qa_worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qa_worker)


def _db_with_running_run(path: Path, *, task_id: str = "t_qa", run_id: int = 7):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            current_run_id INTEGER
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            outcome TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO tasks(id, status, current_run_id) VALUES (?, 'running', ?)",
        (task_id, run_id),
    )
    conn.execute(
        "INSERT INTO task_runs(id, task_id, outcome) VALUES (?, ?, NULL)",
        (run_id, task_id),
    )
    conn.commit()
    conn.close()


def test_running_owned_run_is_detected_without_writing(tmp_path):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)

    assert qa_worker._still_owned_and_unfinished(db, "t_qa", 7) is True


def test_terminal_run_is_not_reblocked(tmp_path):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = 't_qa'")
    conn.execute("UPDATE task_runs SET outcome = 'blocked' WHERE id = 7")
    conn.commit()
    conn.close()

    assert qa_worker._still_owned_and_unfinished(db, "t_qa", 7) is False


def test_clean_exit_without_marker_calls_existing_block_once(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_qa")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_PROFILE", "qa-department")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setattr(qa_worker, "_run_real_worker", lambda argv: 0)
    calls = []
    monkeypatch.setattr(
        qa_worker,
        "_block_owned_task",
        lambda task_id, **kwargs: calls.append(task_id) or True,
    )

    assert qa_worker.main(["chat", "-q", "work kanban task t_qa"]) == 0
    assert calls == ["t_qa"]


def test_normal_terminal_state_does_not_call_block(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = 't_qa'")
    conn.execute("UPDATE task_runs SET outcome = 'completed' WHERE id = 7")
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_qa")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_PROFILE", "qa-department")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setattr(qa_worker, "_run_real_worker", lambda argv: 0)
    calls = []
    monkeypatch.setattr(
        qa_worker,
        "_block_owned_task",
        lambda task_id, **kwargs: calls.append(task_id) or True,
    )

    assert qa_worker.main(["chat", "-q", "work kanban task t_qa"]) == 0
    assert calls == []


def test_nonzero_exit_keeps_existing_crash_semantics(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_qa")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_PROFILE", "qa-department")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setattr(qa_worker, "_run_real_worker", lambda argv: 9)
    calls = []
    monkeypatch.setattr(
        qa_worker,
        "_block_owned_task",
        lambda task_id, **kwargs: calls.append(task_id) or True,
    )

    assert qa_worker.main(["chat", "-q", "work kanban task t_qa"]) == 9
    assert calls == []


def test_known_empty_codex_pool_blocks_before_starting_hermes(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: openai-codex\n", encoding="utf-8"
    )
    (tmp_path / "auth.json").write_text(
        '{"credential_pool": {"openai-codex": []}}', encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_qa")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_PROFILE", "qa-department")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setattr(
        qa_worker,
        "_run_real_worker",
        lambda argv: (_ for _ in ()).throw(AssertionError("worker started")),
    )
    calls = []
    monkeypatch.setattr(
        qa_worker,
        "_block_owned_task",
        lambda task_id, **kwargs: calls.append(task_id) or True,
    )

    assert qa_worker.main(["chat", "-q", "work kanban task t_qa"]) == 0
    assert calls == ["t_qa"]
