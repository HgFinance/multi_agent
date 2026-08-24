from __future__ import annotations

from orchestration.conditional_rules.worker_store import PostgresRuleWorkerStore


class _Cursor:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query, _params=None) -> None:
        self.queries.append(str(query))

    def fetchall(self):
        return []


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
