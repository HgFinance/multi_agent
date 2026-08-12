#!/usr/bin/env python3
"""GOV-02 에스컬레이션 — escalation.py의 EscalationRepository 실제 PostgreSQL 구현.

소유: 영주 (CEO Office)
근거: supabase/migrations/20260729000200_governance_workforce.sql(governance.escalations)

escalation.py의 상태 머신은 여기서 재구현하지 않는다 - 이 모듈은 SQL 왕복만 담당한다.

불변식:
  1. `case_id`는 NOT NULL FK다 - 존재하지 않는 Case로 만들면 DB가 거절한다. 애플리케이션에서
     미리 조회해 우회하지 않고 DB가 막게 둔다(경합 상황에서 사전 검사는 신뢰할 수 없다).
  2. `save`는 `on conflict (escalation_id) do update`다. severity는 갱신 대상에서 제외한다 -
     escalation.py 불변식 4(CRITICAL을 조용히 낮추지 않는다)를 저장 계층에서도 지킨다.

자체 점검: python departments/00-ceo-office/src/escalation/postgres_escalation_repository.py
  - DATABASE_URL(또는 GOVERNANCE_WORKFORCE_DATABASE_URL) 없으면 import만 확인한다.
  - 있으면 기존 Case 한 건을 찾아 에스컬레이션 생성 -> 전이 -> 조회 왕복을 검증한 뒤
    정리(delete)한다. escalations에는 append-only 트리거가 없어 삭제가 가능하다
    (case_events와 다른 점).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from escalation import (
    EscalationRecord,
    EscalationRepository,
    EscalationStatus,
    Severity,
)


class EscalationPersistenceError(RuntimeError):
    """에스컬레이션 저장/조회에 실패한 경우."""


_SELECT = """
    select escalation_id, case_id, reason, severity, target, status, due_at,
           resolution, created_at, resolved_at
    from governance.escalations
"""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.extras import register_uuid
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise EscalationPersistenceError(
            "PostgreSQL 에스컬레이션 저장에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    register_uuid()
    return ThreadedConnectionPool


class PostgresEscalationRepository(EscalationRepository):
    """`governance.escalations`에 대한 실제 구현."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresEscalationRepository:
        ThreadedConnectionPool = _load_postgres_driver()
        # minconn=0 - 유휴 커넥션을 잡지 않는다
        return cls(ThreadedConnectionPool(0, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def save(self, escalation: EscalationRecord) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into governance.escalations
                      (escalation_id, case_id, reason, severity, target, status, due_at,
                       resolution, created_at, resolved_at)
                    values (%(escalation_id)s, %(case_id)s, %(reason)s, %(severity)s,
                            %(target)s, %(status)s, %(due_at)s, %(resolution)s,
                            %(created_at)s, %(resolved_at)s)
                    on conflict (escalation_id) do update set
                      status = excluded.status,
                      resolution = excluded.resolution,
                      resolved_at = excluded.resolved_at,
                      due_at = excluded.due_at
                    """,
                    {
                        "escalation_id": escalation.escalation_id,
                        "case_id": escalation.case_id,
                        "reason": escalation.reason,
                        "severity": escalation.severity.value,
                        "target": escalation.target,
                        "status": escalation.status.value,
                        "due_at": escalation.due_at,
                        "resolution": escalation.resolution,
                        "created_at": escalation.created_at,
                        "resolved_at": escalation.resolved_at,
                    },
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def get(self, escalation_id: str) -> EscalationRecord | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(_SELECT + " where escalation_id = %s", (escalation_id,))
                row = cur.fetchone()
            conn.commit()
            return None if row is None else self._to_record(row)
        finally:
            self._pool.putconn(conn)

    def list_by_case(self, case_id: str) -> list[EscalationRecord]:
        return self._list(_SELECT + " where case_id = %s order by created_at", (case_id,))

    def list_open(self, *, target: str | None = None) -> list[EscalationRecord]:
        if target is None:
            return self._list(
                _SELECT + " where status in ('OPEN', 'ACKNOWLEDGED') order by created_at", ()
            )
        return self._list(
            _SELECT + " where status in ('OPEN', 'ACKNOWLEDGED') and target = %s "
            "order by created_at",
            (target,),
        )

    def _list(self, query: str, params: tuple) -> list[EscalationRecord]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
            conn.commit()
            return [self._to_record(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_record(db_row: tuple) -> EscalationRecord:
        (escalation_id, case_id, reason, severity, target, status, due_at,
         resolution, created_at, resolved_at) = db_row
        return EscalationRecord(
            escalation_id=str(escalation_id), case_id=str(case_id), reason=reason,
            severity=Severity(severity), target=target, status=EscalationStatus(status),
            due_at=due_at, resolution=resolution, created_at=created_at,
            resolved_at=resolved_at,
        )


# ---------------------------------------------------------------------------
# 자체 점검 (python .../src/escalation/postgres_escalation_repository.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import uuid
    from datetime import datetime, timedelta, timezone

    from escalation import open_escalation, transition

    print("ok - import 확인 (psycopg2 lazy load)")

    from dotenv import load_dotenv

    load_dotenv()  # 저장소 루트 .env - 이미 설정된 값은 덮어쓰지 않는다.

    dsn = os.environ.get("GOVERNANCE_WORKFORCE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    repo = PostgresEscalationRepository.connect(dsn)
    escalation_id = str(uuid.uuid4())
    try:
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("select case_id, display_id from governance.cases limit 1")
                case_row = cur.fetchone()
        finally:
            repo._pool.putconn(conn)

        if case_row is None:
            print("SKIP - governance.cases가 비어 있어 건너뛴다 (case_id는 NOT NULL FK)")
            raise SystemExit(0)
        case_id, display_id = str(case_row[0]), case_row[1]

        t0 = datetime(2026, 8, 4, tzinfo=timezone.utc)

        # 1) 생성 -> 조회.
        esc = open_escalation(
            escalation_id=escalation_id, case_id=case_id,
            reason="자체 점검 - Risk 한도 초과 미해결", severity=Severity.HIGH,
            target="risk-management", created_at=t0, due_at=t0 + timedelta(days=1),
        )
        repo.save(esc)
        loaded = repo.get(escalation_id)
        assert loaded is not None and loaded.status is EscalationStatus.OPEN
        assert loaded.severity is Severity.HIGH and loaded.case_id == case_id
        print(f"ok - 에스컬레이션 생성/조회 (실 DB) 통과 (case={display_id})")

        # 2) 불변식 1 - 존재하지 않는 Case로는 만들 수 없다 (DB FK).
        try:
            repo.save(open_escalation(
                escalation_id=str(uuid.uuid4()),
                case_id="00000000-0000-4000-8000-000000000000",
                reason="없는 Case", severity=Severity.LOW, target="x", created_at=t0,
            ))
            raise AssertionError("존재하지 않는 case_id가 통과했다")
        except AssertionError:
            raise
        except Exception:
            pass
        print("ok - 존재하지 않는 case_id 차단(escalations_case_id_fkey) 확인")

        # 3) 전이 - ACKNOWLEDGED -> RESOLVED(resolution 필수) 영속화.
        ack = transition(loaded, to_status=EscalationStatus.ACKNOWLEDGED, at=t0 + timedelta(hours=1))
        repo.save(ack)
        assert repo.get(escalation_id).status is EscalationStatus.ACKNOWLEDGED
        resolved = transition(
            repo.get(escalation_id), to_status=EscalationStatus.RESOLVED,
            at=t0 + timedelta(hours=2), resolution="자체 점검 - 한도 재적용으로 해소",
        )
        repo.save(resolved)
        reloaded = repo.get(escalation_id)
        assert reloaded.status is EscalationStatus.RESOLVED
        assert reloaded.resolution == "자체 점검 - 한도 재적용으로 해소"
        assert reloaded.resolved_at is not None
        print("ok - 전이 영속화 (실 DB) 통과 - resolution/resolved_at 확인")

        # 4) 조회 경로 - Case별, 미해결(RESOLVED는 제외돼야 한다), target 필터.
        assert any(e.escalation_id == escalation_id for e in repo.list_by_case(case_id))
        open_ids = {e.escalation_id for e in repo.list_open()}
        assert escalation_id not in open_ids, "RESOLVED가 미해결 목록에 남았다"
        print("ok - list_by_case / list_open (실 DB) 통과")

        # 5) 불변식 2 - severity는 갱신되지 않는다.
        from dataclasses import replace as _replace

        repo.save(_replace(reloaded, severity=Severity.LOW))
        assert repo.get(escalation_id).severity is Severity.HIGH, "severity가 조용히 낮아졌다"
        print("ok - severity 무기록 하향 차단 확인 (불변식 2)")
    finally:
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "delete from governance.escalations where escalation_id = %s",
                    (escalation_id,),
                )
            conn.commit()
        finally:
            repo._pool.putconn(conn)
        repo.close()
        print("ok - 자체 점검 행 정리 완료")
