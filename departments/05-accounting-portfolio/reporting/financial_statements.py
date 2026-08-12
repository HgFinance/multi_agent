#!/usr/bin/env python3
"""재무상태표·손익계산서. **시산표를 재분류할 뿐, 새 수치를 만들지 않는다.**

소유: 도현 (회계·포트폴리오본부)
근거: docs/HEDGE_FUND_MASTER_PLAN.md 12.3(Fund Ledger), 19.16(Reporting)
      계정과목과 유형은 `ledger.ACCOUNT_TYPES` 하나가 소유한다

지금까지 밖으로 나가는 것은 NAV와 일일 손익뿐이었다. 그건 운용 성과이고,
**재무제표는 그 성과가 어느 계정에서 왔는지**를 보여준다. 둘은 다른 질문이라
NAV만으로는 "현금이 왜 이만큼인지", "부채가 뭔지"를 답할 수 없다.

  재무상태표:  자산 = 부채 + 자본 + 당기순이익
  손익계산서:  수익 - 비용 = 당기순이익

**항등식이 안 맞으면 예외다.** 맞춰서 내보내지 않는다 - 안 맞는다는 것은 원장이
깨졌다는 뜻이고, 그 상태의 재무제표는 숫자가 아니라 거짓말이다.

**평가손익(4100)은 미실현이다.** 재무상태표에는 이미 유가증권 평가액에 반영돼
있지 않다 - 우리 원장은 취득원가로 유가증권을 들고 미실현손익을 분개하지 않기
때문이다(평가는 `portfolio.value_portfolio`가 스냅샷에서만 한다). 그래서 이
재무제표는 **취득원가 기준**이고, 시가 기준 순자산은 NAV 쪽을 본다. 둘을 한 표에
섞으면 어느 기준인지 알 수 없어진다.

자체 점검: python departments/05-accounting-portfolio/reporting/financial_statements.py
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent / "ledger") not in sys.path:
    sys.path.insert(0, str(_HERE.parent / "ledger"))

from ledger import ACCOUNT_TYPES, ZERO, Ledger, decimal_str  # noqa: E402

# 계정유형별 정상 잔액 방향. 차변이 정상이면 +1, 대변이 정상이면 -1.
# 시산표는 (차변 - 대변)이라 대변 정상 계정은 음수로 나온다 - 표에는 양수로 싣는다.
_NORMAL_SIDE = {"asset": 1, "expense": 1, "liability": -1, "equity": -1, "income": -1}


class StatementError(Exception):
    """재무제표를 만들 수 없는 경우. 항등식을 맞춰서 내보내지 않는다."""


def _by_type(ledger: Ledger, until: date | None = None) -> dict[str, dict[str, Decimal]]:
    """계정유형 -> {계정코드: 표시 잔액}. `until`을 주면 그날까지의 분개만 본다."""
    grouped: dict[str, dict[str, Decimal]] = {}
    for journal in ledger.journals:
        if until is not None and journal.accounting_date > until:
            continue
        for line in journal.lines:
            kind = ACCOUNT_TYPES.get(line.account_code)
            if kind is None:
                raise StatementError(
                    f"유형을 모르는 계정입니다: {line.account_code}. "
                    f"ledger.ACCOUNT_TYPES 에 추가해야 합니다"
                )
            bucket = grouped.setdefault(kind, {})
            signed = (line.debit - line.credit) * _NORMAL_SIDE[kind]
            bucket[line.account_code] = bucket.get(line.account_code, ZERO) + signed
    return grouped


def _total(bucket: dict[str, Decimal]) -> Decimal:
    return sum(bucket.values(), ZERO)


def income_statement(ledger: Ledger, *, until: date | None = None) -> dict:
    """수익 - 비용 = 당기순이익. 취득원가 기준이며 미실현 평가는 들어 있지 않다."""
    grouped = _by_type(ledger, until)
    income = grouped.get("income", {})
    expense = grouped.get("expense", {})
    revenue_total = _total(income)
    expense_total = _total(expense)
    return {
        "as_of": until.isoformat() if until else None,
        "revenue": {code: decimal_str(v) for code, v in sorted(income.items())},
        "expense": {code: decimal_str(v) for code, v in sorted(expense.items())},
        "revenue_total": decimal_str(revenue_total),
        "expense_total": decimal_str(expense_total),
        "net_income": decimal_str(revenue_total - expense_total),
        "basis": "historical_cost",
        "decided_by": "deterministic",
    }


def balance_sheet(ledger: Ledger, *, until: date | None = None) -> dict:
    """자산 = 부채 + 자본 + 당기순이익. 안 맞으면 예외."""
    grouped = _by_type(ledger, until)
    assets = grouped.get("asset", {})
    liabilities = grouped.get("liability", {})
    equity = grouped.get("equity", {})
    net_income = (_total(grouped.get("income", {}))
                  - _total(grouped.get("expense", {})))

    asset_total = _total(assets)
    liability_total = _total(liabilities)
    equity_total = _total(equity)
    right = liability_total + equity_total + net_income
    if asset_total != right:
        raise StatementError(
            f"재무상태표가 맞지 않습니다: 자산 {decimal_str(asset_total)} != "
            f"부채 {decimal_str(liability_total)} + 자본 {decimal_str(equity_total)} + "
            f"당기순이익 {decimal_str(net_income)}"
        )
    return {
        "as_of": until.isoformat() if until else None,
        "assets": {code: decimal_str(v) for code, v in sorted(assets.items())},
        "liabilities": {code: decimal_str(v) for code, v in sorted(liabilities.items())},
        "equity": {code: decimal_str(v) for code, v in sorted(equity.items())},
        "asset_total": decimal_str(asset_total),
        "liability_total": decimal_str(liability_total),
        "equity_total": decimal_str(equity_total),
        "net_income": decimal_str(net_income),
        "balanced": True,
        "basis": "historical_cost",
        "decided_by": "deterministic",
    }


def statements(ledger: Ledger, *, until: date | None = None) -> dict:
    """두 표를 한 번에. 보고·화면이 부르는 입구다."""
    return {"balance_sheet": balance_sheet(ledger, until=until),
            "income_statement": income_statement(ledger, until=until)}


if __name__ == "__main__":
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal as D
    from uuid import uuid4

    sys.path.insert(0, str(_HERE.parent.parent / "02-trading" / "contracts"))
    sys.path.insert(0, str(_HERE.parent / "close"))
    from contracts import Side  # noqa: E402

    from ledger import CASH, Position, SECURITIES  # noqa: E402

    ok = lambda label: print(f"  {label:26} OK")  # noqa: E731
    now = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)

    class FakeFill:
        def __init__(self, qty, price, fee="1000", tax="0", bid="bf-1"):
            self.quantity, self.price = D(qty), D(price)
            self.fee, self.tax = D(fee), D(tax)
            self.event_time, self.broker_fill_id = now, bid
            self.fill_id = uuid4()

    def raises(fn, why, exc=StatementError):
        try:
            fn()
        except exc:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    led = Ledger(fund_id=uuid4(), book_id=uuid4())
    led.post_capital(D("10000000"), now, "seed")

    # 1. 자본만 있는 상태 - 자산 = 자본
    bs = balance_sheet(led)
    assert bs["asset_total"] == "10000000" and bs["equity_total"] == "10000000"
    assert bs["net_income"] == "0" and bs["balanced"] is True
    ok("자본 납입 후 균형")

    # 2. 매수하면 자산 구성만 바뀐다(현금 -> 유가증권). 총자산은 수수료만큼 준다
    inst = uuid4()
    led.post_fill(FakeFill("100", "70000"), Side.BUY, inst, Position(inst), now)
    bs = balance_sheet(led)
    assert bs["assets"][SECURITIES] == "7000000", bs["assets"]
    assert bs["assets"][CASH] == "2999000", bs["assets"]
    assert bs["asset_total"] == "9999000", bs
    # 수수료는 비용이므로 당기순이익이 -1000이 된다
    assert bs["net_income"] == "-1000", bs["net_income"]
    ok("매수 = 자산 대체 + 비용")

    # 3. 손익계산서와 재무상태표의 당기순이익이 같다
    is_ = income_statement(led)
    assert is_["net_income"] == bs["net_income"], (is_, bs)
    assert is_["expense_total"] == "1000"
    ok("두 표의 순이익 일치")

    # 4. 매도로 실현손익이 생기면 수익에 잡힌다
    positions, _ = led.rebuild()
    led.post_fill(FakeFill("40", "71000", fee="1000", tax="400", bid="bf-2"),
                  Side.SELL, inst, positions[inst], now)
    is_ = income_statement(led)
    assert is_["revenue"]["4000"] == "40000", is_["revenue"]
    assert is_["expense_total"] == "2400", is_["expense"]   # 수수료 2000 + 세금 400
    assert is_["net_income"] == "37600", is_
    assert balance_sheet(led)["balanced"] is True
    ok("실현손익 = 수익")

    # 5. 보수 발생은 부채와 비용을 동시에 늘린다
    import fees  # noqa: E402

    cfg = fees.load_settings()
    fees.accrue(led, nav=D("10000000"), accrual_date=now.date(), when=now,
                high_water_mark=ZERO, settings=cfg)
    bs = balance_sheet(led)
    assert bs["liabilities"].get("2100"), "미지급보수가 부채에 없다"
    assert bs["balanced"] is True
    ok("보수 = 부채 + 비용")

    # 6. 기간을 자르면 그날까지만 본다
    later = led.post_capital(D("500"), now + timedelta(days=2), "late-seed")
    assert later is not None
    today_only = balance_sheet(led, until=now.date())
    assert today_only["equity_total"] == "10000000", today_only["equity_total"]
    assert balance_sheet(led)["equity_total"] == "10000500"
    ok("기간 절단")

    # 7. 유형을 모르는 계정은 통과시키지 않는다
    from ledger import JournalLine  # noqa: E402

    unknown = Ledger(fund_id=uuid4(), book_id=uuid4())
    unknown.post(unknown._journal("odd", "x", now, [
        JournalLine("9999", debit=D("1")), JournalLine(CASH, credit=D("1"))]))
    raises(lambda: balance_sheet(unknown), "모르는 계정")
    ok("미등록 계정 차단")

    print("ok - 재무제표 7개 영역 점검 통과 "
          "(취득원가 기준, 항등식 강제, 순이익 두 표 일치)")
