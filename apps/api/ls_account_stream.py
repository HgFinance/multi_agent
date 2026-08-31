#!/usr/bin/env python3
"""브로커 계좌 실시간 파이프라인. **읽기 전용.**

소유: 도현 (트레이딩본부)
근거: docs/06-integrations/ls-openapi/03-stock/16-9a2800c3.md 24~28번 TR(SC0~SC4),
      docs/06-integrations/ls-openapi/03-stock/14-37d22d4d.md 11번 TR(t0424)

▶ 파이프라인

    LS 계좌
      └ WebSocket 계좌등록 (tr_type = 1)
          ├ 주문 접수/정정/취소  →  Order State 변경
          └ 체결 발생            →  Position Event 발생
                                     └ 로컬 계좌 상태 변경
                                         └ t0424 REST 확인 (chegb = 2)
                                             └ LS 실제잔고와 동기화

  체결이 오면 로컬 포지션을 **먼저** 움직이고, 곧바로 t0424로 브로커 잔고를
  확인한다. 두 값이 다르면 브로커 값을 정본으로 쓰되 **차이를 지우지 않고**
  `drift`로 남긴다 — 조용히 덮어쓰면 유실된 체결과 정상 상태가 같아 보인다.

▶ LS는 여기서 끝난다
  이 모듈 밖으로 `SC0`·`t0424`·`accno1` 같은 LS 어휘를 내보내지 않는다.
  화면 계약은 접수/체결/정정/취소/거부라는 도메인 어휘뿐이고, 브로커를 바꿔도
  화면이 따라 바뀌지 않는다.

▶ 무엇이 아닌가
  주문을 내지 않는다. 등록(tr_type="1")과 조회 TR만 부른다.
  브로커가 자기 장부로 말해 주는 값이라 **공식 원장이 아니다** — 응답에
  `authoritative: false`를 항상 싣는다(`account_snapshot.py`와 같은 규칙).

자체 점검:
    python apps/api/ls_account_stream.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# 자격 해석(PAPER/LIVE 접미사 규칙)은 리스크본부가 이미 갖고 있다(동규 소유).
# 같은 규칙을 두 벌 두면 한쪽만 고쳐졌을 때 Live 자격으로 Paper에 붙는다.
_LS_PATH = ROOT / "departments" / "03-risk" / "integrations"
if str(_LS_PATH) not in sys.path:
    sys.path.insert(0, str(_LS_PATH))

try:
    from .ledger_store import STORE as LEDGER_STORE
except ImportError:  # pragma: no cover - direct module self-check compatibility
    from ledger_store import STORE as LEDGER_STORE

try:
    from .ls_accounting_evidence import normalize_ls_accounting_evidence
except ImportError:  # pragma: no cover - direct module self-check compatibility
    from ls_accounting_evidence import normalize_ls_accounting_evidence

try:
    from .conditional_rule_workflow import (
        ConditionalRuleUnavailable,
        conditional_rule_repository,
    )
    from .user_order_workflow import (
        BrokerOrderCorrelation,
        UserOrderWorkflowUnavailable,
        user_order_repository,
    )
except ImportError:  # pragma: no cover - direct module self-check compatibility
    from conditional_rule_workflow import (  # type: ignore[no-redef]
        ConditionalRuleUnavailable,
        conditional_rule_repository,
    )
    from user_order_workflow import (  # type: ignore[no-redef]
        BrokerOrderCorrelation,
        UserOrderWorkflowUnavailable,
        user_order_repository,
    )


@asynccontextmanager
async def _portfolio_live_lifespan(_app: Any) -> AsyncIterator[None]:
    await _start_accounting_evidence_refresh()
    try:
        yield
    finally:
        await _stop_accounting_evidence_refresh()


router = APIRouter(tags=["portfolio-live"], lifespan=_portfolio_live_lifespan)
KST = timezone(timedelta(hours=9))


def _env_flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


ENABLE_LS_ORDER_EVENTS = _env_flag("ENABLE_LS_ORDER_EVENTS")
# 시장 순위는 REST만 사용한다. 계좌 WebSocket을 켜지 않고도 시장 데이터만
# 복구할 수 있게 별도 opt-in으로 둔다.
ENABLE_LS_MARKET_DATA = _env_flag("ENABLE_LS_MARKET_DATA")
# 거래내역·잔고 조회도 REST만 사용한다. 실시간 주문 이벤트와 분리한다.
ENABLE_LS_ACCOUNT_DATA = _env_flag("ENABLE_LS_ACCOUNT_DATA")
PORTFOLIO_LIVE_MODE = os.getenv("PORTFOLIO_LIVE_MODE", "broker").strip().casefold()
MAX_EVENTS = int(os.getenv("LS_ORDER_EVENTS_MAX", "200"))
# 거래내역 TR은 초당 1건이다. 화면이 3초마다 폴링해도 브로커를 때리지 않도록
# 응답을 캐시한다 - 확정된 과거 거래라 자주 바뀌지 않는다.
LEDGER_DAYS = int(os.getenv("ACCOUNTING_LEDGER_DAYS", "30"))
LEDGER_CACHE_SECONDS = int(os.getenv("ACCOUNTING_LEDGER_CACHE_SECONDS", "60"))
# The dashboard polls every 3 seconds, while one broker-backed read can take
# longer than that. Cache only the read-only ledger/history portion long enough
# to prevent overlapping TR refreshes; the live FEED events are still merged on
# every response, so new order events are not hidden behind this cache.
ORDER_HISTORY_CACHE_SECONDS = int(os.getenv("LS_ORDER_HISTORY_CACHE_SECONDS", "10"))
ACCOUNT_PROJECTION_RESYNC_SECONDS = max(
    float(os.getenv("LS_ACCOUNT_PROJECTION_RESYNC_SECONDS", "30")), 5.0
)
MARKET_RANKING_CACHE_SECONDS = int(os.getenv("LS_MARKET_RANKING_CACHE_SECONDS", "15"))
MARKET_RANKING_LIMIT = 5
# 회계 Agent는 셸·웹 도구가 없으므로 이 프로세스가 12개 계좌 TR을 미리 읽고
# 정규화한 증거를 붙인다. 정기 갱신은 초당 1건 제한을 지키며 백그라운드에서 돈다.
ACCOUNTING_EVIDENCE_CACHE_SECONDS = max(
    30, int(os.getenv("LS_ACCOUNTING_EVIDENCE_CACHE_SECONDS", "300"))
)
ACCOUNTING_EVIDENCE_REFRESH_SECONDS = max(
    60, int(os.getenv("LS_ACCOUNTING_EVIDENCE_REFRESH_SECONDS", "300"))
)
ACCOUNTING_EVIDENCE_MAX_PAGES = max(
    1, min(50, int(os.getenv("LS_ACCOUNTING_EVIDENCE_MAX_PAGES", "10")))
)
ACCOUNTING_EVIDENCE_DAYS = max(
    1, min(365, int(os.getenv("LS_ACCOUNTING_EVIDENCE_DAYS", "30")))
)

MARKET_RANKINGS: dict[str, dict[str, Any]] = {
    "volume": {
        "tr_cd": "t1452",
        "label": "거래량 상위",
        "metric_label": "거래량",
        "out_block": "t1452OutBlock1",
        "payload": {
            "t1452InBlock": {
                "gubun": "0",
                "jnilgubun": "0",
                "sdiff": 0,
                "ediff": 0,
                "jc_num": 0,
                "sprice": 0,
                "eprice": 0,
                "volume": 0,
                "idx": 0,
            }
        },
    },
    "change": {
        "tr_cd": "t1441",
        "label": "등락률 상위",
        "metric_label": "등락률",
        "out_block": "t1441OutBlock1",
        "payload": {
            "t1441InBlock": {
                "gubun1": "0",
                "gubun2": "0",
                "gubun3": "0",
                "jc_num": 0,
                "sprice": 0,
                "eprice": 0,
                "volume": 0,
                "idx": 0,
                "jc_num2": 0,
                "exchgubun": "0",
            }
        },
    },
    "amount": {
        "tr_cd": "t1463",
        "label": "거래대금 상위",
        "metric_label": "거래대금",
        "out_block": "t1463OutBlock1",
        "payload": {
            "t1463InBlock": {
                "gubun": "0",
                "jnilgubun": "0",
                "jc_num": 0,
                "sprice": 0,
                "eprice": 0,
                "volume": 0,
                "idx": 0,
                "jc_num2": 0,
                "exchgubun": "0",
            }
        },
    },
    "market_cap": {
        "tr_cd": "t1444",
        "label": "시가총액 상위",
        "metric_label": "시가총액",
        "out_block": "t1444OutBlock1",
        "payload": {"t1444InBlock": {"upcode": "001", "idx": 0}},
    },
    "volume_surge": {
        "tr_cd": "t1466",
        "label": "전일 동시간 대비 거래급증",
        "metric_label": "거래급증률",
        "out_block": "t1466OutBlock1",
        "payload": {"t1466InBlock": {"gubun": "0", "type1": "0", "type2": "0", "jc_num": 0, "sprice": 0, "eprice": 0, "volume": 0, "idx": 0, "jc_num2": 0, "exchgubun": "0"}},
    },
    "after_hours_change": {
        "tr_cd": "t1481",
        "label": "시간외 등락률 상위",
        "metric_label": "등락률",
        "out_block": "t1481OutBlock1",
        "payload": {"t1481InBlock": {"gubun1": "0", "gubun2": "0", "jongchk": "0", "volume": "0", "idx": 0}},
    },
    "after_hours_volume": {
        "tr_cd": "t1482",
        "label": "시간외 거래량 상위",
        "metric_label": "거래량",
        "out_block": "t1482OutBlock1",
        "payload": {"t1482InBlock": {"sort_gbn": 1, "gubun": "0", "jongchk": "0", "idx": 0}},
    },
    "expected_volume": {
        "tr_cd": "t1489",
        "label": "예상 체결량 상위",
        "metric_label": "예상거래량",
        "out_block": "t1489OutBlock1",
        "payload": {"t1489InBlock": {"gubun": "0", "jgubun": "0", "jongchk": "0", "idx": 0, "yesprice": 0, "yeeprice": 0, "yevolume": 0}},
    },
    "single_price_change": {
        "tr_cd": "t1492",
        "label": "단일가 예상 등락률 상위",
        "metric_label": "예상 등락률",
        "out_block": "t1492OutBlock1",
        "payload": {"t1492InBlock": {"gubun1": "0", "gubun2": "0", "jongchk": "0", "volume": "0", "idx": 0}},
    },
}

# 브로커 TR → 도메인 어휘. **이 표가 LS가 새어 나가는 마지막 지점이다.**
# 왼쪽(SC*)은 이 파일 안에서만 쓰이고, 밖으로는 오른쪽만 나간다.
_TR_TO_KIND: dict[str, str] = {
    "SC0": "ACCEPTED",
    "SC1": "FILLED",
    "SC2": "AMENDED",
    "SC3": "CANCELLED",
    "SC4": "REJECTED",
}
KIND_LABELS: dict[str, str] = {
    "ACCEPTED": "접수",
    "FILLED": "체결",
    "AMENDED": "정정",
    "CANCELLED": "취소",
    "REJECTED": "거부",
}
KINDS = tuple(KIND_LABELS)


# --------------------------------------------------------------------------
# 정규화 — 필드명을 추측하지 않고 후보를 훑는다
# --------------------------------------------------------------------------
#
# SC2/SC3/SC4의 응답 바디는 우리 수집본(2026-07-29)에 "해당 필드가 없습니다"로
# 비어 있고, 원문 사이트는 '+' 버튼으로 동적 로드하는 SPA라 정적 수집이 닿지
# 않았다. 그래서 필드명을 **추측해서 박지 않는다** — 후보 키를 순서대로 훑고
# 못 찾으면 None으로 둔다. SC0(`shtcode`/`hname`/`ordprice`)와
# SC1(`shtnIsuno`/`Isunm`/`ordprc`)이 이미 같은 뜻에 다른 이름을 쓰므로 어차피
# 후보 훑기가 필요했고, SC2~SC4는 그 덕에 실제 이름이 무엇이든 같이 붙는다.

def _pick(body: dict[str, Any], *names: str) -> str | None:
    """후보 키를 순서대로 보고 처음 나오는 비어 있지 않은 값을 준다."""
    for name in names:
        value = body.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _number(value: Any) -> str | None:
    """고정폭 0 패딩을 벗기고 문자열로 둔다.

    float로 바꾸지 않는다 — 가격·수량에 float를 쓰지 않는 것이 저장소 규칙이다.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (int,)):
        return str(value)
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("-")
    digits = text[1:] if negative else text
    if not digits or not digits.replace(".", "", 1).isdigit():
        return text
    trimmed = digits.lstrip("0")
    if trimmed in ("", "."):
        trimmed = "0"
    elif trimmed.startswith("."):
        trimmed = "0" + trimmed
    if "." in trimmed:
        trimmed = trimmed.rstrip("0").rstrip(".") or "0"
    return "-" + trimmed if negative else trimmed


def normalize_market_ranking(
    payload: dict[str, Any], ranking: str, limit: int = MARKET_RANKING_LIMIT
) -> dict[str, Any]:
    """허용된 시장 순위 TR 응답을 화면용 상위 종목 목록으로 줄인다."""
    definition = MARKET_RANKINGS.get(ranking)
    if definition is None:
        raise ValueError(f"지원하지 않는 시장 순위 종류입니다: {ranking}")

    raw_rows = payload.get(definition["out_block"])
    raw_rows = raw_rows if isinstance(raw_rows, list) else []
    rows: list[dict[str, Any]] = []
    row_limit = max(1, min(int(limit), MARKET_RANKING_LIMIT))
    for index, row in enumerate(raw_rows[:row_limit], start=1):
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "rank": index,
                "symbol": _symbol(_pick(row, "shcode", "expcode")),
                "name": _pick(row, "hname", "Isunm"),
                "price": _number(row.get("price")),
                "change": _number(row.get("change")),
                "change_rate": _number(row.get("diff")),
                "volume": _number(row.get("volume")),
                "amount": _number(_pick(row, "value", "trade_amt")),
                "market_cap": _number(row.get("total")),
                "expected_volume": _number(row.get("yevolume")),
                "volume_surge_rate": _number(row.get("voldiff")),
            }
        )
    return {
        "kind": ranking,
        "label": definition["label"],
        "metric_label": definition["metric_label"],
        "rows": rows,
    }


def _side(code: str | None) -> str | None:
    """매매구분. 문서 다른 절의 범례가 `1'매도'2'매수`다.

    ponytail: SC0/SC1 자체에는 범례가 없다. 모르는 코드는 번역하지 않고 그대로
    내보낸다 — 매수/매도를 잘못 뒤집어 보여 주는 것보다 코드가 보이는 편이 낫다.
    """
    return {"1": "매도", "2": "매수"}.get(code or "", code or None)


def _symbol(value: str | None) -> str | None:
    """실시간 `A` 접두사를 제거하고 6자리 영숫자 KRX 코드로 맞춘다.

    두 경로의 종목코드가 어긋나면 로컬 포지션과 브로커 잔고를 대조할 수 없다.
    """
    if not value:
        return None
    text = str(value).strip().upper()
    if re.fullmatch(r"[0-9A-Z]{6}", text):
        return text
    if text.startswith("A") and re.fullmatch(r"[0-9A-Z]{6}", text[1:]):
        return text[1:]
    return None


def _account(body: dict[str, Any]) -> str | None:
    head = _pick(body, "accno1", "accno")
    tail = _pick(body, "accno2")
    if not head:
        return None
    return head + tail if tail else head


def mask_account(account_no: str | None) -> str | None:
    """계좌번호는 뒤 4자리만 나간다(`account_snapshot.py`와 같은 규칙)."""
    if not account_no:
        return None
    return "****" + str(account_no)[-4:]


def _event_id(source: str, *parts: Any) -> str:
    """Return a stable, non-secret identity for one projected broker event."""

    identity = json.dumps(
        [source, *("" if part is None else str(part) for part in parts)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _execution_origin(channel: str | None) -> str:
    """Classify only what the broker explicitly says; never guess our caller."""

    normalized = str(channel or "").strip().casefold()
    if "hts" in normalized or "투혼" in normalized:
        return "EXTERNAL_HTS"
    if "api" in normalized:
        return "BROKER_API_UNATTRIBUTED"
    if normalized:
        return "BROKER_CHANNEL_UNATTRIBUTED"
    return "BROKER_ACCOUNT_UNATTRIBUTED"


def normalize_order_event(tr_cd: str, body: dict[str, Any], seq: int) -> dict[str, Any]:
    """브로커 푸시 1건 → 화면 계약. LS 필드명은 여기서 끝난다."""
    kind = _TR_TO_KIND[tr_cd]
    received_at = datetime.now(timezone.utc).isoformat()
    event_time = _pick(body, "exectime", "ordtm")
    order_no = _number(_pick(body, "ordno"))
    symbol = _symbol(_pick(body, "shtcode", "shtnIsuno", "expcode", "Isuno"))
    side = _side(_pick(body, "bnstp"))
    quantity = _number(
        _pick(body, "execqty", "mdfycnfqty", "canccnfqty", "rjtqty", "ordqty")
    )
    price = _number(_pick(body, "execprc", "mdfycnfprc", "ordprice", "ordprc"))
    return {
        "seq": seq,
        "event_id": _event_id(
            "LS_REALTIME", kind, order_no, symbol, side, event_time, quantity, price
        ),
        "kind": kind,
        "label": KIND_LABELS[kind],
        "received_at": received_at,
        # 시각: 체결은 체결시각, 나머지는 주문시각
        "event_time": event_time,
        "order_no": order_no,
        "broker_order_id": order_no,
        "broker_order_ids": [order_no] if order_no else [],
        "trade_no": None,
        "orig_order_no": _number(_pick(body, "orgordno")),
        "symbol": symbol,
        "symbol_name": _pick(body, "hname", "Isunm"),
        "side": side,
        # 체결이면 체결수량·체결가, 아니면 주문수량·주문가
        "quantity": quantity,
        "price": price,
        "unfilled_quantity": _number(_pick(body, "unercqty", "orgordunercqty")),
        "source": "LS_REALTIME",
        "execution_channel": None,
        "execution_channels": [],
        "origin": "BROKER_EVENT_UNATTRIBUTED",
        "correlation_status": "UNATTRIBUTED",
    }


def normalize_holdings(payload: dict[str, Any]) -> dict[str, Any]:
    """잔고 조회 응답 → 화면 계약. 여기서도 LS 필드명은 밖으로 안 나간다."""
    summary = payload.get("t0424OutBlock")
    rows = payload.get("t0424OutBlock1")
    summary = summary if isinstance(summary, dict) else {}
    rows = rows if isinstance(rows, list) else []
    return {
        "net_asset": _number(summary.get("sunamt")),
        "realized_pnl": _number(summary.get("dtsunik")),
        "purchase_amount": _number(summary.get("mamt")),
        "valuation": _number(summary.get("tappamt")),
        "valuation_pnl": _number(summary.get("tdtsunik")),
        "rows": [
            {
                "symbol": _symbol(_pick(row, "expcode")),
                "name": _pick(row, "hname"),
                "quantity": _number(row.get("janqty")),
                "sellable_quantity": _number(row.get("mdposqt")),
                "average_cost": _number(row.get("pamt")),
                "purchase_amount": _number(row.get("mamt")),
                "last_price": _number(row.get("price")),
                "market_value": _number(row.get("appamt")),
                "unrealized_pnl": _number(row.get("dtsunik")),
                "return_rate": _number(row.get("sunikrt")),
                "weight": _number(row.get("janrt")),
            }
            for row in rows
            if isinstance(row, dict)
        ],
    }


def normalize_today_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """t0150 응답 → 오늘 매매 요약 화면 계약."""
    summary = payload.get("t0150OutBlock")
    rows = payload.get("t0150OutBlock1")
    if not isinstance(summary, dict):
        raise RuntimeError("t0150 응답에 당일 매매 요약 블록이 없습니다.")
    rows = rows if isinstance(rows, list) else []
    return {
        "trade_count": len([row for row in rows if isinstance(row, dict)]),
        "summary": {
            "buy_quantity": _number(summary.get("msqty")),
            "sell_quantity": _number(summary.get("mdqty")),
            "buy_amount": _number(summary.get("msamt")),
            "sell_amount": _number(summary.get("mdamt")),
            "total_amount": _number(summary.get("tamt")),
            "total_fee": _number(summary.get("tfee")),
            "total_tax": _number(summary.get("ttax")),
            "total_settlement": _number(summary.get("tadjamt")),
        },
    }


def normalize_executions(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """당일 체결내역 → `(종목, 매매구분)` → 시각·종목명 색인.

    당일 매매일지(`t0150`)에는 **시각도 종목명도 없다.** 회계 원장에서 거래가
    몇 시에 났는지는 대사할 때 필요한 값이라, 체결내역에서 가져와 붙인다.

    같은 종목을 같은 방향으로 여러 번 체결하면 매매일지는 종목소계 한 줄로
    합친다. 그 줄에 붙일 시각은 **그 묶음의 마지막 체결시각**이다 - 소계가
    묶음 전체를 말하므로 첫 체결로 적으면 나중 체결이 없던 일이 된다.
    """
    rows = payload.get("CSPAQ13700OutBlock3")
    rows = rows if isinstance(rows, list) else []
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = _symbol(_pick(row, "IsuNo"))
        side = _side(_pick(row, "BnsTpCode"))
        if not symbol or not side:
            continue
        time_text = _pick(row, "LastExecTime", "ExecTrxTime", "OrdTime")
        key = (symbol, side)
        current = index.setdefault(
            key,
            {"time": None, "name": None, "count": 0, "broker_order_ids": []},
        )
        current["count"] = current.get("count", 0) + 1
        if (time_text or "") > (current.get("time") or ""):
            current["time"] = time_text
        if not current.get("name"):
            current["name"] = _pick(row, "IsuNm")
        order_no = _number(row.get("OrdNo"))
        if order_no and order_no not in current["broker_order_ids"]:
            current["broker_order_ids"].append(order_no)
    return index


def normalize_accepted_orders(
    payload: dict[str, Any], order_date: str | None = None
) -> list[dict[str, Any]]:
    """당일 주문·체결 조회에서 실제 주문번호가 있는 접수 사건을 복원한다.

    체결 원장 행을 접수로 바꾸지 않는다. 주문번호·주문시각·주문수량이 따로 있는
    주문 조회 행만 사용하므로, 프로세스가 주문 뒤에 시작돼 SC0을 놓친 경우에도
    대시보드의 접수 사건을 다시 구성할 수 있다.
    """
    rows = payload.get("CSPAQ13700OutBlock3")
    rows = rows if isinstance(rows, list) else []
    day = order_date or date.today().isoformat()
    events: list[dict[str, Any]] = []
    seen_order_nos: set[str] = set()

    for row in rows:
        if not isinstance(row, dict):
            continue
        order_no = _number(row.get("OrdNo"))
        if not order_no or order_no in seen_order_nos:
            continue
        seen_order_nos.add(order_no)
        order_time = _pick(row, "OrdTime")
        symbol = _symbol(_pick(row, "IsuNo"))
        side = _side(_pick(row, "BnsTpCode"))
        quantity = _number(row.get("OrdQty"))
        requested_price = _number(row.get("OrdPrc"))
        filled_quantity = _number(
            row.get("AllExecQty") or row.get("ExecQty") or 0
        )
        execution_price = _number(row.get("ExecPrc"))
        has_fill = _dec(filled_quantity) > 0
        kind = "FILLED" if has_fill else "ACCEPTED"
        event_time = _pick(row, "LastExecTime", "ExecTrxTime") if has_fill else order_time
        price = execution_price if has_fill and _dec(execution_price) > 0 else requested_price
        unfilled_quantity = max(Decimal(0), _dec(quantity) - _dec(filled_quantity))
        events.append({
            "seq": 0,  # 병합 뒤 전체 목록 기준으로 다시 부여한다.
            "event_id": _event_id(
                "LS_ORDER_HISTORY",
                kind,
                order_no,
                symbol,
                side,
                order_time,
                quantity,
                price,
            ),
            "kind": kind,
            "label": (
                "부분체결"
                if has_fill and unfilled_quantity > 0
                else KIND_LABELS[kind]
            ),
            "received_at": day + ("T" + event_time if event_time else ""),
            "event_time": event_time,
            "order_no": order_no,
            "broker_order_id": order_no,
            "broker_order_ids": [order_no],
            "trade_no": None,
            "orig_order_no": _number(row.get("OrgOrdNo")),
            "symbol": symbol,
            "symbol_name": _pick(row, "IsuNm"),
            "side": side,
            "quantity": quantity,
            "price": price,
            "requested_price": (
                requested_price if _dec(requested_price) > 0 else None
            ),
            "filled_quantity": filled_quantity,
            "average_fill_price": (
                execution_price
                if has_fill and _dec(execution_price) > 0
                else None
            ),
            "unfilled_quantity": str(unfilled_quantity),
            "source": "LS_ORDER_HISTORY",
            "execution_channel": None,
            "execution_channels": [],
            "origin": "BROKER_ORDER_UNATTRIBUTED",
            "correlation_status": "UNATTRIBUTED",
        })

    events.sort(
        key=lambda event: (
            str(event.get("received_at") or ""),
            str(event.get("order_no") or ""),
        ),
        reverse=True,
    )
    return events


def attach_executions(
    rows: list[dict[str, Any]], index: dict[tuple[str, str], dict[str, Any]]
) -> list[dict[str, Any]]:
    """원장 줄에 체결시각·종목명을 붙인다. 못 찾으면 비워 둔다(지어내지 않는다)."""
    for row in rows:
        found = index.get((row.get("symbol") or "", row.get("category") or ""))
        if not found:
            continue
        if not row.get("trade_time"):
            row["trade_time"] = found.get("time")
        if not row.get("symbol_name"):
            row["symbol_name"] = found.get("name")
        broker_order_ids = list(found.get("broker_order_ids") or [])
        row["broker_order_ids"] = broker_order_ids
        row["broker_order_id"] = (
            broker_order_ids[0] if len(broker_order_ids) == 1 else None
        )
        row["execution_count"] = found.get("count")
    return rows


def build_pnl(holdings: dict[str, Any] | None, totals: Mapping[str, Any]) -> dict[str, Any]:
    """손익 구성.

    ▶ 총손익 = 실현손익 + 평가손익. **여기서 비용을 다시 빼지 않는다.**
      잔고를 `제비용포함(charge=1)`으로 조회하므로 브로커가 주는 실현손익에
      수수료·세금이 이미 반영돼 있다. 한 번 더 빼면 이중 차감이고, 그건 그냥
      틀린 손익이다. 거래비용은 **참고 수치로 따로** 보여 준다.

    ▶ 평가손익은 아직 팔지 않은 값이다. 실현손익과 한 칸에 합쳐 두면 확정된
      돈처럼 보이므로 줄을 나눈다.
    """
    holdings = holdings or {}
    realized = _dec(holdings.get("realized_pnl"))
    valuation = _dec(holdings.get("valuation_pnl"))
    commission = _dec(totals.get("commission"))
    tax = _dec(totals.get("tax"))
    return {
        "realized": _number(realized),
        "valuation": _number(valuation),
        "total": _number(realized + valuation),
        "commission": _number(commission),
        "tax": _number(tax),
        "cost": _number(commission + tax),
        # 화면이 "왜 비용을 안 뺐지"를 묻지 않도록 근거를 같이 내린다.
        "cost_included_in_realized": True,
    }


def summarize_ledger_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """정규화된 원장 줄에서 합계를 낸다.

    확정분(결제완료)과 당일 매매일지(미결제)를 합친 뒤에도 같은 규칙으로 세야
    화면의 합계와 표가 어긋나지 않는다.
    """
    totals = {k: Decimal(0) for k in ("commission", "tax", "realized_pnl", "dividend", "settled")}
    unsettled = 0
    for row in rows:
        totals["commission"] += _dec(row.get("commission"))
        totals["tax"] += _dec(row.get("tax"))
        totals["realized_pnl"] += _dec(row.get("realized_pnl"))
        totals["dividend"] += _dec(row.get("dividend"))
        totals["settled"] += _dec(row.get("settled_amount"))
        if row.get("settlement") == "UNSETTLED":
            unsettled += 1
    return {
        "count": len(rows),
        "unsettled_count": unsettled,
        "cost": _number(totals["commission"] + totals["tax"]),
        **{k: _number(v) for k, v in totals.items()},
    }


def normalize_today_trades(payload: dict[str, Any], today: str) -> dict[str, Any]:
    """당일 매매일지 → 원장 줄(미결제).

    ▶ 왜 필요한가
      확정 거래내역(`CDPCQ04700`)은 **결제 기준**이라 체결 당일에는 비어 있다
      (T+2). 그런데 회계가 오늘 나간 수수료·세금을 못 보면 그날 장부가 빈다.
      그래서 결제 전 구간은 매매일지로 메우되 `settlement`로 구분해 둔다.

    ▶ 응답 모양 (2026-08-18 실측)
      `[매매행, 종목소계, 매매행, 종목소계, ...]`로 온다. **매매행의 수수료·세금은
      0이고 실제 비용은 바로 뒤 종목소계에 실린다.** 매매행만 읽으면 비용이 전부
      0으로 보이고, 소계만 읽으면 종목번호가 빈다(`expcode: ""`).
      → 소계가 나올 때까지 모은 뒤 한 줄로 합친다.

    ▶ 부호
      `adjamt`는 매수·매도 모두 양수다. 예수금 증감 방향은 매매구분이 정하므로
      매수는 음수로 뒤집는다 - LS 자신의 합계도 `매도정산 - 매수정산`이다.
    """
    rows = payload.get("t0150OutBlock1")
    rows = rows if isinstance(rows, list) else []

    merged: list[dict[str, Any]] = []
    group: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        side = _pick(row, "medosu") or ""
        if side != "종목소계":
            group.append(row)
            continue
        if not group:
            continue  # 짝이 없는 소계는 귀속시킬 곳이 없다
        head = group[0]
        side = _pick(head, "medosu") or ""
        execution_channels = list(
            dict.fromkeys(
                channel
                for item in group
                if (channel := _pick(item, "middiv")) is not None
            )
        )
        # 비용은 소계, 종목·매매구분은 매매행. 둘 중 하나만 보면 반쪽이다.
        commission = _dec(row.get("fee"))
        tax = _dec(row.get("tax")) + _dec(row.get("argtax"))
        settled = _dec(row.get("adjamt"))
        if side == "매수":
            settled = -settled
        merged.append({
            "trade_date": today,
            "trade_no": None,
            "trade_time": None,
            "category": side or None,
            "summary": "당일 매매" + ("" if len(group) == 1 else f" {len(group)}건"),
            "cancelled": None,
            "symbol": _symbol(_pick(head, "expcode")),
            "symbol_name": None,
            "quantity": _number(row.get("qty")),
            "unit_price": _number(row.get("price")),
            "amount": _number(row.get("amt")),
            "settled_amount": _number(settled),
            "commission": _number(commission),
            "tax": _number(tax),
            # 매매일지는 실현손익을 주지 않는다. 0으로 채우면 손익이 0이라는 뜻이
            # 되므로 비워 둔다.
            "realized_pnl": None,
            "dividend": None,
            "settlement": "UNSETTLED",
            "cash_before": None,
            "cash_after": None,
            "currency": "KRW",
            # LS가 명시한 주문 채널(예: "투혼(HTS)")을 버리지 않는다. 이 값은
            # 자연어 주문과 외부 HTS 주문을 구분하는 유일한 브로커 근거가 될 수 있다.
            "execution_channel": (
                execution_channels[0]
                if len(execution_channels) == 1
                else ("MIXED" if execution_channels else None)
            ),
            "execution_channels": execution_channels,
            "broker_order_id": None,
            "broker_order_ids": [],
            "execution_count": len(group),
        })
        group = []
    return {"rows": merged}


def summarize_today_trade_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the today-summary contract from normalized broker trade rows.

    Some PAPER accounts return the trade rows but omit ``t0150OutBlock``.
    Those rows are still authoritative broker observations, so the dashboard
    can derive a bounded summary without turning the missing summary block into
    a fake 500/error state.
    """

    buy_quantity = sell_quantity = Decimal(0)
    buy_amount = sell_amount = Decimal(0)
    total_fee = total_tax = total_settlement = Decimal(0)
    for row in rows:
        if not isinstance(row, dict):
            continue
        side = str(row.get("category") or "")
        quantity = _dec(row.get("quantity"))
        amount = abs(_dec(row.get("amount")))
        settled = _dec(row.get("settled_amount"))
        if "매수" in side:
            buy_quantity += quantity
            buy_amount += amount
        elif "매도" in side:
            sell_quantity += quantity
            sell_amount += amount
        total_fee += _dec(row.get("commission"))
        total_tax += _dec(row.get("tax"))
        total_settlement += settled
    return {
        "trade_count": len(rows),
        "summary": {
            "buy_quantity": _number(buy_quantity),
            "sell_quantity": _number(sell_quantity),
            "buy_amount": _number(buy_amount),
            "sell_amount": _number(sell_amount),
            "total_amount": _number(buy_amount + sell_amount),
            "total_fee": _number(total_fee),
            "total_tax": _number(total_tax),
            "total_settlement": _number(total_settlement),
        },
    }


def reconcile_today_activity(
    activity: dict[str, Any] | None,
    events: list[dict[str, Any]],
    business_day: str,
) -> dict[str, Any] | None:
    """Reconcile a zero broker diary with explicit fills for one business day."""

    current = activity if isinstance(activity, dict) else {}
    if int(current.get("trade_count") or 0) > 0:
        return current

    history_fills = [
        event
        for event in events
        if event.get("kind") == "FILLED"
        and event.get("source") == "LS_ORDER_HISTORY"
        and str(event.get("received_at") or "").startswith(business_day)
    ]
    realtime_fills = [
        event
        for event in events
        if event.get("kind") == "FILLED"
        and event.get("source") == "LS_REALTIME"
        and str(event.get("received_at") or "").startswith(business_day)
    ]
    fills = history_fills or realtime_fills
    if not fills:
        return activity

    seen: set[str] = set()
    buy_quantity = sell_quantity = Decimal(0)
    buy_amount = sell_amount = Decimal(0)
    trade_count = 0
    for event in fills:
        identity = str(
            event.get("order_no")
            or event.get("event_id")
            or (
                event.get("received_at"),
                event.get("symbol"),
                event.get("side"),
                event.get("quantity"),
                event.get("price"),
            )
        )
        if identity in seen:
            continue
        seen.add(identity)
        quantity = _dec(event.get("filled_quantity") or event.get("quantity"))
        price = _dec(event.get("average_fill_price") or event.get("price"))
        side = str(event.get("side") or "").upper()
        if "매수" in side or side == "BUY":
            buy_quantity += quantity
            buy_amount += quantity * price
        elif "매도" in side or side == "SELL":
            sell_quantity += quantity
            sell_amount += quantity * price
        else:
            continue
        trade_count += 1

    if trade_count == 0:
        return activity
    current_summary = current.get("summary")
    current_summary = current_summary if isinstance(current_summary, dict) else {}
    return {
        "trade_count": trade_count,
        "summary": {
            "buy_quantity": _number(buy_quantity),
            "sell_quantity": _number(sell_quantity),
            "buy_amount": _number(buy_amount),
            "sell_amount": _number(sell_amount),
            "total_amount": _number(buy_amount + sell_amount),
            "total_fee": _number(current_summary.get("total_fee")),
            "total_tax": _number(current_summary.get("total_tax")),
            "total_settlement": _number(sell_amount - buy_amount),
        },
        "source": "ORDER_HISTORY_RECONCILIATION",
    }


def _dec(value: Any) -> Decimal:
    """합계용. 해석 못 하는 값은 0으로 두되 행 자체는 버리지 않는다."""
    if value is None or value == "":
        return Decimal(0)
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal(0)


def _date(value: Any) -> str | None:
    """`YYYYMMDD` → `YYYY-MM-DD`. 해석 못 하면 원본을 그대로 둔다."""
    text = str(value or "").strip()
    if len(text) != 8 or not text.isdigit():
        return text or None
    return text[:4] + "-" + text[4:6] + "-" + text[6:]


def normalize_ledger(payload: dict[str, Any]) -> dict[str, Any]:
    """계좌 거래내역 응답 → 회계 화면 계약.

    회계가 보는 축은 트레이딩과 다르다 - 주문이 어떻게 흘렀는지가 아니라
    **얼마가 오갔고 비용과 세금이 얼마였는지**다. 그래서 수수료·거래세·소득세·
    주민세·매매손익·배당과 예수금 전잔/금잔을 남기고 주문 상태는 버린다.
    """
    rows = payload.get("CDPCQ04700OutBlock3")
    rows = rows if isinstance(rows, list) else []
    normalized: list[dict[str, Any]] = []
    totals = {
        "commission": Decimal(0),
        "tax": Decimal(0),
        "realized_pnl": Decimal(0),
        "dividend": Decimal(0),
        "settled": Decimal(0),
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        # 세금은 항목이 흩어져 있다. 합계 필드가 오면 그것을 쓰고, 없으면
        # 거래세+소득세+주민세로 만든다 - 둘을 더하면 이중 계상이 된다.
        tax_total = _dec(row.get("TaxSumAmt"))
        if tax_total == 0:
            tax_total = _dec(row.get("Trtax")) + _dec(row.get("Ictax")) + _dec(row.get("Ihtax"))
        commission = _dec(row.get("CmsnAmt"))
        realized = _dec(row.get("BnsplAmt"))
        dividend = _dec(row.get("MnyDvdAmt"))
        settled = _dec(row.get("AdjstAmt"))
        totals["commission"] += commission
        totals["tax"] += tax_total
        totals["realized_pnl"] += realized
        totals["dividend"] += dividend
        totals["settled"] += settled
        normalized.append({
            "trade_date": _date(row.get("TrdDt")),
            "trade_no": _number(row.get("TrdNo")),
            "trade_time": _pick(row, "TrxTime"),
            "category": _pick(row, "TpCodeNm"),
            "summary": _pick(row, "SmryNm"),
            "cancelled": _pick(row, "CancTpNm"),
            "symbol": _symbol(_pick(row, "IsuNo")),
            "symbol_name": _pick(row, "IsuNm"),
            "quantity": _number(row.get("TrdQty")),
            "unit_price": _number(row.get("TrdUprc")),
            "amount": _number(row.get("TrdAmt")),
            "settled_amount": _number(settled),
            "commission": _number(commission),
            "tax": _number(tax_total),
            "realized_pnl": _number(realized),
            "dividend": _number(dividend),
            # 결제까지 끝난 줄이다. 당일 매매일지에서 온 줄과 섞이면 회계가
            # 미결제를 확정 수치로 착각한다.
            "settlement": "SETTLED",
            "cash_before": _number(row.get("DpsBfbalAmt")),
            "cash_after": _number(row.get("DpsCrbalAmt")),
            "currency": _pick(row, "CrcyCode"),
            # 결제 원장에는 주문 채널과 주문번호가 없다. 거래번호를 주문번호로
            # 승격하지 않고 명시적으로 미상으로 둔다.
            "execution_channel": None,
            "execution_channels": [],
            "broker_order_id": None,
            "broker_order_ids": [],
            "execution_count": None,
        })
    # 최신이 위로. 같은 날이면 거래번호 순이다.
    normalized.sort(key=lambda item: (item["trade_date"] or "", item["trade_no"] or ""), reverse=True)
    return {
        "rows": normalized,
        "totals": {
            "count": len(normalized),
            # 회계가 보는 비용은 수수료와 세금을 합친 값이다. 화면이 문자열을
            # 더하게 두면 소수점이 섞이는 순간 깨진다 - Decimal인 여기서 낸다.
            "cost": _number(totals["commission"] + totals["tax"]),
            **{k: _number(v) for k, v in totals.items()},
        },
        # 기간 마지막 예수금 잔액 = 가장 최근 거래의 금잔
        "cash_balance": normalized[0]["cash_after"] if normalized else None,
        "notice": None if normalized else (_pick(payload, "rsp_msg") or None),
    }


def ledger_to_order_events(ledger: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """CDPCQ04700 거래내역을 기존 주문 사건 화면 계약으로 투영한다.

    화면 계약은 유지하되 원천만 바꾼다. CDPCQ04700은 확정 거래내역이므로
    여기서 만드는 상태는 모두 체결이다. 접수/거부 같은 미체결 주문 상태는
    계좌 거래내역에 존재하지 않으므로 실시간 SC 이벤트의 의미를 섞지 않는다.
    """
    rows = ledger.get("rows")
    if not isinstance(rows, list):
        return []
    events: list[dict[str, Any]] = []
    for index, row in enumerate(rows[: max(1, min(limit, MAX_EVENTS))]):
        if not isinstance(row, dict):
            continue
        trade_date = str(row.get("trade_date") or "")
        trade_time = str(row.get("trade_time") or "") or None
        trade_no = str(row.get("trade_no") or "") or None
        broker_order_ids = [
            str(value)
            for value in (row.get("broker_order_ids") or [])
            if str(value or "").strip()
        ]
        explicit_broker_order_id = str(row.get("broker_order_id") or "").strip()
        if explicit_broker_order_id and explicit_broker_order_id not in broker_order_ids:
            broker_order_ids.append(explicit_broker_order_id)
        broker_order_id = broker_order_ids[0] if len(broker_order_ids) == 1 else None
        execution_channel = str(row.get("execution_channel") or "").strip() or None
        execution_channels = [
            str(value)
            for value in (row.get("execution_channels") or [])
            if str(value or "").strip()
        ]
        source = (
            "LS_TODAY_TRADE_LEDGER"
            if row.get("settlement") == "UNSETTLED"
            else "LS_SETTLED_ACCOUNT_LEDGER"
        )
        category = str(row.get("category") or "")
        if "매도" in category:
            side = "매도"
        elif "매수" in category:
            side = "매수"
        else:
            side = category or None
        event_id = _event_id(
            source,
            trade_date,
            trade_no,
            row.get("symbol"),
            side,
            trade_time,
            row.get("quantity"),
            row.get("unit_price"),
            execution_channel,
        )
        events.append({
            # 최신 거래가 먼저 오므로 seq도 화면에서 유일하게 역순으로 준다.
            "seq": len(rows) - index,
            "event_id": event_id,
            "kind": "FILLED",
            "label": "체결",
            "received_at": trade_date + ("T" + trade_time if trade_time else ""),
            "event_time": trade_time,
            # 거래번호와 주문번호는 서로 다른 식별자다. 주문번호가 LS 주문
            # 조회로 한 개 확정된 경우에만 기존 order_no 칸에도 넣는다.
            "order_no": broker_order_id,
            "broker_order_id": broker_order_id,
            "broker_order_ids": broker_order_ids,
            "trade_no": trade_no,
            "orig_order_no": None,
            "symbol": row.get("symbol"),
            "symbol_name": row.get("symbol_name"),
            "side": side,
            "quantity": row.get("quantity"),
            "price": row.get("unit_price"),
            "unfilled_quantity": None,
            "source": source,
            "execution_channel": execution_channel,
            "execution_channels": execution_channels,
            "origin": _execution_origin(execution_channel),
            "correlation_status": "UNATTRIBUTED",
        })
    return events


def merge_order_events(
    ledger_events: list[dict[str, Any]],
    realtime_events: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """확정 체결내역과 실시간 주문 사건을 기존 화면 계약으로 합친다.

    CDPCQ04700에는 접수·정정·취소·거부가 없으므로 그 상태는 SC 실시간 피드에서
    가져온다. SC1 체결도 거래내역이 갱신되기 전까지는 즉시 보여 주고, 계좌
    거래내역이 도착하면 주문번호 기준으로 한 건만 남긴다.
    """
    # 실시간/당일 주문 조회가 같은 접수를 함께 주는 경우 주문번호로 한 건만 남긴다.
    # 주문번호가 없을 때도 화면 계약 필드 조합으로 중복을 막되 값을 지어내지 않는다.
    events: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    projected_ledger = [dict(event) for event in ledger_events]
    supplemental: list[dict[str, Any]] = []
    history_fills: list[dict[str, Any]] = []
    realtime_fills: list[dict[str, Any]] = []

    def order_ids(event: dict[str, Any]) -> set[str]:
        values = {
            str(value).strip()
            for value in (
                event.get("order_no"),
                event.get("broker_order_id"),
                *(event.get("broker_order_ids") or []),
            )
            if str(value or "").strip()
        }
        return values

    def event_time_key(value: Any) -> str:
        digits = "".join(character for character in str(value or "") if character.isdigit())
        return digits[:6] if len(digits) >= 6 else digits

    def same_fill(left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_ids = order_ids(left)
        right_ids = order_ids(right)
        if left_ids and right_ids:
            return bool(left_ids & right_ids)
        # 주문번호가 없는 계좌 원장과도 동일 체결을 알아볼 수 있을 때만
        # 보조적으로 합친다. 시각까지 같아야 하므로 같은 종목의 부분 체결을
        # 임의로 하나로 숨기지 않는다.
        return (
            left.get("symbol")
            and left.get("symbol") == right.get("symbol")
            and left.get("side")
            and left.get("side") == right.get("side")
            and left.get("quantity") is not None
            and left.get("quantity") == right.get("quantity")
            and left.get("price") is not None
            and left.get("price") == right.get("price")
            and event_time_key(left.get("event_time"))
            and event_time_key(left.get("event_time")) == event_time_key(right.get("event_time"))
        )

    for event in realtime_events:
        if event.get("kind") != "FILLED":
            supplemental.append(event)
            continue
        if event.get("source") == "LS_ORDER_HISTORY":
            # CSPAQ13700 carries the actual LS order number plus requested and
            # execution prices. Enrich one exact ledger match, or retain it as
            # independent broker evidence when t0150 omitted that fill.
            order_no = str(event.get("order_no") or "").strip()
            matches = [
                candidate
                for candidate in projected_ledger
                if order_no and order_no in order_ids(candidate)
            ]
            if len(matches) == 1:
                match = matches[0]
                match["order_no"] = order_no
                match["broker_order_id"] = order_no
                for field in (
                    "requested_price",
                    "filled_quantity",
                    "average_fill_price",
                    "unfilled_quantity",
                ):
                    if event.get(field) is not None:
                        match[field] = event[field]
            else:
                history_fills.append(event)
        else:
            # Keep the SC1 event visible during the history-cache window. It is
            # removed below when an authoritative history event already covers
            # the same fill, so this does not create a second order row.
            realtime_fills.append(event)

    known_history_fills = history_fills + projected_ledger
    for event in realtime_fills:
        if not any(same_fill(event, candidate) for candidate in known_history_fills):
            supplemental.append(event)

    # Prefer account-history evidence over its provisional realtime counterpart
    # when both are present and share an order number.
    candidates = supplemental + history_fills + projected_ledger
    for event in candidates:
        order_no = event.get("order_no")
        key = (
            event.get("kind"),
            ("order", str(order_no)) if order_no else (
                "event",
                event.get("symbol"),
                event.get("side"),
                event.get("event_time"),
                event.get("quantity"),
                event.get("price"),
            ),
        )
        if key in seen:
            continue
        seen.add(key)
        events.append(dict(event))
    events.sort(
        key=lambda event: (
            str(event.get("received_at") or ""),
            str(event.get("event_time") or ""),
            int(event.get("seq") or 0),
        ),
        reverse=True,
    )
    events = events[: max(1, min(limit, MAX_EVENTS))]
    # 원장과 실시간 피드는 각자 1부터 seq를 매겨 그대로 합치면 React key가
    # 충돌한다. 최종 화면 목록에서 유일한 순번으로 다시 부여한다.
    for index, event in enumerate(events):
        event["seq"] = len(events) - index
    return events


def _event_broker_day(event: Mapping[str, Any]) -> date | None:
    value = str(event.get("received_at") or "").strip()
    if not value:
        return None
    try:
        observed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if observed.tzinfo is not None:
        observed = observed.astimezone(KST)
    return observed.date()


def _same_decimal(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except Exception:  # noqa: BLE001 - invalid evidence must not be attributed.
        return False


def _matches_internal_correlation(
    event: Mapping[str, Any], correlation: BrokerOrderCorrelation
) -> bool:
    side = str(event.get("side") or "").upper()
    normalized_side = "BUY" if "매수" in side or side == "BUY" else (
        "SELL" if "매도" in side or side == "SELL" else ""
    )
    if (
        str(event.get("symbol") or "") != correlation.symbol
        or normalized_side != correlation.side
        or not _same_decimal(event.get("quantity"), correlation.requested_quantity)
    ):
        return False
    if event.get("kind") == "FILLED":
        event_price = event.get("average_fill_price") or event.get("price")
        if correlation.average_fill_price is None or not _same_decimal(
            event_price, correlation.average_fill_price
        ):
            return False
    return True


def _project_internal_order_correlations(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Join LS facts to the existing PAPER audit trail without mutating orders."""

    broker_day = datetime.now(KST).date()
    current_events = [
        event
        for event in events
        if event.get("source") in {"LS_ORDER_HISTORY", "LS_REALTIME"}
        and _event_broker_day(event) == broker_day
    ]
    order_nos = {
        str(event.get("broker_order_id") or event.get("order_no") or "").strip()
        for event in current_events
        if str(event.get("broker_order_id") or event.get("order_no") or "").strip()
    }
    summary = {
        "status": "READY",
        "source": "execution.user_order_request_events",
        "attributed": 0,
        "unattributed": len(current_events),
        "error": None,
    }
    if not order_nos:
        return [dict(event) for event in events], summary

    start_kst = datetime.combine(broker_day, datetime.min.time(), tzinfo=KST)
    try:
        correlations = user_order_repository().broker_correlations(
            order_nos,
            recorded_after=start_kst.astimezone(timezone.utc),
        )
    except UserOrderWorkflowUnavailable:
        summary.update(status="DEGRADED", error="ORDER_AUTHORITY_UNAVAILABLE")
        return [dict(event) for event in events], summary

    rule_by_directive: dict[str, Any] = {}
    try:
        conditional_repository = conditional_rule_repository()
        rule_by_directive = conditional_repository.find_by_directive_ids(
            {item.directive_id for item in correlations.values()}
        )
    except ConditionalRuleUnavailable:
        summary.update(status="DEGRADED", error="CONDITIONAL_AUTHORITY_UNAVAILABLE")

    projected: list[dict[str, Any]] = []
    attributed = 0
    for raw_event in events:
        event = dict(raw_event)
        order_no = str(
            event.get("broker_order_id") or event.get("order_no") or ""
        ).strip()
        correlation = correlations.get(order_no)
        if correlation is None or not _matches_internal_correlation(event, correlation):
            projected.append(event)
            continue
        rule = rule_by_directive.get(correlation.directive_id)
        event.update(
            {
                "origin": (
                    "INTERNAL_CONDITIONAL_ORDER"
                    if rule is not None
                    else "INTERNAL_USER_ORDER"
                ),
                "correlation_status": "ATTRIBUTED",
                "correlation_source": "execution.user_order_request_events",
                "internal_broker_order_id": correlation.broker_order_id,
                "directive_id": correlation.directive_id,
                "directive_state": correlation.directive_state,
                "directive_leg_state": correlation.leg_state,
                "order_request_id": correlation.order_request_id,
                "client_request_id": correlation.client_request_id,
                "request_source": correlation.request_source,
                "conditional_rule_id": rule.rule_id if rule is not None else None,
                "conditional_rule_state": (
                    rule.state.value if rule is not None else None
                ),
            }
        )
        attributed += 1
        projected.append(event)
    summary["attributed"] = attributed
    summary["unattributed"] = max(0, len(current_events) - attributed)
    return projected, summary


def apply_fill(local: dict[str, Decimal], event: dict[str, Any]) -> None:
    """체결 → 로컬 포지션 변경. 파이프라인의 'Position Event' 단계.

    브로커 확인(t0424)을 기다리지 않고 먼저 움직인다. 확인은 그다음 단계이고,
    어긋난 값은 `compare_positions()`가 드러낸다.
    """
    symbol = event.get("symbol")
    quantity = event.get("quantity")
    if not symbol or quantity is None:
        return
    try:
        amount = Decimal(str(quantity))
    except Exception:  # noqa: BLE001 - 해석 못 하는 수량으로 포지션을 흔들지 않는다
        return
    if event.get("side") == "매도":
        amount = -amount
    elif event.get("side") != "매수":
        return  # 매수·매도 중 무엇인지 모르면 부호를 찍지 않는다
    updated = local.get(symbol, Decimal(0)) + amount
    if updated == 0:
        local.pop(symbol, None)
    else:
        local[symbol] = updated


def compare_positions(local: dict[str, Decimal], holdings: dict[str, Any]) -> list[dict[str, str]]:
    """로컬 포지션과 브로커 잔고의 차이. 같으면 빈 목록이다."""
    broker: dict[str, Decimal] = {}
    for row in holdings.get("rows", []):
        symbol = row.get("symbol")
        if not symbol:
            continue
        try:
            broker[symbol] = Decimal(row.get("quantity") or "0")
        except Exception:  # noqa: BLE001
            continue
    drift = []
    for symbol in sorted(set(local) | set(broker)):
        mine = local.get(symbol, Decimal(0))
        theirs = broker.get(symbol, Decimal(0))
        if mine != theirs:
            drift.append({"symbol": symbol, "local": str(mine), "broker": str(theirs)})
    return drift


# --------------------------------------------------------------------------
# 피드 — WebSocket 계좌등록 1개를 프로세스가 공유한다
# --------------------------------------------------------------------------

class _Feed:
    def __init__(self) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self.status = "IDLE"
        self.error: str | None = None
        self.connected_at: str | None = None
        self.seq = 0
        self.account: str | None = None
        self.account_error: str | None = None
        self.local_positions: dict[str, Decimal] = {}
        # 프로세스 최초의 t0424 응답은 비교 대상이 아니라 기준선이다.
        # 계좌에 이미 보유 중인 종목을 빈 로컬 상태와 비교하면 정상 잔고가
        # 전부 drift로 표시된다.
        self.positions_initialized = False
        self.holdings: dict[str, Any] | None = None
        self.holdings_as_of: str | None = None
        self.holdings_error: str | None = None
        self.today_activity: dict[str, Any] | None = None
        self.today_activity_as_of: str | None = None
        self.today_activity_error: str | None = None
        self.drift: list[dict[str, str]] = []
        self._task: asyncio.Task[None] | None = None

    def ingest(self, message: dict[str, Any]) -> str | None:
        """등록 ack는 흘리고 주문 사건만 받는다. 받았으면 그 종류를 돌려준다."""
        header = message.get("header") or {}
        body = message.get("body")
        tr_cd = str(header.get("tr_cd") or "").strip()
        if tr_cd not in _TR_TO_KIND or not isinstance(body, dict):
            return None
        # 계좌등록 응답은 {rsp_cd, rsp_msg}뿐이다. 주문 사건으로 세지 않는다.
        if set(body).issubset({"rsp_cd", "rsp_msg"}):
            return None
        self.seq += 1
        event = normalize_order_event(tr_cd, body, self.seq)
        self.events.appendleft(event)
        if not self.account:
            account = _account(body)
            if account:
                # CSPAQ12200 can transiently fail while the authenticated
                # realtime channel is already usable.  Once an order event
                # proves which account the channel belongs to, the old REST
                # error is no longer the current account state.
                self.account = account
                self.account_error = None
        if event["kind"] == "FILLED":
            apply_fill(self.local_positions, event)
        return event["kind"]

    def sync_holdings(self, holdings: dict[str, Any]) -> None:
        """t0424 확인 결과 반영. 브로커가 정본이지만 차이는 지우지 않는다."""
        if self.positions_initialized:
            self.drift = compare_positions(self.local_positions, holdings)
        else:
            # 첫 잔고 조회는 애플리케이션이 체결 이벤트를 받기 전의 기준선이다.
            # 이 시점의 로컬 {}와 계좌 잔고를 비교하지 않는다.
            self.drift = []
        self.holdings = holdings
        self.holdings_as_of = datetime.now(timezone.utc).isoformat()
        self.local_positions = {
            row["symbol"]: Decimal(row.get("quantity") or "0")
            for row in holdings.get("rows", [])
            if row.get("symbol")
        }
        self.positions_initialized = True

    def sync_today_activity(self, activity: dict[str, Any]) -> None:
        self.today_activity = activity
        self.today_activity_as_of = datetime.now(timezone.utc).isoformat()

    def start(self) -> None:
        if self._task is None or self._task.done():
            # ponytail: 첫 조회에서 켜고 끄지 않는다. 연결은 프로세스당 1개라
            # 새는 양이 없다. 유휴 해제가 필요해지면 마지막 폴링 시각으로 끊는다.
            self._task = asyncio.get_running_loop().create_task(_run_feed())


FEED = _Feed()


def _config(
    *,
    require_ws: bool = True,
) -> tuple[Any, str]:
    """(REST 자격, WS URL). 자격이 없으면 여기서 fail-closed."""
    import ls_openapi  # type: ignore[import-not-found]

    config = ls_openapi.LSOpenAPIConfig.from_env()
    suffix = "_PAPER" if config.environment == "PAPER" else ""
    if not require_ws:
        return config, ""
    ws_url = (os.getenv("LS_WS_BASE_URL" + suffix, "") or os.getenv("LS_WS_BASE_URL", "")).strip()
    if require_ws and not ws_url:
        raise RuntimeError("LS_WS_BASE_URL" + suffix + "가 설정되지 않았습니다.")
    # 수집 문서의 "접속 경로 `/websocket/stock`"은 실제 게이트웨이와 다르다.
    # `/websocket/stock`은 handshake에 응답조차 하지 않고, `/websocket`이 붙는다
    # (2026-08-18 두 포트 모두 실측). 바꾸기 전에 handshake부터 확인할 것.
    return config, ws_url.rstrip("/") + "/websocket"


async def _issue_token(config: Any) -> tuple[str, float]:
    """OAuth. 동기 클라이언트를 asyncio에 끌어들이지 않으려고 여기서만 비동기다.

    (토큰, 만료 epoch초). LS는 롤링 TTL이 아니라 **다음날 07:00 KST 고정 만료**이고,
    그날 안에 다시 요청하면 **같은 토큰을 그대로 돌려준다**(2026-08-20 실측: 49분
    뒤 재요청에도 iat이 11:25:01로 동일). 그래서 `expires_in`은 지금이 아니라
    최초 발급 시각 기준이라 `now + expires_in`으로 계산하면 경과한 만큼 만료를
    넘겨 잡는다 - 토큰이 싣고 온 `exp`를 그대로 쓴다.
    """
    from ls_http import ls_async_client  # type: ignore[import-not-found]

    async with ls_async_client(timeout=config.timeout_seconds) as client:
        response = await client.post(
            config.base_url + "/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "appkey": config.app_key,
                "appsecretkey": config.app_secret_key,
                "scope": config.scope,
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    response.raise_for_status()
    body = response.json()
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("LS OAuth 응답에 access_token이 없습니다")
    return token, _token_expiry(token)


def _token_expiry(token: str) -> float:
    """JWT payload의 `exp`(epoch초). 못 읽으면 4분 뒤로 본다.

    서명은 검증하지 않는다 - 우리가 이 토큰을 인증하는 게 아니라 LS가 알려 준
    만료 시각을 읽을 뿐이고, 못 읽으면 짧게 잡아 재발급으로 떨어진다.
    """
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except Exception:  # noqa: BLE001 - 만료를 못 읽는 것은 오류가 아니라 짧은 캐시다
        return time.time() + 240.0


_token_cache: dict[tuple[str, str], tuple[str, float]] = {}


async def _access_token(config: Any) -> str:
    """같은 환경·App Key의 토큰만 프로세스가 공유한다.

    거래내역 조회마다 OAuth를 새로 치면 호출 한도를 그쪽에 쓰게 된다.
    LS_ENV가 바뀐 뒤 이전 환경의 토큰을 재사용하지 않도록 환경·App Key별로
    캐시를 분리한다.
    만료는 토큰이 싣고 온 exp를 쓰되 1분 일찍 버린다 - 만료된 토큰으로 조회하면
    조용히 401이다. 예전에는 실제 수명을 몰라 240초로 두었는데, 그러면 하루 한
    장이면 될 것을 4분마다 새로 받는다(LS 권장은 하루 1회).
    """
    now = time.time()
    cache_key = (config.environment, config.app_key)
    cached = _token_cache.get(cache_key)
    if cached and cached[1] > now:
        return cached[0]
    token, expires_at = await _issue_token(config)
    _token_cache[cache_key] = (token, max(now + 60.0, expires_at - 60.0))
    return token


async def _post_tr(
    config: Any,
    token: str,
    tr_cd: str,
    payload: dict[str, Any],
    path: str = "/stock/accno",
) -> dict[str, Any]:
    body, _ = await _post_tr_page(config, token, tr_cd, payload, path=path)
    return body


async def _post_tr_page(
    config: Any,
    token: str,
    tr_cd: str,
    payload: dict[str, Any],
    path: str = "/stock/accno",
    *,
    tr_cont: str = "N",
    tr_cont_key: str = "",
) -> tuple[dict[str, Any], dict[str, str]]:
    """Call one LS page and retain only non-sensitive continuation metadata."""
    # 일반 httpx 클라이언트가 아니다 - LS는 `tr_cont_key`를 NUL로 패딩해 돌려주고
    # h11은 그런 헤더를 가진 응답을 통째로 버린다. 근거는 `ls_http` docstring.
    from ls_http import ls_async_client  # type: ignore[import-not-found]

    async with ls_async_client(timeout=config.timeout_seconds) as client:
        response = await client.post(
            config.base_url + path,
            json=payload,
            headers={
                "content-type": "application/json; charset=UTF-8",
                "authorization": "Bearer " + token,
                "tr_cd": tr_cd,
                "tr_cont": tr_cont,
                "tr_cont_key": tr_cont_key,
            },
        )
    response.raise_for_status()
    # JSON number는 double이라 Decimal이 깨진다. 금액·수량은 Decimal로 받는다.
    body = json.loads(response.text, parse_float=Decimal)
    headers = getattr(response, "headers", {})

    def _clean_header(name: str) -> str:
        try:
            value = headers.get(name, "")
        except (AttributeError, TypeError):
            value = ""
        return str(value or "").replace("\x00", "").strip()

    metadata = {
        "tr_cont": _clean_header("tr_cont").upper(),
        "tr_cont_key": _clean_header("tr_cont_key"),
    }
    return (body if isinstance(body, dict) else {}), metadata


def _ls_error_detail(exc: Exception) -> str:
    """브로커 인증 거부의 원인을 화면에 전달하되 자격값은 노출하지 않는다."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status in {401, 403} and response is not None:
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - 원래 예외를 유지한다
            body = None
        if isinstance(body, dict):
            description = body.get("error_description")
            if isinstance(description, str) and description.strip():
                return f"{description.strip()} (HTTP {status})"
        return f"LS 인증이 거부되었습니다 (HTTP {status})"
    return f"{type(exc).__name__}: {exc}"


async def _fetch_account_no(config: Any, token: str) -> str | None:
    """계좌번호를 브로커에게 묻는다.

    appkey에 이미 계좌가 묶여 있으므로 주문가능금액 조회가 자기 계좌번호를
    되돌려 준다. 사람이 `.env`에 옮겨 적을 이유가 없고, 옮겨 적으면 실제로
    붙는 계좌와 화면에 뜨는 계좌가 갈라질 수 있다.
    """
    body = await _post_tr(config, token, "CSPAQ12200", {"CSPAQ12200InBlock1": {"BalCreTp": "0"}})
    block = body.get("CSPAQ12200OutBlock1")
    if not isinstance(block, dict):
        return None
    return str(block.get("AcntNo") or "").strip() or None


async def _fetch_holdings(config: Any, token: str) -> dict[str, Any]:
    """t0424 잔고 확인. `chegb=2`(체결기준)로 부른다 — 파이프라인의 확인 단계."""
    body = await _post_tr(
        config,
        token,
        "t0424",
        {
            "t0424InBlock": {
                "prcgb": "1",
                "chegb": "2",
                "dangb": "0",
                "charge": "1",
                "cts_expcode": "",
            }
        },
    )
    return normalize_holdings(body)


async def _fetch_today_activity(config: Any, token: str) -> dict[str, Any]:
    """t0150으로 당일 매매 금액·수수료·세금 요약을 조회한다."""
    body = await _post_tr(
        config,
        token,
        "t0150",
        {
            "t0150InBlock": {
                "cts_medosu": "",
                "cts_expcode": "",
                "cts_price": "",
                "cts_middiv": "",
            }
        },
    )
    return normalize_today_activity(body)


async def _fetch_today_activity_fallback(config: Any, token: str) -> dict[str, Any]:
    """Use the working normalized trade ledger when t0150 omits its summary."""

    payload = await _fetch_today_trades(config, token)
    return summarize_today_trade_rows(payload)


async def _fetch_today_executions(
    config: Any, token: str
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    """당일 주문·체결내역에서 원장 색인과 접수 사건을 함께 만든다."""
    body = await _post_tr(
        config,
        token,
        "CSPAQ13700",
        {
            "CSPAQ13700InBlock1": {
                "OrdMktCode": "00",
                "BnsTpCode": "0",
                "IsuNo": "",
                "ExecYn": "1",
                "OrdDt": date.today().strftime("%Y%m%d"),
                "SrtOrdNo2": 0,
                "BkseqTpCode": "0",
                "OrdPtnCode": "00",
            }
        },
    )
    return normalize_executions(body), normalize_accepted_orders(body)


async def _fetch_today_trades(config: Any, token: str) -> list[dict[str, Any]]:
    """당일 매매일지를 원장 줄로 받는다. 결제 전 구간을 메우는 원천이다."""
    body = await _post_tr(
        config,
        token,
        "t0150",
        {"t0150InBlock": {"cts_medosu": "", "cts_expcode": "", "cts_price": "", "cts_middiv": ""}},
    )
    return normalize_today_trades(body, date.today().isoformat())["rows"]


async def _fetch_ledger(config: Any, token: str, start: date, end: date) -> dict[str, Any]:
    """계좌 거래내역. 조회 TR 하나만 부르고 주문 경로는 여기에 없다.

    `QryTp="0"`(전체)로 부른다. 상품유형·종목대분류·종목번호를 비워 두면 계좌의
    모든 거래가 온다 - 회계는 특정 종목이 아니라 계좌 전체를 본다.
    """
    body = await _post_tr(
        config,
        token,
        "CDPCQ04700",
        {
            "CDPCQ04700InBlock1": {
                "RecCnt": 1,
                "QryTp": "0",
                "QrySrtDt": start.strftime("%Y%m%d"),
                "QryEndDt": end.strftime("%Y%m%d"),
                "SrtNo": 0,
                "PdptnCode": "",
                "IsuLgclssCode": "",
                "IsuNo": "",
            }
        },
    )
    return normalize_ledger(body)


async def _fetch_market_ranking(config: Any, token: str, ranking: str) -> dict[str, Any]:
    definition = MARKET_RANKINGS.get(ranking)
    if definition is None:
        raise ValueError(f"지원하지 않는 시장 순위 종류입니다: {ranking}")
    body = await _post_tr(
        config,
        token,
        definition["tr_cd"],
        definition["payload"],
        path="/stock/high-item",
    )
    return normalize_market_ranking(body, ranking)


async def _resync(config: Any, token: str) -> None:
    """브로커 잔고와 맞춘다. 실패해도 구독은 계속 돈다."""
    try:
        FEED.sync_holdings(await _fetch_holdings(config, token))
    except Exception as exc:  # noqa: BLE001 - 조회 실패를 '잔고 없음'으로 위장하지 않는다
        FEED.holdings_error = _ls_error_detail(exc)[:200]
    else:
        FEED.holdings_error = None



async def _resync_today_activity(config: Any, token: str) -> None:
    """당일 매매 요약 실패가 계좌 잔고 스트림을 막지 않게 별도로 기록한다."""
    try:
        FEED.sync_today_activity(await _fetch_today_activity(config, token))
    except Exception as exc:  # noqa: BLE001 - PAPER 응답 변형은 행 기반으로 복구한다
        try:
            FEED.sync_today_activity(
                await _fetch_today_activity_fallback(config, token)
            )
        except Exception:  # noqa: BLE001 - 카드만 비활성화하고 계좌 스트림은 유지한다
            FEED.today_activity_error = _ls_error_detail(exc)[:300]
        else:
            FEED.today_activity_error = None
    else:
        FEED.today_activity_error = None


def _connect_order_stream(ws_url: str) -> Any:
    import websockets

    # LS realtime does not answer WebSocket protocol ping frames. Disabling the
    # client keepalive prevents a healthy idle stream from being closed locally.
    return websockets.connect(ws_url, ping_interval=None)


async def _run_feed() -> None:
    backoff = 1.0
    while True:
        try:
            config, ws_url = _config()
            token, _ = await _issue_token(config)

            configured_account = _configured_account(config.environment)
            if configured_account:
                FEED.account = configured_account
                FEED.account_error = None
            elif not FEED.account:
                try:
                    FEED.account = await _fetch_account_no(config, token)
                except Exception as exc:  # noqa: BLE001 - 계좌 조회 실패가 구독을 막지 않는다
                    FEED.account_error = _ls_error_detail(exc)[:200]
                else:
                    FEED.account_error = None

            # 잔고와 당일 거래는 REST 조회다. WebSocket opening handshake가
            # 지연되거나 실패해도 대시보드의 계좌 Projection까지 비우지 않는다.
            await _resync(config, token)
            await _resync_today_activity(config, token)

            # 계좌등록 (tr_type = 1)
            #
            # LS 게이트웨이는 WebSocket protocol ping에 pong을 돌려주지 않는다.
            # websockets의 client keepalive를 켜면 30초 뒤 ping을 보내고 기본
            # ping_timeout(20초)이 지난 약 50초 시점에 정상 연결을 스스로
            # 끊는다. 게이트웨이는 방금 끊긴 세션을 바로 정리하지 않아 이어지는
            # opening handshake도 timeout이 나므로 대시보드에는 연결 오류가
            # 계속 남았다. LS 주문 push 자체가 연결의 liveness 원천이므로 protocol
            # keepalive는 끄고, 실제 close/EOF가 왔을 때 아래 재연결 루프를 탄다.
            async with _connect_order_stream(ws_url) as socket:
                for tr_cd in _TR_TO_KIND:
                    await socket.send(
                        json.dumps(
                            {
                                "header": {"token": token, "tr_type": "1"},
                                "body": {"tr_cd": tr_cd, "tr_key": ""},
                            }
                        )
                    )
                FEED.status = "CONNECTED"
                FEED.error = None
                FEED.connected_at = datetime.now(timezone.utc).isoformat()
                backoff = 1.0
                while True:
                    try:
                        raw = await asyncio.wait_for(
                            socket.recv(), timeout=ACCOUNT_PROJECTION_RESYNC_SECONDS
                        )
                    except asyncio.TimeoutError:
                        # 주문 push가 누락되거나 조용한 장에서도 브로커 잔고가
                        # 최초 접속 시각에 고정되지 않게 REST 정본을 주기 갱신한다.
                        await _resync(config, token)
                        await _resync_today_activity(config, token)
                        continue
                    try:
                        message = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if FEED.ingest(message) == "FILLED":
                        # ponytail: 체결 처리 중에는 수신이 잠깐 멈춘다. 우리
                        # 주문량에서는 문제가 안 된다. 체결이 몰려 밀리면 별도
                        # 태스크로 빼고 연속 체결을 하나로 합친다.
                        await _resync(config, token)
                        await _resync_today_activity(config, token)
        except asyncio.CancelledError:
            FEED.status = "STOPPED"
            raise
        except Exception as exc:  # noqa: BLE001 - 끊김을 "사건 없음"으로 위장하지 않는다
            FEED.status = "DISCONNECTED"
            FEED.error = _ls_error_detail(exc)[:300]
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


# --------------------------------------------------------------------------
# 조회
# --------------------------------------------------------------------------

def _fixture_portfolio_live() -> dict[str, Any]:
    """Return a deterministic empty PAPER view for the local mock stack."""

    now = datetime.now(timezone.utc).isoformat()
    zero_summary = {
        "buy_quantity": "0",
        "sell_quantity": "0",
        "buy_amount": "0",
        "sell_amount": "0",
        "total_amount": "0",
        "total_fee": "0",
        "total_tax": "0",
        "total_settlement": "0",
    }
    return {
        "schema_version": "trading.portfolio-live.v1",
        "environment": "PAPER",
        "environment_label": "모의투자",
        "account": {"registered": True, "masked": "모의계좌", "error": None},
        "stream": {"status": "IDLE", "error": None, "connected_at": None},
        "orders": {
            "kinds": [{"kind": kind, "label": KIND_LABELS[kind]} for kind in KINDS],
            "counts": {kind: 0 for kind in KINDS},
            "source": "LOCAL_FIXTURE",
            "error": None,
            "correlation": {
                "status": "READY",
                "source": "LOCAL_FIXTURE",
                "attributed": 0,
                "unattributed": 0,
                "error": None,
            },
            "recent": [],
        },
        "holdings": {
            "as_of": now,
            "error": None,
            "synced": True,
            "drift": [],
            "net_asset": "0",
            "realized_pnl": "0",
            "purchase_amount": "0",
            "valuation": "0",
            "valuation_pnl": "0",
            "rows": [],
        },
        "today_activity": {
            "as_of": now,
            "error": None,
            "data": {"trade_count": 0, "summary": zero_summary},
        },
        "server_time": now,
        "authoritative": False,
        "official_nav_source": "/accounting/v1/ledgers/{ledger_id}",
    }

def _registered_account() -> str | None:
    """정본은 브로커가 말해 준 값이다. `.env`는 계좌가 여럿일 때의 덮어쓰기용."""
    return _configured_account() or FEED.account


def _configured_account(environment: str | None = None) -> str | None:
    selected = (environment or os.getenv("LS_ENV", "LIVE")).strip().upper()
    suffix = "_PAPER" if selected == "PAPER" else ""
    return (os.getenv("LS_ACCOUNT_NO" + suffix, "") or "").strip() or None


@router.get("/ui/portfolio/live", operation_id="portfolio_live")
async def portfolio_live(limit: int = 50) -> dict[str, Any]:
    """주문 상태와 브로커 잔고. 화면이 폴링으로 읽는다."""
    if PORTFOLIO_LIVE_MODE == "fixture":
        return _fixture_portfolio_live()
    if not ENABLE_LS_ORDER_EVENTS:
        raise HTTPException(
            503,
            "브로커 실시간 연동은 기본 비활성화 상태입니다 "
            "(ENABLE_LS_ORDER_EVENTS=true 로 엽니다).",
        )
    FEED.start()
    environment = os.getenv("LS_ENV", "LIVE").strip().upper()
    history_error: str | None = None
    order_source = "CDPCQ04700+CSPAQ13700+SC_REALTIME"
    try:
        # 체결은 계좌 거래내역을 기준으로 삼고, 접수·정정·취소·거부는
        # 실시간 피드에서 보충한다. 3초 폴링과 TR 호출 제한을 함께 고려한
        # 짧은 캐시다.
        ledger, _, _ = await _load_ledger(cache_seconds=ORDER_HISTORY_CACHE_SECONDS)
        cached_day, accepted_orders = _accepted_order_cache
        if cached_day != date.today().isoformat():
            accepted_orders = []
        recent_orders = merge_order_events(
            ledger_to_order_events(ledger, limit),
            list(FEED.events) + accepted_orders,
            limit,
        )
    except Exception as exc:  # noqa: BLE001 - 잔고/스트림 화면을 거래내역 장애로 막지 않는다
        history_error = _ls_error_detail(exc)[:300]
        order_source = "SC_REALTIME_FALLBACK"
        recent_orders = list(FEED.events)[: max(1, min(limit, MAX_EVENTS))]

    # LS 주문번호와 기존 BROKER_EXECUTION_SNAPSHOT을 읽기 전용으로 결합한다.
    # DB 장애는 브로커 주문/잔고 화면을 막지 않고 출처만 DEGRADED로 남긴다.
    recent_orders, correlation = await asyncio.to_thread(
        _project_internal_order_correlations,
        recent_orders,
    )
    today_activity = reconcile_today_activity(
        FEED.today_activity,
        recent_orders,
        datetime.now(KST).date().isoformat(),
    )

    account_no = _registered_account()
    account_masked = mask_account(account_no)
    for event in recent_orders:
        # 주문 사건만 따로 떼어 로그로 남겨도 어느 PAPER 계좌를 읽은 것인지
        # 확인할 수 있게 한다. 전체 계좌번호는 절대 싣지 않는다.
        event["account_masked"] = account_masked
    counts = {kind: 0 for kind in KINDS}
    for event in recent_orders:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    return {
        "schema_version": "trading.portfolio-live.v1",
        "environment": environment,
        "environment_label": "모의투자" if environment == "PAPER" else "실전투자",
        "account": {
            "registered": account_no is not None,
            "masked": account_masked,
            "error": FEED.account_error,
        },
        "stream": {
            "status": FEED.status,
            "error": FEED.error,
            "connected_at": FEED.connected_at,
        },
        "orders": {
            "kinds": [{"kind": kind, "label": KIND_LABELS[kind]} for kind in KINDS],
            "counts": counts,
            "source": order_source,
            "error": history_error,
            "correlation": correlation,
            "recent": recent_orders,
        },
        "holdings": {
            "as_of": FEED.holdings_as_of,
            "error": FEED.holdings_error,
            # 로컬 상태와 브로커 잔고가 어긋나면 감춘 채 넘어가지 않는다.
            "synced": None if FEED.holdings is None else not FEED.drift,
            "drift": FEED.drift,
            **(FEED.holdings or {"net_asset": None, "realized_pnl": None,
                                 "purchase_amount": None, "valuation": None,
                                 "valuation_pnl": None, "rows": []}),
        },
        "today_activity": {
            "as_of": FEED.today_activity_as_of,
            "error": FEED.today_activity_error,
            "data": today_activity,
        },
        "server_time": datetime.now(timezone.utc).isoformat(),
        # 브로커 값이 공식 NAV로 둔갑하지 않게 하는 계약 두 줄.
        "authoritative": False,
        "official_nav_source": "/accounting/v1/ledgers/{ledger_id}",
    }


_ledger_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_accepted_order_cache: tuple[str, list[dict[str, Any]]] = ("", [])
# 계좌 조회 TR은 초당 1~2건이다. 대시보드와 회계 화면이 동시에 폴링하면 캐시
# 키가 달라 두 호출이 같은 초에 나가고 그대로 거부당한다(오늘 90일 조회 502의
# 원인). 실제 호출만 직렬화하고 최소 간격을 둔다.
_tr_gate = asyncio.Lock()
_tr_last_call = 0.0
_market_ranking_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_market_ranking_gate = asyncio.Lock()


async def _tr_slot() -> None:
    global _tr_last_call
    now = time.monotonic()
    wait = 1.1 - (now - _tr_last_call)
    if wait > 0:
        await asyncio.sleep(wait)
    _tr_last_call = time.monotonic()


def _accounting_tr_requests(
    start: date,
    end: date,
    previous: date,
    *,
    symbol: str | None = None,
    order_price: str | None = None,
    side: str | None = None,
    loan_detail_class: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Official `/stock/accno` request shapes used by the accounting view."""

    requests: dict[str, dict[str, Any]] = {
        "CDPCQ04700": {
            "payload": {
                "CDPCQ04700InBlock1": {
                    "QryTp": "0",
                    "QrySrtDt": start.strftime("%Y%m%d"),
                    "QryEndDt": end.strftime("%Y%m%d"),
                    "SrtNo": 0,
                    "PdptnCode": "01",
                    "IsuLgclssCode": "00",
                    "IsuNo": "",
                }
            },
            "list_blocks": ("CDPCQ04700OutBlock3",),
        },
        "CSPAQ12200": {
            "payload": {"CSPAQ12200InBlock1": {"BalCreTp": "0"}},
            "list_blocks": (),
        },
        "CSPAQ12300": {
            "payload": {
                "CSPAQ12300InBlock1": {
                    "BalCreTp": "0",
                    "CmsnAppTpCode": "1",
                    "D2balBaseQryTp": "0",
                    "UprcTpCode": "1",
                }
            },
            "list_blocks": ("CSPAQ12300OutBlock3",),
        },
        "CSPAQ13700": {
            "payload": {
                "CSPAQ13700InBlock1": {
                    "OrdMktCode": "00",
                    "BnsTpCode": "0",
                    "IsuNo": "",
                    "ExecYn": "0",
                    "OrdDt": end.strftime("%Y%m%d"),
                    "SrtOrdNo2": 0,
                    "BkseqTpCode": "0",
                    "OrdPtnCode": "00",
                }
            },
            "list_blocks": ("CSPAQ13700OutBlock3",),
        },
        "CSPAQ22200": {
            "payload": {"CSPAQ22200InBlock1": {"BalCreTp": "0"}},
            "list_blocks": (),
        },
        "FOCCQ33600": {
            "payload": {
                "FOCCQ33600InBlock1": {
                    "QrySrtDt": start.strftime("%Y%m%d"),
                    "QryEndDt": end.strftime("%Y%m%d"),
                    "TermTp": "1",
                }
            },
            "list_blocks": ("FOCCQ33600OutBlock3",),
        },
        "t0150": {
            "payload": {
                "t0150InBlock": {
                    "cts_medosu": "",
                    "cts_expcode": "",
                    "cts_price": "",
                    "cts_middiv": "",
                }
            },
            "list_blocks": ("t0150OutBlock1",),
            "continuation": (
                "t0150InBlock",
                "t0150OutBlock",
                ("cts_medosu", "cts_expcode", "cts_price", "cts_middiv"),
            ),
        },
        "t0151": {
            "payload": {
                "t0151InBlock": {
                    "date": previous.strftime("%Y%m%d"),
                    "cts_medosu": "",
                    "cts_expcode": "",
                    "cts_price": "",
                    "cts_middiv": "",
                }
            },
            "list_blocks": ("t0151OutBlock1",),
            "continuation": (
                "t0151InBlock",
                "t0151OutBlock",
                ("cts_medosu", "cts_expcode", "cts_price", "cts_middiv"),
            ),
        },
        "t0424": {
            "payload": {
                "t0424InBlock": {
                    "prcgb": "1",
                    "chegb": "2",
                    "dangb": "0",
                    "charge": "1",
                    "cts_expcode": "",
                }
            },
            "list_blocks": ("t0424OutBlock1",),
            "continuation": ("t0424InBlock", "t0424OutBlock", ("cts_expcode",)),
        },
        "t0425": {
            "payload": {
                "t0425InBlock": {
                    "expcode": "",
                    "chegb": "0",
                    "medosu": "0",
                    "sortgb": "1",
                    "cts_ordno": "",
                }
            },
            "list_blocks": ("t0425OutBlock1",),
            "continuation": ("t0425InBlock", "t0425OutBlock", ("cts_ordno",)),
        },
    }
    clean_symbol = str(symbol or "").strip()
    clean_price = str(order_price or "").replace(",", "").strip()
    if clean_symbol and clean_price and loan_detail_class:
        requests["CSPAQ00600"] = {
            "payload": {
                "CSPAQ00600InBlock1": {
                    "LoanDtlClssCode": str(loan_detail_class).strip(),
                    "IsuNo": clean_symbol,
                    "OrdPrc": clean_price,
                    "CommdaCode": "41",
                }
            },
            "list_blocks": (),
        }
    if clean_symbol and clean_price and side:
        requests["CSPBQ00200"] = {
            "payload": {
                "CSPBQ00200InBlock1": {
                    "BnsTpCode": str(side).strip(),
                    "IsuNo": clean_symbol,
                    "OrdPrc": clean_price,
                }
            },
            "list_blocks": (),
        }
    return requests


async def _fetch_tr_pages(
    config: Any,
    token: str,
    tr_cd: str,
    definition: Mapping[str, Any],
) -> dict[str, Any]:
    """Read and merge an LS continuation chain without leaking its headers."""

    source_payload = definition.get("payload")
    if not isinstance(source_payload, Mapping):
        raise ValueError(f"{tr_cd}: payload가 없습니다")
    payload = {
        str(key): dict(value) if isinstance(value, Mapping) else value
        for key, value in source_payload.items()
    }
    list_blocks = tuple(str(item) for item in definition.get("list_blocks", ()))
    continuation = definition.get("continuation")
    merged: dict[str, Any] = {}
    tr_cont = "N"
    tr_cont_key = ""
    seen_tokens: set[tuple[str, tuple[str, ...]]] = set()
    more = False
    pages = 0

    for _ in range(ACCOUNTING_EVIDENCE_MAX_PAGES):
        async with _tr_gate:
            await _tr_slot()
            body, headers = await _post_tr_page(
                config,
                token,
                tr_cd,
                payload,
                tr_cont=tr_cont,
                tr_cont_key=tr_cont_key,
            )
        pages += 1
        for key, value in body.items():
            if key in list_blocks and isinstance(value, list):
                merged.setdefault(key, []).extend(value)
            elif key not in merged or value not in (None, "", [], {}):
                merged[key] = value

        tr_cont_key = headers.get("tr_cont_key", "")
        more = headers.get("tr_cont", "") in {"Y", "F", "M"}
        cts_values: tuple[str, ...] = ()
        if continuation:
            input_block, output_block, fields = continuation
            output = body.get(output_block)
            target = payload.get(input_block)
            if isinstance(output, Mapping) and isinstance(target, dict):
                cts_values = tuple(str(output.get(field) or "").strip() for field in fields)
                for field, value in zip(fields, cts_values, strict=True):
                    target[field] = value
        if not more:
            break
        token_marker = (tr_cont_key, cts_values)
        if token_marker in seen_tokens or (not tr_cont_key and not any(cts_values)):
            break
        seen_tokens.add(token_marker)
        tr_cont = "Y"

    return {
        "body": merged,
        "meta": {
            "pages": pages,
            "complete": not more,
            "truncated": more,
        },
    }


async def _collect_accounting_evidence(
    start: date,
    end: date,
    previous: date,
    *,
    symbol: str | None = None,
    order_price: str | None = None,
    side: str | None = None,
    loan_detail_class: str | None = None,
) -> dict[str, Any]:
    config, _ = _config(require_ws=False)
    token = await _access_token(config)
    requests = _accounting_tr_requests(
        start,
        end,
        previous,
        symbol=symbol,
        order_price=order_price,
        side=side,
        loan_detail_class=loan_detail_class,
    )
    responses: dict[str, Any] = {}
    for tr_cd, definition in requests.items():
        try:
            responses[tr_cd] = await _fetch_tr_pages(config, token, tr_cd, definition)
        except Exception as exc:  # noqa: BLE001 - 한 TR 실패가 나머지 증거를 지우면 안 된다
            responses[tr_cd] = {
                "error": _ls_error_detail(exc)[:200],
                "meta": {"pages": 0, "complete": False, "truncated": False},
            }
    return normalize_ls_accounting_evidence(
        responses,
        period_start=start,
        period_end=end,
        previous_date=previous,
        environment=config.environment,
    )


_accounting_evidence_cache: dict[
    tuple[str, str, str, str, str, str, str], tuple[float, dict[str, Any]]
] = {}
_accounting_evidence_refresh_gate = asyncio.Lock()
_accounting_evidence_refresh_task: asyncio.Task[None] | None = None


async def _load_accounting_evidence(
    *,
    days: int = ACCOUNTING_EVIDENCE_DAYS,
    previous_date: date | None = None,
    symbol: str | None = None,
    order_price: str | None = None,
    side: str | None = None,
    loan_detail_class: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    span = max(1, min(365, days))
    end = datetime.now(KST).date()
    start = end - timedelta(days=span - 1)
    previous = previous_date or (end - timedelta(days=1))
    key = (
        start.isoformat(),
        end.isoformat(),
        previous.isoformat(),
        str(symbol or ""),
        str(order_price or ""),
        str(side or ""),
        str(loan_detail_class or ""),
    )
    cached = _accounting_evidence_cache.get(key)
    if not force and cached and time.time() - cached[0] < ACCOUNTING_EVIDENCE_CACHE_SECONDS:
        return cached[1]
    # 백그라운드 갱신은 10개 TR의 호출간격 때문에 수 초 걸린다. 이미 한 번 본
    # 증거가 있으면 그동안 Agent 요청을 막지 않고 직전 관측치를 돌려준다.
    if not force and cached and _accounting_evidence_refresh_gate.locked():
        return cached[1]

    async with _accounting_evidence_refresh_gate:
        cached = _accounting_evidence_cache.get(key)
        if not force and cached and time.time() - cached[0] < ACCOUNTING_EVIDENCE_CACHE_SECONDS:
            return cached[1]
        try:
            evidence = await _collect_accounting_evidence(
                start,
                end,
                previous,
                symbol=symbol,
                order_price=order_price,
                side=side,
                loan_detail_class=loan_detail_class,
            )
        except Exception:
            if cached:
                return cached[1]
            raise
        _accounting_evidence_cache[key] = (time.time(), evidence)
        return evidence


async def _accounting_evidence_refresh_loop() -> None:
    while True:
        try:
            await _load_accounting_evidence(force=True)
        except Exception:  # noqa: BLE001 - 다음 주기에 다시 시도한다
            pass
        await asyncio.sleep(ACCOUNTING_EVIDENCE_REFRESH_SECONDS)


async def _start_accounting_evidence_refresh() -> None:
    global _accounting_evidence_refresh_task
    if ENABLE_LS_ACCOUNT_DATA and _accounting_evidence_refresh_task is None:
        _accounting_evidence_refresh_task = asyncio.create_task(
            _accounting_evidence_refresh_loop(), name="ls-accounting-evidence-refresh"
        )


async def _stop_accounting_evidence_refresh() -> None:
    global _accounting_evidence_refresh_task
    if _accounting_evidence_refresh_task is not None:
        _accounting_evidence_refresh_task.cancel()
        try:
            await _accounting_evidence_refresh_task
        except asyncio.CancelledError:
            pass
        _accounting_evidence_refresh_task = None


async def _load_ledger(
    days: int = LEDGER_DAYS,
    *,
    cache_seconds: int = LEDGER_CACHE_SECONDS,
) -> tuple[dict[str, Any], date, date]:
    """기간 원장을 공용 캐시에서 읽거나 새로 조회한다.

    두 원천을 합친다 - 결제까지 끝난 확정 거래내역과, 아직 결제 전이라 거기
    안 잡히는 **당일 매매**다. 확정분만 쓰면 오늘 거래한 날의 장부가 통째로
    비고(T+2), 매매일지만 쓰면 과거가 없다. 줄마다 `settlement`로 구분한다.
    """
    global _accepted_order_cache

    span = max(1, min(days, 365))
    end = date.today()
    start = end - timedelta(days=span - 1)
    key = (start.isoformat(), end.isoformat())

    cached = _ledger_cache.get(key)
    if cached and time.time() - cached[0] < max(0, cache_seconds):
        return cached[1], start, end

    async with _tr_gate:
        # 잠금 안에서 캐시를 다시 본다. 두 화면이 같이 들어오면 뒤차는 앞차가
        # 채워 둔 값을 쓰면 되고, 굳이 브로커를 한 번 더 때릴 이유가 없다.
        cached = _ledger_cache.get(key)
        if cached and time.time() - cached[0] < max(0, cache_seconds):
            return cached[1], start, end

        try:
            config, _ = _config(require_ws=False)
            token = await _access_token(config)
            # 실시간 구독이 아직 계좌번호를 채우지 못해도 거래내역 화면은 독립적으로
            # 동작해야 한다. 같은 자격으로 계좌번호를 먼저 확인한다.
            if not FEED.account:
                await _tr_slot()
                FEED.account = await _fetch_account_no(config, token)
            await _tr_slot()
            ledger = await _fetch_ledger(config, token, start, end)
        except Exception as exc:  # noqa: BLE001
            # 브로커가 죽었다고 이미 적어 둔 장부까지 사라지면 안 된다. 적어 둔
            # 것이 있으면 그것을 내보내고 조회 실패 사실만 같이 싣는다.
            account_key = mask_account(_registered_account()) or ""
            kept = LEDGER_STORE.read(account_key, start.isoformat(), end.isoformat())
            if not kept:
                raise
            merged = {
                "rows": kept,
                "totals": summarize_ledger_rows(kept),
                "cash_balance": kept[0].get("cash_after"),
                "notice": None,
                "persisted": True,
                "source_error": _ls_error_detail(exc)[:200],
                "today_error": None,
            }
            _ledger_cache[key] = (time.time(), merged)
            return merged, start, end

        # 당일 매매일지가 실패해도 확정분은 그대로 보여 준다. 사유만 남긴다.
        today_rows: list[dict[str, Any]] = []
        today_error: str | None = None
        try:
            await _tr_slot()
            today_rows = await _fetch_today_trades(config, token)
        except Exception as exc:  # noqa: BLE001 - 오늘분 실패를 '거래 없음'으로 위장하지 않는다
            today_error = _ls_error_detail(exc)[:200]
        else:
            # 시각·종목명은 있으면 좋은 값이다. 여기서 실패해도 금액은 그대로
            # 나가야 하므로 원장 전체를 막지 않는다.
            try:
                await _tr_slot()
                execution_index, accepted_orders = await _fetch_today_executions(config, token)
                attach_executions(today_rows, execution_index)
                _accepted_order_cache = (date.today().isoformat(), accepted_orders)
            except Exception:  # noqa: BLE001
                pass

        observed = today_rows + list(ledger.get("rows") or [])

        # ▶ 본 것을 적어 둔다.
        #   체결일과 결제일 사이에는 어느 조회로도 그 거래를 다시 못 가져온다
        #   (매매일지는 오늘만, 확정 거래내역은 결제 뒤에만 준다). 남기지 않으면
        #   날짜가 바뀌는 순간 장부가 빈다. 자세한 근거는 `ledger_store.py`.
        account_key = mask_account(_registered_account()) or ""
        LEDGER_STORE.record(account_key, observed)
        stored = LEDGER_STORE.read(account_key, start.isoformat(), end.isoformat())
        rows = stored or observed

        merged = {
            **ledger,
            "rows": rows,
            "totals": summarize_ledger_rows(rows),
            "today_error": today_error,
            "persisted": LEDGER_STORE.enabled,
            # 확정분이 비어 있는 사유는 줄이 채워졌으면 더 이상 안내가 아니다.
            "notice": None if rows else ledger.get("notice"),
        }
        _ledger_cache[key] = (time.time(), merged)
        return merged, start, end


async def _load_market_ranking(ranking: str) -> dict[str, Any]:
    cached = _market_ranking_cache.get(ranking)
    if cached and time.time() - cached[0] < max(0, MARKET_RANKING_CACHE_SECONDS):
        return cached[1]

    async with _market_ranking_gate:
        cached = _market_ranking_cache.get(ranking)
        if cached and time.time() - cached[0] < max(0, MARKET_RANKING_CACHE_SECONDS):
            return cached[1]
        config, _ = _config(require_ws=False)
        token = await _access_token(config)
        await _tr_slot()
        result = await _fetch_market_ranking(config, token, ranking)
        _market_ranking_cache[ranking] = (time.time(), result)
        return result


@router.get(
    "/internal/accounting/broker-evidence",
    operation_id="accounting_broker_evidence",
)
async def accounting_broker_evidence(
    days: int = ACCOUNTING_EVIDENCE_DAYS,
    previous_date: date | None = None,
    symbol: str | None = None,
    order_price: str | None = None,
    side: str | None = None,
    loan_detail_class: str | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Bounded, credential-free LS evidence for the Accounting Hermes context."""

    if not ENABLE_LS_ACCOUNT_DATA:
        raise HTTPException(
            503,
            "브로커 계좌 조회는 기본 비활성화 상태입니다 "
            "(ENABLE_LS_ACCOUNT_DATA=true 로 엽니다).",
        )
    parameter_values = (symbol, order_price, side, loan_detail_class)
    if any(value is not None for value in parameter_values):
        if not symbol or not order_price:
            raise HTTPException(400, "종목별 한도 조회에는 symbol과 order_price가 필요합니다.")
        if side is not None and side not in {"1", "2"}:
            raise HTTPException(400, "side는 1(매도) 또는 2(매수)여야 합니다.")
        if loan_detail_class is not None and loan_detail_class not in {"01", "03", "05", "07"}:
            raise HTTPException(400, "loan_detail_class는 01, 03, 05, 07 중 하나여야 합니다.")
    try:
        return await _load_accounting_evidence(
            days=days,
            previous_date=previous_date,
            symbol=symbol,
            order_price=order_price,
            side=side,
            loan_detail_class=loan_detail_class,
            force=refresh,
        )
    except Exception as exc:  # noqa: BLE001 - 빈 증거가 아니라 명시적 실패로 전달한다
        raise HTTPException(
            502, ("회계용 브로커 증거 조회 실패: " + _ls_error_detail(exc))[:400]
        ) from exc


@router.get("/ui/market/rankings", operation_id="market_rankings")
async def market_rankings(kind: str = "volume") -> dict[str, Any]:
    """시장 상위 종목 한 종류만 조회한다. 화면의 버튼 전환용 얇은 BFF다."""
    if not ENABLE_LS_MARKET_DATA:
        raise HTTPException(
            503,
            "브로커 시장 데이터 연동은 기본 비활성화 상태입니다 "
            "(ENABLE_LS_MARKET_DATA=true 로 엽니다).",
        )
    if kind not in MARKET_RANKINGS:
        raise HTTPException(400, "지원하지 않는 시장 순위 종류입니다.")
    try:
        ranking = await _load_market_ranking(kind)
    except Exception as exc:  # noqa: BLE001 - 화면에서 조회 실패와 빈 결과를 구분한다
        raise HTTPException(
            502, ("시장 상위 종목 조회 실패: " + _ls_error_detail(exc))[:400]
        ) from exc
    return {
        "schema_version": "market.rankings.v1",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "source": "LS /stock/high-item",
        **ranking,
    }


@router.get("/ui/portfolio/ledger", operation_id="portfolio_ledger")
async def portfolio_ledger(days: int = LEDGER_DAYS) -> dict[str, Any]:
    """계좌 거래내역 원장. 회계본부 화면이 읽는다.

    트레이딩의 `/ui/portfolio/live`와 원천이 다르다 - 저쪽은 주문·보유이고
    이쪽은 확정된 거래와 비용·세금이다. 둘을 한 응답에 합치지 않는다.
    """
    if not ENABLE_LS_ACCOUNT_DATA:
        raise HTTPException(
            503,
            "브로커 계좌 조회는 기본 비활성화 상태입니다 "
            "(ENABLE_LS_ACCOUNT_DATA=true 로 엽니다).",
        )
    try:
        ledger, start, end = await _load_ledger(cache_seconds=LEDGER_CACHE_SECONDS)
    except Exception as exc:  # noqa: BLE001 - 조회 실패를 '거래 없음'으로 위장하지 않는다
        raise HTTPException(
            502, ("거래내역 조회 실패: " + _ls_error_detail(exc))[:400]
        ) from exc
    span = (end - start).days + 1

    # 계좌 기본정보. 실시간 구독이 이미 잔고를 들고 있으면 그걸 쓰고, 회계 화면만
    # 열린 경우에는 한 번 조회한다 - 조회 TR 한도를 아끼되 화면이 비지는 않게.
    holdings_error = FEED.holdings_error
    if FEED.holdings is None:
        try:
            async with _tr_gate:
                if FEED.holdings is None:
                    config, _ = _config(require_ws=False)
                    token = await _access_token(config)
                    await _tr_slot()
                    FEED.sync_holdings(await _fetch_holdings(config, token))
        except Exception as exc:  # noqa: BLE001 - 잔고 조회 실패로 원장을 막지 않는다
            holdings_error = _ls_error_detail(exc)[:200]

    account_no = _registered_account()
    environment = os.getenv("LS_ENV", "LIVE").strip().upper()
    return {
        "schema_version": "accounting.ledger-transactions.v1",
        "environment": environment,
        "environment_label": "모의투자" if environment == "PAPER" else "실전투자",
        "account": {
            "registered": account_no is not None,
            "masked": mask_account(account_no),
        },
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": span},
        # 계좌 자체의 현재 모습. 기간 원장과 축이 달라 따로 싣는다.
        "account_summary": {
            "as_of": FEED.holdings_as_of,
            "error": holdings_error,
            "net_asset": (FEED.holdings or {}).get("net_asset"),
            "valuation": (FEED.holdings or {}).get("valuation"),
            "purchase_amount": (FEED.holdings or {}).get("purchase_amount"),
            "valuation_pnl": (FEED.holdings or {}).get("valuation_pnl"),
            "realized_pnl": (FEED.holdings or {}).get("realized_pnl"),
            "holding_count": len((FEED.holdings or {}).get("rows") or []),
        },
        "pnl": build_pnl(FEED.holdings, ledger.get("totals") or {}),
        **ledger,
        "server_time": datetime.now(timezone.utc).isoformat(),
        # 브로커 값이 공식 원장으로 둔갑하지 않게 하는 계약 두 줄.
        "authoritative": False,
        "official_nav_source": "/accounting/v1/ledgers/{ledger_id}",
    }


__all__ = ["router", "ENABLE_LS_ORDER_EVENTS", "ENABLE_LS_MARKET_DATA", "ENABLE_LS_ACCOUNT_DATA", "KIND_LABELS", "normalize_order_event",
           "normalize_holdings", "normalize_today_activity", "normalize_ledger",
           "normalize_accepted_orders",
           "normalize_market_ranking",
           "ledger_to_order_events", "merge_order_events", "build_pnl", "apply_fill",
           "compare_positions", "mask_account"]


if __name__ == "__main__":  # 자체 점검 - pytest 미도입(CLAUDE.md)
    # 계좌번호가 통째로 새지 않는가
    assert mask_account("12345678901") == "****8901"
    assert mask_account(None) is None

    # 0 패딩을 벗기되 float로 바꾸지 않는가
    assert _number("0000000010") == "10"
    assert _number("0000000000") == "0"
    assert _number("-0000000007") == "-7"
    assert _number(Decimal("1.20")) == "1.2"
    assert _number(71000) == "71000"
    assert _number(None) is None

    market = normalize_market_ranking(
        {
            "t1452OutBlock1": [
                {
                    "hname": "삼성전자",
                    "shcode": "005930",
                    "price": Decimal("71000"),
                    "diff": Decimal("1.25"),
                    "volume": Decimal("1234567"),
                },
            ]
        },
        "volume",
    )
    assert market["label"] == "거래량 상위"
    assert market["rows"][0]["symbol"] == "005930"
    assert market["rows"][0]["volume"] == "1234567"

    # 실시간(A005930)과 잔고 조회(005930)의 종목코드가 같은 키로 모이는가
    assert _symbol("A005930") == "005930" and _symbol("005930") == "005930"

    # 접수와 체결은 같은 뜻에 다른 이름을 쓴다. 둘 다 붙어야 한다.
    accepted = normalize_order_event("SC0", {
        "ordno": "0000000123", "shtcode": "A005930", "hname": "삼성전자",
        "bnstp": "2", "ordqty": "0000000010", "ordprice": "0000000071000",
        "ordtm": "091502000", "accno1": "12345678901", "accno2": "000000000",
    }, 1)
    assert accepted["kind"] == "ACCEPTED" and accepted["label"] == "접수"
    assert accepted["symbol"] == "005930" and accepted["symbol_name"] == "삼성전자"
    assert accepted["side"] == "매수" and accepted["quantity"] == "10"
    assert accepted["price"] == "71000" and accepted["order_no"] == "123"
    # LS 어휘가 화면 계약으로 새지 않는가
    assert "tr_cd" not in accepted and "raw" not in accepted

    filled = normalize_order_event("SC1", {
        "ordno": "0000000123", "shtnIsuno": "A005930", "Isunm": "삼성전자",
        "bnstp": "2", "execqty": "0000000004", "execprc": "0000000070900",
        "ordprc": "0000000071000", "exectime": "091503000", "unercqty": "0000000006",
    }, 2)
    assert filled["kind"] == "FILLED" and filled["quantity"] == "4"
    assert filled["price"] == "70900" and filled["unfilled_quantity"] == "6"

    # 문서에 필드가 없는 정정/취소/거부도 종류는 정확히 나온다
    rejected = normalize_order_event("SC4", {"ordno": "0000000123", "rjtqty": "0000000010"}, 3)
    assert rejected["kind"] == "REJECTED" and rejected["quantity"] == "10"
    assert rejected["symbol"] is None  # 모르는 것은 None으로 둔다
    assert normalize_order_event("SC2", {}, 4)["label"] == "정정"
    assert normalize_order_event("SC3", {}, 5)["label"] == "취소"

    # 모르는 매매구분 코드를 번역하지 않는가
    assert _side("9") == "9" and _side(None) is None

    # 체결 → 로컬 포지션 변경
    local: dict[str, Decimal] = {}
    apply_fill(local, filled)
    assert local == {"005930": Decimal(4)}, local
    apply_fill(local, {"symbol": "005930", "quantity": "4", "side": "매도"})
    assert local == {}, local  # 0이 되면 남기지 않는다
    apply_fill(local, {"symbol": "005930", "quantity": "3", "side": None})
    assert local == {}  # 부호를 모르면 흔들지 않는다

    # 잔고 조회 정규화 - LS 필드명이 밖으로 안 나간다
    holdings = normalize_holdings({
        "t0424OutBlock": {"sunamt": 1000000, "dtsunik": 0, "mamt": 700000,
                          "tappamt": 710000, "tdtsunik": 10000},
        "t0424OutBlock1": [{"expcode": "005930", "hname": "삼성전자", "janqty": 10,
                            "mdposqt": 10, "pamt": 70000, "mamt": 700000,
                            "price": 71000, "appamt": 710000, "dtsunik": 10000,
                            "sunikrt": Decimal("1.42"), "janrt": Decimal("71.00")}],
    })
    assert holdings["net_asset"] == "1000000" and holdings["valuation_pnl"] == "10000"
    row = holdings["rows"][0]
    assert row["symbol"] == "005930" and row["quantity"] == "10"
    assert row["average_cost"] == "70000" and row["return_rate"] == "1.42"
    assert not any(k.startswith("t0424") for k in holdings)

    today_activity = normalize_today_activity({
        "t0150OutBlock": {
            "msqty": 4, "mdqty": 2, "msamt": 400000, "mdamt": 210000,
            "tamt": 610000, "tfee": 120, "ttax": 80, "tadjamt": 609800,
        },
        "t0150OutBlock1": [{"medosu": "매수", "expcode": "005930"}],
    })
    assert today_activity["trade_count"] == 1
    assert today_activity["summary"]["buy_amount"] == "400000"

    # 로컬 상태와 브로커 잔고 대조 - 차이를 조용히 지우지 않는다
    assert compare_positions({"005930": Decimal(10)}, holdings) == []
    assert compare_positions({"005930": Decimal(7)}, holdings) == [
        {"symbol": "005930", "local": "7", "broker": "10"}
    ]
    assert compare_positions({"000660": Decimal(5)}, holdings) == [
        {"symbol": "000660", "local": "5", "broker": "0"},
        {"symbol": "005930", "local": "0", "broker": "10"},
    ]

    # 최초 잔고 조회는 기준선으로 잡아, 기존 보유 종목을 오탐하지 않는다
    feed = _Feed()
    feed.sync_holdings(holdings)
    assert feed.local_positions == {"005930": Decimal(10)}
    assert feed.drift == []

    # 기준선 이후의 체결·잔고 불일치는 계속 표시한다
    feed.local_positions = {"005930": Decimal(7)}
    feed.sync_holdings(holdings)
    assert feed.drift == [{"symbol": "005930", "local": "7", "broker": "10"}]

    # 계좌등록 응답을 주문 사건으로 세지 않는가
    feed = _Feed()
    assert feed.ingest({"header": {"tr_cd": "SC0"}, "body": {"rsp_cd": "00000", "rsp_msg": "정상"}}) is None
    assert feed.ingest({"header": {"tr_cd": "S3_"}, "body": {"price": "1"}}) is None
    assert feed.ingest({"header": {"tr_cd": "SC0"}, "body": {"ordno": "1", "accno1": "98765432109"}}) == "ACCEPTED"
    assert len(feed.events) == 1 and feed.account == "98765432109"

    # 거래내역 정규화 - 회계가 보는 축(비용·세금·손익)이 나오는가
    ledger = normalize_ledger({"CDPCQ04700OutBlock3": [
        {"TrdDt": "20260817", "TrdNo": 11, "TpCodeNm": "매수", "SmryNm": "주식매수",
         "IsuNo": "A005930", "IsuNm": "삼성전자", "TrdQty": 10, "TrdUprc": Decimal("71000"),
         "TrdAmt": 710000, "AdjstAmt": -710350, "CmsnAmt": 350, "Trtax": 0,
         "BnsplAmt": 0, "DpsBfbalAmt": 1000000, "DpsCrbalAmt": 289650, "CrcyCode": "KRW"},
        {"TrdDt": "20260818", "TrdNo": 12, "TpCodeNm": "매도", "SmryNm": "주식매도",
         "IsuNo": "A005930", "IsuNm": "삼성전자", "TrdQty": 10, "TrdUprc": Decimal("72000"),
         "TrdAmt": 720000, "AdjstAmt": 718344, "CmsnAmt": 356, "Trtax": 1300,
         "Ictax": 0, "Ihtax": 0, "BnsplAmt": 8294, "DpsCrbalAmt": 1007994, "CrcyCode": "KRW"},
    ]})
    assert ledger["totals"]["count"] == 2
    assert ledger["totals"]["commission"] == "706"          # 350 + 356
    assert ledger["totals"]["cost"] == "2006"               # 수수료 706 + 세금 1300
    assert ledger["totals"]["tax"] == "1300"                # 합계 필드가 없으면 거래세+소득세+주민세
    assert ledger["totals"]["realized_pnl"] == "8294"
    # 최신이 위로 온다 - 원장은 최근 거래부터 본다
    assert ledger["rows"][0]["trade_date"] == "2026-08-18"
    assert ledger["rows"][0]["symbol"] == "005930"
    assert ledger["cash_balance"] == "1007994"
    assert ledger["notice"] is None
    # LS 필드명이 화면 계약으로 새지 않는가
    assert not any(k.startswith(("Trd", "Cmsn", "Dps")) for k in ledger["rows"][0])
    ledger_events = ledger_to_order_events(ledger, 50)
    assert len(ledger_events) == 2
    assert ledger_events[0]["kind"] == "FILLED"
    assert ledger_events[0]["label"] == "체결"
    assert ledger_events[0]["side"] == "매도"
    assert ledger_events[0]["event_time"] is None
    assert ledger_events[0]["order_no"] is None
    assert ledger_events[0]["trade_no"] == "12"
    assert ledger_events[0]["source"] == "LS_SETTLED_ACCOUNT_LEDGER"
    assert ledger_events[0]["origin"] == "BROKER_ACCOUNT_UNATTRIBUTED"

    # 확정 원장에 없는 접수는 실시간 피드에서 보충하되, 체결은 원장과 중복하지 않는다
    duplicate_accepted = {**accepted, "seq": 99}
    merged_orders = merge_order_events(
        ledger_events,
        [accepted, duplicate_accepted, filled],
        50,
    )
    assert any(event["kind"] == "ACCEPTED" for event in merged_orders)
    assert sum(event["kind"] == "ACCEPTED" for event in merged_orders) == 1
    assert sum(event["kind"] == "FILLED" for event in merged_orders) == len(ledger_events)
    assert len({event["seq"] for event in merged_orders}) == len(merged_orders)

    # 합계 필드가 오면 그것을 쓰고 개별 세금과 이중 계상하지 않는가
    one = normalize_ledger({"CDPCQ04700OutBlock3": [
        {"TrdDt": "20260818", "TaxSumAmt": 5000, "Trtax": 1300, "Ictax": 3000, "Ihtax": 700},
    ]})
    assert one["totals"]["tax"] == "5000", one["totals"]["tax"]

    # 거래가 없으면 0원이라고 단정하지 않고 브로커가 준 사유를 그대로 전달한다
    empty = normalize_ledger({"CDPCQ04700OutBlock3": [], "rsp_msg": "조회할 내역이 없습니다."})
    assert empty["rows"] == [] and empty["cash_balance"] is None
    assert empty["notice"] == "조회할 내역이 없습니다."

    assert _date("20260818") == "2026-08-18" and _date("") is None and _date("abc") == "abc"

    # 당일 매매일지 - 2026-08-18 실제 응답 모양 그대로다
    today_trades = normalize_today_trades({
        "t0150OutBlock1": [
            {"medosu": "매도", "expcode": "000660", "qty": 1, "price": 1670000,
             "amt": 1670000, "fee": 0, "tax": 0, "argtax": 0, "adjamt": 1670000,
             "middiv": "투혼(HTS)"},
            {"medosu": "종목소계", "expcode": "", "qty": 1, "price": 1670000,
             "amt": 1670000, "fee": 250, "tax": 835, "argtax": 2505, "adjamt": 1666410},
            {"medosu": "매수", "expcode": "000660", "qty": 1, "price": 1655000,
             "amt": 1655000, "fee": 0, "tax": 0, "argtax": 0, "adjamt": 1655000},
            {"medosu": "종목소계", "expcode": "", "qty": 1, "price": 1655000,
             "amt": 1655000, "fee": 248, "tax": 0, "argtax": 0, "adjamt": 1655248},
        ],
    }, "2026-08-18")["rows"]
    assert len(today_trades) == 2, today_trades
    sell, buy = today_trades
    # 비용은 매매행이 아니라 종목소계에 실려 있다. 매매행만 읽으면 전부 0이 된다.
    assert sell["category"] == "매도" and sell["symbol"] == "000660"
    assert sell["execution_channel"] == "투혼(HTS)"
    assert sell["commission"] == "250" and sell["tax"] == "3340"   # 거래세 835 + 농특세 2505
    assert sell["settled_amount"] == "1666410"
    # 매수는 예수금이 줄어든다. adjamt는 양수로 오므로 부호를 뒤집어야 한다.
    assert buy["category"] == "매수" and buy["settled_amount"] == "-1655248"
    assert buy["commission"] == "248" and buy["tax"] == "0"
    # 결제 전이라는 사실을 줄마다 들고 다닌다
    assert all(row["settlement"] == "UNSETTLED" for row in today_trades)
    # 매매일지는 실현손익을 주지 않는다. 0으로 채워 '손익 0'이라고 말하지 않는다.
    assert sell["realized_pnl"] is None
    # LS 필드명이 화면 계약으로 새지 않는가
    assert not any(key in ("medosu", "expcode", "adjamt", "argtax") for key in sell)

    # 짝 없는 소계는 귀속시킬 곳이 없어 버린다
    assert normalize_today_trades({"t0150OutBlock1": [
        {"medosu": "종목소계", "fee": 100, "adjamt": 1}]}, "2026-08-18")["rows"] == []

    # 체결내역 색인 - 시각과 종목명이 매매일지 줄에 붙는가
    execution_payload = {"CSPAQ13700OutBlock3": [
        {"OrdNo": 101, "OrgOrdNo": 0, "OrdTime": "09:05:00", "OrdQty": 1,
         "OrdPrc": 1650000, "IsuNo": "A000660", "IsuNm": "SK하이닉스",
         "BnsTpCode": "2", "LastExecTime": "09:05:11"},
        # 같은 종목·같은 방향을 여러 번 체결하면 소계는 한 줄이다. 시각은 마지막 것.
        {"OrdNo": 102, "OrdTime": "14:30:00", "OrdQty": 2, "OrdPrc": 1655000,
         "IsuNo": "A000660", "IsuNm": "SK하이닉스", "BnsTpCode": "2",
         "LastExecTime": "14:31:02"},
        {"OrdNo": 103, "OrdTime": "09:59:00", "OrdQty": 1, "OrdPrc": 1670000,
         "IsuNo": "A000660", "IsuNm": "SK하이닉스", "BnsTpCode": "1",
         "LastExecTime": "10:00:00", "AllExecQty": 1, "ExecPrc": 1681500},
        # 같은 주문번호가 여러 체결 행에 반복돼도 접수는 한 건이다.
        {"OrdNo": 102, "OrdTime": "14:30:00", "OrdQty": 2, "OrdPrc": 1655000,
         "IsuNo": "A000660", "IsuNm": "SK하이닉스", "BnsTpCode": "2",
         "LastExecTime": "14:30:30"},
    ]}
    execs = normalize_executions(execution_payload)
    assert execs[("000660", "매수")]["time"] == "14:31:02", execs
    assert execs[("000660", "매수")]["count"] == 3
    # 매도는 별개 묶음이다 - 종목만으로 묶으면 매수 시각이 매도에 붙는다
    assert execs[("000660", "매도")]["time"] == "10:00:00"

    accepted_history = normalize_accepted_orders(execution_payload, "2026-08-18")
    assert len(accepted_history) == 3
    assert accepted_history[0]["kind"] == "ACCEPTED"
    assert accepted_history[0]["order_no"] == "102"
    assert accepted_history[0]["side"] == "매수"
    assert accepted_history[0]["quantity"] == "2"
    assert accepted_history[0]["price"] == "1655000"
    assert accepted_history[0]["broker_order_id"] == "102"
    assert accepted_history[0]["source"] == "LS_ORDER_HISTORY"
    filled_history = next(
        event for event in accepted_history if event["order_no"] == "103"
    )
    assert filled_history["kind"] == "FILLED"
    assert filled_history["requested_price"] == "1670000"
    assert filled_history["average_fill_price"] == "1681500"
    assert filled_history["price"] == "1681500"
    assert filled_history["event_time"] == "10:00:00"
    history_merged = merge_order_events(ledger_events, accepted_history, 50)
    projected_broker_fill = next(
        event for event in history_merged if event.get("order_no") == "103"
    )
    assert projected_broker_fill["kind"] == "FILLED"
    assert projected_broker_fill["requested_price"] == "1670000"
    assert projected_broker_fill["average_fill_price"] == "1681500"

    attach_executions(today_trades, execs)
    assert sell["trade_time"] == "10:00:00" and sell["symbol_name"] == "SK하이닉스"
    assert buy["trade_time"] == "14:31:02"
    assert sell["broker_order_id"] == "103"
    assert set(buy["broker_order_ids"]) == {"101", "102"}
    assert buy["broker_order_id"] is None
    today_events = ledger_to_order_events({"rows": today_trades}, 50)
    projected_sell = next(event for event in today_events if event["side"] == "매도")
    assert projected_sell["order_no"] == "103"
    assert projected_sell["trade_no"] is None
    assert projected_sell["origin"] == "EXTERNAL_HTS"
    assert projected_sell["correlation_status"] == "UNATTRIBUTED"
    # 색인에 없는 줄은 지어내지 않고 비워 둔다
    orphan = [{"symbol": "999999", "category": "매수", "trade_time": None, "symbol_name": None}]
    assert attach_executions(orphan, execs)[0]["trade_time"] is None

    # 확정분 + 미결제분을 합친 뒤에도 합계가 표와 같은 규칙으로 나오는가
    merged = summarize_ledger_rows(today_trades + ledger["rows"])
    assert merged["count"] == 4 and merged["unsettled_count"] == 2
    assert merged["commission"] == "1204"                    # 706 + 250 + 248
    assert merged["tax"] == "4640"                           # 1300 + 3340
    assert merged["cost"] == "5844"
    # 미결제분 순정산 = 1666410 - 1655248 = 11162. 확정분 합계를 더한 값이어야 한다.
    assert _dec(merged["settled"]) == Decimal(11162) + _dec(ledger["totals"]["settled"])
    # 미결제 줄의 None 실현손익이 합계를 깨뜨리지 않는가
    assert merged["realized_pnl"] == ledger["totals"]["realized_pnl"]

    # 손익 구성 - 총손익에서 비용을 다시 빼지 않는다(제비용포함 조회이므로 이중 차감)
    pnl = build_pnl(
        {"realized_pnl": "10546", "valuation_pnl": "-99555"},
        {"commission": "1668", "tax": "3876"},
    )
    assert pnl["realized"] == "10546" and pnl["valuation"] == "-99555"
    assert pnl["total"] == "-89009", pnl["total"]          # 실현 + 평가, 비용 재차감 없음
    assert pnl["cost"] == "5544" and pnl["cost_included_in_realized"] is True
    # 잔고를 아직 못 받았어도 0으로 단정하지 않고 형태는 유지한다
    empty_pnl = build_pnl(None, {})
    assert empty_pnl["total"] == "0" and empty_pnl["cost"] == "0"

    # 스위치가 꺼져 있으면 붙지 않는다(비용·권한 경계)
    saved_events = ENABLE_LS_ORDER_EVENTS
    saved_account = ENABLE_LS_ACCOUNT_DATA
    try:
        globals()["ENABLE_LS_ORDER_EVENTS"] = False
        globals()["ENABLE_LS_ACCOUNT_DATA"] = False
        for guarded in (portfolio_live(), portfolio_ledger()):
            try:
                asyncio.run(guarded)
            except HTTPException as exc:
                assert exc.status_code == 503, exc
            else:
                raise AssertionError("스위치가 꺼졌는데 조회했다")
    finally:
        globals()["ENABLE_LS_ORDER_EVENTS"] = saved_events
        globals()["ENABLE_LS_ACCOUNT_DATA"] = saved_account

    # 토큰 만료를 payload의 exp에서 읽는가. `now + expires_in`으로 계산하면 LS가
    # 같은 토큰을 재사용해 줄 때 경과한 만큼 만료를 넘겨 잡는다(2026-08-20 실측).
    def _jwt(exp: object) -> str:
        payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
        return "h." + payload + ".s"

    assert _token_expiry(_jwt(1787263204)) == 1787263204.0
    # 못 읽으면 예외가 아니라 4분짜리 짧은 캐시로 떨어진다
    for broken in (_jwt("없음"), "not.a.jwt", "", "a.b"):
        assert abs(_token_expiry(broken) - (time.time() + 240.0)) < 5, broken

    # 캐시가 그 exp를 1분 앞당겨 쓰되, 이미 지난 exp로 hot loop에 빠지지 않는가
    _cfg = type("C", (), {"environment": "PAPER", "app_key": "k"})()
    saved_issue = _issue_token
    for offset, floor, ceil in ((70503.0, 70440, 70445), (10.0, 59, 61), (-9999.0, 59, 61)):
        async def _fake(config, _at=time.time() + offset):  # noqa: ANN001 - 자체 점검용 대역
            return "tok", _at
        globals()["_issue_token"] = _fake
        _token_cache.clear()
        try:
            assert asyncio.run(_access_token(_cfg)) == "tok"
            remaining = _token_cache[("PAPER", "k")][1] - time.time()
            assert floor <= remaining <= ceil, (offset, remaining)
        finally:
            globals()["_issue_token"] = saved_issue
            _token_cache.clear()

    print("ls_account_stream 자체 점검 통과")
