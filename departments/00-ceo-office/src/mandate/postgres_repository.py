#!/usr/bin/env python3
"""MandateVersionRepository의 실제 PostgreSQL(Supabase governance 스키마) 구현.

담당: 영주 (CEO Office)
근거: HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F01, service.py의 MandateVersionRepository 인터페이스,
      departments/03-risk/risk_repository.py·departments/06-ai-qa-audit/audit/repository.py 패턴.

config.yaml의 not_started 항목은 이 구현을 "asyncpg"로 적어뒀지만, 실제 컨벤션은 그 뒤에
바뀌었다 - audit/repository.py docstring: "asyncpg가 아니라 psycopg2를 쓰는 이유: 이들을
부르는 scripts.py가 이미 동기다 - workforce F19(asyncpg)는 그쪽 도메인이 이미 비동기라
다르다." CEO의 daily_report.py/notification.py/mandate/service.py도 전부 동기라 Risk/QA와
같은 psycopg2로 맞춘다.

이 Repository는 governance.mandates 행이 **이미 존재한다고 가정한다.** Mandate 엔티티
생성(Fund 배정, 최초 owner 지정)은 F01 범위 밖이다(config.yaml "Y1 나머지" 백로그) -
여기서 mandates 행을 암묵적으로 만들지 않는다. 없는 mandate_id로 쓰면 FK 위반이나
"영향받은 행 0개"로 실패한다 - RiskDecisionRepository와 같은 fail-closed 원칙
(개발 원칙 9: 위험한 기능은 실패 시 확대가 아니라 차단).

자체 점검(python postgres_repository.py):
  - DATABASE_URL 없으면 import만 확인한다.
  - DATABASE_URL 있으면 실제 DB에 연결해 조회 경로(latest_version/content_hash_exists/
    get_mandate_current/get_fund_base_currency)를 존재하지 않는 UUID로 검증한다 - 이건
    governance.mandates 부모 행이 없어도 안전하게 통과한다.
  - insert()/record_decision() 등 쓰기 경로의 완전한 왕복 검증은 governance.mandates가
    요구하는 owner_user_id(auth.users FK)가 필요하다. 이 저장소에는 auth.users 행이
    0건이라(Supabase Auth로 생성돼야 함), 자체 점검에서 계정을 만들어 우회하지 않는다.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from typing import Any

from service import MandateDecisionRow, MandateVersionRepository, MandateVersionRow


class MandatePersistenceError(RuntimeError):
    """Mandate Version/Decision을 기록하거나 조회하지 못한 경우."""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> tuple[Any, Any]:
    """PostgreSQL 저장을 실제로 사용할 때만 psycopg2를 로드한다."""
    try:
        from psycopg2.extras import Json
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise MandatePersistenceError(
            "PostgreSQL Mandate 저장에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    return Json, ThreadedConnectionPool


def _json_safe(value: Any) -> Any:
    """psycopg2 Json에 넣을 수 있도록 결정론적 JSON 값으로 변환한다."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _json_safe(asdict(value))
    return value


class PostgresMandateVersionRepository(MandateVersionRepository):
    """`governance.mandates/mandate_versions/mandate_decisions` 전용 저장소."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresMandateVersionRepository:
        _, ThreadedConnectionPool = _load_postgres_driver()
        return cls(ThreadedConnectionPool(1, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    # --- 조회 (Fund/Mandate 부모 행 없어도 안전) -------------------------------

    def latest_version(self, mandate_id: str) -> int:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select max(version) from governance.mandate_versions where mandate_id = %s",
                    (mandate_id,),
                )
                row = cur.fetchone()
            conn.commit()
            return row[0] or 0
        finally:
            self._pool.putconn(conn)

    def content_hash_exists(self, mandate_id: str, content_hash: str) -> bool:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select exists(
                        select 1 from governance.mandate_versions
                        where mandate_id = %s and content_hash = %s
                    )
                    """,
                    (mandate_id, content_hash),
                )
                (exists,) = cur.fetchone()
            conn.commit()
            return bool(exists)
        finally:
            self._pool.putconn(conn)

    def get_mandate_current(self, mandate_id: str) -> tuple[int, str]:
        """(current_version, status). mandates 행 자체가 없으면 (0, 'DRAFT') - In-Memory와 동일 기본값."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select current_version, status from governance.mandates where mandate_id = %s",
                    (mandate_id,),
                )
                row = cur.fetchone()
            conn.commit()
            return (row[0], row[1]) if row else (0, "DRAFT")
        finally:
            self._pool.putconn(conn)

    def get_fund_base_currency(self, mandate_id: str) -> str | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select f.base_currency
                    from accounting.funds f
                    join governance.mandates m on m.fund_id = f.fund_id
                    where m.mandate_id = %s
                    """,
                    (mandate_id,),
                )
                row = cur.fetchone()
            conn.commit()
            return row[0] if row else None
        finally:
            self._pool.putconn(conn)

    # --- 쓰기 (governance.mandates 부모 행이 이미 있어야 한다) ------------------

    def insert(self, row: MandateVersionRow) -> None:
        Json, _ = _load_postgres_driver()
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into governance.mandate_versions (
                        mandate_id, version, objective_text, objective, allowed_assets,
                        forbidden_assets, universe_policy, risk_bounds, approval_rules,
                        execution_rules, effective_from, effective_to, content_hash, created_by
                    ) values (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        row.mandate_id, row.version, row.objective_text,
                        Json(_json_safe(row.objective)), Json(_json_safe(row.allowed_assets)),
                        Json(_json_safe(row.forbidden_assets)), Json(_json_safe(row.universe_policy)),
                        Json(_json_safe(row.risk_bounds)), Json(_json_safe(row.approval_rules)),
                        Json(_json_safe(row.execution_rules)), row.effective_from, row.effective_to,
                        row.content_hash, row.created_by,
                    ),
                )
            conn.commit()
        except Exception as exc:  # psycopg2 예외를 API 경계에서 통일한다.
            conn.rollback()
            raise MandatePersistenceError(f"Mandate Version 기록 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def set_mandate_current(self, mandate_id: str, version: int, status: str) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update governance.mandates
                    set current_version = %s, status = %s, updated_at = now()
                    where mandate_id = %s
                    """,
                    (version, status, mandate_id),
                )
                if cur.rowcount == 0:
                    raise MandatePersistenceError(
                        f"governance.mandates에 mandate_id={mandate_id} 행이 없다 - "
                        "Mandate 엔티티를 먼저 만들어야 한다(F01 범위 밖, Y1 나머지)"
                    )
            conn.commit()
        except MandatePersistenceError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise MandatePersistenceError(f"mandates.current_version 갱신 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def set_effective_to(self, mandate_id: str, version: int, ts: datetime) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update governance.mandate_versions
                    set effective_to = %s
                    where mandate_id = %s and version = %s and effective_to is null
                    """,
                    (ts, mandate_id, version),
                )
                if cur.rowcount == 0:
                    raise MandatePersistenceError(
                        f"종료할 활성 Version을 찾지 못했다 (mandate_id={mandate_id}, version={version}) - "
                        "이미 종료됐거나 존재하지 않는다"
                    )
            conn.commit()
        except MandatePersistenceError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise MandatePersistenceError(f"effective_to 갱신 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def record_decision(self, decision: MandateDecisionRow) -> None:
        Json, _ = _load_postgres_driver()
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                # mandate_decisions는 mandate_version_id(FK)를 쓴다 - 자연키(mandate_id, version)를
                # 먼저 조회해서 변환한다(service.py 문서화된 SQL 구현 방식과 동일).
                cur.execute(
                    """
                    select mandate_version_id from governance.mandate_versions
                    where mandate_id = %s and version = %s
                    """,
                    (decision.mandate_id, decision.version),
                )
                row = cur.fetchone()
                if row is None:
                    raise MandatePersistenceError(
                        f"mandate_version_id를 찾지 못했다 (mandate_id={decision.mandate_id}, "
                        f"version={decision.version})"
                    )
                mandate_version_id = row[0]
                cur.execute(
                    """
                    insert into governance.mandate_decisions (
                        mandate_version_id, decision, conditions, reason, approved_by,
                        trace_id, decided_at
                    ) values (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        mandate_version_id, decision.decision, Json(_json_safe(decision.conditions)),
                        decision.reason, decision.approved_by, decision.trace_id, decision.decided_at,
                    ),
                )
            conn.commit()
        except MandatePersistenceError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise MandatePersistenceError(f"Mandate Decision 기록 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/mandate/postgres_repository.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import uuid

    print("ok - import 확인 (psycopg2 lazy load)")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 조회 경로 실 DB 검증은 건너뛴다")
        raise SystemExit(0)

    repo = PostgresMandateVersionRepository.connect(dsn)
    try:
        missing = str(uuid.uuid4())

        # 1) 존재하지 않는 mandate_id - governance.mandates 부모 행 없이도 안전한 기본값.
        assert repo.latest_version(missing) == 0
        assert repo.content_hash_exists(missing, "x") is False
        assert repo.get_mandate_current(missing) == (0, "DRAFT")
        assert repo.get_fund_base_currency(missing) is None
        print("ok - 조회 경로 4개 (존재하지 않는 mandate_id, 실 DB) 통과")

        # 2) 쓰기 경로(insert/set_mandate_current/record_decision)는 governance.mandates
        #    부모 행이 필요하고, 그건 owner_user_id(auth.users FK)를 요구한다. 이 환경의
        #    auth.users는 0건이라(Supabase Auth로만 만들 수 있음) 계정을 만들어 우회하지
        #    않는다 - 여기서 검증을 멈춘다.
        print("SKIP - 쓰기 경로 왕복 검증: governance.mandates 부모 행이 없다 "
              "(auth.users 0건 -> owner_user_id FK 불가, 계정 생성으로 우회하지 않음)")
    finally:
        repo.close()
