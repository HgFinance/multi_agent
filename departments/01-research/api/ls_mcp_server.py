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
           "t1602": "market_investor_flow_intraday",
           "t3320": "stock_fundamental",
           "t1637": "program_trade_trend"}

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

    시장·계좌 조회는 단일 `LS_ENV`를 사용한다. 이 서버에 주문 도구는 없다.
    """
    from ls_client import LsEnvironment, LsRestClient
    return LsRestClient(LsEnvironment.from_env(dict(os.environ)))


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


def _num(v):
    """LS 응답 값 -> float. 숫자가 아니면 None (0 으로 접지 않는다)."""
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _unaggregated(values, volume) -> bool:
    """거래는 있는데 투자자 컬럼이 전부 0 인가 = 아직 집계 전인가.

    실측 2026-08-14 장중: t1717 이 거래량 4,513,420 주인 당일 행에 개인·기관계·
    외인계를 모두 0 으로 준다(같은 시각 t1637 은 정상 수치). '순매수 0'과
    '아직 집계 안 됨'은 다른 상태다 - 0 을 그대로 주면 에이전트가 중립 수급으로
    읽는다. 투자자 15종이 4.5백만주 거래일에 동시에 정확히 0 일 수는 없다.
    """
    nums = [n for n in values if n is not None]
    vol = _num(volume)
    return bool(nums) and (vol or 0) > 0 and all(n == 0 for n in nums)


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
    items, unaggregated = [], []
    for r in rows[:days]:
        item = {"date": r.get("date"), "close": r.get("close"),
                "change_pct": r.get("diff"), "volume": r.get("volume")}
        cols = {label: r.get(col) for col, label in _T1717_COLS.items()}
        if _unaggregated(( _num(v) for v in cols.values()), r.get("volume")):
            # 키를 빼고 사유를 남긴다 - 없는 것을 0 으로 위장하지 않는다.
            item["집계상태"] = "장중_미집계 - 이 날짜의 투자자별 수급은 아직 없다"
            unaggregated.append(r.get("date"))
        else:
            item.update(cols)
        items.append(item)
    out = {"corp": resolved, "unit": "순매수량(주)", "count": len(items),
           "items": items, "tr": "t1717", "queried_at": datetime.now().isoformat(),
           "note": "지금 시점 조회값 - 백테스트·사후 채점 인용 금지"}
    if unaggregated:
        out["unavailable"] = {
            "dates": unaggregated,
            "reason": "장중이라 투자자별 집계 전(t1717 이 0 을 준다) - '순매수 0' 이 "
                      "아니다. 장중 수급이 필요하면 program_trade_trend(t1637) 또는 "
                      "market_investor_flow_intraday(t1602) 를 쓸 것."}
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


def program_trade_trend(corp: str, days: int = 10) -> dict:
    """종목별 프로그램매매 일자별 추이 (LS t1637 종목별프로그램매매추이).

    카드 t_cc435a46(2026-08-14) 큐레이션 요청분 - 스킬 검증 E2E 가 "카탈로그에
    있으나 미큐레이션"으로 정확히 보고했던 그 TR 이다. 함정 3건 전부 실측 반영:
      · 금액모드(gubun1=1)의 svolume, 수량모드(gubun1=0)의 svalue 는 주필드와
        불일치하는 보조값 - 각 모드의 주필드만 쓰고 두 호출을 일자로 합친다.
      · 금액 필드 단위는 '천원' (매도수량×평균단가 교차검증으로 확정).
      · cts_idx 는 Number 로 보낸다 (문자열이면 IGW40011).
    """
    days = max(1, min(int(days), 20))
    resolved = _shcode_of(corp)
    today = date_cls.today().strftime("%Y%m%d")

    def _one(gubun1: str) -> dict:
        spend("ls", LS_DAILY_CAP)
        body = _client().call_tr(
            path="/stock/program", tr_cd="t1637",
            in_block={"t1637InBlock": {
                "gubun1": gubun1, "gubun2": "1", "shcode": resolved["stock_code"],
                "date": today, "time": "000000", "cts_idx": 0, "exchgubun": "0"}},
            rate_limit_per_sec=1.0)
        rows = body.get("t1637OutBlock1") or body.get("t1637OutBlock") or []
        return {r.get("date"): r for r in (rows if isinstance(rows, list) else [rows])}

    by_vol, by_val = _one("0"), _one("1")   # 수량 -> 금액, 초당 1회는 클라이언트가 지킴
    items = []
    for d in sorted(set(by_vol) | set(by_val), reverse=True)[:days]:
        v, m = by_vol.get(d, {}), by_val.get(d, {})
        base = m or v
        items.append({
            "date": d, "close": base.get("price"), "change_pct": base.get("diff"),
            "volume": base.get("volume"),
            "P매도금액_천원": m.get("offervalue"), "P매수금액_천원": m.get("stksvalue"),
            "P순매수금액_천원": m.get("svalue"),
            "P매도수량": v.get("offervolume"), "P매수수량": v.get("stksvolume"),
            "P순매수수량": v.get("svolume")})
    out = {"corp": resolved, "units": {"금액": "천원", "수량": "주"},
           "count": len(items), "items": items, "tr": "t1637",
           "queried_at": datetime.now().isoformat(),
           "note": "프로그램매매(차익·비차익 바스켓)는 투자주체별 수급(t1717)과 "
                   "다른 축이다 - 섞지 말 것. 지금 시점 조회값 - 백테스트·사후 "
                   "채점 인용 금지"}
    out["citation"] = _snapshot("program_trade_trend", {"corp": corp, "days": days}, out)
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


def stock_fundamental(corp: str) -> dict:
    """종목 밸류에이션·수익성 요약 (LS t3320, FnGuide 데이터) - 무료.

    PER/PBR/EPS/BPS/ROE/ROA/EV·EBITDA + 당기예상(t_per/t_eps) + 외인비율·시총.
    KRX 정보데이터시스템이 유료화(2025-12)된 뒤 남은 무료 정식 경로다
    (재일 확인 2026-08-13 - pykrx 는 유료 회원 벽에 막혀 폐기).
    확정 결산 기준이라 이익 급변 구간에서는 t_per(예상)와 같이 읽어라.
    """
    resolved = _shcode_of(corp)
    spend("ls", LS_DAILY_CAP)
    body = _client().call_tr(
        path="/stock/investinfo", tr_cd="t3320",
        in_block={"t3320InBlock": {"gicode": resolved["stock_code"]}},
        rate_limit_per_sec=1.0)
    ob = body.get("t3320OutBlock") or {}
    ob1 = body.get("t3320OutBlock1") or {}
    out = {"corp": resolved, "결산년월": ob1.get("gsym"),
           "PER": ob1.get("per"), "PBR": ob1.get("pbr"),
           "EPS": ob1.get("eps"), "BPS": ob1.get("bps"),
           "ROE": ob1.get("roe"), "ROA": ob1.get("roa"),
           "EV_EBITDA": ob1.get("evebitda"), "PEG": ob1.get("peg"),
           "예상PER": ob1.get("t_per"), "예상EPS": ob1.get("t_eps"),
           "외국인비율_pct": ob.get("foreignratio"),
           "시가총액_억원": ob.get("sigavalue"),
           "업종": ob.get("upgubunnm"), "시장": ob.get("marketnm"),
           "tr": "t3320", "queried_at": datetime.now().isoformat(),
           "note": "FnGuide 확정결산 기준 - 백테스트·사후 채점 인용 금지. "
                   "업종 상대비교는 종목별로 이 도구를 반복 호출(초당 1건)."}
    out["citation"] = _snapshot("stock_fundamental", {"corp": corp}, out)
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
        name="program_trade_trend",
        description="종목별 프로그램매매 일자별 추이(매도/매수/순매수, 금액 천원+"
                    "수량 주). 투자주체별 수급(investor_flow)과 다른 축. 기업명 "
                    "또는 6자리 코드.")(program_trade_trend)
    server.tool(
        name="market_investor_flow_intraday",
        description="시장 전체(코스피 001/코스닥 301)의 장중 시간대별 투자자 순매수. "
                    "종목 단위가 아니다 - 종목은 investor_flow.")(market_investor_flow_intraday)
    server.tool(
        name="stock_fundamental",
        description="종목 밸류에이션 요약(FnGuide): PER/PBR/EPS/BPS/ROE/ROA/"
                    "EV·EBITDA/예상PER + 외인비율·시총. 업종 상대비교는 종목별 "
                    "반복 호출. 무료(LS).")(stock_fundamental)
    server.tool(
        name="ls_tr_spec",
        description="TR 하나의 전체 명세(요청·응답 필드 한글 설명). 큐레이션 없는 "
                    "TR 을 이해하거나 응답 컬럼 뜻을 확인할 때.")(ls_tr_spec)
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
    assert any(i["tr_code"] == "t1637" and i["executable_tool"] == "program_trade_trend"
               for i in ls_tr_catalog("프로그램매매추이", limit=30)["items"]), \
        "t1637 큐레이션 연결 실패"
    print(f"  카탈로그 {cat['tr_count']}개 로드, '투자자' 검색 {r['count']}건  OK")
    assert _T1717_COLS["tjj0016_vol"] == "외인계"
    print("  t1717 컬럼 사상          OK")
    # 장중 미집계 판별 - 거짓 0 을 수급 0 으로 내보내지 않는다 (실측 2026-08-14)
    assert _unaggregated([0.0] * 15, 4513420) is True, "장중 0 행을 못 잡는다"
    assert _unaggregated([0.0, -401379.0], 35530867) is False, "정상 행을 미집계로 몬다"
    assert _unaggregated([0.0] * 15, 0) is False, "거래 없는 날은 미집계가 아니다"
    assert _unaggregated([None, None], 100) is False, "값이 없는 것은 판별 대상 아님"
    print("  장중 미집계 판별         OK")
    print("자체 점검 통과")
