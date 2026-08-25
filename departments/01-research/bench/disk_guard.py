#!/usr/bin/env python3
"""디스크 감시기 - 실험이 운영을 죽이지 못하게 한다.

담당: 재일 (리서치본부 RES)

▶ 왜 (2026-08-25 실측)
  연구 벤치 r0003 의 집계 하나가 디스크를 26GB -> 6.8GB(98%) 로 밀었다.
  라이브 수집·공장·DB·연구가 **같은 디스크**를 쓰므로, 여기가 차면 연구가
  아니라 **운영 전체가 죽는다**(Postgres 가 멈추면 시세 수집도 멈춘다).

  `temp_file_limit` 을 걸어 폭주 쿼리는 스스로 죽게 했지만 그건 한 쿼리의
  방어다. 이 모듈은 **시스템 수준**의 방어다.

▶ 3단 방어
  1. 여유 >= SAFE      : 아무것도 안 한다
  2. WARN > 여유 >= CRIT: 회수 가능한 것만 정리한다(빌드 캐시·오래된 산출물).
                          **데이터는 절대 안 지운다.**
  3. 여유 < CRIT       : 정리 + **연구 쿼리만** 취소한다.
                          라이브 수집·공장 쿼리는 건드리지 않는다 -
                          연구가 운영에 양보하는 것이 이 순서의 요점이다.

▶ 절대 안 하는 것
  - 시장 데이터·연구 로그·스크립트 삭제. 회수는 **다시 만들 수 있는 것**만이다
    (빌드 캐시, 떠 있지 않은 이미지, 오래된 out/*.json).
  - 라이브 수집·주문 경로 쿼리 취소. 그건 돈이 걸린 경로다.

사용:
    python3 disk_guard.py --self-check
    python3 disk_guard.py --check          # 상태만
    python3 disk_guard.py --once           # 필요하면 조치
    python3 disk_guard.py --loop --interval-min 5
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE_VERSION = "disk-guard-v1"

SAFE_GB = int(os.getenv("DISK_SAFE_GB", "25"))     # 이 위면 평온
WARN_GB = int(os.getenv("DISK_WARN_GB", "20"))     # 이 아래면 청소
CRIT_GB = int(os.getenv("DISK_CRIT_GB", "10"))     # 이 아래면 연구를 끊는다

DB_CONTAINER = os.getenv("DISK_DB_CONTAINER", "hedgefund-timescaledb")
RESEARCH_OUT = Path(os.getenv("RESEARCH_OUT",
                              str(Path.home() / "hgfinance" / "quant-data"
                                  / "research" / "out")))
OUT_KEEP_DAYS = int(os.getenv("RESEARCH_OUT_KEEP_DAYS", "14"))

# 연구 쿼리로 보는 표식. 라이브 수집(insert)·주문 경로는 여기 안 걸린다.
_RESEARCH_QUERY = re.compile(
    r"(time_bucket|ext_src\.|microstructure_features|markout|"
    r"select\s+.*\bfrom\s+ext_src)", re.I)
# 절대 끊지 않는 것 - 돈과 데이터가 걸린 경로.
_PROTECTED = re.compile(r"(insert\s+into|copy\s|compress_chunk|autovacuum|"
                        r"market_quotes|market_ticks)", re.I)


def _run(argv, timeout=120):
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def free_gb(path: str = "/") -> float:
    st = shutil.disk_usage(path)
    return st.free / (1024 ** 3)


def level(free: float) -> str:
    if free < CRIT_GB:
        return "CRIT"
    if free < WARN_GB:
        return "WARN"
    if free < SAFE_GB:
        return "LOW"
    return "OK"


# ── 회수: 다시 만들 수 있는 것만 ────────────────────────────────────────────
def reclaim(*, dry_run: bool = False) -> list[str]:
    done: list[str] = []

    # ① 빌드 캐시 - 순수 재생성 가능. 제일 안전하고 보통 제일 크다.
    if dry_run:
        done.append("[dry-run] docker builder prune -af")
    else:
        rc, out = _run(["docker", "builder", "prune", "-af"])
        m = re.search(r"Total:\s*([\d.]+\s*\w+)", out)
        done.append(f"빌드 캐시 회수 {m.group(1) if m else 'n/a'}")

    # ② 떠 있지 않은 이미지 중 **오래된 것만**. 최근 것은 롤백 안전망이라 둔다.
    if dry_run:
        done.append("[dry-run] docker image prune -a --filter until=168h")
    else:
        rc, out = _run(["docker", "image", "prune", "-a", "-f",
                        "--filter", "until=168h"])
        m = re.search(r"Total reclaimed space:\s*([\d.]+\s*\w+)", out)
        done.append(f"구세대 이미지 회수 {m.group(1) if m else '0B'}")

    # ③ 오래된 연구 산출물. **스크립트와 로그는 절대 안 지운다** - 계보다.
    cutoff = datetime.now(timezone.utc) - timedelta(days=OUT_KEEP_DAYS)
    n, freed = 0, 0
    if RESEARCH_OUT.is_dir():
        for f in RESEARCH_OUT.iterdir():
            if not f.is_file():
                continue
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
                if mtime >= cutoff:
                    continue
                size = f.stat().st_size
                if not dry_run:
                    f.unlink()
                n += 1
                freed += size
            except OSError:
                continue
    done.append(f"{OUT_KEEP_DAYS}일 지난 산출물 {n}건 "
                f"({freed / 1024 / 1024:.0f}MB)"
                + (" [dry-run]" if dry_run else ""))
    return done


# ── 연구 쿼리만 끊는다 ──────────────────────────────────────────────────────
_SQL_ACTIVE = """
select pid, coalesce(replace(query, chr(10), ' '), '')
  from pg_stat_activity
 where state = 'active' and pid <> pg_backend_pid()
   and query_start < now() - interval '20 seconds'
"""


def cancel_research_queries(*, dry_run: bool = False) -> list[str]:
    """**연구 쿼리만.** 수집·주문·압축은 건드리지 않는다."""
    rc, out = _run(["docker", "exec", DB_CONTAINER, "psql", "-U", "postgres",
                    "-d", "market", "-tAc", _SQL_ACTIVE])
    if rc != 0:
        return [f"활성 쿼리 조회 실패: {out.strip()[:80]}"]
    acted = []
    for line in out.splitlines():
        if "|" not in line:
            continue
        pid, _, q = line.partition("|")
        pid = pid.strip()
        if not pid.isdigit():
            continue
        if _PROTECTED.search(q):
            continue                      # 운영 경로는 절대 안 끊는다
        if not _RESEARCH_QUERY.search(q):
            continue
        if dry_run:
            acted.append(f"[dry-run] cancel {pid}: {q[:50]}")
            continue
        _run(["docker", "exec", DB_CONTAINER, "psql", "-U", "postgres",
              "-d", "market", "-tAc", f"select pg_cancel_backend({pid})"])
        acted.append(f"연구 쿼리 취소 {pid}: {q[:50]}")
    return acted or ["끊을 연구 쿼리 없음"]


def run_once(*, dry_run: bool = False) -> dict:
    free = free_gb()
    lv = level(free)
    print(f"  디스크 여유 {free:.1f}GB [{lv}]", flush=True)
    acted: list[str] = []

    if lv in ("WARN", "CRIT", "LOW"):
        for line in reclaim(dry_run=dry_run):
            print(f"    {line}", flush=True)
            acted.append(line)
        free = free_gb()
        print(f"    회수 후 {free:.1f}GB", flush=True)

    if level(free) == "CRIT":
        # 회수로도 모자라면 **연구가 운영에 양보한다.**
        for line in cancel_research_queries(dry_run=dry_run):
            print(f"    {line}", flush=True)
            acted.append(line)

    return {"free_gb": round(free, 1), "level": level(free), "acted": acted}


# ── 자체 점검 ───────────────────────────────────────────────────────────────
def _selfcheck() -> int:
    fails = 0

    def ok(name, cond):
        nonlocal fails
        print(("  ✓ " if cond else "  ✗ ") + name)
        if not cond:
            fails += 1

    ok("임계값 순서가 맞다", CRIT_GB < WARN_GB < SAFE_GB)
    ok("여유가 충분하면 OK", level(SAFE_GB + 1) == "OK")
    ok("경고 구간을 잡는다", level(WARN_GB - 1) == "WARN")
    ok("위험 구간을 잡는다", level(CRIT_GB - 1) == "CRIT")

    live = "insert into market.market_quotes (event_time, ...) values"
    comp = "select compress_chunk('_timescaledb_internal._hyper_9_1')"
    res = "with q as (select time_bucket('1 second', ts) ts from ext_src.quotes)"
    copy = "COPY ext_src.ticks FROM STDIN csv"

    ok("라이브 수집은 보호된다", bool(_PROTECTED.search(live)))
    ok("압축은 보호된다", bool(_PROTECTED.search(comp)))
    ok("이관 COPY 는 보호된다", bool(_PROTECTED.search(copy)))
    ok("연구 집계는 취소 대상", bool(_RESEARCH_QUERY.search(res))
       and not _PROTECTED.search(res))

    ok("실제 여유를 읽는다", free_gb() > 0)
    print("자체점검 통과" if fails == 0 else f"자체점검 실패 {fails}건")
    return fails


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    m = p.add_mutually_exclusive_group(required=True)
    m.add_argument("--self-check", action="store_true")
    m.add_argument("--check", action="store_true")
    m.add_argument("--once", action="store_true")
    m.add_argument("--loop", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--interval-min", type=int, default=5)
    a = p.parse_args(argv)

    if a.self_check:
        return _selfcheck()
    if a.check:
        f = free_gb()
        print(f"여유 {f:.1f}GB [{level(f)}]  "
              f"(SAFE>={SAFE_GB} WARN<{WARN_GB} CRIT<{CRIT_GB})")
        return 0
    if a.once:
        run_once(dry_run=a.dry_run)
        return 0
    interval = max(1, a.interval_min) * 60
    print(f"{MODULE_VERSION} 반복 시작 - {a.interval_min}분마다", flush=True)
    while True:
        try:
            run_once(dry_run=a.dry_run)
        except Exception as e:                 # 감시기가 죽어도 운영은 산다
            print(f"  감시 주기 오류(계속): {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
