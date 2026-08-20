#!/usr/bin/env python3
"""Treasury: 결제(T+2) 기준 현금. **원장 현금 != 오늘 쓸 수 있는 돈.**

소유: 도현 (회계·포트폴리오본부)
근거: docs/HEDGE_FUND_MASTER_PLAN.md 19.13(Treasury 자동화) 중 "일별 Cash와
      Settlement Forecast" 한 항목만 구현한다.

19.13은 Margin·Collateral·Borrow·환전·Prime Broker Concentration까지 열거하지만
**우리 시장에는 그 대상이 없다** - KOSPI/KOSDAQ 현물 Long-only, 단일통화 KRW,
Broker 하나. 대상 없는 모듈을 만들면 늘 0을 보여주고, 0은 "없음"과 "확인 안 함"을
구분해 주지 않는다. 그 항목들은 파생·공매도·복수 Broker가 실제로 생길 때 만든다.

여기서 하는 일은 둘이다:

  1. `settlement_date_for()` — 체결일 -> 결제일(T+2 영업일)
  2. `build_ladder()`        — 결제일별 예정 현금 사다리
  3. `settle_due()`          — 결제일 도래분을 현금으로 옮기는 분개

**NAV는 이 모듈이 건드리지 않는다.** 미수/미지급은 이미 NAV에 들어 있어
(`PortfolioSnapshot.nav = 현금 + 유가증권 + 미수 - 미지급`) 결제는 NAV를 바꾸지
않고 현금과 미수/미지급 사이를 옮길 뿐이다. 바뀌는 것은 **가용 현금**이다.

**판정은 없다.** 어떤 주문을 낼 수 있는지, 현금이 부족한지는 여기서 정하지 않는다.
사다리를 만들어 놓을 뿐이고 한도 판정은 리스크본부다.

자체 점검: python departments/05-accounting-portfolio/treasury/settlement.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import lru_cache
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import yaml

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "ledger"))

from ledger import CASH, PAYABLE, RECEIVABLE, Journal, Ledger  # noqa: E402

OPS_PATH = _HERE.parent / "accounting_ops.yaml"
ZERO = Decimal(0)


class SettlementError(Exception):
    """결제일을 정할 수 없거나 사다리를 만들 수 없는 경우."""


@dataclass(frozen=True)
class SettlementSettings:
    days: int
    holidays: frozenset[date]
    ladder_days: int


def load_settings(ops: Mapping[str, Any] | None = None,
                  path: Path = OPS_PATH) -> SettlementSettings:
    """튜닝값은 코드가 아니라 accounting_ops.yaml에 있다."""
    if ops is None:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ops = doc.get("settlement")
    if not ops:
        raise SettlementError(f"{path} 에 settlement 블록이 없습니다")
    days = int(ops["days"])
    if days < 0:
        raise SettlementError(f"결제 영업일 수가 음수입니다: {days}")
    holidays = frozenset(
        value if isinstance(value, date) else date.fromisoformat(str(value))
        for value in (ops.get("holidays") or ())
    )
    return SettlementSettings(days=days, holidays=holidays,
                              ladder_days=int(ops.get("ladder_days", days + 1)))


@lru_cache(maxsize=1)
def default_settings() -> SettlementSettings:
    """기본 설정. 소비 루프가 1초마다 부르므로 파일을 매번 읽지 않는다.

    바꾸려면 재기동한다 - 결제일 규칙이 프로세스 중간에 바뀌면 같은 체결이 주기마다
    다른 결제일을 갖게 된다.
    """
    return load_settings()


def is_business_day(day: date, settings: SettlementSettings) -> bool:
    return day.weekday() < 5 and day not in settings.holidays


def settlement_date_for(trade_date: date,
                        settings: SettlementSettings | None = None) -> date:
    """체결일 -> 결제일. 주말과 주입된 휴장일을 건너뛴다.

    거래소 캘린더가 없으므로 **주말만 확실히 안다**. 공휴일은 설정으로 받는다 -
    모르는 휴장일이 있으면 결제일이 실제보다 앞서 나오고, 그건 "현금이 이미
    들어왔다"고 보는 쪽이라 안전한 방향이 아니다. 그래서 캘린더가 생기면 그걸
    쓰고, 그 전까지는 아는 만큼만 반영한다.
    """
    settings = settings or default_settings()
    day = trade_date
    remaining = settings.days
    while remaining > 0:
        day += timedelta(days=1)
        if is_business_day(day, settings):
            remaining -= 1
    return day


def _unsettled(ledger: Ledger,
               settings: SettlementSettings) -> list[tuple[Journal, date]]:
    """아직 결제되지 않은 체결 분개와 그 결제일.

    판단 근거를 **전부 분개 자체에서 얻는다** - 미수/미지급 라인이 있으면 미결제,
    `<원천>:settle` 분개가 있으면 결제 완료, 결제일은 `accounting_date`에서 T+2로
    유도한다. 셋 다 DB에 저장되는 사실이라 프로세스를 재기동해도 같은 답이 나온다.
    별도 컬럼에 결제일을 들고 있으면 DB에서 다시 읽은 분개만 그 값을 잃고,
    그때 미결제분이 조용히 사라진다 - 현금이 실제보다 많아 보이는 방향이다.
    """
    settled = {j.source_event_id for j in ledger.journals
               if j.event_type == "settlement"}
    pending: list[tuple[Journal, date]] = []
    for journal in ledger.journals:
        if journal.event_type != "fill" or journal.status == "reversed":
            continue
        if f"{journal.source_event_id}:settle" in settled:
            continue
        if not any(l.account_code in (PAYABLE, RECEIVABLE) for l in journal.lines):
            continue   # 즉시 결제로 기록된 분개 - 옮길 것이 없다
        pending.append((journal, settlement_date_for(journal.accounting_date, settings)))
    return pending


def build_ladder(ledger: Ledger, as_of: date,
                 settings: SettlementSettings | None = None) -> dict[str, Any]:
    """결제일별 예정 현금 사다리 (원장 객체를 들고 있을 때).

    DB에서 집계해 오는 경로는 `ladder_from_pending()`을 직접 부른다 - 사다리 산술은
    한 군데뿐이어야 화면이 원천에 따라 다른 답을 보지 않는다.
    """
    settings = settings or default_settings()
    _, cash = ledger.rebuild()

    pending: list[tuple[date, Decimal, Decimal, dict[str, str]]] = []
    for journal, due in _unsettled(ledger, settings):
        pending.append((
            due,
            sum(l.debit for l in journal.lines if l.account_code == RECEIVABLE),
            sum(l.credit for l in journal.lines if l.account_code == PAYABLE),
            {"journal_id": str(journal.journal_id),
             "source_event_id": journal.source_event_id},
        ))
    return ladder_from_pending(pending, cash=cash, as_of=as_of, settings=settings)


def ladder_from_pending(
    pending: list[tuple[date, Decimal, Decimal, dict[str, str]]],
    *,
    cash: Decimal,
    as_of: date,
    settings: SettlementSettings | None = None,
) -> dict[str, Any]:
    """미결제 (결제일, 받을 돈, 줄 돈, 참조) 목록을 사다리로 만든다.

    `available_cash`는 원장의 현금 계정 잔액 그대로다 - 이미 결제가 끝난 돈만
    거기 있다. 아직 안 끝난 것은 `buckets`에 결제일별로 들어간다.

    `overdue`는 결제일이 지났는데 결제 분개가 없는 것이다. 조용히 오늘 칸에
    합치지 않는다 - 그러면 브로커 결제 누락이 정상 흐름처럼 보인다.
    """
    settings = settings or default_settings()
    by_day: dict[date, dict[str, Decimal]] = {}
    overdue: list[dict[str, Any]] = []
    for due, incoming, outgoing, ref in pending:
        if due < as_of:
            overdue.append({**ref, "settlement_date": due.isoformat(),
                            "incoming": incoming, "outgoing": outgoing})
            continue
        bucket = by_day.setdefault(due, {"incoming": ZERO, "outgoing": ZERO})
        bucket["incoming"] += incoming
        bucket["outgoing"] += outgoing

    # 사다리는 달력일로 세는데 결제일은 영업일로 센다. 그래서 목·금 체결은 T+2가
    # `ladder_days` 밖으로 밀리고(2026-08-20 목요일 실측: 결제일이 +4일), 그 칸이
    # `beyond_ladder` 합계로 빠져 화면에서 개별 결제일이 사라진다. 설정값은 최소
    # 폭으로 쓰되 결제 지평까지는 반드시 덮는다.
    last_day = max(as_of + timedelta(days=settings.ladder_days),
                   settlement_date_for(as_of, settings))
    buckets: list[dict[str, Any]] = []
    running = cash
    day = as_of
    while day <= last_day:
        bucket = by_day.pop(day, {"incoming": ZERO, "outgoing": ZERO})
        running += bucket["incoming"] - bucket["outgoing"]
        buckets.append({
            "date": day.isoformat(),
            "incoming": bucket["incoming"],
            "outgoing": bucket["outgoing"],
            "net": bucket["incoming"] - bucket["outgoing"],
            "projected_cash": running,
        })
        day += timedelta(days=1)
    # 사다리 밖(더 먼 결제일)은 버리지 않고 합계로 남긴다.
    beyond_in = sum((b["incoming"] for b in by_day.values()), ZERO)
    beyond_out = sum((b["outgoing"] for b in by_day.values()), ZERO)

    return {
        "as_of": as_of.isoformat(),
        "available_cash": cash,
        "buckets": buckets,
        "beyond_ladder": {"incoming": beyond_in, "outgoing": beyond_out},
        "overdue": overdue,
        "projected_cash_end": running + beyond_in - beyond_out,
        "decided_by": "deterministic",
    }


def settle_due(ledger: Ledger, on: date, *, now: datetime | None = None,
               settings: SettlementSettings | None = None) -> list[Journal]:
    """결제일이 도래한 미결제 분개를 현금으로 옮긴다.

    결제일보다 **앞당겨 결제하지 않는다.** 두 번 부르면 두 번째는 `post()`의
    멱등 검사에 걸려 아무것도 만들지 않는다.
    """
    settings = settings or default_settings()
    now = now or datetime.now(tz=None).astimezone()
    return [ledger.post_settlement(journal, now)
            for journal, due in _unsettled(ledger, settings) if due <= on]


if __name__ == "__main__":
    from dataclasses import dataclass as dc
    from datetime import timezone
    from uuid import UUID, uuid4

    sys.path.insert(0, str(_HERE.parent.parent / "02-trading" / "contracts"))
    from contracts import Side  # noqa: E402

    from ledger import Position  # noqa: E402

    D = Decimal
    ok = lambda label: print(f"  {label:26} OK")  # noqa: E731

    @dc
    class FakeFill:
        quantity: Decimal
        price: Decimal
        fee: Decimal
        tax: Decimal
        event_time: datetime
        broker_fill_id: str
        fill_id: UUID = None

    def raises(fn, why, exc=Exception):
        try:
            fn()
        except exc:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    settings = load_settings()
    assert settings.days == 2, settings

    # 1. T+2는 영업일 기준이다. 금요일 체결은 화요일 결제.
    assert settlement_date_for(date(2026, 8, 10), settings) == date(2026, 8, 12)  # 월->수
    assert settlement_date_for(date(2026, 8, 14), settings) == date(2026, 8, 18)  # 금->화
    assert settlement_date_for(date(2026, 8, 13), settings) == date(2026, 8, 17)  # 목->월
    ok("T+2 영업일")

    # 2. 주입된 휴장일은 건너뛴다
    with_holiday = SettlementSettings(days=2, holidays=frozenset({date(2026, 8, 17)}),
                                      ladder_days=3)
    assert settlement_date_for(date(2026, 8, 13), with_holiday) == date(2026, 8, 18)
    ok("휴장일 반영")

    # 3. 체결일 분개는 현금을 건드리지 않는다 - 미지급금이 선다
    fund, book, inst = uuid4(), uuid4(), uuid4()
    trade_day = date(2026, 8, 10)
    when = datetime(2026, 8, 10, 6, 0, tzinfo=timezone.utc)
    led = Ledger(fund_id=fund, book_id=book)
    led.post_capital(D("10000000"), when, "seed")
    buy = FakeFill(D("100"), D("70000"), D("105"), D("0"), when, "bf-1", uuid4())
    led.post_fill(buy, Side.BUY, inst, Position(inst), when,
                  settlement_date=settlement_date_for(trade_day, settings))
    _, cash = led.rebuild()
    assert cash == D("10000000"), f"체결일에 현금이 움직였다: {cash}"
    tb = led.trial_balance()
    assert -tb[PAYABLE] == D("7000105"), tb
    assert sum(tb.values()) == ZERO
    ok("체결일 = 미지급금")

    # 4. 사다리 - 오늘 가용 현금과 결제일 예정액이 분리된다
    ladder = build_ladder(led, trade_day, settings)
    assert ladder["available_cash"] == D("10000000")
    assert ladder["buckets"][0]["net"] == ZERO, "결제일이 아닌데 오늘 칸에 잡혔다"
    settle_bucket = next(b for b in ladder["buckets"] if b["date"] == "2026-08-12")
    assert settle_bucket["outgoing"] == D("7000105"), settle_bucket
    assert settle_bucket["projected_cash"] == D("2999895"), settle_bucket
    assert ladder["overdue"] == []
    ok("결제 사다리")

    # 5. 결제일 전에는 아무것도 옮기지 않는다
    assert settle_due(led, date(2026, 8, 11), now=when) == []
    _, cash = led.rebuild()
    assert cash == D("10000000"), "결제일 전에 현금이 나갔다"
    ok("조기 결제 금지")

    # 6. 결제일에 현금으로 옮긴다. 두 번 불러도 한 번만.
    moved = settle_due(led, date(2026, 8, 12), now=when)
    assert len(moved) == 1
    _, cash = led.rebuild()
    assert cash == D("2999895"), cash
    assert led.trial_balance().get(PAYABLE, ZERO) == ZERO, "미지급금이 남았다"
    assert settle_due(led, date(2026, 8, 12), now=when) == [], "같은 체결이 두 번 결제됐다"
    _, cash_again = led.rebuild()
    assert cash_again == cash
    ok("결제일 이체 + 멱등")

    # 7. 매도는 미수금 -> 현금 (받을 돈)
    sell_day = date(2026, 8, 12)
    sell_when = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)
    positions, _ = led.rebuild()
    sell = FakeFill(D("40"), D("71000"), D("105"), D("400"), sell_when, "bf-2", uuid4())
    led.post_fill(sell, Side.SELL, inst, positions[inst], sell_when,
                  settlement_date=settlement_date_for(sell_day, settings))
    tb = led.trial_balance()
    assert tb[RECEIVABLE] == D("2839495"), tb   # 40*71000 - 105 - 400
    ladder = build_ladder(led, sell_day, settings)
    assert next(b for b in ladder["buckets"]
                if b["date"] == "2026-08-14")["incoming"] == D("2839495")
    settle_due(led, date(2026, 8, 14), now=sell_when)
    _, cash = led.rebuild()
    assert cash == D("5839390"), cash
    assert led.trial_balance().get(RECEIVABLE, ZERO) == ZERO
    ok("매도 = 미수금")

    # 8. NAV 구성요소는 결제로 바뀌지 않는다 (현금 <-> 미수/미지급 이동일 뿐)
    led2 = Ledger(fund_id=fund, book_id=book)
    led2.post_capital(D("10000000"), when, "seed2")
    led2.post_fill(buy, Side.BUY, inst, Position(inst), when,
                   settlement_date=settlement_date_for(trade_day, settings))
    tb_before = led2.trial_balance()
    before = (tb_before.get(CASH, ZERO) + tb_before.get(RECEIVABLE, ZERO)
              + tb_before.get(PAYABLE, ZERO))
    settle_due(led2, date(2026, 8, 12), now=when)
    tb_after = led2.trial_balance()
    after = (tb_after.get(CASH, ZERO) + tb_after.get(RECEIVABLE, ZERO)
             + tb_after.get(PAYABLE, ZERO))
    assert before == after, f"결제가 순자산을 바꿨다: {before} -> {after}"
    ok("결제는 NAV 불변")

    # 9. 결제일이 지났는데 결제가 안 된 것은 오늘 칸에 섞지 않는다
    stale = build_ladder(led2, date(2026, 8, 20), settings)
    assert stale["overdue"] == [], stale     # 8번에서 이미 결제됨
    led3 = Ledger(fund_id=fund, book_id=book)
    led3.post_capital(D("10000000"), when, "seed3")
    led3.post_fill(buy, Side.BUY, inst, Position(inst), when,
                   settlement_date=settlement_date_for(trade_day, settings))
    late = build_ladder(led3, date(2026, 8, 20), settings)
    assert len(late["overdue"]) == 1, late
    assert all(b["net"] == ZERO for b in late["buckets"]), "미결제가 오늘 칸에 섞였다"
    ok("결제 지연 분리")

    # 10. 결제 대상이 아닌 분개는 결제하지 않는다
    plain = Ledger(fund_id=fund, book_id=book)
    capital = plain.post_capital(D("1000"), when, "seed4")
    raises(lambda: plain.post_settlement(capital, when), "settlement_date 없는 분개")
    assert settle_due(plain, date(2026, 8, 20), now=when) == []
    ok("비결제 분개 제외")

    # 11. 설정이 깨지면 결제일을 지어내지 않는다
    raises(lambda: load_settings({}), "빈 settlement 블록", SettlementError)
    raises(lambda: load_settings({"days": -1}), "음수 결제일", SettlementError)
    ok("설정 fail-closed")

    print("ok - Treasury 결제 사다리 11개 영역 점검 통과 "
          "(판정 없음, NAV 불변, 캘린더는 주말+주입 휴장일만)")
