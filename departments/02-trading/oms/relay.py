#!/usr/bin/env python3
"""Outbox Relay. `execution.outbox`의 PENDING을 Redis Stream으로 발행하고 SENT로 찍는다.

소유: 도현 (트레이딩본부)
근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md (Override v2.0) P0-2
        "Transactional Outbox와 Relay, consumer idempotency를 PLAT-03 계약에 맞춰 구현한다"
      docs/02-engineering/adr/0002-per-department-redis-instances.md 4절(부서 간 Fan-out)
      departments/02-trading/oms/outbox.py 상단 주석

**이 프로세스가 없으면 회계 원장이 영원히 비어 있다.** 회계 소비자
(`departments/05-accounting-portfolio/ledger/consumer.py`)는 `status = 'SENT'` 행만
읽는데, `SENT`를 찍는 것은 `outbox.drain()` 하나이고 그 호출자가 여기다.

**publisher를 가짜로 만들지 않는다.** `outbox.drain()`은 publish 없이 돌기를 거부한다
(발행한 척 금지, 개발 원칙 9). 그 가드를 우회하는 대신 진짜 발행 수단을 준다 - Redis는
루트 docker-compose.yml이 소유한 공통 인프라이고 `REDIS_URL`로 받는다. 없으면 뜨지 않는다.

at-least-once다. 발행 후 커밋 전에 죽으면 같은 봉투가 다시 나가고, 소비자가
`execution.outbox_consumed`로 거른다. 반대(먼저 SENT 찍고 발행)는 유실이라 택하지 않는다.

발행 실패는 여기서 삼키지 않는다 - `outbox.drain()`이 FAILED -> 지수 backoff -> DLQ로
떨어뜨리고 `last_error`에 사유를 남긴다.

실행:      python oms/relay.py --serve
자체 점검: python oms/relay.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import outbox  # noqa: E402
from store_postgres import PostgresOrderStore  # noqa: E402

# 부서가 내보내는 Stream 하나. 소비자별 분기는 Stream을 쪼개는 게 아니라 Consumer
# Group이 한다(ADR-0002 4절) - 지금 구독자는 회계뿐이지만 QA도 같은 Fill을 본다.
STREAM = "trading_events"
MAXLEN = 10_000

# **outbox 기본값 5를 그대로 쓰지 않는다.** 5는 backoff 합이 ~75초라 Redis를 재기동하는
# 것만으로 체결 봉투가 DLQ로 떨어진다. DLQ는 `pending()`이 다시 집지 않으므로 그 체결은
# 사람이 SQL로 꺼낼 때까지 회계에 영원히 도달하지 않는다 - DLQ는 원인을 남기는 자리이지
# 평범한 재기동으로 닿는 자리가 아니다. 12면 backoff 합이 ~66분이다.
MAX_ATTEMPTS = max(int(os.environ.get("RELAY_MAX_ATTEMPTS", "12")), 1)


def _log(message: str) -> None:
    print(f"[relay] {message}", flush=True)


def publisher(client, stream: str = STREAM):
    """봉투 하나를 Stream에 넣는다. 실패는 올린다 - outbox가 FAILED로 받아야 한다."""
    def publish(envelope: dict) -> None:
        client.xadd(stream, {"body": json.dumps(envelope, sort_keys=True)},
                    maxlen=MAXLEN, approximate=True)
    return publish


def poll(store, publish, *, max_attempts: int = MAX_ATTEMPTS) -> outbox.DrainResult:
    """한 배치. 발행과 SENT 표시가 같은 트랜잭션에서 끝난다.

    `pending()`이 `for update skip locked`로 집으므로 이 프로세스를 여러 개 띄워도
    같은 행이 두 번 발행되지 않는다.
    """
    with store.cursor() as cur:
        return outbox.drain(cur, publish, max_attempts=max_attempts)


def serve() -> None:
    dsn = (
        os.environ.get("TRADING_OUTBOX_DATABASE_URL", "").strip()
        or os.environ.get("TRADING_OMS_DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not dsn:
        raise SystemExit("[relay] DATABASE_URL이 없습니다")
    if not redis_url:
        raise SystemExit("[relay] REDIS_URL이 없습니다 - 발행 수단 없이 SENT를 찍지 않는다")

    import redis  # 여기서만 필요하다. 자체 점검은 Redis 없이 돈다

    client = redis.Redis.from_url(redis_url, socket_connect_timeout=5, socket_timeout=5)
    store = PostgresOrderStore.connect(dsn)
    publish = publisher(client)
    idle = max(float(os.environ.get("RELAY_POLL_SECONDS", "1.0")), 0.1)
    _log(f"start stream={STREAM} poll={idle}s")

    while True:
        try:
            result = poll(store, publish)
        except Exception as exc:  # noqa: BLE001
            # DB/Redis 순단으로 컨테이너를 죽이지 않는다. 다음 주기에 같은 행을 다시 집는다.
            _log(f"batch failed: {type(exc).__name__}: {exc}")
            time.sleep(idle)
            continue
        if result.sent or result.failed or result.dlq:
            _log(f"picked={result.picked} sent={result.sent} "
                 f"failed={result.failed} dlq={result.dlq} "
                 f"{'; '.join(result.errors) if result.errors else ''}".rstrip())
        if result.picked == 0:
            time.sleep(idle)


def _self_check() -> None:
    # 1. 봉투가 손대지 않은 채 Stream으로 나간다.
    published: list[dict] = []

    class _Client:
        def xadd(self, stream, fields, **kw):
            assert stream == STREAM, "다른 Stream으로 나갔다"
            assert kw == {"maxlen": MAXLEN, "approximate": True}, "trim 설정이 빠졌다"
            published.append(json.loads(fields["body"]))
            return b"1-0"

    envelope = {"event_id": "e1", "event_type": outbox.FILL_RECORDED,
                "idempotency_key": "fill_paper_B1", "payload_ref": {"artifact_type": "FILL"}}
    publisher(_Client())(envelope)
    assert published == [envelope], "봉투가 그대로 발행되지 않았다"

    # 2. poll이 호출자 커서를 drain에 그대로 넘긴다 - 발행과 SENT가 한 트랜잭션이어야 한다.
    seen: dict = {}

    class _Store:
        @contextmanager
        def cursor(self):
            yield "CUR"

    def _fake_drain(cur, publish, **kw):
        seen["cur"], seen["kw"] = cur, kw
        return outbox.DrainResult(picked=1, sent=1, failed=0, dlq=0, errors=())

    real_drain = outbox.drain
    outbox.drain = _fake_drain
    try:
        result = poll(_Store(), publisher(_Client()))
    finally:
        outbox.drain = real_drain
    assert seen["cur"] == "CUR", "drain이 커서를 못 받았다"
    assert result.sent == 1
    # 재시도 한도가 drain까지 간다. outbox 기본값 5로 돌면 Redis 재기동 한 번에
    # 체결이 DLQ로 떨어지고 회계가 그걸 영영 못 본다.
    assert seen["kw"]["max_attempts"] == MAX_ATTEMPTS >= 12, "재시도 한도가 안 넘어갔다"

    # 3. 발행 수단 없이는 SENT를 찍지 않는다. 이 Relay의 존재 이유가 이 가드다.
    try:
        outbox.drain(None, None)
    except outbox.OutboxError:
        pass
    else:
        raise AssertionError("publish 없이 drain이 돌았다")

    # 4. 발행이 실패하면 예외가 drain까지 올라간다(여기서 삼키면 유실이 된다).
    class _DeadClient:
        def xadd(self, *a, **kw):
            raise ConnectionError("redis down")

    try:
        publisher(_DeadClient())(envelope)
    except ConnectionError:
        pass
    else:
        raise AssertionError("발행 실패를 삼켰다")

    print("ok - Relay 4개 점검 통과")


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve()
    else:
        _self_check()
