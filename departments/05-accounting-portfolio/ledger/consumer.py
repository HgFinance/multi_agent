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
나온 사실이라 시세와 무관하게 참이고, NAV만 없다(D3, market-api 대기). 여기서 예외를
루프 밖으로 흘리면 시세가 없다는 이유로 **체결 분개까지 멈춘다** - 그게 더 나쁘다.
대신 NAV를 추정해서 채우지도 않는다. 없는 것은 없는 채로 둔다.

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
from uuid import UUID

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent / "portfolio")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fill_consumer  # noqa: E402
from portfolio import MarkPrice, ValuationError  # noqa: E402
from repository import LedgerRepository  # noqa: E402

# ponytail: Mark 공급원이 없다(D3 - market-api 종가 대기). 빈 dict면 보유 종목이 생기는
# 순간부터 NAV가 거부되고 분개·Position만 진행된다. 틀린 NAV를 만드는 것보다 낫고,
# market-api가 붙으면 이 자리에서 종가를 읽어 채운다(`portfolio.MarkPrice`).
MARKS: dict[UUID, MarkPrice] = {}


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


def poll(repo: LedgerRepository, marks: dict[UUID, MarkPrice],
         as_of: datetime) -> tuple[int, int]:
    """장부마다 SENT 체결을 분개한다. 돌려주는 값은 (본 장부 수, NAV까지 확정한 장부 수)."""
    seen = valued = 0
    for fund_id, book_id in active_books(repo):
        seen += 1
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
    repo = LedgerRepository.from_env(required=True)
    idle = max(float(os.environ.get("LEDGER_CONSUMER_POLL_SECONDS", "1.0")), 0.1)
    _log(f"start poll={idle}s")

    while True:
        try:
            poll(repo, MARKS, datetime.now(timezone.utc))
        except Exception as exc:  # noqa: BLE001
            # DB 순단으로 컨테이너를 죽이지 않는다. SENT 행은 ack 전까지 남아 있다.
            _log(f"cycle failed: {type(exc).__name__}: {exc}")
        time.sleep(idle)


def _self_check() -> None:
    global active_books

    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    books = [(UUID(int=1), UUID(int=11)), (UUID(int=1), UUID(int=12))]
    real_books, real_run = active_books, fill_consumer.run_once
    active_books = lambda repo: books  # noqa: E731

    try:
        # 1. 장부를 하나도 빠뜨리지 않는다.
        called: list[UUID] = []

        def _ok(repo, fund_id, book_id, marks, as_of, **kw):
            assert as_of == now, "as_of를 그대로 넘기지 않았다"
            called.append(book_id)
            return ([], None)

        fill_consumer.run_once = _ok
        assert poll(None, {}, now) == (2, 2)
        assert called == [b for _, b in books], "장부를 빠뜨렸다"

        # 2. Mark 없는 장부가 나머지 장부의 분개를 막지 않는다. 이 파일의 존재 이유다.
        def _first_has_no_mark(repo, fund_id, book_id, marks, as_of, **kw):
            if book_id == books[0][1]:
                raise ValuationError("평가 불가 - 가격 없음 1건")
            return ([], None)

        fill_consumer.run_once = _first_has_no_mark
        assert poll(None, {}, now) == (2, 1), "ValuationError가 루프를 끊었다"

        # 3. 한 장부의 저장 실패가 뒤 장부를 굶기지 않는다.
        reached: list[UUID] = []

        def _first_book_broken(repo, fund_id, book_id, marks, as_of, **kw):
            reached.append(book_id)
            if book_id == books[0][1]:
                raise RuntimeError("원장 DB 작업 실패")
            return ([], None)

        fill_consumer.run_once = _first_book_broken
        assert poll(None, {}, now) == (2, 1), "장부 하나의 실패가 주기를 끊었다"
        assert reached == [b for _, b in books], "앞 장부가 실패하자 뒤 장부를 건너뛰었다"

        # 4. Mark 공급원이 아직 없다는 사실이 코드에 남아 있어야 한다(D3 대기).
        assert MARKS == {}, "Mark를 하드코딩했다 - 시세는 market-api가 준다"
    finally:
        active_books, fill_consumer.run_once = real_books, real_run

    print("ok - Ledger Consumer 4개 점검 통과")


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve()
    else:
        _self_check()
