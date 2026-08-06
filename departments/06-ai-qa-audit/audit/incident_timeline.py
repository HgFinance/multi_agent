"""Sprint K3 경계 위 P0 조각: Incident Timeline과 Corrective Action.

소유: 동규 (AI QA/감사본부)
근거: docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md 6.3(Finding과 Incident 불변식),
      11.2("Incident Timeline에서 확인된 사실과 추론을 별도 Field로 저장"),
      14(DoD "Incident Timeline과 Corrective Action을 Evidence로 재현할 수 있다")
      supabase/migrations/20260729000500_audit_api_security.sql의 audit.incident_events,
      audit.corrective_actions 컬럼·Enum 값과 이름을 맞췄다.

`ops_health_monitor.py`가 Incident을 "감지"까지 했다면, 이 모듈은 그 뒤 실제로 무슨 일이
있었는지(Fact) 와 그걸 어떻게 해석했는지(Inference)를 섞지 않고 남기고, 재발 방지 조치
(Corrective Action)가 QA 검증 없이 조용히 닫히지 않게 한다.

자체 점검: python departments/06-ai-qa-audit/audit/incident_timeline.py
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from repository import PostgresAuditRepository


class IncidentEntryType(StrEnum):
    """audit.incident_events.entry_type과 값이 같아야 한다."""

    FACT = "FACT"
    INFERENCE = "INFERENCE"
    ACTION = "ACTION"
    DECISION = "DECISION"


class CorrectiveActionStatus(StrEnum):
    """audit.corrective_actions.status와 값이 같아야 한다."""

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class IncidentTimelineError(Exception):
    """QA 검증 없이 조용히 Close하거나, Incident/Finding 둘 다 없는 조치를 만들지 않는다."""


@dataclass(frozen=True)
class IncidentEventRecord:
    """audit.incident_events 1행."""

    incident_event_id: UUID
    incident_id: UUID
    source: str
    entry_type: IncidentEntryType
    summary: str
    evidence: dict
    occurred_at: datetime
    recorded_at: datetime
    recorded_by: str


@dataclass
class CorrectiveActionRecord:
    """audit.corrective_actions 1행."""

    corrective_action_id: UUID
    owner: str
    action_plan: dict
    due_at: datetime
    created_at: datetime
    incident_id: UUID | None = None
    finding_id: UUID | None = None
    verification: dict | None = None
    verifier: str | None = None
    status: CorrectiveActionStatus = CorrectiveActionStatus.OPEN
    completed_at: datetime | None = None


class IncidentTimeline:
    """결정론적 Timeline 기록소. Fact와 Inference를 같은 자리에 섞지 않는다."""

    def __init__(self, repository: PostgresAuditRepository | None = None) -> None:
        self.events: list[IncidentEventRecord] = []
        self.corrective_actions: dict[UUID, CorrectiveActionRecord] = {}
        # repository가 있으면 audit.incident_events/corrective_actions에 write-through 한다.
        # incident_id가 가리키는 audit.incidents 부모 행은 이 클래스가 만들지 않으므로,
        # 그 행이 아직 없으면 add_event의 insert는 FK 위반으로 실패한다(repository.py 참고).
        self._repository = repository

    def add_event(
        self,
        incident_id: UUID,
        source: str,
        entry_type: IncidentEntryType,
        summary: str,
        occurred_at: datetime,
        recorded_by: str,
        *,
        evidence: dict | None = None,
    ) -> IncidentEventRecord:
        event = IncidentEventRecord(
            incident_event_id=uuid4(),
            incident_id=incident_id,
            source=source,
            entry_type=entry_type,
            summary=summary,
            evidence=evidence or {},
            occurred_at=occurred_at,
            recorded_at=_now(),
            recorded_by=recorded_by,
        )
        if self._repository is not None:
            self._repository.insert_incident_event(event)
        self.events.append(event)
        return event

    def timeline_for(self, incident_id: UUID) -> list[IncidentEventRecord]:
        """발생 시각(occurred_at) 순서 - 기록된 순서가 아니라 실제 일어난 순서로 재현한다."""
        return sorted(
            (e for e in self.events if e.incident_id == incident_id),
            key=lambda e: e.occurred_at,
        )

    def open_corrective_action(
        self,
        owner: str,
        action_plan: dict,
        due_at: datetime,
        *,
        incident_id: UUID | None = None,
        finding_id: UUID | None = None,
    ) -> CorrectiveActionRecord:
        # audit.corrective_actions의 check (incident_id is not null or finding_id is not null)
        if incident_id is None and finding_id is None:
            raise IncidentTimelineError(
                "Corrective Action은 Incident나 Finding 중 하나에는 연결돼야 합니다"
            )
        action = CorrectiveActionRecord(
            corrective_action_id=uuid4(),
            owner=owner,
            action_plan=action_plan,
            due_at=due_at,
            created_at=_now(),
            incident_id=incident_id,
            finding_id=finding_id,
        )
        if self._repository is not None:
            self._repository.insert_corrective_action(action)
        self.corrective_actions[action.corrective_action_id] = action
        return action

    def start_action(self, corrective_action_id: UUID) -> CorrectiveActionRecord:
        action = self._require_action(corrective_action_id)
        action.status = CorrectiveActionStatus.IN_PROGRESS
        self._sync_action(action)
        return action

    def submit_for_verification(
        self, corrective_action_id: UUID
    ) -> CorrectiveActionRecord:
        action = self._require_action(corrective_action_id)
        action.status = CorrectiveActionStatus.VERIFYING
        self._sync_action(action)
        return action

    def verify_and_close(
        self,
        corrective_action_id: UUID,
        verifier: str,
        verification: dict,
    ) -> CorrectiveActionRecord:
        """QA 검증 후에만 Close한다(팀 가이드 6.3 불변식) - verifier 없이 COMPLETED로
        못 간다. 조치를 만든 사람이 스스로 검증자가 될 수도 없다."""
        action = self._require_action(corrective_action_id)
        if action.status not in (
            CorrectiveActionStatus.IN_PROGRESS,
            CorrectiveActionStatus.VERIFYING,
        ):
            raise IncidentTimelineError(
                f"상태 {action.status.value}에서는 검증·종료할 수 없습니다 (IN_PROGRESS/VERIFYING만 가능)"
            )
        if verifier == action.owner:
            raise IncidentTimelineError(
                "조치를 실행한 담당자가 스스로 검증할 수 없습니다"
            )
        action.status = CorrectiveActionStatus.COMPLETED
        action.verifier = verifier
        action.verification = verification
        action.completed_at = _now()
        self._sync_action(action)
        return action

    def cancel_action(
        self, corrective_action_id: UUID, reason: str
    ) -> CorrectiveActionRecord:
        action = self._require_action(corrective_action_id)
        action.status = CorrectiveActionStatus.CANCELLED
        action.verification = {"cancelled_reason": reason}
        action.completed_at = _now()
        self._sync_action(action)
        return action

    def _sync_action(self, action: CorrectiveActionRecord) -> None:
        if self._repository is not None:
            self._repository.update_corrective_action(action)

    def _require_action(self, corrective_action_id: UUID) -> CorrectiveActionRecord:
        action = self.corrective_actions.get(corrective_action_id)
        if action is None:
            raise IncidentTimelineError(
                f"존재하지 않는 Corrective Action입니다: {corrective_action_id}"
            )
        return action


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_default_incident_timeline() -> IncidentTimeline:
    """QA_INCIDENT_PERSIST=true + DATABASE_URL(또는 RISK_QA_DATABASE_URL)이 모두 있을 때만
    Postgres에 쓴다. 기본은 인메모리라 테스트/개발 환경은 DB 없이 같은 경로를 돈다 -
    worker_trace_bridge.build_default_trace_bridge와 같은 production/test 분리 기준이다."""
    import os

    if os.environ.get("QA_INCIDENT_PERSIST", "false").strip().lower() != "true":
        return IncidentTimeline()
    dsn = (
        os.environ.get("RISK_QA_DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )
    if not dsn:
        raise IncidentTimelineError("QA_INCIDENT_PERSIST requires DATABASE_URL")
    try:
        from repository import PostgresAuditRepository
    except ImportError:
        from audit.repository import PostgresAuditRepository
    return IncidentTimeline(PostgresAuditRepository.connect(dsn))


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    incident_id, finding_id = uuid4(), uuid4()
    timeline = IncidentTimeline()

    def raises(fn, why: str):
        try:
            fn()
        except IncidentTimelineError:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    # 1. FACT와 INFERENCE를 같은 Incident에 섞어도 각각 구분돼 기록된다
    fact = timeline.add_event(
        incident_id,
        "agent-ops-monitor",
        IncidentEntryType.FACT,
        "research-department 에러율이 5분간 15%로 관측됨",
        now,
        "svc_audit_collector",
        evidence={"error_count": 150, "request_count": 1000},
    )
    inference = timeline.add_event(
        incident_id,
        "incident-postmortem-agent",
        IncidentEntryType.INFERENCE,
        "market-api 응답 지연이 원인으로 추정됨 (확정 아님)",
        now + timedelta(minutes=2),
        "svc_audit_collector",
    )
    assert fact.entry_type is IncidentEntryType.FACT
    assert inference.entry_type is IncidentEntryType.INFERENCE
    assert fact.evidence["error_count"] == 150

    # 2. Timeline은 occurred_at 순서로 재현된다 (기록 순서가 아니라 실제 발생 순서)
    late_fact = timeline.add_event(
        incident_id,
        "agent-ops-monitor",
        IncidentEntryType.FACT,
        "복구 확인",
        now + timedelta(minutes=1),
        "svc_audit_collector",
    )
    ordered = timeline.timeline_for(incident_id)
    assert [e.summary for e in ordered] == [
        fact.summary,
        late_fact.summary,
        inference.summary,
    ], "occurred_at 순서로 정렬 안 됨"

    # 3. Incident에만 연결된 Corrective Action
    action1 = timeline.open_corrective_action(
        "research-department",
        {"plan": "market-api 타임아웃 값 상향"},
        now + timedelta(days=3),
        incident_id=incident_id,
    )
    assert action1.status is CorrectiveActionStatus.OPEN

    # 4. Finding에만 연결된 Corrective Action
    action2 = timeline.open_corrective_action(
        "research-department",
        {"plan": "Evidence Curator 인용 로직 수정"},
        now + timedelta(days=5),
        finding_id=finding_id,
    )
    assert action2.finding_id == finding_id and action2.incident_id is None

    # 5. Incident도 Finding도 없는 Corrective Action은 거부
    raises(
        lambda: timeline.open_corrective_action("x", {}, now + timedelta(days=1)),
        "Incident/Finding 둘 다 없는 조치",
    )

    # 6. IN_PROGRESS를 거치지 않고 바로 검증·종료 -> 거부 (상태 순서 강제)
    raises(
        lambda: timeline.verify_and_close(
            action1.corrective_action_id, "qa-audit-supervisor", {}
        ),
        "OPEN에서 바로 검증·종료",
    )

    # 7. 정상 흐름: 시작 -> 검증 제출 -> QA가 검증하고 종료
    timeline.start_action(action1.corrective_action_id)
    timeline.submit_for_verification(action1.corrective_action_id)
    closed = timeline.verify_and_close(
        action1.corrective_action_id,
        "qa-audit-supervisor",
        {"checked": "타임아웃 값 반영 확인함"},
    )
    assert closed.status is CorrectiveActionStatus.COMPLETED
    assert closed.verifier == "qa-audit-supervisor"
    assert closed.completed_at is not None

    # 8. 조치를 실행한 담당자 본인이 검증자로 스스로 종료할 수 없다
    timeline.start_action(action2.corrective_action_id)
    raises(
        lambda: timeline.verify_and_close(
            action2.corrective_action_id, "research-department", {}
        ),
        "본인이 본인 조치를 검증",
    )

    # 9. 취소 경로도 있다 - 종결 상태로 남되 COMPLETED와는 구분됨
    action3 = timeline.open_corrective_action(
        "x", {}, now + timedelta(days=1), incident_id=incident_id
    )
    cancelled = timeline.cancel_action(
        action3.corrective_action_id, "더 이상 필요 없음"
    )
    assert cancelled.status is CorrectiveActionStatus.CANCELLED

    print("ok - Incident Timeline/Corrective Action 9개 시나리오 점검 통과")
