#!/usr/bin/env python3
"""전종목 바스켓 생성 - 거래 가능한 상장 보통주 전부.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-02 "종목수 늘려버리자 전종목으로" +
      "거래정지 관리종목 제외".

▶ 범위
  LS 실시간 호가·체결과 일봉 백필이 같은 국내 주식 전종목 파일을 사용한다.
  뉴스·공시·재무 수집 범위에는 재사용하지 않는다. 비시장 정보는 필요할 때
  MCP로 조회한다.

▶ 제외 규칙
  research.symbol_restrictions 의 **모든 사유**를 뺀다. 재일님이 지목한
  거래정지·관리종목을 포함하며, 정리매매·투자경고·투자유의·불성실공시도
  함께 뺀다 - universe_manager 의 거래가능 판정과 **같은 규칙**이어야
  "바스켓에는 있는데 분석 단계에서 제외되는" 어긋남이 안 생긴다.

▶ 스냅샷이 없으면 만들지 않는다
  제한 목록을 모르는 채로 전종목 파일을 쓰면 정지 종목이 섞인다.
  universe_restriction_collector 가 먼저 돌아야 한다(fail-closed).

사용
  python collectors/full_universe_builder.py            # 자체 점검
  python collectors/full_universe_builder.py --build    # 파일 생성
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))

BUILDER_VERSION = "research-full-universe-builder-v1"
KST = timezone(timedelta(hours=9))
OUTPUT = _BASE.parent / "config" / "full_universe.txt"
# 스냅샷이 이보다 오래되면 쓰지 않는다 - 낡은 제한 목록으로 거르면
# 그 사이 정지된 종목이 그대로 들어온다.
MAX_SNAPSHOT_AGE_DAYS = 5


def render(symbols: list[str], *, as_of: str, excluded: dict) -> str:
    """파일 본문. 사람이 열었을 때 **무엇이 왜 빠졌는지** 보이게 쓴다."""
    lines = [
        "# 전종목 바스켓 (거래 가능한 상장 보통주)",
        f"# 생성: {BUILDER_VERSION}, 제한 스냅샷 {as_of}",
        "# 자동 생성 파일 - 직접 고치지 말고 --build 로 다시 만든다.",
        "#",
        "# 제외 사유별 종목 수 (research.symbol_restrictions):",
    ]
    for reason, n in sorted(excluded.items(), key=lambda kv: -kv[1]):
        lines.append(f"#   {reason:20s} {n}")
    lines += [f"# 최종 {len(symbols)}종목", ""]
    lines += symbols
    return "\n".join(lines) + "\n"


def build() -> int:
    import psycopg2
    from source_registry import load_project_env

    conn = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)
    try:
        with conn.cursor() as cur:
            cur.execute("select max(as_of) from research.symbol_restriction_runs")
            row = cur.fetchone()
            as_of = row[0] if row else None
            if as_of is None:
                print("거래제한 스냅샷이 없다 - universe_restriction_collector 를 "
                      "먼저 돌린다(정지 종목이 섞이는 것을 막는다)", flush=True)
                return 1
            age = (datetime.now(KST).date() - as_of).days
            if age > MAX_SNAPSHOT_AGE_DAYS:
                print(f"제한 스냅샷이 {age}일 전({as_of})이라 쓰지 않는다 - "
                      f"그 사이 정지된 종목이 그대로 들어온다", flush=True)
                return 1

            cur.execute("""
                select isym.symbol
                from reference.instruments i
                join reference.instrument_symbols isym
                  on isym.instrument_id = i.instrument_id
                 and isym.provider = 'LS'
                 and isym.market = 'KRX'
                 and isym.symbol_type = 'TRADING'
                 and isym.is_primary
                 and isym.valid_from <= now()
                 and (isym.valid_to is null or isym.valid_to > now())
                where upper(i.market) = 'KRX'
                  and upper(i.asset_class) = 'EQUITY'
                  and upper(i.instrument_type) = 'STOCK'
                  and upper(i.status) = 'ACTIVE'
                  and upper(i.venue) in ('KOSPI', 'KOSDAQ')
                  and lower(coalesce(i.metadata->>'is_spac', 'false')) <> 'true'
                order by isym.symbol
            """)
            all_syms = [r[0] for r in cur.fetchall()]
            duplicate_symbols = sorted(
                symbol for symbol in set(all_syms) if all_syms.count(symbol) > 1
            )
            if duplicate_symbols:
                print(
                    "현재 LS/KRX 대표 심볼 매핑이 중복되어 universe를 만들 수 없다: "
                    f"{duplicate_symbols}",
                    flush=True,
                )
                return 1

            cur.execute("""
                select reason, symbol from research.symbol_restrictions
                where as_of = %s""", (as_of,))
            restricted, by_reason = set(), {}
            for reason, sym in cur.fetchall():
                restricted.add(sym)
                by_reason[reason] = by_reason.get(reason, 0) + 1
    finally:
        conn.close()

    kept = [s for s in all_syms if s not in restricted]
    if not kept:
        print("남는 종목이 0 - 파일을 쓰지 않는다", flush=True)
        return 1

    OUTPUT.write_text(render(kept, as_of=str(as_of), excluded=by_reason),
                      encoding="utf-8")
    print(f"{BUILDER_VERSION}: 상장 {len(all_syms)} - 제한 "
          f"{len(all_syms) - len(kept)} = {len(kept)}종목 -> {OUTPUT.name} "
          f"(스냅샷 {as_of}, {by_reason})", flush=True)
    return 0


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _check_render():
    body = render(["000660", "005930"], as_of="2026-08-02",
                  excluded={"HALTED": 120, "ADMINISTERED": 113})
    assert "000660" in body and "005930" in body
    assert "HALTED" in body and "120" in body, "제외 내역이 안 보인다"
    assert "최종 2종목" in body
    assert body.rstrip().endswith("005930"), "종목이 마지막에 와야 파서가 읽는다"
    # 주석 줄은 전부 # 로 시작해야 parse_symbol_file 이 건너뛴다
    for line in body.splitlines():
        if line and not line[0].isdigit():
            assert line.startswith("#"), f"주석이 아닌 비종목 줄: {line!r}"
    print("  파일 렌더링              OK")


def _check_parser_roundtrip():
    """생성한 파일을 기존 파서가 그대로 읽어야 한다 - 형식이 갈리면 조용히 0종목."""
    from symbol_universe import parse_symbol_file

    body = render(["000660", "005930", "035720"], as_of="2026-08-02",
                  excluded={"HALTED": 1})
    got = list(parse_symbol_file(body))
    assert got == ["000660", "005930", "035720"], got
    print("  기존 파서 왕복           OK")


def _check_exclusion_rule():
    """universe_manager 와 같은 사유 집합을 빼는가 - 어긋나면 바스켓에는 있는데
    분석 단계에서 제외되는 종목이 생긴다."""
    sys.path.insert(0, str(_BASE.parent / "agents"))
    from universe_manager import RESTRICTION_SOURCES

    reasons = {r for _t, _j, r in RESTRICTION_SOURCES}
    assert {"HALTED", "ADMINISTERED"} <= reasons, "지목된 두 사유가 목록에 없다"
    # 이 빌더는 스냅샷의 모든 사유를 빼므로 자동으로 상위집합이다
    assert len(reasons) == 6, f"제한 사유가 {len(reasons)}개로 바뀌었다 - 확인 필요"
    print("  제외 규칙 일치           OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--build" in sys.argv:
        raise SystemExit(build())

    print(f"{BUILDER_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_render()
    _check_parser_roundtrip()
    _check_exclusion_rule()
    print("전종목 바스켓 3개 영역 통과. 생성은 --build")
