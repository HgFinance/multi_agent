#!/usr/bin/env python3
"""재무상태표·손익계산서. **시산표를 계정 성격으로 접은 것뿐이다.**

소유: 도현 (회계·포트폴리오본부)
근거: docs/HEDGE_FUND_MASTER_PLAN.md 21.16(Financial Control), 12.4(Close Process)

`ledger.trial_balance()`가 이미 계정별 잔액을 준다. 여기서 새로 계산하는 수치는
하나도 없고, `ACCOUNT_TYPES`가 정한 성격(asset/liability/equity/income/expense)대로
묶어서 두 표로 접는다. 그래서 이 파일에는 회계 규칙이 없다 - 규칙은 ledger.py에 있다.

**원장 기준이다(원가).** 유가증권은 취득원가로 서 있고 미실현 평가손익은 들어가지
않는다 - 평가는 Mark가 있어야 하고 그건 `portfolio.value_portfolio`의 일이다.
그래서 `총자산 != NAV`가 정상이며, 차이는 미실현 평가손익이다. 두 수치를 같은
줄에 놓고 싶으면 `reconcile_to_nav()`가 그 차이를 명시적으로 보여준다.

**공식 재무제표가 아니다.** `is_official`은 항상 False다 - 확정은 승인 절차
(`close/nav_close.py`)를 거친 것만이고, 이 표는 그 승인 대상 자료다.

자체 점검: python departments/05-accounting-portfolio/reporting/statements.py
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parent / "ledger", _HERE.parent / "portfolio"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ledger import ACCOUNT_TYPES, ZERO, Ledger, decimal_str  # noqa: E402

# 표시 순서. dict 순서에 기대지 않는다 - 보고서 줄 순서가 실행마다 바뀌면 diff가 무의미해진다.
_ORDER = ("asset", "liability", "equity", "income", "expense")


class StatementError(Exception):
    """표를 만들 수 없는 경우. 균형이 안 맞는 표를 내놓지 않는다."""


def _by_type(balances: dict[str, Decimal]) -> dict[str, dict[str, Decimal]]:
    """계정 잔액을 성격별로 나눈다. 모르는 계정은 조용히 버리지 않고 예외다."""
    grouped: dict[str, dict[str, Decimal]] = {kind: {} for kind in _ORDER}
    for code, amount in balances.items():
        kind = ACCOUNT_TYPES.get(code)
        if kind is None:
            raise StatementError(
                f"성격을 모르는 계정과목입니다: {code}. ledger.ACCOUNT_TYPES에 추가하세요"
            )
        if amount != ZERO:
            grouped[kind][code] = amount
    return grouped


def _natural(kind: str, amount: Decimal) -> Decimal:
    """계정 성격의 정상 잔액 방향으로 부호를 맞춘다.

    `trial_balance()`는 차변 - 대변이라 부채·자본·수익이 음수로 나온다. 표에서는
    그게 양수여야 읽힌다 - 부채가 -1,000으로 찍히면 사람은 자산으로 읽는다.
    """
    return -amount if kind in ("liability", "equity", "income") else amount


def build_statements(ledger: Ledger, *, as_of: date,
                     period_start: date | None = None) -> dict[str, Any]:
    """재무상태표 + 손익계산서 한 묶음.

    두 표는 **같은 시산표에서 나온다.** 따로 만들면 순이익이 두 값이 될 수 있고,
    그러면 어느 쪽이 맞는지 판정할 방법이 없다.

    항등식: 자산 = 부채 + 자본 + 당기순이익
    수익·비용을 자본으로 마감(closing)하지 않은 상태라 순이익이 우변에 따로 선다.
    이 식이 안 맞으면 시산표가 깨진 것이고, 그때는 표를 만들지 않고 예외다.
    """
    balances = ledger.trial_balance()
    grouped = _by_type(balances)

    totals = {
        kind: sum((_natural(kind, amount) for amount in accounts.values()), ZERO)
        for kind, accounts in grouped.items()
    }
    net_income = totals["income"] - totals["expense"]
    left = totals["asset"]
    right = totals["liability"] + totals["equity"] + net_income
    if left != right:
        raise StatementError(
            f"재무상태표가 균형이 아닙니다: 자산 {decimal_str(left)} != "
            f"부채+자본+순이익 {decimal_str(right)}. 시산표를 먼저 확인하세요"
        )

    def lines(kind: str) -> list[dict[str, str]]:
        return [
            {"account_code": code, "amount": decimal_str(_natural(kind, amount))}
            for code, amount in sorted(grouped[kind].items())
        ]

    return {
        "fund_id": str(ledger.fund_id),
        "book_id": str(ledger.book_id),
        "as_of": as_of.isoformat(),
        "period_start": (period_start or as_of).isoformat(),
        "basis": "ledger_cost",   # 원가 기준. 미실현 평가손익은 여기 없다
        "is_official": False,     # 확정은 승인 절차를 거친 것만이다
        "balance_sheet": {
            "assets": lines("asset"),
            "liabilities": lines("liability"),
            "equity": lines("equity"),
            "total_assets": decimal_str(totals["asset"]),
            "total_liabilities": decimal_str(totals["liability"]),
            "total_equity": decimal_str(totals["equity"]),
            "net_income": decimal_str(net_income),
            "balanced": True,
        },
        "income_statement": {
            "revenue": lines("income"),
            "expenses": lines("expense"),
            "total_revenue": decimal_str(totals["income"]),
            "total_expenses": decimal_str(totals["expense"]),
            "net_income": decimal_str(net_income),
        },
        "decided_by": "deterministic",
    }


def reconcile_to_nav(statements: dict[str, Any], snapshot) -> dict[str, Any]:
    """원가 기준 표와 평가 기준 NAV의 차이를 드러낸다.

    **차이를 없애지 않는다.** 둘은 다른 기준이고 차이는 미실현 평가손익이어야 한다.
    그 값과 안 맞으면 어느 한쪽이 틀린 것이고, 그 사실이 나와야 조사할 수 있다.
    """
    bs = statements["balance_sheet"]
    book_equity = (Decimal(bs["total_assets"]) - Decimal(bs["total_liabilities"]))
    difference = snapshot.nav - book_equity
    return {
        "book_equity": decimal_str(book_equity),
        "nav": decimal_str(snapshot.nav),
        "difference": decimal_str(difference),
        "unrealized_pnl": decimal_str(snapshot.unrealized_pnl),
        # 차이가 미실현 평가손익과 같아야 한다. 다르면 설명되지 않은 간극이다.
        "explained": difference == snapshot.unrealized_pnl,
    }


if __name__ == "__main__":
    from datetime import datetime, timezone
    from uuid import uuid4

    sys.path.insert(0, str(_HERE.parent.parent / "02-trading" / "contracts"))
    from contracts import Side  # noqa: E402
    from dataclasses import dataclass  # noqa: E402

    from ledger import CASH, MGMT_FEE_EXPENSE, SECURITIES, Position  # noqa: E402
    from portfolio import MarkPrice, value_portfolio  # noqa: E402

    D = Decimal
    ok = lambda label: print(f"  {label:26} OK")  # noqa: E731
    now = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    today = now.date()
    fund, book, inst = uuid4(), uuid4(), uuid4()

    @dataclass
    class FakeFill:
        quantity: Decimal
        price: Decimal
        fee: Decimal
        tax: Decimal
        event_time: datetime
        broker_fill_id: str
        fill_id: Any = None

    def raises(fn, why, exc=StatementError):
        try:
            fn()
        except exc:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    led = Ledger(fund_id=fund, book_id=book)
    led.post_capital(D("1000000"), now, "seed")

    # 1. 자본만 있는 원장 - 자산 = 자본, 순이익 0
    st = build_statements(led, as_of=today)
    assert st["balance_sheet"]["total_assets"] == "1000000"
    assert st["balance_sheet"]["total_equity"] == "1000000"
    assert st["income_statement"]["net_income"] == "0"
    assert st["is_official"] is False, "재무제표가 공식으로 나왔다"
    ok("자본 납입 직후")

    # 2. 매수 - 자산 구성만 바뀌고 총자산은 수수료만큼 줄어든다(비용 인식)
    buy = FakeFill(D("10"), D("70000"), D("105"), D("0"), now, "bf-1", uuid4())
    led.post_fill(buy, Side.BUY, inst, Position(inst), now)
    st = build_statements(led, as_of=today)
    bs = st["balance_sheet"]
    assert bs["total_assets"] == "999895", bs          # 1,000,000 - 105
    assert bs["net_income"] == "-105", bs              # 수수료가 손익으로
    assert st["income_statement"]["total_expenses"] == "105"
    codes = {line["account_code"] for line in bs["assets"]}
    assert codes == {CASH, SECURITIES}, codes
    ok("매수 = 자산 대체")

    # 3. 항등식이 표 안에서 성립한다 (자산 = 부채 + 자본 + 순이익)
    assert (D(bs["total_assets"])
            == D(bs["total_liabilities"]) + D(bs["total_equity"]) + D(bs["net_income"]))
    ok("재무상태표 항등식")

    # 4. 보수 발생 - 비용과 부채가 같이 선다(현금은 그대로)
    sys.path.insert(0, str(_HERE.parent / "close"))
    import fees as fee_accrual  # noqa: E402

    cfg = fee_accrual.FeeSettings(enabled=True, management_fee_bps=D("200"), day_count=365,
                                  performance_fee_rate=D("0"), performance_base=ZERO,
                                  min_accrual=D("1"))
    fee_accrual.accrue(led, nav=D("1000000"), accrual_date=today, when=now, settings=cfg)
    st = build_statements(led, as_of=today)
    bs = st["balance_sheet"]
    assert bs["total_liabilities"] == "55", bs         # 1,000,000 x 2% / 365
    assert bs["net_income"] == "-160", bs              # 105 + 55
    fee_line = [l for l in st["income_statement"]["expenses"]
                if l["account_code"] == MGMT_FEE_EXPENSE]
    assert fee_line and fee_line[0]["amount"] == "55", st["income_statement"]
    assert (D(bs["total_assets"])
            == D(bs["total_liabilities"]) + D(bs["total_equity"]) + D(bs["net_income"]))
    ok("보수 = 비용 + 부채")

    # 5. 부채·자본·수익은 표에서 양수로 읽힌다 (시산표 부호를 그대로 내보내지 않는다)
    assert all(D(line["amount"]) > 0 for line in bs["liabilities"]), bs["liabilities"]
    assert all(D(line["amount"]) > 0 for line in bs["equity"]), bs["equity"]
    ok("정상 잔액 부호")

    # 6. 원가 기준 표와 평가 기준 NAV의 차이는 미실현 평가손익이다
    marks = {inst: MarkPrice(inst, D("77000"), now)}
    snap = value_portfolio(led, marks, now)
    recon = reconcile_to_nav(st, snap)
    assert recon["explained"] is True, recon
    assert recon["difference"] == recon["unrealized_pnl"] == "70000", recon
    ok("NAV 대사 = 미실현")

    # 7. 모르는 계정과목은 조용히 버리지 않는다
    from ledger import Journal, JournalLine  # noqa: E402

    rogue = Ledger(fund_id=fund, book_id=book)
    rogue.post(Journal(journal_id=uuid4(), fund_id=fund, book_id=book,
                       event_type="manual", source_event_id="x", effective_at=now,
                       accounting_date=today,
                       lines=[JournalLine("9999", debit=D("1")),
                              JournalLine(CASH, credit=D("1"))]))
    raises(lambda: build_statements(rogue, as_of=today), "미등록 계정과목")
    ok("미등록 계정 fail-closed")

    print("ok - 재무제표 7개 영역 점검 통과 "
          "(시산표 재해석만, 원가 기준, 공식 아님)")
