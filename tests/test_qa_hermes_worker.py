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


def test_non_qa_dispatcher_worker_uses_shared_observer(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_research")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "11")
    monkeypatch.setenv("HERMES_PROFILE", "research-department")
    calls = []
    monkeypatch.setattr(
        qa_worker,
        "_run_real_worker",
        lambda argv: calls.append(list(argv)) or 0,
    )
    monkeypatch.setattr(qa_worker, "_still_owned_and_unfinished", lambda *args: False)

    argv = ["--cli", "chat", "-q", "work kanban task t_research"]
    assert qa_worker.main(argv) == 0
    assert calls == [argv]


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


def test_hr_e2e_uses_one_bounded_read_only_helper_pass(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        (
            "workflow_role=primary\n"
            "origin=user-query\n"
            "workflow_mode=binding\n"
            "scope=PAPER read-only Workforce API GET 3개\n"
            "/workforce/v1/improvements /workforce/v1/departments/observability\n",
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.delenv("HGFINANCE_HR_E2E_MAX_TURNS", raising=False)
    monkeypatch.delenv("HGFINANCE_HR_E2E_REASONING", raising=False)

    bounded = qa_worker._bounded_worker_argv(
        ["--profile", "hr-department", "chat", "--toolsets", "all", "-q", "old"],
        db_path=db,
        task_id="t_qa",
        profile="hr-department",
    )

    assert qa_worker._response_task_kind(
        qa_worker._task_body(db, "t_qa"), profile="hr-department"
    ) == "hr_e2e_readonly"
    assert bounded[bounded.index("--max-turns") + 1] == "6"
    assert bounded[bounded.index("--reasoning") + 1] == "low"
    assert bounded[bounded.index("--toolsets") + 1] == qa_worker.HR_E2E_TOOLSETS
    prompt = bounded[bounded.index("-q") + 1]
    assert "hr_e2e_readonly.py" in prompt
    assert "exactly once" in prompt
    assert "kanban_complete exactly once" in prompt
    assert "raw response bodies" in prompt


def test_generic_hr_work_is_not_rewritten_as_e2e(tmp_path):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        ("workflow_role=primary\norigin=user-query\n채용 정책 검토",),
    )
    conn.commit()
    conn.close()

    argv = ["chat", "-q", "채용 정책 검토"]
    assert qa_worker._bounded_worker_argv(
        argv, db_path=db, task_id="t_qa", profile="hr-department"
    ) == argv


def test_quant_fast_advisory_replaces_dispatcher_broad_toolsets(tmp_path):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        ("analysis_mode=fast_advisory\nquestion",),
    )
    conn.commit()
    conn.close()

    bounded = qa_worker._bounded_worker_argv(
        ["--profile", "quant-backtest-department", "chat", "--toolsets", "all", "-q", "work"],
        db_path=db,
        task_id="t_qa",
        profile="quant-backtest-department",
    )

    assert bounded.count("--toolsets") == 1
    assert bounded[bounded.index("--toolsets") + 1] == (
        qa_worker.QUANT_FAST_ADVISORY_TOOLSETS
    )


def test_research_fast_advisory_replaces_dispatcher_broad_toolsets(tmp_path):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        ("analysis_mode=fast_advisory\nquestion",),
    )
    conn.commit()
    conn.close()

    bounded = qa_worker._bounded_worker_argv(
        ["chat", "--toolsets", "all", "-q", "work"],
        db_path=db,
        task_id="t_qa",
        profile="research-department",
    )

    assert bounded.count("--toolsets") == 1
    assert bounded[bounded.index("--toolsets") + 1] == (
        qa_worker.RESEARCH_FAST_ADVISORY_TOOLSETS
    )


def test_quant_liaison_fast_advisory_keeps_only_read_only_library(tmp_path):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        ("analysis_mode=fast_advisory\nquestion",),
    )
    conn.commit()
    conn.close()

    bounded = qa_worker._bounded_worker_argv(
        ["--profile", "quant-liaison", "chat", "--toolsets", "all", "-q", "work"],
        db_path=db,
        task_id="t_qa",
        profile="quant-liaison",
    )

    assert bounded.count("--toolsets") == 1
    assert bounded[bounded.index("--toolsets") + 1] == (
        qa_worker.QUANT_LIAISON_FAST_ADVISORY_TOOLSETS
    )


def test_quant_standard_user_primary_replaces_dispatcher_broad_toolsets(tmp_path):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        (
            "workflow_role=primary\norigin=user-query\n"
            "analysis_mode=standard_analysis\nquestion",
        ),
    )
    conn.commit()
    conn.close()

    bounded = qa_worker._bounded_worker_argv(
        ["--profile", "quant-backtest-department", "chat", "--toolsets", "all", "-q", "work"],
        db_path=db,
        task_id="t_qa",
        profile="quant-backtest-department",
    )

    assert bounded.count("--toolsets") == 1
    assert bounded[bounded.index("--toolsets") + 1] == (
        qa_worker.QUANT_FAST_ADVISORY_TOOLSETS
    )


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

    assert (
        qa_worker._bounded_worker_argv(standard, db_path=db, task_id="t_qa") == standard
    )
    assert (
        qa_worker._bounded_worker_argv(explicit, db_path=db, task_id="t_qa") == explicit
    )


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
        "chat",
        "--max-turns",
        "12",
        "--reasoning",
        "medium",
        "-q",
        "work",
    ]
    assert bounded("workflow_role=synthesis\nworkflow_plane=response") == [
        "chat",
        "--max-turns",
        "12",
        "--reasoning",
        "medium",
        "-q",
        "work",
    ]


def test_qa_governance_review_gets_bounded_high_reasoning_budget(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        ("workflow_role=qa\nworkflow_plane=governance",),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HGFINANCE_QA_AUDIT_MAX_TURNS", "16")
    monkeypatch.setenv("HGFINANCE_QA_PRIMARY_MAX_TURNS", "16")
    monkeypatch.setenv("HGFINANCE_QA_AUDIT_REASONING", "high")

    assert qa_worker._bounded_worker_argv(
        ["chat", "-q", "review"], db_path=db, task_id="t_qa", profile="qa-department"
    ) == [
        "chat",
        "--max-turns",
        "16",
        "--reasoning",
        "high",
        "--toolsets",
        "kanban",
        "-q",
        "review",
    ]


def test_qa_governance_review_default_stops_runaway_turns(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        ("workflow_role=qa\nworkflow_plane=governance",),
    )
    conn.commit()
    conn.close()
    monkeypatch.delenv("HGFINANCE_QA_AUDIT_MAX_TURNS", raising=False)
    monkeypatch.delenv("HGFINANCE_QA_AUDIT_REASONING", raising=False)

    bounded = qa_worker._bounded_worker_argv(
        ["chat", "-q", "review"],
        db_path=db,
        task_id="t_qa",
        profile="qa-department",
    )

    assert bounded[bounded.index("--max-turns") + 1] == "8"
    assert bounded[bounded.index("--reasoning") + 1] == "high"


def test_direct_qa_primary_gets_the_same_bounded_review_budget(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        (
            (
                "origin=user-query\n"
                "workflow_role=primary\n"
                "selected_primary_profiles=qa-department\n"
                "delegation_instruction.qa-department=review"
            ),
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HGFINANCE_QA_AUDIT_MAX_TURNS", "16")
    monkeypatch.setenv("HGFINANCE_QA_PRIMARY_MAX_TURNS", "16")
    monkeypatch.setenv("HGFINANCE_QA_AUDIT_REASONING", "high")

    assert qa_worker._bounded_worker_argv(
        ["chat", "-q", "review"],
        db_path=db,
        task_id="t_qa",
        profile="qa-department",
    ) == [
        "chat",
        "--max-turns",
        "16",
        "--reasoning",
        "high",
        "--toolsets",
        "kanban",
        "-q",
        "review",
    ]


def test_qa_governance_review_replaces_broad_explicit_toolsets(tmp_path):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        ("workflow_role=qa\nworkflow_plane=governance",),
    )
    conn.commit()
    conn.close()

    bounded = qa_worker._bounded_worker_argv(
        ["chat", "--toolsets", "all", "-q", "review"],
        db_path=db,
        task_id="t_qa",
        profile="qa-department",
    )

    assert bounded == [
        "chat",
        "--max-turns",
        "8",
        "--reasoning",
        "high",
        "--toolsets",
        "kanban",
        "-q",
        "review",
    ]


def test_direct_qa_fast_advisory_uses_qa_primary_budget(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        (
            (
                "analysis_mode=fast_advisory\n"
                "origin=user-query\n"
                "workflow_role=primary\n"
                "selected_primary_profiles=qa-department"
            ),
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HGFINANCE_QA_AUDIT_MAX_TURNS", "16")
    monkeypatch.setenv("HGFINANCE_QA_PRIMARY_MAX_TURNS", "16")
    monkeypatch.setenv("HGFINANCE_QA_AUDIT_REASONING", "high")

    assert qa_worker._response_task_kind(
        qa_worker._task_body(db, "t_qa"), profile="qa-department"
    ) == "qa_primary"
    bounded = qa_worker._bounded_worker_argv(
        ["--profile", "qa-department", "chat", "-q", "work"],
        db_path=db,
        task_id="t_qa",
        profile="qa-department",
    )

    assert bounded == [
        "--profile",
        "qa-department",
        "chat",
        "--max-turns",
        "16",
        "--reasoning",
        "high",
        "--toolsets",
        "kanban",
        "-q",
        "work",
    ]


def test_qa_primary_query_is_bounded_and_does_not_reopen_general_tools():
    bounded = qa_worker._qa_primary_worker_argv(
        ["chat", "-q", "work kanban task t_qa"],
        task_body="workflow_role=primary\norigin=user-query",
    )

    assert bounded[:2] == ["chat", "-q"]
    assert "TASK PAYLOAD:" in bounded[2]
    assert "skill_view/skill_manage" in bounded[2]
    assert "kanban_complete exactly once" in bounded[2]
    assert "use terminal" in bounded[2]


def test_direct_qa_does_not_use_post_response_payload_prompt():
    assert qa_worker._is_post_response_qa(
        "workflow_role=primary\nanalysis_mode=fast_advisory"
    ) is False
    assert qa_worker._is_post_response_qa(
        "qa_phase=post_response\n"
        "qa_timing=after_ceo_response\n"
        "response_delivered=true"
    ) is True


def test_qa_audit_query_inlines_one_task_payload_and_keeps_worker_command_shape():
    bounded = qa_worker._qa_audit_worker_argv(
        ["chat", "-q", "work kanban task t_qa"],
        task_body="workflow_role=qa\nresponse_plane=completed\nchecks={}",
    )

    assert bounded[:2] == ["chat", "-q"]
    assert "TASK PAYLOAD:" in bounded[2]
    assert "workflow_role=qa" in bounded[2]
    assert "do not call kanban_show or kanban_list" in bounded[2]


def test_risk_user_primary_receives_budget_without_changing_other_profiles(tmp_path):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        ("origin=user-query\nworkflow_role=primary\nanalysis_mode=standard_analysis",),
    )
    conn.commit()
    conn.close()
    argv = ["chat", "-q", "work"]

    assert qa_worker._bounded_worker_argv(
        argv,
        db_path=db,
        task_id="t_qa",
        profile="risk-management",
    ) == [
        "chat",
        "--max-turns",
        "12",
        "--reasoning",
        "medium",
        "--toolsets",
        "kanban,risk-legal",
        "-q",
        "work",
    ]
    assert (
        qa_worker._bounded_worker_argv(
            argv,
            db_path=db,
            task_id="t_qa",
            profile="research-department",
        )
        == argv
    )


def test_risk_user_primary_restricts_dispatcher_toolsets(tmp_path):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = 't_qa'",
        ("origin=user-query\nworkflow_role=primary",),
    )
    conn.commit()
    conn.close()
    argv = ["chat", "--toolsets", "kanban", "-q", "work"]

    bounded = qa_worker._bounded_worker_argv(
        argv,
        db_path=db,
        task_id="t_qa",
        profile="risk-management",
    )

    assert bounded.count("--toolsets") == 1
    assert (
        bounded[bounded.index("--toolsets") + 1]
        == qa_worker.RISK_USER_PRIMARY_TOOLSETS
    )


def test_unmarked_risk_worker_still_uses_bounded_toolsets(tmp_path):
    db = tmp_path / "kanban.db"
    _db_with_running_run(db)

    bounded = qa_worker._bounded_worker_argv(
        ["chat", "-q", "work"],
        db_path=db,
        task_id="t_qa",
        profile="risk-management",
    )

    assert bounded[bounded.index("--toolsets") + 1] == (
        qa_worker.RISK_USER_PRIMARY_TOOLSETS
    )


def test_profile_is_read_from_dispatcher_argv():
    assert (
        qa_worker._profile_from_argv(
            ["chat", "-q", "work", "-p", "risk-management"]
        )
        == "risk-management"
    )
    assert (
        qa_worker._profile_from_argv(["--profile=risk-management", "chat"])
        == "risk-management"
    )


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

    explicit = ["chat", "--max-turns", "9", "--reasoning", "high", "-q", "work"]
    assert (
        qa_worker._bounded_worker_argv(explicit, db_path=db, task_id="t_qa") == explicit
    )


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


def test_dispatch_worker_without_run_env_drops_deprecated_terminal_cwd(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_12345678")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.setenv("TERMINAL_CWD", "/opt/data/shared-kanban/workspace")

    qa_worker._drop_dispatcher_terminal_cwd()

    assert "TERMINAL_CWD" not in os.environ


def test_dispatch_worker_workspace_marker_drops_terminal_env_without_task_marker(
    monkeypatch,
):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.setenv(
        "HERMES_KANBAN_WORKSPACE", "/opt/data/shared-kanban/workspaces/t_12345678"
    )
    monkeypatch.setenv("TERMINAL_CWD", "/opt/data/shared-kanban/workspaces/t_12345678")

    env = qa_worker._real_worker_environment()

    assert "TERMINAL_CWD" not in env


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
