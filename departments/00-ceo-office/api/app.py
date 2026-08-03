#!/usr/bin/env python3
"""CEO Office Domain API — mandate/service.py, mandate/lifecycle.py,
reporting/daily_report.py, notification/notification.py를 감싸는 FastAPI 래퍼.

소유: 영주 (CEO Office)
근거: docs/02-engineering/GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 2절(governance-api),
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
  - GET /governance/v1/mandates/{fund_id}/current(스펙 2.1)는 fund_id -> mandate_id 역참조
    쿼리가 아직 없어(accounting-api 미구현) mandate_id로 대신 조회한다.
  - POST .../versions는 In-Memory Repository일 때 Fund 기준 통화를 accounting-api에서
    조회할 수 없으므로, 요청에 fund_base_currency를 선택 필드로 받아 seed한다(데모용,
    Postgres Repository를 쓸 때는 무시 - 실제 accounting.funds를 그대로 조회한다).
  - GET /reporting/v1/reports/{report_id}(스펙 4절)는 report_id가 DB 배선 전이라 없다 -
    content_hash + fund_id로 대신 조회한다.

실행: uvicorn app:app --app-dir departments/00-ceo-office/api
자체 점검: python departments/00-ceo-office/api/app.py
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

_BASE = Path(__file__).resolve().parent.parent
_MANDATE_DIR = _BASE / "src" / "mandate"
_REPORTING_DIR = _BASE / "src" / "reporting"
_NOTIFICATION_DIR = _BASE / "src" / "notification"
_APPROVAL_DIR = _BASE / "src" / "approval"
_CASE_DIR = _BASE / "src" / "case"
_ESCALATION_DIR = _BASE / "src" / "escalation"
for _p in (
    _MANDATE_DIR, _REPORTING_DIR, _NOTIFICATION_DIR, _APPROVAL_DIR, _CASE_DIR, _ESCALATION_DIR
):
    sys.path.insert(0, str(_p))

from approval import (
    AlreadyDecidedError,
    ApprovalDecision,
    ApprovalExpiredError,
    ApprovalRecord,
    InMemoryApprovalRepository,
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
from lifecycle import (
    ActivationResult,
    MandateActivationService,
    UserApproval,
)
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
    MandateVersionService,
)

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
    from postgres_notification_repository import PostgresNotificationRepository
except ImportError:
    PostgresNotificationRepository = None  # type: ignore[assignment,misc]

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


class ApprovalIn(BaseModel):
    approved_by: str
    trace_id: str
    reason: str | None = None


class ActivateRequest(BaseModel):
    direction: ChangeDirection
    at: datetime
    approval: ApprovalIn | None = None


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
    """

    decision: ApprovalDecision
    actor_department: str = Field(min_length=1)
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
    actor_department: str = Field(min_length=1)
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


@app.exception_handler(FundNotFoundError)
def _on_fund_not_found(request, exc: FundNotFoundError):
    return JSONResponse(status_code=404, content={
        "error_code": "FUND_NOT_FOUND", "message": str(exc), "detail": {}, "trace_id": None,
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


# --- 2.1 Mandate ----------------------------------------------------------------


@app.post("/governance/v1/mandates/{mandate_id}/versions")
def propose_version(mandate_id: str, body: ProposeVersionRequest):
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
    return {
        "mandate_id": mandate_id, "version": result.row.version,
        "direction": result.direction.value, "requires_user_reapproval": result.requires_user_reapproval,
        "content_hash": result.row.content_hash,
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


@app.get("/governance/v1/mandates/{mandate_id}/current")
def get_mandate_current(mandate_id: str):
    version, status = _mandate_repo.get_mandate_current(mandate_id)
    return {"mandate_id": mandate_id, "current_version": version, "status": status}


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
        payload={"escalation_id": escalation.escalation_id, "case_id": escalation.case_id,
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
    """
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
    """이미 내준 승인을 철회한다. APPROVED만 REVOKED가 될 수 있고 사유가 필수다."""
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
        "effective_from": now,
    })
    assert r1.status_code == 404 and r1.json()["error_code"] == "FUND_NOT_FOUND", r1.text

    # 2. fund_base_currency seed 후 제안 -> 200, v1, NEUTRAL(최초라 previous 없음).
    r2 = client.post("/governance/v1/mandates/m1/versions", json={
        "policy": _policy(), "objective_text": "장기 성장", "objective": {"style": "growth"},
        "effective_from": now, "fund_base_currency": "KRW",
    })
    assert r2.status_code == 200, r2.text
    assert r2.json()["version"] == 1 and r2.json()["direction"] == "NEUTRAL"

    # 3. 최초 활성화는 승인 없이 차단.
    r3 = client.post("/governance/v1/mandates/m1/versions/1/activate", json={
        "direction": "NEUTRAL", "at": now,
    })
    assert r3.status_code == 200 and r3.json()["activated"] is False, r3.text

    # 4. 승인 주면 활성화 + get_mandate_current로 반영 확인.
    r4 = client.post("/governance/v1/mandates/m1/versions/1/activate", json={
        "direction": "NEUTRAL", "at": now, "approval": {"approved_by": "u1", "trace_id": "t1"},
    })
    assert r4.status_code == 200 and r4.json()["activated"] is True, r4.text
    r5 = client.get("/governance/v1/mandates/m1/current")
    assert r5.json() == {"mandate_id": "m1", "current_version": 1, "status": "ACTIVE"}

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

    print("ok - CEO Office Domain API 24개 시나리오 점검 통과 "
          "(승인 8개 + Case Root 5개 + 에스컬레이션 4개 포함)")
