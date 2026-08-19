#!/usr/bin/env python3
"""거시 지표 질의 도구 (ECOS·FRED) - 내린 macro 수집기의 조회식 대체.

소유: 재일 (리서치본부)
근거: MCP_ONDEMAND_ARCHITECTURE.md §3 "ECOS 는 MCP 부재 - 자작 필요" 집행.
      macro 수집기는 2026-08-12 내렸고(적재 중단), 이 모듈이 그 자리의
      **질의 응대** 몫이다. 팩터·백테스트가 이 결과를 쓰면 안 되는 이유는
      ECOS 는 vintage 를 주지 않아 "지금 조회한 최신값" 만 온다(개정 전 값
      복원 불가). 따라서 이 응답은 운영 질의용이며 백테스트 입력이 아니다.

▶ 카탈로그 우선 (LS TR 카탈로그와 같은 패턴)
  ECOS 통계표는 수천 개다. ecos_search 로 통계표를 찾고 ecos_series 로
  값을 받는 2단계 - FinAgentBench 의 "어느 문서 -> 어느 조항" 그대로다.
  FRED 도 fred_search -> fred_series 같은 짝이다.

예산·비영속 인용 해시·정직성 규약은 external_sources 의 것을 그대로 쓴다.
자체 점검: python api/external_macro.py   (네트워크 없는 검사만)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime

from external_sources import _get, _snapshot, spend

ECOS_BASE = "https://ecos.bok.or.kr/api"
FRED_BASE = "https://api.stlouisfed.org/fred"

ECOS_DAILY_CAP = int(os.environ.get("MCP_ECOS_DAILY_CAP", "2000"))
FRED_DAILY_CAP = int(os.environ.get("MCP_FRED_DAILY_CAP", "2000"))

# ECOS 주기 코드 -> 기간 포맷 예시. 조회 구간 기본값을 주기에 맞춘다.
_ECOS_CYCLE_FMT = {"A": "%Y", "Q": None, "M": "%Y%m", "D": "%Y%m%d"}


def _ecos_key() -> str:
    key = os.environ.get("ECOS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ECOS_API_KEY 가 없다 - compose environment 확인")
    return key


def _fred_key() -> str:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FRED_API_KEY 가 없다 - compose environment 확인")
    return key


def ecos_search(query: str, limit: int = 10) -> dict:
    """한국은행 ECOS 통계표 검색. 결과의 stat_code·cycle 로 ecos_series 를 부른다."""
    spend("ecos", ECOS_DAILY_CAP)
    n = max(1, min(int(limit), 30))
    url = (f"{ECOS_BASE}/StatisticTableList/{_ecos_key()}/json/kr/1/500/")
    body = json.loads(_get(url, timeout=20).decode("utf-8"))
    rows = (body.get("StatisticTableList") or {}).get("row", [])
    q = query.strip()
    hits = [r for r in rows
            if q in (r.get("STAT_NAME") or "")][:n]
    items = [{"stat_code": r.get("STAT_CODE"), "stat_name": r.get("STAT_NAME"),
              "cycle": r.get("CYCLE"), "org": r.get("ORG_NAME"),
              "searchable": r.get("SRCH_YN")} for r in hits]
    out = {"query": query, "count": len(items), "items": items,
           "note": "값 조회는 ecos_series(stat_code, cycle, ...). "
                   "item_code 를 모르면 ecos_items 로 항목을 먼저 본다."}
    out["citation"] = _snapshot("ecos_search", {"query": query}, out)
    return out


def ecos_items(stat_code: str, limit: int = 20) -> dict:
    """ECOS 통계표 하나의 항목(item_code) 목록 - 기준금리처럼 항목이 갈리는 표용."""
    spend("ecos", ECOS_DAILY_CAP)
    n = max(1, min(int(limit), 100))
    url = f"{ECOS_BASE}/StatisticItemList/{_ecos_key()}/json/kr/1/{n}/{stat_code}"
    body = json.loads(_get(url, timeout=20).decode("utf-8"))
    rows = (body.get("StatisticItemList") or {}).get("row", [])
    items = [{"item_code": r.get("ITEM_CODE"), "item_name": r.get("ITEM_NAME"),
              "cycle": r.get("CYCLE"), "start": r.get("START_TIME"),
              "end": r.get("END_TIME")} for r in rows]
    out = {"stat_code": stat_code, "count": len(items), "items": items}
    out["citation"] = _snapshot("ecos_items", {"stat_code": stat_code}, out)
    return out


def ecos_series(stat_code: str, cycle: str, start: str, end: str,
                item_code: str = "?", limit: int = 100) -> dict:
    """ECOS 시계열 값. cycle=A|Q|M|D, start/end 는 주기 포맷(M 이면 202601).

    ⚠ ECOS 는 개정 이력(vintage)을 주지 않는다 - 이 값은 '지금 시점의 최신값'
    이며 과거 시점 재현·백테스트에 쓰면 look-ahead 다.
    """
    spend("ecos", ECOS_DAILY_CAP)
    n = max(1, min(int(limit), 1000))
    cyc = cycle.strip().upper()
    if cyc not in _ECOS_CYCLE_FMT:
        raise RuntimeError(f"cycle 은 A|Q|M|D 중 하나다: {cycle}")
    url = (f"{ECOS_BASE}/StatisticSearch/{_ecos_key()}/json/kr/1/{n}/"
           f"{stat_code}/{cyc}/{start}/{end}/{item_code}")
    body = json.loads(_get(url, timeout=20).decode("utf-8"))
    block = body.get("StatisticSearch")
    if block is None:
        # ECOS 오류 규약: RESULT.CODE/MESSAGE
        res = body.get("RESULT") or {}
        raise RuntimeError(f"ECOS [{res.get('CODE')}] {res.get('MESSAGE')}")
    rows = block.get("row", [])
    items = [{"period": r.get("TIME"), "value": r.get("DATA_VALUE"),
              "item": r.get("ITEM_NAME1")} for r in rows]
    out = {"stat_code": stat_code, "cycle": cyc, "count": len(items),
           "items": items, "vintage": "none - 지금 조회한 최신값(개정 반영됨)",
           "queried_at": datetime.now().isoformat()}
    out["citation"] = _snapshot("ecos_series", {
        "stat_code": stat_code, "cycle": cyc, "start": start, "end": end,
        "item_code": item_code}, out)
    return out


def fred_search(query: str, limit: int = 10) -> dict:
    """FRED 시리즈 검색(미국·글로벌 거시). 결과 id 로 fred_series 를 부른다."""
    spend("fred", FRED_DAILY_CAP)
    n = max(1, min(int(limit), 30))
    q = urllib.parse.urlencode({
        "search_text": query, "api_key": _fred_key(), "file_type": "json",
        "limit": n, "order_by": "popularity", "sort_order": "desc"})
    body = json.loads(_get(f"{FRED_BASE}/series/search?{q}", timeout=20)
                      .decode("utf-8"))
    items = [{"id": s.get("id"), "title": s.get("title"),
              "frequency": s.get("frequency_short"),
              "units": s.get("units_short"),
              "last_updated": s.get("last_updated")}
             for s in body.get("seriess", [])]
    out = {"query": query, "count": len(items), "items": items}
    out["citation"] = _snapshot("fred_search", {"query": query}, out)
    return out


def fred_series(series_id: str, start: str = "", end: str = "",
                limit: int = 100) -> dict:
    """FRED 관측값. start/end 는 YYYY-MM-DD (비우면 최근부터).

    FRED 는 vintage(realtime)를 주는 드문 원천이지만, 이 도구는 질의 응대용
    최신값만 준다. 현재 Runtime에서는 PIT 연구 입력으로 쓰지 않으며, 필요하면
    별도 데이터 계약과 승인된 역사 원천을 먼저 마련해야 한다.
    """
    spend("fred", FRED_DAILY_CAP)
    n = max(1, min(int(limit), 1000))
    params = {"series_id": series_id, "api_key": _fred_key(),
              "file_type": "json", "limit": n, "sort_order": "desc"}
    if start:
        params["observation_start"] = start
    if end:
        params["observation_end"] = end
    q = urllib.parse.urlencode(params)
    body = json.loads(_get(f"{FRED_BASE}/series/observations?{q}", timeout=20)
                      .decode("utf-8"))
    items = [{"date": o.get("date"), "value": o.get("value")}
             for o in body.get("observations", [])]
    out = {"series_id": series_id, "count": len(items), "items": items,
           "queried_at": datetime.now().isoformat()}
    out["citation"] = _snapshot("fred_series", {
        "series_id": series_id, "start": start, "end": end}, out)
    return out


def register_macro_tools(server) -> None:
    """mcp_server.build_server() 가 부른다. 전부 읽기 전용."""
    server.tool(
        name="ecos_search",
        description="한국은행 ECOS 통계표 검색(기준금리·물가·환율 등 한국 거시). "
                    "2단계: 여기서 stat_code 를 찾고 ecos_series 로 값을 받는다.")(ecos_search)
    server.tool(
        name="ecos_items",
        description="ECOS 통계표의 항목 코드 목록 - 표 안에서 항목이 갈릴 때 먼저 본다.")(ecos_items)
    server.tool(
        name="ecos_series",
        description="ECOS 시계열 값 조회. ⚠ 지금 시점 최신값이며 개정 전 값은 복원 "
                    "불가 - 과거 재현·백테스트 인용 금지.")(ecos_series)
    server.tool(
        name="fred_search",
        description="FRED 시리즈 검색(미국·글로벌 거시: 금리·CPI·고용 등).")(fred_search)
    server.tool(
        name="fred_series",
        description="FRED 관측값 조회(질의 응대용 최신값).")(fred_series)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    # 네트워크 없는 검사만: cycle 검증과 키 부재 오류가 정직한가
    try:
        ecos_series("722Y001", "X", "2026", "2026")
        raise AssertionError("잘못된 cycle 이 통과했다")
    except RuntimeError as e:
        assert "cycle" in str(e) or "ECOS_API_KEY" in str(e)
        print("  cycle/키 검증            OK")
    print("자체 점검 통과")
