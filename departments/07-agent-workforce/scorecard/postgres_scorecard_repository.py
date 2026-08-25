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
  - DATABASE_URL 없으면 import 와 append_cost_snapshot 의 거부 조건만 확인한다
    (거부는 DB 를 타기 전에 걸리므로 DSN 없이도 의미가 있다).
  - 있으면 실제 workforce.agent_profiles/departments 행을 찾아 cost_snapshots/
    capacity_snapshots에 자체 점검용 행을 넣고 조회 왕복 + 같은 창 재보고 멱등성을
    검증한 뒤 정리(delete)한다 (두 테이블 다 append-only 트리거가 없어 삭제 가능 -
    Improvements의 이벤트 테이블과 다르다).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any

from cost import CapacitySnapshot, CostSnapshot
from quality import QualitySnapshot


class ScorecardQueryError(RuntimeError):
    """Snapshot 조회에 실패한 경우."""


class QualitySnapshotPersistenceError(RuntimeError):
    """workforce.quality_snapshots 기록/조회에 실패한 경우."""


class CostSnapshotPersistenceError(RuntimeError):
    """workforce.cost_snapshots 기록에 실패한 경우."""


class UnknownCostSnapshotSubjectError(CostSnapshotPersistenceError):
    """등록되지 않은 agent_id/profile_version_id 로 비용을 보고한 경우.

    다른 기록 실패(연결 끊김 등)와 구분한다 - 이쪽은 재시도해도 낫지 않는 호출자
    오류이고, 조용히 삼키면 "비용 0"으로 읽힌다(cost.py 불변식 3).
    """


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
    """`workforce.cost_snapshots`/`workforce.quality_snapshots` 읽기/쓰기 +
    `workforce.capacity_snapshots` 읽기 전용 조회.

    F27 담당 분리는 그대로다 - **수치를 만드는 주체**와 **행을 적는 주체**는 다르다.
    cost 의 토큰·금액은 여전히 플랫폼/인프라의 과금 계측이 만들고, 인사팀은 그것을
    귀속·Scorecard·권고에 쓴다. append_cost_snapshot 은 그 보고를 받아 적는 창구일
    뿐 수치를 계산하지 않는다 - 그래서 recorded_by 를 반드시 요구한다(누가 보고한
    값인지 없이 적으면 인사팀이 지어낸 것과 구별되지 않는다). quality 는 반대로
    인사팀이 직접 집계해서 쓴다(quality_snapshots 테이블 주석: "여기 채우는 값은
    인사팀이 집계하는 finding_count/rework_rate 뿐").

    capacity 에는 여전히 쓰기가 없다 - writer 주체가 아직 정해지지 않았고
    (P1-2 미착수), 지금은 observability.py 가 Langfuse 실행 이벤트를 직접 집계해
    그 자리를 메우고 있다. 그쪽을 DB Snapshot 으로 옮길지는 별도 결정이다."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresScorecardRepository:
        ThreadedConnectionPool = _load_postgres_driver()
        # minconn=0 - 유휴 커넥션을 잡지 않는다
        return cls(ThreadedConnectionPool(0, 4, dsn))

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

    def append_cost_snapshot(self, snapshot: CostSnapshot) -> tuple[str, bool]:
        """플랫폼이 보고한 비용 계측 1건을 적는다. (snapshot_id, 새로 만들었는가) 반환.

        같은 (agent_id, profile_version_id, window_start, window_end)를 다시 보고하면
        **행을 늘리지 않고 갱신한다**. reader 가 창 안의 행을 합산하기 때문에 - 재보고가
        새 행이 되면 사용량이 조용히 두 배가 되고 예산 판정이 뒤집힌다
        (20260825000100 migration 의 unique index 가 그 키다).

        수치 자체는 검증하지 않는다(그건 플랫폼 소유다). 대신 **적으면 안 되는 것**만
        막는다 - 보고자 없는 행, 역전된 창, 음수. 음수 비용·토큰은 DDL 이 막지 않지만
        assess_budget 의 사용률을 실제보다 낮게 만들어 "예산 여유"로 읽힌다
        (개발 원칙 9: 실패는 거래 확대가 아니라 차단 방향으로).
        """
        if snapshot.recorded_by is None or not snapshot.recorded_by.strip():
            raise ValueError("recorded_by 가 없으면 비용을 적을 수 없다 - 플랫폼 보고자를 남겨야 한다")
        if snapshot.window_end <= snapshot.window_start:
            raise ValueError("window_end 는 window_start 이후여야 한다")
        if snapshot.input_tokens < 0 or snapshot.output_tokens < 0 or snapshot.case_count < 0:
            raise ValueError("토큰·case_count 는 음수일 수 없다")
        if snapshot.model_cost < 0 or snapshot.tool_cost < 0 or snapshot.infra_cost < 0:
            raise ValueError("비용은 음수일 수 없다")

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into workforce.cost_snapshots (
                        agent_id, profile_version_id, window_start, window_end,
                        input_tokens, output_tokens, model_cost, tool_cost, infra_cost,
                        case_count, currency, recorded_by
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (agent_id, profile_version_id, window_start, window_end)
                    do update set
                        input_tokens = excluded.input_tokens,
                        output_tokens = excluded.output_tokens,
                        model_cost = excluded.model_cost,
                        tool_cost = excluded.tool_cost,
                        infra_cost = excluded.infra_cost,
                        case_count = excluded.case_count,
                        currency = excluded.currency,
                        recorded_by = excluded.recorded_by
                    returning snapshot_id, (xmax = 0) as inserted
                    """,
                    (snapshot.agent_id, snapshot.profile_version_id,
                     snapshot.window_start, snapshot.window_end,
                     snapshot.input_tokens, snapshot.output_tokens,
                     str(snapshot.model_cost), str(snapshot.tool_cost), str(snapshot.infra_cost),
                     snapshot.case_count, snapshot.currency, snapshot.recorded_by),
                )
                snapshot_id, inserted = cur.fetchone()
            conn.commit()
            return str(snapshot_id), bool(inserted)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            # 23503 = foreign_key_violation. 등록되지 않은 agent/profile version 으로
            # 온 보고는 재시도해도 낫지 않는다 - 일시 장애와 같은 예외로 뭉뚱그리면
            # 호출자가 재시도 루프에 갇힌다.
            if getattr(exc, "pgcode", None) == "23503":
                raise UnknownCostSnapshotSubjectError(
                    f"등록되지 않은 agent_id/profile_version_id: agent_id={snapshot.agent_id}, "
                    f"profile_version_id={snapshot.profile_version_id}"
                ) from exc
            raise CostSnapshotPersistenceError(f"cost_snapshot 기록 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def list_cost_snapshots_by_agent(
        self, agent_id: str, *, window_start: datetime, window_end: datetime
    ) -> list[CostSnapshot]:
        return self._list_cost_snapshots(
            "select snapshot_id, agent_id, profile_version_id, window_start, window_end, "
            "input_tokens, output_tokens, model_cost, tool_cost, infra_cost, case_count, currency, "
            "recorded_by "
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
                   cs.tool_cost, cs.infra_cost, cs.case_count, cs.currency, cs.recorded_by
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
         output_tokens, model_cost, tool_cost, infra_cost, case_count, currency,
         recorded_by) = db_row
        return CostSnapshot(
            agent_id=str(agent_id), profile_version_id=str(profile_version_id),
            window_start=window_start, window_end=window_end, input_tokens=input_tokens,
            output_tokens=output_tokens, model_cost=Decimal(model_cost), tool_cost=Decimal(tool_cost),
            infra_cost=Decimal(infra_cost), case_count=case_count, currency=currency,
            recorded_by=recorded_by,
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


    def append_quality_snapshot(self, snapshot: QualitySnapshot) -> str:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into workforce.quality_snapshots (
                        department_id, agent_id, profile_version_id, window_start, window_end,
                        eval_run_id, finding_count, rework_rate, role_kpi, recorded_by
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    returning snapshot_id
                    """,
                    (snapshot.department_id, snapshot.agent_id, snapshot.profile_version_id,
                     snapshot.window_start, snapshot.window_end, snapshot.eval_run_id,
                     snapshot.finding_count,
                     str(snapshot.rework_rate) if snapshot.rework_rate is not None else None,
                     snapshot.role_kpi, snapshot.recorded_by),
                )
                snapshot_id = cur.fetchone()[0]
            conn.commit()
            return str(snapshot_id)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            raise QualitySnapshotPersistenceError(f"quality_snapshot 기록 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def list_quality_snapshots_by_department(
        self, department_id: str, *, window_start: datetime, window_end: datetime
    ) -> list[QualitySnapshot]:
        return self._list_quality_snapshots(
            "where department_id = %s and window_start >= %s and window_end <= %s",
            (department_id, window_start, window_end),
        )

    def list_quality_snapshots_by_agent(
        self, agent_id: str, *, window_start: datetime, window_end: datetime
    ) -> list[QualitySnapshot]:
        return self._list_quality_snapshots(
            "where agent_id = %s and window_start >= %s and window_end <= %s",
            (agent_id, window_start, window_end),
        )

    def _list_quality_snapshots(self, where_clause: str, params: tuple) -> list[QualitySnapshot]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select department_id, agent_id, profile_version_id, window_start, window_end,
                           eval_run_id, finding_count, rework_rate, role_kpi, recorded_by
                    from workforce.quality_snapshots
                    {where_clause}
                    """,
                    params,
                )
                rows = cur.fetchall()
            conn.commit()
            return [self._to_quality_snapshot(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            raise QualitySnapshotPersistenceError(f"quality_snapshot 조회 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_quality_snapshot(db_row: tuple) -> QualitySnapshot:
        (department_id, agent_id, profile_version_id, window_start, window_end,
         eval_run_id, finding_count, rework_rate, role_kpi, recorded_by) = db_row
        return QualitySnapshot(
            window_start=window_start, window_end=window_end, recorded_by=recorded_by,
            department_id=str(department_id) if department_id else None,
            agent_id=str(agent_id) if agent_id else None,
            profile_version_id=str(profile_version_id) if profile_version_id else None,
            eval_run_id=str(eval_run_id) if eval_run_id else None,
            finding_count=finding_count,
            rework_rate=Decimal(rework_rate) if rework_rate is not None else None,
            role_kpi=role_kpi or {},
        )


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/scorecard/postgres_scorecard_repository.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    from datetime import timedelta, timezone

    print("ok - import 확인 (psycopg2 lazy load)")

    # append_cost_snapshot 의 거부 조건은 DB 를 타기 전에 걸린다 - DSN 없이도 돈다.
    # pool 은 쓰이지 않지만 None 을 넣으면 "왜 안 터졌지"가 모호해지므로 명시한다.
    _pure = PostgresScorecardRepository(pool=None)
    _t0 = datetime(2026, 8, 25, tzinfo=timezone.utc)
    _t1 = _t0 + timedelta(hours=1)

    def _reject(label: str, **overrides) -> None:
        base = dict(
            agent_id="a1", profile_version_id="v1", window_start=_t0, window_end=_t1,
            input_tokens=10, output_tokens=10, model_cost=Decimal("1"),
            tool_cost=Decimal("0"), infra_cost=Decimal("0"), case_count=1,
            recorded_by="platform-metering",
        )
        base.update(overrides)
        try:
            _pure.append_cost_snapshot(CostSnapshot(**base))
            raise AssertionError(f"{label} 이 통과함")
        except ValueError:
            pass

    _reject("보고자 없는 비용", recorded_by=None)
    _reject("공백뿐인 보고자", recorded_by="   ")
    _reject("역전된 window", window_start=_t1, window_end=_t0)
    _reject("음수 input_tokens", input_tokens=-1)
    _reject("음수 case_count", case_count=-1)
    _reject("음수 model_cost", model_cost=Decimal("-0.01"))
    print("ok - append_cost_snapshot 거부 조건 6개 통과")

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
        #    2026-08-25 부터 raw INSERT 가 아니라 append_cost_snapshot 을 탄다 -
        #    자체 점검이 실제 writer 경로를 지나야 writer 결함을 잡는다.
        def _self_check_snapshot(**overrides) -> CostSnapshot:
            base = dict(
                agent_id=agent_id, profile_version_id=profile_version_id,
                window_start=t0, window_end=t1, input_tokens=100, output_tokens=100,
                model_cost=Decimal("1.5"), tool_cost=Decimal("0"), infra_cost=Decimal("0"),
                case_count=1, recorded_by="self-check",
            )
            base.update(overrides)
            return CostSnapshot(**base)

        cost_snapshot_id, created = repo.append_cost_snapshot(_self_check_snapshot())
        assert created, "첫 보고는 새 행이어야 한다"
        print("ok - append_cost_snapshot 신규 기록 (실 DB) 통과")

        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
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
            assert by_agent[0].recorded_by == "self-check", "보고자가 왕복에서 사라졌다"
            by_dept = repo.list_cost_snapshots_by_department(department_id, window_start=t0, window_end=t1)
            assert any(s.agent_id == agent_id for s in by_dept)
            print("ok - list_cost_snapshots_by_agent/by_department (실 DB) 통과")

            # 3) 같은 창 재보고 - 행이 늘지 않고 갱신돼야 한다. 여기가 이 writer 의
            #    핵심 계약이다(reader 가 창 안을 합산하므로 중복 행 = 사용량 2배).
            again_id, again_created = repo.append_cost_snapshot(
                _self_check_snapshot(input_tokens=300, model_cost=Decimal("4.5"))
            )
            assert again_created is False, "같은 창 재보고가 새 행을 만들었다"
            assert str(again_id) == str(cost_snapshot_id), "재보고가 다른 행으로 갔다"
            regathered = repo.list_cost_snapshots_by_agent(agent_id, window_start=t0, window_end=t1)
            assert len(regathered) == 1, f"재보고 후 행이 {len(regathered)}개 - 합산이 두 배가 된다"
            assert regathered[0].input_tokens == 300, "재보고 값이 반영되지 않았다"
            assert regathered[0].model_cost == Decimal("4.5")
            print("ok - 같은 창 재보고가 행을 늘리지 않고 갱신 (실 DB) 통과")

            # 4) 등록되지 않은 agent 로 온 보고는 일시 장애와 구분해서 거부한다.
            try:
                repo.append_cost_snapshot(
                    _self_check_snapshot(agent_id="00000000-0000-0000-0000-000000000000")
                )
                raise AssertionError("등록되지 않은 agent_id 가 통과함")
            except UnknownCostSnapshotSubjectError:
                pass
            print("ok - 미등록 agent_id 보고 거부 (실 DB) 통과")

            cap = repo.get_capacity_snapshot(department_id=department_id, window_start=t0, window_end=t1)
            assert cap is not None and cap.arrivals == 5
            print("ok - get_capacity_snapshot (실 DB) 통과")

            # 5) 불변식 2 - department/agent 둘 다 없으면 거부.
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
