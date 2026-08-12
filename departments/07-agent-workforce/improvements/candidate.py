#!/usr/bin/env python3
"""F19: 자기 개선 후보(ImprovementCandidate) 계약.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F19(승인형 Hermes 자기 개선),
      docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md 6.5, docs/HEDGE_FUND_MASTER_PLAN.md 5.10

이 Pydantic 계약과 대응 테이블 workforce.improvement_candidates 의 DDL check 제약(근거·롤백·
상태 enum)은 같은 규칙을 강제한다
(supabase/migrations/20260730000600_workforce_improvement_candidates.sql).

불변식:
  1. 근거(evidence) 없는 후보는 만들 수 없다.
  2. 롤백 대상(rollback_target) 없는 후보는 만들 수 없다.
  3. 롤백 대상은 현재 Version 이하의 실재 Version 이어야 한다.
  4. 후보 작성자(author)는 자기 후보를 단독 승인할 수 없다 (workflow.py 에서 강제).

자체 점검: python departments/07-agent-workforce/improvements/candidate.py
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TargetType(str, Enum):
    """개선 대상 유형 (6.5 후보 유형)."""

    SKILL = "SKILL"
    PROFILE = "PROFILE"      # Agent Profile Version
    WORKFLOW = "WORKFLOW"
    AGENT = "AGENT"


class RiskClass(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class CandidateStatus(str, Enum):
    """개선 후보 생명주기 상태.

    PROPOSED       근거·대상·예상효과·위험·롤백을 갖춘 후보 접수
    EVALUATING     QA 고정 Eval 실행 (audit.eval_runs 참조)
    SHADOW         Read-only/Mock Tool 만으로 Shadow 실행
    PENDING_APPROVAL  HR Build-vs-Extend 검토 후 승인 대기
    APPROVED       독립 승인자 승인 완료
    REJECTED       (종료) 반려
    HOLD           (종료) Eval 실패/보류 — 기존 Profile을 유지하고 재평가를 기다림
    DEPLOYED       새 Version 배포 (agent_profile_versions 등)
    OBSERVING      Scorecard 관찰
    KEPT           (종료) 유지 성공
    ROLLED_BACK    (종료) 회귀 -> 이전 Champion 복귀
    RETIRED        (종료) 폐기
    """

    PROPOSED = "PROPOSED"
    EVALUATING = "EVALUATING"
    SHADOW = "SHADOW"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    HOLD = "HOLD"
    DEPLOYED = "DEPLOYED"
    OBSERVING = "OBSERVING"
    KEPT = "KEPT"
    ROLLED_BACK = "ROLLED_BACK"
    RETIRED = "RETIRED"


TERMINAL_STATUSES: frozenset[CandidateStatus] = frozenset(
    {
        CandidateStatus.REJECTED,
        CandidateStatus.HOLD,
        CandidateStatus.KEPT,
        CandidateStatus.ROLLED_BACK,
        CandidateStatus.RETIRED,
    }
)


class ImprovementCandidate(BaseModel):
    """자기 개선 후보 한 건.

    같은 candidate_id 로 관찰 -> Eval -> Shadow -> 승인 -> 배포 -> 관찰 -> 롤백까지
    추적한다 (F19 완료조건: 같은 ID 재현).
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    author: str = Field(min_length=1, description="후보를 만든 주체 (부서 Hermes id 등)")

    target_type: TargetType
    target_ref: str = Field(min_length=1, description="대상 Artifact 식별자 (agent_id/skill_id 등)")
    target_current_version: int = Field(gt=0, description="개선 대상의 현재 Version")

    evidence_ids: list[str] = Field(
        min_length=1, description="근거 ID (Case/Incident/Eval/사용자 교정). 비면 후보 불가"
    )
    expected_effect: str = Field(min_length=1, description="예상 효과")
    risk_class: RiskClass
    rollback_target_version: int = Field(
        gt=0, description="문제 발생 시 복귀할 Version (필수)"
    )

    status: CandidateStatus = CandidateStatus.PROPOSED

    @model_validator(mode="after")
    def _check(self) -> ImprovementCandidate:
        # 롤백 대상은 현재 Version 이하의 실재 Version 이어야 한다.
        if self.rollback_target_version > self.target_current_version:
            raise ValueError(
                "rollback_target_version 은 target_current_version 을 넘을 수 없다 "
                f"({self.rollback_target_version} > {self.target_current_version})"
            )
        # 근거 ID 중복 제거 방어.
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids 에 중복이 있다")
        return self


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/improvements/candidate.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from pydantic import ValidationError

    def _valid(**over) -> dict:
        base = {
            "candidate_id": "ic-1",
            "author": "qa-department-hermes",
            "target_type": "PROFILE",
            "target_ref": "agent-citation-checker",
            "target_current_version": 3,
            "evidence_ids": ["finding-101", "finding-102"],
            "expected_effect": "인용 누락 오탐 감소",
            "risk_class": "MEDIUM",
            "rollback_target_version": 3,
        }
        base.update(over)
        return base

    # 1) 정상 후보.
    c = ImprovementCandidate(**_valid())
    assert c.status == CandidateStatus.PROPOSED
    assert c.target_type == TargetType.PROFILE

    def _rejects(label: str, factory) -> None:
        try:
            factory()
        except (ValidationError, ValueError):
            return
        raise AssertionError(f"거부돼야 하는데 통과함: {label}")

    # 2) 근거 없는 후보 불가.
    _rejects("evidence 없음", lambda: ImprovementCandidate(**_valid(evidence_ids=[])))
    # 3) 롤백 대상이 현재 Version 초과.
    _rejects(
        "rollback>current",
        lambda: ImprovementCandidate(**_valid(rollback_target_version=5)),
    )
    # 4) 현재 Version 0 이하.
    _rejects("current<=0", lambda: ImprovementCandidate(**_valid(target_current_version=0)))
    # 5) 알 수 없는 필드.
    _rejects("unknown field", lambda: ImprovementCandidate(**_valid(foo="bar")))
    # 6) 근거 중복.
    _rejects(
        "evidence 중복",
        lambda: ImprovementCandidate(**_valid(evidence_ids=["f1", "f1"])),
    )

    print("ok - ImprovementCandidate 계약 점검 통과")
