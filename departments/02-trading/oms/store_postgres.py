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

import outbox
from contracts import BrokerOrderState, IntentState, MarketSnapshot, OrderIntent, Side
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
             where strategy_id = %s and deployment_state = 'PAPER'
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

    def _validate_owner_context(self, cur, intent: OrderIntent) -> None:
        """Revalidate every owner-owned row before durable insertion.

        A foreign key proves existence, not that all owners agree.  This
        single-leg PAPER slice therefore requires the Case, Signal, Strategy
        Version, Capability, Fund, Book, and instrument to describe exactly
        the requested intent.
        """
        cur.execute(
            """
            select tc.trade_case_id
              from execution.trade_cases tc
              join accounting.funds f
                on f.fund_id = tc.fund_id
              join accounting.books b
                on b.book_id = tc.book_id and b.fund_id = tc.fund_id
              join strategy.versions sv
                on sv.strategy_version_id = tc.strategy_version_id
               and sv.strategy_id = %s
               and sv.deployment_state = 'PAPER'
              join strategy.strategies s
                on s.strategy_id = sv.strategy_id and s.status = 'PAPER'
              join strategy.capability_profiles cp
                on cp.capability_profile_id = sv.capability_profile_id
               and cp.status in ('ACTIVE', 'APPROVED')
              join strategy.signals sig
                on sig.signal_id = tc.signal_id
               and sig.case_id = tc.trade_case_id
               and sig.fund_id = tc.fund_id
               and sig.strategy_version_id = tc.strategy_version_id
              join reference.instruments i
                on i.instrument_id = %s
               and i.instrument_id = tc.primary_instrument_id
               and i.asset_class = 'EQUITY'
               and i.status = 'ACTIVE'
              join strategy.signal_targets st
                on st.signal_id = sig.signal_id
               and st.leg_index = 0
               and st.instrument_id = %s
             where tc.trade_case_id = %s
               and tc.fund_id = %s
               and tc.book_id = %s
               and f.status = 'ACTIVE'
               and b.status = 'ACTIVE'
               and tc.case_status = 'OPEN'
            """,
            (
                intent.strategy_id,
                intent.instrument_id,
                intent.instrument_id,
                intent.trade_case_id,
                intent.fund_id,
                intent.book_id,
            ),
        )
        if cur.fetchone() is None:
            raise OrderStorePersistenceError(
                "Paper Intent의 Case/Signal/Strategy/Capability/Fund/Book/instrument "
                "소유 증거가 없거나 서로 일치하지 않습니다"
            )
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
            self._validate_owner_context(cur, intent)
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

    def _write_order_event(self, cur, event: StateEvent, Json) -> bool:
        """Persist one order event and projection on the caller's cursor."""
        if event.broker_event_id is not None:
            cur.execute(
                """
                select order_id, event_type, from_state, to_state, payload, event_time
                  from execution.order_events
                 where broker_adapter = %s and broker_event_id = %s
                """,
                (event.broker_adapter, event.broker_event_id),
            )
            existing = cur.fetchone()
            if existing is not None:
                if (
                    existing[0] != event.stream_id
                    or existing[1] != event.event_type
                    or existing[2] != event.from_state
                    or existing[3] != event.to_state
                    or existing[4] != event.payload
                    or existing[5] != event.event_time
                ):
                    raise OrderStorePersistenceError(
                        "같은 broker_event_id의 내용이 서로 다릅니다"
                    )
                return False

        cur.execute(
            """
            insert into execution.order_events
                (order_event_id, order_id, event_type, event_time, received_at,
                 broker_adapter, broker_event_id, from_state, to_state, payload,
                 payload_hash, sequence, trace_id)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (event.event_id, event.stream_id, event.event_type, event.event_time,
             event.received_at, event.broker_adapter, event.broker_event_id,
             event.from_state, event.to_state, Json(event.payload),
             _hash(event.payload), event.sequence,
             _trace_uuid("", event.stream_id)),
        )
        cur.execute(
            "update execution.orders set state = %s, last_event_at = %s, "
            "version = version + 1, broker_order_id = coalesce(%s, broker_order_id) "
            "where order_id = %s and state <> %s",
            (
                event.to_state, event.event_time,
                event.payload.get("broker_order_id"),
                event.stream_id, event.to_state,
            ),
        )
        outbox.enqueue(cur, outbox.envelope_for(
            event_type=outbox.ORDER_STATE_CHANGED,
            trace_id=str(_trace_uuid("", event.stream_id)),
            idempotency_key=f"osc_{event.event_id}",
            occurred_at=event.event_time,
            artifact_type="ORDER_EVENT", artifact_id=event.event_id,
            artifact_schema=f"trading-order-event-v{SCHEMA_VERSION}",
            content_hash=f"sha256:{_hash(event.payload)}"))
        return True

    def add_event(self, event: StateEvent) -> None:
        _, Json, _ = _load_driver()
        with self.cursor() as cur:
            if event.stream == "intent":
                cur.execute(
                    "update execution.order_intents set intent_status = %s "
                    "where order_intent_id = %s",
                    (event.to_state, event.stream_id),
                )
                return
            if event.stream == "broker_order":
                self._write_order_event(cur, event, Json)

    def apply_fill(
        self,
        order: BrokerOrder,
        event: StateEvent,
        fill: Fill,
        *,
        filled_quantity: Decimal,
        average_fill_price: Decimal,
    ) -> None:
        """Persist event, Fill, projection, and payload-ref outbox atomically."""
        _, Json, _ = _load_driver()
        with self.cursor() as cur:
            broker_fill_id = fill.broker_fill_id or str(fill.fill_id)
            cur.execute(
                """
                select quantity, price, fee_amount, tax_amount
                  from execution.fills
                 where order_id = %s and broker_fill_id = %s
                """,
                (fill.order_id, broker_fill_id),
            )
            existing = cur.fetchone()
            if existing is not None:
                if tuple(existing) != (
                    fill.quantity, fill.price, fill.fee, fill.tax
                ):
                    raise OrderStorePersistenceError(
                        "같은 broker_fill_id의 내용이 서로 다릅니다"
                    )
                return
            self._write_order_event(cur, event, Json)
            cur.execute(
                """
                insert into execution.fills
                    (fill_id, order_id, broker_fill_id, instrument_id, side, quantity,
                     price, gross_amount, fee_amount, tax_amount, currency,
                     event_time, received_at, trace_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (fill.fill_id, fill.order_id, broker_fill_id, order.instrument_id,
                 str(order.side), fill.quantity, fill.price,
                 fill.quantity * fill.price, fill.fee, fill.tax, DEFAULT_CURRENCY,
                 fill.event_time, datetime.now(timezone.utc),
                 _trace_uuid("", order.order_id)),
            )
            cur.execute(
                "update execution.orders set filled_quantity = %s, average_fill_price = %s "
                "where order_id = %s",
                (filled_quantity, average_fill_price, order.order_id),
            )
            outbox.enqueue(cur, outbox.envelope_for(
                event_type=outbox.FILL_RECORDED,
                trace_id=str(_trace_uuid("", order.order_id)),
                idempotency_key=f"fill_{self.adapter}_{broker_fill_id}",
                occurred_at=fill.event_time,
                artifact_type="FILL", artifact_id=fill.fill_id,
                artifact_schema=f"trading-fill-v{SCHEMA_VERSION}",
                content_hash=f"sha256:{_hash({'fill_id': str(fill.fill_id), 'order_id': str(fill.order_id), 'quantity': str(fill.quantity), 'price': str(fill.price)})}"))
        order.state = BrokerOrderState(event.to_state)
        order.version += 1
        order.filled_quantity = filled_quantity
        order.fills.append(fill)
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
            # 회계 Ledger Consumer 가 `execution.fills` 를 폴링하는 대신 이 이벤트를
            # 읽는다(P0-1 "Fill 은 실제 Event/Consumer 경로에서 생성한다").
            # 체결 자체는 위 표가 사실이고 봉투는 그 표를 가리키기만 한다 - 금액을
            # payload 에 싣지 않는다(6.1 "payload 본문을 내보내지 않는다").
            outbox.enqueue(cur, outbox.envelope_for(
                event_type=outbox.FILL_RECORDED,
                trace_id=str(_trace_uuid("", order.order_id)),
                idempotency_key=f"fill_{self.adapter}_{fill.broker_fill_id or fill.fill_id}",
                occurred_at=fill.event_time,
                artifact_type="FILL", artifact_id=fill.fill_id,
                artifact_schema=f"trading-fill-v{SCHEMA_VERSION}",
                content_hash=f"sha256:{_hash({'fill_id': str(fill.fill_id), 'order_id': str(fill.order_id), 'quantity': str(fill.quantity), 'price': str(fill.price)})}"))
        order.fills.append(fill)

    # -- 읽기 -----------------------------------------------------------------

    def get_intent(self, intent_id: UUID) -> OrderIntentRecord | None:
        with self.cursor() as cur:
            cur.execute(
                """
                select oi.order_intent_id, oi.fund_id, oi.idempotency_key, oi.quantity,
                       oi.valid_until, oi.intent_status, oi.risk_decision_id,
                       rd.approved_quantity, rd.valid_until
                  from execution.order_intents oi
                  left join risk.risk_decisions rd on rd.risk_decision_id = oi.risk_decision_id
                 where oi.order_intent_id = %s
                """,
                (intent_id,),
            )
            row = cur.fetchone()
        return _to_record(row) if row else None

    def find_intent_by_idempotency(self, key: str) -> OrderIntentRecord | None:
        with self.cursor() as cur:
            cur.execute(
                """
                select oi.order_intent_id, oi.fund_id, oi.idempotency_key, oi.quantity,
                       oi.valid_until, oi.intent_status, oi.risk_decision_id,
                       rd.approved_quantity, rd.valid_until
                  from execution.order_intents oi
                  left join risk.risk_decisions rd on rd.risk_decision_id = oi.risk_decision_id
                 where oi.idempotency_key = %s
                """,
                (key,),
            )
            row = cur.fetchone()
        return _to_record(row) if row else None

    def list_intents(self, limit: int | None = None) -> list[OrderIntentRecord]:
        with self.cursor() as cur:
            cur.execute(
                """
                select oi.order_intent_id, oi.fund_id, oi.idempotency_key, oi.quantity,
                       oi.valid_until, oi.intent_status, oi.risk_decision_id,
                       rd.approved_quantity, rd.valid_until
                  from execution.order_intents oi
                  left join risk.risk_decisions rd on rd.risk_decision_id = oi.risk_decision_id
                 order by oi.created_at desc limit %s
                """,
                (limit or 200,),
            )
            rows = cur.fetchall()
        return [_to_record(r) for r in rows]

    def get_order(self, order_id: UUID) -> BrokerOrder | None:
        with self.cursor() as cur:
            cur.execute(_ORDER_SELECT + " where o.order_id = %s", (order_id,))
            row = cur.fetchone()
        return self._hydrate_fills(_to_order(row)) if row else None

    def find_order_by_intent(self, intent_id: UUID) -> BrokerOrder | None:
        with self.cursor() as cur:
            cur.execute(_ORDER_SELECT + " where o.order_intent_id = %s", (intent_id,))
            row = cur.fetchone()
        return self._hydrate_fills(_to_order(row)) if row else None

    def list_orders(self, limit: int | None = None) -> list[BrokerOrder]:
        with self.cursor() as cur:
            cur.execute(_ORDER_SELECT + " order by o.created_at desc limit %s", (limit or 200,))
            rows = cur.fetchall()
        return [self._hydrate_fills(_to_order(r)) for r in rows]

    def find_unknown_order(self, fund_id: UUID) -> BrokerOrder | None:
        """전체 스캔이 아니라 조회 한 방이다(인메모리 구현의 ponytail 주석 참고)."""
        with self.cursor() as cur:
            cur.execute(_ORDER_SELECT + " where i.fund_id = %s and o.state = 'UNKNOWN' limit 1",
                        (fund_id,))
            row = cur.fetchone()
        return self._hydrate_fills(_to_order(row)) if row else None

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

    def existing_broker_event(
        self, adapter: str, broker_event_id: str | None
    ) -> StateEvent | None:
        if broker_event_id is None:
            return None
        with self.cursor() as cur:
            cur.execute(
                """
                select order_id, event_type, sequence, from_state, to_state,
                       event_time, received_at, broker_event_id, payload
                  from execution.order_events
                 where broker_adapter = %s and broker_event_id = %s
                """,
                (adapter, broker_event_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        order_id, event_type, sequence, from_state, to_state, event_time, received_at, bid, payload = row
        return StateEvent(
            event_id=UUID(int=0), stream="broker_order", stream_id=order_id,
            event_type=event_type, sequence=sequence, from_state=from_state,
            to_state=to_state, event_time=event_time, received_at=received_at,
            broker_adapter=adapter, broker_event_id=bid, payload=payload,
        )

    def existing_fill(self, order_id: UUID, broker_fill_id: str | None) -> Fill | None:
        if broker_fill_id is None:
            return None
        with self.cursor() as cur:
            cur.execute(
                """
                select fill_id, quantity, price, fee_amount, tax_amount, event_time
                  from execution.fills
                 where order_id = %s and broker_fill_id = %s
                """,
                (order_id, broker_fill_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        fill_id, quantity, price, fee, tax, event_time = row
        return Fill(
            fill_id=fill_id, order_id=order_id, event_id=UUID(int=0),
            quantity=quantity, price=price, fee=fee, tax=tax,
            event_time=event_time, broker_fill_id=broker_fill_id,
        )

    def reconstruct_intent(self, intent_id: UUID) -> OrderIntent | None:
        with self.cursor() as cur:
            cur.execute(
                """
                select oi.order_intent_id, oi.trade_case_id, oi.fund_id, oi.book_id,
                       sv.strategy_id, oi.instrument_id, oi.side, oi.order_type,
                       oi.quantity, oi.limit_price, oi.time_in_force, oi.valid_until,
                       oi.idempotency_key, oi.trace_id, oi.created_at,
                       ms.source_ref, ms.as_of, ms.bid, ms.ask
                  from execution.order_intents oi
                  join strategy.versions sv
                    on sv.strategy_version_id = oi.strategy_version_id
                  join execution.market_snapshots ms
                    on ms.market_snapshot_id = oi.market_snapshot_id
                 where oi.order_intent_id = %s
                """,
                (intent_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        (
            oid, case_id, fund_id, book_id, strategy_id, instrument_id, side,
            order_type, quantity, limit_price, tif, valid_until, idem, trace_id,
            created_at, source_ref, as_of, bid, ask,
        ) = row
        return OrderIntent(
            order_intent_id=oid, trade_case_id=case_id, fund_id=fund_id,
            book_id=book_id, strategy_id=strategy_id, instrument_id=instrument_id,
            side=Side(side), order_type=order_type, quantity=quantity,
            limit_price=limit_price, time_in_force=tif, valid_until=valid_until,
            snapshot=MarketSnapshot(
                market_snapshot_id=source_ref, as_of=as_of, bid=bid, ask=ask,
            ),
            idempotency_key=idem, created_by="durable-reconstruction",
            trace_id=str(trace_id), created_at=created_at,
        )
    def _risk_evidence(
        self, cur, rec: OrderIntentRecord, decision_id: UUID | None = None
    ):
        cur.execute(
            """
            select rd.decision, rd.approved_quantity, rd.max_price, rd.valid_until,
                   rr.fund_id, rr.book_id, rr.strategy_version_id,
                   rr.capability_profile_id, ri.order_intent_id,
                   oi.fund_id, oi.book_id, oi.strategy_version_id,
                   oi.instrument_id, oi.side
              from risk.risk_decisions rd
              join risk.risk_requests rr on rr.risk_request_id = rd.risk_request_id
              join risk.risk_request_items ri on ri.risk_request_id = rr.risk_request_id
              join execution.order_intents oi on oi.order_intent_id = ri.order_intent_id
             where rd.risk_decision_id = %s
               and ri.order_intent_id = %s
            """,
            (decision_id or rec.risk_decision_id, rec.order_intent_id),
        )
        return cur.fetchone()

    def validate_risk_decision(
        self, rec: OrderIntentRecord, decision
    ) -> None:
        with self.cursor() as cur:
            row = self._risk_evidence(cur, rec, decision.risk_decision_id)
        if row is None:
            raise OrderStorePersistenceError(
                "Risk-owner의 canonical risk_decision/risk_request/item 증거가 없습니다"
            )
        (
            verdict, approved_qty, max_price, expires_at, fund_id, book_id,
            strategy_version_id, capability_profile_id, order_intent_id,
            oi_fund_id, oi_book_id, oi_strategy_version_id, _instrument_id, _side,
        ) = row
        expected_verdict = str(decision.verdict).upper()
        if expected_verdict == "APPROVE":
            expected_verdict = "APPROVE"
        if (
            verdict != expected_verdict
            or approved_qty != decision.approved_quantity
            or max_price != decision.max_price
            or expires_at != decision.expires_at
            or order_intent_id != rec.order_intent_id
            or fund_id != rec.fund_id
            or oi_fund_id != rec.fund_id
            or book_id != oi_book_id
            or strategy_version_id != oi_strategy_version_id
            or capability_profile_id is None
        ):
            raise OrderStorePersistenceError(
                "Risk-owner 판정이 Intent의 canonical 소유 범위와 일치하지 않습니다"
            )

    def save_risk_decision(self, rec: OrderIntentRecord, decision) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                update execution.order_intents
                   set risk_decision_id = %s, quantity = %s
                 where order_intent_id = %s
                """,
                (decision.risk_decision_id, rec.requested_quantity, rec.order_intent_id),
            )
            if cur.rowcount != 1:
                raise OrderStorePersistenceError("Risk 판정을 저장할 Intent가 없습니다")

    def revalidate_risk_decision(self, rec: OrderIntentRecord, order: BrokerOrder) -> None:
        with self.cursor() as cur:
            row = self._risk_evidence(cur, rec)
        if row is None:
            raise OrderStorePersistenceError(
                "Submit 시 canonical Risk-owner 증거를 재검증할 수 없습니다"
            )
        verdict, approved_qty, max_price, expires_at = row[:4]
        if (
            verdict == "REJECT"
            or approved_qty is None
            or approved_qty < order.requested_quantity
            or (
                max_price is not None
                and order.limit_price is not None
                and order.limit_price > max_price
            )
            or expires_at != rec.risk_expires_at
            or approved_qty != rec.risk_approved_qty
        ):
            raise OrderStorePersistenceError(
                "Submit 시 canonical Risk 판정이 없거나 변경되었습니다"
            )
    def _hydrate_fills(self, order: BrokerOrder) -> BrokerOrder:
        with self.cursor() as cur:
            cur.execute(
                """
                select fill_id, broker_fill_id, quantity, price, fee_amount, tax_amount,
                       event_time
                  from execution.fills
                 where order_id = %s
                 order by event_time, created_at, fill_id
                """,
                (order.order_id,),
            )
            rows = cur.fetchall()
        order.fills = [
            Fill(
                fill_id=row[0], order_id=order.order_id, event_id=UUID(int=0),
                quantity=row[2], price=row[3], fee=row[4], tax=row[5],
                event_time=row[6], broker_fill_id=row[1],
            )
            for row in rows
        ]
        return order
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
       o.broker_order_id, o.filled_quantity, o.version, o.average_fill_price
  from execution.orders o
  join execution.order_intents i on i.order_intent_id = o.order_intent_id
"""


def _to_record(row) -> OrderIntentRecord:
    (
        intent_id, fund_id, key, quantity, valid_until, status, risk_decision_id,
        risk_approved_qty, risk_expires_at,
    ) = (*row[:7], *(row[7:9] if len(row) >= 9 else (None, None)))
    return OrderIntentRecord(
        order_intent_id=intent_id, fund_id=fund_id, idempotency_key=key,
        requested_quantity=quantity, valid_until=valid_until,
        state=IntentState(status), risk_decision_id=risk_decision_id,
        risk_approved_qty=risk_approved_qty, risk_expires_at=risk_expires_at,
    )


def _to_order(row) -> BrokerOrder:
    (
        order_id, intent_id, fund_id, client_order_id, adapter, side, instrument_id,
        requested, limit_price, state, broker_order_id, filled, version, _average,
    ) = row
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

        # 10. **Transactional Outbox** — 상태 변경이 outbox 행을 같은 트랜잭션에 남긴다
        cur.execute("select event_type, status, trace_id, payload_ref "
                    "  from execution.outbox where idempotency_key like %s",
                    (f"fill_{store.adapter}_trd01_bf_1",))
        fill_row = cur.fetchone()
        assert fill_row is not None, "체결이 outbox 이벤트를 안 남겼다"
        assert fill_row[0] == outbox.FILL_RECORDED and fill_row[1] == "PENDING"
        assert fill_row[3]["artifact_type"] == "FILL", fill_row[3]
        assert fill_row[3]["content_hash"].startswith("sha256:")
        cur.execute("select count(*) from execution.outbox where event_type = %s",
                    (outbox.ORDER_STATE_CHANGED,))
        assert cur.fetchone()[0] >= 1, "상태 전이가 outbox 이벤트를 안 남겼다"

        # 같은 체결을 다시 받아도 이벤트가 두 번 나가지 않는다 (봉투 멱등)
        oms.on_broker_event(order, "fill", "trd01_fill_1", now,
                            {"quantity": "40", "price": "70000", "fee": "105", "tax": "0",
                             "broker_fill_id": "trd01_bf_1"})
        cur.execute("select count(*) from execution.outbox where idempotency_key = %s",
                    (f"fill_{store.adapter}_trd01_bf_1",))
        assert cur.fetchone()[0] == 1, "중복 체결이 이벤트를 두 번 냈다"

        # 11. **롤백 불변식** — 상태가 사라지면 outbox 도 같이 사라진다.
        #     이게 "Transactional" Outbox 의 전부다. 다른 트랜잭션에서 넣었다면 여기서 남는다.
        cur.execute("savepoint before_outbox_rollback")
        oms.on_broker_event(order, "fill", "trd01_fill_2", now,
                            {"quantity": "10", "price": "70000", "fee": "26", "tax": "0",
                             "broker_fill_id": "trd01_bf_rollback"})
        cur.execute("select count(*) from execution.outbox where idempotency_key = %s",
                    (f"fill_{store.adapter}_trd01_bf_rollback",))
        assert cur.fetchone()[0] == 1, "롤백 전인데 이벤트가 없다"
        cur.execute("rollback to savepoint before_outbox_rollback")
        cur.execute("select count(*) from execution.outbox where idempotency_key = %s",
                    (f"fill_{store.adapter}_trd01_bf_rollback",))
        assert cur.fetchone()[0] == 0, "상태는 롤백됐는데 outbox 행이 남았다"
        cur.execute("select filled_quantity from execution.orders where order_id = %s",
                    (order.order_id,))
        assert cur.fetchone()[0] == D("40"), "롤백 후 체결 수량이 안 되돌아갔다"

        # 12. **재시작 복구** — 연결을 새로 열어도 상태·수량·hash 가 같다.
        #     같은 트랜잭션을 보려면 커밋해야 하는데 자체 점검은 아무것도 안 남긴다.
        #     그래서 같은 연결에서 저장소 객체만 새로 만들어 Projection 재구축을 확인한다.
        #     ponytail: 진짜 프로세스 재시작 검증은 커밋이 필요하다. 전용 스키마나
        #               throwaway DB 가 생기면 그때 붙인다 - 지금 커밋하면 공용 DB 를 더럽힌다.
        reopened = PostgresOrderStore(conn=conn, adapter="paper")
        again = reopened.get_order(order.order_id)
        assert again is not None and again.state is restored.state
        assert again.filled_quantity == restored.filled_quantity
        assert again.average_fill_price == restored.average_fill_price
        before = [(e.sequence, e.to_state, e.event_id) for e in
                  store.events_for("broker_order", order.order_id)]
        after = [(e.sequence, e.to_state, e.event_id) for e in
                 reopened.events_for("broker_order", order.order_id)]
        assert before == after, "재조회에서 이벤트 열이 달라졌다"

        # 13. Relay 가 미발행 이벤트를 집고, 소비자 중복 제거가 동작한다
        published: list = []
        result = outbox.drain(cur, published.append)
        assert result.sent >= 2 and result.clean, result
        fill_events = [p for p in published if p["event_type"] == outbox.FILL_RECORDED]
        assert fill_events, "체결 이벤트가 발행되지 않았다"
        target = fill_events[0]["event_id"]
        assert outbox.mark_processed(cur, "accounting-ledger", target) is True
        assert outbox.mark_processed(cur, "accounting-ledger", target) is False
        todo = outbox.unconsumed(cur, "accounting-ledger", outbox.FILL_RECORDED)
        assert target not in {t["event_id"] for t in todo}, "처리한 이벤트가 다시 잡힌다"

    finally:
        conn.rollback()  # 선행 행까지 전부 되돌린다. 남의 표에 아무것도 남기지 않는다
        cur.execute("select count(*) from execution.order_intents")
        assert cur.fetchone()[0] == 0, "롤백 후에도 주문이 남았다"
        cur.execute("select count(*) from execution.outbox")
        assert cur.fetchone()[0] == 0, "롤백 후에도 outbox 이벤트가 남았다"
        conn.close()

    print("ok - psycopg OrderStore 13개 영역 점검 통과 "
          "(실 DB 트랜잭션, 롤백으로 선행 행 미영속, 전이표는 DB 트리거가 강제, "
          "Outbox 는 상태와 같은 트랜잭션)")
