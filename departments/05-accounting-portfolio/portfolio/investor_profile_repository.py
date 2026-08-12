#!/usr/bin/env python3
"""`accounting.investor_profiles` 저장소.

소유: 도현 (회계·포트폴리오본부)
근거: docs/02-engineering/USER_INPUT_API_SPEC.md 1.5(**G-2**), 2.3
      docs/01-product/USER_INPUT_SPEC.md 2(계층 1 - 화면 직접 선택)
      supabase/migrations/20260812000200_accounting_investor_profiles.sql

`ledger/repository.py`의 Pool·에러 매핑 관례를 그대로 따른다 - 같은 부서에서
저장소 두 개가 서로 다른 방식으로 연결·실패하면 운영자가 장애를 한 가지로
읽을 수 없다.

**이 모듈은 적합성 판정을 하지 않는다.** `effective_risk_band`는
`suitability.py`의 `min(mindset, experience)` 결과를 호출부가 그대로 노출하며
(USER_INPUT_API_SPEC 2.3 "화면이 재계산하지 않는다"), 여기서는 저장·조회만 한다.

**Version은 append 방식이다.** 수정(UPDATE)이 없다 - "그때 어떤 성향으로
추천했는가"가 감사 대상이라(API_SPEC 1.5) 과거 버전이 나중 입력으로 덮이면
과거 추천의 근거가 사라진다(개발 원칙 5). Mandate와 달리 승인 절차는 없다.

자체 점검: python departments/05-accounting-portfolio/portfolio/investor_profile_repository.py
          (DATABASE_URL 없으면 계약 검증만 하고 DB는 건너뛴다)
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator


class InvestorProfilePersistenceError(RuntimeError):
    """저장소를 쓸 수 없다. 호출자는 503으로 옮긴다."""


class InvestorProfileConflictError(InvestorProfilePersistenceError):
    """같은 (user, fund)에 같은 version이 동시에 들어왔다. 재시도로 풀린다."""


def _load_driver() -> tuple[Any, Any]:
    try:
        import psycopg2
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:  # pragma: no cover - 환경 문제
        raise InvestorProfilePersistenceError(
            "investor profile 저장에는 psycopg2-binary가 필요합니다."
        ) from exc
    return psycopg2, ThreadedConnectionPool


class InvestorProfileRepository:
    """`accounting.investor_profiles` 전용 저장소."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> InvestorProfileRepository:
        _, ThreadedConnectionPool = _load_driver()
        # minconn=0 - 유휴 커넥션을 잡지 않는다(ledger/repository.py와 같은 이유).
        return cls(ThreadedConnectionPool(0, 4, dsn))

    @classmethod
    def from_env(cls) -> InvestorProfileRepository | None:
        """`DATABASE_URL`이 없으면 None. 호출부가 503으로 fail closed 한다.

        인메모리 후퇴를 두지 않는 이유: 프로필은 "최초 1회 입력하고 계속 쓰는"
        값이라, 메모리에 저장하면 재기동 때 조용히 사라지고 사용자는 온보딩을
        다시 해야 한다. 저장이 안 되면 저장이 안 된다고 말하는 편이 낫다.
        """

        dsn = os.environ.get("DATABASE_URL", "").strip()
        if not dsn:
            return None
        return cls.connect(dsn)

    def close(self) -> None:
        self._pool.closeall()

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        psycopg2, _ = _load_driver()
        conn = self._pool.getconn()
        try:
            with conn:  # 정상 종료면 commit, 예외면 rollback
                with conn.cursor() as cur:
                    yield cur
        except psycopg2.errors.UniqueViolation as exc:
            raise InvestorProfileConflictError(
                f"investor profile version 충돌: {exc}"
            ) from exc
        except psycopg2.Error as exc:
            raise InvestorProfilePersistenceError(
                f"investor profile DB 작업 실패: {exc}"
            ) from exc
        finally:
            self._pool.putconn(conn)

    # -- 쓰기 -----------------------------------------------------------------

    def save(
        self,
        *,
        user_id: str,
        fund_id: str,
        mindset: str,
        experience: str,
        investment_horizon_years: int,
        max_drawdown_pct: str,
        liquidity_need: str,
        as_of: str,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """항상 새 version으로 저장한다(API_SPEC 2.3 "항상 새 `version`").

        version을 애플리케이션에서 미리 읽어 +1 하지 않는다 - 조회와 삽입 사이에
        다른 요청이 끼면 같은 번호를 두 개 만든다. `insert ... select max+1`을 한
        문장으로 두면 그 틈이 없고, 그래도 동시 트랜잭션이 겹치면
        `unique(user_id, fund_id, version)`이 잡아 `InvestorProfileConflictError`로
        올라간다(조용히 덮어쓰지 않는다).
        """

        with self.cursor() as cur:
            cur.execute(
                """
                insert into accounting.investor_profiles (
                    user_id, fund_id, version,
                    mindset, experience, investment_horizon_years,
                    max_drawdown_pct, liquidity_need, as_of, created_by
                )
                select
                    %(user_id)s, %(fund_id)s,
                    coalesce(max(version), 0) + 1,
                    %(mindset)s, %(experience)s, %(horizon)s,
                    %(drawdown)s, %(liquidity)s, %(as_of)s, %(created_by)s
                from accounting.investor_profiles
                where user_id = %(user_id)s and fund_id = %(fund_id)s
                returning investor_profile_id, version, as_of, created_at
                """,
                {
                    "user_id": user_id,
                    "fund_id": fund_id,
                    "mindset": mindset,
                    "experience": experience,
                    "horizon": investment_horizon_years,
                    "drawdown": max_drawdown_pct,
                    "liquidity": liquidity_need,
                    "as_of": as_of,
                    "created_by": created_by,
                },
            )
            row = cur.fetchone()
        if row is None:  # pragma: no cover - returning이 있으므로 도달 불가
            raise InvestorProfilePersistenceError("investor profile 저장 결과가 비었습니다")
        return {
            "investor_profile_id": str(row[0]),
            "version": int(row[1]),
            "as_of": row[2].isoformat().replace("+00:00", "Z") if row[2] else None,
            "created_at": row[3].isoformat().replace("+00:00", "Z") if row[3] else None,
        }

    # -- 읽기 -----------------------------------------------------------------

    def current(self, *, user_id: str, fund_id: str) -> dict[str, Any] | None:
        """가장 높은 version 하나. 없으면 None(호출부가 404로 옮긴다)."""

        with self.cursor() as cur:
            cur.execute(
                """
                select investor_profile_id, version, mindset, experience,
                       investment_horizon_years, max_drawdown_pct, liquidity_need,
                       as_of, created_at
                from accounting.investor_profiles
                where user_id = %s and fund_id = %s
                order by version desc
                limit 1
                """,
                (user_id, fund_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "investor_profile_id": str(row[0]),
            "version": int(row[1]),
            "user_id": user_id,
            "fund_id": fund_id,
            "mindset": row[2],
            "experience": row[3],
            "investment_horizon_years": int(row[4]),
            # numeric -> str. float로 넘기면 0.15가 0.14999...로 바뀔 수 있어
            # 한도 값을 실수로 다루지 않는다(개발 원칙 2와 같은 방향).
            "max_drawdown_pct": str(row[5]),
            "liquidity_need": row[6],
            "as_of": row[7].isoformat().replace("+00:00", "Z") if row[7] else None,
            "created_at": row[8].isoformat().replace("+00:00", "Z") if row[8] else None,
        }


__all__ = [
    "InvestorProfileConflictError",
    "InvestorProfilePersistenceError",
    "InvestorProfileRepository",
]


if __name__ == "__main__":  # 자체 점검 (pytest 미도입 - CLAUDE.md)
    # 1) DATABASE_URL 없으면 from_env가 None을 준다 (인메모리 후퇴 없음)
    saved = os.environ.pop("DATABASE_URL", None)
    try:
        assert InvestorProfileRepository.from_env() is None, "DSN 없으면 None이어야 한다"
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved

    # 2) 에러 계층 - Conflict는 Persistence의 하위여야 호출부가 하나로 잡을 수 있다
    assert issubclass(InvestorProfileConflictError, InvestorProfilePersistenceError)

    print("investor_profile_repository 자체 점검 통과")
