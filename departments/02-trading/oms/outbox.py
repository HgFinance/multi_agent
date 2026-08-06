#!/usr/bin/env python3
"""Transactional Outbox와 Relay. **부서 밖으로 나가는 유일한 경로다.**

소유: 도현 (트레이딩본부)
근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md (Override v2.0) P0-2
        "Transactional Outbox와 Relay, consumer idempotency를 PLAT-03 계약에 맞춰 구현한다"
      docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md 6.1
        "Event Bus는 at-least-once를 전제로 하며 Outbox 또는 동등한 재시도 가능한 발행,
         DLQ, 원인 코드를 사용한다"
      supabase/migrations/20260806000100_execution_outbox.sql

**`enqueue()`가 커서를 인자로 받는 것이 이 파일의 요지다.** 새 연결을 열 방법이
시그니처에 없다 - 상태 변경과 다른 트랜잭션에서 기록하면 "Transactional" Outbox가
이름뿐이 된다. 상태가 롤백되면 outbox 행도 같이 사라져야 하고, 그 불변식은
`store_postgres.py` 자체 점검이 직접 검사한다.

**기본 publisher를 만들지 않는다.** Redis 배선이 아직 없는데 `SENT`로 찍으면 발행한
척이 된다. `Relay.drain()`은 `publish`를 안 주면 `OutboxError`로 거부한다(개발 원칙 9).

전송 실패는 세 단계로 떨어진다:

  PENDING -> (실패) -> FAILED + 지수 backoff -> ... -> max_attempts 초과 -> DLQ

DLQ는 조용히 버리는 자리가 아니라 **원인 코드를 남기는 자리**다. `last_error`에
사유가 남고 `attempts`가 몇 번 시도했는지 말한다.

자체 점검: python departments/02-trading/oms/outbox.py
  DATABASE_URL 이 있으면 실 DB 왕복까지 본다. **전부 트랜잭션 안에서 하고 롤백한다.**
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import UUID

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent / "contracts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from contracts import EventEnvelope  # noqa: E402

# 트레이딩이 내는 이벤트 타입. 새 타입을 늘리면 여기 먼저 적는다 - 소비자가 무엇을
# 구독할 수 있는지가 코드 한 곳에 모여 있어야 한다.
ORDER_STATE_CHANGED = "execution.order_state_changed.v1"
FILL_RECORDED = "execution.fill.v1"
PRODUCER = "trading-oms"

# backoff. 실패마다 2배씩 늘리되 상한을 둔다.
_BASE_BACKOFF = timedelta(seconds=5)
_MAX_BACKOFF = timedelta(minutes=15)


class OutboxError(RuntimeError):
    """Outbox를 쓰거나 발행하지 못한 경우. 발행한 척하지 않는다."""


Publisher = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class OutboxRow:
    outbox_id: int
    envelope: dict[str, Any]     # event-envelope-v1 canonical dict
    status: str
    attempts: int


@dataclass(frozen=True)
class DrainResult:
    picked: int
    sent: int
    failed: int
    dlq: int
    errors: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return self.failed == 0 and self.dlq == 0


# ── 쓰기 — 반드시 호출자 트랜잭션 안에서 ──────────────────────────────────
def enqueue(cur, envelope: EventEnvelope) -> int:
    """상태 변경과 **같은 커서**로 outbox 행을 넣는다.

    커서를 인자로 받는 것이 계약이다. 여기서 연결을 새로 열면 상태와 이벤트가 서로
    다른 트랜잭션에 들어가고, 상태만 커밋되거나 이벤트만 커밋되는 창이 생긴다.

    같은 `idempotency_key`가 이미 있으면 조용히 넘어간다(멱등) - 재시도로 같은 명령이
    두 번 들어와도 이벤트가 두 번 나가지 않는다.
    """
    body = envelope.to_canonical()
    cur.execute(
        """
        insert into execution.outbox
            (event_id, event_type, schema_version, case_id, trace_id, producer,
             occurred_at, idempotency_key, payload_ref)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (idempotency_key) do nothing
        returning outbox_id
        """,
        (body["event_id"], body["event_type"], body["schema_version"], body["case_id"],
         body["trace_id"], body["producer"], body["occurred_at"], body["idempotency_key"],
         _json(body["payload_ref"])),
    )
    row = cur.fetchone()
    if row is not None:
        return int(row[0])
    # 이미 있던 행. 같은 사실이므로 그 id를 돌려준다.
    cur.execute("select outbox_id from execution.outbox where idempotency_key = %s",
                (body["idempotency_key"],))
    existing = cur.fetchone()
    if existing is None:  # pragma: no cover - conflict 후 사라질 수 없다
        raise OutboxError(f"outbox 행을 넣지도 찾지도 못했습니다: {body['idempotency_key']}")
    return int(existing[0])


def _json(value: Any):
    if value is None:
        return None
    from psycopg2.extras import Json

    return Json(value)


# ── 읽기·발행 ──────────────────────────────────────────────────────────────
# 지수 상한. 이 값을 넘으면 어차피 _MAX_BACKOFF 로 잘리므로 더 곱할 이유가 없다.
# 자르지 않으면 attempts 가 커질 때 2**n 이 C int 를 넘겨 timedelta 곱셈이
# OverflowError 를 낸다 - 상한이 있는데 그 상한을 계산하다 터지면 의미가 없다.
_MAX_BACKOFF_EXPONENT = (_MAX_BACKOFF // _BASE_BACKOFF).bit_length()


def _backoff(attempts: int) -> timedelta:
    exponent = min(max(attempts - 1, 0), _MAX_BACKOFF_EXPONENT)
    return min(_BASE_BACKOFF * (2 ** exponent), _MAX_BACKOFF)


def _row_to_envelope(row) -> dict[str, Any]:
    (_oid, event_id, event_type, schema_version, case_id, trace_id, producer,
     occurred_at, idem, payload_ref, _status, _attempts) = row
    return {"event_id": str(event_id), "event_type": event_type,
            "schema_version": schema_version,
            "case_id": str(case_id) if case_id else None,
            "trace_id": str(trace_id), "producer": producer,
            "occurred_at": occurred_at.isoformat(), "idempotency_key": idem,
            "payload_ref": payload_ref}


def pending(cur, *, limit: int = 100, now: datetime | None = None) -> list[OutboxRow]:
    """발행 대기 행. **`for update skip locked`** 로 잠근다 - Relay를 여러 개 띄워도
    같은 행을 두 번 집지 않는다."""
    cur.execute(
        """
        select outbox_id, event_id, event_type, schema_version, case_id, trace_id,
               producer, occurred_at, idempotency_key, payload_ref, status, attempts
          from execution.outbox
         where status in ('PENDING', 'FAILED') and available_at <= %s
         order by outbox_id
         limit %s
           for update skip locked
        """,
        (now or datetime.now(timezone.utc), limit),
    )
    return [OutboxRow(outbox_id=int(r[0]), envelope=_row_to_envelope(r),
                      status=r[10], attempts=int(r[11])) for r in cur.fetchall()]


def mark_sent(cur, outbox_id: int, *, now: datetime | None = None) -> None:
    cur.execute(
        "update execution.outbox set status = 'SENT', sent_at = %s, last_error = null "
        " where outbox_id = %s",
        (now or datetime.now(timezone.utc), outbox_id),
    )


def mark_failed(cur, outbox_id: int, *, attempts: int, error: str,
                max_attempts: int, now: datetime | None = None) -> str:
    """실패를 기록한다. 한도를 넘기면 DLQ로 보내고 **원인을 남긴다.**"""
    now = now or datetime.now(timezone.utc)
    attempts += 1
    if attempts >= max_attempts:
        cur.execute(
            "update execution.outbox set status = 'DLQ', attempts = %s, last_error = %s "
            " where outbox_id = %s",
            (attempts, error[:2000], outbox_id))
        return "DLQ"
    cur.execute(
        "update execution.outbox set status = 'FAILED', attempts = %s, last_error = %s, "
        "       available_at = %s where outbox_id = %s",
        (attempts, error[:2000], now + _backoff(attempts), outbox_id))
    return "FAILED"


def drain(cur, publish: Publisher | None = None, *, batch: int = 100,
          max_attempts: int = 5, now: datetime | None = None) -> DrainResult:
    """대기 행을 발행한다. **publish 없이는 돌지 않는다.**

    at-least-once다. 발행 후 커밋 전에 죽으면 같은 이벤트를 다시 보낸다 - 그래서
    소비자가 `already_processed()`로 중복을 거른다. 반대(먼저 SENT 찍고 발행)는
    유실이 되므로 택하지 않는다.
    """
    if publish is None:
        raise OutboxError(
            "publish 가 없습니다. 전송 수단 없이 SENT 로 표시하면 발행한 척이 됩니다 "
            "— Relay 를 돌리려면 실제 publisher 를 주입하십시오")
    rows = pending(cur, limit=batch, now=now)
    sent = failed = dlq = 0
    errors: list[str] = []
    for row in rows:
        try:
            publish(row.envelope)
        except Exception as exc:  # noqa: BLE001 - 발행 경계는 fail-closed
            reason = f"{type(exc).__name__}: {exc}"
            errors.append(reason)
            if mark_failed(cur, row.outbox_id, attempts=row.attempts, error=reason,
                           max_attempts=max_attempts, now=now) == "DLQ":
                dlq += 1
            else:
                failed += 1
            continue
        mark_sent(cur, row.outbox_id, now=now)
        sent += 1
    return DrainResult(picked=len(rows), sent=sent, failed=failed, dlq=dlq,
                       errors=tuple(errors[:5]))


# ── 소비자 중복 제거 ───────────────────────────────────────────────────────
def already_processed(cur, consumer: str, event_id: UUID | str) -> bool:
    cur.execute(
        "select 1 from execution.outbox_consumed where consumer = %s and event_id = %s",
        (consumer, str(event_id)))
    return cur.fetchone() is not None


def mark_processed(cur, consumer: str, event_id: UUID | str) -> bool:
    """소비 기록. 이미 있으면 False - 소비자가 이걸 보고 건너뛴다."""
    cur.execute(
        "insert into execution.outbox_consumed (consumer, event_id) values (%s, %s) "
        "on conflict do nothing returning 1",
        (consumer, str(event_id)))
    return cur.fetchone() is not None


def unconsumed(cur, consumer: str, event_type: str, *, limit: int = 500) -> list[dict[str, Any]]:
    """소비자가 아직 처리하지 않은 이벤트. **폴링이 아니라 Event 경로다.**

    회계 Ledger Consumer가 `execution.fills` 테이블을 훑는 대신 여기를 읽는다
    (P0-1 "Fill은 실제 Event/Consumer 경로에서 생성한다").
    """
    cur.execute(
        """
        select o.outbox_id, o.event_id, o.event_type, o.schema_version, o.case_id,
               o.trace_id, o.producer, o.occurred_at, o.idempotency_key, o.payload_ref,
               o.status, o.attempts
          from execution.outbox o
          left join execution.outbox_consumed c
                 on c.event_id = o.event_id and c.consumer = %s
         where o.event_type = %s and c.event_id is null and o.status <> 'DLQ'
         order by o.outbox_id
         limit %s
        """,
        (consumer, event_type, limit))
    return [_row_to_envelope(r) for r in cur.fetchall()]


def envelope_for(*, event_type: str, trace_id: str, idempotency_key: str,
                 occurred_at: datetime, case_id: UUID | None = None,
                 artifact_type: str | None = None, artifact_id: UUID | None = None,
                 artifact_schema: str | None = None,
                 content_hash: str | None = None) -> EventEnvelope:
    """트레이딩 이벤트 봉투 생성기. producer 를 매번 손으로 적지 않게 한다."""
    return EventEnvelope(
        event_type=event_type, event_time=occurred_at, trace_id=trace_id,
        idempotency_key=idempotency_key, case_id=case_id, producer=PRODUCER,
        artifact_type=artifact_type, artifact_id=artifact_id,
        artifact_schema=artifact_schema, content_hash=content_hash)


if __name__ == "__main__":
    from uuid import uuid4

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    now = datetime(2026, 8, 6, 6, 0, tzinfo=timezone.utc)

    def raises(fn, why, exc=OutboxError):
        try:
            fn()
        except exc:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    # 1. 봉투 생성기가 정본 필드를 채운다
    env = envelope_for(event_type=ORDER_STATE_CHANGED, trace_id="trace-abc",
                       idempotency_key="ob_test_0001", occurred_at=now, case_id=uuid4())
    body = env.to_canonical()
    assert body["producer"] == PRODUCER and body["schema_version"] == "event-envelope-v1"
    assert body["event_type"] == ORDER_STATE_CHANGED
    assert body["occurred_at"] == now.isoformat()
    print("  봉투 생성기                OK")

    # 2. **publish 없이는 안 돈다** - 발행한 척 금지
    raises(lambda: drain(None, None), "publisher 없는 drain")
    print("  publisher 없으면 거부      OK")

    # 3. backoff 는 늘되 상한이 있다
    assert _backoff(1) == _BASE_BACKOFF and _backoff(2) == _BASE_BACKOFF * 2
    assert _backoff(99) == _MAX_BACKOFF, "backoff 상한이 없다"
    print("  지수 backoff + 상한        OK")

    # 4. 실 DB 왕복. **전부 트랜잭션 안에서 하고 롤백한다**
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("  실 DB 왕복                 skip - DATABASE_URL 없음")
        print("ok - Outbox 3개 영역 점검 통과 (DB 검사는 DATABASE_URL 필요)")
        raise SystemExit(0)

    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            trace = uuid4()

            # 4-1. enqueue -> PENDING
            e1 = envelope_for(event_type=ORDER_STATE_CHANGED, trace_id=str(trace),
                              idempotency_key=f"ob_{uuid4().hex[:16]}", occurred_at=now)
            oid = enqueue(cur, e1)
            assert oid > 0
            rows = pending(cur, now=now)
            assert any(r.outbox_id == oid for r in rows), "PENDING 에 안 잡힌다"

            # 4-2. **멱등** - 같은 idempotency_key 로 두 번 넣어도 행이 하나다
            assert enqueue(cur, e1) == oid, "같은 사실이 두 번 들어갔다"
            cur.execute("select count(*) from execution.outbox where idempotency_key = %s",
                        (e1.idempotency_key,))
            assert cur.fetchone()[0] == 1

            # 4-3. 발행 성공 -> SENT (sent_at 제약이 상태와 함께 강제된다)
            published: list[dict] = []
            result = drain(cur, published.append, now=now)
            assert result.sent >= 1 and result.clean, result
            assert any(p["event_id"] == str(e1.event_id) for p in published)
            cur.execute("select status, sent_at from execution.outbox where outbox_id = %s",
                        (oid,))
            status, sent_at = cur.fetchone()
            assert status == "SENT" and sent_at is not None
            assert pending(cur, now=now) == [] or all(
                r.outbox_id != oid for r in pending(cur, now=now)), "SENT 를 또 집는다"

            # 4-4. 발행 실패 -> FAILED + backoff. 재시도 시각 전에는 안 집는다
            e2 = envelope_for(event_type=FILL_RECORDED, trace_id=str(trace),
                              idempotency_key=f"ob_{uuid4().hex[:16]}", occurred_at=now)
            oid2 = enqueue(cur, e2)

            def boom(_body):
                raise RuntimeError("broker down")

            failed = drain(cur, boom, now=now)
            assert failed.failed >= 1 and failed.sent == 0, failed
            assert failed.errors and "broker down" in failed.errors[0]
            cur.execute("select status, attempts, last_error, available_at "
                        "  from execution.outbox where outbox_id = %s", (oid2,))
            st, attempts, err, avail = cur.fetchone()
            assert st == "FAILED" and attempts == 1 and "broker down" in err
            assert avail > now, "backoff 없이 즉시 재시도한다"
            assert all(r.outbox_id != oid2 for r in pending(cur, now=now)), "backoff 무시"
            # backoff 가 지나면 다시 집힌다
            assert any(r.outbox_id == oid2
                       for r in pending(cur, now=now + timedelta(hours=1)))

            # 4-5. **max_attempts 초과는 DLQ 로 가고 조용히 사라지지 않는다**
            later = now + timedelta(hours=1)
            for _ in range(6):
                drain(cur, boom, max_attempts=3, now=later)
                later += timedelta(hours=1)
            cur.execute("select status, attempts, last_error from execution.outbox "
                        " where outbox_id = %s", (oid2,))
            st, attempts, err = cur.fetchone()
            assert st == "DLQ", f"한도를 넘겼는데 {st} 다"
            assert attempts >= 3 and err, "DLQ 인데 원인이 없다"
            assert all(r.outbox_id != oid2 for r in pending(cur, now=later)), "DLQ 를 또 집는다"

            # 4-6. 소비자 중복 제거 - 같은 event 를 두 번 처리하지 않는다
            assert already_processed(cur, "accounting-ledger", e1.event_id) is False
            assert mark_processed(cur, "accounting-ledger", e1.event_id) is True
            assert mark_processed(cur, "accounting-ledger", e1.event_id) is False, "두 번 처리됐다"
            assert already_processed(cur, "accounting-ledger", e1.event_id) is True
            # **소비자별로** 기록된다 - 회계가 처리했다고 QA 가 건너뛰지 않는다
            assert already_processed(cur, "qa-audit", e1.event_id) is False
            print("  DB 왕복: 멱등/발행/재시도/DLQ/중복제거 OK")

            # 4-7. unconsumed - 폴링이 아니라 Event 경로
            e3 = envelope_for(event_type=FILL_RECORDED, trace_id=str(trace),
                              idempotency_key=f"ob_{uuid4().hex[:16]}", occurred_at=now)
            enqueue(cur, e3)
            todo = unconsumed(cur, "accounting-ledger", FILL_RECORDED)
            ids = {t["event_id"] for t in todo}
            assert str(e3.event_id) in ids, "미처리 이벤트가 안 잡힌다"
            assert str(e2.event_id) not in ids, "DLQ 이벤트가 소비 대상에 있다"
            mark_processed(cur, "accounting-ledger", e3.event_id)
            assert str(e3.event_id) not in {
                t["event_id"] for t in unconsumed(cur, "accounting-ledger", FILL_RECORDED)}
            print("  미처리 이벤트 조회         OK")
    finally:
        conn.rollback()   # 자체 점검은 아무것도 남기지 않는다
        conn.close()

    print("ok - Outbox 5개 영역 점검 통과 (전부 롤백, 남긴 행 없음)")
