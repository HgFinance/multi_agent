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
  - insert()/set_mandate_current()/set_effective_to()/record_decision() 쓰기 경로는
    governance.mandates 부모 행이 필요하고, 그 행은 owner_user_id(auth.users FK)를
    요구한다. auth.users는 Supabase Auth로만 만들 수 있어(계정 생성으로 우회 금지)
    2026-08-03까지 왕복 검증을 못 했다. 2026-08-04 GOV-02 2단계에서 플레이스홀더
    회원(supabase/seed.sql, RFC 2606 .invalid 주소, 로그인 불가)을 seed한 뒤로는
    tests/schema/supabase_governance_test_fixture.sql의 TEST-CEO-MANDATE Fund와
    엮어 실제 MandateVersionService.propose_version()/MandateActivationService.
    activate()를 그대로 태워 4개 쓰기 메서드 전부 검증한다(아래 자체 점검 참고).
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from typing import Any

from service import (
    FundNotFoundError,
    MandateAlreadyExistsError,
    MandateDecisionRow,
    MandateVersionRepository,
    MandateVersionRow,
)


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
        # minconn=0 - 유휴 커넥션을 잡지 않는다
        return cls(ThreadedConnectionPool(0, 4, dsn))

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

    def get_mandate_metadata(self, mandate_id: str) -> dict | None:
        """Return the single current UI metadata object for a Mandate."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select metadata from governance.mandates where mandate_id = %s",
                    (mandate_id,),
                )
                row = cur.fetchone()
            conn.commit()
            if not row or not row[0]:
                return None
            return dict(row[0])
        finally:
            self._pool.putconn(conn)

    def replace_mandate_metadata(self, mandate_id: str, metadata: dict) -> None:
        """Replace the current Mandate metadata in one parent row."""
        Json, _ = _load_postgres_driver()
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update governance.mandates
                       set metadata = %s,
                           status = 'ACTIVE',
                           current_version = 0,
                           updated_at = now()
                     where mandate_id = %s
                    """,
                    (Json(_json_safe(metadata)), mandate_id),
                )
                if cur.rowcount == 0:
                    raise MandatePersistenceError(
                        f"governance.mandates에 mandate_id={mandate_id} 행이 없다"
                    )
            conn.commit()
        except MandatePersistenceError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise MandatePersistenceError(f"Mandate metadata 갱신 실패: {exc}") from exc
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

    def get_mandate_version_id(self, mandate_id: str, version: int) -> str | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select mandate_version_id from governance.mandate_versions "
                    "where mandate_id = %s and version = %s",
                    (mandate_id, version),
                )
                row = cur.fetchone()
            conn.commit()
            return str(row[0]) if row else None
        finally:
            self._pool.putconn(conn)
    def get_mandate_content_hash(self, mandate_id: str, version: int) -> str | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select content_hash from governance.mandate_versions "
                    "where mandate_id = %s and version = %s",
                    (mandate_id, version),
                )
                row = cur.fetchone()
            conn.commit()
            return str(row[0]) if row and row[0] else None
        finally:
            self._pool.putconn(conn)

    def mandate_ids_for_fund(self, fund_id: str) -> list[str]:
        """Return all Mandates for a Fund; the API rejects ambiguous results."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select mandate_id from governance.mandates where fund_id = %s",
                    (fund_id,),
                )
                rows = cur.fetchall()
            conn.commit()
            return [str(row[0]) for row in rows]
        finally:
            self._pool.putconn(conn)

    def create_mandate(self, *, fund_id: str, owner_user_id: str, name: str) -> str:
        """`governance.mandates` 부모 행 하나. 2026-08-12 신설.

        그 전까지 이 INSERT 는 `change_workflow.py` 자체 점검 코드 안에만 있어서
        최초 Mandate 를 만들 API 경로가 없었다(온보딩 첫 사용자가 시작할 수 없었다).

        `status`/`current_version` 을 명시하지 않는다 - DDL 기본값이 `'DRAFT'`/`0`
        이고, 그게 "Version 이 아직 없다" 는 정확한 상태다. 여기서 `ACTIVE` 로
        만들면 정책 없는 Mandate 가 활성으로 보인다.

        `fund_id`·`owner_user_id` 의 존재 검증을 따로 하지 않는다 - FK
        (`accounting.funds`, `governance.user_profiles`)가 이미 잡고, 조회 후 INSERT
        사이에 행이 사라지는 틈도 FK 에는 없다. 애플리케이션에서 미리 확인하면
        그 틈이 생기고 검사도 두 곳으로 갈라진다.
        """
        # 예외 타입만 필요하다. _load_postgres_driver() 는 Json/Pool 을 주므로
        # 여기서는 psycopg2 자체를 지역 import 한다 - Pool 이 이미 살아 있는
        # 시점이라 드라이버 부재는 발생할 수 없다.
        import psycopg2

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "insert into governance.mandates (fund_id, owner_user_id, name) "
                    "values (%s, %s, %s) returning mandate_id",
                    (fund_id, owner_user_id, name),
                )
                row = cur.fetchone()
            conn.commit()
            if row is None:  # pragma: no cover - returning 이 있어 도달 불가
                raise MandatePersistenceError("Mandate 생성 결과가 비었습니다")
            return str(row[0])
        except psycopg2.errors.UniqueViolation as exc:
            # unique (fund_id, name). 기존 것을 조용히 돌려주지 않고 id 만 알려준다 -
            # "새로 만들었다" 와 "이미 있었다" 를 호출자가 구분해야 한다.
            conn.rollback()
            existing = self._mandate_id_by_fund_name(conn, fund_id, name)
            raise MandateAlreadyExistsError(
                f"fund_id={fund_id} 에 name={name!r} Mandate 가 이미 있습니다",
                mandate_id=existing,
            ) from exc
        except psycopg2.errors.ForeignKeyViolation as exc:
            conn.rollback()
            # 어느 FK 인지까지 지어내지 않는다 - 메시지에 제약 이름이 들어 있다.
            raise FundNotFoundError(
                f"fund_id 또는 owner_user_id 가 존재하지 않습니다: {exc}"
            ) from exc
        except psycopg2.Error as exc:
            conn.rollback()
            raise MandatePersistenceError(f"Mandate 생성 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _mandate_id_by_fund_name(conn: Any, fund_id: str, name: str) -> str | None:
        """중복 충돌 때 기존 mandate_id 를 찾는다. 실패해도 None 으로 삼킨다 -
        이 조회가 깨져서 원래 에러(중복)를 가리면 안 된다."""
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select mandate_id from governance.mandates "
                    "where fund_id = %s and name = %s",
                    (fund_id, name),
                )
                found = cur.fetchone()
            conn.commit()
            return str(found[0]) if found else None
        except Exception:  # pragma: no cover - 보조 조회
            conn.rollback()
            return None

    def get(self, mandate_id: str, version: int) -> MandateVersionRow | None:
        """USER_INPUT_API_SPEC.md 2.1 - GET .../current 가 전체 policy 를 돌려주는 데 쓴다.

        jsonb 컬럼(objective/allowed_assets/forbidden_assets/universe_policy/risk_bounds/
        approval_rules/execution_rules)은 psycopg2 가 dict/list 로 자동 변환해 돌려준다 -
        insert() 의 Json() 래핑과 대칭이라 여기선 추가 파싱이 필요 없다.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select mandate_id, version, objective_text, objective, allowed_assets,
                           forbidden_assets, universe_policy, risk_bounds, approval_rules,
                           execution_rules, effective_from, effective_to, content_hash, created_by
                    from governance.mandate_versions
                    where mandate_id = %s and version = %s
                    """,
                    (mandate_id, version),
                )
                row = cur.fetchone()
            conn.commit()
            if row is None:
                return None
            return MandateVersionRow(
                mandate_id=str(row[0]), version=row[1], objective_text=row[2], objective=row[3],
                allowed_assets=row[4], forbidden_assets=row[5], universe_policy=row[6],
                risk_bounds=row[7], approval_rules=row[8], execution_rules=row[9],
                effective_from=row[10], effective_to=row[11], content_hash=row[12],
                created_by=str(row[13]) if row[13] is not None else None,
            )
        finally:
            self._pool.putconn(conn)

    def mandate_ids_for_fund(self, fund_id: str) -> list[str]:
        """USER_INPUT_API_SPEC.md 2.1 - fund_id 기준 조회(by-fund Route)가 쓴다.

        unique(fund_id, name) 이라 한 Fund에 이름이 다른 Mandate가 여러 개 있을 수
        있다 - 여기서 하나를 임의로 고르지 않고 전부 돌려준다. 몇 개인지 판단은
        호출자(app.py)가 한다(0=404, 1=단일 조회, 2개 이상=409 모호).
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select mandate_id from governance.mandates where fund_id = %s",
                    (fund_id,),
                )
                rows = cur.fetchall()
            conn.commit()
            return [str(r[0]) for r in rows]
        finally:
            self._pool.putconn(conn)

    def mandate_ids_for_fund_owner(
        self, fund_id: str, owner_user_id: str
    ) -> list[str]:
        """Return Mandates scoped to the selected Fund and Mandate owner."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select mandate_id
                      from governance.mandates
                     where fund_id = %s and owner_user_id = %s
                    """,
                    (fund_id, owner_user_id),
                )
                rows = cur.fetchall()
            conn.commit()
            return [str(row[0]) for row in rows]
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
    import sys
    import uuid
    from datetime import timedelta, timezone
    from pathlib import Path

    print("ok - import 확인 (psycopg2 lazy load)")

    from dotenv import load_dotenv

    load_dotenv()  # 저장소 루트 .env - 이미 설정된 값은 덮어쓰지 않는다.

    dsn = os.environ.get("GOVERNANCE_WORKFORCE_DATABASE_URL") or os.environ.get("DATABASE_URL")
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

        # 2) 쓰기 경로(insert/set_mandate_current/set_effective_to/record_decision) -
        #    tests/schema/supabase_governance_test_fixture.sql의 TEST-CEO-MANDATE Fund와
        #    supabase/seed.sql의 플레이스홀더 회원이 둘 다 있어야 governance.mandates
        #    부모 행을 만들 수 있다(둘 다 이 Repository의 책임 밖 - docstring 참고).
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("select fund_id from accounting.funds where fund_code = %s",
                           ("TEST-CEO-MANDATE",))
                fund_row = cur.fetchone()
                cur.execute("select user_id from governance.user_profiles limit 1")
                user_row = cur.fetchone()
        finally:
            repo._pool.putconn(conn)

        if fund_row is None or user_row is None:
            print("SKIP - 쓰기 경로 왕복 검증: TEST-CEO-MANDATE Fund 또는 플레이스홀더 "
                  "회원이 없다 (psql -f tests/schema/supabase_governance_test_fixture.sql "
                  "과 supabase/seed.sql 선행 필요)")
            raise SystemExit(0)
        fund_id, owner_user_id = str(fund_row[0]), str(user_row[0])

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from lifecycle import MandateActivationService, UserApproval
        from policy import (
            ApprovalRules,
            MandatePolicy,
            PaperOrderMode,
            RiskBounds,
            UniversePolicy,
        )
        from service import MandateVersionService

        def _policy(base_capital: str, max_instrument_weight: str) -> MandatePolicy:
            return MandatePolicy(
                allowed_assets=["A005930"], forbidden_assets=[],
                risk_bounds=RiskBounds(
                    base_capital=base_capital, currency="KRW",
                    max_instrument_weight=max_instrument_weight, max_sector_weight="0.3",
                    max_gross_exposure="1.0", max_concurrent_positions=10, max_daily_loss="0.03",
                ),
                universe_policy=UniversePolicy(
                    allowed_markets=["KRX"], trading_start="09:00", trading_end="15:30",
                ),
                approval_rules=ApprovalRules(paper_order_mode=PaperOrderMode.USER_APPROVAL),
            )

        version_service = MandateVersionService(repo)
        activation_service = MandateActivationService(repo)
        t0 = datetime(2026, 8, 4, tzinfo=timezone.utc)
        mandate_name = "GOV write-path selfcheck (postgres_repository.py)"

        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                # 이전 실행이 중간에 죽었을 경우를 대비해 같은 이름의 행을 먼저 정리한다
                # (mandates에 unique(fund_id, name) - 이 이름은 이 자체 점검 전용이다).
                cur.execute(
                    "select mandate_id from governance.mandates where fund_id = %s and name = %s",
                    (fund_id, mandate_name),
                )
                stale = cur.fetchone()
                if stale is not None:
                    stale_id = stale[0]
                    cur.execute(
                        "delete from governance.mandate_decisions where mandate_version_id in "
                        "(select mandate_version_id from governance.mandate_versions where mandate_id = %s)",
                        (stale_id,),
                    )
                    cur.execute("delete from governance.mandate_versions where mandate_id = %s", (stale_id,))
                    cur.execute("delete from governance.mandates where mandate_id = %s", (stale_id,))
                cur.execute(
                    "insert into governance.mandates (fund_id, owner_user_id, name) "
                    "values (%s, %s, %s) returning mandate_id",
                    (fund_id, owner_user_id, mandate_name),
                )
                mandate_id = str(cur.fetchone()[0])
            conn.commit()
        finally:
            repo._pool.putconn(conn)

        try:
            # 3) v1 제안(insert) -> 최초 활성화(set_mandate_current + record_decision,
            #    이전 Version이 없어 set_effective_to는 안 탄다).
            r1 = version_service.propose_version(
                mandate_id=mandate_id, policy=_policy("100000000", "0.1"),
                objective_text="자체 점검", objective={"style": "growth"}, effective_from=t0,
                created_by=owner_user_id,
            )
            assert r1.row.version == 1
            a1 = activation_service.activate(
                mandate_id=mandate_id, version=1, direction=r1.direction, at=t0,
                approval=UserApproval(approved_by=owner_user_id, trace_id=str(uuid.uuid4()),
                                       reason="최초 활성화"),
            )
            assert a1.activated is True
            assert repo.get_mandate_current(mandate_id) == (1, "ACTIVE")
            print("ok - v1 제안+최초 활성화 (실 DB) 통과 - insert/set_mandate_current/"
                  "record_decision 확인")

            # 4) v2 제안(TIGHTEN: 한도를 더 좁힘) -> 승인 없이 즉시 활성화
            #    (set_effective_to로 v1 종료 + set_mandate_current + record_decision).
            # t1 > t0로 둔다 - mandate_versions DDL이 effective_to > effective_from을
            # 강제해서(check 제약), v1 종료 시각이 v1의 effective_from과 같으면 거부된다.
            t1 = t0 + timedelta(hours=1)
            r2 = version_service.propose_version(
                mandate_id=mandate_id, policy=_policy("100000000", "0.05"),
                previous_policy=_policy("100000000", "0.1"),
                objective_text="자체 점검 v2", objective={"style": "growth"},
                effective_from=t1, created_by=owner_user_id,
            )
            assert r2.row.version == 2 and r2.direction.value == "TIGHTEN"
            a2 = activation_service.activate(
                mandate_id=mandate_id, version=2, direction=r2.direction, at=t1,
            )
            assert a2.activated is True and a2.decision.approved_by is None  # 자동 적용
            assert repo.get_mandate_current(mandate_id) == (2, "ACTIVE")
            print("ok - v2 제안+자동 활성화(TIGHTEN) (실 DB) 통과 - set_effective_to 확인")

            # 4b) get_mandate_version_id - HITL(2026-08-04)이 governance.approvals.object_id
            # 로 쓸 실제 uuid PK. 존재하는 (mandate_id, version)은 uuid, 없는 조합은 None.
            v1_id = repo.get_mandate_version_id(mandate_id, 1)
            v2_id = repo.get_mandate_version_id(mandate_id, 2)
            assert v1_id is not None and v2_id is not None and v1_id != v2_id
            assert repo.get_mandate_version_id(mandate_id, 99) is None
            print(f"ok - get_mandate_version_id (실 DB) 통과 (v1={v1_id[:8]}..., v2={v2_id[:8]}...)")

            # 4c) get() - USER_INPUT_API_SPEC.md 2.1, GET .../current 가 전체 policy 를
            # 돌려주는 데 쓴다. jsonb 왕복(psycopg2 자동 dict 변환)이 실제로 되는지 확인.
            fetched_v2 = repo.get(mandate_id, 2)
            assert fetched_v2 is not None
            assert fetched_v2.content_hash == r2.row.content_hash
            assert fetched_v2.risk_bounds["max_instrument_weight"] == "0.05", fetched_v2.risk_bounds
            assert isinstance(fetched_v2.universe_policy, dict)
            assert isinstance(fetched_v2.allowed_assets, list)
            assert repo.get(mandate_id, 99) is None, "없는 Version 은 None"
            print("ok - get() (실 DB) 통과 - jsonb 컬럼 dict/list 왕복 확인")

            # 4d) mandate_ids_for_fund() - 방금 만든 mandate_id 가 포함되는지만 확인한다
            # (TEST-CEO-MANDATE Fund 에 다른 자체 점검이 남긴 행이 있을 수 있어 exact match 안 함).
            fund_mandates = repo.mandate_ids_for_fund(fund_id)
            assert mandate_id in fund_mandates
            assert repo.mandate_ids_for_fund(str(uuid.uuid4())) == [], "없는 fund_id 는 빈 목록"
            print(f"ok - mandate_ids_for_fund (실 DB) 통과 - fund_id={fund_id[:8]}... 에 "
                  f"{len(fund_mandates)}개")

            # 5) DB에서 직접 확인 - v1이 실제로 종료됐는지, Decision이 2건 쌓였는지.
            conn = repo._pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "select version, effective_to is not null from governance.mandate_versions "
                        "where mandate_id = %s order by version",
                        (mandate_id,),
                    )
                    versions = cur.fetchall()
                    cur.execute(
                        "select count(*) from governance.mandate_decisions d "
                        "join governance.mandate_versions v on v.mandate_version_id = d.mandate_version_id "
                        "where v.mandate_id = %s",
                        (mandate_id,),
                    )
                    decision_count = cur.fetchone()[0]
            finally:
                repo._pool.putconn(conn)
            assert versions == [(1, True), (2, False)], versions
            assert decision_count == 2
            print("ok - DB 직접 조회 통과 - v1.effective_to 종료됨, Decision 2건 확인")

            # 6) content_hash 중복 방지 - 같은 policy로 재제안하면 거부돼야 한다(DDL unique).
            assert repo.content_hash_exists(mandate_id, r1.row.content_hash) is True
        finally:
            # 정리 - mandate_decisions/mandate_versions/mandates에는 append-only 트리거가
            # 없다(case_events/improvement_candidate_events와 다른 점, 2026-08-04 실측).
            conn = repo._pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "delete from governance.mandate_decisions where mandate_version_id in "
                        "(select mandate_version_id from governance.mandate_versions where mandate_id = %s)",
                        (mandate_id,),
                    )
                    cur.execute("delete from governance.mandate_versions where mandate_id = %s", (mandate_id,))
                    cur.execute("delete from governance.mandates where mandate_id = %s", (mandate_id,))
                conn.commit()
            finally:
                repo._pool.putconn(conn)
            print("ok - 자체 점검 행 정리 완료 (mandates/mandate_versions/mandate_decisions)")
    finally:
        repo.close()
