#!/usr/bin/env python3
"""Sprint D2: 이중분개 원장과 Position/Cash Projection.

소유: 도현 (회계/포트폴리오본부)
근거: docs/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.4, 8.2
      docs/HEDGE_FUND_MASTER_PLAN.md 12.3(Fund Ledger), 5.9(결정론적 서비스)

불변식:
  1. 모든 분개는 차변 합계 = 대변 합계.
  2. Posted Journal은 수정·삭제하지 않는다. 반대 분개(Reversal)로만 정정한다.
  3. 같은 체결 이벤트로 분개가 두 번 생기지 않는다.
  4. Position과 Cash는 projection이다. 분개에서 재계산할 수 있어야 한다.
  5. 회계 수치는 체결·원장 이벤트에서만 나온다. LLM 문장에서 추출하지 않는다.

자체 점검: python departments/05-accounting-portfolio/ledger/ledger.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4
from typing import Any

# 트레이딩본부 contracts.py를 직접 참조한다. contracts/가 아직 공용 최상위 경계로
# 분리되지 않았기 때문이며(REPOSITORY_DEPARTMENT_STRUCTURE.md 4절), 그 전까지는
# 본부 간 의존 방향이 이 파일에 그대로 남는다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "02-trading" / "contracts"))

from contracts import Side

# ---------------------------------------------------------------------------
# 계정과목 (최소 세트)
# db/004_seed.sql의 account_code와 반드시 일치해야 한다.
#
# 자본금(3000)은 8개 최소 세트 밖이지만 초기 자본 납입 분개의 대변이 없으면
# 차변만 있는 분개가 되므로 함께 넣었다.
# ---------------------------------------------------------------------------

CASH = "1000"            # 현금
SECURITIES = "1100"      # 유가증권
RECEIVABLE = "1200"      # 미수금
PAYABLE = "2000"         # 미지급금
CAPITAL = "3000"         # 자본금
REALIZED_PNL = "4000"    # 실현손익
UNREALIZED_PNL = "4100"  # 평가손익
FEE_EXPENSE = "5000"     # 수수료비용 (거래 체결 비용 — TCA 가 쓰는 계정이다)
TAX_EXPENSE = "5100"     # 세금비용
# 보수 발생주의 (supabase/migrations/20260811000100_accounting_fee_accounts.sql).
# 거래 수수료(5000)와 섞지 않는다 - 거래를 안 한 날에도 보수는 발생하고,
# 그 둘을 합치면 집행 품질(TCA)과 운용 보수를 분리할 수 없다.
FEE_PAYABLE = "2100"     # 미지급보수 (확정 전까지 남는 부채)
MGMT_FEE_EXPENSE = "5200"        # 관리보수비용
PERF_FEE_EXPENSE = "5300"        # 성과보수비용

ACCOUNT_TYPES = {
    CASH: "asset", SECURITIES: "asset", RECEIVABLE: "asset",
    PAYABLE: "liability", FEE_PAYABLE: "liability", CAPITAL: "equity",
    REALIZED_PNL: "income", UNREALIZED_PNL: "income",
    FEE_EXPENSE: "expense", TAX_EXPENSE: "expense",
    MGMT_FEE_EXPENSE: "expense", PERF_FEE_EXPENSE: "expense",
}

ZERO = Decimal(0)


def decimal_str(value: Decimal) -> str:
    """수 하나에 문자열 하나. 금액을 밖으로 내보내는 모든 곳이 이걸 쓴다.

    DB numeric(30,10)에서 읽은 `20.0000000000`과 방금 계산한 `20`은 같은 수인데
    str()이 다르다. 그대로 두면 (1) 같은 스냅샷이 다른 content_hash를 갖고
    (2) 저장소 모드에 따라 API 응답 문자열이 달라진다. 둘 다 실제로 겪은 버그다.
    """
    normalized = value.normalize()
    return f"{normalized:f}"  # normalize()가 만드는 지수 표기(1E+2)를 펴준다


class LedgerError(Exception):
    """원장 불변식 위반. 절대 조용히 넘어가지 않는다."""


class PeriodClosedError(LedgerError):
    """마감된 회계기간에 분개하려 한 경우. 따로 잡을 수 있게 나눠 둔다.

    호출자가 구분해야 하는 실패다 - 분개가 틀린 것이 아니라 **날짜가 틀린 것**이고,
    고치는 방법도 다르다(당기로 다시 낸다).
    """


@dataclass(frozen=True)
class JournalLine:
    account_code: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    instrument_id: UUID | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.debit < 0 or self.credit < 0:
            raise LedgerError("차변·대변은 음수일 수 없습니다")
        if (self.debit > 0) == (self.credit > 0):
            raise LedgerError("한 줄은 차변이거나 대변이어야 합니다")


@dataclass
class Journal:
    journal_id: UUID
    fund_id: UUID
    book_id: UUID
    event_type: str
    source_event_id: str
    effective_at: datetime
    accounting_date: date
    lines: list[JournalLine]
    status: str = "posted"
    reversal_of: UUID | None = None
    created_by_service: str = "svc_ledger"
    trace_id: str = ""
    # 정정 사유. trace_id에 섞어 넣던 것을 분리했다 - accounting.journals.trace_id는
    # uuid 컬럼이라 사람이 쓴 사유가 거기 들어가면 저장 자체가 안 된다.
    reason: str = ""
    # Canonical envelope lineage is retained in the existing
    # accounting.journal_lines.metadata column by the repository.
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.lines:
            raise LedgerError("분개 라인이 없습니다")
        debit = sum(l.debit for l in self.lines)
        credit = sum(l.credit for l in self.lines)
        if debit != credit:
            raise LedgerError(f"불균형 분개: 차변 {debit} <> 대변 {credit}")


@dataclass
class Position:
    instrument_id: UUID
    quantity: Decimal = ZERO
    average_cost: Decimal = ZERO

    @property
    def cost_basis(self) -> Decimal:
        return self.quantity * self.average_cost


@dataclass
class Ledger:
    fund_id: UUID
    book_id: UUID
    journals: list[Journal] = field(default_factory=list)
    # 공식 NAV가 승인된 마지막 회계일. 이 날짜 이하로는 분개할 수 없다.
    # None이면 아직 한 번도 마감하지 않은 것이고, 그때는 전 기간이 열려 있다.
    # 값은 `close/nav_close.py::closed_through`가 `accounting.nav_runs`에서 읽는다 -
    # 여기서 정하지 않는다(마감은 승인의 결과이지 원장의 의견이 아니다).
    closed_through: date | None = None
    _posted_sources: set[tuple[str, str]] = field(default_factory=set)

    # -- Posting ------------------------------------------------------------

    def post(self, journal: Journal) -> Journal:
        """분개를 기록한다. 같은 원천 이벤트는 한 번만 반영된다.

        **마감된 회계기간에는 분개하지 않는다.** 공식 NAV가 승인된 날짜까지는
        확정된 과거이고, 거기에 분개가 하나 더 들어가면 이미 보고한 NAV가 조용히
        바뀐다. 정정은 당기(열린 기간)에 한다 - 그게 회계에서 소급 수정을 금지하는
        이유이자 `reverse()`가 반대 분개를 당기로 미는 이유다.
        """
        if self.closed_through is not None and journal.accounting_date <= self.closed_through:
            raise PeriodClosedError(
                f"{journal.accounting_date}는 마감된 기간입니다 "
                f"(공식 NAV 승인 {self.closed_through}까지). 정정은 당기 분개로 합니다"
            )
        key = (journal.event_type, journal.source_event_id)
        if key in self._posted_sources:
            # 이미 반영된 체결. 재처리에서 흔히 발생하며 두 번 잡히면 잔고가 틀어진다.
            existing = next(
                j for j in self.journals
                if (j.event_type, j.source_event_id) == key and j.reversal_of is None
            )
            # at-least-once 재전달에서 내용 해시는 같아야 같은 사실이다.
            incoming_hash = journal.metadata.get("content_hash")
            existing_hash = existing.metadata.get("content_hash")
            if incoming_hash and existing_hash and incoming_hash != existing_hash:
                raise LedgerError(
                    f"같은 체결 원천의 content_hash가 다릅니다: {journal.source_event_id}"
                )
            if (
                journal.metadata.get("canonical") is True
                and existing.metadata.get("evidence_class") == "fixture_only"
            ):
                raise LedgerError(
                    f"fixture-only Fill은 canonical event로 승격할 수 없습니다: "
                    f"{journal.source_event_id}"
                )
            return existing
        self.journals.append(journal)
        self._posted_sources.add(key)
        return journal

    def reverse(self, journal_id: UUID, reason: str) -> Journal:
        """반대 분개. 원본은 손대지 않는다 (팀 가이드 8.2)."""
        original = next((j for j in self.journals if j.journal_id == journal_id), None)
        if original is None:
            raise LedgerError("존재하지 않는 분개입니다")
        if original.reversal_of is not None:
            raise LedgerError("반대 분개를 다시 반대분개할 수 없습니다")
        if any(j.reversal_of == journal_id for j in self.journals):
            raise LedgerError("이미 반대분개된 분개입니다")

        flipped = [
            JournalLine(
                account_code=l.account_code, debit=l.credit, credit=l.debit,
                instrument_id=l.instrument_id,
                quantity=-l.quantity if l.quantity is not None else None,
                unit_price=l.unit_price,
            )
            for l in original.lines
        ]
        # 원본 기간이 마감됐으면 반대 분개는 **당기로 낸다.** 마감된 날짜에 되돌려
        # 놓으면 이미 승인된 NAV가 바뀐다 - 정정을 당기에 인식하는 것이 회계의 답이고,
        # 그래서 원본 회계일과 반대 분개 회계일이 다를 수 있다(그게 정상이다).
        now = datetime.now(timezone.utc)
        accounting_date = original.accounting_date
        if self.closed_through is not None and accounting_date <= self.closed_through:
            # 첫 열린 회계일로 민다. 오늘이 아니라 `마감일 + 1일`인 이유는 마감이
            # 오늘까지일 수 있어서다 - 그때 오늘로 밀면 다시 막힌 날짜가 된다.
            accounting_date = max(now.date(), self.closed_through + timedelta(days=1))
        rev = Journal(
            journal_id=uuid4(), fund_id=original.fund_id, book_id=original.book_id,
            event_type=f"{original.event_type}_reversal",
            source_event_id=f"{original.source_event_id}:rev",
            effective_at=now,
            accounting_date=accounting_date,
            lines=flipped, reversal_of=journal_id, reason=reason,
        )
        original.status = "reversed"
        # post()를 거친다. 저장소를 붙인 하위 클래스(PostgresLedger)가 post()만
        # 가로채도 반대 분개가 함께 저장되게 하기 위해서다.
        self.post(rev)
        return rev

    # -- 분개 규칙 -----------------------------------------------------------

    def _journal(self, event_type: str, source_event_id: str, when: datetime,
                 lines: list[JournalLine], *, trace_id: str = "",
                 metadata: dict[str, Any] | None = None) -> Journal:
        return Journal(
            journal_id=uuid4(), fund_id=self.fund_id, book_id=self.book_id,
            event_type=event_type, source_event_id=source_event_id,
            effective_at=when, accounting_date=when.date(), lines=lines,
            trace_id=trace_id, metadata=dict(metadata or {}),
        )

    def post_capital(self, amount: Decimal, when: datetime, source_event_id: str) -> Journal:
        """자본 납입.  차) 현금 / 대) 자본금"""
        return self.post(self._journal("capital_injection", source_event_id, when, [
            JournalLine(CASH, debit=amount),
            JournalLine(CAPITAL, credit=amount),
        ]))

    def post_fill(self, fill, side: Side, instrument_id: UUID,
                  position: Position, when: datetime | None = None,
                  *, trace_id: str = "",
                  metadata: dict[str, Any] | None = None,
                  settlement_date: date | None = None) -> Journal:
        """체결 하나를 균형 잡힌 분개로 변환한다 (팀 가이드 DoD 5번).

        매수:  차) 유가증권 + 수수료비용   대) 현금
        매도:  차) 현금 + 수수료 + 세금 + (손실)   대) 유가증권(원가) + (이익)

        `settlement_date`를 주면 **현금 자리에 미지급금/미수금이 들어간다.** 한국
        주식은 T+2 결제라 체결일과 현금 이동일이 다르고, 둘을 한 분개로 뭉치면
        원장의 현금이 "오늘 쓸 수 있는 돈"이 아니게 된다 - 그 값으로 주문을
        사이징하면 아직 들어오지 않은 매도 대금까지 쓴다. 현금은 결제일에
        `post_settlement()`가 옮긴다.

        NAV는 어느 쪽이든 같다. `PortfolioSnapshot.nav`가 현금에 미수를 더하고
        미지급을 빼기 때문이다 - 바뀌는 것은 NAV가 아니라 **가용 현금**이다.

        실현손익은 (체결가 - 평균원가) x 수량으로 계산하고, 수수료·세금은
        손익에 섞지 않고 별도 비용 계정으로 뺀다. 섞으면 나중에 TCA에서
        집행 비용과 전략 알파를 분리할 수 없다. 수수료·세금도 체결일에 비용으로
        인식하고 현금은 결제일에 나간다(발생주의).
        """
        when = when or fill.event_time
        notional = fill.quantity * fill.price
        source = fill.broker_fill_id or str(fill.fill_id)
        lines: list[JournalLine] = []
        # 매수는 갚을 돈(미지급금), 매도는 받을 돈(미수금)이다.
        settling = settlement_date is not None
        cash_account = CASH if not settling else (PAYABLE if side is Side.BUY else RECEIVABLE)

        def add(account: str, *, debit: Decimal = ZERO, credit: Decimal = ZERO, **kw) -> None:
            # 금액 0인 라인은 만들지 않는다. 수수료·세금·실현손익이 0인 경우가 흔하고
            # (매수에는 거래세가 없다), 0원 라인은 DB의
            # journal_lines_side_chk((debit > 0) <> (credit > 0))에서 거부된다.
            if debit > 0 or credit > 0:
                lines.append(JournalLine(account, debit=debit, credit=credit, **kw))

        if side is Side.BUY:
            add(SECURITIES, debit=notional, instrument_id=instrument_id,
                quantity=fill.quantity, unit_price=fill.price)
            add(FEE_EXPENSE, debit=fill.fee)
            add(cash_account, credit=notional + fill.fee)
        else:
            if position.quantity < fill.quantity:
                raise LedgerError(
                    f"보유({position.quantity})보다 많은 매도({fill.quantity})입니다"
                )
            cost = position.average_cost * fill.quantity
            realized = notional - cost

            add(cash_account, debit=notional - fill.fee - fill.tax)
            add(FEE_EXPENSE, debit=fill.fee)
            add(TAX_EXPENSE, debit=fill.tax)
            add(SECURITIES, credit=cost, instrument_id=instrument_id,
                quantity=-fill.quantity, unit_price=position.average_cost)
            # 실현손익에도 instrument_id를 단다. 이게 없으면 accounting.positions의
            # realized_pnl을 종목별로 채울 방법이 없어 0으로 남고, System of Record에
            # 틀린 0이 들어간다.
            add(REALIZED_PNL, credit=realized if realized > 0 else ZERO,
                debit=-realized if realized < 0 else ZERO,
                instrument_id=instrument_id)

        # **결제일은 분개에 저장하지 않는다.** accounting.journals 에 컬럼이 없고,
        # 있어도 중복이다 - T+2 는 `accounting_date` 에서 그대로 유도된다
        # (`treasury/settlement.py::settlement_date_for`). 저장하면 DB 에서 다시
        # 읽은 분개만 그 값을 잃어 재기동 후 미결제분이 조용히 사라진다.
        return self.post(self._journal("fill", source, when, lines,
                                       trace_id=trace_id, metadata=metadata))

    def post_settlement(self, journal: Journal, when: datetime) -> Journal:
        """미지급금/미수금을 현금으로 옮긴다. 체결 분개는 손대지 않는다.

        **결제일 판단은 여기서 하지 않는다** - 호출자(`treasury.settle_due`)가
        도래분만 골라 넘긴다. 같은 체결을 두 번 결제하지도 않는다:
        `source_event_id`가 `<원천>:settle` 하나뿐이라 `post()`의 멱등 검사가
        두 번째를 걸러낸다.
        """
        lines: list[JournalLine] = []
        for line in journal.lines:
            if line.account_code == PAYABLE:
                # 매수 결제: 차) 미지급금  대) 현금
                lines.append(JournalLine(PAYABLE, debit=line.credit))
                lines.append(JournalLine(CASH, credit=line.credit))
            elif line.account_code == RECEIVABLE:
                # 매도 결제: 차) 현금  대) 미수금
                lines.append(JournalLine(CASH, debit=line.debit))
                lines.append(JournalLine(RECEIVABLE, credit=line.debit))
        if not lines:
            raise LedgerError(f"미결제 잔액이 없는 분개입니다: {journal.journal_id}")
        return self.post(self._journal(
            "settlement", f"{journal.source_event_id}:settle", when, lines,
            trace_id=journal.trace_id,
            metadata={"settles_journal_id": str(journal.journal_id)},
        ))

    # -- Projection ----------------------------------------------------------

    def rebuild(self) -> tuple[dict[UUID, Position], Decimal]:
        """분개만으로 Position과 현금을 재계산한다 (팀 가이드 DoD 6번).

        저장된 projection이 이 결과와 다르면 projection이 틀린 것이다.
        """
        positions: dict[UUID, Position] = {}
        cash = ZERO

        for j in self.journals:
            for line in j.lines:
                if line.account_code == CASH:
                    cash += line.debit - line.credit
                elif line.account_code == SECURITIES and line.instrument_id is not None:
                    pos = positions.setdefault(line.instrument_id, Position(line.instrument_id))
                    qty = line.quantity or ZERO
                    if qty > 0:
                        # 매수: 이동평균 갱신
                        total_cost = pos.cost_basis + line.debit
                        pos.quantity += qty
                        pos.average_cost = total_cost / pos.quantity if pos.quantity else ZERO
                    else:
                        # 매도: 수량만 줄이고 평균단가는 유지한다
                        pos.quantity += qty
                        if pos.quantity == 0:
                            pos.average_cost = ZERO

        return {k: v for k, v in positions.items() if v.quantity != 0}, cash

    def trial_balance(self) -> dict[str, Decimal]:
        """계정별 잔액. 전체 합이 0이 아니면 원장이 깨진 것이다."""
        balances: dict[str, Decimal] = {}
        for j in self.journals:
            for line in j.lines:
                balances[line.account_code] = (
                    balances.get(line.account_code, ZERO) + line.debit - line.credit
                )
        return balances


if __name__ == "__main__":
    from dataclasses import dataclass as dc

    @dc
    class FakeFill:
        quantity: Decimal
        price: Decimal
        fee: Decimal
        tax: Decimal
        event_time: datetime
        broker_fill_id: str
        fill_id: UUID = field(default_factory=uuid4)

    now = datetime.now(timezone.utc)
    fund, book, stock = uuid4(), uuid4(), uuid4()
    led = Ledger(fund_id=fund, book_id=book)

    def raises(fn, why):
        try:
            fn()
        except LedgerError:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    # 1. 불균형 분개는 만들어지지 않는다
    raises(lambda: Journal(uuid4(), fund, book, "t", "s1", now, now.date(),
                           [JournalLine(CASH, debit=Decimal(100))]), "차변만 있는 분개")
    raises(lambda: JournalLine(CASH, debit=Decimal(1), credit=Decimal(1)), "차대 동시")
    raises(lambda: Journal(uuid4(), fund, book, "t", "s2", now, now.date(), []), "빈 분개")

    # 2. 초기 자본 10억 (db/004_seed.sql과 같은 값)
    led.post_capital(Decimal(1000000000), now, "seed_capital")
    _, cash = led.rebuild()
    assert cash == Decimal(1000000000)

    # 3. 매수 100주 @70,000 (수수료 1,050)
    buy = FakeFill(Decimal(100), Decimal(70000), Decimal(1050), ZERO, now, "bf_1")
    positions = {}
    led.post_fill(buy, Side.BUY, stock, Position(stock))
    positions, cash = led.rebuild()
    assert positions[stock].quantity == Decimal(100)
    assert positions[stock].average_cost == Decimal(70000)
    assert cash == Decimal(1000000000) - Decimal(7000000) - Decimal(1050)

    # 4. 멱등성 - 같은 체결 재처리 (DoD 3번)
    led.post_fill(buy, Side.BUY, stock, Position(stock))
    positions, cash2 = led.rebuild()
    assert positions[stock].quantity == Decimal(100), "중복 분개로 포지션이 두 배가 됨"
    assert cash2 == cash

    # 5. 매도 40주 @75,000 -> 실현이익 (75000-70000)*40 = 200,000
    sell = FakeFill(Decimal(40), Decimal(75000), Decimal(450), Decimal(4500), now, "sf_1")
    led.post_fill(sell, Side.SELL, stock, positions[stock])
    positions, cash3 = led.rebuild()
    assert positions[stock].quantity == Decimal(60)
    assert positions[stock].average_cost == Decimal(70000), "매도가 평균단가를 바꿨다"
    tb = led.trial_balance()
    assert tb[REALIZED_PNL] == Decimal(-200000), f"실현이익 오류: {tb[REALIZED_PNL]}"
    assert tb[FEE_EXPENSE] == Decimal(1500) and tb[TAX_EXPENSE] == Decimal(4500)
    assert cash3 == cash + Decimal(3000000) - Decimal(450) - Decimal(4500)

    # 6. 보유보다 많은 매도 차단
    raises(lambda: led.post_fill(
        FakeFill(Decimal(999), Decimal(75000), ZERO, ZERO, now, "sf_bad"),
        Side.SELL, stock, positions[stock]), "보유 초과 매도")

    # 7. 시산표 합계는 항상 0 (이중분개 불변식)
    assert sum(led.trial_balance().values()) == ZERO, "차대가 안 맞는다"

    # 8. 손실 매도도 균형이 맞는가
    led2 = Ledger(fund_id=fund, book_id=book)
    led2.post_capital(Decimal(10000000), now, "seed2")
    led2.post_fill(FakeFill(Decimal(10), Decimal(70000), ZERO, ZERO, now, "b2"),
                   Side.BUY, stock, Position(stock))
    p2, _ = led2.rebuild()
    led2.post_fill(FakeFill(Decimal(10), Decimal(60000), ZERO, ZERO, now, "s2"),
                   Side.SELL, stock, p2[stock])
    assert led2.trial_balance()[REALIZED_PNL] == Decimal(100000), "실현손실 부호 오류"
    assert sum(led2.trial_balance().values()) == ZERO
    p2, _ = led2.rebuild()
    assert stock not in p2, "전량 매도 후에도 포지션이 남았다"

    # 9. Reversal - 원본은 남고 효과만 상쇄된다
    target = next(j for j in led2.journals if j.event_type == "fill" and j.source_event_id == "b2")
    before = len(led2.journals)
    led2.reverse(target.journal_id, "브로커 정정")
    assert len(led2.journals) == before + 1, "원본을 지웠다"
    assert target.status == "reversed"
    assert sum(led2.trial_balance().values()) == ZERO
    raises(lambda: led2.reverse(target.journal_id, "again"), "이중 반대분개")

    # 10. 수수료·세금이 실현손익에 섞이지 않았는가
    tb = led.trial_balance()
    assert tb[REALIZED_PNL] == Decimal(-200000), "손익에 비용이 섞였다"

    print("ok - 원장 불변식 10개 점검 통과")
