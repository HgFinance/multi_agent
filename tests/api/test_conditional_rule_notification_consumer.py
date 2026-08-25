from __future__ import annotations

from datetime import datetime, timezone

from apps.api.conditional_rule_notification_consumer import (
    ConditionalRuleNotificationConsumer,
    RedisConditionalNotificationRunner,
)
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
    discord = Discord()
    snapshots = [
        _directive(accounting_pending=True),
        _directive(accounting_pending=False),
    ]
    consumer = ConditionalRuleNotificationConsumer(
        rule_store=object(),
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
    assert orders.transitions[-1][1]["state"] == "ACCOUNTING_PENDING"
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
