#!/usr/bin/env python3
"""Sprint K2/K3 연장: TraceRecorder/IncidentTimeline의 psycopg2 저장 계층.

소유: 동규 (AI QA/감사본부)
근거: trace_recorder.py, incident_timeline.py의 dataclass 필드는 이미
      supabase/migrations/20260729000500_audit_api_security.sql의 audit.agent_runs/
      tool_calls/incident_events/corrective_actions 컬럼과 1:1로 맞춰져 있다 -
      여기서는 그 매핑을 그대로 INSERT/UPDATE로 옮긴다.
      패턴은 departments/03-risk/engine/trading_state_store.py(RedisTradingStateStore)를
      따른다 - 인메모리가 여전히 불변식 검사의 근거(hot state)고, 여기(Postgres)는
      "공식 이력"이라 쓰기만 실패해도 조용히 삼키지 않는다(트레이싱 기록을 잃지 않는다는
      TraceRecorderError/IncidentTimelineError 원칙과 동일).
      asyncpg가 아니라 psycopg2를 쓰는 이유: TraceRecorder/IncidentTimeline과 이들을 부르는
      api/app.py 라우트가 전부 동기 코드다(트레이딩본부 oms.py의 ponytail 주석이 예고한
      psycopg 방향과 같다) - workforce F19(asyncpg)는 그쪽 도메인이 이미 비동기라 다르다.

불변식:
  1. 접속 문자열은 .env의 DATABASE_URL만 쓴다. 비밀번호/service_role Key를 로그에 남기지 않는다.
  2. audit.tool_calls, audit.incident_events는 DB 트리거로 append-only다(update/delete 거부) -
     tool_calls는 종결 상태(DENIED/COMPLETED/FAILED)에 도달했을 때 그 최종 스냅샷 1행만
     insert한다(REQUESTED/ALLOWED 같은 중간 상태는 DB에 쓰지 않는다 - update가 막혀 있어서다).
  3. audit.agent_runs.agent_id/profile_version_id는 workforce.agent_profiles/
     agent_profile_versions에 대한 not null FK다. 그 두 테이블이 비어 있는 동안(HR 배정 전)
     insert_run은 FK 위반으로 실패한다 - 가짜 workforce 행을 만들어 우회하지 않는다.
  4. audit.incident_events.incident_id는 audit.incidents에 대한 not null FK다. 이 모듈도
     IncidentTimeline도 audit.incidents 부모 행을 만들지 않으므로, 그 행이 먼저 없으면
     insert_incident_event는 FK 위반으로 실패한다 - 마찬가지로 우회하지 않는다.

이 모듈은 live DB를 요구하므로 __main__ 자체 점검이 없다(F19 repository.py와 같은 이유).
검증은 tests/에서 DATABASE_URL이 있을 때만 도는 통합 테스트로 한다.
"""
from __future__ import annotations

from evidence_qa_engine import QaAssessment
from incident_timeline import CorrectiveActionRecord, IncidentEventRecord
from psycopg2.extras import Json, register_uuid
from psycopg2.pool import ThreadedConnectionPool
from trace_recorder import AgentRunRecord, ToolCallRecord

register_uuid()


class QaDecisionPersistenceError(RuntimeError):
    """Canonical QA Decision을 기록하지 못한 경우."""


class PostgresAuditRepository:
    """psycopg2 기반 audit 스키마 Repository. Pool을 주입받거나 connect()로 만든다."""

    def __init__(self, pool: ThreadedConnectionPool) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str, *, minconn: int = 1, maxconn: int = 4) -> PostgresAuditRepository:
        return cls(ThreadedConnectionPool(minconn, maxconn, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def record_domain_event(
        self,
        *,
        event_id,
        event_type: str,
        source_department: str,
        trace_id,
        payload: dict,
        occurred_at,
    ) -> None:
        """Redis Event 수신을 Canonical Audit Event로 멱등 기록한다."""

        self._execute(
            """
            insert into audit.domain_events (
                event_id, event_type, source_department, trace_id,
                payload, occurred_at, status
            ) values (%s, %s, %s, %s, %s, %s, 'PROCESSED')
            on conflict (event_id) do nothing
            """,
            (
                event_id,
                event_type,
                source_department,
                trace_id,
                Json(payload),
                occurred_at,
            ),
        )

    def _execute(self, query: str, params: tuple) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def save_qa_assessment(self, assessment: QaAssessment) -> None:
        """QA Decision과 Claim Check/Finding을 하나의 DB 트랜잭션으로 기록한다."""

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into audit.qa_decisions (
                        qa_decision_id, artifact_version_id, gate, decision,
                        conditions, reason_codes, decided_by, trace_id,
                        decided_at, calculation_version, input_hash
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (artifact_version_id, gate) do nothing
                    returning qa_decision_id
                    """,
                    (
                        assessment.qa_decision_id,
                        assessment.artifact_version_id,
                        assessment.gate,
                        assessment.decision.value,
                        Json(
                            {
                                "calculation_version": assessment.calculation_version,
                                "input_hash": assessment.input_hash,
                            }
                        ),
                        [reason.value for reason in assessment.reason_codes],
                        assessment.decided_by,
                        assessment.trace_id,
                        assessment.decided_at,
                        assessment.calculation_version,
                        assessment.input_hash,
                    ),
                )
                inserted = cur.fetchone() is not None
                if inserted:
                    for check in assessment.claim_checks:
                        cur.execute(
                            """
                            insert into audit.claim_checks (
                                claim_check_id, artifact_version_id, claim_index,
                                claim, evidence_chunk_ids, result, reason,
                                checker_version, checked_at
                            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            on conflict (artifact_version_id, claim_index, checker_version)
                            do nothing
                            """,
                            (
                                check.claim_check_id,
                                check.artifact_version_id,
                                check.claim_index,
                                check.claim,
                                list(check.evidence_chunk_ids),
                                check.result.value,
                                check.reason,
                                check.checker_version,
                                check.checked_at,
                            ),
                        )
                    for finding in assessment.findings:
                        cur.execute(
                            """
                            insert into audit.findings (
                                finding_id, fund_id, finding_type, severity,
                                artifact_version_id, description, owner,
                                trace_id, created_at
                            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            on conflict (finding_id) do nothing
                            """,
                            (
                                finding.finding_id,
                                finding.fund_id,
                                finding.finding_type,
                                finding.severity.value,
                                finding.artifact_version_id,
                                finding.description,
                                finding.opened_by,
                                finding.trace_id,
                                finding.created_at,
                            ),
                        )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise QaDecisionPersistenceError(f"Canonical QA Decision 기록 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    # -- audit.agent_runs --------------------------------------------------------

    def insert_run(self, run: AgentRunRecord) -> None:
        self._execute(
            """
            insert into audit.agent_runs
              (agent_run_id, trace_id, case_id, fund_id, agent_id, profile_version_id,
               model_id, input_hash, started_at, status)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (run.agent_run_id, run.trace_id, run.case_id, run.fund_id, run.agent_id,
             run.profile_version_id, run.model_id, run.input_hash, run.started_at, run.status.value),
        )

    def update_run_terminal(self, run: AgentRunRecord) -> None:
        self._execute(
            """
            update audit.agent_runs
            set status = %s, ended_at = %s, error_code = %s, token_usage = %s, cost = %s,
                output_artifact_version_id = %s, trace_uri = %s
            where agent_run_id = %s
            """,
            (run.status.value, run.ended_at, run.error_code, Json(run.token_usage), Json(run.cost),
             run.output_artifact_version_id, run.trace_uri, run.agent_run_id),
        )

    # -- audit.tool_calls (append-only - 종결 상태 1행만 insert) -----------------------

    def insert_tool_call_terminal(self, call: ToolCallRecord) -> None:
        self._execute(
            """
            insert into audit.tool_calls
              (tool_call_id, agent_run_id, trace_id, tool_name, scope, input_hash, output_hash,
               status, policy_version, latency_ms, error_code, occurred_at, completed_at, metadata)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (call.tool_call_id, call.agent_run_id, call.trace_id, call.tool_name, Json(call.scope),
             call.input_hash, call.output_hash, call.status.value, call.policy_version, call.latency_ms,
             call.error_code, call.occurred_at, call.completed_at, Json(call.metadata)),
        )

    # -- audit.incident_events (append-only) --------------------------------------

    def insert_incident_event(self, event: IncidentEventRecord) -> None:
        self._execute(
            """
            insert into audit.incident_events
              (incident_event_id, incident_id, source, entry_type, summary, evidence,
               occurred_at, recorded_at, recorded_by)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (event.incident_event_id, event.incident_id, event.source, event.entry_type.value,
             event.summary, Json(event.evidence), event.occurred_at, event.recorded_at, event.recorded_by),
        )

    # -- audit.corrective_actions --------------------------------------------------

    def insert_corrective_action(self, action: CorrectiveActionRecord) -> None:
        self._execute(
            """
            insert into audit.corrective_actions
              (corrective_action_id, incident_id, finding_id, owner, action_plan, due_at,
               status, created_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (action.corrective_action_id, action.incident_id, action.finding_id, action.owner,
             Json(action.action_plan), action.due_at, action.status.value, action.created_at),
        )

    def update_corrective_action(self, action: CorrectiveActionRecord) -> None:
        self._execute(
            """
        update audit.corrective_actions
        set status = %s, verification = %s, verifier = %s, completed_at = %s
        where corrective_action_id = %s
            """,
            (action.status.value, Json(action.verification) if action.verification is not None else None,
             action.verifier, action.completed_at, action.corrective_action_id),
        )

    def _ensure_incident_parent(
        self,
        cur,
        *,
        incident_id,
        occurred_at,
        source: str,
        summary: str,
        recorded_by: str,
    ) -> None:
        """Create the FK parent in the same transaction as its child row."""
        cur.execute(
            """
            insert into audit.incidents
            (incident_id, incident_code, severity, title, impact, status,
             started_at, detected_at, commander, trace_id)
            values (%s, %s, 'SEV3', %s, %s, 'OPEN', %s, %s, %s, %s)
            on conflict (incident_id) do nothing
            """,
            (
                incident_id,
                f"QA-AUTO-{incident_id}",
                f"QA incident auto-created from {source}",
                Json({"auto_created": True, "first_event_summary": summary}),
                occurred_at,
                occurred_at,
                recorded_by,
                incident_id,
            ),
        )

    def insert_incident_event(self, event: IncidentEventRecord) -> None:  # noqa: F811
        """Insert Incident parent and event atomically."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                self._ensure_incident_parent(
                    cur,
                    incident_id=event.incident_id,
                    occurred_at=event.occurred_at,
                    source=event.source,
                    summary=event.summary,
                    recorded_by=event.recorded_by,
                )
                cur.execute(
                    """
                    insert into audit.incident_events
                    (incident_event_id, incident_id, source, entry_type, summary,
                     evidence, occurred_at, recorded_at, recorded_by)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        event.incident_event_id,
                        event.incident_id,
                        event.source,
                        event.entry_type.value,
                        event.summary,
                        Json(event.evidence),
                        event.occurred_at,
                        event.recorded_at,
                        event.recorded_by,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def insert_corrective_action(self, action: CorrectiveActionRecord) -> None:  # noqa: F811
        """Insert an Incident parent and Corrective Action atomically."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                if action.incident_id is not None:
                    self._ensure_incident_parent(
                        cur,
                        incident_id=action.incident_id,
                        occurred_at=action.created_at,
                        source="qa-corrective-action",
                        summary="Corrective Action parent incident",
                        recorded_by=action.owner,
                    )
                cur.execute(
                    """
                    insert into audit.corrective_actions
                    (corrective_action_id, incident_id, finding_id, owner, action_plan,
                     due_at, status, created_at)
                    values (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        action.corrective_action_id,
                        action.incident_id,
                        action.finding_id,
                        action.owner,
                        Json(action.action_plan),
                        action.due_at,
                        action.status.value,
                        action.created_at,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)
