from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from apps.api.conditional_rule_outbox_relay import RedisConditionalRulePublisher
from orchestration.conditional_rules.worker_store import ConditionalRuleOutboxRow


class _FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.kwargs: list[dict[str, object]] = []

    def xadd(self, stream: str, fields: dict[str, str], **kwargs: object) -> str:
        self.calls.append((stream, fields))
        self.kwargs.append(kwargs)
        return "1-0"


def test_conditional_rule_outbox_publisher_emits_one_canonical_stream_event() -> None:
    fake = _FakeRedis()
    with patch(
        "apps.api.conditional_rule_outbox_relay.redis.Redis.from_url",
        return_value=fake,
    ):
        publisher = RedisConditionalRulePublisher(
            "redis://unused",
            stream="hf:test-conditional-events:v1",
        )
        publisher.publish(
            ConditionalRuleOutboxRow(
                event_id="conditional-event-1",
                aggregate_id="rule-1",
                event_type="DIRECTIVE_SUBMITTED",
                payload={"directive_id": "directive-1"},
                created_at=datetime.now(timezone.utc),
                attempts=0,
            )
        )

    assert len(fake.calls) == 1
    stream, fields = fake.calls[0]
    assert stream == "hf:test-conditional-events:v1"
    assert fields["event_id"] == "conditional-event-1"
    assert fields["aggregate_id"] == "rule-1"
    assert fields["event_type"] == "DIRECTIVE_SUBMITTED"
    assert '"directive_id":"directive-1"' in fields["payload"]
    assert fake.kwargs == [{}]
