#!/usr/bin/env python3
"""스타일·안전자산 지수 수집 - 시장이 '무엇으로 도망가는지'의 관측치.

소유: 재일 (리서치본부)
근거: capability_audit 이 "접근 가능한데 미사용"으로 집어낸 LS 업종 252개 중
      기존 축과 겹치지 않는 6개. 우선순위 3(재일님 지시 2026-08-02
      "이어서 진행 시작").
      방법론 등재: evidence/methods.py `flight_to_quality`,
      `style_rotation_ratio`, `low_volatility_preference`

▶ 왜 필요한가 - 이미 있는 축과 무엇이 다른가
  등락 단면·SMA 상회는 "얼마나 올랐나"를 본다. VKOSPI 는 "얼마나 무서운가"를
  본다. 여기 지수들은 **"그래서 어디로 갔나"** 를 본다. 같은 -1% 하락이어도
  돈이 국채로 갔는지, 고배당으로 갔는지, 아무 데도 안 갔는지는 다른 국면이다.

▶ 핵심은 수준이 아니라 비율
  고배당지수가 올랐다는 사실만으로는 아무 말도 못 한다 - 시장 전체가 올랐을
  수 있다. 의미는 **KOSPI200 대비**에서 나온다. 그래서 분모가 되는 K200 을
  같은 시각에 함께 받고, 비율 계산은 evidence/style_ratios.py 가 한다.
  이 수집기는 원자료만 넣는다(수집과 해석의 분리).

▶ 부분 성공을 성공으로 치지 않는다
  6개 중 5개만 받으면 그날 비율은 계산되지 않거나, 더 나쁘게는 **다른 날
  분모와 섞인다.** 하나라도 실패하면 전체를 실패시킨다.

▶ 휴장일 적재 금지
  t1511 은 휴장에도 직전 세션 종가를 그대로 준다. 그것을 오늘 값으로 넣으면
  없는 관측이 생기고, 비율의 분모·분자가 서로 다른 날이 될 수 있다.

사용
  python collectors/style_index_collector.py            # 자체 점검
  python collectors/style_index_collector.py --collect  # 당일 종가 적재
"""
from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))

from market_observations import FREQ_DAILY, Observation, SeriesSpec
from volatility_index_collector import (
    parse_block,
    position_in_52w,
)

COLLECTOR_VERSION = "research-style-index-v1"
KST = timezone(timedelta(hours=9))
EXIT_SKIP = 2                      # 수집기 규약: 의도된 미수집

SOURCE_ID = "ls_openapi_rest"
CLOSE_TIME = time(15, 45)          # 정규장 마감 - 종가 확정 시각
CALENDAR_MARKET = "KRX"
CONFIG = _BASE.parent / "config" / "style_indices.txt"
BENCHMARK_CODE = "KOSPI200"        # 비율의 분모. 빠지면 나머지가 무의미하다


class StyleIndexError(RuntimeError):
    """수집 실패. 빈 결과로 바꾸지 않는다."""


# ---------------------------------------------------------------------------
# 순수 함수 - 설정 파싱
# ---------------------------------------------------------------------------

def parse_config(body: str) -> list[tuple[str, str, str]]:
    """설정 본문 -> [(업종코드, 계열코드, 설명)].

    형식이 깨진 줄을 조용히 건너뛰지 않는다 - 오타 한 글자로 지수가 하나
    빠지면 그날 비율이 통째로 사라지는데 로그에는 아무 것도 안 남는다.
    """
    rows: list[tuple[str, str, str]] = []
    seen_up: set[str] = set()
    seen_series: set[str] = set()
    for lineno, raw in enumerate(body.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|", 2)]
        if len(parts) != 3 or not all(parts[:2]):
            raise StyleIndexError(
                f"{CONFIG.name}:{lineno} 형식 오류 - '<업종코드>|<계열코드>|<설명>' "
                f"이어야 한다: {raw!r}")
        upcode, series_code, desc = parts
        if not upcode.isdigit():
            raise StyleIndexError(
                f"{CONFIG.name}:{lineno} 업종코드가 숫자가 아니다: {upcode!r}")
        if upcode in seen_up:
            raise StyleIndexError(f"{CONFIG.name}:{lineno} 업종코드 중복: {upcode}")
        if series_code in seen_series:
            raise StyleIndexError(
                f"{CONFIG.name}:{lineno} 계열코드 중복: {series_code}")
        seen_up.add(upcode)
        seen_series.add(series_code)
        rows.append((upcode, series_code, desc))
    if not rows:
        raise StyleIndexError(f"{CONFIG.name} 에 지수가 하나도 없다")
    if BENCHMARK_CODE not in seen_series:
        raise StyleIndexError(
            f"기준 지수 {BENCHMARK_CODE} 가 없다 - 비율의 분모가 없으면 나머지 "
            f"지수는 '올랐다/내렸다' 밖에 말하지 못한다")
    return rows


def load_specs(rows: list[tuple[str, str, str]]) -> tuple[SeriesSpec, ...]:
    return tuple(
        SeriesSpec(SOURCE_ID, series_code, desc, FREQ_DAILY,
                   unit="index", country_code="KR")
        for _up, series_code, desc in rows
    )


def build_observation(parsed: dict, *, series_code: str, upcode: str,
                      trade_date: date, observed_at: datetime) -> Observation:
    """VKOSPI 와 같은 PIT 규약 - period=거래일, published=마감, vintage=period."""
    meta = {"source": "ls:t1511", "upcode": upcode,
            "name": parsed.get("name"),
            "position_in_52w_pct": position_in_52w(
                parsed["close"], parsed.get("week52_low"),
                parsed.get("week52_high"))}
    for k in ("open", "high", "low", "prev_close", "week52_high", "week52_low"):
        v = parsed.get(k)
        if v is not None:
            meta[k] = float(v)
    return Observation(
        external_series_code=series_code,
        period=trade_date,
        value=parsed["close"],
        published_at=datetime.combine(trade_date, CLOSE_TIME, tzinfo=KST),
        observed_at=observed_at,
        vintage_date=trade_date,     # 지수는 개정되지 않는다 - 재수집 멱등
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------

def collect() -> int:
    sys.path.insert(0, str(_BASE.parent / "repository"))
    from ls_client import LsRestClient
    from market_breadth_collector import fetch_index_breadth
    from reference_repository import SupabaseReferenceRepository
    from source_registry import SourceRegistry, UseScope, load_project_env

    env = load_project_env()
    registry = SourceRegistry(env=env)
    registry.check_use(SOURCE_ID, UseScope.FULLTEXT_STORE)
    market_source = registry.require(SOURCE_ID)

    rows = parse_config(CONFIG.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    trade_date = now.astimezone(KST).date()

    repo = SupabaseReferenceRepository()
    try:
        # None 은 '휴장'이 아니라 '캘린더 미기재'다 - 섞으면 조용히 멈춘다.
        session = repo.market_session(trade_date, market=CALENDAR_MARKET)
        if session is None:
            raise StyleIndexError(
                f"{trade_date} 가 거래 Calendar 에 없다 - 휴장인지 미기재인지 "
                f"구분되지 않아 적재를 멈춘다(calendar_collector 확인 필요)")
        if not session[0]:
            print(f"{COLLECTOR_VERSION}: {trade_date} 는 휴장 - 적재하지 않는다",
                  flush=True)
            return EXIT_SKIP

        client = LsRestClient()
        observations: list[Observation] = []
        for upcode, series_code, _desc in rows:
            try:
                parsed = parse_block(fetch_index_breadth(client, upcode))
            except Exception as exc:
                # 부분 성공 금지: 하나가 빠지면 그날 비율이 사라지거나
                # 분모·분자가 다른 날로 어긋난다.
                raise StyleIndexError(
                    f"{series_code}(업종 {upcode}) 수집 실패 - 전체를 실패로 "
                    f"처리한다: {type(exc).__name__} {exc}") from exc
            observations.append(build_observation(
                parsed, series_code=series_code, upcode=upcode,
                trade_date=trade_date, observed_at=now))

        _, _, src = repo.sync_data_sources(specs=[market_source])
        _, _, sid = repo.upsert_macro_series(list(load_specs(rows)),
                                             source_ids=src)
        n, u = repo.upsert_macro_observations(observations, series_ids=sid)
        levels = ", ".join(f"{o.external_series_code}={o.value}"
                           for o in observations)
        print(f"{COLLECTOR_VERSION}: {trade_date} {len(observations)}계열 "
              f"신규 {n} / 갱신 {u} - {levels}", flush=True)
        return 0
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

_BLOCK = {"hname": "국채선물지수", "pricejisu": "1209.33", "jniljisu": "1206.87",
          "openjisu": "1207.10", "highjisu": "1210.02", "lowjisu": "1206.55",
          "whjisu": "1244.42", "wljisu": "1196.60"}


def _check_config_parse():
    rows = parse_config(CONFIG.read_text(encoding="utf-8"))
    codes = {c for _u, c, _d in rows}
    assert BENCHMARK_CODE in codes, "분모가 실제 설정에 없다"
    assert {"BOND_FUT", "DIV_50", "MIN_VOL"} <= codes, codes
    assert len(rows) >= 2
    # 형식 오류는 조용히 건너뛰지 않고 예외
    for bad, why in ((f"606|BOND_FUT\n{BENCHMARK_CODE}", "구분자 부족"),
                     (f"abc|X|d\n101|{BENCHMARK_CODE}|k", "숫자 아닌 업종코드"),
                     (f"606|A|x\n606|B|y\n101|{BENCHMARK_CODE}|k", "업종 중복"),
                     (f"606|A|x\n607|A|y\n101|{BENCHMARK_CODE}|k", "계열 중복")):
        try:
            parse_config(bad)
            raise AssertionError(f"{why} 인 설정이 통과했다")
        except StyleIndexError:
            pass
    # 분모가 빠지면 거부
    try:
        parse_config("606|BOND_FUT|국채")
        raise AssertionError("분모 없는 설정이 통과했다")
    except StyleIndexError as exc:
        assert BENCHMARK_CODE in str(exc)
    print("  설정 파싱·형식 방어      OK")


def _check_specs():
    rows = parse_config(CONFIG.read_text(encoding="utf-8"))
    specs = load_specs(rows)
    assert len(specs) == len(rows)
    assert all(s.source_code == SOURCE_ID and s.frequency == FREQ_DAILY
               for s in specs)
    # 계열코드가 VKOSPI 와 겹치면 서로 덮어쓴다
    codes = {s.external_series_code for s in specs}
    assert "VKOSPI" not in codes, "VKOSPI 는 별도 수집기 소관이다"
    # 분석가가 조회하는 코드 목록과 어긋나면 수집해도 아무도 못 읽는다
    sys.path.insert(0, str(_BASE.parent / "agents"))
    from sector_regime_analyst import OVERLAY_CODES

    missing = codes - set(OVERLAY_CODES)
    assert not missing, f"RES-07 이 조회하지 않는 계열: {missing}"
    print("  계열 스펙 생성           OK")


def _check_observation_pit():
    obs = build_observation(parse_block(_BLOCK), series_code="BOND_FUT",
                            upcode="606", trade_date=date(2026, 8, 3),
                            observed_at=datetime(2026, 8, 3, 9, 0,
                                                 tzinfo=timezone.utc))
    assert obs.period == date(2026, 8, 3)
    assert obs.value == Decimal("1209.33")
    assert obs.published_at.astimezone(KST).hour == 15
    assert obs.vintage_date == obs.period, "vintage 가 흔들리면 재수집이 중복된다"
    assert obs.metadata["upcode"] == "606"
    # 같은 날 두 번 만들어도 멱등
    obs2 = build_observation(parse_block(_BLOCK), series_code="BOND_FUT",
                             upcode="606", trade_date=date(2026, 8, 3),
                             observed_at=datetime(2026, 8, 4, 1, 0,
                                                  tzinfo=timezone.utc))
    assert obs2.vintage_date == obs.vintage_date
    print("  PIT 3시각·멱등 vintage   OK")


def _check_registry():
    from source_registry import SourceRegistry, UseScope

    SourceRegistry(env={}).check_use(SOURCE_ID, UseScope.FULLTEXT_STORE)
    sys.path.insert(0, str(_BASE.parent / "evidence"))
    from methods import METHODS

    m = {x.key: x for x in METHODS}
    for key in ("flight_to_quality", "style_rotation_ratio",
                "low_volatility_preference"):
        assert key in m, f"방법론 레지스트리에 {key} 가 없다"
        assert m[key].status == "ADOPTED", key
    print("  Source·방법론 등재       OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--collect" in sys.argv:
        raise SystemExit(collect())

    print(f"{COLLECTOR_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_config_parse()
    _check_specs()
    _check_observation_pit()
    _check_registry()
    print("스타일 지수 수집 4개 영역 통과. 수집은 --collect")
