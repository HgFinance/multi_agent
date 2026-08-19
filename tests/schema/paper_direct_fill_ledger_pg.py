"""Isolated PostgreSQL smoke for direct USER PAPER Fill -> Ledger projection.

Run only against a disposable database after all migrations:
    DATABASE_URL=postgresql://... python tests/schema/paper_direct_fill_ledger_pg.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json


ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "departments" / "05-accounting-portfolio" / "ledger"
PORTFOLIO = ROOT / "departments" / "05-accounting-portfolio" / "portfolio"
sys.path[:0] = [str(LEDGER), str(PORTFOLIO)]

from fill_consumer import run_once  # noqa: E402
from portfolio import MarkPrice  # noqa: E402
from repository import (  # noqa: E402
    ACCOUNTING_LEDGER_DATABASE_ROLE,
    LedgerRepository,
)


def main() -> None:
    dsn = os.environ["DATABASE_URL"]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    broad_repo = LedgerRepository.connect(dsn)
    suffix = uuid4().hex[:12]
    fund_id, book_id = broad_repo.bootstrap(
        f"PGFILL-{suffix}", "MAIN", fund_name="PG Fill Smoke", book_name="Main"
    )
    instrument_id = uuid4()
    user_id = uuid4()
    directive_id = uuid4()
    leg_id = uuid4()
    fill_id = uuid4()
    event_id = uuid4()
    trace_id = uuid4()
    broker_fill_id = f"pg-direct-fill-{suffix}"

    connection = psycopg2.connect(dsn)
    try:
        with connection:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    insert into reference.instruments (
                      instrument_id,instrument_type,asset_class,market,venue,
                      currency,display_name,isin,status,lot_size,tick_size
                    ) values (%s,'EQUITY','EQUITY','KRX','KRX','KRW',%s,%s,
                              'ACTIVE',1,1)
                    """,
                    (instrument_id, f"PG Fill {suffix}", f"PG{suffix.upper()}"),
                )
                # The smoke fixture is not an auth test. Bypass only FK/scope
                # triggers while preserving NOT NULL/CHECK constraints.
                cur.execute("set local session_replication_role = replica")
                payload = {
                    "instrument_id": str(instrument_id),
                    "symbol": "005930",
                    "side": "BUY",
                    "quantity": "1",
                    "order_type": "MARKET",
                    "limit_price": None,
                    "time_in_force": "DAY",
                }
                cur.execute(
                    """
                    insert into execution.user_directives (
                      directive_id,user_id,fund_id,book_id,action,instruction_ref,
                      idempotency_key,payload,payload_sha256,proof_issuer,
                      proof_audience,proof_issued_at,proof_not_before,
                      proof_expires_at,priority,state,completed_at
                    ) values (
                      %s,%s,%s,%s,'PLACE_ORDER',%s,%s,%s,%s,
                      'portfolio-bff','trading-api',%s,%s,%s,1000,'COMPLETED',%s
                    )
                    """,
                    (
                        directive_id,
                        user_id,
                        fund_id,
                        book_id,
                        f"instruction-{suffix}",
                        f"idem-{suffix}",
                        Json(payload),
                        "0" * 64,
                        now,
                        now,
                        now + timedelta(seconds=60),
                        now,
                    ),
                )
                cur.execute(
                    """
                    insert into execution.user_directive_legs (
                      leg_id,directive_id,leg_index,instrument_id,symbol,side,
                      order_type,time_in_force,requested_quantity,filled_quantity,
                      target_filled_quantity,state,expires_at
                    ) values (%s,%s,0,%s,'005930','BUY','MARKET','DAY',1,1,1,
                              'FILLED',%s)
                    """,
                    (leg_id, directive_id, instrument_id, now + timedelta(hours=1)),
                )
                cur.execute(
                    """
                    insert into execution.paper_order_reservations (
                      directive_id,leg_id,fund_id,book_id,reservation_type,
                      reserved_cash,currency,state
                    ) values (%s,%s,%s,%s,'CASH',101,'KRW','ACTIVE')
                    """,
                    (directive_id, leg_id, fund_id, book_id),
                )
                cur.execute(
                    """
                    insert into execution.paper_user_directive_fills (
                      fill_id,leg_id,directive_id,quote_event_key,broker_fill_id,
                      instrument_id,side,quantity,price,gross_amount,fee_amount,
                      tax_amount,currency,event_time,received_at,quote_source,trace_id
                    ) values (%s,%s,%s,%s,%s,%s,'BUY',1,100,100,0.02,0,
                              'KRW',%s,%s,'pg-smoke',%s)
                    """,
                    (
                        fill_id,
                        leg_id,
                        directive_id,
                        "1" * 64,
                        broker_fill_id,
                        instrument_id,
                        now,
                        now,
                        trace_id,
                    ),
                )
                cur.execute(
                    """
                    insert into execution.outbox (
                      event_id,event_type,schema_version,trace_id,producer,
                      occurred_at,idempotency_key,payload_ref,status,sent_at
                    ) values (%s,'trading.fill.v1','event-envelope-v1',%s,
                              'trading-user-directive',%s,%s,%s,'SENT',%s)
                    """,
                    (
                        event_id,
                        trace_id,
                        now,
                        f"direct-fill:{fill_id}",
                        Json(
                            {
                                "artifact_type": "FILL",
                                "artifact_id": str(fill_id),
                                "artifact_schema": "trading-user-directive-fill-v1",
                                "content_hash": "a" * 64,
                            }
                        ),
                        now,
                    ),
                )

        role_repo = LedgerRepository.connect(
            dsn, database_role=ACCOUNTING_LEDGER_DATABASE_ROLE
        )
        journals, snapshot = run_once(
            role_repo,
            fund_id,
            book_id,
            {
                instrument_id: MarkPrice(
                    instrument_id, Decimal("100"), now, "pg-smoke", True
                )
            },
            now,
        )
        assert any(j.source_event_id == broker_fill_id for j in journals)
        assert snapshot.fund_id == fund_id and snapshot.book_id == book_id

        with connection:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    select
                      exists(select 1 from accounting.journals
                              where event_type='fill' and source_event_id=%s),
                      (select quantity from accounting.positions
                        where fund_id=%s and book_id=%s and instrument_id=%s),
                      (select accounting_acknowledged_at is not null
                         from execution.paper_user_directive_fills where fill_id=%s),
                      (select state from execution.paper_order_reservations
                         where leg_id=%s),
                      exists(select 1 from execution.outbox_consumed
                              where consumer='accounting-ledger' and event_id=%s)
                    """,
                    (
                        broker_fill_id,
                        fund_id,
                        book_id,
                        instrument_id,
                        fill_id,
                        leg_id,
                        event_id,
                    ),
                )
                journal, quantity, acknowledged, reservation, receipt = cur.fetchone()
        assert (journal, quantity, acknowledged, reservation, receipt) == (
            True,
            Decimal("1.0000000000"),
            True,
            "RELEASED",
            True,
        )
        print(
            "PASS direct Fill -> Journal -> position -> receipt/ack/reservation release"
        )
    finally:
        broad_repo.close()
        connection.close()


if __name__ == "__main__":
    main()
