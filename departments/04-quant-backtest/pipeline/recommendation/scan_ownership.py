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
from ownership_flow import (aggregate, normalize_date, parse_filings,
                            rank_by_observation)

DAYS = int(os.environ.get("SCAN_DAYS", "14"))
TOP_N = int(os.environ.get("SCAN_TOP", "15"))
MAX_CORPS = int(os.environ.get("SCAN_MAX_CORPS", "60"))
OUT = os.environ.get("SCAN_OUT", "/tmp/ownership_scan.json")
PAGE = 100
# DART 는 남의 공용 API 다. 계산이 아니라 네트워크 대기가 전부라 동시성이
# 곧 속도지만, 두들기지 않으려고 보수적으로 잡는다.
CONCURRENCY = int(os.environ.get("SCAN_CONCURRENCY", "6"))


def _day(offset: int) -> str:
    from datetime import date, timedelta
    return (date.today() - timedelta(days=offset)).strftime("%Y%m%d")


def main() -> int:
    t0 = time.time()
    begin, end = _day(DAYS), _day(0)
    print(f"지분공시 스캔 {begin} ~ {end}", flush=True)

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
        "note": ("지분공시는 후행 지표다(5% 룰은 5영업일 내 보고). "
                 "'기관이 샀다'가 '오른다'는 뜻이 아니며 그 관계는 측정하지 않았다."),
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)

    print(f"\n{'종목':>8} {'회사':<14} {'장내순증':>8} {'매수자':>5}  주요 매수자")
    print("-" * 78)
    for a in ranked[:TOP_N]:
        d = a.as_dict()
        print(f"{d['symbol']:>8} {d['company'][:13]:<14} "
              f"{d['net_market_buy_ratio_pp']:>7.2f}%p {d['buyer_count']:>4}  "
              f"{', '.join(d['buyers'][:3])[:34]}")
    print(f"\n{len(ranked)}종목 매집 관측 -> {OUT}   ({time.time()-t0:.0f}초)")
    print("budget:", ex.budget_state())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
