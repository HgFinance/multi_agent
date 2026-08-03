#!/usr/bin/env python3
"""거래제한 종목 스냅샷 - 자격을 수집기에 가두고 API 는 DB 만 읽게 한다.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-02 "발생한 모든 문제들 해결해놔" 중 ③.
      마이그레이션 20260802002000 (research.symbol_restrictions)

▶ 왜 만드나
  universe_manager(RES-01)가 LS REST 로 직접 제한 목록을 물었다. 그래서
  파이프라인을 돌리는 컨테이너마다 LS 자격이 필요해졌다(research-mcp 에
  LS 키를 넣어야 했던 이유). **자격이 퍼지는 것 자체가 위험**이고, 통합계획
  6.2 "Agent 는 부서 읽기 전용 API 만" 과도 어긋난다.
  이 수집기가 자격을 독점하고, 판정은 DB 를 읽어 한다.

▶ fail-closed
  목록 조회가 실패하면 **적재하지 않는다.** 일부만 넣으면 정지 종목이
  거래가능으로 새는 방향이라 가장 위험하다(universe_manager 의 원래 규율을
  그대로 계승한다). 부분 성공도 실패로 본다.

▶ 빈 목록과 미수집의 구분
  제한 종목이 0건인 날과 수집을 못 한 날은 완전히 다른 사실이다.
  symbol_restriction_runs 에 실행 자체를 남겨 구분한다.

사용
  python collectors/universe_restriction_collector.py            # 자체 점검
  python collectors/universe_restriction_collector.py --collect  # 오늘 스냅샷
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE / "collectors"))
sys.path.insert(0, str(_BASE / "agents"))

COLLECTOR_VERSION = "research-universe-restriction-v1"
KST = timezone(timedelta(hours=9))


def snapshot_rows(restricted: dict[str, set[str]]) -> list[tuple[str, str]]:
    """{사유: {종목}} -> [(종목, 사유)] 정렬. 한 종목이 여러 사유에 걸리면
    **전부 남긴다** - 판정은 심각도로 하나를 고르지만 사실을 지우진 않는다."""
    out = [(sym, reason) for reason, syms in restricted.items() for sym in syms]
    return sorted(set(out))


def source_tr_of(reason: str, sources) -> str:
    """사유 -> 근거 TR. 목록 정의가 바뀌면 여기서 드러난다."""
    for tr, _jongchk, r in sources:
        if r == reason:
            return tr
    return "?"


def collect() -> int:
    import psycopg2
    from ls_client import LsRestClient
    from source_registry import load_project_env
    from universe_manager import RESTRICTION_SOURCES, fetch_restricted

    as_of = datetime.now(KST).date()
    env = load_project_env()

    restricted: dict[str, set[str]] = {}
    for tr, jongchk, reason in RESTRICTION_SOURCES:
        # 실패는 예외로 터진다 - 부분 적재보다 미적재가 안전하다(fail-closed)
        restricted[reason] = fetch_restricted(LsRestClient(), tr, jongchk)

    rows = snapshot_rows(restricted)
    sizes = {r: len(s) for r, s in restricted.items()}

    conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    try:
        with conn.cursor() as cur:
            # 같은 날 재실행은 갱신이다 - 목록이 줄었으면 줄어든 것이 사실이다
            cur.execute("delete from research.symbol_restrictions where as_of = %s",
                        (as_of,))
            for sym, reason in rows:
                cur.execute("""
                    insert into research.symbol_restrictions
                      (as_of, symbol, reason, source_tr)
                    values (%s, %s, %s, %s)
                    on conflict (as_of, symbol, reason) do nothing
                """, (as_of, sym, reason,
                      source_tr_of(reason, RESTRICTION_SOURCES)))
            cur.execute("""
                insert into research.symbol_restriction_runs
                  (as_of, list_sizes, total_rows, collector_version)
                values (%s, %s::jsonb, %s, %s)
                on conflict (as_of) do update set
                  list_sizes = excluded.list_sizes,
                  total_rows = excluded.total_rows,
                  collected_at = now()
            """, (as_of, json.dumps(sizes, ensure_ascii=False), len(rows),
                  COLLECTOR_VERSION))
        conn.commit()
    finally:
        conn.close()

    print(f"{COLLECTOR_VERSION}: {as_of} 제한 {len(rows)}건 {sizes}", flush=True)
    return 0


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _check_snapshot_rows():
    """한 종목이 여러 사유에 걸리면 전부 남긴다 - 사실을 지우지 않는다."""
    got = snapshot_rows({"HALTED": {"000660", "005930"},
                         "ADMINISTERED": {"000660"}})
    assert got == [("000660", "ADMINISTERED"), ("000660", "HALTED"),
                   ("005930", "HALTED")], got
    assert snapshot_rows({}) == []
    assert snapshot_rows({"HALTED": set()}) == []
    print("  스냅샷 행 구성           OK")


def _check_source_tr():
    from universe_manager import RESTRICTION_SOURCES

    assert source_tr_of("HALTED", RESTRICTION_SOURCES) == "t1405"
    assert source_tr_of("ADMINISTERED", RESTRICTION_SOURCES) == "t1404"
    assert source_tr_of("없는사유", RESTRICTION_SOURCES) == "?"
    # 목록 정의와 실제 사유가 어긋나면 여기서 드러난다
    reasons = {r for _, _, r in RESTRICTION_SOURCES}
    assert len(reasons) == len(RESTRICTION_SOURCES), "사유가 중복 정의됐다"
    print("  사유-TR 매핑             OK")


def _check_fail_closed_contract():
    """부분 적재 금지 - fetch 실패가 예외로 터지는지(빈 목록 위장 금지)."""
    import inspect

    from universe_manager import fetch_restricted

    src = inspect.getsource(fetch_restricted)
    assert "raise" in src or "except" not in src, \
        "fetch_restricted 가 실패를 삼키면 정지 종목이 거래가능으로 샌다"
    src2 = inspect.getsource(collect)
    assert "try:" not in src2.split("conn = psycopg2")[0], \
        "목록 조회를 try 로 감싸면 부분 적재가 된다"
    print("  fail-closed 계약         OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--collect" in sys.argv:
        raise SystemExit(collect())

    print(f"{COLLECTOR_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_snapshot_rows()
    _check_source_tr()
    _check_fail_closed_contract()
    print("거래제한 스냅샷 3개 영역 통과. 적재는 --collect")
