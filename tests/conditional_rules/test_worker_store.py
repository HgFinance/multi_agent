from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from orchestration.conditional_rules.worker_store import (
    ConditionalRuleOutboxRow,
    PostgresRuleWorkerStore,
)


class _Cursor:
    def __init__(self, rows: list | None = None, fetchone_rows: list | None = None) -> None:
        self.queries: list[str] = []
        self.params: list = []
        self._rows = rows or []
        self._fetchone_rows = list(fetchone_rows or [])
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query, params=None) -> None:
        self.queries.append(str(query))
        self.params.append(params)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._fetchone_rows.pop(0) if self._fetchone_rows else None


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
    cursor = _Cursor(
        rows=[(loser, 1, "ACTIVE")],
        fetchone_rows=[("oco-exit-1", uuid4(), uuid4(), uuid4())],
    )
    store = PostgresRuleWorkerStore("postgresql://test")

    assert store._cancel_oco_siblings(cursor, rule_id=winner) == 1

    cancel_query = next(
        query.lower()
        for query in cursor.queries
        if "state in ('active','paused')" in query.lower()
    )
    assert "oco_group_id" in cancel_query
    assert "state='cancelled'" in cancel_query
    # Only an armed sibling is revoked; one already executing belongs to the
    # broker, and the group must not reach across users, funds or books.
    assert "sibling.user_id=%s" in cancel_query
    assert "sibling.fund_id=%s" in cancel_query
    assert "sibling.book_id=%s" in cancel_query
    assert "state in ('active','paused')" in cancel_query
    assert any(
        params and "OCO_CANCELLED" in str(params)
        for params in cursor.params
    )


def test_rule_without_an_oco_group_cancels_nothing() -> None:
    cursor = _Cursor(rows=[], fetchone_rows=[None])
    store = PostgresRuleWorkerStore("postgresql://test")

    assert store._cancel_oco_siblings(cursor, rule_id=uuid4()) == 0
    assert len(cursor.queries) == 1


def _outbox_row() -> ConditionalRuleOutboxRow:
    return ConditionalRuleOutboxRow(
        event_id="conditional-event-1",
        aggregate_id="rule-1",
        event_type="DIRECTIVE_SUBMITTED",
        payload={"directive_id": "directive-1"},
        created_at=datetime.now(timezone.utc),
        attempts=0,
    )


def test_outbox_claim_uses_a_short_lease_transaction(monkeypatch) -> None:
    db_row = (
        "conditional-event-1",
        "rule-1",
        "DIRECTIVE_SUBMITTED",
        {"directive_id": "directive-1"},
        datetime.now(timezone.utc),
        0,
    )
    cursor = _Cursor(rows=[db_row])
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))

    token, rows = store._claim_outbox_rows(limit=10)

    assert token
    assert rows[0].event_id == "conditional-event-1"
    claim_query = next(
        query.lower() for query in cursor.queries if "claim_token" in query.lower()
    )
    assert "for update skip locked" in claim_query
    assert "claim_token" in claim_query
    assert "claim_expires_at" in claim_query
    assert "published_at is null" in claim_query


def test_outbox_publish_is_between_claim_and_finalize(monkeypatch) -> None:
    store = PostgresRuleWorkerStore("postgresql://test")
    row = _outbox_row()
    events: list[object] = []

    monkeypatch.setattr(
        store,
        "_claim_outbox_rows",
        lambda *, limit: (events.append(("claim", limit)) or "claim-1", [row]),
    )

    def finalize(
        value: ConditionalRuleOutboxRow, *, claim_token: str, error: str | None
    ) -> bool:
        events.append(("finalize", value.event_id, claim_token, error))
        return True

    monkeypatch.setattr(store, "_finalize_outbox_claim", finalize)

    result = store.drain_outbox(
        lambda value: events.append(("publish", value.event_id)), limit=10
    )

    assert events == [
        ("claim", 10),
        ("publish", "conditional-event-1"),
        ("finalize", "conditional-event-1", "claim-1", None),
    ]
    assert result == {"picked": 1, "published": 1, "failed": 0, "lost": 0}


def test_outbox_publish_failure_releases_the_lease_for_retry(monkeypatch) -> None:
    store = PostgresRuleWorkerStore("postgresql://test")
    row = _outbox_row()
    finalized: list[tuple[str, str | None]] = []

    monkeypatch.setattr(store, "_claim_outbox_rows", lambda *, limit: ("claim-1", [row]))

    def finalize(
        value: ConditionalRuleOutboxRow, *, claim_token: str, error: str | None
    ) -> bool:
        finalized.append((value.event_id, error))
        assert claim_token == "claim-1"
        return True

    monkeypatch.setattr(store, "_finalize_outbox_claim", finalize)

    def publish(_value: ConditionalRuleOutboxRow) -> None:
        raise RuntimeError("redis unavailable")

    assert store.drain_outbox(publish) == {
        "picked": 1,
        "published": 0,
        "failed": 1,
        "lost": 0,
    }
    assert finalized == [("conditional-event-1", "redis unavailable")]


def test_outbox_finalize_loss_is_reported_without_claiming_success(monkeypatch) -> None:
    store = PostgresRuleWorkerStore("postgresql://test")
    row = _outbox_row()
    published: list[str] = []

    monkeypatch.setattr(store, "_claim_outbox_rows", lambda *, limit: ("claim-1", [row]))
    monkeypatch.setattr(
        store, "_finalize_outbox_claim", lambda _row, **_kwargs: False
    )

    assert store.drain_outbox(
        lambda value: published.append(value.event_id), limit=10
    ) == {
        "picked": 1,
        "published": 0,
        "failed": 0,
        "lost": 1,
    }
    assert published == ["conditional-event-1"]
