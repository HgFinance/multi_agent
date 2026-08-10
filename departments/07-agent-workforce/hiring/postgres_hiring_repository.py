#!/usr/bin/env python3
"""HiringRequestRepository의 실제 PostgreSQL 구현.

소유: 영주 (Agent Workforce 인사팀)
근거: hiring_request.py의 HiringRequestRepository 인터페이스,
      lifecycle/postgres_access_repository.py와 동일 패턴(CLAUDE.md: 호출부가
      동기라 psycopg2로 통일).
      대응 테이블: supabase/migrations/20260729000200_governance_workforce.sql
      (workforce.hiring_requests) + 20260810000100(requested_by/decided_by 추가)

불변식:
  1. department_id는 workforce.departments에 대한 FK다 - 실재하는 부서만 쓸 수 있다.
  2. save_request는 upsert다 - 같은 request_id로 다시 부르면 OPEN->EVALUATING->
     APPROVED 같은 상태 전이를 같은 행에 반영한다(access.py와 동일 설계).

자체 점검: python departments/07-agent-workforce/hiring/postgres_hiring_repository.py
  - DATABASE_URL 없으면 import만 확인한다.
  - 있으면 workforce.departments의 실제 부서 하나를 찾아 그 department_id로
    HiringRequest 생성 -> 조회 -> 상태 전이 -> 목록 조회까지 왕복 검증하고,
    검증에 쓴 행은 정리(delete)한다.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from hiring_request import HiringRequest, HiringRequestRepository, HiringRequestStatus


class HiringPersistenceError(RuntimeError):
    """HiringRequest를 기록하거나 조회하지 못한 경우."""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.extras import Json
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise HiringPersistenceError(
            "PostgreSQL Hiring Request 저장에는 psycopg2-binary가 필요합니다."
        ) from exc
    return Json, ThreadedConnectionPool


_COLUMNS = (
    "request_id, department_id, business_problem, evidence, required_capabilities, "
    "budget, status, trace_id, created_at, requested_by, decided_by, decided_at, "
    "decision_reason"
)


class PostgresHiringRequestRepository(HiringRequestRepository):
    """`workforce.hiring_requests` 전용 저장소."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresHiringRequestRepository:
        _, ThreadedConnectionPool = _load_postgres_driver()
        return cls(ThreadedConnectionPool(1, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def get_request(self, request_id: str) -> HiringRequest | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"select {_COLUMNS} from workforce.hiring_requests where request_id = %s",
                    (request_id,),
                )
                row = cur.fetchone()
            conn.commit()
            return None if row is None else self._to_request(row)
        finally:
            self._pool.putconn(conn)

    def save_request(self, request: HiringRequest) -> None:
        Json, _ = _load_postgres_driver()
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    insert into workforce.hiring_requests ({_COLUMNS})
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (request_id) do update set
                        status = excluded.status,
                        decided_by = excluded.decided_by,
                        decided_at = excluded.decided_at,
                        decision_reason = excluded.decision_reason
                    """,
                    (
                        request.request_id, request.department_id, request.business_problem,
                        Json(request.evidence), Json(request.required_capabilities),
                        Json(request.budget), request.status.value, request.trace_id,
                        request.created_at, request.requested_by, request.decided_by,
                        request.decided_at, request.decision_reason,
                    ),
                )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise HiringPersistenceError(f"Hiring Request 저장 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def list_requests_by_status(self, status: HiringRequestStatus) -> list[HiringRequest]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"select {_COLUMNS} from workforce.hiring_requests where status = %s "
                    "order by created_at asc",
                    (status.value,),
                )
                rows = cur.fetchall()
            conn.commit()
            return [self._to_request(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    def list_requests_by_department(self, department_id: str) -> list[HiringRequest]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"select {_COLUMNS} from workforce.hiring_requests where department_id = %s "
                    "order by created_at asc",
                    (department_id,),
                )
                rows = cur.fetchall()
            conn.commit()
            return [self._to_request(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_request(db_row: tuple) -> HiringRequest:
        (request_id, department_id, business_problem, evidence, required_capabilities,
         budget, status, trace_id, created_at, requested_by, decided_by, decided_at,
         decision_reason) = db_row
        return HiringRequest(
            request_id=str(request_id), department_id=str(department_id),
            business_problem=business_problem, evidence=evidence or {},
            required_capabilities=required_capabilities or {}, budget=budget or {},
            requested_by=requested_by, trace_id=str(trace_id), created_at=created_at,
            status=HiringRequestStatus(status), decided_by=decided_by,
            decided_at=decided_at, decision_reason=decision_reason,
        )


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/hiring/postgres_hiring_repository.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import uuid
    from datetime import datetime, timezone

    from hiring_request import transition

    print("ok - import 확인 (psycopg2 lazy load)")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    repo = PostgresHiringRequestRepository.connect(dsn)
    try:
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("select department_id from workforce.departments limit 1")
                dept_row = cur.fetchone()
        finally:
            repo._pool.putconn(conn)

        if dept_row is None:
            print("SKIP - workforce.departments가 비어 있어 왕복 검증을 건너뛴다")
            raise SystemExit(0)

        department_id = str(dept_row[0])
        t0 = datetime(2026, 8, 10, tzinfo=timezone.utc)
        request_id = str(uuid.uuid4())

        assert repo.get_request(str(uuid.uuid4())) is None
        print("ok - 존재하지 않는 request_id 조회 (실 DB) 통과")

        req = HiringRequest(
            request_id=request_id, department_id=department_id,
            business_problem="자체 점검 - Postgres Hiring Repository 왕복",
            evidence={"selfcheck": True}, required_capabilities={}, budget={},
            requested_by="selfcheck", trace_id=str(uuid.uuid4()), created_at=t0,
        )
        repo.save_request(req)
        found = repo.get_request(request_id)
        assert found is not None and found.status is HiringRequestStatus.OPEN
        print("ok - save_request -> get_request 왕복 (실 DB) 통과")

        evaluating = transition(found, to_status=HiringRequestStatus.EVALUATING, actor="qa", at=t0)
        repo.save_request(evaluating)
        by_status = repo.list_requests_by_status(HiringRequestStatus.EVALUATING)
        assert any(r.request_id == request_id for r in by_status)
        print("ok - 상태 전이 upsert -> list_requests_by_status 왕복 (실 DB) 통과")

        by_dept = repo.list_requests_by_department(department_id)
        assert any(r.request_id == request_id for r in by_dept)
        print("ok - list_requests_by_department 왕복 (실 DB) 통과")

        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("delete from workforce.hiring_requests where request_id = %s", (request_id,))
            conn.commit()
        finally:
            repo._pool.putconn(conn)
        print("ok - 자체 점검 행 정리 완료")
    finally:
        repo.close()
