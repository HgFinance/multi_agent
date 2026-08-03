"""Paper 거래 한 바퀴 관통 테스트.

소유: 도현 (트레이딩 + 회계·포트폴리오)
범위: F15 -> F11 -> F14 -> F13 -> 원장 -> F15

    PortfolioSnapshot(NAV, 보유수량)
      -> StrategySignal
      -> OrderIntent            (F11 build_order_intent)
      -> RiskDecision           (F12 RiskEngine.check_order - 동규님 리스크본부)
      -> BrokerOrder            (F14 create_broker_order, submit)
      -> ack / fill             (F13 PaperBroker)
      -> Journal                (원장 post_fill)
      -> PortfolioSnapshot      (F15 value_portfolio)

각 모듈의 자체 점검은 자기 자리만 본다. 이 파일은 **모듈 사이의 배선**을 본다 -
한쪽 출력이 다른 쪽 입력으로 실제로 들어가는지, 한 바퀴 돈 뒤 상태가 수렴하는지.

판정은 대역이 아니라 실제 RiskEngine이 낸다. 우리는 Mandate와 한도를 입력으로
줄 뿐이고 approve/resize/reject 판단에는 관여하지 않는다. RiskEngine이 요구하는
PortfolioState는 "portfolio-api가 줄 값의 스텁"이라 주석돼 있는데, 그 portfolio-api가
바로 F15다 - _risk_context()가 그 어댑터다.

실행: python -m unittest discover -s tests/e2e
      (tests/에 __init__.py가 없어 상위에서 discover하면 0건이 나온다.
       tests/schema를 도는 .github/workflows/schema-contract.yml과 같은 방식이다.)
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
for _p in (
    ROOT / "departments" / "02-trading" / "contracts",
    ROOT / "departments" / "02-trading" / "oms",
    ROOT / "departments" / "02-trading" / "broker",
    ROOT / "departments" / "03-risk" / "engine",
    ROOT / "departments" / "05-accounting-portfolio" / "ledger",
    ROOT / "departments" / "05-accounting-portfolio" / "portfolio",
):
    sys.path.insert(0, str(_p))

from contracts import (
    BrokerOrderState,
    IntentState,
    MarketSnapshot,
    RiskVerdict,
    Side,
    StrategySignal,
)
from intent_builder import build_order_intent, load_presets
from ledger import Ledger, Position
from oms import OMS, OMSError
from paper_broker import PaperBroker, Quote
from portfolio import MarkPrice, value_portfolio
from risk_engine import (
    CounterpartyHealth,
    CounterpartyStatus,
    LimitSet,
    MandateScope,
    MarketStatus,
    PortfolioState,
    RiskContext,
    RiskEngine,
    TradingState,
)

CAPITAL = D(1000000000)   # 10억
PRICE = D(70000)


class PaperLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.fund, self.book, self.strategy = uuid4(), uuid4(), uuid4()
        self.instrument = uuid4()
        self.presets = load_presets()
        self.broker = PaperBroker()
        self.oms = OMS()
        self.engine = RiskEngine()

        self.ledger = Ledger(fund_id=self.fund, book_id=self.book)
        self.ledger.post_capital(CAPITAL, self.now, "capital_seed")

    # -- 헬퍼 ---------------------------------------------------------------

    def snapshot(self, price=PRICE):
        """보유 종목이 있으면 Mark를 주고, 없으면 빈 dict로도 평가된다."""
        positions, _ = self.ledger.rebuild()
        marks = {i: MarkPrice(i, D(price), self.now) for i in positions}
        return value_portfolio(self.ledger, marks, self.now)

    def signal(self, weight="0.02"):
        return StrategySignal(
            strategy_id=self.strategy, strategy_version="v1",
            fund_id=self.fund, book_id=self.book, instrument_id=self.instrument,
            philosophy="momentum", target_weight=D(weight), stage="paper",
            as_of=self.now, valid_until=self.now + timedelta(hours=1), trace_id="e2e",
        )

    def market(self):
        return MarketSnapshot(market_snapshot_id="e2e_snap", as_of=self.now,
                              bid=PRICE - D(100), ask=PRICE + D(100))

    def build_intent(self, signal, snap):
        return build_order_intent(
            signal, snapshot=self.market(), nav=snap.nav,
            current_quantity=snap.quantity_of(self.instrument),
            preset=self.presets[signal.philosophy],
            trade_case_id=uuid4(), now=self.now,
        )

    def _risk_context(self, limits=None, mandate=None, trading_state=TradingState.ENABLED):
        """F15 스냅샷을 리스크본부가 요구하는 PortfolioState로 옮긴다.

        RiskEngine은 포지션을 직접 집계하지 않는다(팀 가이드 3.1). 회계본부가
        확정한 수치를 받아서 검사할 뿐이다. 이 어댑터가 그 경계다.
        """
        snap = self.snapshot()
        return RiskContext(
            mandate=mandate or MandateScope(
                fund_id=self.fund, allowed_instrument_ids=None,
                min_order_notional=D(1), max_order_notional=D(1000000000),
            ),
            # 집중도 한도를 100%로 열어둔다. RiskEngine의 집중도 분모가
            # gross_exposure라서 보유가 없는 첫 주문은 정의상 항상 100%가 되고,
            # 어떤 현실적인 한도로도 통과할 수 없다(test_concentration_is_gross_based).
            # 이 파일의 목적은 배선 검증이므로 그 검사만 열고 나머지는 살려둔다.
            # 분모를 NAV로 볼지 gross로 볼지는 미해결 - PR 참조.
            limits=limits or LimitSet(
                soft_single_issuer_pct=D(1), hard_single_issuer_pct=D(1),
                max_daily_turnover_notional=D(1000000000), max_daily_order_count=100,
                max_daily_loss=D(100000000), max_drawdown_pct=D("0.20"),
            ),
            restricted_items=(),
            portfolio=PortfolioState(
                fund_id=self.fund,
                cash=snap.cash,
                buying_power=snap.cash,          # Long-only, 신용 없음
                gross_exposure=snap.gross_exposure,
                positions={p.instrument_id: p.quantity for p in snap.positions},
                realized_pnl_today=snap.realized_pnl,
                unrealized_pnl_today=snap.unrealized_pnl,
                equity=snap.nav,
                peak_equity=max(snap.nav, CAPITAL),
            ),
            market_status=MarketStatus(tradable=True),
            counterparty=CounterpartyStatus("paper", CounterpartyHealth.OK),
            trading_state=trading_state,
            as_of=self.now,
        )

    def assess(self, intent, **ctx_kwargs):
        """실제 Risk Engine 판정. 우리는 입력만 주고 판단에는 관여하지 않는다."""
        return self.engine.check_order(intent, self._risk_context(**ctx_kwargs))

    def route(self, intent, **ctx_kwargs):
        """Intent를 심사 통과시키고 브로커에 전송한다."""
        decision = self.assess(intent, **ctx_kwargs).decision
        rec = self.oms.register_intent(intent)
        self.oms.request_risk_review(rec)
        self.oms.apply_risk_decision(rec, decision)
        order = self.oms.create_broker_order(rec, intent)
        self.oms.submit(order, rec)
        ev = self.broker.accept(order)
        self.assertEqual(ev.event_type, "ack")
        self.oms.on_broker_event(order, "ack", ev.broker_event_id, self.now, ev.payload)
        return rec, order

    def fill_completely(self, order, quote_size="3000"):
        """호가 잔량 한도 때문에 여러 번 나눠 체결된다."""
        quote = Quote(PRICE - D(100), PRICE, D(quote_size), D(quote_size))
        fills = 0
        while (ev := self.broker.try_fill(order, quote, self.now)) is not None:
            self.oms.on_broker_event(order, "fill", ev.broker_event_id, self.now, ev.payload)
            fills += 1
            self.assertLess(fills, 50, "체결 루프가 끝나지 않는다")
        return fills

    def post_fills_to_ledger(self, order):
        for fill in order.fills:
            positions, _ = self.ledger.rebuild()
            self.ledger.post_fill(
                fill, order.side, order.instrument_id,
                positions.get(order.instrument_id, Position(order.instrument_id)),
            )

    # -- 관통 -----------------------------------------------------------------

    def test_full_loop_signal_to_nav(self):
        opening = self.snapshot()
        self.assertEqual(opening.nav, CAPITAL)
        self.assertEqual(opening.quantity_of(self.instrument), D(0))

        # 1. NAV와 보유수량이 Intent 수량을 정한다
        intent = self.build_intent(self.signal(), opening)
        self.assertIsNotNone(intent)
        self.assertIs(intent.side, Side.BUY)
        expected_qty = (CAPITAL * D("0.02") / PRICE).to_integral_value(rounding="ROUND_DOWN")
        self.assertEqual(intent.quantity, expected_qty)

        # 2. 심사 -> 전송 -> 체결
        rec, order = self.route(intent)
        self.assertIs(rec.state, IntentState.READY_TO_SUBMIT)
        self.assertGreater(self.fill_completely(order), 1, "한 번에 다 체결됐다")
        self.assertIs(order.state, BrokerOrderState.FILLED)
        self.assertEqual(order.filled_quantity, intent.quantity)

        # 3. 체결이 원장으로. Position은 주문 의도가 아니라 체결에서 나온다
        self.post_fills_to_ledger(order)
        closing = self.snapshot()
        self.assertEqual(closing.quantity_of(self.instrument), intent.quantity)

        # 4. 현금은 체결금액과 수수료만큼 줄었다
        notional = sum(f.quantity * f.price for f in order.fills)
        fees = sum(f.fee for f in order.fills)
        self.assertEqual(closing.cash, CAPITAL - notional - fees)
        self.assertEqual(closing.fees, fees)

        # 5. 체결가로 평가하면 평가손익은 0이고 NAV는 수수료만큼만 줄어든다
        self.assertEqual(closing.unrealized_pnl, D(0))
        self.assertEqual(closing.nav, CAPITAL - fees)
        self.assertEqual(closing.realized_pnl, D(0))

        # 6. NAV 항등식
        self.assertEqual(
            closing.nav,
            closing.cash + closing.securities_value + closing.receivable - closing.payable,
        )

        # 7. 원장 차대균형
        self.assertEqual(sum(self.ledger.trial_balance().values()), D(0))

    def test_loop_converges_on_second_pass(self):
        """같은 시그널을 다시 돌리면 목표에 도달해 주문이 생기지 않는다.

        이게 성립하지 않으면 매 주기마다 같은 종목을 계속 사들인다.
        """
        intent = self.build_intent(self.signal(), self.snapshot())
        _, order = self.route(intent)
        self.fill_completely(order)
        self.post_fills_to_ledger(order)

        again = self.build_intent(self.signal(), self.snapshot())
        self.assertIsNone(again, "목표 도달인데 또 주문이 만들어졌다")

    def test_price_rise_does_not_move_cash(self):
        """평가는 NAV를 바꾸지만 현금은 못 바꾼다."""
        intent = self.build_intent(self.signal(), self.snapshot())
        _, order = self.route(intent)
        self.fill_completely(order)
        self.post_fills_to_ledger(order)

        at_cost = self.snapshot(price=PRICE)
        higher = self.snapshot(price=PRICE + D(7000))
        self.assertEqual(higher.cash, at_cost.cash)
        self.assertGreater(higher.nav, at_cost.nav)
        self.assertEqual(higher.unrealized_pnl, intent.quantity * D(7000))
        self.assertEqual(higher.realized_pnl, D(0), "팔지 않았는데 실현손익이 생겼다")

    def test_sell_realizes_pnl_through_ledger(self):
        """가격이 오른 뒤 비중을 줄이면 실현손익이 원장에 남는다."""
        intent = self.build_intent(self.signal(), self.snapshot())
        _, order = self.route(intent)
        self.fill_completely(order)
        self.post_fills_to_ledger(order)

        # 목표 비중을 절반으로 낮추면 매도 Intent가 나온다
        higher = self.snapshot(price=PRICE)
        sell_intent = self.build_intent(self.signal(weight="0.01"), higher)
        self.assertIsNotNone(sell_intent)
        self.assertIs(sell_intent.side, Side.SELL)

        _, sell_order = self.route(sell_intent)
        quote = Quote(PRICE + D(7000), PRICE + D(7100), D(3000), D(3000))
        while (ev := self.broker.try_fill(sell_order, quote, self.now)) is not None:
            self.oms.on_broker_event(sell_order, "fill", ev.broker_event_id, self.now, ev.payload)
        self.assertIs(sell_order.state, BrokerOrderState.FILLED)
        self.post_fills_to_ledger(sell_order)

        after = self.snapshot(price=PRICE + D(7000))
        self.assertGreater(after.realized_pnl, D(0), "이익 실현이 원장에 안 잡혔다")
        self.assertGreater(after.taxes, D(0), "매도 거래세가 없다")
        self.assertEqual(
            after.quantity_of(self.instrument),
            intent.quantity - sell_intent.quantity,
        )
        self.assertEqual(sum(self.ledger.trial_balance().values()), D(0))

    # -- 경계 -----------------------------------------------------------------

    def test_concentration_is_gross_based_not_nav_based(self):
        """집중도 분모가 NAV가 아니라 gross_exposure다. 미해결 항목을 고정해 둔다.

        F11은 NAV 대비 2%로 수량을 정하는데(philosophies.yaml max_weight),
        RiskEngine은 gross_exposure 대비로 집중도를 본다. 보유가 없으면
        분모가 그 주문 자체라 첫 주문은 언제나 100%가 되고, 10% Hard Limit 같은
        현실적인 값으로는 포트폴리오를 시작할 수 없다.

        정의가 정해지면 이 테스트가 먼저 깨진다. 그때 함께 고친다.
        """
        realistic = LimitSet(
            soft_single_issuer_pct=D("0.05"), hard_single_issuer_pct=D("0.10"),
            max_daily_turnover_notional=D(1000000000), max_daily_order_count=100,
            max_daily_loss=D(100000000), max_drawdown_pct=D("0.20"),
        )
        intent = self.build_intent(self.signal(), self.snapshot())

        # NAV 대비로는 2%다 - 어떤 상식적인 한도도 넘지 않는다
        nav_pct = intent.quantity * intent.limit_price / self.snapshot().nav
        self.assertLess(nav_pct, D("0.03"))

        # 그런데 gross 대비로는 100%라 Hard Limit에서 거부된다
        assessment = self.assess(intent, limits=realistic)
        self.assertIs(assessment.decision.verdict, RiskVerdict.REJECT)
        self.assertIn("concentration_limit_hard",
                      [r.value for r in assessment.reason_codes])

    def test_risk_gate_blocks_unapproved_order(self):
        """Risk 판정 없이는 Broker Order 자체가 생기지 않는다."""
        intent = self.build_intent(self.signal(), self.snapshot())
        rec = self.oms.register_intent(intent)
        with self.assertRaises(OMSError):
            self.oms.create_broker_order(rec, intent)
        self.oms.request_risk_review(rec)
        with self.assertRaises(OMSError):
            self.oms.create_broker_order(rec, intent)

    def test_duplicate_broker_event_does_not_double_position(self):
        """브로커 재전송이 포지션을 두 배로 만들지 않는다."""
        intent = self.build_intent(self.signal(), self.snapshot())
        _, order = self.route(intent)
        ev = self.broker.try_fill(
            order, Quote(PRICE - D(100), PRICE, D(3000), D(3000)), self.now
        )
        self.oms.on_broker_event(order, "fill", ev.broker_event_id, self.now, ev.payload)
        self.oms.on_broker_event(order, "fill", ev.broker_event_id, self.now, ev.payload)
        self.assertEqual(len(order.fills), 1)

        self.post_fills_to_ledger(order)
        self.post_fills_to_ledger(order)   # 원장 재처리도 멱등이어야 한다
        self.assertEqual(
            self.snapshot().quantity_of(self.instrument), order.filled_quantity
        )

    def test_unpriced_position_blocks_next_order(self):
        """평가할 수 없으면 NAV가 없고, NAV가 없으면 다음 주문도 못 만든다.

        Entry 차단 방향으로 실패한다 (마스터플랜 25장).
        """
        from portfolio import ValuationError

        intent = self.build_intent(self.signal(), self.snapshot())
        _, order = self.route(intent)
        self.fill_completely(order)
        self.post_fills_to_ledger(order)

        with self.assertRaises(ValuationError):
            value_portfolio(self.ledger, {}, self.now)

    def test_position_only_moves_on_fills(self):
        """Intent를 만들고 승인만 받고 체결이 없으면 포지션은 그대로다."""
        intent = self.build_intent(self.signal(), self.snapshot())
        rec, order = self.route(intent)
        self.assertIs(order.state, BrokerOrderState.ACKNOWLEDGED)
        self.post_fills_to_ledger(order)
        snap = self.snapshot()
        self.assertEqual(snap.quantity_of(self.instrument), D(0))
        self.assertEqual(snap.nav, CAPITAL)


if __name__ == "__main__":
    unittest.main()
