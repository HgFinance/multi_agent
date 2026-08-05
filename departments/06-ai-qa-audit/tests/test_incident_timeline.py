"""incident_timeline.py의 __main__ 자체 점검을 pytest로 옮긴 것.

소유: 동규 (AI QA/감사본부). repository 없는 기본 IncidentTimeline()을 쓰므로 DB 의존이
없다 - 원본과 동일하게 9개 시나리오를 검증한다. 각 테스트는 독립적으로 실행되도록
IncidentTimeline을 새로 만든다(원본은 순서대로 이어지는 단일 흐름이었다).

실행: python -m pytest departments/06-ai-qa-audit/tests/test_incident_timeline.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "audit"))

from incident_timeline import (
    CorrectiveActionStatus,
    IncidentEntryType,
    IncidentTimeline,
    IncidentTimelineError,
)

now = datetime.now(timezone.utc)
incident_id, finding_id = uuid4(), uuid4()


def raises(fn, why: str):
    with pytest.raises(IncidentTimelineError):
        fn()


def test_01_fact_and_inference_recorded_distinctly():
    timeline = IncidentTimeline()
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


def test_02_timeline_ordered_by_occurred_at_not_record_order():
    timeline = IncidentTimeline()
    fact = timeline.add_event(
        incident_id,
        "agent-ops-monitor",
        IncidentEntryType.FACT,
        "research-department 에러율이 5분간 15%로 관측됨",
        now,
        "svc_audit_collector",
    )
    inference = timeline.add_event(
        incident_id,
        "incident-postmortem-agent",
        IncidentEntryType.INFERENCE,
        "market-api 응답 지연이 원인으로 추정됨 (확정 아님)",
        now + timedelta(minutes=2),
        "svc_audit_collector",
    )
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


def test_03_corrective_action_linked_to_incident_only():
    timeline = IncidentTimeline()
    action1 = timeline.open_corrective_action(
        "research-department",
        {"plan": "market-api 타임아웃 값 상향"},
        now + timedelta(days=3),
        incident_id=incident_id,
    )
    assert action1.status is CorrectiveActionStatus.OPEN


def test_04_corrective_action_linked_to_finding_only():
    timeline = IncidentTimeline()
    action2 = timeline.open_corrective_action(
        "research-department",
        {"plan": "Evidence Curator 인용 로직 수정"},
        now + timedelta(days=5),
        finding_id=finding_id,
    )
    assert action2.finding_id == finding_id and action2.incident_id is None


def test_05_corrective_action_without_incident_or_finding_rejected():
    timeline = IncidentTimeline()
    raises(
        lambda: timeline.open_corrective_action("x", {}, now + timedelta(days=1)),
        "Incident/Finding 둘 다 없는 조치",
    )


def test_06_verify_close_before_in_progress_rejected():
    timeline = IncidentTimeline()
    action1 = timeline.open_corrective_action(
        "research-department",
        {"plan": "market-api 타임아웃 값 상향"},
        now + timedelta(days=3),
        incident_id=incident_id,
    )
    raises(
        lambda: timeline.verify_and_close(
            action1.corrective_action_id, "qa-audit-supervisor", {}
        ),
        "OPEN에서 바로 검증·종료",
    )


def test_07_normal_flow_start_submit_verify_close():
    timeline = IncidentTimeline()
    action1 = timeline.open_corrective_action(
        "research-department",
        {"plan": "market-api 타임아웃 값 상향"},
        now + timedelta(days=3),
        incident_id=incident_id,
    )
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


def test_08_owner_cannot_self_verify():
    timeline = IncidentTimeline()
    action2 = timeline.open_corrective_action(
        "research-department",
        {"plan": "Evidence Curator 인용 로직 수정"},
        now + timedelta(days=5),
        finding_id=finding_id,
    )
    timeline.start_action(action2.corrective_action_id)
    raises(
        lambda: timeline.verify_and_close(
            action2.corrective_action_id, "research-department", {}
        ),
        "본인이 본인 조치를 검증",
    )


def test_write_through_failure_does_not_leave_memory_state():
    class FailingRepository:
        def insert_incident_event(self, _event):
            raise RuntimeError("database unavailable")

        def insert_corrective_action(self, _action):
            raise RuntimeError("database unavailable")

    timeline = IncidentTimeline(FailingRepository())

    with pytest.raises(RuntimeError):
        timeline.add_event(
            incident_id,
            "agent-ops-monitor",
            IncidentEntryType.FACT,
            "DB 저장 실패 테스트",
            now,
            "qa-test",
            evidence={},
        )
    assert timeline.events == []

    with pytest.raises(RuntimeError):
        timeline.open_corrective_action(
            "qa-test",
            {"plan": "DB 저장 실패 테스트"},
            now + timedelta(days=1),
            incident_id=incident_id,
        )
    assert timeline.corrective_actions == {}


def test_09_cancel_path_reaches_terminal_state_distinct_from_completed():
    timeline = IncidentTimeline()
    action3 = timeline.open_corrective_action(
        "x", {}, now + timedelta(days=1), incident_id=incident_id
    )
    cancelled = timeline.cancel_action(
        action3.corrective_action_id, "더 이상 필요 없음"
    )
    assert cancelled.status is CorrectiveActionStatus.CANCELLED
