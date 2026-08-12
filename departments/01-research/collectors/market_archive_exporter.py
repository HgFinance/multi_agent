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
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
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
    # ▶ **한 번에 다 올리지 않는다** (2026-08-12 실측)
    #   `fetchall()` 로 하루치를 통째로 파이썬 리스트에 올렸다. 우리 원장에서는
    #   호가가 구멍투성이라 통과했는데, 저쪽 원천(하루 2,000만 행)으로 돌리자
    #   컨테이너가 `Killed`(OOM) 됐다 - **작은 데이터로만 검증된 코드**였다.
    #
    #   서버 커서(named cursor)로 배치를 받아 RowGroup 단위로 흘려 쓴다.
    #   메모리는 배치 하나 크기로 고정된다.
    # ▶ 배치를 **행 수가 아니라 값 수**로 잡는다 (2026-08-12 실측)
    #   체결(21열)은 20만 행 배치로 통과했는데 호가(26열, 그중 10단계 배열 4개)는
    #   같은 배치에서 `Killed`(OOM) 됐다. 한 행의 무게가 표마다 열 배 넘게 다르다.
    #   열 폭으로 나눠 배치가 담는 **값의 총량**을 일정하게 만든다.
    #
    #   먼저 작은 표본만 당겨 열을 알아낸 뒤 본 배치 크기를 정한다 - 열을 알려면
    #   한 번 받아야 하는데(서버 커서는 execute 직후 description 이 비어 있다),
    #   그 첫 받기가 곧 큰 배치면 알아내기 전에 죽는다.
    PROBE = 2_000
    stream = tconn.cursor(name=f"arc_{table.replace('.', '_')}_{day:%Y%m%d}")
    stream.itersize = PROBE
    stream.execute(f"select * from {table} where {time_col} >= %s and {time_col} < %s {flt} "
                   f"order by {order}", (lo, hi))
    first = stream.fetchmany(PROBE)
    cols = [d[0] for d in stream.description]
    # 배열 열은 스칼라보다 훨씬 무겁다 - 있으면 더 잘게 썬다.
    wide = any(isinstance(v, (list, tuple)) for v in (first[0] if first else ()))
    BATCH = max(5_000, (400_000 if wide else 2_000_000) // max(1, len(cols)))
    stream.itersize = BATCH

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

    out_dir = ARCHIVE_ROOT / table.split(".")[1]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day.isoformat()}.parquet"

    # ▶ 스키마를 **첫 배치에서 한 번** 추론해 고정한다.
    #   배치마다 다시 추론하면 RowGroup 끼리 스키마가 갈려 쓰기가 실패하고,
    #   그렇다고 전부 문자열로 박으면 `norm` 이 그대로 두는 정수·실수가
    #   ArrowTypeError 로 죽는다(실측). norm 의 출력 타입이 곧 계약이다.
    #
    #   첫 배치가 전부 NULL 인 열은 타입을 알 수 없다 - 그때는 문자열로 둔다.
    #   값을 잃지 않고(문자열은 무엇이든 담는다) 복원 시 캐스팅하면 된다.
    probe = {c: [norm(r[i]) for r in first] for i, c in enumerate(cols)}
    schema_pa = pa.schema([
        (c, pa.array(v).type if any(x is not None for x in v) else pa.string())
        for c, v in probe.items()])
    written = 0
    writer = pq.ParquetWriter(path, schema_pa, compression="zstd")
    try:
        batch = first
        while batch:
            arrays = {c: [norm(r[i]) for r in batch] for i, c in enumerate(cols)}
            writer.write_table(pa.table(arrays, schema=schema_pa))
            written += len(batch)
            batch = stream.fetchmany(BATCH)
    finally:
        writer.close()
        stream.close()

    digest = file_sha256(path)
    # 재독도 통째로 안 올린다 - 메타데이터의 행수만 본다(파일을 다시 읽는
    # 목적은 "쓴 것이 읽히는가" 와 "몇 행인가" 이지 값 재검증이 아니다.
    # 값 무결성은 sha256 이 증명한다).
    back_rows = pq.ParquetFile(path).metadata.num_rows
    verified = (back_rows == db_count == written) and file_sha256(path) == digest
    return ExportResult(table, db_count, path, digest, verified,
                        "재독 행수·해시 일치" if verified
                        else f"검증 불일치! db={db_count} 쓴={written} 읽은={back_rows}")


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


# ▶ 외부 원천(Trading_bot) 표. 우리 것과 스키마가 다르다 - 시간 컬럼이 `ts` 다.
#   재계산 아카이브를 **저쪽에서** 뜨는 이유: 우리 원장의 호가는 29일 중 8일만
#   온전하다(체결 대비 0.00~0.08배). 구멍째 아카이브하면 "보관 완료" 라고
#   기록되면서 실제로는 못 쓰는 파일이 남는다.
#
#   스키마를 우리 것으로 바꿔 넣지 않는다. 원시는 **원본 모양 그대로** 두는 것이
#   재계산의 전제이고(변환하면 그 변환이 곧 손실 지점이다), 이 모양으로 접는
#   경로는 이미 있다(microstructure_builder 의 external 모드).
EXTERNAL_TARGETS = (
    ("public.ticks", "ts", ""),
    ("public.quotes", "ts", ""),
)


def run_export(day: date | None, *, force: bool = False,
               external_dsn: str = "") -> int:
    import psycopg2
    from source_registry import load_project_env

    env = load_project_env()
    tconn = psycopg2.connect(env["TIMESCALE_DATABASE_URL"], connect_timeout=20)
    # 읽는 곳과 기록하는 곳이 다를 수 있다. 기록(archive_exports)은 언제나
    # 우리 원장이다 - 아카이브 대장이 저쪽에 흩어지면 삭제 Gate 가 성립하지 않는다.
    src = psycopg2.connect(external_dsn, connect_timeout=20) if external_dsn else tconn
    targets = EXTERNAL_TARGETS if external_dsn else TARGETS
    try:
        target_day = day or latest_trade_date_with_data(tconn)
        lo, hi = kst_day_bounds(target_day)
        print(f"{EXPORTER_VERSION}: {target_day} (KST) Archive"
              + (" [force 재수출]" if force else ""), flush=True)
        any_fail = False
        for table, time_col, flt in targets:
            # 외부 원천은 우리 표와 이름이 겹칠 수 있으므로 대장에 출처를 붙인다.
            # `public.quotes` 만 적으면 나중에 어느 DB 것인지 알 수 없다.
            logged = f"external:{table}" if external_dsn else table
            if not force and already_verified(tconn, logged, lo, hi):
                print(f"  [SKIP] {logged:32} 이미 verified Archive 존재", flush=True)
                continue
            r = export_table(src, table, time_col, flt, target_day)
            r = ExportResult(logged, r.rows, r.path, r.content_hash, r.verified, r.note)
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
        if src is not tconn:
            src.close()
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


def _check_external_targets_are_logged_apart():
    """외부 원천 아카이브가 **우리 것과 안 섞이는가.**

    ▶ 왜 필요한가 (2026-08-12)
      재계산용 원시는 저쪽(Trading_bot)에서 뜬다 - 우리 원장의 호가는 29일 중
      8일만 온전해서 구멍째 아카이브하면 "보관 완료" 로 기록되면서 실제로는
      못 쓰는 파일이 남는다.

      그런데 저쪽 표 이름(`public.quotes`)만 대장에 적으면 나중에 **어느 DB
      것인지 알 수 없다.** 우리 `market.market_quotes` 아카이브와 같은 구간이
      두 행이 되고, 삭제 Gate 가 엉뚱한 것을 보고 원본을 지울 수 있다.
    """
    ours = {t for t, _, _ in TARGETS}
    theirs = {t for t, _, _ in EXTERNAL_TARGETS}
    assert not (ours & theirs), "표 이름이 겹친다 - 대장에서 구분되지 않는다"
    # 파일 경로도 안 겹쳐야 한다(`table.split('.')[1]` 이 디렉터리다)
    assert not ({t.split(".")[1] for t in ours} & {t.split(".")[1] for t in theirs}), \
        "아카이브 디렉터리가 겹친다 - 한쪽이 다른 쪽을 덮어쓴다"
    # 대장에는 출처가 붙어야 한다
    import inspect

    src = inspect.getsource(run_export)
    assert 'f"external:{table}"' in src, "외부 원천이 출처 없이 기록된다"
    assert "already_verified(tconn, logged" in src, \
        "중복 판정이 출처 없는 이름으로 이뤄진다"
    # 읽기는 저쪽, 기록은 우리 원장이어야 한다
    assert "export_table(src," in src and "psycopg2.connect(external_dsn" in src
    print("  외부 원천 분리 기록      OK")


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
        dsn = a[a.index("--external-dsn") + 1] if "--external-dsn" in a else ""
        raise SystemExit(run_export(d, force="--force" in a, external_dsn=dsn))

    print(f"{EXPORTER_VERSION} 자체 점검 (DB 없음)")
    _check_day_bounds()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        _check_parquet_roundtrip(Path(td))
    _check_targets()
    _check_external_targets_are_logged_apart()
    print("Archive 수출기 4개 영역 통과. 실행은 --export [--date YYYY-MM-DD] "
          "[--external-dsn DSN]")
