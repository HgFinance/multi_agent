#!/usr/bin/env python3
"""F19: 개선 후보 생명주기 상태 머신 + 권한 분리(Separation of Duties) 게이트.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F19,
      docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md 6.5, 10.2(승인과 감사)

여기서 승인 판정은 결정론적 코드만 한다. LLM이 상태를 바꾸지 않는다.

불변식:
  1. 후보를 만든 author 는 자기 후보를 단독 승인할 수 없다 (자기승인 차단).
  2. 승인(APPROVED)에는 독립 승인자 + QA Eval 근거가 있어야 한다.
  3. 모든 전이는 같은 candidate_id 로 Append-only Event 에 기록된다 (같은 ID 재현).
  4. 허용되지 않은 상태 전이와 종료 상태 재전이는 막는다 (조용한 덮어쓰기 금지).
  5. 후보 작성자는 Promotion(DEPLOYED)이나 Rollback(ROLLED_BACK)을 단독 수행할 수 없다.

자체 점검: python departments/07-agent-workforce/improvements/workflow.py
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from candidate import (
    TERMINAL_STATUSES,
    CandidateStatus,
    ImprovementCandidate,
)

# 허용 전이표 (6.5 흐름).
ALLOWED_TRANSITIONS: dict[CandidateStatus, frozenset[CandidateStatus]] = {
    CandidateStatus.PROPOSED: frozenset(
        {CandidateStatus.EVALUATING, CandidateStatus.RETIRED}
    ),
    CandidateStatus.EVALUATING: frozenset(
        {CandidateStatus.SHADOW, CandidateStatus.HOLD, CandidateStatus.REJECTED, CandidateStatus.RETIRED}
    ),
    CandidateStatus.SHADOW: frozenset(
        {CandidateStatus.PENDING_APPROVAL, CandidateStatus.REJECTED, CandidateStatus.RETIRED}
    ),
    CandidateStatus.PENDING_APPROVAL: frozenset(
        {CandidateStatus.APPROVED, CandidateStatus.REJECTED}
    ),
    CandidateStatus.APPROVED: frozenset(
        {CandidateStatus.DEPLOYED, CandidateStatus.RETIRED}
    ),
    CandidateStatus.DEPLOYED: frozenset({CandidateStatus.OBSERVING}),
    CandidateStatus.OBSERVING: frozenset(
        {CandidateStatus.KEPT, CandidateStatus.ROLLED_BACK}
    ),
}


class IllegalTransition(Exception):
    """허용되지 않은 상태 전이."""


class SelfApprovalError(Exception):
    """후보 작성자가 자기 후보를 승인하려 함 (권한 분리 위반)."""


class MissingEvidenceError(Exception):
    """승인에 QA Eval 근거가 없음."""


@dataclass(frozen=True)
class CandidateEvent:
    """개선 후보 상태 전이 기록 (Append-only, audit)."""

    candidate_id: str
    sequence: int
    from_status: CandidateStatus
    to_status: CandidateStatus
    actor: str
    reason: str
    occurred_at: datetime
    qa_eval_run_id: str | None = None


@dataclass(frozen=True)
class Approval:
    """독립 승인 근거."""

    approver: str          # author 와 달라야 한다
    qa_eval_run_id: str    # audit.eval_runs 참조 (QA 근거)
    reason: str


class ImprovementRepository:
    """Event Ledger 조회·저장 인터페이스. 실제 구현은 workforce.improvement_candidate_events에
    반영한다(append-only - DB 트리거로도 강제된다). candidate 저장은 워크플로 밖에서
    api/app.py가 직접 호출한다(여기 인터페이스는 sequence/event만 다룬다)."""

    def next_sequence(self, candidate_id: str) -> int:
        raise NotImplementedError

    def append_event(self, event: CandidateEvent) -> None:
        raise NotImplementedError

    def events_for(self, candidate_id: str) -> list[CandidateEvent]:
        raise NotImplementedError


class InMemoryImprovementRepository(ImprovementRepository):
    def __init__(self) -> None:
        self._events: list[CandidateEvent] = []
        # candidate 저장은 ImprovementRepository 인터페이스 밖(PostgresImprovementRepository와
        # 대칭을 맞추려고 여기 추가) - api/app.py가 candidate CRUD에 쓴다.
        self._candidates: dict[str, ImprovementCandidate] = {}
        self._scorecards = []

    def next_sequence(self, candidate_id: str) -> int:
        return len(self.events_for(candidate_id)) + 1

    def append_event(self, event: CandidateEvent) -> None:
        self._events.append(event)

    def events_for(self, candidate_id: str) -> list[CandidateEvent]:
        return [e for e in self._events if e.candidate_id == candidate_id]

    def get_candidate(self, candidate_id: str) -> ImprovementCandidate | None:
        return self._candidates.get(candidate_id)

    def list_candidates(self) -> list[ImprovementCandidate]:
        return [
            self._candidates[candidate_id]
            for candidate_id in sorted(self._candidates)
        ]

    def save_candidate(self, candidate: ImprovementCandidate) -> None:
        self._candidates[candidate.candidate_id] = candidate

    def append_scorecard(self, scorecard) -> None:
        self._scorecards.append(scorecard)

    def scorecards_for(self, candidate_id: str):
        return [s for s in self._scorecards if s.candidate_id == candidate_id]


class ImprovementWorkflow:
    """개선 후보 상태 머신. Event Ledger 저장소는 주입받는다(기본 in-memory,
    api/app.py가 DATABASE_URL이 있으면 Postgres 구현을 주입한다)."""

    def __init__(self, repo: ImprovementRepository | None = None) -> None:
        self._repo = repo if repo is not None else InMemoryImprovementRepository()

    # --- 조회 ---

    def events_for(self, candidate_id: str) -> list[CandidateEvent]:
        return self._repo.events_for(candidate_id)

    def _next_sequence(self, candidate_id: str) -> int:
        return self._repo.next_sequence(candidate_id)

    # --- 전이 ---

    def transition(
        self,
        candidate: ImprovementCandidate,
        to_status: CandidateStatus,
        *,
        actor: str,
        reason: str,
        at: datetime,
        approval: Approval | None = None,
    ) -> ImprovementCandidate:
        frm = candidate.status

        if frm in TERMINAL_STATUSES:
            raise IllegalTransition(f"종료 상태에서 전이 불가: {frm.value}")

        allowed = ALLOWED_TRANSITIONS.get(frm, frozenset())
        if to_status not in allowed:
            raise IllegalTransition(
                f"{frm.value} -> {to_status.value} 는 허용되지 않는다 "
                f"(허용: {sorted(s.value for s in allowed)})"
            )

        qa_eval_run_id: str | None = None

        # 승인 게이트: 권한 분리 + QA 근거.
        if to_status == CandidateStatus.APPROVED:
            if approval is None:
                raise MissingEvidenceError("승인에는 Approval(독립 승인자 + QA 근거)이 필요하다")
            if approval.approver == candidate.author:
                raise SelfApprovalError(
                    f"작성자({candidate.author})는 자기 후보를 승인할 수 없다"
                )
            if not approval.qa_eval_run_id:
                raise MissingEvidenceError("승인에는 QA Eval 근거(qa_eval_run_id)가 필요하다")
            qa_eval_run_id = approval.qa_eval_run_id
            actor = approval.approver
            reason = approval.reason

        # Promotion/Rollback도 승인과 마찬가지로 후보 작성자가 단독 실행할 수 없다.
        # 실제 Profile 배포/복귀 실행기는 QA/Platform 소유지만, HR 상태 전이 계층에서도
        # 같은 불변식을 먼저 강제해 우회 경로를 만들지 않는다.
        if to_status in {CandidateStatus.DEPLOYED, CandidateStatus.ROLLED_BACK}:
            if actor == candidate.author:
                raise SelfApprovalError(
                    f"작성자({candidate.author})는 {to_status.value}를 단독 수행할 수 없다"
                )

        event = CandidateEvent(
            candidate_id=candidate.candidate_id,
            sequence=self._next_sequence(candidate.candidate_id),
            from_status=frm,
            to_status=to_status,
            actor=actor,
            reason=reason,
            occurred_at=at,
            qa_eval_run_id=qa_eval_run_id,
        )
        self._repo.append_event(event)

        return candidate.model_copy(update={"status": to_status})


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/improvements/workflow.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timezone

    from candidate import ImprovementCandidate

    def _candidate(**over) -> ImprovementCandidate:
        base = {
            "candidate_id": "ic-1",
            "author": "qa-department-hermes",
            "target_type": "PROFILE",
            "target_ref": "agent-citation-checker",
            "target_current_version": 3,
            "evidence_ids": ["finding-101"],
            "expected_effect": "인용 누락 오탐 감소",
            "risk_class": "MEDIUM",
            "rollback_target_version": 3,
        }
        base.update(over)
        return ImprovementCandidate(**base)

    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    wf = ImprovementWorkflow()
    c = _candidate()

    # 1) Happy path: PROPOSED -> ... -> KEPT.
    c = wf.transition(c, CandidateStatus.EVALUATING, actor="hr", reason="Eval 시작", at=now)
    c = wf.transition(c, CandidateStatus.SHADOW, actor="hr", reason="Shadow 시작", at=now)
    c = wf.transition(c, CandidateStatus.PENDING_APPROVAL, actor="hr", reason="Build-vs-Extend 검토", at=now)
    # 독립 승인자 + QA 근거로 승인 (author 와 다른 사람).
    approval = Approval(approver="ceo-office-hermes", qa_eval_run_id="eval-777", reason="기준 통과")
    c = wf.transition(c, CandidateStatus.APPROVED, actor="ignored", reason="ignored", at=now, approval=approval)
    assert c.status == CandidateStatus.APPROVED
    c = wf.transition(c, CandidateStatus.DEPLOYED, actor="hr", reason="v4 배포", at=now)
    c = wf.transition(c, CandidateStatus.OBSERVING, actor="hr", reason="Scorecard 관찰", at=now)
    c = wf.transition(c, CandidateStatus.KEPT, actor="hr", reason="유지", at=now)
    assert c.status == CandidateStatus.KEPT

    # 같은 candidate_id 로 7개 Event, sequence 1..7 (같은 ID 재현).
    evs = wf.events_for("ic-1")
    assert len(evs) == 7
    assert [e.sequence for e in evs] == list(range(1, 8))
    assert all(e.candidate_id == "ic-1" for e in evs)
    # 승인 Event 에 QA 근거와 독립 승인자가 남는다.
    approve_ev = next(e for e in evs if e.to_status == CandidateStatus.APPROVED)
    assert approve_ev.actor == "ceo-office-hermes" and approve_ev.qa_eval_run_id == "eval-777"

    # 2) 자기승인 차단 (author == approver).
    wf2 = ImprovementWorkflow()
    c2 = _candidate(candidate_id="ic-2")
    c2 = wf2.transition(c2, CandidateStatus.EVALUATING, actor="hr", reason="", at=now)
    c2 = wf2.transition(c2, CandidateStatus.SHADOW, actor="hr", reason="", at=now)
    c2 = wf2.transition(c2, CandidateStatus.PENDING_APPROVAL, actor="hr", reason="", at=now)
    self_approval = Approval(approver="qa-department-hermes", qa_eval_run_id="eval-1", reason="")
    try:
        wf2.transition(c2, CandidateStatus.APPROVED, actor="x", reason="x", at=now, approval=self_approval)
        raise AssertionError("자기승인이 통과함")
    except SelfApprovalError:
        pass

    # 3) QA 근거 없는 승인 차단.
    try:
        wf2.transition(c2, CandidateStatus.APPROVED, actor="x", reason="x", at=now, approval=None)
        raise AssertionError("근거 없는 승인이 통과함")
    except MissingEvidenceError:
        pass

    # 4) 허용되지 않은 전이 차단 (PROPOSED -> APPROVED 건너뛰기).
    wf3 = ImprovementWorkflow()
    c3 = _candidate(candidate_id="ic-3")
    try:
        wf3.transition(c3, CandidateStatus.APPROVED, actor="x", reason="x", at=now,
                       approval=Approval(approver="other", qa_eval_run_id="e", reason=""))
        raise AssertionError("불법 전이가 통과함")
    except IllegalTransition:
        pass

    # 5) Eval 실패는 기존 Profile을 유지하는 HOLD로 종료한다.
    wf_hold = ImprovementWorkflow()
    c_hold = _candidate(candidate_id="ic-hold")
    c_hold = wf_hold.transition(c_hold, CandidateStatus.EVALUATING, actor="hr", reason="Eval 시작", at=now)
    c_hold = wf_hold.transition(c_hold, CandidateStatus.HOLD, actor="qa-eval-consumer", reason="Eval 실패", at=now)
    assert c_hold.status == CandidateStatus.HOLD
    try:
        wf_hold.transition(c_hold, CandidateStatus.EVALUATING, actor="hr", reason="재시도", at=now)
        raise AssertionError("HOLD 종료 상태에서 전이됨")
    except IllegalTransition:
        pass

    # 6) 작성자는 Promotion을 단독 수행할 수 없다.
    wf_deploy = ImprovementWorkflow()
    c_deploy = _candidate(candidate_id="ic-deploy")
    for to, appr in [
        (CandidateStatus.EVALUATING, None),
        (CandidateStatus.SHADOW, None),
        (CandidateStatus.PENDING_APPROVAL, None),
        (CandidateStatus.APPROVED, Approval(approver="ceo", qa_eval_run_id="e8", reason="ok")),
    ]:
        c_deploy = wf_deploy.transition(c_deploy, to, actor="hr", reason="", at=now, approval=appr)
    try:
        wf_deploy.transition(c_deploy, CandidateStatus.DEPLOYED,
                             actor=c_deploy.author, reason="자기 배포", at=now)
        raise AssertionError("작성자의 Promotion이 통과함")
    except SelfApprovalError:
        pass

    # 7) 롤백 경로: OBSERVING -> ROLLED_BACK. 작성자는 Rollback도 단독 수행할 수 없다.
    wf4 = ImprovementWorkflow()
    c4 = _candidate(candidate_id="ic-4")
    for to, appr in [
        (CandidateStatus.EVALUATING, None),
        (CandidateStatus.SHADOW, None),
        (CandidateStatus.PENDING_APPROVAL, None),
        (CandidateStatus.APPROVED, Approval(approver="ceo", qa_eval_run_id="e9", reason="ok")),
        (CandidateStatus.DEPLOYED, None),
        (CandidateStatus.OBSERVING, None),
        (CandidateStatus.ROLLED_BACK, None),
    ]:
        actor = "independent-operator" if to in {CandidateStatus.DEPLOYED, CandidateStatus.ROLLED_BACK} else "hr"
        c4 = wf4.transition(c4, to, actor=actor, reason="", at=now, approval=appr)
    assert c4.status == CandidateStatus.ROLLED_BACK

    # 8) 종료 상태에서 추가 전이 불가.
    try:
        wf4.transition(c4, CandidateStatus.OBSERVING, actor="hr", reason="", at=now)
        raise AssertionError("종료 상태에서 전이됨")
    except IllegalTransition:
        pass

    print("ok - improvement workflow 상태머신·권한분리 점검 통과")
