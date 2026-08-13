#!/usr/bin/env python3
"""리서치본부 MCP 서버 - Hermes 가 부서를 실제로 돌리게 하는 다리.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-02 "헤르메스 써서 효과를 보려던 건데 혼란오네
      → 일단 시작해서 하나씩 개선해보자".
      계획서 3.3 "본부 간 호출은 HTTP", Hermes `mcp add --url` (HTTP/SSE 지원 실측)

▶ 이 다리가 없으면 헤르메스는 '대화만 되는 껍데기'다
  분석 실체는 LangGraph(scripts.py 분석가 6인)이고 헤르메스는 부서 인터페이스·
  기억·위임 계층이다(TECH_STACK_DECISIONS 2절). 둘을 잇는 것이 도구 호출이며,
  그게 비어 있어서 지금까지 헤르메스가 헛도는 것처럼 보였다.

▶ 노출 원칙 (권한 경계)
  - **읽기 도구가 기본이다.** 리서치본부는 주문·리스크 판정·원장에 관여하지
    않는다(config.yaml forbidden_tools). 여기 없는 것은 호출할 수 없다.
  - 유일한 쓰기성 작업은 `run_research_packet` 인데 이것도 **Packet 생성**일
    뿐 거래 결정이 아니다. Agent Decision != Order (CLAUDE.md).
  - 파이프라인은 분석가 6인 x LLM 이라 수 분이 걸린다. MCP 호출이 그동안
    묶이면 대화가 죽으므로 **비동기 시작 + 조회** 두 도구로 나눈다.

▶ 정직성
  - 도구는 결과를 요약하지 않는다. 결정론 산출물(판정·수치·근거)을 그대로
    돌려주고 해석은 호출자(페르소나)가 한다 - 중간에서 요약하면 그 자체가
    검증되지 않은 서술 계층이 하나 더 생긴다.
  - 실패는 실패로 돌려준다. 빈 결과로 위장하지 않는다.

실행
  python api/mcp_server.py            # 자체 점검 (네트워크 없음)
  python api/mcp_server.py --serve    # MCP 서버 (기본 0.0.0.0:8037/mcp)
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))
sys.path.insert(0, str(_BASE / "collectors"))

MCP_VERSION = "research-mcp-v1"
KST = timezone(timedelta(hours=9))
DEFAULT_PORT = int(os.environ.get("RESEARCH_MCP_PORT", "8037"))

# 진행 중·완료된 Packet 작업 (프로세스 메모리). 재시작하면 사라지지만 결과
# 자체는 research.pipeline_runs 와 reports/*.md 에 남으므로 유실이 아니다.
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
MAX_JOBS_KEPT = 50

# 직원 Worker(LangGraph) 실행 작업 - Packet 작업과 저장소를 분리한다.
# 모양이 다르다(Packet 은 subprocess tail 파싱, Worker 는 registry 결과 dict).
_WORKER_JOBS: dict[str, dict] = {}
_WORKER_JOBS_LOCK = threading.Lock()


def _db():
    import psycopg2
    from source_registry import load_project_env

    return psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=15)


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 순수 로직 (자체 점검 대상)
# ---------------------------------------------------------------------------

def is_authorized(header: str | None, token: str | None) -> bool:
    """Authorization 헤더 검증.

    - 토큰이 설정돼 있지 않으면 **누구나 통과**한다(개발 편의). 대신 기동 시
      경고를 찍어 '설정을 잊은 것'과 '일부러 연 것'이 구분되게 한다.
    - 설정돼 있으면 정확히 일치해야 한다. 비교는 상수 시간으로 한다 -
      토큰을 한 글자씩 맞춰보는 타이밍 공격을 막는다.
    - 빈 문자열 토큰은 '설정 안 함'과 같게 취급한다. Hermes 가 미치환
      `${MCP_RESEARCH_API_KEY}` 를 그대로 보내는 경우도 통과시키지 않는다.
    """
    import hmac

    if not token:
        return True
    if not header:
        return False
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1].strip(), token)


def build_app(server, *, token: str | None):
    """MCP Starlette 앱 + Bearer 검사 미들웨어."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    app = server.streamable_http_app()

    class _Auth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if not is_authorized(request.headers.get("authorization"), token):
                return JSONResponse(
                    {"error": "unauthorized",
                     "detail": "MCP_RESEARCH_API_KEY 가 필요하다"}, status_code=401)
            return await call_next(request)

    app.add_middleware(_Auth)
    return app


def normalize_symbol(symbol: str) -> str:
    """KRX 6자리 종목코드만 받는다. 지어내거나 추측하지 않는다."""
    s = str(symbol or "").strip()
    if not (len(s) == 6 and s.isdigit()):
        raise ValueError(f"종목코드는 6자리 숫자여야 한다 (받은 값: {symbol!r})")
    return s


def register_job(symbol: str, *, now: datetime) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {"job_id": job_id, "symbol": symbol, "status": "RUNNING",
                         "started_at": now.isoformat(), "ended_at": None,
                         "exit_code": None, "report": None, "tail": None}
        if len(_JOBS) > MAX_JOBS_KEPT:      # 오래된 것부터 버린다(결과는 DB 에 있다)
            for k in list(_JOBS)[:-MAX_JOBS_KEPT]:
                _JOBS.pop(k, None)
    return job_id


def finish_job(job_id: str, *, exit_code: int, tail: str, now: datetime) -> dict:
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
        if j is None:
            return {}
        j["status"] = "COMPLETED" if exit_code == 0 else "FAILED"
        j["exit_code"] = exit_code
        j["ended_at"] = now.isoformat()
        j["tail"] = (tail or "")[-1500:]
        for line in reversed((tail or "").splitlines()):
            if "리포트 저장:" in line:
                j["report"] = line.split("리포트 저장:", 1)[1].strip()
                break
        return dict(j)


def register_worker_job(payload_fields: list[str], *, now: datetime,
                        symbol: str = "") -> str:
    job_id = uuid.uuid4().hex[:12]
    with _WORKER_JOBS_LOCK:
        _WORKER_JOBS[job_id] = {
            "job_id": job_id, "kind": "employee_workers",
            "payload_fields": payload_fields, "symbol": symbol or None,
            "status": "RUNNING",
            "started_at": now.isoformat(), "ended_at": None,
            "result": None, "model_plane": None, "evidence": None, "error": None,
        }
        if len(_WORKER_JOBS) > MAX_JOBS_KEPT:
            # RUNNING 은 퇴거하지 않는다 - 몇 분짜리 실행 도중에 자리가
            # 사라지면 GPU 를 태운 결과가 어디에도 안 남는다(2026-08-13 리뷰).
            # RUNNING 만으로 상한을 넘는 폭주는 그대로 남긴다 - 곧 끝나고,
            # 다음 등록 때 완료분부터 치워진다.
            evictable = [k for k, v in _WORKER_JOBS.items()
                         if v.get("status") != "RUNNING"]
            for k in evictable[:len(_WORKER_JOBS) - MAX_JOBS_KEPT]:
                _WORKER_JOBS.pop(k, None)
    return job_id


def finish_worker_job(job_id: str, *, result: dict | None,
                      model_plane: dict | None, error: str | None,
                      now: datetime, evidence: dict | None = None) -> dict:
    with _WORKER_JOBS_LOCK:
        j = _WORKER_JOBS.get(job_id)
        if j is None:
            # 자리가 사라진 완료 - 결과를 조용히 버리지 않고 로그에 남긴다
            print(f"⚠ worker job {job_id} 의 자리가 사라져 결과를 버린다 "
                  f"(error={error!r})", file=sys.stderr)
            return {}
        # degraded=True 여도 작업 자체는 끝났다 - FAILED 는 registry 실행이
        # 예외로 죽은 경우만이다. degraded 는 결과 안에 그대로 보인다.
        j["status"] = "FAILED" if error else "COMPLETED"
        j["ended_at"] = now.isoformat()
        j["result"] = result
        j["model_plane"] = model_plane
        j["evidence"] = evidence
        j["error"] = error
        return dict(j)


def build_worker_payload(holding_question: str = "", proposal_draft: str = "",
                         portfolio_state: str = "", news: str = "") -> dict:
    """Worker 트리거 payload. 빈 인자는 싣지 않는다.

    employee_worker_runtime.should_run 은 trigger 필드가 비어 있지 않을 때만
    Worker 를 돌린다 - 빈 문자열을 실으면 '트리거됐는데 근거가 빈' 워커가
    생겨 DEGRADED 로 보인다. 트리거가 하나도 없으면 ValueError 다 -
    아무도 안 도는 실행을 RUNNING 으로 위장하지 않는다.
    """
    payload = {k: v for k, v in {
        "holding_question": holding_question.strip(),
        "proposal_draft": proposal_draft.strip(),
        "portfolio_state": portfolio_state.strip(),
        "news": news.strip(),
    }.items() if v}
    if not ({"holding_question", "proposal_draft"} & set(payload)):
        raise ValueError(
            "holding_question 또는 proposal_draft 중 하나는 있어야 한다 - "
            "리서치 Worker 2인의 트리거가 그 둘뿐이다")
    return payload


# ── Evidence First (FINAL_RUNTIME_ARCHITECTURE §11) ─────────────────────────
# Runner 는 Worker 를 부르기 전에 Tool/Evidence 부터 모은다 - Worker 가 근거
# 없이 추론하지 않게 하기 위해서다. 소스(뉴스·공시·가격)별로 **독립 시도**하고
# 실패는 status 로 정직하게 남긴다. evidence/bundle.assemble_bundle 을 안 쓰는
# 이유: 그건 첫 실패를 전체 실패로 전파하는 파이프라인 계약이라, market-api
# (TSDB)가 비어 있는 환경에서 Supabase 뉴스·공시까지 같이 죽는다.

HOLDINGS_EVIDENCE_PERSONA = "holdings-analyst-worker"


def _evidence_getter():
    """읽기면 호출자. X-Agent-Persona 를 워커 명의로 싣는다.

    research-api 가 TOOL_GATEWAY_ENFORCE=true 로 돌면 persona 없는 요청은
    403 이다 - holdings-analyst-worker 는 news/disclosures/snapshot/bars
    스코프를 전부 가진 persona 다(hermes/config.yaml tool_allowlist).
    """
    from evidence.api_client import get_json

    def get(url: str):
        return get_json(url, persona=HOLDINGS_EVIDENCE_PERSONA, timeout=10)

    return get


def gather_holdings_evidence(symbol: str, *, get=None) -> dict:
    """보유 질문용 근거를 부서 읽기면에서 모은다. 소스별 독립·정직 보고.

    뉴스는 48시간(주말을 넘겨도 최근 흐름이 잡히게), 공시는 7일. 값이 비면
    빈 대로 싣는다 - "없다" 는 사실이고, 지어내는 것보다 낫다.
    """
    research = (os.environ.get("RESEARCH_API_URL")
                or "http://127.0.0.1:8035").rstrip("/")
    if get is None:
        get = _evidence_getter()
    sources: dict[str, dict] = {}
    out: dict = {"symbol": symbol, "sources": sources}

    try:
        news = get(f"{research}/evidence/news?symbol={symbol}&hours=48&limit=10")
        # ref('n1'..)는 짧아서 LLM 이 정확히 인용한다. evidence_id(document_id)
        # 는 코드가 대조할 진짜 좌표다(evidence/bundle.py 의 계약과 동형).
        out["news_headlines"] = [
            {"ref": f"n{i + 1}", "evidence_id": n.get("document_id"),
             "title": n.get("title"), "relation": n.get("relation_type"),
             "url": n.get("url"), "published_at": str(n.get("published_at") or ""),
             "observed_at": str(n.get("observed_at") or "")}
            for i, n in enumerate(news)]
        sources["news"] = {"status": "OK", "count": len(news)}
    except Exception as e:  # noqa: BLE001 - 소스 하나의 실패가 전체를 못 죽인다
        sources["news"] = {"status": "FAILED",
                           "reason": f"{type(e).__name__}: {e}"}

    try:
        disc = get(f"{research}/evidence/disclosures?symbol={symbol}&days=7&limit=5")
        out["disclosures_7d"] = [
            {"ref": f"d{i + 1}", "evidence_id": d.get("document_id"),
             "title": d.get("title"), "url": d.get("url"),
             "published_at": str(d.get("published_at") or ""),
             "observed_at": str(d.get("observed_at") or "")}
            for i, d in enumerate(disc)]
        sources["disclosures"] = {"status": "OK", "count": len(disc)}
    except Exception as e:  # noqa: BLE001
        sources["disclosures"] = {"status": "FAILED",
                                  "reason": f"{type(e).__name__}: {e}"}

    try:
        from evidence import bundle as evidence_bundle
        # fetch_price_context 는 실패를 스스로 UNAVAILABLE 로 기술한다 -
        # TSDB 가 빈 환경에서도 뉴스·공시와 독립적으로 동작한다.
        ctx = evidence_bundle.fetch_price_context(symbol, get=get)
        out["price_context"] = ctx
        sources["price_context"] = {"status": ctx.get("status", "UNKNOWN")}
    except Exception as e:  # noqa: BLE001
        sources["price_context"] = {"status": "FAILED",
                                    "reason": f"{type(e).__name__}: {e}"}
    return out


# Worker 프롬프트 예산. employee_worker_runtime._compact 가 tool_output 직렬화를
# **문자 단위 raw[:8000]** 으로 자른다 - 넘치면 JSON 이 중간에서 끊겨 '수집은
# OK 인데 수치는 없는' 오정보가 된다(2026-08-13 리뷰 실측: 뉴스 10건+공시 5건+
# price_context ≈ 5,400~7,000자, 본부장이 3KB 텍스트를 얹으면 절벽을 넘는다).
# 어댑터 래핑(worker_id·tools 키) 오버헤드 몫을 빼고 7,200자를 상한으로 잡아
# **항목 단위로** 덜어낸다 - 덜어낸 사실은 evidence_truncated 로 남긴다.
_EVIDENCE_CHAR_BUDGET = 7200
_USER_TEXT_CAP = 1000


def _clip_text(value, limit: int = _USER_TEXT_CAP):
    s = str(value)
    return s if len(s) <= limit else s[:limit] + "…(잘림)"


def merge_holdings_evidence(payload: dict, evidence: dict) -> dict:
    """수집한 근거를 Worker input_fields(news·portfolio_state)에 접어 넣는다.

    holdings-analyst-worker 의 tool 어댑터는 payload 투영이라, 여기 안 실린
    근거는 Worker 에게 영원히 안 보인다. 사용자가 준 텍스트는 user_note/
    user_state 로 보존한다(단 1,000자 캡) - 덮어쓰면 질문에 실어준 맥락이
    사라지고, 캡이 없으면 본부장이 실은 긴 텍스트가 가격 블록을 통째로
    밀어낸다. 전체 직렬화가 예산을 넘으면 오래된 뉴스·공시부터 항목 단위로
    덜어내고 그 사실을 evidence_truncated 에 남긴다.
    """
    import json

    p = dict(payload)
    # closes(일봉 21개 리스트)는 요약 통계(last_close·수익률)와 중복인 원자료라
    # 프롬프트에서 뺀다 - 수치 근거는 price_context 의 요약 필드가 이미 든다.
    ctx = evidence.get("price_context")
    if isinstance(ctx, dict) and "closes" in ctx:
        ctx = {k: v for k, v in ctx.items() if k != "closes"}
    news_block = {
        "user_note": _clip_text(p["news"]) if p.get("news") else None,
        "headlines": list(evidence.get("news_headlines") or []),
        "disclosures_7d": list(evidence.get("disclosures_7d") or []),
        "source_status": evidence.get("sources"),
    }
    p["news"] = {k: v for k, v in news_block.items() if v}
    state_block = {
        "user_state": (_clip_text(p["portfolio_state"])
                       if p.get("portfolio_state") else None),
        "price_context": ctx,
    }
    merged_state = {k: v for k, v in state_block.items() if v}
    if merged_state:
        p["portfolio_state"] = merged_state

    def _size(d: dict) -> int:
        # _compact 와 같은 직렬화 규칙으로 잰다(ensure_ascii=False, sort_keys)
        return len(json.dumps(d, ensure_ascii=False, sort_keys=True, default=str))

    news_dict = p.get("news") if isinstance(p.get("news"), dict) else None
    dropped = 0
    while news_dict and _size(p) > _EVIDENCE_CHAR_BUDGET:
        if news_dict.get("headlines"):
            news_dict["headlines"].pop()       # 뒤(오래된 것)부터 통째로
        elif news_dict.get("disclosures_7d"):
            news_dict["disclosures_7d"].pop()
        else:
            break
        dropped += 1
    if dropped:
        news_dict["evidence_truncated"] = {
            "dropped_items": dropped,
            "reason": f"Worker 프롬프트 예산({_EVIDENCE_CHAR_BUDGET}자) 초과",
        }
    # 항목을 다 덜어냈는데도 넘으면(사용자 텍스트가 지배하는 경우) 텍스트를
    # 단계적으로 더 죈다 - 예산 보장을 _compact 의 문자 절단에 맡기지 않는다.
    if _size(p) > _EVIDENCE_CHAR_BUDGET:
        if news_dict and news_dict.get("user_note"):
            news_dict["user_note"] = _clip_text(news_dict["user_note"], 300)
        ps = p.get("portfolio_state")
        if isinstance(ps, dict) and ps.get("user_state"):
            ps["user_state"] = _clip_text(ps["user_state"], 300)
    for key in ("holding_question", "proposal_draft"):
        if _size(p) > _EVIDENCE_CHAR_BUDGET and isinstance(p.get(key), str):
            p[key] = _clip_text(p[key], 3000)
    return p


def summarize_health(rows: list[dict]) -> dict:
    """collector_health 행 -> 한 눈에 보는 상태. SKIP 은 고장이 아니다."""
    bad = [r for r in rows if (r.get("bad_24h") or 0) > 0]
    return {
        "jobs_seen_24h": len(rows),
        "jobs_failing": len(bad),
        "healthy": not bad,
        "failing": [{"job": r["job_name"], "failures_24h": r["bad_24h"],
                     "last_status": r.get("last_status"),
                     "last_ok_at": str(r.get("last_ok_at")) if r.get("last_ok_at") else None,
                     "last_error": (r.get("last_error_tail") or "")[:200]}
                    for r in sorted(bad, key=lambda x: -(x.get("bad_24h") or 0))],
    }


# ---------------------------------------------------------------------------
# MCP 서버
# ---------------------------------------------------------------------------

def _server_class():
    """MCP SDK 진입 클래스. 버전에 따라 이름이 다르다 - 둘 다 받는다.

    실측 2026-08-02: 이미지에는 mcp 1.29.0(FastMCP)이 깔렸는데, 같은 명령을
    호스트에서 돌렸을 때는 최신판(MCPServer)이 왔다. pydantic 핀이 리졸버를
    다른 버전으로 민 것이다. 우리가 쓰는 표면(생성·tool·streamable-http)은
    양쪽이 같으므로 import 만 관용적으로 처리하고 동작은 하나로 둔다.
    """
    try:
        from mcp.server.mcpserver import MCPServer  # 신판
        return MCPServer, "mcpserver"
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # 1.x
        return FastMCP, "fastmcp"


# ── 응대 창구(도서관) 면에서 빼는 도구 (2026-08-13, 도서관/연구소 분리) ────
# **프롬프트로 금지하는 게 아니라 등록에서 뺀다** - OWASP LLM06(Excessive
# Agency)의 도구 최소화, capability 원칙(건네지 않은 권한은 우회 불가).
# 창구는 조회·설명 전용이고, 상태를 바꾸거나 파이프라인을 낳는 손은
# 실험대(부서 본체) 프로필에만 있다.
#   run_research_packet     - 30분짜리 분석 파이프라인 생성(쓰기·고비용)
#   factory_submit_leads    - 공장 리드 적재(쓰기)
#   factory_submit_proposal - 공장 기획안 발행(쓰기) = 질의→공장 순환의 입구
LIAISON_EXCLUDED_TOOLS = frozenset({
    "run_research_packet", "factory_submit_leads", "factory_submit_proposal"})


def _restrict_to_liaison(server) -> None:
    """등록된 도구에서 쓰기 면을 **제거**한다. 검증 불능이면 기동 거부(fail-closed).

    지우는 API 가 SDK 판마다 달라 두 경로를 시도하고, 마지막에 list_tools 로
    실제 표면을 재확인한다 - "지웠다고 믿었는데 남아 있는" 것이 최악의 상태라
    재확인이 최종 판정이다.
    """
    import asyncio

    for name in sorted(LIAISON_EXCLUDED_TOOLS):
        remove = getattr(server, "remove_tool", None)
        if callable(remove):
            try:
                remove(name)
                continue
            except Exception:  # noqa: BLE001 - 아래 경로와 재확인이 받는다
                pass
        tm = getattr(server, "_tool_manager", None)
        tools = getattr(tm, "_tools", None)
        if isinstance(tools, dict):
            tools.pop(name, None)
    names = {t.name for t in asyncio.run(server.list_tools())}
    leaked = names & LIAISON_EXCLUDED_TOOLS
    if leaked:
        raise RuntimeError(
            f"liaison 면에서 쓰기 도구를 제거하지 못했다: {sorted(leaked)} - "
            f"창구가 공장 쓰기 손을 가진 채 뜨는 것은 금지다(기동 거부)")


def build_server(*, host: str = "0.0.0.0", port: int = DEFAULT_PORT,
                 surface: str = "full"):
    cls, flavor = _server_class()
    kwargs = {"instructions": None}
    # 신판만 title 을 받는다 - 옛판에 넘기면 TypeError 다
    if flavor == "mcpserver":
        kwargs["title"] = "리서치본부"
    else:
        kwargs.update(host=host, port=port)      # 1.x 는 생성 시 settings 로 받는다

    liaison = str(surface).strip().lower() == "liaison"
    instructions = (
        "리서치본부 응대 창구(도서관)다. 조회·설명 전용 - 실험을 만들거나 "
        "공장에 무엇도 제출하지 않으며, 그 도구 자체가 이 면에 없다. 사용자 "
        "질의에는 조회 도구의 결과만으로 답하고, 새 분석·실험·수집이 필요한 "
        "요청은 즉석 수행 대신 '연구소 격상 필요'를 명시해 접수 사실만 답한다. "
        "도구 결과는 결정론 산출물이므로 그대로 인용하고, 수치를 다시 계산하거나 "
        "지어내지 않는다.") if liaison else (
        "리서치본부(RES)의 도구 면이다. 시세·Evidence 조회와 Research Packet "
        "생성만 한다. 주문 제출·리스크 판정·원장 기록은 이 본부의 권한이 "
        "아니며 여기에 도구도 없다. 도구 결과는 결정론 산출물이므로 그대로 "
        "인용하고, 수치를 다시 계산하거나 지어내지 않는다.")
    server = cls(
        name="research-liaison" if liaison else "research-department",
        **{k: v for k, v in kwargs.items() if k != "instructions"},
        instructions=instructions,
    )

    @server.tool(
        name="run_research_packet",
        description="종목 하나에 대해 리서치 파이프라인(분석가 6인)을 시작한다. "
                    "수 분이 걸리므로 job_id 만 즉시 돌려준다 - 결과는 "
                    "get_packet_job 으로 조회한다.")
    def run_research_packet(symbol: str) -> dict:
        sym = normalize_symbol(symbol)
        job_id = register_job(sym, now=datetime.now(KST))

        def _worker():
            try:
                proc = subprocess.run(
                    [sys.executable, "scripts.py", "--run", sym],
                    check=False, cwd=str(_BASE), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=60 * 30)
                out = (proc.stdout or "") + "\n" + (proc.stderr or "")[-800:]
                finish_job(job_id, exit_code=proc.returncode, tail=out,
                           now=datetime.now(KST))
            except Exception as e:  # noqa: BLE001 - 실패를 실패로 남긴다
                finish_job(job_id, exit_code=-1,
                           tail=f"{type(e).__name__}: {e}", now=datetime.now(KST))

        threading.Thread(target=_worker, daemon=True).start()
        return {"job_id": job_id, "symbol": sym, "status": "RUNNING",
                "note": "get_packet_job(job_id) 으로 진행 상태를 확인한다"}

    @server.tool(
        name="get_packet_job",
        description="run_research_packet 이 만든 작업의 상태·결과 경로를 조회한다.")
    def get_packet_job(job_id: str) -> dict:
        with _JOBS_LOCK:
            j = _JOBS.get(str(job_id).strip())
        if j is None:
            return {"error": "그런 job_id 가 없다(서버 재시작 시 메모리가 비워진다). "
                             "완료된 Packet 은 list_recent_packets 로 찾는다"}
        return dict(j)

    # ── 직원 Worker 실행 (LangGraph runner) ──────────────────────────────
    # ▶ 지금까지 run_employee_workers 를 부르는 손은 파이썬 오케스트레이터
    #   (paper_pipeline, portfolio_recommendation)뿐이었다 - 본부장(Hermes)이
    #   자기 부서 Worker 를 돌릴 간선이 없었다. 이 도구가 그 간선이다.
    # ▶ Worker 는 Worker Model Gateway(departments/worker_model_gateway.py)를
    #   통해 모델을 부른다. AWS 에서는 vLLM Qwen2.5-14B FP8, DEV 에서는
    #   기존 Ollama 로 자동 해석된다. Worker 산출은 언제나 비구속
    #   worker-context 다 - 주문·판정·원장에 닿지 않는다.
    def _gateway_module():
        """repo 루트를 sys.path 에 얹고 gateway 모듈만 준다 (경량, stdlib).

        컨테이너에서 repo 루트는 /app 이다. worker_model_gateway.py 등은
        이미지에 없고 docker-compose.model.yml 이 마운트한다 - 없으면
        ImportError 가 그대로 실패로 남는다(조용한 성공 위장 금지).
        """
        repo_root = _BASE.parent.parent
        for p in (str(repo_root), str(_BASE.parent)):
            if p not in sys.path:
                sys.path.insert(0, p)
        import worker_model_gateway as gateway  # departments/ 에서 온다
        return gateway

    def _worker_modules():
        """(gateway, employee_workers). 후자는 langgraph 를 끌어와 무겁다 -
        health 처럼 gateway 만 필요한 자리는 _gateway_module 을 쓴다."""
        gateway = _gateway_module()
        import employee_workers                 # _BASE 에서 온다
        return gateway, employee_workers

    @server.tool(
        name="run_research_workers",
        description="리서치 직원 Worker(LangGraph 2인)를 실행한다. "
                    "holding_question 을 주면 holdings-analyst-worker, "
                    "proposal_draft 를 주면 competing-explanation-worker 가 돈다 "
                    "(둘 다 주면 둘 다 돈다). **보유 질문이 특정 종목에 관한 "
                    "것이면 symbol(6자리 종목코드)을 반드시 함께 줘라** - 러너가 "
                    "Worker 호출 전에 부서 읽기면(research-api·market-api)에서 "
                    "최근 뉴스·공시·가격 근거를 모아 Worker 에게 준다(Evidence "
                    "First). symbol 없이 주는 portfolio_state·news 텍스트는 "
                    "보조 근거로만 실린다. 모델 추론이 수십 초 걸리므로 job_id "
                    "만 즉시 돌려준다 - get_worker_job 으로 결과(worker-context, "
                    "비구속)를 조회한다. Worker 산출은 요약하지 말고 summary·"
                    "confidence·evidence_refs·escalate 를 그대로 인용하고, "
                    "job 의 evidence.sources 에 FAILED 가 있으면 그 사실도 "
                    "함께 보고하라.")
    def run_research_workers(holding_question: str = "", proposal_draft: str = "",
                             portfolio_state: str = "", news: str = "",
                             symbol: str = "") -> dict:
        try:
            payload = build_worker_payload(holding_question, proposal_draft,
                                           portfolio_state, news)
            sym = normalize_symbol(symbol) if str(symbol or "").strip() else ""
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        job_id = register_worker_job(sorted(payload), now=datetime.now(KST),
                                     symbol=sym)

        def _run():
            # try 밖에서 초기화한다 - 근거를 모은 뒤 Worker 단계에서 죽어도
            # FAILED job 에 evidence 요약이 남아야 진단이 된다.
            evidence_summary = None
            try:
                gateway, employee_workers = _worker_modules()
                # Evidence First (§11) - Worker 를 부르기 전에 근거부터 모은다.
                # 실패해도 job 을 죽이지 않는다 - 소스별 상태가 evidence 에
                # 그대로 남고, Worker 는 모인 만큼의 근거로 판단한다.
                if sym and "holding_question" in payload:
                    evidence = gather_holdings_evidence(sym)
                    payload_with_evidence = merge_holdings_evidence(
                        payload, evidence)
                    evidence_summary = {"symbol": sym,
                                        "sources": evidence.get("sources")}
                else:
                    payload_with_evidence = payload
                # ▶ 워커별로 binding 을 해석한다 (2026-08-13 리뷰 반영).
                #   단일 llm 을 공유하면 registry 의 worker→adapter 해석이
                #   전달되지 않아 LoRA 승격이 조용한 no-op 이 된다.
                worker_bindings: dict[str, dict] = {}

                def llm_factory(worker_id: str):
                    llm, binding = gateway.llm_for_worker(worker_id)
                    worker_bindings[worker_id] = binding.as_metadata()
                    return llm

                result = employee_workers.run_employee_workers(
                    payload_with_evidence, llm_factory=llm_factory)
                default_binding = gateway.resolve()
                # runtime 블록의 provider/model 은 공용 런타임이 Ollama 를
                # 하드코딩한 값이다 - 실제 사용한 게이트웨이 좌표로 바로잡는다.
                # 안 그러면 본부장이 '이 분석은 ollama qwen3:1.7b' 라는 틀린
                # 계보를 그대로 인용한다.
                runtime = dict(result.get("runtime") or {})
                runtime["provider"] = default_binding.provider
                runtime["model"] = default_binding.model
                runtime["model_source"] = "worker_model_gateway"
                result["runtime"] = runtime
                finish_worker_job(job_id, result=result,
                                  model_plane={
                                      "default": default_binding.as_metadata(),
                                      "workers": worker_bindings,
                                  },
                                  error=None, now=datetime.now(KST),
                                  evidence=evidence_summary)
            except Exception as e:  # noqa: BLE001 - 실패를 실패로 남긴다
                finish_worker_job(job_id, result=None, model_plane=None,
                                  error=f"{type(e).__name__}: {e}",
                                  now=datetime.now(KST),
                                  evidence=evidence_summary)

        threading.Thread(target=_run, daemon=True).start()
        return {"job_id": job_id, "status": "RUNNING",
                "payload_fields": sorted(payload),
                "symbol": sym or None,
                "evidence_first": bool(sym and "holding_question" in payload),
                "note": "get_worker_job(job_id) 으로 결과를 조회한다"}

    @server.tool(
        name="get_worker_job",
        description="run_research_workers 가 만든 작업의 상태·결과를 조회한다. "
                    "COMPLETED 여도 result.degraded 가 true 면 일부 Worker 가 "
                    "실패한 것이다 - 그 사실을 숨기지 말고 그대로 보고하라.")
    def get_worker_job(job_id: str) -> dict:
        with _WORKER_JOBS_LOCK:
            j = _WORKER_JOBS.get(str(job_id).strip())
        if j is None:
            return {"error": "그런 job_id 가 없다(서버 재시작 시 메모리가 "
                             "비워진다). run_research_workers 로 다시 시작하라"}
        return dict(j)

    @server.tool(
        name="worker_model_health",
        description="Worker 모델 서빙 상태를 확인한다 - 어떤 게이트웨이 좌표로 "
                    "해석되는지, 모델 서버가 실제로 응답하는지, registry 에 켜진 "
                    "adapter 가 실제 서빙 목록에 있는지. Worker 실행이 계속 "
                    "실패하면 먼저 이걸 본다.")
    async def worker_model_health() -> dict:
        # async + to_thread: sync 도구는 이벤트 루프에서 그대로 돌아, vLLM 이
        # 멎어 있으면 urlopen(timeout=10) 동안 서버의 모든 MCP 세션이 함께
        # 멎는다(2026-08-13 리뷰). 진단 도구가 서버를 세우면 안 된다.
        import asyncio

        def _probe() -> dict:
            import urllib.request as _rq

            try:
                gateway = _gateway_module()   # 경량 - langgraph 를 안 끌어온다
                binding = gateway.resolve()
                enabled = gateway.enabled_adapters()
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "stage": "resolve",
                        "error": f"{type(e).__name__}: {e}"}
            meta = binding.as_metadata()
            req = _rq.Request(binding.base_url + "/models",
                              headers={"Authorization": f"Bearer {binding.api_key}"})
            try:
                with _rq.urlopen(req, timeout=10) as r:
                    import json as _json
                    served = [m.get("id")
                              for m in _json.loads(r.read()).get("data", [])]
            except Exception as e:  # noqa: BLE001 - 죽었으면 죽었다고 말한다
                return {"ok": False, "stage": "server", "binding": meta,
                        "error": f"{type(e).__name__}: {e}"}
            # registry 에 켜진 adapter 가 서빙 목록에 없으면 그 워커 호출은
            # 실패한다 - 불일치를 여기서 표면화한다
            missing = {w: a for w, a in enabled.items() if a not in served}
            notes = []
            if binding.model not in served:
                notes.append(f"서버는 살아 있는데 '{binding.model}' 이 서빙 목록에 없다")
            for w, a in missing.items():
                notes.append(f"registry 는 {w}→{a} 를 켰는데 서버에 그 adapter 가 없다")
            return {"ok": binding.model in served and not missing,
                    "binding": meta, "served_models": served,
                    "registry_enabled_adapters": enabled,
                    "note": "; ".join(notes) or None}

        return await asyncio.to_thread(_probe)

    @server.tool(
        name="list_recent_packets",
        description="최근 생성된 Research Packet 실행 기록(종목·상태·근거품질·시각).")
    def list_recent_packets(limit: int = 10) -> list[dict]:
        n = max(1, min(int(limit), 50))
        return _rows("""
            select symbol, status, evidence_quality, numeric_check_ok,
                   started_at, report_path, trace_id::text
            from research.pipeline_runs
            order by started_at desc limit %s""", (n,))

    @server.tool(
        name="collector_health",
        description="24시간 수집 Job 건강 상태. 지금 무엇이 고장나 있는지 알려준다 "
                    "(SKIP=휴장 등 의도된 미수집은 고장이 아니다).")
    def collector_health() -> dict:
        return summarize_health(_rows("""
            select job_name, runs_24h, ok_24h, skip_24h, bad_24h,
                   last_ok_at, last_status, last_error_tail
            from research.collector_health"""))

    @server.tool(
        name="geopolitical_state",
        description="현재 지정학 국면(GPR 위협/실제 분리, GDELT 테마 배율). "
                    "지수는 게시가 늦으므로 관측일과 지연일을 함께 본다.")
    def geopolitical_state() -> dict:
        sys.path.insert(0, str(_BASE / "agents"))
        from geopolitical_analyst import analyze as geo_analyze

        r = geo_analyze()
        ro = r.get("readout") or {}
        return {"verdict": r.get("verdict"), "driver": r.get("driver"),
                "label_reason": ro.get("label_reason"),
                "gpr_latest": ro.get("gpr_latest"),
                "gpr_percentile": ro.get("gpr_percentile"),
                "gpr_observed_on": ro.get("latest_gpr_date"),
                "gpr_lag_days": ro.get("gpr_lag_days"),
                "hot_themes": ro.get("hot_themes"),
                "summary": r.get("summary"), "cautions": r.get("cautions")}

    @server.tool(
        name="analyst_calibration",
        description="분석가 판정별 사후 성과(선순환 되먹임). 표본이 적으면 "
                    "숫자를 믿지 않는다 - n 을 반드시 함께 본다.")
    def analyst_calibration() -> list[dict]:
        return _rows("""
            select node, verdict, horizon_days, n, avg_forward_return_pct,
                   median_forward_return_pct, pct_positive
            from research.analyst_calibration
            order by n desc, node limit 50""")

    # ── 공장 도구 ──────────────────────────────────────────────────────────
    # ▶ **에이전트가 공장을 직접 돌리게 하는 손이다.** 지금까지 스카우트 산출을
    #   사람이 파일로 받아 CLI 에 넣었다 - 그 사람이 빠지면 루프가 멈췄다.
    #
    # ▶ **쓰기 권한이 생기는 첫 도구들이라 경계를 좁게 긋는다.**
    #   에이전트는 **제출만** 한다. 파싱·검증·판정·적재는 전부 코드가 한다.
    #   그래서 `quant.hypotheses` 에 닿는 도구는 여기 없다 - 있으면 에이전트가
    #   Gate 0 를 우회해 자기 가설을 등록할 수 있고, 그러면 생성자·검증자 분리가
    #   조직이 아니라 코드 수준에서 무너진다. 접수는 `factory_bridge --intake`
    #   가 별도로 돌린다.
    def _factory_path():
        import sys as _s
        from pathlib import Path as _P
        _s.path.insert(0, str(_P(__file__).resolve().parents[1] / "factory"))
        _s.path.insert(0, str(_P(__file__).resolve().parents[1] / "collectors"))

    @server.tool(
        name="factory_brief",
        description="회차 브리핑. 원장에서 읽은 사실만 준다 - 지난 실험이 왜 "
                    "끝났는지(교훈), 계열별 남은 시도 예산, 최근 돌린 질의, 지금 "
                    "데이터가 지원하는 범위. 스카우트를 소집하기 전에 반드시 읽어라: "
                    "여기 적힌 기각 사유에 대응하지 않으면 Gate 0 가 접수를 막는다.")
    def factory_brief() -> str:
        _factory_path()
        import cycle_brief

        conn = _db()
        mkt = None
        try:
            import psycopg2
            from source_registry import load_project_env

            try:
                mkt = psycopg2.connect(
                    load_project_env()["TIMESCALE_DATABASE_URL"], connect_timeout=10)
            except Exception:
                mkt = None          # 못 재면 커버리지를 비운다(지어내지 않는다)
            return cycle_brief.build(conn, market_conn=mkt).as_prompt()
        finally:
            conn.close()
            if mkt:
                mkt.close()

    @server.tool(
        name="factory_submit_leads",
        description="스카우트 산출을 방법론 리드로 제출한다. TITLE/URL/MECHANISM "
                    "블록 형식 원문을 그대로 넣어라. 코드가 링크를 실제로 열어 보고, "
                    "메커니즘 없는 블록을 반려하고, 중복을 접어서 적재한다. "
                    "**네가 적재 여부를 판단하지 않는다** - 반려 사유를 받아 고쳐라.")
    def factory_submit_leads(text: str, lens: str = "ACADEMIC",
                             source_type: str = "PAPER",
                             model_version: str = "", prompt_version: str = "") -> dict:
        _factory_path()
        import lead_intake

        if not model_version.strip() or not prompt_version.strip():
            # 계보 없는 리드는 재현할 수 없다. 여기서 막지 않으면 손으로 넣은
            # 것과 에이전트가 찾은 것이 DB 에서 구분되지 않는다.
            return {"ok": False,
                    "error": "model_version·prompt_version 이 필요하다 - "
                             "계보 없는 리드는 재현할 수 없어 받지 않는다"}
        from datetime import datetime as _dt, timezone as _tz

        case_id = f"scout-{lens.lower()}-{_dt.now(_tz.utc):%Y%m%d}"
        r = lead_intake.intake(text, lens=lens, source_type=source_type,
                               case_id=case_id, model_version=model_version,
                               prompt_version=prompt_version)
        new = dup = 0
        if r.leads:
            conn = _db()
            try:
                new, dup = lead_intake.persist(conn, r.leads)
            finally:
                conn.close()
        return {"ok": bool(r.leads), "accepted": len(r.leads),
                "new": new, "merged_as_mention": dup,
                "rejected": [{"title": x.title, "why": x.reason} for x in r.rejected],
                "link_checks": r.link_notes}

    @server.tool(
        name="factory_submit_proposal",
        description="기획안을 발행 게이트에 제출한다. **이 호출이 곧 납품이다** - "
                    "카드 텍스트·요약은 납품으로 세지 않는다. `planner_run` 에 "
                    "지금 작업 중인 칸반 카드 ID(t_ 로 시작)를 넣으면 납품이 그 "
                    "카드 몫으로 대조된다. 기획자 산출과 **독립 회의론자** 산출을 "
                    "함께 넣어라 - 서명이 기획자와 같은 실행이면 거부된다. 근거 "
                    "리드는 원장에서 다시 읽어 대조하므로 없는 리드를 대면 막힌다. "
                    "차단 사유가 이 도구의 결과로 즉시 돌아온다 - 사유에 답해 같은 "
                    "카드 안에서 다시 제출하라. `llm_model_id` 와 "
                    "`llm_training_cutoff`(YYYY-MM-DD, 네 지식 컷오프)를 함께 "
                    "넣어라 - 백테스트 성적을 컷오프 이전/이후로 가르는 데 쓴다.")
    def factory_submit_proposal(planner_text: str, skeptic_text: str,
                                planner_run: str, skeptic_run: str,
                                planner_prompt: str = "",
                                skeptic_prompt: str = "",
                                llm_model_id: str = "",
                                llm_training_cutoff: str = "") -> dict:
        _factory_path()
        import proposal_intake as PI

        from datetime import datetime as _dt, timezone as _tz

        conn = _db()
        try:
            wanted = {i for b in PI.parse_blocks(planner_text, PI.PLANNER_KEYS)
                      for i in PI._split(b.get("LEAD_IDS", ""))}
            leads = PI.load_leads(conn, sorted(wanted))
            missing = sorted(wanted - set(leads))
            # ▶ **계열 이력을 넘긴다** (2026-08-13). 호스트 수확 경로와 같은
            #   버그가 여기에도 있었다 - 인자가 빠져 모든 기획안이 "이 계열은
            #   처음" 으로 접수됐고, 승격 관문만 진짜 이력을 봐서 뒤늦게
            #   영구 반려했다. 에이전트가 실제로 쓰는 경로가 이쪽이므로
            #   여기가 안 맞으면 고쳐도 안 고쳐진 것과 같다.
            # ▶ **납품을 카드 몫으로 각인한다** (2026-08-13, 납품=도구 호출 계약)
            #   planner_run 에 칸반 카드 ID(t_...)가 오면 case_id 에 그대로
            #   찍는다. 수확기 `_mcp_deliveries` 가 이 각인으로 "이 카드가
            #   납품했는가" 를 원장에서 결정론적으로 대조한다 - 텍스트 채널의
            #   2,434자/123자 변동성이 납품 판정과 무관해지는 자리다.
            _pr = str(planner_run or "").strip()
            r = PI.intake(planner_text, skeptic_text,
                          case_id=(f"card-{_pr[:40]}" if _pr.startswith("t_")
                                   else f"plan-{_dt.now(_tz.utc):%Y%m%d}"),
                          planner_run=planner_run, skeptic_run=skeptic_run,
                          leads=leads,
                          outcomes_for=lambda b: PI.load_past_outcomes(
                              conn,
                              (b.get("EDGE_TYPE") or "").strip().lower(),
                              (b.get("UNIVERSE_KEY") or "").strip().lower(),
                              label=(b.get("LABEL") or "").strip(),
                              baseline=(b.get("BASELINE") or "").strip()))
            for prop, _g in r.proposals:
                setattr(prop, "_planner_prompt", planner_prompt)
                setattr(prop, "_skeptic_prompt", skeptic_prompt)
            pub = r.publishable
            new = dup = 0
            if pub:
                new, dup = PI.persist(conn, pub)
                # ▶ **LLM 시점 스탬프** (2026-08-13, 관문 9조항의 계측 기초)
                #   Sarkar-Vafa 실증: 익명화해도 LLM 은 재식별한다 - 방어선은
                #   시간 분할이고, 그 전제가 "가설을 쓴 모델의 지식 컷오프가
                #   원장에 각인돼 있는 것" 이다. 스탬프가 있어야 나중에 성적을
                #   컷오프 이전(참고치)/이후(판정치)로 가를 수 있다. 값이 없으면
                #   NULL 로 남는다 - 지어내지 않는다.
                if llm_model_id.strip() or llm_training_cutoff.strip():
                    with conn.cursor() as _cu:
                        _cu.execute(
                            "update research.experiment_proposals set"
                            "  llm_model_id = coalesce(nullif(%s,''), llm_model_id),"
                            "  llm_training_cutoff = coalesce(nullif(%s,'')::date,"
                            "                                 llm_training_cutoff)"
                            " where proposal_id = any(%s)",
                            (llm_model_id.strip()[:120],
                             llm_training_cutoff.strip()[:10],
                             [p.proposal_id for p in pub]))
                    conn.commit()
            return {
                "ok": bool(pub), "published": new, "already_present": dup,
                "unknown_lead_ids": missing,
                "rejected": [{"title": x.title, "why": x.reason} for x in r.rejected],
                "gate": [{"proposal_id": p.proposal_id, "passed": g.ok,
                          "blockers": g.blockers, "warnings": g.warnings}
                         for p, g in r.proposals],
            }
        finally:
            conn.close()

    @server.tool(
        name="factory_outcomes",
        description="최근 실험 종결 기록(판정·교훈 코드·지표). 같은 계열을 다시 "
                    "제안하기 전에 읽어라 - 기각 교훈에 대응이 없으면 Gate 0 가 "
                    "DUPLICATE_UNADDRESSED 로 막는다.")
    def factory_outcomes(limit: int = 10) -> list[dict]:
        n = max(1, min(int(limit), 50))
        return _rows("""
            select experiment_id::text, trial_family_id, decision, lesson_codes,
                   oos_summary, coalesce(notes, '') as notes, created_at
            from research.experiment_outcomes
            order by created_at desc limit %s""", (n,))

    # 외부 정보원(DART·네이버·ECOS·FRED) 질의 도구 - 정성 데이터의 MCP 검색 통합
    # (재일 결정 2026-08-13, docs/02-engineering/MCP_ONDEMAND_ARCHITECTURE.md).
    # 별도 모듈인 이유: 예산·스냅샷·정직성 규약이 한 파일에 살아야 감사가 쉽다.
    from external_sources import register_external_tools
    from external_macro import register_macro_tools
    register_external_tools(server)
    register_macro_tools(server)

    if liaison:
        _restrict_to_liaison(server)
    return server


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _check_symbol_guard():
    assert normalize_symbol("000660") == "000660"
    assert normalize_symbol("  000660 ") == "000660"
    for bad in ("66", "00066O", "", None, "0006601"):
        try:
            normalize_symbol(bad)
            raise AssertionError(f"잘못된 코드가 통과했다: {bad!r}")
        except ValueError:
            pass
    print("  종목코드 가드            OK")


def _check_job_lifecycle():
    now = datetime(2026, 8, 2, 10, tzinfo=KST)
    jid = register_job("000660", now=now)
    with _JOBS_LOCK:
        assert _JOBS[jid]["status"] == "RUNNING"
    done = finish_job(jid, exit_code=0,
                      tail="어쩌고\n리포트 저장: reports/research_packet_000660_x.md\n",
                      now=now)
    assert done["status"] == "COMPLETED"
    assert done["report"] == "reports/research_packet_000660_x.md", done
    jid2 = register_job("000660", now=now)
    fail = finish_job(jid2, exit_code=1, tail="ValueError: Packet 초안 거부", now=now)
    assert fail["status"] == "FAILED" and fail["report"] is None
    assert finish_job("없는아이디", exit_code=0, tail="", now=now) == {}
    print("  작업 수명주기            OK")


def _check_worker_payload():
    p = build_worker_payload(holding_question="  삼성전자 실적?  ",
                             news="", portfolio_state="보유 10주")
    assert p == {"holding_question": "삼성전자 실적?", "portfolio_state": "보유 10주"}, p
    p = build_worker_payload(proposal_draft="draft-x")
    assert p == {"proposal_draft": "draft-x"}
    p = build_worker_payload(holding_question="q", proposal_draft="d")
    assert set(p) == {"holding_question", "proposal_draft"}
    # 트리거 없이 보조 필드만 주면 거부한다 - 아무도 안 도는 실행을
    # RUNNING 으로 위장하지 않는다
    for kwargs in ({}, {"news": "n"}, {"portfolio_state": "s"},
                   {"holding_question": "   "}):
        try:
            build_worker_payload(**kwargs)
            raise AssertionError(f"트리거 없는 payload 가 통과했다: {kwargs}")
        except ValueError:
            pass
    print("  Worker payload 계약      OK")


def _check_worker_job_lifecycle():
    now = datetime(2026, 8, 13, 10, tzinfo=KST)
    jid = register_worker_job(["holding_question"], now=now)
    with _WORKER_JOBS_LOCK:
        assert _WORKER_JOBS[jid]["status"] == "RUNNING"
        assert _WORKER_JOBS[jid]["payload_fields"] == ["holding_question"]
    done = finish_worker_job(
        jid, result={"executed": ["holdings-analyst-worker"], "degraded": False},
        model_plane={"provider": "vllm-openai", "model_version": "m"},
        error=None, now=now)
    assert done["status"] == "COMPLETED" and done["result"]["degraded"] is False
    assert done["model_plane"]["provider"] == "vllm-openai"

    jid2 = register_worker_job(["proposal_draft"], now=now)
    fail = finish_worker_job(jid2, result=None, model_plane=None,
                             error="ImportError: worker_model_gateway", now=now)
    assert fail["status"] == "FAILED" and "ImportError" in fail["error"]
    assert finish_worker_job("없는아이디", result=None, model_plane=None,
                             error=None, now=now) == {}
    print("  Worker 작업 수명주기     OK")


def _check_holdings_evidence():
    """Evidence First - 소스별 독립 시도·정직 보고·payload 병합 계약."""
    def fake_get(url: str):
        if "/evidence/news" in url:
            return [{"document_id": "doc-1", "title": "제목",
                     "relation_type": "direct", "url": "http://u",
                     "published_at": "2026-08-13", "observed_at": "2026-08-13"}]
        if "/evidence/disclosures" in url:
            raise RuntimeError("research-api down")
        raise ValueError("tsdb empty")          # /bars → price_context 경로

    ev = gather_holdings_evidence("005930", get=fake_get)
    assert ev["symbol"] == "005930"
    assert ev["sources"]["news"] == {"status": "OK", "count": 1}
    assert ev["news_headlines"][0]["ref"] == "n1"
    assert ev["news_headlines"][0]["evidence_id"] == "doc-1"
    # 한 소스의 실패가 다른 소스를 못 죽인다 - 사유는 그대로 남는다
    assert ev["sources"]["disclosures"]["status"] == "FAILED"
    assert "research-api down" in ev["sources"]["disclosures"]["reason"]
    # price_context 는 자기 기술 UNAVAILABLE (fetch_price_context 내장 규율)
    assert ev["sources"]["price_context"]["status"] == "UNAVAILABLE", ev["sources"]

    merged = merge_holdings_evidence(
        {"holding_question": "q", "news": "사용자 메모", "portfolio_state": "10주"},
        ev)
    assert merged["holding_question"] == "q", "질문은 그대로 남는다"
    assert merged["news"]["user_note"] == "사용자 메모", "사용자 텍스트를 보존한다"
    assert merged["news"]["headlines"][0]["ref"] == "n1"
    assert merged["news"]["source_status"]["disclosures"]["status"] == "FAILED"
    assert merged["portfolio_state"]["user_state"] == "10주"
    # price_context 는 UNAVAILABLE 상태 그대로 실린다 - 실패를 숨기지 않는다
    assert merged["portfolio_state"]["price_context"]["status"] == "UNAVAILABLE"

    # 근거가 하나도 없으면 news 는 상태 보고만 남는다(빈 성공으로 위장 금지)
    def all_down(url: str):
        raise RuntimeError("down")

    ev2 = gather_holdings_evidence("005930", get=all_down)
    merged2 = merge_holdings_evidence({"holding_question": "q"}, ev2)
    assert set(merged2["news"]) == {"source_status"}, merged2["news"]

    # ── 프롬프트 예산 경계 (2026-08-13 리뷰) ──
    # _compact 는 문자 단위 raw[:8000] 절단이라, 예산을 넘기면 JSON 이 중간에서
    # 끊겨 '수집 OK 인데 수치 없음' 오정보가 된다. 최대 픽스처로 항목 단위
    # 절단이 예산을 지키는지 잰다.
    import json as _json

    def big_get(url: str):
        if "/evidence/news" in url:
            return [{"document_id": f"doc-{i}", "title": "제" * 80,
                     "relation_type": "direct", "url": "http://u/" + "x" * 150,
                     "published_at": "2026-08-13T09:00:00+09:00",
                     "observed_at": "2026-08-13T09:00:00+09:00"}
                    for i in range(10)]
        if "/evidence/disclosures" in url:
            return [{"document_id": f"d-{i}", "title": "공" * 80,
                     "url": "http://d/" + "y" * 150,
                     "published_at": "2026-08-13", "observed_at": "2026-08-13"}
                    for i in range(5)]
        return [{"bucket_time": f"2026-08-{i:02d}T00:00:00", "close": 70000.0 + i}
                for i in range(1, 22)]          # /bars → closes 21개

    ev3 = gather_holdings_evidence("005930", get=big_get)
    merged3 = merge_holdings_evidence(
        {"holding_question": "질문" * 100, "news": "뉴스요약" * 800,
         "portfolio_state": "상태" * 800}, ev3)
    size = len(_json.dumps(merged3, ensure_ascii=False, sort_keys=True, default=str))
    assert size <= _EVIDENCE_CHAR_BUDGET, f"예산 초과: {size}자"
    assert merged3["news"]["evidence_truncated"]["dropped_items"] >= 1
    # 사용자 텍스트 캡 - 본부장이 실은 긴 텍스트가 가격 블록을 밀어내지 않는다
    assert len(merged3["news"]["user_note"]) <= _USER_TEXT_CAP + 8
    assert len(merged3["portfolio_state"]["user_state"]) <= _USER_TEXT_CAP + 8
    # closes 원자료는 프롬프트에서 빠지고 요약 수치는 남는다
    assert "closes" not in merged3["portfolio_state"]["price_context"]
    assert "last_close" in merged3["portfolio_state"]["price_context"], \
        merged3["portfolio_state"]["price_context"]
    # 작은 픽스처는 절단 없이 그대로 - 예산 로직이 과절단하지 않는다
    assert "evidence_truncated" not in merged["news"]
    print("  Evidence First 계약     OK (예산 경계 포함)")


def _check_health_summary():
    rows = [
        {"job_name": "vkospi", "runs_24h": 2, "ok_24h": 0, "skip_24h": 2,
         "bad_24h": 0, "last_status": "SKIP", "last_ok_at": None,
         "last_error_tail": None},
        {"job_name": "geopolitical", "runs_24h": 3, "ok_24h": 0, "skip_24h": 0,
         "bad_24h": 3, "last_status": "FAILED", "last_ok_at": None,
         "last_error_tail": "FileNotFoundError"},
    ]
    s = summarize_health(rows)
    assert s["jobs_failing"] == 1 and not s["healthy"]
    assert s["failing"][0]["job"] == "geopolitical", s
    assert summarize_health([])["healthy"] is True
    print("  건강 요약(SKIP 제외)     OK")


def _check_bearer_auth():
    """토큰 검증 - 미설정이면 열고, 설정되면 정확히 일치할 때만 통과."""
    assert is_authorized(None, None) and is_authorized("Bearer x", None)
    assert is_authorized("", "") , "빈 토큰은 미설정과 같게 취급한다"

    T = "s3cr3t-value"
    assert is_authorized(f"Bearer {T}", T)
    assert is_authorized(f"bearer {T}", T), "스킴은 대소문자 무관이다"
    for bad in (None, "", "Bearer", "Bearer wrong", T, f"Basic {T}",
                "Bearer ${MCP_RESEARCH_API_KEY}"):
        assert not is_authorized(bad, T), f"통과하면 안 된다: {bad!r}"
    # 미치환 변수를 그대로 보내는 경우(Hermes 가 env 를 못 찾으면 이 모양이다)
    assert not is_authorized("Bearer ${MCP_RESEARCH_API_KEY}", T)
    print("  Bearer 인증 판정         OK")


def _check_tool_surface():
    """권한 경계 - 주문·원장·리스크 도구가 노출되지 않았는가."""
    try:
        server = build_server()
    except ImportError as e:
        print(f"  ⚠ mcp 패키지 없음 - 도구 면 검사 생략 ({e})")
        return
    import asyncio

    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "run_research_packet" in names and "collector_health" in names, names
    # 직원 Worker 실행 간선(2026-08-13) - 이 셋이 빠지면 본부장은 다시
    # '워커를 못 부르는 껍데기'다
    for required in ("run_research_workers", "get_worker_job",
                     "worker_model_health"):
        assert required in names, f"{required} 도구가 등록되지 않았다: {names}"
    forbidden = {"submit_order", "oms_submit", "write_ledger", "risk_decision",
                 "promote_strategy"}
    assert not (names & forbidden), f"권한 밖 도구가 노출됐다: {names & forbidden}"
    print(f"  도구 면·권한 경계        OK ({len(names)}개)")


def _check_liaison_surface():
    """**창구 면에는 쓰기 손이 없어야 한다** (2026-08-13, 도서관/연구소 분리).

    질의→공장 무한루프의 유일한 코드 수준 입구가 factory_submit_* 이다.
    창구가 그 도구를 가진 채 뜨면 SOUL.md 의 산문 금지는 장식이 된다 -
    프롬프트가 아니라 등록 제거(capability 절단)로 막고, 여기서 고정한다.
    """
    import asyncio

    srv = build_server(surface="liaison")
    names = {t.name for t in asyncio.run(srv.list_tools())}
    leaked = names & LIAISON_EXCLUDED_TOOLS
    assert not leaked, f"창구 면에 쓰기 도구가 남았다: {sorted(leaked)}"
    # 창구의 존재 이유인 조회 도구는 살아 있어야 한다 - 다 지우면 그건
    # 분리가 아니라 불구다. 보유 질의 응답 간선(run_research_workers)도 창구
    # 몫이다(비구속 worker-context, 원장에 닿지 않는다).
    for required in ("factory_brief", "factory_outcomes", "list_recent_packets",
                     "collector_health", "run_research_workers",
                     "get_worker_job"):
        assert required in names, f"창구 면에서 {required} 가 사라졌다: {names}"
    print(f"  창구 면 capability 절단  OK ({len(names)}개, 쓰기 0개)")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--serve" in sys.argv:
        import uvicorn

        token = os.environ.get("MCP_RESEARCH_API_KEY", "").strip()
        # 같은 이미지가 두 면으로 뜬다(도서관/연구소 분리, 2026-08-13):
        #   full    - 부서 본체(실험대). 공장 쓰기 도구 포함.
        #   liaison - 응대 창구(도서관). 쓰기 도구가 등록에서 빠진다.
        surface = os.environ.get("RESEARCH_MCP_SURFACE", "full").strip().lower()
        srv = build_server(host="0.0.0.0", port=DEFAULT_PORT, surface=surface)
        s = getattr(srv, "settings", None)
        if s is not None:
            s.host, s.port = "0.0.0.0", DEFAULT_PORT
        face = "창구(liaison·읽기 전용)" if surface == "liaison" else "본체(full)"
        if token:
            print(f"{MCP_VERSION}: 0.0.0.0:{DEFAULT_PORT}/mcp [{face}] "
                  f"(Bearer 인증 켜짐)", flush=True)
        else:
            # 잊은 것과 일부러 연 것을 구분되게 남긴다 - 조용한 무인증 금지
            print(f"{MCP_VERSION}: 0.0.0.0:{DEFAULT_PORT}/mcp [{face}] "
                  f"⚠ MCP_RESEARCH_API_KEY 미설정 - 인증 없이 연다. compose "
                  f"네트워크 밖으로 노출하지 말 것", flush=True)
        uvicorn.run(build_app(srv, token=token or None),
                    host="0.0.0.0", port=DEFAULT_PORT, log_level="info")
        raise SystemExit(0)

    print(f"{MCP_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_symbol_guard()
    _check_bearer_auth()
    _check_job_lifecycle()
    _check_worker_payload()
    _check_worker_job_lifecycle()
    _check_holdings_evidence()
    _check_health_summary()
    _check_tool_surface()
    _check_liaison_surface()
    print("리서치 MCP 9개 영역 통과. 서버는 --serve")
