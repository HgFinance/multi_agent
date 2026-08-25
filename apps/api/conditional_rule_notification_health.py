"""Lightweight authority probe for the conditional notification service.

This module intentionally imports no Discord, Kanban, Notion, LangSmith, or
order workflow code.  Docker runs it out-of-process, so importing the complete
consumer every few seconds would steal CPU from the immediate delivery lane.
"""

from __future__ import annotations

import os
import re

import psycopg2
import redis
from psycopg2 import sql


def main() -> int:
    dsn = str(os.getenv("CONDITIONAL_RULE_DATABASE_URL") or "").strip()
    redis_url = str(
        os.getenv("CONDITIONAL_RULE_EVENT_REDIS_URL")
        or os.getenv("REDIS_URL")
        or ""
    ).strip()
    role = str(
        os.getenv("CONDITIONAL_RULE_WORKER_DATABASE_ROLE")
        or "svc_conditional_rule_worker"
    )
    if not dsn or not redis_url or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", role
    ):
        raise RuntimeError("conditional notification authority is not configured")

    with psycopg2.connect(dsn, connect_timeout=3) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
            cursor.execute("select 1")
            cursor.fetchone()

    client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=3,
    )
    client.ping()
    stream = str(
        os.getenv("CONDITIONAL_RULE_EVENT_STREAM")
        or "hf:conditional-rule-events:v1"
    )
    expected = {
        str(
            os.getenv("CONDITIONAL_RULE_NOTIFICATION_GROUP")
            or "conditional-paper-reporting-v1"
        ),
        str(
            os.getenv("CONDITIONAL_RULE_PROJECTION_GROUP")
            or "conditional-paper-projection-v1"
        ),
    }
    groups = {
        str(row.get("name") or ""): int(row.get("consumers") or 0)
        for row in client.xinfo_groups(stream)
    }
    if missing := expected - set(groups):
        raise RuntimeError(f"conditional notification groups missing: {sorted(missing)}")
    inactive = []
    for name in expected:
        consumers = client.xinfo_consumers(stream, name)
        if not any(int(row.get("idle") or 0) < 60_000 for row in consumers):
            inactive.append(name)
    if inactive:
        raise RuntimeError(
            f"conditional notification consumers inactive: {sorted(inactive)}"
        )
    print("conditional-rule-notification-consumer ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
