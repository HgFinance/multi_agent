#!/usr/bin/env python3
"""HR-02: Profile·Tool Permission 공식 Read API의 도메인 계약.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md 5.4(Workforce - Roster·Profile 소유),
      실행 Route 는 docs/02-engineering/contracts/route-registry.v1.json,
      docs/PROJECT_IMPLEMENTATION_STATUS.md HR-02("QA와 Model Gateway가 같은 Profile
      Version을 조회할 Read API"),
      supabase/migrations/20260729000200_governance_workforce.sql
      (workforce.agent_profiles/agent_profile_versions/role_templates/departments/models)

여기엔 LLM이 없다. Roster 조회와 Profile Version 발급·상태 전이는 전부 결정론적 코드다.

아래 dataclass 들은 원래 GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC 3.1 의 DTO 와 1:1 이었다.
그 문서가 UNIFIED_DOMAIN_API_SPEC 로 통합되면서 **개별 응답 모양은 옮겨지지 않았으므로**
(통합 문서는 Route Namespace 와 소유 경계까지만 정한다), 지금 응답 모양의 정본은
이 모듈의 dataclass 와 자체 점검이다.

불변식:
  1. `submit_profile`은 항상 새 Version을 만든다 - 기존 Version을 수정하는 경로를 두지
     않는다(TEAM_YOUNGJU §4.3 "Prompt만 바꾸고 Version 유지 금지"). Repository에 update가
     없고 insert만 있는 이유다.
  2. `to_status="ACTIVE"`로의 전이는 `qa_eval_run_id`와 `ceo_approval_id`가 **둘 다** 있어야
     한다 - 하나라도 없으면 거절한다(구 API 설계서 3.1 규칙. 통합 문서 5.4 에는 "작성자와
     승인자가 같으면 403"만 남아, 둘 다 요구하는 이 규칙의 정본은 validate_status_change 다). 인사팀이
     자기 후보를 자기가 ACTIVE로 못 올리는 권한 분리(CLAUDE.md)를 여기서 강제한다.
  3. Agent 개별 모델(agent_profile_versions.model_id)과 부서 Supervisor 모델
     (hermes/config.yaml의 model:)은 다른 레이어다 - 응답에서 섞지 않는다.

자체 점검: python departments/07-agent-workforce/roster/roster.py
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class EmploymentStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    PROBATION = "PROBATION"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"


class ProfileVersionStatus(str, Enum):
    DRAFT = "DRAFT"
    EVALUATING = "EVALUATING"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"


class MissingActivationEvidenceError(Exception):
    """ACTIVE 전이에 qa_eval_run_id/ceo_approval_id 중 하나 이상이 없다 (불변식 2)."""


class UnverifiedActivationEvidenceError(Exception):
    """P0-3(2026-08-05) - qa_eval_run_id/ceo_approval_id가 비어있지 않아도 실재하지
    않거나 조건을 안 채우면 거절한다.

    불변식 2(MissingActivationEvidenceError)는 "칸이 비어있는지"만 봤다 - 아무 문자열이나
    넣으면 통과했다. 이 예외는 그보다 좁혀서: qa_eval_run_id는 이 profile_version_id를
    대상으로 한 audit.eval_runs 행이 실재하고 status='COMPLETED'여야 하고,
    ceo_approval_id는 이 profile_version_id를 object_id로 하는 governance.approvals
    행(object_type=AGENT_PROFILE_VERSION, required_role=CEO)이 실재하고
    decision='APPROVED'여야 한다. 그 결과 "작성자(HR)와 승인자(QA+CEO)가 분리된다"는
    요구도 자동으로 지켜진다 - HR은 QA eval_runs도 governance.approvals의 CEO 결정도
    스스로 만들 수 없는 부서라서다.
    """


class ToolAllowlistMissingError(Exception):
    """P0-3 - tool_allowlist가 비어있는 Persona는 ACTIVE(실행 권한)로 전이할 수 없다."""


class AgentNotFoundError(Exception):
    """agent_id가 Roster에 없다."""


@dataclass(frozen=True)
class ModelRef:
    """agent_profile_versions.model_id가 가리키는 workforce.models 한 행의 요약."""

    provider: str
    model_name: str
    model_version: str


@dataclass(frozen=True)
class ProfileVersionSummary:
    """workforce.agent_profile_versions 한 행의 Read View (머리말 참고 - 이 모듈이 정본)."""

    profile_version_id: str
    version: int
    model: ModelRef
    memory_namespace: str
    status: ProfileVersionStatus


@dataclass(frozen=True)
class AgentSummary:
    """workforce.agent_profiles + role_templates + 현재 Version의 Read View (get_roster 응답)."""

    agent_id: str
    employee_code: str
    display_name: str
    department_code: str
    role_code: str
    employment_status: EmploymentStatus
    current_version: int
    current_profile_version: ProfileVersionSummary | None
    owner_user_id: str | None
    backup_owner_user_id: str | None = None


@dataclass(frozen=True)
class ProfileVersionSubmission:
    """POST .../profile-versions Request. agent_profile_versions 컬럼과 1:1."""

    model_id: str
    prompt_artifact_path: str
    skill_manifest: dict
    tool_allowlist: dict
    data_scopes: dict
    memory_namespace: str
    token_budget: dict
    sla: dict
    eval_requirements: dict
    forbidden_actions: list
    effective_from: datetime
    effective_to: datetime | None = None

    def __post_init__(self) -> None:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to는 effective_from 이후여야 한다")


@dataclass(frozen=True)
class ProfileVersionRow:
    """workforce.agent_profile_versions insert 결과 한 행."""

    profile_version_id: str
    agent_id: str
    version: int
    submission: ProfileVersionSubmission
    artifact_hash: str
    status: ProfileVersionStatus


@dataclass(frozen=True)
class StatusChangeRequest:
    """POST .../status Request (change_status)."""

    to_status: EmploymentStatus
    profile_version_id: str
    reason: str
    idempotency_key: str
    qa_eval_run_id: str | None = None
    ceo_approval_id: str | None = None


def compute_artifact_hash(submission: ProfileVersionSubmission) -> str:
    """agent_profile_versions.artifact_hash - 제출 내용의 결정론적 지문.

    unique(agent_id, artifact_hash) 제약(DDL)의 목적은 "내용이 완전히 같은 재제출"을
    막는 것이므로, 요청 필드 중 내용을 규정하는 것만 해시에 넣는다(effective_from/
    effective_to처럼 스케줄만 바꾸는 재제출은 의도적으로 허용 - 해시에서 제외).
    """
    canonical = json.dumps(
        {
            "model_id": submission.model_id,
            "prompt_artifact_path": submission.prompt_artifact_path,
            "skill_manifest": submission.skill_manifest,
            "tool_allowlist": submission.tool_allowlist,
            "data_scopes": submission.data_scopes,
            "memory_namespace": submission.memory_namespace,
            "token_budget": submission.token_budget,
            "sla": submission.sla,
            "eval_requirements": submission.eval_requirements,
            "forbidden_actions": submission.forbidden_actions,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_status_change(request: StatusChangeRequest) -> None:
    """불변식 2 - ACTIVE 전이는 QA Eval과 CEO 승인 둘 다 있어야 한다."""
    if (
        request.to_status is EmploymentStatus.ACTIVE
        and (not request.qa_eval_run_id or not request.ceo_approval_id)
    ):
        raise MissingActivationEvidenceError(
            "ACTIVE 전이는 qa_eval_run_id와 ceo_approval_id가 둘 다 있어야 한다 "
            f"(qa_eval_run_id={request.qa_eval_run_id!r}, ceo_approval_id={request.ceo_approval_id!r})"
        )


def verify_activation_evidence(
    *, eval_run_status: str | None, approval_decision: str | None, tool_allowlist: dict,
) -> None:
    """P0-3 - validate_status_change 다음 단계. 실재성까지 검증한다.

    호출자가 audit.eval_runs/governance.approvals를 먼저 조회해 얻은 값을 넘긴다 -
    이 함수 자체는 DB를 모른다(actor_identity.py verify_actor_user와 같은 분리 원칙:
    조회는 Repository, 판정은 순수 함수).
    """
    if eval_run_status != "COMPLETED":
        raise UnverifiedActivationEvidenceError(
            f"qa_eval_run_id가 이 Profile Version을 대상으로 한 완료된 Eval Run을 "
            f"가리키지 않는다 (조회된 status={eval_run_status!r})"
        )
    if approval_decision != "APPROVED":
        raise UnverifiedActivationEvidenceError(
            f"ceo_approval_id가 이 Profile Version에 대한 승인된 CEO 결정을 가리키지 "
            f"않는다 (조회된 decision={approval_decision!r})"
        )
    if not tool_allowlist:
        raise ToolAllowlistMissingError(
            "tool_allowlist가 비어있는 Persona는 ACTIVE(실행 권한)로 전이할 수 없다"
        )


class RosterRepository:
    """조회·저장 인터페이스. 실제 구현은 workforce.agent_profiles/agent_profile_versions에 반영한다."""

    def list_roster(self) -> list[AgentSummary]:
        raise NotImplementedError

    def get_agent(self, agent_id: str) -> AgentSummary | None:
        raise NotImplementedError

    def submit_profile(self, agent_id: str, submission: ProfileVersionSubmission) -> ProfileVersionRow:
        """항상 새 Version을 insert한다 - update 경로 없음 (불변식 1).

        Version 번호 부여와 artifact_hash 계산은 Repository 책임이다(호출자가 미리
        만들지 않는다) - 그래야 "다음 Version이 몇 번인지"를 Repository가 단일하게
        소유하고 경합을 그 안에서 처리한다.
        """
        raise NotImplementedError

    def change_status(
        self, agent_id: str, *, to_status: EmploymentStatus, at: datetime,
        trace_id: str, reason: str | None = None, approvals: list | None = None,
    ) -> None:
        """상태를 바꾸고 그 전이를 workforce.lifecycle_events 에 남긴다.

        trace_id/reason/approvals 가 여기 있는 이유: 전이 사실만 남기면 "무슨 근거로
        ACTIVE 가 됐는지"를 사후에 확인할 수 없다(HR-04 KPI "승인 없는 활성화 0").
        구현은 상태 변경과 이벤트 기록을 **한 트랜잭션**에 넣어야 한다 -
        lifecycle_event.py 불변식 1.
        """
        raise NotImplementedError

    def get_profile_version_tool_allowlist(self, profile_version_id: str) -> dict | None:
        """P0-3 - ACTIVE 전이 게이트가 tool_allowlist 비어있음을 판정하려면 필요하다.
        AgentSummary/ProfileVersionSummary(list_roster/get_agent의 Read View)에는 이
        필드가 없다 - 목록 응답을 무겁게 만들지 않으려는 기존 설계라 별도 조회로 뺀다.
        존재하지 않는 profile_version_id는 None."""
        raise NotImplementedError


class InMemoryRosterRepository(RosterRepository):
    def __init__(self) -> None:
        self._agents: dict[str, AgentSummary] = {}
        self._versions: dict[str, list[ProfileVersionRow]] = {}
        self._lifecycle_events: list[dict] = []

    def seed_agent(self, agent: AgentSummary) -> None:
        """테스트·개발용 seed. 실 구현에서는 workforce.agent_profiles를 조회한다."""
        self._agents[agent.agent_id] = agent

    def list_roster(self) -> list[AgentSummary]:
        return list(self._agents.values())

    def get_agent(self, agent_id: str) -> AgentSummary | None:
        return self._agents.get(agent_id)

    def submit_profile(self, agent_id: str, submission: ProfileVersionSubmission) -> ProfileVersionRow:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise AgentNotFoundError(f"agent_id={agent_id}를 찾을 수 없다")
        existing = self._versions.setdefault(agent_id, [])
        version = len(existing) + 1
        row = ProfileVersionRow(
            profile_version_id=f"{agent_id}-v{version}", agent_id=agent_id, version=version,
            submission=submission, artifact_hash=compute_artifact_hash(submission),
            status=ProfileVersionStatus.DRAFT,
        )
        existing.append(row)
        model = ModelRef(provider="", model_name="", model_version="")
        updated_version = ProfileVersionSummary(
            profile_version_id=row.profile_version_id, version=row.version,
            model=model, memory_namespace=row.submission.memory_namespace, status=row.status,
        )
        self._agents[agent_id] = AgentSummary(
            **{**agent.__dict__, "current_version": row.version, "current_profile_version": updated_version}
        )
        return row

    def get_profile_version_tool_allowlist(self, profile_version_id: str) -> dict | None:
        for versions in self._versions.values():
            for row in versions:
                if row.profile_version_id == profile_version_id:
                    return row.submission.tool_allowlist
        return None

    def change_status(
        self, agent_id: str, *, to_status: EmploymentStatus, at: datetime,
        trace_id: str = "", reason: str | None = None, approvals: list | None = None,
    ) -> None:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise AgentNotFoundError(f"agent_id={agent_id}를 찾을 수 없다")
        # In-Memory 도 전이를 남긴다 - 실 DB 구현만 기록하면 이 대역으로 도는 테스트가
        # "이벤트 없이 상태가 바뀌는" 경로를 정상으로 보게 된다.
        self._lifecycle_events.append(
            {
                "agent_id": agent_id, "event_type": "STATUS_CHANGE",
                "from_status": agent.employment_status.value, "to_status": to_status.value,
                "approvals": approvals or [], "reason": reason,
                "trace_id": trace_id, "occurred_at": at,
            }
        )
        self._agents[agent_id] = AgentSummary(**{**agent.__dict__, "employment_status": to_status})

    def list_lifecycle_events(self, agent_id: str) -> list[dict]:
        return [e for e in self._lifecycle_events if e["agent_id"] == agent_id]


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/roster/roster.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timezone

    t0 = datetime(2026, 8, 3, tzinfo=timezone.utc)

    # 1) ACTIVE 전이 - 증거 없으면 거절 (불변식 2).
    try:
        validate_status_change(StatusChangeRequest(
            to_status=EmploymentStatus.ACTIVE, profile_version_id="pv-1",
            reason="", idempotency_key="idem-1",
        ))
        raise AssertionError("증거 없는 ACTIVE 전이가 통과함")
    except MissingActivationEvidenceError:
        pass

    # 2) qa_eval_run_id만 있고 ceo_approval_id 없음 - 거절.
    try:
        validate_status_change(StatusChangeRequest(
            to_status=EmploymentStatus.ACTIVE, profile_version_id="pv-1",
            reason="", idempotency_key="idem-2", qa_eval_run_id="eval-1",
        ))
        raise AssertionError("CEO 승인 없는 ACTIVE 전이가 통과함")
    except MissingActivationEvidenceError:
        pass

    # 3) 둘 다 있으면 통과.
    validate_status_change(StatusChangeRequest(
        to_status=EmploymentStatus.ACTIVE, profile_version_id="pv-1",
        reason="", idempotency_key="idem-3", qa_eval_run_id="eval-1", ceo_approval_id="appr-1",
    ))

    # 4) ACTIVE가 아닌 전이는 증거 없이도 통과 (PROBATION/SUSPENDED/RETIRED).
    validate_status_change(StatusChangeRequest(
        to_status=EmploymentStatus.SUSPENDED, profile_version_id="pv-1",
        reason="성과 미달", idempotency_key="idem-4",
    ))

    # 5) Roster 조회 - InMemory 왕복.
    repo = InMemoryRosterRepository()
    repo.seed_agent(AgentSummary(
        agent_id="a1", employee_code="HR-00", display_name="agent-workforce-supervisor",
        department_code="hr-department", role_code="HR-00",
        employment_status=EmploymentStatus.CANDIDATE, current_version=0,
        current_profile_version=None, owner_user_id=None,
    ))
    assert len(repo.list_roster()) == 1
    assert repo.get_agent("a1") is not None
    assert repo.get_agent("missing") is None

    # 6) 새 Profile Version 제출 - current_version 갱신 확인 (불변식 1).
    submission = ProfileVersionSubmission(
        model_id="m1", prompt_artifact_path="departments/07-agent-workforce/hermes/config.yaml#agent-workforce-supervisor",
        skill_manifest={"required": ["HR-01"]}, tool_allowlist={"read": ["capacity_snapshots"]},
        data_scopes={"workforce": "read"}, memory_namespace="workforce/hr-00",
        token_budget={"per_case_tokens": 200000, "daily_tokens": 2000000},
        sla={"decision_latency_hours": 24},
        eval_requirements={"status": "PENDING_QA"}, forbidden_actions=["investment_decision"],
        effective_from=t0,
    )
    row1 = repo.submit_profile("a1", submission)
    assert row1.version == 1
    assert row1.status == ProfileVersionStatus.DRAFT
    updated = repo.get_agent("a1")
    assert updated.current_version == 1
    assert updated.current_profile_version.status == ProfileVersionStatus.DRAFT

    # 6b) 두 번째 제출 - Version이 증가하고, 동일 내용 재제출은 같은 artifact_hash를 낸다.
    row2 = repo.submit_profile("a1", submission)
    assert row2.version == 2
    assert row2.artifact_hash == row1.artifact_hash
    assert row1.profile_version_id != row2.profile_version_id

    # 7) effective_to <= effective_from 거부.
    try:
        ProfileVersionSubmission(
            model_id="m1", prompt_artifact_path="x", skill_manifest={}, tool_allowlist={},
            data_scopes={}, memory_namespace="x", token_budget={}, sla={}, eval_requirements={},
            forbidden_actions=[], effective_from=t0, effective_to=t0,
        )
        raise AssertionError("effective_to <= effective_from이 통과함")
    except ValueError:
        pass

    # 8) 존재하지 않는 Agent에 전이 시도 - 거부.
    try:
        repo.change_status("missing", to_status=EmploymentStatus.ACTIVE, at=t0)
        raise AssertionError("존재하지 않는 Agent 전이가 통과함")
    except AgentNotFoundError:
        pass

    # 9) P0-3 - 증거 실재성 검증. eval_run_status/approval_decision이 조건을 못 채우면
    # UnverifiedActivationEvidenceError, tool_allowlist가 비면 ToolAllowlistMissingError.
    for bad_eval_status in (None, "RUNNING", "FAILED", "QUEUED"):
        try:
            verify_activation_evidence(
                eval_run_status=bad_eval_status, approval_decision="APPROVED",
                tool_allowlist={"read": ["x"]},
            )
            raise AssertionError(f"eval_run_status={bad_eval_status!r}가 통과함")
        except UnverifiedActivationEvidenceError:
            pass
    for bad_decision in (None, "PENDING", "REJECTED", "EXPIRED"):
        try:
            verify_activation_evidence(
                eval_run_status="COMPLETED", approval_decision=bad_decision,
                tool_allowlist={"read": ["x"]},
            )
            raise AssertionError(f"approval_decision={bad_decision!r}가 통과함")
        except UnverifiedActivationEvidenceError:
            pass
    try:
        verify_activation_evidence(
            eval_run_status="COMPLETED", approval_decision="APPROVED", tool_allowlist={},
        )
        raise AssertionError("빈 tool_allowlist가 통과함")
    except ToolAllowlistMissingError:
        pass
    verify_activation_evidence(  # 셋 다 만족하면 통과 (raise 없음).
        eval_run_status="COMPLETED", approval_decision="APPROVED", tool_allowlist={"read": ["x"]},
    )
    print("ok - P0-3 증거 실재성 검증 10개 시나리오 통과")

    print("ok - HR-02 Roster 도메인 계약 19개 시나리오 통과")
