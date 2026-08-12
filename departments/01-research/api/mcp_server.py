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


def build_server(*, host: str = "0.0.0.0", port: int = DEFAULT_PORT):
    cls, flavor = _server_class()
    kwargs = {"instructions": None}
    # 신판만 title 을 받는다 - 옛판에 넘기면 TypeError 다
    if flavor == "mcpserver":
        kwargs["title"] = "리서치본부"
    else:
        kwargs.update(host=host, port=port)      # 1.x 는 생성 시 settings 로 받는다

    server = cls(
        name="research-department",
        **{k: v for k, v in kwargs.items() if k != "instructions"},
        instructions=(
            "리서치본부(RES)의 도구 면이다. 시세·Evidence 조회와 Research Packet "
            "생성만 한다. 주문 제출·리스크 판정·원장 기록은 이 본부의 권한이 "
            "아니며 여기에 도구도 없다. 도구 결과는 결정론 산출물이므로 그대로 "
            "인용하고, 수치를 다시 계산하거나 지어내지 않는다."),
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
        description="기획안을 발행 게이트에 제출한다. 기획자 산출과 **독립 회의론자** "
                    "산출을 함께 넣어라 - 서명이 기획자와 같은 실행이면 거부된다. "
                    "근거 리드는 원장에서 다시 읽어 대조하므로 없는 리드를 대면 막힌다. "
                    "차단 사유를 받으면 그 사유에 답해 다시 제출하라.")
    def factory_submit_proposal(planner_text: str, skeptic_text: str,
                                planner_run: str, skeptic_run: str,
                                planner_prompt: str = "",
                                skeptic_prompt: str = "") -> dict:
        _factory_path()
        import proposal_intake as PI

        from datetime import datetime as _dt, timezone as _tz

        conn = _db()
        try:
            wanted = {i for b in PI.parse_blocks(planner_text, PI.PLANNER_KEYS)
                      for i in PI._split(b.get("LEAD_IDS", ""))}
            leads = PI.load_leads(conn, sorted(wanted))
            missing = sorted(wanted - set(leads))
            r = PI.intake(planner_text, skeptic_text,
                          case_id=f"plan-{_dt.now(_tz.utc):%Y%m%d}",
                          planner_run=planner_run, skeptic_run=skeptic_run,
                          leads=leads)
            for prop, _g in r.proposals:
                setattr(prop, "_planner_prompt", planner_prompt)
                setattr(prop, "_skeptic_prompt", skeptic_prompt)
            pub = r.publishable
            new = dup = 0
            if pub:
                new, dup = PI.persist(conn, pub)
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
    forbidden = {"submit_order", "oms_submit", "write_ledger", "risk_decision",
                 "promote_strategy"}
    assert not (names & forbidden), f"권한 밖 도구가 노출됐다: {names & forbidden}"
    print(f"  도구 면·권한 경계        OK ({len(names)}개)")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--serve" in sys.argv:
        import uvicorn

        token = os.environ.get("MCP_RESEARCH_API_KEY", "").strip()
        srv = build_server(host="0.0.0.0", port=DEFAULT_PORT)
        s = getattr(srv, "settings", None)
        if s is not None:
            s.host, s.port = "0.0.0.0", DEFAULT_PORT
        if token:
            print(f"{MCP_VERSION}: 0.0.0.0:{DEFAULT_PORT}/mcp (Bearer 인증 켜짐)",
                  flush=True)
        else:
            # 잊은 것과 일부러 연 것을 구분되게 남긴다 - 조용한 무인증 금지
            print(f"{MCP_VERSION}: 0.0.0.0:{DEFAULT_PORT}/mcp "
                  f"⚠ MCP_RESEARCH_API_KEY 미설정 - 인증 없이 연다. compose "
                  f"네트워크 밖으로 노출하지 말 것", flush=True)
        uvicorn.run(build_app(srv, token=token or None),
                    host="0.0.0.0", port=DEFAULT_PORT, log_level="info")
        raise SystemExit(0)

    print(f"{MCP_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_symbol_guard()
    _check_bearer_auth()
    _check_job_lifecycle()
    _check_health_summary()
    _check_tool_surface()
    print("리서치 MCP 5개 영역 통과. 서버는 --serve")
