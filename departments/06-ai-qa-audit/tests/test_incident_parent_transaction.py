"""QA Incident parent creation and child write transaction tests."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from uuid import uuid4

QA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QA_DIR))
sys.path.insert(0, str(QA_DIR / "audit"))
sys.path.insert(0, str(QA_DIR / "evidence"))

from audit.repository import PostgresAuditRepository


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, query: str, params: tuple) -> None:
        self.calls.append((query, params))
        if self.connection.fail_on_call == len(self.calls):
            raise RuntimeError("database unavailable")


class FakeConnection:
    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.fail_on_call = fail_on_call
        self.cursor_instance = FakeCursor(self)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def getconn(self) -> FakeConnection:
        return self.connection

    def putconn(self, _connection: FakeConnection) -> None:
        return None


def _event() -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        incident_event_id=uuid4(),
        incident_id=uuid4(),
        source="agent-ops-monitor",
        entry_type=SimpleNamespace(value="FACT"),
        summary="fallback rate exceeded",
        evidence={"test_only": True},
        occurred_at=now,
        recorded_at=now,
        recorded_by="qa-test",
    )


def _action(incident_id) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        corrective_action_id=uuid4(),
        incident_id=incident_id,
        finding_id=None,
        owner="qa-test",
        action_plan={"test_only": True},
        due_at=now,
        status=SimpleNamespace(value="OPEN"),
        created_at=now,
    )


def test_incident_event_creates_parent_and_commits_atomically() -> None:
    connection = FakeConnection()
    PostgresAuditRepository(FakePool(connection)).insert_incident_event(_event())

    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert "audit.incidents" in connection.cursor_instance.calls[0][0]
    assert "audit.incident_events" in connection.cursor_instance.calls[1][0]


def test_incident_event_rolls_back_parent_when_child_insert_fails() -> None:
    connection = FakeConnection(fail_on_call=2)

    try:
        PostgresAuditRepository(FakePool(connection)).insert_incident_event(_event())
    except RuntimeError:
        pass
    else:
        raise AssertionError("child insert failure must propagate")

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_corrective_action_creates_incident_parent_in_same_transaction() -> None:
    connection = FakeConnection()
    PostgresAuditRepository(FakePool(connection)).insert_corrective_action(
        _action(uuid4())
    )

    assert connection.commits == 1
    assert len(connection.cursor_instance.calls) == 2
    assert "audit.incidents" in connection.cursor_instance.calls[0][0]
    assert "audit.corrective_actions" in connection.cursor_instance.calls[1][0]
