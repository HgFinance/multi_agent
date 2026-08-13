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
from datetime import date, datetime, timedelta
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
CURATED = {"t1717": "investor_flow"}


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
    end = date.today()
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
