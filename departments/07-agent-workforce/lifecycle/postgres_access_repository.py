#!/usr/bin/env python3
"""Y4 Access Lifecycle: AccessRepository의 실제 PostgreSQL 구현.

소유: 영주 (Agent Workforce 인사팀)
근거: access.py의 AccessRepository 인터페이스, departments/00-ceo-office/src/mandate/
      postgres_repository.py·departments/06-ai-qa-audit/audit/repository.py 패턴
      (CLAUDE.md: 호출부가 동기라 asyncpg가 아니라 psycopg2로 통일).
      대응 테이블: supabase/migrations/20260731000700_workforce_access_lifecycle.sql

불변식:
  1. workforce.access_requests.agent_id/access_assignments.agent_id는 workforce.agent_profiles에
     대한 not null FK다. Y3 Registry에 등록된 실제 agent_id만 쓸 수 있다 - 가짜 Agent 행을
     만들어 우회하지 않는다.
  2. access_requests.approval_id는 governance.approvals에 대한 FK다. HR-04 승인 흐름이
     실제로 governance.approvals 행을 만드는 서비스는 아직 없어서, approval_id가 채워진
     요청을 저장하려면 그 부모 행이 먼저 있어야 한다 - 이 Repository는 그 행을 만들지
     않는다(Mandate owner_user_id와 같은 fail-closed 원칙).
  3. save_request/save_assignment는 upsert다 - 같은 request_id/assignment_id로 다시
     부르면 REQUESTED->APPROVED->PROVISIONED 같은 상태 전이를 같은 행에 반영한다
     (Append-only가 아니다 - lifecycle.py가 상태를 새 dataclass로 복제해 돌려주는 설계와 맞춘다).

자체 점검: python departments/07-agent-workforce/lifecycle/postgres_access_repository.py
  - DATABASE_URL 없으면 import만 확인한다.
  - 있으면 workforce.agent_profiles의 실제 employee(HR-04 우선)를 찾아 그 agent_id로
    Access Request(REQUESTED, approval_id 없음) -> Access Assignment(DATA, tool_permission_id
    없음) -> revoke까지 왕복 검증하고, 검증에 쓴 행은 정리(delete)한다.
    approval_id가 채워진 승인 왕복은 governance.approvals 부모 행이 없어 건너뛴다(불변식 2).
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any

from access import (
    AccessAssignment,
    AccessRepository,
    AccessRequest,
    AssignmentStatus,
    Environment,
    RequestStatus,
    ResourceKind,
)


class AccessPersistenceError(RuntimeError):
    """Access Request/Assignment를 기록하거나 조회하지 못한 경우."""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.extras import Json
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise AccessPersistenceError(
            "PostgreSQL Access Lifecycle 저장에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    return Json, ThreadedConnectionPool


class PostgresAccessRepository(AccessRepository):
    """`workforce.access_requests`/`workforce.access_assignments` 전용 저장소."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresAccessRepository:
        _, ThreadedConnectionPool = _load_postgres_driver()
        return cls(ThreadedConnectionPool(1, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    # --- access_requests ---------------------------------------------------

    def get_request(self, request_id: str) -> AccessRequest | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select request_id, agent_id, resource_kind, tool_id, resource_ref, scope,
                           environment, justification, requested_by, expires_at, approval_id,
                           approvals, status, trace_id, created_at
                    from workforce.access_requests
                    where request_id = %s
                    """,
                    (request_id,),
                )
                row = cur.fetchone()
            conn.commit()
            return None if row is None else self._to_request(row)
        finally:
            self._pool.putconn(conn)

    def save_request(self, request: AccessRequest) -> None:
        Json, _ = _load_postgres_driver()
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into workforce.access_requests (
                        request_id, agent_id, resource_kind, tool_id, resource_ref, scope,
                        environment, justification, requested_by, expires_at, approval_id,
                        approvals, status, trace_id, created_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (request_id) do update set
                        approval_id = excluded.approval_id,
                        approvals = excluded.approvals,
                        status = excluded.status,
                        updated_at = now()
                    """,
                    (
                        request.request_id, request.agent_id, request.resource_kind.value,
                        request.tool_id, request.resource_ref, Json(request.scope),
                        request.environment.value, request.justification, request.requested_by,
                        request.expires_at, request.approval_id, Json(request.approvals),
                        request.status.value, request.trace_id or None, request.requested_at,
                    ),
                )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise AccessPersistenceError(f"Access Request 저장 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_request(db_row: tuple) -> AccessRequest:
        (request_id, agent_id, resource_kind, tool_id, resource_ref, scope, environment,
         justification, requested_by, expires_at, approval_id, approvals, status, trace_id,
         created_at) = db_row
        return AccessRequest(
            request_id=str(request_id), agent_id=str(agent_id),
            resource_kind=ResourceKind(resource_kind), tool_id=str(tool_id) if tool_id else None,
            resource_ref=resource_ref, environment=Environment(environment),
            justification=justification, requested_by=requested_by, expires_at=expires_at,
            requested_at=created_at, scope=scope or {}, approval_id=str(approval_id) if approval_id else None,
            approvals=approvals or [], status=RequestStatus(status), trace_id=str(trace_id) if trace_id else "",
        )

    # --- access_assignments --------------------------------------------------

    def get_assignment(self, assignment_id: str) -> AccessAssignment | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select assignment_id, request_id, agent_id, resource_kind, resource_ref,
                           scope, environment, tool_permission_id, provisioning_ref,
                           provisioned_by, effective_from, effective_to, revoked_at,
                           revocation_evidence, status
                    from workforce.access_assignments
                    where assignment_id = %s
                    """,
                    (assignment_id,),
                )
                row = cur.fetchone()
            conn.commit()
            return None if row is None else self._to_assignment(row)
        finally:
            self._pool.putconn(conn)

    def save_assignment(self, assignment: AccessAssignment) -> None:
        Json, _ = _load_postgres_driver()
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into workforce.access_assignments (
                        assignment_id, request_id, agent_id, resource_kind, resource_ref,
                        scope, environment, tool_permission_id, provisioning_ref,
                        provisioned_by, effective_from, effective_to, revoked_at,
                        revocation_evidence, status
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (assignment_id) do update set
                        revoked_at = excluded.revoked_at,
                        revocation_evidence = excluded.revocation_evidence,
                        status = excluded.status,
                        updated_at = now()
                    """,
                    (
                        assignment.assignment_id, assignment.request_id, assignment.agent_id,
                        assignment.resource_kind.value, assignment.resource_ref,
                        Json(assignment.scope), assignment.environment.value,
                        assignment.tool_permission_id, assignment.provisioning_ref,
                        assignment.provisioned_by, assignment.effective_from,
                        assignment.effective_to, assignment.revoked_at,
                        Json(assignment.revocation_evidence) if assignment.revocation_evidence is not None else None,
                        assignment.status.value,
                    ),
                )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise AccessPersistenceError(f"Access Assignment 저장 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def list_assignments_by_agent(self, agent_id: str) -> list[AccessAssignment]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select assignment_id, request_id, agent_id, resource_kind, resource_ref,
                           scope, environment, tool_permission_id, provisioning_ref,
                           provisioned_by, effective_from, effective_to, revoked_at,
                           revocation_evidence, status
                    from workforce.access_assignments
                    where agent_id = %s
                    """,
                    (agent_id,),
                )
                rows = cur.fetchall()
            conn.commit()
            return [self._to_assignment(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_assignment(db_row: tuple) -> AccessAssignment:
        (assignment_id, request_id, agent_id, resource_kind, resource_ref, scope, environment,
         tool_permission_id, provisioning_ref, provisioned_by, effective_from, effective_to,
         revoked_at, revocation_evidence, status) = db_row
        return AccessAssignment(
            assignment_id=str(assignment_id), request_id=str(request_id), agent_id=str(agent_id),
            resource_kind=ResourceKind(resource_kind), resource_ref=resource_ref,
            environment=Environment(environment),
            tool_permission_id=str(tool_permission_id) if tool_permission_id else None,
            provisioning_ref=provisioning_ref, provisioned_by=provisioned_by,
            effective_from=effective_from, effective_to=effective_to, scope=scope or {},
            revoked_at=revoked_at, revocation_evidence=revocation_evidence,
            status=AssignmentStatus(status),
        )


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/lifecycle/postgres_access_repository.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import uuid
    from datetime import timedelta, timezone

    print("ok - import 확인 (psycopg2 lazy load)")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    repo = PostgresAccessRepository.connect(dsn)
    try:
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select agent_id from workforce.agent_profiles "
                    "where employee_code = 'HR-04' limit 1"
                )
                agent_row = cur.fetchone()
        finally:
            repo._pool.putconn(conn)

        if agent_row is None:
            print("SKIP - workforce.agent_profiles에 HR-04가 없어 왕복 검증을 건너뛴다")
            raise SystemExit(0)

        agent_id = str(agent_row[0])
        t0 = datetime(2026, 8, 3, tzinfo=timezone.utc)
        t_exp = t0 + timedelta(days=30)
        request_id = str(uuid.uuid4())

        # 1) 존재하지 않는 request_id - None.
        assert repo.get_request(str(uuid.uuid4())) is None
        print("ok - 존재하지 않는 request_id 조회 (실 DB) 통과")

        # 2) 실제 왕복 - REQUESTED 상태 요청 저장 후 재현 (불변식 2 - approval_id 없음).
        req = AccessRequest(
            request_id=request_id, agent_id=agent_id, resource_kind=ResourceKind.DATA,
            resource_ref="market-api:read", environment=Environment.SHADOW,
            justification="자체 점검 - Postgres Access Repository 왕복", requested_by="selfcheck",
            expires_at=t_exp, requested_at=t0, trace_id=str(uuid.uuid4()),
        )
        repo.save_request(req)
        found = repo.get_request(request_id)
        assert found is not None and found.agent_id == agent_id
        assert found.status is RequestStatus.REQUESTED
        print("ok - save_request -> get_request 왕복 (실 DB) 통과")

        # 3) 실제 왕복 - Access Assignment(DATA, tool_permission_id 없음) 저장/조회/agent별 목록.
        assignment_id = str(uuid.uuid4())
        asg = AccessAssignment(
            assignment_id=assignment_id, request_id=request_id, agent_id=agent_id,
            resource_kind=ResourceKind.DATA, resource_ref="market-api:read",
            environment=Environment.SHADOW, provisioning_ref="selfcheck-iam-1",
            provisioned_by="selfcheck", effective_from=t0, effective_to=t_exp,
        )
        repo.save_assignment(asg)
        found_asg = repo.get_assignment(assignment_id)
        assert found_asg is not None and found_asg.status is AssignmentStatus.ACTIVE
        by_agent = repo.list_assignments_by_agent(agent_id)
        assert any(a.assignment_id == assignment_id for a in by_agent)
        print("ok - save_assignment -> get_assignment/list_assignments_by_agent 왕복 (실 DB) 통과")

        # 4) 회수 - 같은 assignment_id로 REVOKED 상태 갱신 (upsert, 불변식 3).
        from access import revoke

        revoked = revoke(found_asg, at=t0 + timedelta(days=1), evidence={"ticket": "SELFCHECK-1"})
        repo.save_assignment(revoked)
        found_revoked = repo.get_assignment(assignment_id)
        assert found_revoked.status is AssignmentStatus.REVOKED
        assert found_revoked.revocation_evidence == {"ticket": "SELFCHECK-1"}
        print("ok - revoke 후 upsert 왕복 (실 DB) 통과")

        # 정리 - 공유 개발 DB에 자체 점검 흔적을 남기지 않는다.
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "delete from workforce.access_assignments where assignment_id = %s",
                    (assignment_id,),
                )
                cur.execute(
                    "delete from workforce.access_requests where request_id = %s", (request_id,)
                )
            conn.commit()
        finally:
            repo._pool.putconn(conn)
        print("ok - 자체 점검 행 정리 완료")
    finally:
        repo.close()
