#!/usr/bin/env python3
"""Sprint K1: P0 Pre-trade Risk Gate.

소유: 동규 (리스크본부)
근거: docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md 4.1(P0 Pre-trade Rule), 5.2(Risk Request/Decision),
      5.3(Trading State), 8.1(svc_risk_engine), 12(Sprint K1)
      docs/HEDGE_FUND_MASTER_PLAN.md 5.3(Risk/OMS 분리), 5.9(결정론적 서비스)
      supabase/migrations/20260729000400_execution_risk_accounting.sql의 risk.risk_decisions,
      risk.trading_states 컬럼과 이름을 맞췄다 (state는 'ENABLED'이지 'NORMAL'이 아니다).

여기에 LLM은 없다 (팀 가이드 2절 "Pre-trade Hot Path는 LLM 호출 없이 끝나야 한다").
risk-supervisor 등 Hermes 페르소나는 이 엔진의 판정을 해석·설명·Escalation만 하고,
판정 자체를 만들거나 바꾸지 않는다 (CLAUDE.md "절대 깨면 안 되는 권한 분리").

팀 가이드 4.1의 10단계 순서 검사를 전부 통과해야 APPROVE다. 순서를 바꾸지 않는다 -
먼저 걸리는 Hard 조건이 있으면 뒤 단계는 평가할 필요도 없이 즉시 REJECT다.
Soft Limit만 넘으면 그 조건이 허용하는 최대 수량으로 RESIZE하고 다음 단계로 계속 간다.
검사 도중 입력이 애매하거나 예외적이면 항상 REJECT/차단 방향으로 떨어진다 - 확대 방향으로
추정하지 않는다 (CLAUDE.md 개발 원칙 9번 "위험한 기능은 실패 시 거래 확대가 아니라
Entry 차단 방향으로 동작한다").

market-api/portfolio-api/Governance Service가 아직 없으므로 RiskContext는 그 값들의
스텁이다. Risk Engine은 이 값을 스스로 수집하지 않는다 - 주어진 그대로 판단만 한다.

자체 점검: python departments/03-risk/engine/risk_engine.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

# 트레이딩본부 contracts.py를 직접 참조한다. contracts/가 아직 공용 최상위 경계로
# 분리되지 않았기 때문이며(REPOSITORY_DEPARTMENT_STRUCTURE.md 4절), 그 전까지는
# 본부 간 의존 방향이 이 파일에 그대로 남는다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "02-trading" / "contracts"))

from contracts import (
    OrderIntent,
    RiskDecision,
    RiskVerdict,
    Side,
)

CALCULATION_VERSION = "risk-p0-v1"

# Pre-trade 판정 유효 기간. OMS.submit이 이 창을 벗어나면 재심사를 요구한다
# (팀 가이드 DoD "Decision을 입력·Policy·Calculation Version으로 재현할 수 있다").
DECISION_VALIDITY = timedelta(minutes=2)

# 스냅샷이 이보다 오래되면 품질과 무관하게 stale로 본다 (팀 가이드 10.1 Pre-trade Latency 예산).
MAX_SNAPSHOT_AGE = timedelta(seconds=5)


class TradingState(StrEnum):
    """risk.trading_states.state와 값이 같아야 한다 - 이름만 바꿔 쓰지 않는다."""

    ENABLED = "ENABLED"
    REDUCE_ONLY = "REDUCE_ONLY"
    ENTRY_BLOCKED = "ENTRY_BLOCKED"
    HALTED = "HALTED"


class RestrictionType(StrEnum):
    NO_NEW_POSITION = "no_new_position"
    NO_TRADING = "no_trading"


class CounterpartyHealth(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class RejectReason(StrEnum):
    """risk.risk_decisions.reason_codes에 그대로 들어갈 구조화된 코드.
    "작게 거래" 같은 자연어 사유는 허용하지 않는다 (팀 가이드 4.1)."""

    STALE_SNAPSHOT = "stale_snapshot"
    MARKET_NOT_TRADABLE = "market_not_tradable"
    OUTSIDE_MANDATE = "outside_mandate"
    RESTRICTED_INSTRUMENT = "restricted_instrument"
    NOTIONAL_BELOW_MINIMUM = "notional_below_minimum"
    NOTIONAL_ABOVE_MAXIMUM = "notional_above_maximum"
    INSUFFICIENT_BUYING_POWER = "insufficient_buying_power"
    OVERSELL = "oversell"
    CONCENTRATION_LIMIT_SOFT = "concentration_limit_soft"
    CONCENTRATION_LIMIT_HARD = "concentration_limit_hard"
    TURNOVER_LIMIT = "turnover_limit"
    ORDER_COUNT_LIMIT = "order_count_limit"
    TRADING_STATE_BLOCKED = "trading_state_blocked"
    LOSS_LIMIT_BREACHED = "loss_limit_breached"
    DRAWDOWN_LIMIT_BREACHED = "drawdown_limit_breached"
    COUNTERPARTY_UNHEALTHY = "counterparty_unhealthy"
    RESIZED_TO_ZERO = "resized_to_zero"


class RiskEngineError(Exception):
    """입력 자체가 검사를 수행할 수 없는 상태. 조용히 통과시키지 않는다."""


# ---------------------------------------------------------------------------
# 입력 (market-api / portfolio-api / Governance Service의 스텁)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MandateScope:
    """risk.policies 요약. fund_id 하나의 유효 Mandate 한 장."""

    fund_id: UUID
    allowed_instrument_ids: frozenset[UUID] | None  # None이면 유니버스 전체 허용
    min_order_notional: Decimal
    max_order_notional: Decimal


@dataclass(frozen=True)
class RestrictedItem:
    instrument_id: UUID
    restriction_type: RestrictionType
    effective_from: datetime
    effective_to: datetime | None = None

    def applies_at(self, when: datetime) -> bool:
        if when < self.effective_from:
            return False
        return not (self.effective_to is not None and when >= self.effective_to)


@dataclass(frozen=True)
class LimitSet:
    """risk.limits 요약. soft는 RESIZE, hard는 REJECT (팀 가이드 4.1/5.1)."""

    soft_single_issuer_pct: Decimal
    hard_single_issuer_pct: Decimal
    max_daily_turnover_notional: Decimal
    max_daily_order_count: int
    max_daily_loss: Decimal
    max_drawdown_pct: Decimal


@dataclass
class PortfolioState:
    """portfolio-api가 줄 값의 스텁. Risk가 직접 집계하지 않는다 (팀 가이드 3.1)."""

    fund_id: UUID
    cash: Decimal
    buying_power: Decimal
    gross_exposure: Decimal
    positions: dict[UUID, Decimal] = field(default_factory=dict)          # instrument_id -> qty
    issuer_of: dict[UUID, str] = field(default_factory=dict)              # instrument_id -> issuer
    issuer_exposure: dict[str, Decimal] = field(default_factory=dict)     # issuer -> notional
    realized_pnl_today: Decimal = Decimal(0)
    unrealized_pnl_today: Decimal = Decimal(0)
    peak_equity: Decimal = Decimal(0)
    equity: Decimal = Decimal(0)
    orders_today: int = 0
    notional_traded_today: Decimal = Decimal(0)

    @property
    def daily_pnl(self) -> Decimal:
        return self.realized_pnl_today + self.unrealized_pnl_today

    @property
    def drawdown_pct(self) -> Decimal:
        if self.peak_equity <= 0:
            return Decimal(0)
        return max(Decimal(0), (self.peak_equity - self.equity) / self.peak_equity)

    def issuer_of_instrument(self, instrument_id: UUID) -> str:
        return self.issuer_of.get(instrument_id, str(instrument_id))


@dataclass(frozen=True)
class MarketStatus:
    tradable: bool
    reason: str = ""


@dataclass(frozen=True)
class CounterpartyStatus:
    broker_adapter: str
    health: CounterpartyHealth


@dataclass
class RiskContext:
    """Pre-trade 검사 1건에 필요한 입력 전부."""

    mandate: MandateScope
    limits: LimitSet
    restricted_items: tuple[RestrictedItem, ...]
    portfolio: PortfolioState
    market_status: MarketStatus
    counterparty: CounterpartyStatus
    trading_state: TradingState
    as_of: datetime


# ---------------------------------------------------------------------------
# 출력 (risk.risk_decisions 컬럼과 이름을 맞춘 감사용 상세 기록)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckOutcome:
    check_name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class RiskAssessment:
    """RiskDecision(OMS가 그대로 소비하는 단순 계약)을 감싸는 감사 기록.
    risk.risk_decisions의 check_results/reason_codes/calculation_version/input_hash/
    approved_legs/aggregate_exposure에 대응한다. Supabase 연동 시 이 필드를 그대로 옮긴다."""

    risk_request_id: UUID
    decision: RiskDecision
    check_results: tuple[CheckOutcome, ...]
    reason_codes: tuple[RejectReason, ...]
    calculation_version: str
    input_hash: str
    trading_state: TradingState
    approved_legs: tuple[dict, ...]
    aggregate_exposure: dict


def _decimal_str(value) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def _context_hash(intent: OrderIntent, ctx: RiskContext) -> str:
    """같은 입력·같은 Policy Version이면 항상 같은 해시 - 판정 재현성의 근거 (팀 가이드 DoD)."""
    payload = {
        "order_intent_evidence_hash": intent.evidence_hash(),
        "calculation_version": CALCULATION_VERSION,
        "mandate_fund_id": _decimal_str(ctx.mandate.fund_id),
        "mandate_min_notional": _decimal_str(ctx.mandate.min_order_notional),
        "mandate_max_notional": _decimal_str(ctx.mandate.max_order_notional),
        "restricted_items": sorted(
            f"{r.instrument_id}:{r.restriction_type.value}" for r in ctx.restricted_items
        ),
        "portfolio_cash": _decimal_str(ctx.portfolio.cash),
        "portfolio_buying_power": _decimal_str(ctx.portfolio.buying_power),
        "portfolio_position_qty": _decimal_str(
            ctx.portfolio.positions.get(intent.instrument_id, Decimal(0))
        ),
        "market_tradable": ctx.market_status.tradable,
        "counterparty_health": ctx.counterparty.health.value,
        "trading_state": ctx.trading_state.value,
        "as_of": _decimal_str(ctx.as_of),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class RiskEngine:
    """결정론적 Pre-trade Risk Gate. 여기 없는 판정 경로는 없다."""

    def __init__(self, service_identity: str = "svc_risk_engine") -> None:
        self.service_identity = service_identity

    def check_order(
        self,
        intent: OrderIntent,
        ctx: RiskContext,
        risk_request_id: UUID | None = None,
    ) -> RiskAssessment:
        risk_request_id = risk_request_id or uuid4()
        checks: list[CheckOutcome] = []
        reasons: list[RejectReason] = []
        allowed_qty = intent.quantity
        current_qty = ctx.portfolio.positions.get(intent.instrument_id, Decimal(0))
        reducing = intent.side is Side.SELL and current_qty > 0

        def reject(check_name: str, reason: RejectReason, detail: str) -> RiskAssessment:
            checks.append(CheckOutcome(check_name, False, detail))
            reasons.append(reason)
            decision = RiskDecision(
                order_intent_id=intent.order_intent_id,
                verdict=RiskVerdict.REJECT,
                approved_quantity=None,
                max_price=None,
                expires_at=ctx.as_of + DECISION_VALIDITY,
                reason=detail,
                decided_by=self.service_identity,
                decided_at=ctx.as_of,
            )
            return RiskAssessment(
                risk_request_id=risk_request_id,
                decision=decision,
                check_results=tuple(checks),
                reason_codes=tuple(reasons),
                calculation_version=CALCULATION_VERSION,
                input_hash=_context_hash(intent, ctx),
                trading_state=ctx.trading_state,
                approved_legs=(),
                aggregate_exposure={},
            )

        # 1. Data Freshness와 Quality.
        # 품질(quality) 자체는 OrderIntent 생성 시점에 이미 걸러진다(contracts.py의
        # _bid_not_above_ask/_check). 여기서는 스냅샷 나이만 별도로 본다.
        snapshot_age = ctx.as_of - intent.snapshot.as_of
        if snapshot_age > MAX_SNAPSHOT_AGE:
            return reject(
                "data_freshness", RejectReason.STALE_SNAPSHOT,
                f"스냅샷 나이 {snapshot_age}가 허용치 {MAX_SNAPSHOT_AGE}를 초과했습니다",
            )
        checks.append(CheckOutcome("data_freshness", True))

        # 2. Market/Instrument 거래 가능 상태.
        if not ctx.market_status.tradable:
            return reject(
                "market_tradable", RejectReason.MARKET_NOT_TRADABLE,
                ctx.market_status.reason or "시장/종목이 거래 불가 상태입니다",
            )
        checks.append(CheckOutcome("market_tradable", True))

        # 3. Strategy/Book/Fund Mandate.
        if (
            ctx.mandate.allowed_instrument_ids is not None
            and intent.instrument_id not in ctx.mandate.allowed_instrument_ids
        ):
            return reject(
                "mandate", RejectReason.OUTSIDE_MANDATE,
                "종목이 현재 Mandate의 허용 유니버스 밖입니다",
            )
        checks.append(CheckOutcome("mandate", True))

        # 4. Restricted List와 Compliance Policy.
        for item in ctx.restricted_items:
            if item.instrument_id != intent.instrument_id or not item.applies_at(ctx.as_of):
                continue
            if item.restriction_type is RestrictionType.NO_TRADING:
                return reject(
                    "restricted_list", RejectReason.RESTRICTED_INSTRUMENT,
                    "Restricted List: 거래 전면 금지 종목입니다",
                )
            if item.restriction_type is RestrictionType.NO_NEW_POSITION and not reducing:
                return reject(
                    "restricted_list", RejectReason.RESTRICTED_INSTRUMENT,
                    "Restricted List: 신규 포지션 금지 종목입니다 (청산만 허용)",
                )
        checks.append(CheckOutcome("restricted_list", True))

        # 5. 주문 가격, 수량, Notional.
        ref_price = intent.limit_price or intent.snapshot.mid
        if ref_price is None:
            return reject(
                "notional_bounds", RejectReason.NOTIONAL_BELOW_MINIMUM,
                "참조 가격이 없어 Notional을 계산할 수 없습니다",
            )
        notional = allowed_qty * ref_price
        if notional < ctx.mandate.min_order_notional:
            return reject(
                "notional_bounds", RejectReason.NOTIONAL_BELOW_MINIMUM,
                f"주문 Notional {notional}이 최소 {ctx.mandate.min_order_notional} 미만입니다",
            )
        if notional > ctx.mandate.max_order_notional:
            capped_qty = (ctx.mandate.max_order_notional / ref_price).quantize(
                Decimal(1), rounding=ROUND_DOWN
            )
            allowed_qty = min(allowed_qty, capped_qty)
            reasons.append(RejectReason.NOTIONAL_ABOVE_MAXIMUM)
            checks.append(CheckOutcome(
                "notional_bounds", True,
                f"최대 Notional 초과로 {allowed_qty}주로 축소",
            ))
        else:
            checks.append(CheckOutcome("notional_bounds", True))
        if allowed_qty <= 0:
            return reject(
                "notional_bounds", RejectReason.RESIZED_TO_ZERO,
                "Notional 상한을 적용하면 남는 수량이 없습니다",
            )

        # 6. Cash/Buying Power와 Pending Order 포함 Position (매도 초과분 포함).
        if intent.side is Side.BUY:
            affordable_qty = (ctx.portfolio.buying_power / ref_price).quantize(
                Decimal(1), rounding=ROUND_DOWN
            )
            if affordable_qty <= 0:
                return reject(
                    "buying_power", RejectReason.INSUFFICIENT_BUYING_POWER,
                    "매수 여력이 0 이하입니다",
                )
            if affordable_qty < allowed_qty:
                allowed_qty = affordable_qty
                reasons.append(RejectReason.INSUFFICIENT_BUYING_POWER)
                checks.append(CheckOutcome(
                    "buying_power", True, f"매수 여력 한도로 {allowed_qty}주로 축소",
                ))
            else:
                checks.append(CheckOutcome("buying_power", True))
        else:
            if current_qty <= 0:
                return reject(
                    "buying_power", RejectReason.OVERSELL,
                    "보유하지 않은 종목의 매도입니다 (공매도는 아직 인증 전)",
                )
            if allowed_qty > current_qty:
                allowed_qty = current_qty
                reasons.append(RejectReason.OVERSELL)
                checks.append(CheckOutcome(
                    "buying_power", True, f"보유 수량 한도로 {allowed_qty}주로 축소",
                ))
            else:
                checks.append(CheckOutcome("buying_power", True))
        if allowed_qty <= 0:
            return reject(
                "buying_power", RejectReason.RESIZED_TO_ZERO,
                "매수 여력/보유 수량 한도를 적용하면 남는 수량이 없습니다",
            )

        # 7. 단일 종목·Issuer Concentration (Long만 가정 - 공매도 미인증).
        issuer = ctx.portfolio.issuer_of_instrument(intent.instrument_id)
        signed_notional = allowed_qty * ref_price * (1 if intent.side is Side.BUY else -1)
        projected_issuer_exposure = ctx.portfolio.issuer_exposure.get(issuer, Decimal(0)) + signed_notional
        projected_gross = ctx.portfolio.gross_exposure + abs(signed_notional) * (
            1 if intent.side is Side.BUY else -1
        )
        if projected_gross > 0:
            issuer_pct = abs(projected_issuer_exposure) / projected_gross
        else:
            issuer_pct = Decimal(0)
        if intent.side is Side.BUY and issuer_pct > ctx.limits.hard_single_issuer_pct:
            return reject(
                "concentration", RejectReason.CONCENTRATION_LIMIT_HARD,
                f"{issuer} 집중도 {issuer_pct:.2%}가 Hard Limit "
                f"{ctx.limits.hard_single_issuer_pct:.2%}를 초과합니다",
            )
        if intent.side is Side.BUY and issuer_pct > ctx.limits.soft_single_issuer_pct:
            current_issuer_exposure = ctx.portfolio.issuer_exposure.get(issuer, Decimal(0))
            max_allowed_notional = max(
                Decimal(0),
                ctx.limits.soft_single_issuer_pct * ctx.portfolio.gross_exposure
                - current_issuer_exposure,
            )
            capped_qty = (max_allowed_notional / ref_price).quantize(Decimal(1), rounding=ROUND_DOWN)
            allowed_qty = min(allowed_qty, capped_qty)
            reasons.append(RejectReason.CONCENTRATION_LIMIT_SOFT)
            checks.append(CheckOutcome(
                "concentration", True, f"{issuer} Soft Limit로 {allowed_qty}주로 축소",
            ))
        else:
            checks.append(CheckOutcome("concentration", True))
        if allowed_qty <= 0:
            return reject(
                "concentration", RejectReason.RESIZED_TO_ZERO,
                "집중도 한도를 적용하면 남는 수량이 없습니다",
            )

        # 8. 일일 Turnover와 주문 빈도.
        if ctx.portfolio.orders_today + 1 > ctx.limits.max_daily_order_count:
            return reject(
                "turnover", RejectReason.ORDER_COUNT_LIMIT,
                f"오늘 주문 {ctx.portfolio.orders_today}건으로 이미 한도 "
                f"{ctx.limits.max_daily_order_count}건에 도달했습니다",
            )
        remaining_turnover = ctx.limits.max_daily_turnover_notional - ctx.portfolio.notional_traded_today
        if remaining_turnover <= 0:
            return reject(
                "turnover", RejectReason.TURNOVER_LIMIT,
                "오늘 회전율 한도를 이미 소진했습니다",
            )
        order_notional = allowed_qty * ref_price
        if order_notional > remaining_turnover:
            capped_qty = (remaining_turnover / ref_price).quantize(Decimal(1), rounding=ROUND_DOWN)
            allowed_qty = min(allowed_qty, capped_qty)
            reasons.append(RejectReason.TURNOVER_LIMIT)
            checks.append(CheckOutcome(
                "turnover", True, f"잔여 회전율 한도로 {allowed_qty}주로 축소",
            ))
        else:
            checks.append(CheckOutcome("turnover", True))
        if allowed_qty <= 0:
            return reject(
                "turnover", RejectReason.RESIZED_TO_ZERO,
                "회전율 한도를 적용하면 남는 수량이 없습니다",
            )

        # 9. Drawdown, Loss Limit와 Trading State.
        # HALTED는 청산 주문도 막는다. ENTRY_BLOCKED/REDUCE_ONLY는 포지션을 줄이는
        # 주문(reducing)만 예외로 통과시킨다 - 8.1절 "신규 주문 차단"과 같은 원칙이다.
        if ctx.trading_state is TradingState.HALTED:
            return reject(
                "trading_state", RejectReason.TRADING_STATE_BLOCKED,
                "Trading State가 HALTED입니다 - 모든 주문이 차단됩니다",
            )
        if ctx.trading_state in (TradingState.ENTRY_BLOCKED, TradingState.REDUCE_ONLY) and not reducing:
            return reject(
                "trading_state", RejectReason.TRADING_STATE_BLOCKED,
                f"Trading State가 {ctx.trading_state.value}입니다 - 신규/증액 주문이 차단됩니다",
            )
        if not reducing and ctx.portfolio.daily_pnl <= -ctx.limits.max_daily_loss:
            return reject(
                "trading_state", RejectReason.LOSS_LIMIT_BREACHED,
                f"일일 손실 {ctx.portfolio.daily_pnl}이 한도 -{ctx.limits.max_daily_loss}를 초과했습니다",
            )
        if not reducing and ctx.portfolio.drawdown_pct >= ctx.limits.max_drawdown_pct:
            return reject(
                "trading_state", RejectReason.DRAWDOWN_LIMIT_BREACHED,
                f"Drawdown {ctx.portfolio.drawdown_pct:.2%}가 한도 "
                f"{ctx.limits.max_drawdown_pct:.2%}에 도달했습니다",
            )
        checks.append(CheckOutcome("trading_state", True))

        # 10. Broker Session과 Counterparty Health.
        if ctx.counterparty.health is CounterpartyHealth.DOWN:
            return reject(
                "counterparty_health", RejectReason.COUNTERPARTY_UNHEALTHY,
                f"{ctx.counterparty.broker_adapter} 브로커 상태가 DOWN입니다",
            )
        checks.append(CheckOutcome(
            "counterparty_health", True,
            "" if ctx.counterparty.health is CounterpartyHealth.OK else "DEGRADED 상태 - 통과하되 기록",
        ))

        # 전부 통과 - 축소가 있었으면 RESIZE, 없었으면 APPROVE.
        verdict = RiskVerdict.APPROVE if allowed_qty == intent.quantity else RiskVerdict.RESIZE
        decision = RiskDecision(
            order_intent_id=intent.order_intent_id,
            verdict=verdict,
            approved_quantity=allowed_qty,
            max_price=ref_price,
            expires_at=ctx.as_of + DECISION_VALIDITY,
            reason="; ".join(r.value for r in reasons) if reasons else "",
            decided_by=self.service_identity,
            decided_at=ctx.as_of,
        )
        return RiskAssessment(
            risk_request_id=risk_request_id,
            decision=decision,
            check_results=tuple(checks),
            reason_codes=tuple(reasons),
            calculation_version=CALCULATION_VERSION,
            input_hash=_context_hash(intent, ctx),
            trading_state=ctx.trading_state,
            approved_legs=({
                "order_intent_id": str(intent.order_intent_id),
                "approved_quantity": _decimal_str(allowed_qty),
                "max_price": _decimal_str(ref_price),
            },),
            aggregate_exposure={
                "issuer": issuer,
                "projected_issuer_exposure": _decimal_str(projected_issuer_exposure),
                "projected_gross_exposure": _decimal_str(projected_gross),
            },
        )


if __name__ == "__main__":
    from contracts import MarketSnapshot, OrderType, TimeInForce

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "02-trading" / "oms"))
    from oms import OMS

    now = datetime.now(timezone.utc)
    fund, book, strategy = uuid4(), uuid4(), uuid4()
    aapl, msft = uuid4(), uuid4()

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
        defaults = {
            "mandate": MandateScope(
                fund_id=fund, allowed_instrument_ids=None,
                min_order_notional=Decimal(100000), max_order_notional=Decimal(50000000),
            ),
            "limits": LimitSet(
                soft_single_issuer_pct=Decimal("0.20"), hard_single_issuer_pct=Decimal("0.30"),
                max_daily_turnover_notional=Decimal(100000000), max_daily_order_count=50,
                max_daily_loss=Decimal(10000000), max_drawdown_pct=Decimal("0.20"),
            ),
            "restricted_items": (),
            "portfolio": PortfolioState(
                fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
                gross_exposure=Decimal(100000000), peak_equity=Decimal(1000000000),
                equity=Decimal(1000000000),
            ),
            "market_status": MarketStatus(tradable=True),
            "counterparty": CounterpartyStatus(broker_adapter="paper", health=CounterpartyHealth.OK),
            "trading_state": TradingState.ENABLED,
            "as_of": now,
        }
        defaults.update(overrides)
        return RiskContext(**defaults)

    engine = RiskEngine()

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

    # 1. 정상 주문 -> APPROVE, 전체 10개 검사 전부 기록됨
    r = approved(lambda: engine.check_order(make_intent(), base_context()), "평범한 매수")
    assert r.decision.approved_quantity == Decimal(100)
    assert len(r.check_results) == 10, "10단계 검사가 다 기록돼야 함"
    assert all(c.passed for c in r.check_results)
    assert r.approved_legs and r.approved_legs[0]["order_intent_id"] == str(r.decision.order_intent_id)

    # 2. 재현성 - 같은 Intent·같은 Context를 두 번 평가하면 같은 input_hash, 같은 판정
    #    (make_intent()를 두 번 부르면 trade_case_id가 매번 새로 생겨 다른 Intent가 되므로
    #    같은 Intent 객체를 재사용해서 검사한다).
    same_intent = make_intent()
    r_repeat = engine.check_order(same_intent, base_context())
    assert r_repeat.input_hash == engine.check_order(same_intent, base_context()).input_hash, \
        "같은 Intent·Context인데 해시가 다름"
    r3 = engine.check_order(make_intent(qty="101"), base_context())
    assert r.input_hash != r3.input_hash, "다른 입력인데 해시가 같음"

    # 3. 최대 Notional 초과 -> RESIZE (100000000 한도, 70000원 * 100주=7,000,000이라
    #    최대치를 낮게 잡아 강제로 걸리게 한다)
    tight_mandate_ctx = base_context(mandate=MandateScope(
        fund_id=fund, allowed_instrument_ids=None,
        min_order_notional=Decimal(100000), max_order_notional=Decimal(3500000),
    ))
    r = resized(lambda: engine.check_order(make_intent(), tight_mandate_ctx), "최대 Notional 초과")
    assert r.decision.approved_quantity == Decimal(50), "3,500,000 / 70,000 = 50주여야 함"
    assert RejectReason.NOTIONAL_ABOVE_MAXIMUM in r.reason_codes

    # 4. Notional 최소 미달 -> REJECT
    below_min_ctx = base_context(mandate=MandateScope(
        fund_id=fund, allowed_instrument_ids=None,
        min_order_notional=Decimal(50000000), max_order_notional=Decimal(100000000),
    ))
    r = rejected(lambda: engine.check_order(make_intent(), below_min_ctx), "최소 Notional 미달")
    assert RejectReason.NOTIONAL_BELOW_MINIMUM in r.reason_codes

    # 5. Restricted List(NO_TRADING) -> REJECT
    restricted_ctx = base_context(restricted_items=(
        RestrictedItem(aapl, RestrictionType.NO_TRADING, now - timedelta(days=1)),
    ))
    r = rejected(lambda: engine.check_order(make_intent(), restricted_ctx), "거래 전면 금지 종목")
    assert RejectReason.RESTRICTED_INSTRUMENT in r.reason_codes

    # 6. Restricted List(NO_NEW_POSITION) - 청산(SELL, 보유 있음)은 예외로 통과
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

    # 7. Mandate 유니버스 밖 종목 -> REJECT
    narrow_mandate_ctx = base_context(mandate=MandateScope(
        fund_id=fund, allowed_instrument_ids=frozenset({msft}),
        min_order_notional=Decimal(100000), max_order_notional=Decimal(100000000),
    ))
    r = rejected(lambda: engine.check_order(make_intent(instrument=aapl), narrow_mandate_ctx), "유니버스 밖 종목")
    assert RejectReason.OUTSIDE_MANDATE in r.reason_codes

    # 8. 스냅샷 오래됨 -> REJECT (팀 가이드 4.1 #1, 11.2절 데이터 단절 시 신규 진입 금지)
    stale_ctx = base_context(as_of=now + timedelta(seconds=30))
    r = rejected(lambda: engine.check_order(make_intent(snapshot=snap(as_of=now)), stale_ctx), "오래된 스냅샷")
    assert RejectReason.STALE_SNAPSHOT in r.reason_codes

    # 9. 시장 거래 불가 -> REJECT
    halted_market_ctx = base_context(market_status=MarketStatus(tradable=False, reason="거래정지"))
    r = rejected(lambda: engine.check_order(make_intent(), halted_market_ctx), "거래정지 종목")
    assert RejectReason.MARKET_NOT_TRADABLE in r.reason_codes

    # 10. 매수 여력 부족 -> RESIZE
    low_buying_power_ctx = base_context(portfolio=PortfolioState(
        fund_id=fund, cash=Decimal(3500000), buying_power=Decimal(3500000),
        gross_exposure=Decimal(100000000), peak_equity=Decimal(1000000000),
        equity=Decimal(1000000000),
    ))
    r = resized(lambda: engine.check_order(make_intent(), low_buying_power_ctx), "매수 여력 부족")
    assert r.decision.approved_quantity == Decimal(50)
    assert RejectReason.INSUFFICIENT_BUYING_POWER in r.reason_codes

    # 11. 초과 매도(공매도) -> RESIZE to 보유 수량 (아직 공매도 미인증)
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

    # 12. 집중도 Hard Limit 초과 -> REJECT
    conc_hard_ctx = base_context(portfolio=PortfolioState(
        fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
        gross_exposure=Decimal(10000000), issuer_of={aapl: "AAPL"},
        issuer_exposure={"AAPL": Decimal(2900000)},
        peak_equity=Decimal(1000000000), equity=Decimal(1000000000),
    ))
    r = rejected(lambda: engine.check_order(make_intent(qty="100"), conc_hard_ctx), "집중도 Hard Limit 초과")
    assert RejectReason.CONCENTRATION_LIMIT_HARD in r.reason_codes

    # 13. 집중도 Soft Limit만 초과 -> RESIZE
    conc_soft_ctx = base_context(portfolio=PortfolioState(
        fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
        gross_exposure=Decimal(10000000), issuer_of={aapl: "AAPL"},
        issuer_exposure={"AAPL": Decimal(1900000)},
        peak_equity=Decimal(1000000000), equity=Decimal(1000000000),
    ))
    r = resized(lambda: engine.check_order(make_intent(qty="10"), conc_soft_ctx), "집중도 Soft Limit 초과")
    assert RejectReason.CONCENTRATION_LIMIT_SOFT in r.reason_codes

    # 14. 일일 회전율 한도 초과 -> RESIZE
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

    # 15. 일일 주문 건수 한도 초과 -> REJECT
    order_count_ctx = base_context(portfolio=PortfolioState(
        fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
        gross_exposure=Decimal(100000000), orders_today=50,
        peak_equity=Decimal(1000000000), equity=Decimal(1000000000),
    ))
    r = rejected(lambda: engine.check_order(make_intent(), order_count_ctx), "일일 주문 건수 한도")
    assert RejectReason.ORDER_COUNT_LIMIT in r.reason_codes

    # 16. Trading State HALTED -> 청산 주문도 REJECT
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

    # 17. Trading State ENTRY_BLOCKED -> 신규는 막히고 청산은 통과
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

    # 18. 일일 손실 한도 초과 -> REJECT (신규 진입), 청산은 통과
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

    # 19. Drawdown 한도 초과 -> REJECT
    drawdown_ctx = base_context(portfolio=PortfolioState(
        fund_id=fund, cash=Decimal(100000000), buying_power=Decimal(100000000),
        gross_exposure=Decimal(100000000),
        peak_equity=Decimal(1000000000), equity=Decimal(750000000),
    ))
    r = rejected(lambda: engine.check_order(make_intent(), drawdown_ctx), "Drawdown 한도 초과")
    assert RejectReason.DRAWDOWN_LIMIT_BREACHED in r.reason_codes

    # 20. 브로커 DOWN -> REJECT / DEGRADED -> 통과하되 기록
    down_ctx = base_context(counterparty=CounterpartyStatus("paper", CounterpartyHealth.DOWN))
    r = rejected(lambda: engine.check_order(make_intent(), down_ctx), "브로커 DOWN")
    assert RejectReason.COUNTERPARTY_UNHEALTHY in r.reason_codes
    degraded_ctx = base_context(counterparty=CounterpartyStatus("paper", CounterpartyHealth.DEGRADED))
    approved(lambda: engine.check_order(make_intent(), degraded_ctx), "DEGRADED는 차단하지 않고 통과")

    # 21. Hard 조건이 먼저 걸리면 뒤 단계(Soft RESIZE 후보)는 평가하지 않고 즉시 REJECT.
    #     (거래정지 + 동시에 Notional도 초과하는 상황 - 시장 상태가 먼저 걸려야 한다)
    combo_ctx = base_context(
        market_status=MarketStatus(tradable=False, reason="거래정지"),
        mandate=MandateScope(
            fund_id=fund, allowed_instrument_ids=None,
            min_order_notional=Decimal(100000), max_order_notional=Decimal(1000),
        ),
    )
    r = rejected(lambda: engine.check_order(make_intent(), combo_ctx), "Hard 조건 우선 차단")
    assert r.reason_codes == (RejectReason.MARKET_NOT_TRADABLE,), "market_tradable에서 바로 멈춰야 함"

    # 22. 통합 - RiskEngine의 RiskDecision이 실제 OMS Gate를 그대로 통과하는지 (파이프라인 전체 검증)
    intent = make_intent(key="idem_r_final")
    assessment = engine.check_order(intent, base_context())
    # OMS 상태 머신이 v1.2에서 둘로 분리돼 호출 순서가 바뀌었다.
    # Intent를 심사 통과시킨 뒤 별도의 Broker Order를 만든다 - 상태 전이가 아니다.
    oms = OMS()
    rec = oms.register_intent(intent)
    oms.request_risk_review(rec)
    oms.apply_risk_decision(rec, assessment.decision)
    order = oms.create_broker_order(rec, intent)
    oms.submit(order, rec)
    from contracts import BrokerOrderState
    assert order.state is BrokerOrderState.SUBMITTED, "RiskEngine 판정이 OMS Risk Gate를 통과해야 한다"

    print("ok - P0 Pre-trade Risk Gate 22개 시나리오 점검 통과")
