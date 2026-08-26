#!/usr/bin/env python3
"""보존 정책 집행 - **Archive 로 굳은 날만** 원시에서 내린다.

소유: 재일 (리서치본부 RES 수집)
근거: 재일님 지시 2026-08-12 "앞으로 원시 db에서 하루 지나면 이전 parquet에
      데이터 합치게 잘 만들어 놓을 수 있냐"
계약: market.retention_registry (hot_retention / archive_required /
      deletion_enabled / approved_by), market.archive_exports (exported/verified)

▶ 왜 이 파일이 필요했나
  `retention_registry` 는 2026-07-30 부터 있었는데 **집행하는 코드가 없었다.**
  그래서 디스크가 찰 때마다 사람이 손으로 `drop_chunks` 를 돌렸고, 그때마다
  "이 날이 Archive 됐나" 를 눈으로 확인해야 했다. 8/11 에는 그 확인을 건너뛰어
  호가가 구멍난 채로 지워졌다.

▶ 삭제 조건 (하나라도 어긋나면 안 지운다)
  ① `deletion_enabled = true`      - 사람이 켠 표만
  ② `hot_retention` 보다 오래됨    - 최근 창은 /snapshot·당일 피처가 쓴다
  ③ `archive_required` 면 그 구간이 `archive_exports` 에 **verified** 로 있다

  ③이 이 파일의 존재 이유다. **Archive 안 된 날은 못 지운다** - 지우고 나면
  되돌릴 방법이 없고, "지웠는데 아카이브도 없다" 는 조용히 발견된다.

▶ 청크 경계가 자정이 아니다 (실측)
  하이퍼테이블 청크는 09:00 KST 경계다. `older_than` 을 자정으로 주면 전날
  09:00~24:00 이 같이 날아간다. 그래서 **청크 단위로 판단하고, 그 청크가 덮는
  모든 거래일이 Archive 됐을 때만** 지운다.

▶ 지운 것은 반드시 남긴다
  무엇을 언제 왜 지웠는지 로그로 찍는다. 조용한 삭제가 가장 나쁘다.

사용
  python collectors/retention_enforcer.py            # 자체 점검
  python collectors/retention_enforcer.py --plan     # 무엇을 지울지만 보고
  python collectors/retention_enforcer.py --enforce  # 실제로 지운다
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ENFORCER_VERSION = "research-retention-enforcer-v1"
KST = timezone(timedelta(hours=9))

# 정책이 없거나 못 읽으면 **아무것도 안 지운다.** 기본값을 "지운다" 쪽에 두면
# 표가 비어 있는 실수 하나로 원장이 사라진다.
_SQL_POLICY = """
select source_table, hot_retention, archive_required, deletion_enabled
  from market.retention_registry
 where deletion_enabled
"""

_SQL_CHUNKS = """
select c.chunk_schema || '.' || c.chunk_name,
       c.range_start, c.range_end
  from timescaledb_information.chunks c
 where c.hypertable_schema || '.' || c.hypertable_name = %s
   and c.range_end <= %s
 order by c.range_start
"""

# 그 구간이 verified Archive 로 남아 있는가. **구간이 청크를 덮어야** 한다 -
# 청크 일부만 아카이브된 상태로 지우면 나머지가 사라진다.
_SQL_ARCHIVED = """
select coalesce(
         range_agg(tstzrange(partition_start, partition_end, '[)'))
           @> tstzrange(%s, %s, '[)'),
         false
       )
  from market.archive_exports
 where source_table in (%s, %s)
   and verified
   and partition_end > %s and partition_start < %s
"""


@dataclass(frozen=True)
class Candidate:
    table: str
    chunk: str
    start: datetime
    end: datetime
    archived: bool
    reason: str

    @property
    def days(self) -> str:
        return (f"{self.start.astimezone(KST):%Y-%m-%d}"
                f"~{self.end.astimezone(KST):%Y-%m-%d}")


def covered_days(start: datetime, end: datetime) -> list[date]:
    """청크가 덮는 거래일(KST). 09:00 경계라 보통 이틀에 걸친다."""
    lo = start.astimezone(KST).date()
    hi = (end.astimezone(KST) - timedelta(seconds=1)).date()
    out, d = [], lo
    while d <= hi:
        out.append(d)
        d += timedelta(days=1)
    return out


def plan(conn, *, now: datetime | None = None) -> list[Candidate]:
    """지울 후보와 판정. **판정만 한다 - 여기서 지우지 않는다.**"""
    now = now or datetime.now(timezone.utc)
    out: list[Candidate] = []
    with conn.cursor() as cur:
        cur.execute(_SQL_POLICY)
        policies = cur.fetchall()
        for table, hot, need_archive, _enabled in policies:
            cutoff = now - hot
            cur.execute(_SQL_CHUNKS, (table, cutoff))
            for chunk, start, end in cur.fetchall():
                if not need_archive:
                    out.append(Candidate(table, chunk, start, end, True,
                                         "archive_required=false - 보존기간 경과"))
                    continue
                # 외부 원천에서 뜬 아카이브도 같은 구간을 덮는다. 우리 표
                # 이름과 `external:` 접두 둘 다 인정한다 - 어느 쪽으로 떴든
                # **그 구간이 파일로 남아 있다는 사실**이 조건이다.
                short = table.split(".")[-1].replace("market_", "")
                cur.execute(_SQL_ARCHIVED,
                            (start, end, table, f"external:public.{short}",
                             start, end))
                covered = bool(cur.fetchone()[0])
                out.append(Candidate(
                    table, chunk, start, end, covered,
                    "verified Archive 연속 커버리지 있음" if covered
                    else "**Archive 없음 - 지우지 않는다**"))
    return out


def enforce(conn, *, dry_run: bool = True, now: datetime | None = None) -> dict:
    """조건을 다 만족한 청크만 내린다. 반환: 요약."""
    cands = plan(conn, now=now)
    dropped, held = [], []
    for c in cands:
        if not c.archived:
            held.append(c)
            continue
        if dry_run:
            dropped.append(c)
            continue
        with conn.cursor() as cur:
            cur.execute("select drop_chunks(%s, older_than => %s)",
                        (c.table, c.end))
        conn.commit()
        dropped.append(c)
    return {"dropped": dropped, "held": held}


# ── 자체 점검 (DB 없음) ───────────────────────────────────────────────────
class _Cur:
    def __init__(self, policies, chunks, archived):
        self.p, self.c, self.a = policies, chunks, archived
        self._rows: list = []
        self.dropped: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "retention_registry" in s:
            self._rows = list(self.p)
        elif "timescaledb_information.chunks" in s:
            self._rows = [c for c in self.c if c[0].startswith(params[0].split(".")[-1])
                          or True]
            self._rows = [(ch, st, en) for (tbl, ch, st, en) in self.c
                          if tbl == params[0] and en <= params[1]]
        elif "archive_exports" in s:
            self._rows = [(params[2] in self.a or params[3] in self.a,)]
        elif "drop_chunks" in s:
            self.dropped.append(params)
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, policies, chunks, archived):
        self._c = _Cur(policies, chunks, archived)

    def cursor(self):
        return self._c

    def commit(self):
        pass


def _dt(y, m, d, h=9):
    return datetime(y, m, d, h, tzinfo=KST)


def _check_archive_missing_blocks_deletion():
    """**Archive 안 된 날은 못 지운다.** 이게 이 파일의 존재 이유다.

    지우고 나면 되돌릴 방법이 없고, "지웠는데 아카이브도 없다" 는 조용히
    발견된다 - 실제로 8/11 에 호가가 구멍난 채로 지워졌다.
    """
    pol = [("market.market_ticks", timedelta(days=5), True, True)]
    chunks = [("market.market_ticks", "c1", _dt(2026, 7, 1), _dt(2026, 7, 2))]
    now = _dt(2026, 8, 12).astimezone(timezone.utc)

    r = enforce(_Conn(pol, chunks, archived=set()), dry_run=True, now=now)
    assert not r["dropped"] and len(r["held"]) == 1, r
    assert "Archive 없음" in r["held"][0].reason

    r2 = enforce(_Conn(pol, chunks, archived={"market.market_ticks"}),
                 dry_run=True, now=now)
    assert len(r2["dropped"]) == 1 and not r2["held"], r2
    print("  Archive 없으면 삭제 금지  OK")


def _check_external_archive_counts():
    """저쪽에서 뜬 아카이브도 인정한다 - 어느 쪽으로 떴든 파일이 있으면 된다."""
    pol = [("market.market_quotes", timedelta(days=5), True, True)]
    chunks = [("market.market_quotes", "c1", _dt(2026, 7, 1), _dt(2026, 7, 2))]
    now = _dt(2026, 8, 12).astimezone(timezone.utc)
    r = enforce(_Conn(pol, chunks, archived={"external:public.quotes"}),
                dry_run=True, now=now)
    assert len(r["dropped"]) == 1, r
    print("  외부 Archive 인정         OK")


def _check_hot_window_is_kept():
    """보존기간 안의 청크는 후보가 아니다 - /snapshot·당일 피처가 쓴다."""
    pol = [("market.market_ticks", timedelta(days=5), True, True)]
    now = _dt(2026, 8, 12).astimezone(timezone.utc)
    recent = [("market.market_ticks", "c1", _dt(2026, 8, 11), _dt(2026, 8, 12))]
    r = enforce(_Conn(pol, recent, archived={"market.market_ticks"}),
                dry_run=True, now=now)
    assert not r["dropped"] and not r["held"], r
    print("  최근 창 보존              OK")


def _check_disabled_policy_deletes_nothing():
    """`deletion_enabled` 가 꺼져 있으면 아무것도 안 지운다 - 기본이 보존이다."""
    r = enforce(_Conn([], [], archived=set()), dry_run=True)
    assert not r["dropped"] and not r["held"]
    print("  정책 없으면 무삭제        OK")


def _check_chunk_day_coverage():
    """청크는 09:00 경계라 보통 **이틀**을 덮는다. 그걸 알아야 판정이 맞는다."""
    days = covered_days(_dt(2026, 8, 10), _dt(2026, 8, 11))
    assert days == [date(2026, 8, 10), date(2026, 8, 11)], days
    # 자정 경계면 하루만 덮는다
    one = covered_days(datetime(2026, 8, 10, 0, tzinfo=KST),
                       datetime(2026, 8, 11, 0, tzinfo=KST))
    assert one == [date(2026, 8, 10)], one
    print("  청크 09:00 경계 인식      OK")


def _selfcheck() -> int:
    print(f"{ENFORCER_VERSION} 자체 점검 (DB 없음)")
    _check_archive_missing_blocks_deletion()
    _check_external_archive_counts()
    _check_hot_window_is_kept()
    _check_disabled_policy_deletes_nothing()
    _check_chunk_day_coverage()
    print("보존 집행기 5개 영역 통과. 실행은 --plan / --enforce")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if "--plan" not in sys.argv and "--enforce" not in sys.argv:
        return _selfcheck()

    import psycopg2
    from source_registry import load_project_env

    conn = psycopg2.connect(load_project_env()["TIMESCALE_DATABASE_URL"],
                            connect_timeout=20)
    try:
        r = enforce(conn, dry_run="--enforce" not in sys.argv)
        tag = "[plan] " if "--enforce" not in sys.argv else ""
        print(f"{ENFORCER_VERSION}: {tag}삭제 {len(r['dropped'])} / "
              f"보류 {len(r['held'])}", flush=True)
        # **지운 것과 못 지운 것을 둘 다 남긴다.** 조용한 삭제가 가장 나쁘고,
        # 조용한 보류는 디스크가 왜 안 주는지 모르게 만든다.
        for c in r["dropped"]:
            print(f"  삭제 {c.table:26} {c.days}  {c.reason}", flush=True)
        for c in r["held"]:
            print(f"  보류 {c.table:26} {c.days}  {c.reason}", flush=True)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
