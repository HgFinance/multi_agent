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

import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

_BASE = Path(__file__).resolve().parent.parent
_MANDATE_DIR = _BASE / "src" / "mandate"
_REPORTING_DIR = _BASE / "src" / "reporting"
_NOTIFICATION_DIR = _BASE / "src" / "notification"
for _p in (_MANDATE_DIR, _REPORTING_DIR, _NOTIFICATION_DIR):
    sys.path.insert(0, str(_p))

from daily_report import (  # noqa: E402
    DailyReportAssembler,
    DailyReportSections,
    InMemoryReportRunRepository,
    SnapshotRef,
)
from lifecycle import ActivationResult, MandateActivationService, UserApproval  # noqa: E402
from notification import (  # noqa: E402
    InMemoryNotificationRepository,
    NotificationRequest,
    NotificationService,
)
from policy import MandatePolicy  # noqa: E402
from service import (  # noqa: E402
    ChangeDirection,
    CurrencyMismatchError,
    FundNotFoundError,
    InMemoryMandateVersionRepository,
    MandateVersionService,
)

try:
    from postgres_repository import MandatePersistenceError, PostgresMandateVersionRepository
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

_GOVERNANCE_EVENTS_DIR = _BASE / "governance_events"
sys.path.insert(0, str(_GOVERNANCE_EVENTS_DIR))

from redis_event_bus import (  # noqa: E402
    DEFAULT_GROUP as _GOVERNANCE_EVENT_GROUP,
    DEFAULT_STREAM as _GOVERNANCE_EVENT_STREAM,
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
        risk = dict(base_capital="100000000", currency="KRW", max_instrument_weight="0.1",
                    max_sector_weight="0.3", max_gross_exposure="1.0", max_concurrent_positions=10,
                    max_daily_loss="0.03")
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

    print("ok - CEO Office Domain API 7개 시나리오 점검 통과")
