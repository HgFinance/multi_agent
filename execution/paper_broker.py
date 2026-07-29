#!/usr/bin/env python3
"""Sprint D1: Paper Broker.

소유: 도현 (트레이딩본부)
근거: docs/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 3.3(P0 수집 범위), 4.2
      docs/HEDGE_FUND_MASTER_PLAN.md 12.2(모의 체결)

OMS와 분리된 별도 모듈이다. 팀 가이드 5.1에서 svc_oms와 svc_broker_adapter의
권한이 다르게 정의돼 있고, P1에서 실거래 Broker Adapter로 교체될 자리이기 때문이다.
가격 Source가 LS로 확정됐다고 실거래 Broker도 LS라고 가정하지 않는다(가이드 3.3).

이 클래스는 OMS를 호출하지 않는다. 이벤트를 만들어 돌려주고, 그걸 OMS에
넣는 것은 호출자의 몫이다. 브로커가 우리 주문 상태를 직접 바꿀 수 없어야 한다.

자체 점검: python execution/paper_broker.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading.contracts import OrderType, Side  # noqa: E402

WON = Decimal("1")
QTY_UNIT = Decimal("1")  # 국내 주식은 1주 단위


@dataclass(frozen=True)
class BrokerEvent:
    """브로커가 알려준 사실. OMS.on_broker_event가 그대로 소비한다."""

    event_type: str          # ack | fill | reject | cancel | expire
    broker_event_id: str
    event_time: datetime
    payload: dict


@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal


def tick_size(price: Decimal) -> Decimal:
    """KRX 호가 단위. 가격대별로 다르다.

    2023-01 개편 기준. 이 값이 틀리면 지정가가 거래소에서 거부되므로
    실거래 전에 반드시 최신 규정과 대조해야 한다.
    """
    p = int(price)
    if p < 2_000:
        return Decimal("1")
    if p < 5_000:
        return Decimal("5")
    if p < 20_000:
        return Decimal("10")
    if p < 50_000:
        return Decimal("50")
    if p < 200_000:
        return Decimal("100")
    if p < 500_000:
        return Decimal("500")
    return Decimal("1000")


def is_valid_tick(price: Decimal) -> bool:
    return price % tick_size(price) == 0


class PaperBroker:
    """모의 체결. 실제 시장에 주문을 내지 않는다.

    체결 규칙:
      - 지정가 매수는 ask <= limit 일 때만 체결된다. 가격이 안 맞으면 미체결로 남는다.
      - 한 번에 반대 호가 잔량까지만 체결된다. 나머지는 부분 체결로 남는다.
      - 참여율 상한을 적용해 현실보다 낙관적인 체결을 막는다.

    ponytail: 호가 잔량 기반의 단순 모델이다. 시장충격·큐 대기·취소 재주문을
              반영하지 않으므로 체결률이 실제보다 낙관적이다. D4에서 TCA 실측이
              쌓이면 그 분포로 교정한다. Paper 단계 검증에는 충분하다.
    """

    def __init__(self, fee_bps: Decimal = Decimal("1.5"), sell_tax_bps: Decimal = Decimal("15"),
                 max_participation: Decimal = Decimal("0.05")) -> None:
        # 기본값은 trading/philosophies.yaml의 market 블록과 같은 값이다.
        self.fee_bps = fee_bps
        self.sell_tax_bps = sell_tax_bps
        self.max_participation = max_participation
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"paper_{prefix}_{self._seq:06d}"

    # -- 주문 접수 --------------------------------------------------------------

    def accept(self, order, when: datetime | None = None) -> BrokerEvent:
        """주문 유효성 검사 후 ack 또는 reject.

        거래소가 거부할 주문을 여기서 먼저 걸러낸다. Paper에서 통과한 주문이
        Live에서 거부되면 Paper-Live 괴리가 생긴다(마스터플랜 25장).
        """
        when = when or datetime.now(timezone.utc)

        reason = None
        if order.requested_quantity % QTY_UNIT != 0:
            reason = f"수량 단위 위반: {order.requested_quantity}"
        elif order.limit_price is not None and not is_valid_tick(order.limit_price):
            reason = f"호가 단위 위반: {order.limit_price} (단위 {tick_size(order.limit_price)})"

        if reason:
            return BrokerEvent("reject", self._next_id("rej"), when, {"reason": reason})
        return BrokerEvent("ack", self._next_id("ack"), when,
                           {"broker_order_id": self._next_id("ord")})

    # -- 체결 ------------------------------------------------------------------

    def try_fill(self, order, quote: Quote, when: datetime | None = None) -> BrokerEvent | None:
        """현재 호가로 체결 가능한 만큼 체결한다. 불가능하면 None."""
        when = when or datetime.now(timezone.utc)
        leaves = order.requested_quantity - order.filled_quantity
        if leaves <= 0:
            return None

        if order.side is Side.BUY:
            available, exec_price = quote.ask_size, quote.ask
            crossed = order.limit_price is None or quote.ask <= order.limit_price
        else:
            available, exec_price = quote.bid_size, quote.bid
            crossed = order.limit_price is None or quote.bid >= order.limit_price

        if not crossed:
            return None  # 가격 미도달. 미체결로 남는다 - 추정 체결 금지

        cap = (available * self.max_participation).quantize(QTY_UNIT, rounding=ROUND_DOWN)
        qty = min(leaves, max(cap, QTY_UNIT))
        if qty <= 0:
            return None

        notional = qty * exec_price
        fee = (notional * self.fee_bps / 10000).quantize(WON, rounding=ROUND_HALF_UP)
        tax = (
            (notional * self.sell_tax_bps / 10000).quantize(WON, rounding=ROUND_HALF_UP)
            if order.side is Side.SELL
            else Decimal(0)
        )

        return BrokerEvent(
            "fill", self._next_id("fill"), when,
            {
                "quantity": str(qty),
                "price": str(exec_price),
                "fee": str(fee),
                "tax": str(tax),
                "broker_fill_id": self._next_id("bf"),
            },
        )

    def cancel(self, order, when: datetime | None = None) -> BrokerEvent:
        return BrokerEvent("cancel", self._next_id("cxl"),
                           when or datetime.now(timezone.utc),
                           {"leaves": str(order.requested_quantity - order.filled_quantity)})


if __name__ == "__main__":
    from datetime import timedelta
    from decimal import Decimal as D
    from uuid import uuid4

    from trading.contracts import MarketSnapshot, OrderIntent, OrderState, RiskDecision, RiskVerdict

    from oms import OMS  # noqa: E402

    now = datetime.now(timezone.utc)

    # 1. 호가 단위 테이블
    assert tick_size(D("1500")) == D("1")
    assert tick_size(D("70000")) == D("100")
    assert tick_size(D("300000")) == D("500")
    assert is_valid_tick(D("70100")) and not is_valid_tick(D("70050"))

    broker = PaperBroker()

    def build(qty="100", price="70000", side=Side.BUY, key="idem_p001"):
        snap = MarketSnapshot(market_snapshot_id="s", as_of=now, bid=D("69900"), ask=D("70000"))
        intent = OrderIntent(
            trade_case_id=uuid4(), fund_id=uuid4(), book_id=uuid4(), strategy_id=uuid4(),
            instrument_id=uuid4(), side=side, order_type=OrderType.LIMIT,
            quantity=D(qty), limit_price=D(price), valid_until=now + timedelta(hours=1),
            snapshot=snap, idempotency_key=key, created_by="pm", trace_id="t", created_at=now,
        )
        oms = OMS()
        order = oms.create_order(intent)
        oms.request_risk_review(order)
        oms.apply_risk_decision(order, RiskDecision(
            order_intent_id=intent.order_intent_id, verdict=RiskVerdict.APPROVE,
            approved_quantity=D(qty), expires_at=now + timedelta(minutes=5),
            decided_by="risk", decided_at=now,
        ), intent)
        oms.submit(order, intent)
        return oms, order, intent

    # 2. 호가 단위 위반 주문은 브로커가 거부한다
    oms, order, _ = build(price="70050")
    ev = broker.accept(order)
    assert ev.event_type == "reject" and "호가 단위" in ev.payload["reason"]
    oms.on_broker_event(order, ev.event_type, ev.broker_event_id, now, ev.payload)
    assert order.state is OrderState.REJECTED

    # 3. 정상 주문 ack -> 체결
    oms, order, _ = build(key="idem_p002")
    ev = broker.accept(order)
    assert ev.event_type == "ack"
    oms.on_broker_event(order, "ack", ev.broker_event_id, now, ev.payload)

    # 가격 미도달이면 체결되지 않는다 (ask 70100 > limit 70000)
    assert broker.try_fill(order, Quote(D("70000"), D("70100"), D("1000"), D("1000"))) is None
    assert order.filled_quantity == 0, "가격 미도달인데 체결됐다"

    # 가격 도달 - 참여율 5%, 잔량 1000 -> 50주만 체결
    fill = broker.try_fill(order, Quote(D("69900"), D("70000"), D("1000"), D("1000")))
    assert fill is not None and fill.payload["quantity"] == "50", fill
    oms.on_broker_event(order, "fill", fill.broker_event_id, now, fill.payload)
    assert order.state is OrderState.PARTIALLY_FILLED and order.filled_quantity == D("50")

    # 잔량 전부 체결
    fill2 = broker.try_fill(order, Quote(D("69900"), D("70000"), D("5000"), D("5000")))
    assert fill2.payload["quantity"] == "50", "잔량 초과 체결 시도"
    oms.on_broker_event(order, "fill", fill2.broker_event_id, now, fill2.payload)
    assert order.state is OrderState.FILLED
    assert broker.try_fill(order, Quote(D("69900"), D("70000"), D("5000"), D("5000"))) is None

    # 4. 매수에는 거래세가 없고 매도에는 있다
    buy_fee = D(fill.payload["fee"])
    assert buy_fee == (D("50") * D("70000") * D("1.5") / 10000).quantize(WON)
    assert D(fill.payload["tax"]) == 0, "매수에 거래세가 붙었다"

    oms_s, order_s, _ = build(side=Side.SELL, price="69900", key="idem_p003")
    ev = broker.accept(order_s)
    oms_s.on_broker_event(order_s, "ack", ev.broker_event_id, now, ev.payload)
    sell = broker.try_fill(order_s, Quote(D("69900"), D("70000"), D("2000"), D("2000")))
    assert D(sell.payload["tax"]) > 0, "매도에 거래세가 없다"
    assert D(sell.payload["price"]) == D("69900"), "매도가 ask에 체결됐다"

    print("ok - Paper Broker 4개 영역 점검 통과")
