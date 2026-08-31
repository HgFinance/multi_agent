from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from apps.api.conditional_rule_notification_consumer import (
    ConditionalRuleNotificationConsumer,
    RedisConditionalNotificationRunner,
)
from apps.api.conditional_rule_status import ConditionalStatusError
from apps.api.user_order_workflow import UserOrderRequestRecord
from apps.api.user_orders import UserDirectiveResponse


class OrderStore:
    def __init__(self, record):
        self.record = record
        self.transitions = []

    def get(self, order_request_id):
        assert order_request_id == self.record.order_request_id
        return self.record

    def mark_outcome(self, order_request_id, **kwargs):
        self.transitions.append((order_request_id, kwargs))
        return self.record


class Kanban:
    def __init__(self) -> None:
        self.environment = {"HERMES_HOME": "/tmp/test-hermes"}

    def show(self, _task_id):
        raise RuntimeError("archived")

    def list_tasks(self):
        return ()


class Discord:
    def __init__(self):
        self.calls = []

    def deliver_to_existing_thread(self, **kwargs):
        self.calls.append(kwargs)
        return "sent"


class Projection:
    def project(self, **_kwargs):
        return {"status": "updated"}


class RuleStore:
    def __init__(
        self,
        record,
        *,
        linked: bool = True,
        activation_state: str = "ACTIVE",
        trigger_state: str = "TRIGGERED",
    ) -> None:
        self.record = record
        self.linked = linked
        self.activation_state = activation_state
        self.trigger_state = trigger_state
        self.calls = []
        self.lifecycle_calls = []
        self.expiry_calls = []
        self.activation_blocked_calls = []
        self.activated_calls = []
        self.trigger_calls = []

    def notification_context(self, *, rule_id, directive_id):
        self.calls.append((rule_id, directive_id))
        return SimpleNamespace(
            rule_id=rule_id,
            rule_execution_id="99999999-9999-4999-8999-999999999999",
            directive_id=directive_id,
            user_id=self.record.user_id,
            fund_id=self.record.fund_id,
            book_id=self.record.book_id,
            client_request_id=self.record.client_request_id,
            order_request_id=(self.record.order_request_id if self.linked else None),
            ceo_root_task_id=self.record.ceo_root_task_id,
            trading_task_id=self.record.trading_task_id,
        )

    def entry_position_mismatch_notification_context(self, *, rule_id):
        self.lifecycle_calls.append(rule_id)
        return SimpleNamespace(
            rule_id=rule_id,
            symbol="000660",
            user_id=self.record.user_id,
            fund_id=self.record.fund_id,
            book_id=self.record.book_id,
            client_request_id=self.record.client_request_id,
            order_request_id=(self.record.order_request_id if self.linked else None),
            ceo_root_task_id=self.record.ceo_root_task_id,
            trading_task_id=self.record.trading_task_id,
            expected_position_quantity="5",
            actual_position_quantity="3",
            occurred_at=datetime(2026, 8, 29, 1, 2, 3, tzinfo=timezone.utc),
            lifecycle_event_id="trail_abcdef0123456789abcdef0123456789abcdef0123456789",
        )

    def expired_rule_notification_context(self, *, rule_id):
        self.expiry_calls.append(rule_id)
        return SimpleNamespace(
            rule_id=rule_id,
            symbol="000660",
            action_side="SELL",
            prior_state="ACTIVE",
            expires_at=datetime(2026, 8, 29, 6, 30, tzinfo=timezone.utc),
            occurred_at=datetime(2026, 8, 29, 6, 31, tzinfo=timezone.utc),
            lifecycle_event_id="exp_abcdef0123456789abcdef0123456789abcdef0123456789",
            user_id=self.record.user_id,
            fund_id=self.record.fund_id,
            book_id=self.record.book_id,
            client_request_id=self.record.client_request_id,
            order_request_id=(self.record.order_request_id if self.linked else None),
            ceo_root_task_id=self.record.ceo_root_task_id,
            trading_task_id=self.record.trading_task_id,
            is_compound_entry_exit=True,
        )

    def activation_blocked_notification_context(self, *, rule_id):
        self.activation_blocked_calls.append(rule_id)
        return SimpleNamespace(
            rule_id=rule_id,
            symbol="000660",
            user_id=self.record.user_id,
            fund_id=self.record.fund_id,
            book_id=self.record.book_id,
            client_request_id=self.record.client_request_id,
            order_request_id=(self.record.order_request_id if self.linked else None),
            ceo_root_task_id=self.record.ceo_root_task_id,
            trading_task_id=self.record.trading_task_id,
            failure_code="ENTRY_EXIT_ACTIVATION_KRX_CALENDAR_UNAVAILABLE",
            occurred_at=datetime(2026, 8, 29, 1, 2, 3, tzinfo=timezone.utc),
            lifecycle_event_id="blk_abcdef0123456789abcdef0123456789abcdef0123456789",
        )

    def bundle_activated_notification_context(self, *, rule_id):
        self.activated_calls.append(rule_id)
        return SimpleNamespace(
            rule_id=rule_id,
            symbol="000660",
            action_side="SELL",
            current_state=self.activation_state,
            expires_at=datetime(2026, 9, 4, 6, 30, tzinfo=timezone.utc),
            activation_lifetime_trading_days=5,
            occurred_at=datetime(2026, 8, 29, 1, 2, 3, tzinfo=timezone.utc),
            lifecycle_event_id="dep_abcdef0123456789abcdef0123456789abcdef0123456789",
            user_id=self.record.user_id,
            fund_id=self.record.fund_id,
            book_id=self.record.book_id,
            client_request_id=self.record.client_request_id,
            order_request_id=(self.record.order_request_id if self.linked else None),
            ceo_root_task_id=self.record.ceo_root_task_id,
            trading_task_id=self.record.trading_task_id,
        )

    def trigger_claimed_notification_context(self, *, rule_id):
        self.trigger_calls.append(rule_id)
        return SimpleNamespace(
            rule_id=rule_id,
            symbol="000660",
            action_side="SELL",
            current_state=self.trigger_state,
            trigger_id="trg_abcdef0123456789abcdef0123456789abcdef0123456789",
            data_watermark=datetime(2026, 8, 29, 1, 2, tzinfo=timezone.utc),
            occurred_at=datetime(2026, 8, 29, 1, 2, 3, tzinfo=timezone.utc),
            lifecycle_event_id="cre_abcdef0123456789abcdef0123456789abcdef0123456789",
            user_id=self.record.user_id,
            fund_id=self.record.fund_id,
            book_id=self.record.book_id,
            client_request_id=self.record.client_request_id,
            order_request_id=(self.record.order_request_id if self.linked else None),
            ceo_root_task_id=self.record.ceo_root_task_id,
            trading_task_id=self.record.trading_task_id,
        )


def _directive(*, accounting_pending: bool) -> UserDirectiveResponse:
    now = datetime.now(timezone.utc)
    return UserDirectiveResponse.model_validate(
        {
            "directive_id": "55555555-5555-4555-8555-555555555555",
            "state": "COMPLETED",
            "action": "PLACE_ORDER",
            "priority": 1000,
            "fund_id": "22222222-2222-4222-8222-222222222222",
            "book_id": "33333333-3333-4333-8333-333333333333",
            "idempotency_key": "conditional:test",
            "instruction_ref": "conditional:test",
            "payload_sha256": "0" * 64,
            "created_at": now,
            "updated_at": now,
            "completed_at": now,
            "error_code": (
                "TRADING_FILL_ACCOUNTING_PENDING" if accounting_pending else None
            ),
            "legs": [
                {
                    "leg_id": "66666666-6666-4666-8666-666666666666",
                    "leg_index": 0,
                    "symbol": "005930",
                    "side": "SELL",
                    "order_type": "MARKET",
                    "requested_quantity": "1",
                    "filled_quantity": "1",
                    "average_fill_price": "248250",
                    "target_filled_quantity": "0",
                    "state": "FILLED",
                    "reduce_only": True,
                    "broker_order_id": "ls-paper:12695",
                }
            ],
        }
    )


def test_consumer_waits_for_accounting_then_reports_terminal_state_once() -> None:
    record = UserOrderRequestRecord(
        order_request_id="77777777-7777-4777-8777-777777777777",
        user_id="11111111-1111-4111-8111-111111111111",
        fund_id="22222222-2222-4222-8222-222222222222",
        book_id="33333333-3333-4333-8333-333333333333",
        client_request_id="discord:guild:channel:123456789",
        raw_instruction="삼성전자 조건 매도",
        normalized_instruction="삼성전자 조건 매도",
        raw_instruction_sha256="0" * 64,
        ceo_root_task_id="t_root1",
        trading_task_id="t_trade1",
    )
    orders = OrderStore(record)
    rules = RuleStore(record)
    discord = Discord()
    snapshots = [
        _directive(accounting_pending=True),
        _directive(accounting_pending=False),
    ]
    consumer = ConditionalRuleNotificationConsumer(
        rule_store=rules,
        order_store=orders,
        status_reader=lambda **_kwargs: snapshots.pop(0),
        kanban_client=Kanban(),
        discord_delivery=discord,
        discord_store=object(),
        ceo_projection=Projection(),
        department_projection=Projection(),
    )
    event = {
        "event_id": "cro_test",
        "aggregate_id": "88888888-8888-4888-8888-888888888888",
        "event_type": "DIRECTIVE_SUBMITTED",
        "payload": {
            "rule_execution_id": "99999999-9999-4999-8999-999999999999",
            "directive_id": "55555555-5555-4555-8555-555555555555",
            "user_id": record.user_id,
            "fund_id": record.fund_id,
            "book_id": record.book_id,
            "client_request_id": record.client_request_id,
            "order_request_id": record.order_request_id,
            "ceo_root_task_id": record.ceo_root_task_id,
            "trading_task_id": record.trading_task_id,
        },
    }

    assert consumer.handle_event(event) is False
    assert rules.calls[-1] == (
        "88888888-8888-4888-8888-888888888888",
        "55555555-5555-4555-8555-555555555555",
    )
    assert orders.transitions[-1][1]["state"] == "ACCOUNTING_PENDING"
    assert orders.transitions[-1][1]["event_id"] == "cro_test"
    assert discord.calls == []

    assert consumer.handle_event(event) is True
    assert orders.transitions[-1][1]["state"] == "COMPLETED"
    assert len(discord.calls) == 1
    assert "평균 체결가 : 248,250원" in discord.calls[0]["content"]
    assert (
        "directive_id=55555555-5555-4555-8555-555555555555"
        in discord.calls[0]["content"]
    )
    assert "QA 검증 : PASS" in discord.calls[0]["content"]
    assert "COMPLETED" in discord.calls[0]["response_key_suffix"]


def test_consumer_uses_authoritative_context_and_acks_unlinked_rule() -> None:
    record = UserOrderRequestRecord(
        order_request_id="77777777-7777-4777-8777-777777777777",
        user_id="11111111-1111-4111-8111-111111111111",
        fund_id="22222222-2222-4222-8222-222222222222",
        book_id="33333333-3333-4333-8333-333333333333",
        client_request_id="discord:guild:channel:123456789",
        raw_instruction="복수 조건 주문",
        normalized_instruction="복수 조건 주문",
        raw_instruction_sha256="0" * 64,
        ceo_root_task_id="t_root1",
        trading_task_id="t_trade1",
    )
    rules = RuleStore(record, linked=False)
    status_calls = []
    consumer = ConditionalRuleNotificationConsumer(
        rule_store=rules,
        order_store=OrderStore(record),
        status_reader=lambda **kwargs: status_calls.append(kwargs),
        kanban_client=Kanban(),
        discord_delivery=Discord(),
        discord_store=object(),
        ceo_projection=Projection(),
        department_projection=Projection(),
    )

    assert consumer.handle_event(
        {
            "event_id": "cro_unlinked",
            "aggregate_id": "88888888-8888-4888-8888-888888888888",
            "event_type": "DIRECTIVE_SUBMITTED",
            "payload": {
                "directive_id": "55555555-5555-4555-8555-555555555555",
                # Present-but-untrusted values must not bypass DB resolution.
                "order_request_id": "not-a-uuid",
                "client_request_id": "spoofed-request",
                "user_id": "spoofed-user",
                "fund_id": "spoofed-fund",
                "book_id": "spoofed-book",
                "ceo_root_task_id": "spoofed-root",
                "trading_task_id": "spoofed-task",
            },
        }
    ) is True
    assert len(rules.calls) == 1
    assert status_calls == []


def test_entry_position_mismatch_notifies_from_durable_context_without_order_submit() -> None:
    record = UserOrderRequestRecord(
        order_request_id="77777777-7777-4777-8777-777777777777",
        user_id="11111111-1111-4111-8111-111111111111",
        fund_id="22222222-2222-4222-8222-222222222222",
        book_id="33333333-3333-4333-8333-333333333333",
        client_request_id="discord:guild:channel:123456789",
        raw_instruction="하이닉스 5주 매수 뒤 트레일링 매도",
        normalized_instruction="하이닉스 5주 매수 뒤 트레일링 매도",
        raw_instruction_sha256="0" * 64,
        ceo_root_task_id="t_root1",
        trading_task_id="t_trade1",
    )
    orders = OrderStore(record)
    rules = RuleStore(record)
    discord = Discord()
    consumer = ConditionalRuleNotificationConsumer(
        rule_store=rules,
        order_store=orders,
        status_reader=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("safety stop must not read or submit a directive")
        ),
        kanban_client=Kanban(),
        discord_delivery=discord,
        discord_store=object(),
        ceo_projection=Projection(),
        department_projection=Projection(),
        mode="delivery",
    )

    assert consumer.handle_event(
        {
            "event_id": "cro_mismatch",
            "aggregate_id": "88888888-8888-4888-8888-888888888888",
            "event_type": "ENTRY_POSITION_MISMATCH",
            "payload": {
                "expected_position_quantity": "999",  # Redis is untrusted.
                "actual_position_quantity": "1",
            },
        }
    ) is True
    assert rules.lifecycle_calls == ["88888888-8888-4888-8888-888888888888"]
    assert orders.transitions == []
    assert len(discord.calls) == 1
    content = discord.calls[0]["content"]
    assert "최초 진입 수량 5주" in content
    assert "현재 보유 수량 3주" in content
    assert "999" not in content
    assert "추가 매도 주문 : 없음" in content
    assert "QA 검증 : PASS" in content
    assert "entry-position-mismatch-v1:cro_mismatch" in discord.calls[0]["response_key_suffix"]


def test_expired_compound_exit_notifies_without_inferring_a_fill_or_submitting_order() -> None:
    record = UserOrderRequestRecord(
        order_request_id="77777777-7777-4777-8777-777777777777",
        user_id="11111111-1111-4111-8111-111111111111",
        fund_id="22222222-2222-4222-8222-222222222222",
        book_id="33333333-3333-4333-8333-333333333333",
        client_request_id="discord:guild:channel:123456789",
        raw_instruction="하이닉스 5주 매수 뒤 5거래일 트레일링 매도",
        normalized_instruction="하이닉스 5주 매수 뒤 5거래일 트레일링 매도",
        raw_instruction_sha256="0" * 64,
        ceo_root_task_id="t_root1",
        trading_task_id="t_trade1",
    )
    orders = OrderStore(record)
    rules = RuleStore(record)
    discord = Discord()
    consumer = ConditionalRuleNotificationConsumer(
        rule_store=rules,
        order_store=orders,
        status_reader=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("expiry report must not read or submit a directive")
        ),
        kanban_client=Kanban(),
        discord_delivery=discord,
        discord_store=object(),
        ceo_projection=Projection(),
        department_projection=Projection(),
        mode="delivery",
    )

    assert consumer.handle_event(
        {
            "event_id": "cro_expired",
            "aggregate_id": "88888888-8888-4888-8888-888888888888",
            "event_type": "CONDITIONAL_RULE_EXPIRED",
            "payload": {"order_submitted": True},  # Redis payload is untrusted.
        }
    ) is True
    assert rules.expiry_calls == ["88888888-8888-4888-8888-888888888888"]
    assert orders.transitions == []
    assert len(discord.calls) == 1
    content = discord.calls[0]["content"]
    assert "조건 규칙 상태 : EXPIRED" in content
    assert "추가 주문 생성 : 없음" in content
    assert "보유분이 남아 있을 수 있으나 자동 매도는 실행하지 않습니다." in content
    assert "order_submitted" not in content
    assert "QA 검증 : PASS" in content
    assert "conditional-rule-expired-v1:cro_expired" in discord.calls[0]["response_key_suffix"]


def test_activation_blocked_exit_notifies_without_submitting_a_compensating_order() -> None:
    record = UserOrderRequestRecord(
        order_request_id="77777777-7777-4777-8777-777777777777",
        user_id="11111111-1111-4111-8111-111111111111",
        fund_id="22222222-2222-4222-8222-222222222222",
        book_id="33333333-3333-4333-8333-333333333333",
        client_request_id="discord:guild:channel:123456789",
        raw_instruction="하이닉스 5주 매수 뒤 5거래일 트레일링 매도",
        normalized_instruction="하이닉스 5주 매수 뒤 5거래일 트레일링 매도",
        raw_instruction_sha256="0" * 64,
        ceo_root_task_id="t_root1",
        trading_task_id="t_trade1",
    )
    orders = OrderStore(record)
    rules = RuleStore(record)
    discord = Discord()
    consumer = ConditionalRuleNotificationConsumer(
        rule_store=rules,
        order_store=orders,
        status_reader=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("activation-blocked report must not read or submit a directive")
        ),
        kanban_client=Kanban(),
        discord_delivery=discord,
        discord_store=object(),
        ceo_projection=Projection(),
        department_projection=Projection(),
        mode="delivery",
    )

    assert consumer.handle_event(
        {
            "event_id": "cro_activation_blocked",
            "aggregate_id": "88888888-8888-4888-8888-888888888888",
            "event_type": "BUNDLE_ACTIVATION_BLOCKED",
            "payload": {"code": "spoofed"},
        }
    ) is True
    assert rules.activation_blocked_calls == ["88888888-8888-4888-8888-888888888888"]
    assert orders.transitions == []
    assert len(discord.calls) == 1
    content = discord.calls[0]["content"]
    assert "조건 규칙 상태 : FAILED" in content
    assert "보호 청산 규칙 : 활성화하지 않음" in content
    assert "추가 주문 생성 : 없음" in content
    assert "spoofed" not in content
    assert "QA 검증 : PASS" in content
    assert "bundle-activation-blocked-v1:cro_activation_blocked" in discord.calls[0]["response_key_suffix"]


def test_activated_compound_exit_confirms_actual_expiry_without_submitting_an_order() -> None:
    record = UserOrderRequestRecord(
        order_request_id="77777777-7777-4777-8777-777777777777",
        user_id="11111111-1111-4111-8111-111111111111",
        fund_id="22222222-2222-4222-8222-222222222222",
        book_id="33333333-3333-4333-8333-333333333333",
        client_request_id="discord:guild:channel:123456789",
        raw_instruction="하이닉스 5주 매수 뒤 5거래일 트레일링 매도",
        normalized_instruction="하이닉스 5주 매수 뒤 5거래일 트레일링 매도",
        raw_instruction_sha256="0" * 64,
        ceo_root_task_id="t_root1",
        trading_task_id="t_trade1",
    )
    orders = OrderStore(record)
    rules = RuleStore(record)
    discord = Discord()
    consumer = ConditionalRuleNotificationConsumer(
        rule_store=rules,
        order_store=orders,
        status_reader=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("activation report must not read or submit a directive")
        ),
        kanban_client=Kanban(),
        discord_delivery=discord,
        discord_store=object(),
        ceo_projection=Projection(),
        department_projection=Projection(),
        mode="delivery",
    )

    assert consumer.handle_event(
        {
            "event_id": "cro_activated",
            "aggregate_id": "88888888-8888-4888-8888-888888888888",
            "event_type": "BUNDLE_ACTIVATED",
            "payload": {
                "active_expires_at": "spoofed",
                "activation_lifetime_trading_days": 99,
                "order_submitted": True,
            },
        }
    ) is True
    assert rules.activated_calls == ["88888888-8888-4888-8888-888888888888"]
    assert orders.transitions == []
    assert len(discord.calls) == 1
    content = discord.calls[0]["content"]
    assert "조건 규칙 상태 : ACTIVE" in content
    assert "보호 청산 : SELL 조건 감시 중" in content
    assert "추적 기간 : 전량 체결 뒤 5거래일" in content
    assert "보호 만료 시각 : 2026-09-04T06:30:00+00:00" in content
    assert "추가 주문 생성 : 없음 (조건 충족 전)" in content
    assert "spoofed" not in content
    assert "99" not in content
    assert "QA 검증 : PASS" in content
    assert "bundle-activated-v1:cro_activated" in discord.calls[0]["response_key_suffix"]


def test_stale_activated_event_is_suppressed_without_order_or_discord_side_effect() -> None:
    record = UserOrderRequestRecord(
        order_request_id="77777777-7777-4777-8777-777777777777",
        user_id="11111111-1111-4111-8111-111111111111",
        fund_id="22222222-2222-4222-8222-222222222222",
        book_id="33333333-3333-4333-8333-333333333333",
        client_request_id="discord:guild:channel:123456789",
        raw_instruction="하이닉스 5주 매수 뒤 5거래일 트레일링 매도",
        normalized_instruction="하이닉스 5주 매수 뒤 5거래일 트레일링 매도",
        raw_instruction_sha256="0" * 64,
        ceo_root_task_id="t_root1",
        trading_task_id="t_trade1",
    )
    orders = OrderStore(record)
    rules = RuleStore(record, activation_state="EXPIRED")
    discord = Discord()
    consumer = ConditionalRuleNotificationConsumer(
        rule_store=rules,
        order_store=orders,
        status_reader=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale activation report must not read a directive")
        ),
        kanban_client=Kanban(),
        discord_delivery=discord,
        discord_store=object(),
        ceo_projection=Projection(),
        department_projection=Projection(),
        mode="delivery",
    )

    assert consumer.handle_event(
        {
            "event_id": "cro_stale_activated",
            "aggregate_id": "88888888-8888-4888-8888-888888888888",
            "event_type": "BUNDLE_ACTIVATED",
        }
    ) is True
    assert rules.activated_calls == ["88888888-8888-4888-8888-888888888888"]
    assert orders.transitions == []
    assert discord.calls == []


def test_true_condition_reports_pending_paper_submission_without_submitting_an_order() -> None:
    record = UserOrderRequestRecord(
        order_request_id="77777777-7777-4777-8777-777777777777",
        user_id="11111111-1111-4111-8111-111111111111",
        fund_id="22222222-2222-4222-8222-222222222222",
        book_id="33333333-3333-4333-8333-333333333333",
        client_request_id="discord:guild:channel:123456789",
        raw_instruction="하이닉스 5주 매수 뒤 2% 상승하면 매도",
        normalized_instruction="하이닉스 5주 매수 뒤 2% 상승하면 매도",
        raw_instruction_sha256="0" * 64,
        ceo_root_task_id="t_root1",
        trading_task_id="t_trade1",
    )
    orders = OrderStore(record)
    rules = RuleStore(record, trigger_state="EXECUTION_PENDING")
    discord = Discord()
    consumer = ConditionalRuleNotificationConsumer(
        rule_store=rules,
        order_store=orders,
        status_reader=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("trigger report must not read or submit a directive")
        ),
        kanban_client=Kanban(),
        discord_delivery=discord,
        discord_store=object(),
        ceo_projection=Projection(),
        department_projection=Projection(),
        mode="delivery",
    )

    assert consumer.handle_event(
        {
            "event_id": "cro_triggered",
            "aggregate_id": "88888888-8888-4888-8888-888888888888",
            "event_type": "TRIGGER_CLAIMED",
            "payload": {"trigger_id": "spoofed", "order_submitted": True},
        }
    ) is True
    assert rules.trigger_calls == ["88888888-8888-4888-8888-888888888888"]
    assert orders.transitions == []
    assert len(discord.calls) == 1
    content = discord.calls[0]["content"]
    assert "조건 규칙 상태 : EXECUTION_PENDING" in content
    assert "감지된 실행 방향 : SELL" in content
    assert "조건 데이터 시각 : 2026-08-29T01:02:00+00:00" in content
    assert "후속 처리 : PAPER 주문 제출 준비 중" in content
    assert "주문 제출 : 아직 확인되지 않음" in content
    assert "spoofed" not in content
    assert "QA 검증 : PASS" in content
    assert "conditional-trigger-claimed-v1:cro_triggered" in discord.calls[0]["response_key_suffix"]


def test_stale_true_condition_event_is_suppressed_when_submission_already_completed() -> None:
    record = UserOrderRequestRecord(
        order_request_id="77777777-7777-4777-8777-777777777777",
        user_id="11111111-1111-4111-8111-111111111111",
        fund_id="22222222-2222-4222-8222-222222222222",
        book_id="33333333-3333-4333-8333-333333333333",
        client_request_id="discord:guild:channel:123456789",
        raw_instruction="하이닉스 5주 매수 뒤 2% 상승하면 매도",
        normalized_instruction="하이닉스 5주 매수 뒤 2% 상승하면 매도",
        raw_instruction_sha256="0" * 64,
        ceo_root_task_id="t_root1",
        trading_task_id="t_trade1",
    )
    orders = OrderStore(record)
    rules = RuleStore(record, trigger_state="COMPLETED")
    discord = Discord()
    consumer = ConditionalRuleNotificationConsumer(
        rule_store=rules,
        order_store=orders,
        status_reader=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale trigger report must not read a directive")
        ),
        kanban_client=Kanban(),
        discord_delivery=discord,
        discord_store=object(),
        ceo_projection=Projection(),
        department_projection=Projection(),
        mode="delivery",
    )

    assert consumer.handle_event(
        {
            "event_id": "cro_stale_triggered",
            "aggregate_id": "88888888-8888-4888-8888-888888888888",
            "event_type": "TRIGGER_CLAIMED",
        }
    ) is True
    assert rules.trigger_calls == ["88888888-8888-4888-8888-888888888888"]
    assert orders.transitions == []
    assert discord.calls == []


def test_delivery_lane_does_not_wait_for_kanban_notion_or_langsmith() -> None:
    record = UserOrderRequestRecord(
        order_request_id="77777777-7777-4777-8777-777777777777",
        user_id="11111111-1111-4111-8111-111111111111",
        fund_id="22222222-2222-4222-8222-222222222222",
        book_id="33333333-3333-4333-8333-333333333333",
        client_request_id="discord:guild:channel:123456789",
        raw_instruction="삼성전자 조건 매도",
        normalized_instruction="삼성전자 조건 매도",
        raw_instruction_sha256="0" * 64,
        ceo_root_task_id="t_root1",
        trading_task_id="t_trade1",
    )

    class ForbiddenDependency:
        def __getattr__(self, name):
            raise AssertionError(f"immediate lane touched slow dependency: {name}")

    orders = OrderStore(record)
    discord = Discord()
    consumer = ConditionalRuleNotificationConsumer(
        rule_store=RuleStore(record),
        order_store=orders,
        status_reader=lambda **_kwargs: _directive(accounting_pending=False),
        kanban_client=ForbiddenDependency(),
        discord_delivery=discord,
        discord_store=object(),
        ceo_projection=ForbiddenDependency(),
        department_projection=ForbiddenDependency(),
        mode="delivery",
    )

    assert consumer.handle_event(
        {
            "event_id": "cro_fast",
            "aggregate_id": "88888888-8888-4888-8888-888888888888",
            "event_type": "DIRECTIVE_SUBMITTED",
            "payload": {
                "directive_id": "55555555-5555-4555-8555-555555555555",
            },
        }
    ) is True
    assert len(discord.calls) == 1
    assert len(orders.transitions) == 1


def test_projection_lane_does_not_redeliver_or_duplicate_order_audit() -> None:
    record = UserOrderRequestRecord(
        order_request_id="77777777-7777-4777-8777-777777777777",
        user_id="11111111-1111-4111-8111-111111111111",
        fund_id="22222222-2222-4222-8222-222222222222",
        book_id="33333333-3333-4333-8333-333333333333",
        client_request_id="discord:guild:channel:123456789",
        raw_instruction="삼성전자 조건 매도",
        normalized_instruction="삼성전자 조건 매도",
        raw_instruction_sha256="0" * 64,
        ceo_root_task_id="t_root1",
        trading_task_id="t_trade1",
    )
    orders = OrderStore(record)
    discord = Discord()
    consumer = ConditionalRuleNotificationConsumer(
        rule_store=RuleStore(record),
        order_store=orders,
        status_reader=lambda **_kwargs: _directive(accounting_pending=False),
        kanban_client=Kanban(),
        discord_delivery=discord,
        discord_store=object(),
        ceo_projection=Projection(),
        department_projection=Projection(),
        mode="projection",
    )

    assert consumer.handle_event(
        {
            "event_id": "cro_projection",
            "aggregate_id": "88888888-8888-4888-8888-888888888888",
            "event_type": "DIRECTIVE_SUBMITTED",
            "payload": {
                "directive_id": "55555555-5555-4555-8555-555555555555",
            },
        }
    ) is True
    assert discord.calls == []
    assert orders.transitions == []


def test_runner_does_not_let_one_poison_event_block_the_batch() -> None:
    class RedisBatch:
        def __init__(self) -> None:
            self.acked = []

        def xgroup_create(self, *_args, **_kwargs):
            return True

        def xautoclaim(self, *_args, **_kwargs):
            return (
                "0-0",
                [
                    ("1-0", {"event_id": "bad"}),
                    ("2-0", {"event_id": "good"}),
                ],
                [],
            )

        def xreadgroup(self, *_args, **_kwargs):
            return []

        def xack(self, _stream, _group, message_id):
            self.acked.append(message_id)

    class BatchConsumer:
        def handle_event(self, event):
            if event["event_id"] == "bad":
                raise RuntimeError("poison event")
            return True

    client = RedisBatch()
    runner = RedisConditionalNotificationRunner(client, BatchConsumer())

    assert runner.poll_once(block_ms=1) == 1
    assert client.acked == ["2-0"]


def test_runner_acknowledges_terminal_authority_contract_error() -> None:
    class RedisBatch:
        def __init__(self) -> None:
            self.acked = []

        def xgroup_create(self, *_args, **_kwargs):
            return True

        def xautoclaim(self, *_args, **_kwargs):
            return ("0-0", [("poison-1", {"event_id": "bad-authority"})], [])

        def xreadgroup(self, *_args, **_kwargs):
            return []

        def xack(self, _stream, _group, message_id):
            self.acked.append(message_id)

    class AuthorityConsumer:
        def handle_event(self, _event):
            raise ConditionalStatusError("conditional directive must contain exactly one order leg")

    client = RedisBatch()
    runner = RedisConditionalNotificationRunner(client, AuthorityConsumer())

    assert runner.poll_once(block_ms=1) == 1
    assert client.acked == ["poison-1"]
