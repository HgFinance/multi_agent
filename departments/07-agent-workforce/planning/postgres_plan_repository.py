#!/usr/bin/env python3
"""P1-2 HR-04: workforce_plan.py 가 쓰는 WorkforcePlan/승인 증거의 실제 PostgreSQL 계층.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md P1-2,
      workforce_plan.py(순수 상태 머신, DB를 모른다),
      roster/activation_evidence.py(같은 조회-판정 분리 패턴)

governance.approvals 는 CEO Office 소유 테이블이다 - 이 모듈은 그 스키마를 읽기만
하고 쓰지 않는다 (HR 은 CEO 승인을 스스로 만들 수 없다는 게 이 검증이 강제하려는
권한 분리).

자체 점검: python departments/07-agent-workforce/planning/postgres_plan_repository.py
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any

from workforce_plan import PlanApprovalEvidenceRepository, WorkforcePlan, WorkforcePlanStatus


class PlanPersistenceError(RuntimeError):
    """workforce.workforce_plans 기록/조회에 실패한 경우."""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise PlanPersistenceError(
            "PostgreSQL 조회에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    return ThreadedConnectionPool


class PostgresPlanRepository:
    """`workforce.workforce_plans` 읽기/쓰기."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresPlanRepository:
        ThreadedConnectionPool = _load_postgres_driver()
        return cls(ThreadedConnectionPool(1, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def create_plan(self, plan: WorkforcePlan) -> WorkforcePlan:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into workforce.workforce_plans (
                        department_id, period_start, period_end, skill_gaps, actions,
                        budget, assumptions, status
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                    returning plan_id
                    """,
                    (plan.department_id, plan.period_start, plan.period_end, plan.skill_gaps,
                     plan.actions, plan.budget, plan.assumptions, plan.status.value),
                )
                plan_id = cur.fetchone()[0]
            conn.commit()
            return WorkforcePlan(**{**plan.__dict__, "plan_id": str(plan_id)})
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            raise PlanPersistenceError(f"workforce_plan 생성 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def save_plan(self, plan: WorkforcePlan) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update workforce.workforce_plans
                    set status = %s, approval_id = %s
                    where plan_id = %s
                    """,
                    (plan.status.value, plan.approval_id, plan.plan_id),
                )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            raise PlanPersistenceError(f"workforce_plan 갱신 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def get_plan(self, plan_id: str) -> WorkforcePlan | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select plan_id, department_id, period_start, period_end, skill_gaps,
                           actions, budget, assumptions, status, approval_id
                    from workforce.workforce_plans where plan_id = %s
                    """,
                    (plan_id,),
                )
                row = cur.fetchone()
            conn.commit()
            return None if row is None else self._to_plan(row)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            raise PlanPersistenceError(f"workforce_plan 조회 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def list_plans_by_department(self, department_id: str) -> list[WorkforcePlan]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select plan_id, department_id, period_start, period_end, skill_gaps,
                           actions, budget, assumptions, status, approval_id
                    from workforce.workforce_plans where department_id = %s
                    order by period_start
                    """,
                    (department_id,),
                )
                rows = cur.fetchall()
            conn.commit()
            return [self._to_plan(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            raise PlanPersistenceError(f"workforce_plan 목록 조회 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_plan(db_row: tuple) -> WorkforcePlan:
        (plan_id, department_id, period_start, period_end, skill_gaps, actions, budget,
         assumptions, status, approval_id) = db_row
        return WorkforcePlan(
            plan_id=str(plan_id), department_id=str(department_id),
            period_start=period_start, period_end=period_end,
            skill_gaps=skill_gaps or {}, actions=actions or [], budget=budget or {},
            assumptions=assumptions or {}, status=WorkforcePlanStatus(status),
            approval_id=str(approval_id) if approval_id else None,
        )


class PostgresPlanApprovalEvidenceRepository(PlanApprovalEvidenceRepository):
    """`governance.approvals`에 대한 읽기 전용 구현 (object_type=WORKFORCE_PLAN)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresPlanApprovalEvidenceRepository:
        ThreadedConnectionPool = _load_postgres_driver()
        return cls(ThreadedConnectionPool(1, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def get_ceo_approval_decision(self, approval_id: str, plan_id: str) -> str | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select decision from governance.approvals "
                    "where approval_id = %s and object_id = %s "
                    "and object_type = 'WORKFORCE_PLAN' and required_role = 'CEO'",
                    (approval_id, plan_id),
                )
                row = cur.fetchone()
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            raise PlanPersistenceError(f"approval_id 조회 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)
        return row[0] if row else None


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/planning/postgres_plan_repository.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    from datetime import timedelta, timezone

    print("ok - import 확인 (psycopg2 lazy load)")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    repo = PostgresPlanRepository.connect(dsn)
    evidence_repo = PostgresPlanApprovalEvidenceRepository.connect(dsn)
    try:
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select department_id from workforce.departments "
                    "where department_code = 'hr-department' limit 1"
                )
                dept_row = cur.fetchone()
        finally:
            repo._pool.putconn(conn)

        if dept_row is None:
            print("SKIP - workforce.departments에 hr-department가 없어 왕복 검증을 건너뛴다")
            raise SystemExit(0)

        department_id = str(dept_row[0])
        t0 = datetime(2026, 8, 6, tzinfo=timezone.utc)
        t1 = t0 + timedelta(days=30)

        created = repo.create_plan(WorkforcePlan(
            plan_id="", department_id=department_id, period_start=t0, period_end=t1,
            skill_gaps={"research": 1}, actions=[{"type": "HIRE"}], budget={"monthly_usd": "1000"},
        ))
        assert created.plan_id
        print("ok - create_plan (실 DB) 통과")

        try:
            fetched = repo.get_plan(created.plan_id)
            assert fetched is not None and fetched.status is WorkforcePlanStatus.DRAFT
            print("ok - get_plan (실 DB) 통과")

            # 실재하지 않는 approval_id는 None - 위조 승인 차단의 기반.
            assert evidence_repo.get_ceo_approval_decision("00000000-0000-0000-0000-000000000000",
                                                            created.plan_id) is None
            print("ok - 미실재 approval_id 조회 시 None (실 DB) 통과")

            listed = repo.list_plans_by_department(department_id)
            assert any(p.plan_id == created.plan_id for p in listed)
            print("ok - list_plans_by_department (실 DB) 통과")
        finally:
            conn = repo._pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("delete from workforce.workforce_plans where plan_id = %s",
                                (created.plan_id,))
                conn.commit()
            finally:
                repo._pool.putconn(conn)
            print("ok - 자체 점검 행 정리 완료")
    finally:
        repo.close()
        evidence_repo.close()
