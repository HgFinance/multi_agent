from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestration.conditional_rules.contracts import ExpressionNode
from orchestration.conditional_rules.worker_store import (
    ConditionalRuleOutboxRow,
    PostgresRuleWorkerStore,
    RuleWorkerStoreError,
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


def test_deferred_activation_materializes_explicit_lifetime_from_krx_calendar(
    monkeypatch,
) -> None:
    rule_id = uuid4()
    bundle_id = uuid4()
    authority_id = uuid4()
    instrument_id = uuid4()
    spec = {
        "schema_version": "conditional-trade-rule.v1",
        "authority": {
            "user_id": str(authority_id),
            "fund_id": str(uuid4()),
            "book_id": str(uuid4()),
        },
        "instrument_id": str(instrument_id),
        "symbol": "000660",
        "condition": {
            "type": "COMPARISON",
            "operator": "GTE",
            "left": {"type": "MARKET", "field": "LAST_PRICE"},
            "right": {"type": "LITERAL", "value": "1", "unit": "PRICE"},
        },
        "action": {
            "side": "SELL",
            "sizing": {"type": "FIXED_SHARES", "value": "5"},
        },
        "evaluation": {"clock": "QUOTE"},
        "execution_mode": "PAPER",
        "repeat_policy": "ONCE",
        "expires_at": "2026-09-12T02:00:00+00:00",
        "activation_lifetime_trading_days": 5,
        "raw_instruction_sha256": hashlib.sha256(b"test").hexdigest(),
    }
    from orchestration.conditional_rules.contracts import (
        ConditionalRuleSpec,
        rule_fingerprint,
    )

    spec_sha = rule_fingerprint(ConditionalRuleSpec.model_validate(spec))
    expected_close = datetime(2026, 9, 4, 6, 30, tzinfo=timezone.utc)
    cursor = _Cursor(
        rows=[
            (
                bundle_id,
                rule_id,
                "COMPLETED",
                "PENDING_CONFIRMATION",
                1,
                spec_sha,
                spec,
            )
        ],
        fetchone_rows=[(expected_close,)],
    )
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))

    assert store.activate_ready_bundles(limit=1) == 1

    sql = "\n".join(cursor.queries).lower()
    assert "reference.market_sessions" in sql
    assert "reference.market_calendar_versions" in sql
    assert "offset %s" in sql
    assert "expires_at=%s" in sql
    calendar_params = next(
        params for query, params in zip(cursor.queries, cursor.params)
        if "governed_sessions" in query
    )
    assert calendar_params == (4,)
    activation_params = next(
        params for query, params in zip(cursor.queries, cursor.params)
        if "set state='active'" in query.lower()
    )
    assert activation_params == (spec_sha, expected_close, rule_id)
    event_params = next(
        params for query, params in zip(cursor.queries, cursor.params)
        if "conditional_trade_rule_events" in query
        and params[3] == "BUNDLE_ACTIVATED"
    )
    assert event_params[3:6] == ("BUNDLE_ACTIVATED", "PENDING_CONFIRMATION", "ACTIVE")
    assert event_params[-1].adapted == {
        "bundle_id": str(bundle_id),
        "activation_lifetime_trading_days": 5,
        "active_expires_at": expected_close.isoformat(),
        "order_submitted": False,
    }
    outbox_params = next(
        params for query, params in zip(cursor.queries, cursor.params)
        if "conditional_rule_outbox" in query and params[2] == "BUNDLE_ACTIVATED"
    )
    assert outbox_params[3].adapted == event_params[-1].adapted


def test_deferred_activation_waits_when_official_krx_calendar_is_incomplete(
    monkeypatch,
) -> None:
    cursor = _Cursor()
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))

    with pytest.raises(RuleWorkerStoreError) as raised:
        store._krx_close_after_activation(cursor, trading_days=1)

    assert raised.value.code == "CONDITIONAL_RULE_KRX_CALENDAR_UNAVAILABLE"


def test_full_entry_with_missing_krx_calendar_fails_unarmed_exit_and_emits_report_event(
    monkeypatch,
) -> None:
    rule_id = uuid4()
    bundle_id = uuid4()
    spec = {
        "schema_version": "conditional-trade-rule.v1",
        "authority": {
            "user_id": str(uuid4()),
            "fund_id": str(uuid4()),
            "book_id": str(uuid4()),
        },
        "instrument_id": str(uuid4()),
        "symbol": "000660",
        "condition": {
            "type": "COMPARISON",
            "operator": "GTE",
            "left": {"type": "MARKET", "field": "LAST_PRICE"},
            "right": {"type": "LITERAL", "value": "1", "unit": "PRICE"},
        },
        "action": {
            "side": "SELL",
            "sizing": {"type": "FIXED_SHARES", "value": "5"},
        },
        "evaluation": {"clock": "QUOTE"},
        "execution_mode": "PAPER",
        "repeat_policy": "ONCE",
        "expires_at": "2026-09-12T02:00:00+00:00",
        "activation_lifetime_trading_days": 5,
        "raw_instruction_sha256": hashlib.sha256(b"calendar-missing").hexdigest(),
    }
    from orchestration.conditional_rules.contracts import (
        ConditionalRuleSpec,
        rule_fingerprint,
    )

    spec_sha = rule_fingerprint(ConditionalRuleSpec.model_validate(spec))
    cursor = _Cursor(
        rows=[
            (
                bundle_id,
                rule_id,
                "COMPLETED",
                "PENDING_CONFIRMATION",
                1,
                spec_sha,
                spec,
            )
        ],
        # Calendar lookup has no eligible session; the safe FAILED transition
        # then returns its locked rule id.
        fetchone_rows=[None, (rule_id,)],
    )
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))

    assert store.activate_ready_bundles(limit=1) == 1

    sql = "\n".join(cursor.queries).lower()
    assert "reference.market_sessions" in sql
    assert "set state='failed'" in sql
    assert "set state='active'" not in sql
    assert "conditional_rule_outbox" in sql
    event_params = next(
        params for query, params in zip(cursor.queries, cursor.params)
        if "conditional_trade_rule_events" in query
    )
    assert event_params[3:6] == (
        "BUNDLE_ACTIVATION_BLOCKED",
        "PENDING_CONFIRMATION",
        "FAILED",
    )
    assert event_params[-1].adapted == {
        "code": "ENTRY_EXIT_ACTIVATION_KRX_CALENDAR_UNAVAILABLE",
        "order_submitted": False,
    }
    bundle_params = next(
        params for query, params in zip(cursor.queries, cursor.params)
        if "protective paper exit could not activate" in query.lower()
    )
    assert bundle_params == (
        "ENTRY_EXIT_ACTIVATION_KRX_CALENDAR_UNAVAILABLE",
        bundle_id,
        rule_id,
    )


def test_activation_blocked_context_reloads_lifecycle_and_bundle_facts(monkeypatch) -> None:
    occurred = datetime(2026, 8, 29, 6, 31, tzinfo=timezone.utc)
    rule_id = uuid4()
    cursor = _Cursor(
        fetchone_rows=[
            (
                rule_id,
                "000660",
                uuid4(),
                uuid4(),
                uuid4(),
                "discord:guild:channel:123456789",
                uuid4(),
                "root-1",
                "trading-1",
                "ENTRY_EXIT_ACTIVATION_KRX_CALENDAR_UNAVAILABLE",
                occurred,
                "blk_abcdef0123456789abcdef0123456789abcdef0123456789",
            )
        ]
    )
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))

    context = store.activation_blocked_notification_context(rule_id=str(rule_id))

    assert context.rule_id == str(rule_id)
    assert context.failure_code == "ENTRY_EXIT_ACTIVATION_KRX_CALENDAR_UNAVAILABLE"
    assert context.order_request_id is not None
    sql = "\n".join(cursor.queries).lower()
    assert "bundle_activation_blocked" in sql
    assert "user_paper_order_bundles" in sql


def test_bundle_activated_context_reloads_current_state_and_actual_expiry(monkeypatch) -> None:
    occurred = datetime(2026, 8, 29, 6, 31, tzinfo=timezone.utc)
    expires = datetime(2026, 9, 4, 6, 30, tzinfo=timezone.utc)
    rule_id = uuid4()
    cursor = _Cursor(
        fetchone_rows=[
            (
                rule_id,
                "000660",
                "SELL",
                "ACTIVE",
                expires,
                "5",
                occurred,
                "dep_abcdef0123456789abcdef0123456789abcdef0123456789",
                uuid4(),
                uuid4(),
                uuid4(),
                "discord:guild:channel:123456789",
                uuid4(),
                "root-1",
                "trading-1",
            )
        ]
    )
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))

    context = store.bundle_activated_notification_context(rule_id=str(rule_id))

    assert context.rule_id == str(rule_id)
    assert context.action_side == "SELL"
    assert context.current_state == "ACTIVE"
    assert context.expires_at == expires
    assert context.activation_lifetime_trading_days == 5
    assert context.order_request_id is not None
    sql = "\n".join(cursor.queries).lower()
    assert "bundle_activated" in sql
    assert "user_paper_order_bundles" in sql
    assert "version.spec->'action'->>'side'" in sql


def test_true_trigger_records_a_lifecycle_event_and_outbox_row(monkeypatch) -> None:
    rule = SimpleNamespace(
        rule_id=uuid4(),
        rule_version=1,
        row_version=7,
        spec_sha256="a" * 64,
        spec=SimpleNamespace(
            condition=ExpressionNode.model_validate(
                {
                    "type": "COMPARISON",
                    "operator": "GTE",
                    "left": {"type": "MARKET", "field": "LAST_PRICE"},
                    "right": {"type": "LITERAL", "value": "1", "unit": "PRICE"},
                }
            ),
            evaluation=SimpleNamespace(clock=SimpleNamespace(value="QUOTE")),
        ),
    )
    cursor = _Cursor(fetchone_rows=[("eval-1",), (8,)])
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))

    claim = store.claim_true(
        rule,
        evaluation_key="quote:2026-08-29T01:02:00+00:00",
        context_sha256="b" * 64,
        data_watermark=datetime(2026, 8, 29, 1, 2, tzinfo=timezone.utc),
    )

    assert claim is not None
    sql = "\n".join(cursor.queries).lower()
    assert "conditional_rule_outbox" in sql
    event_params = next(
        params for query, params in zip(cursor.queries, cursor.params)
        if "conditional_trade_rule_events" in query
        and params[3] == "TRIGGER_CLAIMED"
    )
    assert event_params[3:6] == ("TRIGGER_CLAIMED", "ACTIVE", "TRIGGERED")
    assert event_params[-1].adapted == {
        "trigger_id": claim.trigger_id,
        "order_submitted": False,
    }
    outbox_params = next(
        params for query, params in zip(cursor.queries, cursor.params)
        if "conditional_rule_outbox" in query and params[2] == "TRIGGER_CLAIMED"
    )
    assert outbox_params[3].adapted == event_params[-1].adapted


def test_duplicate_true_evaluation_emits_only_one_trigger_and_outbox_row(monkeypatch) -> None:
    rule = SimpleNamespace(
        rule_id=uuid4(),
        rule_version=1,
        row_version=7,
        spec_sha256="a" * 64,
        spec=SimpleNamespace(
            condition=ExpressionNode.model_validate(
                {
                    "type": "COMPARISON",
                    "operator": "GTE",
                    "left": {"type": "MARKET", "field": "LAST_PRICE"},
                    "right": {"type": "LITERAL", "value": "1", "unit": "PRICE"},
                }
            ),
            evaluation=SimpleNamespace(clock=SimpleNamespace(value="QUOTE")),
        ),
    )
    # First insert owns the evaluation/trigger. A replay of the same
    # evaluation key sees the unique conflict and never reaches state changes.
    cursor = _Cursor(fetchone_rows=[("eval-1",), (8,), None])
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))
    kwargs = {
        "evaluation_key": "quote:2026-08-29T01:02:00+00:00",
        "context_sha256": "b" * 64,
        "data_watermark": datetime(2026, 8, 29, 1, 2, tzinfo=timezone.utc),
    }

    first = store.claim_true(rule, **kwargs)
    second = store.claim_true(rule, **kwargs)

    assert first is not None
    assert second is None
    assert sum(
        "insert into execution.conditional_rule_triggers" in query.lower()
        for query in cursor.queries
    ) == 1
    assert sum(
        "conditional_trade_rule_events" in query
        and params[3] == "TRIGGER_CLAIMED"
        for query, params in zip(cursor.queries, cursor.params)
    ) == 1
    assert sum(
        "conditional_rule_outbox" in query and params[2] == "TRIGGER_CLAIMED"
        for query, params in zip(cursor.queries, cursor.params)
    ) == 1


def test_true_trigger_context_reloads_trigger_and_market_data_facts(monkeypatch) -> None:
    occurred = datetime(2026, 8, 29, 1, 2, 3, tzinfo=timezone.utc)
    watermark = datetime(2026, 8, 29, 1, 2, tzinfo=timezone.utc)
    rule_id = uuid4()
    cursor = _Cursor(
        fetchone_rows=[
            (
                rule_id,
                "000660",
                "SELL",
                "TRIGGERED",
                "trg_abcdef0123456789abcdef0123456789abcdef0123456789",
                watermark,
                occurred,
                "cre_abcdef0123456789abcdef0123456789abcdef0123456789",
                uuid4(),
                uuid4(),
                uuid4(),
                "discord:guild:channel:123456789",
                uuid4(),
                "root-1",
                "trading-1",
            )
        ]
    )
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))

    context = store.trigger_claimed_notification_context(rule_id=str(rule_id))

    assert context.rule_id == str(rule_id)
    assert context.action_side == "SELL"
    assert context.current_state == "TRIGGERED"
    assert context.data_watermark == watermark
    assert context.order_request_id is not None
    sql = "\n".join(cursor.queries).lower()
    assert "trigger_claimed" in sql
    assert "conditional_rule_evaluations" in sql
    assert "user_paper_order_bundles" in sql


def test_expiry_records_one_durable_terminal_event_without_submitting_an_order(
    monkeypatch,
) -> None:
    rule_id = uuid4()
    expires_at = datetime(2026, 8, 29, 6, 30, tzinfo=timezone.utc)
    cursor = _Cursor(rows=[(rule_id, 1, "ACTIVE", expires_at)])
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))

    assert store.expire_due() == 1

    sql = "\n".join(cursor.queries).lower()
    assert "for update skip locked" in sql
    assert "conditional_rule_outbox" in sql
    assert "conditional_rule_executions" not in sql
    event_params = next(
        params for query, params in zip(cursor.queries, cursor.params)
        if "conditional_trade_rule_events" in query
    )
    assert event_params[3:6] == (
        "CONDITIONAL_RULE_EXPIRED",
        "ACTIVE",
        "EXPIRED",
    )
    assert event_params[-1].adapted == {
        "expires_at": expires_at.isoformat(),
        "prior_state": "ACTIVE",
        "order_submitted": False,
    }
    bundle_params = next(
        params for query, params in zip(cursor.queries, cursor.params)
        if "conditional_exit_expired" in query.lower()
    )
    assert bundle_params == (rule_id,)


def test_expiry_notification_context_reloads_lifecycle_and_bundle_facts(monkeypatch) -> None:
    occurred = datetime(2026, 8, 29, 6, 31, tzinfo=timezone.utc)
    expires = datetime(2026, 8, 29, 6, 30, tzinfo=timezone.utc)
    rule_id = uuid4()
    cursor = _Cursor(
        fetchone_rows=[
            (
                rule_id,
                "000660",
                "SELL",
                "ACTIVE",
                expires,
                occurred,
                "exp_abcdef0123456789abcdef0123456789abcdef0123456789",
                uuid4(),
                uuid4(),
                uuid4(),
                "discord:guild:channel:123456789",
                uuid4(),
                "root-1",
                "trading-1",
                True,
            )
        ]
    )
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))

    context = store.expired_rule_notification_context(rule_id=str(rule_id))

    assert context.rule_id == str(rule_id)
    assert context.action_side == "SELL"
    assert context.prior_state == "ACTIVE"
    assert context.is_compound_entry_exit is True
    sql = "\n".join(cursor.queries).lower()
    assert "conditional_rule_expired" in sql
    assert "user_paper_order_bundles" in sql
    assert "version.spec->'action'->>'side'" in sql


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


def test_trailing_stop_persists_a_locked_high_watermark_update(monkeypatch) -> None:
    observed = datetime.now(timezone.utc)
    rule = SimpleNamespace(
        rule_id=uuid4(),
        rule_version=1,
        spec=SimpleNamespace(
            condition=ExpressionNode.model_validate(
                {
                    "type": "TRAILING_STOP",
                    "parameters": {"DRAWDOWN": "0.01", "ACTIVATION_RETURN": "0.02"},
                }
            )
        ),
    )
    cursor = _Cursor(
        fetchone_rows=[(Decimal("102"), observed, observed, None)],
    )
    # The initial INSERT collides with the already-persisted state, so the
    # store must lock it, derive the result, and update it in one transaction.
    cursor.rowcount = 0
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))

    result = store.observe_trailing_stop(
        rule,
        last_price=Decimal("100.98"),
        average_entry_price=Decimal("100"),
        observed_at=observed + timedelta(seconds=1),
    )

    assert result.condition_result is True
    assert result.state.high_price == Decimal("102")
    sql = "\n".join(cursor.queries).lower()
    assert "insert into execution.conditional_rule_trailing_states" in sql
    assert "on conflict (rule_id,rule_version) do nothing" in sql
    assert "for update" in sql
    assert "baseline_average_entry_price" in sql
    assert "update execution.conditional_rule_trailing_states" in sql


def test_started_entry_trailing_quantity_drift_cancels_rule_and_fails_bundle(monkeypatch) -> None:
    rule = SimpleNamespace(
        rule_id=uuid4(),
        rule_version=1,
        row_version=7,
        spec=SimpleNamespace(evaluation=SimpleNamespace(clock=SimpleNamespace(value="QUOTE"))),
    )
    cursor = _Cursor(fetchone_rows=[("ACTIVE",), (1,), (rule.rule_id,)])
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))

    assert store.cancel_entry_trailing_on_position_mismatch(
        rule,
        expected_position_quantity=Decimal("2"),
        actual_position_quantity=Decimal("3"),
        evaluation_key="quote:test:1",
        context_sha256="a" * 64,
        data_watermark=datetime.now(timezone.utc),
    ) is True

    sql = "\n".join(cursor.queries).lower()
    assert "conditional_rule_trailing_states" in sql
    assert "for update" in sql
    assert "state='cancelled'" in sql
    assert "conditional_rule_outbox" in sql
    assert "user_paper_order_bundles" in sql
    assert any(
        params and (
            "ENTRY_POSITION_QUANTITY_MISMATCH" in str(params)
            or "ENTRY_POSITION_MISMATCH" in str(params)
        )
        for params in cursor.params
    )


def test_entry_position_mismatch_notification_reloads_durable_bundle_context(monkeypatch) -> None:
    occurred = datetime.now(timezone.utc)
    rule_id = uuid4()
    cursor = _Cursor(
        fetchone_rows=[
            (
                rule_id,
                "000660",
                uuid4(),
                uuid4(),
                uuid4(),
                "discord:guild:channel:123456789",
                uuid4(),
                "root-1",
                "trading-1",
                "5",
                "3",
                occurred,
                "trail_abcdef0123456789abcdef0123456789abcdef0123456789",
            )
        ]
    )
    store = PostgresRuleWorkerStore("postgresql://test")
    monkeypatch.setattr(store, "_connect", lambda: _Connection(cursor))

    context = store.entry_position_mismatch_notification_context(rule_id=str(rule_id))

    assert context.rule_id == str(rule_id)
    assert context.expected_position_quantity == "5"
    assert context.actual_position_quantity == "3"
    assert context.order_request_id is not None
    sql = "\n".join(cursor.queries).lower()
    assert "entry_position_mismatch" in sql
    assert "conditional_trade_rule_events" in sql
    assert "user_paper_order_bundles" in sql
    assert "bundle.immediate_order_request_id" in sql


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
