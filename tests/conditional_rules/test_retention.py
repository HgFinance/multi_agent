from __future__ import annotations

from typing import Any, Self

from orchestration.conditional_rules.retention import ConditionalRuleRetentionStore


class Cursor:
    def __init__(self, *, rowcount: int = 0) -> None:
        self.rowcount = rowcount
        self.executed: list[tuple[Any, Any]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: Any, args: Any = ()) -> None:
        self.executed.append((query, args))


class Connection:
    def __init__(self, *, rowcount: int = 0) -> None:
        self.cursor_value = Cursor(rowcount=rowcount)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> Cursor:
        return self.cursor_value

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


def test_retention_deletes_only_bounded_detail_and_published_outbox() -> None:
    connection = Connection(rowcount=2)
    store = ConditionalRuleRetentionStore(
        "postgresql://test",
        batch_size=500,
        connect_factory=lambda *_args, **_kwargs: connection,
    )

    result = store.run_once()

    assert result.available is True
    assert result.deleted_total == 10
    assert result.outbox_deleted == 2
    assert connection.commits == 1
    assert connection.rollbacks == 0

    sql_text = "\n".join(str(query) for query, _args in connection.cursor_value.executed)
    assert "published_at IS NOT NULL" in sql_text
    assert "conditional_rule_outbox" in sql_text
    assert "conditional_rule_executions" in sql_text
    assert "conditional_rule_triggers" in sql_text
    assert "conditional_rule_evaluations" in sql_text
    assert "conditional_trade_rule_events" in sql_text
    assert "LIMIT %s" in sql_text
    assert "directive_id IS NOT NULL" in sql_text


def test_retention_does_not_connect_when_disabled() -> None:
    def fail_connect(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("disabled retention must not connect")

    result = ConditionalRuleRetentionStore(
        "postgresql://unused",
        enabled=False,
        connect_factory=fail_connect,
    ).run_once()

    assert result.enabled is False
    assert result.available is False
    assert result.error_code == "DISABLED"


def test_retention_failure_rolls_back_and_fails_open() -> None:
    class BrokenConnection(Connection):
        def cursor(self) -> Cursor:
            raise RuntimeError("database unavailable")

    connection = BrokenConnection()
    result = ConditionalRuleRetentionStore(
        "postgresql://test",
        connect_factory=lambda *_args, **_kwargs: connection,
    ).run_once()

    assert result.available is False
    assert result.error_code == "CONDITIONAL_RULE_RETENTION_UNAVAILABLE"
    assert connection.rollbacks == 1


def test_retention_configuration_matches_policy() -> None:
    store = ConditionalRuleRetentionStore(
        "postgresql://test",
        evaluation_retention_days=30,
        detail_retention_days=90,
        outbox_retention_days=7,
        batch_size=500,
    )

    assert store.evaluation_retention_days == 30
    assert store.detail_retention_days == 90
    assert store.outbox_retention_days == 7
    assert store.batch_size == 500


def test_readiness_check_is_read_only() -> None:
    connection = Connection()

    class ReadinessCursor(Cursor):
        def fetchone(self) -> tuple[str]:
            return ("conditional_trade_rules",)

    connection.cursor_value = ReadinessCursor()
    store = ConditionalRuleRetentionStore(
        "postgresql://test",
        connect_factory=lambda *_args, **_kwargs: connection,
    )

    store.check_ready()

    assert connection.commits == 0
    assert all("DELETE" not in str(query).upper() for query, _args in connection.cursor_value.executed)
