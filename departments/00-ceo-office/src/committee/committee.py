#!/usr/bin/env python3
"""Y2 위원회 — Vote·Quorum·SoD 도메인 계약.

소유: 영주 (CEO Office)
근거: docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md 5.4(Governance)
      (open/close_session ✅, submit_vote ✅ — "Quorum과 SoD 판정은 결정론적
      Service가 한다. API는 투표를 기록만 하고 정족수를 임의로 계산해 승인
      처리하지 않는다"),
      supabase/migrations/20260729000200_governance_workforce.sql
      (governance.committee_sessions/committee_votes/committee_decisions —
      status/decision 허용 값이 DDL에 이미 있다, GOV-02의 cases.status와
      달리 여기는 지어낼 값이 없다),
      docs/HEDGE_FUND_MASTER_PLAN.md 5.2·5.4·18.2절(투자위원회/전략기획위원회
      구성, SoD 원칙 "동일한 에이전트가 전략 생성과 최종 검증을 동시에
      담당하지 않도록")

여기엔 LLM이 없다. Quorum 판정과 SoD 검증은 전부 결정론적 코드다.

불변식:
  1. **투표는 부서 단위 1표다.** DDL의 `unique(session_id, department,
     voter_agent_id)`는 voter_agent_id가 NULL이면 Postgres가 서로 다른
     행으로 취급해 중복을 못 막는다(NULL은 unique 제약에서 서로 다르다고
     본다) - 그래서 애플리케이션이 같은 (session_id, department) 재투표를
     직접 막는다(불변식 4의 find_vote_by_department가 그 경로).
  2. **Veto 부서의 REJECT는 다른 표와 무관하게 결과를 REJECT로 만든다.**
     quorum_policy가 지정한 veto_departments 중 하나라도 REJECT를 던지면
     승인 문턱을 채웠어도 뒤집는다 - CLAUDE.md "Risk/QA의 거부를 CEO가
     우회·해제할 수 없다"를 위원회 표결에도 그대로 적용한다.
  3. **정족수(quorum)를 못 채우면 승인으로 떨어지지 않는다.** required_
     departments 전원이 투표하기 전에 세션을 닫으면 결과는 DEFER다(승인도
     거절도 아니다) - 개발 원칙 9 "확인 불가 시 확대가 아니라 차단 방향".
  4. **SoD — Case를 올린 부서는 그 Case의 위원회에서 투표할 수 없다.**
     MASTER_PLAN 18.2 "동일한 에이전트가 전략 생성과 최종 검증을 동시에
     담당하지 않도록"를 일반화한 것이다 - session이 case_id를 가지면 그
     case의 owner_department는 투표 부서에서 자동 제외된다.
  5. **quorum_policy는 committee_type별로 이 코드가 강제하지 않는다.**
     투자위원회(CEO·리서치·트레이딩·리스크·회계)와 전략기획위원회(리서치·
     퀀트·리스크·QA)는 구성원이 다르다(MASTER_PLAN 5.2/5.4/18.2, QA는
     투자위원회 구성원이 아니다 - 2108행). 그래서 required_departments/
     veto_departments/approval_threshold는 세션을 여는 쪽이 매번 지정하는
     jsonb 값이고, 이 모듈은 그 값을 그대로 평가만 한다.

이 모듈이 제안하는 것은 quorum_policy의 **형태**뿐이다(DDL이 jsonb로만
남겨둠). GOV-02의 cases.status처럼 새 DB 제약을 만드는 게 아니라 순수
Python 계약이라 마이그레이션이 필요 없다 - 리뷰에서 다른 형태가 낫다고
판단되면 QuorumPolicy 데이터클래스만 바꾸면 된다.

자체 점검: python departments/00-ceo-office/src/committee/committee.py
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum


class SessionStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    OPEN = "OPEN"
    DECIDED = "DECIDED"
    CANCELLED = "CANCELLED"


class VoteDecision(str, Enum):
    """committee_votes.decision — 개별 투표 값 (DDL check)."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    ABSTAIN = "ABSTAIN"
    CONDITIONAL = "CONDITIONAL"


class CommitteeDecision(str, Enum):
    """committee_decisions.decision — 세션 종료 시 나오는 최종 값 (DDL check).

    VoteDecision과 이름이 겹치지만 다른 어휘다 - ABSTAIN은 투표엔 있어도
    최종 결정엔 없고(기권은 정족수에서 빠질 뿐 결과를 만들지 않는다),
    DEFER는 최종 결정에만 있다(정족수 미달 - 불변식 3).
    """

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    DEFER = "DEFER"
    CONDITIONAL = "CONDITIONAL"


class IllegalSessionTransition(Exception):
    """OPEN이 아닌 세션에 투표하거나, SCHEDULED/OPEN이 아닌 세션을 닫으려 했다."""


class DuplicateVoteError(Exception):
    """같은 부서가 같은 세션에 이미 투표했다 (불변식 1)."""


class SelfReviewError(Exception):
    """Case를 올린 부서가 그 Case의 위원회에서 투표하려 했다 (불변식 4, SoD)."""


class InvalidQuorumPolicyError(Exception):
    """quorum_policy 형태가 잘못됐다 - veto_departments가 required_departments의
    부분집합이 아니거나 approval_threshold가 범위를 벗어났다."""


@dataclass(frozen=True)
class QuorumPolicy:
    """committee_sessions.quorum_policy(jsonb)의 내부 계약 - 이 모듈이 제안하는 형태.

    실제 구성 예시(MASTER_PLAN 5.2/18.2, 강제하지 않고 참고만):
      투자위원회 — required=[ceo-agent, research-department, trading-department,
        risk-management, accounting-portfolio-department], veto=[risk-management],
        approval_threshold=3
      전략기획위원회 — required=[research-department, quant-backtest-department,
        risk-management, qa-department], veto=[risk-management, qa-department],
        approval_threshold=3
    """

    required_departments: tuple[str, ...]
    veto_departments: tuple[str, ...] = ()
    approval_threshold: int = 1

    def __post_init__(self) -> None:
        if not self.required_departments:
            raise InvalidQuorumPolicyError("required_departments는 최소 1개 필요하다")
        if len(set(self.required_departments)) != len(self.required_departments):
            raise InvalidQuorumPolicyError("required_departments에 중복 부서가 있다")
        if not set(self.veto_departments).issubset(self.required_departments):
            raise InvalidQuorumPolicyError(
                "veto_departments는 required_departments의 부분집합이어야 한다 - "
                f"required에 없는 veto: {set(self.veto_departments) - set(self.required_departments)}"
            )
        if not 1 <= self.approval_threshold <= len(self.required_departments):
            raise InvalidQuorumPolicyError(
                f"approval_threshold({self.approval_threshold})는 1~"
                f"{len(self.required_departments)} 범위여야 한다"
            )

    def to_jsonb(self) -> dict:
        return {
            "required_departments": list(self.required_departments),
            "veto_departments": list(self.veto_departments),
            "approval_threshold": self.approval_threshold,
        }

    @classmethod
    def from_jsonb(cls, data: dict) -> QuorumPolicy:
        return cls(
            required_departments=tuple(data["required_departments"]),
            veto_departments=tuple(data.get("veto_departments", ())),
            approval_threshold=int(data.get("approval_threshold", 1)),
        )


@dataclass(frozen=True)
class CommitteeSession:
    """governance.committee_sessions 한 행."""

    session_id: str
    fund_id: str
    committee_type: str
    quorum_policy: QuorumPolicy
    status: SessionStatus
    opened_at: datetime
    trace_id: str
    case_id: str | None = None
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.committee_type.strip():
            raise ValueError("committee_type은 비울 수 없다 (DDL not null)")
        if self.closed_at is not None and self.closed_at < self.opened_at:
            raise ValueError("closed_at은 opened_at 이후여야 한다 (DDL check)")


@dataclass(frozen=True)
class Vote:
    """governance.committee_votes 한 행."""

    vote_id: str
    session_id: str
    department: str
    decision: VoteDecision
    voted_at: datetime
    voter_agent_id: str | None = None
    conditions: dict = field(default_factory=dict)
    artifact_ids: tuple[str, ...] = ()
    rationale: str | None = None


@dataclass(frozen=True)
class CommitteeDecisionRecord:
    """governance.committee_decisions 한 행 - session_id당 유일(unique)."""

    committee_decision_id: str
    session_id: str
    decision: CommitteeDecision
    scope: dict
    decided_at: datetime
    conditions: dict = field(default_factory=dict)
    valid_until: datetime | None = None
    dissent: tuple[dict, ...] = ()
    approvals: tuple[dict, ...] = ()


def open_session(
    *,
    session_id: str,
    fund_id: str,
    committee_type: str,
    quorum_policy: QuorumPolicy,
    opened_at: datetime,
    trace_id: str,
    case_id: str | None = None,
) -> CommitteeSession:
    """세션을 OPEN으로 연다. quorum_policy 형태 검증은 QuorumPolicy.__post_init__이 한다."""
    return CommitteeSession(
        session_id=session_id, fund_id=fund_id, case_id=case_id,
        committee_type=committee_type, quorum_policy=quorum_policy,
        status=SessionStatus.OPEN, opened_at=opened_at, trace_id=trace_id,
    )


def cast_vote(
    session: CommitteeSession,
    existing_votes: list[Vote],
    *,
    vote_id: str,
    department: str,
    decision: VoteDecision,
    voted_at: datetime,
    case_owner_department: str | None = None,
    voter_agent_id: str | None = None,
    conditions: dict | None = None,
    artifact_ids: tuple[str, ...] = (),
    rationale: str | None = None,
) -> Vote:
    """투표 한 표를 기록한다. 세션이 OPEN이어야 하고, 부서당 1표, SoD를 지킨다.

    case_owner_department는 session.case_id가 가리키는 governance.cases의
    owner_department다 - 호출자(Repository 경유)가 조회해 넘긴다. 이 함수는
    DB를 모르므로 순수 인자로 받는다 (불변식 4).
    """
    if session.status is not SessionStatus.OPEN:
        raise IllegalSessionTransition(
            f"세션이 {session.status.value}이다 - OPEN 세션에만 투표할 수 있다"
        )
    if find_vote_by_department(existing_votes, department) is not None:
        raise DuplicateVoteError(
            f"부서 {department!r}는 이미 이 세션에 투표했다 (session_id={session.session_id})"
        )
    if case_owner_department is not None and department == case_owner_department:
        raise SelfReviewError(
            f"부서 {department!r}는 이 Case(owner_department={case_owner_department!r})를 "
            "직접 올린 부서라 같은 위원회에서 투표할 수 없다 (SoD, MASTER_PLAN 18.2)"
        )
    return Vote(
        vote_id=vote_id, session_id=session.session_id, department=department,
        decision=decision, voted_at=voted_at, voter_agent_id=voter_agent_id,
        conditions=conditions or {}, artifact_ids=artifact_ids, rationale=rationale,
    )


def find_vote_by_department(votes: list[Vote], department: str) -> Vote | None:
    for v in votes:
        if v.department == department:
            return v
    return None


@dataclass(frozen=True)
class QuorumResult:
    """evaluate_quorum()의 결과 - close_session()이 그대로 committee_decisions에 옮긴다."""

    met: bool
    decision: CommitteeDecision
    approvals: tuple[dict, ...]
    dissent: tuple[dict, ...]
    missing_departments: tuple[str, ...]


def evaluate_quorum(policy: QuorumPolicy, votes: list[Vote]) -> QuorumResult:
    """Quorum·Veto 판정 - 순수 함수, DB도 LLM도 없다.

    순서(불변식 2·3):
      1. required_departments 전원이 투표하지 않았으면 -> DEFER (미달).
      2. veto_departments 중 하나라도 REJECT면 -> REJECT (전원 투표 여부와 무관하게
         이미 1번을 통과한 뒤라 정족수는 이미 찬 상태).
      3. APPROVE+CONDITIONAL 합이 approval_threshold 이상이면 -> CONDITIONAL
         (하나라도 CONDITIONAL 있으면) 또는 APPROVE. **veto 아닌 부서의 REJECT는
         이 판정을 막지 않는다** - approval_threshold가 "전원 찬성"이 아니라
         "이 수만큼 찬성하면 통과"라는 뜻이기 때문이다(threshold < len(required)일
         때 threshold 자체가 무의미해지지 않도록). REJECT 표는 dissent에만 남는다.
      4. 그 외(문턱 미달) -> REJECT.
    ABSTAIN 표는 required 정족수 충족 여부(투표 여부)에는 포함되지만 찬성 문턱
    계산에서는 찬성으로도 반대로도 안 세다 - 기권은 기권이다.
    """
    by_dept = {v.department: v for v in votes if v.department in policy.required_departments}
    missing = tuple(d for d in policy.required_departments if d not in by_dept)

    if missing:
        return QuorumResult(
            met=False, decision=CommitteeDecision.DEFER, approvals=(), dissent=(),
            missing_departments=missing,
        )

    def _entry(v: Vote) -> dict:
        return {
            "department": v.department, "decision": v.decision.value,
            "rationale": v.rationale, "conditions": v.conditions,
        }

    vetoed = [
        v for v in by_dept.values()
        if v.department in policy.veto_departments and v.decision is VoteDecision.REJECT
    ]
    if vetoed:
        approvals = tuple(_entry(v) for v in by_dept.values() if v.decision is VoteDecision.APPROVE)
        dissent = tuple(_entry(v) for v in by_dept.values() if v.decision is not VoteDecision.APPROVE)
        return QuorumResult(
            met=True, decision=CommitteeDecision.REJECT, approvals=approvals,
            dissent=dissent, missing_departments=(),
        )

    supporting = [v for v in by_dept.values() if v.decision in (VoteDecision.APPROVE, VoteDecision.CONDITIONAL)]
    approvals = tuple(_entry(v) for v in supporting)
    dissent = tuple(_entry(v) for v in by_dept.values() if v.decision is not VoteDecision.APPROVE)

    if len(supporting) >= policy.approval_threshold:
        decision = (
            CommitteeDecision.CONDITIONAL
            if any(v.decision is VoteDecision.CONDITIONAL for v in supporting)
            else CommitteeDecision.APPROVE
        )
        return QuorumResult(met=True, decision=decision, approvals=approvals, dissent=dissent,
                            missing_departments=())

    return QuorumResult(met=True, decision=CommitteeDecision.REJECT, approvals=approvals,
                        dissent=dissent, missing_departments=())


def close_session(
    session: CommitteeSession,
    votes: list[Vote],
    *,
    committee_decision_id: str,
    at: datetime,
    scope: dict | None = None,
    valid_until: datetime | None = None,
) -> tuple[CommitteeSession, CommitteeDecisionRecord]:
    """세션을 닫고 evaluate_quorum() 결과를 committee_decisions로 확정한다.

    DEFER로 닫힌 세션도 status는 DECIDED다 - "정족수 미달로 결론을 못 냈다"는
    것 자체가 하나의 결정이지, 세션이 아직 안 끝난 게 아니다(불변식 3).
    """
    if session.status is not SessionStatus.OPEN:
        raise IllegalSessionTransition(
            f"세션이 {session.status.value}이다 - OPEN 세션만 닫아 결정할 수 있다"
        )
    result = evaluate_quorum(session.quorum_policy, votes)
    updated = replace(session, status=SessionStatus.DECIDED, closed_at=at)
    decision = CommitteeDecisionRecord(
        committee_decision_id=committee_decision_id, session_id=session.session_id,
        decision=result.decision, scope=scope or {}, decided_at=at,
        valid_until=valid_until, dissent=result.dissent, approvals=result.approvals,
    )
    return updated, decision


def cancel_session(session: CommitteeSession, *, at: datetime) -> CommitteeSession:
    """SCHEDULED 또는 OPEN 세션을 CANCELLED로 닫는다. 이미 DECIDED/CANCELLED면 거부."""
    if session.status not in (SessionStatus.SCHEDULED, SessionStatus.OPEN):
        raise IllegalSessionTransition(
            f"세션이 {session.status.value}이다 - SCHEDULED/OPEN만 취소할 수 있다"
        )
    return replace(session, status=SessionStatus.CANCELLED, closed_at=at)


class CommitteeRepository:
    """조회·저장 인터페이스. 실제 구현은 governance.committee_*에 반영한다."""

    def save_session(self, session: CommitteeSession) -> None:
        raise NotImplementedError

    def get_session(self, session_id: str) -> CommitteeSession | None:
        raise NotImplementedError

    def save_vote(self, vote: Vote) -> None:
        raise NotImplementedError

    def list_votes(self, session_id: str) -> list[Vote]:
        raise NotImplementedError

    def save_decision(self, decision: CommitteeDecisionRecord) -> None:
        raise NotImplementedError

    def get_case_owner_department(self, case_id: str) -> str | None:
        """SoD 검증용 - session.case_id -> governance.cases.owner_department 조회."""
        raise NotImplementedError


class InMemoryCommitteeRepository(CommitteeRepository):
    def __init__(self) -> None:
        self._sessions: dict[str, CommitteeSession] = {}
        self._votes: dict[str, list[Vote]] = {}
        self._decisions: dict[str, CommitteeDecisionRecord] = {}
        self._case_owners: dict[str, str] = {}

    def seed_case_owner(self, case_id: str, owner_department: str) -> None:
        """테스트·개발용 seed. 실 구현에서는 governance.cases를 조회한다."""
        self._case_owners[case_id] = owner_department

    def save_session(self, session: CommitteeSession) -> None:
        self._sessions[session.session_id] = session

    def get_session(self, session_id: str) -> CommitteeSession | None:
        return self._sessions.get(session_id)

    def save_vote(self, vote: Vote) -> None:
        self._votes.setdefault(vote.session_id, []).append(vote)

    def list_votes(self, session_id: str) -> list[Vote]:
        return list(self._votes.get(session_id, []))

    def save_decision(self, decision: CommitteeDecisionRecord) -> None:
        if decision.session_id in self._decisions:
            raise ValueError(f"session_id={decision.session_id}는 이미 결정이 있다 (unique)")
        self._decisions[decision.session_id] = decision

    def get_case_owner_department(self, case_id: str) -> str | None:
        return self._case_owners.get(case_id)


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/committee/committee.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta, timezone
    import uuid

    t0 = datetime(2026, 8, 4, tzinfo=timezone.utc)
    fund = "b13f5cd1-5df0-4025-92cf-9be03b1a0296"

    investment_policy = QuorumPolicy(
        required_departments=("ceo-agent", "research-department", "trading-department",
                              "risk-management", "accounting-portfolio-department"),
        veto_departments=("risk-management",), approval_threshold=3,
    )

    # 1) QuorumPolicy 검증 - veto가 required의 부분집합이어야 한다.
    try:
        QuorumPolicy(required_departments=("a", "b"), veto_departments=("c",))
        raise AssertionError("required에 없는 veto가 통과함")
    except InvalidQuorumPolicyError:
        pass
    try:
        QuorumPolicy(required_departments=("a", "b"), approval_threshold=3)
        raise AssertionError("approval_threshold 범위 초과가 통과함")
    except InvalidQuorumPolicyError:
        pass
    try:
        QuorumPolicy(required_departments=())
        raise AssertionError("빈 required_departments가 통과함")
    except InvalidQuorumPolicyError:
        pass

    # 2) 세션 열기.
    session = open_session(
        session_id="s1", fund_id=fund, committee_type="INVESTMENT",
        quorum_policy=investment_policy, opened_at=t0, trace_id=str(uuid.uuid4()),
        case_id="case-1",
    )
    assert session.status is SessionStatus.OPEN

    # 3) SoD - Case를 올린 부서(trading-department)는 투표 불가.
    try:
        cast_vote(session, [], vote_id="v0", department="trading-department",
                 decision=VoteDecision.APPROVE, voted_at=t0,
                 case_owner_department="trading-department")
        raise AssertionError("Case 작성 부서의 자기 투표가 통과함")
    except SelfReviewError:
        pass

    # 4) 정상 투표 축적 - 부서당 1표, 정족수 미달이면 DEFER.
    votes: list[Vote] = []
    votes.append(cast_vote(session, votes, vote_id="v1", department="ceo-agent",
                           decision=VoteDecision.APPROVE, voted_at=t0))
    votes.append(cast_vote(session, votes, vote_id="v2", department="research-department",
                           decision=VoteDecision.APPROVE, voted_at=t0))
    r_defer = evaluate_quorum(investment_policy, votes)
    assert r_defer.met is False and r_defer.decision is CommitteeDecision.DEFER
    assert set(r_defer.missing_departments) == {"trading-department", "risk-management",
                                                  "accounting-portfolio-department"}

    # 5) 중복 투표 차단 (불변식 1).
    try:
        cast_vote(session, votes, vote_id="v-dup", department="ceo-agent",
                 decision=VoteDecision.APPROVE, voted_at=t0)
        raise AssertionError("중복 투표가 통과함")
    except DuplicateVoteError:
        pass

    # 6) 정족수 채우기 - 3표 찬성(threshold=3) -> APPROVE.
    votes.append(cast_vote(session, votes, vote_id="v3", department="trading-department",
                           decision=VoteDecision.APPROVE, voted_at=t0))
    votes.append(cast_vote(session, votes, vote_id="v4", department="risk-management",
                           decision=VoteDecision.APPROVE, voted_at=t0))
    votes.append(cast_vote(session, votes, vote_id="v5", department="accounting-portfolio-department",
                           decision=VoteDecision.REJECT, voted_at=t0))
    r1 = evaluate_quorum(investment_policy, votes)
    assert r1.met is True and r1.decision is CommitteeDecision.APPROVE, r1
    assert len(r1.approvals) == 4 and len(r1.dissent) == 1

    # 7) Veto - risk-management가 REJECT면 나머지가 전부 APPROVE여도 REJECT (불변식 2).
    veto_votes = [replace(v, decision=VoteDecision.REJECT) if v.department == "risk-management" else v
                  for v in votes]
    r2 = evaluate_quorum(investment_policy, veto_votes)
    assert r2.decision is CommitteeDecision.REJECT, r2

    # 8) 문턱 미달 - 비veto 부서 반대로 찬성 2표뿐이면 REJECT(veto 없이도).
    low_policy = QuorumPolicy(required_departments=("a", "b", "c"), approval_threshold=3)
    low_votes = [
        Vote(vote_id="x1", session_id="s2", department="a", decision=VoteDecision.APPROVE, voted_at=t0),
        Vote(vote_id="x2", session_id="s2", department="b", decision=VoteDecision.APPROVE, voted_at=t0),
        Vote(vote_id="x3", session_id="s2", department="c", decision=VoteDecision.REJECT, voted_at=t0),
    ]
    r3 = evaluate_quorum(low_policy, low_votes)
    assert r3.decision is CommitteeDecision.REJECT and r3.met is True

    # 9) CONDITIONAL - 찬성 문턱을 CONDITIONAL로 채우면 최종도 CONDITIONAL.
    # ABSTAIN은 정족수(투표 여부)엔 들어가되 찬성 문턱엔 안 들어간다 - threshold=2로
    # 낮춰 a(APPROVE)+b(CONDITIONAL) 둘만으로 문턱을 채우는 경우를 검증한다.
    cond_policy = QuorumPolicy(required_departments=("a", "b", "c"), approval_threshold=2)
    cond_votes = [
        Vote(vote_id="y1", session_id="s3", department="a", decision=VoteDecision.APPROVE, voted_at=t0),
        Vote(vote_id="y2", session_id="s3", department="b", decision=VoteDecision.CONDITIONAL, voted_at=t0,
             conditions={"max_position": "0.05"}),
        Vote(vote_id="y3", session_id="s3", department="c", decision=VoteDecision.ABSTAIN, voted_at=t0),
    ]
    r4 = evaluate_quorum(cond_policy, cond_votes)
    assert r4.decision is CommitteeDecision.CONDITIONAL, r4

    # 10) 세션 닫기 - DECIDED 상태로 전이, 이미 닫힌 세션은 재종결 불가.
    closed, decision_row = close_session(
        session, votes, committee_decision_id="d1", at=t0 + timedelta(hours=1),
        scope={"case_id": "case-1"},
    )
    assert closed.status is SessionStatus.DECIDED and closed.closed_at is not None
    assert decision_row.decision is CommitteeDecision.APPROVE
    try:
        close_session(closed, votes, committee_decision_id="d2", at=t0)
        raise AssertionError("DECIDED 세션을 다시 닫음")
    except IllegalSessionTransition:
        pass

    # 11) DECIDED 세션에는 투표 불가.
    try:
        cast_vote(closed, votes, vote_id="v-late", department="qa-department",
                 decision=VoteDecision.APPROVE, voted_at=t0)
        raise AssertionError("DECIDED 세션에 투표가 통과함")
    except IllegalSessionTransition:
        pass

    # 12) 취소 - SCHEDULED/OPEN만 가능, DECIDED는 불가.
    open2 = open_session(session_id="s4", fund_id=fund, committee_type="STRATEGY_PLANNING",
                         quorum_policy=low_policy, opened_at=t0, trace_id=str(uuid.uuid4()))
    cancelled = cancel_session(open2, at=t0)
    assert cancelled.status is SessionStatus.CANCELLED
    try:
        cancel_session(closed, at=t0)
        raise AssertionError("DECIDED 세션 취소가 통과함")
    except IllegalSessionTransition:
        pass

    # 13) Repository - 세션/투표/결정 왕복, SoD 조회 경로.
    repo = InMemoryCommitteeRepository()
    repo.seed_case_owner("case-1", "trading-department")
    repo.save_session(session)
    for v in votes[:2]:
        repo.save_vote(v)
    assert repo.get_session("s1") is not None
    assert len(repo.list_votes("s1")) == 2
    assert repo.get_case_owner_department("case-1") == "trading-department"
    assert repo.get_case_owner_department("missing") is None
    repo.save_decision(decision_row)
    try:
        repo.save_decision(decision_row)
        raise AssertionError("session_id당 결정 unique 위반이 통과함")
    except ValueError:
        pass

    print("ok - Y2 위원회 도메인 계약 13개 시나리오 통과")
