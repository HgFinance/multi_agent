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


def _probe_database(
    *,
    dsn_name: str,
    role_name: str,
    default_role: str,
) -> None:
    """Prove the configured login can assume its runtime authority."""

    dsn = str(os.getenv(dsn_name) or "").strip()
    role = str(os.getenv(role_name) or default_role).strip()
    if not dsn or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role):
        raise RuntimeError(f"{dsn_name} authority is not configured")
    with psycopg2.connect(dsn, connect_timeout=3) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
            cursor.execute("select 1")
            cursor.fetchone()


def main() -> int:
    redis_url = str(os.getenv("REDIS_URL") or "").strip()
    if not redis_url:
        raise RuntimeError("conditional notification authority is not configured")

    _probe_database(
        dsn_name="CONDITIONAL_RULE_DATABASE_URL",
        role_name="CONDITIONAL_RULE_WORKER_DATABASE_ROLE",
        default_role="svc_conditional_rule_worker",
    )
    _probe_database(
        dsn_name="ORDER_ORCHESTRATOR_DATABASE_URL",
        role_name="ORDER_ORCHESTRATOR_DATABASE_ROLE",
        default_role="svc_order_orchestrator",
    )

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
