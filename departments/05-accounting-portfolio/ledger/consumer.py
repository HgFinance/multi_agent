#!/usr/bin/env python3
"""Ledger Consumer. Relay가 SENT로 찍은 `trading.fill.v1` 봉투를 분개로 만든다.

소유: 도현 (회계·포트폴리오본부)
근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md (Override v2.0) P0-1
        "Fill은 실제 Event/Consumer 경로에서 생성한다"
      docs/HEDGE_FUND_MASTER_PLAN.md 19.12(공식 숫자는 Accounting Engine이 계산한다)
      departments/05-accounting-portfolio/ledger/fill_consumer.py

**판정은 하나도 없다.** 순서만 안다 - 어떤 장부를 볼지 고르고, `fill_consumer.run_once`를
부르고, Mark가 없어 NAV가 거부되면 그 사실을 남긴다. 분개·평가·저장은 전부 그쪽이 한다.

**Mark가 없어도 분개는 멈추지 않는다.** `run_once`는 Projection을 저장한 뒤에 평가하므로
`ValuationError`가 나는 시점에 분개와 Position은 이미 커밋돼 있다. Position은 체결에서
나온 사실이라 시세와 무관하게 참이고, NAV만 없다. 여기서 예외를 루프 밖으로 흘리면
시세가 없다는 이유로 **체결 분개까지 멈춘다** - 그게 더 나쁘다.
대신 NAV를 추정해서 채우지도 않는다. 없는 것은 없는 채로 둔다.

**시세는 market-api가 준다**(2026-08-10 배선, D3). 주기마다 장부의 보유 종목을 읽어
`portfolio/mark_provider.py`로 Mark를 받아 넘긴다. 시세 조회 실패도 위와 같다 - 분개는
그대로 가고 NAV만 보류된다. 새로 산 종목은 Projection에 오른 다음 주기부터 평가된다
(첫 체결 주기에는 NAV 보류 한 번, 다음 주기에 스스로 풀린다).

한 장부의 실패가 다른 장부의 분개를 막지 않는다.

실행:      python departments/05-accounting-portfolio/ledger/consumer.py --serve
자체 점검: python departments/05-accounting-portfolio/ledger/consumer.py
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import UUID

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent / "portfolio")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fill_consumer  # noqa: E402
import mark_provider  # noqa: E402
from portfolio import MarkPrice, ValuationError  # noqa: E402
from repository import (  # noqa: E402
    ACCOUNTING_LEDGER_DATABASE_ROLE,
    LedgerPersistenceError,
    LedgerRepository,
)

# 종가 평가로 돌리려면 `1D`. 기본은 장중 체결가라 봉을 기다리지 않는다.
# 종가 봉은 valuation 시각과 몇 시간 벌어지므로 max_staleness도 같이 넓혀야 한다.
MARK_INTERVAL = os.environ.get("ACCOUNTING_MARK_INTERVAL") or None


def _log(message: str) -> None:
    print(f"[ledger-consumer] {message}", flush=True)


def active_books(repo: LedgerRepository) -> list[tuple[UUID, UUID]]:
    """분개 대상 장부. env로 고정하지 않는다 - Book이 늘면 설정이 조용히 낡는다."""
    with repo.cursor() as cur:
        cur.execute(
            "select fund_id, book_id from accounting.books "
            " where status = 'ACTIVE' order by book_id"
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def held_instruments(repo: LedgerRepository, fund_id: UUID, book_id: UUID) -> list[UUID]:
    """평가가 필요한 종목. Projection이 원천이고 수량 0은 뺀다.

    전량 매도한 종목까지 시세를 받으면 남의 부서 API에 쓸데없는 부하가 가고,
    그 종목의 시세가 없다는 이유로 NAV가 거부되는 일이 생긴다(보유하지도 않는데).
    `value_portfolio`도 수량이 있는 종목만 평가한다.
    """
    with repo.cursor() as cur:
        cur.execute(
            "select instrument_id from accounting.positions "
            " where fund_id = %s and book_id = %s and quantity <> 0 "
            " order by instrument_id",
            (fund_id, book_id),
        )
        return [row[0] for row in cur.fetchall()]


# ponytail: 마지막으로 남긴 실패 사유. 이유가 그대로면 다시 안 적는다 - 1초 주기라
#           안 그러면 종목 하나가 하루 8만 줄을 만든다. 이유가 바뀌거나 한 번
#           복구되면 다시 적는다. 프로세스 로컬이라 재시작하면 한 번은 다시 나온다.
_LAST_MISS: dict[tuple[UUID, UUID], str] = {}


def _log_miss(book_id: UUID, instrument_id: UUID, why: str) -> None:
    if _LAST_MISS.get((book_id, instrument_id)) == why:
        return
    _LAST_MISS[(book_id, instrument_id)] = why
    _log(f"Mark 없음 book={book_id} instrument={instrument_id}: {why}")


def resolve_marks(repo: LedgerRepository, fund_id: UUID, book_id: UUID,
                  as_of: datetime) -> dict[UUID, MarkPrice]:
    """이 장부의 보유 종목에 붙일 Mark. 못 받은 종목은 로그에 이유를 남긴다.

    **가격을 만들지 않는다.** 빠진 종목은 그대로 빠진 채 넘어가고 `value_portfolio`가
    그 사실로 NAV를 거부한다. 여기서 직전 스냅샷의 가격으로 메우면 시세가 끊긴 동안
    NAV가 움직이지 않으면서 정상처럼 보인다 - 그게 제일 나쁜 실패다.
    """
    instrument_ids = held_instruments(repo, fund_id, book_id)
    if not instrument_ids:
        return {}
    symbols = repo.symbols_for(instrument_ids, as_of=as_of)
    for instrument_id in instrument_ids:
        if instrument_id not in symbols:
            _log_miss(book_id, instrument_id,
                      f"symbol 미상 - reference.instrument_symbols에 "
                      f"{as_of.isoformat()} 시점 행이 없다")
    marks = mark_provider.fetch_marks(symbols, as_of, interval=MARK_INTERVAL)
    for instrument_id, why in marks.missing:
        _log_miss(book_id, instrument_id, why)
    # 복구된 종목은 기억에서 지운다 - 다음에 또 끊기면 그 사실이 다시 로그에 남아야 한다.
    for instrument_id in marks.prices:
        _LAST_MISS.pop((book_id, instrument_id), None)
    return marks.prices


def poll(repo: LedgerRepository, as_of: datetime, *,
         marks_for: Callable[..., dict[UUID, MarkPrice]] = resolve_marks,
         ) -> tuple[int, int]:
    """장부마다 SENT 체결을 분개한다. 돌려주는 값은 (본 장부 수, NAV까지 확정한 장부 수)."""
    seen = valued = 0
    for fund_id, book_id in active_books(repo):
        seen += 1
        try:
            marks = marks_for(repo, fund_id, book_id, as_of)
        except Exception as exc:  # noqa: BLE001
            # **시세 조회 실패가 분개를 멈추지 않는다.** market-api가 죽어도 체결은
            # 계속 들어오고, 그 사실을 원장에 못 적으면 나중에 대사가 통째로 어긋난다.
            # 빈 dict면 NAV만 보류된다.
            _log(f"시세 조회 실패 book={book_id}: {type(exc).__name__}: {exc}")
            marks = {}
        try:
            fill_consumer.run_once(repo, fund_id, book_id, marks, as_of)
            valued += 1
        except ValuationError as exc:
            # 분개와 Position은 커밋됐고 스냅샷만 없다. 파일 상단 참고.
            _log(f"NAV 보류 book={book_id}: {exc}")
        except Exception as exc:  # noqa: BLE001
            # **장부 하나의 실패가 다른 장부를 막지 않는다.** 봉투 하나가 깨졌거나
            # 한 장부만 잠겨 있을 때 뒤 장부까지 굶기면 장애 범위가 넓어진다.
            # 삼켜도 유실이 아니다 - ack는 분개 뒤에 찍히므로 그 SENT 행은 그대로
            # 남아 다음 주기에 다시 잡힌다. 대신 어느 장부인지 로그에 남긴다.
            _log(f"분개 실패 book={book_id}: {type(exc).__name__}: {exc}")
    return seen, valued


def serve() -> None:
    # required=True. DATABASE_URL이 없으면 인메모리로 뜨지 않고 여기서 멈춘다 -
    # 조용한 인메모리 후퇴가 "기록됐다고 믿은 분개"를 만든다(repository.from_env).
    configured_role = os.environ.get("ACCOUNTING_DATABASE_ROLE", "").strip()
    if configured_role != ACCOUNTING_LEDGER_DATABASE_ROLE:
        raise LedgerPersistenceError(
            "accounting ledger consumer requires "
            "ACCOUNTING_DATABASE_ROLE=svc_accounting_ledger"
        )
    repo = LedgerRepository.from_env(required=True)
    idle = max(float(os.environ.get("LEDGER_CONSUMER_POLL_SECONDS", "1.0")), 0.1)
    _log(f"start poll={idle}s marks={mark_provider.MARKET_API} "
         f"interval={MARK_INTERVAL or 'tick'}")

    while True:
        try:
            poll(repo, datetime.now(timezone.utc))
        except Exception as exc:  # noqa: BLE001
            # DB 순단으로 컨테이너를 죽이지 않는다. SENT 행은 ack 전까지 남아 있다.
            _log(f"cycle failed: {type(exc).__name__}: {exc}")
        time.sleep(idle)


def _self_check() -> None:
    global active_books
    from decimal import Decimal

    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    books = [(UUID(int=1), UUID(int=11)), (UUID(int=1), UUID(int=12))]
    real_books, real_run = active_books, fill_consumer.run_once
    active_books = lambda repo: books  # noqa: E731

    none = lambda repo, fund_id, book_id, as_of: {}  # noqa: E731 - 시세를 안 보는 주기

    try:
        # 1. 장부를 하나도 빠뜨리지 않는다.
        called: list[UUID] = []

        def _ok(repo, fund_id, book_id, marks, as_of, **kw):
            assert as_of == now, "as_of를 그대로 넘기지 않았다"
            called.append(book_id)
            return ([], None)

        fill_consumer.run_once = _ok
        assert poll(None, now, marks_for=none) == (2, 2)
        assert called == [b for _, b in books], "장부를 빠뜨렸다"

        # 2. Mark 없는 장부가 나머지 장부의 분개를 막지 않는다. 이 파일의 존재 이유다.
        def _first_has_no_mark(repo, fund_id, book_id, marks, as_of, **kw):
            if book_id == books[0][1]:
                raise ValuationError("평가 불가 - 가격 없음 1건")
            return ([], None)

        fill_consumer.run_once = _first_has_no_mark
        assert poll(None, now, marks_for=none) == (2, 1), "ValuationError가 루프를 끊었다"

        # 3. 한 장부의 저장 실패가 뒤 장부를 굶기지 않는다.
        reached: list[UUID] = []

        def _first_book_broken(repo, fund_id, book_id, marks, as_of, **kw):
            reached.append(book_id)
            if book_id == books[0][1]:
                raise RuntimeError("원장 DB 작업 실패")
            return ([], None)

        fill_consumer.run_once = _first_book_broken
        assert poll(None, now, marks_for=none) == (2, 1), "장부 하나의 실패가 주기를 끊었다"
        assert reached == [b for _, b in books], "앞 장부가 실패하자 뒤 장부를 건너뛰었다"

        # 4. 시세가 실제로 넘어간다. 예전에는 여기 빈 dict가 박혀 있었다(D3 대기).
        AAA = UUID(int=101)
        mark = MarkPrice(AAA, Decimal("70000"), now)
        seen_marks: list[dict] = []

        def _record(repo, fund_id, book_id, marks, as_of, **kw):
            seen_marks.append(marks)
            return ([], None)

        fill_consumer.run_once = _record
        assert poll(None, now, marks_for=lambda *a: {AAA: mark}) == (2, 2)
        assert seen_marks and seen_marks[0][AAA] is mark, "Mark가 평가까지 전달되지 않았다"

        # 5. **시세 조회 실패가 분개를 멈추지 않는다.** market-api가 죽어도 체결은
        #    계속 들어오고, 못 적은 분개는 나중에 대사를 통째로 어긋나게 한다.
        seen_marks.clear()

        def _market_down(repo, fund_id, book_id, as_of):
            raise ConnectionError("market-api 연결 실패")

        assert poll(None, now, marks_for=_market_down) == (2, 2), "시세 실패가 분개를 막았다"
        assert seen_marks == [{}, {}], "실패한 조회가 빈 Mark로 떨어지지 않았다"
    finally:
        active_books, fill_consumer.run_once = real_books, real_run

    # 6. 보유 종목이 없으면 시세를 조회하지 않는다(전량 매도한 장부).
    real_held = globals()["held_instruments"]
    globals()["held_instruments"] = lambda repo, fund_id, book_id: []
    try:
        assert resolve_marks(None, books[0][0], books[0][1], now) == {}
    finally:
        globals()["held_instruments"] = real_held

    # 7. 같은 실패를 매 주기 다시 적지 않는다. 1초 주기라 로그가 잠긴다.
    #    이유가 바뀌거나 한 번 복구되면 다시 적는다.
    lines: list[str] = []
    real_log = globals()["_log"]
    globals()["_log"] = lines.append
    try:
        _LAST_MISS.clear()
        book, inst = books[0][1], UUID(int=201)
        for _ in range(3):
            _log_miss(book, inst, "/snapshot HTTP 404")
        assert len(lines) == 1, f"같은 사유를 {len(lines)}번 적었다"
        _log_miss(book, inst, "/snapshot HTTP 500")
        assert len(lines) == 2, "사유가 바뀌었는데 안 적었다"
        _LAST_MISS.pop((book, inst))            # 복구된 경우
        _log_miss(book, inst, "/snapshot HTTP 500")
        assert len(lines) == 3, "복구 후 재발을 안 적었다"
    finally:
        globals()["_log"] = real_log
        _LAST_MISS.clear()

    print("ok - Ledger Consumer 7개 점검 통과")


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve()
    else:
        _self_check()
