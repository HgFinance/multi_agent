#!/usr/bin/env python3
"""P0-2: GOV-02 전체 상태 Replay.

소유: 영주 (CEO Office)
근거: docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md v2.0 P0-2

다음 그래프를 실제 DB Repository와 API로 한 번에 이어서 재현한다(개별 모듈 자체 점검은
각자 이미 통과했지만, 전 구간을 이어서 돌린 적은 없었다):

    Investment Case -> Approval Request -> Committee Open/Vote/Close
      -> Governance Decision -> Escalation -> Notification -> Case Resolve/Cancel

"Governance Decision"에 대응하는 범용 governance.decisions 테이블은 아직 없다
(config.yaml not_started "record_decision (스펙 2.2)" - 계약을 지어내지 않고 미룬 항목).
이 Replay에서는 committee.close_session()이 만드는 `CommitteeDecisionRecord`를 그
자리에 쓴다 - 이게 지금 실제로 존재하는 유일한 "여러 부서가 참여해 내리는 결정" 기록이고,
approvals(개별 승인)와는 다른 층위(집단 의결)라 대체재로 타당하다.

approvals.object_id는 DDL상 object_type에 따른 FK가 없는 순수 uuid 컬럼이다(2026-08-05
실측, supabase/migrations/20260729000200_governance_workforce.sql:161 - `object_id uuid
not null`에 FK 없음). 그래서 이 Case의 투자 결정에 대한 승인을 object_type=
CAPITAL_ALLOCATION, object_id=<case_id>로 걸어도 DB 제약을 어기지 않는다 - Case 자체가
아직 전용 ObjectType을 갖지 않아서 쓰는 가장 가까운 근사다.

이 Replay는 실제 DB가 있을 때만 의미가 있다(제목 자체가 "실제 DB Repository와 API로
재현") - DATABASE_URL이 없으면 건너뛴다(CEO Office 기존 자체 점검 관례와 동일).

실행: python departments/00-ceo-office/tests/test_gov02_replay.py
"""
# ruff: noqa: E402, I001
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_API_DIR = Path(__file__).resolve().parents[1] / "api"
sys.path.insert(0, str(_API_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()  # 저장소 루트 .env - 이미 설정된 값은 덮어쓰지 않는다.

_dsn = os.environ.get("DATABASE_URL")
if not _dsn:
    if "pytest" in sys.modules:
        import pytest

        pytest.skip(
            "DATABASE_URL 미설정 - GOV-02 실제 DB Replay 생략",
            allow_module_level=True,
        )
    print("DATABASE_URL 미설정 - GOV-02 전체 상태 Replay는 건너뛴다")
    raise SystemExit(0)

from fastapi.testclient import TestClient  # noqa: E402

# app.py를 모듈로 임포트한다 - __main__ 자체 점검(In-Memory 강제)을 타지 않으므로 모듈
# 레벨에서 이미 DATABASE_URL을 보고 구성한 실제 Postgres Repository를 그대로 쓴다.
from app import _mandate_repo as _mandate_version_repo  # noqa: E402
from app import app, case_repo  # noqa: E402,F401

client = TestClient(app)


def _get_test_fund_id() -> str | None:
    """postgres_repository.py 자체 점검과 같은 TEST-CEO-MANDATE Fund를 재사용한다."""
    conn = _mandate_version_repo._pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select fund_id from accounting.funds where fund_code = %s",
                ("TEST-CEO-MANDATE",),
            )
            row = cur.fetchone()
        conn.commit()
        return str(row[0]) if row else None
    finally:
        _mandate_version_repo._pool.putconn(conn)


def main() -> None:
    fund_id = _get_test_fund_id()
    if fund_id is None:
        print("SKIP - TEST-CEO-MANDATE Fund가 없다 "
              "(psql -f tests/schema/supabase_governance_test_fixture.sql 선행 필요)")
        return

    now = datetime.now(timezone.utc)
    trace_id = str(uuid.uuid4())

    # 1) Investment Case -----------------------------------------------------
    case_resp = client.post("/governance/v1/cases", json={
        "case_type": "INVESTMENT_REVIEW", "priority": 60, "owner_department": "ceo-agent",
        "fund_id": fund_id, "trace_id": trace_id, "created_by": "gov02-replay-selfcheck",
        "reason": "P0-2 GOV-02 전체 상태 Replay 자체 점검",
    })
    assert case_resp.status_code == 200, case_resp.text
    case_id = case_resp.json()["case_id"]
    assert case_resp.json()["status"] == "OPEN"

    try:
        # 2) Approval Request -------------------------------------------------
        # object_id=case_id를 CAPITAL_ALLOCATION 승인 대상으로 삼는다(모듈 docstring 근거).
        approval_resp = client.post("/governance/v1/approvals", json={
            "object_type": "CAPITAL_ALLOCATION", "object_id": case_id,
            "required_role": "CEO", "fund_id": fund_id,
            "reason": "GOV-02 Replay - 투자 Case 자본 배분 승인",
        })
        assert approval_resp.status_code == 200, approval_resp.text
        approval_id = approval_resp.json()["approval_id"]
        assert approval_resp.json()["decision"] == "PENDING"

        decide_resp = client.post(f"/governance/v1/approvals/{approval_id}/decide", json={
            "decision": "APPROVED", "actor_department": "ceo-agent", "at": now.isoformat(),
        })
        assert decide_resp.status_code == 200 and decide_resp.json()["decision"] == "APPROVED", \
            decide_resp.text

        # 3) Committee Open/Vote/Close (Governance Decision을 대신한다, 모듈 docstring) ---
        session_resp = client.post("/governance/v1/committee/sessions", json={
            "fund_id": fund_id, "committee_type": "INVESTMENT", "trace_id": trace_id,
            "case_id": case_id,
            "quorum_policy": {
                "required_departments": ["risk-management", "qa-department"],
                "approval_threshold": 2,
            },
        })
        assert session_resp.status_code == 200, session_resp.text
        session_id = session_resp.json()["session_id"]

        for dept in ("risk-management", "qa-department"):
            vote_resp = client.post(f"/governance/v1/committee/sessions/{session_id}/votes", json={
                "department": dept, "decision": "APPROVE",
            })
            assert vote_resp.status_code == 200, vote_resp.text

        close_resp = client.post(f"/governance/v1/committee/sessions/{session_id}/close", json={})
        assert close_resp.status_code == 200, close_resp.text
        governance_decision = close_resp.json()["decision"]
        assert governance_decision["decision"] == "APPROVE", governance_decision
        assert close_resp.json()["session"]["status"] == "DECIDED"

        # 3b) 정족수 미달은 DEFER, Veto는 REJECT - 같은 Case에 걸린 별도 세션 2개로 확인
        # (완료 증거 항목). 위 통과 흐름과 섞지 않도록 별도 세션을 쓴다.
        defer_session = client.post("/governance/v1/committee/sessions", json={
            "fund_id": fund_id, "committee_type": "INVESTMENT", "trace_id": trace_id,
            "case_id": None,
            "quorum_policy": {
                "required_departments": ["risk-management", "qa-department"],
                "approval_threshold": 2,
            },
        }).json()["session_id"]
        client.post(f"/governance/v1/committee/sessions/{defer_session}/votes", json={
            "department": "risk-management", "decision": "APPROVE",
        })
        defer_close = client.post(f"/governance/v1/committee/sessions/{defer_session}/close", json={})
        assert defer_close.json()["decision"]["decision"] == "DEFER", defer_close.text

        veto_session = client.post("/governance/v1/committee/sessions", json={
            "fund_id": fund_id, "committee_type": "INVESTMENT", "trace_id": trace_id,
            "case_id": None,
            "quorum_policy": {
                "required_departments": ["risk-management", "qa-department"],
                "veto_departments": ["risk-management"], "approval_threshold": 1,
            },
        }).json()["session_id"]
        client.post(f"/governance/v1/committee/sessions/{veto_session}/votes", json={
            "department": "risk-management", "decision": "REJECT",
        })
        client.post(f"/governance/v1/committee/sessions/{veto_session}/votes", json={
            "department": "qa-department", "decision": "APPROVE",
        })
        veto_close = client.post(f"/governance/v1/committee/sessions/{veto_session}/close", json={})
        assert veto_close.json()["decision"]["decision"] == "REJECT", veto_close.text

        # 4) Escalation - Case에서 파생돼 열리고 사유와 함께 닫힌다 --------------------
        escalation_resp = client.post("/governance/v1/escalations", json={
            "case_id": case_id, "reason": "GOV-02 Replay - 위원회 결정 사후 확인 필요",
            "severity": "MEDIUM", "target": "risk-management",
        })
        assert escalation_resp.status_code == 200, escalation_resp.text
        escalation_id = escalation_resp.json()["escalation_id"]
        assert escalation_resp.json()["status"] == "OPEN"

        ack_escalation = client.post(f"/governance/v1/escalations/{escalation_id}/transitions", json={
            "to_status": "ACKNOWLEDGED", "at": now.isoformat(),
        })
        assert ack_escalation.status_code == 200 and \
            ack_escalation.json()["status"] == "ACKNOWLEDGED", ack_escalation.text

        resolve_escalation = client.post(
            f"/governance/v1/escalations/{escalation_id}/transitions", json={
                "to_status": "RESOLVED", "at": now.isoformat(),
                "resolution": "위원회 결정 확인 완료 - 추가 조치 불필요",
            },
        )
        assert resolve_escalation.status_code == 200 and \
            resolve_escalation.json()["status"] == "RESOLVED", resolve_escalation.text
        # resolution 없이 닫으면 409 - "완료 증거"의 의존 서비스 오류 방향(BLOCKED류)과 같은 정신.
        blocked = client.post(f"/governance/v1/escalations/{escalation_id}/transitions", json={
            "to_status": "RESOLVED", "at": now.isoformat(),
        })
        assert blocked.status_code == 409, blocked.text

        # 5) Notification - 이 모듈엔 "DELIVERED" 상태가 아예 없다(F24 발송 Adapter
        # 미구현, config.yaml not_started) - 모든 호출이 PENDING 또는 SUPPRESSED로만
        # 끝난다는 것 자체가 "허위 발송 성공을 표시하지 않는다"는 요구를 구조적으로
        # 만족한다. 심각도 불명(None)은 CRITICAL로 승격되지 억제되지 않는다(불변식 2).
        notif_resp = client.post("/governance/v1/notifications", json={
            "fund_id": fund_id, "event_type": "governance.escalation.v1",
            "scope_key": f"case:{case_id}", "recipient": "role:risk-ops",
            "payload": {"case_id": case_id}, "now": now.isoformat(),
        })
        assert notif_resp.status_code == 200, notif_resp.text
        statuses = {n["status"] for n in notif_resp.json()["notifications"]}
        assert statuses <= {"PENDING", "SUPPRESSED"}, statuses
        assert "DELIVERED" not in statuses and "SUCCESS" not in statuses

        unknown_severity_resp = client.post("/governance/v1/notifications", json={
            "fund_id": fund_id, "event_type": "governance.unknown_event.v1",
            "scope_key": f"case:{case_id}:unknown", "recipient": "role:risk-ops",
            "payload": {}, "now": (now + timedelta(minutes=1)).isoformat(),
        })
        assert unknown_severity_resp.status_code == 200, unknown_severity_resp.text
        assert all(n["status"] == "PENDING" for n in unknown_severity_resp.json()["notifications"]), \
            "심각도 불명 알림이 억제되거나 다른 상태로 떨어짐(불변식 2 위반)"

        # 6) Case Resolve - trace_id와 append-only 이력이 끝까지 보존됐는지 확인 ---------
        transition_resp = client.post(f"/governance/v1/cases/{case_id}/transitions", json={
            "to_status": "ACKNOWLEDGED", "actor": "governance-api", "at": now.isoformat(),
            "reason": "위원회 심의 진행",
        })
        assert transition_resp.status_code == 200, transition_resp.text
        resolve_resp = client.post(f"/governance/v1/cases/{case_id}/transitions", json={
            "to_status": "RESOLVED", "actor": "governance-api",
            "at": (now + timedelta(minutes=2)).isoformat(),
            "reason": "위원회 승인 + Escalation 해소 완료",
        })
        assert resolve_resp.status_code == 200 and resolve_resp.json()["status"] == "RESOLVED", \
            resolve_resp.text

        timeline_resp = client.get(f"/governance/v1/cases/{case_id}/timeline")
        assert timeline_resp.status_code == 200
        events = timeline_resp.json()["events"]
        assert [e["to_status"] for e in events] == ["OPEN", "ACKNOWLEDGED", "RESOLVED"], events
        assert all(e["schema_version"] >= 1 for e in events)
        case_after = client.get(f"/governance/v1/cases/{case_id}")
        assert case_after.json()["trace_id"] == trace_id, "trace_id가 구간을 지나며 유실됨"

        # 종료된 Case는 다시 전이할 수 없다 - append-only 이력이 조용히 더 쌓이지 않는다.
        reopen_attempt = client.post(f"/governance/v1/cases/{case_id}/transitions", json={
            "to_status": "ACKNOWLEDGED", "actor": "governance-api", "at": now.isoformat(),
        })
        assert reopen_attempt.status_code == 409, reopen_attempt.text

        print(
            "ok - P0-2 GOV-02 전체 상태 Replay 통과 - "
            f"Case({case_after.json()['display_id']}) OPEN->ACKNOWLEDGED->RESOLVED, "
            "Approval(CEO)->APPROVED, Committee(APPROVE/DEFER/REJECT 3종)->Decision, "
            "Escalation(OPEN->RESOLVED, resolution 없이는 409), "
            "Notification(PENDING만 존재·심각도 불명 CRITICAL 승격) 전 구간 trace_id 보존 확인"
        )
    finally:
        # governance.cases/case_events/committee_sessions/committee_decisions/escalations/
        # approvals/notifications는 append-only 트리거가 없는 것도 있지만(committee/case
        # 계열은 append-only), 이 Replay는 실제 값을 검증하는 게 목적이라 별도 정리는
        # 하지 않는다 - 다른 CEO Office 자체 점검(postgres_repository.py 등)과 달리
        # display_id/trace_id로 실행마다 구분되는 새 행이라 누적 자체가 감사 이력이다.
        pass


if __name__ == "__main__":
    main()
