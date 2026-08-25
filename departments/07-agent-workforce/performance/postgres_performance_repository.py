#!/usr/bin/env python3
"""review.py/action.py 가 쓰는 PerformanceReview/PerformanceAction 의 PostgreSQL 계층.

소유: 영주 (Agent Workforce 인사팀)
근거: review.py/action.py(순수 계약·상태 머신, DB를 모른다),
      planning/postgres_plan_repository.py(같은 psycopg2 pool 패턴)
      대응 테이블: workforce.performance_reviews(20260729000200_governance_workforce.sql),
      workforce.performance_actions(20260731000800_workforce_plan_quality_probation.sql)

이 모듈은 매핑만 한다 - 판정(근거 없는 조치 제안 거부, VERIFIED 근거 요구, 평가·조치
종류 일치)은 전부 review.py/action.py 가 이미 했고, 여기서 다시 하지 않는다.

불변식:
  1. performance_reviews 는 (agent_id, profile_version_id, period_start, period_end)
     unique 다 - 같은 기간 재평가는 새 행이 아니라 **갱신**이다. cost_snapshots 와
     같은 이유: 같은 기간에 평가가 두 벌이면 어느 것이 유효한지 알 수 없다.
  2. 등록되지 않은 agent/profile version/review 로 온 기록은 다른 실패와 구분해
     거부한다(UnknownPerformanceSubjectError) - 재시도해도 낫지 않는 호출자 오류다.
  3. 조회 결과가 없으면 None/빈 목록을 그대로 돌려주고 기본값을 만들지 않는다
     (cost.py 불변식 3과 같은 원칙).
  4. 같은 Agent 에 **열린 수습은 하나뿐이다**. 둘이 동시에 열려 있으면 어느 기준으로
     판정할지가 정해지지 않는다 - probation.py 의 불변식 1(기준을 미리 고정)이
     의미를 가지려면 그 기준이 하나여야 한다. 이건 행 하나만 보는 DDL check 로는
     못 막아서 여기서 막는다.

자체 점검: python departments/07-agent-workforce/performance/postgres_performance_repository.py
  - DATABASE_URL 없으면 import 만 확인한다(이 모듈의 거부 조건은 전부 DB 제약이라
    DSN 없이 검증할 수 있는 것이 없다 - 계약 검증은 review.py/action.py 자체 점검).
"""
from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from typing import Any

from action import ActionStatus, ActionType, PerformanceAction
from probation import ProbationPeriod, ProbationResult, ProbationStage
from review import PerformanceReview, ReviewDecision


class PerformancePersistenceError(RuntimeError):
    """workforce.performance_reviews/performance_actions 기록·조회에 실패한 경우."""


class UnknownPerformanceSubjectError(PerformancePersistenceError):
    """등록되지 않은 agent_id/profile_version_id/review_id 로 기록하려 한 경우 (불변식 2)."""


class OverlappingProbationError(PerformancePersistenceError):
    """같은 Agent 에 열린 수습이 이미 있다 (불변식 4).

    둘이 동시에 열려 있으면 어느 기준으로 판정할지가 정해지지 않는다 - 앞의 수습을
    먼저 닫아야 한다(EXTENDED 로 닫고 새 기간을 여는 것이 정상 경로다).
    """


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise PerformancePersistenceError(
            "PostgreSQL 조회에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    return ThreadedConnectionPool


class PostgresPerformanceRepository:
    """`workforce.performance_reviews`/`workforce.performance_actions` 읽기/쓰기."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresPerformanceRepository:
        ThreadedConnectionPool = _load_postgres_driver()
        return cls(ThreadedConnectionPool(0, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    # --- performance_reviews ---------------------------------------------------

    def save_review(self, review: PerformanceReview) -> tuple[str, bool]:
        """평가 1건을 적는다. (review_id, 새로 만들었는가) 반환.

        같은 (agent, profile version, period) 재평가는 행을 늘리지 않고 갱신한다
        (불변식 1). review_id 는 DB 가 생성하므로, 갱신이면 기존 행의 id 가 돌아온다 -
        review.review_id 는 신규일 때만 쓰인다.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into workforce.performance_reviews (
                        agent_id, profile_version_id, period_start, period_end,
                        role_metrics, cost, findings, decision, reviewer
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (agent_id, profile_version_id, period_start, period_end)
                    do update set
                        role_metrics = excluded.role_metrics,
                        cost = excluded.cost,
                        findings = excluded.findings,
                        decision = excluded.decision,
                        reviewer = excluded.reviewer
                    returning review_id, (xmax = 0) as inserted
                    """,
                    (review.agent_id, review.profile_version_id,
                     review.period_start, review.period_end,
                     json.dumps(review.role_metrics), json.dumps(review.cost),
                     json.dumps(review.findings), review.decision.value, review.reviewer),
                )
                review_id, inserted = cur.fetchone()
            conn.commit()
            return str(review_id), bool(inserted)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            raise self._mapped_error(exc, f"performance_review 기록 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def get_review(self, review_id: str) -> PerformanceReview | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select review_id, agent_id, profile_version_id, period_start, period_end,
                           role_metrics, cost, findings, decision, reviewer
                    from workforce.performance_reviews where review_id = %s
                    """,
                    (review_id,),
                )
                row = cur.fetchone()
            conn.commit()
            return None if row is None else self._to_review(row)
        finally:
            self._pool.putconn(conn)

    def list_reviews_by_agent(self, agent_id: str) -> list[PerformanceReview]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select review_id, agent_id, profile_version_id, period_start, period_end,
                           role_metrics, cost, findings, decision, reviewer
                    from workforce.performance_reviews
                    where agent_id = %s order by period_start
                    """,
                    (agent_id,),
                )
                rows = cur.fetchall()
            conn.commit()
            return [self._to_review(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_review(db_row: tuple) -> PerformanceReview:
        (review_id, agent_id, profile_version_id, period_start, period_end,
         role_metrics, cost, findings, decision, reviewer) = db_row
        return PerformanceReview(
            review_id=str(review_id), agent_id=str(agent_id),
            profile_version_id=str(profile_version_id),
            period_start=period_start, period_end=period_end,
            role_metrics=role_metrics or {}, cost=cost or {}, findings=findings or [],
            decision=ReviewDecision(decision), reviewer=reviewer,
        )

    # --- performance_actions ---------------------------------------------------

    def save_action(self, action: PerformanceAction) -> str:
        """조치 1건을 적거나 갱신한다. action_id 반환.

        review 와 달리 자연 unique key 가 없다(같은 Agent 에 같은 종류의 조치가 여러
        번 있을 수 있다) - action_id 로 upsert 한다. 상태 전이는 action.transition 이
        이미 검증했고 여기서는 결과만 반영한다.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into workforce.performance_actions (
                        action_id, agent_id, review_id, action_type, plan, due_at,
                        verification, status, completed_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (action_id) do update set
                        plan = excluded.plan,
                        due_at = excluded.due_at,
                        verification = excluded.verification,
                        status = excluded.status,
                        completed_at = excluded.completed_at
                    returning action_id
                    """,
                    (action.action_id, action.agent_id, action.review_id,
                     action.action_type.value, json.dumps(action.plan), action.due_at,
                     json.dumps(action.verification) if action.verification is not None else None,
                     action.status.value, action.completed_at),
                )
                action_id = cur.fetchone()[0]
            conn.commit()
            return str(action_id)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            raise self._mapped_error(exc, f"performance_action 기록 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def get_action(self, action_id: str) -> PerformanceAction | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select action_id, agent_id, review_id, action_type, plan, due_at,
                           verification, status, completed_at
                    from workforce.performance_actions where action_id = %s
                    """,
                    (action_id,),
                )
                row = cur.fetchone()
            conn.commit()
            return None if row is None else self._to_action(row)
        finally:
            self._pool.putconn(conn)

    def list_actions_by_agent(
        self, agent_id: str, *, status: ActionStatus | None = None
    ) -> list[PerformanceAction]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                if status is None:
                    cur.execute(
                        """
                        select action_id, agent_id, review_id, action_type, plan, due_at,
                               verification, status, completed_at
                        from workforce.performance_actions
                        where agent_id = %s order by due_at
                        """,
                        (agent_id,),
                    )
                else:
                    cur.execute(
                        """
                        select action_id, agent_id, review_id, action_type, plan, due_at,
                               verification, status, completed_at
                        from workforce.performance_actions
                        where agent_id = %s and status = %s order by due_at
                        """,
                        (agent_id, status.value),
                    )
                rows = cur.fetchall()
            conn.commit()
            return [self._to_action(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_action(db_row: tuple) -> PerformanceAction:
        (action_id, agent_id, review_id, action_type, plan, due_at,
         verification, status, completed_at) = db_row
        return PerformanceAction(
            action_id=str(action_id), agent_id=str(agent_id),
            review_id=str(review_id) if review_id else None,
            action_type=ActionType(action_type), plan=plan or {}, due_at=due_at,
            verification=verification, status=ActionStatus(status),
            completed_at=completed_at,
        )

    # --- probation_periods -----------------------------------------------------

    def open_probation(self, probation: ProbationPeriod) -> str:
        """수습 1건을 연다. probation_id 반환.

        같은 Agent 에 열린 수습이 이미 있으면 거절한다(불변식 4).

        실제 방어는 부분 unique index 다(20260825000600 migration) - 열린 수습이 없을
        때는 잠글 행이 없어서 select-then-insert 로는 동시 요청 둘을 막지 못한다.
        여기 select 는 **더 나은 에러 메시지**를 위한 것이고, 경합에서 뚫리면 아래
        23505(unique_violation)가 같은 예외로 접힌다.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select probation_id from workforce.probation_periods
                    where agent_id = %s and ended_at is null
                    for update
                    """,
                    (probation.agent_id,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    conn.rollback()
                    raise OverlappingProbationError(
                        f"agent_id={probation.agent_id} 에 이미 열린 수습이 있다 "
                        f"(probation_id={existing[0]}) - 먼저 판정해 닫아야 한다"
                    )
                cur.execute(
                    """
                    insert into workforce.probation_periods (
                        probation_id, agent_id, profile_version_id, stage,
                        started_at, success_metrics
                    ) values (%s, %s, %s, %s, %s, %s)
                    returning probation_id
                    """,
                    (probation.probation_id, probation.agent_id,
                     probation.profile_version_id, probation.stage.value,
                     probation.started_at, json.dumps(probation.success_metrics)),
                )
                probation_id = cur.fetchone()[0]
            conn.commit()
            return str(probation_id)
        except OverlappingProbationError:
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            # 23505 = unique_violation. 부분 unique index 가 경합을 잡은 경우라
            # 위 select 가 놓친 것과 같은 결함이다 - 같은 예외로 접는다.
            if getattr(exc, "pgcode", None) == "23505":
                raise OverlappingProbationError(
                    f"agent_id={probation.agent_id} 에 이미 열린 수습이 있다 - 먼저 판정해 닫아야 한다"
                ) from exc
            raise self._mapped_error(exc, f"probation 기록 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def close_probation(self, probation: ProbationPeriod) -> None:
        """판정 결과를 반영한다.

        success_metrics 를 update 목록에 넣지 않는다 - 기준은 판정 시점에 바뀌지
        않는다(probation.py 불변식 2). close_probation 이 인자로도 안 받지만, 저장
        계층에서 한 번 더 막아 다른 경로로 새는 것을 방지한다.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update workforce.probation_periods
                    set ended_at = %s, result = %s
                    where probation_id = %s and ended_at is null
                    """,
                    (probation.ended_at,
                     probation.result.value if probation.result else None,
                     probation.probation_id),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    raise PerformancePersistenceError(
                        f"probation_id={probation.probation_id} 가 없거나 이미 종료됐다"
                    )
            conn.commit()
        except PerformancePersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            raise self._mapped_error(exc, f"probation 판정 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def get_probation(self, probation_id: str) -> ProbationPeriod | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select probation_id, agent_id, profile_version_id, stage,
                           started_at, success_metrics, ended_at, result
                    from workforce.probation_periods where probation_id = %s
                    """,
                    (probation_id,),
                )
                row = cur.fetchone()
            conn.commit()
            return None if row is None else self._to_probation(row)
        finally:
            self._pool.putconn(conn)

    def list_probations_by_agent(self, agent_id: str) -> list[ProbationPeriod]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select probation_id, agent_id, profile_version_id, stage,
                           started_at, success_metrics, ended_at, result
                    from workforce.probation_periods
                    where agent_id = %s order by started_at
                    """,
                    (agent_id,),
                )
                rows = cur.fetchall()
            conn.commit()
            return [self._to_probation(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_probation(db_row: tuple) -> ProbationPeriod:
        (probation_id, agent_id, profile_version_id, stage, started_at,
         success_metrics, ended_at, result) = db_row
        return ProbationPeriod(
            probation_id=str(probation_id), agent_id=str(agent_id),
            profile_version_id=str(profile_version_id), stage=ProbationStage(stage),
            started_at=started_at, success_metrics=success_metrics or {},
            ended_at=ended_at, result=ProbationResult(result) if result else None,
        )

    @staticmethod
    def _mapped_error(exc: Exception, message: str) -> PerformancePersistenceError:
        # 23503 = foreign_key_violation. 등록되지 않은 agent/profile version/review 로
        # 온 기록은 재시도해도 낫지 않는다 - 일시 장애와 뭉뚱그리면 호출자가 재시도
        # 루프에 갇힌다(postgres_scorecard_repository 와 같은 처리).
        if getattr(exc, "pgcode", None) == "23503":
            return UnknownPerformanceSubjectError(
                f"등록되지 않은 agent_id/profile_version_id/review_id: {exc}"
            )
        return PerformancePersistenceError(message)


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    print("ok - import 확인 (psycopg2 lazy load)")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        # 이 모듈의 거부 조건은 전부 DB 제약(FK·unique)이라 DSN 없이 검증할 것이 없다.
        # 계약 검증은 review.py/action.py 자체 점검이 담당한다.
        print("DATABASE_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    print("SKIP - 실 DB 왕복 검증은 Migration 적용 후 별도로 수행한다")
