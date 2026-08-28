#!/usr/bin/env python3
"""Sprint J0: MarketDataRepository 인터페이스와 TimescaleDB 구현.

소유: 재일 (리서치본부)
근거: docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 2.2, 4.1, 4.3, 9(Sprint J0/J1)
      docs/database/README.md 2절(저장소 분리), 7절(적용 방법)
      docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F03, F04

왜 인터페이스로 감싸는가(가이드 2.2) - 지금은 로컬 Docker TimescaleDB 이고 나중에
VPS 또는 Managed Timescale 로 옮긴다. 그때 수집기와 Feature Engine 을 고치지 않으려면
저장 계층이 이 인터페이스 뒤에 있어야 한다.

멱등성은 마이그레이션의 primary key (event_time, source_event_id) 가 만든다.
여기서는 `on conflict do nothing` 으로 재접속 중복 적재를 흡수하고, 실제로 몇 건이
중복이었는지 세어서 DQ 로 넘긴다 - 조용히 삼키면 Gap 과 구분할 수 없다(가이드 8.2).

다른 본부는 이 Repository 를 쓰지 않는다. market-api 를 호출한다(가이드 2.2, 6.1).

자체 점검(DB 없이):        python departments/01-research/repository/market_repository.py
통합 점검(컨테이너 필요):  python departments/01-research/repository/market_repository.py --integration
"""
from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

# 같은 본부 안의 계약이라 상대 import 로 붙인다. 본부 경계를 넘는 import 는 하지 않는다
# (REPOSITORY_DEPARTMENT_STRUCTURE 8절 의존성 방향).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contracts"))
from market_events import (
    InstrumentRef,
    Market,
    MarketQuote,
    MarketTick,
    ObservationTimes,
    QualityFlag,
    SessionType,
    Side,
    build_source_event_id,
)

REPOSITORY_VERSION = "research-market-repository-v1"


import itertools

from reference_repository import guarded_execute_values


@dataclass(frozen=True)


class WriteResult:
    """적재 결과. 중복을 숨기지 않고 세어서 돌려준다.

    duplicates 가 계속 0 이 아니면 Collector 의 Sequence 처리나 재접속 로직을
    봐야 한다는 신호다. 정상 재접속에서는 일시적으로 0 이 아닐 수 있다.
    """

    attempted: int
    inserted: int
    duplicates: int

    def __post_init__(self) -> None:
        if self.inserted + self.duplicates != self.attempted:
            raise ValueError(
                f"집계 불일치: attempted={self.attempted} "
                f"inserted={self.inserted} duplicates={self.duplicates}"
            )


@dataclass(frozen=True)
class Snapshot:
    """market-api get_snapshot 이 내보낼 최신 상태(가이드 6.1).

    as_of / freshness / quality_status 를 값과 함께 넘긴다 - 값만 주면 소비 본부가
    Stale 을 정상값으로 쓴다(가이드 10절 Handoff 규칙).
    """

    instrument_id: UUID
    market: Market
    as_of: datetime
    last_price: Decimal | None
    last_quantity: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    spread: Decimal | None
    freshness: timedelta
    quality_flags: tuple[QualityFlag, ...]


class MarketDataRepository(ABC):
    """시장 시계열 저장·조회 경계. 구현체를 바꿔도 호출부는 그대로여야 한다."""

    @abstractmethod
    def write_ticks(self, ticks: list[MarketTick]) -> WriteResult:
        """멱등 적재. 같은 source_event_id 재적재는 duplicates 로 센다."""

    @abstractmethod
    def write_quotes(self, quotes: list[MarketQuote]) -> WriteResult:
        ...

    @abstractmethod
    def get_snapshot(self, instrument_id: UUID, *, now: datetime | None = None) -> Snapshot | None:
        ...

    @abstractmethod
    def count_ticks(self, instrument_id: UUID, start: datetime, end: datetime) -> int:
        """Timescale Row Count. Parquet Row Count 와 비교하는 DQ 입력이다(가이드 8.2)."""

    @abstractmethod
    def find_sequence_gaps(
        self, instrument_id: UUID, start: datetime, end: datetime
    ) -> tuple[tuple[str, str], ...]:
        """연속하지 않은 sequence_no 경계를 돌려준다. F03 완료 조건의 Gap 조회다."""

    @abstractmethod
    def close(self) -> None:
        ...


class InMemoryMarketRepository(MarketDataRepository):
    """DB 없이 계약을 검증하는 구현. Test 와 Replay Fixture 용이다.

    Production 경로에서 쓰지 않는다. Timescale 구현과 동일한 멱등 규칙
    (event_time, source_event_id)을 재현하는 것이 목적이다.
    """

    def __init__(self) -> None:
        self._ticks: dict[tuple[datetime, str], MarketTick] = {}
        self._quotes: dict[tuple[datetime, str], MarketQuote] = {}

    def write_ticks(self, ticks: list[MarketTick]) -> WriteResult:
        inserted = 0
        for t in ticks:
            key = (t.times.event_time, t.source_event_id)
            if key in self._ticks:
                continue
            self._ticks[key] = t
            inserted += 1
        return WriteResult(len(ticks), inserted, len(ticks) - inserted)

    def write_quotes(self, quotes: list[MarketQuote]) -> WriteResult:
        inserted = 0
        for q in quotes:
            key = (q.times.event_time, q.source_event_id)
            if key in self._quotes:
                continue
            self._quotes[key] = q
            inserted += 1
        return WriteResult(len(quotes), inserted, len(quotes) - inserted)

    def get_snapshot(self, instrument_id: UUID, *, now: datetime | None = None) -> Snapshot | None:
        now = now or datetime.now(timezone.utc)
        ticks = [t for t in self._ticks.values() if t.instrument.instrument_id == instrument_id]
        quotes = [q for q in self._quotes.values() if q.instrument.instrument_id == instrument_id]
        if not ticks and not quotes:
            return None

        last_tick = max(ticks, key=lambda t: t.times.event_time, default=None)
        last_quote = max(quotes, key=lambda q: q.times.event_time, default=None)
        as_of = max(
            [x.times.event_time for x in (last_tick, last_quote) if x is not None]
        )
        ref = (last_tick or last_quote).instrument
        flags: list[QualityFlag] = []
        for src in (last_tick, last_quote):
            if src is not None:
                flags.extend(src.quality_flags)

        return Snapshot(
            instrument_id=instrument_id,
            market=ref.market,
            as_of=as_of,
            last_price=last_tick.price if last_tick else None,
            last_quantity=last_tick.quantity if last_tick else None,
            best_bid=last_quote.best_bid if last_quote else None,
            best_ask=last_quote.best_ask if last_quote else None,
            spread=last_quote.spread if last_quote else None,
            freshness=now - as_of,
            quality_flags=tuple(dict.fromkeys(flags)),
        )

    def count_ticks(self, instrument_id: UUID, start: datetime, end: datetime) -> int:
        return sum(
            1
            for t in self._ticks.values()
            if t.instrument.instrument_id == instrument_id and start <= t.times.event_time < end
        )

    def find_sequence_gaps(
        self, instrument_id: UUID, start: datetime, end: datetime
    ) -> tuple[tuple[str, str], ...]:
        rows = sorted(
            (
                t
                for t in self._ticks.values()
                if t.instrument.instrument_id == instrument_id
                and start <= t.times.event_time < end
                and t.sequence_no is not None
            ),
            key=lambda t: t.times.event_time,
        )
        gaps = []
        for prev, cur in itertools.pairwise(rows):
            if not _is_consecutive(prev.sequence_no, cur.sequence_no):
                gaps.append((prev.sequence_no, cur.sequence_no))
        return tuple(gaps)

    def close(self) -> None:
        return None


def _is_consecutive(a: str | None, b: str | None) -> bool:
    """Sequence 가 연속인지 판정한다. 숫자로 안 떨어지면 판정하지 않는다(False 아님).

    공급자 Sequence 형식을 추측해서 Gap 이라고 단정하면 없는 장애를 만든다.
    숫자가 아니면 '판정 불가'이므로 연속으로 취급하고 별도 Flag 로 남긴다.
    """
    if a is None or b is None:
        return True
    try:
        return int(b) == int(a) + 1
    except (TypeError, ValueError):
        return True


class TimescaleMarketRepository(MarketDataRepository):
    """psycopg2 기반 TimescaleDB 구현.

    DSN 은 .env 의 TIMESCALE_DATABASE_URL 을 그대로 쓴다(가이드 문서 7절이 지정한 이름).
    다른 본부에는 이 DSN 을 주지 않는다.
    """

    def __init__(self, dsn: str) -> None:
        import psycopg2  # requirements.txt: psycopg2-binary
        from psycopg2.extras import register_uuid

        # psycopg2 는 uuid.UUID 를 기본 어댑트하지 못한다. 등록하지 않으면
        # instrument_id / trace_id 삽입에서 "can't adapt type 'UUID'" 가 난다.
        # str() 로 우회하지 않는 이유 - 계약이 UUID 타입을 보장하는데 문자열로
        # 바꾸면 잘못된 형식이 DB까지 내려간다.
        register_uuid()

        self._dsn = dsn
        self._psycopg2 = psycopg2
        self._execute_values = guarded_execute_values
        self._conn = self._connect()

    def _connect(self):
        """Open one transactional market connection.

        The realtime collector deliberately shares this repository between its
        websocket shards.  Keeping connection setup here gives the writer one
        recovery boundary without creating a second database implementation.
        """
        conn = self._psycopg2.connect(self._dsn)
        conn.autocommit = False
        return conn

    def _reconnect(self) -> None:
        """Replace a connection known to be unusable.

        A dropped PostgreSQL socket cannot be repaired with rollback.  Closing
        it first also releases the stale client-side handle before a new
        backend is opened.
        """
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - the connection is already broken
            pass
        self._conn = self._connect()

    def _is_connection_error(self, exc: Exception) -> bool:
        """Return whether retrying the idempotent write on a new socket is safe."""
        return isinstance(exc, (self._psycopg2.InterfaceError, self._psycopg2.OperationalError))

    # market.market_ticks 의 Column 순서. contracts.to_row() 의 key 와 일치해야 한다.
    _TICK_COLUMNS = (
        "event_time", "received_at", "observed_at", "instrument_id", "provider", "tr_code",
        "market", "session_type", "price", "quantity", "side", "cumulative_volume",
        "cumulative_value", "sequence_no", "source_event_id", "correction_code",
        "trading_status", "raw_flags", "schema_version", "trace_id",
    )
    _QUOTE_COLUMNS = (
        "event_time", "received_at", "observed_at", "instrument_id", "provider", "tr_code",
        "market", "session_type", "bid_prices", "bid_sizes", "ask_prices", "ask_sizes",
        "total_bid_size", "total_ask_size", "best_bid", "best_ask", "mid_price", "spread",
        "depth_imbalance", "sequence_no", "source_event_id", "trading_status", "raw_flags",
        "schema_version", "trace_id",
    )

    def _insert(self, table: str, columns: tuple[str, ...], rows: list[dict]) -> WriteResult:
        if not rows:
            return WriteResult(0, 0, 0)

        import json as _json

        values = []
        for r in rows:
            missing = set(columns) - set(r)
            if missing:
                raise ValueError(f"{table}: Column 누락 {sorted(missing)}")
            extra = set(r) - set(columns)
            if extra:
                raise ValueError(f"{table}: 계약 밖 Column {sorted(extra)}")
            values.append(
                tuple(
                    _json.dumps(r[c], ensure_ascii=False) if c == "raw_flags" else r[c]
                    for c in columns
                )
            )

        # returning 으로 실제 삽입된 건수를 센다. rowcount 는 conflict 를 제외한 값이다.
        sql = (
            f"insert into {table} ({', '.join(columns)}) values %s "
            f"on conflict (event_time, source_event_id) do nothing "
            f"returning 1"
        )
        # 실패하면 **반드시 rollback 한다.** 안 그러면 커넥션이 aborted
        # transaction 에 갇혀 이후 모든 문장이 InFailedSqlTransaction 으로 죽는다 -
        # 한 번의 일시적 실패가 그 소켓의 남은 세션 전체를 못 쓰게 만들었다
        # (2026-08-02 수정). 예외는 그대로 올린다 - 삼키면 유실이 조용해진다.
        for attempt in range(2):
            try:
                with self._conn.cursor() as cur:
                    # fetch=True 가 필수다. execute_values 는 page_size 단위로 INSERT 를 여러
                    # 문장으로 쪼개는데, 그냥 cur.fetchall() 을 하면 **마지막 문장의 RETURNING
                    # 만** 잡힌다. 500건 이하 배치에서는 맞아 보이다가 그 이상에서 조용히
                    # 어긋난다(실측: 4293건 넣고 293건으로 셌다). 그러면 없는 중복이
                    # 보고되어 DQ 경보가 잘못 뜬다.
                    returned = self._execute_values(cur, sql, values, page_size=500, fetch=True)
                    inserted = len(returned)
                self._conn.commit()
                return WriteResult(len(rows), inserted, len(rows) - inserted)
            except Exception as exc:
                try:
                    self._conn.rollback()
                except Exception:  # noqa: BLE001, S110 - 커넥션이 이미 죽었으면 rollback 도 실패한다
                    pass
                if attempt == 0 and self._is_connection_error(exc):
                    # The row identity is protected by the primary key and the
                    # statement is ON CONFLICT DO NOTHING.  If the server
                    # committed immediately before the socket died, retrying
                    # is therefore safe and reports those rows as duplicates.
                    self._reconnect()
                    continue
                raise

    def write_ticks(self, ticks: list[MarketTick]) -> WriteResult:
        return self._insert("market.market_ticks", self._TICK_COLUMNS, [t.to_row() for t in ticks])

    def write_quotes(self, quotes: list[MarketQuote]) -> WriteResult:
        return self._insert(
            "market.market_quotes", self._QUOTE_COLUMNS, [q.to_row() for q in quotes]
        )

    # market.market_breadth 의 Column 순서. Tick/Quote 와 달리 ABC 에 올리지 않는다 -
    # ABC 는 실시간 Tick/Quote 계약이고 InMemory 구현은 그 계약의 대조군이다. 시장
    # Breadth 는 REST Snapshot 이라 성격이 다르므로 Timescale 구현에만 둔다.
    _BREADTH_COLUMNS = (
        "event_time", "observed_at", "market", "universe_version_id",
        "advancers", "decliners", "unchanged", "new_highs", "new_lows",
        "up_volume", "down_volume", "total_value", "values", "source", "quality_status",
    )

    def write_market_breadth(self, rows: list[dict]) -> WriteResult:
        """시장 Breadth Snapshot 을 적재한다. PK 는 (event_time, market, source) 다.

        같은 폐장 Snapshot 을 여러 번 수집해도 event_time 이 폐장 시각으로 고정되므로
        두 번째부터는 conflict 로 걸러진다(수집기 쪽 event_time 규칙 참고).

        ▶ do nothing 의 대가 - 먼저 쓰인 행이 영구히 이긴다
          같은 (event_time, market, source) 에 대해 나중 수집은 값이 무엇이든 조용히
          버려지고 duplicates 로만 집계된다. 그래서 **불량 행이 절대 들어오면 안 된다.**
          호출자가 quality_status='FAIL' 을 걸러서 넘기며, 여기서도 한 번 더 막는다 -
          Repository 는 계약을 지키는 마지막 지점이다.

          거래소가 Breadth 를 정정하는 경우는 이 구조로 반영할 수 없다. 정정 판정
          규칙(무엇을 나중 개정본으로 볼 것인가)이 없는 상태에서 do update 로 바꾸면
          잘못된 재수집이 정상 데이터를 덮어쓴다. 규칙이 생기면 그때 바꾼다.
        """
        if not rows:
            return WriteResult(0, 0, 0)

        bad = [r for r in rows if r.get("quality_status") == "FAIL"]
        if bad:
            raise ValueError(
                f"market_breadth: quality_status=FAIL 행 {len(bad)}건은 적재할 수 없다 - "
                f"on conflict do nothing 이라 같은 좌표의 정상 데이터가 영구히 막힌다"
            )

        import json as _json

        values = []
        for r in rows:
            missing = set(self._BREADTH_COLUMNS) - set(r)
            if missing:
                raise ValueError(f"market_breadth: Column 누락 {sorted(missing)}")
            extra = set(r) - set(self._BREADTH_COLUMNS)
            if extra:
                raise ValueError(f"market_breadth: 계약 밖 Column {sorted(extra)}")
            values.append(
                tuple(
                    _json.dumps(r[c], ensure_ascii=False) if c == "values" else r[c]
                    for c in self._BREADTH_COLUMNS
                )
            )

        sql = (
            f"insert into market.market_breadth ({', '.join(self._BREADTH_COLUMNS)}) values %s "
            f"on conflict (event_time, market, source) do nothing "
            f"returning 1"
        )
        # _insert 와 같은 이유로 실패 시 rollback 한다 - 안 하면 커넥션이
        # aborted transaction 에 갇혀 이후 전부 연쇄 실패한다.
        try:
            with self._conn.cursor() as cur:
                # _insert 와 같은 이유로 fetch=True 다.
                returned = self._execute_values(cur, sql, values, page_size=500, fetch=True)
                inserted = len(returned)
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # noqa: BLE001, S110 - intentional fallback boundary
                pass
            raise
        return WriteResult(len(rows), inserted, len(rows) - inserted)

    def count_market_breadth(self, market: str | None = None) -> int:
        with self._conn.cursor() as cur:
            if market is None:
                cur.execute("select count(*) from market.market_breadth")
            else:
                cur.execute(
                    "select count(*) from market.market_breadth where market = %s", (market,)
                )
            return int(cur.fetchone()[0])

    def get_snapshot(self, instrument_id: UUID, *, now: datetime | None = None) -> Snapshot | None:
        now = now or datetime.now(timezone.utc)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select event_time, market, price, quantity, raw_flags
                from market.market_ticks
                where instrument_id = %s
                order by event_time desc
                limit 1
                """,
                (str(instrument_id),),
            )
            tick = cur.fetchone()
            cur.execute(
                """
                select event_time, market, best_bid, best_ask, spread, raw_flags
                from market.market_quotes
                where instrument_id = %s
                order by event_time desc
                limit 1
                """,
                (str(instrument_id),),
            )
            quote = cur.fetchone()

        if tick is None and quote is None:
            return None

        as_of = max(x[0] for x in (tick, quote) if x is not None)
        market = Market((tick or quote)[1])
        flags: list[QualityFlag] = []
        for row, idx in ((tick, 4), (quote, 5)):
            if row is not None and row[idx]:
                flags.extend(QualityFlag(f) for f in row[idx].get("quality_flags", []))

        return Snapshot(
            instrument_id=instrument_id,
            market=market,
            as_of=as_of,
            last_price=tick[2] if tick else None,
            last_quantity=tick[3] if tick else None,
            best_bid=quote[2] if quote else None,
            best_ask=quote[3] if quote else None,
            spread=quote[4] if quote else None,
            freshness=now - as_of,
            quality_flags=tuple(dict.fromkeys(flags)),
        )

    def count_ticks(self, instrument_id: UUID, start: datetime, end: datetime) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "select count(*) from market.market_ticks "
                "where instrument_id = %s and event_time >= %s and event_time < %s",
                (str(instrument_id), start, end),
            )
            return int(cur.fetchone()[0])

    def find_sequence_gaps(
        self, instrument_id: UUID, start: datetime, end: datetime
    ) -> tuple[tuple[str, str], ...]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                select sequence_no
                from market.market_ticks
                where instrument_id = %s and event_time >= %s and event_time < %s
                  and sequence_no is not null
                order by event_time
                """,
                (str(instrument_id), start, end),
            )
            seqs = [r[0] for r in cur.fetchall()]
        return tuple(
            (a, b) for a, b in itertools.pairwise(seqs) if not _is_consecutive(a, b)
        )

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Fixture - 두 구현이 같은 계약을 지키는지 확인하는 데 쓴다
# ---------------------------------------------------------------------------

INSTRUMENT = UUID("10000000-0000-0000-0000-000000000001")


@dataclass(frozen=True)
class Fixture:
    """실행마다 격리되는 Fixture 좌표.

    Raw Table 은 append-only 다 - 마이그레이션의 market.reject_raw_mutation() 트리거가
    update/delete 를 거부한다("write a correction event instead"). 그래서 통합 점검은
    이전 데이터를 지우는 방식으로 반복 실행할 수 없다.

    대신 실행마다 instrument_id 와 provider_symbol 을 새로 만든다. provider_symbol 이
    source_event_id 해시 재료에 들어가므로 두 실행의 primary key 가 겹치지 않는다.
    Row 는 누적되지만 로컬 개발 DB 이고, 불변식을 우회하는 것보다 낫다.
    """

    instrument_id: UUID
    provider_symbol: str

    @classmethod
    def fixed(cls) -> Fixture:
        """InMemory 점검용. 매번 같은 좌표여도 상태가 프로세스 안에서만 산다."""
        return cls(instrument_id=INSTRUMENT, provider_symbol="005930")

    @classmethod
    def isolated(cls) -> Fixture:
        """통합 점검용. 실행마다 새 좌표를 만든다."""
        token = uuid4().hex[:8]
        return cls(instrument_id=uuid4(), provider_symbol=f"T{token}")


def _ref(fx: Fixture) -> InstrumentRef:
    return InstrumentRef(
        instrument_id=fx.instrument_id,
        provider="LS",
        provider_symbol=fx.provider_symbol,
        market=Market.KRX,
    )


def _times(offset_us: int) -> ObservationTimes:
    base = datetime(2026, 7, 30, 2, 0, 0, tzinfo=timezone.utc) + timedelta(microseconds=offset_us)
    return ObservationTimes(
        event_time=base,
        received_at=base + timedelta(milliseconds=8),
        observed_at=base + timedelta(milliseconds=12),
    )


def make_tick(fx: Fixture, seq: int, *, price: str = "70000") -> MarketTick:
    t = _times(seq * 1000)
    return MarketTick(
        times=t,
        instrument=_ref(fx),
        tr_code="S3_",
        session_type=SessionType.REGULAR,
        price=Decimal(price),
        quantity=Decimal(10),
        side=Side.BUY,
        sequence_no=str(seq),
        source_event_id=build_source_event_id(
            provider="LS",
            provider_symbol=fx.provider_symbol,
            event_time=t.event_time,
            payload_identity=str(seq),
        ),
        trace_id=uuid4(),
    )


def make_quote(fx: Fixture, seq: int) -> MarketQuote:
    t = _times(seq * 1000)
    return MarketQuote(
        times=t,
        instrument=_ref(fx),
        tr_code="H1_",
        session_type=SessionType.REGULAR,
        bid_prices=(Decimal(69900), Decimal(69800)),
        bid_sizes=(Decimal(100), Decimal(200)),
        ask_prices=(Decimal(70000), Decimal(70100)),
        ask_sizes=(Decimal(150), Decimal(50)),
        total_bid_size=Decimal(300),
        total_ask_size=Decimal(200),
        sequence_no=str(seq),
        source_event_id=build_source_event_id(
            provider="LS",
            provider_symbol=fx.provider_symbol,
            event_time=t.event_time,
            payload_identity=f"h{seq}",
        ),
    )


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

def _check_contract(repo: MarketDataRepository, label: str, fx: Fixture) -> None:
    """두 구현이 통과해야 하는 공통 계약. Timescale 구현도 이 함수를 그대로 쓴다."""
    iid = fx.instrument_id
    ticks = [make_tick(fx, i) for i in range(1, 6)]
    r = repo.write_ticks(ticks)
    assert (r.attempted, r.inserted, r.duplicates) == (5, 5, 0), f"{label}: 최초 적재 {r}"

    # 멱등 - 같은 Event 재적재는 전부 duplicates 다(재접속 시나리오)
    r2 = repo.write_ticks(ticks)
    assert (r2.attempted, r2.inserted, r2.duplicates) == (5, 0, 5), f"{label}: 재적재 {r2}"

    # 섞어서 넣으면 새 것만 들어간다
    r3 = repo.write_ticks([*ticks[:2], make_tick(fx, 6), make_tick(fx, 7)])
    assert (r3.inserted, r3.duplicates) == (2, 2), f"{label}: 부분 중복 {r3}"

    q = repo.write_quotes([make_quote(fx, i) for i in range(1, 4)])
    assert (q.inserted, q.duplicates) == (3, 0), f"{label}: 호가 적재 {q}"
    assert repo.write_quotes([make_quote(fx, 1)]).duplicates == 1, f"{label}: 호가 멱등"

    start = datetime(2026, 7, 30, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    assert repo.count_ticks(iid, start, end) == 7, f"{label}: count"

    snap = repo.get_snapshot(iid, now=datetime(2026, 7, 30, 2, 0, 1, tzinfo=timezone.utc))
    assert snap is not None
    assert snap.market is Market.KRX
    assert snap.last_price == Decimal(70000)
    assert snap.best_bid == Decimal(69900) and snap.best_ask == Decimal(70000)
    assert snap.spread == Decimal(100)
    assert snap.freshness > timedelta(0), f"{label}: freshness 가 음수/0 이다"

    # 없는 종목은 None 이다. 빈 Snapshot 을 만들어 주지 않는다
    assert repo.get_snapshot(uuid4()) is None

    # Sequence 1..7 을 연속으로 넣었으므로 Gap 이 없어야 한다
    assert repo.find_sequence_gaps(iid, start, end) == (), f"{label}: 없는 Gap 이 나왔다"
    print(f"  {label:28} OK")


def _check_write_result_invariant():
    try:
        WriteResult(5, 3, 1)
        raise AssertionError("집계 불일치가 통과했다")
    except ValueError:
        pass
    print("  WriteResult 불변식           OK")


def _check_sequence_gap_detection():
    fx = Fixture.fixed()
    repo = InMemoryMarketRepository()
    repo.write_ticks([make_tick(fx, 1), make_tick(fx, 2), make_tick(fx, 5)])
    start = datetime(2026, 7, 30, tzinfo=timezone.utc)
    gaps = repo.find_sequence_gaps(fx.instrument_id, start, start + timedelta(days=1))
    assert gaps == (("2", "5"),), f"Gap 탐지 실패: {gaps}"

    # 숫자가 아닌 Sequence 는 Gap 이라고 단정하지 않는다
    assert _is_consecutive("A1", "A9") is True
    assert _is_consecutive(None, "3") is True
    assert _is_consecutive("3", "4") is True
    assert _is_consecutive("3", "5") is False
    print("  Sequence Gap 탐지            OK")


def _run_integration() -> int:
    """살아 있는 컨테이너에 실제로 적재한다. TIMESCALE_DATABASE_URL 이 필요하다."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collectors"))
    from source_registry import load_project_env

    dsn = load_project_env().get("TIMESCALE_DATABASE_URL", "").strip()
    if not dsn:
        print("TIMESCALE_DATABASE_URL 이 비어 있다. docker compose up -d 후 .env 를 채우세요")
        return 1

    repo = TimescaleMarketRepository(dsn)
    fx = Fixture.isolated()
    try:
        # Raw Table 은 append-only 다. 이전 데이터를 지우지 않고 새 좌표로 적재한다.
        _check_contract(repo, "TimescaleMarketRepository", fx)

        # 압축·CAGG 가 붙은 실제 hypertable 인지 확인한다
        with repo._conn.cursor() as cur:
            cur.execute(
                "select count(*) from timescaledb_information.hypertables "
                "where hypertable_schema = 'market'"
            )
            assert int(cur.fetchone()[0]) == 7, "hypertable 7개가 아니다"
        print("  hypertable 7개 확인          OK")

        # execute_values page_size(500) 를 넘는 배치에서 삽입 건수가 정확한지 확인한다.
        # fetch=True 를 빼면 마지막 page 만 세어 없는 중복이 보고된다 - 실제로 겪은 버그다.
        big_fx = Fixture.isolated()
        big = [make_tick(big_fx, i) for i in range(1, 1201)]
        r = repo.write_ticks(big)
        assert (r.attempted, r.inserted, r.duplicates) == (1200, 1200, 0), f"대량 배치 {r}"
        r2 = repo.write_ticks(big)
        assert (r2.inserted, r2.duplicates) == (0, 1200), f"대량 배치 재적재 {r2}"
        print("  대량 배치(1200건) 카운트     OK")

        # append-only 불변식이 실제로 살아 있는지 확인한다. 이게 막히지 않으면
        # 마이그레이션의 reject_raw_mutation 트리거가 사라진 것이다.
        with repo._conn.cursor() as cur:
            try:
                cur.execute(
                    "delete from market.market_ticks where instrument_id = %s",
                    (str(fx.instrument_id),),
                )
                raise AssertionError("Raw Table DELETE 가 통과했다 - append-only 가 깨졌다")
            except Exception as e:
                if "immutable" not in str(e):
                    raise
        repo._conn.rollback()
        print("  append-only 불변식           OK")
        return 0
    finally:
        repo.close()


if __name__ == "__main__":
    if "--integration" in sys.argv:
        print(f"{REPOSITORY_VERSION} 통합 점검 (실제 TimescaleDB)")
        raise SystemExit(_run_integration())

    print(f"{REPOSITORY_VERSION} 자체 점검 (DB 없이)")
    _check_write_result_invariant()
    _check_contract(InMemoryMarketRepository(), "InMemoryMarketRepository", Fixture.fixed())
    _check_sequence_gap_detection()
    print("Repository 계약 통과. 통합 점검은 --integration")
