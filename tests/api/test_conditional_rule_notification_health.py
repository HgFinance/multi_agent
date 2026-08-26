from __future__ import annotations

import pytest

from apps.api import conditional_rule_notification_health as health


class _Cursor:
    def __init__(self) -> None:
        self.executions: list[object] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: object) -> None:
        self.executions.append(statement)

    @staticmethod
    def fetchone() -> tuple[int]:
        return (1,)


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor


def test_probe_database_checks_role_assumption(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _Cursor()
    calls: list[tuple[str, int]] = []

    def connect(dsn: str, *, connect_timeout: int) -> _Connection:
        calls.append((dsn, connect_timeout))
        return _Connection(cursor)

    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://isolated/control")
    monkeypatch.setenv("TEST_DATABASE_ROLE", "svc_test_runtime")
    monkeypatch.setattr(health.psycopg2, "connect", connect)

    health._probe_database(
        dsn_name="TEST_DATABASE_URL",
        role_name="TEST_DATABASE_ROLE",
        default_role="svc_default",
    )

    assert calls == [("postgresql://isolated/control", 3)]
    assert len(cursor.executions) == 2


@pytest.mark.parametrize(
    ("dsn", "role"),
    (("", "svc_test_runtime"), ("postgresql://isolated/control", "bad-role;")),
)
def test_probe_database_rejects_missing_or_invalid_authority(
    monkeypatch: pytest.MonkeyPatch,
    dsn: str,
    role: str,
) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", dsn)
    monkeypatch.setenv("TEST_DATABASE_ROLE", role)

    with pytest.raises(RuntimeError, match="authority is not configured"):
        health._probe_database(
            dsn_name="TEST_DATABASE_URL",
            role_name="TEST_DATABASE_ROLE",
            default_role="svc_default",
        )
