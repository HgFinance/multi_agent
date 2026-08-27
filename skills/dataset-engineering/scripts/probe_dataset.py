#!/usr/bin/env python3
"""데이터셋 하나를 **실험에 넣을 수 있는지** 층별로 실제로 열어 본다.

▶ 왜 이 도구가 있나 (2026-08-12 실측)
  `krx-microstructure-daily/v1-20260812` 로 실험을 걸었더니 이렇게 죽었다:

      BadGzipFile: Not a gzipped file (b'PA')

  이 메시지만 보면 "파일이 깨졌다" 로 읽힌다. 실제로는 파일이 멀쩡했고
  148,931행이 정상이었다. 죽은 자리는 훨씬 뒤였다. 한 층씩 열어 보니:

      1 명세 조회   OK        5 load_dataset      OK (148,931행)
      2 매니페스트  OK 59개    6 Market.from_rows  **KeyError: 'open'**
      3 파일        OK        7 식별자 교집합      2,558 (부분집합)
      4 해시        OK        8 유니버스           미연결

  즉 **데이터는 완벽했고 실행면에 "피처 데이터셋" 이라는 자리가 없었다.**
  Market 은 가격 시계열인데 마이크로구조에는 가격이 없다. 그건 데이터
  문제가 아니라 설계 공백이고, 고칠 곳도 완전히 다르다.

  **한 층에서 난 예외를 그 층의 문제로 읽으면 엉뚱한 것을 고친다.**
  이 도구는 어디까지 갔는지를 사실로 보여 준다.

사용:
    quant-py probe_dataset.py <이름>/<버전>
    quant-py probe_dataset.py <이름>/<버전> --against krx-basket-daily/v3
    quant-py probe_dataset.py --list
"""
from __future__ import annotations

import io
import sys
import traceback
from pathlib import Path

for _p in ("/app/departments/04-quant-backtest/pipeline",
           "/app/departments/01-research/collectors"):
    if Path(_p).is_dir() and _p not in sys.path:
        sys.path.insert(0, _p)
# 저장소 배치가 달라도 돈다 - 경로를 박아 두면 구조가 바뀔 때 "도구가 없다" 가 된다
_HERE = Path(__file__).resolve()
for _up in _HERE.parents:
    _pipe = _up / "departments" / "04-quant-backtest" / "pipeline"
    if _pipe.is_dir():
        sys.path.insert(0, str(_pipe))
        sys.path.insert(0, str(_up / "departments" / "01-research" / "collectors"))
        break

PASS, FAIL, SKIP = "OK", "**막힘**", "건너뜀"


class Probe:
    """층별 결과. **못 한 것과 실패한 것을 구분한다.**"""

    def __init__(self) -> None:
        self.rows: list[tuple[int, str, str, str]] = []
        self.stopped_at: int | None = None

    def ok(self, n: int, label: str, detail: str = "") -> None:
        self.rows.append((n, label, PASS, detail))

    def fail(self, n: int, label: str, detail: str) -> None:
        self.rows.append((n, label, FAIL, detail))
        if self.stopped_at is None:
            self.stopped_at = n

    def skip(self, n: int, label: str, why: str) -> None:
        self.rows.append((n, label, SKIP, why))

    def report(self) -> None:
        print("\n%-3s %-26s %-9s %s" % ("층", "무엇을 보나", "결과", "사실"))
        print("-" * 78)
        for n, label, st, detail in self.rows:
            print("%-3d %-26s %-9s %s" % (n, label, st, detail[:44]))
        print()
        if self.stopped_at is None:
            print("▶ 모든 층 통과. 이 데이터셋은 지금 실행면으로 실험까지 간다.")
        else:
            print("▶ **%d층까지 통과하고 %d층에서 막혔다.**" % (
                self.stopped_at - 1, self.stopped_at))
            print("  앞 층은 사실로 확인됐다 - 거기를 고치지 마라.")


def _conn():
    import psycopg2
    from source_registry import load_project_env
    return psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)


def probe(ref: str, against: str | None = None) -> Probe:
    name, _, version = ref.rpartition("/")
    p = Probe()
    if not name or not version:
        p.fail(0, "인자", f"'{ref}' 는 <이름>/<버전> 꼴이 아니다")
        return p

    # 1. 명세 등록부 ─────────────────────────────────────────────────────────
    spec = None
    try:
        from dataset_spec import spec_for
        spec = spec_for(name, version)
        if spec is None:
            p.ok(1, "명세 조회", "명세 없음 - 일봉(가격) 계열로 취급된다")
        else:
            p.ok(1, "명세 조회", f"{spec.name}/{spec.version} 열 {len(spec.columns)}개")
    except Exception as e:  # noqa: BLE001
        p.fail(1, "명세 조회", f"{type(e).__name__}: {e}")
        return p

    conn = None
    try:
        conn = _conn()
    except Exception as e:  # noqa: BLE001
        p.fail(2, "원장 연결", f"{type(e).__name__}: {str(e)[:60]}")
        return p

    # 2. 매니페스트 + 파티션 ─────────────────────────────────────────────────
    parts, uid = [], None
    try:
        with conn.cursor() as cur:
            cur.execute("""select dataset_id, universe_version_id, row_count
                             from quant.dataset_manifests
                            where name=%s and version=%s""", (name, version))
            row = cur.fetchone()
            if row is None:
                p.fail(2, "매니페스트", "등재되지 않았다 - 먼저 빌드해야 한다")
                return p
            did, uid, rc = row
            cur.execute("""select partition_key, object_path, content_hash
                             from quant.dataset_partitions where dataset_id=%s
                            order by partition_key""", (did,))
            parts = cur.fetchall()
        p.ok(2, "매니페스트·파티션", f"{len(parts)}개 파티션 / {rc:,}행 등재")
    except Exception as e:  # noqa: BLE001
        p.fail(2, "매니페스트·파티션", f"{type(e).__name__}: {str(e)[:60]}")
        return p

    # 3. 파일이 실제로 있나 ──────────────────────────────────────────────────
    try:
        from pit_dataset import resolve_object_path
        miss = [k for k, path, _ in parts if not Path(resolve_object_path(path)).exists()]
        if miss:
            p.fail(3, "파티션 파일", f"{len(miss)}개 없음 (예: {miss[0]})")
            return p
        total = sum(Path(resolve_object_path(pp)).stat().st_size for _, pp, _ in parts)
        p.ok(3, "파티션 파일", f"{len(parts)}개 전부 존재 / {total/1e6:.1f}MB")
    except Exception as e:  # noqa: BLE001
        p.fail(3, "파티션 파일", f"{type(e).__name__}: {str(e)[:60]}")
        return p

    # 4. 읽기 + 해시 ────────────────────────────────────────────────────────
    try:
        k, path, phash = parts[0]
        if spec is not None:
            from spec_dataset_builder import content_hash as ch
            from spec_dataset_builder import read_partition
            chunk = read_partition(spec, resolve_object_path(path))
            got = ch(spec, chunk)
        else:
            from pit_dataset import content_hash as ch
            from pit_dataset import load_partition
            chunk = load_partition(resolve_object_path(path))
            got = ch(chunk)
        if got != phash:
            p.fail(4, "읽기·해시", f"{k} 해시 불일치 - 파일이 바뀌었다")
            return p
        cols = sorted(chunk[0]) if chunk else []
        p.ok(4, "읽기·해시", f"{k}: {len(chunk):,}행, 해시 일치")
        p.rows.append((4, "  읽힌 열", "", ", ".join(cols)[:44]))
    except Exception as e:  # noqa: BLE001
        p.fail(4, "읽기·해시", f"{type(e).__name__}: {str(e)[:60]}")
        return p

    # 5. load_dataset 전체 ──────────────────────────────────────────────────
    rows = []
    try:
        from backtest_runner import load_dataset
        _d, uid2, _c, rows = load_dataset(conn, name, version)
        # load_dataset 은 uuid 를 문자열로 준다 - None 도 "None" 이 된다.
        # 그대로 넘기면 8층이 `invalid input syntax for uuid` 로 죽는다(실측).
        uid = uid if str(uid2) in ("", "None") else uid2
        p.ok(5, "load_dataset", f"{len(rows):,}행 로드")
    except Exception as e:  # noqa: BLE001
        p.fail(5, "load_dataset", f"{type(e).__name__}: {str(e)[:60]}")
        return p

    # 6. 실행면이 이 행을 받나 ──────────────────────────────────────────────
    #    **여기가 갈림길이다.** Market 은 가격 시계열이다. 가격이 없는
    #    데이터셋은 여기서 죽는데, 그건 데이터가 아니라 설계의 문제다.
    try:
        from backtest_runner import Market
        m = Market.from_rows(rows)
        p.ok(6, "Market 구성", f"{len(m.dates)}일 / {len(m.symbols):,}종목")
    except KeyError as e:
        p.fail(6, "Market 구성", f"KeyError: {e} - 가격 열이 없다(피처 데이터셋)")
    except Exception as e:  # noqa: BLE001
        p.fail(6, "Market 구성", f"{type(e).__name__}: {str(e)[:60]}")

    # 7. 다른 데이터셋과 종목을 이을 수 있나 ────────────────────────────────
    if against:
        try:
            an, _, av = against.rpartition("/")
            from backtest_runner import load_dataset as _ld
            _d, _u, _c, other = _ld(conn, an, av)
            a = {str(r["instrument_id"]) for r in rows}
            b = {str(r["instrument_id"]) for r in other}
            inter = a & b
            if not inter:
                p.fail(7, "식별자 대조", f"{against} 와 교집합 0 - 사상표가 필요하다")
            else:
                sub = "부분집합" if a <= b else "부분 겹침"
                p.ok(7, "식별자 대조",
                     f"교집합 {len(inter):,} / 이쪽 {len(a):,} ({sub})")
        except Exception as e:  # noqa: BLE001
            p.skip(7, "식별자 대조", f"{type(e).__name__}: {str(e)[:50]}")
    else:
        p.skip(7, "식별자 대조", "--against 를 안 줬다")

    # 8. 유니버스 ───────────────────────────────────────────────────────────
    try:
        if uid is None:
            p.skip(8, "유니버스 연결", "미연결 - 횡단면 순위를 매길 모집단이 없다")
        else:
            with conn.cursor() as cur:
                cur.execute("""select count(*) from quant.universe_members
                                where universe_version_id=%s""", (uid,))
                n = cur.fetchone()[0]
            p.ok(8, "유니버스 연결", f"{n:,}종목")
    except Exception as e:  # noqa: BLE001
        p.skip(8, "유니버스 연결", f"{type(e).__name__}: {str(e)[:50]}")
    finally:
        conn.close()
    return p


def _list() -> int:
    try:
        conn = _conn()
    except Exception as e:  # noqa: BLE001
        print("원장에 못 붙었다:", type(e).__name__, str(e)[:80])
        return 1
    try:
        from data_resolution import catalog
        rows = catalog(conn)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1
    finally:
        conn.close()
    if not rows:
        print("등재된 데이터셋이 없다.")
        return 0
    print("%-38s %-28s %12s %14s" % ("데이터셋", "덮는 원천", "종목", "행수"))
    for r in rows:
        syms = f"{r['symbols']:,}" if r["symbols"] is not None else "유니버스없음"
        print("%-38s %-28s %12s %14s"
              % (r["dataset"], ",".join(r["sources"])[:28], syms, f"{r['rows']:,}"))
    print("\n▶ 하나를 골라 `probe_dataset.py <이름>/<버전>` 으로 실제로 열어 봐라.")
    return 0


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    else:                                     # 옛 파이썬
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if "--list" in argv or not argv:
        return _list()
    ref = argv[0]
    against = argv[argv.index("--against") + 1] if "--against" in argv else None
    print(f"데이터셋 진단: {ref}" + (f"  (대조: {against})" if against else ""))
    p = probe(ref, against)
    p.report()
    if p.stopped_at == 6:
        print("""
  6층에서 막혔다는 것은 **데이터가 아니라 실행면**의 문제다.
  Market 은 가격 시계열이고, 이 데이터셋에는 체결할 가격이 없다.
  이건 결함이 아니라 **아직 없는 기능**이다 - 피처 데이터셋을 쓰려면
  가격 데이터셋(체결용)과 피처 데이터셋(신호용)을 함께 받는 길을
  실행면에 내야 한다. 러너를 고쳐도 된다(autonomous-quant-research 검증 루프).
  고치기 전에 그 설계를 카드에 적어라 - 사전등록이 먼저다.""")
    return 0 if p.stopped_at is None else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
