#!/usr/bin/env python3
"""뉴스 감시 Watchlist 생성 - LS t1444 시가총액 상위 기반.

담당: 재일 (리서치/퀀트)
근거: 재일님 지적(2026-07-31) "구독 종목수가 적네" + 가이드 Sprint J3
      (공시건수 대리지표는 증권사로 쏠린다 - 시가총액 Source 가 생기면 교체)

▶ 왜 오프라인 생성인가
  news-watcher Container 에는 **LS Credential 을 주지 않는다**(compose 가 필요한
  것만 주입 - DATABASE_URL·NAVER 키뿐). 시가총액 순위는 하루에도 안 변하는
  수준이라, 이 스크립트를 호스트에서 돌려 파일로 떨궈 두고 Container 는 파일만
  읽는다. 갱신 주기는 사람이 정한다(주 1회면 충분).

▶ 우선주 처리
  t1444 는 삼성전자우(005935) 같은 우선주도 시총 상위에 올린다. 뉴스 질의는
  종목명 기반이라 우선주 질의는 보통주와 같은 기사를 다른 이름으로 찾는 낭비다.
  이름 규칙(끝의 '우')은 오탐이 있으므로 **같은 issuer 의 두 번째 종목을 버리는**
  방식으로 걸러낸다 - Instrument Master 의 issuer 연결이 판정 근거다.

▶ NAVER 일 한도 검산
  news_watch_service 의 ensure_quota_headroom 과 같은 식으로, 목표 종목 수 ×
  폴링 횟수가 한도의 90% 를 넘으면 파일을 쓰기 전에 거부한다.

사용
  python collectors/watchlist_builder.py                # 자체 점검 (호출 없음)
  python collectors/watchlist_builder.py --build        # t1444 조회 -> 파일 생성
    옵션: --kospi 55 --kosdaq 15 --interval 300
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))

from naver_news_collector import DAILY_QUOTA  # noqa: E402
from news_watch_service import (  # noqa: E402
    QUOTA_SOFT_RATIO,
    ensure_quota_headroom,
    parse_watchlist_file,
)

BUILDER_VERSION = "research-news-watchlist-builder-v1"
KST = timezone(timedelta(hours=9))

T1444_PATH = "/stock/high-item"
T1444_RATE_LIMIT = 2.0  # 문서 "초당 호출 제한: 2"
T1444_PAGE = 20         # 실측 2026-07-31: idx 페이징, 페이지당 20행

UPCODE = {"KOSPI": "001", "KOSDAQ": "301"}  # market_breadth_collector 와 동일

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "config" / "news_watchlist.txt"


class WatchlistBuildError(RuntimeError):
    """Watchlist 를 만들 수 없다. 빈 목록·모르는 종목으로 대충 채우지 않는다."""


@dataclass(frozen=True)
class RankedSymbol:
    symbol: str
    name: str
    market_cap: int  # t1444 total (백만원)
    venue: str


def fetch_top_mcap(client, *, venue: str, count: int) -> list[RankedSymbol]:
    """t1444 를 idx 페이징으로 돌아 시총 상위 count 개를 가져온다."""
    upcode = UPCODE.get(venue)
    if upcode is None:
        raise WatchlistBuildError(f"모르는 venue 다: {venue}")

    out: list[RankedSymbol] = []
    seen: set[str] = set()
    idx = 0
    tr_cont, tr_cont_key = "N", ""
    while len(out) < count:
        resp, hdrs = client.call_tr(
            path=T1444_PATH, tr_cd="t1444",
            in_block={"t1444InBlock": {"upcode": upcode, "idx": idx}},
            rate_limit_per_sec=T1444_RATE_LIMIT,
            tr_cont=tr_cont, tr_cont_key=tr_cont_key,
            return_headers=True,
        )
        rsp_cd = str(resp.get("rsp_cd", ""))
        if rsp_cd != "00000":
            raise WatchlistBuildError(f"t1444 거절: {rsp_cd} {resp.get('rsp_msg')}")
        rows = resp.get("t1444OutBlock1") or []
        if not rows:
            break  # 상위 목록이 여기까지다 - 있는 만큼만 쓴다
        added = 0
        for r in rows:
            sym = str(r["shcode"]).strip()
            if sym in seen:
                continue
            seen.add(sym)
            added += 1
            out.append(RankedSymbol(
                symbol=sym,
                name=str(r["hname"]).strip(),
                market_cap=int(r.get("total") or 0),
                venue=venue,
            ))
        if added == 0:
            break  # 끝에서 같은 페이지가 반복되면 끝난 것이다 (자체 점검이 잡은 함정)
        # 연속조회는 응답 헤더가 계약이다 - tr_cont "Y" 가 아니면 더 없다
        if str(hdrs.get("tr_cont", "")).upper() != "Y":
            break
        tr_cont, tr_cont_key = "Y", str(hdrs.get("tr_cont_key", ""))
        idx = int((resp.get("t1444OutBlock") or {}).get("idx") or 0)
    return out[:count]


# 우선주 종목명 접미사. 보통주 이름 + 이 접미사 **정확 일치**만 우선주로 본다 -
# startswith 만 쓰면 'LG' 를 보고 'LG디스플레이' 를 버리는 오탐이 난다.
PREFERRED_SUFFIXES = ("우", "우B", "1우", "1우B", "2우", "2우B", "3우", "3우B")


def _is_preferred_variant(name: str, kept_names: set[str]) -> bool:
    for kn in kept_names:
        if name.startswith(kn) and name[len(kn):] in PREFERRED_SUFFIXES:
            return True
    return False


def dedupe_by_issuer(
    ranked: list[RankedSymbol], issuer_by_symbol: dict[str, object]
) -> tuple[list[RankedSymbol], list[tuple[str, str]]]:
    """같은 회사의 두 번째 이후 종목(우선주 등)을 버린다. 판정은 두 겹이다.

    1. issuer 연결이 같으면 버린다 - Instrument Master 가 근거다.
    2. 우선주는 issuer 연결이 없는 경우가 많다(DART corp_code 매핑이 보통주
       stock_code 만 준다 - 실측 2026-07-31 삼성전자우가 살아남았다). 그래서
       이름 규칙을 보조로 쓴다: **목록 전체의 다른 이름** + PREFERRED_SUFFIXES
       정확 일치. '남긴 이름' 만 보면 우선주가 보통주보다 먼저 온 경우를 놓친다.
       그 밖의 미연결 종목은 판정 근거가 없으므로 버리지 않는다.
    """
    all_names: set[str] = {r.name for r in ranked}
    seen: set = set()
    kept: list[RankedSymbol] = []
    dropped: list[tuple[str, str]] = []
    for r in ranked:
        iid = issuer_by_symbol.get(r.symbol)
        if (iid is not None and iid in seen) or (
            iid is None and _is_preferred_variant(r.name, all_names - {r.name})
        ):
            dropped.append((r.symbol, r.name))
            continue
        if iid is not None:
            seen.add(iid)
        kept.append(r)
    return kept, dropped


def render_file(kept: list[RankedSymbol], *, interval_seconds: float, built_at: datetime) -> str:
    """news_watchlist.txt 내용. 형식 계약은 news_watch_service.parse_watchlist_file 이다."""
    lines = [
        "# 뉴스 감시 Watchlist - watchlist_builder.py 가 생성한다. 손으로 고쳐도 된다.",
        f"# 생성: {built_at:%Y-%m-%d %H:%M} KST / 기준: LS t1444 시가총액 상위",
        f"# {len(kept)}종목, 폴링 {interval_seconds:.0f}초 간격 기준 일 호출 "
        f"{int(86400 / interval_seconds) * len(kept):,}회 (한도 {DAILY_QUOTA:,}의 "
        f"{QUOTA_SOFT_RATIO:.0%} 이내 검산 완료)",
    ]
    for r in kept:
        lines.append(f"{r.symbol}  # {r.venue} {r.name}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def _build(kospi: int, kosdaq: int, interval: float, output: Path) -> int:
    from ls_client import LsRestClient
    from reference_repository import SupabaseReferenceRepository

    # 시작 전에 한도 검산 - 파일을 쓰고 나서 서비스가 기동 거부하면 늦다
    ensure_quota_headroom(kospi + kosdaq, interval)

    client = LsRestClient()
    ranked = fetch_top_mcap(client, venue="KOSPI", count=kospi + 10)
    ranked += fetch_top_mcap(client, venue="KOSDAQ", count=kosdaq + 5)
    print(f"  t1444 수신: {len(ranked)}종목 (여유분 포함)")

    ref = SupabaseReferenceRepository()
    try:
        with ref._conn.cursor() as cur:
            cur.execute(
                """
                select s.symbol, i.issuer_id
                from reference.instruments i
                join reference.instrument_symbols s using (instrument_id)
                where i.market = 'KRX' and i.instrument_type = 'STOCK'
                  and i.status = 'ACTIVE' and s.symbol = any(%s)
                """,
                ([r.symbol for r in ranked],),
            )
            found = dict(cur.fetchall())
    finally:
        ref.close()

    # Instrument Master 에 없는 종목은 뉴스 연결을 못 하므로 뺀다 - 몇 개인지 남긴다
    missing = [r for r in ranked if r.symbol not in found]
    ranked = [r for r in ranked if r.symbol in found]
    if missing:
        print(f"  ⚠ Instrument Master 에 없어 제외: "
              f"{', '.join(f'{r.name}({r.symbol})' for r in missing[:6])}")

    kept, dropped = dedupe_by_issuer(ranked, found)
    if dropped:
        print(f"  같은 발행사 중복 제외 {len(dropped)}건: "
              f"{', '.join(f'{n}({s})' for s, n in dropped[:6])}")

    by_venue: dict[str, list[RankedSymbol]] = {"KOSPI": [], "KOSDAQ": []}
    for r in kept:
        by_venue[r.venue].append(r)
    final = by_venue["KOSPI"][:kospi] + by_venue["KOSDAQ"][:kosdaq]
    if len(final) < (kospi + kosdaq) * 0.8:
        raise WatchlistBuildError(
            f"목표 {kospi + kosdaq}종목 중 {len(final)}개뿐이다 - 원인을 보고 다시"
        )

    ensure_quota_headroom(len(final), interval)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_file(final, interval_seconds=interval, built_at=datetime.now(KST)),
        encoding="utf-8",
    )
    print(f"  {output} 에 {len(final)}종목 "
          f"(KOSPI {len(by_venue['KOSPI'][:kospi])} / KOSDAQ {len(by_venue['KOSDAQ'][:kosdaq])})")
    print(f"  적용: docker compose restart news-watcher")
    return 0


# ---------------------------------------------------------------------------
# 자체 점검 - 호출·DB 없이
# ---------------------------------------------------------------------------

class _FakeT1444Client:
    """실측 규격대로: 연속 여부는 헤더 tr_cont 로, 잘못된 연속 요청은 1페이지 반복."""

    def __init__(self, pages: dict[str, list[list[dict]]]) -> None:
        self._pages = pages
        self.calls = 0

    def call_tr(self, *, path, tr_cd, in_block, rate_limit_per_sec,
                tr_cont="N", tr_cont_key="", return_headers=False):
        self.calls += 1
        blk = in_block["t1444InBlock"]
        pages = self._pages[blk["upcode"]]
        idx = blk["idx"]
        page_no = idx // T1444_PAGE
        if page_no > 0 and tr_cont != "Y":
            page_no = 0  # 실측: tr_cont 없이 다음 idx 를 청하면 1페이지가 반복된다
        rows = pages[page_no] if page_no < len(pages) else []
        body = {
            "rsp_cd": "00000", "rsp_msg": "조회완료",
            "t1444OutBlock": {"idx": idx + len(rows)},
            "t1444OutBlock1": rows,
        }
        more = page_no + 1 < len(pages)
        hdrs = {"tr_cont": "Y" if more else "N", "tr_cont_key": f"k{page_no}"}
        return (body, hdrs) if return_headers else body


def _row(sym: str, name: str, total: int) -> dict:
    return {"shcode": sym, "hname": name, "total": total}


def _check_paging():
    pages = {"001": [
        [_row(f"{i:06d}", f"기업{i}", 1000 - i) for i in range(20)],
        [_row(f"{i:06d}", f"기업{i}", 1000 - i) for i in range(20, 40)],
    ]}
    c = _FakeT1444Client(pages)
    got = fetch_top_mcap(c, venue="KOSPI", count=30)
    assert len(got) == 30 and c.calls == 2, (len(got), c.calls)
    assert got[0].symbol == "000000" and got[-1].symbol == "000029"

    # 목록이 짧으면 있는 만큼만 - 헤더 tr_cont "N" 에서 즉시 멈춘다
    c2 = _FakeT1444Client({"001": [[_row("000001", "하나", 10)]]})
    got2 = fetch_top_mcap(c2, venue="KOSPI", count=50)
    assert len(got2) == 1 and c2.calls == 1, (len(got2), c2.calls)

    # 3페이지 연속조회 - tr_cont_key 를 되돌려주며 끝까지 간다
    pages3 = {"001": [
        [_row(f"{i:06d}", f"기업{i}", 900 - i) for i in range(s, s + 20)]
        for s in (0, 20, 40)
    ]}
    c3 = _FakeT1444Client(pages3)
    got3 = fetch_top_mcap(c3, venue="KOSPI", count=55)
    assert len(got3) == 55 and c3.calls == 3, (len(got3), c3.calls)
    print("  t1444 페이징             OK")


def _check_dedupe():
    ranked = [
        RankedSymbol("005930", "삼성전자", 100, "KOSPI"),
        RankedSymbol("005935", "삼성전자우", 10, "KOSPI"),    # 같은 issuer - 버린다
        RankedSymbol("000660", "SK하이닉스", 90, "KOSPI"),
        RankedSymbol("003550", "LG", 80, "KOSPI"),
        RankedSymbol("034220", "LG디스플레이", 40, "KOSPI"),  # 우선주 아님 - 남긴다
        RankedSymbol("066570", "LG전자우", 8, "KOSPI"),       # issuer 미연결 우선주 - 이름 규칙으로 버린다
        RankedSymbol("066571", "LG전자", 70, "KOSPI"),
        RankedSymbol("999999", "연결없음", 5, "KOSPI"),       # issuer 미연결 - 남긴다
    ]
    issuers = {"005930": "i-samsung", "005935": "i-samsung", "000660": "i-hynix",
               "003550": "i-lg", "034220": "i-lgd", "066571": "i-lge"}
    kept, dropped = dedupe_by_issuer(ranked, issuers)
    assert [r.symbol for r in kept] == [
        "005930", "000660", "003550", "034220", "066571", "999999"
    ], [r.symbol for r in kept]
    assert ("005935", "삼성전자우") in dropped and ("066570", "LG전자우") in dropped
    print("  발행사 중복 제거         OK")


def _check_render_parse():
    kept = [RankedSymbol("005930", "삼성전자", 100, "KOSPI"),
            RankedSymbol("247540", "에코프로비엠", 50, "KOSDAQ")]
    text = render_file(kept, interval_seconds=300.0,
                       built_at=datetime(2026, 7, 31, 11, 0, tzinfo=KST))
    assert parse_watchlist_file(text) == ("005930", "247540")
    # 주석과 빈 줄은 무시하고, 주석 뒤 코드는 살아남지 않는다
    assert parse_watchlist_file("# 주석\n\n005930 # 삼성전자\n") == ("005930",)
    print("  파일 왕복                OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--build" in sys.argv:
        def opt(name: str, default):
            if name in sys.argv:
                return type(default)(sys.argv[sys.argv.index(name) + 1])
            return default

        print(f"{BUILDER_VERSION} 생성")
        raise SystemExit(_build(
            kospi=opt("--kospi", 55), kosdaq=opt("--kosdaq", 15),
            interval=opt("--interval", 300.0), output=DEFAULT_OUTPUT,
        ))

    print(f"{BUILDER_VERSION} 자체 점검 (호출 없음)")
    _check_paging()
    _check_dedupe()
    _check_render_parse()
    print("Watchlist Builder 3개 영역 통과. 생성은 --build")
