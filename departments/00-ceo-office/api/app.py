#!/usr/bin/env python3
"""CEO Office Domain API — mandate/service.py, mandate/lifecycle.py,
reporting/daily_report.py, notification/notification.py를 감싸는 FastAPI 래퍼.

소유: 영주 (CEO Office)
근거: docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md 5.4(Governance·Workforce·Reporting),
      실행 Route 목록은 docs/02-engineering/contracts/route-registry.v1.json 이 정본이다,
      4절(reporting-api), departments/03-risk/api/app.py·departments/06-ai-qa-audit/api/app.py 패턴
      (TECH_STACK_DECISIONS.md 7절 - Hermes는 Domain 서비스를 API 경계로만 부른다).

여기엔 새 판정 로직이 없다. MandateVersionService/MandateActivationService/
DailyReportAssembler/NotificationService가 이미 하는 일을 얇게 감싼다.

Repository는 기본 In-Memory다. DATABASE_URL이 설정돼 있으면 Mandate/Report/Notification
셋 다 Postgres 구현으로 전환한다 - postgres_repository.py, postgres_report_repository.py,
postgres_notification_repository.py가 각각 실제로 검증된 조회·왕복 경로를 그대로 쓴다.

REDIS_URL(또는 GOVERNANCE_EVENT_REDIS_URL)이 설정돼 있으면 hf:governance Stream에도
발행한다(2026-08-03) - Mandate 제안/활성화는 governance.mandate.changed.v1, Report는
성공(QUEUED, 신규)일 때만 report.ready.v1. Redis가 없거나 발행이 실패해도 요청 자체는
실패시키지 않는다(_publish_governance_event 문서 참고 - Postgres가 이미 Canonical Source
of Truth라 Event는 부가 채널). governance_events/worker.py(notification-worker)가 이
Stream을 소비해 report.ready.v1/risk.breach.v1 등을 알림으로 바꾸는데, governance.
mandate.changed.v1 같은 다른 본부용 Event는 조용히 넘긴다(_KNOWN_NON_NOTIFICATION_EVENTS).

스펙과 의도적으로 다른 부분(투명하게 남긴다, 조용히 어기지 않는다):
  - GET /governance/v1/mandates/{mandate_id}/current는 canonical binding과 정책 snapshot을
    함께 반환한다. fund_id 기준 GET .../mandates/by-fund/{fund_id}/current는 canonical
    Repository가 역참조를 제공할 때만 단일 Mandate를 반환하며, 구형 Prototype에서는
    임의 선택하지 않고 503으로 닫는다.
  - POST .../versions는 In-Memory Repository일 때 Fund 기준 통화를 accounting-api에서
    조회할 수 없으므로, 요청에 fund_base_currency를 선택 필드로 받아 seed한다(데모용,
    Postgres Repository를 쓸 때는 무시 - 실제 accounting.funds를 그대로 조회한다).
  - GET /reporting/v1/reports/{report_id}(스펙 4절)는 report_id가 DB 배선 전이라 없다 -
    content_hash + fund_id로 대신 조회한다.

실행: uvicorn app:app --app-dir departments/00-ceo-office/api
자체 점검: python departments/00-ceo-office/api/app.py
"""
from __future__ import annotations

import hmac
import logging
import os
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# 저장소 루트의 .env를 읽는다(find_dotenv()가 이 파일 위치부터 상위로 탐색 - 실행
# 위치(cwd)나 uvicorn --app-dir 여부와 무관). 이미 설정된 프로세스 환경변수는 덮어쓰지
# 않는다(override=False 기본값) - 배포 환경 값이 항상 우선한다.
load_dotenv()
GOVERNANCE_API_AUTH_REQUIRED = os.getenv(
    "GOVERNANCE_API_AUTH_REQUIRED", "false"
).casefold() in {"1", "true", "yes", "on"}
GOVERNANCE_API_AUTH_TOKEN = os.getenv("GOVERNANCE_API_AUTH_TOKEN", "").strip()


def _require_internal_auth(
    x_governance_internal_token: str | None = Header(default=None),
) -> None:
    """Enforce the deployment-provided service identity for canonical reads."""
    if not GOVERNANCE_API_AUTH_REQUIRED:
        return
    if not GOVERNANCE_API_AUTH_TOKEN:
        raise HTTPException(status_code=503, detail="governance_auth_not_configured")
    if not x_governance_internal_token or not hmac.compare_digest(
        x_governance_internal_token, GOVERNANCE_API_AUTH_TOKEN
    ):
        raise HTTPException(status_code=401, detail="governance_auth_required")

_BASE = Path(__file__).resolve().parent.parent
_MANDATE_DIR = _BASE / "src" / "mandate"
_REPORTING_DIR = _BASE / "src" / "reporting"
_NOTIFICATION_DIR = _BASE / "src" / "notification"
_APPROVAL_DIR = _BASE / "src" / "approval"
_CASE_DIR = _BASE / "src" / "case"
_ESCALATION_DIR = _BASE / "src" / "escalation"
_COMMITTEE_DIR = _BASE / "src" / "committee"
for _p in (
    _MANDATE_DIR, _REPORTING_DIR, _NOTIFICATION_DIR, _APPROVAL_DIR, _CASE_DIR,
    _ESCALATION_DIR, _COMMITTEE_DIR,
):
    sys.path.insert(0, str(_p))

from actor_identity import (
    InMemoryActorIdentityRepository,
    UnverifiedActorUserError,
    verify_actor_user,
)
from approval import (
    AlreadyDecidedError,
    ApprovalDecision,
    ApprovalExpiredError,
    ApprovalRecord,
    InMemoryApprovalRepository,
    MissingActorUserError,
    ObjectType,
    OwnerApprovalNotSupportedError,
    RequiredRole,
    UnauthorizedDeciderError,
)
from approval import (
    decide as decide_approval,
)
from approval import (
    request_approval as build_approval_request,
)
from approval import (
    revoke as revoke_approval,
)
from case_root import (
    CaseEvent,
    CaseRecord,
    CaseStatus,
    IllegalCaseTransition,
    InMemoryCaseRepository,
    build_display_id,
)
from case_root import (
    open_case as open_case_root,
)
from case_root import (
    transition as transition_case,
)
from change_workflow import (
    CaseAlreadyResolvedError,
    ChangeRequestResult,
    MandateChangeWorkflow,
    NotAMandateChangeCaseError,
    ReviewApprovalMissingError,
)
from committee import (
    CommitteeDecisionRecord,
    CommitteeSession,
    DuplicateVoteError,
    IllegalSessionTransition,
    InMemoryCommitteeRepository,
    InvalidQuorumPolicyError,
    QuorumPolicy,
    SelfReviewError,
    Vote,
    VoteDecision,
)
from committee import (
    cancel_session as cancel_committee_session,
)
from committee import (
    cast_vote as cast_committee_vote,
)
from committee import (
    close_session as close_committee_session,
)
from committee import (
    open_session as open_committee_session,
)
from daily_report import (
    DailyReportAssembler,
    DailyReportSections,
    InMemoryReportRunRepository,
    SnapshotRef,
)
from escalation import (
    EscalationRecord,
    EscalationStatus,
    IllegalEscalationTransition,
    InMemoryEscalationRepository,
    MissingResolutionError,
    Severity,
)
from escalation import (
    open_escalation as open_escalation_record,
)
from escalation import (
    transition as transition_escalation,
)
from langgraph.checkpoint.memory import InMemorySaver
from lifecycle import (
    ActivationResult,
    MandateActivationService,
    UserApproval,
)
from mandate_assistant import AssistantMessage as _AssistantMessage
from mandate_assistant import suggest as _mandate_assistant_suggest
from notification import (
    InMemoryNotificationRepository,
    NotificationRequest,
    NotificationService,
)
from policy import MandatePolicy
from service import (
    ChangeDirection,
    CurrencyMismatchError,
    FundNotFoundError,
    InMemoryMandateVersionRepository,
    MandateAlreadyExistsError,
    MandateVersionService,
)

try:
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
except ImportError:  # langgraph-checkpoint-postgres 미설치 환경에서도 앱 자체는 뜬다
    PostgresSaver = None  # type: ignore[assignment,misc]

try:
    from postgres_repository import (
        MandatePersistenceError,
        PostgresMandateVersionRepository,
    )
except ImportError:  # psycopg2 미설치 환경에서도 앱 자체는 뜬다
    PostgresMandateVersionRepository = None  # type: ignore[assignment,misc]

    class MandatePersistenceError(RuntimeError):  # type: ignore[no-redef]
        pass

try:
    from postgres_report_repository import PostgresReportRunRepository
except ImportError:
    PostgresReportRunRepository = None  # type: ignore[assignment,misc]

try:
    from postgres_notification_repository import (
        NotificationPersistenceError,
        PostgresNotificationRepository,
    )
except ImportError:
    PostgresNotificationRepository = None  # type: ignore[assignment,misc]

    class NotificationPersistenceError(RuntimeError):  # type: ignore[no-redef]
        pass

try:
    from postgres_approval_repository import PostgresApprovalRepository
except ImportError:
    PostgresApprovalRepository = None  # type: ignore[assignment,misc]

try:
    from postgres_case_repository import PostgresCaseRepository
except ImportError:
    PostgresCaseRepository = None  # type: ignore[assignment,misc]

try:
    from postgres_escalation_repository import PostgresEscalationRepository
except ImportError:
    PostgresEscalationRepository = None  # type: ignore[assignment,misc]

try:
    from postgres_committee_repository import PostgresCommitteeRepository
except ImportError:
    PostgresCommitteeRepository = None  # type: ignore[assignment,misc]

try:
    from actor_identity import PostgresActorIdentityRepository
except ImportError:
    PostgresActorIdentityRepository = None  # type: ignore[assignment,misc]

_GOVERNANCE_EVENTS_DIR = _BASE / "governance_events"
sys.path.insert(0, str(_GOVERNANCE_EVENTS_DIR))

from redis_event_bus import (
    DEFAULT_GROUP as _GOVERNANCE_EVENT_GROUP,
)
from redis_event_bus import (
    DEFAULT_STREAM as _GOVERNANCE_EVENT_STREAM,
)
from redis_event_bus import (
    GovernanceEventBusError,
    RedisEventBus,
)

# --- Request/Response 모델 ---------------------------------------------------


class ProposeVersionRequest(BaseModel):
    policy: MandatePolicy
    objective_text: str = Field(min_length=1)
    objective: dict
    effective_from: datetime
    previous_policy: MandatePolicy | None = None
    effective_to: datetime | None = None
    execution_rules: dict | None = None
    created_by: str | None = None
    fund_base_currency: str | None = Field(
        default=None,
        description="In-Memory Repository 데모용 seed - accounting-api 없이 통화 검증하려면 채운다. "
                    "Postgres Repository를 쓸 때는 무시하고 accounting.funds를 그대로 조회한다.",
    )


class ReplaceMandateMetadataRequest(BaseModel):
    """현재 사용자 Mandate 메타데이터를 한 행에 덮어쓴다."""

    policy: MandatePolicy
    objective_text: str = Field(min_length=1)
    objective: dict = Field(default_factory=dict)
    execution_rules: dict = Field(default_factory=dict)
    created_by: str | None = None


class SuggestRequestIn(BaseModel):
    """POST /governance/v1/mandate-assistant/suggest (USER_INPUT_API_SPEC.md 2.4).

    Stateless - fund_id는 미래에 Fund별 컨텍스트(허용 자산군 등)를 프롬프트에
    반영할 자리로만 받아두고 아직 쓰지 않는다. messages/current_draft만 실제로
    LLM 제안을 만든다.
    """

    fund_id: str
    messages: list[_AssistantMessage] = Field(min_length=1)
    current_draft: dict | None = None


class ApprovalIn(BaseModel):
    approved_by: str
    trace_id: str
    reason: str | None = None


class ActivateRequest(BaseModel):
    direction: ChangeDirection
    at: datetime
    approval: ApprovalIn | None = None


class SubmitChangeRequestIn(BaseModel):
    """POST /governance/v1/mandates/{mandate_id}/change-requests
    (HITL §5.1, change_workflow.MandateChangeWorkflow.submit()).

    `created_by`(Case 감사 표지, 자유 텍스트)와 `version_created_by`
    (mandate_versions.created_by, governance.user_profiles를 가리키는 uuid, nullable)는
    컬럼 타입이 달라 분리한다 - change_workflow.submit() 문서 참고.
    """

    fund_id: str
    policy: MandatePolicy
    objective_text: str = Field(min_length=1)
    objective: dict
    effective_from: datetime
    created_by: str = Field(min_length=1)
    trace_id: str
    now: datetime
    previous_policy: MandatePolicy | None = None
    priority: int = Field(default=50, ge=0, le=100)
    review_expires_at: datetime | None = None
    user_approval_ttl_seconds: int = Field(
        default=24 * 60 * 60, ge=1,
        description="Risk/QA 통과 뒤 사용자 승인 요청이 유효한 시간(초). 기본 24시간.",
    )
    version_created_by: str | None = None
    fund_base_currency: str | None = Field(
        default=None,
        description="ProposeVersionRequest.fund_base_currency와 동일한 In-Memory 데모용 seed. "
                    "Postgres Repository를 쓸 때는 무시한다.",
    )


class AdvanceCaseIn(BaseModel):
    """POST /governance/v1/cases/{case_id}/advance
    (change_workflow.MandateChangeWorkflow.advance()). 재개(resume)가 아니라 매번 새로
    판단하는 짧은 호출이다 - Risk/QA/사용자 승인이 결정될 때마다 다시 부른다.
    """

    at: datetime


class NotificationRequestIn(BaseModel):
    fund_id: str
    event_type: str
    scope_key: str
    recipient: str
    payload: dict = {}
    severity: str | None = None
    now: datetime


# GOV-02 1단계 승인 모델. 위 ApprovalIn(Mandate 활성화 시 '사용자 승인' 증적)과 다른 개념이라
# 이름을 구분한다 - 저건 lifecycle.py의 UserApproval이고 이건 governance.approvals 행이다.


class ApprovalRequestIn(BaseModel):
    """POST /governance/v1/approvals (스펙 2.2 request_approval Request 그대로)."""

    object_type: ObjectType
    object_id: str
    required_role: RequiredRole
    fund_id: str
    reason: str | None = None
    expires_at: datetime | None = None
    conditions: dict = {}
    idempotency_key: str | None = Field(
        default=None,
        description="스펙 2.2 필드. 실제 중복 방지는 DDL의 "
                    "unique(object_type, object_id, required_role)가 하므로 여기서 별도로 "
                    "저장하지 않는다 - governance.approvals에 이 컬럼이 없다.",
    )


class ApprovalDecisionIn(BaseModel):
    """POST /governance/v1/approvals/{approval_id}/decide.

    스펙에 이름이 없는 제안 엔드포인트다(2.2는 request_approval만 확정). 요청 생성과 결정
    기록을 분리해야 하는 이유는 권한이 다르기 때문이다 - approval.py 불변식 2 참고.

    actor_department는 호출자가 자기 부서를 밝히는 값이고, 이 API는 그것이 required_role과
    맞는지만 결정론적으로 검사한다. 실제 신원 증명(mTLS/JWT 등)은 Platform 계층 몫이며
    아직 없다 - 그 전까지 이 검사는 '실수 방지'이고 '침입 방지'가 아니다(투명하게 남긴다).

    required_role=USER(HITL 사용자 승인, 2026-08-04)는 부서가 아니라 사람이 결정하는
    역할이라 actor_department가 필요 없다 - 대신 actor_user_id가 필수다. 그래서 이 필드는
    선택이고, 어느 쪽이 필요한지는 approval.py의 assert_can_decide()가 required_role을
    보고 판단한다(400/403으로 거절).
    """

    decision: ApprovalDecision
    actor_department: str | None = None
    at: datetime
    actor_agent_id: str | None = Field(
        default=None,
        description="결정한 Agent. workforce.agent_profiles FK이므로 Roster에 등록된 "
                    "agent_id여야 한다(미등록 uuid는 DB가 거절).",
    )
    actor_user_id: str | None = Field(
        default=None,
        description="사람이 찍은 승인일 때만 채운다. governance.user_profiles FK.",
    )
    conditions: dict | None = None
    reason: str | None = None


class ApprovalRevokeIn(BaseModel):
    actor_department: str | None = None
    at: datetime
    reason: str = Field(min_length=1)
    actor_agent_id: str | None = None
    actor_user_id: str | None = None


class CreateCaseIn(BaseModel):
    """POST /governance/v1/cases (스펙 2.2 create_case Request 그대로).

    display_id는 요청에 없다 - NOT NULL unique인데 스펙이 정의하지 않아 서버가
    `PREFIX-YYYYMMDD-NNNN` 꼴로 만든다(case_root.build_display_id, MSU_SPEC 8절의
    "IC-20260731-0001" 형태를 따랐다).
    """

    case_type: str = Field(min_length=1)
    priority: int = Field(ge=0, le=100)
    owner_department: str = Field(min_length=1)
    fund_id: str
    trace_id: str
    created_by: str = Field(min_length=1)
    due_at: datetime | None = None
    reason: str | None = None
    payload: dict = {}
    idempotency_key: str | None = Field(
        default=None,
        description="case_events.idempotency_key(unique)로 저장된다. 없으면 서버가 생성한다.",
    )


class CreateEscalationIn(BaseModel):
    """POST /governance/v1/escalations (스펙 2.2 제안 엔드포인트).

    case_id는 NOT NULL FK다 - 에스컬레이션은 항상 어떤 Case에 붙는다.
    """

    case_id: str
    reason: str = Field(min_length=1)
    severity: Severity
    target: str = Field(min_length=1)
    due_at: datetime | None = None


class EscalationTransitionIn(BaseModel):
    """POST /governance/v1/escalations/{escalation_id}/transitions.

    RESOLVED로 닫을 때는 resolution이 필수다 - 사유 없이 닫힌 에스컬레이션은 추적이 끊긴다.
    """

    to_status: EscalationStatus
    at: datetime
    resolution: str | None = None


class QuorumPolicyIn(BaseModel):
    """committee_sessions.quorum_policy(jsonb)의 요청 형태 - committee.py QuorumPolicy 참고.

    committee_type별로 구성이 다르다(투자위원회 vs 전략기획위원회 - MASTER_PLAN 5.2/18.2,
    QA는 투자위원회 구성원이 아니다) - 그래서 세션을 여는 쪽이 매번 지정한다.
    """

    required_departments: list[str] = Field(min_length=1)
    veto_departments: list[str] = []
    approval_threshold: int = Field(default=1, ge=1)


class OpenCommitteeSessionIn(BaseModel):
    """POST /governance/v1/committee/sessions (스펙 2.3 open_session)."""

    fund_id: str
    committee_type: str = Field(min_length=1)
    quorum_policy: QuorumPolicyIn
    trace_id: str
    case_id: str | None = None


class CloseCommitteeSessionIn(BaseModel):
    scope: dict = {}
    valid_until: datetime | None = None


class CancelCommitteeSessionIn(BaseModel):
    reason: str | None = None


class SubmitVoteIn(BaseModel):
    """POST /governance/v1/committee/sessions/{session_id}/votes (스펙 2.3 submit_vote 그대로)."""

    department: str = Field(min_length=1)
    decision: VoteDecision
    conditions: dict = {}
    artifact_ids: list[str] = []
    rationale: str | None = None
    voter_agent_id: str | None = None


class CaseTransitionIn(BaseModel):
    """POST /governance/v1/cases/{case_id}/transitions.

    스펙에 이름이 없는 제안 엔드포인트다. Case를 만들 수만 있고 진행시킬 수 없으면
    MSU_SPEC 3절이 금지한 "무기한 대기하는 Case"가 되므로 함께 넣는다.
    """

    to_status: CaseStatus
    actor: str = Field(min_length=1)
    at: datetime
    reason: str | None = None
    payload: dict = {}
    idempotency_key: str | None = None


class SnapshotRefIn(BaseModel):
    snapshot_id: str
    as_of: datetime


class AssembleReportRequest(BaseModel):
    fund_id: str
    as_of: date
    template_version: str
    trace_id: str
    portfolio: SnapshotRefIn | None = None
    risk: SnapshotRefIn | None = None
    research: SnapshotRefIn | None = None
    execution: SnapshotRefIn | None = None
    strategy: SnapshotRefIn | None = None
    qa: SnapshotRefIn | None = None
    pending_user_action_case_ids: list[str] = []


# --- App ----------------------------------------------------------------------


app = FastAPI(title="CEO Office Domain API", version="v1")


# ── Health 계약 ───────────────────────────────────────────────────────────────
# 전 부서 공통 규격이다(통합계획 8.1). `/health` 는 프로세스 생존만 보고 외부를
# 만지지 않는다 - 여기서 DB 를 만지면 순단마다 오케스트레이터가 멀쩡한 인스턴스를
# 교체한다. 저장소 판단은 `/health/ready` 가 한다(trading/risk 와 같은 규약).
#
# ⚠ 이 서비스는 DATABASE_URL 이 없으면 **InMemory 저장소로 조용히 후퇴**한다.
#   그 상태를 200 "ok" 로만 보고하면 "승인 원장이 메모리에 있다"는 사실이 묻힌다.
#   그래서 ready 가 저장소 종류를 그대로 드러낸다.


@app.get("/health")
def health() -> dict:
    """Liveness. 저장소가 죽어도 200 이다."""
    return {
        "status": "ok",
        "service": "governance-api",
        "api_version": "v1",
        "canonical_db_configured": bool(os.environ.get("DATABASE_URL", "").strip()),
    }


@app.get("/health/ready")
def health_ready() -> dict:
    """Readiness. 승인·Mandate 원장이 durable 저장소인지 드러낸다."""
    durable = type(_mandate_repo).__name__.startswith("Postgres")
    return {
        "status": "ready" if durable else "degraded",
        "service": "governance-api",
        "mandate_store": "postgres" if durable else "in-memory",
        # in-memory 는 프로세스가 죽으면 사라진다. 운영 원장이 아니다.
        "authoritative": durable,
    }


_mandate_repo: object
if os.environ.get("DATABASE_URL") and PostgresMandateVersionRepository is not None:
    _mandate_repo = PostgresMandateVersionRepository.connect(os.environ["DATABASE_URL"])
else:
    _mandate_repo = InMemoryMandateVersionRepository()

mandate_service = MandateVersionService(_mandate_repo)
activation_service = MandateActivationService(_mandate_repo)

if os.environ.get("DATABASE_URL") and PostgresReportRunRepository is not None:
    report_repo = PostgresReportRunRepository.connect(os.environ["DATABASE_URL"])
else:
    report_repo = InMemoryReportRunRepository()
report_assembler = DailyReportAssembler(report_repo)

if os.environ.get("DATABASE_URL") and PostgresNotificationRepository is not None:
    notification_repo = PostgresNotificationRepository.connect(os.environ["DATABASE_URL"])
else:
    notification_repo = InMemoryNotificationRepository()
notification_service = NotificationService(notification_repo)

# GOV-02 1단계 - 승인. Mandate/Report/Notification과 같은 패턴(In-Memory 기본,
# DATABASE_URL 있으면 Postgres)이다. 여기선 In-Memory 대체가 의미 있다 - 승인 상태 전이와
# 권한 분리 규칙 자체는 저장소와 무관한 순수 함수라서 DB 없이도 계약을 검증할 수 있다.
if os.environ.get("DATABASE_URL") and PostgresApprovalRepository is not None:
    approval_repo = PostgresApprovalRepository.connect(os.environ["DATABASE_URL"])
else:
    approval_repo = InMemoryApprovalRepository()

if os.environ.get("DATABASE_URL") and PostgresCaseRepository is not None:
    case_repo = PostgresCaseRepository.connect(os.environ["DATABASE_URL"])
else:
    case_repo = InMemoryCaseRepository()

if os.environ.get("DATABASE_URL") and PostgresEscalationRepository is not None:
    escalation_repo = PostgresEscalationRepository.connect(os.environ["DATABASE_URL"])
else:
    escalation_repo = InMemoryEscalationRepository()

if os.environ.get("DATABASE_URL") and PostgresCommitteeRepository is not None:
    committee_repo = PostgresCommitteeRepository.connect(os.environ["DATABASE_URL"])
else:
    committee_repo = InMemoryCommitteeRepository()

# P0-1(2026-08-05, TEAM_YOUNGJU_CEO_HR_GUIDE.md v2.0) - actor_user_id 실재성 검증.
# 로컬 고정 데모 identity가 governance.user_profiles에 실재하고 ACTIVE인지 확인한다.
if os.environ.get("DATABASE_URL") and PostgresActorIdentityRepository is not None:
    actor_identity_repo = PostgresActorIdentityRepository.connect(os.environ["DATABASE_URL"])
else:
    actor_identity_repo = InMemoryActorIdentityRepository()

# HITL Mandate 변경 워크플로 (change_workflow.py) - 위 5개 Repository/Service를 그대로
# 재사용한다. 별도 저장소를 새로 만들지 않는다 - "승인 대기의 진실은 governance.approvals다"
# 라는 그 모듈의 설계상, 이 오케스트레이터는 "실행이 어디서 멈췄는지"만 checkpointer에 둔다.
#
# checkpointer는 프로세스 생애주기 동안 하나만 만들어 재사용한다 - 매 요청마다 새로
# PostgresSaver를 만들면 그 사이의 interrupt() 재개 스레드를 잃는다(change_workflow.py
# 모듈 docstring "checkpointer는 생성자 필수 인자다" 참고). psycopg(v3)는 prepare_threshold=
# None으로 연다 - Supabase Pooler가 transaction 모드(6543 포트)면 서버사이드 prepared
# statement가 풀링된 연결 사이에서 충돌한다(known psycopg3+PgBouncer 비호환, 자체 점검에서
# 실측).
if os.environ.get("DATABASE_URL") and PostgresSaver is not None:
    _checkpoint_conn = psycopg.connect(
        os.environ["DATABASE_URL"], autocommit=True, prepare_threshold=None, row_factory=dict_row,
    )
    _mandate_checkpointer: object = PostgresSaver(_checkpoint_conn)
    _mandate_checkpointer.setup()
else:
    _mandate_checkpointer = InMemorySaver()

mandate_change_workflow = MandateChangeWorkflow(
    version_repo=_mandate_repo, version_service=mandate_service,
    activation_service=activation_service, approval_repo=approval_repo, case_repo=case_repo,
    checkpointer=_mandate_checkpointer,
)


# --- F24 Notification Event Consumer (governance_events/worker.py가 이 함수들을 쓴다) --------
#
# notification-worker는 아직 실제 Producer가 없다 - risk.breach.v1/qa.finding.v1/
# incident.opened.v1/governance.escalation.v1/report.ready.v1 어느 것도 현재 다른 본부가
# 발행하지 않는다(2026-08-03 확인. 유일하게 실제 발행되는 건 risk-api의 risk.decision.v1인데,
# 그건 §6.1이 정한 CEO Notification의 입력이 아니다 - 임의로 갖다 붙이지 않는다). 그래도
# 배관(Redis Streams + Consumer Group + dedup + ACK)은 risk/qa와 같은 방식으로 실제로
# 구현하고 검증한다 - Producer가 생기면 바로 동작한다.
_EVENT_DEFAULT_SEVERITY: dict[str, str] = {
    "risk.breach.v1": "HIGH",
    "qa.finding.v1": "MEDIUM",
    "incident.opened.v1": "CRITICAL",
    "governance.escalation.v1": "HIGH",
    "report.ready.v1": "LOW",
}

# TODO(영주): 실제 수신자 Routing Table이 아직 없다(부서·역할별 실제 수신자 설계 전) -
# 임시로 고정값을 쓴다. 실제 Routing Table이 생기면 event_type/payload 기준으로 교체한다.
_PLACEHOLDER_RECIPIENT = "role:ceo-ops"

# hf:governance는 CEO Office가 발행하는 Mandate/Report Event를 다른 본부(Risk 등)도 같이
# 구독하는 공용 Stream이다(§8.3 - Consumer Group마다 같은 Stream의 전체 메시지를 받는다).
# 즉 이 notification-worker의 Consumer Group에도 "알림 대상이 아닌" Event가 그대로
# 들어온다 - _handle_governance_event가 이걸 "모르는 Event"로 착각해 예외를 내고 무한
# 재시도에 빠지면 안 되므로, 알려진 비-알림 Event는 조용히 넘긴다(ACK만, notify 없음).
_KNOWN_NON_NOTIFICATION_EVENTS: frozenset[str] = frozenset({
    "governance.mandate.changed.v1",
    "governance.case.created.v1",
    "governance.decision.v1",
    "governance.capital_allocation.v1",
})

_governance_event_bus_instance: RedisEventBus | None = None


def _governance_event_bus() -> RedisEventBus | None:
    """hf:governance Redis Stream을 실제 호출 시점에만 연결한다."""

    global _governance_event_bus_instance
    redis_url = os.environ.get("GOVERNANCE_EVENT_REDIS_URL") or os.environ.get("REDIS_URL")
    if not redis_url:
        return None
    if _governance_event_bus_instance is None:
        import redis

        try:
            dedupe_ttl_seconds = int(
                os.environ.get("GOVERNANCE_EVENT_DEDUPE_TTL_SECONDS", "604800")
            )
        except ValueError as exc:
            raise GovernanceEventBusError(
                "GOVERNANCE_EVENT_DEDUPE_TTL_SECONDS must be an integer"
            ) from exc

        _governance_event_bus_instance = RedisEventBus(
            redis.Redis.from_url(redis_url),
            stream=os.environ.get("GOVERNANCE_EVENT_STREAM", _GOVERNANCE_EVENT_STREAM),
            group=os.environ.get("GOVERNANCE_EVENT_GROUP", _GOVERNANCE_EVENT_GROUP),
            consumer=os.environ.get("GOVERNANCE_EVENT_CONSUMER", "governance-api"),
            dedupe_ttl_seconds=dedupe_ttl_seconds,
        )
    return _governance_event_bus_instance


def _handle_governance_event(event: dict) -> None:
    """Domain Event 하나를 NotificationService.notify() 호출로 변환한다.

    여기엔 판정 로직이 없다 - 심각도 기본값 매핑과 필수 필드 추출만 하고, 실제 채널·억제
    판정은 notification.py의 NotificationService가 그대로 한다 (CLAUDE.md 원칙: 규칙 판정은
    결정론적 코드가 하고, 이 함수는 그 앞단 배선일 뿐).
    """

    event_type = event.get("event_type")
    if event_type in _KNOWN_NON_NOTIFICATION_EVENTS:
        return  # 다른 Consumer Group을 위한 Event - 조용히 넘긴다(ACK, notify 없음).
    if event_type not in _EVENT_DEFAULT_SEVERITY:
        raise GovernanceEventBusError(
            f"governance Notification Consumer가 모르는 Event입니다: {event_type}"
        )
    payload = event.get("payload") or {}
    fund_id = payload.get("fund_id")
    scope_key = payload.get("scope_key")
    if not fund_id or not scope_key:
        raise GovernanceEventBusError(
            f"{event_type} payload에 fund_id/scope_key가 없습니다 (event_id={event.get('event_id')})"
        )

    request = NotificationRequest(
        fund_id=fund_id,
        event_type=event_type,
        scope_key=scope_key,
        recipient=payload.get("recipient", _PLACEHOLDER_RECIPIENT),
        payload=payload,
        severity=payload.get("severity", _EVENT_DEFAULT_SEVERITY[event_type]),
    )
    occurred_at = event.get("occurred_at")
    now = datetime.fromisoformat(occurred_at) if occurred_at else datetime.now(timezone.utc)
    notification_service.notify(request, now=now)


_logger = logging.getLogger("governance-api")


def _publish_governance_event(*, event_type: str, trace_id: str, payload: dict) -> None:
    """Mandate/Report Domain Event를 hf:governance Stream에 발행한다 (Best-effort).

    Risk의 RedisEventPublisher(risk_events/redis_event_bus.py)는 발행 실패를 호출자에게
    전달해 API가 성공 응답을 못 내게 한다 - QA로 가는 감사 이력을 잃으면 안 되기 때문이다.
    여기는 의도적으로 다르게 간다: Mandate/Report의 Canonical Source of Truth는 이미
    Postgres에 안전하게 커밋된 뒤이고(Repository.insert가 먼저 성공), 이 발행은 그 사실을
    다른 본부에 알리는 부가 채널일 뿐이다. Redis가 없거나 장애여도 governance-api 자체의
    핵심 기능(Mandate 저장·활성화, Report 조립)을 막지 않는다 - master plan §13 "Redis 장애:
    Outbox 적재 유지, 비동기 전파 중단"과 같은 방향(전파만 지연되고 원장은 안전).
    실제 outbox_events 테이블은 아직 없어 여기서 직접 발행한다 - Postgres commit과 Redis
    publish가 하나의 Transaction은 아니므로, 이 둘 사이에 죽으면 Event가 유실될 수 있다는
    한계는 있다(진짜 Outbox 패턴으로 옮기기 전까지의 임시 배선).
    """

    bus = _governance_event_bus()
    if bus is None:
        return
    try:
        bus.publish(event_id=uuid.uuid4(), event_type=event_type, trace_id=trace_id, payload=payload)
    except GovernanceEventBusError:
        _logger.exception("governance Domain Event 발행 실패 (event_type=%s)", event_type)


@app.exception_handler(GovernanceEventBusError)
def _on_governance_event_bus_error(request, exc: GovernanceEventBusError):
    return JSONResponse(status_code=503, content={
        "error_code": "GOVERNANCE_EVENT_BUS_ERROR", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(CurrencyMismatchError)
def _on_currency_mismatch(request, exc: CurrencyMismatchError):
    return JSONResponse(status_code=400, content={
        "error_code": "MANDATE_CURRENCY_MISMATCH", "message": str(exc), "detail": {}, "trace_id": None,
    })


# GOV-02 승인 도메인 예외 -> HTTP. 어느 쪽도 200으로 새지 않는다(approval.py 불변식 1).
#   권한 없음 403 / 만료·중복결정 409 / OWNER 미지원 501
@app.exception_handler(UnauthorizedDeciderError)
def _on_unauthorized_decider(request, exc: UnauthorizedDeciderError):
    return JSONResponse(status_code=403, content={
        "error_code": "UnauthorizedDeciderError", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(ApprovalExpiredError)
def _on_approval_expired(request, exc: ApprovalExpiredError):
    return JSONResponse(status_code=409, content={
        "error_code": "ApprovalExpiredError", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(AlreadyDecidedError)
def _on_already_decided(request, exc: AlreadyDecidedError):
    return JSONResponse(status_code=409, content={
        "error_code": "AlreadyDecidedError", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(OwnerApprovalNotSupportedError)
def _on_owner_approval_unsupported(request, exc: OwnerApprovalNotSupportedError):
    return JSONResponse(status_code=501, content={
        "error_code": "OwnerApprovalNotSupportedError", "message": str(exc), "detail": {},
        "trace_id": None,
    })


@app.exception_handler(MissingActorUserError)
def _on_missing_actor_user(request, exc: MissingActorUserError):
    return JSONResponse(status_code=400, content={
        "error_code": "MissingActorUserError", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(UnverifiedActorUserError)
def _on_unverified_actor_user(request, exc: UnverifiedActorUserError):
    return JSONResponse(status_code=403, content={
        "error_code": "UnverifiedActorUserError", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(IllegalCaseTransition)
def _on_illegal_case_transition(request, exc: IllegalCaseTransition):
    return JSONResponse(status_code=409, content={
        "error_code": "IllegalCaseTransition", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(IllegalEscalationTransition)
def _on_illegal_escalation_transition(request, exc: IllegalEscalationTransition):
    return JSONResponse(status_code=409, content={
        "error_code": "IllegalEscalationTransition", "message": str(exc), "detail": {},
        "trace_id": None,
    })


@app.exception_handler(MissingResolutionError)
def _on_missing_resolution(request, exc: MissingResolutionError):
    return JSONResponse(status_code=409, content={
        "error_code": "MissingResolutionError", "message": str(exc), "detail": {}, "trace_id": None,
    })


# 위원회(Y2) 예외 -> HTTP.
#   quorum_policy 형태 오류 400 / OPEN 아닌 세션 조작 409 / 중복 투표 409 / SoD 위반 403
@app.exception_handler(InvalidQuorumPolicyError)
def _on_invalid_quorum_policy(request, exc: InvalidQuorumPolicyError):
    return JSONResponse(status_code=400, content={
        "error_code": "InvalidQuorumPolicyError", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(IllegalSessionTransition)
def _on_illegal_session_transition(request, exc: IllegalSessionTransition):
    return JSONResponse(status_code=409, content={
        "error_code": "IllegalSessionTransition", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(DuplicateVoteError)
def _on_duplicate_vote(request, exc: DuplicateVoteError):
    return JSONResponse(status_code=409, content={
        "error_code": "DuplicateVoteError", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(SelfReviewError)
def _on_self_review(request, exc: SelfReviewError):
    return JSONResponse(status_code=403, content={
        "error_code": "SelfReviewError", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(FundNotFoundError)
def _on_fund_not_found(request, exc: FundNotFoundError):
    return JSONResponse(status_code=404, content={
        "error_code": "FUND_NOT_FOUND", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(MandateAlreadyExistsError)
def _on_mandate_already_exists(request, exc: MandateAlreadyExistsError):
    """`unique (fund_id, name)` 충돌. 409 + 기존 mandate_id.

    200으로 기존 Mandate를 돌려주지 않는다 - 호출자가 "새로 만들었다"와 "이미
    있었다"를 구분해야 한다(온보딩 버튼 중복 클릭과 이름 충돌은 서버가 구분할 수
    없다). `detail.mandate_id`를 실어 재조회 없이 이어갈 수 있게 한다.
    """
    return JSONResponse(status_code=409, content={
        "error_code": "MANDATE_ALREADY_EXISTS", "message": str(exc),
        "detail": {"mandate_id": exc.mandate_id}, "trace_id": None,
    })


@app.exception_handler(ValueError)
def _on_value_error(request, exc: ValueError):
    return JSONResponse(status_code=400, content={
        "error_code": "MANDATE_CONTRADICTORY_BOUNDS", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(MandatePersistenceError)
def _on_mandate_persistence_error(request, exc: MandatePersistenceError):
    return JSONResponse(status_code=409, content={
        "error_code": "MANDATE_PERSISTENCE_ERROR", "message": str(exc), "detail": {}, "trace_id": None,
    })


# P0-2(2026-08-05) - 이전엔 핸들러가 없어 DB 오류가 원시 스택트레이스 500으로 새나갔다.
# "의존 서비스 오류는 BLOCKED/ESCALATE다"(TEAM_YOUNGJU_CEO_HR_GUIDE.md P0-2) 원칙대로
# 503로 닫는다 - 발송 성공으로 오인될 수 있는 200을 절대 내지 않는다.
@app.exception_handler(NotificationPersistenceError)
def _on_notification_persistence_error(request, exc: NotificationPersistenceError):
    return JSONResponse(status_code=503, content={
        "error_code": "NOTIFICATION_PERSISTENCE_ERROR", "message": str(exc), "detail": {},
        "trace_id": None,
    })


# HITL Mandate 변경 워크플로(change_workflow.py) 예외 -> HTTP.
#   종료된 Case 재advance 409 / MANDATE_CHANGE 아닌·없는 case_id 404 /
#   submit()이 만들었어야 할 RISK·QA 승인 행 누락(데이터 정합성 문제) 500
@app.exception_handler(CaseAlreadyResolvedError)
def _on_case_already_resolved(request, exc: CaseAlreadyResolvedError):
    return JSONResponse(status_code=409, content={
        "error_code": "CaseAlreadyResolvedError", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(NotAMandateChangeCaseError)
def _on_not_a_mandate_change_case(request, exc: NotAMandateChangeCaseError):
    return JSONResponse(status_code=404, content={
        "error_code": "NotAMandateChangeCaseError", "message": str(exc), "detail": {}, "trace_id": None,
    })


@app.exception_handler(ReviewApprovalMissingError)
def _on_review_approval_missing(request, exc: ReviewApprovalMissingError):
    return JSONResponse(status_code=500, content={
        "error_code": "ReviewApprovalMissingError", "message": str(exc), "detail": {}, "trace_id": None,
    })


# --- 2.1 Mandate ----------------------------------------------------------------


class CreateMandateRequest(BaseModel):
    """`governance.mandates` 부모 행 생성 요청.

    정책 metadata는 여기서 만들지 않는다 - 생성 후 `PUT /mandates/{id}`로
    부모 행에 덮어쓴다. 기존 승인·변경 워크플로의 `POST .../versions`는
    레거시 경로로 유지한다.
    껍데기와 정책을 한 호출로 묶으면 정책 검증 실패 때 부모 행만 남는다.
    """

    fund_id: str = Field(min_length=1, max_length=128)
    owner_user_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)


@app.post("/governance/v1/mandates", status_code=201)
def create_mandate(body: CreateMandateRequest):
    """온보딩 최초 Mandate를 만든다. 2026-08-12 신설.

    그 전까지 `governance.mandates` INSERT는 `change_workflow.py` 자체 점검 코드
    안에만 있어서 **최초 Mandate를 만들 API 경로가 없었다** — Version을 제안하는
    모든 경로가 `mandate_id`를 path로 받으므로 첫 사용자는 시작할 수 없었다.

    반환 상태는 `DRAFT`/`current_version=0`이다(DDL 기본값). 정책이 아직 없으니
    그게 정확한 상태이고, 여기서 `ACTIVE`로 만들면 정책 없는 Mandate가 활성으로
    보인다.
    """

    creator = getattr(_mandate_repo, "create_mandate", None)
    if not callable(creator):  # pragma: no cover - Postgres Repository에는 있다
        raise HTTPException(status_code=503, detail="mandate_create_unavailable")
    try:
        mandate_id = creator(
            fund_id=body.fund_id,
            owner_user_id=body.owner_user_id,
            name=body.name,
        )
    except NotImplementedError as exc:
        # In-Memory Repository는 이 경로를 구현하지 않는다. 조용히 메모리에 만들면
        # 재기동 때 사라지고 사용자는 온보딩을 다시 해야 한다(개발 원칙 9).
        raise HTTPException(status_code=503, detail="mandate_create_unavailable") from exc

    _publish_governance_event(
        event_type="governance.mandate.changed.v1",
        trace_id=str(uuid.uuid4()),
        payload={
            # mandate_id를 fund_id 자리에 쓰지 않는다 - 이 Event는 실제 fund_id를
            # 알고 있다(propose_version의 주석 참고: 거기선 몰라서 mandate_id를 썼다).
            "fund_id": body.fund_id,
            "scope_key": f"mandate:{mandate_id}",
            "action": "CREATED",
            "mandate_id": mandate_id,
            "owner_user_id": body.owner_user_id,
            "name": body.name,
        },
    )
    return {
        "mandate_id": mandate_id,
        "fund_id": body.fund_id,
        "owner_user_id": body.owner_user_id,
        "name": body.name,
        "status": "DRAFT",
        "current_version": 0,
    }


@app.put("/governance/v1/mandates/{mandate_id}")
def replace_mandate_metadata(mandate_id: str, body: ReplaceMandateMetadataRequest):
    """Reject the retired unversioned write path."""
    raise HTTPException(
        status_code=409,
        detail={
            "error_code": "MANDATE_VERSION_REQUIRED",
            "message": "Use POST /governance/v1/mandates/{id}/versions",
        },
    )


@app.post("/governance/v1/mandates/{mandate_id}/versions")
def propose_version(mandate_id: str, body: ProposeVersionRequest):
    """Version을 만들고 그 자리에서 활성화까지 한다 - Risk/QA Case 없음.

    2026-08-13(F01 방향 A): 이 저장 경로는 `change_workflow.py`의 HITL Case
    승인을 거치지 않는다. 대신 화면에서 사용자가 직접 '지침 저장'을 눌러
    이 요청을 보냈다는 사실 자체를 사용자 승인으로 본다 - `lifecycle.py`의
    `activate()`가 요구하는 `UserApproval`을 매 호출마다 합성해서 넘긴다.

    `created_by`가 필수인 이유: 이게 곧 `mandate_decisions.approved_by`가 된다.
    없이 활성화하면 "누가 승인했는가"가 없는 감사 기록이 생긴다(개발 원칙 9 -
    위험한 기능은 실패 시 확대가 아니라 차단 방향. 여기서는 활성화를 막는 게
    안전 방향이다).

    `activate()`는 `is_initial`이든 `direction=LOOSEN`이든 승인이 있으면 항상
    활성화한다(`lifecycle.py` 참고) - 그래서 이 경로는 매 제출을 예외 없이
    최신 활성 Version으로 만든다. `change_workflow.py`의 Risk/QA 검토 경로는
    그대로 남아 있고, 이 Route가 대체하지 않는다.
    """
    if not body.created_by:
        raise HTTPException(
            status_code=422,
            detail="created_by is required - it becomes the approver of record when this save activates the version",
        )

    if body.fund_base_currency and isinstance(_mandate_repo, InMemoryMandateVersionRepository):
        _mandate_repo.set_fund_base_currency(mandate_id, body.fund_base_currency)

    result = mandate_service.propose_version(
        mandate_id=mandate_id, policy=body.policy, objective_text=body.objective_text,
        objective=body.objective, effective_from=body.effective_from,
        previous_policy=body.previous_policy, effective_to=body.effective_to,
        execution_rules=body.execution_rules, created_by=body.created_by,
    )
    # MandateVersionRow/VersionResult에 trace_id가 없다(Mandate 도메인이 아직 그 개념을
    # 안 갖고 있다) - 이 Event 하나만의 trace_id를 새로 만든다(Mandate 자체의 trace가 아님).
    _publish_governance_event(
        event_type="governance.mandate.changed.v1",
        trace_id=str(uuid.uuid4()),
        payload={
            # mandate_id를 fund_id 자리에 쓴다 - 이 Event는 _KNOWN_NON_NOTIFICATION_EVENTS라
            # notification-worker가 fund_id를 실제로 읽지 않는다(§6.1 다른 Consumer 몫).
            # 진짜 accounting.funds.fund_id 매핑이 필요해지면(예: 알림 대상으로 승격) 그때 바꾼다.
            "fund_id": mandate_id,
            "scope_key": f"mandate:{mandate_id}",
            "action": "PROPOSED",
            "mandate_id": mandate_id,
            "version": result.row.version,
            "direction": result.direction.value,
            "requires_user_reapproval": result.requires_user_reapproval,
            "content_hash": result.row.content_hash,
        },
    )

    activation: ActivationResult = activation_service.activate(
        mandate_id=mandate_id,
        version=result.row.version,
        direction=result.direction,
        at=body.effective_from,
        approval=UserApproval(
            approved_by=body.created_by,
            trace_id=str(uuid.uuid4()),
            reason="지침 저장 제출 - Risk/QA Case 없이 사용자 본인 제출을 승인으로 간주",
        ),
    )
    if activation.activated:
        _publish_governance_event(
            event_type="governance.mandate.changed.v1",
            trace_id=activation.decision.trace_id if activation.decision else str(uuid.uuid4()),
            payload={
                "fund_id": mandate_id,
                "scope_key": f"mandate:{mandate_id}",
                "action": "ACTIVATED",
                "mandate_id": mandate_id,
                "version": result.row.version,
                "direction": result.direction.value,
                "decided_by": activation.decision.approved_by if activation.decision else None,
            },
        )

    return {
        "mandate_id": mandate_id, "version": result.row.version,
        "mandate_version_id": _mandate_repo.get_mandate_version_id(
            mandate_id, result.row.version
        ),
        "direction": result.direction.value, "requires_user_reapproval": result.requires_user_reapproval,
        "content_hash": result.row.content_hash,
        "activated": activation.activated,
        "activation_trace_id": (
            activation.decision.trace_id if activation.decision else None
        ),
    }


@app.post("/governance/v1/mandates/{mandate_id}/versions/{version}/activate")
def activate_version(mandate_id: str, version: int, body: ActivateRequest):
    approval = None
    if body.approval is not None:
        approval = UserApproval(approved_by=body.approval.approved_by, trace_id=body.approval.trace_id,
                                 reason=body.approval.reason)
    result: ActivationResult = activation_service.activate(
        mandate_id=mandate_id, version=version, direction=body.direction, at=body.at, approval=approval,
    )
    if not result.activated:
        return {"activated": False, "direction": result.direction.value, "blocked_reason": result.blocked_reason}

    _publish_governance_event(
        event_type="governance.mandate.changed.v1",
        trace_id=result.decision.trace_id if result.decision else str(uuid.uuid4()),
        payload={
            "fund_id": mandate_id,  # _handle_governance_event에서 fund_id를 안 쓰는 Event (위 주석 참고)
            "scope_key": f"mandate:{mandate_id}",
            "action": "ACTIVATED",
            "mandate_id": mandate_id,
            "version": version,
            "direction": result.direction.value,
            "decided_by": result.decision.approved_by if result.decision else None,
        },
    )
    return {
        "activated": True, "direction": result.direction.value,
        "decision": {"decision": result.decision.decision, "approved_by": result.decision.approved_by,
                     "trace_id": result.decision.trace_id, "reason": result.decision.reason} if result.decision else None,
    }


@app.get(
    "/governance/v1/mandates/{mandate_id}/current",
    dependencies=[Depends(_require_internal_auth)],
)
def get_mandate_current(mandate_id: str):
    """Return the canonical binding and the available Mandate snapshot.

    The portfolio BFF consumes ``mandate_version_id`` and ``policy_hash`` as
    the binding contract.  The user-input API also needs the policy itself,
    so keep both contracts in one response instead of making callers choose
    between two incompatible shapes.  Older repository implementations may
    not expose ``get`` yet; in that case the canonical binding remains
    available and an ACTIVE mandate fails closed when its binding is absent.

    성능(2026-08-14): Postgres 구현이 있으면 `get_mandate_current_snapshot()` 한 번의
    JOIN 왕복으로 아래 4단계(각자 별도 커넥션 체크아웃)를 대신한다 - 없거나
    `None`을 돌려주면(판단 유보) 기존 4단계 경로로 그대로 떨어진다. 응답 모양은
    두 경로가 동일해야 하고, `postgres_repository.py`의 `get_mandate_current_snapshot`
    docstring에 그 계약이 적혀 있다.
    """
    # Historical unversioned UI metadata is readable for migration only.  A
    # subsequently activated Version always supersedes it.
    get_access_context = getattr(
        _mandate_repo, "get_mandate_access_context", None
    )
    access_context = (
        get_access_context(mandate_id) if callable(get_access_context) else None
    ) or {}
    canonical_version, canonical_status = _mandate_repo.get_mandate_current(mandate_id)
    get_metadata = getattr(_mandate_repo, "get_mandate_metadata", None)
    metadata = get_metadata(mandate_id) if callable(get_metadata) else None
    if metadata and canonical_version <= 0:
        policy = metadata.get("policy")
        return {
            "mandate_id": mandate_id,
            **access_context,
            "case_id": None,
            "current_version": 0,
            "mandate_version_id": None,
            "policy_hash": metadata.get("content_hash"),
            "status": "REQUIRES_USER_REVIEW",
            "effective_from": None,
            "effective_to": None,
            "content_hash": metadata.get("content_hash"),
            "objective_text": metadata.get("objective_text", ""),
            "objective": metadata.get("objective", {}),
            "policy": policy,
            "metadata": metadata,
        }

    # metadata override가 없을 때만 아래 legacy version 경로를 탄다 - 그 경로
    # 안에서 fast path(성능 최적화, 2026-08-14)를 먼저 시도한다.
    fast_snapshot = getattr(_mandate_repo, "get_mandate_current_snapshot", None)
    if callable(fast_snapshot):
        response = fast_snapshot(mandate_id)
        if response is not None:
            if response["status"] == "ACTIVE" and (
                not response.get("mandate_version_id") or not response.get("policy_hash")
            ):
                raise HTTPException(status_code=503, detail="canonical_mandate_binding_unavailable")
            return {**access_context, **response}

    version, status = canonical_version, canonical_status
    if version <= 0:
        return {
            "mandate_id": mandate_id,
            **access_context,
            "current_version": version,
            "status": status,
        }
    mandate_version_id = (
        _mandate_repo.get_mandate_version_id(mandate_id, version)
        if version > 0 else None
    )
    policy_hash = (
        _mandate_repo.get_mandate_content_hash(mandate_id, version)
        if version > 0 else None
    )
    if status == "ACTIVE" and (not mandate_version_id or not policy_hash):
        raise HTTPException(status_code=503, detail="canonical_mandate_binding_unavailable")
    response = {
        "mandate_id": mandate_id,
        **access_context,
        "case_id": None,
        "current_version": version,
        "mandate_version_id": mandate_version_id,
        "policy_hash": policy_hash,
        "status": status,
    }

    get_version = getattr(_mandate_repo, "get", None)
    row = get_version(mandate_id, version) if callable(get_version) else None
    if row is not None:
        response.update({
            "effective_from": row.effective_from.isoformat(),
            "effective_to": row.effective_to.isoformat() if row.effective_to else None,
            "content_hash": row.content_hash,
            "objective_text": row.objective_text,
            "objective": row.objective,
            "policy": {
                "allowed_assets": row.allowed_assets,
                "forbidden_assets": row.forbidden_assets,
                "risk_bounds": row.risk_bounds,
                "universe_policy": row.universe_policy,
                "approval_rules": row.approval_rules,
                "execution_rules": row.execution_rules,
            },
        })
    return response


@app.get(
    "/governance/v1/mandates/by-fund/{fund_id}/current",
    dependencies=[Depends(_require_internal_auth)],
)
def get_mandate_current_by_fund(
    fund_id: str,
    owner_user_id: str | None = None,
):
    """Resolve a Fund to exactly one Mandate without choosing arbitrarily.

    ``mandate_ids_for_fund`` is supplied by the canonical repository.  The
    explicit 503 fallback keeps older prototype repositories fail-closed
    instead of silently returning a stale or guessed Mandate.
    """
    lookup_name = (
        "mandate_ids_for_fund_owner"
        if owner_user_id
        else "mandate_ids_for_fund"
    )
    lookup = getattr(_mandate_repo, lookup_name, None)
    if not callable(lookup):
        raise HTTPException(status_code=503, detail="mandate_fund_lookup_unavailable")
    mandate_ids = (
        lookup(fund_id, owner_user_id)
        if owner_user_id
        else lookup(fund_id)
    )
    if not mandate_ids:
        raise HTTPException(
            status_code=404,
            detail=f"fund_id={fund_id}에 연결된 Mandate가 없습니다",
        )
    if len(mandate_ids) > 1:
        raise HTTPException(
            status_code=409,
            detail=(
                f"fund_id={fund_id}에 Mandate가 {len(mandate_ids)}개 있어 단일 조회가 모호합니다: "
                f"{mandate_ids}"
            ),
        )
    return get_mandate_current(mandate_ids[0])


@app.get(
    "/governance/v1/users/{user_id}/fund",
    dependencies=[Depends(_require_internal_auth)],
)
def get_fund_for_user(user_id: str):
    """`user_id -> fund_id` 역참조. 2026-08-18 추가.

    ## 왜 필요한가

    이 경로가 없어서 **프론트엔드가 `fund_id`를 계정과 쌍으로 하드코딩**해
    요청 body에 실어 보내고 있었다(`ai-office/app/lib/currentAccount.ts`,
    `apps/api/ceo.py`의 `CeoAsk.fund_id`). Discord 작성자 매핑표
    (`apps/api/discord_actor_map.py`)도 같은 이유로 fund를 함께 적어야 했다.
    `governance.fund_memberships`가 0건이라 서버가 알 방법이 없었기 때문이고,
    2026-08-18 seed로 소유 관계가 채워지면서 조회가 가능해졌다.

    ## 왜 단수(`/fund`)인가

    호출자가 원하는 건 "이 사용자의 Fund 하나"다. 여러 개면 어느 것의 Mandate로
    판단할지가 모호하므로 **임의로 고르지 않고 409로 닫는다** -
    `GET .../mandates/by-fund/{fund_id}/current`와 같은 규약이다. 목록이 필요한
    화면이 생기면 그때 별도 복수형 경로를 만든다.

    ## 상태 코드

    | 상황 | 응답 |
    |---|---|
    | 소유 Fund 1개 | 200 `{"user_id", "fund_id"}` |
    | 0개 | 404 - "이 사용자는 아직 Fund가 없다"가 정확한 사실이다 |
    | 2개 이상 | 409 - 모호. 임의 선택 금지 |
    | Repository가 역참조 미지원(구형 Prototype) | 503 - fail-closed |
    """
    lookup = getattr(_mandate_repo, "fund_ids_for_user", None)
    if not callable(lookup):
        # 조용히 빈 값을 주면 호출자는 "Fund 없음"과 "조회 불가"를 구분할 수 없다.
        raise HTTPException(status_code=503, detail="user_fund_lookup_unavailable")
    fund_ids = lookup(user_id)
    if not fund_ids:
        raise HTTPException(
            status_code=404, detail=f"user_id={user_id}에 연결된 Fund가 없습니다"
        )
    if len(fund_ids) > 1:
        raise HTTPException(
            status_code=409,
            detail=(
                f"user_id={user_id}가 소유한 Fund가 {len(fund_ids)}개라 단일 조회가 "
                f"모호합니다: {fund_ids}"
            ),
        )
    return {"user_id": user_id, "fund_id": fund_ids[0]}


# --- 2.4 Mandate 온보딩 챗봇 제안 (mandate_assistant.py) ----------------------------
#
# USER_INPUT_API_SPEC.md 2.4. Stateless - 이 endpoint 는 governance.mandate_versions나
# 다른 어떤 테이블도 쓰지 않는다. LLM 실패는 500 으로 전파하지 않고 빈 제안 +
# 안내 reply 로 감싼다(mandate_assistant.py docstring "왜 Schema 위반 시 예외를
# 던지는가" 참고) - 채팅 UI가 LLM 장애 한 번으로 멈추면 안 된다.


@app.post("/governance/v1/mandate-assistant/suggest")
def mandate_assistant_suggest(body: SuggestRequestIn):
    """Stateless Mandate 제안. LLM 장애는 빈 제안으로 fail-closed한다."""
    try:
        result = _mandate_assistant_suggest(
            messages=body.messages, current_draft=body.current_draft,
        )
    except ValueError:
        raise
    except Exception:
        _logger.exception("Mandate assistant 제안 실패 (fund_id=%s)", body.fund_id)
        return {
            "reply": "죄송합니다, 지금은 제안을 만들 수 없습니다. 직접 입력해 주세요.",
            "suggestions": [], "requires_user_confirmation": True, "dropped_fields": [],
        }
    return result.model_dump()


# --- 2.1b HITL Mandate 변경 워크플로 (change_workflow.py) --------------------------
#
# TEAM_YOUNGJU_CEO_HR_GUIDE.md 5.1절 7단계(제출 -> Draft Version -> Risk/QA 검토 ->
# 사용자 승인 -> 활성화)를 오케스트레이션한다. propose_version/activate_version(위 2.1)을
# 대체하지 않는다 - 저 둘은 여전히 "Version 하나만 딱 만들고/활성화하고 끝내고 싶을 때" 쓰는
# 저수준 경로이고, 여긴 그 사이의 Risk/QA/사용자 승인 배선까지 묶어서 처리하는 상위 경로다.


def _change_request_dict(r: ChangeRequestResult) -> dict:
    return {
        "stage": r.stage.value, "mandate_id": r.mandate_id, "version": r.version,
        "direction": r.direction.value, "case_id": r.case_id, "detail": r.detail,
    }


@app.post("/governance/v1/mandates/{mandate_id}/change-requests")
def submit_change_request(mandate_id: str, body: SubmitChangeRequestIn):
    """5.1 1단계 — Draft Version 생성 + (필요 시) Risk/QA 동시 승인 요청.

    TIGHTEN/NEUTRAL은 Case 없이 즉시 적용되고 stage=FAST_APPLIED로 끝난다. LOOSEN·최초
    활성화는 Case를 열고 stage=AWAITING_REVIEW로 돌아온다 - 이후 진행은 담당자가
    /approvals/{id}/decide로 결정할 때마다 POST .../cases/{case_id}/advance를 다시
    불러야 한다(재개가 아니라 매번 새로 판단하는 짧은 호출).
    """
    if isinstance(_mandate_repo, InMemoryMandateVersionRepository):
        # The canonical repository records this relation from governance.mandates.
        # Prototype repositories that do not yet expose set_fund_id simply keep
        # the older mandate-change behavior; they must not guess a Fund later.
        set_fund_id = getattr(_mandate_repo, "set_fund_id", None)
        if callable(set_fund_id):
            set_fund_id(mandate_id, body.fund_id)
        if body.fund_base_currency:
            _mandate_repo.set_fund_base_currency(mandate_id, body.fund_base_currency)

    result = mandate_change_workflow.submit(
        mandate_id=mandate_id, fund_id=body.fund_id, policy=body.policy,
        objective_text=body.objective_text, objective=body.objective,
        effective_from=body.effective_from, created_by=body.created_by,
        trace_id=body.trace_id, now=body.now, previous_policy=body.previous_policy,
        priority=body.priority, review_expires_at=body.review_expires_at,
        user_approval_ttl_seconds=body.user_approval_ttl_seconds,
        version_created_by=body.version_created_by,
    )
    _publish_governance_event(
        event_type="governance.mandate.changed.v1", trace_id=body.trace_id,
        payload={
            "fund_id": mandate_id, "scope_key": f"mandate:{mandate_id}",
            "action": result.stage.value, "mandate_id": mandate_id, "version": result.version,
            "direction": result.direction.value, "case_id": result.case_id,
        },
    )
    return _change_request_dict(result)


@app.post("/governance/v1/cases/{case_id}/advance")
def advance_change_request(case_id: str, body: AdvanceCaseIn):
    """5.1 2단계 — 대기 중인 승인(Risk/QA/사용자) 상태를 다시 읽어 다음 단계로 넘긴다.

    상태 변화가 없으면(승인이 아직 PENDING이면) 조회만 하고 아무것도 쓰지 않는다.
    RESOLVED/CANCELLED Case에 다시 부르면 409(CaseAlreadyResolvedError).
    """
    case = case_repo.get(case_id)
    result = mandate_change_workflow.advance(case_id, at=body.at)
    _publish_governance_event(
        event_type="governance.mandate.changed.v1",
        trace_id=case.trace_id if case is not None else str(uuid.uuid4()),
        payload={
            "fund_id": result.mandate_id, "scope_key": f"mandate:{result.mandate_id}",
            "action": result.stage.value, "mandate_id": result.mandate_id,
            "version": result.version, "direction": result.direction.value, "case_id": case_id,
        },
    )
    return _change_request_dict(result)


# --- 4. reporting-api -------------------------------------------------------------


@app.post("/reporting/v1/reports")
def request_report(body: AssembleReportRequest):
    def _ref(r: SnapshotRefIn | None) -> SnapshotRef | None:
        return None if r is None else SnapshotRef(snapshot_id=r.snapshot_id, as_of=r.as_of)

    sections = DailyReportSections(
        portfolio=_ref(body.portfolio), risk=_ref(body.risk), research=_ref(body.research),
        execution=_ref(body.execution), strategy=_ref(body.strategy), qa=_ref(body.qa),
        pending_user_action_case_ids=tuple(body.pending_user_action_case_ids),
    )
    assembly = report_assembler.assemble(
        fund_id=body.fund_id, as_of=body.as_of, template_version=body.template_version,
        sections=sections, trace_id=body.trace_id,
    )
    # created(새 Report)이고 status가 QUEUED(필수 Section 다 있어 실제로 "준비됨")일 때만
    # 발행한다 - FAILED는 "준비"가 아니고, 동일 content_hash 재요청(created=False)은 이미
    # 한 번 발행했으므로 다시 발행하면 다른 본부·notification-worker가 중복 처리한다.
    if assembly.created and assembly.row.status == "QUEUED":
        _publish_governance_event(
            event_type="report.ready.v1",
            trace_id=assembly.row.trace_id,
            payload={
                "fund_id": assembly.row.fund_id,
                "scope_key": f"report:{assembly.row.content_hash}",
                "report_type": assembly.row.report_type,
                "as_of": assembly.row.as_of.isoformat(),
                "content_hash": assembly.row.content_hash,
                "template_version": assembly.row.template_version,
            },
        )
    return {
        "fund_id": assembly.row.fund_id, "type": assembly.row.report_type, "as_of": assembly.row.as_of.isoformat(),
        "status": assembly.row.status, "source_snapshot_ids": list(assembly.row.source_snapshot_ids),
        "template_version": assembly.row.template_version, "content_hash": assembly.row.content_hash,
        "missing_required": list(assembly.missing_required), "created": assembly.created,
    }


@app.get("/reporting/v1/reports/{content_hash}")
def get_report(content_hash: str, fund_id: str):
    row = report_repo.find_by_content_hash(fund_id, content_hash)
    if row is None:
        raise HTTPException(status_code=404, detail=f"fund_id={fund_id}, content_hash={content_hash} Report 없음")
    return {
        "fund_id": row.fund_id, "type": row.report_type, "as_of": row.as_of.isoformat(),
        "status": row.status, "source_snapshot_ids": list(row.source_snapshot_ids),
        "template_version": row.template_version, "content_hash": row.content_hash,
    }


# --- 2.2 Case Root (GOV-02) -------------------------------------------------------
#
# `create_case`는 스펙 2.2 확정 엔드포인트다. 조회·timeline·전이는 스펙에 이름이 없는 제안이다.
# 투자 Case는 여기서 만들지 않는다 - 스펙 2.2가 "투자 Case는 MSU_SPEC 11절
# POST /investment-cases를 쓰고 여기를 복제하지 않는다"고 명시했고, 그 19단계 상태 머신은
# 리서치·트레이딩·회계본부가 담당한다.
#
# `POST /cases/{case_id}/decisions`(스펙 2.2 record_decision)는 넣지 않았다 - 범용
# governance.decisions 테이블이 없고(mandate_decisions/committee_decisions만 있다) 스펙에
# Request 본문도 정의돼 있지 않아 계약을 지어내야 한다. 별도 작업으로 남긴다.


def _case_dict(c: CaseRecord) -> dict:
    return {
        "case_id": c.case_id, "fund_id": c.fund_id, "display_id": c.display_id,
        "case_type": c.case_type, "priority": c.priority, "status": c.status.value,
        "owner_department": c.owner_department, "trace_id": c.trace_id,
        "created_by": c.created_by, "schema_version": c.schema_version,
        "due_at": c.due_at.isoformat() if c.due_at else None,
        "created_at": c.created_at.isoformat(), "updated_at": c.updated_at.isoformat(),
    }


def _case_event_dict(e: CaseEvent) -> dict:
    return {
        "sequence": e.sequence, "event_type": e.event_type,
        "from_status": e.from_status.value if e.from_status else None,
        "to_status": e.to_status.value, "producer": e.producer, "actor": e.actor,
        "reason": e.reason, "payload": e.payload or {},
        "occurred_at": e.occurred_at.isoformat(), "schema_version": e.schema_version,
    }


def _load_case(case_id: str) -> CaseRecord:
    case = case_repo.get(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"case {case_id} 없음")
    return case


@app.post("/governance/v1/cases")
def create_case(body: CreateCaseIn):
    """Case를 OPEN으로 만든다. cases 행과 case_events 첫 줄이 한 트랜잭션으로 들어간다."""
    now = datetime.now(timezone.utc)
    display_id = build_display_id(
        body.case_type, created_at=now,
        sequence=case_repo.next_display_sequence(body.case_type, now),
    )
    case, event = open_case_root(
        case_id=str(uuid.uuid4()), fund_id=body.fund_id, display_id=display_id,
        case_type=body.case_type, priority=body.priority,
        owner_department=body.owner_department, trace_id=body.trace_id,
        created_by=body.created_by, created_at=now,
        idempotency_key=body.idempotency_key or str(uuid.uuid4()),
        due_at=body.due_at, reason=body.reason, payload=body.payload,
    )
    case_repo.save_new(case, event)
    _publish_governance_event(
        event_type="governance.case.created.v1", trace_id=body.trace_id,
        payload={"case_id": case.case_id, "display_id": case.display_id,
                 "case_type": case.case_type, "owner_department": case.owner_department,
                 "status": case.status.value},
    )
    return _case_dict(case)


@app.get("/governance/v1/cases/{case_id}")
def get_case(case_id: str):
    return _case_dict(_load_case(case_id))


@app.get("/governance/v1/cases/{case_id}/timeline")
def get_case_timeline(case_id: str):
    """변경 이력의 기준은 case_events다 (MSU_SPEC 12절). cases는 현재 상태 Projection."""
    _load_case(case_id)  # 없는 case_id면 404
    return {"events": [_case_event_dict(e) for e in case_repo.timeline(case_id)]}


@app.post("/governance/v1/cases/{case_id}/transitions")
def transition_case_endpoint(case_id: str, body: CaseTransitionIn):
    """상태를 바꾸고 case_events에 한 줄 남긴다. Terminal 이후 전이는 409."""
    case = _load_case(case_id)
    updated, event = transition_case(
        case, to_status=body.to_status, actor=body.actor, at=body.at,
        next_sequence=case_repo.next_sequence(case_id),
        idempotency_key=body.idempotency_key or str(uuid.uuid4()),
        reason=body.reason, payload=body.payload,
    )
    case_repo.apply_transition(updated, event)
    return _case_dict(updated)


# --- 2.2 Escalation (GOV-02) ------------------------------------------------------
#
# 상태 값을 새로 정할 필요가 없었다 - DDL에 severity/status 허용 값이 이미 있다.
# 스펙 5.1절이 "risk-api — Trading State/Breach | CEO | Incident·Escalation"이라 했으므로
# 리스크본부의 breach가 CEO로 올라오는 수신 경로이기도 하다. 생성 시 스펙 5.3절의
# governance.escalation.v1을 발행해 담당 본부와 QA가 Owner·기한을 추적할 수 있게 한다.


def _escalation_dict(e: EscalationRecord) -> dict:
    return {
        "escalation_id": e.escalation_id, "case_id": e.case_id, "reason": e.reason,
        "severity": e.severity.value, "target": e.target, "status": e.status.value,
        "resolution": e.resolution,
        "due_at": e.due_at.isoformat() if e.due_at else None,
        "created_at": e.created_at.isoformat(),
        "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
    }


def _load_escalation(escalation_id: str) -> EscalationRecord:
    escalation = escalation_repo.get(escalation_id)
    if escalation is None:
        raise HTTPException(status_code=404, detail=f"escalation {escalation_id} 없음")
    return escalation


@app.post("/governance/v1/escalations")
def create_escalation(body: CreateEscalationIn):
    """에스컬레이션을 OPEN으로 만든다. 존재하지 않는 case_id면 DB FK가 거절한다."""
    case = case_repo.get(body.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"case {body.case_id} 없음")

    escalation = open_escalation_record(
        escalation_id=str(uuid.uuid4()), case_id=body.case_id, reason=body.reason,
        severity=body.severity, target=body.target,
        created_at=datetime.now(timezone.utc), due_at=body.due_at,
    )
    escalation_repo.save(escalation)
    _publish_governance_event(
        event_type="governance.escalation.v1", trace_id=case.trace_id,
        payload={"fund_id": case.fund_id, "scope_key": f"escalation:{escalation.escalation_id}",
                 "escalation_id": escalation.escalation_id, "case_id": escalation.case_id,
                 "severity": escalation.severity.value, "target": escalation.target,
                 "status": escalation.status.value,
                 "due_at": escalation.due_at.isoformat() if escalation.due_at else None},
    )
    return _escalation_dict(escalation)


@app.get("/governance/v1/escalations/{escalation_id}")
def get_escalation(escalation_id: str):
    return _escalation_dict(_load_escalation(escalation_id))


@app.get("/governance/v1/escalations")
def list_escalations(case_id: str | None = None, target: str | None = None):
    """case_id를 주면 그 Case의 전체, 안 주면 미해결 건만 (담당 본부·QA의 추적 경로)."""
    if case_id is not None:
        rows = escalation_repo.list_by_case(case_id)
    else:
        rows = escalation_repo.list_open(target=target)
    return {"escalations": [_escalation_dict(e) for e in rows]}


@app.post("/governance/v1/escalations/{escalation_id}/transitions")
def transition_escalation_endpoint(escalation_id: str, body: EscalationTransitionIn):
    """상태를 전이한다. RESOLVED에 resolution이 없으면 409, Terminal 이후 전이도 409."""
    updated = transition_escalation(
        _load_escalation(escalation_id), to_status=body.to_status, at=body.at,
        resolution=body.resolution,
    )
    escalation_repo.save(updated)
    return _escalation_dict(updated)


# --- 2.3 위원회 (Y2 - Vote/Quorum/SoD) --------------------------------------------
#
# `open/close_session`, `submit_vote`는 스펙 2.3 확정 엔드포인트다. cancel/get은
# 스펙에 이름이 없는 제안이지만 DDL의 CANCELLED 상태·조회 없이는 쓸 수 없어 함께 넣는다.
#
# 판정 로직은 여기 없다 - committee.py의 순수 함수(evaluate_quorum/cast_vote)가 전부
# 하고, 이 계층은 HTTP <-> 도메인 변환과 저장만 담당한다("API는 투표를 기록만 하고
# 정족수를 임의로 계산해 승인 처리하지 않는다", 스펙 2.3).


def _committee_session_dict(s: CommitteeSession) -> dict:
    return {
        "session_id": s.session_id, "fund_id": s.fund_id, "case_id": s.case_id,
        "committee_type": s.committee_type, "status": s.status.value,
        "quorum_policy": s.quorum_policy.to_jsonb(), "trace_id": s.trace_id,
        "opened_at": s.opened_at.isoformat(),
        "closed_at": s.closed_at.isoformat() if s.closed_at else None,
    }


def _vote_dict(v: Vote) -> dict:
    return {
        "vote_id": v.vote_id, "session_id": v.session_id, "department": v.department,
        "voter_agent_id": v.voter_agent_id, "decision": v.decision.value,
        "conditions": v.conditions or {}, "artifact_ids": list(v.artifact_ids),
        "rationale": v.rationale, "voted_at": v.voted_at.isoformat(),
    }


def _committee_decision_dict(d: CommitteeDecisionRecord) -> dict:
    return {
        "committee_decision_id": d.committee_decision_id, "session_id": d.session_id,
        "decision": d.decision.value, "scope": d.scope or {}, "conditions": d.conditions or {},
        "valid_until": d.valid_until.isoformat() if d.valid_until else None,
        "dissent": list(d.dissent), "approvals": list(d.approvals),
        "decided_at": d.decided_at.isoformat(),
    }


def _load_committee_session(session_id: str) -> CommitteeSession:
    session = committee_repo.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"committee session {session_id} 없음")
    return session


@app.post("/governance/v1/committee/sessions")
def open_committee_session_endpoint(body: OpenCommitteeSessionIn):
    """위원회 세션을 OPEN으로 연다. quorum_policy 형태가 잘못되면 400."""
    policy = QuorumPolicy(
        required_departments=tuple(body.quorum_policy.required_departments),
        veto_departments=tuple(body.quorum_policy.veto_departments),
        approval_threshold=body.quorum_policy.approval_threshold,
    )
    session = open_committee_session(
        session_id=str(uuid.uuid4()), fund_id=body.fund_id, committee_type=body.committee_type,
        quorum_policy=policy, opened_at=datetime.now(timezone.utc), trace_id=body.trace_id,
        case_id=body.case_id,
    )
    committee_repo.save_session(session)
    return _committee_session_dict(session)


@app.get("/governance/v1/committee/sessions/{session_id}")
def get_committee_session(session_id: str):
    session = _load_committee_session(session_id)
    return {
        **_committee_session_dict(session),
        "votes": [_vote_dict(v) for v in committee_repo.list_votes(session_id)],
    }


@app.post("/governance/v1/committee/sessions/{session_id}/votes")
def submit_vote(session_id: str, body: SubmitVoteIn):
    """투표 한 표를 기록한다. OPEN 세션에만 가능하고, 부서당 1표, SoD를 지킨다.

    case_owner_department는 세션에 case_id가 있을 때만 조회한다 - 없으면 SoD 검사
    자체가 성립하지 않는다(위원회가 특정 Case를 심의하는 게 아니라는 뜻). Case Root의
    실제 저장소(case_repo)에서 직접 조회한다 - committee_repo와 case_repo는 별개
    저장소(In-Memory 모드에서는 서로 다른 인스턴스)라 committee_repo만 봐서는
    Case가 실제로 아는 owner_department를 알 수 없다.
    """
    session = _load_committee_session(session_id)
    case_owner_department = None
    if session.case_id is not None:
        case = case_repo.get(session.case_id)
        case_owner_department = case.owner_department if case is not None else None
    vote = cast_committee_vote(
        session, committee_repo.list_votes(session_id), vote_id=str(uuid.uuid4()),
        department=body.department, decision=body.decision, voted_at=datetime.now(timezone.utc),
        case_owner_department=case_owner_department, voter_agent_id=body.voter_agent_id,
        conditions=body.conditions, artifact_ids=tuple(body.artifact_ids), rationale=body.rationale,
    )
    committee_repo.save_vote(vote)
    return _vote_dict(vote)


@app.post("/governance/v1/committee/sessions/{session_id}/close")
def close_committee_session_endpoint(session_id: str, body: CloseCommitteeSessionIn):
    """세션을 닫고 Quorum·Veto 판정 결과를 committee_decisions로 확정한다.

    정족수 미달이면 DEFER로 결정되지만 세션 status는 여전히 DECIDED다(committee.py
    close_session 문서 참고 - "결론을 못 냈다"도 하나의 결정이다).
    """
    session = _load_committee_session(session_id)
    votes = committee_repo.list_votes(session_id)
    updated, decision = close_committee_session(
        session, votes, committee_decision_id=str(uuid.uuid4()),
        at=datetime.now(timezone.utc), scope=body.scope, valid_until=body.valid_until,
    )
    committee_repo.save_session(updated)
    committee_repo.save_decision(decision)
    return {
        "session": _committee_session_dict(updated),
        "decision": _committee_decision_dict(decision),
    }


@app.post("/governance/v1/committee/sessions/{session_id}/cancel")
def cancel_committee_session_endpoint(session_id: str, body: CancelCommitteeSessionIn):
    """SCHEDULED/OPEN 세션을 취소한다. 이미 DECIDED/CANCELLED면 409."""
    session = _load_committee_session(session_id)
    updated = cancel_committee_session(session, at=datetime.now(timezone.utc))
    committee_repo.save_session(updated)
    return _committee_session_dict(updated)


# --- 2.2 Approval (GOV-02 1단계) --------------------------------------------------
#
# `request_approval`은 스펙 2.2 확정 엔드포인트다. decide/revoke/조회는 스펙에 이름이 없는
# 제안이지만, 요청만 만들고 결정할 수 없으면 승인이 영원히 PENDING으로 남으므로 함께 넣는다.
#
# 판정 로직은 여기 없다 - approval.py의 순수 함수(assert_can_decide/decide/revoke)가 전부
# 하고, 이 계층은 HTTP <-> 도메인 변환과 저장만 담당한다.


def _approval_dict(a: ApprovalRecord) -> dict:
    return {
        "approval_id": a.approval_id, "fund_id": a.fund_id,
        "object_type": a.object_type.value, "object_id": a.object_id,
        "required_role": a.required_role.value, "decision": a.decision.value,
        "reason": a.reason, "conditions": a.conditions or {},
        "created_at": a.created_at.isoformat(),
        "expires_at": a.expires_at.isoformat() if a.expires_at else None,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "actor_user_id": a.actor_user_id, "actor_agent_id": a.actor_agent_id,
    }


def _load_approval(approval_id: str) -> ApprovalRecord:
    approval = approval_repo.get(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=f"approval {approval_id} 없음")
    return approval


@app.post("/governance/v1/approvals")
def request_approval(body: ApprovalRequestIn):
    """승인 요청을 만든다. 요청 생성 자체에는 부서 제한이 없다 (결정만 제한된다).

    같은 (object_type, object_id, required_role) 조합이 이미 있으면 새로 만들지 않고 기존
    건을 그대로 돌려준다 - DDL unique 제약이 정한 계약이며, 거절된 건이 있으면 그 거절이
    조회된다(approval.py 불변식 4). 재요청으로 거절을 지우지 않는다.
    """
    existing = approval_repo.find(body.object_type, body.object_id, body.required_role)
    if existing is not None:
        return _approval_dict(existing)

    approval = build_approval_request(
        approval_id=str(uuid.uuid4()), fund_id=body.fund_id, object_type=body.object_type,
        object_id=body.object_id, required_role=body.required_role,
        created_at=datetime.now(timezone.utc), reason=body.reason,
        expires_at=body.expires_at, conditions=body.conditions,
    )
    approval_repo.save(approval)
    return _approval_dict(approval)


@app.get("/governance/v1/approvals/{approval_id}")
def get_approval(approval_id: str):
    return _approval_dict(_load_approval(approval_id))


@app.get("/governance/v1/approvals")
def list_approvals(object_type: ObjectType, object_id: str):
    """대상 하나에 걸린 승인 전부. HR-02가 ceo_approval_id를 찾을 때 쓰는 경로다."""
    return {"approvals": [
        _approval_dict(a) for a in approval_repo.list_by_object(object_type, object_id)
    ]}


@app.post("/governance/v1/approvals/{approval_id}/decide")
def decide_approval_endpoint(approval_id: str, body: ApprovalDecisionIn):
    """승인/거절을 기록한다. required_role과 actor_department가 안 맞으면 403.

    CEO Office가 호스팅하는 API지만 required_role=RISK/QA인 승인은 CEO Office가 결정할 수
    없다(CLAUDE.md 권한 경계). 만료·중복 결정도 409로 막고 어떤 경로로도 APPROVED로
    자동 승격되지 않는다.

    actor_user_id가 있으면(주로 required_role=USER, HITL 사용자 승인) P0-1 게이트를 먼저
    통과해야 한다 - governance.user_profiles에 실재하는 ACTIVE 계정이어야 한다(위 모듈
    "팀 합의" 참고, 서명된 인증은 아니다).
    """
    if body.actor_user_id:
        verify_actor_user(actor_identity_repo.get_status(body.actor_user_id), body.actor_user_id)
    updated = decide_approval(
        _load_approval(approval_id), decision=body.decision,
        actor_department=body.actor_department, at=body.at,
        actor_agent_id=body.actor_agent_id, actor_user_id=body.actor_user_id,
        conditions=body.conditions, reason=body.reason,
    )
    approval_repo.save(updated)
    return _approval_dict(updated)


@app.post("/governance/v1/approvals/{approval_id}/revoke")
def revoke_approval_endpoint(approval_id: str, body: ApprovalRevokeIn):
    """이미 내준 승인을 철회한다. APPROVED만 REVOKED가 될 수 있고 사유가 필수다.

    actor_user_id가 있으면 decide와 같은 P0-1 게이트를 거친다.
    """
    if body.actor_user_id:
        verify_actor_user(actor_identity_repo.get_status(body.actor_user_id), body.actor_user_id)
    updated = revoke_approval(
        _load_approval(approval_id), actor_department=body.actor_department, at=body.at,
        reason=body.reason, actor_agent_id=body.actor_agent_id,
        actor_user_id=body.actor_user_id,
    )
    approval_repo.save(updated)
    return _approval_dict(updated)


# --- F24 Notification (governance-api §8.1에 이름 없는 제안 엔드포인트) -------------


@app.post("/governance/v1/notifications")
def create_notification(body: NotificationRequestIn):
    request = NotificationRequest(
        fund_id=body.fund_id, event_type=body.event_type, scope_key=body.scope_key,
        recipient=body.recipient, payload=body.payload, severity=body.severity,
    )
    rows = notification_service.notify(request, now=body.now)
    return {"notifications": [
        {"channel": r.channel.value, "status": r.status.value, "dedup_key": r.dedup_key} for r in rows
    ]}


if __name__ == "__main__":
    from fastapi.testclient import TestClient

    # 이 자체 점검은 "m1"/"m2"/"m3" 같은 합성 mandate_id로 In-Memory 계약을 검증한다 -
    # DATABASE_URL이 .env에 있어도(위 load_dotenv()) 실 DB로 새면 uuid 타입 에러로 즉시
    # 깨진다(합성 ID가 유효한 uuid가 아니라서다). 실 DB 왕복 검증은 change_workflow.py/
    # postgres_repository.py의 별도 실 DB 구간이 실제 uuid로 담당하므로, 여기서는 자체
    # 점검 목적에 맞게 In-Memory Repository로 강제 전환한다.
    if not isinstance(_mandate_repo, InMemoryMandateVersionRepository):
        _mandate_repo = InMemoryMandateVersionRepository()
        mandate_service = MandateVersionService(_mandate_repo)
        activation_service = MandateActivationService(_mandate_repo)
    if not isinstance(report_repo, InMemoryReportRunRepository):
        report_repo = InMemoryReportRunRepository()
        report_assembler = DailyReportAssembler(report_repo)
    if not isinstance(notification_repo, InMemoryNotificationRepository):
        notification_repo = InMemoryNotificationRepository()
        notification_service = NotificationService(notification_repo)
    if not isinstance(approval_repo, InMemoryApprovalRepository):
        approval_repo = InMemoryApprovalRepository()
    if not isinstance(case_repo, InMemoryCaseRepository):
        case_repo = InMemoryCaseRepository()
    if not isinstance(escalation_repo, InMemoryEscalationRepository):
        escalation_repo = InMemoryEscalationRepository()
    if not isinstance(committee_repo, InMemoryCommitteeRepository):
        committee_repo = InMemoryCommitteeRepository()
    if not isinstance(_mandate_checkpointer, InMemorySaver):
        _mandate_checkpointer = InMemorySaver()
    if not isinstance(actor_identity_repo, InMemoryActorIdentityRepository):
        actor_identity_repo = InMemoryActorIdentityRepository()
    # 자체 점검이 쓰는 고정 데모 identity를 미리 심어둔다(P0-1 게이트가 이제 실재성을
    # 검사하므로, 임의 문자열은 더 이상 통과하지 않는다).
    actor_identity_repo.seed("00000000-0000-4000-8000-00000000cec0")
    actor_identity_repo.seed("user-1")
    mandate_change_workflow = MandateChangeWorkflow(
        version_repo=_mandate_repo, version_service=mandate_service,
        activation_service=activation_service, approval_repo=approval_repo, case_repo=case_repo,
        checkpointer=_mandate_checkpointer,
    )

    client = TestClient(app)

    def _policy(**over) -> dict:
        risk = {"base_capital": "100000000", "currency": "KRW", "max_instrument_weight": "0.1",
                    "max_sector_weight": "0.3", "max_gross_exposure": "1.0", "max_concurrent_positions": 10,
                    "max_daily_loss": "0.03"}
        risk.update(over.pop("risk", {}))
        return {
            "allowed_assets": over.pop("allowed_assets", ["A005930"]), "forbidden_assets": [],
            "risk_bounds": risk,
            "universe_policy": {"allowed_markets": ["KRX"], "trading_start": "09:00", "trading_end": "15:30"},
            "approval_rules": {"paper_order_mode": "USER_APPROVAL"},
        }

    now = "2026-08-02T00:00:00+00:00"

    # 1. 통화 seed 없이 제안 -> 404 FUND_NOT_FOUND (fail-closed).
    r1 = client.post("/governance/v1/mandates/m1/versions", json={
        "policy": _policy(), "objective_text": "장기 성장", "objective": {"style": "growth"},
        "effective_from": now, "created_by": "selfcheck-user",
    })
    assert r1.status_code == 404 and r1.json()["error_code"] == "FUND_NOT_FOUND", r1.text

    # 1b. created_by 없이 제안 -> 422. 이게 곧 활성화 승인자가 되므로 없으면
    #     "누가 승인했는가"가 빈 감사 기록이 생긴다(개발 원칙 9, 차단 방향).
    r1b = client.post("/governance/v1/mandates/m1/versions", json={
        "policy": _policy(), "objective_text": "장기 성장", "objective": {"style": "growth"},
        "effective_from": now, "fund_base_currency": "KRW",
    })
    assert r1b.status_code == 422, r1b.text

    # 2. fund_base_currency seed 후 제안 -> 200, v1, NEUTRAL(최초라 previous 없음).
    #    2026-08-13(F01 방향 A)부터 이 저장 경로는 Risk/QA Case 없이 그 자리에서 즉시
    #    활성화한다 - "저장 버튼을 눌렀다"는 사실 자체를 사용자 승인으로 본다.
    r2 = client.post("/governance/v1/mandates/m1/versions", json={
        "policy": _policy(), "objective_text": "장기 성장", "objective": {"style": "growth"},
        "effective_from": now, "fund_base_currency": "KRW", "created_by": "selfcheck-user",
    })
    assert r2.status_code == 200, r2.text
    assert r2.json()["version"] == 1 and r2.json()["direction"] == "NEUTRAL"
    assert r2.json()["activated"] is True, "저장 즉시 활성화되지 않았다(F01 방향 A 회귀)"

    # 3. 별도 활성화 Gate(POST .../versions/{version}/activate)는 그대로 살아 있다 -
    #    change_workflow.py의 HITL Case 경로가 여전히 이걸 쓴다(Version 서비스를 직접
    #    부르지 HTTP 저장 경로를 거치지 않는다). 그 경로를 흉내 내려면 여기서도
    #    mandate_service.propose_version()을 직접 불러야 한다 - HTTP `/versions`는
    #    이제 항상 자동 활성화하므로 "승인 없인 안 열린다"를 더는 재현하지 못한다.
    direct = mandate_service.propose_version(
        mandate_id="m1", policy=MandatePolicy(**_policy(risk={"max_gross_exposure": "2.0"})),
        objective_text="장기 성장(확대)", objective={"style": "growth"},
        effective_from=datetime.fromisoformat(now),
        previous_policy=MandatePolicy(**_policy()),
    )
    assert direct.row.version == 2 and direct.direction.value == "LOOSEN"

    # v1의 effective_to는 v2 활성화 시각이 된다 - v1.effective_from(now)보다 뒤여야
    # 하므로(DDL·set_effective_to 둘 다 강제) 여기서만 뒤 시각을 쓴다.
    later = "2026-08-02T00:00:01+00:00"
    r3 = client.post("/governance/v1/mandates/m1/versions/2/activate", json={
        "direction": "LOOSEN", "at": later,
    })
    assert r3.status_code == 200 and r3.json()["activated"] is False, r3.text

    # 4. 승인 주면 활성화 + get_mandate_current로 반영 확인(2026-08-06부터 policy 포함).
    r4 = client.post("/governance/v1/mandates/m1/versions/2/activate", json={
        "direction": "LOOSEN", "at": later, "approval": {"approved_by": "u1", "trace_id": "t1"},
    })
    assert r4.status_code == 200 and r4.json()["activated"] is True, r4.text
    r5 = client.get("/governance/v1/mandates/m1/current")
    body5 = r5.json()
    assert body5["mandate_id"] == "m1"
    assert body5["case_id"] is None
    # v1은 저장 즉시 자동 활성화됐고(2번), v2는 방금 수동 승인으로 활성화됐다(4번) -
    # v2가 최신이므로 current_version은 2다.
    assert body5["current_version"] == 2
    assert body5["mandate_version_id"] == _mandate_repo.get_mandate_version_id("m1", 2)
    assert body5["policy_hash"] == _mandate_repo.get_mandate_content_hash("m1", 2)
    assert body5["status"] == "ACTIVE"
    assert body5["content_hash"] == body5["policy_hash"]
    assert body5["policy"]["risk_bounds"]["max_instrument_weight"] == "0.1"
    assert body5["policy"]["risk_bounds"]["max_gross_exposure"] == "2.0"
    assert body5["objective_text"] == "장기 성장(확대)"
    assert body5["policy"]["universe_policy"]["allowed_markets"] == ["KRX"]
    assert isinstance(body5["content_hash"], str) and len(body5["content_hash"]) == 64

    r5b = client.get("/governance/v1/mandates/never-proposed/current")
    assert r5b.json() == {"mandate_id": "never-proposed", "current_version": 0, "status": "DRAFT"}

    _mandate_repo.set_fund_id("m1", "f1")
    r5c = client.get("/governance/v1/mandates/by-fund/f1/current")
    assert r5c.status_code == 200 and r5c.json()["mandate_id"] == "m1", r5c.text
    _mandate_repo.set_owner_user_id("m1", "u1")
    r5_owner = client.get(
        "/governance/v1/mandates/by-fund/f1/current",
        params={"owner_user_id": "u1"},
    )
    assert r5_owner.status_code == 200 and r5_owner.json()["mandate_id"] == "m1", r5_owner.text
    r5_other_owner = client.get(
        "/governance/v1/mandates/by-fund/f1/current",
        params={"owner_user_id": "u2"},
    )
    assert r5_other_owner.status_code == 404, r5_other_owner.text
    r5d = client.get("/governance/v1/mandates/by-fund/no-such-fund/current")
    assert r5d.status_code == 404, r5d.text
    _mandate_repo.set_fund_id("m1b", "f1")
    r5e = client.get("/governance/v1/mandates/by-fund/f1/current")
    assert r5e.status_code == 409, r5e.text

    # 4b. user -> fund 역참조. In-Memory Repository는 `fund_ids_for_user`가 없으므로
    # **503으로 닫힌다** - 조용히 404("Fund 없음")를 주면 호출자가 "이 사용자는
    # Fund가 없다"와 "이 저장소는 역참조를 못 한다"를 구분할 수 없다. Postgres
    # Repository를 쓰면 governance.fund_memberships를 실제로 읽는다.
    r5f = client.get("/governance/v1/users/u1/fund")
    assert r5f.status_code == 503, r5f.text
    assert r5f.json()["detail"] == "user_fund_lookup_unavailable", r5f.text

    # 5. Report 조립 - 필수 Section 없으면 FAILED.
    r6 = client.post("/reporting/v1/reports", json={
        "fund_id": "f1", "as_of": "2026-08-02", "template_version": "v1", "trace_id": "t1",
    })
    assert r6.status_code == 200 and r6.json()["status"] == "FAILED", r6.text
    assert set(r6.json()["missing_required"]) == {"portfolio", "risk"}

    # 6. Report 조립 - 필수 Section 있으면 QUEUED, 조회도 된다.
    r7 = client.post("/reporting/v1/reports", json={
        "fund_id": "f1", "as_of": "2026-08-02", "template_version": "v1", "trace_id": "t1",
        "portfolio": {"snapshot_id": "s1", "as_of": now}, "risk": {"snapshot_id": "s2", "as_of": now},
    })
    assert r7.status_code == 200 and r7.json()["status"] == "QUEUED", r7.text
    r8 = client.get(f"/reporting/v1/reports/{r7.json()['content_hash']}", params={"fund_id": "f1"})
    assert r8.status_code == 200 and r8.json()["status"] == "QUEUED"

    # 7. Notification - CRITICAL은 억제 안 됨(2연속 호출 둘 다 PENDING).
    n1 = client.post("/governance/v1/notifications", json={
        "fund_id": "f1", "event_type": "risk.breach.v1", "scope_key": "case:1",
        "recipient": "user:u1", "severity": "CRITICAL", "now": now,
    })
    n2 = client.post("/governance/v1/notifications", json={
        "fund_id": "f1", "event_type": "risk.breach.v1", "scope_key": "case:1",
        "recipient": "user:u1", "severity": "CRITICAL", "now": now,
    })
    assert all(x["status"] == "PENDING" for x in n1.json()["notifications"])
    assert all(x["status"] == "PENDING" for x in n2.json()["notifications"]), "CRITICAL이 억제됐다"

    # 8. 승인 요청(GOV-02) - PENDING 생성, 같은 대상·역할 재요청은 같은 건을 돌려준다.
    a1 = client.post("/governance/v1/approvals", json={
        "object_type": "AGENT_PROFILE_VERSION", "object_id": "pv-1",
        "required_role": "CEO", "fund_id": "f1", "reason": "HR-02 활성화",
    })
    assert a1.status_code == 200 and a1.json()["decision"] == "PENDING", a1.text
    approval_id = a1.json()["approval_id"]
    a1b = client.post("/governance/v1/approvals", json={
        "object_type": "AGENT_PROFILE_VERSION", "object_id": "pv-1",
        "required_role": "CEO", "fund_id": "f1",
    })
    assert a1b.json()["approval_id"] == approval_id, "재요청이 새 승인을 만들었다"

    # 9. 권한 분리 - RISK 승인은 CEO Office가 결정할 수 없다(403).
    a2 = client.post("/governance/v1/approvals", json={
        "object_type": "MANDATE_VERSION", "object_id": "mv-1",
        "required_role": "RISK", "fund_id": "f1",
    })
    risk_id = a2.json()["approval_id"]
    d_bad = client.post(f"/governance/v1/approvals/{risk_id}/decide", json={
        "decision": "APPROVED", "actor_department": "ceo-agent", "at": now,
    })
    assert d_bad.status_code == 403, d_bad.text
    assert client.get(f"/governance/v1/approvals/{risk_id}").json()["decision"] == "PENDING"

    # 10. 리스크본부 본인은 결정 가능. DB 표기(risk-management)도 받는다.
    d_ok = client.post(f"/governance/v1/approvals/{risk_id}/decide", json={
        "decision": "REJECTED", "actor_department": "risk-management", "at": now,
        "reason": "한도 초과",
    })
    assert d_ok.status_code == 200 and d_ok.json()["decision"] == "REJECTED", d_ok.text

    # 11. 이미 결정된 승인 재결정 409, 거절된 건 재요청해도 거절이 그대로 조회된다.
    d_again = client.post(f"/governance/v1/approvals/{risk_id}/decide", json={
        "decision": "APPROVED", "actor_department": "risk-management", "at": now,
    })
    assert d_again.status_code == 409, d_again.text
    a2b = client.post("/governance/v1/approvals", json={
        "object_type": "MANDATE_VERSION", "object_id": "mv-1",
        "required_role": "RISK", "fund_id": "f1",
    })
    assert a2b.json()["decision"] == "REJECTED", "재요청이 거절을 지웠다"

    # 12. OWNER는 fail-closed(501) - 소유 부서 검증 경로가 없다.
    a3 = client.post("/governance/v1/approvals", json={
        "object_type": "CAPITAL_ALLOCATION", "object_id": "ca-1",
        "required_role": "OWNER", "fund_id": "f1",
    })
    d_owner = client.post(f"/governance/v1/approvals/{a3.json()['approval_id']}/decide", json={
        "decision": "APPROVED", "actor_department": "ceo-agent", "at": now,
    })
    assert d_owner.status_code == 501, d_owner.text

    # 13. 만료된 승인은 결정 불가(409) - 자동 승인으로 떨어지지 않는다.
    # created_at은 서버의 실제 now라 expires_at은 그보다 뒤여야 요청이 만들어진다(그게 아니면
    # 400). 그래서 기한을 넉넉히 미래로 두고, 그 기한을 넘긴 시점에 결정을 시도한다.
    a4 = client.post("/governance/v1/approvals", json={
        "object_type": "IMPROVEMENT_CANDIDATE", "object_id": "ic-1",
        "required_role": "CEO", "fund_id": "f1",
        "expires_at": "2026-12-31T00:00:00+00:00",
    })
    assert a4.status_code == 200, a4.text
    d_exp = client.post(f"/governance/v1/approvals/{a4.json()['approval_id']}/decide", json={
        "decision": "APPROVED", "actor_department": "ceo-agent",
        "at": "2027-01-01T00:00:00+00:00",
    })
    assert d_exp.status_code == 409, d_exp.text
    # 만료 시도 후에도 PENDING이어야 한다 - 실패가 상태를 바꾸지 않는다.
    assert client.get(
        f"/governance/v1/approvals/{a4.json()['approval_id']}"
    ).json()["decision"] == "PENDING"

    # 14. CEO 승인 결정 -> 철회. 사람 승인이 아니므로 actor_user_id는 None 유지.
    d_ceo = client.post(f"/governance/v1/approvals/{approval_id}/decide", json={
        "decision": "APPROVED", "actor_department": "ceo-agent", "at": now,
    })
    assert d_ceo.status_code == 200 and d_ceo.json()["decision"] == "APPROVED", d_ceo.text
    assert d_ceo.json()["actor_user_id"] is None
    rv = client.post(f"/governance/v1/approvals/{approval_id}/revoke", json={
        "actor_department": "ceo-agent", "at": now, "reason": "Mandate 변경",
    })
    assert rv.status_code == 200 and rv.json()["decision"] == "REVOKED", rv.text

    # 15. 대상별 승인 목록 조회 - HR-02가 ceo_approval_id를 찾는 경로.
    lst = client.get("/governance/v1/approvals", params={
        "object_type": "AGENT_PROFILE_VERSION", "object_id": "pv-1",
    })
    assert lst.status_code == 200 and len(lst.json()["approvals"]) == 1, lst.text

    # 15b. required_role=USER(HITL 사용자 승인) - actor_department 없이 actor_user_id만으로
    # 결정된다. 없으면 400.
    a5 = client.post("/governance/v1/approvals", json={
        "object_type": "MANDATE_VERSION", "object_id": "mv-user-1",
        "required_role": "USER", "fund_id": "f1",
    })
    assert a5.status_code == 200, a5.text
    d_no_user = client.post(f"/governance/v1/approvals/{a5.json()['approval_id']}/decide", json={
        "decision": "APPROVED", "at": now,
    })
    assert d_no_user.status_code == 400, d_no_user.text
    d_user = client.post(f"/governance/v1/approvals/{a5.json()['approval_id']}/decide", json={
        "decision": "APPROVED", "at": now, "actor_user_id": "00000000-0000-4000-8000-00000000cec0",
    })
    assert d_user.status_code == 200 and d_user.json()["decision"] == "APPROVED", d_user.text
    assert d_user.json()["actor_user_id"] == "00000000-0000-4000-8000-00000000cec0"

    # 15c. P0-1 - governance.user_profiles에 실재하지 않는 actor_user_id는 403(비어있지 않은
    # 문자열이라는 것만으로 통과하던 이전 상태보다 좁힌 검증). 존재하는 사용자는 여전히 통과.
    a5b = client.post("/governance/v1/approvals", json={
        "object_type": "MANDATE_VERSION", "object_id": "mv-user-2",
        "required_role": "USER", "fund_id": "f1",
    })
    assert a5b.status_code == 200, a5b.text
    d_ghost = client.post(f"/governance/v1/approvals/{a5b.json()['approval_id']}/decide", json={
        "decision": "APPROVED", "at": now, "actor_user_id": "00000000-0000-0000-0000-000000000000",
    })
    assert d_ghost.status_code == 403 and d_ghost.json()["error_code"] == "UnverifiedActorUserError", \
        d_ghost.text
    d_real = client.post(f"/governance/v1/approvals/{a5b.json()['approval_id']}/decide", json={
        "decision": "APPROVED", "at": now, "actor_user_id": "user-1",
    })
    assert d_real.status_code == 200, d_real.text

    # 16. Case 생성 - OPEN + display_id 자동 생성 + timeline 1건.
    c1 = client.post("/governance/v1/cases", json={
        "case_type": "HIRING", "priority": 2, "owner_department": "hr-department",
        "fund_id": "f1", "trace_id": "trace-case-1", "created_by": "ceo-agent",
        "reason": "리스크본부 Queue 적체",
    })
    assert c1.status_code == 200 and c1.json()["status"] == "OPEN", c1.text
    case_id = c1.json()["case_id"]
    assert c1.json()["display_id"].startswith("HR-"), c1.json()["display_id"]
    tl1 = client.get(f"/governance/v1/cases/{case_id}/timeline")
    assert tl1.status_code == 200 and len(tl1.json()["events"]) == 1, tl1.text
    assert tl1.json()["events"][0]["to_status"] == "OPEN"
    assert tl1.json()["events"][0]["from_status"] is None

    # 17. 전이 - OPEN -> ACKNOWLEDGED, timeline이 함께 쌓인다.
    t1 = client.post(f"/governance/v1/cases/{case_id}/transitions", json={
        "to_status": "ACKNOWLEDGED", "actor": "hr-department", "at": now,
    })
    assert t1.status_code == 200 and t1.json()["status"] == "ACKNOWLEDGED", t1.text
    tl2 = client.get(f"/governance/v1/cases/{case_id}/timeline")
    assert [e["sequence"] for e in tl2.json()["events"]] == [1, 2]

    # 18. OPEN -> RESOLVED 직행 차단(409), Terminal 이후 전이 차단(409).
    c2 = client.post("/governance/v1/cases", json={
        "case_type": "INCIDENT", "priority": 90, "owner_department": "risk-management",
        "fund_id": "f1", "trace_id": "trace-case-2", "created_by": "ceo-agent",
    })
    skip = client.post(f"/governance/v1/cases/{c2.json()['case_id']}/transitions", json={
        "to_status": "RESOLVED", "actor": "x", "at": now,
    })
    assert skip.status_code == 409, skip.text
    assert client.get(f"/governance/v1/cases/{c2.json()['case_id']}").json()["status"] == "OPEN"

    client.post(f"/governance/v1/cases/{case_id}/transitions", json={
        "to_status": "RESOLVED", "actor": "hr-department", "at": now,
    })
    dead = client.post(f"/governance/v1/cases/{case_id}/transitions", json={
        "to_status": "CANCELLED", "actor": "x", "at": now,
    })
    assert dead.status_code == 409, dead.text

    # 19. display_id는 같은 타입·날짜에서 연번이 증가하고 타입이 다르면 접두어가 다르다.
    c3 = client.post("/governance/v1/cases", json={
        "case_type": "HIRING", "priority": 1, "owner_department": "hr-department",
        "fund_id": "f1", "trace_id": "trace-case-3", "created_by": "ceo-agent",
    })
    assert c3.json()["display_id"] != c1.json()["display_id"]
    assert c3.json()["display_id"].startswith("HR-")
    assert c2.json()["display_id"].startswith("IN-"), c2.json()["display_id"]

    # 20. 없는 Case는 404.
    assert client.get("/governance/v1/cases/00000000-0000-4000-8000-000000000000").status_code == 404

    # 21. 에스컬레이션 생성 - Case에 붙는다. 없는 Case면 404.
    e1 = client.post("/governance/v1/escalations", json={
        "case_id": c2.json()["case_id"], "reason": "Risk 한도 초과 24시간 미해결",
        "severity": "CRITICAL", "target": "risk-management",
    })
    assert e1.status_code == 200 and e1.json()["status"] == "OPEN", e1.text
    escalation_id = e1.json()["escalation_id"]
    assert client.post("/governance/v1/escalations", json={
        "case_id": "00000000-0000-4000-8000-000000000000", "reason": "x",
        "severity": "LOW", "target": "x",
    }).status_code == 404

    # 22. resolution 없이 RESOLVED 불가(409), ACKNOWLEDGED 건너뛰기도 불가(409).
    assert client.post(f"/governance/v1/escalations/{escalation_id}/transitions", json={
        "to_status": "RESOLVED", "at": now,
    }).status_code == 409
    ack_e = client.post(f"/governance/v1/escalations/{escalation_id}/transitions", json={
        "to_status": "ACKNOWLEDGED", "at": now,
    })
    assert ack_e.status_code == 200 and ack_e.json()["status"] == "ACKNOWLEDGED", ack_e.text
    assert client.post(f"/governance/v1/escalations/{escalation_id}/transitions", json={
        "to_status": "RESOLVED", "at": now, "resolution": "   ",
    }).status_code == 409

    # 23. 미해결 목록 -> 해소 후 목록에서 빠진다. severity는 유지된다.
    open_list = client.get("/governance/v1/escalations", params={"target": "risk-management"})
    assert escalation_id in {e["escalation_id"] for e in open_list.json()["escalations"]}
    done = client.post(f"/governance/v1/escalations/{escalation_id}/transitions", json={
        "to_status": "RESOLVED", "at": now, "resolution": "한도 재적용으로 해소",
    })
    assert done.status_code == 200 and done.json()["severity"] == "CRITICAL"
    assert done.json()["resolved_at"] is not None
    after_list = client.get("/governance/v1/escalations", params={"target": "risk-management"})
    assert escalation_id not in {e["escalation_id"] for e in after_list.json()["escalations"]}

    # 24. Terminal 이후 전이 차단, Case별 조회에는 여전히 보인다.
    assert client.post(f"/governance/v1/escalations/{escalation_id}/transitions", json={
        "to_status": "CANCELLED", "at": now,
    }).status_code == 409
    by_case = client.get("/governance/v1/escalations", params={"case_id": c2.json()["case_id"]})
    assert len(by_case.json()["escalations"]) == 1, by_case.text

    # 25. 위원회 세션 생성 - quorum_policy 형태 오류(threshold 범위 초과)는 400.
    bad_policy = client.post("/governance/v1/committee/sessions", json={
        "fund_id": "f1", "committee_type": "INVESTMENT", "trace_id": "t-committee",
        "quorum_policy": {"required_departments": ["a", "b"], "approval_threshold": 5},
    })
    assert bad_policy.status_code == 400, bad_policy.text

    # hr-department를 required_departments에 넣지 않는다 - c1의 owner_department가
    # hr-department라(16번 참고) SoD로 영원히 투표를 못 해 정족수가 절대 안 찬다
    # (required이면서 SoD로 막힌 부서가 있으면 설계상 DEFER에서 못 벗어난다 - 의도된
    # 동작이지만 이 시나리오의 목적은 그게 아니므로 required에서 뺀다).
    s1 = client.post("/governance/v1/committee/sessions", json={
        "fund_id": "f1", "committee_type": "STRATEGY_PLANNING", "trace_id": "t-committee",
        "case_id": c1.json()["case_id"],
        "quorum_policy": {
            "required_departments": ["risk-management", "qa-department"],
            "veto_departments": ["risk-management"], "approval_threshold": 2,
        },
    })
    assert s1.status_code == 200 and s1.json()["status"] == "OPEN", s1.text
    session_id = s1.json()["session_id"]

    # 26. SoD - c1의 owner_department는 hr-department다. required_departments에
    # 없어도 SoD 차단은 부서 소속과만 관련이 있어 그대로 걸린다.
    self_vote = client.post(f"/governance/v1/committee/sessions/{session_id}/votes", json={
        "department": "hr-department", "decision": "APPROVE",
    })
    assert self_vote.status_code == 403, self_vote.text

    # 27. 정상 투표 - 부서당 1표, 중복은 409.
    v1 = client.post(f"/governance/v1/committee/sessions/{session_id}/votes", json={
        "department": "risk-management", "decision": "APPROVE",
    })
    assert v1.status_code == 200 and v1.json()["decision"] == "APPROVE", v1.text
    dup = client.post(f"/governance/v1/committee/sessions/{session_id}/votes", json={
        "department": "risk-management", "decision": "REJECT",
    })
    assert dup.status_code == 409, dup.text

    # 28. 정족수 미달 상태로 종료 -> DEFER, 그래도 세션 status는 DECIDED.
    early_close = client.post("/governance/v1/committee/sessions", json={
        "fund_id": "f1", "committee_type": "STRATEGY_PLANNING", "trace_id": "t-early",
        "quorum_policy": {"required_departments": ["hr-department", "risk-management"],
                          "approval_threshold": 2},
    })
    early_id = early_close.json()["session_id"]
    client.post(f"/governance/v1/committee/sessions/{early_id}/votes", json={
        "department": "risk-management", "decision": "APPROVE",
    })
    closed_early = client.post(f"/governance/v1/committee/sessions/{early_id}/close", json={})
    assert closed_early.json()["decision"]["decision"] == "DEFER", closed_early.text
    assert closed_early.json()["session"]["status"] == "DECIDED"

    # 29. 정족수 채우기 -> 종료 -> APPROVE, DECIDED 세션 재종결/재투표는 409.
    client.post(f"/governance/v1/committee/sessions/{session_id}/votes", json={
        "department": "qa-department", "decision": "APPROVE",
    })
    done = client.post(f"/governance/v1/committee/sessions/{session_id}/close", json={})
    assert done.status_code == 200 and done.json()["decision"]["decision"] == "APPROVE", done.text
    assert client.post(f"/governance/v1/committee/sessions/{session_id}/close", json={}).status_code == 409
    assert client.post(f"/governance/v1/committee/sessions/{session_id}/votes", json={
        "department": "hr-department", "decision": "APPROVE",
    }).status_code == 409

    # 30. 취소 - SCHEDULED/OPEN만 가능. GET으로 투표 내역까지 함께 조회된다.
    to_cancel = client.post("/governance/v1/committee/sessions", json={
        "fund_id": "f1", "committee_type": "INVESTMENT", "trace_id": "t-cancel",
        "quorum_policy": {"required_departments": ["ceo-agent"], "approval_threshold": 1},
    })
    cancel_id = to_cancel.json()["session_id"]
    cancelled = client.post(f"/governance/v1/committee/sessions/{cancel_id}/cancel", json={})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
    assert client.post(f"/governance/v1/committee/sessions/{cancel_id}/cancel", json={}).status_code == 409

    detail = client.get(f"/governance/v1/committee/sessions/{session_id}")
    assert detail.status_code == 200 and len(detail.json()["votes"]) == 2, detail.text
    assert client.get("/governance/v1/committee/sessions/00000000-0000-4000-8000-000000000000").status_code == 404

    # 31. HITL 변경 요청 — 최초 활성화(UC-1: Risk+QA 동시 승인 -> 사용자 승인 -> 활성화) API 경로.
    r31 = client.post("/governance/v1/mandates/m3/change-requests", json={
        "fund_id": "f1", "policy": _policy(), "objective_text": "최초 활성화",
        "objective": {}, "effective_from": now, "created_by": "selfcheck-api",
        "trace_id": "trace-m3", "now": now, "fund_base_currency": "KRW",
    })
    assert r31.status_code == 200 and r31.json()["stage"] == "AWAITING_REVIEW", r31.text
    case_id_m3 = r31.json()["case_id"]
    version_id_m3 = _mandate_repo.get_mandate_version_id("m3", 1)
    for role, dept in (("RISK", "risk-management"), ("QA", "qa-department")):
        pending = client.get("/governance/v1/approvals", params={
            "object_type": "MANDATE_VERSION", "object_id": version_id_m3,
        }).json()["approvals"]
        target = next(a for a in pending if a["required_role"] == role)
        d = client.post(f"/governance/v1/approvals/{target['approval_id']}/decide", json={
            "decision": "APPROVED", "actor_department": dept, "at": now,
        })
        assert d.status_code == 200, d.text
    r32 = client.post(f"/governance/v1/cases/{case_id_m3}/advance", json={"at": now})
    assert r32.status_code == 200 and r32.json()["stage"] == "AWAITING_USER_APPROVAL", r32.text
    user_pending_m3 = client.get("/governance/v1/approvals", params={
        "object_type": "MANDATE_VERSION", "object_id": version_id_m3,
    }).json()["approvals"]
    assert next(a for a in user_pending_m3 if a["required_role"] == "USER")["expires_at"] \
        == "2026-08-03T00:00:00+00:00"
    user_approval_id_m3 = next(a["approval_id"] for a in user_pending_m3 if a["required_role"] == "USER")
    du = client.post(f"/governance/v1/approvals/{user_approval_id_m3}/decide", json={
        "decision": "APPROVED", "actor_user_id": "user-1", "at": now,
    })
    assert du.status_code == 200, du.text
    r33 = client.post(f"/governance/v1/cases/{case_id_m3}/advance", json={"at": now})
    assert r33.status_code == 200 and r33.json()["stage"] == "ACTIVATED", r33.text
    m3_current = client.get("/governance/v1/mandates/m3/current").json()
    assert m3_current["mandate_id"] == "m3"
    assert m3_current["case_id"] is None
    assert m3_current["current_version"] == 1
    assert m3_current["mandate_version_id"] == _mandate_repo.get_mandate_version_id("m3", 1)
    assert m3_current["policy_hash"] == _mandate_repo.get_mandate_content_hash("m3", 1)
    assert m3_current["status"] == "ACTIVE"

    # 32. TIGHTEN — 이미 활성 v1이 있으니 승인 없이 즉시 적용(FAST_APPLIED), Case 없음.
    # v1의 effective_to가 v1의 effective_from(now)보다 뒤여야 한다(DDL check) - 같은 시각을
    # 재사용하면 안 된다(change_workflow.py 자체 점검에서 겪은 것과 같은 함정).
    now_v2 = "2026-08-02T01:00:00+00:00"
    r34 = client.post("/governance/v1/mandates/m3/change-requests", json={
        "fund_id": "f1", "policy": _policy(risk={"max_gross_exposure": "0.5"}),
        "objective_text": "한도 축소", "objective": {}, "effective_from": now_v2,
        "created_by": "selfcheck-api", "trace_id": "trace-m3-v2", "now": now_v2,
        "previous_policy": _policy(),
    })
    assert r34.status_code == 200, r34.text
    assert r34.json()["stage"] == "FAST_APPLIED" and r34.json()["case_id"] is None, r34.text
    assert client.get("/governance/v1/mandates/m3/current").json()["current_version"] == 2

    # 33. 종료된 Case(위 31에서 RESOLVED) 재advance -> 409 CaseAlreadyResolvedError.
    r35 = client.post(f"/governance/v1/cases/{case_id_m3}/advance", json={"at": now})
    assert r35.status_code == 409 and r35.json()["error_code"] == "CaseAlreadyResolvedError", r35.text

    print("ok - HITL 변경요청/advance API 3개 시나리오 통과 (최초활성화·TIGHTEN즉시적용·재advance차단)")

    # 34. Mandate assistant - 정상 응답(FastAPI TestClient는 실제 anthropic 호출 없이
    # mandate_assistant._anthropic_call이 ANTHROPIC_API_KEY 부재로 RuntimeError를 내는
    # 경로를 그대로 태운다 - 즉 이 자체 점검은 "LLM 실패 시 500이 아니라 빈 제안으로
    # 감싼다"는 계약을 실제로 검증한다).
    r36 = client.post("/governance/v1/mandate-assistant/suggest", json={
        "fund_id": "f1", "messages": [{"role": "user", "content": "10년 정도 투자할 생각이에요"}],
    })
    assert r36.status_code == 200, r36.text
    body36 = r36.json()
    assert body36["requires_user_confirmation"] is True
    assert body36["suggestions"] == [] and body36["dropped_fields"] == []
    assert "제안을 만들 수 없습니다" in body36["reply"]

    # 34b. 빈 messages는 422(Pydantic min_length).
    r37 = client.post("/governance/v1/mandate-assistant/suggest", json={
        "fund_id": "f1", "messages": [],
    })
    assert r37.status_code == 422, r37.text
    print("ok - Mandate assistant suggest API 통과 (LLM 실패를 500이 아니라 빈 제안으로 감쌈, 빈 messages 422)")

    print("ok - CEO Office Domain API 36개 시나리오 점검 통과 "
          "(승인 10개(P0-1 actor_user_id 실재성 포함) + Case Root 5개 + 에스컬레이션 4개 + "
          "위원회 6개 + HITL 변경요청 3개 + Mandate assistant 2개 포함)")
