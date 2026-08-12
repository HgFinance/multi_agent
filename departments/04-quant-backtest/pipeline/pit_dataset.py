#!/usr/bin/env python3
"""PIT Dataset Builder - 일봉 시계열을 Manifest 와 함께 재현 가능하게 굳힌다.

소유: 재일 (퀀트/백테스트본부, QNT-02 Feature/Dataset Engineer 직무의 결정론 부분)
근거: supabase/migrations/20260729000300 (quant.universe_versions/dataset_manifests/
      dataset_partitions), timescaledb 001 (market.market_bars),
      TEAM_JAEIL 가이드 수용 기준 "Backtest가 PIT Dataset Manifest로 재현된다"

▶ 이 파일이 지키는 PIT 원칙 (QNT-02 페르소나 계약의 코드 구현)
  - 가시성은 observed_at 으로 판단한다. 차트 백필 봉은 event_time(bucket_time)이
    과거라도 관측은 2026-07-31 이후다 - **가격 역사로는 쓸 수 있지만 "그 시점에
    알았던 것"은 아니므로** Manifest 의 point_in_time_policy 에 backfill_vintage
    (관측 시각 최댓값)를 박아 소비자가 구분하게 한다.
  - **아는 편향은 선언한다.** v1 유니버스는 2026-07-31 의 코스피200+코스닥150
    바스켓을 과거로 투영한 것이다 - 이것은 생존 편향이다(QNT-02 금지 사항).
    과거 구성 종목 이력을 아직 못 구했으므로, 숨기는 대신 universe rules 와
    leakage_check 에 SURVIVORSHIP_BIAS_DECLARED 로 명시해 WARN 등급을 강제한다.
    이력을 구하면 새 universe version 으로 갈아끼운다 - 소급 수정하지 않는다.
  - 수정주가(sujung=Y)는 공급자가 소급 조정한 가격이다. adjustment_policy 로 선언.
  - Dataset 은 불변이다. content_hash 가 같으면 같은 데이터셋이고(중복 등록 방지),
    다르면 새 버전이다. 수정·삭제 경로가 없다.

▶ 저장 구조
  - Manifest·Partition 메타 = Supabase quant 스키마 (팀 전체가 조회)
  - 행 데이터 = quant-data/<name>-<version>/<YYYY-MM>.csv.gz (git 제외,
    TSDB 에서 언제든 재유도 가능 - content_hash 가 동일성을 증명한다)

사용
  python pipeline/pit_dataset.py                 # 자체 점검 (DB 없음)
  python pipeline/pit_dataset.py --build \
      --name krx-basket-daily --version v1 \
      --from 2024-01-02 --to 2026-07-30         # 실제 빌드 + 등록
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

BUILDER_VERSION = "quant-pit-dataset-v1"
KST = timezone(timedelta(hours=9))
SCHEMA_VERSION = 1
BAR_SOURCE = "ls_chart"
INTERVAL = "1D"
DATA_ROOT = Path(__file__).resolve().parents[3] / "quant-data"


def resolve_object_path(object_path: str, *, root: Path | None = None) -> Path:
    """원장에 적힌 `object_path` 를 이 OS 의 실제 경로로 푼다.

    ▶ 왜 필요한가 (2026-08-12 실측)
      `object_path` 는 `str(path.relative_to(DATA_ROOT.parent))` 로 저장된다.
      즉 **빌드한 OS 의 구분자가 그대로 원장에 박힌다.** v1·v2 는 윈도우에서
      만들어져 `quant-data\\krx-basket-daily-v2` 로 들어가 있는데, 리눅스에서는
      역슬래시가 파일명의 한 글자라 `parent / object_path` 가 존재하지 않는
      경로가 된다 - 데이터는 멀쩡히 있는데 "파티션 없음"이 된다.

      경로를 원장에서 고치지 않고 읽는 쪽에서 정규화한다. 원장 값은 그 빌드가
      실제로 쓴 문자열이라 사실이고, 사실을 고치는 대신 해석을 OS 에 맞춘다.
    """
    return (root or DATA_ROOT.parent) / Path(str(object_path).replace("\\", "/"))

# Dataset 행 스키마 - 컬럼 순서가 content_hash 의 일부다. 바꾸면 새 스키마다.
# ▶ notional(거래대금) 추가 2026-08-04. **유동성 계층 슬리피지의 유일한
#   재료**인데 SELECT·파티션 어디에도 없어서 전 종목이 시장 중앙값으로
#   떨어졌다 - 리서치본부 /bars 의 notional 누락과 같은 유형이다.
#   열이 늘었으므로 content_hash 가 바뀐다(데이터가 바뀐 것이니 맞다).
COLUMNS = ("instrument_id", "trade_date", "open", "high", "low", "close",
           "volume", "notional", "observed_at")


# ---------------------------------------------------------------------------
# 정규화·해시 - 재현성의 심장
# ---------------------------------------------------------------------------

def canon_number(raw) -> str:
    """Decimal/float/str 표기 차이가 해시를 흔들지 않게 정규화한다.

    '70000.0000000000' 과 '70000' 은 같은 값이다 - normalize 후 지수 표기를
    피해서 고정 문자열로 만든다.
    """
    d = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    d = d.normalize()
    return format(d, "f")


def row_line(row: dict) -> str:
    return "|".join((
        str(row["instrument_id"]),
        row["trade_date"].isoformat() if isinstance(row["trade_date"], date)
        else str(row["trade_date"]),
        canon_number(row["open"]), canon_number(row["high"]),
        canon_number(row["low"]), canon_number(row["close"]),
        canon_number(row["volume"]),
        # 거래대금. 값이 없는 행이 있으므로 빈 문자열로 정규화한다 - 0 으로
        # 채우면 "거래대금 0" 과 "미확인" 이 같아져 유동성 계층이 오염된다.
        canon_number(row["notional"]) if row.get("notional") is not None else "",
        row["observed_at"].astimezone(timezone.utc).isoformat()
        if isinstance(row["observed_at"], datetime) else str(row["observed_at"]),
    ))


def content_hash(rows: list[dict]) -> str:
    """행 순서와 무관하게 같은 내용이면 같은 해시. 정렬 키 = (종목, 날짜)."""
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: (str(r["instrument_id"]), str(r["trade_date"]))):
        h.update(row_line(r).encode())
        h.update(b"\n")
    return h.hexdigest()


def partition_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


# ---------------------------------------------------------------------------
# Leakage Check - 결정론 판정 (LLM 관여 없음)
# ---------------------------------------------------------------------------

@dataclass
class LeakageReport:
    checks: dict            # 이름 -> {"status": PASS|WARN|FAIL, ...}

    @property
    def overall(self) -> str:
        levels = [c["status"] for c in self.checks.values()]
        if "FAIL" in levels:
            return "FAIL"
        return "WARN" if "WARN" in levels else "PASS"


def run_leakage_checks(rows: list[dict], *, as_of: datetime,
                       trading_days: set[date] | None,
                       survivorship_declared: bool) -> LeakageReport:
    checks: dict = {}

    future = [r for r in rows
              if (r["trade_date"] if isinstance(r["trade_date"], date)
                  else date.fromisoformat(str(r["trade_date"]))) > as_of.date()]
    checks["no_future_bars"] = (
        {"status": "FAIL", "count": len(future)} if future else {"status": "PASS"})

    bad_clock = [r for r in rows
                 if isinstance(r["observed_at"], datetime)
                 and isinstance(r["trade_date"], date)
                 and r["observed_at"].date() < r["trade_date"]]
    checks["observed_not_before_event"] = (
        {"status": "FAIL", "count": len(bad_clock)} if bad_clock else {"status": "PASS"})

    if trading_days is None:
        # 검사를 못 한 것과 통과를 같은 값으로 기록하지 않는다 (breadth 원칙)
        checks["calendar_alignment"] = {"status": "WARN", "note": "calendar 미제공 - 미검증"}
    else:
        # 커버리지 구분 (실측 2026-07-31: 세션 데이터가 2026년뿐이라 2024~2025
        # 봉 486일이 전부 '비거래일'로 오판돼 빌드가 거부됐다). Calendar 가
        # **아는 구간 안에서 비거래일** 이면 FAIL, **모르는 구간**이면 미검증
        # WARN 이다 - 둘을 같은 값으로 판정하면 검사가 커버리지를 잣대로 쓴다.
        lo, hi = min(trading_days), max(trading_days)
        off, unverified = set(), 0
        for r in rows:
            d = (r["trade_date"] if isinstance(r["trade_date"], date)
                 else date.fromisoformat(str(r["trade_date"])))
            if d < lo or d > hi:
                unverified += 1
            elif d not in trading_days:
                off.add(str(d))
        if off:
            checks["calendar_alignment"] = {
                "status": "FAIL", "off_calendar_dates": sorted(off)[:10],
                "count": len(off)}
        elif unverified:
            checks["calendar_alignment"] = {
                "status": "WARN", "verified_range": [str(lo), str(hi)],
                "unverified_rows": unverified,
                "note": "calendar 커버리지 밖 구간은 미검증"}
        else:
            checks["calendar_alignment"] = {"status": "PASS"}

    # 선언된 생존 편향 - v1 유니버스의 구조적 한계. 숨기지 않고 WARN 을 강제한다.
    checks["survivorship_bias"] = (
        {"status": "WARN", "note": "유니버스가 as-of 스냅샷의 과거 투영 - "
                                   "과거 구성 이력 확보 시 새 universe version 필요"}
        if survivorship_declared else {"status": "PASS"})

    checks["provider_adjustment"] = {
        "status": "WARN",
        "note": "수정주가(sujung=Y) - 공급자가 CA 를 소급 반영한 가격. "
                "원가격 복원이 필요한 실험은 이 Dataset 을 쓰면 안 된다"}
    return LeakageReport(checks=checks)


# ---------------------------------------------------------------------------
# 빌드 본체
# ---------------------------------------------------------------------------

def fetch_bars(tconn, start: date, end: date) -> list[dict]:
    with tconn.cursor() as cur:
        cur.execute(
            """
            -- ▶ **열 순서는 COLUMNS 와 같아야 한다.** 행을 위치로 매핑하므로
            --   notional 을 뒤에 붙였다가 observed_at 자리와 어긋나
            --   Decimal 변환이 깨졌다(실측).
            select instrument_id, (bucket_time at time zone 'Asia/Seoul')::date,
                   open, high, low, close, volume,
                   -- 거래대금: 유동성 계층 슬리피지의 유일한 재료
                   notional, observed_at
            from market.market_bars
            where interval_code = %s and source = %s
              and bucket_time >= %s and bucket_time < %s
            order by instrument_id, bucket_time
            """,
            (INTERVAL, BAR_SOURCE,
             datetime.combine(start, datetime.min.time(), tzinfo=KST),
             datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=KST)))
        return [dict(zip(COLUMNS, r)) for r in cur.fetchall()]


def fetch_trading_days(conn, start: date, end: date) -> set[date] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select s.trade_date from reference.market_sessions s
            join reference.market_calendar_versions v using (calendar_version_id)
            where s.market = 'KRX' and s.session_type = 'REGULAR'
              and s.is_trading_day and s.trade_date between %s and %s
              and v.version = (select max(version) from reference.market_calendar_versions
                               where market = 'KRX')
            """, (start, end))
        days = {r[0] for r in cur.fetchall()}
    return days or None


def register_universe(conn, instrument_ids: list[str], *, as_of: datetime) -> str:
    """v1 유니버스 = 이 Dataset 에 봉이 존재하는 종목 전체 (편향 선언 포함)."""
    ids = sorted(set(instrument_ids))
    uhash = hashlib.sha256("\n".join(ids).encode()).hexdigest()
    rules = {
        "builder": BUILDER_VERSION,
        "definition": "market_bars(1D, ls_chart) 존재 종목 = 백필 바스켓(K200+Q150)",
        "known_bias": "SURVIVORSHIP_BIAS_DECLARED",
        "bias_note": "2026-07-31 바스켓의 과거 투영 - 과거 구성 이력 미확보",
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into quant.universe_versions
              (name, as_of, rules, member_count, content_hash, source_versions)
            values (%s, %s, %s::jsonb, %s, %s, %s::jsonb)
            on conflict (name, as_of, content_hash) do update set rules = excluded.rules
            returning universe_version_id
            """,
            ("krx-basket-backfill", as_of, json.dumps(rules, ensure_ascii=False),
             len(ids), uhash, json.dumps({"market_bars": BAR_SOURCE})))
        uid = str(cur.fetchone()[0])
        for iid in ids:
            cur.execute(
                """
                insert into quant.universe_members (universe_version_id, instrument_id)
                values (%s, %s) on conflict do nothing
                """, (uid, iid))
    conn.commit()
    return uid


def export_partitions(rows: list[dict], out_dir: Path) -> list[dict]:
    """월별 CSV.gz 로 내보내고 Partition 메타(행수·구간·해시)를 돌려준다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    by_month: dict[str, list[dict]] = {}
    for r in rows:
        by_month.setdefault(partition_key(r["trade_date"]), []).append(r)

    parts = []
    for key in sorted(by_month):
        chunk = sorted(by_month[key],
                       key=lambda r: (str(r["instrument_id"]), str(r["trade_date"])))
        path = out_dir / f"{key}.csv.gz"
        with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(COLUMNS)
            for r in chunk:
                w.writerow([
                    r["instrument_id"], r["trade_date"].isoformat(),
                    canon_number(r["open"]), canon_number(r["high"]),
                    canon_number(r["low"]), canon_number(r["close"]),
                    canon_number(r["volume"]),
                    canon_number(r["notional"]) if r.get("notional") is not None else "",
                    r["observed_at"].astimezone(timezone.utc).isoformat(),
                ])
        dates = [r["trade_date"] for r in chunk]
        parts.append({
            "partition_key": key,
            # as_posix() - 구분자를 원장에 남기지 않는다(위 resolve_object_path 참고).
            "object_path": path.relative_to(DATA_ROOT.parent).as_posix(),
            "row_count": len(chunk),
            "min_event_time": datetime.combine(min(dates), datetime.min.time(), tzinfo=KST),
            "max_event_time": datetime.combine(max(dates), datetime.min.time(), tzinfo=KST),
            "content_hash": content_hash(chunk),
        })
    return parts


def load_partition(path: Path) -> list[dict]:
    """CSV.gz -> 행 dict (빌드 시와 같은 타입으로 복원 - 해시 재검증용)."""
    rows = []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        for rec in csv.DictReader(f):
            rows.append({
                "instrument_id": rec["instrument_id"],
                "trade_date": date.fromisoformat(rec["trade_date"]),
                "open": Decimal(rec["open"]), "high": Decimal(rec["high"]),
                "low": Decimal(rec["low"]), "close": Decimal(rec["close"]),
                "volume": Decimal(rec["volume"]),
                # 옛 파티션엔 이 열이 없다 - get 으로 읽고 빈 값은 None
                "notional": (Decimal(rec["notional"])
                             if (rec.get("notional") or "").strip() else None),
                "observed_at": datetime.fromisoformat(rec["observed_at"]),
            })
    return rows


def build(name: str, version: str, start: date, end: date) -> int:
    import psycopg2

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "01-research" / "collectors"))
    from source_registry import load_project_env

    env = load_project_env()
    as_of = datetime.now(timezone.utc)
    conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    tconn = psycopg2.connect(env["TIMESCALE_DATABASE_URL"], connect_timeout=20)
    try:
        rows = fetch_bars(tconn, start, end)
        if not rows:
            print("봉이 0행이다 - 기간·소스 확인", flush=True)
            return 1
        days = fetch_trading_days(conn, start, end)
        report = run_leakage_checks(rows, as_of=as_of, trading_days=days,
                                    survivorship_declared=True)
        if report.overall == "FAIL":
            print(f"Leakage Check FAIL - 등록 거부: "
                  f"{json.dumps(report.checks, ensure_ascii=False, default=str)[:400]}",
                  flush=True)
            return 1

        uid = register_universe(conn, [str(r["instrument_id"]) for r in rows], as_of=as_of)
        out_dir = DATA_ROOT / f"{name}-{version}"
        parts = export_partitions(rows, out_dir)
        chash = content_hash(rows)
        vintage = max(r["observed_at"] for r in rows)

        pit_policy = {
            "visibility": "observed_at",
            "backfill_vintage_utc": vintage.astimezone(timezone.utc).isoformat(),
            "adjustment_policy": "provider_adjusted(sujung=Y)",
            "known_biases": ["SURVIVORSHIP_BIAS_DECLARED"],
            "reproduce": f"python pipeline/pit_dataset.py --build --name {name} "
                         f"--version {version} --from {start} --to {end}",
        }
        quality = {"leakage_overall": report.overall, "checks": report.checks}
        schema_def = {"columns": list(COLUMNS), "interval": INTERVAL,
                      "bar_source": BAR_SOURCE, "hash": "sha256(sorted rows)"}

        with conn.cursor() as cur:
            cur.execute(
                """
                insert into quant.dataset_manifests
                  (name, version, as_of, universe_version_id, source_versions,
                   feature_spec_versions, partitions, point_in_time_policy,
                   quality_summary, object_path, content_hash, row_count,
                   schema_definition)
                values (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                        %s::jsonb, %s, %s, %s, %s::jsonb)
                on conflict (content_hash) do update set quality_summary = excluded.quality_summary
                returning dataset_id
                """,
                (name, version, as_of, uid,
                 json.dumps({"market_bars": f"{BAR_SOURCE}/{INTERVAL}"}),
                 json.dumps({}),  # v1 은 원시 봉만 - Feature 는 후속
                 json.dumps([p["partition_key"] for p in parts]),
                 json.dumps(pit_policy, ensure_ascii=False),
                 json.dumps(quality, ensure_ascii=False, default=str),
                 out_dir.relative_to(DATA_ROOT.parent).as_posix(), chash, len(rows),
                 json.dumps(schema_def)))
            dataset_id = str(cur.fetchone()[0])
            for p in parts:
                cur.execute(
                    """
                    insert into quant.dataset_partitions
                      (dataset_id, partition_key, object_path, row_count,
                       min_event_time, max_event_time, content_hash, quality_status)
                    values (%s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (dataset_id, partition_key) do nothing
                    """,
                    (dataset_id, p["partition_key"], p["object_path"], p["row_count"],
                     p["min_event_time"], p["max_event_time"], p["content_hash"],
                     "PASS" if report.overall == "PASS" else "WARN"))
        conn.commit()

        n_inst = len({str(r["instrument_id"]) for r in rows})
        print(f"{BUILDER_VERSION}: {name}/{version} 등록 완료", flush=True)
        print(f"  dataset_id {dataset_id} | {len(rows):,}행 / {n_inst}종목 / "
              f"{len(parts)}개 월 Partition | Leakage {report.overall}", flush=True)
        print(f"  content_hash {chash[:16]}… | vintage {vintage:%Y-%m-%d %H:%M}UTC",
              flush=True)
        return 0
    finally:
        conn.close()
        tconn.close()


# ---------------------------------------------------------------------------
# 자체 점검 - DB 없음
# ---------------------------------------------------------------------------

def _mk_row(iid="i1", d=date(2026, 7, 30), o="100", c="101", obs=None):
    return {"instrument_id": iid, "trade_date": d,
            "open": Decimal(o), "high": Decimal(102), "low": Decimal(99),
            "close": Decimal(c), "volume": Decimal(1000),
            "observed_at": obs or datetime(2026, 7, 31, 5, 0, tzinfo=timezone.utc)}


def _check_hash_stability():
    a = [_mk_row("i1"), _mk_row("i2")]
    b = [_mk_row("i2"), _mk_row("i1")]              # 순서 무관
    assert content_hash(a) == content_hash(b)
    c = [_mk_row("i1"), _mk_row("i2", c="999")]     # 값이 다르면 다른 해시
    assert content_hash(a) != content_hash(c)
    # 표기 정규화: 70000.0000000000 == 70000
    assert canon_number(Decimal("70000.0000000000")) == "70000"
    assert canon_number("101.50") == "101.5"
    print("  content_hash 안정성      OK")


def _check_leakage():
    ok = [_mk_row()]
    as_of = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    days = {date(2026, 7, 30)}
    r = run_leakage_checks(ok, as_of=as_of, trading_days=days, survivorship_declared=True)
    assert r.checks["no_future_bars"]["status"] == "PASS"
    assert r.overall == "WARN"                       # 선언된 편향 + 수정주가 = WARN
    # 미래 봉 -> FAIL
    fut = [_mk_row(d=date(2026, 8, 15))]
    r2 = run_leakage_checks(fut, as_of=as_of, trading_days=None, survivorship_declared=True)
    assert r2.checks["no_future_bars"]["status"] == "FAIL" and r2.overall == "FAIL"
    # 관측이 event 보다 앞 -> FAIL
    bad = [_mk_row(obs=datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc))]
    r3 = run_leakage_checks(bad, as_of=as_of, trading_days=days, survivorship_declared=True)
    assert r3.checks["observed_not_before_event"]["status"] == "FAIL"
    # 커버리지 **안** 비거래일 -> FAIL (7/30 이 목록에 없는데 구간 안이다)
    r4 = run_leakage_checks(ok, as_of=as_of,
                            trading_days={date(2026, 7, 29), date(2026, 7, 31)},
                            survivorship_declared=True)
    assert r4.checks["calendar_alignment"]["status"] == "FAIL"
    # 커버리지 **밖** -> 미검증 WARN (2024~2025 백필 486일 오판 사례의 회귀 방지)
    r5 = run_leakage_checks(ok, as_of=as_of,
                            trading_days={date(2026, 7, 31)},
                            survivorship_declared=True)
    assert r5.checks["calendar_alignment"]["status"] == "WARN"
    assert r5.checks["calendar_alignment"]["unverified_rows"] == 1
    # 캘린더 미제공 -> WARN (통과로 위장 금지)
    r6 = run_leakage_checks(ok, as_of=as_of, trading_days=None, survivorship_declared=True)
    assert r6.checks["calendar_alignment"]["status"] == "WARN"
    print("  Leakage Check 판정       OK")


def _check_partition_roundtrip(tmp: Path):
    rows = [_mk_row("i1", date(2026, 6, 30)), _mk_row("i1", date(2026, 7, 1)),
            _mk_row("i2", date(2026, 7, 2))]
    parts = export_partitions(rows, tmp / "ds-v1")
    assert [p["partition_key"] for p in parts] == ["2026-06", "2026-07"]
    assert sum(p["row_count"] for p in parts) == 3
    # 파일 -> 행 복원 -> Partition 해시 재검증 (Runner 가 쓰는 무결성 경로)
    for p in parts:
        loaded = load_partition(resolve_object_path(p["object_path"]))
        assert content_hash(loaded) == p["content_hash"], p["partition_key"]
    # 전체 해시 = 복원 행 전체 해시
    all_loaded = [r for p in parts
                  for r in load_partition(resolve_object_path(p["object_path"]))]
    assert content_hash(all_loaded) == content_hash(rows)
    print("  Partition 왕복·재검증    OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--build" in sys.argv:
        a = sys.argv
        def opt(n, d=None):
            return a[a.index(n) + 1] if n in a else d
        name = opt("--name", "krx-basket-daily")
        ver = opt("--version", "v1")
        s = date.fromisoformat(opt("--from", "2024-01-02"))
        e = date.fromisoformat(opt("--to", "2026-07-30"))
        raise SystemExit(build(name, ver, s, e))

    print(f"{BUILDER_VERSION} 자체 점검 (DB 없음)")
    _check_hash_stability()
    _check_leakage()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        # 점검용 export 는 임시 폴더에 쓰되 상대경로 계산이 성립해야 한다
        _tmp_root = Path(td)
        _orig = DATA_ROOT
        DATA_ROOT = _tmp_root / "quant-data"
        try:
            _check_partition_roundtrip(DATA_ROOT)
        finally:
            DATA_ROOT = _orig
    print("PIT Dataset 3개 영역 통과. 빌드는 --build")
