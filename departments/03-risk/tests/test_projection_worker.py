from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from risk_events.projection_worker import (
    InMemoryProjectionStore,
    RiskProjectionError,
    RiskProjectionWorker,
)


class _Redis:
    def __init__(
        self, messages: list[tuple[str, list[tuple[str, dict[str, str]]]]]
    ) -> None:
        self.messages = messages
        self.acks: list[tuple[str, str, str]] = []

    def xgroup_create(self, *_args: object, **_kwargs: object) -> None:
        return None

    def xreadgroup(self, *_args: object, **_kwargs: object):
        return self.messages

    def xack(self, stream: str, group: str, message_id: str) -> int:
        self.acks.append((stream, group, message_id))
        return 1


def _message() -> tuple[str, list[tuple[str, dict[str, str]]]]:
    return (
        "portfolio.snapshot.v1",
        [
            (
                "1-0",
                {
                    "event_id": str(uuid4()),
                    "event_type": "portfolio.snapshot.v1",
                    "trace_id": str(uuid4()),
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "payload": json.dumps({"fund_id": str(uuid4()), "equity": "1000"}),
                },
            )
        ],
    )


def test_projection_persists_before_ack_and_is_idempotent() -> None:
    redis = _Redis([_message()])
    store = InMemoryProjectionStore()
    worker = RiskProjectionWorker(redis, store, streams=("portfolio.snapshot.v1",))

    assert worker.consume_once() == 1
    assert len(store.events) == 1
    assert redis.acks == [("portfolio.snapshot.v1", "risk-p1-projection", "1-0")]


def test_projection_rejects_naive_timestamps_without_ack() -> None:
    stream, messages = _message()
    messages[0][1]["occurred_at"] = "2026-08-04T00:00:00"
    redis = _Redis([(stream, messages)])
    worker = RiskProjectionWorker(redis, InMemoryProjectionStore(), streams=(stream,))

    with pytest.raises(RiskProjectionError, match="timestamp"):
        worker.consume_once()
    assert redis.acks == []
