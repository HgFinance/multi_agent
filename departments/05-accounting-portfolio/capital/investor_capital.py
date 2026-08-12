#!/usr/bin/env python3
"""투자자 설정·환매와 좌수(Unit) 회계. **NAV는 총액이고, 투자자 몫은 좌당이다.**

소유: 도현 (회계·포트폴리오본부)
근거: docs/HEDGE_FUND_MASTER_PLAN.md 5.7(Fund/Book 계층), 12.4(NAV Close)
      close/nav_close.py (확정 NAV), fees/fee_accrual.py (보수 차감 후 NAV)

지금까지 원장에는 자본이 `post_capital()` 한 줄로만 들어왔다. 그건 "돈이 들어왔다"
까지고, **누가 얼마의 몫을 갖는지**는 없었다. 투자자가 둘 이상이면 그 정보 없이는
NAV를 나눌 수 없다.

  설정(subscription):  차) 현금 1000    대) 자본금 3000   + 좌수 발행
  환매(redemption):    차) 자본금 3000  대) 현금 1000     + 좌수 소각

**좌수는 확정 NAV로만 거래한다.** 미확정(Preliminary) NAV로 설정을 받으면 기존
투자자와 신규 투자자 사이에서 몫이 조용히 옮겨간다 - 나중에 그 NAV가 정정돼도
이미 발행된 좌수는 그대로다. 그래서 `dealing_price()`는 **승인된 공식 NAV**만
쓰고, 없으면 거래를 거부한다(계산해서 대신 채우지 않는다).

**좌수 원장을 새 테이블로 만들지 않았다.** 분개 metadata 에 좌수와 투자자를
싣고 거기서 집계한다 - 자본 이동과 좌수 발행은 같은 사건이라 두 저장소에 나눠
두면 어긋날 수 있고, 어긋나면 어느 쪽이 참인지 정할 방법이 없다.

**여기서 하지 않는 것:** Equalization(투자자별 성과보수 정산), Series 회계,
Lock-up·Gate·Side Pocket. 전부 Fund Mandate 소관이고 아직 미확정이다.

자체 점검: python departments/05-accounting-portfolio/capital/investor_capital.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from uuid import UUID

_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parent / "ledger", _HERE.parent / "portfolio"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ledger import (  # noqa: E402
    CAPITAL, CASH, ZERO, Journal, JournalLine, Ledger, decimal_str,
)

# 좌수 소수점. 금액을 좌수로 나눌 때 남는 끝자리를 버려 **발행 좌수가 실제 납입액을
# 넘지 않게** 한다. 반올림하면 마지막 투자자가 내지 않은 몫을 받는다.
UNIT_QUANTUM = Decimal("0.00000001")
# 좌당 가격 소수점. 원 단위로 자르면 대형 펀드에서 오차가 커진다.
PRICE_QUANTUM = Decimal("0.0001")

SUBSCRIPTION = "investor_subscription"
REDEMPTION = "investor_redemption"


class CapitalError(Exception):
    """자본거래를 처리할 수 없는 경우. 좌수나 가격을 추정해서 진행하지 않는다."""


@dataclass(frozen=True)
class UnitHolding:
    investor_id: str
    units: Decimal


def _unit_rows(ledger: Ledger) -> list[tuple[str, Decimal]]:
    """자본거래 분개에서 (투자자, 좌수 증감)을 뽑는다. 환매는 음수다."""
    rows: list[tuple[str, Decimal]] = []
    for journal in ledger.journals:
        if journal.event_type not in (SUBSCRIPTION, REDEMPTION):
            continue
        if journal.status == "reversed":
            continue
        meta = journal.metadata or {}
        investor = str(meta.get("investor_id", ""))
        units = Decimal(str(meta.get("units", "0")))
        if not investor or units == 0:
            # 좌수 없는 자본 분개는 집계에 넣지 않는다. `post_capital()`로 들어온
            # 초기 시딩이 그런 모양이고, 그건 투자자 지분이 아니라 운용사 출자다.
            continue
        rows.append((investor, units if journal.event_type == SUBSCRIPTION else -units))
    return rows


def units_outstanding(ledger: Ledger) -> Decimal:
    """발행 좌수 총계."""
    return sum((units for _, units in _unit_rows(ledger)), ZERO)


def holdings(ledger: Ledger) -> list[UnitHolding]:
    """투자자별 보유 좌수. 0좌는 빼고 투자자 id 순으로 돌려준다."""
    totals: dict[str, Decimal] = {}
    for investor, units in _unit_rows(ledger):
        totals[investor] = totals.get(investor, ZERO) + units
    return [UnitHolding(investor, units) for investor, units in sorted(totals.items())
            if units != 0]


def units_of(ledger: Ledger, investor_id: str) -> Decimal:
    return next((h.units for h in holdings(ledger) if h.investor_id == investor_id), ZERO)


def nav_per_unit(nav: Decimal, units: Decimal) -> Decimal:
    """좌당 순자산. 좌수가 0이면 정의되지 않는다 - 0으로 나누지도, 1로 채우지도 않는다."""
    if units <= 0:
        raise CapitalError("발행 좌수가 0입니다. 좌당 NAV가 정의되지 않습니다")
    if nav <= 0:
        raise CapitalError(f"NAV가 {nav}입니다. 좌당 NAV를 만들 수 없습니다")
    return (nav / units).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)


def dealing_price(repo, fund_id: UUID, ledger: Ledger, *,
                  initial_price: Decimal = Decimal("1000")) -> Decimal:
    """거래 기준가. **승인된 공식 NAV에서만 나온다.**

    아직 좌수가 없으면 최초 설정이므로 Mandate 의 최초 좌당 가격을 쓴다. 좌수가
    있는데 승인된 공식 NAV가 없으면 거래를 거부한다 - 그 상태에서 좌수를 발행하면
    기존 투자자의 몫이 확정되지 않은 수치로 희석된다.
    """
    outstanding = units_outstanding(ledger)
    if outstanding == 0:
        return initial_price
    if repo is None:
        raise CapitalError("확정 NAV를 조회할 수 없습니다 (저장소 없음)")
    with repo.cursor() as cur:
        cur.execute(
            """
            select total_nav from accounting.nav_runs
             where fund_id = %s and run_type = 'OFFICIAL' and status = 'APPROVED'
             order by valuation_date desc limit 1
            """,
            (fund_id,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        raise CapitalError(
            "승인된 공식 NAV가 없습니다. 미확정 NAV로 좌수를 발행하지 않습니다"
        )
    return nav_per_unit(Decimal(str(row[0])), outstanding)


def subscribe(ledger: Ledger, *, investor_id: str, amount: Decimal,
              price: Decimal, when: datetime, reference: str,
              on: date | None = None) -> Journal:
    """설정. 납입 현금만큼 좌수를 발행한다.

    좌수는 **내림**이다. 올림하면 납입액보다 많은 몫을 주게 되고, 그 차이는 기존
    투자자 주머니에서 나온다.
    """
    if amount <= 0:
        raise CapitalError(f"납입액이 0 이하입니다: {amount}")
    if price <= 0:
        raise CapitalError(f"거래 기준가가 0 이하입니다: {price}")
    if not investor_id:
        raise CapitalError("투자자 식별자가 없습니다")
    units = (amount / price).quantize(UNIT_QUANTUM, rounding=ROUND_DOWN)
    if units <= 0:
        raise CapitalError(f"발행 좌수가 0입니다 (납입 {amount} / 기준가 {price})")
    when_date = on or when.date()
    return ledger.post(ledger._journal(
        SUBSCRIPTION, f"sub:{reference}", when,
        [JournalLine(CASH, debit=amount), JournalLine(CAPITAL, credit=amount)],
        metadata={"investor_id": investor_id, "units": str(units),
                  "price": str(price), "accounting_date": when_date.isoformat()},
    ))


def redeem(ledger: Ledger, *, investor_id: str, units: Decimal,
           price: Decimal, when: datetime, reference: str,
           on: date | None = None) -> Journal:
    """환매. 보유 좌수를 소각하고 그만큼 현금을 지급한다.

    보유보다 많이 환매할 수 없다 - 없는 몫을 돈으로 바꿔주면 그 돈은 남의 것이다.
    """
    if units <= 0:
        raise CapitalError(f"환매 좌수가 0 이하입니다: {units}")
    if price <= 0:
        raise CapitalError(f"거래 기준가가 0 이하입니다: {price}")
    held = units_of(ledger, investor_id)
    if units > held:
        raise CapitalError(f"보유 {held}좌보다 많은 {units}좌를 환매할 수 없습니다")
    amount = (units * price).quantize(Decimal("1"), rounding=ROUND_DOWN)
    _, cash = ledger.rebuild()
    if amount > cash:
        # 현금이 모자라면 자산을 팔아야 한다. 그건 거래 결정이라 우리 권한이 아니다.
        raise CapitalError(
            f"현금 {cash}로 환매대금 {amount}을 지급할 수 없습니다. "
            f"유동화가 먼저입니다(트레이딩본부 결정)"
        )
    when_date = on or when.date()
    return ledger.post(ledger._journal(
        REDEMPTION, f"red:{reference}", when,
        [JournalLine(CAPITAL, debit=amount), JournalLine(CASH, credit=amount)],
        metadata={"investor_id": investor_id, "units": str(units),
                  "price": str(price), "accounting_date": when_date.isoformat()},
    ))


def capital_summary(ledger: Ledger, nav: Decimal | None = None) -> dict:
    """화면·보고서용 요약. 좌당 NAV는 NAV를 줄 때만 계산한다(없으면 None)."""
    outstanding = units_outstanding(ledger)
    price = None
    if nav is not None and outstanding > 0 and nav > 0:
        price = decimal_str(nav_per_unit(nav, outstanding))
    # 금액·좌수 문자열은 `decimal_str` 하나가 만든다 - 같은 수가 저장소에 따라
    # "15000"과 "15000.00000000"으로 갈리면 해시와 화면 표시가 같이 흔들린다.
    return {
        "units_outstanding": decimal_str(outstanding),
        "nav_per_unit": price,
        "investors": [{"investor_id": h.investor_id, "units": decimal_str(h.units)}
                      for h in holdings(ledger)],
        "decided_by": "deterministic",
    }


if __name__ == "__main__":
    from datetime import timezone
    from uuid import uuid4

    D = Decimal
    ok = lambda label: print(f"  {label:26} OK")  # noqa: E731
    now = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)

    def raises(fn, why, exc=CapitalError):
        try:
            fn()
        except exc:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    def fresh() -> Ledger:
        return Ledger(fund_id=uuid4(), book_id=uuid4())

    # 1. 최초 설정 - 좌수가 없으면 Mandate 최초 가격으로 발행한다
    led = fresh()
    assert dealing_price(None, led.fund_id, led) == D("1000")
    subscribe(led, investor_id="inv-a", amount=D("10000000"), price=D("1000"),
              when=now, reference="a-1")
    assert units_outstanding(led) == D("10000"), units_outstanding(led)
    _, cash = led.rebuild()
    assert cash == D("10000000"), cash
    ok("최초 설정")

    # 2. 좌당 NAV. 10억/10000좌 = 100,000원
    assert nav_per_unit(D("1000000000"), D("10000")) == D("100000.0000")
    raises(lambda: nav_per_unit(D("1000000000"), ZERO), "좌수 0에 좌당 NAV")
    raises(lambda: nav_per_unit(ZERO, D("10000")), "NAV 0에 좌당 NAV")
    ok("좌당 NAV")

    # 3. 좌수는 내림이다 - 납입액보다 많은 몫을 주지 않는다
    odd = fresh()
    subscribe(odd, investor_id="inv-b", amount=D("1000"), price=D("3"),
              when=now, reference="b-1")
    assert units_of(odd, "inv-b") == D("333.33333333"), units_of(odd, "inv-b")
    ok("좌수 내림")

    # 4. 두 번째 투자자는 그때의 기준가로 들어온다 - 기존 투자자 몫이 안 변한다
    two = fresh()
    subscribe(two, investor_id="inv-a", amount=D("10000000"), price=D("1000"),
              when=now, reference="a-1")
    subscribe(two, investor_id="inv-b", amount=D("12000000"), price=D("1200"),
              when=now, reference="b-1")
    assert units_of(two, "inv-a") == D("10000") and units_of(two, "inv-b") == D("10000")
    assert units_outstanding(two) == D("20000")
    ok("복수 투자자")

    # 5. 환매는 보유 좌수를 넘을 수 없다
    raises(lambda: redeem(two, investor_id="inv-a", units=D("10001"), price=D("1200"),
                          when=now, reference="a-r"), "보유 초과 환매")
    raises(lambda: redeem(two, investor_id="inv-c", units=D("1"), price=D("1200"),
                          when=now, reference="c-r"), "보유 0인 투자자 환매")
    ok("보유 초과 차단")

    # 6. 환매하면 좌수가 줄고 현금이 나간다
    redeem(two, investor_id="inv-a", units=D("5000"), price=D("1200"),
           when=now, reference="a-r")
    assert units_of(two, "inv-a") == D("5000")
    assert units_outstanding(two) == D("15000")
    _, cash = two.rebuild()
    assert cash == D("22000000") - D("6000000"), cash
    ok("환매 소각")

    # 7. 현금이 모자라면 환매하지 않는다 - 자산 매각은 우리 권한이 아니다
    poor = fresh()
    subscribe(poor, investor_id="inv-a", amount=D("1000000"), price=D("1000"),
              when=now, reference="p-1")
    poor.post(poor._journal("spend", "buy-something", now,
                            [JournalLine("1100", debit=D("900000")),
                             JournalLine(CASH, credit=D("900000"))]))
    raises(lambda: redeem(poor, investor_id="inv-a", units=D("1000"), price=D("1000"),
                          when=now, reference="p-r"), "현금 부족 환매")
    ok("현금 부족 차단")

    # 8. 잘못된 입력은 좌수를 만들지 않는다
    raises(lambda: subscribe(fresh(), investor_id="", amount=D("1"), price=D("1"),
                             when=now, reference="x"), "투자자 없음")
    raises(lambda: subscribe(fresh(), investor_id="i", amount=ZERO, price=D("1"),
                             when=now, reference="x"), "납입 0")
    # 좌수 소수점(1e-8) 아래로 떨어지는 납입은 좌수가 0이라 발행하지 않는다.
    # 돈만 받고 몫을 안 주는 분개가 되기 때문이다.
    raises(lambda: subscribe(fresh(), investor_id="i", amount=D("0.000001"),
                             price=D("1000"), when=now, reference="x"), "발행 좌수 0")
    ok("입력 검증")

    # 9. `post_capital()` 시딩은 투자자 좌수가 아니다 - 집계에 섞이지 않는다
    seeded = fresh()
    seeded.post_capital(D("500000000"), now, "seed")
    assert units_outstanding(seeded) == ZERO, "운용사 출자가 좌수로 잡혔다"
    ok("시딩 != 좌수")

    # 10. 승인된 공식 NAV가 없으면 추가 설정을 거부한다
    class _NoApproved:
        def cursor(self):
            from contextlib import contextmanager

            @contextmanager
            def _c():
                class _Cur:
                    def execute(self, *_a, **_k): pass
                    def fetchone(self): return None
                yield _Cur()
            return _c()

    raises(lambda: dealing_price(_NoApproved(), two.fund_id, two),
           "확정 NAV 없이 좌수 발행")
    ok("미확정 NAV 차단")

    # 11. 요약은 수치를 만들지 않는다 - NAV를 안 주면 좌당 NAV도 없다
    summary = capital_summary(two)
    assert summary["nav_per_unit"] is None and summary["units_outstanding"] == "15000"
    priced = capital_summary(two, D("18000000"))
    assert priced["nav_per_unit"] == "1200", priced
    assert [i["investor_id"] for i in priced["investors"]] == ["inv-a", "inv-b"]
    ok("요약")

    print("ok - 투자자 자본거래 11개 영역 점검 통과 "
          "(확정 NAV로만 거래, 좌수 내림, 보유·현금 초과 차단)")
