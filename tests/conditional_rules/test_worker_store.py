from __future__ import annotations

from uuid import uuid4

from orchestration.conditional_rules.worker_store import PostgresRuleWorkerStore


class _Cursor:
    def __init__(self, rows: list | None = None) -> None:
        self.queries: list[str] = []
        self.params: list = []
        self._rows = rows or []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query, params=None) -> None:
        self.queries.append(str(query))
        self.params.append(params)

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def cursor(self):
        return self._cursor


def test_deferred_activation_locks_only_rows_the_worker_updates(monkeypatch) -> None:
    cursor = _Cursor()
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))

    assert store.activate_ready_bundles(limit=10) == 0

    activation_query = next(query for query in cursor.queries if "WAITING_FOR_IMMEDIATE_FILL" in query)
    assert "for update of bundle,rule skip locked" in activation_query.lower()
    assert "for update of bundle,rule,request skip locked" not in activation_query.lower()


def test_oco_siblings_are_cancelled_in_the_submitting_transaction() -> None:
    """A filled take-profit must disarm its stop-loss, and vice versa.

    Both legs are exits for one position, so leaving the loser armed sells that
    position twice.  The cancel rides the same transaction as the submission so
    a crash right after the broker accepted the order cannot leave the pair
    half-resolved.
    """

    winner = uuid4()
    loser = uuid4()
    cursor = _Cursor(rows=[(loser, 1)])
    store = PostgresRuleWorkerStore("postgresql://test")

    assert store._cancel_oco_siblings(cursor, rule_id=winner) == 1

    cancel_query = cursor.queries[0].lower()
    assert "oco_group_id" in cancel_query
    assert "state='cancelled'" in cancel_query
    # Only an armed sibling is revoked; one already executing belongs to the
    # broker, and the group must not reach across users, funds or books.
    assert "sibling.state='active'" in cancel_query
    for boundary in ("sibling.user_id=winner.user_id",
                     "sibling.fund_id=winner.fund_id",
                     "sibling.book_id=winner.book_id"):
        assert boundary in cancel_query
    assert "OCO_CANCELLED" in cursor.queries[1]


def test_rule_without_an_oco_group_cancels_nothing() -> None:
    cursor = _Cursor(rows=[])
    store = PostgresRuleWorkerStore("postgresql://test")

    assert store._cancel_oco_siblings(cursor, rule_id=uuid4()) == 0
    assert len(cursor.queries) == 1
