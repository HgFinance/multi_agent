from __future__ import annotations

import sqlite3

import pytest

from orchestration.service_health import probe_sqlite


def _runtime_store(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            create table portfolio_runtime_snapshots(run_id text);
            create table portfolio_runtime_active(slot integer);
            create table portfolio_runtime_queue(run_id text);
            """
        )


def test_probe_sqlite_checks_integrity_and_schema(tmp_path) -> None:
    path = tmp_path / "runtime.sqlite3"
    _runtime_store(path)

    probe_sqlite(
        path,
        required_tables=(
            "portfolio_runtime_snapshots",
            "portfolio_runtime_active",
            "portfolio_runtime_queue",
        ),
    )


def test_probe_sqlite_fails_closed_for_missing_store_or_table(tmp_path) -> None:
    with pytest.raises(RuntimeError):
        probe_sqlite(tmp_path / "missing.sqlite3")

    path = tmp_path / "partial.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("create table portfolio_runtime_queue(run_id text)")
    with pytest.raises(RuntimeError):
        probe_sqlite(path, required_tables=("portfolio_runtime_snapshots",))
