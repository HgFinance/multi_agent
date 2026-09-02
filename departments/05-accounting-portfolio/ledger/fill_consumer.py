#!/usr/bin/env python3
"""ACC-01: 체결 하나가 Journal · Position · Portfolio Snapshot이 되는 경로.

소유: 도현 (회계·포트폴리오본부)
근거: docs/PROJECT_IMPLEMENTATION_STATUS.md `ACC-01`(Fill Consumer·Ledger·Snapshot Projector),
      Wave 2 "결정론적 Paper Investment Case" 5번
      docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.4, 9(D2)

체결 사실 -> 분개 -> Projection -> 스냅샷. 판정은 하나도 없다. 계산은 전부
`ledger.py`/`portfolio.py`가 하고 저장은 `repository.py`가 한다. 이 파일은 순서만 안다.

**체결의 정식 런타임 원천과 offline fixture 경계:**
  1. `execution.outbox` — 트레이딩본부가 만든 `trading.fill.v1` 봉투를 Relay가
     `SENT`로 표시한 뒤 소비한다. PENDING/FAILED는 소비하지 않는다.
  2. 회계 API `POST /ledgers/{id}/fills` — 명시적인 offline fixture-only 경로다.
     durable/PAPER_DB 런타임 증거로 승격하지 않는다.

두 경로 모두 멱등 키는 `broker_fill_id`다. `accounting.journals`의
`unique (event_type, source_event_id)`가 DB 수준에서 같은 체결의 이중 분개를 막는다.

**여기서 하지 않는 것:** 시세 조회(marks는 호출자가 준다), NAV 확정, Break 종결,
`execution.*` 쓰기.

자체 점검: python departments/05-accounting-portfolio/ledger/fill_consumer.py
           (DATABASE_URL 필요 - ACC-01 DoD를 실 DB에서 확인한다)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from pathlib import Path
from uuid import UUID

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "portfolio"))
sys.path.insert(0, str(_HERE.parent / "treasury"))
sys.path.insert(0, str(_HERE.parent.parent / "02-trading" / "contracts"))

from contracts import Side
from ledger import Journal, Ledger, Position, decimal_str
from portfolio import MarkPrice, PortfolioSnapshot, value_portfolio  # noqa: F401  (자체 점검이 value_portfolio를 직접 쓴다)
from repository import LedgerPersistenceError, LedgerRepository, PostgresLedger
from settlement import settle_due, settlement_date_for


@dataclass(frozen=True)
class FillRow:
    """`execution.fills` 한 줄. 우리가 값을 만들지 않고 받아 적기만 한다.

    `Ledger.post_fill()`이 요구하는 속성 이름(quantity/price/fee/tax/event_time/
    broker_fill_id/fill_id)을 그대로 쓴다 - 어댑터를 한 겹 더 두지 않기 위해서다.
    """

    fill_id: UUID
    instrument_id: UUID
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    tax: Decimal
    event_time: datetime
    broker_fill_id: str
    # Envelope lineage is optional for the explicit offline fixture path.
    event_id: UUID | None = None
    case_id: UUID | None = None
    trace_id: str = ""
    schema_version: str = ""
    occurred_at: datetime | None = None
    idempotency_key: str = ""
    producer: str = ""
    content_hash: str | None = None
    payload_ref: dict[str, Any] | None = None


def pending_fills(repo: LedgerRepository, fund_id: UUID, book_id: UUID) -> list[FillRow]:
    """아직 분개되지 않은 체결. 이미 분개된 것은 조인에서 걸러진다.

    Fund/Book은 `execution.order_intents`에서 온다 - 체결 자체는 Book을 모르고,
    어느 장부의 거래인지는 Intent가 정한다.
    """
    with repo.cursor() as cur:
        cur.execute(
            """
            select f.fill_id, f.instrument_id, f.side, f.quantity, f.price,
                   f.fee_amount, f.tax_amount, f.event_time, f.broker_fill_id
              from execution.fills f
              join execution.orders o on o.order_id = f.order_id
              join execution.order_intents i on i.order_intent_id = o.order_intent_id
              left join accounting.journals j
                     on j.event_type = 'fill' and j.source_event_id = f.broker_fill_id
             where i.fund_id = %s and i.book_id = %s and j.journal_id is null
             order by f.event_time, f.created_at
            """,
            (fund_id, book_id),
        )
        rows = cur.fetchall()
    return [
        FillRow(fill_id=fill_id, instrument_id=instrument_id, side=Side(side),
                quantity=quantity, price=price, fee=fee, tax=tax,
                event_time=event_time, broker_fill_id=broker_fill_id)
        for (fill_id, instrument_id, side, quantity, price, fee, tax,
             event_time, broker_fill_id) in rows
    ]


# 소비자 이름. `execution.outbox_consumed` 에 이 이름으로 기록된다.
CONSUMER = "accounting-ledger"
FILL_EVENT = "trading.fill.v1"


def pending_fill_events(repo: LedgerRepository, fund_id: UUID, book_id: UUID) -> list[FillRow]:
    """Return only relay-delivered canonical Fill events.

    PENDING/FAILED rows are deliberately invisible to this consumer. A Journal
    commit may precede acknowledgement; therefore rows with an existing Journal
    are still selected until their exact envelope event_id is acknowledged.
    """
    with repo.cursor() as cur:
        cur.execute(
            """
            select delivered.event_id,delivered.case_id,delivered.trace_id,
                   delivered.schema_version,delivered.occurred_at,
                   delivered.producer,delivered.idempotency_key,
                   delivered.payload_ref,delivered.fill_id,
                   delivered.instrument_id,delivered.side,delivered.quantity,
                   delivered.price,delivered.fee_amount,delivered.tax_amount,
                   delivered.event_time,delivered.broker_fill_id
              from (
                select o.outbox_id,o.event_id,o.case_id,o.trace_id,o.schema_version,
                       o.occurred_at,o.producer,o.idempotency_key,o.payload_ref,
                       f.fill_id,f.instrument_id,f.side,f.quantity,f.price,
                       f.fee_amount,f.tax_amount,f.event_time,f.broker_fill_id
                  from execution.outbox o
                  join execution.fills f
                    on f.fill_id=(o.payload_ref->>'artifact_id')::uuid
                  join execution.orders ord on ord.order_id=f.order_id
                  join execution.order_intents i
                    on i.order_intent_id=ord.order_intent_id
                  left join execution.outbox_consumed c
                    on c.event_id=o.event_id and c.consumer=%s
                 where o.event_type=%s and o.status='SENT'
                   and o.payload_ref->>'artifact_type'='FILL'
                   and (o.payload_ref->>'artifact_schema') is distinct from
                       'trading-user-directive-fill-v1'
                   and c.event_id is null
                   and i.fund_id=%s and i.book_id=%s
                union all
                select o.outbox_id,o.event_id,o.case_id,o.trace_id,o.schema_version,
                       o.occurred_at,o.producer,o.idempotency_key,o.payload_ref,
                       f.fill_id,f.instrument_id,f.side,f.quantity,f.price,
                       f.fee_amount,f.tax_amount,f.event_time,f.broker_fill_id
                  from execution.outbox o
                  join execution.paper_user_directive_fills f
                    on f.fill_id=(o.payload_ref->>'artifact_id')::uuid
                  join execution.user_directives directive
                    on directive.directive_id=f.directive_id
                  left join execution.outbox_consumed c
                    on c.event_id=o.event_id and c.consumer=%s
                 where o.event_type=%s and o.status='SENT'
                   and o.payload_ref->>'artifact_type'='FILL'
                   and o.payload_ref->>'artifact_schema'='trading-user-directive-fill-v1'
                   and c.event_id is null
                   and directive.fund_id=%s and directive.book_id=%s
              ) delivered
             -- Recovery can discover an older broker fill after a newer one
             -- has already been relayed. outbox_id is discovery order, not
             -- economic order; consuming a recovered SELL before its earlier
             -- BUY makes the position projection fail forever and prevents
             -- either envelope from being acknowledged. The immutable broker
             -- event_time is canonical. outbox_id remains a deterministic
             -- tie-breaker for fills with the same timestamp.
             order by delivered.event_time, delivered.outbox_id
            """,
            (
                CONSUMER, FILL_EVENT, fund_id, book_id,
                CONSUMER, FILL_EVENT, fund_id, book_id,
            ),
        )
        rows = cur.fetchall()

    result: list[FillRow] = []
    for (
        event_id, case_id, trace_id, schema_version, occurred_at, producer,
        idempotency_key, payload_ref, fill_id, instrument_id, side, quantity,
        price, fee, tax, event_time, broker_fill_id,
    ) in rows:
        payload_ref = dict(payload_ref or {})
        if (
            schema_version != "event-envelope-v1"
            or payload_ref.get("artifact_type") != "FILL"
            or str(payload_ref.get("artifact_id")) != str(fill_id)
            or not payload_ref.get("artifact_schema")
            or not payload_ref.get("content_hash")
            or not idempotency_key
            or not trace_id
        ):
            raise LedgerPersistenceError(
                f"canonical Fill envelope is incomplete (event_id={event_id})"
            )
        result.append(FillRow(
            fill_id=fill_id, instrument_id=instrument_id, side=Side(side),
            quantity=quantity, price=price, fee=fee, tax=tax,
            event_time=event_time, broker_fill_id=broker_fill_id,
            event_id=event_id, case_id=case_id, trace_id=str(trace_id),
            schema_version=schema_version, occurred_at=occurred_at,
            idempotency_key=idempotency_key, producer=producer,
            content_hash=payload_ref.get("content_hash"), payload_ref=payload_ref,
        ))
    return result


def ack_fill_events(
    repo: LedgerRepository,
    fill_ids: list[UUID] | None = None,
    *,
    event_ids: list[UUID] | None = None,
) -> int:
    """Acknowledge canonical envelope IDs after Journal commit.

    The historical positional ``fill_ids`` form remains an explicit offline
    compatibility path. The canonical consumer always passes ``event_ids`` by
    keyword, so it acknowledges exact envelope IDs.
    """
    fill_ids = fill_ids or []
    event_ids = event_ids or []
    if not event_ids and not fill_ids:
        return 0
    with repo.cursor() as cur:
        inserted = 0
        if event_ids:
            # Direct USER fills have no strategy order/order_intent by design.
            # Acknowledge them only if their exact Journal already exists, and
            # decrement the Trading reservation in the same transaction as
            # the consumer receipt.  A crash before this block is conservative
            # (the reservation remains); a crash within it rolls everything
            # back together.
            cur.execute(
                """
                select o.event_id,f.fill_id,f.leg_id,f.side,f.quantity,
                       f.gross_amount,f.fee_amount,f.tax_amount
                  from execution.outbox o
                  join execution.paper_user_directive_fills f
                    on f.fill_id=(o.payload_ref->>'artifact_id')::uuid
                 where o.event_type=%s and o.status='SENT'
                   and o.event_id=any(%s)
                   and o.payload_ref->>'artifact_schema'=
                       'trading-user-directive-fill-v1'
                   and f.accounting_acknowledged_at is null
                   and exists (
                     select 1 from accounting.journals journal
                      where journal.event_type='fill'
                        and journal.source_event_id=f.broker_fill_id
                   )
                 order by o.outbox_id
                 for update of f
                """,
                (FILL_EVENT, event_ids),
            )
            seen_direct_fills: set[UUID] = set()
            for event_id, fill_id, leg_id, side, quantity, gross, fee, tax in cur.fetchall():
                if fill_id in seen_direct_fills:
                    raise LedgerPersistenceError(
                        f"multiple direct Fill envelopes reference fill_id={fill_id}"
                    )
                seen_direct_fills.add(fill_id)
                cur.execute(
                    """
                    insert into execution.outbox_consumed (consumer,event_id)
                    values (%s,%s) on conflict do nothing
                    returning event_id
                    """,
                    (CONSUMER, event_id),
                )
                if cur.fetchone() is None:
                    continue
                inserted += 1
                if side == "BUY":
                    cur.execute(
                        """
                        update execution.paper_order_reservations
                           set reserved_cash=case
                                 when reserved_cash>(%s+%s+%s)
                                   then reserved_cash-(%s+%s+%s)
                                 else reserved_cash end,
                               state=case
                                 when reserved_cash<=(%s+%s+%s)
                                   then 'RELEASED' else state end,
                               released_at=case
                                 when reserved_cash<=(%s+%s+%s)
                                   then now() else released_at end,
                               version=version+1
                         where leg_id=%s and reservation_type='CASH'
                           and state='ACTIVE'
                        """,
                        (
                            gross, fee, tax, gross, fee, tax,
                            gross, fee, tax, gross, fee, tax, leg_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        update execution.paper_order_reservations
                           set reserved_quantity=case
                                 when reserved_quantity>%s
                                   then reserved_quantity-%s
                                 else reserved_quantity end,
                               state=case when reserved_quantity<=%s
                                          then 'RELEASED' else state end,
                               released_at=case when reserved_quantity<=%s
                                                then now() else released_at end,
                               version=version+1
                         where leg_id=%s and reservation_type='POSITION'
                           and state='ACTIVE'
                        """,
                        (quantity, quantity, quantity, quantity, leg_id),
                    )
                cur.execute(
                    """
                    update execution.paper_user_directive_fills
                       set accounting_acknowledged_at=now()
                     where fill_id=%s and accounting_acknowledged_at is null
                    """,
                    (fill_id,),
                )
                cur.execute(
                    """
                    update execution.paper_order_reservations reservation
                       set state='RELEASED',released_at=now(),version=version+1
                      from execution.user_directive_legs leg
                     where reservation.leg_id=%s
                       and leg.leg_id=reservation.leg_id
                       and reservation.state='ACTIVE'
                       and leg.state in ('FILLED','CANCELLED','EXPIRED','REJECTED')
                       and not exists (
                         select 1
                           from execution.paper_user_directive_fills pending
                          where pending.leg_id=reservation.leg_id
                            and pending.accounting_acknowledged_at is null
                       )
                    """,
                    (leg_id,),
                )
            cur.execute(
                """
                insert into execution.outbox_consumed (consumer, event_id)
                select %s, o.event_id
                  from execution.outbox o
                 where o.event_type = %s and o.status = 'SENT'
                   and o.event_id = any(%s)
                   and (o.payload_ref->>'artifact_schema') is distinct from
                       'trading-user-directive-fill-v1'
                on conflict do nothing
                """,
                (CONSUMER, FILL_EVENT, event_ids),
            )
            inserted += cur.rowcount
        if fill_ids:
            cur.execute(
                """
                insert into execution.outbox_consumed (consumer, event_id)
                select %s, o.event_id
                  from execution.outbox o
                 where o.event_type = %s and o.status = 'SENT'
                   and (o.payload_ref ->> 'artifact_id')::uuid = any(%s)
                   and (o.payload_ref->>'artifact_schema') is distinct from
                       'trading-user-directive-fill-v1'
                on conflict do nothing
                """,
                (CONSUMER, FILL_EVENT, fill_ids),
            )
            inserted += cur.rowcount
        return inserted


def _fill_evidence(fill: FillRow) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "evidence_class": "canonical_event" if fill.event_id else "fixture_only",
        "canonical": bool(fill.event_id),
    }
    if fill.event_id is not None:
        evidence.update({
            "event_id": str(fill.event_id),
            "event_type": FILL_EVENT,
            "schema_version": fill.schema_version,
            "case_id": str(fill.case_id) if fill.case_id else None,
            "trace_id": fill.trace_id,
            "occurred_at": fill.occurred_at.isoformat() if fill.occurred_at else None,
            "event_time": fill.event_time.isoformat(),
            "idempotency_key": fill.idempotency_key,
            "producer": fill.producer,
            "content_hash": fill.content_hash,
            "payload_ref": fill.payload_ref,
        })
    return evidence
def consume_fill(ledger: Ledger, fill: FillRow) -> Journal:
    """체결 하나를 분개로 만든다. 두 원천이 합류하는 지점이다.

    평균원가는 원장에서 재계산한다. 호출자가 주면 실현손익이 호출자가 정하는 값이 된다.

    **현금은 체결일에 움직이지 않는다.** KOSPI/KOSDAQ 현물은 T+2 결제라 체결일
    분개는 미지급금/미수금을 세우고 현금은 결제일에
    `treasury/settlement.py::settle_due`가 옮긴다. NAV는 그대로다 - 미수/미지급이
    이미 NAV에 들어 있다. 달라지는 것은 가용 현금이고, 그게 원래 사실이다.
    """
    positions, _ = ledger.rebuild()
    position = positions.get(fill.instrument_id, Position(fill.instrument_id))
    return ledger.post_fill(
        fill, fill.side, fill.instrument_id, position,
        trace_id=fill.trace_id, metadata=_fill_evidence(fill),
        settlement_date=settlement_date_for(fill.event_time.date()),
    )


def _save_snapshot(repo: LedgerRepository, ledger: PostgresLedger,
                   marks: dict[UUID, MarkPrice], as_of: datetime,
                   max_staleness: timedelta | None = None) -> PortfolioSnapshot:
    """Value and persist NAV after the accounting projection is durable."""
    snapshot = (value_portfolio(ledger, marks, as_of, max_staleness) if max_staleness
                else value_portfolio(ledger, marks, as_of))
    repo.save_snapshot(snapshot)
    return snapshot


def project(repo: LedgerRepository, ledger: PostgresLedger,
            marks: dict[UUID, MarkPrice], as_of: datetime,
            max_staleness: timedelta | None = None) -> PortfolioSnapshot:
    """Position/Cash를 갱신하고 스냅샷을 남긴다.

    보유 종목 중 하나라도 신선한 Mark가 없으면 `value_portfolio`가 예외를 낸다.
    그때 Projection은 이미 저장돼 있고 스냅샷만 없다 - 의도한 순서다. Position은
    체결에서 나온 사실이라 시세와 무관하게 참이고, NAV는 시세 없이는 틀린 수치다.
    """
    repo.save_projection(ledger)
    return _save_snapshot(repo, ledger, marks, as_of, max_staleness)


def run_once(
    repo: LedgerRepository,
    fund_id: UUID,
    book_id: UUID,
    marks: dict[UUID, MarkPrice],
    as_of: datetime,
    *,
    events_first: bool = True,
    fixture_only: bool = False,
) -> tuple[list[Journal], PortfolioSnapshot]:
    """Consume canonical SENT events, then project.

    ``fixture_only`` is an explicit offline compatibility path. It is never
    enabled by the durable canonical consumer, so API/table-injected fills
    cannot become runtime evidence accidentally.
    """
    ledger = repo.load(fund_id, book_id)
    journals: list[Journal] = []
    event_ids: list[UUID] = []

    if events_first:
        for fill in pending_fill_events(repo, fund_id, book_id):
            journals.append(consume_fill(ledger, fill))
            if fill.event_id is None:
                raise LedgerPersistenceError("canonical Fill is missing event_id")
            event_ids.append(fill.event_id)
    if fixture_only:
        journals.extend(consume_fill(ledger, fill)
                        for fill in pending_fills(repo, fund_id, book_id))

    # 결제일이 도래한 미지급금/미수금을 현금으로 옮긴다. 이 줄이 없으면 체결일
    # 분개만 쌓이고 현금은 영원히 안 움직인다 - 매수 대금이 안 나간 것처럼 보여
    # 가용 현금이 실제보다 많아진다. Projection 앞에 둬야 그 주기 스냅샷에 반영된다.
    journals.extend(settle_due(ledger, as_of.date(), now=as_of))
    # Journal plus position/cash are the canonical accounting projection and
    # therefore the Fill acknowledgement boundary. NAV is a downstream
    # valuation: a missing/stale mark must not leave an already-projected Fill
    # and its reservation permanently unacknowledged.
    repo.save_projection(ledger)
    ack_fill_events(repo, event_ids=event_ids)
    snapshot = _save_snapshot(repo, ledger, marks, as_of)
    return journals, snapshot


if __name__ == "__main__":
    from datetime import timezone
    from uuid import NAMESPACE_OID, uuid5

    try:
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env")
    except ModuleNotFoundError:
        pass

    repo = LedgerRepository.from_env()
    if repo is None:
        print("skip - DATABASE_URL이 없다. ACC-01은 실 DB 확인이라 건너뛴다")
        raise SystemExit(0)

    D = Decimal
    # 재실행해도 같은 상태가 되도록 시각과 체결 id를 고정한다.
    FIXED = datetime(2026, 8, 4, 6, 0, tzinfo=timezone.utc)
    fund_id, book_id = repo.bootstrap(
        "ACC01-PAPER", "MAIN",
        fund_name="Paper Fund (ACC-01 Fixture)", book_name="Paper Main Book")

    with repo.cursor() as cur:
        cur.execute("select instrument_id from reference.instruments order by instrument_id limit 1")
        instrument_id = cur.fetchone()[0]

    def row_counts() -> tuple[int, int, int]:
        with repo.cursor() as cur:
            cur.execute("select count(*) from accounting.journals where fund_id=%s and book_id=%s",
                        (fund_id, book_id))
            journals = cur.fetchone()[0]
            cur.execute("select count(*) from accounting.positions where fund_id=%s and book_id=%s",
                        (fund_id, book_id))
            positions = cur.fetchone()[0]
            cur.execute("select count(*) from accounting.portfolio_snapshots "
                        "where fund_id=%s and book_id=%s", (fund_id, book_id))
            return journals, positions, cur.fetchone()[0]

    fill = FillRow(
        fill_id=uuid5(NAMESPACE_OID, "ACC01-FILL-0001"), instrument_id=instrument_id,
        side=Side.BUY, quantity=D("10"), price=D("50000"), fee=D("75"), tax=D("0"),
        event_time=FIXED, broker_fill_id="ACC01-FILL-0001")
    # 확정 종가 Fixture다. is_final을 명시하지 않으면 미확정으로 잡혀 quality_status가
    # WARN이 된다 - 그 기본값이 의도된 것임은 아래 6번이 검사한다.
    marks = {instrument_id: MarkPrice(instrument_id, D("52000"), FIXED, is_final=True)}

    # 0. 정식 경로(execution.fills)는 아직 비어 있다. 그 사실을 숨기지 않는다
    pending = pending_fills(repo, fund_id, book_id)
    if pending:
        print(f"note - execution.fills에 미처리 체결 {len(pending)}건이 있다(TRD-01 연결됨)")

    # 1. 체결 1건 -> 분개. 같은 broker_fill_id면 두 번째는 기존 분개가 돌아온다
    ledger = repo.load(fund_id, book_id)
    if not ledger.journals:
        ledger.post_capital(D("100000000"), FIXED, "ACC01-SEED-CAPITAL")
    journal = consume_fill(ledger, fill)
    assert journal.event_type == "fill" and journal.source_event_id == "ACC01-FILL-0001"
    assert sum(l.debit for l in journal.lines) == sum(l.credit for l in journal.lines)
    assert consume_fill(ledger, fill).journal_id == journal.journal_id, "같은 체결로 분개가 두 건"

    # 2. Projection + 스냅샷
    snapshot = project(repo, ledger, marks, FIXED)

    # 3. ACC-01 DoD - 세 표에 행이 남았는가
    with repo.cursor() as cur:
        cur.execute("select status, count(*) from accounting.journal_lines l "
                    "join accounting.journals j on j.journal_id = l.journal_id "
                    "where j.source_event_id = %s group by status", ("ACC01-FILL-0001",))
        status, line_count = cur.fetchone()
        assert status == "POSTED" and line_count == 3, (status, line_count)

        cur.execute("select quantity, average_cost, realized_pnl from accounting.positions "
                    "where fund_id=%s and book_id=%s and instrument_id=%s",
                    (fund_id, book_id, instrument_id))
        quantity, average_cost, realized = cur.fetchone()
        assert quantity == D("10") and average_cost == D("50000"), (quantity, average_cost)
        assert realized == 0, f"매수만 했는데 실현손익이 생겼다: {realized}"

        cur.execute("select nav, quality_status, positions from accounting.portfolio_snapshots "
                    "where fund_id=%s and book_id=%s and as_of=%s", (fund_id, book_id, FIXED))
        nav, quality, snapshot_positions = cur.fetchone()
        assert nav == snapshot.nav and quality == "PASS", (nav, snapshot.nav)
        assert snapshot_positions[0]["mark_price"] == "52000", snapshot_positions

        cur.execute("select settled_amount from accounting.cash_balances "
                    "where fund_id=%s and book_id=%s", (fund_id, book_id))
        assert cur.fetchone()[0] == snapshot.cash, "현금 잔고가 스냅샷과 다르다"

    # 4. 평가손익이 원장이 아니라 Mark에서 나온다 (10주 x (52,000-50,000))
    assert snapshot.unrealized_pnl == D("20000"), snapshot.unrealized_pnl
    assert snapshot.nav == snapshot.cash + D("520000"), snapshot.nav

    # 5. 멱등 - 전 과정을 다시 돌려도 행이 늘지 않는다
    before = row_counts()
    run_once(repo, fund_id, book_id, marks, FIXED)
    ledger2 = repo.load(fund_id, book_id)
    consume_fill(ledger2, fill)
    project(repo, ledger2, marks, FIXED)
    assert row_counts() == before, f"재실행으로 행이 늘었다: {before} -> {row_counts()}"

    # 6. Mark가 없으면 스냅샷을 만들지 않는다. Position은 이미 저장돼 있다
    try:
        project(repo, repo.load(fund_id, book_id), {}, FIXED)
        raise AssertionError("Mark 없이 NAV가 만들어졌다")
    except Exception as exc:
        assert "평가 불가" in str(exc), exc
    assert row_counts()[2] == before[2], "실패한 평가가 스냅샷을 남겼다"

    # 7. is_final을 말하지 않은 Mark는 미확정으로 잡힌다 (2026-08-03 `3978ee1` 재발 방지).
    #    NAV는 나오되 PASS가 아니다 - 진행 중인 봉으로 만든 NAV가 확정 종가 NAV와
    #    같은 얼굴을 하면 안 된다. 이건 저장하지 않고 계산만 확인한다.
    unstated = value_portfolio(
        repo.load(fund_id, book_id),
        {instrument_id: MarkPrice(instrument_id, D("52000"), FIXED)}, FIXED)
    assert unstated.nav == snapshot.nav, "확정 여부가 NAV 값을 바꿨다"
    assert unstated.quality_status == "WARN", unstated.quality_status
    assert snapshot.quality_status == "PASS", snapshot.quality_status

    # 8. symbol -> instrument_id 다리. market-api는 symbol을 주고 우리는 UUID를 쓴다
    with repo.cursor() as cur:
        cur.execute("select symbol from reference.instrument_symbols "
                    "where instrument_id=%s and valid_to is null limit 1", (instrument_id,))
        row = cur.fetchone()
    if row is not None:
        assert repo.instrument_by_symbol(row[0], as_of=FIXED) == instrument_id, \
            f"symbol {row[0]} 이 다른 instrument로 풀렸다"
    assert repo.instrument_by_symbol("없는코드", as_of=FIXED) is None, \
        "모르는 symbol에 instrument를 붙였다"

    print(f"ok - ACC-01 통과: 체결 1건 -> 분개 {line_count}줄 · "
          f"Position {decimal_str(quantity)}주 · NAV {decimal_str(snapshot.nav)} "
          f"(실 DB, 재실행 멱등)")
