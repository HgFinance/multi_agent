"""Risk Decision Repository의 commit·idempotency 계약."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from risk_repository import (  # noqa: E402
    RiskDecisionPersistenceError,
    RiskDecisionRepository,
)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        self.executed.append((query, params))
        if self.connection.fail:
            raise RuntimeError("database unavailable")

    def fetchone(self):
        return (self.connection.decision_id,)


class FakeConnection:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.decision_id = uuid4()
        self.commits = 0
        self.rollbacks = 0
        self.cursor_instance = FakeCursor(self)

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def getconn(self):
        return self.connection

    def putconn(self, _connection):
        pass

    def closeall(self):
        pass


def assessment():
    return SimpleNamespace(
        risk_request_id=uuid4(),
        decision=SimpleNamespace(
            verdict=SimpleNamespace(value="REJECT"),
            approved_quantity=None,
            max_price=None,
            expires_at=datetime.now(timezone.utc),
            decided_by="svc_risk_engine",
        ),
        approved_legs=(),
        aggregate_exposure={},
        reason_codes=(SimpleNamespace(value="stale_snapshot"),),
        check_results=(SimpleNamespace(check_name="freshness", passed=False, detail="stale"),),
        calculation_version="risk-p0-v1",
        input_hash="hash-1",
    )


def test_risk_decision_is_committed():
    connection = FakeConnection()
    repo = RiskDecisionRepository(FakePool(connection))
    decision_id = repo.save(assessment())
    assert decision_id == connection.decision_id
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_risk_persistence_failure_is_not_swallowed():
    connection = FakeConnection(fail=True)
    repo = RiskDecisionRepository(FakePool(connection))
    with pytest.raises(RiskDecisionPersistenceError):
        repo.save(assessment())
    assert connection.commits == 0
    assert connection.rollbacks == 1
