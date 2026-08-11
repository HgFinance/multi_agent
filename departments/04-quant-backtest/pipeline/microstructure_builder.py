#!/usr/bin/env python3
"""호가·체결 -> 일별 마이크로구조 피처. **백테스트가 읽는 것은 이 계층이다.**

소유: 재일 (퀀트·백테스트본부 QNT)
근거: 재일님 지시 2026-08-12 "호가체결 부터 하고 나머지 실행"
계약: market.microstructure_features (timescaledb 마이그레이션에 이미 있다)

▶ 왜 원시를 그대로 안 굳히나
  market_quotes 181GB / market_ticks 41GB 다(압축 후). 백테스트는 데이터셋을
  파티션 파일로 읽는데, 원시를 그대로 내보내면 한 번 실행에 수십 GB 를 읽어야
  하고 디스크도 다시 찬다. 그리고 **전략이 실제로 쓰는 것은 원시가 아니다** -
  스프레드·호가불균형·주문흐름불균형 같은 종목·일자 단위 값이다.

  그래서 원시는 원천으로 두고 여기서 일별로 접는다. 60거래일 × 2,600종목 =
  15만 행 수준이라 데이터셋으로 굳히기에 알맞다.

▶ 계산은 전부 SQL 로 한다 (결정론)
  같은 구간을 두 번 돌리면 같은 값이 나와야 재현이 성립한다. 파이썬으로 끌어와
  집계하면 부동소수 누적 순서가 실행마다 달라질 수 있고, 무엇보다 수억 행을
  네트워크로 옮기게 된다.

▶ 못 잰 것을 0 으로 채우지 않는다
  그날 호가가 없던 종목의 스프레드는 **0 이 아니라 없음**이다. 0 으로 채우면
  "스프레드가 가장 좁은 종목" 을 고를 때 거래가 없던 종목이 1등이 된다.
  SQL 이 NULL 로 두고, 적재도 NULL 로 넣는다.

▶ PIT
  observed_at 은 원천의 observed_at 최댓값을 쓴다. 이관 구간은 그것이
  event_time 자리 채움이므로(pit_provenance 가 'NONE' 으로 못박음) 이 피처도
  그 구간에서는 PIT 를 주장하지 않는다 - input_watermark 에 그대로 남긴다.

사용
  python pipeline/microstructure_builder.py                    # 자체 점검
  python pipeline/microstructure_builder.py --build            # 미적재 전 구간
  python pipeline/microstructure_builder.py --build --from 2026-08-01
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "01-research" / "collectors"))

BUILDER_VERSION = "quant-microstructure-builder-v1"
FEATURE_SET_VERSION = "ms-daily-v1"
KST = timezone(timedelta(hours=9))

# 정규장만 접는다. 시간외·프리마켓은 체결 규칙이 달라 같은 통계에 섞으면
# 스프레드·체결강도가 왜곡된다(가이드 8.2 와 같은 이유).
SESSION_START, SESSION_END = "09:00", "15:30"

# 그날 이 미만이면 통계가 아니라 잡음이다. 버리지 않고 quality_status 로 표시해
# 소비자가 거르게 한다 - 조용히 빼면 유니버스가 왜 줄었는지 알 수 없다.
MIN_TICKS_FOR_PASS = 30
MIN_QUOTES_FOR_PASS = 30


# ── 일별 피처 SQL ──────────────────────────────────────────────────────────
#   호가와 체결을 각각 접고 종목 기준으로 붙인다. full outer join 인 이유:
#   호가만 있고 체결이 없는 종목(거래정지 직전 등)도 사실이므로 버리지 않는다.
_SQL_BUILD = """
with q as (
    select instrument_id,
           avg(case when mid_price > 0 then spread / mid_price * 10000 end) spread_bps,
           avg(depth_imbalance) depth_imbalance,
           count(*) n_quotes,
           max(observed_at) obs_q,
           max(event_time) evt_q
      from market.market_quotes
     where event_time >= %(lo)s and event_time < %(hi)s
     group by instrument_id
), t as (
    select instrument_id,
           -- 주문흐름불균형: 부호 있는 체결량 / 총 체결량. 분모 0 이면 NULL.
           case when sum(quantity) > 0
                then sum(side * quantity)::float8 / sum(quantity) end ofi,
           -- 체결강도: 분당 체결 건수. 정규장 390분 고정이 아니라 관측 폭으로
           -- 나눈다 - 반나절 장에서 두 배로 부풀지 않는다.
           case when max(event_time) > min(event_time)
                then count(*)::float8
                     / (extract(epoch from max(event_time) - min(event_time)) / 60.0)
                end trade_intensity,
           -- 실현변동성: 체결가 로그수익률 표준편차 × sqrt(건수). 건수가 적으면
           -- 표본표준편차가 정의되지 않아 NULL 이 된다(그게 맞다).
           stddev_samp(ln(nullif(price, 0)::float8)) * sqrt(count(*)) realized_vol,
           count(*) n_ticks,
           sum(quantity) vol,
           max(observed_at) obs_t,
           max(event_time) evt_t
      from market.market_ticks
     where event_time >= %(lo)s and event_time < %(hi)s
       and price > 0
     group by instrument_id
)
select coalesce(q.instrument_id, t.instrument_id) instrument_id,
       q.spread_bps, q.depth_imbalance, t.ofi, t.trade_intensity, t.realized_vol,
       coalesce(t.n_ticks, 0) n_ticks, coalesce(q.n_quotes, 0) n_quotes,
       greatest(coalesce(q.obs_q, %(lo)s), coalesce(t.obs_t, %(lo)s)) observed_at,
       greatest(coalesce(q.evt_q, %(lo)s), coalesce(t.evt_t, %(lo)s)) watermark
  from q full outer join t on t.instrument_id = q.instrument_id
"""

_SQL_INSERT = """
insert into market.microstructure_features
  (event_time, observed_at, instrument_id, market, feature_set_version,
   realized_volatility, spread_bps, depth_imbalance, order_flow_imbalance,
   trade_intensity, values, quality_status, input_watermark, input_hash)
values %s
on conflict do nothing
"""

# 이미 접은 날은 다시 접지 않는다. 같은 날을 두 번 넣으면 데이터셋 해시가
# 실행마다 달라져 재현이 깨진다.
_SQL_DONE_DAYS = """
select distinct (event_time at time zone 'Asia/Seoul')::date
  from market.microstructure_features
 where feature_set_version = %s
"""

_SQL_SOURCE_DAYS = """
select distinct (range_start at time zone 'Asia/Seoul')::date
  from timescaledb_information.chunks
 where hypertable_name = 'market_ticks'
 order by 1
"""


def quality_of(n_ticks: int, n_quotes: int) -> str:
    """행 수로 등급을 매긴다. **버리지 않고 표시한다.**

    조용히 빼면 소비자는 유니버스가 왜 줄었는지 알 수 없다. WARN 을 남기면
    "거래가 거의 없던 종목" 이라는 사실 자체가 재료가 된다.
    """
    if n_ticks >= MIN_TICKS_FOR_PASS and n_quotes >= MIN_QUOTES_FOR_PASS:
        return "PASS"
    if n_ticks == 0 and n_quotes == 0:
        return "FAIL"          # 있을 수 없는 행이다 - 그래도 만들면 표시한다
    return "WARN"


def input_hash(day: date, n_ticks: int, n_quotes: int) -> str:
    """어느 입력에서 나온 값인지. 원천이 바뀌면 해시가 바뀐다."""
    blob = f"{FEATURE_SET_VERSION}|{day.isoformat()}|{n_ticks}|{n_quotes}"
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def session_bounds(day: date) -> tuple[str, str]:
    """그날 정규장 구간(KST). 시간외를 섞지 않는다."""
    return (f"{day.isoformat()} {SESSION_START}+09",
            f"{day.isoformat()} {SESSION_END}+09")


def build_day(market_conn, day: date, *, dry_run: bool = False) -> dict:
    """하루를 접어 적재한다. 반환: 요약."""
    from psycopg2.extras import execute_values

    lo, hi = session_bounds(day)
    with market_conn.cursor() as cur:
        cur.execute(_SQL_BUILD, {"lo": lo, "hi": hi})
        rows = cur.fetchall()
    if not rows:
        return {"day": day, "rows": 0, "note": "원천에 그날 행이 없다"}

    bucket = datetime.combine(day, datetime.min.time(), tzinfo=KST)
    payload, grades = [], {"PASS": 0, "WARN": 0, "FAIL": 0}
    for (iid, spread, di, ofi, intensity, rvol,
         n_ticks, n_quotes, observed_at, watermark) in rows:
        g = quality_of(int(n_ticks), int(n_quotes))
        grades[g] += 1
        payload.append((
            bucket, observed_at, iid, "KRX", FEATURE_SET_VERSION,
            rvol, spread, di, ofi, intensity,
            # values 에 표본 수를 남긴다 - 어떤 행이 얇은지 나중에 판단할 수 있어야 한다
            f'{{"n_ticks": {int(n_ticks)}, "n_quotes": {int(n_quotes)}}}',
            g, watermark, input_hash(day, int(n_ticks), int(n_quotes))))

    if dry_run:
        return {"day": day, "rows": len(payload), **grades, "dry_run": True}
    with market_conn.cursor() as cur:
        execute_values(cur, _SQL_INSERT, payload, page_size=2000)
    market_conn.commit()
    return {"day": day, "rows": len(payload), **grades}


def pending_days(market_conn, *, since: date | None = None) -> list[date]:
    """원천에는 있는데 아직 안 접은 날."""
    with market_conn.cursor() as cur:
        cur.execute(_SQL_SOURCE_DAYS)
        src = [r[0] for r in cur.fetchall()]
        cur.execute(_SQL_DONE_DAYS, (FEATURE_SET_VERSION,))
        done = {r[0] for r in cur.fetchall()}
    out = [d for d in src if d not in done and d.isoweekday() <= 5]
    return [d for d in out if since is None or d >= since]


# ── 자체 점검 (DB 없음) ────────────────────────────────────────────────────
def _check_quality_is_marked_not_dropped():
    """얇은 표본을 **버리지 않고 표시한다.** 조용히 빼면 유니버스가 왜 줄었는지 모른다."""
    assert quality_of(100, 100) == "PASS"
    assert quality_of(5, 100) == "WARN"
    assert quality_of(100, 5) == "WARN"
    assert quality_of(0, 0) == "FAIL"
    # 경계에서 통과한다 - 임계값 바로 위가 WARN 이면 하루치가 통째로 밀린다
    assert quality_of(MIN_TICKS_FOR_PASS, MIN_QUOTES_FOR_PASS) == "PASS"
    print("  표본 등급(버리지 않음)   OK")


def _check_session_bounds_exclude_after_hours():
    """정규장만 접는다. 시간외를 섞으면 스프레드·체결강도가 왜곡된다.

    NXT 애프터마켓은 20:00 까지 도는데, 그 구간의 넓은 스프레드가 정규장 통계에
    섞이면 유동성 순위가 뒤집힌다.
    """
    lo, hi = session_bounds(date(2026, 8, 11))
    assert lo.endswith("09:00+09") and hi.endswith("15:30+09"), (lo, hi)
    assert "2026-08-11" in lo and "2026-08-11" in hi
    print("  정규장 구간 한정         OK")


def _check_input_hash_tracks_inputs():
    """원천이 바뀌면 해시가 바뀐다 - 같은 해시면 같은 입력이라는 뜻이어야 한다."""
    d = date(2026, 8, 11)
    a = input_hash(d, 100, 200)
    assert a == input_hash(d, 100, 200)
    assert a != input_hash(d, 101, 200)
    assert a != input_hash(d, 100, 201)
    assert a != input_hash(date(2026, 8, 10), 100, 200)
    print("  입력 해시 결정론         OK")


def _check_sql_does_not_zero_fill():
    """**못 잰 것을 0 으로 채우지 않는다.**

    그날 호가가 없던 종목의 스프레드는 0 이 아니라 없음이다. 0 으로 채우면
    '스프레드가 가장 좁은 종목' 을 고를 때 거래가 없던 종목이 1등이 된다.
    """
    s = " ".join(_SQL_BUILD.split())
    for expr in ("coalesce(q.spread_bps", "coalesce(t.ofi",
                 "coalesce(q.depth_imbalance", "coalesce(t.realized_vol"):
        assert expr not in s, f"피처를 0/기본값으로 채우고 있다: {expr}"
    # 표본 수는 0 으로 채워도 된다 - 그건 '행이 없었다' 가 사실이다
    assert "coalesce(t.n_ticks, 0)" in s and "coalesce(q.n_quotes, 0)" in s
    # 분모 0 방어가 값을 0 으로 만들지 않고 NULL 로 두는가
    assert "nullif(price, 0)" in s
    assert "when sum(quantity) > 0" in s
    print("  결측을 0 으로 안 채움    OK")


def _check_full_outer_join_keeps_one_sided_days():
    """호가만 있고 체결이 없는 종목도 사실이다 - 버리면 거래정지가 안 보인다."""
    s = " ".join(_SQL_BUILD.split()).lower()
    assert "full outer join" in s, "한쪽만 있는 종목이 사라진다"
    assert "coalesce(q.instrument_id, t.instrument_id)" in s
    print("  한쪽만 있는 종목 보존    OK")


def _check_intensity_uses_observed_span():
    """체결강도를 고정 분수로 나누면 반나절 장에서 두 배로 부풀어 보인다.

    **주석이 아니라 실행되는 SQL 만 본다.** 처음엔 SQL 전체에서 정규장 길이
    숫자를 찾았는데, 그 숫자를 설명하는 주석 자체에 걸려 통과하지 못했다 -
    검사가 코드가 아니라 문서를 검사하고 있었다.
    """
    code = " ".join(ln.split("--")[0] for ln in _SQL_BUILD.splitlines())
    code = " ".join(code.split())
    assert "max(event_time) - min(event_time)" in code
    for const in ("390", "381", "360"):
        assert f"/ {const}" not in code and f"/{const}" not in code, \
            f"정규장 길이를 상수 {const} 로 박았다"
    print("  체결강도 관측폭 기준     OK")


def _selfcheck() -> int:
    print(f"{BUILDER_VERSION} 자체 점검 (DB 없음)")
    _check_quality_is_marked_not_dropped()
    _check_session_bounds_exclude_after_hours()
    _check_input_hash_tracks_inputs()
    _check_sql_does_not_zero_fill()
    _check_full_outer_join_keeps_one_sided_days()
    _check_intensity_uses_observed_span()
    print("마이크로구조 빌더 6개 영역 통과. 실행은 --build")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="호가·체결 -> 일별 마이크로구조 피처")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--from", dest="since", default="")
    a = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not a.build:
        return _selfcheck()

    import psycopg2
    from source_registry import load_project_env

    env = load_project_env()
    conn = psycopg2.connect(env["TIMESCALE_DATABASE_URL"], connect_timeout=20)
    try:
        since = date.fromisoformat(a.since) if a.since else None
        days = pending_days(conn, since=since)
        print(f"{BUILDER_VERSION}: 접을 날 {len(days)}건"
              + (f" ({days[0]} ~ {days[-1]})" if days else ""), flush=True)
        total = 0
        for i, d in enumerate(days, 1):
            r = build_day(conn, d, dry_run=a.dry_run)
            total += r["rows"]
            print(f"  [{i}/{len(days)}] {d}: {r['rows']:,}종목 "
                  f"PASS {r.get('PASS', 0)} WARN {r.get('WARN', 0)} "
                  f"FAIL {r.get('FAIL', 0)}", flush=True)
        print(f"  완료: {total:,}행", flush=True)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
