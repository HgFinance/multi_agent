# 담당자: 영주 (CEO Office)
# 근거: HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F01(사용자 Mandate),
#       TEAM_YOUNGJU_CEO_HR_GUIDE.md 5.1(사용자 Mandate 변경), 10.2(승인과 감사)
#
# F01 의 활성화(Effective Time) 상태 전이.
#   propose(service.py) 로 만든 새 Version 을 "활성"으로 올린다.
#   - TIGHTEN/NEUTRAL: 즉시 활성화        ("장중 Risk 완화는 즉시 적용")
#   - LOOSEN         : 사용자 재승인 필요  ("장중 Risk 확대는 사용자 재승인")
#   - 최초 활성화     : 항상 사용자 승인 필요 (5.1: 사용자 승인 -> Active)
#
# 활성화 시:
#   1) 이전 활성 Version 의 effective_to 를 새 적용 시각으로 닫는다 (덮어쓰기 금지, 10.1).
#   2) mandates.current_version / status 를 갱신한다.
#   3) mandate_decisions 에 APPROVE 를 append 한다 (감사, 10.2).
#
# 이 모듈은 결정을 강제할 뿐 사용자 승인 자체를 만들지 않는다. 재승인이 필요한데 승인이
# 없으면 활성화하지 않고 blocked 를 돌려준다 (CLAUDE.md 개발 원칙 9: 위험은 차단 방향).

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from service import (
    ChangeDirection,
    MandateDecisionRow,
    MandateVersionRepository,
    requires_user_reapproval,
)


@dataclass(frozen=True)
class UserApproval:
    """사용자(또는 권한 있는 승인자)의 명시적 승인 근거."""

    approved_by: str  # governance.user_profiles.user_id
    trace_id: str
    reason: str | None = None


@dataclass(frozen=True)
class ActivationResult:
    activated: bool
    direction: ChangeDirection
    decision: MandateDecisionRow | None
    blocked_reason: str | None = None


class MandateActivationService:
    """Mandate Version 활성화 (5.1)."""

    def __init__(self, repo: MandateVersionRepository) -> None:
        self._repo = repo

    def activate(
        self,
        *,
        mandate_id: str,
        version: int,
        direction: ChangeDirection,
        at: datetime,
        approval: UserApproval | None = None,
    ) -> ActivationResult:
        current_version, _status = self._repo.get_mandate_current(mandate_id)
        is_initial = current_version == 0

        # 재승인 게이트: 최초 활성화 또는 확대(LOOSEN)면 사용자 승인이 있어야 한다.
        needs_approval = is_initial or requires_user_reapproval(direction)
        if needs_approval and approval is None:
            reason = "최초 활성화" if is_initial else "장중 Risk 확대(LOOSEN)"
            return ActivationResult(
                activated=False,
                direction=direction,
                decision=None,
                blocked_reason=f"사용자 재승인 필요: {reason}",
            )

        # 1) 이전 활성 Version 종료.
        if current_version and current_version != version:
            self._repo.set_effective_to(mandate_id, current_version, at)

        # 2) 현재 Version/상태 갱신.
        self._repo.set_mandate_current(mandate_id, version, "ACTIVE")

        # 3) 결정 기록 (감사).
        if approval is not None:
            reason = approval.reason or "사용자 승인"
            approved_by: str | None = approval.approved_by
            trace_id = approval.trace_id
        else:
            # 완화/중립 자동 적용. approved_by 는 null, trace_id 는 생성.
            reason = "장중 Risk 완화 즉시 적용(자동)"
            approved_by = None
            trace_id = str(uuid.uuid4())

        decision = MandateDecisionRow(
            mandate_id=mandate_id,
            version=version,
            decision="APPROVE",
            conditions={},
            reason=reason,
            approved_by=approved_by,
            trace_id=trace_id,
            decided_at=at,
        )
        self._repo.record_decision(decision)

        return ActivationResult(
            activated=True, direction=direction, decision=decision, blocked_reason=None
        )


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/mandate/lifecycle.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timezone

    from policy import MandatePolicy
    from service import (
        InMemoryMandateVersionRepository,
        MandateVersionService,
    )

    def _policy(**over) -> MandatePolicy:
        risk = dict(
            base_capital="100000000",
            currency="KRW",
            max_instrument_weight="0.1",
            max_sector_weight="0.3",
            max_gross_exposure="1.0",
            max_concurrent_positions=10,
            max_daily_loss="0.03",
        )
        risk.update(over.pop("risk", {}))
        return MandatePolicy(
            allowed_assets=["A005930"],
            forbidden_assets=[],
            risk_bounds=risk,
            universe_policy=dict(
                allowed_markets=["KRX"], trading_start="09:00", trading_end="15:30"
            ),
            approval_rules=dict(paper_order_mode="USER_APPROVAL"),
        )

    def _obj() -> dict:
        return {"style": "growth"}

    repo = InMemoryMandateVersionRepository()
    repo.set_fund_base_currency("m1", "KRW")  # accounting.funds.base_currency (결정 4-A)
    vsvc = MandateVersionService(repo)
    asvc = MandateActivationService(repo)

    t1 = datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 7, 30, 13, 0, tzinfo=timezone.utc)

    base = _policy()

    # v1 제안.
    r1 = vsvc.propose_version(
        mandate_id="m1", policy=base, objective_text="성장", objective=_obj(),
        effective_from=t1,
    )
    assert r1.row.version == 1

    # 최초 활성화는 승인 없으면 막힌다.
    blocked = asvc.activate(mandate_id="m1", version=1, direction=r1.direction, at=t1)
    assert blocked.activated is False and "최초" in blocked.blocked_reason

    # 승인 주면 활성화.
    approval = UserApproval(approved_by="u1", trace_id=str(uuid.uuid4()), reason="초기 설정")
    a1 = asvc.activate(
        mandate_id="m1", version=1, direction=r1.direction, at=t1, approval=approval
    )
    assert a1.activated is True
    assert repo.get_mandate_current("m1") == (1, "ACTIVE")

    # v2: 완화(gross 축소) -> 승인 없이 즉시 활성화.
    tightened = _policy(risk={"max_gross_exposure": "0.5"})
    r2 = vsvc.propose_version(
        mandate_id="m1", policy=tightened, objective_text="성장", objective=_obj(),
        effective_from=t2, previous_policy=base,
    )
    assert r2.direction == ChangeDirection.TIGHTEN
    a2 = asvc.activate(mandate_id="m1", version=2, direction=r2.direction, at=t2)
    assert a2.activated is True, "완화는 즉시 적용돼야 한다"
    assert repo.get_mandate_current("m1") == (2, "ACTIVE")
    # 이전 v1 의 effective_to 가 t2 로 닫혔는지.
    v1_row = repo.get("m1", 1)
    assert v1_row.effective_to == t2
    # 자동 적용 결정은 approved_by 가 null.
    assert a2.decision.approved_by is None and a2.decision.trace_id

    # v3: 확대(gross 확대) -> 승인 없으면 막히고, 승인 주면 활성화.
    loosened = _policy(risk={"max_gross_exposure": "2.0"})
    r3 = vsvc.propose_version(
        mandate_id="m1", policy=loosened, objective_text="성장", objective=_obj(),
        effective_from=t3, previous_policy=tightened,
    )
    assert r3.direction == ChangeDirection.LOOSEN
    b3 = asvc.activate(mandate_id="m1", version=3, direction=r3.direction, at=t3)
    assert b3.activated is False and "확대" in b3.blocked_reason
    assert repo.get_mandate_current("m1") == (2, "ACTIVE"), "막힌 동안 현재 Version 불변"

    a3 = asvc.activate(
        mandate_id="m1", version=3, direction=r3.direction, at=t3,
        approval=UserApproval(approved_by="u1", trace_id=str(uuid.uuid4())),
    )
    assert a3.activated is True
    assert repo.get_mandate_current("m1") == (3, "ACTIVE")
    assert repo.get("m1", 2).effective_to == t3

    # 감사: 활성화된 3개 Version 에 대해 APPROVE 결정이 남는다 (막힌 시도는 제외).
    decisions = repo.decisions_for("m1")
    assert len(decisions) == 3
    assert all(d.decision == "APPROVE" for d in decisions)

    print("lifecycle.py 자체 점검 통과")
