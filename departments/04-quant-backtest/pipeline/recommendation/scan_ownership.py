"""지분공시 매집 스캔 - 시장 전체에서 "누가 사 모으고 있나"를 뽑는다.

research-mcp 안에서 돈다(DART 자격이 거기 있다).

    docker exec -e SCAN_DAYS=14 hedgefund-research-mcp python /tmp/scan_ownership.py

DART 예산: `list.json` 1회 + 종목별 상세 1~2회. 하루 캡 2,000 이라 넉넉하다.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, "/app/departments/01-research/api")
sys.path.insert(0, "/app/departments/01-research/evidence")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import external_sources as ex
from ownership_flow import (CONTROLLING, INSIDER, INSTITUTION, aggregate,
                            normalize_date, parse_filings,
                            by_holder, rank_by_buyer_type,
                            rank_by_observation)

DAYS = int(os.environ.get("SCAN_DAYS", "14"))
TOP_N = int(os.environ.get("SCAN_TOP", "15"))
# 기본은 **전수**다. 60곳으로 자르면 상장사 509곳 중 12% 만 보고,
# 실측 2026-08-25 비교: 60곳 -> 7종목(24초) vs 전수 -> 34종목(83초).
# 3.5배 시간에 4.9배 후보이고, 잘린 쪽에 만호제강(+5.53%p)·대교(+3.99%p)·
# 미코(MiriCapitalManagementLLC +1.72%p) 같은 것들이 들어 있었다.
MAX_CORPS = int(os.environ.get("SCAN_MAX_CORPS", "999"))
OUT = os.environ.get("SCAN_OUT", "/tmp/ownership_scan.json")
PAGE = 100
# DART 는 남의 공용 API 다. 계산이 아니라 네트워크 대기가 전부라 동시성이
# 곧 속도지만, 두들기지 않으려고 보수적으로 잡는다.
CONCURRENCY = int(os.environ.get("SCAN_CONCURRENCY", "6"))
# 지분공시는 하루 단위로 바뀐다. 같은 창을 반복 스캔하는 것은 낭비이고
# DART 예산도 먹는다. 0 으로 두면 캐시를 끈다.
CACHE_TTL = int(os.environ.get("SCAN_CACHE_TTL", "21600"))  # 6시간


def _day(offset: int) -> str:
    from datetime import date, timedelta
    return (date.today() - timedelta(days=offset)).strftime("%Y%m%d")


def _fresh_cache(begin: str, end: str) -> dict | None:
    """같은 창을 이미 스캔했으면 재사용한다. 창이 다르면 무효다."""
    if CACHE_TTL <= 0 or not os.path.exists(OUT):
        return None
    if time.time() - os.path.getmtime(OUT) > CACHE_TTL:
        return None
    try:
        cached = json.load(open(OUT, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    w = cached.get("window") or {}
    return cached if (w.get("begin") == begin and w.get("end") == end) else None


def main() -> int:
    t0 = time.time()
    begin, end = _day(DAYS), _day(0)
    print(f"지분공시 스캔 {begin} ~ {end}", flush=True)

    cached = _fresh_cache(begin, end)
    if cached is not None:
        age = int(time.time() - os.path.getmtime(OUT))
        print(f"캐시 사용 ({age}초 전, 후보 {len(cached.get('candidates', []))}개) "
              f"-> {OUT}   [SCAN_CACHE_TTL=0 으로 끌 수 있다]", flush=True)
        for label in ("by_institution", "by_controlling"):
            for c in cached.get(label, [])[:5]:
                v = c.get("institution_ratio_pp") if label.endswith(
                    "institution") else c.get("net_market_buy_ratio_pp")
                print(f"  [{label[3:]}] {c['symbol']} "
                      f"{c['company'][:12]} {v:.2f}%p")
        return 0

    # 1) 시장 전체 지분공시 목록 (pblntf_ty=D).
    #    첫 페이지로 총 페이지 수를 안 뒤 나머지를 **동시에** 받는다.
    def _page(n: int) -> list[dict]:
        r = ex._dart_json("list.json", bgn_de=begin, end_de=end,
                          pblntf_ty="D", page_no=str(n), page_count=str(PAGE))
        if r.get("status") != "000":
            print(f"  list.json page {n}: {r.get('status')} {r.get('message')}")
            return []
        return r.get("list") or []

    first = ex._dart_json("list.json", bgn_de=begin, end_de=end, pblntf_ty="D",
                          page_no="1", page_count=str(PAGE))
    listings = list(first.get("list") or [])
    total_pages = min(int(first.get("total_page") or 1), 12)
    if total_pages > 1:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            for rows in pool.map(_page, range(2, total_pages + 1)):
                listings.extend(rows)
    print(f"공시 {len(listings)}건 ({total_pages}페이지)", flush=True)

    # 2) 종목코드가 있는 상장사만, 공시가 많은 순으로 상세 조회
    by_corp: dict[str, list[dict]] = defaultdict(list)
    for row in listings:
        if str(row.get("stock_code") or "").strip():
            by_corp[str(row["corp_code"])].append(row)
    ordered = sorted(by_corp.items(), key=lambda kv: -len(kv[1]))[:MAX_CORPS]
    print(f"상장사 {len(by_corp)}곳 -> 상세조회 {len(ordered)}곳", flush=True)

    def fetch(corp_code: str, endpoint: str, source: str,
              stock_code: str) -> list:
        """상세 한 건. 실패는 빈 리스트 - 한 종목이 전체를 못 죽인다."""
        try:
            d = ex._dart_json(endpoint, corp_code=corp_code)
        except Exception as exc:  # noqa: BLE001
            print(f"  {corp_code} {endpoint}: {type(exc).__name__}", flush=True)
            return []
        if d.get("status") != "000":
            return []
        enriched = [{**item, "stock_code": item.get("stock_code") or stock_code}
                    for item in (d.get("list") or [])]
        # 조회 구간 안의 공시만 센다 - 상세 API 는 **과거 것까지** 준다.
        # 날짜 형식이 목록(20260825)과 상세(2026-08-25)가 달라 정규화가 필수다.
        enriched = [i for i in enriched
                    if begin <= normalize_date(i.get("rcept_dt")) <= end]
        return parse_filings(enriched, source=source)

    # ▶ 순위 계산에는 **majorstock 만** 쓴다. elestock(임원·주요주주)은 사유
    #   필드가 없어 항상 UNCLASSIFIED 이고 net_ratio 에 들어가지 않는다 -
    #   순위를 바꾸지 못하는 호출을 60곳 전부에 돌릴 이유가 없다(실측: 104회 중
    #   44회가 이것이었다). 최종 후보에만 뒤에서 붙인다.
    jobs = [(corp, "majorstock.json", "majorstock", rows[0].get("stock_code"))
            for corp, rows in ordered
            if any("대량보유" in r.get("report_nm", "") for r in rows)]
    filings = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(fetch, *j): j for j in jobs}
        for fut in as_completed(futures):
            filings.extend(fut.result())
    detail_calls = len(jobs)
    print(f"대량보유 상세 {detail_calls}회(동시성 {CONCURRENCY}), "
          f"파싱 {len(filings)}건", flush=True)

    books = by_holder(filings)
    accs = aggregate(filings)
    ranked = rank_by_observation(accs)

    # 최종 후보에만 임원·주요주주 공시를 붙인다(순위는 이미 정해졌다).
    corp_of = {rows[0].get("stock_code"): corp for corp, rows in ordered}
    ele_jobs = [(corp_of[a.symbol], "elestock.json", "elestock", a.symbol)
                for a in ranked[:TOP_N] if a.symbol in corp_of]
    if ele_jobs:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            extra = [f for fut in as_completed(
                        [pool.submit(fetch, *j) for j in ele_jobs])
                     for f in fut.result()]
        by_symbol = {a.symbol: a for a in ranked}
        for f in extra:
            acc = by_symbol.get(f.symbol)
            if acc is not None:
                acc.filings.append(f)
        detail_calls += len(ele_jobs)
        print(f"임원공시 {len(ele_jobs)}회 추가, {len(extra)}건", flush=True)
    payload = {
        "window": {"begin": begin, "end": end, "days": DAYS},
        "listing_count": len(listings),
        "listed_corps": len(by_corp),
        "detail_calls": detail_calls,
        "parsed_filings": len(filings),
        "candidates": [a.as_dict() for a in ranked[:TOP_N]],
        # 유형별 목록. 지배주주 거래와 외부 기관 거래는 성격이 다르다.
        "by_institution": [a.as_dict()
                           for a in rank_by_buyer_type(accs, INSTITUTION)[:TOP_N]],
        "by_controlling": [a.as_dict()
                           for a in rank_by_buyer_type(accs, CONTROLLING)[:TOP_N]],
        "by_insider": [a.as_dict()
                       for a in rank_by_buyer_type(accs, INSIDER)[:TOP_N]],
        # 13F 는 종목이 아니라 **보유자**가 주어다. 한 운용사가 여러
        # 종목을 같은 기간에 담았다면 종목별 %p 보다 그게 신호다.
        "by_holder": [b.as_dict() for b in books[:12]],
        "note": ("지분공시는 후행 지표다(5% 룰은 5영업일 내 보고). "
                 "'기관이 샀다'가 '오른다'는 뜻이 아니며 그 관계는 측정하지 않았다."),
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)

    def show(title, rows, key):
        if not rows:
            print(f"\n[{title}] 해당 없음")
            return
        print(f"\n[{title}]")
        print(f"{'종목':>8} {'회사':<14} {'순증':>8}  매수자")
        print("-" * 68)
        for d in rows:
            v = d.get(key, 0.0)
            print(f"{d['symbol']:>8} {d['company'][:13]:<14} {v:>7.2f}%p  "
                  f"{', '.join(d['buyers'][:2])[:32]}")

    # 유형을 섞어 한 줄로 세우면 지배주주가 늘 위로 온다 - 지분을 크게
    # 움직이는 쪽이라서지 신호가 강해서가 아니다. 따로 낸다.
    show("외부 기관 매수", payload["by_institution"], "institution_ratio_pp")
    show("지배주주·계열 매수", payload["by_controlling"][:8], "net_market_buy_ratio_pp")
    show("임원·개인 매수", payload["by_insider"][:6], "net_market_buy_ratio_pp")

    multi = [b for b in payload["by_holder"] if b["position_count"] >= 2]
    if multi:
        print("\n[여러 종목을 담은 매수자]")
        for b in multi[:6]:
            names = ", ".join(f"{q['company'][:8]}({q['ratio_change_pp']:.2f}%p)"
                              for q in b["positions"][:4])
            print(f"  {b['holder'][:26]:28s} [{b['buyer_type']}] "
                  f"{b['position_count']}종목  {names}")
    print(f"\n{len(ranked)}종목 매집 관측 -> {OUT}   ({time.time()-t0:.0f}초)")
    print("budget:", ex.budget_state())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
