#!/usr/bin/env python3
"""F23 CEO 절반: ReportRunRepository의 실제 PostgreSQL(governance.report_runs) 구현.

담당: 영주 (CEO Office)
근거: daily_report.py의 ReportRunRepository 인터페이스, mandate/postgres_repository.py
      패턴(같은 부서, 같은 psycopg2 conventions).

불변식:
  1. governance.report_runs.fund_id는 accounting.funds에 대한 not null FK다 - 없는
     fund_id로 insert하면 FK 위반으로 실패한다. 가짜 Fund 행을 만들어 우회하지 않는다.
  2. source_snapshot_ids는 DB 컬럼이 uuid[]다. daily_report.py의 SnapshotRef.snapshot_id는
     자유 문자열(In-Memory 자체 점검은 "s-portfolio" 같은 값을 쓴다)이라, 실제 DB에 넣으려면
     호출자가 진짜 UUID 문자열을 snapshot_id로 써야 한다 - 여기서 임의로 변환하지 않는다.
  3. as_of는 DB 컬럼이 timestamptz, Row.as_of는 date다. 자정(UTC)으로 채워 저장하고,
     조회 시 다시 .date()로 되돌린다 - 회계일 단위 비교가 시각 성분에 흔들리지 않게 한다.

자체 점검: python departments/00-ceo-office/src/reporting/postgres_report_repository.py
  - DATABASE_URL 없으면 import만 확인한다.
  - 있으면 실제 accounting.funds 행 하나를 찾아 그 fund_id로 insert/find_by_content_hash
    왕복을 검증하고, 검증에 쓴 행은 정리(delete)한다 - 공유 개발 DB를 더럽히지 않는다.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from functools import lru_cache
from typing import Any

from daily_report import ReportRunRepository, ReportRunRow


class ReportRunPersistenceError(RuntimeError):
    """Report Run을 기록하거나 조회하지 못한 경우."""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.extras import register_uuid
        from psycopg2.pool import ThreadedConnectionPool
        # source_snapshot_ids(uuid[])를 Python list[uuid.UUID]로 되돌리려면 array
        # typecaster까지 등록해야 한다 (register_uuid()는 uuid, uuid[] 둘 다 등록한다) -
        # 안 하면 raw 문자열 "{a,b}"로만 돌아온다.
        register_uuid()
    except ModuleNotFoundError as exc:
        raise ReportRunPersistenceError(
            "PostgreSQL Report 저장에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    return ThreadedConnectionPool


class PostgresReportRunRepository(ReportRunRepository):
    """`governance.report_runs` 전용 저장소."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresReportRunRepository:
        ThreadedConnectionPool = _load_postgres_driver()
        # minconn=0 - 유휴 커넥션을 잡지 않는다
        return cls(ThreadedConnectionPool(0, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def find_by_content_hash(self, fund_id: str, content_hash: str) -> ReportRunRow | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select fund_id, report_type, as_of, source_snapshot_ids, template_version,
                           content_hash, status, trace_id, object_path
                    from governance.report_runs
                    where fund_id = %s and content_hash = %s
                    order by created_at desc
                    limit 1
                    """,
                    (fund_id, content_hash),
                )
                row = cur.fetchone()
            conn.commit()
            if row is None:
                return None
            return self._to_row(row)
        finally:
            self._pool.putconn(conn)

    def insert(self, row: ReportRunRow) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into governance.report_runs (
                        fund_id, report_type, as_of, source_snapshot_ids, template_version,
                        content_hash, status, trace_id, object_path
                    ) values (
                        %s, %s, %s, %s::uuid[], %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        row.fund_id,
                        row.report_type,
                        _to_timestamptz(row.as_of),
                        list(row.source_snapshot_ids),
                        row.template_version,
                        row.content_hash,
                        row.status,
                        row.trace_id,
                        row.object_path,
                    ),
                )
            conn.commit()
        except Exception as exc:  # psycopg2 예외를 API 경계에서 통일한다.
            conn.rollback()
            raise ReportRunPersistenceError(f"Report Run 기록 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_row(db_row: tuple) -> ReportRunRow:
        (fund_id, report_type, as_of, source_snapshot_ids, template_version,
         content_hash, status, trace_id, object_path) = db_row
        return ReportRunRow(
            fund_id=str(fund_id),
            report_type=report_type,
            as_of=as_of.date() if isinstance(as_of, datetime) else as_of,
            source_snapshot_ids=tuple(str(s) for s in (source_snapshot_ids or ())),
            template_version=template_version,
            content_hash=content_hash,
            status=status,
            trace_id=str(trace_id),
            object_path=object_path,
        )


def _to_timestamptz(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/reporting/postgres_report_repository.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import uuid

    print("ok - import 확인 (psycopg2 lazy load)")

    from dotenv import load_dotenv

    load_dotenv()  # 저장소 루트 .env - 이미 설정된 값은 덮어쓰지 않는다.

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    repo = PostgresReportRunRepository.connect(dsn)
    try:
        # 실제 accounting.funds 행 하나를 찾는다 (FK 요구 - 불변식 1).
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("select fund_id from accounting.funds limit 1")
                fund_row = cur.fetchone()
        finally:
            repo._pool.putconn(conn)

        if fund_row is None:
            print("SKIP - accounting.funds에 행이 없어 왕복 검증을 건너뛴다")
            raise SystemExit(0)

        fund_id = str(fund_row[0])

        # 1) 존재하지 않는 content_hash - None.
        missing_hash = str(uuid.uuid4())
        assert repo.find_by_content_hash(fund_id, missing_hash) is None
        print("ok - 존재하지 않는 content_hash 조회 (실 DB) 통과")

        # 2) 실제 왕복 - insert 후 find_by_content_hash로 재현.
        content_hash = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        snapshot_ids = (str(uuid.uuid4()), str(uuid.uuid4()))
        row = ReportRunRow(
            fund_id=fund_id, report_type="DAILY", as_of=date(2026, 8, 3),
            source_snapshot_ids=snapshot_ids, template_version="v1-selfcheck",
            content_hash=content_hash, status="QUEUED", trace_id=trace_id,
        )
        repo.insert(row)
        found = repo.find_by_content_hash(fund_id, content_hash)
        assert found is not None
        assert found.fund_id == fund_id
        assert found.as_of == date(2026, 8, 3)
        assert set(found.source_snapshot_ids) == set(snapshot_ids)
        assert found.status == "QUEUED"
        assert found.trace_id == trace_id
        print("ok - insert -> find_by_content_hash 왕복 (실 DB) 통과")

        # 정리 - 공유 개발 DB에 자체 점검 흔적을 남기지 않는다.
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "delete from governance.report_runs where content_hash = %s", (content_hash,)
                )
            conn.commit()
        finally:
            repo._pool.putconn(conn)
        print("ok - 자체 점검 행 정리 완료")
    finally:
        repo.close()
