#!/usr/bin/env python3
"""D3 Mark 공급원 - market-api 응답을 `MarkPrice`로 옮긴다.

소유: 도현 (회계·포트폴리오본부)
근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 9(D3 Valuation/PnL/NAV)
      docs/02-engineering/TECH_STACK_DECISIONS.md
        (트레이딩·회계는 TimescaleDB 자격증명을 갖지 않는다 - 시계열은 market-api 경유)
      departments/05-accounting-portfolio/portfolio/portfolio.py
        ("가격을 수집하지 않는다. market-api가 준 Mark를 받아 쓴다")

**가격을 만들지 않는다.** 응답에 있는 값만 옮기고, 없으면 없는 채로 둔다 - 못 받은
종목은 결과 dict에 **아예 없고**, 그 사실로 `value_portfolio`가 NAV를 거부한다.
빠진 자리를 직전 가격·호가 중간값·체결가로 메우지 않는다. 메우는 순간 그 NAV는
틀린 수치가 되고 F11 주문 사이징으로 흘러간다(마스터플랜 25장).

**두 가지 모드가 있고 뜻이 다르다:**
  - 장중(기본, `interval=None`)  `/snapshot/{symbol}` 마지막 **체결가**.
    is_final은 언제나 False다 - 틱은 확정 종가가 아니다.
  - 종가(`interval="1D"`)         `/bars/{symbol}` 마지막 봉의 **종가**.
    is_final은 응답의 `is_final`을 그대로 옮긴다. 우리가 True로 올리지 않는다.

`bucket_time`은 봉의 시작 시각이라 장 마감 후에도 valuation 시각과 몇 시간 벌어진다.
종가 모드를 쓸 때는 호출자가 `max_staleness`를 그만큼 넓혀야 한다(기본 5분으로는
전부 stale로 걸린다). 신선도 판정은 여기가 아니라 `MarkPrice.is_fresh_at`이 한다.

⚠ market-api의 Tool Gateway는 현재 관찰 모드다(`TOOL_GATEWAY_ENFORCE_MARKET`).
   강제로 올라가도 이 호출은 `X-Agent-Persona: back-office-runner`로 판정되며,
   리서치본부 `hermes/config.yaml`의 `tool_allowlist`에
   `market.snapshot.read` / `market.bars.read`를 연결해 두었다. 따라서 enforcement
   전환은 이 호출 경로를 막지 않는다.

자체 점검: python departments/05-accounting-portfolio/portfolio/mark_provider.py
           (네트워크 없이 - HTTP 호출은 `get`으로 주입한다)
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, NamedTuple
from uuid import UUID

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from portfolio import MarkPrice, ValuationError  # noqa: E402

# 리서치본부 조회면. 이름·기본값은 저장소 관행 그대로다(01-research/scripts.py 등).
MARKET_API = os.environ.get("MARKET_API_URL", "http://127.0.0.1:8036").rstrip("/")
TIMEOUT_SECONDS = float(os.environ.get("MARKET_API_TIMEOUT_SECONDS", "5"))
# Tool Gateway가 호출자를 식별하는 헤더값. 결정론 직원 id를 쓴다 - 이 조회의 주인은
# `back-office-runner`이고(config.yaml `absorbed`), LLM 직원이 아니다.
PERSONA = os.environ.get("ACCOUNTING_MARKET_PERSONA", "back-office-runner")
# 같은 구간 안에서는 종목당 한 번만 조회한다. 0 이하면 캐시를 쓰지 않는다.
CACHE_SECONDS = float(os.environ.get("MARK_CACHE_SECONDS", "15"))

SOURCE = "market-api"


class Quote(NamedTuple):
    """market-api 응답 한 건. 값이 없으면 `why`에 이유가 남는다."""

    price: Decimal | None
    as_of: datetime | None
    is_final: bool = False
    why: str = ""


@dataclass(frozen=True)
class Marks:
    """조회 결과. **못 받은 종목을 조용히 빠뜨리지 않는다.**

    `prices`에 없는 종목은 `missing`에 이유와 함께 남는다 - "가격이 없다"와 "조회가
    실패했다"와 "그런 심볼을 모른다"는 대응이 서로 다르고, 로그에 이유가 없으면
    NAV가 왜 안 나오는지 아무도 모른다.
    """

    prices: dict[UUID, MarkPrice]
    missing: tuple[tuple[UUID, str], ...] = ()


# ponytail: 프로세스 로컬 dict 하나. 소비자가 1초 주기로 도는데 종목마다 매번 HTTP를
#           때리면 남의 부서 API에 초당 N건이 간다. 여러 프로세스로 늘어나면 Redis
#           캐시(P0 확정)로 옮긴다. 상한을 넘으면 통째로 비운다 - LRU가 필요할 만큼
#           종목이 많아지면 그때가 Redis로 옮길 시점이다.
_CACHE: dict[tuple[str, str | None, int], Quote] = {}
_CACHE_MAX = 512


def _bucket(as_of: datetime) -> int:
    """`as_of`를 CACHE_SECONDS로 내림한 구간 번호.

    **PIT를 깨지 않는다.** 한 구간의 첫 조회 결과를 그 구간 안에서만 재사용하므로
    캐시는 Mark를 *더 오래된* 쪽으로만 만들 수 있고 미래 데이터를 앞당기지 못한다.
    신선도는 캐시가 아니라 `MarkPrice.as_of`로 판정하므로 낡은 값은 그대로 거부된다.
    Replay도 안전하다 - 같은 `as_of`면 항상 같은 구간이다.
    """
    return int(as_of.timestamp() // CACHE_SECONDS)


def _decimal(value: Any) -> Decimal | None:
    """JSON 숫자를 Decimal로. **float를 거치지 않는다**(가격에 float 금지).

    호출부가 `json.loads(..., parse_float=Decimal)`로 읽으므로 여기 오는 값은 이미
    Decimal이거나 int다. 그래도 str을 한 번 거쳐 이진 부동소수점 경로를 막는다.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _moment(value: Any) -> datetime | None:
    """ISO8601 -> tz 있는 datetime. **naive는 거부한다.**

    market-api도 같은 규칙이다(`/bars`의 `to`). KST/UTC를 섞으면 9시간짜리 PIT
    오차가 조용히 생기고, 그 오차는 NAV에 그대로 남는다.
    """
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return moment if moment.tzinfo is not None else None


def _http_get(url: str, params: dict[str, Any]) -> tuple[int, Any]:
    """market-api 한 번 호출. 자체 점검은 이 함수를 대신 주입한다."""
    import httpx

    response = httpx.get(url, params=params, timeout=TIMEOUT_SECONDS,
                         headers={"X-Agent-Persona": PERSONA})
    # parse_float=Decimal - FastAPI가 numeric을 JSON number로 내보내므로 기본
    # 파서로 읽으면 가격이 float가 된다. 문자열 리터럴 그대로 Decimal로 받는다.
    body = json.loads(response.text, parse_float=Decimal) if response.text else None
    return response.status_code, body


Getter = Callable[[str, dict[str, Any]], tuple[int, Any]]


def _fetch_one(get: Getter, base_url: str, symbol: str, as_of: datetime,
               interval: str | None) -> Quote:
    if interval:
        status, body = get(f"{base_url}/bars/{symbol}",
                           {"interval": interval, "limit": 1, "to": as_of.isoformat()})
        if status != 200:
            return Quote(None, None, why=f"/bars HTTP {status}")
        rows = body if isinstance(body, list) else []
        if not rows:
            return Quote(None, None, why=f"{interval} 봉 없음(<= {as_of.isoformat()})")
        row = rows[0] if isinstance(rows[0], Mapping) else {}
        # is_final은 응답 그대로다. 없으면 False - 말 안 하면 미확정이다.
        return Quote(_decimal(row.get("close")), _moment(row.get("bucket_time")),
                     bool(row.get("is_final")))

    status, body = get(f"{base_url}/snapshot/{symbol}", {"as_of": as_of.isoformat()})
    if status != 200:
        return Quote(None, None, why=f"/snapshot HTTP {status}")
    trade = (body or {}).get("last_trade") if isinstance(body, Mapping) else None
    if not isinstance(trade, Mapping):
        return Quote(None, None, why=f"체결 기록 없음(<= {as_of.isoformat()})")
    # **호가 중간값으로 대체하지 않는다.** 체결가는 실제로 거래된 값이고 mid는
    # 아무도 그 가격에 사고팔지 않은 값이다. 체결이 없으면 없는 것이다.
    return Quote(_decimal(trade.get("price")), _moment(trade.get("event_time")))


def fetch_marks(symbols: Mapping[UUID, str], as_of: datetime, *,
                interval: str | None = None, get: Getter = _http_get,
                base_url: str | None = None) -> Marks:
    """`{instrument_id: symbol}` -> `{instrument_id: MarkPrice}`.

    symbol 매핑은 호출자가 `LedgerRepository.symbols_for()`로 만든다 - 그 표가
    Point-in-Time이라 상장폐지 코드 재배정에도 그 시점의 주인이 나온다.

    한 종목의 실패가 다른 종목을 막지 않는다. 실패는 예외가 아니라 `missing`이다.
    """
    base = (base_url or MARKET_API).rstrip("/")
    prices: dict[UUID, MarkPrice] = {}
    missing: list[tuple[UUID, str]] = []
    cached = CACHE_SECONDS > 0
    bucket = _bucket(as_of) if cached else 0

    for instrument_id, symbol in symbols.items():
        key = (symbol, interval, bucket)
        quote = _CACHE.get(key) if cached else None
        if quote is None:
            try:
                quote = _fetch_one(get, base, symbol, as_of, interval)
            except Exception as exc:  # noqa: BLE001 - 조회 실패는 NAV 보류이지 중단이 아니다
                missing.append((instrument_id, f"{symbol}: {type(exc).__name__}: {exc}"))
                continue
            if cached:
                if len(_CACHE) >= _CACHE_MAX:
                    _CACHE.clear()
                _CACHE[key] = quote

        if quote.price is None or quote.as_of is None:
            missing.append((instrument_id,
                            f"{symbol}: {quote.why or '가격 또는 시각이 비었다'}"))
            continue
        try:
            prices[instrument_id] = MarkPrice(
                instrument_id, quote.price, quote.as_of,
                source=SOURCE, is_final=quote.is_final)
        except ValuationError as exc:
            # 0 이하 가격 등. 거부는 `MarkPrice`가 하고 우리는 이유만 옮긴다.
            missing.append((instrument_id, f"{symbol}: {exc}"))

    return Marks(prices, tuple(missing))


if __name__ == "__main__":
    from datetime import timedelta, timezone
    from uuid import uuid4

    D = Decimal
    NOW = datetime(2026, 8, 10, 6, 0, tzinfo=timezone.utc)
    AAA, BBB = uuid4(), uuid4()

    calls: list[tuple[str, dict]] = []

    def fake(routes: dict[str, tuple[int, Any]]) -> Getter:
        def _get(url: str, params: dict) -> tuple[int, Any]:
            calls.append((url, params))
            for suffix, response in routes.items():
                if url.endswith(suffix):
                    return response
            return 404, {"detail": "no route"}
        return _get

    def reset() -> None:
        _CACHE.clear()
        calls.clear()

    tick = {"symbol": "005930", "last_trade": {
        "event_time": "2026-08-10T05:59:30+00:00", "price": D("70000"), "quantity": 10},
        "last_quote": {"mid_price": D("70050")}}

    # 1. 장중 - 마지막 체결가가 Mark가 된다. 틱은 확정 종가가 아니므로 is_final False
    reset()
    marks = fetch_marks({AAA: "005930"}, NOW, get=fake({"/snapshot/005930": (200, tick)}))
    assert marks.missing == (), marks.missing
    mark = marks.prices[AAA]
    assert mark.price == D("70000") and mark.source == "market-api"
    assert mark.is_final is False, "틱을 확정 종가로 올렸다"
    assert mark.as_of == datetime(2026, 8, 10, 5, 59, 30, tzinfo=timezone.utc)
    assert mark.is_fresh_at(NOW, timedelta(minutes=5)), "30초 전 체결이 stale로 걸렸다"
    # PIT - 조회 시각을 그대로 넘긴다. 안 넘기면 market-api가 미래 틱을 준다
    assert calls[0][1]["as_of"] == NOW.isoformat(), calls[0]

    # 2. 종가 - is_final을 응답 그대로 옮긴다. 우리가 True로 올리지 않는다
    def bar(is_final: bool) -> tuple[int, Any]:
        return 200, [{"bucket_time": "2026-08-09T15:00:00+00:00", "close": D("71500"),
                      "is_final": is_final, "source": "ls_chart"}]

    for final in (True, False):
        reset()
        marks = fetch_marks({AAA: "005930"}, NOW, interval="1D",
                            get=fake({"/bars/005930": bar(final)}))
        assert marks.prices[AAA].is_final is final, final
        assert marks.prices[AAA].price == D("71500")
    # 종가 봉은 valuation 시각과 벌어진다 - 기본 5분으로는 stale이고, 그 판정은
    # 여기가 아니라 value_portfolio(max_staleness)가 한다
    assert not marks.prices[AAA].is_fresh_at(NOW, timedelta(minutes=5))
    assert marks.prices[AAA].is_fresh_at(NOW, timedelta(days=1))

    # 3. 못 받은 종목은 prices에 없고 이유가 남는다. 다른 종목을 막지 않는다
    reset()
    marks = fetch_marks({AAA: "005930", BBB: "000660"}, NOW,
                        get=fake({"/snapshot/005930": (200, tick)}))
    assert set(marks.prices) == {AAA}, "실패한 종목이 prices에 들어갔다"
    assert len(marks.missing) == 1 and marks.missing[0][0] == BBB
    assert "404" in marks.missing[0][1], marks.missing

    # 4. 체결 기록이 없는 것과 조회 실패는 다르다. 둘 다 값을 지어내지 않는다
    reset()
    empty = fetch_marks({AAA: "005930"}, NOW,
                        get=fake({"/snapshot/005930": (200, {"last_trade": None})}))
    assert empty.prices == {} and "체결 기록 없음" in empty.missing[0][1]
    reset()
    nobar = fetch_marks({AAA: "005930"}, NOW, interval="1D",
                        get=fake({"/bars/005930": (200, [])}))
    assert nobar.prices == {} and "봉 없음" in nobar.missing[0][1]

    # 5. **호가로 체결가를 대신하지 않는다.** mid가 응답에 있어도 쓰지 않는다
    assert empty.prices == {}, "체결이 없는데 호가 중간값으로 Mark를 만들었다"

    # 6. naive 시각은 거부한다 - KST/UTC를 섞으면 9시간짜리 PIT 오차가 생긴다
    reset()
    naive = fetch_marks({AAA: "005930"}, NOW, get=fake({"/snapshot/005930": (
        200, {"last_trade": {"event_time": "2026-08-10T14:59:30", "price": D("70000")}})}))
    assert naive.prices == {}, "timezone 없는 시각으로 Mark를 만들었다"

    # 7. 0 이하 가격은 MarkPrice가 거부하고, 우리는 죽지 않고 이유만 옮긴다
    reset()
    zero = fetch_marks({AAA: "005930"}, NOW, get=fake({"/snapshot/005930": (
        200, {"last_trade": {"event_time": "2026-08-10T05:59:30+00:00", "price": D("0")}})}))
    assert zero.prices == {} and "0 이하" in zero.missing[0][1], zero.missing

    # 8. 가격이 float를 거치지 않는다. 소수 자리가 이진 부동소수점으로 뭉개지면 안 된다
    reset()
    exact = fetch_marks({AAA: "005930"}, NOW, get=fake({"/snapshot/005930": (200, {
        "last_trade": {"event_time": "2026-08-10T05:59:30+00:00",
                       "price": Decimal("0.1")}})}))
    assert exact.prices[AAA].price == Decimal("0.1"), exact.prices[AAA].price
    assert exact.prices[AAA].price * 3 == Decimal("0.3"), "float로 샜다"

    # 9. 같은 구간이면 종목당 한 번만 조회한다. 구간이 바뀌면 다시 조회한다
    reset()
    getter = fake({"/snapshot/005930": (200, tick)})
    fetch_marks({AAA: "005930"}, NOW, get=getter)
    fetch_marks({AAA: "005930"}, NOW, get=getter)
    assert len(calls) == 1, f"같은 구간에서 {len(calls)}번 조회했다"
    later = NOW + timedelta(seconds=CACHE_SECONDS + 1)
    fetch_marks({AAA: "005930"}, later, get=getter)
    assert len(calls) == 2, "구간이 바뀌었는데 캐시를 재사용했다"
    # 캐시는 Mark를 더 오래되게 만들 뿐이다 - 신선도는 MarkPrice.as_of로 판정한다
    assert calls[1][1]["as_of"] == later.isoformat()

    # 10. 조회할 종목이 없으면 네트워크를 쓰지 않는다
    reset()
    assert fetch_marks({}, NOW, get=fake({})) == Marks({}, ())
    assert calls == [], "조회 대상이 없는데 API를 불렀다"

    # 11. **체결 -> Mark -> NAV -> 일일보고 PnL.** 이 파일이 존재하는 이유다 -
    #     D3가 막혀 있던 동안 PnL은 계산은 되는데 값이 안 나오는 상태였다.
    #     여기서 나오는 수치는 전부 원장과 Mark에서 온다(이 검사가 지어낸 값 없음).
    from dataclasses import field
    from datetime import date as _date

    sys.path.insert(0, str(_HERE.parent / "ledger"))
    sys.path.insert(0, str(_HERE.parent / "reporting"))
    sys.path.insert(0, str(_HERE.parent.parent / "02-trading" / "contracts"))
    from contracts import Side  # noqa: E402
    from daily_report import build_daily_report  # noqa: E402
    from ledger import ZERO, Ledger, Position  # noqa: E402
    from portfolio import value_portfolio  # noqa: E402

    @dataclass(frozen=True)
    class Fill:
        quantity: Decimal
        price: Decimal
        fee: Decimal
        tax: Decimal
        event_time: datetime
        broker_fill_id: str
        fill_id: UUID = field(default_factory=uuid4)

    def priced(at: datetime, won: str) -> dict[UUID, MarkPrice]:
        """market-api가 그 시각에 이 가격을 주면 Mark가 이렇게 나온다."""
        reset()
        body = {"last_trade": {"event_time": at.isoformat(), "price": Decimal(won)}}
        got = fetch_marks({AAA: "005930"}, at, get=fake({"/snapshot/005930": (200, body)}))
        assert got.missing == (), got.missing
        return got.prices

    T0 = datetime(2026, 8, 10, 5, 0, tzinfo=timezone.utc)
    T1 = T0 + timedelta(minutes=1)
    led = Ledger(fund_id=uuid4(), book_id=uuid4())
    led.post_capital(Decimal("100000000"), T0, "SEED")
    led.post_fill(Fill(D("100"), D("70000"), D("1050"), D("0"), T0, "F1"),
                  Side.BUY, AAA, Position(AAA))
    opening = value_portfolio(led, priced(T0, "70000"), T0)

    positions, _ = led.rebuild()
    led.post_fill(Fill(D("40"), D("77000"), D("462"), D("4620"), T1, "F2"),
                  Side.SELL, AAA, positions[AAA])
    closing = value_portfolio(led, priced(T1, "77000"), T1)

    report = build_daily_report(snapshots=[opening, closing], ledger=led,
                                accounting_date=_date(2026, 8, 10))
    assert report.realized_pnl == D("280000"), report.realized_pnl      # (77000-70000)*40
    assert report.unrealized_pnl == D("420000"), report.unrealized_pnl  # 남은 60주
    assert report.fees == D("462") and report.taxes == D("4620")
    assert report.net_pnl == D("694918"), report.net_pnl
    # **항등식.** NAV 변화 = 총손익 - 비용 + 자본 유출입. 0이 아니면 어딘가 어긋난 것이다.
    assert report.unexplained_pnl == ZERO, report.unexplained_pnl
    assert report.is_official is False, "이 경로로 공식 수치가 확정됐다"
    # 틱으로 만든 NAV는 확정 종가 NAV와 같은 얼굴을 하지 않는다
    assert closing.quality_status == "WARN", closing.quality_status

    print(f"ok - Mark Provider 11개 영역 점검 통과 (네트워크 없이)\n"
          f"     체결->Mark->NAV->PnL: NAV {opening.nav} -> {closing.nav} · "
          f"실현 {report.realized_pnl} · 평가 {report.unrealized_pnl} · "
          f"비용 {report.cost_total} · 순손익 {report.net_pnl} · "
          f"미설명 {report.unexplained_pnl}")
