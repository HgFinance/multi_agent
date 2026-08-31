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

import json
import os
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from uuid import UUID

from audit.db_session import configure_writer_connection, runtime_session_dsn
from orchestration.connection_pool import create_blocking_connection_pool

try:
    # `audit.repository` is imported as a package by the supervisor.  Keep the
    # package-qualified evidence import so the repository root is not required
    # to be added to sys.path by every caller.
    from evidence.evidence_qa_engine import QaAssessment
except ImportError:  # pragma: no cover - legacy script/test entry point
    from evidence_qa_engine import QaAssessment

try:
    from .incident_timeline import CorrectiveActionRecord, IncidentEventRecord
    from .trace_recorder import AgentRunRecord, ToolCallRecord
except (ImportError, ValueError):  # pragma: no cover - legacy flat imports
    from incident_timeline import CorrectiveActionRecord, IncidentEventRecord
    from trace_recorder import AgentRunRecord, ToolCallRecord


class QaDecisionPersistenceError(RuntimeError):
    """Canonical QA Decision을 기록하지 못한 경우."""


class EvalPersistenceConflict(QaDecisionPersistenceError):
    """A replay changed an immutable EvalRun/EvalResult payload."""


class DomainEventConflict(QaDecisionPersistenceError):
    """An existing event ID was replayed with different canonical content."""


class ForwardQaRequestConflict(QaDecisionPersistenceError):
    """A forward-QA event conflicts with its immutable outbox/request."""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> tuple[Any, Any]:
    """PostgreSQL 저장을 실제로 사용할 때만 psycopg2를 로드한다."""
    try:
        from psycopg2.extras import Json, register_uuid
        from psycopg2.pool import ThreadedConnectionPool

        register_uuid()
    except ModuleNotFoundError as exc:
        raise QaDecisionPersistenceError(
            "PostgreSQL QA 감사 저장에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    return Json, ThreadedConnectionPool


def _json_param(value: Any) -> Any:
    """psycopg2가 설치된 DB 저장 경로에서만 JSON 래퍼를 만든다."""
    Json, _ = _load_postgres_driver()
    return Json(value)


class PostgresAuditRepository:
    """psycopg2 기반 audit 스키마 Repository. Pool을 주입받거나 connect()로 만든다."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def _get_connection(self):
        """Borrow a connection with an explicit READ WRITE transaction mode.

        Supabase's database default is deliberately read-only.  This
        repository is the canonical QA writer, so relying on a pooled
        backend's inherited default makes writes fail nondeterministically.
        psycopg2's ``set_session(readonly=False)`` records the transaction
        characteristic without changing the server-wide default.  Injected
        unit-test connections may omit that driver method.
        """
        try:
            conn = self._pool.getconn()
        except Exception as exc:  # noqa: BLE001 - pool errors vary by driver
            raise QaDecisionPersistenceError(
                "QA audit connection pool is unavailable"
            ) from exc
        try:
            configure_writer_connection(conn)
            return conn
        except Exception:
            rollback = getattr(conn, "rollback", None)
            if rollback is not None:
                try:
                    rollback()
                except Exception:
                    pass
            try:
                self._pool.putconn(conn, close=True)
            except TypeError:
                self._pool.putconn(conn)
            raise

    @classmethod
    def connect(
        cls, dsn: str, *, minconn: int = 1, maxconn: int = 4
    ) -> PostgresAuditRepository:
        _, ThreadedConnectionPool = _load_postgres_driver()
        return cls(
            create_blocking_connection_pool(
                ThreadedConnectionPool,
                runtime_session_dsn(dsn),
                minconn=minconn,
                default_maxconn=maxconn,
                env_prefix="RISK_QA",
            )
        )

    @staticmethod
    def _test_mode() -> bool:
        return os.environ.get("RISK_QA_RUNTIME", "").lower() == "test"
    def close(self) -> None:
        self._pool.closeall()

    def runtime_database_status(self) -> dict[str, str]:
        """Prove that a writable scoped role is active on a real transaction."""

        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select session_user, current_user, "
                    "current_setting('transaction_read_only')"
                )
                session_user, current_user, read_only = cur.fetchone()
            conn.commit()
            if str(read_only).lower() not in {"off", "false"}:
                raise RuntimeError("canonical QA connection is read-only")
            return {
                "session_user": str(session_user),
                "current_user": str(current_user),
                "transaction_read_only": str(read_only),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

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

        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                self._record_domain_event_exact(
                    cur,
                    event_id=event_id,
                    event_type=event_type,
                    source_department=source_department,
                    trace_id=trace_id,
                    payload=payload,
                    occurred_at=occurred_at,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _json_object(value: Any, *, field: str) -> dict[str, Any]:
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ForwardQaRequestConflict(f"{field} must be a JSON object")
        return value

    @staticmethod
    def _as_datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str) or not value.strip():
            raise ForwardQaRequestConflict("occurred_at is missing")
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _same_datetime(left: Any, right: Any) -> bool:
        left_dt = PostgresAuditRepository._as_datetime(left)
        right_dt = PostgresAuditRepository._as_datetime(right)
        if left_dt.tzinfo is not None and right_dt.tzinfo is not None:
            return left_dt.astimezone(timezone.utc) == right_dt.astimezone(
                timezone.utc
            )
        return left_dt == right_dt

    def _record_domain_event_exact(
        self,
        cur: Any,
        *,
        event_id: UUID,
        event_type: str,
        source_department: str,
        trace_id: UUID,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        cur.execute(
            """
            insert into audit.domain_events (
                event_id, event_type, source_department, trace_id,
                payload, occurred_at, status
            ) values (%s, %s, %s, %s, %s, %s, 'PROCESSED')
            on conflict (event_id) do nothing
            returning event_id
            """,
            (
                event_id,
                event_type,
                source_department,
                trace_id,
                _json_param(payload),
                occurred_at,
            ),
        )
        if cur.fetchone() is not None:
            return
        cur.execute(
            """
            select event_type, source_department, trace_id::text,
                   payload, occurred_at, status
              from audit.domain_events
             where event_id = %s
            """,
            (event_id,),
        )
        existing = cur.fetchone()
        if existing is None:
            raise DomainEventConflict("domain event conflict row disappeared")
        existing_payload = self._json_object(
            existing[3], field="existing domain payload"
        )
        if (
            str(existing[0]) != event_type
            or str(existing[1]) != source_department
            or UUID(str(existing[2])) != UUID(str(trace_id))
            or existing_payload != payload
            or not self._same_datetime(existing[4], occurred_at)
            or str(existing[5]) != "PROCESSED"
        ):
            raise DomainEventConflict(
                "event_id was replayed with different canonical content"
            )

    def accept_intraday_forward_qa_request(self, event: dict[str, Any]) -> None:
        """Atomically accept one canonical stock-only forward reproduction.

        This transaction records the domain event, immutable reproduction
        request, and durable work item.  It deliberately does not execute the
        long-running reproduction and grants no promotion authority.
        """

        event_id = UUID(str(event.get("event_id", "")))
        event_type = str(event.get("event_type", ""))
        trace_id = UUID(str(event.get("trace_id", "")))
        occurred_at = self._as_datetime(event.get("occurred_at"))
        payload = self._json_object(event.get("payload"), field="event payload")
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select outbox_id, qa_handoff_id::text, event_id::text,
                           event_type, producer, trace_id::text, occurred_at,
                           event_payload, payload_ref, reproduction_contract,
                           payload_fingerprint
                      from quant.intraday_forward_qa_outbox
                     where event_id = %s
                       for share
                    """,
                    (event_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise ForwardQaRequestConflict(
                        "forward QA event has no authoritative outbox row"
                    )
                canonical_payload = self._json_object(
                    row[7], field="outbox event_payload"
                )
                payload_ref = self._json_object(row[8], field="payload_ref")
                contract = self._json_object(
                    row[9], field="reproduction_contract"
                )
                if (
                    UUID(str(row[2])) != event_id
                    or str(row[3]) != event_type
                    or str(row[4]) != "quant-backtest-department"
                    or UUID(str(row[5])) != trace_id
                    or not self._same_datetime(row[6], occurred_at)
                    or canonical_payload != payload
                ):
                    raise ForwardQaRequestConflict(
                        "forward QA delivery changed immutable outbox content"
                    )
                if (
                    event_type != "quant.intraday.forward.qa_requested.v1"
                    or contract.get("decision") != "PASS"
                    or contract.get("hypothesis_status") != "SUPPORTED"
                    or contract.get("asset_class") != "EQUITY"
                    or contract.get("instrument_type") != "STOCK"
                    or contract.get("asset_scope")
                    != "KRX_ACTIVE_STOCK_ONLY"
                    or contract.get("product_filter")
                    != "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY"
                    or contract.get("requested_action")
                    != "INDEPENDENT_QA_REPRODUCTION"
                    or contract.get("promotion_authority") is not False
                    or int(contract.get("instrument_count", 0)) < 1
                    or int(contract.get("session_count", 0)) < 20
                ):
                    raise ForwardQaRequestConflict(
                        "forward QA request is not a stock-only PASS contract"
                    )

                self._record_domain_event_exact(
                    cur,
                    event_id=event_id,
                    event_type=event_type,
                    source_department="quant-backtest-department",
                    trace_id=trace_id,
                    payload=payload,
                    occurred_at=occurred_at,
                )
                request_values = (
                    event_id,
                    event_id,
                    int(row[0]),
                    UUID(str(row[1])),
                    UUID(str(contract["forward_confirmation_id"])),
                    UUID(str(contract["report_revision_id"])),
                    UUID(str(contract["experiment_id"])),
                    UUID(str(contract["hypothesis_id"])),
                    "PASS",
                    "SUPPORTED",
                    "EQUITY",
                    "STOCK",
                    int(contract["instrument_count"]),
                    str(contract["instrument_set_fingerprint"]),
                    int(contract["session_count"]),
                    str(contract["session_set_fingerprint"]),
                    _json_param(payload_ref),
                    _json_param(contract),
                    _json_param(payload),
                    str(row[10]),
                    occurred_at,
                    "qa-forward-consumer/v1",
                )
                cur.execute(
                    """
                    insert into audit.intraday_forward_reproduction_requests (
                      reproduction_request_id, event_id, outbox_id,
                      qa_handoff_id, forward_confirmation_id,
                      report_revision_id, experiment_id, hypothesis_id,
                      decision, hypothesis_status, asset_class,
                      instrument_type, instrument_count,
                      instrument_set_fingerprint, session_count,
                      session_set_fingerprint, payload_ref,
                      reproduction_contract, event_payload,
                      payload_fingerprint, requested_at, accepted_by
                    ) values (
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    on conflict (event_id) do nothing
                    returning reproduction_request_id
                    """,
                    request_values,
                )
                if cur.fetchone() is None:
                    cur.execute(
                        """
                        select outbox_id, qa_handoff_id::text,
                               reproduction_contract, event_payload,
                               payload_fingerprint
                          from audit.intraday_forward_reproduction_requests
                         where event_id = %s
                        """,
                        (event_id,),
                    )
                    existing = cur.fetchone()
                    if (
                        existing is None
                        or int(existing[0]) != int(row[0])
                        or UUID(str(existing[1])) != UUID(str(row[1]))
                        or self._json_object(
                            existing[2], field="existing reproduction contract"
                        )
                        != contract
                        or self._json_object(
                            existing[3], field="existing event payload"
                        )
                        != payload
                        or str(existing[4]) != str(row[10])
                    ):
                        raise ForwardQaRequestConflict(
                            "event replay conflicts with reproduction request"
                        )
                cur.execute(
                    """
                    insert into audit.intraday_forward_reproduction_work_items (
                      reproduction_request_id, status, next_attempt_at
                    ) values (%s, 'READY', now())
                    on conflict (reproduction_request_id) do nothing
                    """,
                    (event_id,),
                )
            conn.commit()
        except (DomainEventConflict, ForwardQaRequestConflict):
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise QaDecisionPersistenceError(
                f"forward QA request acceptance failed: {exc}"
            ) from exc
        finally:
            self._pool.putconn(conn)

    def _execute(self, query: str, params: tuple) -> None:
        conn = self._get_connection()
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

        conn = self._get_connection()
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
                        _json_param(
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
                                opened_by, trace_id, created_at
                            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                                finding.opened_by,
                                finding.trace_id,
                                finding.created_at,
                            ),
                        )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise QaDecisionPersistenceError(
                f"Canonical QA Decision 기록 실패: {exc}"
            ) from exc
        finally:
            self._pool.putconn(conn)

    # -- audit.agent_runs --------------------------------------------------------

    def insert_run(self, run: AgentRunRecord) -> None:
        if self._test_mode():
            return
        self._execute(
            """
            insert into audit.agent_runs
              (agent_run_id, trace_id, case_id, fund_id, agent_id, profile_version_id,
               model_id, input_hash, started_at, status)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run.agent_run_id,
                run.trace_id,
                run.case_id,
                run.fund_id,
                run.agent_id,
                run.profile_version_id,
                run.model_id,
                run.input_hash,
                run.started_at,
                run.status.value,
            ),
        )

    def update_run_terminal(self, run: AgentRunRecord) -> None:
        if self._test_mode():
            return
        self._execute(
            """
            update audit.agent_runs
            set status = %s, ended_at = %s, error_code = %s, token_usage = %s, cost = %s,
                output_artifact_version_id = %s, trace_uri = %s
            where agent_run_id = %s
            """,
            (
                run.status.value,
                run.ended_at,
                run.error_code,
                _json_param(run.token_usage),
                _json_param(run.cost),
                run.output_artifact_version_id,
                run.trace_uri,
                run.agent_run_id,
            ),
        )

    # -- audit.tool_calls (append-only - 종결 상태 1행만 insert) -----------------------

    def insert_tool_call_terminal(self, call: ToolCallRecord) -> None:
        if self._test_mode():
            return
        self._execute(
            """
            insert into audit.tool_calls
              (tool_call_id, agent_run_id, trace_id, tool_name, scope, input_hash, output_hash,
               status, policy_version, latency_ms, error_code, occurred_at, completed_at, metadata)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                call.tool_call_id,
                call.agent_run_id,
                call.trace_id,
                call.tool_name,
                _json_param(call.scope),
                call.input_hash,
                call.output_hash,
                call.status.value,
                call.policy_version,
                call.latency_ms,
                call.error_code,
                call.occurred_at,
                call.completed_at,
                _json_param(call.metadata),
            ),
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
            (
                event.incident_event_id,
                event.incident_id,
                event.source,
                event.entry_type.value,
                event.summary,
                _json_param(event.evidence),
                event.occurred_at,
                event.recorded_at,
                event.recorded_by,
            ),
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
            (
                action.corrective_action_id,
                action.incident_id,
                action.finding_id,
                action.owner,
                _json_param(action.action_plan),
                action.due_at,
                action.status.value,
                action.created_at,
            ),
        )

    def update_corrective_action(self, action: CorrectiveActionRecord) -> None:
        self._execute(
            """
        update audit.corrective_actions
        set status = %s, verification = %s, verifier = %s, completed_at = %s
        where corrective_action_id = %s
            """,
            (
                action.status.value,
                _json_param(action.verification)
                if action.verification is not None
                else None,
                action.verifier,
                action.completed_at,
                action.corrective_action_id,
            ),
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
                _json_param({"auto_created": True, "first_event_summary": summary}),
                occurred_at,
                occurred_at,
                recorded_by,
                incident_id,
            ),
        )

    def insert_incident_event(self, event: IncidentEventRecord) -> None:  # noqa: F811
        """Insert Incident parent and event atomically."""
        conn = self._get_connection()
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
                        _json_param(event.evidence),
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
        conn = self._get_connection()
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
                        _json_param(action.action_plan),
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
    @staticmethod
    def _eval_uuid(value: Any, field: str) -> UUID:
        try:
            return value if isinstance(value, UUID) else UUID(str(value))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be UUID-compatible") from exc

    @staticmethod
    def _eval_value(value: Any, key: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    @staticmethod
    def _same_eval_value(actual: Any, expected: Any) -> bool:
        if hasattr(actual, "adapted"):
            actual = actual.adapted
        if hasattr(expected, "adapted"):
            expected = expected.adapted
        if isinstance(actual, UUID) or isinstance(expected, UUID):
            try:
                return UUID(str(actual)) == UUID(str(expected))
            except (TypeError, ValueError, AttributeError):
                return False
        return actual == expected

    def ensure_eval_set(self, eval_set: Any) -> None:
        """Create the FK parent before inserting ``audit.eval_runs``."""
        eval_set_id = self._eval_uuid(self._eval_value(eval_set, "eval_set_id"), "eval_set_id")
        role_code = self._eval_value(eval_set, "role_code")
        version = int(self._eval_value(eval_set, "version"))
        content_hash = self._eval_value(eval_set, "content_hash")
        manifest_path = f"qa/eval-sets/{eval_set_id}.json"
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                select_sql = (
                    "select eval_set_id, role_code, version, content_hash "
                    "from audit.eval_sets where eval_set_id = %s "
                    "or (role_code = %s and version = %s) "
                    "or (role_code = %s and content_hash = %s)"
                )
                cur.execute(select_sql, (eval_set_id, role_code, version, role_code, content_hash))
                rows = cur.fetchall()
                for existing_id, existing_role, existing_version, existing_hash in rows:
                    same_identity = (
                        self._same_eval_value(existing_id, eval_set_id)
                        and str(existing_role) == str(role_code)
                        and int(existing_version) == version
                        and str(existing_hash) == str(content_hash)
                    )
                    if not same_identity:
                        raise EvalPersistenceConflict("conflicting eval set identity")
                if not rows:
                    cur.execute(
                        """
                        insert into audit.eval_sets (
                            eval_set_id, role_code, version, manifest_path,
                            content_hash, status
                        ) values (%s, %s, %s, %s, %s, 'DRAFT')
                        on conflict do nothing
                        """,
                        (eval_set_id, role_code, version, manifest_path, content_hash),
                    )
                    if getattr(cur, "rowcount", 1) == 0:
                        # A concurrent insert won either uniqueness constraint;
                        # re-read it and classify the replay deterministically.
                        cur.execute(select_sql, (eval_set_id, role_code, version, role_code, content_hash))
                        rows = cur.fetchall()
                        if not rows:
                            raise EvalPersistenceConflict("eval set insert conflict")
                        for existing_id, existing_role, existing_version, existing_hash in rows:
                            same_identity = (
                                self._same_eval_value(existing_id, eval_set_id)
                                and str(existing_role) == str(role_code)
                                and int(existing_version) == version
                                and str(existing_hash) == str(content_hash)
                            )
                            if not same_identity:
                                raise EvalPersistenceConflict("conflicting eval set identity")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def insert_eval_run(self, run: Any) -> None:
        """Append one EvalRun and make identical replay a no-op."""
        run_id = self._eval_uuid(self._eval_value(run, "eval_run_id"), "eval_run_id")
        eval_set_id = self._eval_uuid(self._eval_value(run, "eval_set_id"), "eval_set_id")
        trace_id = self._eval_uuid(self._eval_value(run, "trace_id"), "trace_id")
        values = (
            run_id,
            eval_set_id,
            self._eval_value(run, "candidate_id"),
            self._eval_value(run, "candidate_profile_version"),
            self._eval_value(run, "eval_set_version"),
            self._eval_value(run, "eval_set_hash"),
            _json_param(self._eval_value(run, "champion_ref")),
            _json_param(self._eval_value(run, "config", {})),
            self._eval_value(run, "status"),
            trace_id,
            self._eval_value(run, "environment"),
            _json_param(self._eval_value(run, "mock_tool_manifest", {})),
            self._eval_value(run, "model_version"),
            self._eval_value(run, "adapter_version"),
            self._eval_value(run, "evidence_hash"),
            self._eval_value(run, "started_at"),
            self._eval_value(run, "ended_at"),
            self._eval_value(run, "created_at"),
        )
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into audit.eval_runs (
                        eval_run_id, eval_set_id, candidate_id, candidate_profile_version,
                        eval_set_version, eval_set_hash, champion_ref, config, status, trace_id,
                        environment, mock_tool_manifest, model_version, adapter_version,
                        evidence_hash, started_at, ended_at, created_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (eval_run_id) do nothing
                    returning eval_run_id
                    """,
                    values,
                )
                inserted = cur.fetchone()
                if inserted is None:
                    cur.execute(
                        "select eval_run_id, eval_set_id, candidate_id, candidate_profile_version, "
                        "eval_set_version, eval_set_hash, champion_ref, config, status, trace_id, "
                        "environment, mock_tool_manifest, model_version, adapter_version, evidence_hash, "
                        "started_at, ended_at, created_at from audit.eval_runs where eval_run_id = %s",
                        (run_id,),
                    )
                    existing = cur.fetchone()
                    if existing is None:
                        raise EvalPersistenceConflict("eval run insert conflict")
                    if len(existing) == len(values) and all(
                        self._same_eval_value(a, b) for a, b in zip(existing, values)
                    ):
                        conn.commit()
                        return
                    raise EvalPersistenceConflict("conflicting eval run replay")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def insert_eval_result(self, result: Any) -> None:
        """Insert one immutable EvalResult; exact replay is idempotent."""
        result_id = self._eval_uuid(self._eval_value(result, "eval_result_id"), "eval_result_id")
        run_id = self._eval_uuid(self._eval_value(result, "eval_run_id"), "eval_run_id")
        case_key = self._eval_value(result, "case_key")
        metric = self._eval_value(result, "metric")
        values = (
            result_id,
            run_id,
            case_key,
            metric,
            self._eval_value(result, "score"),
            self._eval_value(result, "passed"),
            _json_param(self._eval_value(result, "evidence", {})),
            self._eval_value(result, "error_code"),
            self._eval_value(result, "created_at"),
        )
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into audit.eval_results (
                        eval_result_id, eval_run_id, case_key, metric,
                        score, passed, evidence, error_code, created_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict do nothing
                    returning eval_result_id
                    """,
                    values,
                )
                inserted = cur.fetchone()
                if inserted is None:
                    cur.execute(
                        "select eval_result_id, eval_run_id, case_key, metric, score, passed, "
                        "evidence, error_code, created_at from audit.eval_results "
                        "where eval_result_id = %s",
                        (result_id,),
                    )
                    existing = cur.fetchone()
                    if existing is not None:
                        if len(existing) == len(values) and all(
                            self._same_eval_value(a, b) for a, b in zip(existing, values)
                        ):
                            conn.commit()
                            return
                        raise EvalPersistenceConflict("conflicting eval result replay")
                    cur.execute(
                        "select eval_result_id from audit.eval_results "
                        "where eval_run_id = %s and case_key = %s and metric = %s",
                        (run_id, case_key, metric),
                    )
                    if cur.fetchone() is not None:
                        raise EvalPersistenceConflict("conflicting eval result key")
                    raise EvalPersistenceConflict("eval result insert conflict")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def insert_kanban_qa_findings(self, record: Any) -> None:
        """Persist QA findings emitted by a Kanban terminal projection.

        ``artifact_version_id`` is intentionally NULL when the Hermes QA task
        did not evaluate a persisted artifact. The existing audit.findings
        table supports that shape; no projection-specific table is introduced.
        """

        findings = self._eval_value(record, "findings", [])
        if not isinstance(findings, (list, tuple)):
            return
        trace_id = self._eval_uuid(self._eval_value(record, "trace_id"), "trace_id")
        projection_key = str(self._eval_value(record, "projection_key"))
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                for index, finding in enumerate(findings):
                    item = finding if isinstance(finding, dict) else {"description": str(finding)}
                    finding_id = uuid.uuid5(
                        uuid.UUID("b8a25c03-2d9d-5f4e-b542-9dcb36db3e91"),
                        f"{projection_key}:finding:{index}:{json.dumps(item, sort_keys=True, default=str)}",
                    )
                    severity = str(
                        item.get("severity") or self._eval_value(record, "highest_severity", "LOW")
                    ).upper()
                    if severity not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
                        severity = "LOW"
                    description = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                    cur.execute(
                        """
                        insert into audit.findings (
                            finding_id, fund_id, finding_type, severity,
                            artifact_version_id, description, owner, status,
                            opened_by, trace_id, created_at
                        ) values (%s, %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s, %s)
                        on conflict (finding_id) do nothing
                        """,
                        (
                            finding_id,
                            None,
                            "kanban-qa",
                            severity,
                            None,
                            description,
                            "qa-department",
                            "qa-department",
                            trace_id,
                            self._eval_value(record, "completed_at") or datetime.now(timezone.utc),
                        ),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)
    def append_comparison(self, comparison: Any) -> None:
        """Persist one immutable Champion comparison, idempotently."""
        run_id = self._eval_uuid(comparison.candidate_run_id, "candidate_run_id")
        champion_id = (
            self._eval_uuid(comparison.champion_run_id, "champion_run_id")
            if comparison.champion_run_id
            else None
        )
        values = (
            run_id,
            str(comparison.status),
            comparison.error_code,
            champion_id,
            _json_param(comparison.metrics),
        )
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into audit.eval_comparisons (
                        eval_run_id, status, error_code, champion_run_id, metrics
                    ) values (%s, %s, %s, %s, %s)
                    on conflict (eval_run_id) do nothing
                    returning eval_run_id
                    """,
                    values,
                )
                inserted = cur.fetchone()
                if inserted is None:
                    cur.execute(
                        "select status, error_code, champion_run_id, metrics "
                        "from audit.eval_comparisons where eval_run_id = %s",
                        (run_id,),
                    )
                    existing = cur.fetchone()
                    if existing is None:
                        raise EvalPersistenceConflict("eval comparison insert conflict")
                    if all(self._same_eval_value(a, b) for a, b in zip(existing, values[1:])):
                        conn.commit()
                        return
                    raise EvalPersistenceConflict("conflicting eval comparison replay")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def comparison_for_run(self, run_id: Any) -> Any | None:
        run_uuid = self._eval_uuid(run_id, "eval_run_id")
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select status, error_code, champion_run_id, metrics "
                    "from audit.eval_comparisons where eval_run_id = %s",
                    (run_uuid,),
                )
                row = cur.fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)
        if row is None:
            return None
        from eval_runner import ChampionComparison
        return ChampionComparison(
            status=str(row[0]),
            error_code=str(row[1]) if row[1] is not None else None,
            candidate_run_id=str(run_uuid),
            champion_run_id=str(row[2]) if row[2] is not None else None,
            metrics=row[3] if isinstance(row[3], dict) else {},
        )


    def transition_eval_run(
        self, eval_run_id: Any, status: str, *, ended_at: Any = None
    ) -> None:
        """Advance EvalRun only through the guarded lifecycle."""
        allowed = {
            "QUEUED": {"RUNNING", "CANCELLED"},
            "RUNNING": {"COMPLETED", "FAILED", "CANCELLED"},
        }
        if status not in {"QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"}:
            raise ValueError(f"invalid eval run status: {status}")
        run_id = self._eval_uuid(eval_run_id, "eval_run_id")
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("select status from audit.eval_runs where eval_run_id = %s", (run_id,))
                row = cur.fetchone()
                if row is None:
                    raise KeyError(str(run_id))
                current = str(row[0])
                if status not in allowed.get(current, set()):
                    raise EvalPersistenceConflict(f"invalid eval run transition {current}->{status}")
                cur.execute(
                    "update audit.eval_runs set status = %s, ended_at = %s "
                    "where eval_run_id = %s and status = %s",
                    (status, ended_at, run_id, current),
                )
                if getattr(cur, "rowcount", 1) != 1:
                    raise EvalPersistenceConflict("eval run transition lost race")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)
    def run_for_id(self, run_id: Any) -> Any | None:
        """Read one EvalRun without exposing database rows to API callers."""
        run_uuid = self._eval_uuid(run_id, "eval_run_id")
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select eval_run_id, eval_set_id, candidate_id, candidate_profile_version, "
                    "eval_set_version, eval_set_hash, champion_ref, config, status, trace_id, "
                    "environment, mock_tool_manifest, model_version, adapter_version, evidence_hash, "
                    "started_at, ended_at, created_at from audit.eval_runs where eval_run_id = %s",
                    (run_uuid,),
                )
                row = cur.fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)
        if row is None:
            return None
        from eval_runner import EvalRun
        return EvalRun(
            eval_run_id=str(row[0]),
            eval_set_id=str(row[1]),
            eval_set_version=int(row[4] or 0),
            eval_set_hash=str(row[5] or ""),
            candidate_id=str(row[2] or ""),
            candidate_profile_version=str(row[3] or ""),
            champion_ref=row[6] if isinstance(row[6], dict) else None,
            config=row[7] if isinstance(row[7], dict) else {},
            status=str(row[8]),
            trace_id=str(row[9]),
            environment=str(row[10] or "SHADOW"),
            mock_tool_manifest=row[11] if isinstance(row[11], dict) else {},
            model_version=str(row[12] or ""),
            adapter_version=str(row[13] or ""),
            evidence_hash=str(row[14] or ""),
            started_at=row[15],
            ended_at=row[16],
            created_at=row[17],
        )

    get_eval_run = run_for_id
    get_run = run_for_id

    def results_for_run(self, run_id: Any) -> list[Any]:
        run_uuid = self._eval_uuid(run_id, "eval_run_id")
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select eval_result_id, eval_run_id, case_key, metric, score, passed, "
                    "evidence, error_code, created_at from audit.eval_results "
                    "where eval_run_id = %s order by created_at, eval_result_id",
                    (run_uuid,),
                )
                rows = cur.fetchall()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)
        from eval_runner import EvalMetric, EvalResult
        return [
            EvalResult(
                eval_result_id=str(row[0]),
                eval_run_id=str(row[1]),
                case_key=row[2],
                metric=EvalMetric(str(row[3])),
                score=row[4],
                passed=row[5],
                evidence=row[6] or {},
                error_code=row[7],
                created_at=row[8],
            )
            for row in rows
        ]

    def append_run(self, run: Any) -> None:
        self.insert_eval_run(run)

    def append_result(self, result: Any) -> None:
        self.insert_eval_result(result)

    def transition_run(
        self, run_id: Any, status: str, *, ended_at: Any = None
    ) -> Any:
        self.transition_eval_run(run_id, status, ended_at=ended_at)
        return {"eval_run_id": run_id, "status": status, "ended_at": ended_at}
