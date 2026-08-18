#!/usr/bin/env python3
"""VKOSPI(코스피200 변동성지수) 수집 - 시장 공포의 직접 관측치.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-02 "vkospi 같은 것도 넣으면 좋을듯".
      실측(2026-08-02): LS t1511 업종코드 **205 = VKOSPI** 로 조회된다.
      pricejisu=84.35, 52주 범위 18.03~97.99 - 스케일 정상 확인.
      방법론 등재: evidence/methods.py `vkospi_fear_gauge`
      (Whaley 2000, The Investor Fear Gauge)

▶ 왜 필요한가
  레짐 분석(RES-07)은 지금 등락 단면·SMA 상회로만 국면을 본다 - 실현된
  가격의 뒷모습이다. VKOSPI 는 옵션 시장이 **앞으로 30일**을 어떻게 보는지의
  관측치라 축이 다르다. 같은 하락이어도 VKOSPI 가 튀는 하락과 조용한 하락은
  다른 국면이다.

▶ 저장
  research.macro_observations 의 일별 계열 'VKOSPI'(source=ls_openapi_rest).
  GPR·GDELT 와 같은 자리라 /macro/observations 한 번으로 같이 읽힌다.
  - period=거래일, published_at=장 마감(15:45 KST) - 종가가 확정된 시각.
    지수는 개정되지 않으므로 vintage=period 로 고정한다(재수집 멱등).
  - **휴장일에는 적재하지 않는다.** t1511 은 휴장이어도 직전 세션 종가를
    그대로 주므로, 그것을 오늘 값으로 넣으면 없는 관측이 생긴다.
    세션 판정은 reference 캘린더로 하고 아니면 exit 2(SKIP).

사용
  python collectors/volatility_index_collector.py            # 자체 점검
  python collectors/volatility_index_collector.py --collect  # 당일 종가 적재
"""
from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_observations import FREQ_DAILY, Observation, SeriesSpec

COLLECTOR_VERSION = "research-volatility-index-v1"
KST = timezone(timedelta(hours=9))
EXIT_SKIP = 2                      # 수집기 규약: 의도된 미수집

VKOSPI_UPCODE = "205"              # 실측 2026-08-02 (t8424 전체업종 252개 중)
VKOSPI_CODE = "VKOSPI"
SOURCE_ID = "ls_openapi_rest"
CLOSE_TIME = time(15, 45)          # 정규장 마감 - 종가 확정 시각
CALENDAR_MARKET = "KRX"

SERIES = (
    SeriesSpec(SOURCE_ID, VKOSPI_CODE, "코스피200 변동성지수 (VKOSPI)",
               FREQ_DAILY, unit="index", country_code="KR"),
)


class VolatilityIndexError(RuntimeError):
    """수집 실패. 빈 결과로 바꾸지 않는다."""


# ---------------------------------------------------------------------------
# 순수 함수
# ---------------------------------------------------------------------------

def parse_block(block: dict) -> dict:
    """t1511OutBlock -> 정규화. 값 타입이 섞여 오므로 여기서 통일한다.

    LS 는 지수를 문자열("84.35")로 준다 - float 로 곧장 쓰면 조용한 오차가
    생길 수 있어 Decimal 로 받는다. 필수 필드가 없으면 예외(추정 금지).
    """
    def dec(key: str) -> Decimal | None:
        raw = block.get(key)
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return Decimal(str(raw).strip())
        except InvalidOperation:
            return None

    close = dec("pricejisu")
    if close is None:
        raise VolatilityIndexError(f"pricejisu 없음 - 응답 키: {sorted(block)[:12]}")
    return {
        "close": close,
        "prev_close": dec("jniljisu"),
        "open": dec("openjisu"),
        "high": dec("highjisu"),
        "low": dec("lowjisu"),
        "week52_high": dec("whjisu"),
        "week52_low": dec("wljisu"),
        "name": str(block.get("hname", "")).strip(),
    }


def position_in_52w(close: Decimal, low: Decimal | None,
                    high: Decimal | None) -> float | None:
    """52주 범위 안 위치(%). 범위가 없거나 폭이 0이면 None - 지어내지 않는다.

    VKOSPI 는 절대 수준(20 이냐 80 이냐)보다 '제 범위에서 어디냐'가 국면
    해석에 쓰인다. 레짐 분석가가 백분위를 계산하기 전 1차 맥락이다.
    """
    if low is None or high is None or high <= low:
        return None
    pos = (close - low) / (high - low) * 100
    return round(float(pos), 1)


def build_observation(parsed: dict, *, trade_date: date,
                      observed_at: datetime) -> Observation:
    published = datetime.combine(trade_date, CLOSE_TIME, tzinfo=KST)
    meta = {"source": "ls:t1511", "upcode": VKOSPI_UPCODE,
            "name": parsed.get("name"),
            "position_in_52w_pct": position_in_52w(
                parsed["close"], parsed.get("week52_low"),
                parsed.get("week52_high"))}
    for k in ("open", "high", "low", "prev_close", "week52_high", "week52_low"):
        v = parsed.get(k)
        if v is not None:
            meta[k] = float(v)
    return Observation(
        external_series_code=VKOSPI_CODE,
        period=trade_date,
        value=parsed["close"],
        published_at=published,
        observed_at=observed_at,
        vintage_date=trade_date,     # 지수는 개정되지 않는다 - 재수집 멱등
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------

def collect() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repository"))
    from ls_client import LsRestClient
    from market_breadth_collector import fetch_index_breadth
    from reference_repository import SupabaseReferenceRepository
    from source_registry import SourceRegistry, UseScope, load_project_env

    env = load_project_env()
    registry = SourceRegistry(env=env)
    registry.check_use(SOURCE_ID, UseScope.FULLTEXT_STORE)
    market_source = registry.require(SOURCE_ID)

    now = datetime.now(timezone.utc)
    trade_date = now.astimezone(KST).date()

    repo = SupabaseReferenceRepository()
    try:
        # market_session 계약: (거래일여부, 개장, 폐장) 또는 None.
        # **None 은 '휴장'이 아니라 '캘린더 미기재'다** - 둘을 섞으면 수집기가
        # 조용히 아무 것도 안 하게 된다(reference_repository 주석·개발원칙 9).
        session = repo.market_session(trade_date, market=CALENDAR_MARKET)
        if session is None:
            raise VolatilityIndexError(
                f"{trade_date} 가 거래 Calendar 에 없다 - 휴장인지 미기재인지 "
                f"구분되지 않아 적재를 멈춘다(calendar_collector 확인 필요)")
        if not session[0]:
            print(f"{COLLECTOR_VERSION}: {trade_date} 는 휴장 - 적재하지 않는다 "
                  f"(t1511 은 직전 종가를 주므로 넣으면 없는 관측이 생긴다)",
                  flush=True)
            return EXIT_SKIP

        block = fetch_index_breadth(LsRestClient(), VKOSPI_UPCODE)
        parsed = parse_block(block)
        obs = build_observation(parsed, trade_date=trade_date, observed_at=now)

        # Market-only runtime must not refresh the retired news/filing/macro
        # source catalogue as a side effect of writing one exchange index.
        _, _, src = repo.sync_data_sources(specs=[market_source])
        _, _, sid = repo.upsert_macro_series(list(SERIES), source_ids=src)
        n, u = repo.upsert_macro_observations([obs], series_ids=sid)
        pos = obs.metadata.get("position_in_52w_pct")
        print(f"{COLLECTOR_VERSION}: {trade_date} VKOSPI {parsed['close']} "
              f"(52주 위치 {pos}%) - 신규 {n} / 갱신 {u}", flush=True)
        return 0
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

_BLOCK = {"hname": "VKOSPI", "pricejisu": "84.35", "jniljisu": "86.18",
          "openjisu": "83.43", "highjisu": "86.59", "lowjisu": "82.68",
          "whjisu": "97.99", "wljisu": "18.03"}


def _check_parse():
    p = parse_block(_BLOCK)
    assert p["close"] == Decimal("84.35") and p["prev_close"] == Decimal("86.18")
    assert p["week52_low"] == Decimal("18.03")
    # 빈 문자열·결측은 None (0 으로 채우지 않는다)
    p2 = parse_block(dict(_BLOCK, jniljisu="", whjisu=None))
    assert p2["prev_close"] is None and p2["week52_high"] is None
    try:
        parse_block({"hname": "VKOSPI"})
        raise AssertionError("종가 없는 응답이 통과했다")
    except VolatilityIndexError:
        pass
    print("  t1511 파싱·결측 방어     OK")


def _check_position():
    # 실측값: (84.35-18.03)/(97.99-18.03) = 82.9%
    assert position_in_52w(Decimal("84.35"), Decimal("18.03"),
                           Decimal("97.99")) == 82.9
    assert position_in_52w(Decimal(50), None, Decimal(90)) is None
    assert position_in_52w(Decimal(50), Decimal(90), Decimal(90)) is None, \
        "폭 0 인 범위에서 위치를 만들면 안 된다"
    print("  52주 위치 계산           OK")


def _check_observation_pit():
    obs = build_observation(parse_block(_BLOCK), trade_date=date(2026, 8, 3),
                            observed_at=datetime(2026, 8, 3, 9, 0,
                                                 tzinfo=timezone.utc))
    assert obs.period == date(2026, 8, 3)
    assert obs.published_at.astimezone(KST).hour == 15, obs.published_at
    assert obs.vintage_date == obs.period, "vintage 가 흔들리면 재수집이 중복된다"
    assert obs.metadata["position_in_52w_pct"] == 82.9
    assert obs.metadata["week52_low"] == 18.03
    # 같은 날 두 번 만들어도 vintage 가 같아야 멱등이다
    obs2 = build_observation(parse_block(_BLOCK), trade_date=date(2026, 8, 3),
                             observed_at=datetime(2026, 8, 4, 1, 0,
                                                  tzinfo=timezone.utc))
    assert obs2.vintage_date == obs.vintage_date
    print("  PIT 3시각·멱등 vintage   OK")


def _check_registry():
    from source_registry import SourceRegistry, UseScope

    SourceRegistry(env={}).check_use(SOURCE_ID, UseScope.FULLTEXT_STORE)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evidence"))
    from methods import METHODS

    m = {x.key: x for x in METHODS}
    assert "vkospi_fear_gauge" in m, "방법론 레지스트리에 등재되지 않았다"
    assert m["vkospi_fear_gauge"].status == "ADOPTED"
    print("  Source·방법론 등재       OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--collect" in sys.argv:
        raise SystemExit(collect())

    print(f"{COLLECTOR_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_parse()
    _check_position()
    _check_observation_pit()
    _check_registry()
    print("VKOSPI 수집 4개 영역 통과. 수집은 --collect")
