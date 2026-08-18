from __future__ import annotations

import runpy
import sqlite3
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "deploy" / "hermes-dispatch-guard" / "sitecustomize.py"


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("create table tasks (id text, assignee text, body text)")
    return connection


def _load_patch(monkeypatch, original):
    package = types.ModuleType("hermes_cli")
    kanban_db = types.ModuleType("hermes_cli.kanban_db")
    kanban_db.check_respawn_guard = original
    package.kanban_db = kanban_db
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", kanban_db)
    monkeypatch.setenv("HGFINANCE_DISPATCH_GUARD", "1")
    runpy.run_path(str(PATCH), run_name="hgfinance_dispatch_guard_test")
    return kanban_db.check_respawn_guard


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
