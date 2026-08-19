"""Isolated PostgreSQL smoke for CEO -> Hermes -> PAPER OMS authority.

Run only against a disposable database after all Supabase migrations:
    CONTROL_DATABASE_URL=postgresql://... python tests/schema/ceo_hermes_paper_order_pg.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import errors

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from apps.api.user_order_workflow import PostgresUserOrderRequestRepository


def _seed_scope(connection, *, prefix: str):
    user_id, fund_id, book_id = (uuid4(), uuid4(), uuid4())
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into accounting.funds (
              fund_id,fund_code,name,base_currency,inception_date,status
            ) values (%s,%s,%s,'KRW',current_date,'ACTIVE')
            """,
            (fund_id, f"{prefix}-{str(fund_id)[:8]}", prefix),
        )
        cursor.execute(
            """
            insert into accounting.books (
              book_id,fund_id,book_code,name,book_type,status
            ) values (%s,%s,'MAIN','Main','TRADING','ACTIVE')
            """,
            (book_id, fund_id),
        )
        cursor.execute(
            """
            insert into governance.user_profiles (user_id,display_name,status)
            values (%s,%s,'ACTIVE')
            """,
            (user_id, prefix),
        )
        cursor.execute(
            """
            insert into governance.fund_memberships (fund_id,user_id,role,status)
            values (%s,%s,'OWNER','ACTIVE')
            """,
            (fund_id, user_id),
        )
    return user_id, fund_id, book_id


def main() -> None:
    dsn = os.environ["CONTROL_DATABASE_URL"]
    now = datetime.now(timezone.utc)
    directive_id = uuid4()
    suffix = uuid4().hex[:12]
    client_request_id = f"pg-order-{suffix}"
    trading_task_id = f"trading-{suffix}"

    with psycopg2.connect(dsn) as connection:
        user_id, fund_id, book_id = _seed_scope(connection, prefix="ORDERPG")
        other_user_id, other_fund_id, other_book_id = _seed_scope(
            connection, prefix="ORDERPGOTHER"
        )
        with connection.cursor() as cursor:
            # This fixture bypasses the 015 admission trigger only to seed the
            # already-validated directive side of the 016 relationship.
            cursor.execute("set local session_replication_role = replica")
            cursor.execute(
                """
                insert into execution.user_directives (
                  directive_id,user_id,fund_id,book_id,action,instruction_ref,
                  idempotency_key,payload,payload_sha256,proof_issuer,
                  proof_audience,proof_issued_at,proof_not_before,
                  proof_expires_at,priority,state
                ) values (
                  %s,%s,%s,%s,'CANCEL_ALL',%s,
                  %s,'{}',%s,'portfolio-bff',
                  'trading-api',%s,%s,%s,2000,'RECEIVED'
                )
                """,
                (
                    directive_id,
                    user_id,
                    fund_id,
                    book_id,
                    f"pg-order-instruction-{suffix}",
                    f"pg-order-idempotency-{suffix}",
                    "0" * 64,
                    now,
                    now,
                    now + timedelta(seconds=20),
                ),
            )

    repository = PostgresUserOrderRequestRepository(dsn)
    request = repository.admit(
        user_id=str(user_id),
        fund_id=str(fund_id),
        book_id=str(book_id),
        client_request_id=client_request_id,
        raw_instruction="모든 미체결 주문 취소",
    )
    replay = repository.admit(
        user_id=str(user_id),
        fund_id=str(fund_id),
        book_id=str(book_id),
        client_request_id=client_request_id,
        raw_instruction="모든 미체결 주문 취소",
    )
    assert replay.order_request_id == request.order_request_id

    request = repository.bind_root(request.order_request_id, f"ceo-root-{suffix}")
    request = repository.bind_trading_task(
        request.order_request_id, trading_task_id
    )
    interpretation = {"action": "CANCEL_ALL", "binding": False, "mode": "PAPER"}
    interpretation_sha256 = hashlib.sha256(
        json.dumps(interpretation, sort_keys=True).encode("utf-8")
    ).hexdigest()
    request = repository.record_interpretation(
        request.order_request_id,
        trading_task_id=trading_task_id,
        interpretation=interpretation,
        interpretation_sha256=interpretation_sha256,
    )
    request = repository.mark_outcome(
        request.order_request_id,
        state="SUBMITTED",
        action="CANCEL_ALL",
        canonical_payload={},
        payload_sha256=hashlib.sha256(b"{}").hexdigest(),
        directive_id=str(directive_id),
    )
    assert request.mode == "PAPER" and request.directive_id == str(directive_id)

    other_request = repository.admit(
        user_id=str(other_user_id),
        fund_id=str(other_fund_id),
        book_id=str(other_book_id),
        client_request_id=f"pg-other-{suffix}",
        raw_instruction="모든 미체결 주문 취소",
    )

    connection = psycopg2.connect(dsn)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select rolcanlogin,rolsuper,rolinherit,rolcreatedb,
                           rolcreaterole,rolreplication,rolbypassrls
                      from pg_roles where rolname='svc_order_orchestrator'
                    """
                )
                assert cursor.fetchone() == (False,) * 7
                cursor.execute(
                    """
                    select
                      has_column_privilege(
                        'svc_order_orchestrator','execution.user_order_requests',
                        'state','UPDATE'),
                      has_column_privilege(
                        'svc_order_orchestrator','execution.user_order_requests',
                        'user_id','UPDATE'),
                      has_table_privilege(
                        'svc_order_orchestrator',
                        'execution.user_order_interpretations','UPDATE'),
                      has_table_privilege(
                        'svc_order_orchestrator','execution.user_directives','INSERT'),
                      has_column_privilege(
                        'svc_order_orchestrator','execution.user_directives',
                        'source_order_request_id','UPDATE')
                    """
                )
                assert cursor.fetchone() == (True, False, False, False, True)
                cursor.execute(
                    """
                    select source_order_request_id
                      from execution.user_directives where directive_id=%s
                    """,
                    (directive_id,),
                )
                assert str(cursor.fetchone()[0]) == request.order_request_id
                cursor.execute(
                    """
                    select count(*) from execution.user_order_interpretations
                     where order_request_id=%s
                    """,
                    (request.order_request_id,),
                )
                assert cursor.fetchone()[0] == 1
                cursor.execute(
                    """
                    select count(*) from execution.user_order_request_events
                     where order_request_id=%s
                    """,
                    (request.order_request_id,),
                )
                assert cursor.fetchone()[0] == 5

        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute("set local role svc_order_orchestrator")
                    cursor.execute(
                        """
                        insert into execution.user_order_requests (
                          user_id,fund_id,book_id,client_request_id,mode,
                          raw_instruction,normalized_instruction,
                          raw_instruction_sha256
                        ) values (%s,%s,%s,%s,'LIVE',
                          '취소','취소',%s)
                        """,
                        (user_id, fund_id, book_id, f"pg-live-{suffix}", "f" * 64),
                    )
        except errors.CheckViolation:
            pass
        else:
            raise AssertionError("LIVE order request was accepted")

        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute("set local role svc_order_orchestrator")
                    cursor.execute(
                        """
                        update execution.user_order_requests set user_id=%s
                         where order_request_id=%s
                        """,
                        (other_user_id, request.order_request_id),
                    )
        except errors.InsufficientPrivilege:
            pass
        else:
            raise AssertionError("admitted user authority remained mutable")

        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute("set local role svc_order_orchestrator")
                    cursor.execute(
                        """
                        update execution.user_directives
                           set source_order_request_id=%s
                         where directive_id=%s
                        """,
                        (other_request.order_request_id, directive_id),
                    )
        except errors.ForeignKeyViolation:
            pass
        else:
            raise AssertionError("cross-user/Fund/Book directive binding was accepted")
    finally:
        connection.close()

    print(
        "PASS CEO/Kanban/Hermes request -> append-only interpretation -> "
        "scope-bound PAPER directive"
    )


if __name__ == "__main__":
    main()
