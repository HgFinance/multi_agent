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
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

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
FEE_EXPENSE = "5000"     # 수수료비용
TAX_EXPENSE = "5100"     # 세금비용

ACCOUNT_TYPES = {
    CASH: "asset", SECURITIES: "asset", RECEIVABLE: "asset",
    PAYABLE: "liability", CAPITAL: "equity",
    REALIZED_PNL: "income", UNREALIZED_PNL: "income",
    FEE_EXPENSE: "expense", TAX_EXPENSE: "expense",
}

ZERO = Decimal(0)


class LedgerError(Exception):
    """원장 불변식 위반. 절대 조용히 넘어가지 않는다."""


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
    _posted_sources: set[tuple[str, str]] = field(default_factory=set)

    # -- Posting ------------------------------------------------------------

    def post(self, journal: Journal) -> Journal:
        """분개를 기록한다. 같은 원천 이벤트는 한 번만 반영된다."""
        key = (journal.event_type, journal.source_event_id)
        if key in self._posted_sources:
            # 이미 반영된 체결. 재처리에서 흔히 발생하며 두 번 잡히면 잔고가 틀어진다.
            return next(
                j for j in self.journals
                if (j.event_type, j.source_event_id) == key and j.reversal_of is None
            )
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
        rev = Journal(
            journal_id=uuid4(), fund_id=original.fund_id, book_id=original.book_id,
            event_type=f"{original.event_type}_reversal",
            source_event_id=f"{original.source_event_id}:rev",
            effective_at=datetime.now(timezone.utc),
            accounting_date=original.accounting_date,
            lines=flipped, reversal_of=journal_id, trace_id=reason,
        )
        original.status = "reversed"
        self.journals.append(rev)
        self._posted_sources.add((rev.event_type, rev.source_event_id))
        return rev

    # -- 분개 규칙 -----------------------------------------------------------

    def _journal(self, event_type: str, source_event_id: str, when: datetime,
                 lines: list[JournalLine]) -> Journal:
        return Journal(
            journal_id=uuid4(), fund_id=self.fund_id, book_id=self.book_id,
            event_type=event_type, source_event_id=source_event_id,
            effective_at=when, accounting_date=when.date(), lines=lines,
        )

    def post_capital(self, amount: Decimal, when: datetime, source_event_id: str) -> Journal:
        """자본 납입.  차) 현금 / 대) 자본금"""
        return self.post(self._journal("capital_injection", source_event_id, when, [
            JournalLine(CASH, debit=amount),
            JournalLine(CAPITAL, credit=amount),
        ]))

    def post_fill(self, fill, side: Side, instrument_id: UUID,
                  position: Position, when: datetime | None = None) -> Journal:
        """체결 하나를 균형 잡힌 분개로 변환한다 (팀 가이드 DoD 5번).

        매수:  차) 유가증권 + 수수료비용   대) 현금
        매도:  차) 현금 + 수수료 + 세금 + (손실)   대) 유가증권(원가) + (이익)

        실현손익은 (체결가 - 평균원가) x 수량으로 계산하고, 수수료·세금은
        손익에 섞지 않고 별도 비용 계정으로 뺀다. 섞으면 나중에 TCA에서
        집행 비용과 전략 알파를 분리할 수 없다.
        """
        when = when or fill.event_time
        notional = fill.quantity * fill.price
        source = fill.broker_fill_id or str(fill.fill_id)
        lines: list[JournalLine] = []

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
            add(CASH, credit=notional + fill.fee)
        else:
            if position.quantity < fill.quantity:
                raise LedgerError(
                    f"보유({position.quantity})보다 많은 매도({fill.quantity})입니다"
                )
            cost = position.average_cost * fill.quantity
            realized = notional - cost

            add(CASH, debit=notional - fill.fee - fill.tax)
            add(FEE_EXPENSE, debit=fill.fee)
            add(TAX_EXPENSE, debit=fill.tax)
            add(SECURITIES, credit=cost, instrument_id=instrument_id,
                quantity=-fill.quantity, unit_price=position.average_cost)
            add(REALIZED_PNL, credit=realized if realized > 0 else ZERO,
                debit=-realized if realized < 0 else ZERO)

        return self.post(self._journal("fill", source, when, lines))

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
