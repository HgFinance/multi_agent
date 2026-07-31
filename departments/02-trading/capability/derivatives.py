#!/usr/bin/env python3
"""F31: Derivatives Capability - Contract, Margin, Roll과 Exercise/Assignment.

소유: 도현 (트레이딩본부)
근거: docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md 4절 F31
      docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 1.1
      supabase/migrations/20260729000100_foundation_reference.sql
        reference.derivative_contracts
      supabase/migrations/20260729000300_research_quant_strategy.sql
        strategy.capability_profiles

**이 모듈은 파생상품 거래를 켜지 않는다.** 팀 가이드 1.1이 정한 그대로다.

    P0는 Long/Short Pair와 Basket Fixture를 포함하고, 실제 공매도·선물·옵션은
    Broker와 Risk/Accounting Certification 후 별도 활성화한다.
    실행·회계 Capability가 없는 Strategy Version은 승인 신호가 있어도
    주문 접수 단계에서 차단한다.

그래서 여기서 만드는 것은 **계약 모델과 차단 게이트**다. Certification이
떨어지기 전에는 파생 주문이 접수 단계에서 막힌다. 초기 시장이 KOSPI/KOSDAQ
현물 Long-only인 것과 모순되지 않는다 - 모델은 있고 스위치가 꺼져 있는 상태다.

파생에서 조용히 틀리는 것 두 가지를 코드로 막는다.

1. **명목금액은 수량 x 가격이 아니라 수량 x 가격 x 승수다.** 승수를 빼먹으면
   KOSPI200 선물 1계약을 25만원짜리로 계산한다(실제로는 x250,000). 한도가
   조용히 250배 느슨해진다.
2. **만기가 지난 계약에 주문을 내면 안 된다.** 만기일 당일도 정산 규칙이 달라
   기본 차단하고, 명시적으로 허용해야 통과한다.

Greeks는 여기서 계산하지 않는다. Capability로 선언만 하고 실제 산출은
리스크본부 소관이며 Certification 항목이다 - 차단된 상품의 Greeks를 미리
계산해두는 것은 쓰이지 않을 코드다.

자체 점검: python departments/02-trading/capability/derivatives.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "contracts"))
sys.path.insert(0, str(_HERE.parent / "multileg"))

from contracts import Side  # noqa: E402
from intent_group import (  # noqa: E402
    AtomicityPolicy,
    FailurePolicy,
    IntentGroup,
    Leg,
    PositionEffect,
)

ZERO = Decimal("0")


class ContractKind(StrEnum):
    """reference.derivative_contracts.contract_kind와 같은 값이어야 한다."""

    FUTURE = "FUTURE"
    CALL = "CALL"
    PUT = "PUT"
    WARRANT = "WARRANT"


class SettlementType(StrEnum):
    CASH = "CASH"
    PHYSICAL = "PHYSICAL"


class ExerciseStyle(StrEnum):
    AMERICAN = "AMERICAN"
    EUROPEAN = "EUROPEAN"
    ASIAN = "ASIAN"
    OTHER = "OTHER"


class ProfileStatus(StrEnum):
    """strategy.capability_profiles.status와 같은 값이어야 한다."""

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"      # 이 상태만 실제 주문을 낼 수 있다
    RETIRED = "RETIRED"


# 옵션은 행사가가 있어야 한다. 선물·워런트는 없다.
STRIKE_REQUIRED = frozenset({ContractKind.CALL, ContractKind.PUT})


class CapabilityError(Exception):
    """계약이나 Capability 선언이 말이 안 되는 경우."""


@dataclass(frozen=True)
class DerivativeContract:
    """파생 계약 명세. reference.derivative_contracts에 대응한다.

    우리가 만들지 않는다 - `reference-api`가 준 것을 받아 쓴다.
    """

    instrument_id: UUID
    contract_kind: ContractKind
    expiry_date: date
    contract_multiplier: Decimal
    settlement_type: SettlementType
    underlying_instrument_id: UUID | None = None
    strike_price: Decimal | None = None
    exercise_style: ExerciseStyle | None = None
    margin_currency: str = "KRW"

    def __post_init__(self) -> None:
        if self.contract_multiplier <= 0:
            raise CapabilityError(f"계약 승수가 0 이하입니다: {self.contract_multiplier}")
        if self.contract_kind in STRIKE_REQUIRED:
            if self.strike_price is None or self.strike_price <= 0:
                raise CapabilityError(f"{self.contract_kind}에 행사가가 없습니다")
            if self.exercise_style is None:
                raise CapabilityError(f"{self.contract_kind}에 행사 방식이 없습니다")
        if len(self.margin_currency) != 3 or not self.margin_currency.isupper():
            raise CapabilityError(f"통화 코드가 아닙니다: {self.margin_currency}")

    def notional(self, quantity: Decimal, price: Decimal) -> Decimal:
        """명목금액 = 수량 x 가격 x **승수**.

        승수를 빼먹으면 한도가 조용히 느슨해진다. 파생 주문의 크기를
        재는 곳은 전부 이 함수를 거쳐야 한다.
        """
        if quantity <= 0 or price <= 0:
            raise CapabilityError(f"수량({quantity})·가격({price})이 0 이하입니다")
        return quantity * price * self.contract_multiplier

    def is_expired_at(self, as_of: date) -> bool:
        return as_of > self.expiry_date

    def days_to_expiry(self, as_of: date) -> int:
        return (self.expiry_date - as_of).days


@dataclass(frozen=True)
class CapabilityProfile:
    """전략 Version이 무엇을 할 수 있는지. strategy.capability_profiles에 대응한다.

    `execution`/`risk`/`accounting` 셋 다 갖춰야 주문이 나간다. 하나라도
    비면 승인 신호가 있어도 접수 단계에서 막힌다 (팀 가이드 1.1).
    """

    capability_profile_id: UUID
    profile_code: str
    version: int
    status: ProfileStatus = ProfileStatus.DRAFT
    # 다룰 수 있는 상품군. "EQUITY", "FUTURE", "OPTION" 등
    required_instruments: frozenset[str] = frozenset({"EQUITY"})
    execution_capabilities: frozenset[str] = frozenset()
    risk_capabilities: frozenset[str] = frozenset()
    accounting_capabilities: frozenset[str] = frozenset()
    # Certification 결과. Broker/Risk/Accounting이 각각 서명해야 한다
    certified_by: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.version <= 0:
            raise CapabilityError(f"version이 0 이하입니다: {self.version}")
        if not self.profile_code:
            raise CapabilityError("profile_code가 없습니다")

    @property
    def is_active(self) -> bool:
        return self.status is ProfileStatus.ACTIVE

    def can_trade(self, asset_class: str) -> bool:
        return asset_class in self.required_instruments


# 파생을 다루려면 세 본부 Certification이 전부 있어야 한다 (팀 가이드 1.1).
DERIVATIVE_CERTIFIERS = frozenset({"broker", "risk", "accounting"})

# 이 상품군은 Certification 없이 못 쓴다. 초기 시장은 KOSPI/KOSDAQ 현물이다.
CERTIFICATION_REQUIRED = frozenset({"FUTURE", "OPTION", "WARRANT", "SHORT"})


@dataclass(frozen=True)
class CapabilityVerdict:
    """접수 게이트 판정. allowed=False면 주문이 만들어지지 않는다."""

    allowed: bool
    reasons: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        return " / ".join(self.reasons)


def check_order_capability(
    profile: CapabilityProfile,
    asset_class: str,
    *,
    contract: DerivativeContract | None = None,
    as_of: date | None = None,
    allow_expiry_day: bool = False,
) -> CapabilityVerdict:
    """주문 접수 게이트. **여기서 막히면 Risk 승인이 있어도 주문이 안 나간다.**

    팀 가이드 1.1: "실행·회계 Capability가 없는 Strategy Version은 승인 신호가
    있어도 주문 접수 단계에서 차단한다."

    실패를 통과로 바꾸지 않는다 - 애매하면 막는 쪽이다(개발 원칙 9번).
    """
    reasons: list[str] = []

    if not profile.is_active:
        reasons.append(f"Capability Profile이 {profile.status}입니다 (ACTIVE 아님)")

    if not profile.can_trade(asset_class):
        reasons.append(
            f"{asset_class}가 required_instruments에 없습니다: "
            f"{sorted(profile.required_instruments)}"
        )

    # 실행·리스크·회계 Capability가 전부 있어야 한다
    for name, caps in (
        ("execution", profile.execution_capabilities),
        ("risk", profile.risk_capabilities),
        ("accounting", profile.accounting_capabilities),
    ):
        if not caps:
            reasons.append(f"{name}_capabilities가 비어 있습니다")

    if asset_class in CERTIFICATION_REQUIRED:
        missing = DERIVATIVE_CERTIFIERS - profile.certified_by
        if missing:
            reasons.append(
                f"{asset_class}는 Certification이 필요합니다. 미서명: {sorted(missing)}"
            )

    if contract is not None:
        as_of = as_of or date.today()
        if contract.is_expired_at(as_of):
            reasons.append(f"만기({contract.expiry_date})가 지난 계약입니다")
        elif contract.days_to_expiry(as_of) == 0 and not allow_expiry_day:
            # 만기일 당일은 정산 규칙이 다르다. 기본 차단하고 명시적으로만 연다.
            reasons.append(f"만기일 당일({contract.expiry_date})입니다")

    return CapabilityVerdict(allowed=not reasons, reasons=tuple(reasons))


def build_roll_group(
    *,
    trade_case_id: UUID,
    fund_id: UUID,
    expiring: DerivativeContract,
    next_contract: DerivativeContract,
    quantity: Decimal,
    side: Side,
    idempotency_key: str,
) -> IntentGroup:
    """Roll을 2-Leg ALL_OR_NONE 그룹으로 만든다.

    Roll은 "만기 계약 청산 + 차기 계약 진입"이며 **둘이 나뉘면 안 된다.**
    청산만 되고 진입이 실패하면 포지션이 사라지고, 진입만 되면 두 배가 된다.
    그래서 F30의 ALL_OR_NONE + CANCEL_ALL을 그대로 쓴다.

    이 함수는 그룹을 만들 뿐이고, 실제 접수는 check_order_capability를
    통과해야 한다 - Roll이라고 Certification을 건너뛰지 않는다.
    """
    if expiring.instrument_id == next_contract.instrument_id:
        raise CapabilityError("만기 계약과 차기 계약이 같습니다")
    if next_contract.expiry_date <= expiring.expiry_date:
        raise CapabilityError(
            f"차기 계약 만기({next_contract.expiry_date})가 "
            f"현재 계약({expiring.expiry_date})보다 늦지 않습니다"
        )
    if expiring.contract_kind is not next_contract.contract_kind:
        raise CapabilityError("Roll 대상의 계약 종류가 다릅니다")

    closing_side = Side.SELL if side is Side.BUY else Side.BUY
    return IntentGroup(
        intent_group_id=uuid4(),
        trade_case_id=trade_case_id,
        fund_id=fund_id,
        atomicity_policy=AtomicityPolicy.ALL_OR_NONE,
        failure_policy=FailurePolicy.CANCEL_ALL,
        capability_profile_id=None,
        idempotency_key=idempotency_key,
        legs=(
            Leg(leg_index=0, instrument_id=expiring.instrument_id,
                side=closing_side, quantity=quantity,
                position_effect=PositionEffect.CLOSE,
                contract_multiplier=expiring.contract_multiplier),
            Leg(leg_index=1, instrument_id=next_contract.instrument_id,
                side=side, quantity=quantity,
                position_effect=PositionEffect.OPEN,
                contract_multiplier=next_contract.contract_multiplier),
        ),
    )


@dataclass(frozen=True)
class ExerciseEvent:
    """행사/배정. 만기에 Position이 바뀌는 사건이다.

    **분개는 회계본부가 한다.** 여기서는 그 사건이 유효한지와 결과 Position만
    계산한다 - 원장은 departments/05-accounting-portfolio 소관이다.
    """

    contract: DerivativeContract
    quantity: Decimal
    is_assignment: bool           # 배정(수동)인가 행사(능동)인가
    as_of: date
    approval_id: str | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise CapabilityError(f"행사 수량이 0 이하입니다: {self.quantity}")
        if self.contract.contract_kind not in STRIKE_REQUIRED:
            raise CapabilityError(
                f"{self.contract.contract_kind}는 행사 대상이 아닙니다"
            )
        style = self.contract.exercise_style
        if style is ExerciseStyle.EUROPEAN and self.as_of != self.contract.expiry_date:
            raise CapabilityError(
                f"유럽형은 만기일({self.contract.expiry_date})에만 행사할 수 있습니다"
            )
        if self.contract.is_expired_at(self.as_of):
            raise CapabilityError(f"만기({self.contract.expiry_date})가 지났습니다")
        # 능동 행사는 선택이므로 승인이 필요하다. 배정은 우리가 고른 게 아니다.
        if not self.is_assignment and not self.approval_id:
            raise CapabilityError("능동 행사는 승인(approval_id) 없이 할 수 없습니다")

    @property
    def underlying_quantity(self) -> Decimal:
        """행사로 생기는 기초자산 수량. 승수를 곱한다."""
        return self.quantity * self.contract.contract_multiplier

    @property
    def cash_amount(self) -> Decimal:
        """실물 인수도면 행사가 x 기초수량이 오간다. 현금정산이면 여기서 안 낸다."""
        if self.contract.settlement_type is SettlementType.CASH:
            return ZERO
        return self.contract.strike_price * self.underlying_quantity


if __name__ == "__main__":
    from datetime import timedelta

    today = date(2026, 7, 31)
    fund, case = uuid4(), uuid4()
    k200, k200_next, samsung = uuid4(), uuid4(), uuid4()

    def raises(fn, why, exc=CapabilityError):
        try:
            fn()
        except exc:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    def future(inst=k200, expiry=None, mult="250000") -> DerivativeContract:
        return DerivativeContract(
            instrument_id=inst, contract_kind=ContractKind.FUTURE,
            expiry_date=expiry or (today + timedelta(days=30)),
            contract_multiplier=Decimal(mult),
            settlement_type=SettlementType.CASH,
        )

    def option(kind=ContractKind.CALL, style=ExerciseStyle.EUROPEAN,
               settle=SettlementType.CASH, expiry=None) -> DerivativeContract:
        return DerivativeContract(
            instrument_id=uuid4(), contract_kind=kind,
            expiry_date=expiry or (today + timedelta(days=30)),
            contract_multiplier=Decimal("250000"),
            settlement_type=settle, strike_price=Decimal("350"),
            exercise_style=style, underlying_instrument_id=k200,
        )

    def profile(**kw) -> CapabilityProfile:
        base = dict(
            capability_profile_id=uuid4(), profile_code="cash-long-only", version=1,
            status=ProfileStatus.ACTIVE,
            required_instruments=frozenset({"EQUITY"}),
            execution_capabilities=frozenset({"market", "limit"}),
            risk_capabilities=frozenset({"position_limit"}),
            accounting_capabilities=frozenset({"double_entry"}),
        )
        return CapabilityProfile(**{**base, **kw})

    # 1. 계약 검증 — 말이 안 되는 파생 계약은 만들어지지 않는다
    raises(lambda: future(mult="0"), "승수 0")
    raises(lambda: DerivativeContract(
        instrument_id=uuid4(), contract_kind=ContractKind.CALL,
        expiry_date=today, contract_multiplier=Decimal("1"),
        settlement_type=SettlementType.CASH), "행사가 없는 콜")
    raises(lambda: DerivativeContract(
        instrument_id=uuid4(), contract_kind=ContractKind.PUT,
        expiry_date=today, contract_multiplier=Decimal("1"),
        settlement_type=SettlementType.CASH,
        strike_price=Decimal("100")), "행사 방식 없는 풋")
    raises(lambda: future().__class__(
        instrument_id=uuid4(), contract_kind=ContractKind.FUTURE,
        expiry_date=today, contract_multiplier=Decimal("1"),
        settlement_type=SettlementType.CASH, margin_currency="won"), "통화 코드 오류")

    # 2. 명목금액은 승수를 곱한다 — 이걸 빼먹으면 한도가 조용히 느슨해진다
    f = future()
    assert f.notional(Decimal("1"), Decimal("350")) == Decimal("87500000"), \
        "승수가 빠졌다. 8,750만원짜리를 350원으로 계산했다"
    assert f.notional(Decimal("2"), Decimal("350")) == Decimal("175000000")
    raises(lambda: f.notional(Decimal("0"), Decimal("350")), "수량 0")
    raises(lambda: f.notional(Decimal("1"), Decimal("0")), "가격 0")

    # 3. 현물 주문은 통과한다 (초기 시장 KOSPI/KOSDAQ 현물)
    v = check_order_capability(profile(), "EQUITY")
    assert v.allowed is True, v.reason

    # 4. **파생은 Certification 없이 막힌다** — 이게 F31의 핵심이다
    derivative_ready = profile(
        required_instruments=frozenset({"EQUITY", "FUTURE"}),
        execution_capabilities=frozenset({"market", "limit"}),
    )
    v = check_order_capability(derivative_ready, "FUTURE", contract=f, as_of=today)
    assert v.allowed is False, "Certification 없이 선물 주문이 통과했다"
    assert "Certification" in v.reason, v.reason

    # 서명이 일부만 있어도 안 된다
    partial = profile(
        required_instruments=frozenset({"EQUITY", "FUTURE"}),
        certified_by=frozenset({"broker", "risk"}),
    )
    v = check_order_capability(partial, "FUTURE", contract=f, as_of=today)
    assert v.allowed is False and "accounting" in v.reason, v.reason

    # 셋 다 서명되면 열린다
    certified = profile(
        required_instruments=frozenset({"EQUITY", "FUTURE"}),
        certified_by=DERIVATIVE_CERTIFIERS,
    )
    v = check_order_capability(certified, "FUTURE", contract=f, as_of=today)
    assert v.allowed is True, v.reason

    # 5. 공매도도 Certification 대상이다 (Long-only 기본)
    v = check_order_capability(
        profile(required_instruments=frozenset({"EQUITY", "SHORT"})), "SHORT")
    assert v.allowed is False and "Certification" in v.reason

    # 6. Profile이 ACTIVE가 아니면 막힌다
    for bad in (ProfileStatus.DRAFT, ProfileStatus.APPROVED, ProfileStatus.RETIRED):
        v = check_order_capability(profile(status=bad), "EQUITY")
        assert v.allowed is False, f"{bad}인데 통과했다"

    # 7. execution/risk/accounting 중 하나라도 비면 막힌다 (팀 가이드 1.1)
    for empty in ("execution_capabilities", "risk_capabilities", "accounting_capabilities"):
        v = check_order_capability(profile(**{empty: frozenset()}), "EQUITY")
        assert v.allowed is False and empty in v.reason, f"{empty} 비었는데 통과했다"

    # 8. 만기 게이트 — 지난 계약과 만기일 당일을 막는다
    expired = future(expiry=today - timedelta(days=1))
    v = check_order_capability(certified, "FUTURE", contract=expired, as_of=today)
    assert v.allowed is False and "만기" in v.reason

    expiry_day = future(expiry=today)
    v = check_order_capability(certified, "FUTURE", contract=expiry_day, as_of=today)
    assert v.allowed is False, "만기일 당일이 기본 통과했다"
    v = check_order_capability(certified, "FUTURE", contract=expiry_day,
                               as_of=today, allow_expiry_day=True)
    assert v.allowed is True, "명시적으로 열었는데 막혔다"

    # 9. 사유는 여러 개가 함께 나온다 — 하나 고치고 또 막히는 일이 없게
    v = check_order_capability(
        profile(status=ProfileStatus.DRAFT, risk_capabilities=frozenset()),
        "FUTURE", contract=expired, as_of=today)
    assert len(v.reasons) >= 4, v.reasons

    # 10. Roll은 2-Leg ALL_OR_NONE 그룹이다 (청산과 진입이 나뉘면 안 된다)
    nxt = future(inst=k200_next, expiry=today + timedelta(days=120))
    g = build_roll_group(trade_case_id=case, fund_id=fund, expiring=f,
                         next_contract=nxt, quantity=Decimal("2"),
                         side=Side.BUY, idempotency_key="roll_1")
    assert g.atomicity_policy is AtomicityPolicy.ALL_OR_NONE
    assert g.failure_policy is FailurePolicy.CANCEL_ALL
    assert g.leg_count == 2
    close_leg, open_leg = g.leg_at(0), g.leg_at(1)
    assert close_leg.side is Side.SELL and close_leg.position_effect is PositionEffect.CLOSE
    assert open_leg.side is Side.BUY and open_leg.position_effect is PositionEffect.OPEN
    assert close_leg.instrument_id == k200 and open_leg.instrument_id == k200_next
    assert open_leg.contract_multiplier == Decimal("250000"), "Leg에 승수가 안 실렸다"

    # 11. Roll 계약 검증
    raises(lambda: build_roll_group(trade_case_id=case, fund_id=fund, expiring=f,
                                    next_contract=f, quantity=Decimal("1"),
                                    side=Side.BUY, idempotency_key="r"), "같은 계약")
    raises(lambda: build_roll_group(trade_case_id=case, fund_id=fund, expiring=nxt,
                                    next_contract=f, quantity=Decimal("1"),
                                    side=Side.BUY, idempotency_key="r"), "차기 만기가 더 이름")
    raises(lambda: build_roll_group(trade_case_id=case, fund_id=fund, expiring=f,
                                    next_contract=option(), quantity=Decimal("1"),
                                    side=Side.BUY, idempotency_key="r"), "계약 종류 다름")

    # 12. Exercise/Assignment — 능동 행사는 승인이 필요하다
    call = option()
    raises(lambda: ExerciseEvent(contract=call, quantity=Decimal("1"),
                                 is_assignment=False, as_of=call.expiry_date),
           "승인 없는 능동 행사")
    ok = ExerciseEvent(contract=call, quantity=Decimal("1"), is_assignment=False,
                       as_of=call.expiry_date, approval_id="apr_1")
    assert ok.underlying_quantity == Decimal("250000"), "승수가 안 곱해졌다"
    assert ok.cash_amount == ZERO, "현금정산인데 인수도 금액이 나왔다"

    # 배정은 우리가 고른 게 아니라 승인이 필요 없다
    assigned = ExerciseEvent(contract=call, quantity=Decimal("1"), is_assignment=True,
                             as_of=call.expiry_date)
    assert assigned.underlying_quantity == Decimal("250000")

    # 13. 유럽형은 만기일에만, 미국형은 만기 전에도
    raises(lambda: ExerciseEvent(contract=call, quantity=Decimal("1"),
                                 is_assignment=True, as_of=today), "유럽형 조기 행사")
    american = option(style=ExerciseStyle.AMERICAN)
    early = ExerciseEvent(contract=american, quantity=Decimal("1"),
                          is_assignment=True, as_of=today)
    assert early.underlying_quantity == Decimal("250000")

    # 만기 지난 계약은 행사할 수 없다
    dead = option(style=ExerciseStyle.AMERICAN, expiry=today - timedelta(days=1))
    raises(lambda: ExerciseEvent(contract=dead, quantity=Decimal("1"),
                                 is_assignment=True, as_of=today), "만기 지난 행사")
    # 선물은 행사 대상이 아니다
    raises(lambda: ExerciseEvent(contract=f, quantity=Decimal("1"),
                                 is_assignment=True, as_of=today), "선물 행사")

    # 14. 실물 인수도는 행사가 x 기초수량이 오간다
    physical = option(settle=SettlementType.PHYSICAL, style=ExerciseStyle.AMERICAN)
    ev = ExerciseEvent(contract=physical, quantity=Decimal("1"),
                       is_assignment=True, as_of=today)
    assert ev.cash_amount == Decimal("350") * Decimal("250000"), ev.cash_amount

    print("ok - Derivatives Capability 14개 영역 점검 통과")
