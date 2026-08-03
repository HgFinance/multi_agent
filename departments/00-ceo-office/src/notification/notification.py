#!/usr/bin/env python3
"""F24: Notification — Feed/Risk/Order Incident 알림.

소유: 영주 (CEO Office)
근거: docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F24,
      docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md 3.1(governance.notifications),
      docs/02-engineering/GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 5.3(report.ready.v1 등
      Governance/Workforce Stream 소비)

여기에 LLM은 없다. 알림 채널·중복 억제 판정은 결정론적 코드만 한다 (CLAUDE.md
"LLM은 관련성 판단과 서술 작성에만 쓴다").

CEO는 Risk/QA/회계의 원본 판정을 다시 계산하지 않는다 (팀 가이드 3.1). 이 모듈은
이미 확정된 Domain Event(risk.breach.v1, qa.finding.v1, incident.opened.v1,
governance.escalation.v1, report.ready.v1 등)를 받아 "누구에게 어떤 채널로,
지금 보낼지 억제할지"만 판정하고 governance.notifications 행으로 매핑한다.

불변식:
  1. CRITICAL은 절대 억제(SUPPRESSED)하지 않는다. 중복 폭주를 줄이려고 안전 신호를
     누락시키면 안 된다 (개발 원칙 9: 위험한 기능은 실패 시 확대가 아니라 차단 —
     여기서 "차단"은 알림을 누락하는 쪽이 아니라 계속 통과시키는 쪽이다).
  2. 심각도를 판정할 수 없으면(None·미지 값) 가장 안전한 방향인 CRITICAL로 취급한다.
     경중을 알 수 없는 신호를 조용히 낮은 우선순위로 내리지 않는다.
  3. dedup_key는 (fund_id, event_type, scope_key)로만 결정된다 — 시각을 섞지 않는다.
     같은 사안은 항상 같은 key를 가져야 억제 판정 시점에 과거 이력을 조회할 수 있다.
  4. 채널·수신자 선택은 표 조회로만 한다. LLM이나 휴리스틱 점수화를 쓰지 않는다.

자체 점검: python departments/00-ceo-office/src/notification/notification.py
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Channel(str, Enum):
    APP = "APP"
    EMAIL = "EMAIL"
    PUSH = "PUSH"
    SMS = "SMS"


class NotificationStatus(str, Enum):
    PENDING = "PENDING"
    SUPPRESSED = "SUPPRESSED"


# 심각도별 채널 — governance.escalations.severity 값 체계를 그대로 쓴다.
CHANNELS_BY_SEVERITY: dict[Severity, tuple[Channel, ...]] = {
    Severity.CRITICAL: (Channel.SMS, Channel.PUSH, Channel.APP),
    Severity.HIGH: (Channel.PUSH, Channel.APP),
    Severity.MEDIUM: (Channel.APP, Channel.EMAIL),
    Severity.LOW: (Channel.APP,),
}

# 같은 dedup_key로 이 기간 안에 이미 보냈으면 억제한다. CRITICAL은 쿨다운이 없다
# (불변식 1) — 값을 아예 안 둬서 "깜빡하고 억제 조건에 넣는" 실수를 구조적으로 막는다.
COOLDOWN_BY_SEVERITY: dict[Severity, timedelta] = {
    Severity.LOW: timedelta(hours=6),
    Severity.MEDIUM: timedelta(hours=1),
    Severity.HIGH: timedelta(minutes=15),
}


def coerce_severity(value: str | None) -> Severity:
    """알 수 없는 심각도는 CRITICAL로 취급한다 (불변식 2)."""
    if value is None:
        return Severity.CRITICAL
    try:
        return Severity(value)
    except ValueError:
        return Severity.CRITICAL


def _canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def compute_dedup_key(*, fund_id: str, event_type: str, scope_key: str) -> str:
    """(fund_id, event_type, scope_key)의 안정적 해시. 시각을 섞지 않는다 (불변식 3)."""
    payload = {"fund_id": fund_id, "event_type": event_type, "scope_key": scope_key}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NotificationRequest:
    """소비하는 Domain Event 한 건 (API 설계서 5.2/5.3 Envelope의 payload 요약)."""

    fund_id: str
    event_type: str
    scope_key: str  # 무엇에 대한 알림인지 식별 (예: "case:uuid", "department:03-risk")
    recipient: str
    payload: dict
    severity: str | None = None  # None·미지 값은 CRITICAL로 승격 (불변식 2)


@dataclass(frozen=True)
class NotificationRow:
    """governance.notifications 한 행. notification_id/created_at은 DB 기본값에 맡긴다."""

    fund_id: str
    event_type: str
    recipient: str
    channel: Channel
    payload: dict
    dedup_key: str
    status: NotificationStatus
    sent_at: datetime | None = None


class NotificationRepository:
    """조회·저장 인터페이스. 실제 구현은 governance.notifications에 반영한다.

    created_at은 호출자(Service)가 판정에 쓴 `now`를 그대로 넘긴다 — Repository가
    벽시계를 다시 읽으면 Service가 검사한 cooldown 기준과 저장된 시각이 어긋난다.
    """

    def recent_by_dedup_key(self, dedup_key: str, *, since: datetime) -> list[NotificationRow]:
        raise NotImplementedError

    def insert(self, row: NotificationRow, *, created_at: datetime) -> None:
        raise NotImplementedError


class InMemoryNotificationRepository(NotificationRepository):
    def __init__(self) -> None:
        self._rows: list[tuple[NotificationRow, datetime]] = []

    def recent_by_dedup_key(self, dedup_key: str, *, since: datetime) -> list[NotificationRow]:
        return [
            row for row, created_at in self._rows
            if row.dedup_key == dedup_key and created_at >= since
        ]

    def insert(self, row: NotificationRow, *, created_at: datetime) -> None:
        self._rows.append((row, created_at))


class NotificationService:
    """F24 알림 판정·저장 서비스."""

    def __init__(self, repo: NotificationRepository) -> None:
        self._repo = repo

    def notify(self, request: NotificationRequest, *, now: datetime) -> list[NotificationRow]:
        """요청 하나를 심각도별 채널로 펼치고, 중복이면 억제한 채로 기록한다.

        모든 채널 행을 만들어 저장한다 — 억제된 행도 감사 이력을 위해 남긴다
        (governance.notifications.status='SUPPRESSED'). 억제는 "안 만듦"이 아니라
        "만들었지만 안 보냄"이다.
        """
        severity = coerce_severity(request.severity)
        dedup_key = compute_dedup_key(
            fund_id=request.fund_id, event_type=request.event_type, scope_key=request.scope_key,
        )

        cooldown = COOLDOWN_BY_SEVERITY.get(severity)
        recent = self._repo.recent_by_dedup_key(dedup_key, since=now - cooldown) if cooldown else []
        # 실제로 보낸(PENDING) 이력만 cooldown 기준으로 본다 — 억제(SUPPRESSED)된 행이
        # 다시 cooldown 창을 늘리면 한 번 억제된 뒤 영원히 억제되는 것과 같아진다.
        already_sent = any(r.status == NotificationStatus.PENDING for r in recent)
        # 불변식 1 — CRITICAL은 cooldown이 없으므로 already_sent가 항상 False.
        status = NotificationStatus.SUPPRESSED if already_sent else NotificationStatus.PENDING

        rows = [
            NotificationRow(
                fund_id=request.fund_id,
                event_type=request.event_type,
                recipient=request.recipient,
                channel=channel,
                payload=request.payload,
                dedup_key=dedup_key,
                status=status,
            )
            for channel in CHANNELS_BY_SEVERITY[severity]
        ]
        for row in rows:
            self._repo.insert(row, created_at=now)
        return rows


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timezone

    t0 = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)

    def req(severity="LOW", scope="case:1", event="risk.breach.v1") -> NotificationRequest:
        return NotificationRequest(
            fund_id="fund-1", event_type=event, scope_key=scope,
            recipient="user:u1", payload={"reason": "테스트"}, severity=severity,
        )

    # 1) 최초 발생 — LOW는 APP 채널 하나, PENDING.
    repo = InMemoryNotificationRepository()
    svc = NotificationService(repo)
    rows = svc.notify(req(severity="LOW"), now=t0)
    assert [r.channel for r in rows] == [Channel.APP]
    assert all(r.status == NotificationStatus.PENDING for r in rows)

    # 2) 같은 dedup_key로 cooldown(6시간) 안에 재발생 — 억제.
    rows2 = svc.notify(req(severity="LOW"), now=t0 + timedelta(hours=1))
    assert all(r.status == NotificationStatus.SUPPRESSED for r in rows2), rows2

    # 3) cooldown 지나면 다시 PENDING.
    rows3 = svc.notify(req(severity="LOW"), now=t0 + timedelta(hours=7))
    assert all(r.status == NotificationStatus.PENDING for r in rows3)

    # 4) CRITICAL은 반복 발생해도 절대 억제하지 않는다 (불변식 1).
    repo_c = InMemoryNotificationRepository()
    svc_c = NotificationService(repo_c)
    first = svc_c.notify(req(severity="CRITICAL", scope="case:2"), now=t0)
    assert all(r.status == NotificationStatus.PENDING for r in first)
    second = svc_c.notify(req(severity="CRITICAL", scope="case:2"), now=t0 + timedelta(seconds=1))
    assert all(r.status == NotificationStatus.PENDING for r in second), "CRITICAL이 억제됐다"
    assert {r.channel for r in first} == {Channel.SMS, Channel.PUSH, Channel.APP}

    # 5) 심각도 None·미지 값은 CRITICAL로 승격한다 (불변식 2).
    assert coerce_severity(None) is Severity.CRITICAL
    assert coerce_severity("알수없음") is Severity.CRITICAL
    assert coerce_severity("HIGH") is Severity.HIGH

    # 6) dedup_key는 fund/event/scope로만 결정된다 — scope가 다르면 다른 key.
    k1 = compute_dedup_key(fund_id="f1", event_type="risk.breach.v1", scope_key="case:1")
    k2 = compute_dedup_key(fund_id="f1", event_type="risk.breach.v1", scope_key="case:2")
    k3 = compute_dedup_key(fund_id="f1", event_type="risk.breach.v1", scope_key="case:1")
    assert k1 != k2 and k1 == k3

    # 7) 채널 매핑이 심각도별로 다르다.
    assert CHANNELS_BY_SEVERITY[Severity.HIGH] == (Channel.PUSH, Channel.APP)
    assert CHANNELS_BY_SEVERITY[Severity.MEDIUM] == (Channel.APP, Channel.EMAIL)

    # 8) 억제된 행도 감사를 위해 저장은 된다 (조용히 버리지 않는다).
    assert len(repo._rows) == 1 + 1 + 1  # LOW 최초 1건 + 억제 1건 + cooldown 이후 1건

    print("ok - F24 Notification 판정 8개 시나리오 통과")
