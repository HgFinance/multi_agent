#!/usr/bin/env python3
"""Market Data Steward - 시세 평면의 심박·품질·지연을 결정론으로 감사한다.

소유: 재일 (리서치본부, market-data-steward 결정론 3호)
근거: timescaledb/migrations/001_initial_market_data.sql
      (ingestion_watermarks / data_quality_windows / feed_gaps),
      TEAM_JAEIL 가이드 8.2 DQ, market_breadth_collector 의
      "검사를 통과한 것과 검사를 못 한 것을 같은 값으로 기록하지 않는다" 원칙

▶ 무엇을 하나 (LLM 관여 없음 - 전부 SQL 집계와 규칙 판정)
  1. 심박: 스트림별(체결/호가/일봉/분봉/Breadth/파생) 마지막 이벤트·수신
     시각을 ingestion_watermarks 에 upsert. "언제까지 들어왔나"의 단일 좌표.
  2. 품질 배터리: 최근 데이터 하루 창에 대해 스트림별 관측수·중복
     source_event_id·지연 p95/최대를 계산해 data_quality_windows 에 기록.
     PK 가 instrument_id 를 요구하므로 전체 집계는 zero-UUID 센티널을 쓴다.
  3. 커버리지: 마지막 거래일에 일봉이 빠진 종목 수 (유니버스 350 대비).
     v1 은 **보고만** 한다 - feed_gaps 행 생성은 OPEN 상태의 생명주기
     관리(회복 판정·재시도)가 같이 가야 해서 후속으로 미룬다.

▶ 판정 규칙 (rule_version steward-v1)
  - 중복 source_event_id 비율 > 0.1% -> WARN, > 1% -> FAIL
  - 체결·호가 지연 p95 > 5,000ms -> WARN, > 30,000ms -> FAIL
    (백필 스트림(ls_chart)은 지연 판정을 하지 않는다 - 소급 관측이 정상)
  - 마지막 거래일 일봉 누락 종목 > 5% -> WARN, > 20% -> FAIL
  - 판정 불가(행 0·캘린더 미상)는 PASS 로 위장하지 않고 WARN + 사유.

사용
  python collectors/market_data_steward.py            # 자체 점검 (DB 없음)
  python collectors/market_data_steward.py --audit    # 감사 실행 + 기록
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

STEWARD_VERSION = "research-market-data-steward-v1"
RULE_VERSION = "steward-v1"
KST = timezone(timedelta(hours=9))
AGG_SENTINEL = "00000000-0000-0000-0000-000000000000"  # 전체 집계용 (PK 가 NOT NULL)

STREAMS = (
    # (stream_type, 테이블, 이벤트 컬럼, 수신 컬럼, 지연 판정 여부, 필터)
    ("ticks", "market.market_ticks", "event_time", "received_at", True, ""),
    ("quotes", "market.market_quotes", "event_time", "received_at", True, ""),
    ("bars_1d_chart", "market.market_bars", "bucket_time", "observed_at", False,
     "and interval_code='1D' and source='ls_chart'"),
    ("bars_1m_chart", "market.market_bars", "bucket_time", "observed_at", False,
     "and interval_code='1M' and source='ls_chart'"),
    ("breadth", "market.market_breadth", "event_time", "observed_at", True, ""),
    ("derivatives", "market.derivative_snapshots", "event_time", "received_at", True, ""),
)

DUP_WARN, DUP_FAIL = 0.001, 0.01
LAT_WARN_MS, LAT_FAIL_MS = 5_000, 30_000
COVER_WARN, COVER_FAIL = 0.05, 0.20


# ---------------------------------------------------------------------------
# 판정 (순수 함수 - 자체점검 대상)
# ---------------------------------------------------------------------------

def judge_duplicates(total: int, dups: int) -> tuple[str, str]:
    if total <= 0:
        return "WARN", "행 0 - 판정 불가(통과로 위장하지 않는다)"
    ratio = dups / total
    if ratio > DUP_FAIL:
        return "FAIL", f"중복 {ratio:.3%}"
    if ratio > DUP_WARN:
        return "WARN", f"중복 {ratio:.3%}"
    return "PASS", f"중복 {ratio:.4%}"


def judge_latency(p95_ms: int | None, *, applicable: bool) -> tuple[str, str]:
    if not applicable:
        return "PASS", "백필 스트림 - 지연 판정 제외(소급 관측이 정상)"
    if p95_ms is None:
        return "WARN", "지연 표본 없음 - 판정 불가"
    if p95_ms > LAT_FAIL_MS:
        return "FAIL", f"p95 {p95_ms:,}ms"
    if p95_ms > LAT_WARN_MS:
        return "WARN", f"p95 {p95_ms:,}ms"
    return "PASS", f"p95 {p95_ms:,}ms"


def judge_coverage(universe: int, missing: int) -> tuple[str, str]:
    if universe <= 0:
        return "WARN", "유니버스 0 - 판정 불가"
    ratio = missing / universe
    if ratio > COVER_FAIL:
        return "FAIL", f"일봉 누락 {missing}/{universe} ({ratio:.1%})"
    if ratio > COVER_WARN:
        return "WARN", f"일봉 누락 {missing}/{universe} ({ratio:.1%})"
    return "PASS", f"일봉 누락 {missing}/{universe}"


def worst(*statuses: str) -> str:
    order = {"PASS": 0, "WARN": 1, "FAIL": 2}
    return max(statuses, key=lambda s: order.get(s, 2))


@dataclass
class StreamAudit:
    stream: str
    rows_24h: int
    dup_count: int
    p95_ms: int | None
    max_ms: int | None
    last_event: datetime | None
    last_received: datetime | None
    status: str
    reasons: list


# ---------------------------------------------------------------------------
# 감사 본체
# ---------------------------------------------------------------------------

def audit_stream(cur, stream, table, ev_col, rc_col, lat_applicable, flt,
                 *, now: datetime) -> StreamAudit:
    since = now - timedelta(hours=24)
    cur.execute(f"""
        select count(*),
               max({ev_col}), max({rc_col}),
               percentile_cont(0.95) within group
                 (order by extract(epoch from ({rc_col} - {ev_col})) * 1000),
               max(extract(epoch from ({rc_col} - {ev_col})) * 1000)
        from {table} where {ev_col} >= %s {flt}
    """, (since,))
    total, last_ev, last_rc, p95, mx = cur.fetchone()

    dups = 0
    if total and "source_event_id" in _columns(cur, table):
        cur.execute(f"""
            select coalesce(sum(c - 1), 0) from (
              select count(*) c from {table}
              where {ev_col} >= %s {flt}
              group by source_event_id having count(*) > 1) d
        """, (since,))
        dups = int(cur.fetchone()[0])

    d_status, d_reason = judge_duplicates(total or 0, dups)
    if not total:
        l_status, l_reason = "PASS", ""   # 행 0 사유는 중복 판정이 이미 남긴다
    else:
        l_status, l_reason = judge_latency(
            None if p95 is None else int(p95), applicable=lat_applicable)
    # 행 0 스트림: 세션 밖(파생·Breadth 야간)일 수 있어 FAIL 대신 WARN 사유만
    return StreamAudit(
        stream=stream, rows_24h=total or 0, dup_count=dups,
        p95_ms=None if p95 is None else int(p95),
        max_ms=None if mx is None else int(mx),
        last_event=last_ev, last_received=last_rc,
        status=worst(d_status, l_status),
        reasons=[r for r in (d_reason, l_reason) if r])


_COL_CACHE: dict = {}


def _columns(cur, table: str) -> set:
    if table not in _COL_CACHE:
        schema, name = table.split(".")
        cur.execute(
            "select column_name from information_schema.columns "
            "where table_schema=%s and table_name=%s", (schema, name))
        _COL_CACHE[table] = {r[0] for r in cur.fetchall()}
    return _COL_CACHE[table]


def audit(write: bool = True) -> int:
    import psycopg2

    from source_registry import load_project_env

    env = load_project_env()
    now = datetime.now(timezone.utc)
    tconn = psycopg2.connect(env["TIMESCALE_DATABASE_URL"], connect_timeout=20)
    sconn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    try:
        tcur = tconn.cursor()
        audits: list[StreamAudit] = []
        for spec in STREAMS:
            audits.append(audit_stream(tcur, *spec, now=now))

        # 커버리지: 마지막 거래일(캘린더 최신 버전)에 일봉이 있는가
        with sconn.cursor() as cur:
            cur.execute("""
                select max(s.trade_date) from reference.market_sessions s
                join reference.market_calendar_versions v using (calendar_version_id)
                where s.market='KRX' and s.session_type='REGULAR' and s.is_trading_day
                  and s.trade_date <= %s
                  and v.version = (select max(version)
                                   from reference.market_calendar_versions where market='KRX')
            """, (now.astimezone(KST).date(),))
            last_td = cur.fetchone()[0]
        universe = missing = 0
        if last_td is not None:
            tcur.execute("""
                with uni as (select distinct instrument_id from market.market_bars
                             where interval_code='1D' and source='ls_chart')
                select (select count(*) from uni),
                       (select count(*) from uni u where not exists (
                          select 1 from market.market_bars b
                          where b.instrument_id=u.instrument_id
                            and b.interval_code='1D' and b.source='ls_chart'
                            and (b.bucket_time at time zone 'Asia/Seoul')::date=%s))
            """, (last_td,))
            universe, missing = tcur.fetchone()
        c_status, c_reason = judge_coverage(universe, missing)

        overall = worst(c_status, *(a.status for a in audits))

        print(f"{STEWARD_VERSION} 감사 {now.astimezone(KST):%Y-%m-%d %H:%M} KST "
              f"(창: 최근 24h)", flush=True)
        for a in audits:
            le = "-" if a.last_event is None else f"{a.last_event.astimezone(KST):%m-%d %H:%M:%S}"
            print(f"  [{a.status:4}] {a.stream:14} {a.rows_24h:>10,}행 | 중복 {a.dup_count} | "
                  f"p95 {a.p95_ms if a.p95_ms is not None else '-'}ms | 최종 {le} | "
                  f"{'; '.join(a.reasons)}", flush=True)
        print(f"  [{c_status:4}] coverage_1d    기준일 {last_td} | {c_reason}", flush=True)
        print(f"  종합: {overall}", flush=True)

        if write:
            for a in audits:
                tcur.execute("""
                    insert into market.ingestion_watermarks
                      (provider, stream_type, market, last_event_time,
                       last_received_at, collector_instance, updated_at)
                    values ('ls', %s, 'KRX', %s, %s, %s, now())
                    on conflict (provider, stream_type, market) do update set
                      last_event_time = excluded.last_event_time,
                      last_received_at = excluded.last_received_at,
                      collector_instance = excluded.collector_instance,
                      updated_at = now()
                """, (a.stream, a.last_event, a.last_received, STEWARD_VERSION))
                tcur.execute("""
                    insert into market.data_quality_windows
                      (window_start, window_end, provider, stream_type, instrument_id,
                       observed_count, duplicate_count, p95_latency_ms,
                       max_latency_ms, quality_status, rule_version, metrics)
                    values (%s, %s, 'ls', %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    on conflict (window_start, provider, stream_type, instrument_id)
                    do update set observed_count = excluded.observed_count,
                                  duplicate_count = excluded.duplicate_count,
                                  p95_latency_ms = excluded.p95_latency_ms,
                                  max_latency_ms = excluded.max_latency_ms,
                                  quality_status = excluded.quality_status,
                                  metrics = excluded.metrics
                """, (now - timedelta(hours=24), now, a.stream, AGG_SENTINEL,
                      a.rows_24h, a.dup_count, a.p95_ms, a.max_ms, a.status,
                      RULE_VERSION,
                      json.dumps({"reasons": a.reasons, "agg": "all-instruments",
                                  "sentinel_note": "instrument_id 는 전체 집계 센티널"})))
            tconn.commit()
            print(f"  기록: watermarks {len(audits)}건 + quality_windows {len(audits)}건",
                  flush=True)
        return 1 if overall == "FAIL" else 0
    finally:
        tconn.close()
        sconn.close()


# ---------------------------------------------------------------------------
# 자체 점검 - DB 없음
# ---------------------------------------------------------------------------

def _check_judges():
    assert judge_duplicates(0, 0)[0] == "WARN"          # 판정 불가 != PASS
    assert judge_duplicates(100000, 10)[0] == "PASS"
    assert judge_duplicates(100000, 500)[0] == "WARN"
    assert judge_duplicates(1000, 100)[0] == "FAIL"
    assert judge_latency(None, applicable=True)[0] == "WARN"
    assert judge_latency(1200, applicable=True)[0] == "PASS"
    assert judge_latency(9000, applicable=True)[0] == "WARN"
    assert judge_latency(60000, applicable=True)[0] == "FAIL"
    assert judge_latency(999999, applicable=False)[0] == "PASS"  # 백필 제외
    assert judge_coverage(350, 0)[0] == "PASS"
    assert judge_coverage(350, 30)[0] == "WARN"
    assert judge_coverage(350, 100)[0] == "FAIL"
    assert judge_coverage(0, 0)[0] == "WARN"
    assert worst("PASS", "WARN") == "WARN" and worst("WARN", "FAIL") == "FAIL"
    assert worst("PASS", "PASS") == "PASS"
    print("  판정 규칙 배터리         OK")


def _check_stream_table():
    assert len({s[0] for s in STREAMS}) == len(STREAMS), "stream_type 중복"
    for s in STREAMS:
        assert len(s) == 6 and s[1].startswith("market."), s
    # 백필 스트림은 지연 판정 제외가 명시돼 있어야 한다
    by_name = {s[0]: s for s in STREAMS}
    assert by_name["bars_1d_chart"][4] is False
    assert by_name["bars_1m_chart"][4] is False
    assert by_name["ticks"][4] is True
    print("  스트림 정의 무결성       OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--audit" in sys.argv:
        raise SystemExit(audit(write="--dry" not in sys.argv))

    print(f"{STEWARD_VERSION} 자체 점검 (DB 없음)")
    _check_judges()
    _check_stream_table()
    print("Steward 2개 영역 통과. 감사는 --audit (--dry 는 기록 없이)")
