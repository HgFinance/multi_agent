#!/usr/bin/env python3
"""TRD-01: OMS 상태를 Supabase `execution.*`에 둔다.

소유: 도현 (트레이딩본부)
근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.3(v1.2), 8.1, 11(DoD)
      supabase/migrations/20260729000400_execution_risk_accounting.sql

`oms.py`의 상태 머신은 한 줄도 옮겨오지 않았다. 여기는 순수 저장 계층이고
전이 판정·멱등·수량 검증은 그대로 `OMS`가 한다. DB도 같은 불변식을 독립적으로
강제하므로(아래) 이중 방어가 된다.

**DB가 이미 강제하는 것** — 우리 코드가 죽어도 남는 방어선:
  - `validate_order_state_transition` 트리거: `orders.state` 전이표를 강제한다.
    내용이 `contracts.py`의 `BROKER_TRANSITIONS`와 글자 단위로 일치한다.
  - `check (filled_quantity <= requested_quantity)`: 초과 체결 차단
  - `unique nulls not distinct (broker_adapter, broker_event_id)` on order_events:
    같은 브로커 이벤트를 두 번 받아도 두 번 기록되지 않는다(불변식 4)
  - `orders.client_order_id` unique, `order_intents.idempotency_key` unique(불변식 2)

**Fund/Book/Case/Strategy를 만들지 않는다.** 아래 행이 먼저 있어야 한다:
  - `execution.trade_cases` — Case 소유는 governance, strategy_version/signal은 리서치·퀀트
  - `strategy.versions` — `strategy_id`만으로는 버전을 특정할 수 없다(계약 공백, 아래 참고)
없으면 조용히 만들지 않고 예외를 낸다. 남의 본부 표에 우리가 행을 지어내면
그 순간 그 표는 더 이상 그 본부의 진실이 아니다.

**알려진 계약 공백 두 개** (팀 합의 대기, 여기서 임의로 메우지 않는다):
  1. `OrderIntent`는 `strategy_id`를 들고 있는데 `execution.order_intents`는
     `strategy_version_id`(NOT NULL)를 요구한다. 지금은 `strategy.versions`에서
     그 전략의 활성 버전을 찾아 쓰고, 없거나 둘 이상이면 예외다 - 아무 버전이나
     고르면 어느 코드로 낸 주문인지가 사후에 달라진다.
  2. **Intent 단계 이벤트에는 canonical 표가 없다.** `execution.order_events.order_id`가
     NOT NULL이라 Broker Order가 생기기 전의 심사 이벤트를 넣을 자리가 없다.
     지금은 `order_intents.intent_status` Projection만 남는다 - 심사 이력이
     필요하면 스키마 델타로 넘긴다.

자체 점검: python departments/02-trading/oms/store_postgres.py
           (DATABASE_URL 필요. **전부 트랜잭션 안에서 하고 롤백한다** - 선행 행을
            남의 표에 남기지 않으면서 스키마·트리거·제약은 실제로 검증한다)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator
from uuid import NAMESPACE_OID, UUID, uuid5

_DEPT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DEPT / "oms"))
sys.path.insert(0, str(_DEPT / "contracts"))
sys.path.insert(0, str(_DEPT / "multileg"))

from contracts import BrokerOrderState, IntentState, OrderIntent, Side
from intent_group import IntentGroup
from oms import BrokerOrder, Fill, OrderIntentRecord, StateEvent

SCHEMA_VERSION = 1
DEFAULT_CURRENCY = "KRW"


class OrderStorePersistenceError(RuntimeError):
    """주문 상태를 저장·조회하지 못한 경우. 조용히 메모리로 되돌아가지 않는다."""


def _load_driver() -> tuple[Any, Any, Any]:
    try:
        import psycopg2
        from psycopg2.extras import Json, register_uuid
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:  # pragma: no cover - 설치 안내
        raise OrderStorePersistenceError(
            "주문 DB 저장에는 psycopg2-binary가 필요합니다."
        ) from exc
    register_uuid()
    return psycopg2, Json, ThreadedConnectionPool


def _trace_uuid(trace_id: str, fallback: UUID) -> UUID:
    """`trace_id` 컬럼이 uuid인데 도메인은 문자열을 쓴다.

    ponytail: PLAT-01 Event Envelope가 붙으면 진짜 trace uuid가 흘러온다. 그 전까지
              같은 문자열이 항상 같은 uuid가 되도록 uuid5로 접는다(Replay 재현성).
              회계 저장소의 같은 이름 함수와 규칙이 같다 - 두 본부가 같은 trace
              문자열을 받으면 같은 uuid가 나와야 나중에 이어 붙는다.
    """
    try:
        return UUID(trace_id)
    except (ValueError, AttributeError, TypeError):
        return uuid5(NAMESPACE_OID, trace_id) if trace_id else uuid5(NAMESPACE_OID, str(fallback))


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class PostgresOrderStore:
    """`OrderStore`와 같은 인터페이스. 상태는 `execution.*`에 있다.

    `conn`을 주면 그 트랜잭션 안에서만 동작하고 스스로 commit하지 않는다.
    자체 점검이 선행 행까지 만들고 통째로 롤백할 수 있는 이유이며, 실제 운용에서는
    `dsn`으로 만들어 요청마다 commit한다.
    """

    def __init__(self, pool: Any = None, conn: Any = None, adapter: str = "paper") -> None:
        if pool is None and conn is None:
            raise OrderStorePersistenceError("pool 또는 conn 중 하나가 필요합니다")
        self._pool = pool
        self._conn = conn
        self.adapter = adapter

    @classmethod
    def connect(cls, dsn: str, adapter: str = "paper") -> PostgresOrderStore:
        _, _, ThreadedConnectionPool = _load_driver()
        return cls(pool=ThreadedConnectionPool(1, 4, dsn), adapter=adapter)

    @classmethod
    def from_env(cls, adapter: str = "paper") -> PostgresOrderStore | None:
        dsn = os.environ.get("DATABASE_URL")
        return cls.connect(dsn, adapter) if dsn else None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.closeall()

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        psycopg2, _, _ = _load_driver()
        if self._conn is not None:
            # 호출자 트랜잭션. commit하지 않는다 - 커밋 시점은 호출자가 정한다.
            with self._conn.cursor() as cur:
                yield cur
            return
        conn = self._pool.getconn()
        try:
            with conn:
                with conn.cursor() as cur:
                    yield cur
        except psycopg2.Error as exc:
            raise OrderStorePersistenceError(f"주문 DB 작업 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    # -- 선행 행 해석 ---------------------------------------------------------

    def _strategy_version(self, cur, strategy_id: UUID) -> UUID:
        """`strategy_id` -> `strategy_version_id`. **아무 버전이나 고르지 않는다.**

        어느 코드로 낸 주문인지가 사후에 달라지면 Replay와 감사가 무너진다.
        활성 버전이 정확히 하나일 때만 통과한다.
        """
        cur.execute(
            """
            select strategy_version_id from strategy.versions
             where strategy_id = %s and deployment_state in ('PAPER', 'LIVE', 'LIVE_CANDIDATE')
             order by version desc
            """,
            (strategy_id,),
        )
        rows = cur.fetchall()
        if not rows:
            raise OrderStorePersistenceError(
                f"strategy {strategy_id}에 활성 버전이 없습니다. "
                "strategy.versions는 리서치·퀀트본부 소유이며 우리가 만들지 않습니다"
            )
        if len(rows) > 1:
            raise OrderStorePersistenceError(
                f"strategy {strategy_id}에 활성 버전이 {len(rows)}개입니다. "
                "OrderIntent가 strategy_version_id를 직접 실어야 합니다(계약 공백 1)"
            )
        return rows[0][0]

    def _market_snapshot(self, cur, intent: OrderIntent) -> UUID:
        """Intent가 참조한 호가를 `execution.market_snapshots`에 남기고 id를 준다.

        시세를 수집하지 않는다 - Intent에 이미 실려 온 값을 증거로 고정할 뿐이다.
        같은 (종목, 시각, 내용)이면 같은 행이다.
        """
        snapshot = intent.snapshot
        content_hash = _hash({"bid": str(snapshot.bid), "ask": str(snapshot.ask),
                              "ref": snapshot.market_snapshot_id})
        cur.execute(
            """
            insert into execution.market_snapshots
                (instrument_id, as_of, bid, ask, mid, currency, quality_status,
                 source_ref, content_hash)
            values (%s, %s, %s, %s, %s, %s, 'PASS', %s, %s)
            on conflict (instrument_id, as_of, content_hash) do nothing
            returning market_snapshot_id
            """,
            (intent.instrument_id, snapshot.as_of, snapshot.bid, snapshot.ask,
             (snapshot.bid + snapshot.ask) / 2, DEFAULT_CURRENCY,
             snapshot.market_snapshot_id, content_hash),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
        cur.execute(
            "select market_snapshot_id from execution.market_snapshots "
            "where instrument_id = %s and as_of = %s and content_hash = %s",
            (intent.instrument_id, snapshot.as_of, content_hash),
        )
        return cur.fetchone()[0]

    def _intent_group(self, cur, intent: OrderIntent, capability_profile_id: UUID) -> UUID:
        """단일 Leg Intent에도 Group이 필요하다(`order_intents.intent_group_id` NOT NULL).

        멀티레그(F30)를 쓰지 않는 주문이라 leg 하나짜리 그룹을 만든다. 그룹
        idempotency_key를 Intent 것에서 파생시켜 재시도해도 그룹이 늘지 않는다.
        """
        key = f"grp:{intent.idempotency_key}"
        cur.execute(
            """
            insert into execution.intent_groups
                (trade_case_id, fund_id, capability_profile_id, atomicity_policy,
                 failure_policy, group_status, idempotency_key, schema_version, trace_id)
            values (%s, %s, %s, 'BEST_EFFORT', 'PARTIAL_OK', 'DRAFT', %s, %s, %s)
            on conflict (idempotency_key) do nothing
            returning intent_group_id
            """,
            (intent.trade_case_id, intent.fund_id, capability_profile_id, key,
             SCHEMA_VERSION, _trace_uuid(intent.trace_id, intent.order_intent_id)),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
        cur.execute(
            "select intent_group_id from execution.intent_groups where idempotency_key = %s",
            (key,),
        )
        return cur.fetchone()[0]

    def _capability_profile(self, cur, strategy_version_id: UUID) -> UUID:
        cur.execute(
            "select capability_profile_id from strategy.versions where strategy_version_id = %s",
            (strategy_version_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise OrderStorePersistenceError(f"없는 strategy_version_id: {strategy_version_id}")
        return row[0]

    # -- 쓰기 -----------------------------------------------------------------

    def add_intent(self, rec: OrderIntentRecord, intent: OrderIntent) -> None:
        with self.cursor() as cur:
            strategy_version_id = self._strategy_version(cur, intent.strategy_id)
            snapshot_id = self._market_snapshot(cur, intent)
            group_id = self._intent_group(
                cur, intent, self._capability_profile(cur, strategy_version_id))
            cur.execute(
                """
                insert into execution.order_intents
                    (order_intent_id, trade_case_id, intent_group_id, fund_id, book_id,
                     strategy_version_id, instrument_id, side, position_effect, leg_index,
                     order_type, quantity, limit_price, time_in_force, valid_until,
                     market_snapshot_id, intent_status, idempotency_key, schema_version, trace_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', 0, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s)
                on conflict (idempotency_key) do nothing
                """,
                (rec.order_intent_id, intent.trade_case_id, group_id, intent.fund_id,
                 intent.book_id, strategy_version_id, intent.instrument_id,
                 str(intent.side), str(intent.order_type), intent.quantity,
                 intent.limit_price, str(intent.time_in_force), intent.valid_until,
                 snapshot_id, str(rec.state), intent.idempotency_key, SCHEMA_VERSION,
                 _trace_uuid(intent.trace_id, rec.order_intent_id)),
            )

    def add_order(self, order: BrokerOrder) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                insert into execution.orders
                    (order_id, order_intent_id, client_order_id, broker_adapter, state,
                     requested_quantity, filled_quantity, trace_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (client_order_id) do nothing
                """,
                (order.order_id, order.order_intent_id, order.client_order_id,
                 order.broker_adapter, str(order.state), order.requested_quantity,
                 order.filled_quantity,
                 _trace_uuid("", order.order_id)),
            )

    def add_group(self, group: IntentGroup) -> None:
        """멀티레그 Group. 단일 Leg 경로는 `_intent_group`이 이미 만든다.

        ponytail: F30 멀티레그를 DB로 옮기는 것은 별도 작업이다. 지금은 단일 Leg
                  주문만 DB에 있고, Group 등록은 인메모리 경로에서만 쓰인다.
        """
        raise OrderStorePersistenceError(
            "멀티레그 Intent Group의 DB 저장은 아직 구현하지 않았습니다(F30 별도 작업)"
        )

    def link_order_to_leg(self, group_id: UUID, leg_index: int, order_id: UUID) -> None:
        raise OrderStorePersistenceError("멀티레그 Leg 연결은 아직 구현하지 않았습니다(F30)")

    def add_event(self, event: StateEvent) -> None:
        """상태 변화 하나. **Projection 갱신을 같은 트랜잭션에서 한다.**

        `orders.state` UPDATE가 `validate_order_state_transition` 트리거를 깨우므로
        허용되지 않은 전이는 우리 코드를 우회해도 DB에서 거부된다.

        Intent 스트림 이벤트는 넣을 표가 없다(파일 상단 계약 공백 2). Projection인
        `order_intents.intent_status`만 갱신한다 - 이벤트를 버리는 것이 아니라
        canonical 표가 아직 없는 것이고, 그 사실을 여기 적어 둔다.
        """
        _, Json, _ = _load_driver()
        with self.cursor() as cur:
            if event.stream == "intent":
                cur.execute(
                    "update execution.order_intents set intent_status = %s "
                    "where order_intent_id = %s",
                    (event.to_state, event.stream_id),
                )
                return
            if event.stream != "broker_order":
                return

            cur.execute(
                """
                insert into execution.order_events
                    (order_event_id, order_id, event_type, event_time, received_at,
                     broker_adapter, broker_event_id, from_state, to_state, payload,
                     payload_hash, sequence, trace_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (broker_adapter, broker_event_id) do nothing
                """,
                (event.event_id, event.stream_id, event.event_type, event.event_time,
                 event.received_at, event.broker_adapter, event.broker_event_id,
                 event.from_state, event.to_state, Json(event.payload),
                 _hash(event.payload), event.sequence,
                 _trace_uuid("", event.stream_id)),
            )
            cur.execute(
                "update execution.orders set state = %s, last_event_at = %s, "
                "version = version + 1 where order_id = %s and state <> %s",
                (event.to_state, event.event_time, event.stream_id, event.to_state),
            )

    def add_fill(self, order: BrokerOrder, fill: Fill) -> None:
        """체결 사실. 회계본부의 `fill_consumer`가 이 표를 읽는다.

        `filled_quantity` 갱신을 같은 트랜잭션에서 한다 - DB의
        `check (filled_quantity <= requested_quantity)`가 초과 체결을 막는다.
        """
        with self.cursor() as cur:
            cur.execute(
                """
                insert into execution.fills
                    (fill_id, order_id, broker_fill_id, instrument_id, side, quantity,
                     price, gross_amount, fee_amount, tax_amount, currency,
                     event_time, received_at, trace_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (order_id, broker_fill_id) do nothing
                """,
                (fill.fill_id, fill.order_id,
                 fill.broker_fill_id or str(fill.fill_id), order.instrument_id,
                 str(order.side), fill.quantity, fill.price,
                 fill.quantity * fill.price, fill.fee, fill.tax, DEFAULT_CURRENCY,
                 fill.event_time, datetime.now(timezone.utc),
                 _trace_uuid("", order.order_id)),
            )
            cur.execute(
                "update execution.orders set filled_quantity = %s, average_fill_price = %s "
                "where order_id = %s",
                (order.filled_quantity, order.average_fill_price, order.order_id),
            )
        order.fills.append(fill)

    # -- 읽기 -----------------------------------------------------------------

    def get_intent(self, intent_id: UUID) -> OrderIntentRecord | None:
        with self.cursor() as cur:
            cur.execute(
                """
                select order_intent_id, fund_id, idempotency_key, quantity, valid_until,
                       intent_status, risk_decision_id
                  from execution.order_intents where order_intent_id = %s
                """,
                (intent_id,),
            )
            row = cur.fetchone()
        return _to_record(row) if row else None

    def find_intent_by_idempotency(self, key: str) -> OrderIntentRecord | None:
        with self.cursor() as cur:
            cur.execute(
                """
                select order_intent_id, fund_id, idempotency_key, quantity, valid_until,
                       intent_status, risk_decision_id
                  from execution.order_intents where idempotency_key = %s
                """,
                (key,),
            )
            row = cur.fetchone()
        return _to_record(row) if row else None

    def list_intents(self, limit: int | None = None) -> list[OrderIntentRecord]:
        with self.cursor() as cur:
            cur.execute(
                """
                select order_intent_id, fund_id, idempotency_key, quantity, valid_until,
                       intent_status, risk_decision_id
                  from execution.order_intents order by created_at desc limit %s
                """,
                (limit or 200,),
            )
            rows = cur.fetchall()
        return [_to_record(r) for r in rows]

    def get_order(self, order_id: UUID) -> BrokerOrder | None:
        with self.cursor() as cur:
            cur.execute(_ORDER_SELECT + " where o.order_id = %s", (order_id,))
            row = cur.fetchone()
        return _to_order(row) if row else None

    def find_order_by_intent(self, intent_id: UUID) -> BrokerOrder | None:
        with self.cursor() as cur:
            cur.execute(_ORDER_SELECT + " where o.order_intent_id = %s", (intent_id,))
            row = cur.fetchone()
        return _to_order(row) if row else None

    def list_orders(self, limit: int | None = None) -> list[BrokerOrder]:
        with self.cursor() as cur:
            cur.execute(_ORDER_SELECT + " order by o.created_at desc limit %s", (limit or 200,))
            rows = cur.fetchall()
        return [_to_order(r) for r in rows]

    def find_unknown_order(self, fund_id: UUID) -> BrokerOrder | None:
        """전체 스캔이 아니라 조회 한 방이다(인메모리 구현의 ponytail 주석 참고)."""
        with self.cursor() as cur:
            cur.execute(_ORDER_SELECT + " where i.fund_id = %s and o.state = 'UNKNOWN' limit 1",
                        (fund_id,))
            row = cur.fetchone()
        return _to_order(row) if row else None

    def find_group_by_idempotency(self, key: str) -> IntentGroup | None:
        return None  # 멀티레그 미구현(add_group 참고)

    def find_order_by_leg(self, group_id: UUID, leg_index: int) -> BrokerOrder | None:
        return None  # 멀티레그 미구현

    def seen_broker_event(self, adapter: str, broker_event_id: str | None) -> bool:
        if broker_event_id is None:
            return False
        with self.cursor() as cur:
            cur.execute(
                "select 1 from execution.order_events "
                "where broker_adapter = %s and broker_event_id = %s limit 1",
                (adapter, broker_event_id),
            )
            return cur.fetchone() is not None

    def next_sequence(self, stream: str, stream_id: UUID) -> int:
        if stream != "broker_order":
            return 1  # Intent 스트림은 표가 없다(계약 공백 2)
        with self.cursor() as cur:
            cur.execute(
                "select coalesce(max(sequence), 0) + 1 from execution.order_events "
                "where order_id = %s",
                (stream_id,),
            )
            return cur.fetchone()[0]

    def events_for(self, stream: str, stream_id: UUID) -> list[StateEvent]:
        if stream != "broker_order":
            return []
        with self.cursor() as cur:
            cur.execute(
                """
                select order_event_id, event_type, sequence, from_state, to_state,
                       event_time, received_at, broker_adapter, broker_event_id, payload
                  from execution.order_events where order_id = %s order by sequence
                """,
                (stream_id,),
            )
            rows = cur.fetchall()
        return [
            StateEvent(event_id=event_id, stream="broker_order", stream_id=stream_id,
                       event_type=event_type, sequence=sequence, from_state=from_state,
                       to_state=to_state, event_time=event_time, received_at=received_at,
                       broker_adapter=adapter, broker_event_id=broker_event_id, payload=payload)
            for (event_id, event_type, sequence, from_state, to_state, event_time,
                 received_at, adapter, broker_event_id, payload) in rows
        ]


_ORDER_SELECT = """
select o.order_id, o.order_intent_id, i.fund_id, o.client_order_id, o.broker_adapter,
       i.side, i.instrument_id, o.requested_quantity, i.limit_price, o.state,
       o.broker_order_id, o.filled_quantity, o.version
  from execution.orders o
  join execution.order_intents i on i.order_intent_id = o.order_intent_id
"""


def _to_record(row) -> OrderIntentRecord:
    (intent_id, fund_id, key, quantity, valid_until, status, risk_decision_id) = row
    return OrderIntentRecord(
        order_intent_id=intent_id, fund_id=fund_id, idempotency_key=key,
        requested_quantity=quantity, valid_until=valid_until,
        state=IntentState(status), risk_decision_id=risk_decision_id,
    )


def _to_order(row) -> BrokerOrder:
    (order_id, intent_id, fund_id, client_order_id, adapter, side, instrument_id,
     requested, limit_price, state, broker_order_id, filled, version) = row
    return BrokerOrder(
        order_id=order_id, order_intent_id=intent_id, fund_id=fund_id,
        client_order_id=client_order_id, broker_adapter=adapter, side=Side(side),
        instrument_id=instrument_id, requested_quantity=requested, limit_price=limit_price,
        state=BrokerOrderState(state), broker_order_id=broker_order_id,
        filled_quantity=filled, version=version,
    )


if __name__ == "__main__":
    from datetime import timedelta
    from uuid import uuid4

    sys.path.insert(0, str(_DEPT / "broker"))
    from contracts import MarketSnapshot, OrderType, RiskDecision, TimeInForce
    from oms import OMS

    try:
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env")
    except ModuleNotFoundError:
        pass

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("skip - DATABASE_URL이 없다. 실 DB 왕복 검사라 건너뛴다")
        raise SystemExit(0)

    psycopg2, _, _ = _load_driver()
    conn = psycopg2.connect(dsn)
    D = Decimal
    now = datetime.now(timezone.utc)

    # ── 선행 행을 트랜잭션 안에서 만든다. 끝에 전부 롤백한다 ──────────────────
    # strategy.versions / signals는 리서치·퀀트본부 소유라 영속시키지 않는다.
    # 롤백해도 스키마·FK·트리거·CHECK는 실제로 다 걸린다.
    cur = conn.cursor()
    cur.execute("select fund_id, book_id from accounting.books b "
                "join accounting.funds f using (fund_id) where f.fund_code = 'ACC01-PAPER' "
                "and b.book_code = 'MAIN'")
    row = cur.fetchone()
    assert row is not None, "ACC01-PAPER/MAIN 장부가 없다. 회계 fill_consumer.py를 먼저 돌린다"
    fund_id, book_id = row

    cur.execute("select instrument_id from reference.instruments order by instrument_id limit 1")
    instrument_id = cur.fetchone()[0]
    cur.execute("select strategy_id from strategy.strategies limit 1")
    strategy_id = cur.fetchone()[0]
    cur.execute("select capability_profile_id from strategy.capability_profiles limit 1")
    profile_id = cur.fetchone()[0]
    cur.execute("select case_id from governance.cases limit 1")
    case_id = cur.fetchone()[0]

    cur.execute(
        """
        insert into strategy.versions
            (strategy_id, version, capability_profile_id, signal_schema,
             target_portfolio_schema, config, artifact_path, artifact_hash,
             code_version, deployment_state)
        values (%s, 999, %s, '{}', '{}', '{}', 'memory://trd01-check', 'h', 'v0', 'PAPER')
        returning strategy_version_id
        """,
        (strategy_id, profile_id),
    )
    strategy_version_id = cur.fetchone()[0]
    cur.execute(
        """
        insert into strategy.signals
            (case_id, fund_id, strategy_version_id, signal_type, directionality,
             as_of, valid_until, payload, input_hash, schema_version, trace_id)
        values (%s, %s, %s, 'ENTRY', 'LONG', %s, %s, '{}', 'h', 1, %s)
        returning signal_id
        """,
        (case_id, fund_id, strategy_version_id, now, now + timedelta(days=1), uuid4()),
    )
    signal_id = cur.fetchone()[0]
    cur.execute(
        """
        insert into execution.trade_cases
            (trade_case_id, fund_id, book_id, strategy_version_id, strategy_family,
             primary_instrument_id, signal_id, case_status, thesis, invalidation,
             expires_at, created_by, trace_id)
        values (%s, %s, %s, %s, 'momentum', %s, %s, 'OPEN', '{}', '{}', %s, 'svc_selfcheck', %s)
        """,
        (case_id, fund_id, book_id, strategy_version_id, instrument_id, signal_id,
         now + timedelta(days=1), uuid4()),
    )

    store = PostgresOrderStore(conn=conn, adapter="paper")
    oms = OMS(store=store, adapter="paper")

    def make_intent(key: str, qty: str = "100") -> OrderIntent:
        return OrderIntent(
            trade_case_id=case_id, fund_id=fund_id, book_id=book_id,
            strategy_id=strategy_id, instrument_id=instrument_id,
            side=Side.BUY, order_type=OrderType.LIMIT, quantity=D(qty),
            limit_price=D("70000"), time_in_force=TimeInForce.DAY,
            valid_until=now + timedelta(hours=6),
            snapshot=MarketSnapshot(market_snapshot_id="snap_trd01", as_of=now,
                                    bid=D("70000"), ask=D("70100")),
            idempotency_key=key, created_by="svc_selfcheck", trace_id="trace_trd01",
        )

    try:
        # 1. Intent 등록 -> execution.order_intents 에 남는다
        intent = make_intent("trd01_selfcheck_0001")
        rec = oms.register_intent(intent)
        cur.execute("select intent_status, strategy_version_id, market_snapshot_id "
                    "from execution.order_intents where order_intent_id = %s",
                    (rec.order_intent_id,))
        status, saved_version, snapshot_id = cur.fetchone()
        assert status == "DRAFT", status
        assert saved_version == strategy_version_id, "strategy 버전이 다른 것으로 붙었다"
        assert snapshot_id is not None, "Intent가 참조한 호가가 증거로 남지 않았다"

        # 2. 멱등 - 같은 idempotency_key로 두 번 등록해도 행이 하나다
        again = oms.register_intent(make_intent("trd01_selfcheck_0001"))
        assert again.order_intent_id == rec.order_intent_id
        cur.execute("select count(*) from execution.order_intents where idempotency_key = %s",
                    ("trd01_selfcheck_0001",))
        assert cur.fetchone()[0] == 1, "같은 키로 Intent가 두 건 생겼다"

        # 3. Risk 승인 없이는 Broker Order가 생기지 않는다 (불변식 1)
        try:
            oms.create_broker_order(rec, intent)
            raise AssertionError("Risk 판정 없이 주문이 생성됐다")
        except Exception as exc:
            assert "Risk" in str(exc) or "READY_TO_SUBMIT" in str(exc), exc

        # 4. 심사 진행 -> Projection이 따라간다
        oms.request_risk_review(rec)
        cur.execute("select intent_status from execution.order_intents where order_intent_id = %s",
                    (rec.order_intent_id,))
        assert cur.fetchone()[0] == "RISK_PENDING"

        oms.apply_risk_decision(rec, RiskDecision(
            risk_decision_id=uuid4(), order_intent_id=rec.order_intent_id,
            verdict="approve", approved_quantity=D("100"),
            expires_at=now + timedelta(hours=1), reason="self check",
            decided_by="svc_risk_engine"))
        cur.execute("select intent_status from execution.order_intents where order_intent_id = %s",
                    (rec.order_intent_id,))
        assert cur.fetchone()[0] == "READY_TO_SUBMIT"

        # 5. Broker Order 생성 -> submit -> ack. execution.orders/order_events에 남는다
        order = oms.create_broker_order(rec, intent)
        oms.submit(order, rec)
        oms.on_broker_event(order, "ack", "trd01_ack_1", now, {"broker_order_id": "B-1"})
        cur.execute("select state, requested_quantity from execution.orders where order_id = %s",
                    (order.order_id,))
        assert cur.fetchone() == ("ACKNOWLEDGED", D("100")), "주문 상태가 DB에 반영되지 않았다"
        cur.execute("select count(*), max(sequence) from execution.order_events where order_id = %s",
                    (order.order_id,))
        assert cur.fetchone() == (3, 3), "이벤트 순번이 어긋났다"

        # 6. 부분 체결 -> execution.fills. 회계본부가 읽을 표다
        oms.on_broker_event(order, "fill", "trd01_fill_1", now,
                            {"quantity": "40", "price": "70000", "fee": "105", "tax": "0",
                             "broker_fill_id": "trd01_bf_1"})
        cur.execute("select quantity, price, gross_amount, side from execution.fills "
                    "where order_id = %s", (order.order_id,))
        assert cur.fetchone() == (D("40"), D("70000"), D("2800000"), "BUY")
        cur.execute("select state, filled_quantity from execution.orders where order_id = %s",
                    (order.order_id,))
        assert cur.fetchone() == ("PARTIALLY_FILLED", D("40"))

        # 7. 같은 broker event 재수신은 두 번 잡히지 않는다 (불변식 4)
        oms.on_broker_event(order, "fill", "trd01_fill_1", now,
                            {"quantity": "40", "price": "70000", "fee": "105", "tax": "0",
                             "broker_fill_id": "trd01_bf_1"})
        cur.execute("select filled_quantity from execution.orders where order_id = %s",
                    (order.order_id,))
        assert cur.fetchone()[0] == D("40"), "중복 이벤트가 두 번 반영됐다"

        # 8. DB 트리거가 전이표를 독립적으로 강제한다 - 우리 코드를 우회해도 막힌다.
        #    실패한 문장은 트랜잭션을 중단시키므로 savepoint로 감싼다. 여기서 그냥
        #    rollback하면 뒤의 검증까지 같이 날아간다.
        cur.execute("savepoint before_bad_transition")
        try:
            cur.execute("update execution.orders set state = 'CREATED' where order_id = %s",
                        (order.order_id,))
            raise AssertionError("PARTIALLY_FILLED -> CREATED 역행이 DB에서 통과했다")
        except psycopg2.Error as exc:
            assert "transition" in str(exc).lower(), exc
        cur.execute("rollback to savepoint before_bad_transition")

        # 9. 저장소에서 복원한 주문이 같은 답을 낸다 (Projection과 이벤트 일치)
        restored = store.get_order(order.order_id)
        assert restored is not None and restored.state is BrokerOrderState.PARTIALLY_FILLED
        assert restored.filled_quantity == D("40") and restored.leaves_quantity == D("60")
        assert oms.rebuild_state("broker_order", order.order_id) == "PARTIALLY_FILLED", \
            "이벤트로 재구축한 상태가 Projection과 다르다"
        assert store.find_order_by_intent(rec.order_intent_id).order_id == order.order_id
        assert store.find_intent_by_idempotency("trd01_selfcheck_0001") is not None
        assert store.find_unknown_order(fund_id) is None, "UNKNOWN이 없는데 있다고 나온다"

    finally:
        conn.rollback()  # 선행 행까지 전부 되돌린다. 남의 표에 아무것도 남기지 않는다
        cur.execute("select count(*) from execution.order_intents")
        assert cur.fetchone()[0] == 0, "롤백 후에도 주문이 남았다"
        conn.close()

    print("ok - psycopg OrderStore 9개 영역 점검 통과 "
          "(실 DB 트랜잭션, 롤백으로 선행 행 미영속, 전이표는 DB 트리거가 강제)")
