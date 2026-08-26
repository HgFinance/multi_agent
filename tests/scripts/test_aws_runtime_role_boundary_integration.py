"""Opt-in, rollback-only PostgreSQL proof for the AWS runtime-role boundary.

Set all ``AWS_ROLE_TEST_*`` DSNs to an isolated database that has already run
``scripts/aws_database_bootstrap.py --seed-paper-principal``.  The normal test
suite skips this module's live check; partial configuration fails closed.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest


DSN_KEYS = (
    "AWS_ROLE_TEST_ADMIN_CONTROL_DSN",
    "AWS_ROLE_TEST_GENERIC_CONTROL_DSN",
    "AWS_ROLE_TEST_GENERIC_MARKET_DSN",
    "AWS_ROLE_TEST_ORDER_CONTROL_DSN",
    "AWS_ROLE_TEST_TRADING_CONTROL_DSN",
    "AWS_ROLE_TEST_ACCOUNTING_CONTROL_DSN",
    "AWS_ROLE_TEST_TRADING_MARKET_DSN",
)


def _configured_dsns() -> dict[str, str]:
    values = {key: os.environ.get(key, "").strip() for key in DSN_KEYS}
    present = [key for key, value in values.items() if value]
    if not present:
        pytest.skip("isolated AWS runtime-role integration DSNs are not configured")
    missing = [key for key, value in values.items() if not value]
    if missing:
        pytest.fail("isolated runtime-role integration DSN set is incomplete")
    return values


def _expect_insufficient_privilege(connection, statement: str) -> None:
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement)
    except psycopg2.Error as exc:
        connection.rollback()
        assert exc.pgcode == "42501"
    else:
        connection.rollback()
        pytest.fail("a forbidden runtime-role statement unexpectedly succeeded")


def test_fresh_aws_runtime_roles_support_bff_and_isolate_paper_mutations() -> None:
    psycopg2 = pytest.importorskip("psycopg2")
    dsns = _configured_dsns()

    with psycopg2.connect(dsns["AWS_ROLE_TEST_GENERIC_CONTROL_DSN"]) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select session_user,current_user")
            assert cursor.fetchone() == ("hgfinance_runtime", "hgfinance_runtime")
            # This is the production BFF identity refresh plus membership/book
            # lookup.  The transaction is rolled back after the proof.
            cursor.execute(
                """
                insert into governance.user_profiles (
                  user_id,display_name,timezone,status,identity_provider,
                  auth_subject_observed_at
                ) values (
                  %s,'AWS PAPER Operator','Asia/Seoul','ACTIVE','supabase',now()
                ) on conflict (user_id) do update
                  set auth_subject_observed_at=excluded.auth_subject_observed_at
                """,
                ("00000000-0000-4000-8000-00000000cec0",),
            )
            cursor.execute(
                """
                select membership.role
                  from governance.fund_memberships membership
                  join accounting.funds fund
                    on fund.fund_id=membership.fund_id
                  join accounting.books book on book.fund_id=fund.fund_id
                 where membership.user_id=%s and membership.status='ACTIVE'
                   and fund.fund_id=%s and book.book_id=%s
                 order by membership.role
                """,
                (
                    "00000000-0000-4000-8000-00000000cec0",
                    "3838f7d6-0c7c-4e54-85f3-316a451e7eeb",
                    "07d913de-9a5b-4cf5-b893-31a625445761",
                ),
            )
            assert [row[0] for row in cursor.fetchall()] == ["OWNER", "TRADER"]
            cursor.execute("select count(*) from execution.market_snapshots")
            cursor.fetchone()
            cursor.execute("set role svc_quant")
            cursor.execute("select current_user")
            assert cursor.fetchone()[0] == "svc_quant"
            cursor.execute("reset role")
        connection.rollback()
        for statement in (
            "insert into execution.user_order_requests default values",
            "insert into execution.user_directives default values",
            "insert into execution.order_intents default values",
            "update execution.orders set state=state where false",
            "update execution.outbox set status=status where false",
            "update accounting.journals set status=status where false",
            "update reference.instruments set status=status where false",
            "set role svc_trading_api",
        ):
            _expect_insufficient_privilege(connection, statement)

    with psycopg2.connect(dsns["AWS_ROLE_TEST_GENERIC_MARKET_DSN"]) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select count(*) from market.market_ticks")
            cursor.fetchone()

    with psycopg2.connect(dsns["AWS_ROLE_TEST_ORDER_CONTROL_DSN"]) as connection:
        with connection.cursor() as cursor:
            cursor.execute("set local role svc_order_orchestrator")
            cursor.execute(
                """
                insert into execution.user_order_requests (
                  user_id,fund_id,book_id,client_request_id,raw_instruction,
                  normalized_instruction,raw_instruction_sha256
                ) values (%s,%s,%s,%s,%s,%s,%s) returning mode,state
                """,
                (
                    "00000000-0000-4000-8000-00000000cec0",
                    "3838f7d6-0c7c-4e54-85f3-316a451e7eeb",
                    "07d913de-9a5b-4cf5-b893-31a625445761",
                    f"integration-{uuid4().hex}",
                    "삼성전자 10주 시장가 매수",
                    "삼성전자 10주 시장가 매수",
                    "a" * 64,
                ),
            )
            assert cursor.fetchone() == ("PAPER", "RECEIVED")
        connection.rollback()

    with psycopg2.connect(dsns["AWS_ROLE_TEST_TRADING_CONTROL_DSN"]) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select session_user,current_user")
            assert cursor.fetchone() == (
                "hgfinance_trading_runtime",
                "svc_trading_api",
            )
            cursor.execute(
                "update execution.outbox set attempts=attempts where false"
            )
            cursor.execute(
                "select has_column_privilege(current_user,"
                "'execution.outbox','status','UPDATE'),"
                "has_table_privilege(current_user,'execution.orders','INSERT')"
            )
            assert cursor.fetchone() == (True, False)
            cursor.execute(
                "select count(*) from risk.risk_decisions decision "
                "left join risk.risk_requests request "
                "on request.risk_request_id=decision.risk_request_id "
                "left join risk.risk_request_items item "
                "on item.risk_request_id=request.risk_request_id"
            )
            cursor.fetchone()
        connection.rollback()
        _expect_insufficient_privilege(
            connection, "update risk.risk_decisions set decision=decision where false"
        )
        _expect_insufficient_privilege(connection, "set role svc_accounting_ledger")

    with psycopg2.connect(dsns["AWS_ROLE_TEST_ACCOUNTING_CONTROL_DSN"]) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select session_user,current_user")
            assert cursor.fetchone() == (
                "hgfinance_accounting_runtime",
                "svc_accounting_ledger",
            )
            cursor.execute("update accounting.nav_runs set status=status where false")
            cursor.execute(
                "update accounting.investor_profiles set as_of=as_of where false"
            )
        connection.rollback()
        _expect_insufficient_privilege(connection, "set role svc_trading_api")

    with pytest.raises(psycopg2.OperationalError) as denied_market:
        psycopg2.connect(
            dsns["AWS_ROLE_TEST_TRADING_MARKET_DSN"], connect_timeout=3
        )
    assert "permission denied for database" in str(denied_market.value).casefold()

    with psycopg2.connect(dsns["AWS_ROLE_TEST_ADMIN_CONTROL_DSN"]) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select member.rolname,granted.rolname,
                       membership.set_option,membership.inherit_option
                  from pg_auth_members membership
                  join pg_roles member on member.oid=membership.member
                  join pg_roles granted on granted.oid=membership.roleid
                 where member.rolname like 'hgfinance%%'
                 order by member.rolname,granted.rolname
                """
            )
            rows = cursor.fetchall()
    exact = {
        "hgfinance_runtime": {
            "service_role",
            "svc_quant",
            "svc_dataset_builder",
            "svc_audit_api",
            "svc_qa_worker",
            "svc_qa_reproducer",
        },
        "hgfinance_order_runtime": {"svc_order_orchestrator"},
        "hgfinance_trading_runtime": {"svc_trading_api"},
        "hgfinance_accounting_runtime": {"svc_accounting_ledger"},
        "hgfinance_conditional_orchestrator": {
            "svc_conditional_rule_orchestrator"
        },
        "hgfinance_conditional_worker": {"svc_conditional_rule_worker"},
    }
    for login, expected in exact.items():
        assert {row[1] for row in rows if row[0] == login} == expected
    assert all(row[2] for row in rows)

    # Future critical-domain objects are deny-by-default.  Each DDL probe and
    # failed read are rolled back together, leaving the isolated DB unchanged.
    for schema_name, runtime_role in (
        ("execution", "service_role"),
        ("accounting", "svc_accounting_ledger"),
    ):
        probe = f"future_role_probe_{uuid4().hex}"
        with psycopg2.connect(
            dsns["AWS_ROLE_TEST_ADMIN_CONTROL_DSN"]
        ) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(f"create table {schema_name}.{probe}(id integer)")
                    cursor.execute(f"set role {runtime_role}")
                    cursor.execute(f"select * from {schema_name}.{probe}")
            except psycopg2.Error as exc:
                connection.rollback()
                assert exc.pgcode == "42501"
            else:
                connection.rollback()
                pytest.fail("a future critical-domain object was inherited by default")
