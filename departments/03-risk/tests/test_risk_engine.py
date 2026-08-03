"""risk_engine.py의 __main__ 자체 점검을 pytest로 옮긴 것.

소유: 동규 (리스크본부). CLAUDE.md "실제로 도입하면 위 자체 점검을 pytest로 옮기고
이 절을 갱신한다"에 따른 전환분. 시나리오 번호와 내용은 원본과 동일하게 유지한다.

실행: python -m pytest departments/03-risk/tests/test_risk_engine.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

_ENGINE_DIR = Path(__file__).resolve().parent.parent / "engine"
_CONTRACTS_DIR = Path(__file__).resolve().parent.parent.parent / "02-trading" / "contracts"
_OMS_DIR = Path(__file__).resolve().parent.parent.parent / "02-trading" / "oms"
for _p in (_ENGINE_DIR, _CONTRACTS_DIR, _OMS_DIR):
    sys.path.insert(0, str(_p))

from contracts import (
    BrokerOrderState,
    MarketSnapshot,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
)
from oms import OMS
from risk_engine import (
    CounterpartyHealth,
    CounterpartyStatus,
    LimitSet,
    MandateScope,
    MarketStatus,
    PortfolioState,
    RejectReason,
    RestrictedItem,
    RestrictionType,
    RiskContext,
    RiskEngine,
    RiskVerdict,
    TradingState,
)

now = datetime.now(timezone.utc)
fund, book, strategy = uuid4(), uuid4(), uuid4()
aapl, msft = uuid4(), uuid4()
engine = RiskEngine()


def snap(bid="69900", ask="70000", as_of=None) -> MarketSnapshot:
    return MarketSnapshot(
        market_snapshot_id="s1", as_of=as_of or now, bid=Decimal(bid), ask=Decimal(ask),
    )


def make_intent(instrument=aapl, qty="100", price="70000", side=Side.BUY,
                 key="idem_r001", snapshot=None) -> OrderIntent:
    return OrderIntent(
        trade_case_id=uuid4(), fund_id=fund, book_id=book, strategy_id=strategy,
        instrument_id=instrument, side=side, order_type=OrderType.LIMIT,
        quantity=Decimal(qty), limit_price=Decimal(price),
        time_in_force=TimeInForce.DAY, valid_until=now + timedelta(hours=1),
        snapshot=snapshot or snap(), idempotency_key=key,
        created_by="trader-pm-agent", trace_id="t1", created_at=now,
    )


def base_context(**overrides) -> RiskContext:
    defaults = dict(
        mandate=MandateScope(
            fund_id=fund, allowed_instrument_ids=None,
            min_order_notional=Decimal(100000), max_order_notional=Decimal(50000000),
        ),
        limits=LimitSet(
            soft_single_issuer_pct=Decimal("0.20"), hard_single_issuer_pct=Decimal("0.30"),
            max_daily_turnover_notional=Decimal(100000000), max_daily_order_count=50,
            max_daily_loss=Decimal(10000000), max_drawdown_pct=Decimal("0.20"),
        ),
        restricted_items=(),
        portfolio=PortfolioState(
            fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
            gross_exposure=Decimal(100000000), peak_equity=Decimal(1000000000),
            equity=Decimal(1000000000),
        ),
        market_status=MarketStatus(tradable=True),
        counterparty=CounterpartyStatus(broker_adapter="paper", health=CounterpartyHealth.OK),
        trading_state=TradingState.ENABLED,
        as_of=now,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


def approved(fn, why: str):
    result = fn()
    assert result.decision.verdict is RiskVerdict.APPROVE, f"승인됐어야 함: {why} ({result.decision.reason})"
    return result


def resized(fn, why: str):
    result = fn()
    assert result.decision.verdict is RiskVerdict.RESIZE, f"축소됐어야 함: {why} ({result.decision.reason})"
    return result


def rejected(fn, why: str):
    result = fn()
    assert result.decision.verdict is RiskVerdict.REJECT, f"거부됐어야 함: {why}"
    return result


def test_01_normal_order_approved_all_10_checks_recorded():
    r = approved(lambda: engine.check_order(make_intent(), base_context()), "평범한 매수")
    assert r.decision.approved_quantity == Decimal(100)
    assert len(r.check_results) == 10, "10단계 검사가 다 기록돼야 함"
    assert all(c.passed for c in r.check_results)
    assert r.approved_legs and r.approved_legs[0]["order_intent_id"] == str(r.decision.order_intent_id)


def test_02_reproducibility_same_input_hash():
    same_intent = make_intent()
    r_repeat = engine.check_order(same_intent, base_context())
    assert r_repeat.input_hash == engine.check_order(same_intent, base_context()).input_hash, \
        "같은 Intent·Context인데 해시가 다름"
    r = engine.check_order(make_intent(), base_context())
    r3 = engine.check_order(make_intent(qty="101"), base_context())
    assert r.input_hash != r3.input_hash, "다른 입력인데 해시가 같음"


def test_03_max_notional_exceeded_resize():
    tight_mandate_ctx = base_context(mandate=MandateScope(
        fund_id=fund, allowed_instrument_ids=None,
        min_order_notional=Decimal(100000), max_order_notional=Decimal(3500000),
    ))
    r = resized(lambda: engine.check_order(make_intent(), tight_mandate_ctx), "최대 Notional 초과")
    assert r.decision.approved_quantity == Decimal(50), "3,500,000 / 70,000 = 50주여야 함"
    assert RejectReason.NOTIONAL_ABOVE_MAXIMUM in r.reason_codes


def test_04_below_min_notional_rejected():
    below_min_ctx = base_context(mandate=MandateScope(
        fund_id=fund, allowed_instrument_ids=None,
        min_order_notional=Decimal(50000000), max_order_notional=Decimal(100000000),
    ))
    r = rejected(lambda: engine.check_order(make_intent(), below_min_ctx), "최소 Notional 미달")
    assert RejectReason.NOTIONAL_BELOW_MINIMUM in r.reason_codes


def test_05_restricted_no_trading_rejected():
    restricted_ctx = base_context(restricted_items=(
        RestrictedItem(aapl, RestrictionType.NO_TRADING, now - timedelta(days=1)),
    ))
    r = rejected(lambda: engine.check_order(make_intent(), restricted_ctx), "거래 전면 금지 종목")
    assert RejectReason.RESTRICTED_INSTRUMENT in r.reason_codes


def test_06_no_new_position_blocks_buy_allows_liquidating_sell():
    no_new_pos_ctx = base_context(
        restricted_items=(RestrictedItem(aapl, RestrictionType.NO_NEW_POSITION, now - timedelta(days=1)),),
        portfolio=PortfolioState(
            fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
            gross_exposure=Decimal(100000000), positions={aapl: Decimal(100)},
            peak_equity=Decimal(1000000000), equity=Decimal(1000000000),
        ),
    )
    rejected(lambda: engine.check_order(make_intent(), no_new_pos_ctx), "NO_NEW_POSITION - 매수는 막힘")
    approved(
        lambda: engine.check_order(make_intent(side=Side.SELL, qty="50"), no_new_pos_ctx),
        "NO_NEW_POSITION이어도 청산 매도는 통과해야 함",
    )


def test_07_outside_mandate_universe_rejected():
    narrow_mandate_ctx = base_context(mandate=MandateScope(
        fund_id=fund, allowed_instrument_ids=frozenset({msft}),
        min_order_notional=Decimal(100000), max_order_notional=Decimal(100000000),
    ))
    r = rejected(lambda: engine.check_order(make_intent(instrument=aapl), narrow_mandate_ctx), "유니버스 밖 종목")
    assert RejectReason.OUTSIDE_MANDATE in r.reason_codes


def test_08_stale_snapshot_rejected():
    stale_ctx = base_context(as_of=now + timedelta(seconds=30))
    r = rejected(lambda: engine.check_order(make_intent(snapshot=snap(as_of=now)), stale_ctx), "오래된 스냅샷")
    assert RejectReason.STALE_SNAPSHOT in r.reason_codes


def test_09_market_not_tradable_rejected():
    halted_market_ctx = base_context(market_status=MarketStatus(tradable=False, reason="거래정지"))
    r = rejected(lambda: engine.check_order(make_intent(), halted_market_ctx), "거래정지 종목")
    assert RejectReason.MARKET_NOT_TRADABLE in r.reason_codes


def test_10_insufficient_buying_power_resize():
    low_buying_power_ctx = base_context(portfolio=PortfolioState(
        fund_id=fund, cash=Decimal(3500000), buying_power=Decimal(3500000),
        gross_exposure=Decimal(100000000), peak_equity=Decimal(1000000000),
        equity=Decimal(1000000000),
    ))
    r = resized(lambda: engine.check_order(make_intent(), low_buying_power_ctx), "매수 여력 부족")
    assert r.decision.approved_quantity == Decimal(50)
    assert RejectReason.INSUFFICIENT_BUYING_POWER in r.reason_codes


def test_11_oversell_resize_to_held_quantity():
    oversell_ctx = base_context(portfolio=PortfolioState(
        fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
        gross_exposure=Decimal(100000000), positions={aapl: Decimal(30)},
        peak_equity=Decimal(1000000000), equity=Decimal(1000000000),
    ))
    r = resized(
        lambda: engine.check_order(make_intent(side=Side.SELL, qty="100"), oversell_ctx),
        "보유량 초과 매도",
    )
    assert r.decision.approved_quantity == Decimal(30)
    assert RejectReason.OVERSELL in r.reason_codes
    rejected(
        lambda: engine.check_order(make_intent(side=Side.SELL, qty="10", instrument=msft), oversell_ctx),
        "미보유 종목 매도는 축소가 아니라 거부",
    )


def test_12_concentration_hard_limit_rejected():
    conc_hard_ctx = base_context(portfolio=PortfolioState(
        fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
        gross_exposure=Decimal(10000000), issuer_of={aapl: "AAPL"},
        issuer_exposure={"AAPL": Decimal(2900000)},
        peak_equity=Decimal(1000000000), equity=Decimal(1000000000),
    ))
    r = rejected(lambda: engine.check_order(make_intent(qty="100"), conc_hard_ctx), "집중도 Hard Limit 초과")
    assert RejectReason.CONCENTRATION_LIMIT_HARD in r.reason_codes


def test_13_concentration_soft_limit_resize():
    conc_soft_ctx = base_context(portfolio=PortfolioState(
        fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
        gross_exposure=Decimal(10000000), issuer_of={aapl: "AAPL"},
        issuer_exposure={"AAPL": Decimal(1900000)},
        peak_equity=Decimal(1000000000), equity=Decimal(1000000000),
    ))
    r = resized(lambda: engine.check_order(make_intent(qty="10"), conc_soft_ctx), "집중도 Soft Limit 초과")
    assert RejectReason.CONCENTRATION_LIMIT_SOFT in r.reason_codes


def test_14_daily_turnover_limit_resize():
    turnover_ctx = base_context(portfolio=PortfolioState(
        fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
        gross_exposure=Decimal(100000000), notional_traded_today=Decimal(96500000),
        peak_equity=Decimal(1000000000), equity=Decimal(1000000000),
    ), limits=LimitSet(
        soft_single_issuer_pct=Decimal("0.20"), hard_single_issuer_pct=Decimal("0.30"),
        max_daily_turnover_notional=Decimal(100000000), max_daily_order_count=50,
        max_daily_loss=Decimal(10000000), max_drawdown_pct=Decimal("0.20"),
    ))
    r = resized(lambda: engine.check_order(make_intent(), turnover_ctx), "회전율 한도 초과")
    assert RejectReason.TURNOVER_LIMIT in r.reason_codes


def test_15_daily_order_count_limit_rejected():
    order_count_ctx = base_context(portfolio=PortfolioState(
        fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
        gross_exposure=Decimal(100000000), orders_today=50,
        peak_equity=Decimal(1000000000), equity=Decimal(1000000000),
    ))
    r = rejected(lambda: engine.check_order(make_intent(), order_count_ctx), "일일 주문 건수 한도")
    assert RejectReason.ORDER_COUNT_LIMIT in r.reason_codes


def test_16_trading_state_halted_blocks_even_liquidation():
    halted_ctx = base_context(
        trading_state=TradingState.HALTED,
        portfolio=PortfolioState(
            fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
            gross_exposure=Decimal(100000000), positions={aapl: Decimal(100)},
            peak_equity=Decimal(1000000000), equity=Decimal(1000000000),
        ),
    )
    rejected(lambda: engine.check_order(make_intent(), halted_ctx), "HALTED - 신규 매수")
    rejected(lambda: engine.check_order(make_intent(side=Side.SELL, qty="50"), halted_ctx), "HALTED - 청산도 막힘")


def test_17_trading_state_entry_blocked_allows_liquidation():
    entry_blocked_ctx = base_context(
        trading_state=TradingState.ENTRY_BLOCKED,
        portfolio=PortfolioState(
            fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
            gross_exposure=Decimal(100000000), positions={aapl: Decimal(100)},
            peak_equity=Decimal(1000000000), equity=Decimal(1000000000),
        ),
    )
    rejected(lambda: engine.check_order(make_intent(), entry_blocked_ctx), "ENTRY_BLOCKED - 신규 매수 차단")
    approved(
        lambda: engine.check_order(make_intent(side=Side.SELL, qty="50"), entry_blocked_ctx),
        "ENTRY_BLOCKED이어도 청산은 통과해야 함",
    )


def test_18_daily_loss_limit_blocks_entry_allows_liquidation():
    loss_limit_ctx = base_context(portfolio=PortfolioState(
        fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
        gross_exposure=Decimal(100000000), positions={aapl: Decimal(100)},
        realized_pnl_today=Decimal(-11000000),
        peak_equity=Decimal(1000000000), equity=Decimal(1000000000),
    ))
    r = rejected(lambda: engine.check_order(make_intent(), loss_limit_ctx), "일일 손실 한도 초과")
    assert RejectReason.LOSS_LIMIT_BREACHED in r.reason_codes
    approved(
        lambda: engine.check_order(make_intent(side=Side.SELL, qty="50"), loss_limit_ctx),
        "손실 한도 초과해도 청산은 통과해야 함",
    )


def test_19_drawdown_limit_rejected():
    drawdown_ctx = base_context(portfolio=PortfolioState(
        fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
        gross_exposure=Decimal(100000000),
        peak_equity=Decimal(1000000000), equity=Decimal(750000000),
    ))
    r = rejected(lambda: engine.check_order(make_intent(), drawdown_ctx), "Drawdown 한도 초과")
    assert RejectReason.DRAWDOWN_LIMIT_BREACHED in r.reason_codes


def test_20_broker_down_rejected_degraded_passes():
    down_ctx = base_context(counterparty=CounterpartyStatus("paper", CounterpartyHealth.DOWN))
    r = rejected(lambda: engine.check_order(make_intent(), down_ctx), "브로커 DOWN")
    assert RejectReason.COUNTERPARTY_UNHEALTHY in r.reason_codes
    degraded_ctx = base_context(counterparty=CounterpartyStatus("paper", CounterpartyHealth.DEGRADED))
    approved(lambda: engine.check_order(make_intent(), degraded_ctx), "DEGRADED는 차단하지 않고 통과")


def test_21_hard_condition_short_circuits_before_soft_resize():
    combo_ctx = base_context(
        market_status=MarketStatus(tradable=False, reason="거래정지"),
        mandate=MandateScope(
            fund_id=fund, allowed_instrument_ids=None,
            min_order_notional=Decimal(100000), max_order_notional=Decimal(1000),
        ),
    )
    r = rejected(lambda: engine.check_order(make_intent(), combo_ctx), "Hard 조건 우선 차단")
    assert r.reason_codes == (RejectReason.MARKET_NOT_TRADABLE,), "market_tradable에서 바로 멈춰야 함"


def test_22_risk_decision_passes_through_real_oms_gate():
    intent = make_intent(key="idem_r_final")
    assessment = engine.check_order(intent, base_context())
    oms = OMS()
    rec = oms.register_intent(intent)
    oms.request_risk_review(rec)
    oms.apply_risk_decision(rec, assessment.decision)
    order = oms.create_broker_order(rec, intent)
    oms.submit(order, rec)
    assert order.state is BrokerOrderState.SUBMITTED, "RiskEngine 판정이 OMS Risk Gate를 통과해야 한다"
