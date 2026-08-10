#!/usr/bin/env python3
"""Platform/IAM 실행 계층 — PostgresGrantPlan을 실제 CREATE ROLE/GRANT로 집행한다.

소유: 영주 (CEO/HR, Platform/IAM 담당자 미정 상태에서 최초 구현)
근거: departments/07-agent-workforce/lifecycle/postgres_access_repository.py와
      동일한 lazy-import·DATABASE_URL 게이트 패턴(CLAUDE.md: 호출부가 동기라
      psycopg2로 통일).

이 파일은 provisioning.py가 세운 계획(PostgresGrantPlan)을 그대로 실행만 한다 -
"이 요청에 뭘 해야 하는가"는 여기서 다시 판단하지 않는다. 판단과 실행을
분리해야 판단 쪽을 DB 없이 테스트할 수 있다(provisioning.py 참고).

## Role 이름·GRANT 대상을 psycopg2 placeholder로 못 바인딩하는 이유

CREATE ROLE/GRANT의 Role 이름과 대상 테이블은 SQL identifier이지 값이 아니다.
psycopg2의 %s는 값만 안전하게 바인딩하고 identifier는 못 넣는다 - 그래서
psycopg2.sql.Identifier로 조립한다. 방어가 두 겹이다: (1) provisioning.py의
_role_name()이 agent_id를 UUID 형식으로 먼저 검증하고 (2) 여기서도
sql.Identifier가 결과를 항상 올바르게 인용부호로 감싼다 - 둘 중 하나만
있어도 되지만 둘 다 둔다.

## CREATE ROLE IF NOT EXISTS가 없는 이유

PostgreSQL 문법에 그런 구문이 없다(CREATE TABLE과 다르다). 존재 여부를
pg_roles에서 먼저 조회하고 없을 때만 만든다 - 멱등성을 이렇게 확보한다.

## NOLOGIN이어야 하는 이유 (2026-08-10 정정 - 이전 판은 LOGIN PASSWORD였다)

이 저장소의 모든 부서 API는 Postgres에 **공유 DATABASE_URL 하나**로만
접속한다(postgres_access_repository.py 등 grep 결과 - 부서별로도 Agent별로도
별도 접속 계정이 없다). Agent가 이 Role로 직접 로그인하는 경로 자체가
시스템 어디에도 없다 - tool_gateway.py가 이미 HTTP 계층(X-Agent-Persona +
config.yaml)에서 권한을 판정하고, 실제 쿼리는 그 뒤에서 공유 연결이 수행한다.

이전 판은 `WITH LOGIN PASSWORD`로 만들어 "누가 로그인할 계정"을 흉내 냈는데,
아무도 그 계정으로 접속하지 않으니 비밀번호를 저장할 이유도 없었다 -
문제는 "비밀번호를 어디에 저장하나"가 아니라 애초에 로그인 가능한 Role이
필요 없었다는 것이다. 이 Role의 실제 역할은 "이 Agent가 이 자원에 접근
가능하다"는 GRANT 기록이지 접속 계정이 아니다 - NOLOGIN으로 만들면 그
사실이 스키마로도 드러난다.

향후 요청 단위 최소권한(공유 연결이 쿼리마다 `SET ROLE agent_x`로 권한을
좁히는 방식)을 붙이려면 이 Role에 로그인 계정을 GRANT 멤버십으로 묶으면
되고, 그때도 여전히 비밀번호는 필요 없다.

자체 점검: python platform_iam/postgres_role_manager.py
  - DATABASE_URL 없으면 import만 확인한다.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

try:
    from provisioning import PostgresGrantPlan
except ModuleNotFoundError:  # direct/standalone execution
    from platform_iam.provisioning import PostgresGrantPlan

# GRANT verb는 여기서도 다시 검증한다 - provisioning.py의 RESOURCE_REF_GRANTS가
# 이미 사람이 채운 값이지만, "여기 도달한 값은 항상 신뢰할 수 있는 값"이라고
# 가정하지 않는다(방어는 각 계층에서 각자 한다).
_ALLOWED_GRANT_VERBS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})


class RoleManagerError(RuntimeError):
    """Role 생성/GRANT 실패. provisioning_ref를 발급하지 못했다는 뜻이다."""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        import psycopg2
        from psycopg2 import sql
    except ModuleNotFoundError as exc:
        raise RoleManagerError(
            "PostgreSQL Role 관리에는 psycopg2-binary가 필요합니다."
        ) from exc
    return psycopg2, sql


def _role_exists(cur: Any, role_name: str) -> bool:
    cur.execute("select 1 from pg_roles where rolname = %s", (role_name,))
    return cur.fetchone() is not None


def apply_grant_plan(plan: PostgresGrantPlan, *, dsn: str) -> str:
    """계획대로 Role을 만들고(없으면) GRANT한다. 이미 있으면 GRANT만 재적용(멱등).

    NOLOGIN이다 - 아무도 이 Role로 직접 접속하지 않는다(위 모듈 docstring).
    비밀번호가 없으니 저장할 곳도 필요 없다.

    반환값은 plan.provisioning_ref 그대로다 - 호출부(service.py)가 이 반환값을
    그대로 HR의 /provision 엔드포인트에 넘긴다.
    """

    if plan.grant_verb not in _ALLOWED_GRANT_VERBS:
        raise RoleManagerError(
            f"허용되지 않은 GRANT verb: {plan.grant_verb!r} "
            f"(허용: {sorted(_ALLOWED_GRANT_VERBS)})"
        )

    psycopg2, sql = _load_postgres_driver()
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            if not _role_exists(cur, plan.role_name):
                cur.execute(
                    sql.SQL("create role {role} nologin").format(
                        role=sql.Identifier(plan.role_name)
                    )
                )
            cur.execute(
                sql.SQL("grant {verb} on {target} to {role}").format(
                    verb=sql.SQL(plan.grant_verb),
                    target=sql.Identifier(*plan.grant_target.split(".")),
                    role=sql.Identifier(plan.role_name),
                )
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise RoleManagerError(f"Role/GRANT 적용 실패 ({plan.role_name}): {exc}") from exc
    finally:
        conn.close()
    return plan.provisioning_ref


def revoke_role(role_name: str, *, dsn: str) -> None:
    """Role이 소유한 객체를 넘긴 뒤 완전히 제거한다 - 좀비 Role을 남기지 않는다."""

    psycopg2, sql = _load_postgres_driver()
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            if not _role_exists(cur, role_name):
                return
            # DROP ROLE은 그 Role이 소유하거나 권한을 가진 객체가 남아 있으면
            # 실패한다 - REASSIGN/DROP OWNED로 먼저 정리한다.
            cur.execute(sql.SQL("reassign owned by {role} to postgres").format(
                role=sql.Identifier(role_name)
            ))
            cur.execute(sql.SQL("drop owned by {role}").format(role=sql.Identifier(role_name)))
            cur.execute(sql.SQL("drop role {role}").format(role=sql.Identifier(role_name)))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise RoleManagerError(f"Role 회수 실패 ({role_name}): {exc}") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 자체 점검 (python platform_iam/postgres_role_manager.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    print("ok - import 확인 (psycopg2 lazy load)")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    # 허용 안 된 verb는 DB 연결 없이도 거부돼야 한다.
    bad_plan = PostgresGrantPlan(
        role_name="agent_selfcheck_shadow", grant_verb="DROP",
        grant_target="workspace.market_data", provisioning_ref="x",
    )
    try:
        apply_grant_plan(bad_plan, dsn=dsn)
        raise AssertionError("허용 안 된 verb(DROP)가 통과했다")
    except RoleManagerError:
        pass
    print("ok - 허용 안 된 GRANT verb 거부 확인")

    # 실제 왕복: 임시 스키마·테이블 하나 만들고 그 위에 Role 왕복 검증 후 정리.
    role_name = "agent_selfcheck_iam_shadow"
    plan = PostgresGrantPlan(
        role_name=role_name, grant_verb="SELECT",
        grant_target="pg_catalog.pg_roles", provisioning_ref=f"postgres-role:{role_name}",
    )
    ref = apply_grant_plan(plan, dsn=dsn)
    assert ref == plan.provisioning_ref
    print(f"ok - CREATE ROLE(NOLOGIN) + GRANT 왕복 완료 ({role_name})")

    # 멱등성 - 두 번째 호출도 에러 없이 통과해야 한다.
    apply_grant_plan(plan, dsn=dsn)
    print("ok - 재적용(멱등) 통과")

    revoke_role(role_name, dsn=dsn)
    print("ok - Role 회수 완료 (정리)")
