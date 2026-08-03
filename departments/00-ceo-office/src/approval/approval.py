#!/usr/bin/env python3
"""GOV-02 1단계: 승인(Approval) 도메인 계약.

소유: 영주 (CEO Office)
근거: docs/02-engineering/GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 2.2절
      (`request_approval` ✅ 확정 — Request/Response JSON 그대로),
      supabase/migrations/20260729000200_governance_workforce.sql
      (governance.approvals — decision/object_type/required_role, unique 제약),
      docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md 10.2("만료된 approval_id로 활성화 거절")

여기엔 LLM이 없다. 승인 상태 전이와 권한 검증은 전부 결정론적 코드다.

불변식:
  1. **승인 방향으로 자동 fallback하지 않는다.** 만료·이미 결정됨·권한 없음은 전부 거절이며,
     어떤 경로로도 PENDING이 스스로 APPROVED가 되지 않는다(CLAUDE.md "실패를 통과로 취급해
     다음 단계로 넘기지 않는다", "승인·승격·권한부여 방향으로 자동 fallback하지 않는다").
  2. **요청 생성과 결정 기록은 다른 권한이다.** `required_role`이 RISK/QA인 승인의 결정은
     CEO Office가 대신 찍을 수 없다(CLAUDE.md "CEO는 리스크 승인, Audit Finding 종결 권한이
     없다"). 이 테이블을 CEO Office의 governance-api가 호스팅하더라도 그렇다.
  3. **`required_role="OWNER"`는 아직 결정할 수 없다.** object_type/object_id가 가리키는 대상의
     소유 부서를 governance.approvals만으로 검증할 방법이 없어서다. 검증 없이 통과시키는
     대신 명시적으로 거절한다(fail-closed) — 지어낸 권한 검사를 넣지 않는다.
  4. **`unique(object_type, object_id, required_role)`가 DDL 제약이다.** 같은 대상·같은 역할의
     승인은 영구히 한 건이며, 거절된 뒤 재요청하면 그 거절된 건이 그대로 조회된다. 이건 스키마가
     정한 계약이므로 애플리케이션에서 우회하지 않는다.

부서 식별자는 **Hermes Profile 이름**을 쓴다 (`ceo-agent`, `risk-management`, `qa-department` 등).
2026-08-04에 확정됐다 — 그 전에는 API 스펙이 대문자 표기(`RISK`/`QA`/`AGENT-WORKFORCE`)를
예시로 썼지만 실제 코드 40개 파일이 전부 Profile 이름을 쓰고 있었고 대문자 표기를 쓰는 코드는
없었으므로, 다수 쪽으로 스펙 문서를 맞췄다(GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 2.2절 주석).
normalize_department()는 대소문자·`_` 차이만 흡수하는 방어 코드로 남긴다.

자체 점검: python departments/00-ceo-office/src/approval/approval.py
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum


class ApprovalDecision(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class ObjectType(str, Enum):
    MANDATE_VERSION = "MANDATE_VERSION"
    AGENT_PROFILE_VERSION = "AGENT_PROFILE_VERSION"
    IMPROVEMENT_CANDIDATE = "IMPROVEMENT_CANDIDATE"
    CAPITAL_ALLOCATION = "CAPITAL_ALLOCATION"


class RequiredRole(str, Enum):
    CEO = "CEO"
    RISK = "RISK"
    QA = "QA"
    OWNER = "OWNER"


class UnauthorizedDeciderError(Exception):
    """호출자 부서가 이 required_role의 결정을 내릴 권한이 없다 (불변식 2)."""


class OwnerApprovalNotSupportedError(Exception):
    """required_role=OWNER의 소유 부서 검증 경로가 아직 없다 (불변식 3)."""


class ApprovalExpiredError(Exception):
    """expires_at이 지난 승인에 결정을 시도했다 (불변식 1)."""


class AlreadyDecidedError(Exception):
    """PENDING이 아닌 승인에 다시 결정을 시도했다."""


# required_role별로 결정을 내릴 수 있는 부서(Hermes Profile 이름). 여기에 없는 조합은 전부
# 거절이다. required_role은 governance.approvals.required_role의 DDL 값이라 그대로 두고,
# 부서 식별자만 Profile 이름을 쓴다 - 둘은 다른 축이다(역할 vs 조직).
_ROLE_DECIDERS: dict[RequiredRole, frozenset[str]] = {
    RequiredRole.CEO: frozenset({"CEO-AGENT"}),
    RequiredRole.RISK: frozenset({"RISK-MANAGEMENT"}),
    RequiredRole.QA: frozenset({"QA-DEPARTMENT"}),
    # OWNER는 의도적으로 비어 있다 - 아래 decide()가 먼저 fail-closed로 막는다.
    RequiredRole.OWNER: frozenset(),
}

# decide()로 기록할 수 있는 결정. EXPIRED는 시간이 만드는 상태이고 REVOKED는 revoke()의
# 몫이라 여기서 직접 쓰지 못한다.
_DECIDABLE = (ApprovalDecision.APPROVED, ApprovalDecision.REJECTED)


def normalize_department(department: str) -> str:
    """부서 코드를 대문자 하이픈 표기로 정규화한다 (권한 판정 전 전처리)."""
    return department.strip().upper().replace("_", "-")


@dataclass(frozen=True)
class ApprovalRecord:
    """governance.approvals 한 행. 스펙 2.2 request_approval Response의 상위집합."""

    approval_id: str
    fund_id: str
    object_type: ObjectType
    object_id: str
    required_role: RequiredRole
    decision: ApprovalDecision
    created_at: datetime
    reason: str | None = None
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    # 둘 다 DB에서 uuid 타입이다(2026-08-03 실측 - 문서에 없던 제약). actor_user_id만
    # governance.user_profiles FK가 있고 actor_agent_id는 FK가 없다 - 의미상
    # workforce.agent_profiles.agent_id지만 CEO Agent는 아직 그 Roster에 미등록이라
    # (config.yaml not_started "타 부서 Agent 등록") DB가 강제하지 않는다.
    actor_user_id: str | None = None
    actor_agent_id: str | None = None
    conditions: dict | None = None

    def is_expired(self, at: datetime) -> bool:
        """expires_at이 없으면 만료되지 않는다(DDL도 nullable)."""
        return self.expires_at is not None and at >= self.expires_at


def request_approval(
    *,
    approval_id: str,
    fund_id: str,
    object_type: ObjectType,
    object_id: str,
    required_role: RequiredRole,
    created_at: datetime,
    reason: str | None = None,
    expires_at: datetime | None = None,
    conditions: dict | None = None,
) -> ApprovalRecord:
    """PENDING 승인 요청을 만든다. 요청 생성 자체에는 부서 제한이 없다 (불변식 2).

    누가 요청했는지는 governance.approvals에 컬럼이 없어 저장하지 않는다 -
    actor_user_id/actor_agent_id는 '결정한 주체'를 담는 칸이다.
    """
    if expires_at is not None and expires_at <= created_at:
        raise ValueError("expires_at은 created_at 이후여야 한다")
    return ApprovalRecord(
        approval_id=approval_id, fund_id=fund_id, object_type=object_type,
        object_id=object_id, required_role=required_role,
        decision=ApprovalDecision.PENDING, created_at=created_at, reason=reason,
        expires_at=expires_at, conditions=conditions or {},
    )


def assert_can_decide(required_role: RequiredRole, actor_department: str) -> None:
    """불변식 2·3 — 이 부서가 이 역할의 결정을 내릴 수 있는지 검증한다."""
    if required_role is RequiredRole.OWNER:
        raise OwnerApprovalNotSupportedError(
            "required_role=OWNER의 결정은 아직 지원하지 않는다 - object_type/object_id의 "
            "소유 부서를 governance.approvals만으로 검증할 수 없어 fail-closed로 막는다"
        )
    allowed = _ROLE_DECIDERS[required_role]
    if normalize_department(actor_department) not in allowed:
        raise UnauthorizedDeciderError(
            f"부서 {actor_department!r}는 required_role={required_role.value} 승인을 "
            f"결정할 수 없다 (허용: {sorted(allowed)})"
        )


def decide(
    approval: ApprovalRecord,
    *,
    decision: ApprovalDecision,
    actor_department: str,
    at: datetime,
    actor_agent_id: str | None = None,
    actor_user_id: str | None = None,
    conditions: dict | None = None,
    reason: str | None = None,
) -> ApprovalRecord:
    """승인 결정을 기록한다. 어떤 실패 경로도 APPROVED로 떨어지지 않는다 (불변식 1).

    actor_user_id는 '사람이 찍은 승인'일 때만 채운다. governance.user_profiles가 비어 있는
    동안에는 None으로 두고, 절대 플레이스홀더 회원으로 조용히 채우지 않는다 - 그러면 감사
    기록에 '사람이 승인했다'고 남는데 실제로는 아무도 승인하지 않은 상태가 된다.

    결정 주체 부서는 conditions["_decider"]에 함께 기록한다. governance.approvals에는
    부서 컬럼이 아예 없어서(actor_user_id/actor_agent_id 둘뿐) 그냥 두면 "어느 부서가
    결정했는지"가 어디에도 남지 않는다. 게다가 actor_agent_id는 workforce.agent_profiles
    FK인데 Agent Roster 등재를 Prototype 이후로 미뤘으므로(2026-08-04 팀 결정) 대부분의
    결정에서 그 칸이 비게 된다 - 그 상태로 부서까지 잃으면 감사 추적이 통째로 사라진다.
    **제거 조건**: approvals에 부서 컬럼이 생기거나 Roster 등재가 끝나 actor_agent_id로
    결정 주체를 특정할 수 있게 되면 이 _decider 기록을 그쪽으로 옮긴다.
    """
    if decision not in _DECIDABLE:
        raise ValueError(
            f"decide()로 기록할 수 있는 결정은 {[d.value for d in _DECIDABLE]}뿐이다 "
            f"(요청: {decision.value}). EXPIRED는 시간이, REVOKED는 revoke()가 만든다"
        )

    assert_can_decide(approval.required_role, actor_department)

    if approval.decision is not ApprovalDecision.PENDING:
        raise AlreadyDecidedError(
            f"이미 {approval.decision.value}로 결정된 승인이다 (approval_id={approval.approval_id})"
        )
    if approval.is_expired(at):
        raise ApprovalExpiredError(
            f"expires_at({approval.expires_at.isoformat()})이 지난 승인이다 - 결정할 수 없다 "
            f"(approval_id={approval.approval_id})"
        )

    merged_conditions = dict(conditions if conditions is not None else (approval.conditions or {}))
    merged_conditions["_decider"] = {"department": normalize_department(actor_department)}

    return replace(
        approval, decision=decision, decided_at=at,
        actor_agent_id=actor_agent_id, actor_user_id=actor_user_id,
        conditions=merged_conditions,
        reason=reason if reason is not None else approval.reason,
    )


def revoke(
    approval: ApprovalRecord,
    *,
    actor_department: str,
    at: datetime,
    reason: str,
    actor_agent_id: str | None = None,
    actor_user_id: str | None = None,
) -> ApprovalRecord:
    """이미 내준 승인을 철회한다. APPROVED만 REVOKED로 갈 수 있다."""
    if not reason.strip():
        raise ValueError("철회에는 사유가 필요하다")
    assert_can_decide(approval.required_role, actor_department)
    if approval.decision is not ApprovalDecision.APPROVED:
        raise AlreadyDecidedError(
            f"APPROVED만 철회할 수 있다 (현재: {approval.decision.value})"
        )
    revoke_conditions = dict(approval.conditions or {})
    revoke_conditions["_decider"] = {"department": normalize_department(actor_department)}
    return replace(
        approval, decision=ApprovalDecision.REVOKED, decided_at=at, reason=reason,
        actor_agent_id=actor_agent_id, actor_user_id=actor_user_id,
        conditions=revoke_conditions,
    )


def expire(approval: ApprovalRecord, at: datetime) -> ApprovalRecord | None:
    """만료 처리(Sweep용 순수 함수). 대상이 아니면 None을 준다.

    PENDING이고 expires_at이 지난 건만 EXPIRED가 된다 - 이미 결정된 건은 건드리지 않는다.
    """
    if approval.decision is not ApprovalDecision.PENDING:
        return None
    if not approval.is_expired(at):
        return None
    return replace(approval, decision=ApprovalDecision.EXPIRED, decided_at=at)


class ApprovalRepository:
    """조회·저장 인터페이스. 실제 구현은 governance.approvals에 반영한다."""

    def save(self, approval: ApprovalRecord) -> None:
        raise NotImplementedError

    def get(self, approval_id: str) -> ApprovalRecord | None:
        raise NotImplementedError

    def find(
        self, object_type: ObjectType, object_id: str, required_role: RequiredRole
    ) -> ApprovalRecord | None:
        """unique(object_type, object_id, required_role) 기준 조회 (불변식 4)."""
        raise NotImplementedError

    def list_by_object(self, object_type: ObjectType, object_id: str) -> list[ApprovalRecord]:
        raise NotImplementedError


class InMemoryApprovalRepository(ApprovalRepository):
    def __init__(self) -> None:
        self._by_id: dict[str, ApprovalRecord] = {}

    def save(self, approval: ApprovalRecord) -> None:
        existing = self.find(approval.object_type, approval.object_id, approval.required_role)
        if existing is not None and existing.approval_id != approval.approval_id:
            raise ValueError(
                "unique(object_type, object_id, required_role) 위반 - 같은 대상·역할의 승인이 이미 있다"
            )
        self._by_id[approval.approval_id] = approval

    def get(self, approval_id: str) -> ApprovalRecord | None:
        return self._by_id.get(approval_id)

    def find(
        self, object_type: ObjectType, object_id: str, required_role: RequiredRole
    ) -> ApprovalRecord | None:
        for a in self._by_id.values():
            if (a.object_type, a.object_id, a.required_role) == (object_type, object_id, required_role):
                return a
        return None

    def list_by_object(self, object_type: ObjectType, object_id: str) -> list[ApprovalRecord]:
        return [
            a for a in self._by_id.values()
            if (a.object_type, a.object_id) == (object_type, object_id)
        ]


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/approval/approval.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta, timezone

    t0 = datetime(2026, 8, 3, tzinfo=timezone.utc)
    t_late = t0 + timedelta(days=2)
    # 실제 accounting.funds 행과 uuid 형태를 맞춘다 - DB 계층은 둘 다 uuid를 요구한다.
    fund = "b13f5cd1-5df0-4025-92cf-9be03b1a0296"
    ceo_agent_id = "3f9d2c41-0000-4000-8000-000000000001"

    def _pending(role: RequiredRole, *, expires: datetime | None = None) -> ApprovalRecord:
        return request_approval(
            approval_id=f"ap-{role.value}", fund_id=fund,
            object_type=ObjectType.AGENT_PROFILE_VERSION, object_id="pv-1",
            required_role=role, created_at=t0, reason="HR-02 Profile 활성화",
            expires_at=expires,
        )

    # 1) 요청 생성 -> PENDING.
    ceo_req = _pending(RequiredRole.CEO)
    assert ceo_req.decision is ApprovalDecision.PENDING
    assert ceo_req.conditions == {}

    # 2) CEO Office가 CEO 승인을 결정 -> APPROVED.
    approved = decide(
        ceo_req, decision=ApprovalDecision.APPROVED, actor_department="ceo-agent",
        at=t0 + timedelta(hours=1), actor_agent_id=ceo_agent_id,
    )
    assert approved.decision is ApprovalDecision.APPROVED
    assert approved.actor_agent_id == ceo_agent_id
    assert approved.actor_user_id is None  # 사람 승인 아님 - 조용히 채우지 않는다
    # 결정 부서는 approvals에 컬럼이 없어 conditions._decider에 남는다.
    assert approved.conditions["_decider"] == {"department": "CEO-AGENT"}

    # 2b) Agent Roster 미등재 상태(actor_agent_id=None)에서도 결정 부서는 남아야 한다.
    no_agent = decide(
        _pending(RequiredRole.CEO), decision=ApprovalDecision.APPROVED,
        actor_department="ceo_agent", at=t0,  # `_` 표기도 정규화가 흡수한다
    )
    assert no_agent.actor_agent_id is None and no_agent.actor_user_id is None
    assert no_agent.conditions["_decider"] == {"department": "CEO-AGENT"}

    # 3) 핵심 권한 분리 - CEO가 RISK 승인을 결정하려 하면 거절 (불변식 2).
    risk_req = _pending(RequiredRole.RISK)
    try:
        decide(risk_req, decision=ApprovalDecision.APPROVED,
               actor_department="ceo-agent", at=t0)
        raise AssertionError("ceo-agent가 RISK 승인을 결정함")
    except UnauthorizedDeciderError:
        pass

    # 4) 리스크본부 본인은 결정할 수 있다. 대소문자·`_` 차이는 정규화가 흡수한다.
    for dept in ("risk-management", "RISK-MANAGEMENT", "risk_management"):
        ok = decide(_pending(RequiredRole.RISK), decision=ApprovalDecision.REJECTED,
                    actor_department=dept, at=t0, reason="한도 초과")
        assert ok.decision is ApprovalDecision.REJECTED

    # 4b) 폐기된 대문자 축약 표기(`RISK`)는 이제 받지 않는다 - Profile 이름만 인정한다.
    try:
        decide(_pending(RequiredRole.RISK), decision=ApprovalDecision.APPROVED,
               actor_department="RISK", at=t0)
        raise AssertionError("폐기된 'RISK' 표기가 통과함")
    except UnauthorizedDeciderError:
        pass

    # 5) QA도 같은 방식 - CEO는 QA 승인도 못 찍는다.
    try:
        decide(_pending(RequiredRole.QA), decision=ApprovalDecision.APPROVED,
               actor_department="ceo-agent", at=t0)
        raise AssertionError("ceo-agent가 QA 승인을 결정함")
    except UnauthorizedDeciderError:
        pass
    assert decide(_pending(RequiredRole.QA), decision=ApprovalDecision.APPROVED,
                  actor_department="qa-department", at=t0).decision is ApprovalDecision.APPROVED

    # 6) OWNER는 fail-closed (불변식 3).
    try:
        decide(_pending(RequiredRole.OWNER), decision=ApprovalDecision.APPROVED,
               actor_department="ceo-agent", at=t0)
        raise AssertionError("OWNER 승인이 검증 없이 통과함")
    except OwnerApprovalNotSupportedError:
        pass

    # 7) 만료된 승인은 결정 불가 - 자동 승인으로 떨어지지 않는다 (불변식 1).
    expiring = _pending(RequiredRole.CEO, expires=t0 + timedelta(hours=1))
    try:
        decide(expiring, decision=ApprovalDecision.APPROVED,
               actor_department="ceo-agent", at=t_late)
        raise AssertionError("만료된 승인이 결정됨")
    except ApprovalExpiredError:
        pass

    # 8) 이미 결정된 승인 재결정 불가.
    try:
        decide(approved, decision=ApprovalDecision.REJECTED,
               actor_department="ceo-agent", at=t_late)
        raise AssertionError("이미 결정된 승인이 다시 결정됨")
    except AlreadyDecidedError:
        pass

    # 9) decide()로 EXPIRED/REVOKED/PENDING을 직접 쓸 수 없다.
    for bad in (ApprovalDecision.PENDING, ApprovalDecision.EXPIRED, ApprovalDecision.REVOKED):
        try:
            decide(_pending(RequiredRole.CEO), decision=bad,
                   actor_department="ceo-agent", at=t0)
            raise AssertionError(f"decide()가 {bad.value}를 받아들임")
        except ValueError:
            pass

    # 10) 철회 - APPROVED만 가능하고 사유가 필요하다.
    revoked = revoke(approved, actor_department="ceo-agent", at=t_late, reason="Mandate 변경")
    assert revoked.decision is ApprovalDecision.REVOKED
    try:
        revoke(revoked, actor_department="ceo-agent", at=t_late, reason="다시")
        raise AssertionError("REVOKED를 다시 철회함")
    except AlreadyDecidedError:
        pass
    try:
        revoke(approved, actor_department="ceo-agent", at=t_late, reason="   ")
        raise AssertionError("사유 없이 철회됨")
    except ValueError:
        pass

    # 11) 만료 Sweep - PENDING+기한 초과만 EXPIRED, 나머지는 None.
    assert expire(expiring, t_late).decision is ApprovalDecision.EXPIRED
    assert expire(expiring, t0) is None            # 아직 기한 안 지남
    assert expire(approved, t_late) is None        # 이미 결정됨
    assert expire(_pending(RequiredRole.CEO), t_late) is None  # expires_at 없음

    # 12) expires_at <= created_at 거부.
    try:
        request_approval(
            approval_id="ap-x", fund_id=fund, object_type=ObjectType.MANDATE_VERSION,
            object_id="mv-1", required_role=RequiredRole.CEO, created_at=t0, expires_at=t0,
        )
        raise AssertionError("expires_at <= created_at이 통과함")
    except ValueError:
        pass

    # 13) Repository - unique(object_type, object_id, required_role) 위반 차단 (불변식 4).
    repo = InMemoryApprovalRepository()
    repo.save(ceo_req)
    assert repo.get("ap-CEO") is not None
    assert repo.find(ObjectType.AGENT_PROFILE_VERSION, "pv-1", RequiredRole.CEO) is not None
    assert repo.find(ObjectType.AGENT_PROFILE_VERSION, "pv-1", RequiredRole.RISK) is None
    repo.save(approved)  # 같은 approval_id 갱신은 허용
    assert repo.get("ap-CEO").decision is ApprovalDecision.APPROVED
    try:
        repo.save(replace(ceo_req, approval_id="ap-other"))
        raise AssertionError("unique 제약 위반이 통과함")
    except ValueError:
        pass
    repo.save(_pending(RequiredRole.RISK))
    assert len(repo.list_by_object(ObjectType.AGENT_PROFILE_VERSION, "pv-1")) == 2

    print("ok - GOV-02 승인 도메인 계약 15개 시나리오 통과")
