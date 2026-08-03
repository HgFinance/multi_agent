#!/usr/bin/env python3
"""F27 인사팀 절반: cost.py가 쓰는 CostSnapshot/CapacitySnapshot의 실제 PostgreSQL 조회 계층.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F27,
      cost.py의 assess_budget/build_department_scorecard(순수 함수, Snapshot을 인자로 받는다),
      api/app.py 상단 docstring 5번째 항목("GET .../scorecard가 아니라 POST인 이유 -
      workforce.cost_snapshots/capacity_snapshots를 조회할 저장소가 아직 없어서").

이 모듈이 그 격차를 메운다 - cost.py 자체는 여전히 순수 함수로 남긴다(LLM도 DB도
없다는 CLAUDE.md 원칙, "집계와 초과 판정은 결정론적 코드만 한다"). Repository는 오직
Snapshot을 real DB에서 읽어오는 역할만 하고, 판정 로직(assess_budget/build_department_
scorecard)은 그대로 호출자가 그 결과를 넘겨 쓴다.

불변식:
  1. workforce.cost_snapshots.agent_id/profile_version_id는 not null FK다 - 등록되지
     않은 agent_id로는 애초에 행이 존재할 수 없다(쓰기가 아니라 읽기 전용 Repository라
     이 모듈이 직접 만들 일은 없다).
  2. workforce.capacity_snapshots는 department_id 또는 agent_id 중 하나 이상 있어야
     한다(DDL check) - department 단위 질의와 agent 단위 질의를 분리한다.
  3. cost.py의 불변식 3(Snapshot 없음을 0으로 채우지 않는다)을 그대로 따른다 - 조회
     결과가 빈 목록/None이면 그대로 반환하고 여기서 기본값을 만들지 않는다.

자체 점검: python departments/07-agent-workforce/scorecard/postgres_scorecard_repository.py
  - DATABASE_URL 없으면 import만 확인한다.
  - 있으면 실제 workforce.agent_profiles/departments 행을 찾아 cost_snapshots/
    capacity_snapshots에 자체 점검용 행을 넣고 조회 왕복을 검증한 뒤 정리(delete)한다
    (두 테이블 다 append-only 트리거가 없어 삭제 가능 - Improvements의 이벤트 테이블과 다르다).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any

from cost import CapacitySnapshot, CostSnapshot


class ScorecardQueryError(RuntimeError):
    """Snapshot 조회에 실패한 경우."""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise ScorecardQueryError(
            "PostgreSQL Scorecard 조회에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    return ThreadedConnectionPool


class PostgresScorecardRepository:
    """`workforce.cost_snapshots`/`workforce.capacity_snapshots` 읽기 전용 조회."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresScorecardRepository:
        ThreadedConnectionPool = _load_postgres_driver()
        return cls(ThreadedConnectionPool(1, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def get_department_id(self, department_code: str) -> str | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select department_id from workforce.departments where department_code = %s",
                    (department_code,),
                )
                row = cur.fetchone()
            conn.commit()
            return str(row[0]) if row else None
        finally:
            self._pool.putconn(conn)

    def list_cost_snapshots_by_agent(
        self, agent_id: str, *, window_start: datetime, window_end: datetime
    ) -> list[CostSnapshot]:
        return self._list_cost_snapshots(
            "select snapshot_id, agent_id, profile_version_id, window_start, window_end, "
            "input_tokens, output_tokens, model_cost, tool_cost, infra_cost, case_count, currency "
            "from workforce.cost_snapshots "
            "where agent_id = %s and window_start >= %s and window_end <= %s",
            (agent_id, window_start, window_end),
        )

    def list_cost_snapshots_by_department(
        self, department_id: str, *, window_start: datetime, window_end: datetime
    ) -> list[CostSnapshot]:
        return self._list_cost_snapshots(
            """
            select cs.snapshot_id, cs.agent_id, cs.profile_version_id, cs.window_start,
                   cs.window_end, cs.input_tokens, cs.output_tokens, cs.model_cost,
                   cs.tool_cost, cs.infra_cost, cs.case_count, cs.currency
            from workforce.cost_snapshots cs
            join workforce.agent_profiles ap on ap.agent_id = cs.agent_id
            where ap.department_id = %s and cs.window_start >= %s and cs.window_end <= %s
            """,
            (department_id, window_start, window_end),
        )

    def _list_cost_snapshots(self, query: str, params: tuple) -> list[CostSnapshot]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
            conn.commit()
            return [self._to_cost_snapshot(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_cost_snapshot(db_row: tuple) -> CostSnapshot:
        (_snapshot_id, agent_id, profile_version_id, window_start, window_end, input_tokens,
         output_tokens, model_cost, tool_cost, infra_cost, case_count, currency) = db_row
        return CostSnapshot(
            agent_id=str(agent_id), profile_version_id=str(profile_version_id),
            window_start=window_start, window_end=window_end, input_tokens=input_tokens,
            output_tokens=output_tokens, model_cost=Decimal(model_cost), tool_cost=Decimal(tool_cost),
            infra_cost=Decimal(infra_cost), case_count=case_count, currency=currency,
        )

    def get_capacity_snapshot(
        self, *, department_id: str | None = None, agent_id: str | None = None,
        window_start: datetime, window_end: datetime,
    ) -> CapacitySnapshot | None:
        """불변식 2 - department/agent 중 하나 이상 필요. 둘 다 주면 department 우선(department
        단위 Scorecard 질의가 주 사용처)."""
        if department_id is None and agent_id is None:
            raise ValueError("department_id/agent_id 중 하나는 있어야 한다 (DDL check와 동일)")

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                if department_id is not None:
                    cur.execute(
                        """
                        select department_id, agent_id, window_start, window_end, arrivals,
                               queue_p95_ms, duration_p95_ms, retry_rate, error_rate, utilization
                        from workforce.capacity_snapshots
                        where department_id = %s and window_start >= %s and window_end <= %s
                        order by window_end desc
                        limit 1
                        """,
                        (department_id, window_start, window_end),
                    )
                else:
                    cur.execute(
                        """
                        select department_id, agent_id, window_start, window_end, arrivals,
                               queue_p95_ms, duration_p95_ms, retry_rate, error_rate, utilization
                        from workforce.capacity_snapshots
                        where agent_id = %s and window_start >= %s and window_end <= %s
                        order by window_end desc
                        limit 1
                        """,
                        (agent_id, window_start, window_end),
                    )
                row = cur.fetchone()
            conn.commit()
            return None if row is None else self._to_capacity_snapshot(row)
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_capacity_snapshot(db_row: tuple) -> CapacitySnapshot:
        (department_id, agent_id, window_start, window_end, arrivals, queue_p95_ms,
         duration_p95_ms, retry_rate, error_rate, utilization) = db_row
        return CapacitySnapshot(
            window_start=window_start, window_end=window_end, arrivals=arrivals,
            queue_p95_ms=Decimal(queue_p95_ms) if queue_p95_ms is not None else None,
            duration_p95_ms=Decimal(duration_p95_ms) if duration_p95_ms is not None else None,
            retry_rate=Decimal(retry_rate) if retry_rate is not None else None,
            error_rate=Decimal(error_rate) if error_rate is not None else None,
            utilization=Decimal(utilization) if utilization is not None else None,
            department_id=str(department_id) if department_id else None,
            agent_id=str(agent_id) if agent_id else None,
        )


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/scorecard/postgres_scorecard_repository.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    from datetime import timedelta, timezone

    print("ok - import 확인 (psycopg2 lazy load)")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    repo = PostgresScorecardRepository.connect(dsn)
    try:
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select agent_id, department_id from workforce.agent_profiles "
                    "where employee_code = 'HR-01' limit 1"
                )
                agent_row = cur.fetchone()
                cur.execute(
                    "select profile_version_id from workforce.agent_profile_versions "
                    "where agent_id = %s limit 1", (agent_row[0],) if agent_row else (None,),
                )
                version_row = cur.fetchone() if agent_row else None
        finally:
            repo._pool.putconn(conn)

        if agent_row is None or version_row is None:
            print("SKIP - workforce.agent_profiles/agent_profile_versions에 HR-01이 없어 왕복 검증을 건너뛴다")
            raise SystemExit(0)

        agent_id, department_id = str(agent_row[0]), str(agent_row[1])
        profile_version_id = str(version_row[0])

        # 1) department_code 조회.
        dept_id = repo.get_department_id("hr-department")
        assert dept_id == department_id
        print("ok - get_department_id (실 DB) 통과")

        t0 = datetime(2026, 8, 3, tzinfo=timezone.utc)
        t1 = t0 + timedelta(hours=1)

        # 2) cost_snapshots에 자체 점검 행 삽입 -> agent/department 조회 왕복.
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into workforce.cost_snapshots
                      (agent_id, profile_version_id, window_start, window_end,
                       input_tokens, output_tokens, model_cost, tool_cost, infra_cost, case_count)
                    values (%s, %s, %s, %s, 100, 100, 1.5, 0, 0, 1)
                    returning snapshot_id
                    """,
                    (agent_id, profile_version_id, t0, t1),
                )
                cost_snapshot_id = cur.fetchone()[0]
                cur.execute(
                    """
                    insert into workforce.capacity_snapshots
                      (department_id, window_start, window_end, arrivals, utilization)
                    values (%s, %s, %s, 5, 0.5)
                    returning snapshot_id
                    """,
                    (department_id, t0, t1),
                )
                capacity_snapshot_id = cur.fetchone()[0]
            conn.commit()
        finally:
            repo._pool.putconn(conn)

        try:
            by_agent = repo.list_cost_snapshots_by_agent(agent_id, window_start=t0, window_end=t1)
            assert len(by_agent) == 1 and by_agent[0].case_count == 1
            by_dept = repo.list_cost_snapshots_by_department(department_id, window_start=t0, window_end=t1)
            assert any(s.agent_id == agent_id for s in by_dept)
            print("ok - list_cost_snapshots_by_agent/by_department (실 DB) 통과")

            cap = repo.get_capacity_snapshot(department_id=department_id, window_start=t0, window_end=t1)
            assert cap is not None and cap.arrivals == 5
            print("ok - get_capacity_snapshot (실 DB) 통과")

            # 3) 불변식 2 - department/agent 둘 다 없으면 거부.
            try:
                repo.get_capacity_snapshot(window_start=t0, window_end=t1)
                raise AssertionError("department_id/agent_id 없이 통과함")
            except ValueError:
                pass
            print("ok - department/agent 둘 다 없으면 거부 통과")
        finally:
            # 정리 - 공유 개발 DB에 자체 점검 흔적을 남기지 않는다 (append-only 트리거 없음).
            conn = repo._pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "delete from workforce.cost_snapshots where snapshot_id = %s",
                        (cost_snapshot_id,),
                    )
                    cur.execute(
                        "delete from workforce.capacity_snapshots where snapshot_id = %s",
                        (capacity_snapshot_id,),
                    )
                conn.commit()
            finally:
                repo._pool.putconn(conn)
            print("ok - 자체 점검 행 정리 완료")
    finally:
        repo.close()
