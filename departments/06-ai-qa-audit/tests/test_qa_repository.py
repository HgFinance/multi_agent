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

from repository import PostgresAuditRepository, QaDecisionPersistenceError
from audit.db_session import runtime_session_dsn


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))
        if self.connection.fail:
            raise RuntimeError("database unavailable")

    def fetchone(self):
        if self.calls and self.calls[-1][0] == "select current_user":
            return ("svc_audit_api",)
        if self.calls and "select session_user, current_user" in self.calls[-1][0]:
            return ("postgres", "svc_qa_audit", "off")
        return (self.connection.qa_decision_id,)


class FakeConnection:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.qa_decision_id = uuid4()
        self.commits = 0
        self.rollbacks = 0
        self.session_modes = []
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

    def set_session(self, **kwargs):
        self.session_modes.append(kwargs)

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
    assert connection.session_modes == [{"readonly": False}]
    assert len(connection.cursor_instance.calls) == 1


def test_qa_persistence_failure_rolls_back():
    connection = FakeConnection(fail=True)
    repo = PostgresAuditRepository(FakePool(connection))
    with pytest.raises(QaDecisionPersistenceError):
        repo.save_qa_assessment(assessment())
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_qa_finding_persists_required_opened_by_column():
    connection = FakeConnection()
    repo = PostgresAuditRepository(FakePool(connection))
    value = assessment()
    finding = SimpleNamespace(
        finding_id=uuid4(),
        fund_id=None,
        finding_type="evidence-gap",
        severity=SimpleNamespace(value="HIGH"),
        artifact_version_id=value.artifact_version_id,
        description="claim lacks primary evidence",
        opened_by="svc_qa_evaluator",
        trace_id=value.trace_id,
        created_at=value.decided_at,
    )
    value.findings = (finding,)

    repo.save_qa_assessment(value)

    query, params = connection.cursor_instance.calls[-1]
    assert "opened_by" in query
    assert params[6] == "svc_qa_evaluator"  # owner
    assert params[7] == "svc_qa_evaluator"  # required audit actor


def test_repository_drops_pool_login_to_runtime_role(monkeypatch):
    connection = FakeConnection()
    pool = FakePool(connection)
    repo = PostgresAuditRepository(pool)
    monkeypatch.setenv("DATABASE_RUNTIME_ROLE", "svc_audit_api")

    assert repo._get_connection() is connection

    assert connection.session_modes == [{"readonly": False}]
    assert connection.commits == 2
    assert connection.cursor_instance.calls == [
        ('SET ROLE "svc_audit_api"', None),
        ("select current_user", None),
    ]


def test_qa_runtime_role_upgrades_supavisor_to_session_mode(monkeypatch):
    monkeypatch.setenv("DATABASE_RUNTIME_ROLE", "svc_audit_api")
    monkeypatch.delenv("DATABASE_SESSION_URL", raising=False)

    assert runtime_session_dsn(
        "postgresql://user:secret@aws-1-ap-northeast-2.pooler.supabase.com:6543/postgres"
    ) == (
        "postgresql://user:secret@aws-1-ap-northeast-2.pooler.supabase.com:5432/postgres"
    )


def test_qa_runtime_role_rejects_transaction_pool_override(monkeypatch):
    monkeypatch.setenv("DATABASE_RUNTIME_ROLE", "svc_audit_api")
    monkeypatch.setenv(
        "DATABASE_SESSION_URL",
        "postgresql://user:secret@pool.example:6543/postgres",
    )

    with pytest.raises(RuntimeError, match="transaction-pool port 6543"):
        runtime_session_dsn("postgresql://unused")


def test_repository_discards_connection_when_configuration_fails(monkeypatch):
    connection = FakeConnection()

    def broken_rollback():
        raise RuntimeError("connection is broken")

    connection.rollback = broken_rollback

    class ClosingPool(FakePool):
        def __init__(self, conn):
            super().__init__(conn)
            self.discarded = False

        def putconn(self, _connection, *, close=False):
            self.discarded = close

    pool = ClosingPool(connection)
    repo = PostgresAuditRepository(pool)
    monkeypatch.setenv("DATABASE_RUNTIME_ROLE", "postgres")

    with pytest.raises(RuntimeError, match="not allowlisted"):
        repo._get_connection()

    assert pool.discarded is True


def test_repository_runtime_status_proves_writable_role(monkeypatch):
    connection = FakeConnection()
    repo = PostgresAuditRepository(FakePool(connection))
    monkeypatch.delenv("DATABASE_RUNTIME_ROLE", raising=False)

    status = repo.runtime_database_status()

    assert status == {
        "session_user": "postgres",
        "current_user": "svc_qa_audit",
        "transaction_read_only": "off",
    }
    assert connection.commits == 1
