#!/usr/bin/env python3
"""LS 증권 OpenAPI MCP 서버 - TR 카탈로그 검색 + 큐레이션 실행 도구.

소유: 재일 (리서치본부)
근거: 재일 결정 2026-08-13 "TR 목록을 정제해서 사용자 질문에 답할 수 있게" +
      "증권사 api 를 mcp 형식으로 조회".

▶ 왜 research-mcp 와 **다른 컨테이너**인가
  2026-08-02 결정: LS 자격은 퍼뜨리지 않는다("자격이 퍼지는 것 자체가 위험이라
  옮기지 않고 없앴다" - compose research-mcp 주석). 그 결정을 뒤집지 않는다 -
  LS 키는 이 컨테이너에만 있고, 에이전트는 도구를 받지 키를 받지 않는다.

▶ 카탈로그 + 큐레이션 2층 (도구 365개를 다 등록하지 않는 이유)
  TR 365개를 전부 MCP 도구로 올리면 에이전트의 도구 선택이 무너진다
  (FinToolBench: 도구 760개 환경의 해법은 '검색 후 선택'). 그래서:
    1층 ls_tr_catalog  - 정제된 색인 검색. "수급" -> t1717 이 나온다.
    2층 큐레이션 도구   - 용례가 확정된 TR 만 실행 가능(수급 2종부터).
  카탈로그에만 있고 큐레이션이 없는 TR 은 "미구현" 을 정직하게 돌려준다 -
  그 목록이 다음 큐레이션 백로그다. 범용 ls_call(tr, params) 는 두지 않는다:
  TR 별 타입 규격(Number 를 문자열로 보내면 IGW40011)·초당 제한을 에이전트가
  다 지키게 하는 것은 실패 설계다.

▶ 규약은 external_sources 와 동일: 일일 예산 / 전 호출 스냅샷 / 무해석 반환.

자체 점검: python api/ls_mcp_server.py           # 네트워크 없는 검사
서버:     python api/ls_mcp_server.py --serve    # 기본 0.0.0.0:8038/mcp
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BASE / "api"))
sys.path.insert(0, str(_BASE / "collectors"))

from external_sources import _snapshot, spend, _resolve  # noqa: E402

CATALOG_PATH = Path(os.environ.get(
    "LS_TR_CATALOG", str(_BASE / "config" / "ls_tr_catalog.json")))
LS_DAILY_CAP = int(os.environ.get("MCP_LS_DAILY_CAP", "2000"))
DEFAULT_PORT = 8038

_catalog_cache: dict | None = None


def _catalog() -> dict:
    global _catalog_cache
    if _catalog_cache is None:
        _catalog_cache = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return _catalog_cache


# 큐레이션 표 - TR 코드 -> 실행 도구 이름. 카탈로그 검색 결과에 같이 실린다.
CURATED = {"t1717": "investor_flow", "t1927": "short_selling",
           "t1602": "market_investor_flow_intraday"}

# LS 투자자 코드 체계 (t1717 tjjXXXX·t1602 sv_XX 공통 - 문서 필드표 실측)
TJJ_CODES = {
    "00": "사모펀드", "01": "증권", "02": "보험", "03": "투신", "04": "은행",
    "05": "종금", "06": "기금", "07": "기타법인", "08": "개인",
    "09": "등록외국인", "10": "미등록외국인", "11": "국가외",
    "16": "외인계", "17": "외국인", "18": "기관계",
}


def _shcode_of(query: str) -> dict:
    """기업명 또는 6자리 코드 -> {stock_code, corp_name}. DART 색인 재사용."""
    hits = _resolve(query)
    if not hits:
        raise RuntimeError(f"'{query}' 종목을 찾지 못했다 - 6자리 코드로 재시도할 것")
    return hits[0]


def _client():
    """LsRestClient 지연 생성 - 키가 없으면 그때 정직하게 실패한다.

    ▶ 조회는 `LS_MARKET_ENV`, 주문은 `LS_ENV` (재일 결정 2026-08-12,
      ls_realtime_service.py:257). 모의 서버는 수급 TR 에 rsp 00000 인데
      빈 행을 준다(실측 2026-08-13 t1717) - "진짜처럼 생긴 가짜" 를 막으려면
      조회면은 LIVE 로 붙어야 한다. 이 서버에 주문 도구는 없다.
    """
    from ls_client import LsEnvironment, LsRestClient
    e = dict(os.environ)
    e["LS_ENV"] = (e.get("LS_MARKET_ENV") or "LIVE").strip().upper()
    return LsRestClient(LsEnvironment.from_env(e))


def ls_tr_catalog(query: str, protocol: str = "", limit: int = 10) -> dict:
    """정제된 LS TR 색인 검색. 이름·카테고리·파라미터 설명에서 부분일치."""
    q = query.strip()
    n = max(1, min(int(limit), 30))
    proto = protocol.strip().upper()
    hits = []
    for e in _catalog()["trs"]:
        if proto and e["protocol"] != proto:
            continue
        hay = " ".join([e["name"], e["category"], e["doc_title"],
                        " ".join(p["desc"] for p in e["in_params"])])
        if q in hay or q.lower() in e["tr_code"]:
            hits.append(e)
        if len(hits) >= n:
            break
    items = [{"tr_code": e["tr_code"], "name": e["name"],
              "category": e["category"], "protocol": e["protocol"],
              "rate_per_sec": e["rate_per_sec"],
              "in_params": [f"{p['name']}({p['desc']})" for p in e["in_params"]],
              "executable_tool": CURATED.get(e["tr_code"]),
              "doc": e["doc"]} for e in hits]
    out = {"query": query, "count": len(items), "items": items,
           "note": "executable_tool 이 null 이면 아직 큐레이션 전 - 그 TR 로 답할 "
                   "수 없다고 정직하게 말하고, 필요하면 큐레이션 요청을 남겨라."}
    out["citation"] = _snapshot("ls_tr_catalog", {"query": query}, out)
    return out


# t1717 OutBlock1 의 투자자 컬럼 - 응답을 사람이 읽을 이름으로 바꾼다(해석 아님,
# 문서 필드표의 한글명 그대로다).
_T1717_COLS = {
    "tjj0008_vol": "개인", "tjj0018_vol": "기관계", "tjj0016_vol": "외인계",
    "tjj0001_vol": "증권", "tjj0002_vol": "보험", "tjj0003_vol": "투신",
    "tjj0004_vol": "은행", "tjj0005_vol": "종금", "tjj0006_vol": "기금",
    "tjj0000_vol": "사모펀드", "tjj0007_vol": "기타법인",
    "tjj0009_vol": "등록외국인", "tjj0010_vol": "미등록외국인", "tjj0011_vol": "국가외",
}


def investor_flow(corp: str, days: int = 10) -> dict:
    """종목 수급 - 일자별 투자자 유형별 순매수량 (LS t1717 외인기관종목별동향).

    단위는 순매수 '수량'(주)이며 문서 필드표의 한글명 그대로 돌려준다.
    """
    days = max(1, min(int(days), 60))
    resolved = _shcode_of(corp)
    spend("ls", LS_DAILY_CAP)
    end = date_cls.today()
    start = end - timedelta(days=days * 2 + 5)   # 휴장 감안한 달력 여유
    body = _client().call_tr(
        path="/stock/frgr-itt", tr_cd="t1717",
        in_block={"t1717InBlock": {
            "shcode": resolved["stock_code"], "gubun": "0",
            "fromdt": start.strftime("%Y%m%d"), "todt": end.strftime("%Y%m%d"),
            "exchgubun": "0"}},
        rate_limit_per_sec=1.0)
    # 문서는 OutBlock(Object)+OutBlock1(Array)이라 하지만 실제 응답(LIVE,
    # 2026-08-13 실측)은 t1717OutBlock 이 곧 일자별 배열이다. 둘 다 받는다.
    rows = body.get("t1717OutBlock1") or body.get("t1717OutBlock") or []
    if isinstance(rows, dict):
        rows = [rows]
    items = []
    for r in rows[:days]:
        item = {"date": r.get("date"), "close": r.get("close"),
                "change_pct": r.get("diff"), "volume": r.get("volume")}
        item.update({label: r.get(col) for col, label in _T1717_COLS.items()})
        items.append(item)
    out = {"corp": resolved, "unit": "순매수량(주)", "count": len(items),
           "items": items, "tr": "t1717", "queried_at": datetime.now().isoformat(),
           "note": "지금 시점 조회값 - 백테스트·사후 채점 인용 금지"}
    out["citation"] = _snapshot("investor_flow", {"corp": corp, "days": days}, out)
    return out


def ls_tr_spec(tr_code: str) -> dict:
    """TR 하나의 전체 명세 - 요청·응답 필드의 한글 설명까지 (문서 정제분).

    큐레이션 도구가 없는 TR 을 이해하거나, 응답 컬럼의 뜻을 확인할 때 쓴다.
    ⚠ 실측 주의 2건: 응답 블록·필드 이름이 문서와 다를 수 있다
    (t1717 은 OutBlock 이 곧 배열, t1602 는 svolume_XX 가 실제로는 sv_XX).
    """
    code = tr_code.strip().lower()
    for e in _catalog()["trs"]:
        if e["tr_code"].lower() == code:
            out = {**e, "executable_tool": CURATED.get(e["tr_code"])}
            out["citation"] = _snapshot("ls_tr_spec", {"tr_code": tr_code}, {
                "tr_code": e["tr_code"], "fields": e["out_fields"]})
            return out
    raise RuntimeError(f"카탈로그에 없는 TR: {tr_code} - ls_tr_catalog 로 검색할 것")


def short_selling(corp: str, days: int = 10) -> dict:
    """종목 공매도 일별추이 (LS t1927) - 수량·대금·거래비중·평균단가·누적."""
    days = max(1, min(int(days), 60))
    resolved = _shcode_of(corp)
    spend("ls", LS_DAILY_CAP)
    end = date_cls.today()
    start = end - timedelta(days=days * 2 + 5)
    body = _client().call_tr(
        path="/stock/etc", tr_cd="t1927",
        in_block={"t1927InBlock": {
            "shcode": resolved["stock_code"], "date": " ",
            "sdate": start.strftime("%Y%m%d"), "edate": end.strftime("%Y%m%d")}},
        rate_limit_per_sec=1.0)
    rows = body.get("t1927OutBlock1") or []
    items = [{"date": r.get("date"), "close": r.get("price"),
              "change_pct": r.get("diff"), "volume": r.get("volume"),
              "공매도수량": r.get("gm_vo"), "공매도대금": r.get("gm_va"),
              "공매도비중_pct": r.get("gm_per"), "평균공매도단가": r.get("gm_avg"),
              "누적공매도수량": r.get("gm_vo_sum"),
              "업틱룰적용수량": r.get("gm_vo1")} for r in rows[:days]]
    out = {"corp": resolved, "count": len(items), "items": items, "tr": "t1927",
           "queried_at": datetime.now().isoformat(),
           "note": "지금 시점 조회값 - 백테스트·사후 채점 인용 금지"}
    out["citation"] = _snapshot("short_selling", {"corp": corp, "days": days}, out)
    return out


def market_investor_flow_intraday(upcode: str = "001", count: int = 30) -> dict:
    """시장 전체의 장중 시간대별 투자자 순매수 (LS t1602). 종목 단위가 아니다.

    upcode: 업종코드 - 001 코스피종합, 301 코스닥종합 (LS 업종 관례).
    ⚠ 실측 전제(2026-08-13): market='1'·gubun1='1'(수량) 조합으로 관측 확인.
      응답 필드는 문서(svolume_XX)와 달리 sv_XX 로 온다. 값은 순매수 수량 계열.
    """
    n = max(1, min(int(count), 120))
    spend("ls", LS_DAILY_CAP)
    body = _client().call_tr(
        path="/stock/investor", tr_cd="t1602",
        in_block={"t1602InBlock": {
            "market": "1", "upcode": upcode, "gubun1": "1", "gubun2": "1",
            "cts_time": " ", "cts_idx": 0, "cnt": n, "gubun3": " ",
            "exchgubun": "0"}},
        rate_limit_per_sec=1.0)
    rows = body.get("t1602OutBlock1") or []
    items = []
    for r in rows[:n]:
        item = {"time": r.get("time")}
        for code, label in TJJ_CODES.items():
            v = r.get(f"sv_{code}")
            if v is not None:
                item[label] = v
        items.append(item)
    out = {"upcode": upcode, "unit": "순매수(수량 계열, gubun1=1 실측 전제)",
           "count": len(items), "items": items, "tr": "t1602",
           "queried_at": datetime.now().isoformat(),
           "note": "지금 시점 조회값 - 백테스트·사후 채점 인용 금지"}
    out["citation"] = _snapshot(
        "market_investor_flow_intraday", {"upcode": upcode, "count": count}, out)
    return out


# ── KRX 밸류에이션 (pykrx) ──────────────────────────────────────────────────
KRX_DAILY_CAP = int(os.environ.get("MCP_KRX_DAILY_CAP", "500"))


def _krx_creds_guard() -> None:
    """pykrx 1.2.x 는 KRX 정보데이터시스템 로그인(무료 계정)을 요구한다.

    없으면 pykrx 가 pandas IndexError 로 어지럽게 죽는다(실측 2026-08-13) -
    먼저 걸러 정직한 안내를 준다. data.krx.co.kr 무료 가입 후 .env 에
    KRX_ID/KRX_PW 를 넣으면 산다.
    """
    if not (os.environ.get("KRX_ID", "").strip()
            and os.environ.get("KRX_PW", "").strip()):
        raise RuntimeError(
            "KRX_ID/KRX_PW 가 없다 - pykrx(KRX 통계)는 data.krx.co.kr 무료 "
            "로그인이 필요하다. .env 에 계정을 넣기 전까지 이 도구는 못 쓴다. "
            "PER/PBR 개별 검증은 dart_financials + 시세로 대신할 것.")


def _krx_day() -> str:
    from pykrx.stock import get_nearest_business_day_in_a_week
    return get_nearest_business_day_in_a_week()


def _df_records(df, index_name: str, limit: int) -> list[dict]:
    df = df.reset_index().rename(columns={df.index.name or "index": index_name})
    return json.loads(df.head(limit).to_json(orient="records", force_ascii=False))


def krx_fundamental(corp: str = "", date: str = "", limit: int = 50) -> dict:
    """KRX 밸류에이션 - PER/PBR/EPS/BPS/배당수익률(DIV). 상대가치 비교의 재료.

    corp 지정 시 그 종목, 비우면 **전 시장 횡단면**(비교·스크리닝용, limit 행).
    ⚠ KRX 웹 기반 참고치 - 재무 반영이 늦다(연보고서 검토기간 후). 정밀
    검증은 dart_financials 원값으로.
    """
    from pykrx.stock import get_market_fundamental
    _krx_creds_guard()
    _krx_creds_guard()
    _krx_creds_guard()
    from external_sources import spend as _sp
    _sp("krx", KRX_DAILY_CAP)
    day = (date or _krx_day()).replace("-", "")
    if corp.strip():
        resolved = _shcode_of(corp)
        df = get_market_fundamental(day, day, resolved["stock_code"])
        items = _df_records(df, "date", 5)
        out = {"mode": "single", "corp": resolved, "date": day, "items": items}
    else:
        df = get_market_fundamental(day, market="ALL")
        n = max(1, min(int(limit), 3000))
        items = _df_records(df.sort_values("PER"), "ticker", n)
        out = {"mode": "cross_section", "date": day, "count": len(items),
               "sorted_by": "PER asc", "items": items}
    out["note"] = "KRX 참고치 - 백테스트·사후 채점 인용 금지"
    out["citation"] = _snapshot("krx_fundamental",
                               {"corp": corp, "date": date, "limit": limit}, out)
    return out


def krx_marketcap(corp: str, date: str = "") -> dict:
    """종목 시가총액·거래대금·상장주식수·외국인보유주식수 (KRX 참고치)."""
    from pykrx.stock import get_market_cap
    from external_sources import spend as _sp
    _sp("krx", KRX_DAILY_CAP)
    resolved = _shcode_of(corp)
    day = (date or _krx_day()).replace("-", "")
    df = get_market_cap(day, day, resolved["stock_code"])
    out = {"corp": resolved, "date": day,
           "items": _df_records(df, "date", 5),
           "note": "KRX 참고치"}
    out["citation"] = _snapshot("krx_marketcap", {"corp": corp, "date": date}, out)
    return out


def krx_foreign_ownership(corp: str, days: int = 10) -> dict:
    """종목 외국인 보유(스톡) 추이 - 한도소진률·보유수량. LS investor_flow 의
    플로우(순매수)와 상호보완: 플로우가 쌓인 결과가 이 스톡이다."""
    from pykrx.stock import get_exhaustion_rates_of_foreign_investment
    from external_sources import spend as _sp
    _sp("krx", KRX_DAILY_CAP)
    days = max(1, min(int(days), 60))
    resolved = _shcode_of(corp)
    end = date_cls.today()
    start = end - timedelta(days=days * 2 + 5)
    df = get_exhaustion_rates_of_foreign_investment(
        start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), resolved["stock_code"])
    out = {"corp": resolved, "count": min(len(df), days),
           "items": _df_records(df.tail(days), "date", days),
           "note": "KRX 참고치 - 한도소진률(%)·보유수량(주)"}
    out["citation"] = _snapshot("krx_foreign_ownership",
                                {"corp": corp, "days": days}, out)
    return out


def ls_budget() -> dict:
    from external_sources import budget_state
    return budget_state()


# ── MCP 서버 ────────────────────────────────────────────────────────────────
def _mcp_class():
    try:
        from mcp.server.mcpserver import MCPServer
        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP
        return FastMCP


def build_server(host: str = "0.0.0.0", port: int = DEFAULT_PORT):
    cls = _mcp_class()
    server = cls(name="ls-securities", host=host, port=port,
                 streamable_http_path="/mcp")
    server.tool(
        name="ls_tr_catalog",
        description="LS 증권 OpenAPI TR 365종의 정제 색인 검색(수급·프로그램·공매도 "
                    "등). executable_tool 이 있으면 그 도구로 실행하고, 없으면 "
                    "미구현이라고 정직하게 답하라.")(ls_tr_catalog)
    server.tool(
        name="investor_flow",
        description="종목 수급 - 일자별 개인/기관/외인 순매수량(주). 기업명 또는 "
                    "6자리 코드. 지금 시점 조회값이므로 과거 재현 인용 금지.")(investor_flow)
    server.tool(
        name="short_selling",
        description="종목 공매도 일별추이 - 수량·대금·거래비중(%)·평균단가·누적. "
                    "기업명 또는 6자리 코드.")(short_selling)
    server.tool(
        name="market_investor_flow_intraday",
        description="시장 전체(코스피 001/코스닥 301)의 장중 시간대별 투자자 순매수. "
                    "종목 단위가 아니다 - 종목은 investor_flow.")(market_investor_flow_intraday)
    server.tool(
        name="ls_tr_spec",
        description="TR 하나의 전체 명세(요청·응답 필드 한글 설명). 큐레이션 없는 "
                    "TR 을 이해하거나 응답 컬럼 뜻을 확인할 때.")(ls_tr_spec)
    server.tool(
        name="krx_fundamental",
        description="KRX 밸류에이션 PER/PBR/EPS/BPS/배당수익률. corp 비우면 전 시장 "
                    "횡단면(상대가치 비교·스크리닝). KRX 참고치 - 정밀 검증은 dart_financials.")(krx_fundamental)
    server.tool(
        name="krx_marketcap",
        description="종목 시가총액·상장주식수·외국인보유주식수 (KRX 참고치).")(krx_marketcap)
    server.tool(
        name="krx_foreign_ownership",
        description="외국인 보유 스톡 추이(한도소진률%·보유수량) - investor_flow(플로우)와 "
                    "상호보완.")(krx_foreign_ownership)
    server.tool(
        name="ls_budget",
        description="오늘 외부 조회 예산 사용량. 소진되면 호출이 거부된다.")(ls_budget)
    return server


def _auth_app(server, token: str | None):
    """MCP_RESEARCH_API_KEY 인증 - research-mcp 와 같은 규약."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    app = server.streamable_http_app()
    if not token:
        print("⚠ MCP_RESEARCH_API_KEY 가 비어 무인증으로 연다 - 의도인지 확인")
        return app

    class _Auth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            got = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
            if got != token:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    app.add_middleware(_Auth)
    return app


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if "--serve" in sys.argv:
        import uvicorn
        srv = build_server()
        token = os.environ.get("MCP_RESEARCH_API_KEY", "").strip()
        uvicorn.run(_auth_app(srv, token or None),
                    host="0.0.0.0", port=DEFAULT_PORT, log_level="info")
        sys.exit(0)

    # 자체 점검 (네트워크 없음)
    cat = _catalog()
    assert cat["tr_count"] >= 300, cat["tr_count"]
    r = ls_tr_catalog("투자자")
    assert r["count"] > 0, "카탈로그 검색이 비었다"
    assert any(i["tr_code"] == "t1717" and i["executable_tool"] == "investor_flow"
               for i in ls_tr_catalog("외인기관")["items"]), "t1717 큐레이션 연결 실패"
    print(f"  카탈로그 {cat['tr_count']}개 로드, '투자자' 검색 {r['count']}건  OK")
    assert _T1717_COLS["tjj0016_vol"] == "외인계"
    print("  t1717 컬럼 사상          OK")
    print("자체 점검 통과")
