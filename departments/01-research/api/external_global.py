#!/usr/bin/env python3
"""글로벌 정보원 질의 도구 - Yahoo(글로벌 시세)·SEC EDGAR(미국 공시).

소유: 재일 (리서치본부)
근거: 재일 결정 2026-08-13 "쓸만한 MCP 싹다 도입, 무료이면서 질 높이는 것".
      KRX 반도체 밸류체인은 미국 문맥(SOX·NVDA·MU 실적/공시) 없이 못 읽는다.

▶ 왜 기성 MCP 대신 직접 부르나
  - yfinance 계열 MCP: 전부 소규모 개인 저장소 + pandas 의존(이미지 +100MB).
    Yahoo v8 chart 공개 엔드포인트는 키·crumb 없이 서빙된다(실측) - 직접 호출.
  - SEC EDGAR MCP(stefanoamorelli): 신뢰할 만하나 전송방식(stdio/http) 확인
    비용 > EDGAR 공식 JSON API 직접 호출 비용. data.sec.gov 는 무료·무키,
    User-Agent 에 연락처만 요구한다(공정접근 정책).
  둘 다 키가 없으므로 공급망 원칙과 무관하게, 규약(예산·비영속 인용 해시·무해석)
  통일을 위해 자작한다.

⚠ 비공식 의존: Yahoo v8 는 문서화된 계약이 아니다. 끊기면 정직하게 실패한다.
자체 점검: python api/external_global.py   (네트워크 없는 검사만)
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime

from external_sources import _get, _snapshot, spend

YAHOO_DAILY_CAP = int(os.environ.get("MCP_YAHOO_DAILY_CAP", "2000"))
SEC_DAILY_CAP = int(os.environ.get("MCP_SEC_DAILY_CAP", "2000"))

# SEC 공정접근 정책: UA 에 소속·연락처를 요구한다.
_SEC_UA = os.environ.get(
    "SEC_USER_AGENT", "hgfinance-research traderjaeil@gmail.com")

_RANGES = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max"}
_INTERVALS = {"1m", "5m", "15m", "1h", "1d", "1wk", "1mo"}


def _yahoo_chart(symbol: str, rng: str, interval: str) -> dict:
    spend("yahoo", YAHOO_DAILY_CAP)
    q = urllib.parse.urlencode({"range": rng, "interval": interval,
                                "events": "div,split"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{q}"
    body = json.loads(_get(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; hgfinance/1.0)"}, timeout=15)
        .decode("utf-8"))
    chart = body.get("chart") or {}
    if chart.get("error"):
        e = chart["error"]
        raise RuntimeError(f"Yahoo [{e.get('code')}] {e.get('description')}")
    results = chart.get("result") or []
    if not results:
        raise RuntimeError(f"Yahoo 응답에 결과가 없다: {symbol}")
    return results[0]


def global_quote(symbols: list) -> dict:
    """글로벌 시세 스냅샷 - 지수(^GSPC·^SOX·^N225)·환율(KRW=X)·원자재(CL=F)·
    미국 종목(NVDA·MU 등). 한 번에 최대 8개."""
    syms = [str(s).strip() for s in (symbols or []) if str(s).strip()][:8]
    if not syms:
        raise RuntimeError("symbols 가 비었다")
    items, failed = [], []
    for s in syms:
        try:
            r = _yahoo_chart(s, "5d", "1d")
            meta = r.get("meta") or {}
            items.append({
                "symbol": meta.get("symbol", s),
                "name": meta.get("shortName") or meta.get("longName"),
                "price": meta.get("regularMarketPrice"),
                "prev_close": meta.get("chartPreviousClose"),
                "currency": meta.get("currency"),
                "exchange": meta.get("fullExchangeName"),
                "market_time": meta.get("regularMarketTime")})
        except Exception as e:  # noqa: BLE001 - 한 심볼 실패가 나머지를 안 막는다
            failed.append({"symbol": s, "error": str(e)[:120]})
    out = {"count": len(items), "items": items, "failed": failed,
           "queried_at": datetime.now().isoformat(),
           "note": "비공식 Yahoo 엔드포인트 - 지연·정확도 보증 없음, 참고 문맥용"}
    out["citation"] = _snapshot("global_quote", {"symbols": syms}, out)
    return out


def global_history(symbol: str, range: str = "3mo",
                   interval: str = "1d") -> dict:
    """글로벌 심볼의 시계열 (Yahoo). range=1d~max, interval=1m~1mo."""
    if range not in _RANGES:
        raise RuntimeError(f"range 는 {sorted(_RANGES)} 중 하나: {range}")
    if interval not in _INTERVALS:
        raise RuntimeError(f"interval 은 {sorted(_INTERVALS)} 중 하나: {interval}")
    r = _yahoo_chart(symbol, range, interval)
    ts = r.get("timestamp") or []
    quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    vols = quote.get("volume") or []
    items = [{"ts": t, "close": c, "volume": v}
             for t, c, v in zip(ts, closes, vols) if c is not None][-500:]
    meta = r.get("meta") or {}
    out = {"symbol": meta.get("symbol", symbol), "currency": meta.get("currency"),
           "range": range, "interval": interval, "count": len(items),
           "items": items, "queried_at": datetime.now().isoformat()}
    out["citation"] = _snapshot("global_history", {
        "symbol": symbol, "range": range, "interval": interval}, out)
    return out


# ── SEC EDGAR ───────────────────────────────────────────────────────────────
_cik_cache: dict | None = None


def _sec_get(url: str) -> bytes:
    spend("sec", SEC_DAILY_CAP)
    return _get(url, headers={"User-Agent": _SEC_UA}, timeout=20)


def _cik_index() -> dict:
    """티커 -> CIK. SEC 공식 매핑 파일(무키), 프로세스 캐시."""
    global _cik_cache
    if _cik_cache is None:
        body = json.loads(_sec_get(
            "https://www.sec.gov/files/company_tickers.json").decode("utf-8"))
        _cik_cache = {v["ticker"].upper(): {
            "cik": int(v["cik_str"]), "name": v["title"]}
            for v in body.values()}
    return _cik_cache


def sec_company(ticker: str) -> dict:
    """미국 티커 -> CIK·회사명. 다른 sec_* 도구의 앞 단계."""
    t = ticker.strip().upper()
    hit = _cik_index().get(t)
    if not hit:
        # 부분일치 후보
        cands = [{"ticker": k, **v} for k, v in _cik_index().items()
                 if t in k][:5]
        out = {"ticker": t, "found": False, "candidates": cands}
    else:
        out = {"ticker": t, "found": True, **hit}
    out["citation"] = _snapshot("sec_company", {"ticker": ticker}, out)
    return out


def sec_filings(ticker: str, form: str = "", limit: int = 10) -> dict:
    """미국 기업 최근 공시 목록 (10-K·10-Q·8-K·Form4 등). form 으로 필터.

    각 건에 문서 URL 이 붙는다 - 본문은 read_url 로 열람.
    """
    t = ticker.strip().upper()
    hit = _cik_index().get(t)
    if not hit:
        raise RuntimeError(f"티커를 찾지 못했다: {t} - sec_company 로 확인할 것")
    cik = hit["cik"]
    body = json.loads(_sec_get(
        f"https://data.sec.gov/submissions/CIK{cik:010d}.json").decode("utf-8"))
    recent = (body.get("filings") or {}).get("recent") or {}
    n = max(1, min(int(limit), 40))
    items = []
    forms = recent.get("form", [])
    for i in range(len(forms)):
        if form and forms[i].upper() != form.strip().upper():
            continue
        acc = recent["accessionNumber"][i].replace("-", "")
        doc = recent["primaryDocument"][i]
        items.append({
            "form": forms[i], "filed": recent["filingDate"][i],
            "accession": recent["accessionNumber"][i],
            "title": recent.get("primaryDocDescription", [""] * len(forms))[i],
            "doc_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"})
        if len(items) >= n:
            break
    out = {"ticker": t, "cik": cik, "name": hit["name"], "form_filter": form,
           "count": len(items), "items": items}
    out["citation"] = _snapshot("sec_filings", {
        "ticker": ticker, "form": form, "limit": limit}, out)
    return out


def sec_concept(ticker: str, concept: str = "Revenues",
                limit: int = 12) -> dict:
    """미국 기업의 XBRL 재무 개념 시계열 (us-gaap). 예: Revenues,
    NetIncomeLoss, Assets, ResearchAndDevelopmentExpense.

    미제출·개념 부재는 그대로 오류로 돌려준다 - 다른 개념명을 시도하라.
    """
    t = ticker.strip().upper()
    hit = _cik_index().get(t)
    if not hit:
        raise RuntimeError(f"티커를 찾지 못했다: {t}")
    cik = hit["cik"]
    tag = re.sub(r"[^A-Za-z]", "", concept)
    body = json.loads(_sec_get(
        f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
    ).decode("utf-8"))
    n = max(1, min(int(limit), 40))
    units = body.get("units") or {}
    unit_key = next(iter(units), None)
    rows = units.get(unit_key, [])
    # 파일 내 순서는 보장이 없다(실측: NVDA 가 2022 를 마지막에 실음) -
    # end 날짜로 정렬해 최근 것부터 낸다. 같은 기간의 정정 재공시는 filed 최신 우선.
    rows = sorted(rows, key=lambda r: (r.get("end") or "", r.get("filed") or ""),
                  reverse=True)
    items = [{"period_end": r.get("end"), "value": r.get("val"),
              "fy": r.get("fy"), "fp": r.get("fp"), "form": r.get("form"),
              "filed": r.get("filed")} for r in rows[:n]]
    out = {"ticker": t, "concept": body.get("tag", tag), "unit": unit_key,
           "label": body.get("label"), "count": len(items), "items": items}
    out["citation"] = _snapshot("sec_concept", {
        "ticker": ticker, "concept": concept}, out)
    return out


ARXIV_DAILY_CAP = int(os.environ.get("MCP_ARXIV_DAILY_CAP", "500"))


def arxiv_search(query: str, category: str = "", max_results: int = 8) -> dict:
    """arXiv 논문 검색 (무키 공식 API) - 방법론 스카우트 축.

    category 예: q-fin.ST, q-fin.PM, cs.LG. 결과의 pdf/abs URL 은
    read_url 로 이어 읽을 수 있다(초록 페이지 권장).
    """
    import xml.etree.ElementTree as ET
    spend("arxiv", ARXIV_DAILY_CAP)
    n = max(1, min(int(max_results), 25))
    terms = f"all:{query}"
    if category.strip():
        terms = f"cat:{category.strip()}+AND+({terms})"
    q = (f"http://export.arxiv.org/api/query?search_query={urllib.parse.quote(terms, safe=':+()')}"
         f"&start=0&max_results={n}&sortBy=submittedDate&sortOrder=descending")
    xml = _get(q, timeout=20).decode("utf-8")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = []
    for e in ET.fromstring(xml).findall("a:entry", ns):
        items.append({
            "title": " ".join((e.findtext("a:title", "", ns) or "").split()),
            "published": e.findtext("a:published", "", ns)[:10],
            "authors": [a.findtext("a:name", "", ns)
                        for a in e.findall("a:author", ns)][:5],
            "abs_url": e.findtext("a:id", "", ns),
            "summary": " ".join((e.findtext("a:summary", "", ns) or "").split())[:400]})
    out = {"query": query, "category": category, "count": len(items),
           "items": items, "queried_at": datetime.now().isoformat()}
    out["citation"] = _snapshot("arxiv_search", {
        "query": query, "category": category}, out)
    return out


def register_global_tools(server) -> None:
    """mcp_server.build_server() 가 부른다. 전부 읽기 전용."""
    server.tool(
        name="global_quote",
        description="글로벌 시세 스냅샷 - 지수(^GSPC·^SOX·^N225), 환율(KRW=X), "
                    "원자재(CL=F·GC=F), 미국 종목(NVDA·MU). 최대 8개. "
                    "비공식 Yahoo 라 참고 문맥용 - 정밀 시세 아님.")(global_quote)
    server.tool(
        name="global_history",
        description="글로벌 심볼 시계열(Yahoo). range=1d~max, interval=1m~1mo.")(global_history)
    server.tool(
        name="sec_company",
        description="미국 티커 -> SEC CIK·회사명 (sec_* 도구의 앞 단계).")(sec_company)
    server.tool(
        name="sec_filings",
        description="미국 기업 최근 공시(10-K·10-Q·8-K·4 등) 목록 + 문서 URL. "
                    "본문은 read_url 로 열람. 엔비디아·마이크론 등 밸류체인 문맥용.")(sec_filings)
    server.tool(
        name="arxiv_search",
        description="arXiv 논문 검색(최신순) - 방법론 스카우트용. category 예: "
                    "q-fin.ST, q-fin.PM. 초록은 abs_url 을 read_url 로.")(arxiv_search)
    server.tool(
        name="sec_concept",
        description="미국 기업 XBRL 재무 개념 시계열(us-gaap) - 공시 원값. "
                    "⚠ 매출은 2018년 이후 대부분 Revenues 가 아니라 "
                    "RevenueFromContractWithCustomerExcludingAssessedTax 태그다 - "
                    "값이 낡았으면 대체 태그를 시도하라.")(sec_concept)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        global_history("^GSPC", range="bad")
        raise AssertionError("잘못된 range 가 통과했다")
    except RuntimeError as e:
        assert "range" in str(e)
        print("  range 검증              OK")
    assert re.sub(r"[^A-Za-z]", "", "Net-Income_Loss;") == "NetIncomeLoss"
    print("  concept 정화             OK")
    assert "@" in _SEC_UA, "SEC UA 에 연락처가 없다"
    print("  SEC UA 정책              OK")
    print("자체 점검 통과")
