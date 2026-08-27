from __future__ import annotations

import os
import runpy
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "deploy" / "hermes-dispatch-guard" / "sitecustomize.py"
PREFLIGHT = ROOT / "deploy" / "hermes-dispatch-guard" / "check_guard.py"
SENSITIVE_ENV = (
    "MCP_RESEARCH_API_KEY",
    "MCP_RISK_API_KEY",
    "MCP_TRADING_ORDER_API_KEY",
    "TIMESCALE_DATABASE_URL",
)


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("create table tasks (id text, assignee text, body text)")
    return connection


def _load_module(monkeypatch, original, spawn=None):
    package = types.ModuleType("hermes_cli")
    kanban_db = types.ModuleType("hermes_cli.kanban_db")
    kanban_db.check_respawn_guard = original
    if spawn is not None:
        kanban_db._default_spawn = spawn
    package.kanban_db = kanban_db
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", kanban_db)
    monkeypatch.setenv("HGFINANCE_DISPATCH_GUARD", "1")
    runpy.run_path(str(PATCH), run_name="hgfinance_dispatch_guard_test")
    return kanban_db


def _load_patch(monkeypatch, original):
    return _load_module(monkeypatch, original).check_respawn_guard


def test_dispatch_guard_delegates_to_current_two_argument_hermes(monkeypatch):
    observed: list[tuple[sqlite3.Connection, str]] = []

    def original(connection, task_id):
        observed.append((connection, task_id))
        return "native"

    patched = _load_patch(monkeypatch, original)
    connection = _database()

    assert patched(connection, "t_plain") == "native"
    assert observed == [(connection, "t_plain")]


def test_dispatch_guard_keeps_legacy_lane_signature_compatible(monkeypatch):
    observed: list[tuple[str, str]] = []

    def original(connection, task_id, *, lane="ready"):
        del connection
        observed.append((task_id, lane))
        return "legacy"

    patched = _load_patch(monkeypatch, original)
    connection = _database()

    assert patched(connection, "t_plain", lane="blocked") == "legacy"
    assert observed == [("t_plain", "blocked")]


def test_dispatch_guard_skips_a_complete_deterministic_bff_root(monkeypatch):
    def original(connection, task_id):
        del connection, task_id
        return "native"

    patched = _load_patch(monkeypatch, original)
    connection = _database()
    connection.execute(
        "insert into tasks values (?, ?, ?)",
        (
            "t_bff_route",
            "ceo-agent",
            (
                "workflow_role=root\n"
                "workflow_mode=analysis\n"
                "producer=portfolio-bff-deterministic\n"
                "selected_primary_profiles=research-department"
            ),
        ),
    )

    assert patched(connection, "t_bff_route") == "hgfinance_control_plane_root"


def test_dispatch_guard_exposes_worker_observer_registry_path(monkeypatch):
    def original(connection, task_id):
        del connection, task_id
        return "native"

    _load_module(monkeypatch, original)

    assert str(ROOT / "scripts") in sys.path


def test_dispatch_health_probe_filters_tracking_and_full_capacity(monkeypatch):
    package = types.ModuleType("hermes_cli")
    kanban_db = types.ModuleType("hermes_cli.kanban_db")
    profiles = types.ModuleType("hermes_cli.profiles")

    def original_guard(connection, task_id):
        del connection, task_id
        return "native"

    kanban_db.check_respawn_guard = original_guard
    kanban_db.has_spawnable_ready = lambda connection: True
    profiles.profile_exists = lambda name: name in {
        "ceo-agent", "research-department"
    }
    package.kanban_db = kanban_db
    package.profiles = profiles
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", kanban_db)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", profiles)
    monkeypatch.setenv("HGFINANCE_DISPATCH_GUARD", "1")
    monkeypatch.setenv("KANBAN_DISPATCH_MAX_SPAWN", "2")
    runpy.run_path(str(PATCH), run_name="hgfinance_dispatch_health_test")

    connection = sqlite3.connect(":memory:")
    connection.execute(
        "create table tasks (assignee text, body text, status text, claim_lock text)"
    )
    connection.execute(
        "insert into tasks values (?, ?, ?, ?)",
        ("ceo-agent", "strategy_research_tracking_only=true", "ready", None),
    )
    assert kanban_db.has_spawnable_ready(connection) is False

    connection.execute(
        "insert into tasks values (?, ?, ?, ?)",
        ("research-department", "real work", "ready", None),
    )
    assert kanban_db.has_spawnable_ready(connection) is True

    connection.executemany(
        "insert into tasks values (?, ?, ?, ?)",
        [("research-department", "running", "running", None)] * 2,
    )
    assert kanban_db.has_spawnable_ready(connection) is False


def test_dispatch_worker_observer_does_not_reap_native_child(monkeypatch):
    del monkeypatch

    assert "os.waitpid" not in PATCH.read_text(encoding="utf-8")


def test_dispatch_guard_ignores_budget_fallback_after_terminal_completion(monkeypatch, tmp_path):
    package = types.ModuleType("hermes_cli")
    kanban_db = types.ModuleType("hermes_cli.kanban_db")
    turn_finalizer = types.ModuleType("agent.turn_finalizer")
    calls: list[tuple[str, int, int]] = []
    finalize_calls: list[dict] = []

    def original(task_id, api_call_count, max_iterations, logger):
        del logger
        calls.append((task_id, api_call_count, max_iterations))

    def finalize(agent, *args, **kwargs):
        del agent, args
        finalize_calls.append(kwargs)
        return "finished"

    turn_finalizer._record_kanban_budget_exhausted = original
    turn_finalizer.finalize_turn = finalize
    package.kanban_db = kanban_db
    agent_package = types.ModuleType("agent")
    agent_package.turn_finalizer = turn_finalizer
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", kanban_db)
    monkeypatch.setitem(sys.modules, "agent", agent_package)
    monkeypatch.setitem(sys.modules, "agent.turn_finalizer", turn_finalizer)
    monkeypatch.setenv("HGFINANCE_DISPATCH_GUARD", "1")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_done")

    db_path = tmp_path / "kanban.db"
    connection = sqlite3.connect(db_path)
    connection.execute("create table tasks (id text primary key, status text)")
    connection.executemany(
        "insert into tasks values (?, ?)",
        [("t_done", "done"), ("t_running", "running")],
    )
    connection.commit()
    connection.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    runpy.run_path(str(PATCH), run_name="hgfinance_dispatch_terminal_race_test")

    turn_finalizer._record_kanban_budget_exhausted(
        "t_done", 8, 8, types.SimpleNamespace()
    )
    turn_finalizer._record_kanban_budget_exhausted(
        "t_running", 8, 8, types.SimpleNamespace()
    )
    turn_finalizer.finalize_turn(
        types.SimpleNamespace(max_iterations=8),
        final_response=None,
        api_call_count=8,
        _turn_exit_reason="budget_exhausted",
    )

    assert calls == [("t_running", 8, 8)]
    assert finalize_calls == [{
        "final_response": "",
        "api_call_count": 8,
        "_turn_exit_reason": "text_response(kanban_terminal)",
        "_pending_verification_response": None,
        "_pending_verification_response_previewed": False,
    }]


@pytest.mark.parametrize(("assignee", "expected"), [
    ("research-department", {"MCP_RESEARCH_API_KEY"}),
    ("research-liaison", {"MCP_RESEARCH_API_KEY"}),
    ("quant-liaison", {"MCP_RESEARCH_API_KEY"}),
    ("quant-backtest-department", {
        "MCP_RESEARCH_API_KEY", "TIMESCALE_DATABASE_URL"}),
    ("trading-department", {"MCP_TRADING_ORDER_API_KEY"}),
    ("risk-management", {"MCP_RISK_API_KEY"}),
    ("ceo-agent", set()),
    ("unknown-profile", set()),
])
def test_dispatch_spawn_scopes_mcp_secrets_by_assignee(
        monkeypatch, assignee, expected):
    observed: list[set[str]] = []

    def original_guard(connection, task_id):
        del connection, task_id
        return "native"

    def original_spawn(task, workspace, *, board=None):
        del task, workspace, board
        observed.append({
            name for name in (
                "MCP_RESEARCH_API_KEY", "MCP_RISK_API_KEY",
                "MCP_TRADING_ORDER_API_KEY",
                "TIMESCALE_DATABASE_URL")
            if name in os.environ
        })
        return 123

    monkeypatch.setenv("MCP_RESEARCH_API_KEY", "research-test-secret")
    monkeypatch.setenv("MCP_RISK_API_KEY", "risk-test-secret")
    monkeypatch.setenv("MCP_TRADING_ORDER_API_KEY", "order-test-secret")
    monkeypatch.setenv("TIMESCALE_DATABASE_URL", "postgresql://test/market")
    module = _load_module(
        monkeypatch, original_guard, spawn=original_spawn)

    assert module._default_spawn(
        SimpleNamespace(assignee=assignee), "/tmp/work", board="default") == 123
    assert observed == [expected]
    # The long-lived dispatcher retains both credentials for the next profile;
    # only the spawned child receives the scoped view.
    assert os.environ["MCP_RESEARCH_API_KEY"] == "research-test-secret"
    assert os.environ["MCP_RISK_API_KEY"] == "risk-test-secret"
    assert os.environ["MCP_TRADING_ORDER_API_KEY"] == "order-test-secret"
    assert os.environ["TIMESCALE_DATABASE_URL"] == "postgresql://test/market"


def test_dispatch_spawn_restores_environment_after_failure(monkeypatch):
    def original_guard(connection, task_id):
        del connection, task_id
        return "native"

    def failing_spawn(task, workspace):
        del task, workspace
        assert "MCP_TRADING_ORDER_API_KEY" not in os.environ
        raise RuntimeError("spawn failed")

    monkeypatch.setenv("MCP_RESEARCH_API_KEY", "research-test-secret")
    monkeypatch.setenv("MCP_RISK_API_KEY", "risk-test-secret")
    monkeypatch.setenv("MCP_TRADING_ORDER_API_KEY", "order-test-secret")
    module = _load_module(monkeypatch, original_guard, spawn=failing_spawn)

    with pytest.raises(RuntimeError, match="spawn failed"):
        module._default_spawn(
            SimpleNamespace(assignee="research-department"), "/tmp/work")
    assert os.environ["MCP_RESEARCH_API_KEY"] == "research-test-secret"
    assert os.environ["MCP_RISK_API_KEY"] == "risk-test-secret"
    assert os.environ["MCP_TRADING_ORDER_API_KEY"] == "order-test-secret"


def test_dispatch_spawn_hook_marks_secret_scope_active(monkeypatch):
    def original_guard(connection, task_id):
        del connection, task_id
        return "native"

    def original_spawn(task, workspace):
        del task, workspace
        return 123

    module = _load_module(monkeypatch, original_guard, spawn=original_spawn)

    assert getattr(
        module._default_spawn, "_hgfinance_secret_scope_active", False
    ) is True
    assert os.environ["HGFINANCE_DISPATCH_SECRET_SCOPE_STATUS"] == "ACTIVE_V1"


def test_dispatch_guard_fails_closed_when_spawn_hook_is_missing(monkeypatch):
    def original_guard(connection, task_id):
        del connection, task_id
        return "native"

    for name in SENSITIVE_ENV:
        monkeypatch.setenv(name, f"{name.casefold()}-test-secret")

    module = _load_module(monkeypatch, original_guard)

    assert not hasattr(module, "_default_spawn")
    assert all(name not in os.environ for name in SENSITIVE_ENV)
    assert os.environ["HGFINANCE_DISPATCH_SECRET_SCOPE_STATUS"] == (
        "FAIL_CLOSED:NO_DEFAULT_SPAWN_HOOK"
    )


def test_dispatch_guard_fails_closed_when_patch_installation_raises(monkeypatch):
    package = types.ModuleType("hermes_cli")
    kanban_db = types.ModuleType("hermes_cli.kanban_db")
    package.kanban_db = kanban_db
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", kanban_db)
    monkeypatch.setenv("HGFINANCE_DISPATCH_GUARD", "1")
    for name in SENSITIVE_ENV:
        monkeypatch.setenv(name, f"{name.casefold()}-test-secret")

    runpy.run_path(str(PATCH), run_name="hgfinance_dispatch_guard_test")

    assert all(name not in os.environ for name in SENSITIVE_ENV)
    assert os.environ["HGFINANCE_DISPATCH_SECRET_SCOPE_STATUS"] == (
        "FAIL_CLOSED:PATCH_INSTALL_FAILED"
    )


def test_dispatcher_workspace_does_not_emit_false_cwd_warning(monkeypatch):
    package = types.ModuleType("hermes_cli")
    kanban_db = types.ModuleType("hermes_cli.kanban_db")
    config = types.ModuleType("hermes_cli.config")
    calls: list[str] = []

    def warn_deprecated_cwd_env_vars(*_args, **_kwargs):
        calls.append("warned")

    def check_respawn_guard(connection, task_id):
        del connection, task_id
        return "native"

    config.warn_deprecated_cwd_env_vars = warn_deprecated_cwd_env_vars
    kanban_db.check_respawn_guard = check_respawn_guard
    package.kanban_db = kanban_db
    package.config = config
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", kanban_db)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setenv("HGFINANCE_DISPATCH_GUARD", "1")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", "/tmp/task-workspace")

    runpy.run_path(str(PATCH), run_name="hgfinance_dispatch_guard_test")
    config.warn_deprecated_cwd_env_vars()

    assert calls == []

    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE")
    config.warn_deprecated_cwd_env_vars()
    assert calls == ["warned"]


def test_dispatch_preflight_accepts_the_installed_scope_hook(
        monkeypatch, capsys):
    def original_guard(connection, task_id):
        del connection, task_id
        return "native"

    def original_spawn(task, workspace):
        del task, workspace
        return 123

    _load_module(monkeypatch, original_guard, spawn=original_spawn)
    preflight = runpy.run_path(
        str(PREFLIGHT), run_name="hgfinance_dispatch_guard_preflight_test"
    )

    assert preflight["main"]() == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == (
        "dispatcher credential-scope preflight: ACTIVE_V1"
    )
    assert captured.err == ""


def test_dispatch_preflight_rejects_an_unmarked_spawn_hook(monkeypatch, capsys):
    package = types.ModuleType("hermes_cli")
    kanban_db = types.ModuleType("hermes_cli.kanban_db")
    kanban_db._default_spawn = lambda task, workspace: None
    package.kanban_db = kanban_db
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", kanban_db)
    monkeypatch.setenv("HGFINANCE_DISPATCH_SECRET_SCOPE_STATUS", "ACTIVE_V1")
    preflight = runpy.run_path(
        str(PREFLIGHT), run_name="hgfinance_dispatch_guard_preflight_test"
    )

    assert preflight["main"]() == 78
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == (
        "dispatcher credential-scope preflight: inactive (fail closed)"
    )


def test_dispatch_preflight_rejects_hermes_import_failure(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", None)
    preflight = runpy.run_path(
        str(PREFLIGHT), run_name="hgfinance_dispatch_guard_preflight_test"
    )

    assert preflight["main"]() == 78
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == (
        "dispatcher credential-scope preflight: Hermes import failed"
    )
