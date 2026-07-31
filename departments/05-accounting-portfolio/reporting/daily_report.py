#!/usr/bin/env python3
"""F23: Daily Report - 전략별 PnL, Drawdown, 비용과 오류.

소유: 도현 (회계·포트폴리오본부)
근거: docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md 4절 F23
      docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 8.2, 8.4
      docs/HEDGE_FUND_MASTER_PLAN.md 12.4, 19.11~19.13

**이 모듈은 수치를 만들지 않는다.** 원장과 스냅샷이 확정한 값을 하루 단위로
모아 차이를 낼 뿐이다 - `investor-reporting-agent` 프롬프트의 "references official
figure IDs directly, never restating them from memory"를 코드로 옮긴 것이다.
그래서 실현손익을 여기서 다시 계산하지 않고 `PortfolioSnapshot.realized_pnl`의
차이를 쓴다. 두 곳에서 계산하면 반드시 갈라진다.

핵심 항등식 하나를 매번 검산한다.

    NAV 변화 = 총손익 - 비용(수수료+세금) + 자본 유출입

이게 안 맞으면 `unexplained_pnl`이 0이 아니고, 그건 **설명되지 않은 손익**이다.
`pnl-performance-attribution-agent` 프롬프트대로 미설명 PnL은 닫지 않고
Exception으로 열어 둔다 - 0으로 반올림해 없애지 않는다.

Preliminary와 Official을 구분한다. 이 모듈이 만드는 것은 전부 Preliminary이며
Official NAV 확정은 별도 승인 절차다(회계본부는 단독 확정 권한이 없다).

자체 점검: python departments/05-accounting-portfolio/reporting/daily_report.py
"""
from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "ledger"))
sys.path.insert(0, str(_HERE.parent / "portfolio"))
sys.path.insert(0, str(_HERE.parent / "reconciliation"))

from ledger import (  # noqa: E402
    CAPITAL,
    FEE_EXPENSE,
    REALIZED_PNL,
    TAX_EXPENSE,
    ZERO,
    Journal,
    Ledger,
)
from portfolio import PortfolioSnapshot  # noqa: E402
from reconciliation import Break, Severity  # noqa: E402

# 전략을 알 수 없는 분개가 모이는 곳. 이름을 비워두거나 버리지 않는다 -
# 귀속 안 된 손익이 얼마인지가 Attribution 품질 지표다.
UNATTRIBUTED = "UNATTRIBUTED"


class ReportError(Exception):
    """보고서를 만들 수 없는 경우. 부분 보고서를 내지 않는다."""


@dataclass(frozen=True)
class StrategyPnL:
    """전략 하나의 하루치 손익과 비용."""

    strategy_id: str
    realized_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    taxes: Decimal = ZERO
    journal_count: int = 0

    @property
    def cost_total(self) -> Decimal:
        return self.fees + self.taxes

    @property
    def net_pnl(self) -> Decimal:
        """비용까지 뺀 실제 기여분. 총손익만 보면 집행 비용이 숨는다."""
        return self.realized_pnl - self.cost_total


@dataclass(frozen=True)
class DailyReport:
    """하루치 Preliminary 보고서. 확정 수치가 아니다."""

    accounting_date: date
    fund_id: UUID
    book_id: UUID

    nav_open: Decimal
    nav_close: Decimal
    nav_peak: Decimal
    nav_trough: Decimal

    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    taxes: Decimal
    capital_flow: Decimal

    max_drawdown: Decimal          # 절대금액(양수)
    max_drawdown_pct: Decimal | None  # 고점 대비 비율. 고점이 0 이하면 정의 불가

    by_strategy: tuple[StrategyPnL, ...] = ()
    breaks_by_severity: Mapping[str, int] = field(default_factory=dict)
    reversal_count: int = 0
    snapshot_count: int = 0
    is_official: bool = False      # 항상 False다. Official 확정은 승인 절차다

    @property
    def pnl_total(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def cost_total(self) -> Decimal:
        return self.fees + self.taxes

    @property
    def net_pnl(self) -> Decimal:
        return self.pnl_total - self.cost_total

    @property
    def nav_change(self) -> Decimal:
        return self.nav_close - self.nav_open

    @property
    def unexplained_pnl(self) -> Decimal:
        """설명되지 않은 손익. 0이어야 한다.

        0이 아니면 원장·평가·자본 유출입 중 어딘가가 어긋난 것이다.
        반올림해서 없애지 않는다 - 이 값이 Break의 근거다.
        """
        return self.nav_change - (self.net_pnl + self.capital_flow)

    @property
    def return_pct(self) -> Decimal | None:
        """기초 NAV 대비 수익률. 자본 유출입을 뺀 순손익 기준이다."""
        if self.nav_open <= 0:
            return None
        return self.net_pnl / self.nav_open

    @property
    def material_break_count(self) -> int:
        return self.breaks_by_severity.get(str(Severity.MATERIAL), 0)

    @property
    def has_errors(self) -> bool:
        """오류 신호. 하나라도 있으면 이 보고서를 성과로만 읽으면 안 된다."""
        return (
            self.material_break_count > 0
            or self.unexplained_pnl != ZERO
            or self.reversal_count > 0
        )

    def to_dict(self) -> dict:
        """화면·API 계약. 금액은 전부 문자열이다.

        JSON number는 IEEE754 double이라 Decimal이 깨진다. `ui_read_model`과
        같은 규칙을 여기서도 지킨다.
        """
        def d(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "accounting_date": self.accounting_date.isoformat(),
            "fund_id": str(self.fund_id),
            "book_id": str(self.book_id),
            "is_official": self.is_official,
            "nav": {
                "open": d(self.nav_open), "close": d(self.nav_close),
                "peak": d(self.nav_peak), "trough": d(self.nav_trough),
                "change": d(self.nav_change),
            },
            "pnl": {
                "realized": d(self.realized_pnl),
                "unrealized": d(self.unrealized_pnl),
                "total": d(self.pnl_total),
                "net": d(self.net_pnl),
                "return_pct": d(self.return_pct),
                "unexplained": d(self.unexplained_pnl),
            },
            "cost": {
                "fees": d(self.fees), "taxes": d(self.taxes), "total": d(self.cost_total),
            },
            "drawdown": {
                "max": d(self.max_drawdown), "max_pct": d(self.max_drawdown_pct),
            },
            "capital_flow": d(self.capital_flow),
            "by_strategy": [
                {
                    "strategy_id": s.strategy_id,
                    "realized_pnl": d(s.realized_pnl),
                    "fees": d(s.fees), "taxes": d(s.taxes),
                    "cost_total": d(s.cost_total), "net_pnl": d(s.net_pnl),
                    "journal_count": s.journal_count,
                }
                for s in self.by_strategy
            ],
            "errors": {
                "breaks_by_severity": dict(self.breaks_by_severity),
                "material_breaks": self.material_break_count,
                "reversals": self.reversal_count,
                "has_errors": self.has_errors,
            },
            "snapshot_count": self.snapshot_count,
        }


def build_daily_report(
    *,
    snapshots: Sequence[PortfolioSnapshot],
    ledger: Ledger,
    accounting_date: date,
    breaks: Sequence[Break] = (),
    strategy_of: Mapping[str, str] | None = None,
) -> DailyReport:
    """하루치 Preliminary 보고서를 만든다.

    `snapshots`는 시각 오름차순 스냅샷들이다. 최소 2개(기초·기말)가 필요하고,
    중간 스냅샷이 많을수록 Drawdown이 정확해진다 - 기초·기말만 주면 장중
    저점을 볼 수 없으므로 Drawdown이 과소평가된다.

    `strategy_of`는 `source_event_id -> strategy_id` 매핑이다. 원장에 전략
    차원이 없어서(분개는 fund/book까지만 안다) 호출자가 OMS 쪽 연결 정보를
    넘겨줘야 전략별 분해가 나온다. 안 주면 전부 UNATTRIBUTED로 모인다.
    """
    if len(snapshots) < 2:
        raise ReportError(
            f"스냅샷이 {len(snapshots)}개입니다. 기초·기말 최소 2개가 필요합니다"
        )

    ordered = sorted(snapshots, key=lambda s: s.as_of)
    first, last = ordered[0], ordered[-1]
    if first.fund_id != ledger.fund_id or first.book_id != ledger.book_id:
        raise ReportError("스냅샷과 원장의 Fund/Book이 다릅니다")
    if any(s.fund_id != first.fund_id or s.book_id != first.book_id for s in ordered):
        raise ReportError("여러 Fund/Book의 스냅샷이 섞였습니다")

    todays = [j for j in ledger.journals if j.accounting_date == accounting_date]

    navs = [s.nav for s in ordered]
    max_dd, peak = _max_drawdown(navs)

    return DailyReport(
        accounting_date=accounting_date,
        fund_id=ledger.fund_id,
        book_id=ledger.book_id,
        nav_open=first.nav,
        nav_close=last.nav,
        nav_peak=max(navs),
        nav_trough=min(navs),
        # 스냅샷의 누적값 차이를 쓴다. 여기서 다시 계산하지 않는다.
        realized_pnl=last.realized_pnl - first.realized_pnl,
        unrealized_pnl=last.unrealized_pnl - first.unrealized_pnl,
        fees=last.fees - first.fees,
        taxes=last.taxes - first.taxes,
        # 손익·비용이 스냅샷 구간의 차이라서, 자본 유출입도 같은 구간으로 잡아야
        # 항등식이 성립한다. 기초 스냅샷 이전의 납입까지 세면 그만큼 어긋난다.
        capital_flow=_capital_flow(ledger.journals, first.as_of, last.as_of),
        max_drawdown=max_dd,
        max_drawdown_pct=(max_dd / peak) if peak > 0 and max_dd > 0 else None,
        by_strategy=_by_strategy(todays, strategy_of or {}),
        breaks_by_severity=_count_breaks(breaks),
        reversal_count=sum(1 for j in todays if j.reversal_of is not None),
        snapshot_count=len(ordered),
    )


def _max_drawdown(navs: Sequence[Decimal]) -> tuple[Decimal, Decimal]:
    """고점 대비 최대 하락폭과 그때의 고점. 비율은 이 고점으로 나눈다.

    ponytail: 주어진 스냅샷 사이만 본다. 여러 날에 걸친 Drawdown은 스냅샷
              저장소가 생긴 뒤 같은 함수에 더 긴 시계열을 넣으면 된다.
    """
    peak = navs[0]
    peak_at_max = navs[0]
    max_dd = ZERO

    for nav in navs:
        if nav > peak:
            peak = nav
        drop = peak - nav
        if drop > max_dd:
            max_dd, peak_at_max = drop, peak

    return max_dd, peak_at_max


def _capital_flow(
    journals: Sequence[Journal], after: datetime, until: datetime
) -> Decimal:
    """구간 내 자본 유출입 순액. 자본금 계정의 대변 증가가 유입이다.

    구간은 (기초 스냅샷, 기말 스냅샷]이다. 기초 스냅샷과 같은 시각의 납입은
    이미 기초 NAV에 반영돼 있으므로 제외한다 - 포함하면 두 번 세는 셈이 된다.

    이걸 빼지 않으면 증자를 수익으로 읽는다.
    """
    flow = ZERO
    for j in journals:
        if not (after < j.effective_at <= until):
            continue
        for line in j.lines:
            if line.account_code == CAPITAL:
                flow += line.credit - line.debit
    return flow


def _by_strategy(
    journals: Sequence[Journal], strategy_of: Mapping[str, str]
) -> tuple[StrategyPnL, ...]:
    """분개를 전략별로 나눈다.

    ponytail: 원장에 전략 차원이 없어(`accounting.journals`도 fund/book까지다)
              호출자가 준 매핑에 의존한다. 근본 해결은 journal_lines에
              strategy_version_id를 넣거나 fill -> order_intent.strategy_version_id를
              조인하는 것이고, 그건 스키마 변경이라 DB 담당에게 넘길 델타다.
    """
    buckets: dict[str, dict[str, Decimal | int]] = {}

    for j in journals:
        key = strategy_of.get(j.source_event_id, UNATTRIBUTED)
        b = buckets.setdefault(
            key, {"realized": ZERO, "fees": ZERO, "taxes": ZERO, "count": 0}
        )
        b["count"] += 1
        for line in j.lines:
            if line.account_code == REALIZED_PNL:
                # 손익은 대변이 이익이다. 스냅샷과 같은 부호 규약으로 뒤집는다.
                b["realized"] += line.credit - line.debit
            elif line.account_code == FEE_EXPENSE:
                b["fees"] += line.debit - line.credit
            elif line.account_code == TAX_EXPENSE:
                b["taxes"] += line.debit - line.credit

    return tuple(
        StrategyPnL(
            strategy_id=key,
            realized_pnl=b["realized"], fees=b["fees"], taxes=b["taxes"],
            journal_count=b["count"],
        )
        # UNATTRIBUTED를 맨 뒤로 보내되 지우지 않는다
        for key, b in sorted(buckets.items(), key=lambda kv: (kv[0] == UNATTRIBUTED, kv[0]))
    )


def _count_breaks(breaks: Sequence[Break]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for b in breaks:
        counts[str(b.severity)] = counts.get(str(b.severity), 0) + 1
    return counts


if __name__ == "__main__":
    import json
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    from ledger import Position  # noqa: E402
    from portfolio import MarkPrice, value_portfolio  # noqa: E402

    day = date(2026, 7, 31)
    t0 = datetime(2026, 7, 31, 0, 30, tzinfo=timezone.utc)   # 09:30 KST
    fund, book, stock = uuid4(), uuid4(), uuid4()

    def raises(fn, why, exc=ReportError):
        try:
            fn()
        except exc:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    def snap(led: Ledger, price: str, when: datetime) -> PortfolioSnapshot:
        positions, _ = led.rebuild()
        marks = {
            i: MarkPrice(instrument_id=i, price=Decimal(price), as_of=when)
            for i in positions
        }
        return value_portfolio(led, marks, when, max_staleness=timedelta(minutes=5))

    class FakeFill:
        def __init__(self, qty, price, fee, tax, when, fid):
            self.quantity, self.price = Decimal(qty), Decimal(price)
            self.fee, self.tax = Decimal(fee), Decimal(tax)
            self.event_time, self.broker_fill_id = when, fid
            self.fill_id = uuid4()

    from ledger import Side  # noqa: E402

    # ---- 시나리오: 자본 10억 -> 100주 매수 -> 40주 익절 -----------------------
    led = Ledger(fund_id=fund, book_id=book)
    led.post_capital(Decimal("1000000000"), t0, "cap_1")
    open_snap = snap(led, "70000", t0)

    led.post_fill(FakeFill("100", "70000", "1050", "0", t0, "bf_1"),
                  Side.BUY, stock, Position(stock))
    positions, _ = led.rebuild()
    mid_snap = snap(led, "72000", t0 + timedelta(hours=2))

    dip_snap = snap(led, "66000", t0 + timedelta(hours=3))   # 장중 저점

    led.post_fill(FakeFill("40", "75000", "450", "4500", t0 + timedelta(hours=4), "sf_1"),
                  Side.SELL, stock, positions[stock])
    close_snap = snap(led, "75000", t0 + timedelta(hours=5))

    series = [open_snap, mid_snap, dip_snap, close_snap]

    # 1. 스냅샷이 2개 미만이면 보고서를 안 만든다
    raises(lambda: build_daily_report(snapshots=[open_snap], ledger=led,
                                      accounting_date=day), "스냅샷 1개")
    raises(lambda: build_daily_report(snapshots=[], ledger=led,
                                      accounting_date=day), "스냅샷 0개")

    rep = build_daily_report(snapshots=series, ledger=led, accounting_date=day)

    # 2. 핵심 항등식 — NAV 변화가 손익·비용·자본유출입으로 전부 설명된다
    assert rep.unexplained_pnl == ZERO, f"설명 안 되는 손익 {rep.unexplained_pnl}"
    assert rep.nav_change == rep.net_pnl + rep.capital_flow

    # 3. 실현손익은 원장에서 온 값이다 (40주 x (75000-70000) = 200,000)
    assert rep.realized_pnl == Decimal("200000"), rep.realized_pnl
    # 미실현: 남은 60주 x (75000-70000) = 300,000
    assert rep.unrealized_pnl == Decimal("300000"), rep.unrealized_pnl
    assert rep.pnl_total == Decimal("500000")

    # 4. 비용은 손익에 섞이지 않고 따로 잡힌다
    assert rep.fees == Decimal("1500") and rep.taxes == Decimal("4500")
    assert rep.net_pnl == Decimal("500000") - Decimal("6000")

    # 5. Drawdown — 고점 72,000 시점에서 66,000 시점까지 떨어진 폭
    assert rep.nav_peak == max(s.nav for s in series)
    assert rep.nav_trough == min(s.nav for s in series)
    assert rep.max_drawdown > ZERO, "장중 저점이 있는데 Drawdown이 0이다"
    assert rep.max_drawdown == mid_snap.nav - dip_snap.nav, rep.max_drawdown
    assert rep.max_drawdown_pct == rep.max_drawdown / mid_snap.nav

    # 6. 단조 상승이면 Drawdown이 0이고 비율은 정의하지 않는다
    up = build_daily_report(snapshots=[open_snap, mid_snap, close_snap],
                            ledger=led, accounting_date=day)
    assert up.max_drawdown == ZERO and up.max_drawdown_pct is None

    # 7. 자본 유출입을 수익으로 읽지 않는다
    led2 = Ledger(fund_id=fund, book_id=book)
    led2.post_capital(Decimal("1000000000"), t0, "cap_a")
    s_open = snap(led2, "70000", t0)
    led2.post_capital(Decimal("500000000"), t0 + timedelta(hours=1), "cap_b")
    s_close = snap(led2, "70000", t0 + timedelta(hours=2))
    r2 = build_daily_report(snapshots=[s_open, s_close], ledger=led2, accounting_date=day)
    assert r2.nav_change == Decimal("500000000"), "NAV가 안 늘었다"
    assert r2.capital_flow == Decimal("500000000"), "자본 유입을 못 잡았다"
    assert r2.net_pnl == ZERO, "증자를 손익으로 읽었다"
    assert r2.unexplained_pnl == ZERO
    assert r2.return_pct == ZERO, "증자로 수익률이 생겼다"

    # 8. 전략 매핑이 없으면 전부 UNATTRIBUTED — 버리지 않는다
    assert [s.strategy_id for s in rep.by_strategy] == [UNATTRIBUTED]
    assert rep.by_strategy[0].realized_pnl == Decimal("200000")
    assert rep.by_strategy[0].cost_total == Decimal("6000")

    # 9. 매핑을 주면 전략별로 갈린다
    tagged = build_daily_report(
        snapshots=series, ledger=led, accounting_date=day,
        strategy_of={"bf_1": "momentum", "sf_1": "momentum", "cap_1": "seed"},
    )
    ids = [s.strategy_id for s in tagged.by_strategy]
    assert ids == ["momentum", "seed"], ids
    momentum = tagged.by_strategy[0]
    assert momentum.realized_pnl == Decimal("200000")
    assert momentum.net_pnl == Decimal("200000") - Decimal("6000")
    # 전략별 합이 전체와 같아야 한다 — 어느 전략에도 안 붙은 손익이 사라지면 안 된다
    assert sum(s.realized_pnl for s in tagged.by_strategy) == rep.realized_pnl
    assert sum(s.fees + s.taxes for s in tagged.by_strategy) == rep.cost_total

    # 10. 일부만 매핑되면 나머지는 UNATTRIBUTED로 남는다 (조용히 버리지 않는다)
    partial = build_daily_report(snapshots=series, ledger=led, accounting_date=day,
                                 strategy_of={"bf_1": "momentum"})
    ids = [s.strategy_id for s in partial.by_strategy]
    assert ids == ["momentum", UNATTRIBUTED], ids
    assert sum(s.realized_pnl for s in partial.by_strategy) == rep.realized_pnl

    # 11. 오류 신호 — Break와 Reversal이 보고서에 드러난다
    assert rep.has_errors is False, "정상인데 오류로 표시됐다"
    from reconciliation import reconcile_cash  # noqa: E402
    bad = reconcile_cash(Decimal("1000000"), Decimal("1"))
    with_breaks = build_daily_report(snapshots=series, ledger=led,
                                     accounting_date=day, breaks=bad.breaks)
    assert with_breaks.material_break_count == 1, with_breaks.breaks_by_severity
    assert with_breaks.has_errors is True, "Material Break가 있는데 정상으로 나온다"

    target = next(j for j in led.journals if j.source_event_id == "bf_1")
    led.reverse(target.journal_id, "브로커 정정")
    after_rev = build_daily_report(snapshots=series, ledger=led, accounting_date=day)
    assert after_rev.reversal_count == 1, "정정 분개를 못 셌다"
    assert after_rev.has_errors is True, "정정이 있었는데 오류 표시가 없다"

    # 12. 다른 Fund/Book 스냅샷은 섞이지 않는다
    other = Ledger(fund_id=uuid4(), book_id=book)
    other.post_capital(Decimal("1000"), t0, "cap_x")
    raises(lambda: build_daily_report(snapshots=[snap(other, "70000", t0),
                                                snap(other, "70000", t0 + timedelta(hours=1))],
                                      ledger=led, accounting_date=day), "Fund 불일치")
    raises(lambda: build_daily_report(snapshots=[open_snap, snap(other, "70000", t0)],
                                      ledger=led, accounting_date=day), "Fund 혼재")

    # 13. 다른 날 분개는 당일 보고서에 안 들어간다
    other_day = build_daily_report(snapshots=series, ledger=led,
                                   accounting_date=date(2026, 7, 30))
    assert other_day.capital_flow == ZERO, "다른 날 자본납입이 섞였다"
    assert other_day.by_strategy == (), "다른 날 분개가 섞였다"

    # 14. 계약 — 금액은 전부 문자열이고 Official이 아니다
    doc = rep.to_dict()
    raw = json.dumps(doc, ensure_ascii=False)
    assert doc["is_official"] is False, "Preliminary가 Official로 나갔다"
    assert isinstance(doc["nav"]["close"], str), "금액이 JSON number로 나갔다"
    assert Decimal(json.loads(raw)["pnl"]["net"]) == rep.net_pnl, "직렬화에서 값이 변했다"
    # rep은 정정 이전에 만든 보고서다. 나중에 원장이 바뀌어도 이미 낸 보고서는
    # 그대로여야 한다 - frozen이 지켜주는 것이 이것이다.
    assert doc["errors"]["has_errors"] is False, "낸 보고서가 나중 사건에 오염됐다"
    assert after_rev.to_dict()["errors"]["reversals"] == 1

    print("ok - Daily Report 14개 영역 점검 통과")
