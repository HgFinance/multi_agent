"""QA Decision Canonical write-through의 commit/rollback 계약."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evidence"))

from repository import QaDecisionPersistenceError, PostgresAuditRepository  # noqa: E402


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        self.calls.append((query, params))
        if self.connection.fail:
            raise RuntimeError("database unavailable")

    def fetchone(self):
        return (self.connection.qa_decision_id,)


class FakeConnection:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.qa_decision_id = uuid4()
        self.commits = 0
        self.rollbacks = 0
        self.cursor_instance = FakeCursor(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

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
        qa_decision_id=uuid4(),
        artifact_version_id=uuid4(),
        gate="evidence_qa",
        decision=SimpleNamespace(value="FAIL"),
        calculation_version="qa-evidence-p0-v1",
        input_hash="hash-qa-1",
        reason_codes=(SimpleNamespace(value="fact_without_evidence"),),
        decided_by="svc_qa_evaluator",
        trace_id=uuid4(),
        decided_at=datetime.now(timezone.utc),
        claim_checks=(),
        findings=(),
    )


def test_qa_decision_is_committed_as_one_transaction():
    connection = FakeConnection()
    repo = PostgresAuditRepository(FakePool(connection))
    repo.save_qa_assessment(assessment())
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert len(connection.cursor_instance.calls) == 1


def test_qa_persistence_failure_rolls_back():
    connection = FakeConnection(fail=True)
    repo = PostgresAuditRepository(FakePool(connection))
    with pytest.raises(QaDecisionPersistenceError):
        repo.save_qa_assessment(assessment())
    assert connection.commits == 0
    assert connection.rollbacks == 1
