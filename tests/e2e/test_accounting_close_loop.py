"""회계·포트폴리오본부 **부서 내부** 관통 테스트.

소유: 도현 (회계·포트폴리오본부)
범위: 체결 사실 -> 분개 -> Position/Cash -> Mark -> NAV -> 일일 보고 -> 대사 -> Break
      Aging -> 기업행위 -> Reversal

    FillRow(체결 사실)
      -> consume_fill          (fill_consumer)
      -> Journal               (ledger.post_fill — 차대 균형)
      -> 미지급금 / 미수금      (T+2. 체결일에는 현금이 움직이지 않는다)
      -> Position / Cash       (ledger.rebuild + treasury.settle_due)
      -> MarkPrice             (호출자가 준다. 없으면 NAV 거부)
      -> PortfolioSnapshot     (portfolio.value_portfolio)
      -> DailyReport           (reporting.build_daily_report — 미설명 손익 0)
      -> ReconResult / Break   (reconciliation.reconcile_fills)
      -> BreakAge              (break_triage.check_aging)
      -> CorporateAction       (corporate_actions.apply_corporate_action)
      -> Reversal              (ledger.reverse — Posted는 수정하지 않는다)

각 모듈의 자체 점검은 자기 자리만 본다. 이 파일은 **모듈 사이의 배선**을 본다.
`test_paper_loop.py`가 트레이딩->회계 경계를 보는 것과 달리 여기는 회계본부 안쪽만
돈다 - 입력은 이미 확정된 체결 사실이고, OMS·Risk·Broker는 등장하지 않는다.

DB는 쓰지 않는다. 저장(`repository.py`)과 실 DB 왕복은 각 모듈 자체 점검이 보고,
여기서는 계산 경로가 서로 물려 있는지만 본다.

실행: python -m unittest discover -s tests/e2e
"""
from __future__ import annotations

import inspect
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
_ACC = ROOT / "departments" / "05-accounting-portfolio"
for _p in (
    ROOT / "departments" / "02-trading" / "contracts",
    _ACC / "ledger",
    _ACC / "portfolio",
    _ACC / "reconciliation",
    _ACC / "reporting",
    _ACC / "corporate_actions",
    _ACC / "treasury",
):
    sys.path.insert(0, str(_p))

# 부서마다 같은 이름의 최상위 모듈이 있다 - `repository`는 회계본부와 QA·감사본부
# 양쪽에 있다. pytest는 한 프로세스에서 여러 부서 테스트를 모으므로 먼저 수집된
# 부서의 모듈이 `sys.modules`에 남아 있고, 그 상태로 `fill_consumer`를 읽으면
# 회계 `repository` 대신 남의 부서 것이 들어온다(ImportError).
# 우리 것으로 읽은 뒤 **원래 있던 것을 도로 돌려놓는다** - 여기서 이긴 채로 두면
# 다음에 수집되는 부서가 같은 사고를 반대 방향으로 겪는다.
_AMBIGUOUS = ("break_triage", "contracts", "corporate_actions", "daily_report",
              "fill_consumer", "ledger", "portfolio", "reconciliation", "repository",
              "settlement")
_OTHERS = {name: sys.modules.pop(name) for name in _AMBIGUOUS if name in sys.modules}

from break_triage import check_aging  # noqa: E402
from contracts import Side  # noqa: E402
from corporate_actions import (  # noqa: E402
    ActionStatus,
    ActionType,
    CorporateAction,
    CorporateActionError,
    apply_corporate_action,
)
from daily_report import build_daily_report  # noqa: E402
import fill_consumer as fill_consumer_module  # noqa: E402
from fill_consumer import FillRow, consume_fill  # noqa: E402
from ledger import PAYABLE, SECURITIES, Ledger, LedgerError  # noqa: E402
from portfolio import MarkPrice, ValuationError, value_portfolio  # noqa: E402
from reconciliation import FillRecord, reconcile_fills  # noqa: E402
from settlement import build_ladder, settle_due, settlement_date_for  # noqa: E402

for _name in _AMBIGUOUS:
    sys.modules.pop(_name, None)
sys.modules.update(_OTHERS)

CAPITAL = D("100000000")   # 1억
BUY_PRICE = D("70000")
SELL_PRICE = D("71000")
QTY = D("100")
FEE = D("105")


class AccountingCloseLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.fund, self.book, self.instrument = uuid4(), uuid4(), uuid4()
        self.ledger = Ledger(fund_id=self.fund, book_id=self.book)
        self.ledger.post_capital(CAPITAL, self.now, "capital_seed")

    # -- 헬퍼 ---------------------------------------------------------------

    def fill(self, side: Side, quantity=QTY, price=BUY_PRICE, *, tax=D("0"), when=None,
             broker_fill_id="bf-1") -> FillRow:
        return FillRow(
            fill_id=uuid4(), instrument_id=self.instrument, side=side,
            quantity=quantity, price=price, fee=FEE, tax=tax,
            event_time=when or self.now, broker_fill_id=broker_fill_id,
            trace_id="acc-e2e",
        )

    def snapshot(self, price, when=None, *, marks=None):
        when = when or self.now
        positions, _ = self.ledger.rebuild()
        if marks is None:
            marks = {i: MarkPrice(i, D(price), when) for i in positions}
        return value_portfolio(self.ledger, marks, when)

    # -- 1. 체결 -> 분개 -> Position/Cash ------------------------------------

    def test_fill_becomes_balanced_journal_and_projection(self) -> None:
        journal = consume_fill(self.ledger, self.fill(Side.BUY))

        debit = sum(line.debit for line in journal.lines)
        credit = sum(line.credit for line in journal.lines)
        self.assertEqual(debit, credit, "불균형 분개가 통과했다")
        # T+2 결제라 체결일 대변은 현금이 아니라 미지급금이다.
        self.assertEqual(
            {line.account_code for line in journal.lines},
            {SECURITIES, PAYABLE, "5000"},
            "매수 분개가 유가증권/미지급금/수수료 3계정이 아니다",
        )

        positions, cash = self.ledger.rebuild()
        self.assertEqual(positions[self.instrument].quantity, QTY)
        self.assertEqual(cash, CAPITAL, "체결일에 현금이 움직였다")
        self.assertEqual(-self.ledger.trial_balance()[PAYABLE], QTY * BUY_PRICE + FEE)
        self.assertEqual(sum(self.ledger.trial_balance().values()), D("0"),
                         "시산표가 균형이 아니다")

    def test_same_fill_is_journaled_once(self) -> None:
        """같은 체결이 두 번 와도 분개는 하나다 (멱등 키 = broker_fill_id)."""
        consume_fill(self.ledger, self.fill(Side.BUY))
        before = len(self.ledger.journals)
        consume_fill(self.ledger, self.fill(Side.BUY))
        self.assertEqual(len(self.ledger.journals), before, "같은 체결이 두 번 분개됐다")

    def test_recovered_fills_use_broker_time_not_discovery_order(self) -> None:
        source = inspect.getsource(fill_consumer_module.pending_fill_events)
        normalized = " ".join(source.split()).lower()

        self.assertIn(
            "order by delivered.event_time, delivered.outbox_id",
            normalized,
        )
        self.assertNotIn("order by delivered.outbox_id", normalized)

    # -- 2. Mark 없으면 NAV를 만들지 않는다 (fail-closed) ---------------------

    def test_fill_ack_is_not_blocked_by_missing_nav_mark(self) -> None:
        """Durable Journal/projection, not NAV availability, is the Fill ACK boundary."""
        event_id = uuid4()
        fill = FillRow(
            fill_id=uuid4(), instrument_id=self.instrument, side=Side.BUY,
            quantity=D("1"), price=BUY_PRICE, fee=FEE, tax=D("0"),
            event_time=self.now, broker_fill_id="bf-ack-boundary",
            event_id=event_id, trace_id="acc-ack-boundary",
        )

        class FakeRepository:
            def __init__(self, ledger):
                self.ledger = ledger
                self.projection_saves = 0
                self.snapshot_saves = 0

            def load(self, _fund_id, _book_id):
                return self.ledger

            def save_projection(self, _ledger):
                self.projection_saves += 1

            def save_snapshot(self, _snapshot):
                self.snapshot_saves += 1

        repo = FakeRepository(self.ledger)
        acknowledged = []

        def record_ack(_repo, fill_ids=None, *, event_ids=None):
            acknowledged.extend(event_ids or fill_ids or [])
            return len(event_ids or fill_ids or [])

        with (
            patch.object(fill_consumer_module, "pending_fill_events", return_value=[fill]),
            patch.object(fill_consumer_module, "ack_fill_events", side_effect=record_ack),
        ):
            with self.assertRaises(ValuationError):
                fill_consumer_module.run_once(
                    repo, self.fund, self.book, {}, self.now,
                )

        self.assertEqual(repo.projection_saves, 1)
        self.assertEqual(acknowledged, [event_id])
        self.assertEqual(repo.snapshot_saves, 0)

    def test_nav_is_refused_without_mark(self) -> None:
        consume_fill(self.ledger, self.fill(Side.BUY))

        with self.assertRaises(ValuationError):
            value_portfolio(self.ledger, {}, self.now)

        stale = {self.instrument: MarkPrice(self.instrument, BUY_PRICE,
                                            self.now - timedelta(hours=3))}
        with self.assertRaises(ValuationError):
            value_portfolio(self.ledger, stale, self.now)

        snap = self.snapshot(BUY_PRICE)
        self.assertEqual(snap.nav, CAPITAL - FEE)   # 매수 직후 평가손익 0
        self.assertEqual(snap.quantity_of(self.instrument), QTY)

    # -- 3. 두 스냅샷 -> 일일 보고. 미설명 손익 0 -----------------------------

    def test_daily_report_explains_every_won(self) -> None:
        open_snap = self.snapshot(BUY_PRICE, self.now)              # 보유 0, 자본금만
        consume_fill(self.ledger, self.fill(Side.BUY))
        sell_time = self.now + timedelta(hours=1)
        consume_fill(self.ledger, self.fill(Side.SELL, quantity=D("40"), price=SELL_PRICE,
                                            tax=D("400"), when=sell_time,
                                            broker_fill_id="bf-2"))
        close_snap = self.snapshot(SELL_PRICE, sell_time)

        report = build_daily_report(
            snapshots=[open_snap, close_snap],
            ledger=self.ledger,
            accounting_date=self.now.date(),
        )

        # 실현: 40주 x (71000 - 70000) = 40,000
        self.assertEqual(report.realized_pnl, D("40000"))
        # 비용: 매수 수수료 + 매도 수수료 + 거래세
        self.assertEqual(report.cost_total, FEE + FEE + D("400"))
        self.assertEqual(report.net_pnl, report.pnl_total - report.cost_total)
        self.assertEqual(report.unexplained_pnl, D("0"),
                         "NAV 변화가 손익·비용으로 전부 설명되지 않는다")
        self.assertFalse(report.to_dict()["is_official"],
                         "Preliminary 보고가 공식으로 나왔다")

    def test_daily_report_needs_two_snapshots(self) -> None:
        with self.assertRaises(Exception):
            build_daily_report(snapshots=[self.snapshot(BUY_PRICE)], ledger=self.ledger,
                               accounting_date=self.now.date())

    # -- 3-2. 결제(T+2) - 원장 현금과 가용 현금이 갈라진다 ---------------------

    def test_cash_moves_on_settlement_date_not_trade_date(self) -> None:
        trade_day = self.now.date()
        consume_fill(self.ledger, self.fill(Side.BUY))
        due = settlement_date_for(trade_day)
        self.assertGreater(due, trade_day, "T+2인데 결제일이 체결일과 같다")

        ladder = build_ladder(self.ledger, trade_day)
        self.assertEqual(ladder["available_cash"], CAPITAL)
        self.assertEqual(ladder["buckets"][0]["net"], D("0"), "체결일에 현금이 잡혔다")
        bucket = next(b for b in ladder["buckets"] if b["date"] == due.isoformat())
        self.assertEqual(bucket["outgoing"], QTY * BUY_PRICE + FEE)
        self.assertEqual(bucket["projected_cash"], CAPITAL - QTY * BUY_PRICE - FEE)
        self.assertEqual(ladder["overdue"], [])

        # NAV는 결제 전후로 같다 - 미지급금이 이미 NAV에 들어 있다
        nav_before = self.snapshot(BUY_PRICE).nav
        self.assertEqual(settle_due(self.ledger, trade_day, now=self.now), [],
                         "결제일 전에 현금이 나갔다")
        settled = settle_due(self.ledger, due, now=self.now)
        self.assertEqual(len(settled), 1)

        _, cash = self.ledger.rebuild()
        self.assertEqual(cash, CAPITAL - QTY * BUY_PRICE - FEE)
        self.assertEqual(self.ledger.trial_balance().get(PAYABLE, D("0")), D("0"))
        self.assertEqual(self.snapshot(BUY_PRICE).nav, nav_before,
                         "결제가 NAV를 바꿨다")
        self.assertEqual(settle_due(self.ledger, due, now=self.now), [],
                         "같은 체결이 두 번 결제됐다")

    # -- 4. 대사 -> Break -> Aging -------------------------------------------

    def test_reconciliation_raises_break_and_break_ages(self) -> None:
        consume_fill(self.ledger, self.fill(Side.BUY))

        internal = [FillRecord(instrument_id=self.instrument, side=Side.BUY, quantity=QTY,
                               price=BUY_PRICE, event_time=self.now,
                               broker_fill_id="bf-1", fee=FEE, ref="internal-1")]
        external = [FillRecord(instrument_id=self.instrument, side=Side.BUY, quantity=D("90"),
                               price=BUY_PRICE, event_time=self.now,
                               broker_fill_id="bf-1", fee=FEE, ref="stmt-1")]

        clean = reconcile_fills(internal, list(internal), as_of=self.now)
        self.assertEqual(clean.result, "matched", "일치하는 명세에서 Break이 났다")

        result = reconcile_fills(internal, external, as_of=self.now)
        self.assertNotEqual(result.result, "matched", "수량 차이를 못 잡았다")
        self.assertTrue(result.breaks)

        brk = result.breaks[0]
        aged = check_aging(
            [{"break_id": str(brk.break_id), "severity": str(brk.severity),
              "status": brk.status, "kind": brk.kind,
              "created_at": self.now - timedelta(days=30)}],
            now=self.now,
        )
        self.assertTrue(aged["sla_breached"], aged)
        self.assertEqual(len(aged["overdue"]), 1, aged)
        self.assertNotIn("WITHIN_SLA", aged["by_aging_status"],
                         "30일 된 Break이 기한 안이라고 나왔다")

        # 보고서가 Break을 세되, 판정은 대사 쪽 severity 그대로다
        report = build_daily_report(
            snapshots=[self.snapshot(BUY_PRICE, self.now),
                       self.snapshot(BUY_PRICE, self.now + timedelta(hours=1))],
            ledger=self.ledger, accounting_date=self.now.date(), breaks=result.breaks,
        )
        self.assertEqual(report.material_break_count,
                         len(result.material_breaks))

    # -- 5. Posted 분개는 고치지 않는다. 역분개만 -----------------------------

    def test_posted_journal_is_reversed_not_edited(self) -> None:
        journal = consume_fill(self.ledger, self.fill(Side.BUY))
        self.assertNotEqual(self.ledger.trial_balance().get(PAYABLE, D("0")), D("0"))

        reversal = self.ledger.reverse(journal.journal_id, "브로커 체결 취소 통보")
        self.assertEqual(journal.status, "reversed")
        self.assertEqual(reversal.reversal_of, journal.journal_id)
        self.assertIn(journal, self.ledger.journals, "원본 분개가 사라졌다")

        positions, cash = self.ledger.rebuild()
        # 수량 0인 포지션은 남기지 않는다 - "0주 보유"와 "보유한 적 없음"을 화면에서
        # 구분할 필요가 없고, 남기면 빈 행이 스냅샷마다 쌓인다.
        self.assertEqual(positions.get(self.instrument, None), None)
        self.assertEqual(cash, CAPITAL, "역분개 후 현금이 원복되지 않았다")
        self.assertEqual(self.ledger.trial_balance().get(PAYABLE, D("0")), D("0"),
                         "역분개 후 미지급금이 남았다")
        self.assertEqual(sum(self.ledger.trial_balance().values()), D("0"))

        with self.assertRaises(LedgerError):
            self.ledger.reverse(journal.journal_id, "두 번째 역분개")
        with self.assertRaises(LedgerError):
            self.ledger.reverse(reversal.journal_id, "역분개의 역분개")

    # -- 6. 기업행위 ----------------------------------------------------------

    def test_effective_dividend_posts_and_announced_does_not(self) -> None:
        consume_fill(self.ledger, self.fill(Side.BUY))
        positions, cash_before = self.ledger.rebuild()
        position = positions[self.instrument]

        def action(status: ActionStatus) -> CorporateAction:
            return CorporateAction(
                action_id=f"ca-{status}", action_type=ActionType.CASH_DIVIDEND,
                instrument_id=self.instrument,
                record_date=self.now - timedelta(days=2),
                effective_at=self.now - timedelta(days=1),
                status=status, amount_per_share=D("500"), withholding_tax=D("7700"),
            )

        # 공시만으로는 분개하지 않는다
        with self.assertRaises(CorporateActionError):
            apply_corporate_action(self.ledger, action(ActionStatus.ANNOUNCED), position,
                                   record_date_quantity=QTY, now=self.now)

        apply_corporate_action(self.ledger, action(ActionStatus.EFFECTIVE), position,
                               record_date_quantity=QTY, now=self.now)
        _, cash_after = self.ledger.rebuild()
        self.assertEqual(cash_after - cash_before, QTY * D("500") - D("7700"))
        self.assertEqual(sum(self.ledger.trial_balance().values()), D("0"))


if __name__ == "__main__":
    unittest.main()
