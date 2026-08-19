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
import gc
import gzip
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

BUILDER_VERSION = "quant-pit-dataset-v2"
KST = timezone(timedelta(hours=9))
SCHEMA_VERSION = 1
BAR_SOURCE = "ls_chart"
INTERVAL = "1D"
DATA_ROOT = Path(__file__).resolve().parents[3] / "quant-data"

from stock_universe import (  # noqa: E402
    STOCK_ASSET_SCOPE,
    STOCK_UNIVERSE_VERSION,
    filter_stock_rows,
)


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


def register_universe(conn, instrument_ids: list[str], *, as_of: datetime,
                      stock_audit: dict | None = None) -> str:
    """v1 유니버스 = 이 Dataset 에 봉이 존재하는 종목 전체 (편향 선언 포함)."""
    ids = sorted(set(instrument_ids))
    uhash = hashlib.sha256("\n".join(ids).encode()).hexdigest()
    rules = {
        "builder": BUILDER_VERSION,
        "definition": "market_bars(1D, ls_chart) intersect governed KRX stocks",
        "asset_scope": STOCK_ASSET_SCOPE,
        "product_filter_version": STOCK_UNIVERSE_VERSION,
        "instrument_type": "STOCK",
        "asset_class": "EQUITY",
        "market": "KRX",
        "status": "ACTIVE",
        "stock_filter_audit": dict(stock_audit or {}),
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


# ---------------------------------------------------------------------------
# 행 표현 - 같은 데이터를 더 적은 자리에
# ---------------------------------------------------------------------------
# ▶ **왜 dict 를 그만두는가** (2026-08-14 실측)
#   한 행을 dict 로 들면 1,062B 다(2021-05 파티션 50,434행, tracemalloc 실측).
#   v3 는 7,257,952행이므로 `backtest_runner._load_dataset` 이 데이터셋 하나를
#   **들기만 해도 7.71GB** 다. 컨테이너 50개가 도는 32GB 호스트에서 그만한
#   덩어리는 자주 못 얻는다 - 그때 나는 것이 `OSError: [Errno 12] Cannot
#   allocate memory` 이고, 원장에 최근 1일 14시도/가설 10건으로 남아 있었다
#   (카드 t_ad5f27e1 재집계 기준 그 시점 최다 낭비).
#
#   실패가 아니라 **자리 부족**이라 재시도로는 안 풀린다. 이미 `Market` 을
#   만든 뒤 원본 행을 놓는 수(backtest_runner:1370 `del rows`)는 들어가 있고,
#   그래도 죽는다 - 놓기 **전에** 한 번은 다 들어야 하기 때문이다. 그러니
#   줄일 것은 보유 시간이 아니라 행 하나의 크기다.
#
#   dict 는 행마다 해시표를 한 벌씩 더 든다. 그런데 열 이름은 이미 COLUMNS 로
#   고정돼 있다 - 그 표는 행마다가 아니라 **클래스가 한 번** 들면 된다.
#
# ▶ 값 자체도 되풀이다
#   한 파티션 안에서 거래일은 20여 종, 종목은 2천여 종, 관측시각은 손에 꼽고,
#   가격 문자열도 호가 단위로 양자화돼 있어 많이 겹친다. 같은 값을 같은 객체로
#   들면(interning) 나머지가 줄어든다. str·date·datetime·Decimal 은 전부
#   불변이라 공유가 안전하다.
#
# ▶ **바꾸지 않는 것**: 값도 타입도 그대로다. 그래서 content_hash 가 한 비트도
#   안 바뀐다 - 이 파일 위쪽의 "Dataset 은 불변이다" 계약을 건드리지 않는다는
#   뜻이고, 이미 등재된 매니페스트가 전부 그대로 검증된다. 소비자도 dict 로
#   읽던 그대로 읽는다(`r["close"]`, `r.get("notional")`).
ROW_BYTES_BUDGET = 600
"""행 하나가 넘으면 안 되는 자리(바이트). dict 시절은 1,062B 였다.

이 숫자를 올리는 것은 곧 7.26M 행짜리 데이터셋의 적재 비용을 올리는 것이다 -
올리려면 위 실측을 다시 하고 왜 올려도 되는지 같이 적어라."""


class BarRow:
    """일봉 한 행. **dict 처럼 읽히지만 dict 가 아니다.**

    지원하는 것은 소비자가 실제로 쓰는 읽기 규약이다 - `r[k]`, `r.get(k)`,
    `k in r`, `keys/values/items`, 순회, dict 와의 동등 비교. 쓰기(`r[k]=v`)도
    받되 **열에 없는 이름은 거절한다**: dict 였다면 새 열이 조용히 생기고
    content_hash 는 그것을 못 보므로, 데이터가 달라졌는데 같은 해시가 된다.
    """

    __slots__ = COLUMNS

    def __init__(self, instrument_id, trade_date, open, high, low, close,
                 volume, notional, observed_at):
        self.instrument_id = instrument_id
        self.trade_date = trade_date
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.notional = notional
        self.observed_at = observed_at

    # ── dict 읽기 규약 ─────────────────────────────────────────────────
    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except (AttributeError, TypeError):
            raise KeyError(key) from None

    def get(self, key, default=None):
        try:
            return getattr(self, key, default)
        except TypeError:            # 문자열이 아닌 키 = dict 에서도 없는 키다
            return default

    def __setitem__(self, key, value):
        if key not in COLUMNS:
            raise KeyError(
                f"{key!r} 는 이 스키마의 열이 아니다 - 열을 늘리려면 COLUMNS 를 "
                f"바꿔라(열 순서가 content_hash 의 일부라 새 스키마가 된다)")
        setattr(self, key, value)

    def __contains__(self, key):
        return key in COLUMNS

    def keys(self):
        return COLUMNS

    def values(self):
        return tuple(getattr(self, k) for k in COLUMNS)

    def items(self):
        return tuple((k, getattr(self, k)) for k in COLUMNS)

    def __iter__(self):
        return iter(COLUMNS)

    def __len__(self):
        return len(COLUMNS)

    def __eq__(self, other):
        if isinstance(other, BarRow):
            return self.values() == other.values()
        if isinstance(other, dict):
            return dict(self.items()) == other
        return NotImplemented

    def __repr__(self):
        return f"BarRow({dict(self.items())!r})"


def _shared(cache: dict, raw: str, make):
    """같은 문자열이면 **같은 객체**를 준다. 값은 안 바꾸고 만드는 횟수만 줄인다.

    캐시는 호출 하나 안에서만 살아 파티션을 넘겨 자라지 않는다.
    """
    got = cache.get(raw)
    if got is None:
        got = cache[raw] = make(raw)
    return got


def load_partition(path: Path) -> list[BarRow]:
    """CSV.gz -> 행 (빌드 시와 같은 타입으로 복원 - 해시 재검증용).

    dict 대신 `BarRow` 를 돌려준다. **값도 타입도 읽는 법도 같고**, 다른 것은
    한 행이 차지하는 자리뿐이다(위 설명 참고).
    """
    rows = []
    ids: dict = {}
    days: dict = {}
    stamps: dict = {}
    nums: dict = {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        for rec in csv.DictReader(f):
            # 옛 파티션엔 이 열이 없다 - 빈 값은 None(거래대금 0 과 구분한다)
            raw_n = (rec.get("notional") or "").strip()
            rows.append(BarRow(
                _shared(ids, rec["instrument_id"], str),
                _shared(days, rec["trade_date"], date.fromisoformat),
                _shared(nums, rec["open"], Decimal),
                _shared(nums, rec["high"], Decimal),
                _shared(nums, rec["low"], Decimal),
                _shared(nums, rec["close"], Decimal),
                _shared(nums, rec["volume"], Decimal),
                _shared(nums, raw_n, Decimal) if raw_n else None,
                _shared(stamps, rec["observed_at"], datetime.fromisoformat),
            ))
    return rows


def build(name: str, version: str, start: date, end: date) -> int:
    import psycopg2

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "01-research" / "collectors"))
    from db_writer import connect as connect_writer
    from source_registry import load_project_env

    env = load_project_env()
    as_of = datetime.now(timezone.utc)
    conn = connect_writer(
        env["DATABASE_URL"],
        connect_timeout=20,
        runtime_role="svc_dataset_builder",
    )
    tconn = psycopg2.connect(env["TIMESCALE_DATABASE_URL"], connect_timeout=20)
    try:
        rows = fetch_bars(tconn, start, end)
        if not rows:
            print("봉이 0행이다 - 기간·소스 확인", flush=True)
            return 1
        rows, stock_audit = filter_stock_rows(conn, rows)
        if not rows:
            print("KRX ACTIVE STOCK filter left zero daily bars", flush=True)
            return 1
        days = fetch_trading_days(conn, start, end)
        report = run_leakage_checks(rows, as_of=as_of, trading_days=days,
                                    survivorship_declared=True)
        if report.overall == "FAIL":
            print(f"Leakage Check FAIL - 등록 거부: "
                  f"{json.dumps(report.checks, ensure_ascii=False, default=str)[:400]}",
                  flush=True)
            return 1

        uid = register_universe(
            conn, [str(r["instrument_id"]) for r in rows], as_of=as_of,
            stock_audit=stock_audit)
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
        quality = {"leakage_overall": report.overall, "checks": report.checks,
                   "stock_universe": stock_audit}
        schema_def = {"columns": list(COLUMNS), "interval": INTERVAL,
                      "bar_source": BAR_SOURCE, "hash": "sha256(sorted rows)",
                      "asset_scope": STOCK_ASSET_SCOPE,
                      "product_filter_version": STOCK_UNIVERSE_VERSION}

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


def _check_row_reads_like_a_dict_but_is_not_one():
    """**행이 dict 가 아니어도 소비자가 읽던 그대로 읽힌다.**

    이 검사가 없으면 자리를 줄이려다 소비자를 소리 없이 깬다. 실제로 읽는
    쪽은 `Market.from_rows`(`r["open"]`, `r.get("notional")`),
    `content_hash`/`row_line`(`row["..."]`, `row.get("notional")`),
    `attach_micro`(`r.get(...)`) 이고 - 여기서 그 접근을 전부 dict 와 맞대
    본다. 마지막 두 줄이 핵심이다: **같은 행을 dict 로 든 것과 해시가 같아야
    한다.** 다르면 이미 등재된 매니페스트가 전부 깨진 것이다.
    """
    d = {"instrument_id": "A005930", "trade_date": date(2026, 7, 1),
         "open": Decimal("70000"), "high": Decimal("71000"),
         "low": Decimal("69500"), "close": Decimal("70500"),
         "volume": Decimal("1234567"), "notional": Decimal("87000000000"),
         "observed_at": datetime(2026, 7, 1, 15, 30, tzinfo=timezone.utc)}
    r = BarRow(**d)

    # 자리: 클래스가 열 이름을 들고 행은 값만 든다
    assert not hasattr(r, "__dict__"), "슬롯이 아니면 자리를 안 줄인다"
    assert BarRow.__slots__ == COLUMNS, "행의 모양은 스키마가 정한다"
    assert sys.getsizeof(r) * 2 < sys.getsizeof(d), \
        (sys.getsizeof(r), sys.getsizeof(d))

    # 읽기: dict 와 같은 답
    for k in COLUMNS:
        assert r[k] == d[k], k
        assert r.get(k) == d[k], k
        assert k in r, k
    assert tuple(r.keys()) == COLUMNS
    assert dict(r.items()) == d
    assert list(r) == list(COLUMNS)
    assert len(r) == len(COLUMNS)
    assert r == d and d == r, "dict 와 같은 값이면 같다고 답해야 한다"

    # 없는 열: dict 와 같은 방식으로 없다고 답한다
    assert r.get("nope") is None
    assert r.get("nope", 7) == 7
    assert r.get(3) is None, "문자열이 아닌 키도 그냥 없는 키다"
    try:
        r["nope"]
    except KeyError:
        pass
    else:
        raise AssertionError("없는 열을 읽었는데 KeyError 가 안 났다")

    # 쓰기: 있는 열은 받고, 없는 열은 거절한다(조용히 새 열이 생기면 안 된다)
    r["close"] = Decimal("70600")
    assert r["close"] == Decimal("70600")
    try:
        r["surprise"] = 1
    except KeyError:
        pass
    else:
        raise AssertionError("스키마에 없는 열이 조용히 생겼다 - 그 값은 "
                             "content_hash 에 안 들어가므로 데이터가 달라도 "
                             "같은 해시가 된다")
    r["close"] = d["close"]

    # **해시 동일성** - 자리를 줄였다고 데이터가 달라지면 안 된다
    assert row_line(r) == row_line(d)
    assert content_hash([r]) == content_hash([d])
    # 거래대금 없는 옛 행도 같은 답(빈 문자열로 정규화되는 자리)
    d2 = dict(d, notional=None)
    assert row_line(BarRow(**d2)) == row_line(d2)
    print("  행은 dict 처럼 읽힌다    OK")


def _check_loader_holds_a_row_in_less_than_the_budget():
    """**행 하나가 예산 안에 들어온다** (2026-08-14 실측이 만든 검사).

    dict 시절 1,062B x 7,257,952행 = 7.71GB 를 `_load_dataset` 이 한 번에
    들었고, 그 자리를 못 얻어 실험이 `OSError: [Errno 12]` 로 죽었다. 여기서
    재는 것은 그 비용 자체다 - 진짜 파티션을 하나 만들어 읽고, 읽은 뒤에도
    남아 있는(=행이 붙들고 있는) 바이트를 행수로 나눈다.

    같이 고정하는 것: **되풀이되는 값은 한 벌만 만든다.** 날짜·종목·시각이
    행마다 새 객체면 예산은 다시 넘는다.
    """
    import tempfile
    import tracemalloc

    global DATA_ROOT
    n_sym, n_day = 200, 100                      # 20,000 행
    base = date(2026, 1, 5)
    obs = datetime(2026, 7, 31, 6, 0, tzinfo=timezone.utc)
    rows = []
    for s in range(n_sym):
        for i in range(n_day):
            px = Decimal(10000 + (s * 37 + i * 11) % 5000)
            rows.append({
                "instrument_id": f"A{s:06d}",
                "trade_date": base + timedelta(days=i),
                "open": px, "high": px + 100, "low": px - 100, "close": px,
                "volume": Decimal((s * 13 + i) % 9999 + 1),
                "notional": px * Decimal((s * 13 + i) % 9999 + 1),
                "observed_at": obs,
            })
    assert len(rows) == n_sym * n_day

    orig_root = DATA_ROOT
    with tempfile.TemporaryDirectory() as td:
        DATA_ROOT = Path(td) / "quant-data"
        try:
            parts = export_partitions(rows, DATA_ROOT / "budget-v1")
            paths = [resolve_object_path(p["object_path"]) for p in parts]
            hashes = [p["content_hash"] for p in parts]

            gc.collect()
            tracemalloc.start()
            start = tracemalloc.get_traced_memory()[0]
            loaded = [r for p in paths for r in load_partition(p)]
            held = tracemalloc.get_traced_memory()[0] - start
            tracemalloc.stop()
            # **줄인 것은 자리뿐이다** - 파티션 해시는 그대로여야 한다.
            # (임시 폴더가 살아 있는 동안 확인한다)
            for p, h in zip(paths, hashes):
                assert content_hash(load_partition(p)) == h, p.name
        finally:
            DATA_ROOT = orig_root

    per_row = held / len(loaded)
    assert len(loaded) == len(rows), (len(loaded), len(rows))
    assert per_row <= ROW_BYTES_BUDGET, (
        f"행 하나가 {per_row:.0f}B 다 - 예산 {ROW_BYTES_BUDGET}B 를 넘었다. "
        f"7,257,952행짜리 v3 로 치면 {per_row * 7257952 / 1e9:.2f}GB 이고, "
        f"그 자리를 못 얻으면 실험이 OSError:[Errno 12] 로 죽는다")

    # 되풀이 값은 한 벌만 - 아니면 예산은 곧 다시 넘는다
    same_day = [r for r in loaded if r["trade_date"] == base]
    assert len(same_day) == n_sym
    assert len({id(r["trade_date"]) for r in same_day}) == 1, \
        "같은 거래일을 행마다 새로 만들고 있다"
    assert len({id(r["observed_at"]) for r in loaded[:1000]}) == 1, \
        "같은 관측시각을 행마다 새로 만들고 있다"

    # 전체 해시도 그대로다 - 매니페스트 등재값이 여기서 나온다
    assert content_hash(loaded) == content_hash(rows), (
        "행 표현을 바꿨더니 데이터셋 해시가 달라졌다 - 등재된 매니페스트가 "
        "전부 깨진다")
    print(f"  행 자리 {per_row:.0f}B/행       OK")


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
    _check_row_reads_like_a_dict_but_is_not_one()
    _check_loader_holds_a_row_in_less_than_the_budget()
    print("PIT Dataset 5개 영역 통과. 빌드는 --build")
