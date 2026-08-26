from __future__ import annotations

import importlib.util
import os
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
            current_run_id INTEGER,
            body TEXT
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


def test_non_qa_dispatcher_worker_delegates_to_real_hermes(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_research")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "11")
    monkeypatch.setenv("HERMES_PROFILE", "research-department")
    calls = []

    def fake_execvpe(executable, argv, env):
        calls.append((executable, list(argv), dict(env)))
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(qa_worker.os, "execvpe", fake_execvpe)

    try:
        qa_worker.main(["--cli", "chat", "-q", "work kanban task t_research"])
    except RuntimeError as exc:
        assert str(exc) == "exec intercepted"
    else:
        raise AssertionError("non-QA worker did not delegate")

    assert len(calls) == 1
    executable, argv, env = calls[0]
    assert executable == qa_worker.REAL_HERMES
    assert argv[0] == qa_worker.REAL_HERMES
    assert env["HERMES_BIN"] == qa_worker.REAL_HERMES


def test_fast_advisory_gets_task_scoped_turn_budget(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        ("analysis_mode=fast_advisory\nquestion",),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HGFINANCE_FAST_ADVISORY_MAX_TURNS", "18")

    bounded = qa_worker._bounded_worker_argv(
        ["--profile", "research-department", "chat", "-q", "work"],
        db_path=db,
        task_id="t_qa",
    )

    assert bounded == [
        "--profile",
        "research-department",
        "chat",
        "--max-turns",
        "18",
        "--reasoning",
        "medium",
        "-q",
        "work",
    ]


def test_response_budget_does_not_change_standard_or_explicit_budget(tmp_path):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        ("analysis_mode=standard_analysis\nquestion",),
    )
    conn.commit()
    conn.close()

    standard = ["chat", "-q", "work"]
    explicit = ["chat", "--max-turns", "80", "-q", "work"]

    assert qa_worker._bounded_worker_argv(
        standard, db_path=db, task_id="t_qa"
    ) == standard
    assert qa_worker._bounded_worker_argv(
        explicit, db_path=db, task_id="t_qa"
    ) == explicit


def test_user_query_planning_and_synthesis_receive_bounded_budget(tmp_path):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)

    def bounded(body):
        conn = sqlite3.connect(db)
        conn.execute("UPDATE tasks SET body = ? WHERE id = 't_qa'", (body,))
        conn.commit()
        conn.close()
        return qa_worker._bounded_worker_argv(
            ["chat", "-q", "work"], db_path=db, task_id="t_qa"
        )

    assert bounded("origin=user-query\nroot_task_role=scope_and_planning") == [
        "chat", "--max-turns", "12", "--reasoning", "medium", "-q", "work"
    ]
    assert bounded("workflow_role=synthesis\nworkflow_plane=response") == [
        "chat", "--max-turns", "12", "--reasoning", "medium", "-q", "work"
    ]


def test_explicit_response_reasoning_and_turn_budget_are_preserved(tmp_path):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        ("analysis_mode=fast_advisory",),
    )
    conn.commit()
    conn.close()

    explicit = [
        "chat", "--max-turns", "9", "--reasoning", "high", "-q", "work"
    ]
    assert qa_worker._bounded_worker_argv(
        explicit, db_path=db, task_id="t_qa"
    ) == explicit


def test_dispatch_worker_uses_process_cwd_instead_of_deprecated_terminal_env(
    monkeypatch,
) -> None:
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_12345678")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("TERMINAL_CWD", "/opt/data/shared-kanban/workspace")
    monkeypatch.setenv("UNCHANGED_SENTINEL", "preserved")

    env = qa_worker._real_worker_environment()

    assert "TERMINAL_CWD" not in env
    assert env["UNCHANGED_SENTINEL"] == "preserved"


def test_interactive_worker_keeps_explicit_terminal_env(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.setenv("TERMINAL_CWD", "/interactive/project")

    env = qa_worker._real_worker_environment()

    assert env["TERMINAL_CWD"] == "/interactive/project"


def test_dispatch_worker_drops_terminal_env_from_wrapper(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_12345678")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("TERMINAL_CWD", "/opt/data/shared-kanban/workspace")

    qa_worker._drop_dispatcher_terminal_cwd()

    assert "TERMINAL_CWD" not in os.environ
