"""Redis relay for the conditional-rule transactional outbox."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import timezone

import redis

from orchestration.conditional_rules.worker_store import (
    ConditionalRuleOutboxRow,
    PostgresRuleWorkerStore,
)

LOG = logging.getLogger("conditional-rule-outbox-relay")
DEFAULT_STREAM = "hf:conditional-rule-events:v1"


class RedisConditionalRulePublisher:
    """Publish canonical outbox envelopes to one Redis Stream."""

    def __init__(self, url: str, *, stream: str, maxlen: int | None = None) -> None:
        self.stream = stream
        self.maxlen = (
            max(100, min(int(maxlen), 1_000_000)) if maxlen is not None else None
        )
        self.client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=3.0,
            socket_keepalive=True,
            health_check_interval=30,
            retry_on_timeout=True,
        )

    def ping(self) -> bool:
        return bool(self.client.ping())

    def publish(self, row: ConditionalRuleOutboxRow) -> None:
        fields = {
            "event_id": row.event_id,
            "aggregate_id": row.aggregate_id,
            "event_type": row.event_type,
            "created_at": row.created_at.astimezone(timezone.utc).isoformat(),
            "payload": json.dumps(
                row.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        if self.maxlen is None:
            # The PostgreSQL outbox is the retention boundary.  Approximate
            # trimming can delete an unclaimed stream entry while the
            # consumer is down, which turns a temporary outage into a lost
            # notification. Operators can opt into a bounded stream only when
            # they have an independent replay/retention policy.
            self.client.xadd(self.stream, fields)
        else:
            self.client.xadd(
                self.stream, fields, maxlen=self.maxlen, approximate=True
            )


def _stream_maxlen(value: str) -> int | None:
    normalized = value.strip()
    if not normalized or normalized == "0":
        return None
    try:
        return max(100, min(int(normalized), 1_000_000))
    except ValueError as exc:
        raise RuntimeError(
            "CONDITIONAL_RULE_EVENT_STREAM_MAXLEN must be an integer or 0"
        ) from exc


def _settings() -> tuple[PostgresRuleWorkerStore, RedisConditionalRulePublisher, float, int]:
    dsn = os.getenv("CONDITIONAL_RULE_DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("CONDITIONAL_RULE_DATABASE_URL is required")
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        raise RuntimeError("REDIS_URL is required")
    store = PostgresRuleWorkerStore(
        dsn,
        role=os.getenv(
            "CONDITIONAL_RULE_WORKER_DATABASE_ROLE", "svc_conditional_rule_worker"
        ).strip(),
    )
    publisher = RedisConditionalRulePublisher(
        redis_url,
        stream=os.getenv("CONDITIONAL_RULE_EVENT_STREAM", DEFAULT_STREAM).strip()
        or DEFAULT_STREAM,
        maxlen=_stream_maxlen(
            os.getenv("CONDITIONAL_RULE_EVENT_STREAM_MAXLEN", "")
        ),
    )
    poll = max(0.5, min(float(os.getenv("CONDITIONAL_RULE_OUTBOX_POLL_SECONDS", "1")), 60.0))
    batch = max(1, min(int(os.getenv("CONDITIONAL_RULE_OUTBOX_BATCH_SIZE", "100")), 1000))
    return store, publisher, poll, batch


def _log_drain_result(result: dict[str, int]) -> None:
    """Keep empty polling cycles out of the default INFO log stream."""

    level = (
        logging.INFO
        if any(result.get(key, 0) for key in ("picked", "published", "failed", "lost"))
        else logging.DEBUG
    )
    LOG.log(
        level,
        "conditional rule outbox cycle picked=%d published=%d failed=%d lost=%d",
        result["picked"],
        result["published"],
        result["failed"],
        result.get("lost", 0),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store, publisher, poll, batch = _settings()
    if args.healthcheck:
        publisher.ping()
        store.healthcheck()
        print("conditional-rule-outbox-relay ready")
        return 0

    while True:
        try:
            result = store.drain_outbox(publisher.publish, limit=batch)
            _log_drain_result(result)
        except Exception:
            LOG.exception("conditional rule outbox cycle failed")
        if args.once:
            return 0
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["RedisConditionalRulePublisher", "main"]
