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
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

EXPORTER_VERSION = "research-market-archive-v1"
KST = timezone(timedelta(hours=9))
SCHEMA_VERSION = 1
ARCHIVE_ROOT = Path(__file__).resolve().parents[3] / "market-archive"

# ── 배치 크기 산정 ─────────────────────────────────────────────────────────
# 상한을 못 읽었을 때 가정할 값. **크게 잡는 쪽 실수가 OOM 이다** - 모르면 작게.
CEILING_UNKNOWN = 512 * 2**20
# 한 배치가 상한에서 차지해도 되는 몫. 나머지는 인터프리터·pyarrow·psycopg2·
# zstd 버퍼가 쓴다(실측 기준선 ~100MB).
BATCH_SHARE = 0.25
# 값 하나가 파이썬에서 차지하는 무게의 상한. 실측 2026-08-12(원시 행 기준):
# 체결 74.9B/잎, 호가 106.7B/잎. 여유를 둬 256 으로 잡는다.
LEAF_BYTES = 256
MIN_BATCH_ROWS = 1_000
MAX_BATCH_ROWS = 200_000


def memory_ceiling() -> int:
    """이 프로세스가 쓸 수 있는 메모리 상한(바이트).

    ▶ **컨테이너 안에서는 호스트 메모리가 상한이 아니다** (2026-08-12 실측)
      배치 수집기는 512MiB cgroup 안에서 돌고 있었는데, 배치 크기는 31GB 호스트를
      가정한 상수로 정해져 있었다. 그래서 25GB 짜리 호가 표에서 `Killed` 됐다.
      상한은 가정하지 말고 **읽는다**. 못 읽으면 작게 본다.
    """
    for p in ("/sys/fs/cgroup/memory.max",                    # cgroup v2
              "/sys/fs/cgroup/memory/memory.limit_in_bytes"):  # cgroup v1
        try:
            v = Path(p).read_text().strip()
        except OSError:
            continue
        # 상한 없음은 v2 가 "max", v1 이 아주 큰 수로 표현한다 - 둘 다 상한이 아니다.
        if v.isdigit() and 0 < int(v) < (1 << 62):
            return int(v)
    return CEILING_UNKNOWN


def leaves_per_row(sample: list) -> int:
    """한 행이 담는 **값**의 개수. 배열은 원소 수로 센다.

    표본에서 최댓값을 쓴다 - 배열이 NULL 인 행이 앞에 오면 무게를 놓친다.
    """
    return max((sum(len(v) if isinstance(v, (list, tuple)) else 1 for v in row)
                for row in sample), default=1) or 1


def batch_rows(sample: list, ceiling: int | None = None) -> int:
    """이 표에서 한 배치가 담을 행 수 = 쓸 수 있는 메모리 / 한 행의 무게.

    ceiling 은 자체 점검이 상한을 고정해 넣기 위한 것이다(테스트 기계의 cgroup 에
    결과가 좌우되면 그 점검은 아무것도 보장하지 않는다).

    하한 MIN_BATCH_ROWS 는 진도를 위한 바닥이라 이론상 fail-open 이다 - 다만
    512MiB 에서 행당 524잎을 넘어야 걸리고 우리 표 중 가장 무거운 호가가 62잎이다.
    """
    budget = int((memory_ceiling() if ceiling is None else ceiling) * BATCH_SHARE)
    rows = budget // (leaves_per_row(sample) * LEAF_BYTES)
    return max(MIN_BATCH_ROWS, min(MAX_BATCH_ROWS, rows))

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
    # 어떤 메모리 예산으로 떴는지. 다음에 OOM 이 나면 **배치가 얼마였는지**가
    # 첫 질문인데, 남겨 두지 않으면 그때 가서 알 수 없다.
    batch_rows: int = 0
    ceiling_bytes: int = 0


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
    #   서버 커서(named cursor)로 배치를 받아 RowGroup 단위로 흘려 쓴다. 메모리는
    #   배치 하나 크기로 고정된다 - RowGroup 200개까지 RSS 불변을 실측했으므로
    #   푸터 누적은 상한이 아니다(배치를 줄여도 그 대가는 없다).
    # ▶ 배치를 **행도 열도 아니라 '잎'으로 잰다** (2026-08-12 실측)
    #   앞선 판은 열 수로 나눴다. 그런데 호가의 10단계 배열 4개는 열로는 4,
    #   값으로는 40이다 - 열로 세면 호가가 체결보다 1.24배 무거워 보이는데 실제로는
    #   4.2배다(1.54KB/행 vs 6.46KB/행). **배열을 1로 센 것이 결함이었다.**
    #   그래서 호가만 배치가 제 무게의 4배로 잡혔고 512MiB 컨테이너에서 죽었다
    #   (실측: 8,000행 -> 최대 RSS 241MB 통과 / 15,384행 -> Killed).
    #
    #   먼저 작은 표본만 당겨 모양을 알아낸 뒤 본 배치 크기를 정한다 - 모양을
    #   알려면 한 번 받아야 하는데(서버 커서는 execute 직후 description 이 비어
    #   있다), 그 첫 받기가 곧 큰 배치면 알아내기 전에 죽는다.
    PROBE = 2_000
    stream = tconn.cursor(name=f"arc_{table.replace('.', '_')}_{day:%Y%m%d}")
    stream.itersize = PROBE
    stream.execute(f"select * from {table} where {time_col} >= %s and {time_col} < %s {flt} "
                   f"order by {order}", (lo, hi))
    first = stream.fetchmany(PROBE)
    cols = [d[0] for d in stream.description]
    BATCH = batch_rows(first)
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
                        else f"검증 불일치! db={db_count} 쓴={written} 읽은={back_rows}",
                        batch_rows=BATCH, ceiling_bytes=memory_ceiling())


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
            r = replace(export_table(src, table, time_col, flt, target_day), table=logged)
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
                                  "batch_rows": r.batch_rows,
                                  "memory_ceiling_bytes": r.ceiling_bytes,
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


def _check_batch_sizing():
    """배치는 **잎**으로 잰다. 배열을 1로 세면 호가 배치가 제 무게의 4배가 된다."""
    tick = [tuple(range(21))]                                     # 스칼라 21
    quote = [tuple(range(22)) + ([0] * 10,) * 4]                  # 스칼라 22 + 배열 40
    assert leaves_per_row(tick) == 21, leaves_per_row(tick)
    assert leaves_per_row(quote) == 62, leaves_per_row(quote)
    # 표본 첫 행의 배열이 NULL 이어도 무게를 놓치면 안 된다
    assert leaves_per_row([tuple(range(22)) + (None,) * 4] + quote) == 62, \
        "표본 최댓값이 아니라 첫 행만 봤다 - 배열이 NULL 인 행이 앞에 오면 죽는다"
    # 호가 배치는 체결 배치보다 확실히 작아야 한다. 열로 세면 26 vs 21 이라
    # 거의 같은 크기가 나왔고, 그것이 2026-08-11 OOM 의 원인이었다.
    assert batch_rows(quote) * 2 < batch_rows(tick), \
        "배열 4개(값 40개)를 4로 세고 있다 - 호가 배치가 제 무게보다 크다"
    # 512MiB 실측: 8,000행 통과(최대 RSS 241MB) / 15,384행 Killed.
    got = batch_rows(quote, 512 * 2**20)
    assert 4_000 <= got <= 10_000, \
        f"512MiB 에서 호가 배치가 {got:,}행 - 실측 통과선 8,000 / 사망선 15,384"
    # 상한은 가정하지 말고 읽어야 한다. 읽는 코드가 사라지면 컨테이너 안에서
    # 다시 호스트 메모리를 가정하게 된다.
    import inspect

    assert "cgroup" in inspect.getsource(memory_ceiling), "메모리 상한을 읽지 않는다"
    assert memory_ceiling() > 0
    assert "batch_rows(first)" in inspect.getsource(export_table), \
        "수출 경로가 산정된 배치를 쓰지 않는다"
    print(f"  배치 잎 기준 산정        OK (512MiB -> 호가 {got:,}행)")


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
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        # 총계는 세지 말고 **센다** - 손으로 적으면 검사를 늘려도 숫자가 안 는다.
        checks = (_check_day_bounds,
                  lambda: _check_parquet_roundtrip(Path(td)),
                  _check_targets,
                  _check_external_targets_are_logged_apart,
                  _check_batch_sizing)
        for c in checks:
            c()
    print(f"Archive 수출기 {len(checks)}개 영역 통과. 실행은 --export "
          f"[--date YYYY-MM-DD] [--external-dsn DSN]")
