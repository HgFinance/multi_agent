#!/usr/bin/env python3
"""P0 시장 상태(Market Breadth) 수집 - LS t1511 업종현재가.

소유: 재일 (리서치본부)
근거: docs/06-integrations/ls-openapi/02-industry/01-88a7c0d3.md (t1511 업종현재가, t8424 전체업종)
      docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.1 P0 "시장 상태", 4.2 PIT, 8.2 DQ
      timescaledb/migrations/001_initial_market_data.sql (market.market_breadth)

▶ 왜 LS 인가
  KRX Open API 의 지수 5종(krx_dd_trd, kospi_dd_trd, kosdaq_dd_trd, bon_dd_trd,
  drvprod_dd_trd)이 등락종목수를 주지만, 2026-07-31 실측에서 **승인 경로와 샘플 경로
  둘 다 401 Unauthorized API Call** 이다(source_registry 의 NOT_AUTHORIZED_OBSERVED 참고).
  서비스 이용 승인이 나기 전까지 Breadth 를 얻을 수 있는 경로는 LS t1511 하나뿐이다.

▶ 실측으로 확인한 것 (2026-07-31)
  - t8424(전체업종) gubun1 = 1:코스피 58개 / 2:코스닥 32개 / 0:전체 252개.
    종합지수 업종코드는 코스피 "001", 코스닥 "301" 이다.
  - t1511 응답은 **타입이 섞여 온다.** 지수·등락률(pricejisu, diffjisu, change)은
    String, 종목수·거래량·거래대금(highjo, lowjo, volume, value)은 JSON Number 다.
    한쪽만 가정하면 조용히 깨진다.
  - 등락종목수 합이 상장 보통주 수와 맞는다(코스피 605+278+58=941, 코스닥
    728+938+151=1817). 이 합을 reference.instruments 와 대조하는 것을 DQ 로 쓴다.
  - t1511 응답에 **현재 시각 필드가 없다.** opentime/hightime/lowtime 만 있다.
    그래서 event_time 을 관측 시각에서 유도한다(아래 규칙).

▶ event_time 규칙 - 이게 이 파일에서 제일 조심할 부분이다
  Breadth 는 Snapshot 이라 폐장 뒤에 몇 번을 조회해도 같은 상태가 온다. 관측 시각을
  그대로 event_time 으로 쓰면 **같은 종가 상태가 시각만 다른 행으로 계속 쌓인다.**
    - 장중(REGULAR)   -> event_time = 관측 시각(초 절삭). 시계열로 쌓이는 게 맞다.
    - 폐장 후(POST)   -> event_time = 그 날 폐장 시각. 여러 번 수집해도 PK 충돌로 1행.
    - 개장 전 / 비거래일 / Calendar 에 없는 날 -> **수집 거부**(개발 원칙 9).
      직전 종가를 오늘 상태처럼 적재하는 것이 이 도메인에서 제일 위험하다.

자체 점검(호출 없음): python departments/01-research/collectors/market_breadth_collector.py
실제 수집:            python departments/01-research/collectors/market_breadth_collector.py --collect
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))
from ls_client import LsApiError, LsRestClient

COLLECTOR_VERSION = "research-market-breadth-v1"

T1511_PATH = "/indtp/market-data"
T1511_RATE_LIMIT = 1.0  # 문서 "초당 호출 제한: 1"
T8424_PATH = "/indtp/market-data"
T8424_RATE_LIMIT = 1.0

BREADTH_SOURCE = "ls:t1511"
CALENDAR_MARKET = "KRX"
KST = timezone(timedelta(hours=9))

# LS 거래대금 단위. t1511 문서에는 단위 표기가 없지만 LS 의 다른 TR 은 value 를 일관되게
# "거래대금(백만)" 으로 적는다(03-stock/01-54a99b02.md 784행, 08-30b6dfd6.md 429행 등).
# 규모도 맞는다 - 코스피 38,320,902 백만원 = 38.3조. 원 단위로 환산해 넣되 **원본 숫자를
# values jsonb 에 같이 남긴다.** 배수 가정이 틀렸을 때 되돌릴 수 있어야 한다.
VALUE_UNIT_MULTIPLIER = Decimal(1_000_000)
VALUE_UNIT_NOTE = (
    "t1511 문서에 단위 표기 없음. LS 타 TR 의 'value=거래대금(백만)' 관례와 규모 검증으로 "
    "백만원 가정. 원본은 total_value_raw 에 보존"
)

# LS 전일대비구분. change 는 부호 없는 크기라서 이 코드로 방향을 붙인다.
SIGN_TO_DIRECTION = {
    "1": "LIMIT_UP", "2": "UP", "3": "UNCHANGED", "4": "LIMIT_DOWN", "5": "DOWN",
}
_NEGATIVE_SIGNS = frozenset({"4", "5"})

# 등락종목수 합 / 상장 보통주 수 가 이 범위를 벗어나면 WARN 이다. 지수 구성이 상장
# 전체와 정확히 같지는 않으므로 여유를 둔다.
COVERAGE_WARN_LOW = 0.5
COVERAGE_WARN_HIGH = 1.5


class BreadthError(RuntimeError):
    """Breadth 수집 실패. 빈 결과나 직전 상태로 바꾸지 않는다."""


class SessionPhase(StrEnum):
    REGULAR = "REGULAR"                    # 오늘이 거래일이고 장중
    POST_CLOSE = "POST_CLOSE"              # 오늘이 거래일이고 폐장 후
    PRIOR_CLOSE = "PRIOR_CLOSE"            # 오늘 개장 전. t1511 이 직전 세션 종가를 준다
    PRE_OPEN_UNRESOLVED = "PRE_OPEN_UNRESOLVED"  # 개장 전인데 직전 세션을 못 찾음
    NON_TRADING = "NON_TRADING"            # 오늘이 휴장으로 확인됨
    CALENDAR_MISSING = "CALENDAR_MISSING"  # Calendar 에 오늘이 없고 이미 개장 시각 지남
    CALENDAR_STALE = "CALENDAR_STALE"      # 직전 세션이 너무 오래됨


# 수집을 허용하는 국면. 나머지는 거부한다.
_COLLECTABLE = frozenset(
    {SessionPhase.REGULAR, SessionPhase.POST_CLOSE, SessionPhase.PRIOR_CLOSE}
)

# Calendar 에 오늘이 없을 때 직전 세션을 얼마나 거슬러 올라가 인정할지. 설·추석
# 연휴가 주말과 붙으면 9일까지 벌어질 수 있어 10일로 잡는다. 이보다 멀면 Calendar
# 갱신을 잊은 것으로 본다.
MAX_PRIOR_SESSION_GAP = timedelta(days=10)


@dataclass(frozen=True)
class BreadthIndex:
    """market_breadth.market 에 넣을 값과 그것을 얻는 업종코드."""

    market: str      # 'KOSPI' | 'KOSDAQ'
    upcode: str      # t1511InBlock.upcode
    label: str       # 사람이 읽는 이름 (t8424 hname 과 대조용)


# reference.instruments 는 market='KRX' / venue='KOSPI'|'KOSDAQ' 로 쓰지만
# market.market_breadth 에는 venue Column 이 없다. Breadth 는 지수 단위로만 의미가
# 있어서(코스피와 코스닥의 등락종목수는 별개다) market Column 에 venue 값을 넣는다.
# 두 Table 의 'market' 이 다른 층위를 가리키므로 조인할 때 주의한다.
INDEX_SPECS: tuple[BreadthIndex, ...] = (
    BreadthIndex(market="KOSPI", upcode="001", label="종합"),
    BreadthIndex(market="KOSDAQ", upcode="301", label="코스닥 종합"),
)


# ---------------------------------------------------------------------------
# 타입 섞인 응답 파싱
# ---------------------------------------------------------------------------

# market.market_breadth 의 종목수 Column 이 integer 다. 이 범위를 넘는 값은 애초에
# 저장할 수 없으므로 '읽을 수 없는 값'으로 취급한다 - 여기서 통과시키면 DQ 를 다
# 지나간 뒤 INSERT 에서 터진다.
INT32_MIN, INT32_MAX = -2_147_483_648, 2_147_483_647


def _as_int(raw: object) -> int | None:
    """String 으로 오든 Number 로 오든 정수로. 못 읽으면 None 이고 0 이 아니다.

    NaN/Infinity 를 None 으로 떨어뜨린다 - Decimal('NaN') 은 유효한 Decimal 이라
    그냥 두면 int() 에서 계약 밖 예외가 나거나 NaN 이 그대로 저장된다.
    integer Column 범위를 넘는 값도 None 이다(위 INT32 주석 참고).
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if INT32_MIN <= raw <= INT32_MAX else None
    if isinstance(raw, float):
        # math.isfinite 대신 자기 비교로 NaN 을, 범위로 inf 를 거른다
        if math.isnan(raw) or raw in (float("inf"), float("-inf")):
            return None
        v = int(raw)
        return v if INT32_MIN <= v <= INT32_MAX else None
    s = str(raw).strip().replace(",", "")
    if not s:
        return None
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    try:
        v = int(d)
    except (InvalidOperation, OverflowError, ValueError):
        return None
    return v if INT32_MIN <= v <= INT32_MAX else None


# market.market_breadth 의 금액 Column 이 numeric(38,10) 이라 정수부가 28자리를
# 넘으면 저장할 수 없다. _as_int 의 INT32 경계와 같은 이유로 여기서 막는다.
_NUMERIC_ABS_MAX = Decimal(10) ** 28


def _as_decimal(raw: object) -> Decimal | None:
    """못 읽으면 None. NaN/Infinity 와 numeric(38,10) 범위 밖도 None 이다.

    NaN·Infinity 는 유효한 Decimal 이지만 값이 아니고, 범위 밖은 값이지만 저장할
    수 없다. 둘 다 '읽을 수 없음' 으로 모아야 DQ 가 한 곳에서 잡는다.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, float) and (math.isnan(raw) or raw in (float("inf"), float("-inf"))):
        return None
    s = str(raw).strip().replace(",", "")
    if not s:
        return None
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return d if abs(d) < _NUMERIC_ABS_MAX else None


# ---------------------------------------------------------------------------
# 세션 판정과 event_time
# ---------------------------------------------------------------------------

def derive_open_time_of_day(
    sessions: list[tuple[date, datetime | None, datetime | None]]
) -> time | None:
    """최근 세션들의 개장 시각(**KST 기준** 시:분)이 모두 같으면 그 값.

    하나라도 다르면 None 이다 - 개장 시각이 바뀐 이력이 있다는 뜻이고, 그때는
    '오늘 개장 예정 시각' 을 추정하면 안 된다. 상수로 09:00 을 박지 않는 이유다.

    ▶ KST 로 정규화하는 이유 (2026-07-31 수정)
      호출부가 이 값을 `datetime.combine(today, tod)` 로 쓰는데 today 는 **KST 달력
      날짜** 다. UTC 로 정규화하면 09:00 KST = 00:00 UTC 라서 우연히 맞을 뿐이고,
      개장이 09:00 KST 보다 이르면(예: 08:30 -> 23:30 UTC, 전날 날짜) expected_open
      이 정확히 24시간 늦게 계산된다. 그러면 "개장 시각을 지났으면 거부" 가드가 그
      KST 하루 내내 발동하지 않아 **fail-closed 가 fail-open 으로 뒤집힌다.**
      Calendar 는 t8410 의 s_time 을 구간 전체에 일괄 적용하므로 재빌드 한 번이면
      최근 세션 전부가 동시에 바뀐다 - "값이 섞이면 None" 안전밸브가 정작 이
      경우에는 발동할 수 없다.
    """
    tods = {s[1].astimezone(KST).timetz() for s in sessions if s[1] is not None}
    return None if len(tods) != 1 else tods.pop()


@dataclass(frozen=True)
class SessionContext:
    phase: SessionPhase
    event_time: datetime | None
    # 어느 세션을 근거로 삼았는지. 판정을 나중에 재현할 수 있어야 한다.
    basis_date: date | None
    detail: str = ""


def resolve_session(
    *,
    today_session: tuple[bool, datetime | None, datetime | None] | None,
    recent_sessions: list[tuple[date, datetime | None, datetime | None]],
    now: datetime,
    today: date,
) -> SessionContext:
    """현재 시각이 어느 거래 세션에 속하는지와 그때의 event_time 을 정한다.

    ▶ 왜 '오늘 날짜' 만으로 판정하지 않는가
      Calendar 가 t8410 일봉 **관측 역산** 이라 원리상 당일·미래 날짜가 없다.
      '오늘이 Calendar 에 없다' 를 곧바로 거부로 만들면 개장 전 시간대에는 영원히
      수집할 수 없다. 그런데 개장 전이라면 t1511 이 주는 것은 **직전 세션의 종가
      상태** 이고, 그것을 직전 세션 폐장 시각으로 적재하는 것은 거짓이 아니다.

    ▶ 대신 절대 하지 않는 것
      '오늘이 Calendar 에 없는데 이미 개장 시각이 지난' 경우는 거부한다. 그 시간대의
      t1511 은 오늘 장중 상태일 수도, 오늘이 휴장이라 직전 종가일 수도 있는데
      응답만으로 구분할 방법이 없다. 구분 못 하는 것을 찍지 않는다(개발 원칙 9).
    """
    if today_session is not None:
        is_trading, opens_at, closes_at = today_session
        if not is_trading:
            return SessionContext(SessionPhase.NON_TRADING, None, today, "Calendar 상 휴장")
        if opens_at is None or closes_at is None:
            return SessionContext(
                SessionPhase.CALENDAR_MISSING, None, today, "거래일인데 개장·폐장 시각이 없다"
            )
        if now > closes_at:
            return SessionContext(SessionPhase.POST_CLOSE, closes_at, today, "당일 폐장 후")
        if now >= opens_at:
            return SessionContext(
                SessionPhase.REGULAR, now.replace(microsecond=0), today, "당일 장중"
            )
        # 오늘이 거래일인데 아직 개장 전 -> 직전 세션 종가로 떨어진다
        return _prior_close(recent_sessions, today, "당일 개장 전")

    # 오늘이 Calendar 에 없다. 개장 예정 시각 이전일 때만 직전 세션으로 인정한다.
    tod = derive_open_time_of_day(recent_sessions)
    if tod is None:
        return SessionContext(
            SessionPhase.CALENDAR_MISSING, None, None,
            "Calendar 에 오늘이 없고 개장 시각도 일정하지 않아 개장 전인지 판정 불가",
        )
    expected_open = datetime.combine(today, tod)
    if now >= expected_open:
        return SessionContext(
            SessionPhase.CALENDAR_MISSING, None, None,
            f"Calendar 에 {today} 가 없는데 개장 예정({expected_open:%H:%M%z})을 지났다. "
            f"Calendar 를 먼저 갱신한다",
        )
    return _prior_close(recent_sessions, today, "Calendar 미확장 + 개장 전")


def _prior_close(
    recent_sessions: list[tuple[date, datetime | None, datetime | None]],
    today: date,
    detail: str,
) -> SessionContext:
    """직전 거래 세션의 폐장 시각을 event_time 으로 쓴다."""
    for trade_date, _opens, closes in recent_sessions:
        if trade_date >= today or closes is None:
            continue
        if today - trade_date > MAX_PRIOR_SESSION_GAP:
            return SessionContext(
                SessionPhase.CALENDAR_STALE, None, trade_date,
                f"직전 거래일이 {trade_date} 로 {(today - trade_date).days}일 전이다. "
                f"Calendar 갱신이 밀렸을 가능성이 크다",
            )
        return SessionContext(SessionPhase.PRIOR_CLOSE, closes, trade_date, detail)
    return SessionContext(
        SessionPhase.PRE_OPEN_UNRESOLVED, None, None, "Calendar 에 직전 거래 세션이 없다"
    )


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BreadthSnapshot:
    market: str
    event_time: datetime
    observed_at: datetime
    advancers: int | None
    decliners: int | None
    unchanged: int | None
    total_value: Decimal | None
    quality_status: str
    values: dict

    def to_row(self) -> dict:
        return {
            "event_time": self.event_time,
            "observed_at": self.observed_at,
            "market": self.market,
            # 거래소가 상장 전체로 계산한 값이라 우리 구독 Universe 와 무관하다.
            "universe_version_id": None,
            "advancers": self.advancers,
            "decliners": self.decliners,
            "unchanged": self.unchanged,
            # t1511 은 신고가/신저가 '종목수' 를 주지 않는다(지수 자체의 52주 최고·최저만
            # 준다). 없는 것을 0 으로 채우면 '신고가 종목이 없었다' 는 거짓이 된다.
            "new_highs": None,
            "new_lows": None,
            # 상승·하락 거래량 구분도 t1511 에 없다. 총거래량은 values 에 남긴다.
            "up_volume": None,
            "down_volume": None,
            "total_value": self.total_value,
            "values": self.values,
            "source": BREADTH_SOURCE,
            "quality_status": self.quality_status,
        }


_EVENT_TIME_ORIGIN = {
    SessionPhase.REGULAR: "OBSERVED_AT",
    SessionPhase.POST_CLOSE: "SESSION_CLOSE",
    SessionPhase.PRIOR_CLOSE: "PRIOR_SESSION_CLOSE",
}


def parse_breadth(
    out_block: dict,
    spec: BreadthIndex,
    *,
    context: SessionContext,
    observed_at: datetime,
    listed_count: int | None = None,
) -> BreadthSnapshot:
    """t1511OutBlock 한 건을 Snapshot 으로. 판정 근거를 values 에 전부 남긴다."""
    if context.event_time is None:
        raise BreadthError(f"event_time 이 없는 국면이다: {context.phase}")
    event_time = context.event_time
    advancers = _as_int(out_block.get("highjo"))
    decliners = _as_int(out_block.get("lowjo"))
    unchanged = _as_int(out_block.get("unchgjo"))

    index_level = _as_decimal(out_block.get("pricejisu"))
    prev_level = _as_decimal(out_block.get("jniljisu"))
    change_pct = _as_decimal(out_block.get("diffjisu"))
    change_abs = _as_decimal(out_block.get("change"))
    sign = str(out_block.get("sign", "")).strip()

    value_raw = _as_int(out_block.get("value"))
    volume_raw = _as_int(out_block.get("volume"))
    total_value = None if value_raw is None else Decimal(value_raw) * VALUE_UNIT_MULTIPLIER

    # change 는 부호 없는 크기다. 방향은 sign 으로 붙인다.
    signed_change = None
    if change_abs is not None and sign in SIGN_TO_DIRECTION:
        signed_change = -change_abs if sign in _NEGATIVE_SIGNS else change_abs

    flags: list[str] = []

    # 필수 3개가 하나라도 없으면 Breadth 가 아니다.
    if advancers is None or decliners is None or unchanged is None:
        flags.append("BREADTH_COUNT_MISSING")
    total_count = sum(v for v in (advancers, decliners, unchanged) if v is not None)
    if total_count == 0:
        flags.append("BREADTH_ALL_ZERO")

    # 음수 종목수는 파싱이나 공급자 어느 쪽이 깨져도 나올 수 있다. 합계만 보면
    # 양수와 상쇄돼 0 이 아니어서 BREADTH_ALL_ZERO 를 빠져나간다.
    if any(v is not None and v < 0 for v in (advancers, decliners, unchanged)):
        flags.append("BREADTH_NEGATIVE")

    # 상승도 하락도 0 인데 전부 보합인 상태. 합계는 상장수와 같아서 위 검사를
    # 통과하지만 실제로는 장 시작 전 리셋 상태이거나 응답이 깨진 것이다.
    # 폐장 스냅샷에서 이 값이 나오면 그 날 아무도 안 움직였다는 뜻이라 사실상 없다.
    if advancers == 0 and decliners == 0:
        flags.append("BREADTH_NO_MOVEMENT")

    # 알 수 없는 sign 코드. 이게 있으면 signed_change 가 None 이 되어 아래 부호
    # 교차검증이 통째로 꺼진다 - 검사가 막으려던 실패 모드가 검사를 끄는 조건이므로
    # 반드시 흔적을 남긴다.
    if sign not in SIGN_TO_DIRECTION:
        flags.append("SIGN_CODE_UNKNOWN")

    # 부호 교차검증 - sign 과 등락률의 방향이 어긋나면 둘 중 하나를 잘못 읽은 것이다.
    if (
        signed_change is not None
        and change_pct is not None
        and (signed_change > 0) != (change_pct > 0)
        and change_pct != 0
        and signed_change != 0
    ):
        flags.append("SIGN_DIRECTION_MISMATCH")

    # 상장 보통주 수와 대조. 지수 구성이 상장 전체와 같지는 않아 넓게 잡는다.
    coverage = None
    if listed_count:
        coverage = round(total_count / listed_count, 4)
        if not (COVERAGE_WARN_LOW <= coverage <= COVERAGE_WARN_HIGH):
            flags.append("COVERAGE_OUT_OF_BAND")
    else:
        # **검사를 통과한 것과 검사를 못 한 것을 같은 값으로 기록하지 않는다.**
        # listed_count 가 None 이든 0 이든 등락종목수를 검증할 수단이 없는 상태다.
        flags.append("COVERAGE_UNVERIFIED")

    fatal = {"BREADTH_COUNT_MISSING", "BREADTH_ALL_ZERO", "BREADTH_NEGATIVE"}
    if fatal & set(flags):
        quality = "FAIL"
    elif flags:
        quality = "WARN"
    else:
        quality = "PASS"

    values = {
        "upcode": spec.upcode,
        "index_name": str(out_block.get("hname", "")).strip(),
        "index_level": None if index_level is None else str(index_level),
        "prev_index_level": None if prev_level is None else str(prev_level),
        "change": None if signed_change is None else str(signed_change),
        "change_pct": None if change_pct is None else str(change_pct),
        "sign_code": sign,
        "direction": SIGN_TO_DIRECTION.get(sign),
        "limit_up_count": _as_int(out_block.get("upjo")),
        "limit_down_count": _as_int(out_block.get("downjo")),
        "total_count": total_count,
        "listed_count": listed_count,
        "coverage_ratio": coverage,
        "total_volume_raw": volume_raw,
        "total_value_raw": value_raw,
        "total_value_unit_raw": "KRW_MILLION",
        "total_value_unit_note": VALUE_UNIT_NOTE,
        "open_index": _as_decimal_str(out_block.get("openjisu")),
        "high_index": _as_decimal_str(out_block.get("highjisu")),
        "low_index": _as_decimal_str(out_block.get("lowjisu")),
        "session_phase": str(context.phase),
        "session_basis_date": None if context.basis_date is None else context.basis_date.isoformat(),
        "session_detail": context.detail,
        # 응답에 서버 시각이 없다. event_time 이 어디서 왔는지 남긴다(4.2 PIT).
        "event_time_origin": _EVENT_TIME_ORIGIN[context.phase],
        "provider_timestamp": None,
        # observed_at - event_time. PRIOR_CLOSE 는 이 값이 크게 나오는 게 정상이다.
        "ingest_lag_seconds": int((observed_at - event_time).total_seconds()),
        "flags": flags,
        "tr_code": "t1511",
    }

    return BreadthSnapshot(
        market=spec.market,
        event_time=event_time,
        observed_at=observed_at,
        advancers=advancers,
        decliners=decliners,
        unchanged=unchanged,
        total_value=total_value,
        quality_status=quality,
        values=values,
    )


def _as_decimal_str(raw: object) -> str | None:
    d = _as_decimal(raw)
    return None if d is None else str(d)


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------

def fetch_index_breadth(client: LsRestClient, upcode: str) -> dict:
    d = client.call_tr(
        path=T1511_PATH,
        tr_cd="t1511",
        in_block={"t1511InBlock": {"upcode": upcode}},
        rate_limit_per_sec=T1511_RATE_LIMIT,
    )
    block = d.get("t1511OutBlock")
    if not isinstance(block, dict):
        raise LsApiError(
            f"t1511OutBlock 이 dict 가 아니다 upcode={upcode} rsp={d.get('rsp_cd')} keys={sorted(d)}"
        )
    return block


def fetch_industry_codes(client: LsRestClient, gubun1: str) -> list[tuple[str, str]]:
    """t8424 전체업종. 종합지수 코드가 바뀌지 않았는지 확인하는 용도다."""
    d = client.call_tr(
        path=T8424_PATH,
        tr_cd="t8424",
        in_block={"t8424InBlock": {"gubun1": gubun1}},
        rate_limit_per_sec=T8424_RATE_LIMIT,
    )
    rows = d.get("t8424OutBlock")
    if rows is None:
        raise LsApiError(f"t8424OutBlock 이 응답에 없다: keys={sorted(d)}")
    return [
        (str(r.get("upcode", "")).strip(), str(r.get("hname", "")).strip())
        for r in rows
    ]


def collect(
    client: LsRestClient,
    *,
    today_session: tuple[bool, datetime | None, datetime | None] | None,
    recent_sessions: list[tuple[date, datetime | None, datetime | None]],
    now: datetime,
    today: date,
    listed_counts: dict[str, int] | None = None,
    indices: tuple[BreadthIndex, ...] = INDEX_SPECS,
) -> tuple[list[BreadthSnapshot], SessionContext]:
    """세션을 판정하고 수집 가능하면 Snapshot 을 만든다.

    수집 불가 국면이면 BreadthError 다. 빈 리스트를 돌려주지 않는 이유 - 호출자가
    '오늘은 데이터가 없었다' 로 오해하고 그대로 넘어갈 수 있다.
    """
    ctx = resolve_session(
        today_session=today_session, recent_sessions=recent_sessions, now=now, today=today
    )
    if ctx.phase not in _COLLECTABLE:
        raise BreadthError(f"{ctx.phase} 상태에서는 수집하지 않는다 — {ctx.detail}")

    counts = listed_counts or {}
    out: list[BreadthSnapshot] = []
    for spec in indices:
        block = fetch_index_breadth(client, spec.upcode)
        out.append(
            parse_breadth(
                block, spec,
                context=ctx,
                observed_at=now,
                listed_count=counts.get(spec.market),
            )
        )
    return out, ctx


# ---------------------------------------------------------------------------
# 자체 점검 - 외부 호출 없음
# ---------------------------------------------------------------------------

_SAMPLE_KOSPI = {
    "hname": "종       합", "gubun": "1",
    "pricejisu": "5593.56", "jniljisu": "5663.24", "sign": "5",
    "change": "69.68", "diffjisu": "-1.23",
    "volume": 378859, "value": 38320902, "jnilvalue": 49217357,
    "openjisu": "5681.77", "highjisu": "5976.82", "lowjisu": "5547.41",
    "highjo": 605, "upjo": 4, "unchgjo": 58, "lowjo": 278, "downjo": 0,
}


def _dt(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _ctx(phase: SessionPhase, event_time: datetime | None, basis: date | None = None):
    return SessionContext(phase, event_time, basis, "self-check")


# 2026-07 의 실제 세션 모양: 09:00-15:30 KST = 00:00-06:30 UTC
def _session_row(d: date) -> tuple[date, datetime, datetime]:
    return (d, datetime.combine(d, time(0, 0, tzinfo=timezone.utc)),
            datetime.combine(d, time(6, 30, tzinfo=timezone.utc)))


_RECENT = [_session_row(date(2026, 7, d)) for d in (30, 29, 28, 27, 24)]


def _check_parsing():
    et = _dt(2026, 7, 30, 6, 30)
    snap = parse_breadth(
        _SAMPLE_KOSPI, INDEX_SPECS[0],
        context=_ctx(SessionPhase.POST_CLOSE, et, date(2026, 7, 30)),
        observed_at=_dt(2026, 7, 30, 8, 0), listed_count=950,
    )
    assert (snap.advancers, snap.decliners, snap.unchanged) == (605, 278, 58)
    assert snap.quality_status == "PASS", snap.values["flags"]
    # 백만원 -> 원
    assert snap.total_value == Decimal(38320902) * Decimal(1_000_000)
    assert snap.values["total_value_raw"] == 38320902
    # change 는 크기, sign=5 면 하락이므로 음수가 붙어야 한다
    assert snap.values["change"] == "-69.68", snap.values["change"]
    assert snap.values["direction"] == "DOWN"
    assert snap.values["total_count"] == 941
    # 없는 것을 0 으로 채우지 않는다
    row = snap.to_row()
    assert row["new_highs"] is None and row["new_lows"] is None
    assert row["up_volume"] is None and row["down_volume"] is None
    assert row["universe_version_id"] is None
    print("  t1511 파싱               OK")


def _check_mixed_types():
    # 종목수가 String 으로 와도 같은 결과여야 한다(공급자가 타입을 바꿔도 안 깨지게)
    as_str = {**_SAMPLE_KOSPI, "highjo": "605", "lowjo": "278", "unchgjo": "58",
              "value": "38320902", "volume": "378859"}
    c = _ctx(SessionPhase.POST_CLOSE, _dt(2026, 7, 30, 6, 30))
    a = parse_breadth(_SAMPLE_KOSPI, INDEX_SPECS[0], context=c,
                      observed_at=_dt(2026, 7, 30, 8, 0))
    b = parse_breadth(as_str, INDEX_SPECS[0], context=c,
                      observed_at=_dt(2026, 7, 30, 8, 0))
    assert (a.advancers, a.decliners, a.unchanged) == (b.advancers, b.decliners, b.unchanged)
    assert a.total_value == b.total_value
    assert _as_int("1,234") == 1234 and _as_int("") is None and _as_int(None) is None
    assert _as_int(True) is None, "bool 을 정수로 읽으면 안 된다"
    print("  타입 혼재 내성           OK")


def _check_quality_flags():
    c, ob = _ctx(SessionPhase.POST_CLOSE, _dt(2026, 7, 30, 6, 30)), _dt(2026, 7, 30, 8, 0)

    def q(patch, listed=950):
        return parse_breadth({**_SAMPLE_KOSPI, **patch}, INDEX_SPECS[0],
                             context=c, observed_at=ob, listed_count=listed)

    s = q({"highjo": None})
    assert s.quality_status == "FAIL" and "BREADTH_COUNT_MISSING" in s.values["flags"]

    s = q({"highjo": 0, "lowjo": 0, "unchgjo": 0})
    assert s.quality_status == "FAIL" and "BREADTH_ALL_ZERO" in s.values["flags"]

    # 음수는 합계가 0 이 아니라서 ALL_ZERO 를 빠져나간다. 따로 잡아야 한다.
    s = q({"highjo": -605})
    assert s.quality_status == "FAIL" and "BREADTH_NEGATIVE" in s.values["flags"]

    # 상승·하락 0, 전부 보합 - 합계는 상장수와 같아 예전엔 PASS 로 통과했다
    s = q({"highjo": 0, "lowjo": 0, "unchgjo": 941})
    assert "BREADTH_NO_MOVEMENT" in s.values["flags"], s.values["flags"]
    assert s.quality_status == "WARN"

    # sign 은 상승인데 등락률은 음수 - 둘 중 하나를 잘못 읽은 것이다
    s = q({"sign": "2"})
    assert s.quality_status == "WARN" and "SIGN_DIRECTION_MISMATCH" in s.values["flags"]

    # 모르는 sign 코드는 부호 교차검증을 통째로 끈다. 흔적을 반드시 남긴다.
    for bad_sign in ("", "9", "X"):
        s = q({"sign": bad_sign})
        assert "SIGN_CODE_UNKNOWN" in s.values["flags"], bad_sign
        assert s.quality_status == "WARN"
        assert s.values["change"] is None and s.values["direction"] is None

    # 상장 수의 10% 밖에 안 잡히면 WARN
    s = q({}, listed=9410)
    assert s.quality_status == "WARN" and "COVERAGE_OUT_OF_BAND" in s.values["flags"]

    # **검사를 못 한 것과 통과한 것을 같게 기록하지 않는다.** None 과 0 둘 다.
    for listed in (None, 0):
        s = q({}, listed=listed)
        assert "COVERAGE_UNVERIFIED" in s.values["flags"], listed
        assert s.quality_status == "WARN" and s.values["coverage_ratio"] is None

    # 정상은 여전히 PASS 여야 한다(플래그를 늘리다 정상까지 WARN 으로 만들면 안 된다)
    assert q({}).quality_status == "PASS"
    print("  DQ Flag                  OK")


def _check_numeric_edges():
    """NaN/Infinity/거대수가 계약을 깨지 않는지. 계약은 '못 읽으면 None' 이다."""
    for bad in (float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", "-Infinity",
                "nan", "1e999", True, False, [], {}):
        assert _as_int(bad) is None, f"_as_int({bad!r}) -> {_as_int(bad)!r}"
        assert _as_decimal(bad) is None, f"_as_decimal({bad!r}) -> {_as_decimal(bad)!r}"

    # 정상값은 그대로 통과
    assert _as_int("1,234") == 1234 and _as_int(605) == 605 and _as_int(605.9) == 605
    assert _as_decimal("5593.56") == Decimal("5593.56")
    assert _as_decimal("-1.23") == Decimal("-1.23")
    assert _as_int("") is None and _as_int(None) is None

    # NaN 이 섞여 들어와도 예외 없이 FAIL 로 떨어져야 한다
    s = parse_breadth({**_SAMPLE_KOSPI, "highjo": float("nan")}, INDEX_SPECS[0],
                      context=_ctx(SessionPhase.POST_CLOSE, _dt(2026, 7, 30, 6, 30)),
                      observed_at=_dt(2026, 7, 30, 8, 0), listed_count=950)
    assert s.quality_status == "FAIL" and "BREADTH_COUNT_MISSING" in s.values["flags"]
    print("  수치 경계값              OK")


def _check_fail_not_written():
    """FAIL 행은 Repository 가 거부해야 한다 - do nothing 이라 한 번 들어가면 끝이다."""
    from market_repository import TimescaleMarketRepository

    s = parse_breadth({**_SAMPLE_KOSPI, "highjo": 0, "lowjo": 0, "unchgjo": 0},
                      INDEX_SPECS[0],
                      context=_ctx(SessionPhase.POST_CLOSE, _dt(2026, 7, 30, 6, 30)),
                      observed_at=_dt(2026, 7, 30, 8, 0))
    assert s.quality_status == "FAIL"

    # 연결 없이 검증부만 태운다
    repo = TimescaleMarketRepository.__new__(TimescaleMarketRepository)
    try:
        TimescaleMarketRepository.write_market_breadth(repo, [s.to_row()])
        raise AssertionError("FAIL 행이 Repository 를 통과했다")
    except ValueError as e:
        assert "FAIL" in str(e)
    print("  FAIL 적재 차단           OK")


def _check_session_resolution():
    d = date(2026, 7, 30)
    trading = (True, _dt(2026, 7, 30, 0, 0), _dt(2026, 7, 30, 6, 30))

    def r(today_session, now, today=d, recent=None):
        return resolve_session(today_session=today_session,
                               recent_sessions=_RECENT if recent is None else recent,
                               now=now, today=today)

    # 오늘이 Calendar 에 있는 경우
    assert r(trading, _dt(2026, 7, 30, 0, 0)).phase is SessionPhase.REGULAR
    assert r(trading, _dt(2026, 7, 30, 6, 30)).phase is SessionPhase.REGULAR
    post = r(trading, _dt(2026, 7, 30, 6, 31))
    assert post.phase is SessionPhase.POST_CLOSE and post.event_time == _dt(2026, 7, 30, 6, 30)
    # 폐장 후 몇 번을 조회해도 event_time 이 같아야 멱등이 성립한다
    assert r(trading, _dt(2026, 7, 30, 20, 0)).event_time == post.event_time
    # 장중은 관측 시각(초 절삭)
    now = datetime(2026, 7, 30, 3, 0, 12, 345678, tzinfo=timezone.utc)
    assert r(trading, now).event_time == now.replace(microsecond=0)

    # 오늘이 거래일인데 개장 전 -> 직전 세션(07-29) 종가
    pre = r(trading, _dt(2026, 7, 29, 23, 59))
    assert pre.phase is SessionPhase.PRIOR_CLOSE and pre.basis_date == date(2026, 7, 29)
    assert pre.event_time == _dt(2026, 7, 29, 6, 30)

    # 오늘이 휴장이면 수집하지 않는다
    assert r((False, None, None), _dt(2026, 7, 30, 3, 0)).phase is SessionPhase.NON_TRADING
    # 거래일인데 시각이 비어 있으면 추정하지 않는다
    assert r((True, None, None), _dt(2026, 7, 30, 3, 0)).phase is SessionPhase.CALENDAR_MISSING

    # Calendar 에 오늘(07-31)이 없는 경우 - 실제로 마주친 상황이다
    t31 = date(2026, 7, 31)
    ok = r(None, _dt(2026, 7, 30, 15, 42), today=t31)   # KST 07-31 00:42, 개장 전
    assert ok.phase is SessionPhase.PRIOR_CLOSE and ok.basis_date == date(2026, 7, 30)
    assert ok.event_time == _dt(2026, 7, 30, 6, 30)
    # 개장 예정(00:00 UTC)을 지났으면 오늘 장중인지 휴장인지 구분 못 하므로 거부
    assert r(None, _dt(2026, 7, 31, 0, 0), today=t31).phase is SessionPhase.CALENDAR_MISSING
    assert r(None, _dt(2026, 7, 31, 3, 0), today=t31).phase is SessionPhase.CALENDAR_MISSING

    # 직전 세션이 10일보다 멀면 Calendar 갱신이 밀린 것으로 본다
    stale = r(None, _dt(2026, 7, 30, 15, 42), today=t31,
              recent=[_session_row(date(2026, 7, 10))])
    assert stale.phase is SessionPhase.CALENDAR_STALE
    # Calendar 가 아예 비었으면 개장 시각을 유도할 수 없다
    assert r(None, _dt(2026, 7, 30, 15, 42), today=t31, recent=[]).phase \
        is SessionPhase.CALENDAR_MISSING

    # 개장 시각이 일정해야만 '오늘 개장 예정' 을 유도한다. KST 기준으로 돌려준다.
    assert derive_open_time_of_day(_RECENT) == time(9, 0, tzinfo=KST)
    mixed = _RECENT[:2] + [(date(2026, 7, 28), _dt(2026, 7, 28, 1, 0), _dt(2026, 7, 28, 6, 30))]
    assert derive_open_time_of_day(mixed) is None

    # ▶ 회귀: 개장이 09:00 KST 보다 이르면 UTC 정규화가 날짜를 하루 밀어
    #   "개장 지났으면 거부" 가드를 하루 내내 무력화했다(적대적 검토 2026-07-31).
    early = [
        (date(2026, 7, d),
         datetime.combine(date(2026, 7, d), time(8, 30, tzinfo=KST)),
         datetime.combine(date(2026, 7, d), time(15, 30, tzinfo=KST)))
        for d in (30, 29, 28, 27, 24)
    ]
    assert derive_open_time_of_day(early) == time(8, 30, tzinfo=KST)
    t31 = date(2026, 7, 31)
    # KST 07-31 11:00 = 장중. Calendar 에 오늘이 없으므로 거부돼야 한다.
    midday = datetime.combine(t31, time(11, 0, tzinfo=KST)).astimezone(timezone.utc)
    assert r(None, midday, today=t31, recent=early).phase is SessionPhase.CALENDAR_MISSING, (
        "이른 개장에서 장중 수집이 PRIOR_CLOSE 로 새어나갔다"
    )
    # 같은 Calendar 로 개장 전(07-31 07:00 KST)은 여전히 직전 종가로 떨어진다
    before_open = datetime.combine(t31, time(7, 0, tzinfo=KST)).astimezone(timezone.utc)
    assert r(None, before_open, today=t31, recent=early).phase is SessionPhase.PRIOR_CLOSE
    print("  세션 판정                OK")


def _check_collect_refuses():
    """수집 불가 국면은 빈 결과가 아니라 예외다."""
    class _NoCall:
        def call_tr(self, **kw):
            raise AssertionError("수집 불가 국면인데 API 를 호출했다")

    cases = [
        # 오늘이 휴장
        ((False, None, None), _dt(2026, 7, 30, 3, 0), date(2026, 7, 30), _RECENT),
        # Calendar 에 오늘이 없는데 개장 시각을 지남
        (None, _dt(2026, 7, 31, 3, 0), date(2026, 7, 31), _RECENT),
        # 직전 세션이 너무 오래됨
        (None, _dt(2026, 7, 30, 15, 42), date(2026, 7, 31), [_session_row(date(2026, 7, 10))]),
        # Calendar 가 비었음
        (None, _dt(2026, 7, 30, 15, 42), date(2026, 7, 31), []),
    ]
    for today_session, now, today, recent in cases:
        try:
            collect(_NoCall(), today_session=today_session, recent_sessions=recent,
                    now=now, today=today)
            raise AssertionError(f"{today_session}/{now} 가 통과했다")
        except BreadthError:
            pass
    print("  수집 거부(fail-closed)   OK")


def _check_row_contract():
    """Repository Column 계약과 to_row() 가 어긋나면 여기서 잡힌다."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))
    from market_repository import TimescaleMarketRepository

    snap = parse_breadth(_SAMPLE_KOSPI, INDEX_SPECS[0],
                         context=_ctx(SessionPhase.POST_CLOSE, _dt(2026, 7, 30, 6, 30)),
                         observed_at=_dt(2026, 7, 30, 8, 0))
    assert set(snap.to_row()) == set(TimescaleMarketRepository._BREADTH_COLUMNS)
    assert snap.to_row()["quality_status"] in ("PASS", "WARN", "FAIL", "PARTIAL")
    print("  Repository Column 계약   OK")


def _collect_and_report() -> int:
    """실제 수집. Calendar/상장수는 Supabase, 적재는 TimescaleDB 다."""
    from market_repository import TimescaleMarketRepository
    from reference_repository import SupabaseReferenceRepository
    from source_registry import load_project_env

    env = load_project_env()
    now = datetime.now(timezone.utc)
    trade_date = now.astimezone(KST).date()

    ref = SupabaseReferenceRepository()
    try:
        today_session = ref.market_session(trade_date, market=CALENDAR_MARKET)
        recent = ref.recent_trading_sessions(market=CALENDAR_MARKET, limit=20)
        listed = ref.listed_counts(market=CALENDAR_MARKET)
    finally:
        ref.close()

    print(f"  기준일(KST): {trade_date}  현재(UTC): {now:%Y-%m-%d %H:%M:%S}")
    print(f"  Calendar 오늘: {today_session}")
    print(f"  Calendar 최근 거래일: {[str(s[0]) for s in recent[:5]]}")
    print(f"  상장 보통주: {listed}")

    client = LsRestClient()

    # 종합지수 업종코드가 그대로인지 t8424 로 확인한다. 코드가 바뀌면 조용히 다른
    # 업종의 Breadth 를 적재하게 된다.
    for gubun1, spec in (("1", INDEX_SPECS[0]), ("2", INDEX_SPECS[1])):
        codes = dict(fetch_industry_codes(client, gubun1))
        actual = codes.get(spec.upcode)
        norm = "".join((actual or "").split())
        if norm != "".join(spec.label.split()):
            raise BreadthError(
                f"업종코드 {spec.upcode} 의 이름이 바뀌었다: 기대 {spec.label!r} 실제 {actual!r}"
            )
    print(f"  업종코드 확인: {', '.join(f'{s.upcode}={s.market}' for s in INDEX_SPECS)} OK")

    try:
        snaps, ctx = collect(
            client, today_session=today_session, recent_sessions=recent,
            now=now, today=trade_date, listed_counts=listed,
        )
    except BreadthError as e:
        print(f"\n  ⚠ 수집하지 않았다 — {e}")
        return 2

    phase = ctx.phase
    print(
        f"\n  국면: {phase} ({ctx.detail})  기준 세션: {ctx.basis_date}"
        f"  event_time: {snaps[0].event_time:%Y-%m-%d %H:%M:%S%z}"
    )
    for s in snaps:
        v = s.values
        print(
            f"    {s.market:7} {v['index_name']:10} {v['index_level']:>10} "
            f"({v['change_pct']:>6}%)  상승 {s.advancers:>4} 하락 {s.decliners:>4} "
            f"보합 {s.unchanged:>4}  cov={v['coverage_ratio']}  {s.quality_status}"
        )
        if v["flags"]:
            print(f"      flags: {v['flags']}")

    # ▶ FAIL 은 적재하지 않는다 (2026-07-31)
    #   PK 가 (event_time, market, source) 이고 on conflict do nothing 이라 **같은
    #   세션에 대해 먼저 쓰인 행이 영구히 이긴다.** 불량 행을 한 번 넣으면 이후 정상
    #   수집이 duplicates 로 집계되어 조용히 버려지고, 그 세션은 영영 복구되지 않는다.
    #   그래서 불량은 저장이 아니라 차단이다(개발 원칙 9).
    bad = [s for s in snaps if s.quality_status == "FAIL"]
    if bad:
        raise BreadthError(
            "quality_status=FAIL 스냅샷이 있어 적재하지 않는다 - 한 번 들어가면 같은 "
            "세션의 정상 데이터가 영구히 막힌다: "
            + "; ".join(f"{s.market} {s.values['flags']}" for s in bad)
        )

    dsn = env.get("TIMESCALE_DATABASE_URL")
    if not dsn:
        print("\n  TIMESCALE_DATABASE_URL 이 없어 적재는 건너뛴다")
        return 0

    repo = TimescaleMarketRepository(dsn)
    try:
        res = repo.write_market_breadth([s.to_row() for s in snaps])
        print(f"\n  적재: 시도 {res.attempted} 삽입 {res.inserted} 중복 {res.duplicates}")
        # 같은 폐장 Snapshot 재수집이 새 행을 만들지 않는지 확인한다
        if phase in (SessionPhase.POST_CLOSE, SessionPhase.PRIOR_CLOSE):
            again = repo.write_market_breadth([s.to_row() for s in snaps])
            print(f"  멱등 재시도: 삽입 {again.inserted} 중복 {again.duplicates}")
            if again.inserted:
                raise BreadthError("폐장 Snapshot 재수집이 새 행을 만들었다 - event_time 규칙 확인")
        print(f"  market_breadth 총 {repo.count_market_breadth()}행")
    finally:
        repo.close()
    return 0


if __name__ == "__main__":
    # 한국어 Windows 기본 Console 이 cp949 라 업종명 출력에서 깨진다.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--collect" in sys.argv:
        print(f"{COLLECTOR_VERSION} 실제 수집")
        raise SystemExit(_collect_and_report())

    print(f"{COLLECTOR_VERSION} 자체 점검 (외부 호출 없음)")
    _check_parsing()
    _check_mixed_types()
    _check_quality_flags()
    _check_numeric_edges()
    _check_session_resolution()
    _check_collect_refuses()
    _check_row_contract()
    _check_fail_not_written()
    print("Breadth 8개 영역 통과. 실제 수집은 --collect")
