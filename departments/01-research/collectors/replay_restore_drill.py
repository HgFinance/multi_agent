#!/usr/bin/env python3
"""Backup 복구 드릴 - Parquet Archive 에서 거래일 하나를 복원해 Replay 가능성을 증명한다.

소유: 재일 (리서치본부)
근거: TEAM_JAEIL 가이드 DoD "Backup에서 거래일 하나를 복구해 Replay할 수 있다",
      market_archive_exporter.py (검증된 Parquet + archive_exports 등록),
      개발 원칙 7 "Replay 환경은 실제 Broker Credential을 가질 수 없다"

▶ 드릴의 정의 - "복구했다"의 증거는 세 가지 대조다
  ① Archive Manifest(행수·sha256)와 파일이 일치
  ② 복원 테이블 행수 = Manifest 행수
  ③ 복원본과 운영 원본의 결정론 지문 일치 - count / min·max event_time /
    sum(price*quantity) / distinct source_event_id 수.
  셋 다 통과해야 성공이다. 복원은 스크래치 스키마(market_replay)에서 하고
  드릴 종료 시 지운다(--keep 이면 보존) - Replay 실험은 이 스키마 사본을
  쓰면 되고, 운영 테이블은 건드리지 않는다.

▶ Broker Credential 격리
  이 드릴은 TIMESCALE_DATABASE_URL 만 쓴다. LS App Key 등 Broker/Vendor
  Credential 은 읽지 않는다 - Replay 원칙 7 의 코드 표현.

사용
  python collectors/replay_restore_drill.py                     # 자체 점검
  python collectors/replay_restore_drill.py --drill --date 2026-07-31
  python collectors/replay_restore_drill.py --drill --date 2026-07-31 --keep
"""
from __future__ import annotations

import csv
import io
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from market_archive_exporter import ARCHIVE_ROOT, file_sha256, kst_day_bounds  # noqa: E402

DRILL_VERSION = "research-replay-drill-v1"
KST = timezone(timedelta(hours=9))
SCRATCH = "market_replay"
DRILL_TABLE = "market_ticks"          # 드릴 대상 - 가장 크고 Replay 의 심장


def fingerprint_sql(schema: str, lo: datetime, hi: datetime) -> str:
    return f"""
        select count(*),
               min(event_time), max(event_time),
               coalesce(sum(price * quantity), 0)::text,
               count(distinct source_event_id)
        from {schema}.{DRILL_TABLE}
        where event_time >= '{lo.isoformat()}' and event_time < '{hi.isoformat()}'
    """


def load_manifest(tcur, lo: datetime, hi: datetime):
    tcur.execute("""
        select object_path, row_count, content_hash
        from market.archive_exports
        where source_table = %s and partition_start = %s and partition_end = %s
          and verified
        order by exported_at desc limit 1
    """, (f"market.{DRILL_TABLE}", lo, hi))
    return tcur.fetchone()


def restore(tcur, parquet_path: Path) -> int:
    import pyarrow.parquet as pq

    tcur.execute(f"drop schema if exists {SCRATCH} cascade")
    tcur.execute(f"create schema {SCRATCH}")
    # 원본과 같은 타입·제약으로 만든다 - 캐스팅이 안 되면 복구가 아니라 눈속임이다
    tcur.execute(f"create table {SCRATCH}.{DRILL_TABLE} "
                 f"(like market.{DRILL_TABLE} including defaults)")

    table = pq.read_table(parquet_path)
    cols = table.column_names
    restored = 0
    for batch in table.to_batches(max_chunksize=100_000):
        buf = io.StringIO()
        w = csv.writer(buf)
        data = batch.to_pydict()
        n = batch.num_rows
        for i in range(n):
            w.writerow(["" if data[c][i] is None else data[c][i] for c in cols])
        buf.seek(0)
        tcur.copy_expert(
            f"copy {SCRATCH}.{DRILL_TABLE} ({', '.join(cols)}) "
            f"from stdin with (format csv, null '')", buf)
        restored += n
    return restored


def run_drill(day: date, keep: bool) -> int:
    import psycopg2

    from source_registry import load_project_env

    env = load_project_env()
    lo, hi = kst_day_bounds(day)
    conn = psycopg2.connect(env["TIMESCALE_DATABASE_URL"], connect_timeout=20)
    try:
        cur = conn.cursor()
        m = load_manifest(cur, lo, hi)
        if m is None:
            print(f"{day} 의 verified ticks Archive 가 없다 - 먼저 "
                  f"market_archive_exporter.py --export", flush=True)
            return 1
        obj_path, m_rows, m_hash = m
        parquet = ARCHIVE_ROOT.parent / obj_path
        print(f"{DRILL_VERSION}: {day} ticks 복구 드릴", flush=True)

        got_hash = file_sha256(parquet)
        if got_hash != m_hash:
            print(f"  ① 파일 해시 불일치 - Archive 가 변조/손상됐다 "
                  f"({m_hash[:12]}… vs {got_hash[:12]}…)", flush=True)
            return 1
        print(f"  ① Manifest 대조: sha256 일치, 기대 {m_rows:,}행", flush=True)

        restored = restore(cur, parquet)
        conn.commit()
        ok2 = restored == m_rows
        print(f"  ② 복원 행수: {restored:,} {'== Manifest' if ok2 else '!= Manifest!'}",
              flush=True)

        cur.execute(fingerprint_sql(SCRATCH, lo, hi))
        fp_replay = cur.fetchone()
        cur.execute(fingerprint_sql("market", lo, hi))
        fp_live = cur.fetchone()
        ok3 = fp_replay == fp_live
        print(f"  ③ 지문 대조(복원 vs 운영): {'일치' if ok3 else '불일치!'}", flush=True)
        print(f"     count {fp_replay[0]:,} | event {fp_replay[1]:%H:%M:%S}~"
              f"{fp_replay[2]:%H:%M:%S} | 거래대금합 {fp_replay[3][:18]}… | "
              f"고유ID {fp_replay[4]:,}", flush=True)

        if not keep:
            cur.execute(f"drop schema {SCRATCH} cascade")
            conn.commit()
            print("  스크래치 스키마 정리 완료 (--keep 으로 보존 가능)", flush=True)
        else:
            print(f"  {SCRATCH}.{DRILL_TABLE} 보존 - Replay 실험용", flush=True)

        success = ok2 and ok3
        print(f"  드릴 {'성공 - 이 Archive 만으로 거래일 복구가 재현된다' if success else '실패'}",
              flush=True)
        return 0 if success else 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 자체 점검 - DB 없음
# ---------------------------------------------------------------------------

def _check_no_broker_credentials():
    """Replay 원칙 7: 이 파일이 Broker/Vendor Credential 을 읽지 않는지.

    검사 문자열 자신이 본문에 있으면 자기 자신에 걸리므로 조각 결합으로 만든다.
    """
    src = Path(__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[2]        # docstring 의 설명 문구는 제외
    tokens = ["LS_APP" + "_KEY", "LS_APP" + "_SECRET", "NAVER" + "_CLIENT",
              "OPEN_DART" + "_API", "SUPABASE_SERVICE" + "_ROLE"]
    for token in tokens:
        assert token not in body, f"Replay 드릴이 {token} 을 참조한다 - 원칙 7 위반"
    print("  Broker Credential 격리   OK")


def _check_fingerprint_shape():
    sql = fingerprint_sql("market", *kst_day_bounds(date(2026, 7, 31)))
    assert "count(*)" in sql and "sum(price * quantity)" in sql
    assert "market.market_ticks" in sql
    sql2 = fingerprint_sql(SCRATCH, *kst_day_bounds(date(2026, 7, 31)))
    assert f"{SCRATCH}.market_ticks" in sql2
    print("  지문 SQL 구성            OK")


def _check_csv_null_handling():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["" if v is None else v for v in ("a", None, 1)])
    assert buf.getvalue().strip() == "a,,1"    # null '' 규약과 일치
    print("  CSV NULL 규약            OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--drill" in sys.argv:
        a = sys.argv
        d = date.fromisoformat(a[a.index("--date") + 1])
        raise SystemExit(run_drill(d, keep="--keep" in a))

    print(f"{DRILL_VERSION} 자체 점검 (DB 없음)")
    _check_no_broker_credentials()
    _check_fingerprint_shape()
    _check_csv_null_handling()
    print("복구 드릴 3개 영역 통과. 실행은 --drill --date YYYY-MM-DD")
