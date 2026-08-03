#!/usr/bin/env python3
"""F15: Portfolio / PnL - Cash, Position, 평가와 NAV.

소유: 도현 (회계·포트폴리오본부)
근거: docs/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 8.3, 9(D3)
      docs/HEDGE_FUND_MASTER_PLAN.md 12(원장·NAV), 19.11~19.13(Middle Office)

원장이 만든 사실(Position, Cash, 실현손익) 위에 **시장가 평가**를 얹어
미실현손익·NAV·비중을 만든다.

경계:
  - Position과 Cash를 여기서 계산하지 않는다. Ledger.rebuild()가 유일한 원천이고
    그 값은 체결·원장 Event에서만 나온다 (가이드 2장 원칙 4).
  - 가격을 수집하지 않는다. market-api가 준 Mark를 받아 쓴다.
  - 실현손익을 다시 계산하지 않는다. 원장의 실현손익 계정 잔액이 정답이다.
    두 곳에서 계산하면 반드시 갈라진다.

가격이 없거나 낡으면 **NAV를 만들지 않고 예외를 낸다.** 추정 가격으로 만든 NAV는
그 자체로 틀린 수치이며, 이 값이 주문 사이징(F11)으로 흘러가면 손실이 된다.
마스터플랜 25장의 "실패 시 거래 확대가 아니라 Entry 차단" 원칙 그대로다.

자체 점검: python departments/05-accounting-portfolio/portfolio/portfolio.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "ledger"))

from ledger import (
    FEE_EXPENSE,
    PAYABLE,
    REALIZED_PNL,
    RECEIVABLE,
    TAX_EXPENSE,
    ZERO,
    Ledger,
    Position,
)

# 시세가 이보다 낡으면 평가에 쓰지 않는다.
# ponytail: 장중 기준 5분이다. 종가 평가(D3 NAV Close)는 기준이 달라야 하므로
#           호출자가 max_staleness로 덮어쓴다. 값 자체는 나중에 yaml로 뺀다.
DEFAULT_MAX_STALENESS = timedelta(minutes=5)


class ValuationError(Exception):
    """평가를 확정할 수 없는 경우. 부분 결과를 반환하지 않는다."""


@dataclass(frozen=True)
class MarkPrice:
    """market-api가 준 평가 기준가.

    우리가 만들지 않는다. instrument_id와 as_of가 없으면 어느 시점 가격인지
    알 수 없어 Point-in-Time 재현이 깨진다.
    """

    instrument_id: UUID
    price: Decimal
    as_of: datetime
    source: str = "market-api"

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValuationError(f"평가가격이 0 이하입니다: {self.price}")

    def is_fresh_at(self, when: datetime, max_staleness: timedelta) -> bool:
        return timedelta(0) <= when - self.as_of <= max_staleness


@dataclass(frozen=True)
class PositionValuation:
    instrument_id: UUID
    quantity: Decimal
    average_cost: Decimal
    mark_price: Decimal
    mark_as_of: datetime

    @property
    def cost_basis(self) -> Decimal:
        return self.quantity * self.average_cost

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.mark_price

    @property
    def unrealized_pnl(self) -> Decimal:
        return self.market_value - self.cost_basis


@dataclass(frozen=True)
class PortfolioSnapshot:
    """어느 한 시점의 확정된 포트폴리오 상태.

    frozen이다. 스냅샷을 나중에 고쳐 쓰면 그 시점 재현이 불가능해진다.
    """

    fund_id: UUID
    book_id: UUID
    as_of: datetime
    cash: Decimal
    receivable: Decimal
    payable: Decimal
    realized_pnl: Decimal
    fees: Decimal
    taxes: Decimal
    positions: tuple[PositionValuation, ...] = ()

    @property
    def securities_value(self) -> Decimal:
        return sum((p.market_value for p in self.positions), ZERO)

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum((p.unrealized_pnl for p in self.positions), ZERO)

    @property
    def nav(self) -> Decimal:
        """순자산가치. 이 값이 F11의 주문 사이징 기준이 된다."""
        return self.cash + self.securities_value + self.receivable - self.payable

    @property
    def gross_exposure(self) -> Decimal:
        """|시장가치| 합. Long-only에서는 net과 같지만 계산은 분리해 둔다."""
        return sum((abs(p.market_value) for p in self.positions), ZERO)

    @property
    def net_exposure(self) -> Decimal:
        return self.securities_value

    def quantity_of(self, instrument_id: UUID) -> Decimal:
        """보유 수량. F11의 current_quantity 입력이다."""
        return next(
            (p.quantity for p in self.positions if p.instrument_id == instrument_id),
            ZERO,
        )

    def weight_of(self, instrument_id: UUID) -> Decimal:
        """NAV 대비 비중. NAV가 0이면 비중을 정의할 수 없다."""
        nav = self.nav
        if nav <= 0:
            raise ValuationError(f"NAV가 {nav}입니다. 비중을 계산할 수 없습니다")
        return next(
            (p.market_value / nav for p in self.positions if p.instrument_id == instrument_id),
            ZERO,
        )


def value_portfolio(
    ledger: Ledger,
    marks: dict[UUID, MarkPrice],
    as_of: datetime,
    max_staleness: timedelta = DEFAULT_MAX_STALENESS,
) -> PortfolioSnapshot:
    """원장 상태에 시장가를 얹어 스냅샷을 만든다.

    보유 중인 모든 종목에 신선한 Mark가 있어야 한다. 하나라도 없으면 NAV 자체를
    만들지 않는다 - 일부만 평가한 NAV는 틀린 NAV이고, 그걸로 주문을 내면
    비중 계산이 조용히 어긋난다.
    """
    positions, cash = ledger.rebuild()

    missing: list[UUID] = []
    stale: list[UUID] = []
    valued: list[PositionValuation] = []

    for instrument_id, pos in sorted(positions.items(), key=lambda kv: str(kv[0])):
        mark = marks.get(instrument_id)
        if mark is None:
            missing.append(instrument_id)
            continue
        if not mark.is_fresh_at(as_of, max_staleness):
            stale.append(instrument_id)
            continue
        valued.append(
            PositionValuation(
                instrument_id=instrument_id,
                quantity=pos.quantity,
                average_cost=pos.average_cost,
                mark_price=mark.price,
                mark_as_of=mark.as_of,
            )
        )

    if missing or stale:
        raise ValuationError(
            f"평가 불가 - 가격 없음 {len(missing)}건, 낡은 가격 {len(stale)}건. "
            f"missing={[str(i) for i in missing]} stale={[str(i) for i in stale]}"
        )

    tb = ledger.trial_balance()
    return PortfolioSnapshot(
        fund_id=ledger.fund_id,
        book_id=ledger.book_id,
        as_of=as_of,
        cash=cash,
        receivable=tb.get(RECEIVABLE, ZERO),
        payable=-tb.get(PAYABLE, ZERO),   # 부채는 대변 잔액이라 부호를 뒤집는다
        # 손익 계정도 대변이 이익이다. 원장에서 가져오고 여기서 다시 계산하지 않는다.
        realized_pnl=-tb.get(REALIZED_PNL, ZERO),
        fees=tb.get(FEE_EXPENSE, ZERO),
        taxes=tb.get(TAX_EXPENSE, ZERO),
        positions=tuple(valued),
    )


if __name__ == "__main__":
    from dataclasses import dataclass as dc
    from datetime import timezone
    from uuid import uuid4

    sys.path.insert(0, str(_HERE.parent.parent / "02-trading" / "contracts"))
    from contracts import Side

    D = Decimal
    now = datetime.now(timezone.utc)

    @dc
    class FakeFill:
        quantity: Decimal
        price: Decimal
        fee: Decimal
        tax: Decimal
        event_time: datetime
        broker_fill_id: str
        fill_id: UUID = field(default_factory=uuid4)

    fund, book = uuid4(), uuid4()
    AAA, BBB = uuid4(), uuid4()

    def fresh(instrument, price, minutes_old=0):
        return MarkPrice(instrument, D(price), now - timedelta(minutes=minutes_old))

    def raises(fn, why):
        try:
            fn()
        except ValuationError:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    # 1. 자본만 있는 원장 - NAV는 현금과 같다
    led = Ledger(fund_id=fund, book_id=book)
    led.post_capital(D("100000000"), now, "cap_1")
    snap = value_portfolio(led, {}, now)
    assert snap.nav == D("100000000") and snap.cash == D("100000000")
    assert snap.positions == () and snap.unrealized_pnl == ZERO

    # 2. 매수 직후 - 체결가로 평가하면 NAV는 수수료만큼 줄어든다
    buy = FakeFill(D("100"), D("70000"), D("1050"), ZERO, now, "bf_1")
    led.post_fill(buy, Side.BUY, AAA, Position(AAA))
    snap = value_portfolio(led, {AAA: fresh(AAA, "70000")}, now)
    assert snap.cash == D("100000000") - D("7000000") - D("1050")
    assert snap.securities_value == D("7000000")
    assert snap.unrealized_pnl == ZERO, "체결 직후 평가손익이 0이 아니다"
    assert snap.nav == D("100000000") - D("1050"), snap.nav
    assert snap.fees == D("1050")

    # 3. 가격이 오르면 미실현손익이 NAV에 반영된다. 현금은 그대로다
    before_cash = snap.cash
    snap = value_portfolio(led, {AAA: fresh(AAA, "77000")}, now)
    assert snap.unrealized_pnl == D("700000")
    assert snap.cash == before_cash, "평가가 현금을 바꿨다"
    assert snap.nav == D("100000000") - D("1050") + D("700000")
    assert snap.realized_pnl == ZERO, "팔지도 않았는데 실현손익이 생겼다"

    # 4. 보유 종목에 가격이 없으면 NAV를 만들지 않는다
    raises(lambda: value_portfolio(led, {}, now), "가격 없는 보유 종목")
    raises(lambda: value_portfolio(led, {AAA: fresh(AAA, "77000", minutes_old=30)}, now),
           "30분 낡은 가격")
    raises(lambda: MarkPrice(AAA, D("0"), now), "0원 평가가격")
    # 미래 가격도 거부한다 - Point-in-Time 위반이다
    raises(lambda: value_portfolio(led, {AAA: MarkPrice(AAA, D("77000"), now + timedelta(minutes=1))}, now),
           "분석 시점 이후의 가격")

    # 5. 매도 후 실현손익은 원장에서 온다. 여기서 다시 계산하지 않는다
    positions, _ = led.rebuild()
    sell = FakeFill(D("40"), D("77000"), D("462"), D("4620"), now, "bf_2")
    led.post_fill(sell, Side.SELL, AAA, positions[AAA])
    snap = value_portfolio(led, {AAA: fresh(AAA, "77000")}, now)
    assert snap.realized_pnl == D("280000"), snap.realized_pnl   # (77000-70000)*40
    assert snap.quantity_of(AAA) == D("60")
    assert snap.unrealized_pnl == D("420000")                    # 남은 60주
    assert snap.taxes == D("4620")

    # 6. NAV 항등식 - 어떤 시점에도 성립해야 한다
    assert snap.nav == snap.cash + snap.securities_value + snap.receivable - snap.payable

    # 7. 비중은 NAV 기준이고, 보유하지 않은 종목은 0이다
    w = snap.weight_of(AAA)
    assert w == snap.positions[0].market_value / snap.nav
    assert snap.weight_of(BBB) == ZERO
    assert snap.quantity_of(BBB) == ZERO, "보유하지 않은 종목에 수량이 있다"

    # 8. 전량 매도하면 평가 대상에서 빠진다. 남은 가격이 있어도 마찬가지다
    positions, _ = led.rebuild()
    led.post_fill(FakeFill(D("60"), D("77000"), D("693"), D("6930"), now, "bf_3"),
                  Side.SELL, AAA, positions[AAA])
    snap = value_portfolio(led, {AAA: fresh(AAA, "77000")}, now)
    assert snap.positions == () and snap.securities_value == ZERO
    assert snap.quantity_of(AAA) == ZERO
    assert snap.nav == snap.cash, "포지션이 없는데 NAV와 현금이 다르다"

    # 9. 종목이 둘이면 Exposure가 합산된다. Long-only라 gross와 net이 같다
    led2 = Ledger(fund_id=fund, book_id=book)
    led2.post_capital(D("100000000"), now, "cap_2")
    led2.post_fill(FakeFill(D("100"), D("70000"), ZERO, ZERO, now, "b1"),
                   Side.BUY, AAA, Position(AAA))
    led2.post_fill(FakeFill(D("50"), D("20000"), ZERO, ZERO, now, "b2"),
                   Side.BUY, BBB, Position(BBB))
    snap2 = value_portfolio(led2, {AAA: fresh(AAA, "70000"), BBB: fresh(BBB, "20000")}, now)
    assert snap2.securities_value == D("8000000")
    assert snap2.gross_exposure == snap2.net_exposure == D("8000000")
    assert len(snap2.positions) == 2
    assert snap2.weight_of(AAA) + snap2.weight_of(BBB) < 1, "비중 합이 NAV를 넘었다"

    # 10. 한 종목만 가격이 없어도 전체 NAV를 만들지 않는다 (부분 평가 금지)
    raises(lambda: value_portfolio(led2, {AAA: fresh(AAA, "70000")}, now),
           "종목 하나의 가격 누락")

    # 11. 스냅샷은 불변이다. 나중에 고치면 그 시점 재현이 깨진다
    try:
        snap2.cash = D("1")  # type: ignore[misc]
        raise AssertionError("스냅샷이 수정됐다")
    except (AttributeError, TypeError):
        pass

    # 12. F11 배선 - 스냅샷이 Intent Builder의 두 입력을 그대로 준다
    assert isinstance(snap2.nav, Decimal) and isinstance(snap2.quantity_of(AAA), Decimal)
    assert snap2.nav > 0 and snap2.quantity_of(AAA) == D("100")

    print("ok - Portfolio/NAV 12개 영역 점검 통과")
