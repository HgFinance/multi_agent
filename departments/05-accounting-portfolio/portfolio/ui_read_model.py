#!/usr/bin/env python3
"""AI Office용 Trading·Portfolio Read Model (F18 중 도현 담당분).

소유: 도현 (트레이딩 + 회계·포트폴리오)
근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 3.2(6·7), 5.1, 9(UI-0), 11
      "도현님 | OMS, Fill, Position, Ledger와 NAV Read Model 제공"

화면은 공식 장부나 위험 상태를 **계산하지 않는다**(계획 1절). 여기서 확정된 값을
투영 가능한 형태로 넘겨줄 뿐이다. 그래서 이 모듈은 새 수치를 만들지 않는다 -
OMS/Ledger/Portfolio가 이미 확정한 것을 옮기기만 한다.

두 가지를 지킨다.

1. **금액·수량은 문자열이다.** JSON number는 IEEE754 double이라 Decimal이 깨진다.
   가격·수량·통화에 float를 쓰지 않는다는 규약이 네트워크 경계에서도 유지돼야 한다.
   프론트는 표시만 하므로 문자열로 충분하고, 계산이 필요해지면 그때 서버에서 한다.
2. **mode를 항상 싣는다.** DEMO/PAPER 데이터가 같은 화면에서 섞이지 않아야 한다
   (계획 4절). 이 Fixture는 Scripted Loop 산출물이므로 DEMO다.

자체 점검: python departments/05-accounting-portfolio/portfolio/ui_read_model.py
Fixture 생성: 같은 명령에 --write 를 붙이면 ai-office/app/ops/ 에 기록한다.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from portfolio import PortfolioSnapshot  # noqa: E402

SCHEMA_VERSION = 1

# 계획 5.3의 UI Event Envelope와 같은 규칙을 Snapshot에도 적용한다.
# 대용량 원문(Trace, Backtest, 전체 분개)은 넣지 않고 개수와 참조만 넣는다.
MAX_ROWS = 50


def _d(value: Decimal | None) -> str | None:
    """Decimal -> 문자열. None은 그대로 둔다(0과 구분해야 한다)."""
    return None if value is None else str(value)


def _portfolio(snapshot: PortfolioSnapshot) -> dict:
    nav = snapshot.nav
    return {
        "as_of": snapshot.as_of.isoformat(),
        "nav": _d(nav),
        "cash": _d(snapshot.cash),
        "securities_value": _d(snapshot.securities_value),
        "gross_exposure": _d(snapshot.gross_exposure),
        "net_exposure": _d(snapshot.net_exposure),
        "realized_pnl": _d(snapshot.realized_pnl),
        "unrealized_pnl": _d(snapshot.unrealized_pnl),
        "fees": _d(snapshot.fees),
        "taxes": _d(snapshot.taxes),
        "positions": [
            {
                "instrument_id": str(p.instrument_id),
                "quantity": _d(p.quantity),
                "average_cost": _d(p.average_cost),
                "mark_price": _d(p.mark_price),
                "mark_as_of": p.mark_as_of.isoformat(),
                "market_value": _d(p.market_value),
                "unrealized_pnl": _d(p.unrealized_pnl),
                # 비중은 NAV가 양수일 때만 정의된다. 0 NAV에서 0%로 보이면 오해를 부른다.
                "weight": _d(p.market_value / nav) if nav > 0 else None,
            }
            for p in snapshot.positions[:MAX_ROWS]
        ],
    }


def _trading(oms) -> dict:
    """OMS의 두 스트림을 그대로 투영한다.

    Intent와 Broker Order를 한 줄로 합치지 않는다. v1.2에서 둘을 분리한 이유가
    화면에서도 유지돼야 한다 - 리스크본부 거부와 브로커 거부는 다른 사건이다.
    """
    intents = [
        {
            "order_intent_id": str(rec.order_intent_id),
            "state": str(rec.state),
            "requested_quantity": _d(rec.requested_quantity),
            "risk_decision_id": str(rec.risk_decision_id) if rec.risk_decision_id else None,
            "risk_approved_qty": _d(rec.risk_approved_qty),
            "valid_until": rec.valid_until.isoformat(),
        }
        for rec in list(oms.store.intents.values())[:MAX_ROWS]
    ]
    orders = [
        {
            "order_id": str(o.order_id),
            "order_intent_id": str(o.order_intent_id),
            "client_order_id": o.client_order_id,
            "broker_order_id": o.broker_order_id,
            "broker_adapter": o.broker_adapter,
            "state": str(o.state),
            "side": str(o.side),
            "instrument_id": str(o.instrument_id),
            "requested_quantity": _d(o.requested_quantity),
            "filled_quantity": _d(o.filled_quantity),
            "leaves_quantity": _d(o.leaves_quantity),
            "limit_price": _d(o.limit_price),
            "average_fill_price": _d(o.average_fill_price),
            "fill_count": len(o.fills),
            "is_terminal": o.is_terminal,
        }
        for o in list(oms.store.orders.values())[:MAX_ROWS]
    ]
    return {
        "intents": intents,
        "orders": orders,
        # UNKNOWN 주문이 있으면 신규 주문이 막힌다. 화면 상단에 띄울 값이라 따로 낸다.
        "blocked_by_unknown": any(o["state"] == "UNKNOWN" for o in orders),
    }


def _ledger(ledger) -> dict:
    """원장은 개수와 균형만 낸다. 분개 전문은 Snapshot에 싣지 않는다."""
    balances = ledger.trial_balance()
    return {
        "journal_count": len(ledger.journals),
        "reversal_count": sum(1 for j in ledger.journals if j.reversal_of is not None),
        # 이 값이 0이 아니면 원장이 깨진 것이다. 화면이 판단하지 않도록 결과만 준다.
        "trial_balance_sum": _d(sum(balances.values(), Decimal(0))),
        "balanced": sum(balances.values(), Decimal(0)) == 0,
        "accounts": {code: _d(amount) for code, amount in sorted(balances.items())},
    }


def build_ui_snapshot(
    *,
    oms,
    ledger,
    snapshot: PortfolioSnapshot,
    mode: str = "DEMO",
    snapshot_version: int = 1,
    server_time: datetime | None = None,
) -> dict:
    """AI Office가 읽는 Trading·Portfolio Snapshot 한 장.

    mode는 DEMO | PAPER | LIVE다. Scripted Loop 산출물이면 DEMO이고,
    실제 Paper Backend에 붙으면 PAPER가 된다 - 그 판단은 호출자가 한다.
    """
    if mode not in ("DEMO", "PAPER", "LIVE"):
        raise ValueError(f"알 수 없는 mode: {mode}")

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "snapshot_version": snapshot_version,
        "server_time": (server_time or datetime.now(timezone.utc)).isoformat(),
        "fund_id": str(ledger.fund_id),
        "book_id": str(ledger.book_id),
        "portfolio": _portfolio(snapshot),
        "trading": _trading(oms),
        "ledger": _ledger(ledger),
    }


if __name__ == "__main__":
    # 관통 테스트의 루프를 그대로 돌려 실제 수치를 얻는다.
    # 화면용 숫자를 따로 지어내지 않기 위해서다 - 손으로 쓴 Fixture는
    # 백엔드가 바뀌어도 안 깨지므로 거짓말이 오래 남는다.
    ROOT = _HERE.parents[2]
    sys.path.insert(0, str(ROOT / "tests" / "e2e"))
    from test_paper_loop import PaperLoopTest  # noqa: E402

    loop = PaperLoopTest("test_full_loop_signal_to_nav")
    loop.setUp()
    intent = loop.build_intent(loop.signal(), loop.snapshot())
    _, order = loop.route(intent)
    loop.fill_completely(order)
    loop.post_fills_to_ledger(order)
    final = loop.snapshot()

    doc = build_ui_snapshot(oms=loop.oms, ledger=loop.ledger, snapshot=final)

    # 1. 계약 필수 필드
    for key in ("schema_version", "mode", "snapshot_version", "server_time",
                "fund_id", "portfolio", "trading", "ledger"):
        assert key in doc, f"필수 필드 누락: {key}"
    assert doc["mode"] == "DEMO", "Scripted Loop 산출물은 DEMO여야 한다"

    # 2. 금액은 전부 문자열이다. JSON number로 나가면 Decimal이 깨진다
    raw = json.dumps(doc, ensure_ascii=False)
    assert isinstance(doc["portfolio"]["nav"], str)
    for pos in doc["portfolio"]["positions"]:
        for field in ("quantity", "average_cost", "mark_price", "market_value"):
            assert isinstance(pos[field], str), f"{field}가 문자열이 아니다"
    parsed = json.loads(raw)
    assert Decimal(parsed["portfolio"]["nav"]) == final.nav, "직렬화에서 NAV가 변했다"

    # 3. 원장 균형과 주문 상태가 그대로 실린다
    assert doc["ledger"]["balanced"] is True
    assert doc["ledger"]["journal_count"] == len(loop.ledger.journals)
    assert len(doc["trading"]["orders"]) == 1
    assert doc["trading"]["orders"][0]["state"] == "FILLED"
    assert doc["trading"]["orders"][0]["filled_quantity"] == str(intent.quantity)
    assert doc["trading"]["blocked_by_unknown"] is False

    # 4. Intent와 Broker Order를 합치지 않는다 (v1.2 분리가 화면까지 유지된다)
    assert doc["trading"]["intents"][0]["state"] == "READY_TO_SUBMIT"
    assert doc["trading"]["orders"][0]["state"] == "FILLED"

    # 5. 포지션 비중은 NAV 대비다
    pos = doc["portfolio"]["positions"][0]
    assert Decimal(pos["weight"]) == Decimal(pos["market_value"]) / final.nav

    # 6. 알 수 없는 mode는 거부한다
    try:
        build_ui_snapshot(oms=loop.oms, ledger=loop.ledger, snapshot=final, mode="REAL")
        raise AssertionError("알 수 없는 mode가 통과했다")
    except ValueError:
        pass

    if "--write" in sys.argv:
        out = ROOT / "ai-office" / "app" / "ops" / "trading-snapshot.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out.relative_to(ROOT)}")

    print("ok - UI Read Model 6개 영역 점검 통과")
