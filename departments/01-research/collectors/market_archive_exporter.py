#!/usr/bin/env python3
"""Raw Market Data -> 검증된 Parquet Archive (거래일 단위).

소유: 재일 (리서치본부)
근거: timescaledb/migrations/001 (market.archive_exports - exported/verified/
      manifest_signed 3단 Gate, retention_registry 의 "Archive 검증 전 삭제 금지"),
      TEAM_JAEIL 가이드 DoD "Raw Market Data가 검증된 Parquet로 Archive된다"

▶ 설계
  - 대상: 거래일 하나(KST)의 ticks / quotes / bars(1D·1M) / breadth /
    derivative_snapshots. 행이 0인 테이블은 Archive 를 만들지 않고 보고만
    한다(빈 파일을 '보관 완료'로 위장하지 않는다).
  - 검증(verified)의 정의: ① 내보낸 Parquet 를 다시 읽어 행수가 DB 원본
    count 와 일치 ② 파일 sha256 이 기록값과 일치. 둘 다 통과해야
    verified=true 다.
  - manifest_signed 는 **false 로 둔다** - 서명 키 체계가 아직 없다.
    서명 없이 signed=true 로 적는 것이 정확히 이 컬럼이 막으려는 거짓이다.
    (retention 삭제 Gate 는 signed 까지 요구하므로 삭제는 여전히 잠겨 있다)
  - 저장: market-archive/<table>/<YYYY-MM-DD>.parquet (git 제외).
    등록: archive_exports 는 (table, 구간) unique 라 **구간당 1행**이 계약이다.
    이미 verified 면 스킵, --force 재수출은 같은 행을 갱신한다(재수출 흔적은
    metadata 에 남는다). 실측 2026-07-31: jsonb 직렬화 결함(str(dict))을
    복구 드릴이 적발해 force 재수출로 교정한 것이 이 경로의 첫 사용이다.

사용
  python collectors/market_archive_exporter.py                 # 자체 점검
  python collectors/market_archive_exporter.py --export        # 최근 거래일
  python collectors/market_archive_exporter.py --export --date 2026-07-31
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

EXPORTER_VERSION = "research-market-archive-v1"
KST = timezone(timedelta(hours=9))
SCHEMA_VERSION = 1
ARCHIVE_ROOT = Path(__file__).resolve().parents[3] / "market-archive"

# (source_table, 시간 컬럼, 추가 필터)
TARGETS = (
    ("market.market_ticks", "event_time", ""),
    ("market.market_quotes", "event_time", ""),
    ("market.market_bars", "bucket_time", ""),
    ("market.market_breadth", "event_time", ""),
    ("market.derivative_snapshots", "event_time", ""),
)


def kst_day_bounds(d: date) -> tuple[datetime, datetime]:
    lo = datetime.combine(d, dtime.min, tzinfo=KST)
    return lo, lo + timedelta(days=1)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class ExportResult:
    table: str
    rows: int
    path: Path | None
    content_hash: str | None
    verified: bool
    note: str


def export_table(tconn, table: str, time_col: str, flt: str, day: date) -> ExportResult:
    import pyarrow as pa
    import pyarrow.parquet as pq

    lo, hi = kst_day_bounds(day)
    with tconn.cursor() as cur:
        cur.execute(f"select count(*) from {table} where {time_col} >= %s and {time_col} < %s {flt}",
                    (lo, hi))
        db_count = cur.fetchone()[0]
        if db_count == 0:
            return ExportResult(table, 0, None, None, False, "행 0 - Archive 생략")

        # 정렬 2차 키는 테이블마다 다르다 (bars 엔 source_event_id 가 없다 -
        # PK 가 instrument/interval/source 조합. 실측 2026-07-31)
        schema, name = table.split(".")
        cur.execute("select column_name from information_schema.columns "
                    "where table_schema=%s and table_name=%s", (schema, name))
        colset = {r[0] for r in cur.fetchall()}
        tiebreak = next((c for c in ("source_event_id", "instrument_id", "market")
                         if c in colset), None)
        order = f"{time_col}, {tiebreak}" if tiebreak else time_col
        cur.execute(f"select * from {table} where {time_col} >= %s and {time_col} < %s {flt} "
                    f"order by {order}", (lo, hi))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()

    # Decimal/UUID/jsonb 는 문자열로 고정한다 - Parquet 타입 협상에 값을 잃지
    # 않고, 복원 시 ::numeric/::uuid/::jsonb 캐스팅으로 되돌릴 수 있다.
    def norm(v):
        if v is None or isinstance(v, (int, float, str, bool)):
            return v
        if isinstance(v, datetime):
            return v.astimezone(timezone.utc).isoformat()
        if isinstance(v, (dict, list)):
            # jsonb 는 json.dumps 로 - str(dict) 는 작은따옴표 repr 이라
            # 복원 시 ::jsonb 캐스팅이 깨진다 (복구 드릴이 실제로 적발한 결함).
            # 내부에 Decimal 이 섞여 올 수 있어 default=str (quotes 실측).
            return json.dumps(v, ensure_ascii=False, default=str)
        return str(v)

    arrays = {c: [norm(r[i]) for r in rows] for i, c in enumerate(cols)}
    out_dir = ARCHIVE_ROOT / table.split(".")[1]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day.isoformat()}.parquet"
    pq.write_table(pa.table(arrays), path, compression="zstd")

    digest = file_sha256(path)
    back = pq.read_table(path)
    verified = (back.num_rows == db_count == len(rows)) and file_sha256(path) == digest
    return ExportResult(table, db_count, path, digest, verified,
                        "재독 행수·해시 일치" if verified else "검증 불일치!")


def latest_trade_date_with_data(tconn) -> date:
    with tconn.cursor() as cur:
        cur.execute("select max(event_time at time zone 'Asia/Seoul')::date from market.market_ticks")
        d = cur.fetchone()[0]
    if d is None:
        raise RuntimeError("체결 데이터가 없다 - 대상 거래일을 정할 수 없다")
    return d


def already_verified(tconn, table: str, lo: datetime, hi: datetime) -> bool:
    with tconn.cursor() as cur:
        cur.execute("""
            select 1 from market.archive_exports
            where source_table = %s and partition_start = %s and partition_end = %s
              and verified limit 1
        """, (table, lo, hi))
        return cur.fetchone() is not None


def run_export(day: date | None, *, force: bool = False) -> int:
    import psycopg2

    from source_registry import load_project_env

    env = load_project_env()
    tconn = psycopg2.connect(env["TIMESCALE_DATABASE_URL"], connect_timeout=20)
    try:
        target_day = day or latest_trade_date_with_data(tconn)
        lo, hi = kst_day_bounds(target_day)
        print(f"{EXPORTER_VERSION}: {target_day} (KST) Archive"
              + (" [force 재수출]" if force else ""), flush=True)
        any_fail = False
        for table, time_col, flt in TARGETS:
            if not force and already_verified(tconn, table, lo, hi):
                print(f"  [SKIP] {table:32} 이미 verified Archive 존재", flush=True)
                continue
            r = export_table(tconn, table, time_col, flt, target_day)
            if r.rows == 0:
                print(f"  [ - ] {r.table:32} {r.note}", flush=True)
                continue
            with tconn.cursor() as cur:
                cur.execute("""
                    insert into market.archive_exports
                      (source_table, partition_start, partition_end, object_path,
                       row_count, min_event_time, max_event_time, content_hash,
                       schema_version, exported, verified, manifest_signed,
                       exported_at, verified_at, metadata)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, false,
                            now(), case when %s then now() end, %s::jsonb)
                    on conflict (source_table, partition_start, partition_end)
                    do update set object_path = excluded.object_path,
                                  row_count = excluded.row_count,
                                  content_hash = excluded.content_hash,
                                  exported = excluded.exported,
                                  verified = excluded.verified,
                                  exported_at = excluded.exported_at,
                                  verified_at = excluded.verified_at,
                                  metadata = excluded.metadata
                                             || '{"re_export": "구간당 1행 계약 - 재수출은 갱신"}'::jsonb
                """, (r.table, lo, hi,
                      str(r.path.relative_to(ARCHIVE_ROOT.parent)), r.rows,
                      lo, hi, r.content_hash, SCHEMA_VERSION, r.verified, r.verified,
                      json.dumps({"exporter": EXPORTER_VERSION,
                                  "compression": "zstd",
                                  "signed_note": "서명 키 체계 미도입 - signed 는 "
                                                 "정직하게 false (삭제 Gate 유지)"})))
            tconn.commit()
            mark = "OK " if r.verified else "FAIL"
            size = r.path.stat().st_size / 1e6
            print(f"  [{mark}] {r.table:32} {r.rows:>10,}행 -> {r.path.name} "
                  f"({size:.1f}MB) {r.note}", flush=True)
            any_fail |= not r.verified
        return 1 if any_fail else 0
    finally:
        tconn.close()


# ---------------------------------------------------------------------------
# 자체 점검 - DB 없음
# ---------------------------------------------------------------------------

def _check_day_bounds():
    lo, hi = kst_day_bounds(date(2026, 7, 31))
    assert lo.tzinfo is KST and (hi - lo) == timedelta(days=1)
    assert lo.hour == 0 and hi.date() == date(2026, 8, 1)
    print("  KST 거래일 경계          OK")


def _check_parquet_roundtrip(tmp: Path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    p = tmp / "t.parquet"
    data = {"a": ["1", "2"], "b": [10, 20], "c": [None, "x"]}
    pq.write_table(pa.table(data), p, compression="zstd")
    back = pq.read_table(p)
    assert back.num_rows == 2 and back.column_names == ["a", "b", "c"]
    h1, h2 = file_sha256(p), file_sha256(p)
    assert h1 == h2 and len(h1) == 64
    print("  Parquet 왕복·파일 해시   OK")


def _check_targets():
    assert len({t[0] for t in TARGETS}) == len(TARGETS)
    for t, col, _ in TARGETS:
        assert t.startswith("market.") and col in ("event_time", "bucket_time")
    print("  대상 테이블 정의         OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--export" in sys.argv:
        a = sys.argv
        d = date.fromisoformat(a[a.index("--date") + 1]) if "--date" in a else None
        raise SystemExit(run_export(d, force="--force" in a))

    print(f"{EXPORTER_VERSION} 자체 점검 (DB 없음)")
    _check_day_bounds()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        _check_parquet_roundtrip(Path(td))
    _check_targets()
    print("Archive 수출기 3개 영역 통과. 실행은 --export [--date YYYY-MM-DD]")
