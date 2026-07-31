#!/usr/bin/env python3
"""F30: Multi-leg Execution - Leg 관계, 부분 체결 복구와 Atomicity Policy.

소유: 도현 (트레이딩본부)
근거: docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md 4절 F30
      docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 1.1(Multi-Strategy 책임)
      supabase/migrations/20260729000400_execution_risk_accounting.sql
        execution.intent_groups, execution.order_intents(leg_index, position_effect)

Pair·Basket·Hedge·Roll처럼 **여러 Leg가 하나의 의도**인 주문을 다룬다.
Leg를 따로따로 낸 주문과 다른 점은 하나뿐이다 - 일부만 체결됐을 때
그것이 "부분 성공"이 아니라 **의도하지 않은 포지션**이라는 것.

그래서 이 모듈의 핵심은 주문 생성이 아니라 **부분 체결 복구 판정**이다.

    ALL_OR_NONE 인데 3개 중 2개만 체결됐다
      -> COMPLETED 아니다. PARTIAL_RECOVERY다.
      -> 미체결 Leg는 취소하고, 체결된 Leg는 정책에 따라 되돌린다.

팀 가이드 1.1: "일부 Leg 체결 시 전략 정책에 따라 전체 취소, Hedge, Retry 또는
Reduce-only로 전환하며 **상태를 숨기지 않는다.**"

개발 원칙 9번대로 실패는 거래 확대가 아니라 축소·차단 방향으로 떨어진다.
복구 자체가 불가능하면 FAILED_SAFE이고, 이 상태에서는 신규 주문을 받지 않는다.

**이 모듈은 주문을 전송하지 않는다.** 판정만 하고 실제 취소·청산은 OMS가 한다.

자체 점검: python departments/02-trading/multileg/intent_group.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "contracts"))

from contracts import Side  # noqa: E402

ZERO = Decimal("0")


class AtomicityPolicy(StrEnum):
    """Leg들이 '전부 되어야 하는가'를 정한다."""

    ALL_OR_NONE = "ALL_OR_NONE"    # 하나라도 못 채우면 전체가 실패다 (Pair, Roll)
    BEST_EFFORT = "BEST_EFFORT"    # Leg마다 독립. 부분 체결도 정상이다 (Basket)
    # 앞 Leg가 채워져야 다음 Leg를 낸다 (Hedge를 먼저 깔고 진입하는 경우)
    SEQUENTIAL = "SEQUENTIAL"


class FailurePolicy(StrEnum):
    """부분 체결이 났을 때 무엇을 할 것인가. 팀 가이드 1.1의 네 가지."""

    CANCEL_ALL = "CANCEL_ALL"      # 미체결 취소 + 체결분 반대매매로 원위치
    HEDGE = "HEDGE"                # 남은 노출을 헤지 Leg로 덮는다
    RETRY = "RETRY"                # 미체결 Leg를 재시도한다
    REDUCE_ONLY = "REDUCE_ONLY"    # 축소 방향 주문만 허용. 신규 진입 금지


class GroupStatus(StrEnum):
    """execution.intent_groups.group_status와 같은 값이어야 한다."""

    DRAFT = "DRAFT"
    RISK_PENDING = "RISK_PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    PARTIAL_RECOVERY = "PARTIAL_RECOVERY"   # 부분 체결. 복구 진행 중
    CANCELLED = "CANCELLED"
    FAILED_SAFE = "FAILED_SAFE"             # 복구 불가. 신규 주문 차단


class PositionEffect(StrEnum):
    """execution.order_intents.position_effect와 같은 값이어야 한다."""

    OPEN = "OPEN"
    CLOSE = "CLOSE"
    INCREASE = "INCREASE"
    REDUCE = "REDUCE"
    HEDGE = "HEDGE"


# 이 상태에서는 그룹이 더 움직이지 않는다.
GROUP_TERMINAL_STATES = frozenset({
    GroupStatus.COMPLETED, GroupStatus.REJECTED,
    GroupStatus.CANCELLED, GroupStatus.FAILED_SAFE,
})

# 신규 진입(OPEN/INCREASE)을 허용하지 않는 효과. REDUCE_ONLY 전환에서 쓴다.
OPENING_EFFECTS = frozenset({PositionEffect.OPEN, PositionEffect.INCREASE})


class MultiLegError(Exception):
    """Leg 구성이나 전이가 계약을 어긴 경우. 조용히 넘어가지 않는다."""


@dataclass(frozen=True)
class Leg:
    """그룹 안의 Leg 하나. OrderIntent 한 건에 대응한다.

    `leg_index`는 그룹 안에서 유일하다 (DB의 unique(intent_group_id, leg_index)).
    SEQUENTIAL에서는 이 순서가 곧 집행 순서다.
    """

    leg_index: int
    instrument_id: UUID
    side: Side
    quantity: Decimal
    position_effect: PositionEffect
    order_intent_id: UUID = field(default_factory=uuid4)
    # 파생 Leg면 승수가 1이 아니다. 명목금액 계산이 달라진다(F31).
    contract_multiplier: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if self.leg_index < 0:
            raise MultiLegError(f"leg_index가 음수입니다: {self.leg_index}")
        if self.quantity <= 0:
            raise MultiLegError(f"Leg 수량이 0 이하입니다: {self.quantity}")
        if self.contract_multiplier <= 0:
            raise MultiLegError(f"계약 승수가 0 이하입니다: {self.contract_multiplier}")


@dataclass(frozen=True)
class LegOutcome:
    """Leg 하나의 체결 결과. OMS의 BrokerOrder에서 뽑아 넣는다."""

    leg_index: int
    filled_quantity: Decimal
    requested_quantity: Decimal
    is_terminal: bool          # 더 이상 체결되지 않는 상태인가
    state: str = ""            # 참고용 원문 상태. 판정에 쓰지 않는다

    def __post_init__(self) -> None:
        if self.filled_quantity < 0:
            raise MultiLegError("체결 수량이 음수입니다")
        if self.filled_quantity > self.requested_quantity:
            raise MultiLegError(
                f"체결({self.filled_quantity})이 주문({self.requested_quantity})을 넘습니다"
            )

    @property
    def is_full(self) -> bool:
        return self.filled_quantity == self.requested_quantity

    @property
    def is_untouched(self) -> bool:
        return self.filled_quantity == 0

    @property
    def leaves(self) -> Decimal:
        return self.requested_quantity - self.filled_quantity


@dataclass(frozen=True)
class RecoveryPlan:
    """부분 체결 복구 계획. **제안이지 집행이 아니다.**

    실제 취소·반대매매는 OMS와 Risk Gate를 다시 통과해야 한다. 여기서
    주문을 내면 Risk 승인 없이 주문이 나가는 경로가 생긴다.
    """

    group_status: GroupStatus
    policy: FailurePolicy
    cancel_leg_indexes: tuple[int, ...] = ()      # 미체결분 취소 대상
    unwind_leg_indexes: tuple[int, ...] = ()      # 체결분 반대매매 대상
    retry_leg_indexes: tuple[int, ...] = ()       # 재시도 대상
    reduce_only: bool = False                     # 신규 진입 차단 여부
    reason: str = ""

    @property
    def blocks_new_entry(self) -> bool:
        """신규 진입을 막아야 하는가. FAILED_SAFE거나 REDUCE_ONLY 전환이면 막는다."""
        return self.reduce_only or self.group_status is GroupStatus.FAILED_SAFE


@dataclass
class IntentGroup:
    """여러 Leg를 하나의 의도로 묶은 것. execution.intent_groups에 대응한다."""

    intent_group_id: UUID
    trade_case_id: UUID
    fund_id: UUID
    atomicity_policy: AtomicityPolicy
    failure_policy: FailurePolicy
    legs: tuple[Leg, ...]
    idempotency_key: str
    capability_profile_id: UUID | None = None
    status: GroupStatus = GroupStatus.DRAFT
    version: int = 0

    def __post_init__(self) -> None:
        if not self.legs:
            raise MultiLegError("Leg가 없는 그룹은 만들 수 없습니다")
        indexes = [l.leg_index for l in self.legs]
        if len(set(indexes)) != len(indexes):
            raise MultiLegError(f"leg_index가 중복됩니다: {sorted(indexes)}")
        if len(self.legs) == 1 and self.atomicity_policy is AtomicityPolicy.ALL_OR_NONE:
            # 단일 Leg에 ALL_OR_NONE은 의미가 없다. 계약을 오해했다는 신호라
            # 조용히 통과시키지 않는다.
            raise MultiLegError(
                "Leg가 1개인데 ALL_OR_NONE입니다. 단일 주문은 그룹이 필요 없습니다"
            )
        if not self.idempotency_key:
            raise MultiLegError("idempotency_key가 없습니다")

    @property
    def leg_count(self) -> int:
        return len(self.legs)

    @property
    def is_terminal(self) -> bool:
        return self.status in GROUP_TERMINAL_STATES

    def leg_at(self, leg_index: int) -> Leg:
        for leg in self.legs:
            if leg.leg_index == leg_index:
                return leg
        raise MultiLegError(f"leg_index {leg_index}가 그룹에 없습니다")

    def next_sequential_leg(self, outcomes: dict[int, LegOutcome]) -> Leg | None:
        """SEQUENTIAL에서 지금 낼 수 있는 Leg. 앞 Leg가 완전 체결돼야 다음이 열린다.

        앞 Leg가 부분 체결이면 None이다 - 덜 채워진 채로 다음 Leg를 내면
        의도한 비율이 깨진다.
        """
        if self.atomicity_policy is not AtomicityPolicy.SEQUENTIAL:
            raise MultiLegError("SEQUENTIAL 그룹에서만 쓸 수 있습니다")
        for leg in sorted(self.legs, key=lambda l: l.leg_index):
            outcome = outcomes.get(leg.leg_index)
            if outcome is None:
                return leg
            if not outcome.is_full:
                return None
        return None


def evaluate_group(
    group: IntentGroup, outcomes: dict[int, LegOutcome]
) -> RecoveryPlan:
    """Leg 결과를 보고 그룹 상태와 복구 계획을 판정한다.

    **부분 체결을 COMPLETED로 만들지 않는다.** 상태를 숨기지 않는다는 것이
    팀 가이드 1.1의 요구이며, 화면과 원장이 같은 사실을 봐야 한다.
    """
    if group.is_terminal:
        raise MultiLegError(f"{group.status}는 종료 상태입니다. 다시 판정하지 않습니다")

    missing = {l.leg_index for l in group.legs} - set(outcomes)
    unknown = set(outcomes) - {l.leg_index for l in group.legs}
    if unknown:
        raise MultiLegError(f"그룹에 없는 leg_index 결과입니다: {sorted(unknown)}")

    filled = [o for o in outcomes.values() if not o.is_untouched]
    settled = [o for o in outcomes.values() if o.is_terminal]

    # 아직 진행 중 - 판정을 서두르지 않는다
    if missing or len(settled) < group.leg_count:
        return RecoveryPlan(
            group_status=GroupStatus.EXECUTING,
            policy=group.failure_policy,
            reason=f"진행 중 - {len(settled)}/{group.leg_count} Leg 종료",
        )

    if all(o.is_full for o in outcomes.values()):
        return RecoveryPlan(
            group_status=GroupStatus.COMPLETED,
            policy=group.failure_policy,
            reason="전 Leg 완전 체결",
        )

    # 아무것도 안 채워졌다 - 되돌릴 포지션이 없으므로 그냥 취소다
    if not filled:
        return RecoveryPlan(
            group_status=GroupStatus.CANCELLED,
            policy=group.failure_policy,
            reason="체결된 Leg 없음",
        )

    # BEST_EFFORT는 부분 체결이 정상 결과다. 다만 COMPLETED는 아니다 -
    # 의도한 것을 다 못 채웠다는 사실이 남아야 한다.
    if group.atomicity_policy is AtomicityPolicy.BEST_EFFORT:
        return RecoveryPlan(
            group_status=GroupStatus.PARTIAL_RECOVERY,
            policy=group.failure_policy,
            cancel_leg_indexes=_leaves(outcomes),
            reduce_only=group.failure_policy is FailurePolicy.REDUCE_ONLY,
            reason="BEST_EFFORT 부분 체결 - 미체결분 정리",
        )

    # 여기부터 ALL_OR_NONE / SEQUENTIAL의 부분 체결 = 의도하지 않은 포지션
    return _recover(group, outcomes)


def _recover(group: IntentGroup, outcomes: dict[int, LegOutcome]) -> RecoveryPlan:
    """ALL_OR_NONE·SEQUENTIAL 부분 체결의 복구 계획."""
    cancels = _leaves(outcomes)
    unwinds = tuple(sorted(o.leg_index for o in outcomes.values() if not o.is_untouched))
    policy = group.failure_policy
    base = f"{group.atomicity_policy} 부분 체결 - "

    if policy is FailurePolicy.CANCEL_ALL:
        return RecoveryPlan(
            group_status=GroupStatus.PARTIAL_RECOVERY, policy=policy,
            cancel_leg_indexes=cancels, unwind_leg_indexes=unwinds,
            reason=base + "미체결 취소 + 체결분 반대매매",
        )

    if policy is FailurePolicy.RETRY:
        retries = _leaves(outcomes)
        if not retries:
            # 재시도할 Leg가 없는데 완전 체결도 아니다 = 복구 수단이 없다.
            # 거래를 늘리는 쪽으로 fallback하지 않고 안전하게 멈춘다.
            return RecoveryPlan(
                group_status=GroupStatus.FAILED_SAFE, policy=policy,
                unwind_leg_indexes=unwinds, reduce_only=True,
                reason=base + "재시도 대상 없음 - 안전 정지",
            )
        return RecoveryPlan(
            group_status=GroupStatus.PARTIAL_RECOVERY, policy=policy,
            retry_leg_indexes=retries,
            reason=base + "미체결 Leg 재시도",
        )

    if policy is FailurePolicy.HEDGE:
        return RecoveryPlan(
            group_status=GroupStatus.PARTIAL_RECOVERY, policy=policy,
            cancel_leg_indexes=cancels,
            # 헤지는 노출을 덮는 것이지 없애는 것이 아니다. 남은 노출이 있는
            # 동안에는 신규 진입을 막는다.
            reduce_only=True,
            reason=base + "잔여 노출 헤지, 신규 진입 차단",
        )

    # REDUCE_ONLY
    return RecoveryPlan(
        group_status=GroupStatus.PARTIAL_RECOVERY, policy=policy,
        cancel_leg_indexes=cancels, reduce_only=True,
        reason=base + "축소 주문만 허용",
    )


def _leaves(outcomes: dict[int, LegOutcome]) -> tuple[int, ...]:
    """아직 안 채워진 Leg들. 취소·재시도 대상이다."""
    return tuple(sorted(o.leg_index for o in outcomes.values() if not o.is_full))


def allows_leg(plan: RecoveryPlan, leg: Leg) -> bool:
    """복구 상태에서 이 Leg를 새로 낼 수 있는가.

    REDUCE_ONLY 전환과 FAILED_SAFE에서 신규 진입(OPEN/INCREASE)을 막는다.
    개발 원칙 9번 - 위험한 상황에서 거래를 늘리는 쪽으로 열리지 않는다.
    """
    if plan.group_status is GroupStatus.FAILED_SAFE:
        return False
    if plan.blocks_new_entry:
        return leg.position_effect not in OPENING_EFFECTS
    return True


if __name__ == "__main__":
    fund, case = uuid4(), uuid4()
    a, b, c = uuid4(), uuid4(), uuid4()

    def raises(fn, why, exc=MultiLegError):
        try:
            fn()
        except exc:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    def leg(i, inst, side=Side.BUY, qty="100", effect=PositionEffect.OPEN, mult="1") -> Leg:
        return Leg(leg_index=i, instrument_id=inst, side=side,
                   quantity=Decimal(qty), position_effect=effect,
                   contract_multiplier=Decimal(mult))

    def group(atom, fail, legs, **kw) -> IntentGroup:
        return IntentGroup(
            intent_group_id=uuid4(), trade_case_id=case, fund_id=fund,
            atomicity_policy=atom, failure_policy=fail, legs=tuple(legs),
            idempotency_key=f"grp_{uuid4()}", **kw,
        )

    def out(i, filled, requested="100", terminal=True) -> LegOutcome:
        return LegOutcome(leg_index=i, filled_quantity=Decimal(filled),
                          requested_quantity=Decimal(requested), is_terminal=terminal)

    pair = [leg(0, a, Side.BUY), leg(1, b, Side.SELL)]

    # 1. 계약 검증 — 말이 안 되는 그룹은 만들어지지 않는다
    raises(lambda: group(AtomicityPolicy.BEST_EFFORT, FailurePolicy.CANCEL_ALL, []),
           "Leg 0개")
    raises(lambda: group(AtomicityPolicy.BEST_EFFORT, FailurePolicy.CANCEL_ALL,
                         [leg(0, a), leg(0, b)]), "leg_index 중복")
    raises(lambda: group(AtomicityPolicy.ALL_OR_NONE, FailurePolicy.CANCEL_ALL,
                         [leg(0, a)]), "단일 Leg ALL_OR_NONE")
    raises(lambda: leg(-1, a), "음수 leg_index")
    raises(lambda: leg(0, a, qty="0"), "수량 0")
    raises(lambda: leg(0, a, mult="0"), "승수 0")
    raises(lambda: LegOutcome(leg_index=0, filled_quantity=Decimal("101"),
                              requested_quantity=Decimal("100"), is_terminal=True),
           "주문보다 많은 체결")

    # 2. 전 Leg 완전 체결 -> COMPLETED
    g = group(AtomicityPolicy.ALL_OR_NONE, FailurePolicy.CANCEL_ALL, pair)
    plan = evaluate_group(g, {0: out(0, "100"), 1: out(1, "100")})
    assert plan.group_status is GroupStatus.COMPLETED, plan.group_status
    assert not plan.blocks_new_entry

    # 3. 아직 진행 중이면 판정을 서두르지 않는다
    plan = evaluate_group(g, {0: out(0, "100")})
    assert plan.group_status is GroupStatus.EXECUTING, "결과가 덜 왔는데 확정했다"
    plan = evaluate_group(g, {0: out(0, "100"), 1: out(1, "0", terminal=False)})
    assert plan.group_status is GroupStatus.EXECUTING, "미종료 Leg가 있는데 확정했다"

    # 4. 아무것도 안 채워지면 되돌릴 게 없다 -> CANCELLED
    plan = evaluate_group(g, {0: out(0, "0"), 1: out(1, "0")})
    assert plan.group_status is GroupStatus.CANCELLED
    assert plan.unwind_leg_indexes == (), "체결이 없는데 반대매매를 걸었다"

    # 5. ALL_OR_NONE 부분 체결은 COMPLETED가 아니다 (상태를 숨기지 않는다)
    plan = evaluate_group(g, {0: out(0, "100"), 1: out(1, "40")})
    assert plan.group_status is GroupStatus.PARTIAL_RECOVERY, plan.group_status
    assert plan.cancel_leg_indexes == (1,), plan.cancel_leg_indexes
    assert plan.unwind_leg_indexes == (0, 1), "체결분 반대매매 대상이 빠졌다"

    # 6. BEST_EFFORT 부분 체결도 COMPLETED가 아니다 — 다만 되돌리지는 않는다
    g2 = group(AtomicityPolicy.BEST_EFFORT, FailurePolicy.CANCEL_ALL,
               [leg(0, a), leg(1, b), leg(2, c)])
    plan = evaluate_group(g2, {0: out(0, "100"), 1: out(1, "100"), 2: out(2, "0")})
    assert plan.group_status is GroupStatus.PARTIAL_RECOVERY
    assert plan.cancel_leg_indexes == (2,)
    assert plan.unwind_leg_indexes == (), "BEST_EFFORT인데 체결분을 되돌렸다"

    # 7. RETRY — 미체결 Leg가 있으면 재시도, 없으면 안전 정지
    g3 = group(AtomicityPolicy.ALL_OR_NONE, FailurePolicy.RETRY, pair)
    plan = evaluate_group(g3, {0: out(0, "100"), 1: out(1, "40")})
    assert plan.group_status is GroupStatus.PARTIAL_RECOVERY
    assert plan.retry_leg_indexes == (1,), plan.retry_leg_indexes
    assert not plan.reduce_only

    # 8. HEDGE — 잔여 노출이 남으므로 신규 진입을 막는다
    g4 = group(AtomicityPolicy.ALL_OR_NONE, FailurePolicy.HEDGE, pair)
    plan = evaluate_group(g4, {0: out(0, "100"), 1: out(1, "0")})
    assert plan.group_status is GroupStatus.PARTIAL_RECOVERY
    assert plan.reduce_only is True and plan.blocks_new_entry

    # 9. REDUCE_ONLY — 축소 주문만 통과한다
    g5 = group(AtomicityPolicy.ALL_OR_NONE, FailurePolicy.REDUCE_ONLY, pair)
    plan = evaluate_group(g5, {0: out(0, "100"), 1: out(1, "30")})
    assert plan.blocks_new_entry
    assert allows_leg(plan, leg(9, a, effect=PositionEffect.REDUCE)) is True
    assert allows_leg(plan, leg(9, a, effect=PositionEffect.CLOSE)) is True
    assert allows_leg(plan, leg(9, a, effect=PositionEffect.OPEN)) is False, \
        "REDUCE_ONLY인데 신규 진입이 통과했다"
    assert allows_leg(plan, leg(9, a, effect=PositionEffect.INCREASE)) is False

    # 10. FAILED_SAFE에서는 축소 주문조차 이 경로로 안 나간다
    safe = RecoveryPlan(group_status=GroupStatus.FAILED_SAFE,
                        policy=FailurePolicy.CANCEL_ALL, reduce_only=True)
    assert safe.blocks_new_entry
    assert allows_leg(safe, leg(0, a, effect=PositionEffect.REDUCE)) is False, \
        "FAILED_SAFE인데 주문이 통과했다"

    # 11. SEQUENTIAL — 앞 Leg가 완전 체결돼야 다음이 열린다
    g6 = group(AtomicityPolicy.SEQUENTIAL, FailurePolicy.CANCEL_ALL,
               [leg(0, a, effect=PositionEffect.HEDGE), leg(1, b)])
    assert g6.next_sequential_leg({}).leg_index == 0, "첫 Leg가 안 열렸다"
    assert g6.next_sequential_leg({0: out(0, "40")}) is None, \
        "앞 Leg가 부분 체결인데 다음 Leg가 열렸다"
    assert g6.next_sequential_leg({0: out(0, "100")}).leg_index == 1
    assert g6.next_sequential_leg({0: out(0, "100"), 1: out(1, "100")}) is None
    raises(lambda: g.next_sequential_leg({}), "SEQUENTIAL이 아닌 그룹")

    # 12. 종료된 그룹은 다시 판정하지 않는다 / 없는 Leg 결과는 거부한다
    g.status = GroupStatus.COMPLETED
    raises(lambda: evaluate_group(g, {0: out(0, "100"), 1: out(1, "100")}),
           "종료 상태 재판정")
    g.status = GroupStatus.EXECUTING
    raises(lambda: evaluate_group(g, {0: out(0, "100"), 9: out(9, "10")}),
           "그룹에 없는 leg_index")

    # 13. leg_at — 인덱스로 Leg를 찾는다
    assert g.leg_at(1).instrument_id == b
    raises(lambda: g.leg_at(7), "없는 leg_index 조회")
    assert g.leg_count == 2 and not g.is_terminal

    print("ok - Multi-leg Execution 13개 영역 점검 통과")
