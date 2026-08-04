#!/usr/bin/env python3
"""ACC-01: 체결 하나가 Journal · Position · Portfolio Snapshot이 되는 경로.

소유: 도현 (회계·포트폴리오본부)
근거: docs/PROJECT_IMPLEMENTATION_STATUS.md `ACC-01`(Fill Consumer·Ledger·Snapshot Projector),
      Wave 2 "결정론적 Paper Investment Case" 5번
      docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.4, 9(D2)

체결 사실 -> 분개 -> Projection -> 스냅샷. 판정은 하나도 없다. 계산은 전부
`ledger.py`/`portfolio.py`가 하고 저장은 `repository.py`가 한다. 이 파일은 순서만 안다.

**체결의 원천은 두 갈래이고 둘 다 우리가 만들지 않는다:**
  1. `execution.fills` — 정식 경로. 트레이딩본부가 브로커 사실을 기록한 표를 폴링한다.
     오늘 이 표는 0행이다(TRD-01의 psycopg OrderStore 대기). 그래서 `pending_fills()`는
     지금 항상 빈 리스트를 준다 - 그게 정상이며, 가짜 체결로 채우지 않는다.
  2. 회계 API `POST /ledgers/{id}/fills` — 서비스 호출자가 밀어 넣는 경로.
     Paper 루프가 지금 쓰는 길이고, 같은 `consume_fill()`로 합류한다.

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
from pathlib import Path
from uuid import UUID

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "portfolio"))
sys.path.insert(0, str(_HERE.parent.parent / "02-trading" / "contracts"))

from contracts import Side
from ledger import Journal, Position, decimal_str
from portfolio import MarkPrice, PortfolioSnapshot, value_portfolio
from repository import LedgerRepository, PostgresLedger


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


def pending_fills(repo: LedgerRepository, fund_id: UUID, book_id: UUID) -> list[FillRow]:
    """아직 분개되지 않은 체결. 이미 분개된 것은 조인에서 걸러진다.

    Fund/Book은 `execution.order_intents`에서 온다 - 체결 자체는 Book을 모르고,
    어느 장부의 거래인지는 Intent가 정한다.
    """
    with repo._cursor() as cur:
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


def consume_fill(ledger: PostgresLedger, fill: FillRow) -> Journal:
    """체결 하나를 분개로 만든다. 두 원천이 합류하는 지점이다.

    평균원가는 원장에서 재계산한다. 호출자가 주면 실현손익이 호출자가 정하는 값이 된다.
    """
    positions, _ = ledger.rebuild()
    position = positions.get(fill.instrument_id, Position(fill.instrument_id))
    return ledger.post_fill(fill, fill.side, fill.instrument_id, position)


def project(repo: LedgerRepository, ledger: PostgresLedger,
            marks: dict[UUID, MarkPrice], as_of: datetime,
            max_staleness: timedelta | None = None) -> PortfolioSnapshot:
    """Position/Cash를 갱신하고 스냅샷을 남긴다.

    보유 종목 중 하나라도 신선한 Mark가 없으면 `value_portfolio`가 예외를 낸다.
    그때 Projection은 이미 저장돼 있고 스냅샷만 없다 - 의도한 순서다. Position은
    체결에서 나온 사실이라 시세와 무관하게 참이고, NAV는 시세 없이는 틀린 수치다.
    """
    repo.save_projection(ledger)
    snapshot = (value_portfolio(ledger, marks, as_of, max_staleness) if max_staleness
                else value_portfolio(ledger, marks, as_of))
    repo.save_snapshot(snapshot)
    return snapshot


def run_once(repo: LedgerRepository, fund_id: UUID, book_id: UUID,
             marks: dict[UUID, MarkPrice], as_of: datetime
             ) -> tuple[list[Journal], PortfolioSnapshot]:
    """`execution.fills`의 미처리 체결을 모두 반영하고 스냅샷을 만든다."""
    ledger = repo.load(fund_id, book_id)
    journals = [consume_fill(ledger, fill) for fill in pending_fills(repo, fund_id, book_id)]
    return journals, project(repo, ledger, marks, as_of)


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

    with repo._cursor() as cur:
        cur.execute("select instrument_id from reference.instruments order by instrument_id limit 1")
        instrument_id = cur.fetchone()[0]

    def row_counts() -> tuple[int, int, int]:
        with repo._cursor() as cur:
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
    marks = {instrument_id: MarkPrice(instrument_id, D("52000"), FIXED)}

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
    with repo._cursor() as cur:
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

    print(f"ok - ACC-01 통과: 체결 1건 -> 분개 {line_count}줄 · "
          f"Position {decimal_str(quantity)}주 · NAV {decimal_str(snapshot.nav)} "
          f"(실 DB, 재실행 멱등)")
