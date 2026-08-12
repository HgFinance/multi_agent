#!/usr/bin/env python3
"""GOV-02 Case Root — case_root.py의 CaseRepository 계약에 대한 실제 PostgreSQL 구현.

소유: 영주 (CEO Office)
근거: supabase/migrations/20260729000200_governance_workforce.sql(governance.cases/case_events),
      supabase/migrations/20260804000200_governance_case_status.sql(status 제약),
      supabase/migrations/20260729000500_audit_api_security.sql 608행(api.get_case_timeline RPC)

case_root.py의 상태 머신은 여기서 재구현하지 않는다 - 이 모듈은 SQL 왕복만 담당한다.

불변식:
  1. **Projection 갱신과 event append는 한 트랜잭션이다** (case_root.py 불변식 1). status만
     바뀌고 event가 없거나 그 반대인 중간 상태를 남기지 않는다.
  2. `next_sequence`/`next_display_sequence`는 조회 후 별도 insert하는 구조라 경합에
     완전하지 않다. 대신 DDL의 `unique(case_id, sequence)`와 `unique(display_id)`가 최종
     방어선이며, 충돌하면 예외가 호출자에게 그대로 올라간다(조용히 덮어쓰지 않는다).
  3. timeline은 `api.get_case_timeline` RPC가 아니라 case_events를 직접 조회한다 - RPC는
     `security definer`로 authenticated 역할에 부여된 사용자 조회용이고, 이 서비스는
     Service Role로 붙어 같은 데이터를 직접 읽을 수 있다. RPC 응답 형태에 의존하면
     그쪽 변경에 결합되므로 Domain 타입으로 직접 매핑한다.

자체 점검: python departments/00-ceo-office/src/case/postgres_case_repository.py
  - DATABASE_URL(또는 GOVERNANCE_WORKFORCE_DATABASE_URL) 없으면 import만 확인한다.
  - 있으면 Case 생성 -> 전이 -> timeline 왕복을 검증한다.

  **정리하지 못한다(2026-08-04 실측)**: governance.case_events에 `case_events_append_only`
  트리거가 있어 DELETE가 거부되고, cases -> case_events가 on delete cascade라 부모 Case도
  지울 수 없다. workforce.improvement_candidate_events와 같은 상황이다.
  그래서 자체 점검은 `created_by='selfcheck'` Case가 이미 있으면 **새로 만들지 않고 읽기
  경로만 검증**한다 - 공유 개발 DB에 행이 실행마다 누적되지 않게 하려는 것이다. 즉 이
  자체 점검이 남기는 흔적은 최초 1회의 Case 1건(+event 3건)뿐이다.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from case_root import (
    CaseEvent,
    CaseRecord,
    CaseRepository,
    CaseStatus,
    display_prefix,
)


class CasePersistenceError(RuntimeError):
    """Case 저장/조회에 실패한 경우."""


_CASE_SELECT = """
    select case_id, fund_id, display_id, case_type, priority, status, owner_department,
           due_at, trace_id, schema_version, created_by, created_at, updated_at
    from governance.cases
"""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.extras import register_uuid
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise CasePersistenceError(
            "PostgreSQL Case 저장에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    register_uuid()
    return ThreadedConnectionPool


class PostgresCaseRepository(CaseRepository):
    """`governance.cases` / `governance.case_events`에 대한 실제 구현."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresCaseRepository:
        ThreadedConnectionPool = _load_postgres_driver()
        # minconn=0 - 유휴 커넥션을 잡지 않는다
        return cls(ThreadedConnectionPool(0, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def save_new(self, case: CaseRecord, event: CaseEvent) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into governance.cases
                      (case_id, fund_id, display_id, case_type, priority, status,
                       owner_department, due_at, trace_id, schema_version, created_by,
                       created_at, updated_at)
                    values (%(case_id)s, %(fund_id)s, %(display_id)s, %(case_type)s,
                            %(priority)s, %(status)s, %(owner_department)s, %(due_at)s,
                            %(trace_id)s, %(schema_version)s, %(created_by)s,
                            %(created_at)s, %(updated_at)s)
                    """,
                    self._case_params(case),
                )
                self._insert_event(cur, event)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def apply_transition(self, case: CaseRecord, event: CaseEvent) -> None:
        """불변식 1 - status 갱신과 event append를 한 트랜잭션으로 처리한다."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "update governance.cases set status = %s, updated_at = %s where case_id = %s",
                    (case.status.value, case.updated_at, case.case_id),
                )
                if cur.rowcount != 1:
                    raise CasePersistenceError(
                        f"case_id={case.case_id} 갱신 대상이 없다 (rowcount={cur.rowcount})"
                    )
                self._insert_event(cur, event)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _insert_event(cur: Any, event: CaseEvent) -> None:
        cur.execute(
            """
            insert into governance.case_events
              (case_id, sequence, event_type, from_status, to_status, schema_version,
               producer, actor, reason, idempotency_key, payload, occurred_at)
            values (%(case_id)s, %(sequence)s, %(event_type)s, %(from_status)s, %(to_status)s,
                    %(schema_version)s, %(producer)s, %(actor)s, %(reason)s,
                    %(idempotency_key)s, %(payload)s::jsonb, %(occurred_at)s)
            """,
            {
                "case_id": event.case_id, "sequence": event.sequence,
                "event_type": event.event_type,
                "from_status": event.from_status.value if event.from_status else None,
                "to_status": event.to_status.value, "schema_version": event.schema_version,
                "producer": event.producer, "actor": event.actor, "reason": event.reason,
                "idempotency_key": event.idempotency_key,
                "payload": json.dumps(event.payload or {}),
                "occurred_at": event.occurred_at,
            },
        )

    @staticmethod
    def _case_params(case: CaseRecord) -> dict:
        return {
            "case_id": case.case_id, "fund_id": case.fund_id, "display_id": case.display_id,
            "case_type": case.case_type, "priority": case.priority, "status": case.status.value,
            "owner_department": case.owner_department, "due_at": case.due_at,
            "trace_id": case.trace_id, "schema_version": case.schema_version,
            "created_by": case.created_by, "created_at": case.created_at,
            "updated_at": case.updated_at,
        }

    def get(self, case_id: str) -> CaseRecord | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(_CASE_SELECT + " where case_id = %s", (case_id,))
                row = cur.fetchone()
            conn.commit()
            return None if row is None else self._to_case(row)
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_case(db_row: tuple) -> CaseRecord:
        (case_id, fund_id, display_id, case_type, priority, status, owner_department,
         due_at, trace_id, schema_version, created_by, created_at, updated_at) = db_row
        return CaseRecord(
            case_id=str(case_id), fund_id=str(fund_id), display_id=display_id,
            case_type=case_type, priority=priority, status=CaseStatus(status),
            owner_department=owner_department, due_at=due_at, trace_id=str(trace_id),
            schema_version=schema_version, created_by=created_by,
            created_at=created_at, updated_at=updated_at,
        )

    def timeline(self, case_id: str) -> list[CaseEvent]:
        """불변식 3 - case_events를 직접 조회한다(RPC 응답 형태에 결합하지 않는다)."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select case_id, sequence, event_type, from_status, to_status,
                           schema_version, producer, actor, reason, idempotency_key,
                           payload, occurred_at
                    from governance.case_events
                    where case_id = %s order by sequence
                    """,
                    (case_id,),
                )
                rows = cur.fetchall()
            conn.commit()
            return [self._to_event(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_event(db_row: tuple) -> CaseEvent:
        (case_id, sequence, event_type, from_status, to_status, schema_version,
         producer, actor, reason, idempotency_key, payload, occurred_at) = db_row
        return CaseEvent(
            case_id=str(case_id), sequence=sequence, event_type=event_type,
            from_status=CaseStatus(from_status) if from_status else None,
            to_status=CaseStatus(to_status), schema_version=schema_version,
            producer=producer, actor=actor, reason=reason,
            idempotency_key=idempotency_key, payload=payload or {}, occurred_at=occurred_at,
        )

    def next_sequence(self, case_id: str) -> int:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select coalesce(max(sequence), 0) + 1 from governance.case_events "
                    "where case_id = %s",
                    (case_id,),
                )
                value = cur.fetchone()[0]
            conn.commit()
            return int(value)
        finally:
            self._pool.putconn(conn)

    def next_display_sequence(self, case_type: str, at) -> int:
        """같은 (접두어, 날짜)의 display_id 개수 + 1. 불변식 2의 경합 한계가 적용된다."""
        pattern = f"{display_prefix(case_type)}-{at:%Y%m%d}-%"
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select count(*) from governance.cases where display_id like %s", (pattern,)
                )
                value = cur.fetchone()[0]
            conn.commit()
            return int(value) + 1
        finally:
            self._pool.putconn(conn)


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/case/postgres_case_repository.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import uuid
    from datetime import datetime, timedelta, timezone

    from case_root import IllegalCaseTransition, build_display_id, open_case, transition

    print("ok - import 확인 (psycopg2 lazy load)")

    from dotenv import load_dotenv

    load_dotenv()  # 저장소 루트 .env - 이미 설정된 값은 덮어쓰지 않는다.

    dsn = os.environ.get("GOVERNANCE_WORKFORCE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    repo = PostgresCaseRepository.connect(dsn)
    case_id = str(uuid.uuid4())
    try:
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("select fund_id from accounting.funds limit 1")
                fund_row = cur.fetchone()
                # 이미 자체 점검 흔적이 있으면 새로 만들지 않는다(append-only라 정리 불가).
                cur.execute(
                    "select case_id from governance.cases where created_by = 'selfcheck' "
                    "order by created_at limit 1"
                )
                existing_row = cur.fetchone()
        finally:
            repo._pool.putconn(conn)
        if fund_row is None:
            print("SKIP - accounting.funds가 비어 있어 건너뛴다")
            raise SystemExit(0)
        fund_id = str(fund_row[0])

        t0 = datetime(2026, 8, 4, tzinfo=timezone.utc)

        if existing_row is not None:
            # 읽기 경로만 검증한다 - 쓰기 경로는 최초 실행에서 이미 검증됐고, append-only
            # 트리거 때문에 정리가 안 되므로 실행마다 새 Case를 만들지 않는다.
            prior_id = str(existing_row[0])
            prior = repo.get(prior_id)
            assert prior is not None and prior.status in tuple(CaseStatus)
            tl = repo.timeline(prior_id)
            assert [e.sequence for e in tl] == list(range(1, len(tl) + 1)), "sequence 불연속"
            assert tl[0].from_status is None and tl[0].to_status is CaseStatus.OPEN
            assert repo.next_sequence(prior_id) == len(tl) + 1
            assert repo.next_display_sequence(prior.case_type, prior.created_at) >= 1
            print(f"ok - 기존 자체 점검 Case({prior.display_id}, {prior.status.value})로 "
                  f"읽기 경로 검증 통과 - event {len(tl)}건 sequence 연속 확인")
            print("SKIP - 쓰기 경로는 건너뛴다 (case_events append-only라 정리 불가, "
                  "공유 DB에 행 누적 방지)")
            raise SystemExit(0)

        # 1) 생성 - Case + 첫 event 한 트랜잭션 (불변식 1).
        seq = repo.next_display_sequence("HIRING", t0)
        case, ev = open_case(
            case_id=case_id, fund_id=fund_id,
            display_id=build_display_id("HIRING", created_at=t0, sequence=seq),
            case_type="HIRING", priority=2, owner_department="hr-department",
            trace_id=str(uuid.uuid4()), created_by="selfcheck", created_at=t0,
            idempotency_key=f"selfcheck-open-{case_id}", reason="자체 점검",
        )
        repo.save_new(case, ev)
        loaded = repo.get(case_id)
        assert loaded is not None and loaded.status is CaseStatus.OPEN
        assert loaded.display_id.startswith("HR-20260804-")
        assert len(repo.timeline(case_id)) == 1
        print(f"ok - Case 생성 (실 DB) 통과 (display_id={loaded.display_id})")

        # 2) 전이 - status 갱신 + event append 한 트랜잭션.
        assert repo.next_sequence(case_id) == 2
        ack, ev2 = transition(
            loaded, to_status=CaseStatus.ACKNOWLEDGED, actor="hr-department",
            at=t0 + timedelta(hours=1), next_sequence=repo.next_sequence(case_id),
            idempotency_key=f"selfcheck-ack-{case_id}",
        )
        repo.apply_transition(ack, ev2)
        assert repo.get(case_id).status is CaseStatus.ACKNOWLEDGED
        tl = repo.timeline(case_id)
        assert [e.sequence for e in tl] == [1, 2]
        assert [e.to_status for e in tl] == [CaseStatus.OPEN, CaseStatus.ACKNOWLEDGED]
        assert tl[1].from_status is CaseStatus.OPEN
        print("ok - 전이 + timeline (실 DB) 통과 - sequence 1,2 연속 확인")

        # 3) idempotency_key 중복은 DB가 막는다 (불변식 4).
        _, dup = transition(
            repo.get(case_id), to_status=CaseStatus.RESOLVED, actor="hr", at=t0,
            next_sequence=3, idempotency_key=f"selfcheck-ack-{case_id}",
        )
        try:
            repo.apply_transition(repo.get(case_id), dup)
            raise AssertionError("idempotency_key 중복이 통과했다")
        except AssertionError:
            raise
        except Exception:
            pass
        # 실패한 전이가 status를 바꾸지 않았는지 확인 (불변식 1의 트랜잭션 보장).
        assert repo.get(case_id).status is CaseStatus.ACKNOWLEDGED
        print("ok - idempotency_key 중복 차단 + 실패 시 status 불변 확인")

        # 4) Terminal 전이 후 더 못 간다 (도메인 규칙, DB 왕복과 함께).
        resolved, ev3 = transition(
            repo.get(case_id), to_status=CaseStatus.RESOLVED, actor="hr-department",
            at=t0 + timedelta(hours=2), next_sequence=repo.next_sequence(case_id),
            idempotency_key=f"selfcheck-resolve-{case_id}",
        )
        repo.apply_transition(resolved, ev3)
        assert repo.get(case_id).status is CaseStatus.RESOLVED
        try:
            transition(repo.get(case_id), to_status=CaseStatus.CANCELLED, actor="x", at=t0,
                       next_sequence=4, idempotency_key=f"selfcheck-x-{case_id}")
            raise AssertionError("Terminal에서 전이가 통과했다")
        except IllegalCaseTransition:
            pass
        print("ok - Terminal 이후 전이 차단 확인")

        # 5) display_id 연번이 실제로 증가한다.
        assert repo.next_display_sequence("HIRING", t0) == seq + 1
        print("ok - display_id 연번 증가 확인")
    finally:
        repo.close()
        # 정리하지 않는다 - case_events_append_only 트리거가 DELETE를 거부하고 cases는
        # cascade로 묶여 있어 부모도 못 지운다(위 docstring 참고). 다음 실행은 이 흔적을
        # 감지해 쓰기 경로를 건너뛰므로 누적되지 않는다.
        print("note - 자체 점검 Case 1건은 남는다 (case_events append-only, 정리 불가)")
