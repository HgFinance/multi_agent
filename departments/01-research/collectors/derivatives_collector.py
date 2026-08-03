#!/usr/bin/env python3
"""KOSPI200 파생(지수선물·지수옵션) 시세 스냅샷 수집 - t9943/t9944/t2301/t2111.

소유: 재일 (리서치본부 / 퀀트·백테스트본부)
근거: docs/06-integrations/ls-openapi/04-derivatives/01-9f467798.md (시세 30 TR)
      timescaledb/migrations/001_initial_market_data.sql (market.derivative_snapshots)
      supabase/migrations/20260729000100 (reference.derivative_contracts)

▶ 실측으로 확인한 것 (2026-07-31, 실전 Domain)
  - 파생 시세는 **운영 Domain 전용**(문서: 모의투자 Domain '-'), 실전 키는 별도
    신청 없이 열려 있다. 이 수집기는 LS_ENV 와 무관하게 LIVE 키를 강제한다.
  - t9943(지수선물 마스터) gubun ''=KOSPI200 정규 13개월물 / 'V'=미니 / 'S'=섹터.
    hname "F 2609" 이 만기 연월이고 expcode 12자가 ISIN 이다(KR4101...).
  - t9944(지수옵션 마스터) 5,224종목 - "C 2608 625.0" 형태로 종류·연월·행사가.
  - t2301(옵션전광판) 이 핵심이다. 월물 하나로 콜·풋 체인 전체(행사가별 IV·
    델타·감마·베가·쎄타·로우·이론가·미결제·1차 호가)와 근월물 선물 시세·대표
    IV·잔존일이 **한 호출**에 온다.
  - t2111(선물/옵션 현재가) 은 이론가·베이시스(sbasis/ibasis)·잔존일 포함 70필드.
  - **타입이 섞여 온다**(t1511 과 같은 함정): 가격·IV·그릭스는 String,
    거래량·미결제(mgjv)는 JSON Number. 한쪽만 가정하면 조용히 깨진다.
  - IV 는 % 단위다(대표 IV "80.840" = 80.84%). DB 에는 소수(0.8084)로 저장하고
    원값을 raw_flags.iv_raw 에 남긴다.

▶ 설계 결정
  - v1 범위는 **KOSPI200 정규**만: 선물 근월+차근월(t2111), 옵션 근월 체인(t2301).
    미니·위클리·섹터·주식선물·야간(t8455~t8460)은 백로그 (아래 주석).
  - 스냅샷 주기 10분(Scheduler), 장중 전용. Breadth 와 달리 PRIOR_CLOSE 를
    만들지 않는다 - 폐장 후 파생 체인은 왜곡이 심해(급등일 실측: 옵션 15:45
    마감가 vs 선물 야간가 괴리) 없는 편이 낫다.
  - **파생 세션 = 주식 세션 ±15분** 규칙: 정규일 09:00~15:30 -> 08:45~15:45,
    수능일 10:00~16:30 -> 09:45~16:45. KRX 파생 개폐장 규정과 일치한다.
  - 만기일 = 해당 월 **두 번째 목요일**(KRX 지수선물·옵션 공통). 근월물은
    응답의 잔존일(jandatecnt)과 ±2일 이내인지 대조하고 어긋나면 WARN.
  - 죽은 행사가(현재가·호가·거래량·미결제 전부 0)는 적재하지 않되 **버린 수를
    로그로 남긴다**(조용한 절단 금지). 체인 390행 중 유효분만 남는다.
  - 계약 등록: reference.instruments(+derivative_contracts, instrument_symbols)에
    스냅샷에 등장한 계약만 등록한다(마스터 전량 5천 행을 미리 채우지 않는다 -
    월물이 구르면 자연히 늘어난다). ISIN 은 마스터 expcode 로 채운다
    (instruments.isin 이 NULLS NOT DISTINCT 라 NULL 두 개가 충돌한다).
  - 기초자산은 KOSPI200 INDEX instrument 를 get-or-create 해 연결한다.

사용
  python collectors/derivatives_collector.py                  # 자체 점검 (호출 없음)
  python collectors/derivatives_collector.py --collect        # 1회 스냅샷 (Scheduler 가 10분마다)
  python collectors/derivatives_collector.py --sync-contracts # 계약 등록만 (세션 무관, 시세 미적재)
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))

COLLECTOR_VERSION = "research-derivatives-v1"
FO_PATH = "/futureoption/market-data"
KST = timezone(timedelta(hours=9))
EXIT_SKIP = 2                      # collector_scheduler 규약: 의도된 미수집
SCHEMA_VERSION = 1
PROVIDER = "ls"
MARKET = "KRX"
K200_MULTIPLIER = Decimal(250000)   # 정규 KOSPI200 선물·옵션 승수 (2017 개정 후)
SESSION_PAD = timedelta(minutes=15)   # 파생 = 주식 세션 ±15분
EXPIRY_TOLERANCE_DAYS = 2
# 상장일을 마스터가 주지 않아 조회 좌표 용도의 고정 valid_from 을 쓴다.
# 실제 상장일이 필요해지면 별도 TR 로 보강한다 - 지어내지 않는다.
DERIV_VALID_FROM = datetime(2000, 1, 1, tzinfo=KST)

# 백로그 (v1 제외 범위 - 조사는 끝났고 필요 시 TR 만 추가하면 된다)
#   미니선물/미니옵션: t9943 gubun 'V' / t2301 gubun 'M'
#   섹터지수선물: t9943 gubun 'S' (126종목)
#   주식선물: t8401(마스터)/t8402(현재가) - 종목 수가 많아 대상 선정부터
#   야간(KRX 글로벌): t8455~t8460, 세션 18:00~다음날 05:00
#   위클리옵션: t9944 안에 섞여 있는지 월물 표기 실측 후 결정


# ---------------------------------------------------------------------------
# 혼합 타입 파싱
# ---------------------------------------------------------------------------

def _dec(raw: object) -> Decimal | None:
    """String/Number 혼합 응답을 Decimal 로. ''·None 은 None (0 과 구분한다)."""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _int(raw: object) -> int | None:
    d = _dec(raw)
    if d is None:
        return None
    try:
        return int(d)
    except (ValueError, OverflowError):
        return None


def _pos_or_none(d: Decimal | None) -> Decimal | None:
    """가격류: 0 은 '없음'이다(폐장·미체결 행사가). 음수도 없음으로 본다."""
    return d if d is not None and d > 0 else None


# ---------------------------------------------------------------------------
# 만기·월물 규칙
# ---------------------------------------------------------------------------

def second_thursday(year: int, month: int) -> date:
    """KRX 지수선물·옵션 만기일 = 해당 월 두 번째 목요일."""
    first = date(year, month, 1)
    offset = (3 - first.weekday()) % 7      # weekday: 목=3
    return first.replace(day=1 + offset + 7)


_FUT_HNAME = re.compile(r"^(F)\s+(\d{2})(\d{2})$")            # "F 2609" (정규만)
_OPT_HNAME = re.compile(r"^([CP])\s+(\d{2})(\d{2})\s+([\d,.]+)$")  # "C 2608 625.0"


def parse_future_hname(hname: str) -> tuple[int, int] | None:
    """정규 지수선물 hname -> (연, 월). 미니(VF)·스프레드 등은 None."""
    m = _FUT_HNAME.match(hname.strip())
    if not m:
        return None
    yy, mm = int(m.group(2)), int(m.group(3))
    if not 1 <= mm <= 12:
        return None
    return 2000 + yy, mm


def parse_option_hname(hname: str) -> tuple[str, int, int, Decimal] | None:
    """지수옵션 hname -> (kind, 연, 월, 행사가). 형식이 다르면 None."""
    m = _OPT_HNAME.match(hname.strip())
    if not m:
        return None
    kind = "CALL" if m.group(1) == "C" else "PUT"
    yy, mm = int(m.group(2)), int(m.group(3))
    strike = _dec(m.group(4))
    if not 1 <= mm <= 12 or strike is None or strike <= 0:
        return None
    return kind, 2000 + yy, mm, strike


def front_month(months: list[tuple[int, int]], today: date) -> tuple[int, int] | None:
    """만기(두 번째 목요일)가 아직 지나지 않은 가장 가까운 월물."""
    live = sorted(m for m in months if second_thursday(*m) >= today)
    return live[0] if live else None


# ---------------------------------------------------------------------------
# 계약·스냅샷 모델
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContractSpec:
    shcode: str
    isin: str
    kind: str                 # FUTURE / CALL / PUT
    expiry: date
    display_name: str
    strike: Decimal | None = None


@dataclass
class SnapshotRow:
    shcode: str
    expiry: date
    option_type: str | None          # CALL/PUT, 선물은 None
    strike: Decimal | None
    last: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    open_interest: int | None
    volume: int | None
    iv: float | None                 # 소수 (0.8084)
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    theory: Decimal | None = None
    underlying_price: Decimal | None = None
    days_to_expiry: Decimal | None = None
    source_event_id: str = ""
    calculation_version: str = ""
    quality: str = "PASS"
    raw_flags: dict = field(default_factory=dict)


def _iv_fraction(raw: object, flags: dict) -> float | None:
    """% 표기 IV -> 소수. 음수는 없음 처리(DB check iv>=0)하고 흔적을 남긴다."""
    d = _dec(raw)
    if d is None:
        return None
    flags["iv_raw"] = str(d)
    if d < 0:
        flags["iv_negative_dropped"] = True
        return None
    return float(d) / 100.0


def _greek(raw: object) -> float | None:
    d = _dec(raw)
    return None if d is None else float(d)


def build_option_row(
    block: dict, *, kind: str, expiry: date, days_left: Decimal | None,
    underlying: Decimal | None, observed_kst: datetime,
) -> SnapshotRow | None:
    """t2301 체인 한 행 -> SnapshotRow. 죽은 행사가(정보 0)는 None."""
    shcode = str(block.get("optcode") or "").strip()
    strike = _dec(block.get("actprice"))
    if not shcode or strike is None or strike <= 0:
        return None

    flags: dict = {}
    last = _pos_or_none(_dec(block.get("price")))
    bid = _pos_or_none(_dec(block.get("bidho1")))
    ask = _pos_or_none(_dec(block.get("offerho1")))
    oi = _int(block.get("mgjv"))
    volume = _int(block.get("volume"))

    # 정보가 하나도 없는 행사가는 적재하지 않는다 (죽은 행 - 호출부가 수를 센다)
    if not any([last, bid, ask, oi, volume]):
        return None

    if oi is not None and oi < 0:
        flags["oi_negative_dropped"] = str(oi)
        oi = None
    # DB check (ask >= bid). 교차 호가는 어느 쪽이 맞는지 알 수 없다 - 둘 다
    # 버리고 원값을 남긴다. 지어서 맞추지 않는다.
    if bid is not None and ask is not None and ask < bid:
        flags["crossed_quote_dropped"] = {"bid": str(bid), "ask": str(ask)}
        bid = ask = None

    if str(block.get("atmgubun", "")).strip() == "1":
        flags["atm"] = True
    if last is not None and bid is None and ask is None:
        flags["no_quote"] = True     # 폐장 스냅샷·유동성 없음 - 정상이라 PASS 유지

    # IV 파싱이 flags 를 채우므로 품질 판정보다 먼저 실행돼야 한다
    iv = _iv_fraction(block.get("iv"), flags)
    quality = "WARN" if any(
        k in flags for k in ("crossed_quote_dropped", "oi_negative_dropped",
                             "iv_negative_dropped")
    ) else "PASS"

    row = SnapshotRow(
        shcode=shcode, expiry=expiry, option_type=kind, strike=strike,
        last=last, bid=bid, ask=ask, open_interest=oi, volume=volume,
        iv=iv,
        delta=_greek(block.get("delt")), gamma=_greek(block.get("gama")),
        theta=_greek(block.get("ceta")), vega=_greek(block.get("vega")),
        rho=_greek(block.get("rhox")),
        theory=_pos_or_none(_dec(block.get("theoryprice"))),
        underlying_price=underlying, days_to_expiry=days_left,
        source_event_id=f"t2301:{shcode}",
        calculation_version="ls-t2301",
        quality=quality, raw_flags=flags,
    )
    # underlying 은 지수가 아니라 근월물 선물가다 - 소비자가 오해하지 않게 명시
    if underlying is not None:
        row.raw_flags["underlying_basis"] = "front_future_gmprice"
    row.raw_flags["observed_kst"] = observed_kst.isoformat()
    return row


def build_future_row(
    block: dict, *, shcode: str, expiry: date, observed_kst: datetime,
) -> SnapshotRow:
    """t2111 선물 현재가 -> SnapshotRow."""
    flags: dict = {}
    for k in ("basis", "sbasis", "ibasis"):
        v = _dec(block.get(k))
        if v is not None:
            flags[k] = str(v)
    last = _pos_or_none(_dec(block.get("price")))
    days_left = _dec(block.get("jandatecnt"))
    oi = _int(block.get("openyak"))
    if oi is not None and oi < 0:
        flags["oi_negative_dropped"] = str(oi)
        oi = None
    flags["observed_kst"] = observed_kst.isoformat()
    return SnapshotRow(
        shcode=shcode, expiry=expiry, option_type=None, strike=None,
        last=last, bid=None, ask=None, open_interest=oi,
        volume=_int(block.get("volume")),
        iv=None, theory=_pos_or_none(_dec(block.get("theoryprice"))),
        underlying_price=None, days_to_expiry=days_left,
        source_event_id=f"t2111:{shcode}",
        calculation_version="ls-t2111",
        quality="PASS" if last is not None else "WARN",
        raw_flags=flags,
    )


def validate_front_expiry(expiry: date, days_left: Decimal | None,
                          today: date) -> bool:
    """만기 규칙(두 번째 목요일)과 응답 잔존일의 정합. ±2일 허용(포함 기준 차이)."""
    if days_left is None:
        return True   # 검증 수단 없음 - 통과가 아니라 미검증(호출부가 flag)
    implied = today + timedelta(days=int(days_left))
    return abs((implied - expiry).days) <= EXPIRY_TOLERANCE_DAYS


# ---------------------------------------------------------------------------
# LS 호출
# ---------------------------------------------------------------------------

def fetch_future_master(client) -> list[tuple[str, str, str]]:
    """t9943 정규 지수선물 -> [(shcode, hname, isin)]."""
    d = client.call_tr(path=FO_PATH, tr_cd="t9943",
                       in_block={"t9943InBlock": {"gubun": ""}},
                       rate_limit_per_sec=2.0)
    rows = d.get("t9943OutBlock") or []
    return [(str(r.get("shcode", "")).strip(), str(r.get("hname", "")).strip(),
             str(r.get("expcode", "")).strip()) for r in rows]


def fetch_option_master(client) -> dict[str, tuple[str, str]]:
    """t9944 지수옵션 -> {shcode: (hname, isin)}."""
    d = client.call_tr(path=FO_PATH, tr_cd="t9944",
                       in_block={"t9944InBlock": {"dummy": ""}},
                       rate_limit_per_sec=2.0)
    out: dict[str, tuple[str, str]] = {}
    for r in d.get("t9944OutBlock") or []:
        shcode = str(r.get("shcode", "")).strip()
        if shcode:
            out[shcode] = (str(r.get("hname", "")).strip(),
                           str(r.get("expcode", "")).strip())
    return out


def fetch_option_board(client, yyyymm: str) -> tuple[dict, list[dict], list[dict]]:
    d = client.call_tr(path=FO_PATH, tr_cd="t2301",
                       in_block={"t2301InBlock": {"yyyymm": yyyymm, "gubun": "G"}},
                       rate_limit_per_sec=2.0)
    header = d.get("t2301OutBlock")
    if not isinstance(header, dict):
        from ls_client import LsApiError
        raise LsApiError(f"t2301OutBlock 이 없다 yyyymm={yyyymm} keys={sorted(d)}")
    return header, d.get("t2301OutBlock1") or [], d.get("t2301OutBlock2") or []


def fetch_future_price(client, focode: str) -> dict:
    d = client.call_tr(path=FO_PATH, tr_cd="t2111",
                       in_block={"t2111InBlock": {"focode": focode}},
                       rate_limit_per_sec=5.0)
    block = d.get("t2111OutBlock")
    if not isinstance(block, dict):
        from ls_client import LsApiError
        raise LsApiError(f"t2111OutBlock 이 없다 focode={focode} keys={sorted(d)}")
    return block


# ---------------------------------------------------------------------------
# reference 등록 (Supabase)
# ---------------------------------------------------------------------------

def ensure_underlying(conn) -> str:
    """KOSPI200 INDEX instrument get-or-create -> instrument_id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select instrument_id from reference.instrument_symbols
            where provider = %s and market = %s and symbol = 'KOSPI200'
            """, (PROVIDER, MARKET))
        row = cur.fetchone()
        if row:
            return str(row[0])
        cur.execute(
            """
            insert into reference.instruments
              (instrument_type, asset_class, market, venue, currency,
               display_name, status, price_scale, metadata)
            values ('INDEX', 'EQUITY_INDEX', %s, 'KRX', 'KRW',
                    'KOSPI200', 'ACTIVE', 2,
                    '{"source": "research-derivatives-v1", "ls_upcode": "101"}')
            returning instrument_id
            """, (MARKET,))
        iid = str(cur.fetchone()[0])
        cur.execute(
            """
            insert into reference.instrument_symbols
              (instrument_id, provider, market, symbol, symbol_type,
               valid_from, is_primary)
            values (%s, %s, %s, 'KOSPI200', 'TRADING', %s, true)
            """, (iid, PROVIDER, MARKET, DERIV_VALID_FROM))
    conn.commit()
    return iid


def ensure_contracts(conn, specs: list[ContractSpec],
                     underlying_id: str) -> tuple[dict[str, str], int]:
    """계약을 reference 3종에 등록하고 {shcode: instrument_id} 를 돌려준다."""
    if not specs:
        return {}, 0
    codes = [s.shcode for s in specs]
    with conn.cursor() as cur:
        cur.execute(
            """
            select symbol, instrument_id from reference.instrument_symbols
            where provider = %s and market = %s and symbol = any(%s)
            """, (PROVIDER, MARKET, codes))
        known = {sym: str(iid) for sym, iid in cur.fetchall()}

        created = 0
        for s in specs:
            if s.shcode in known:
                continue
            if not s.isin:
                # instruments.isin 은 NULLS NOT DISTINCT - NULL 로 넣으면 서로
                # 충돌한다. 마스터에 없는 코드는 등록하지 않는다(지어내지 않는다).
                continue
            cur.execute(
                """
                insert into reference.instruments
                  (instrument_type, asset_class, market, venue, currency,
                   display_name, isin, status, price_scale, metadata)
                values (%s, 'DERIVATIVE', %s, 'KRX', 'KRW', %s, %s, 'ACTIVE', 2,
                        %s::jsonb)
                on conflict (isin) do update set updated_at = now()
                returning instrument_id
                """,
                ("FUTURE" if s.kind == "FUTURE" else "OPTION", MARKET,
                 s.display_name, s.isin,
                 json.dumps({"source": COLLECTOR_VERSION, "ls_shcode": s.shcode})))
            iid = str(cur.fetchone()[0])
            cur.execute(
                """
                insert into reference.instrument_symbols
                  (instrument_id, provider, market, symbol, symbol_type,
                   valid_from, is_primary)
                values (%s, %s, %s, %s, 'TRADING', %s, true)
                on conflict (provider, market, symbol, valid_from) do nothing
                """, (iid, PROVIDER, MARKET, s.shcode, DERIV_VALID_FROM))
            cur.execute(
                """
                insert into reference.derivative_contracts
                  (instrument_id, underlying_instrument_id, contract_kind,
                   expiry_date, strike_price, contract_multiplier,
                   settlement_type, exercise_style, margin_currency, metadata)
                values (%s, %s, %s, %s, %s, %s, 'CASH', %s, 'KRW', %s::jsonb)
                on conflict (instrument_id) do nothing
                """,
                (iid, underlying_id, s.kind, s.expiry, s.strike, K200_MULTIPLIER,
                 "EUROPEAN" if s.kind in ("CALL", "PUT") else None,
                 json.dumps({"expiry_rule": "second_thursday",
                             "registered_by": COLLECTOR_VERSION})))
            known[s.shcode] = iid
            created += 1
    conn.commit()
    return known, created


# ---------------------------------------------------------------------------
# 세션 판정 - 파생 = 주식 세션 ±15분
# ---------------------------------------------------------------------------

def resolve_deriv_session(conn, now_kst: datetime) -> tuple[bool, str]:
    """(수집해도 되는가, 사유). Calendar 미상 평일은 기본 창으로 수집하되 사유에 남긴다."""
    today = now_kst.date()
    with conn.cursor() as cur:
        cur.execute(
            """
            select s.is_trading_day, s.opens_at, s.closes_at
            from reference.market_sessions s
            join reference.market_calendar_versions v using (calendar_version_id)
            where s.market = %s and s.trade_date = %s and s.session_type = 'REGULAR'
            order by v.version desc limit 1
            """, (MARKET, today))
        row = cur.fetchone()
    if row is not None:
        trading, opens_at, closes_at = row
        if not trading:
            return False, "calendar: 비거래일"
        if opens_at is not None and closes_at is not None:
            lo = opens_at - SESSION_PAD
            hi = closes_at + SESSION_PAD
            if lo <= now_kst <= hi:      # 둘 다 aware - tz 가 달라도 비교는 절대시각
                return True, "calendar: 파생 세션 창"
            return False, f"파생 세션 밖 ({lo.astimezone(KST):%H:%M}~{hi.astimezone(KST):%H:%M})"
        return False, "calendar: 세션 시각 미상"
    if today.weekday() >= 5:
        return False, "calendar 없음 + 주말"
    lo = datetime.combine(today, time(8, 45), tzinfo=KST)
    hi = datetime.combine(today, time(15, 45), tzinfo=KST)
    if lo <= now_kst <= hi:
        return True, "calendar 미상 평일 - 기본 창(08:45~15:45), UNVERIFIED"
    return False, "calendar 미상 + 기본 창 밖"


# ---------------------------------------------------------------------------
# TSDB 적재
# ---------------------------------------------------------------------------

def write_snapshots(tconn, rows: list[SnapshotRow], *, id_by_code: dict[str, str],
                    underlying_id: str, event_time: datetime,
                    observed_at: datetime) -> tuple[int, int]:
    inserted = skipped = 0
    with tconn.cursor() as cur:
        for r in rows:
            iid = id_by_code.get(r.shcode)
            if iid is None:
                skipped += 1
                continue
            cur.execute(
                """
                insert into market.derivative_snapshots
                  (event_time, received_at, observed_at, instrument_id,
                   underlying_instrument_id, provider, market, expiry_date,
                   strike_price, option_type, last_price, bid, ask,
                   open_interest, volume, implied_volatility, delta, gamma,
                   theta, vega, rho, theoretical_price, underlying_price,
                   days_to_expiry, source_event_id, calculation_version,
                   quality_status, raw_flags, schema_version)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s::jsonb, %s)
                on conflict (event_time, source_event_id) do nothing
                """,
                (event_time, observed_at, observed_at, iid, underlying_id,
                 PROVIDER, MARKET, r.expiry, r.strike, r.option_type, r.last,
                 r.bid, r.ask, r.open_interest, r.volume, r.iv, r.delta,
                 r.gamma, r.theta, r.vega, r.rho, r.theory,
                 r.underlying_price, r.days_to_expiry, r.source_event_id,
                 r.calculation_version, r.quality, json.dumps(r.raw_flags),
                 SCHEMA_VERSION))
            inserted += cur.rowcount
            skipped += 1 - cur.rowcount
    tconn.commit()
    return inserted, skipped


# ---------------------------------------------------------------------------
# 수집 본체
# ---------------------------------------------------------------------------

def load_universe(client, today: date):
    """마스터 -> (선물 근월+차근월, 옵션 마스터, 옵션 근월 yyyymm).

    세션과 무관한 사실(계약 목록)이라 --collect 와 --sync-contracts 가 공유한다.
    """
    fut_master = fetch_future_master(client)
    futures: list[tuple[tuple[int, int], str, str]] = []
    for shcode, hname, isin in fut_master:
        ym = parse_future_hname(hname)
        if ym and isin:
            futures.append((ym, shcode, isin))
    futures.sort()
    fronts = [f for f in futures if second_thursday(*f[0]) >= today][:2]
    if not fronts:
        raise RuntimeError("정규 선물 근월물을 못 찾았다 - 마스터 파싱 확인 필요")
    opt_master = fetch_option_master(client)
    opt_months: set[tuple[int, int]] = set()
    for hname, _isin in opt_master.values():
        parsed = parse_option_hname(hname)
        if parsed:
            opt_months.add((parsed[1], parsed[2]))
    opt_front = front_month(sorted(opt_months), today)
    if opt_front is None:
        raise RuntimeError("옵션 근월물을 못 찾았다 - 마스터 파싱 확인 필요")
    return fut_master, fronts, opt_master, opt_front


def collect() -> int:
    import psycopg2
    from ls_client import LsEnvironment, LsRestClient
    from source_registry import SourceRegistry, load_project_env

    env = load_project_env()
    SourceRegistry(env=env).require("ls_openapi_rest")
    now_kst = datetime.now(KST)
    observed_at = datetime.now(timezone.utc)

    ref_conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    try:
        ok, reason = resolve_deriv_session(ref_conn, now_kst)
        print(f"{COLLECTOR_VERSION}: 세션 판정 - {reason}", flush=True)
        if not ok:
            return EXIT_SKIP

        # 파생 시세는 운영 Domain 전용 - LS_ENV 와 무관하게 LIVE 키를 쓴다
        client = LsRestClient(LsEnvironment.from_env({**env, "LS_ENV": "LIVE"}))
        today = now_kst.date()
        fut_master, fronts, opt_master, opt_front = load_universe(client, today)

        # 2) 옵션 체인 (t2301 한 호출)
        yyyymm = f"{opt_front[0]:04d}{opt_front[1]:02d}"
        header, calls, puts = fetch_option_board(client, yyyymm)
        opt_expiry = second_thursday(*opt_front)
        days_left = _dec(header.get("jandatecnt"))
        underlying = _pos_or_none(_dec(header.get("gmprice")))
        expiry_ok = validate_front_expiry(opt_expiry, days_left, today)

        rows: list[SnapshotRow] = []
        dead = unknown_isin = 0
        specs: list[ContractSpec] = []
        for kind, blocks in (("CALL", calls), ("PUT", puts)):
            for b in blocks:
                row = build_option_row(b, kind=kind, expiry=opt_expiry,
                                       days_left=days_left, underlying=underlying,
                                       observed_kst=now_kst)
                if row is None:
                    dead += 1
                    continue
                if not expiry_ok:
                    row.raw_flags["expiry_mismatch"] = {
                        "rule": str(opt_expiry), "jandatecnt": str(days_left)}
                    row.quality = "WARN"
                master = opt_master.get(row.shcode)
                if master is None or not master[1]:
                    unknown_isin += 1
                    continue
                specs.append(ContractSpec(
                    shcode=row.shcode, isin=master[1], kind=kind,
                    expiry=opt_expiry, display_name=master[0], strike=row.strike))
                rows.append(row)

        # 3) 선물 근월+차근월 (t2111)
        for ym, shcode, isin in fronts:
            fut_expiry = second_thursday(*ym)
            block = fetch_future_price(client, shcode)
            frow = build_future_row(block, shcode=shcode, expiry=fut_expiry,
                                    observed_kst=now_kst)
            if not validate_front_expiry(fut_expiry, frow.days_to_expiry, today):
                frow.raw_flags["expiry_mismatch"] = {
                    "rule": str(fut_expiry), "jandatecnt": str(frow.days_to_expiry)}
                frow.quality = "WARN"
            hname = next((h for c, h, _ in fut_master if c == shcode), shcode)
            specs.append(ContractSpec(shcode=shcode, isin=isin, kind="FUTURE",
                                      expiry=fut_expiry, display_name=hname))
            rows.append(frow)

        # 4) 계약 등록 + 적재
        underlying_id = ensure_underlying(ref_conn)
        id_by_code, created = ensure_contracts(ref_conn, specs, underlying_id)
        tconn = psycopg2.connect(env["TIMESCALE_DATABASE_URL"], connect_timeout=20)
        try:
            event_time = observed_at.replace(microsecond=0)
            inserted, skipped = write_snapshots(
                tconn, rows, id_by_code=id_by_code, underlying_id=underlying_id,
                event_time=event_time, observed_at=observed_at)
        finally:
            tconn.close()

        warn = sum(1 for r in rows if r.quality != "PASS")
        print(f"  월물 {yyyymm} (만기 {opt_expiry}, 잔존 {days_left}) | "
              f"체인 {len(calls) + len(puts)}행 중 유효 {len(rows) - len(fronts)} / "
              f"죽은 행사가 {dead} / ISIN 미상 제외 {unknown_isin}", flush=True)
        print(f"  적재 {inserted} (중복 {skipped}) / 신규 계약 등록 {created} / "
              f"WARN {warn} / 선물 {', '.join(c for _, c, _ in fronts)}", flush=True)
        return 0
    finally:
        ref_conn.close()


def sync_contracts() -> int:
    """계약 등록만 (세션 무관). 시세는 한 행도 적재하지 않는다 - 계약 목록은
    사실이라 폐장 조회로도 왜곡이 없다. 배포 전 검증과 월물 선등록에 쓴다."""
    import psycopg2
    from ls_client import LsEnvironment, LsRestClient
    from source_registry import SourceRegistry, load_project_env

    env = load_project_env()
    SourceRegistry(env=env).require("ls_openapi_rest")
    today = datetime.now(KST).date()
    client = LsRestClient(LsEnvironment.from_env({**env, "LS_ENV": "LIVE"}))
    fut_master, fronts, opt_master, opt_front = load_universe(client, today)
    yyyymm = f"{opt_front[0]:04d}{opt_front[1]:02d}"
    _header, calls, puts = fetch_option_board(client, yyyymm)
    opt_expiry = second_thursday(*opt_front)

    specs: list[ContractSpec] = []
    for kind, blocks in (("CALL", calls), ("PUT", puts)):
        for b in blocks:
            shcode = str(b.get("optcode") or "").strip()
            strike = _dec(b.get("actprice"))
            master = opt_master.get(shcode)
            if not shcode or strike is None or master is None or not master[1]:
                continue
            specs.append(ContractSpec(shcode=shcode, isin=master[1], kind=kind,
                                      expiry=opt_expiry, display_name=master[0],
                                      strike=strike))
    for ym, shcode, isin in fronts:
        hname = next((h for c, h, _ in fut_master if c == shcode), shcode)
        specs.append(ContractSpec(shcode=shcode, isin=isin, kind="FUTURE",
                                  expiry=second_thursday(*ym), display_name=hname))

    ref_conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    try:
        underlying_id = ensure_underlying(ref_conn)
        id_by_code, created = ensure_contracts(ref_conn, specs, underlying_id)
    finally:
        ref_conn.close()
    print(f"{COLLECTOR_VERSION}: 계약 동기화 - 월물 {yyyymm} (만기 {opt_expiry}) | "
          f"대상 {len(specs)} / 신규 등록 {created} / 매핑 확보 {len(id_by_code)} | "
          f"기초자산 {underlying_id[:8]}…", flush=True)
    return 0


# ---------------------------------------------------------------------------
# 자체 점검 - 호출·DB 없음
# ---------------------------------------------------------------------------

def _check_parsers():
    assert parse_future_hname("F 2609") == (2026, 9)
    assert parse_future_hname("VF 2608") is None          # 미니는 v1 제외
    assert parse_future_hname("에너지화학 2609") is None
    assert parse_option_hname("C 2608   625.0") == ("CALL", 2026, 8, Decimal("625.0"))
    assert parse_option_hname("P 2812 1,590.0") == ("PUT", 2028, 12, Decimal("1590.0"))
    assert parse_option_hname("F 2609") is None
    assert _dec("1,030.65") == Decimal("1030.65") and _dec("") is None
    assert _int(166478) == 166478 and _int("12") == 12
    print("  마스터/혼합타입 파싱     OK")


def _check_expiry_rules():
    assert second_thursday(2026, 8) == date(2026, 8, 13)   # 8/1 토 -> 첫 목 6, 둘째 13
    assert second_thursday(2026, 9) == date(2026, 9, 10)
    assert second_thursday(2027, 1) == date(2027, 1, 14)
    assert front_month([(2026, 8), (2026, 9)], date(2026, 8, 13)) == (2026, 8)  # 만기 당일까지 근월
    assert front_month([(2026, 8), (2026, 9)], date(2026, 8, 14)) == (2026, 9)
    # 실측: 2026-07-31 에 jandatecnt=14 (포함 기준) - 만기 8/13 과 1일 차, 허용
    assert validate_front_expiry(date(2026, 8, 13), Decimal(14), date(2026, 7, 31))
    assert not validate_front_expiry(date(2026, 8, 13), Decimal(30), date(2026, 7, 31))
    print("  만기 규칙(둘째 목요일)   OK")


def _check_option_row():
    base = {"actprice": "872.50", "optcode": "B0168872", "price": "58.10",
            "iv": "80.84", "mgjv": 1, "offerho1": "0.00", "bidho1": "0.04",
            "delt": "0.8894", "gama": "0.0011", "vega": "0.3850",
            "ceta": "-1.1914", "rhox": "0.02", "theoryprice": "183.15",
            "volume": 0, "atmgubun": "1"}
    kw = {"kind": "CALL", "expiry": date(2026, 8, 13), "days_left": Decimal(14),
              "underlying": Decimal("1030.65"),
              "observed_kst": datetime(2026, 7, 31, 22, 0, tzinfo=KST)}
    r = build_option_row(base, **kw)
    assert r is not None and abs(r.iv - 0.8084) < 1e-12
    assert r.raw_flags["iv_raw"] == "80.84"
    assert r.ask is None and r.bid == Decimal("0.04")      # 0.00 호가는 없음
    assert r.raw_flags["atm"] is True and r.quality == "PASS"
    assert r.theta == -1.1914 and r.source_event_id == "t2301:B0168872"
    # 죽은 행사가 - 가격·호가·거래·미결제 전부 없음
    assert build_option_row({**base, "price": "0", "bidho1": "0", "offerho1": "0",
                             "mgjv": 0, "volume": 0}, **kw) is None
    # 교차 호가 - 둘 다 버리고 WARN
    r2 = build_option_row({**base, "bidho1": "2.00", "offerho1": "1.00"}, **kw)
    assert r2.bid is None and r2.ask is None and r2.quality == "WARN"
    assert "crossed_quote_dropped" in r2.raw_flags
    # 음수 IV 는 없음 처리 + 흔적
    r3 = build_option_row({**base, "iv": "-1"}, **kw)
    assert r3.iv is None and r3.raw_flags["iv_negative_dropped"] is True
    print("  옵션 행 품질 가드        OK")


def _check_future_row():
    b = {"price": "1030.65", "theoryprice": "1049.58", "sbasis": "-16.16",
         "jandatecnt": 14, "volume": 166478, "openyak": 250000}
    r = build_future_row(b, shcode="A0169000", expiry=date(2026, 8, 13),
                         observed_kst=datetime(2026, 7, 31, 22, 0, tzinfo=KST))
    assert r.option_type is None and r.strike is None
    assert r.last == Decimal("1030.65") and r.open_interest == 250000
    assert r.raw_flags["sbasis"] == "-16.16" and r.quality == "PASS"
    r2 = build_future_row({}, shcode="X", expiry=date(2026, 8, 13),
                          observed_kst=datetime(2026, 7, 31, 22, 0, tzinfo=KST))
    assert r2.quality == "WARN" and r2.last is None
    print("  선물 행 파싱             OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--collect" in sys.argv:
        raise SystemExit(collect())
    if "--sync-contracts" in sys.argv:
        raise SystemExit(sync_contracts())

    print(f"{COLLECTOR_VERSION} 자체 점검 (호출·DB 없음)")
    _check_parsers()
    _check_expiry_rules()
    _check_option_row()
    _check_future_row()
    print("파생 수집기 4개 영역 통과. 수집은 --collect")
