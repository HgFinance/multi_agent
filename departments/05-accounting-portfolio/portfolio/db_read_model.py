#!/usr/bin/env python3
"""`/ui/snapshot`의 회계 구간을 Canonical 표에서 읽는다.

소유: 도현 (회계·포트폴리오본부)
근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 5.1~5.2,
      supabase/migrations/20260804000500_api_accounting_read_views.sql

`ui_read_model.py`가 도메인 객체에서 만드는 것과 **같은 모양**을 DB에서 만든다.
모양이 갈라지면 화면이 원천에 따라 다르게 동작하게 되고, 그 순간 Read Model이
두 개가 된다. 그래서 필드 이름과 문자열 규약(금액은 str)을 그대로 따른다.

**트레이딩 구간은 여기 없다.** `execution.orders`가 아직 0행이라 만들면 늘 빈
화면을 실데이터인 척 보여주게 된다. 그 구간은 Scripted Loop로 남고 `sources`가
어느 쪽인지 밝힌다 - DEMO와 PAPER가 같은 화면에서 말없이 섞이는 것이
계획 4절이 금지하는 것이다.

자체 점검: python departments/05-accounting-portfolio/portfolio/db_read_model.py
           (DATABASE_URL 필요)
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "ledger"))

from ledger import decimal_str
from repository import LedgerRepository

MAX_ROWS = 50


def _d(value) -> str | None:
    return None if value is None else decimal_str(Decimal(str(value)))


def build_accounting_sections(repo: LedgerRepository, book_id: UUID) -> dict | None:
    """`portfolio`와 `ledger` 구간을 DB에서 만든다. 스냅샷이 없으면 None.

    None을 돌려주는 것이 의도다 - 평가된 적 없는 장부에 0원 NAV를 만들어 주면
    화면은 "자산이 0"과 "아직 평가 안 함"을 구분할 수 없다.
    """
    with repo.cursor() as cur:
        cur.execute(
            """
            select as_of, nav, cash, positions, gross_exposure, net_exposure,
                   quality_status, currency
              from api.portfolio_snapshot_latest where book_id = %s
            """,
            (book_id,),
        )
        snap = cur.fetchone()
        if snap is None:
            return None
        as_of, nav, cash, positions, gross, net, quality, currency = snap

        cur.execute(
            """
            select account_code, balance from api.ledger_balances
             where book_id = %s order by account_code
            """,
            (book_id,),
        )
        balances = {code: Decimal(str(amount)) for code, amount in cur.fetchall()}

        cur.execute(
            """
            select count(*), count(*) filter (where status = 'REVERSED')
              from accounting.journals where book_id = %s
            """,
            (book_id,),
        )
        journal_count, reversed_count = cur.fetchone()

        # 종목 표시용 symbol. market-api는 symbol로 말하고 우리는 UUID로 말한다.
        cur.execute(
            "select instrument_id::text, symbol, display_name from api.position_holdings "
            "where book_id = %s", (book_id,))
        labels = {row[0]: {"symbol": row[1], "display_name": row[2]} for row in cur.fetchall()}

    nav = Decimal(str(nav)) if nav is not None else Decimal(0)
    securities = sum((Decimal(p["market_value"]) for p in positions), Decimal(0))
    unrealized = sum((Decimal(p["unrealized_pnl"]) for p in positions), Decimal(0))
    total = sum(balances.values(), Decimal(0))

    return {
        "portfolio": {
            "as_of": as_of.isoformat(),
            "nav": _d(nav),
            "cash": _d(next(iter(cash.values()), "0")),
            "securities_value": _d(securities),
            "gross_exposure": _d(gross),
            "net_exposure": _d(net),
            # 손익 계정은 대변이 이익이라 부호를 뒤집는다(portfolio.value_portfolio와 동일).
            "realized_pnl": _d(-balances.get("4000", Decimal(0))),
            "unrealized_pnl": _d(unrealized),
            "fees": _d(balances.get("5000", Decimal(0))),
            "taxes": _d(balances.get("5100", Decimal(0))),
            # WARN이면 미확정 봉으로 평가된 NAV다. 화면이 판단하도록 그대로 싣는다.
            "quality_status": quality,
            "currency": currency,
            "positions": [
                {
                    "instrument_id": p["instrument_id"],
                    "symbol": labels.get(p["instrument_id"], {}).get("symbol"),
                    "display_name": labels.get(p["instrument_id"], {}).get("display_name"),
                    "quantity": _d(p["quantity"]),
                    "average_cost": _d(p["average_cost"]),
                    "mark_price": _d(p["mark_price"]),
                    "mark_as_of": p["mark_as_of"],
                    "mark_is_final": bool(p.get("mark_is_final", False)),
                    "market_value": _d(p["market_value"]),
                    "unrealized_pnl": _d(p["unrealized_pnl"]),
                    "weight": (_d(Decimal(p["market_value"]) / nav) if nav > 0 else None),
                }
                for p in positions[:MAX_ROWS]
            ],
        },
        "ledger": {
            "journal_count": journal_count,
            "reversal_count": reversed_count,
            "trial_balance_sum": _d(total),
            "balanced": total == 0,
            "accounts": {code: _d(amount) for code, amount in sorted(balances.items())},
        },
    }


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env")
    except ModuleNotFoundError:
        pass

    repo = LedgerRepository.from_env()
    if repo is None:
        print("skip - DATABASE_URL이 없다")
        raise SystemExit(0)

    fund_id, book_id = repo.bootstrap("ACC01-PAPER", "MAIN")
    sections = build_accounting_sections(repo, book_id)
    assert sections is not None, "ACC-01 Fixture 스냅샷이 없다. fill_consumer.py를 먼저 돌린다"

    # 1. ui_read_model과 같은 필드 이름을 쓴다. 원천이 달라도 화면 계약은 하나다
    from portfolio import PortfolioSnapshot  # noqa: E402
    from ui_read_model import _portfolio as _mem_portfolio  # noqa: E402

    sample = PortfolioSnapshot(fund_id=fund_id, book_id=book_id,
                               as_of=__import__("datetime").datetime.now(
                                   __import__("datetime").timezone.utc),
                               cash=Decimal(0), receivable=Decimal(0), payable=Decimal(0),
                               realized_pnl=Decimal(0), fees=Decimal(0), taxes=Decimal(0))
    memory_keys = set(_mem_portfolio(sample))
    missing = memory_keys - set(sections["portfolio"])
    assert not missing, f"인메모리 Read Model에만 있는 필드: {missing}"

    # 2. 금액은 전부 문자열이다 (JSON number면 Decimal이 깨진다)
    assert isinstance(sections["portfolio"]["nav"], str)
    for pos in sections["portfolio"]["positions"]:
        for field in ("quantity", "average_cost", "mark_price", "market_value"):
            assert isinstance(pos[field], str), field

    # 3. 차대는 항상 0이다. 반대 분개를 뺀 채로 세면 여기서 깨진다
    assert sections["ledger"]["balanced"] is True, sections["ledger"]

    # 4. NAV 항등식 - 현금 + 평가금액
    p = sections["portfolio"]
    assert Decimal(p["nav"]) == Decimal(p["cash"]) + Decimal(p["securities_value"]), p

    # 5. symbol이 붙는다. market-api가 쓰는 이름이 화면까지 온다
    assert p["positions"] and p["positions"][0]["symbol"], p["positions"]

    # 6. 평가 품질이 숨겨지지 않는다
    assert p["quality_status"] in ("PASS", "WARN", "FAIL", "STALE"), p["quality_status"]

    # 7. 스냅샷이 없는 장부는 None이다. 0원 NAV를 지어내지 않는다
    _, empty_book = repo.bootstrap("ACC01-PAPER", "NEVER-VALUED")
    assert build_accounting_sections(repo, empty_book) is None, \
        "평가된 적 없는 장부에 NAV를 만들어 줬다"

    print(f"ok - DB Read Model 7개 영역 점검 통과 "
          f"(NAV {p['nav']} {p['currency']}, quality {p['quality_status']}, "
          f"보유 {len(p['positions'])}종목)")
