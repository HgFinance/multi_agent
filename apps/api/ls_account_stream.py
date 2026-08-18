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
import json
import os
import sys
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

ROOT = Path(__file__).resolve().parents[2]
# 자격 해석(PAPER/LIVE 접미사 규칙)은 리스크본부가 이미 갖고 있다(동규 소유).
# 같은 규칙을 두 벌 두면 한쪽만 고쳐졌을 때 Live 자격으로 Paper에 붙는다.
_LS_PATH = ROOT / "departments" / "03-risk" / "integrations"
if str(_LS_PATH) not in sys.path:
    sys.path.insert(0, str(_LS_PATH))

router = APIRouter(tags=["portfolio-live"])

ENABLE_LS_ORDER_EVENTS = os.getenv("ENABLE_LS_ORDER_EVENTS", "false").strip().lower() in {
    "1", "true", "yes", "on",
}
MAX_EVENTS = int(os.getenv("LS_ORDER_EVENTS_MAX", "200"))

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


def _side(code: str | None) -> str | None:
    """매매구분. 문서 다른 절의 범례가 `1'매도'2'매수`다.

    ponytail: SC0/SC1 자체에는 범례가 없다. 모르는 코드는 번역하지 않고 그대로
    내보낸다 — 매수/매도를 잘못 뒤집어 보여 주는 것보다 코드가 보이는 편이 낫다.
    """
    return {"1": "매도", "2": "매수"}.get(code or "", code or None)


def _symbol(value: str | None) -> str | None:
    """실시간은 `A005930`, 잔고 조회는 `005930`으로 준다. 6자리 숫자로 맞춘다.

    두 경로의 종목코드가 어긋나면 로컬 포지션과 브로커 잔고를 대조할 수 없다.
    """
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else (digits or value.strip() or None)


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


def normalize_order_event(tr_cd: str, body: dict[str, Any], seq: int) -> dict[str, Any]:
    """브로커 푸시 1건 → 화면 계약. LS 필드명은 여기서 끝난다."""
    kind = _TR_TO_KIND[tr_cd]
    return {
        "seq": seq,
        "kind": kind,
        "label": KIND_LABELS[kind],
        "received_at": datetime.now(timezone.utc).isoformat(),
        # 시각: 체결은 체결시각, 나머지는 주문시각
        "event_time": _pick(body, "exectime", "ordtm"),
        "order_no": _number(_pick(body, "ordno")),
        "orig_order_no": _number(_pick(body, "orgordno")),
        "symbol": _symbol(_pick(body, "shtcode", "shtnIsuno", "expcode", "Isuno")),
        "symbol_name": _pick(body, "hname", "Isunm"),
        "side": _side(_pick(body, "bnstp")),
        # 체결이면 체결수량·체결가, 아니면 주문수량·주문가
        "quantity": _number(
            _pick(body, "execqty", "mdfycnfqty", "canccnfqty", "rjtqty", "ordqty")
        ),
        "price": _number(_pick(body, "execprc", "mdfycnfprc", "ordprice", "ordprc")),
        "unfilled_quantity": _number(_pick(body, "unercqty", "orgordunercqty")),
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
        self.holdings: dict[str, Any] | None = None
        self.holdings_as_of: str | None = None
        self.holdings_error: str | None = None
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
            self.account = _account(body)
        if event["kind"] == "FILLED":
            apply_fill(self.local_positions, event)
        return event["kind"]

    def sync_holdings(self, holdings: dict[str, Any]) -> None:
        """t0424 확인 결과 반영. 브로커가 정본이지만 차이는 지우지 않는다."""
        self.drift = compare_positions(self.local_positions, holdings)
        self.holdings = holdings
        self.holdings_as_of = datetime.now(timezone.utc).isoformat()
        self.local_positions = {
            row["symbol"]: Decimal(row.get("quantity") or "0")
            for row in holdings.get("rows", [])
            if row.get("symbol")
        }

    def start(self) -> None:
        if self._task is None or self._task.done():
            # ponytail: 첫 조회에서 켜고 끄지 않는다. 연결은 프로세스당 1개라
            # 새는 양이 없다. 유휴 해제가 필요해지면 마지막 폴링 시각으로 끊는다.
            self._task = asyncio.get_running_loop().create_task(_run_feed())


FEED = _Feed()


def _config() -> tuple[Any, str]:
    """(REST 자격, WS URL). 자격이 없으면 여기서 fail-closed."""
    import ls_openapi  # type: ignore[import-not-found]

    config = ls_openapi.LSOpenAPIConfig.from_env()
    suffix = "_PAPER" if config.environment == "PAPER" else ""
    ws_url = (os.getenv("LS_WS_BASE_URL" + suffix, "") or os.getenv("LS_WS_BASE_URL", "")).strip()
    if not ws_url:
        raise RuntimeError("LS_WS_BASE_URL" + suffix + "가 설정되지 않았습니다.")
    # 수집 문서의 "접속 경로 `/websocket/stock`"은 실제 게이트웨이와 다르다.
    # `/websocket/stock`은 handshake에 응답조차 하지 않고, `/websocket`이 붙는다
    # (2026-08-18 두 포트 모두 실측). 바꾸기 전에 handshake부터 확인할 것.
    return config, ws_url.rstrip("/") + "/websocket"


async def _issue_token(config: Any) -> str:
    """OAuth. 동기 클라이언트를 asyncio에 끌어들이지 않으려고 여기서만 비동기다."""
    import httpx

    async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
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
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("LS OAuth 응답에 access_token이 없습니다")
    return token


async def _post_tr(config: Any, token: str, tr_cd: str, payload: dict[str, Any]) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
        response = await client.post(
            config.base_url + "/stock/accno",
            json=payload,
            headers={
                "content-type": "application/json; charset=UTF-8",
                "authorization": "Bearer " + token,
                "tr_cd": tr_cd,
                "tr_cont": "N",
                "tr_cont_key": "",
            },
        )
    response.raise_for_status()
    # JSON number는 double이라 Decimal이 깨진다. 금액·수량은 Decimal로 받는다.
    body = json.loads(response.text, parse_float=Decimal)
    return body if isinstance(body, dict) else {}


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


async def _resync(config: Any, token: str) -> None:
    """브로커 잔고와 맞춘다. 실패해도 구독은 계속 돈다."""
    try:
        FEED.sync_holdings(await _fetch_holdings(config, token))
    except Exception as exc:  # noqa: BLE001 - 조회 실패를 '잔고 없음'으로 위장하지 않는다
        FEED.holdings_error = (type(exc).__name__ + ": " + str(exc))[:200]
    else:
        FEED.holdings_error = None


async def _run_feed() -> None:
    import websockets

    backoff = 1.0
    while True:
        try:
            config, ws_url = _config()
            token = await _issue_token(config)

            if not FEED.account:
                try:
                    FEED.account = await _fetch_account_no(config, token)
                except Exception as exc:  # noqa: BLE001 - 계좌 조회 실패가 구독을 막지 않는다
                    FEED.account_error = (type(exc).__name__ + ": " + str(exc))[:200]
                else:
                    FEED.account_error = None

            # 계좌등록 (tr_type = 1)
            async with websockets.connect(ws_url, ping_interval=30) as socket:
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
                # 체결이 없어도 잔고는 보여야 한다. 붙자마자 한 번 맞춘다.
                await _resync(config, token)

                async for raw in socket:
                    try:
                        message = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if FEED.ingest(message) == "FILLED":
                        # ponytail: 체결 처리 중에는 수신이 잠깐 멈춘다. 우리
                        # 주문량에서는 문제가 안 된다. 체결이 몰려 밀리면 별도
                        # 태스크로 빼고 연속 체결을 하나로 합친다.
                        await _resync(config, token)
        except asyncio.CancelledError:
            FEED.status = "STOPPED"
            raise
        except Exception as exc:  # noqa: BLE001 - 끊김을 "사건 없음"으로 위장하지 않는다
            FEED.status = "DISCONNECTED"
            FEED.error = (type(exc).__name__ + ": " + str(exc))[:300]
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


# --------------------------------------------------------------------------
# 조회
# --------------------------------------------------------------------------

def _registered_account() -> str | None:
    """정본은 브로커가 말해 준 값이다. `.env`는 계좌가 여럿일 때의 덮어쓰기용."""
    environment = os.getenv("LS_ENV", "PAPER").strip().upper()
    suffix = "_PAPER" if environment == "PAPER" else ""
    return (os.getenv("LS_ACCOUNT_NO" + suffix, "") or "").strip() or FEED.account


@router.get("/ui/portfolio/live", operation_id="portfolio_live")
async def portfolio_live(limit: int = 50) -> dict[str, Any]:
    """주문 상태와 브로커 잔고. 화면이 폴링으로 읽는다."""
    if not ENABLE_LS_ORDER_EVENTS:
        raise HTTPException(
            503,
            "브로커 실시간 연동은 기본 비활성화 상태입니다 "
            "(ENABLE_LS_ORDER_EVENTS=true 로 엽니다).",
        )
    FEED.start()
    account_no = _registered_account()
    environment = os.getenv("LS_ENV", "PAPER").strip().upper()
    counts = {kind: 0 for kind in KINDS}
    for event in FEED.events:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    return {
        "schema_version": "trading.portfolio-live.v1",
        "environment": environment,
        "environment_label": "모의투자" if environment == "PAPER" else "실전투자",
        "account": {
            "registered": account_no is not None,
            "masked": mask_account(account_no),
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
            "recent": list(FEED.events)[: max(1, min(limit, MAX_EVENTS))],
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
        "server_time": datetime.now(timezone.utc).isoformat(),
        # 브로커 값이 공식 NAV로 둔갑하지 않게 하는 계약 두 줄.
        "authoritative": False,
        "official_nav_source": "/accounting/v1/ledgers/{ledger_id}",
    }


__all__ = ["router", "ENABLE_LS_ORDER_EVENTS", "KIND_LABELS", "normalize_order_event",
           "normalize_holdings", "apply_fill", "compare_positions", "mask_account"]


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

    # 로컬 상태와 브로커 잔고 대조 - 차이를 조용히 지우지 않는다
    assert compare_positions({"005930": Decimal(10)}, holdings) == []
    assert compare_positions({"005930": Decimal(7)}, holdings) == [
        {"symbol": "005930", "local": "7", "broker": "10"}
    ]
    assert compare_positions({"000660": Decimal(5)}, holdings) == [
        {"symbol": "000660", "local": "5", "broker": "0"},
        {"symbol": "005930", "local": "0", "broker": "10"},
    ]

    # 동기화 후에는 로컬이 브로커를 따라가고 차이 기록만 남는다
    feed = _Feed()
    feed.local_positions = {"005930": Decimal(7)}
    feed.sync_holdings(holdings)
    assert feed.local_positions == {"005930": Decimal(10)}
    assert feed.drift == [{"symbol": "005930", "local": "7", "broker": "10"}]

    # 계좌등록 응답을 주문 사건으로 세지 않는가
    feed = _Feed()
    assert feed.ingest({"header": {"tr_cd": "SC0"}, "body": {"rsp_cd": "00000", "rsp_msg": "정상"}}) is None
    assert feed.ingest({"header": {"tr_cd": "S3_"}, "body": {"price": "1"}}) is None
    assert feed.ingest({"header": {"tr_cd": "SC0"}, "body": {"ordno": "1", "accno1": "98765432109"}}) == "ACCEPTED"
    assert len(feed.events) == 1 and feed.account == "98765432109"

    # 스위치가 꺼져 있으면 붙지 않는다(비용·권한 경계)
    saved = ENABLE_LS_ORDER_EVENTS
    try:
        globals()["ENABLE_LS_ORDER_EVENTS"] = False
        try:
            asyncio.run(portfolio_live())
        except HTTPException as exc:
            assert exc.status_code == 503, exc
        else:
            raise AssertionError("스위치가 꺼졌는데 구독했다")
    finally:
        globals()["ENABLE_LS_ORDER_EVENTS"] = saved

    print("ls_account_stream 자체 점검 통과")
