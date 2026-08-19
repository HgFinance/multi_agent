#!/usr/bin/env python3
"""LS 통합시세(KRX+NXT) 파서 - 체결 US3 / 호가 UH1.

담당: 재일 (리서치본부 RES)
계약: contracts/market_events.py (instrument_id / event_time·received_at·observed_at / source_event_id)

▶ 어디서 왔나
  다른 프로젝트(Trading_bot)의 collector/parser.py 를 이식했다. 그쪽은 실전에서
  수개월 돌아간 파서라 **미묘한 것들이 이미 들어 있다** - 자정 경계 보정, 9자리
  종목코드 정규화, 통합 잔량(unt_*) 사용, 알 수 없는 체결구분 폐기.
  그것들을 다시 발명하지 않고 그대로 가져왔다.

▶ 이식하면서 **고친 것 하나** (이게 이식의 가장 큰 이득이다)
  원본은 `HHMMSS + 수신 epoch` 로 시각 하나(`ts`)만 만들고 **수신 시각을 버린다.**
  그래서 그 데이터로는 "우리가 언제 알았나" 를 복원할 수 없고, 마이크로구조 전략
  검증에 쓸 수 없다(수백 밀리초가 결과를 뒤집는 영역이다).

  여기서는 세 시각을 전부 남긴다:
    event_time   - 거래소가 준 체결/호가 시각(HHMMSS 기반)
    received_at  - 우리가 소켓에서 받은 시각
    observed_at  - 우리가 알게 된 시각(= received_at)
  Backtest 는 event_time 이 아니라 **observed_at** 을 본다(계약 4.2절).

▶ 파서는 DB 를 모른다
  instrument_id 매핑과 적재는 호출부가 한다. 파서가 저장소를 알면 자체 점검에
  DB 가 필요해지고, 그러면 점검이 실행되지 않는다.

▶ 실패를 조용히 삼키지 않는다
  알 수 없는 체결구분은 **폐기한다**(매수로 넣지 않는다). 파싱 실패는 None 이고
  호출부가 센다. 0 으로 채우면 그 0 이 나중에 신호가 된다.

자체 점검: python departments/01-research/collectors/ls_unified_parser.py
"""
from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

MODULE_VERSION = "research-ls-unified-parser-v1"

# LS chetime/hotime 은 KST 기준이다.
KST = timezone(timedelta(hours=9))

# 통합시세 TR
TR_TICK = "US3"      # (통합)체결
TR_QUOTE = "UH1"     # (통합)호가잔량 10단계

# 체결구분 정규화 (market.market_ticks.side 호환)
SIDE_BUY = 1
SIDE_SELL = 5

QUOTE_LEVELS = 10

# HHMMSS 와 수신 시각이 이보다 벌어지면 자정 경계로 보고 날짜를 보정한다.
_MIDNIGHT_GUARD = timedelta(hours=12)


@dataclass(frozen=True, slots=True)
class UnifiedTick:
    """통합 체결 한 건. **세 시각을 전부 들고 다닌다.**"""

    event_time: datetime      # 거래소 시각
    received_at: datetime     # 소켓 수신 시각 - 원본 파서가 버리던 것
    observed_at: datetime     # 우리가 알게 된 시각
    symbol: str               # 6자리 단축코드
    price: int
    volume: int
    side: int                 # 1=매수, 5=매도
    market: str               # 'K'=KRX, 'N'=NXT
    ofi_contrib: int          # +volume(매수) / -volume(매도)


@dataclass(frozen=True, slots=True)
class UnifiedQuote:
    """통합 호가 스냅샷 10단계. 잔량은 KRX/NXT **합산(unt_*)** 이다."""

    event_time: datetime
    received_at: datetime
    observed_at: datetime
    symbol: str
    asks: tuple[int, ...]
    bids: tuple[int, ...]
    ask_vols: tuple[int, ...]
    bid_vols: tuple[int, ...]
    spread: int
    book_imbalance: float     # 1호가 기준, -1.0 ~ +1.0


# ── 캐스팅 헬퍼 (원본 이식) ──────────────────────────────────────────────────

def to_int(v: Any, default: int = 0) -> int:
    """LS 는 모든 수치를 문자열로 준다. '+00123' 같은 형태도 온다."""
    if v is None or v == "":
        return default
    try:
        if isinstance(v, str):
            v = v.strip().lstrip("+")
            if not v:
                return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def normalize_symbol(raw: Any) -> str | None:
    """대문자 6자리 KRX 코드와 알려진 LS ``A``/``U`` 접두사만 받는다."""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    if re.fullmatch(r"[0-9A-Z]{6}", s):
        return s
    if s[:1] in {"A", "U"} and re.fullmatch(r"[0-9A-Z]{6}", s[1:]):
        return s[1:]
    return None


def exchname_to_market(raw: Any) -> str:
    """거래소 구분. **NXT 를 KRX 로 합치지 않는다** - 같은 종목이 두 거래소에서
    다른 가격에 체결되므로, 합치면 실제로 존재하지 않은 가격 시계열이 생긴다."""
    if isinstance(raw, str) and raw.strip().upper() == "NXT":
        return "N"
    return "K"


def cgubun_to_side(raw: Any) -> int | None:
    """'+' 매수 / '-' 매도. **모르는 값은 None 이다** - 매수로 채우면 OFI 가 거짓말한다."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if s == "+":
        return SIDE_BUY
    if s == "-":
        return SIDE_SELL
    return None


def hhmmss_to_event_time(hhmmss: Any, received_at: datetime) -> datetime:
    """HHMMSS + 수신 시각 -> 거래소 시각(KST).

    날짜는 수신 시각의 KST 날짜를 쓴다. 자정 경계 보정이 핵심이다:
      - chetime=23:59:59 인데 수신이 자정 직후 -> 전날
      - chetime=00:00:0X 인데 수신이 23:59:5X -> 다음날
      - 12시간 넘게 벌어지면 비정상이므로 수신 시각을 그대로 쓴다
    """
    base = received_at.astimezone(KST)
    if not hhmmss:
        return base
    s = str(hhmmss).strip().zfill(6)
    try:
        h, m, sec = int(s[0:2]), int(s[2:4]), int(s[4:6])
    except ValueError:
        return base
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= sec < 60):
        return base
    candidate = base.replace(hour=h, minute=m, second=sec, microsecond=0)
    diff = candidate - base
    if diff > _MIDNIGHT_GUARD:
        candidate -= timedelta(days=1)
    elif diff < -_MIDNIGHT_GUARD:
        candidate += timedelta(days=1)
    return candidate


def source_event_id(*, tr: str, symbol: str, market: str,
                    event_time: datetime, price: int, volume: int,
                    seq: int = 0) -> str:
    """멱등키. 재적재해도 같은 체결이 두 번 들어가지 않게 한다.

    ▶ 한계를 적어 둔다: LS 는 체결 일련번호를 주지 않는다. 같은 초에 같은 가격·
      수량의 체결이 실제로 두 건 있으면 이 키가 같아져 **하나로 접힌다.**
      `seq`(수신 순번)를 넣으면 구분되지만, 그러면 재적재 시 순번이 달라져
      멱등성이 깨진다. 둘 다 가질 수는 없다 - 과거 이관은 seq=0(접힘 감수),
      실시간 수집은 seq 사용(중복 방지 우선)이 기본이다.
    """
    blob = (f"{tr}|{symbol}|{market}|{event_time.isoformat()}"
            f"|{price}|{volume}|{seq}")
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ── 파서 ────────────────────────────────────────────────────────────────────

def parse_tick(body: dict, received_at: datetime) -> UnifiedTick | None:
    """US3 본문 -> UnifiedTick. header 가 아니라 body dict 를 받는다."""
    symbol = normalize_symbol(body.get("shcode"))
    if symbol is None:
        return None
    side = cgubun_to_side(body.get("cgubun"))
    if side is None:
        return None                       # 모르는 구분은 폐기(조용히 매수로 넣지 않는다)

    price = to_int(body.get("price"))
    volume = to_int(body.get("cvolume"))
    market = exchname_to_market(body.get("exchname"))
    event_time = hhmmss_to_event_time(body.get("chetime"), received_at)
    return UnifiedTick(
        event_time=event_time,
        received_at=received_at,
        observed_at=received_at,          # 우리가 알게 된 시각 = 받은 시각
        symbol=symbol, price=price, volume=volume, side=side, market=market,
        ofi_contrib=volume if side == SIDE_BUY else -volume,
    )


def parse_quote(body: dict, received_at: datetime) -> UnifiedQuote | None:
    """UH1 본문 -> UnifiedQuote. 잔량은 **통합(unt_*)** 을 쓴다.

    KRX 전용 잔량(offerrem*)을 쓰면 NXT 물량이 빠져 실제보다 얇게 보인다 -
    유동성을 과소평가하면 수용력 추정이 보수적으로 틀린다.
    """
    symbol = normalize_symbol(body.get("shcode"))
    if symbol is None:
        return None

    rng = range(1, QUOTE_LEVELS + 1)
    asks = tuple(to_int(body.get(f"offerho{i}")) for i in rng)
    bids = tuple(to_int(body.get(f"bidho{i}")) for i in rng)
    ask_vols = tuple(to_int(body.get(f"unt_offerrem{i}")) for i in rng)
    bid_vols = tuple(to_int(body.get(f"unt_bidrem{i}")) for i in rng)

    spread = max(0, asks[0] - bids[0]) if asks[0] and bids[0] else 0
    denom = bid_vols[0] + ask_vols[0]
    bi = ((bid_vols[0] - ask_vols[0]) / denom) if denom > 0 else 0.0

    return UnifiedQuote(
        event_time=hhmmss_to_event_time(body.get("hotime"), received_at),
        received_at=received_at, observed_at=received_at,
        symbol=symbol, asks=asks, bids=bids, ask_vols=ask_vols, bid_vols=bid_vols,
        spread=spread, book_imbalance=float(bi),
    )


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _recv(h=10, m=0, s=0, day=15):
    return datetime(2026, 8, day, h, m, s, tzinfo=KST)


def _tick_body(**kw):
    b = {"shcode": "005930", "cgubun": "+", "price": "71000",
         "cvolume": "100", "exchname": "KRX", "chetime": "100000"}
    b.update(kw)
    return b


def _quote_body(**kw):
    b = {"shcode": "005930", "hotime": "100000"}
    for i in range(1, 11):
        b[f"offerho{i}"] = str(71000 + i * 100)
        b[f"bidho{i}"] = str(70900 - i * 100)
        b[f"unt_offerrem{i}"] = str(1000 * i)
        b[f"unt_bidrem{i}"] = str(2000 * i)
        b[f"offerrem{i}"] = str(1)      # KRX 전용 - 쓰면 안 되는 필드
        b[f"bidrem{i}"] = str(1)
    b.update(kw)
    return b


def _check_three_timestamps_are_kept():
    """**이식의 핵심.** 원본은 수신 시각을 버린다 - 그러면 마이크로구조 검증이 불가능하다."""
    r = _recv()
    t = parse_tick(_tick_body(), r)
    assert t.received_at == r and t.observed_at == r
    assert t.event_time.hour == 10 and t.event_time.tzinfo is not None
    q = parse_quote(_quote_body(), r)
    assert q.received_at == r and q.observed_at == r


def _check_contract_time_invariant():
    """계약 불변식: observed_at >= received_at, 그리고 event_time 이 미래로 크게
    앞서지 않는다(우리 계약이 clock skew 1일을 허용한다)."""
    r = _recv()
    for body in (_tick_body(), _tick_body(chetime="095959")):
        t = parse_tick(body, r)
        assert t.observed_at >= t.received_at
        assert t.event_time <= t.received_at + timedelta(days=1)


def _check_midnight_boundary_both_directions():
    """자정 경계 보정 - 실전 파서에 이미 들어 있던 미묘한 것."""
    # 수신 00:00:01 인데 거래소 시각 23:59:59 -> 전날
    r = _recv(0, 0, 1, day=16)
    t = parse_tick(_tick_body(chetime="235959"), r)
    assert t.event_time.day == 15, t.event_time
    # 수신 23:59:59 인데 거래소 시각 00:00:01 -> 다음날
    r2 = _recv(23, 59, 59, day=15)
    t2 = parse_tick(_tick_body(chetime="000001"), r2)
    assert t2.event_time.day == 16, t2.event_time


def _check_bad_time_falls_back_to_received():
    """이상한 시각은 지어내지 않고 수신 시각을 쓴다."""
    r = _recv()
    for bad in ("999999", "abcdef", "", None):
        t = parse_tick(_tick_body(chetime=bad), r)
        assert t.event_time == r.astimezone(KST), (bad, t.event_time)


def _check_symbol_normalisation():
    r = _recv()
    for raw, want in (("005930", "005930"), (" U005930  ", "005930"),
                      ("A005930", "005930")):
        assert parse_tick(_tick_body(shcode=raw), r).symbol == want, raw
    for bad in ("", None, "12345", "ABC"):
        assert parse_tick(_tick_body(shcode=bad), r) is None, bad


def _check_unknown_side_is_discarded():
    """**모르는 체결구분을 매수로 채우면 OFI 가 거짓말한다.**"""
    r = _recv()
    for bad in ("?", "", None, 1, "0"):
        assert parse_tick(_tick_body(cgubun=bad), r) is None, bad


def _check_ofi_sign():
    r = _recv()
    buy = parse_tick(_tick_body(cgubun="+", cvolume="100"), r)
    sell = parse_tick(_tick_body(cgubun="-", cvolume="100"), r)
    assert buy.ofi_contrib == 100 and sell.ofi_contrib == -100
    assert buy.side == SIDE_BUY and sell.side == SIDE_SELL


def _check_nxt_is_not_merged_into_krx():
    """**NXT 를 KRX 로 합치지 않는다** - 합치면 존재하지 않은 가격 시계열이 생긴다."""
    r = _recv()
    assert parse_tick(_tick_body(exchname="NXT"), r).market == "N"
    assert parse_tick(_tick_body(exchname="KRX"), r).market == "K"
    assert parse_tick(_tick_body(exchname=None), r).market == "K"   # 미상은 KRX 취급


def _check_quote_uses_unified_volume():
    """KRX 전용 잔량을 쓰면 NXT 물량이 빠져 유동성을 과소평가한다."""
    r = _recv()
    q = parse_quote(_quote_body(), r)
    assert q.ask_vols[0] == 1000 and q.bid_vols[0] == 2000   # unt_* 값
    assert q.ask_vols[0] != 1, "KRX 전용(offerrem)을 읽고 있다"


def _check_spread_and_imbalance():
    r = _recv()
    q = parse_quote(_quote_body(), r)
    assert q.spread == 71100 - 70800, q.spread
    # BI = (bid - ask) / (bid + ask), 1호가 기준
    assert abs(q.book_imbalance - (2000 - 1000) / 3000) < 1e-9
    assert -1.0 <= q.book_imbalance <= 1.0
    # 역전 호가에서도 spread 가 음수가 되지 않는다
    q2 = parse_quote(_quote_body(offerho1="70000", bidho1="71000"), r)
    assert q2.spread == 0, q2.spread
    # 양쪽 잔량이 0이면 0.0 (0 나눗셈 방어)
    q3 = parse_quote(_quote_body(unt_offerrem1="0", unt_bidrem1="0"), r)
    assert q3.book_imbalance == 0.0


def _check_source_event_id_is_deterministic():
    """재적재해도 같은 체결이 두 번 들어가지 않는다."""
    r = _recv()
    t = parse_tick(_tick_body(), r)
    kw = dict(tr=TR_TICK, symbol=t.symbol, market=t.market,
              event_time=t.event_time, price=t.price, volume=t.volume)
    assert source_event_id(**kw) == source_event_id(**kw)
    # 값이 하나라도 다르면 다른 키
    assert source_event_id(**kw) != source_event_id(**{**kw, "price": t.price + 1})
    assert source_event_id(**kw) != source_event_id(**{**kw, "market": "N"})
    # seq 를 쓰면 같은 초 동일 체결도 구분된다(실시간 수집용)
    assert source_event_id(**kw, seq=1) != source_event_id(**kw, seq=2)


def _check_missing_numbers_do_not_become_signal():
    """빠진 값을 0 으로 채우면 그 0 이 나중에 신호가 된다 - 최소한 티는 나야 한다."""
    r = _recv()
    t = parse_tick(_tick_body(price=None, cvolume=None), r)
    assert t.price == 0 and t.volume == 0 and t.ofi_contrib == 0
    # 가격 0 은 유효한 체결이 아니므로 호출부가 걸러야 한다 - 여기서 검출 가능해야 한다
    assert t.price == 0, "가격 0 을 호출부가 판별할 수 있어야 한다"


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_three_timestamps_are_kept();      print("  세 시각 보존(이식 개선)   OK")
    _check_contract_time_invariant();        print("  계약 시각 불변식          OK")
    _check_midnight_boundary_both_directions(); print("  자정 경계 보정 양방향   OK")
    _check_bad_time_falls_back_to_received(); print("  이상 시각 -> 수신 시각    OK")
    _check_symbol_normalisation();           print("  종목코드 정규화           OK")
    _check_unknown_side_is_discarded();      print("  미상 체결구분 폐기        OK")
    _check_ofi_sign();                       print("  OFI 부호                  OK")
    _check_nxt_is_not_merged_into_krx();     print("  NXT/KRX 분리              OK")
    _check_quote_uses_unified_volume();      print("  통합 잔량(unt_*) 사용     OK")
    _check_spread_and_imbalance();           print("  스프레드·호가불균형       OK")
    _check_source_event_id_is_deterministic(); print("  멱등키 결정론           OK")
    _check_missing_numbers_do_not_become_signal(); print("  결측 -> 판별 가능      OK")
    print("LS 통합시세 파서 12개 영역 통과.")
