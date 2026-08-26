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
TMP_ROOT = Path(os.getenv("DISK_TMP_ROOT", "/tmp"))
TMP_VENV_MIN_AGE_DAYS = int(os.getenv("TMP_VENV_MIN_AGE_DAYS", "7"))

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


def build_in_progress() -> bool:
    """지금 도커 빌드가 도는가.

    `docker builder prune` 은 **도는 빌드의 캐시도 지운다.** 공유 호스트에서
    남이 빌드하는 중에 이걸 때리면 그 빌드는 처음부터 다시 돌고, 디스크를 더
    쓰고, 감시기가 또 지운다 - 증폭 고리다(2026-08-25 실측).

    프로세스 목록으로 본다. 도커 API 로 빌드 상태를 묻는 깔끔한 방법이 없고,
    `buildx bake`/`buildkit` 은 프로세스로 확실히 보인다. **판정에 실패하면
    빌드 중이라고 본다** - 애매할 때 안 지우는 쪽이 안전하다.
    """
    rc, out = _run(["ps", "-eo", "args"], timeout=20)
    if rc != 0:
        return True
    for line in out.splitlines():
        if "grep" in line:
            continue
        if ("docker-buildx" in line or "buildkit" in line
                or "--build" in line and "compose" in line):
            return True
    return False


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


def mounted_sources() -> set[str]:
    """전 컨테이너(멈춘 것 포함)가 무는 호스트 경로.

    **멈춘 컨테이너도 세는 게 맞다** - 다시 뜨면 그 경로를 요구한다.
    도커를 못 부르면 빈 집합이 아니라 예외를 올려 호출부가 회수를 포기하게
    한다. 확인 못 한 것을 "안 쓰는 것" 으로 취급하면 안 된다.
    """
    rc, out = _run(["docker", "ps", "-aq"])
    if rc != 0:
        raise RuntimeError(f"컨테이너 목록 조회 실패: {out.strip()[:80]}")
    ids = [x for x in out.split() if x]
    if not ids:
        return set()
    rc, out = _run(["docker", "inspect", "--format",
                    "{{range .Mounts}}{{.Source}}\n{{end}}", *ids],
                   timeout=180)
    if rc != 0:
        raise RuntimeError(f"마운트 조회 실패: {out.strip()[:80]}")
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def abandoned_tmp_venvs() -> list[tuple[Path, int]]:
    """버려진 venv 와 그 크기. 셋 다 만족하는 것만 돌려준다."""
    try:
        busy = mounted_sources()
    except RuntimeError:
        return []                      # 확인 못 했으면 아무것도 안 만진다
    cutoff = datetime.now(timezone.utc) - timedelta(days=TMP_VENV_MIN_AGE_DAYS)
    found = []
    if not TMP_ROOT.is_dir():
        return []
    for d in TMP_ROOT.iterdir():
        try:
            if not d.is_dir() or d.is_symlink():
                continue
            if not (d / "pyvenv.cfg").is_file():
                continue               # ① venv 임이 증명된 것만
            sd = str(d)
            if any(m == sd or m.startswith(sd + "/") for m in busy):
                continue               # ② 누가 물고 있으면 손대지 않는다
            if datetime.fromtimestamp(d.stat().st_mtime, timezone.utc) >= cutoff:
                continue               # ③ 최근 것은 둔다
            size = sum(f.stat().st_size for f in d.rglob("*")
                       if f.is_file() and not f.is_symlink())
            found.append((d, size))
        except OSError:
            continue
    return found


# ── 회수: 다시 만들 수 있는 것만 ────────────────────────────────────────────
def reclaim(*, dry_run: bool = False) -> list[str]:
    done: list[str] = []

    # ① 빌드 캐시 - 순수 재생성 가능. 제일 안전하고 보통 제일 크다.
    #    **단, 빌드가 도는 중이면 손대지 않는다.** 만드는 중인 것을 지우는 건
    #    회수가 아니라 방해고, 그 빌드가 다시 돌면서 디스크를 더 쓴다.
    if build_in_progress():
        done.append("빌드 진행 중 - 빌드 캐시 회수 건너뜀(증폭 방지)")
    elif dry_run:
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
    # ④ 버려진 /tmp venv. pip 로 다시 만들어지므로 회수 대상이지만,
    #    /tmp 에는 vLLM 이 무는 LoRA 어댑터도 산다 - 마운트 확인이 필수다.
    for d, size in abandoned_tmp_venvs():
        gb = size / 1024 ** 3
        if dry_run:
            done.append(f"[dry-run] 버려진 venv {d.name} ({gb:.1f}GB)")
            continue
        try:
            shutil.rmtree(d)
            done.append(f"버려진 venv 회수 {d.name} ({gb:.1f}GB)")
        except OSError as e:
            done.append(f"venv 회수 실패 {d.name}: {e}")

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

    # 버려진 venv 판정: 세 조건을 하나씩 어겨 보며 확인한다
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        old = datetime.now(timezone.utc) - timedelta(days=400)
        stamp = old.timestamp()

        def mk(name, *, venv=True, aged=True):
            d = root / name
            (d / "lib").mkdir(parents=True)
            (d / "lib" / "x.bin").write_bytes(b"0" * 1024)
            if venv:
                (d / "pyvenv.cfg").write_text("home = /usr")
            if aged:
                os.utime(d, (stamp, stamp))
            return d

        real = mk("venv-old")
        mk("venv-new", aged=False)
        mk("not-a-venv", venv=False)
        busy = mk("venv-busy")

        import builtins  # noqa: F401  (가독성용 - 아래는 임시 대체다)
        _saved_root, _saved_mounted = TMP_ROOT, mounted_sources
        try:
            globals()["TMP_ROOT"] = root
            globals()["mounted_sources"] = lambda: {str(busy)}
            names = {d.name for d, _ in abandoned_tmp_venvs()}
        finally:
            globals()["TMP_ROOT"] = _saved_root
            globals()["mounted_sources"] = _saved_mounted

        ok("버려진 venv 를 찾는다", "venv-old" in names)
        ok("최근 venv 는 남긴다", "venv-new" not in names)
        ok("venv 가 아니면 안 건드린다", "not-a-venv" not in names)
        ok("마운트된 venv 는 안 건드린다", "venv-busy" not in names)

        _saved_mounted = mounted_sources
        try:
            def _boom():
                raise RuntimeError("도커 안 됨")
            globals()["TMP_ROOT"] = root
            globals()["mounted_sources"] = _boom
            ok("마운트 확인 실패하면 아무것도 안 지운다",
               abandoned_tmp_venvs() == [])
        finally:
            globals()["TMP_ROOT"] = _saved_root
            globals()["mounted_sources"] = _saved_mounted

    # 빌드 감지: 실제 ps 출력 모양으로 판정 로직을 확인한다
    _saved = _run
    try:
        def _fake(argv, timeout=120):
            return 0, ("docker compose up -d --build --force-recreate ai-office\n"
                       "/usr/libexec/docker/cli-plugins/docker-buildx bake --file -\n")
        globals()["_run"] = _fake
        ok("빌드 중을 잡아낸다", build_in_progress() is True)

        def _quiet(argv, timeout=120):
            return 0, "/usr/bin/python3 disk_guard.py --loop\nsshd: ubuntu@pts/0\n"
        globals()["_run"] = _quiet
        ok("한가할 때는 회수한다", build_in_progress() is False)

        def _broken(argv, timeout=120):
            return 1, "ps 못 씀"
        globals()["_run"] = _broken
        ok("판정 실패하면 안 지운다", build_in_progress() is True)
    finally:
        globals()["_run"] = _saved

    ok("실제 여유를 읽는다", free_gb() > 0)
    print("자체점검 통과" if fails == 0 else f"자체점검 실패 {fails}건")
    return fails


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    m = p.add_mutually_exclusive_group(required=True)
    m.add_argument("--self-check", action="store_true")
    m.add_argument("--check", action="store_true")
    m.add_argument("--alert-check", action="store_true")
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
    if a.alert_check:
        f = free_gb()
        current = level(f)
        print(f"disk_guard free={f:.1f}GB level={current} "
              f"warn_below={WARN_GB}GB critical_below={CRIT_GB}GB")
        # A non-zero systemd result is the alert signal.  This mode never
        # reclaims or deletes anything; operators can inspect the journal and
        # explicitly run --once after identifying the growth source.
        return 2 if current == "CRIT" else 1 if current == "WARN" else 0
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
