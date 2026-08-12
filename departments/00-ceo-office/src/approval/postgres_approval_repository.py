#!/usr/bin/env python3
"""GOV-02 1단계: approval.py의 ApprovalRepository 계약에 대한 실제 PostgreSQL 구현.

소유: 영주 (CEO Office)
근거: supabase/migrations/20260729000200_governance_workforce.sql (governance.approvals),
      docs/02-engineering/GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 2.2절

approval.py의 도메인 규칙(권한 분리, 만료 거절, 자동 승인 금지)은 여기서 재구현하지 않는다 -
이 모듈은 SQL 왕복만 담당한다. 규칙은 호출부(api/app.py)가 approval.py 함수로 먼저 적용한다.

불변식:
  1. `save`는 `insert ... on conflict (approval_id) do update`다. DDL의
     `unique (object_type, object_id, required_role)`는 DB가 직접 막게 두고 여기서 미리
     조회해 우회하지 않는다 - 경합 상황에서 애플리케이션 사전 검사는 신뢰할 수 없다.
  2. `actor_user_id`는 governance.user_profiles FK다. 사람 승인이 아니면 None으로 넣는다 -
     플레이스홀더 회원으로 채우면 감사 기록이 거짓이 된다(approval.py decide() 주석 참고).

2026-08-03 실측으로 확인한 제약(설계 문서에는 없다):
  - `actor_agent_id`는 uuid이며 **workforce.agent_profiles FK**다. 같은 마이그레이션 파일
    359행의 `alter table ... add constraint approvals_actor_agent_fk`로 뒤늦게 붙어 있어
    create table 블록만 읽으면 놓친다. 즉 이 칸을 채우려면 Agent가 Roster에 등재돼 있어야 한다.
  - workforce.agent_profiles에는 HR 5명·QA 8명·Risk 6명만 있고 CEO Agent는 없다. 그리고
    Agent Roster 등재는 전체 Prototype 이후로 미루기로 했다(2026-08-04 팀 결정) - 그때까지
    hermes/config.yaml이 Agent 정의의 기준이다.
  - 따라서 이 칸은 **대부분의 결정에서 NULL로 남는다**(nullable이라 DB는 허용한다). 결정
    주체 부서는 approval.py decide()가 conditions._decider에 기록한다 - approvals에 부서
    컬럼이 아예 없어서 그마저 없으면 감사 추적이 통째로 사라진다.
    아래 자체 점검은 두 경로를 다 검증한다: 등재된 Agent를 빌려 FK가 실제로 작동하는 경우와,
    actor_agent_id 없이 부서만 남기는 경우(지금의 기본 경로).

자체 점검: python departments/00-ceo-office/src/approval/postgres_approval_repository.py
  - DATABASE_URL(또는 GOVERNANCE_WORKFORCE_DATABASE_URL) 없으면 import만 확인한다.
  - 있으면 실제 accounting.funds 행을 찾아 승인 요청 -> 조회 -> 결정 -> 재조회 왕복을
    검증한 뒤 삽입한 행을 정리(delete)한다. governance.approvals에는 append-only 트리거가
    없어 삭제가 가능하다(workforce.improvement_candidate_events와 다른 점).
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from approval import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRepository,
    ObjectType,
    RequiredRole,
)


class ApprovalPersistenceError(RuntimeError):
    """승인 저장/조회에 실패한 경우."""


_SELECT = """
    select approval_id, fund_id, object_type, object_id, required_role, decision,
           created_at, reason, expires_at, decided_at, actor_user_id, actor_agent_id, conditions
    from governance.approvals
"""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.extras import register_uuid
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise ApprovalPersistenceError(
            "PostgreSQL 승인 저장에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    register_uuid()
    return ThreadedConnectionPool


class PostgresApprovalRepository(ApprovalRepository):
    """`governance.approvals`에 대한 실제 구현."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresApprovalRepository:
        ThreadedConnectionPool = _load_postgres_driver()
        return cls(ThreadedConnectionPool(1, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def save(self, approval: ApprovalRecord) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into governance.approvals
                      (approval_id, fund_id, object_type, object_id, required_role, decision,
                       actor_user_id, actor_agent_id, conditions, reason, expires_at,
                       decided_at, created_at)
                    values (%(approval_id)s, %(fund_id)s, %(object_type)s, %(object_id)s,
                            %(required_role)s, %(decision)s, %(actor_user_id)s, %(actor_agent_id)s,
                            %(conditions)s::jsonb, %(reason)s, %(expires_at)s, %(decided_at)s,
                            %(created_at)s)
                    on conflict (approval_id) do update set
                      decision = excluded.decision,
                      actor_user_id = excluded.actor_user_id,
                      actor_agent_id = excluded.actor_agent_id,
                      conditions = excluded.conditions,
                      reason = excluded.reason,
                      expires_at = excluded.expires_at,
                      decided_at = excluded.decided_at
                    """,
                    {
                        "approval_id": approval.approval_id,
                        "fund_id": approval.fund_id,
                        "object_type": approval.object_type.value,
                        "object_id": approval.object_id,
                        "required_role": approval.required_role.value,
                        "decision": approval.decision.value,
                        "actor_user_id": approval.actor_user_id,
                        "actor_agent_id": approval.actor_agent_id,
                        "conditions": json.dumps(approval.conditions or {}),
                        "reason": approval.reason,
                        "expires_at": approval.expires_at,
                        "decided_at": approval.decided_at,
                        "created_at": approval.created_at,
                    },
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def get(self, approval_id: str) -> ApprovalRecord | None:
        return self._fetch_one(_SELECT + " where approval_id = %s", (approval_id,))

    def find(
        self, object_type: ObjectType, object_id: str, required_role: RequiredRole
    ) -> ApprovalRecord | None:
        return self._fetch_one(
            _SELECT + " where object_type = %s and object_id = %s and required_role = %s",
            (object_type.value, object_id, required_role.value),
        )

    def list_by_object(self, object_type: ObjectType, object_id: str) -> list[ApprovalRecord]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    _SELECT + " where object_type = %s and object_id = %s order by created_at",
                    (object_type.value, object_id),
                )
                rows = cur.fetchall()
            conn.commit()
            return [self._to_record(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    def _fetch_one(self, query: str, params: tuple) -> ApprovalRecord | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone()
            conn.commit()
            return None if row is None else self._to_record(row)
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_record(db_row: tuple) -> ApprovalRecord:
        (approval_id, fund_id, object_type, object_id, required_role, decision,
         created_at, reason, expires_at, decided_at, actor_user_id, actor_agent_id,
         conditions) = db_row
        return ApprovalRecord(
            approval_id=str(approval_id), fund_id=str(fund_id),
            object_type=ObjectType(object_type), object_id=str(object_id),
            required_role=RequiredRole(required_role), decision=ApprovalDecision(decision),
            created_at=created_at, reason=reason, expires_at=expires_at, decided_at=decided_at,
            actor_user_id=str(actor_user_id) if actor_user_id else None,
            actor_agent_id=str(actor_agent_id) if actor_agent_id else None,
            conditions=conditions or {},
        )


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/approval/postgres_approval_repository.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import uuid
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone

    from approval import decide, request_approval

    print("ok - import 확인 (psycopg2 lazy load)")

    from dotenv import load_dotenv

    load_dotenv()  # 저장소 루트 .env - 이미 설정된 값은 덮어쓰지 않는다.

    dsn = os.environ.get("GOVERNANCE_WORKFORCE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    repo = PostgresApprovalRepository.connect(dsn)
    approval_id = str(uuid.uuid4())
    try:
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("select fund_id from accounting.funds limit 1")
                fund_row = cur.fetchone()
                # actor_agent_id는 workforce.agent_profiles FK다(위 docstring 참고) -
                # CEO Agent 등록 전이라 이미 등록된 Agent 하나를 빌려 SQL 왕복만 검증한다.
                cur.execute(
                    "select agent_id, employee_code from workforce.agent_profiles "
                    "order by employee_code limit 1"
                )
                agent_row = cur.fetchone()
        finally:
            repo._pool.putconn(conn)

        if fund_row is None or agent_row is None:
            print("SKIP - accounting.funds 또는 workforce.agent_profiles가 비어 있어 건너뛴다")
            raise SystemExit(0)
        fund_id = str(fund_row[0])
        decider_agent_id, decider_code = str(agent_row[0]), agent_row[1]

        t0 = datetime(2026, 8, 3, tzinfo=timezone.utc)
        object_id = str(uuid.uuid4())  # 실제 Profile Version이 아니어도 된다 - FK가 없는 칸이다

        # 1) 요청 저장 -> PENDING 조회.
        pending = request_approval(
            approval_id=approval_id, fund_id=fund_id,
            object_type=ObjectType.AGENT_PROFILE_VERSION, object_id=object_id,
            required_role=RequiredRole.CEO, created_at=t0,
            reason="자체 점검", expires_at=t0 + timedelta(days=1),
        )
        repo.save(pending)
        loaded = repo.get(approval_id)
        assert loaded is not None and loaded.decision is ApprovalDecision.PENDING
        assert loaded.conditions == {}
        print("ok - 승인 요청 저장/조회 (실 DB) 통과")

        # 2) unique(object_type, object_id, required_role) 기준 조회.
        found = repo.find(ObjectType.AGENT_PROFILE_VERSION, object_id, RequiredRole.CEO)
        assert found is not None and found.approval_id == approval_id
        assert repo.find(ObjectType.AGENT_PROFILE_VERSION, object_id, RequiredRole.RISK) is None
        print("ok - find (실 DB) 통과")

        # 3) CEO Office가 결정 -> APPROVED 영속화. actor_user_id는 None 유지(불변식 2).
        approved = decide(
            loaded, decision=ApprovalDecision.APPROVED, actor_department="ceo-agent",
            at=t0 + timedelta(hours=2), actor_agent_id=decider_agent_id,
            conditions={"note": "selfcheck"},
        )
        repo.save(approved)
        reloaded = repo.get(approval_id)
        assert reloaded.decision is ApprovalDecision.APPROVED
        assert reloaded.actor_agent_id == decider_agent_id
        assert reloaded.actor_user_id is None
        assert reloaded.conditions["note"] == "selfcheck"
        assert reloaded.conditions["_decider"] == {"department": "CEO-AGENT"}
        assert reloaded.decided_at is not None
        print(f"ok - 결정 기록 (실 DB) 통과 - actor_user_id는 None 유지, "
              f"actor_agent_id는 등재된 {decider_code} 차용, _decider 부서 기록 확인")

        # 3b) 미등록 Agent uuid로는 결정을 기록할 수 없다 - approvals_actor_agent_fk 확인.
        try:
            repo.save(replace(reloaded, actor_agent_id=str(uuid.uuid4())))
            raise AssertionError("미등록 Agent로 결정이 기록됐다 - FK가 막지 못했다")
        except AssertionError:
            raise
        except Exception:
            pass
        print("ok - 미등재 Agent 차단(approvals_actor_agent_fk) 확인")

        # 3c) Roster 미등재가 기본인 지금의 실제 경로 - actor_agent_id 없이 부서만 남긴다.
        #     이게 CEO 승인의 기본 경로이므로 실 DB에서 반드시 동작해야 한다.
        repo.save(replace(reloaded, actor_agent_id=None))
        no_agent = repo.get(approval_id)
        assert no_agent.actor_agent_id is None and no_agent.actor_user_id is None
        assert no_agent.conditions["_decider"] == {"department": "CEO-AGENT"}
        print("ok - Agent 미등재 경로(actor_agent_id=NULL + _decider 부서) 실 DB 저장 확인")

        # 4) DB의 unique 제약이 실제로 막는지 - 같은 대상·역할에 다른 approval_id (불변식 1).
        try:
            repo.save(request_approval(
                approval_id=str(uuid.uuid4()), fund_id=fund_id,
                object_type=ObjectType.AGENT_PROFILE_VERSION, object_id=object_id,
                required_role=RequiredRole.CEO, created_at=t0,
            ))
            raise AssertionError("DB unique 제약이 막지 못했다")
        except AssertionError:
            raise
        except Exception:
            pass  # psycopg2 UniqueViolation - DB가 막았다
        print("ok - DB unique(object_type, object_id, required_role) 제약 확인")

        # 5) list_by_object.
        assert len(repo.list_by_object(ObjectType.AGENT_PROFILE_VERSION, object_id)) == 1
        print("ok - list_by_object (실 DB) 통과")
    finally:
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "delete from governance.approvals where approval_id = %s", (approval_id,)
                )
            conn.commit()
        finally:
            repo._pool.putconn(conn)
        repo.close()
        print("ok - 자체 점검 행 정리 완료")
