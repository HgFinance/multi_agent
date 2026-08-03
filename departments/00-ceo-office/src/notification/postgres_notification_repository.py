#!/usr/bin/env python3
"""F24: NotificationRepository의 실제 PostgreSQL(governance.notifications) 구현.

담당: 영주 (CEO Office)
근거: notification.py의 NotificationRepository 인터페이스, mandate/postgres_repository.py·
      reporting/postgres_report_repository.py와 같은 psycopg2 conventions(같은 부서).

불변식:
  1. governance.notifications.fund_id는 accounting.funds에 대한 FK이지만 nullable이다 -
     실제 Fund가 없어도(또는 Fund에 묶이지 않는 알림이어도) None으로 저장할 수 있다.
     domain의 NotificationRow.fund_id는 str(non-optional)이라 여기서 값을 그대로 넘긴다 -
     실제 Fund와 무관한 시스템 알림을 만들 계획이면 호출자가 fund_id를 비워 보내도록
     서비스가 바뀌어야 한다(이 Repository의 범위 밖).
  2. created_at은 호출자(NotificationService)가 cooldown 판정에 쓴 시각을 그대로 저장한다
     (notification.py의 InMemoryNotificationRepository와 동일 계약) - DB now()에 맡기면
     같은 호출 안에서 서비스가 계산한 cooldown 기준과 저장된 시각이 어긋난다.

자체 점검: python departments/00-ceo-office/src/notification/postgres_notification_repository.py
  - DATABASE_URL 없으면 import만 확인한다.
  - 있으면 fund_id=None(불변식 1)으로 실제 insert/recent_by_dedup_key 왕복을 검증하고,
    검증에 쓴 행은 정리(delete)한다.
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any

from notification import (
    Channel,
    NotificationRepository,
    NotificationRow,
    NotificationStatus,
)


class NotificationPersistenceError(RuntimeError):
    """Notification을 기록하거나 조회하지 못한 경우."""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.extras import Json
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise NotificationPersistenceError(
            "PostgreSQL Notification 저장에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    return Json, ThreadedConnectionPool


class PostgresNotificationRepository(NotificationRepository):
    """`governance.notifications` 전용 저장소."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresNotificationRepository:
        _, ThreadedConnectionPool = _load_postgres_driver()
        return cls(ThreadedConnectionPool(1, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def recent_by_dedup_key(self, dedup_key: str, *, since: datetime) -> list[NotificationRow]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select fund_id, event_type, recipient, channel, payload, dedup_key,
                           status, sent_at
                    from governance.notifications
                    where dedup_key = %s and created_at >= %s
                    """,
                    (dedup_key, since),
                )
                rows = cur.fetchall()
            conn.commit()
            return [self._to_row(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    def insert(self, row: NotificationRow, *, created_at: datetime) -> None:
        Json, _ = _load_postgres_driver()
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into governance.notifications (
                        fund_id, event_type, recipient, channel, payload, dedup_key,
                        status, sent_at, created_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        row.fund_id, row.event_type, row.recipient, row.channel.value,
                        Json(row.payload), row.dedup_key, row.status.value, row.sent_at,
                        created_at,
                    ),
                )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise NotificationPersistenceError(f"Notification 기록 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_row(db_row: tuple) -> NotificationRow:
        fund_id, event_type, recipient, channel, payload, dedup_key, status, sent_at = db_row
        return NotificationRow(
            fund_id=str(fund_id) if fund_id is not None else None,
            event_type=event_type,
            recipient=recipient,
            channel=Channel(channel),
            payload=payload,
            dedup_key=dedup_key,
            status=NotificationStatus(status),
            sent_at=sent_at,
        )


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/notification/postgres_notification_repository.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import uuid
    from datetime import timedelta, timezone

    print("ok - import 확인 (psycopg2 lazy load)")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    repo = PostgresNotificationRepository.connect(dsn)
    try:
        t0 = datetime(2026, 8, 3, tzinfo=timezone.utc)
        dedup_key = str(uuid.uuid4())

        # 1) 존재하지 않는 dedup_key - 빈 목록.
        assert repo.recent_by_dedup_key(dedup_key, since=t0 - timedelta(hours=1)) == []
        print("ok - 존재하지 않는 dedup_key 조회 (실 DB) 통과")

        # 2) 실제 왕복 - fund_id=None(불변식 1)으로 insert 후 recent_by_dedup_key로 재현.
        row = NotificationRow(
            fund_id=None, event_type="risk.breach.v1", recipient="user:selfcheck",
            channel=Channel.APP, payload={"reason": "자체 점검"}, dedup_key=dedup_key,
            status=NotificationStatus.PENDING,
        )
        repo.insert(row, created_at=t0)
        found = repo.recent_by_dedup_key(dedup_key, since=t0 - timedelta(minutes=1))
        assert len(found) == 1
        assert found[0].dedup_key == dedup_key
        assert found[0].channel == Channel.APP
        assert found[0].status == NotificationStatus.PENDING
        assert found[0].fund_id is None
        print("ok - insert -> recent_by_dedup_key 왕복 (실 DB, fund_id=None) 통과")

        # 3) since 기준 이전 행은 안 잡힌다.
        assert repo.recent_by_dedup_key(dedup_key, since=t0 + timedelta(seconds=1)) == []
        print("ok - since 기준 필터링 통과")

        # 정리 - 공유 개발 DB에 자체 점검 흔적을 남기지 않는다.
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "delete from governance.notifications where dedup_key = %s", (dedup_key,)
                )
            conn.commit()
        finally:
            repo._pool.putconn(conn)
        print("ok - 자체 점검 행 정리 완료")
    finally:
        repo.close()
